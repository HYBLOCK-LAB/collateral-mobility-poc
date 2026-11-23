import hashlib
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checks import ROOT, CheckError, read_json
from report import durations, nearest_rank, report_batch, summary_rows
from runner import experiment_plan


def timing_events():
    base = read_json(ROOT / "shared/fixtures/events/commit-observed.json")
    keys = [("COMMIT_OBSERVED", "X", 0), ("READY_OBSERVED", "Y", 2),
            ("COMMAND_SUBMITTED", "Y", 3), ("COMMIT_OBSERVED", "Y", 7),
            ("READY_OBSERVED", "Z", 10), ("COMMAND_SUBMITTED", "Z", 12),
            ("COMMIT_OBSERVED", "Z", 20)]
    return [base | {"producer_id": "conformance-runner", "event_type": kind, "platform": platform,
                    "event_id": f"e-{i}", "sequence": i, "mono_ns": str(1_000_000_000+ms*1_000_000)}
            for i, (kind, platform, ms) in enumerate(keys)]


class ReportTests(unittest.TestCase):
    def test_harness_hops_have_distinct_labels_without_changing_message_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp)
            evidence = batch / "run/evidence"
            evidence.mkdir(parents=True)
            (batch / "manifest.json").write_text(json.dumps({"scope": "conformance", "model": "B1", "synthetic": True}))
            (batch / "checks.json").write_text(json.dumps({"runs": [dict(model="B1", run_id="run-msg",
                test_id="AC-01", variant="normal", repeat=1, path="run", verdict="PASS")]}))
            (evidence / "call-0001.json").write_text(json.dumps({"status_code": 200}))
            request = {"event_type": "MESSAGE_SENT", "message_category": "HARNESS",
                       "message_kind": "REQUEST", "message_id": "snapshot:1:req"}
            response = dict(request, event_type="MESSAGE_RECEIVED", message_kind="RESPONSE", message_id="snapshot:1:res")
            (evidence / "implementation-events.json").write_text(json.dumps({
                "body": "\n".join(json.dumps(event) for event in (request, request, response))}))
            before = {p: p.read_bytes() for p in batch.rglob('*') if p.is_file()}
            with patch("report.plot_figures"):
                report_batch(batch)
            with (batch / "complexity.csv").open() as source:
                rows = [r for r in csv.DictReader(source) if r["metric"] == "N_message"]
            counts = {r["kind"]: int(r["value"]) for r in rows}
            self.assertEqual(len(rows), 4)
            self.assertEqual(counts, {"HARNESS/REQUEST": 1, "HARNESS/RESPONSE": 1,
                                     "HARNESS/IMPLEMENTATION/REQUEST": 1, "HARNESS/IMPLEMENTATION/RESPONSE": 1})
            for path, data in before.items():
                self.assertEqual(path.read_bytes(), data)

    def test_per_run_intervals_and_clock_boundary(self):
        events = timing_events()
        self.assertEqual(durations(events), {"T_A": 5, "T_W": 3, "T_C": 12, "T_E": 20})
        events[-1]["mono_ns"] = "1"
        with self.assertRaises(CheckError):
            durations(events)
        events = timing_events()
        events[-1]["clock_id"] = "other-clock"
        with self.assertRaises(CheckError):
            durations(events)

    def test_nearest_rank_empty_and_no_sum_of_percentiles(self):
        self.assertIsNone(nearest_rank([], .95))
        self.assertEqual(nearest_rank(list(range(1, 31)), .95), 29)
        rows = [dict(model="A", suite="T1", phase="main", verdict="PASS", timing_valid=True,
                     T_A=a, T_W=0, T_C=c, T_E=a+c, T_R=None) for a, c in ((1, 100), (100, 1))]
        rows.append(rows[0] | {"phase": "warmup", "T_E": 99999})
        rows.append(rows[0] | {"verdict": "FAIL", "timing_valid": False, "T_E": 0})
        summary = summary_rows(rows)
        end = next(r for r in summary if r["metric"] == "T_E" and r["phase"] == "main")
        self.assertEqual((end["attempts"], end["n"], end["p95"], end["fail"]), (3, 2, 101, 1))
        self.assertEqual(next(r for r in summary if r["metric"] == "T_R")["p95"], None)

    def test_fixed_attempts_no_success_topup(self):
        for model in ("A", "B1", "B2"):
            plan = experiment_plan("T1", model)
            self.assertEqual(len(plan), 33)
            self.assertEqual(sum(c["warmup"] for c in plan), 3)
        self.assertEqual(len(experiment_plan("T3", "A")), 5)
        self.assertEqual(len(experiment_plan("T2", "B1")), 30)

    def test_report_regeneration_keeps_raw_and_null_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp)
            manifest = {"scope": "experiment", "synthetic": True, "configs": [{"model": "A"}],
                        "order": [{"config_index": 0, "case": experiment_plan("T1", "A")[0]}]}
            (batch / "manifest.json").write_text(json.dumps(manifest))
            (batch / "checks.json").write_text(json.dumps({"runs": [], "final_handoff_eligible": False}))
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in batch.iterdir()}
            rows, summary = report_batch(batch)
            self.assertEqual(rows[0]["verdict"], "NOT_RUN")
            self.assertIsNone(summary[0]["p50"])
            first = (batch / "summary.csv").read_bytes()
            figure_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (batch / "figures").iterdir()}
            report_batch(batch)
            self.assertEqual(first, (batch / "summary.csv").read_bytes())
            self.assertEqual(figure_hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                            for p in (batch / "figures").iterdir()})
            for name, sha in before.items():
                self.assertEqual(hashlib.sha256((batch / name).read_bytes()).hexdigest(), sha)
            self.assertEqual(len(list((batch / "figures").glob("*.png"))), 4)
            self.assertEqual(len(list((batch / "figures").glob("*.svg"))), 4)
