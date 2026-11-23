import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from checks import ROOT, CheckError, EvidenceMissing, read_json, reassignment_check
from runner import case_plan, prerequisites
from argparse import Namespace


def examples():
    base = read_json(ROOT / "shared/fixtures/events/commit-observed.json")
    events, contracts = [], []
    for i in range(2):
        contract = {"contract_id": f"contract-{i}", "reason": "successor lending dependency",
                    "business_refs": [f"business-{i}"], "dependencies": []}
        for j, (kind, ms) in enumerate((("UNASSIGNMENT_STARTED", i*2), ("SOURCE_UNUSABLE", i*2+1),
                                      ("UNASSIGNMENT_COMPLETED", i*2+2),
                                      ("ASSIGNMENT_COMPLETED", i*2+5), ("TARGET_USABLE", i*2+6))):
            event = base | {"run_id": "run-b2", "model": "B2", "event_id": f"e-{i}-{j}",
                            "event_type": kind, "contract_id": contract["contract_id"],
                            "reassignment_id": f"ra-{i}", "source_synchronizer": "S1",
                            "target_synchronizer": "S2", "sequence": i*5+j,
                            "mono_ns": str(1_000_000_000+ms*1_000_000)}
            events.append(event)
            if kind in ("UNASSIGNMENT_COMPLETED", "ASSIGNMENT_COMPLETED"):
                key = "unassigned_evidence" if kind == "UNASSIGNMENT_COMPLETED" else "assigned_evidence"
                contract[key] = {"event_id": event["event_id"], "path": event["event_id"]+".json", "sha256": "f"*64}
        contracts.append(contract)
    calls = []
    for i, operation in enumerate(("UNASSIGN", "ASSIGN")):
        event = base | {"run_id": "run-b2", "model": "B2", "event_id": f"api-event-{i}",
                        "event_type": "MESSAGE_SENT", "message_id": f"api-{i}",
                        "message_kind": "REQUEST", "message_category": "BUSINESS_INTEGRATION",
                        "source_id": "app", "destination_id": "ledger", "sequence": 10+i,
                        "mono_ns": str(1_010_000_000+i*1_000_000)}
        events.append(event)
        calls.append({"call_id": f"api-{i}", "operation": operation, "retry_of": None,
                      "contract_ids": [c["contract_id"] for c in contracts],
                      "evidence": {"event_id": event["event_id"], "path": event["event_id"]+".json", "sha256": "a"*64}})
    return {"run_id": "run-b2", "synchronizers": {"S1": "actual-source", "S2": "actual-target"},
            "required_contracts": contracts, "api_calls": calls}, events


class ReassignmentTests(unittest.TestCase):
    def test_development_final_and_measurement_have_separate_prerequisites(self):
        config = {"model": "B2"}
        development = Namespace(final=False, prerequisite_batch=[])
        self.assertEqual(prerequisites(development, config), [])
        self.assertTrue(prerequisites(development, config, measurement=True))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (ROOT / "spec").glob("*.md")}
            paths = []
            for model in ("A", "B1"):
                path = root / model
                path.mkdir()
                (path / "manifest.json").write_text(json.dumps({"scope": "conformance",
                    "model": model, "source_sha256": spec}))
                (path / "checks.json").write_text(json.dumps({"final_handoff_eligible": True, "runs": []}))
                paths.append(str(path))
            final = Namespace(final=True, prerequisite_batch=paths)
            # The existing final_gate validates complete per-model evidence; test scheduling here.
            with patch("runner.final_gate", return_value=[]):
                self.assertTrue(prerequisites(Namespace(final=True, prerequisite_batch=paths[:1]), config))
                self.assertEqual(prerequisites(final, config), [])
                self.assertTrue(prerequisites(final, config, measurement=True))
                measured = root / "T1"
                measured.mkdir()
                (measured / "manifest.json").write_text(json.dumps({"scope": "experiment", "source_sha256": spec}))
                rows = [dict(model=model, suite="T1", run_id=f"{model}-{i}", warmup=i < 3, verdict="PASS")
                        for model in ("A", "B1") for i in range(33)]
                (measured / "checks.json").write_text(json.dumps({"runs": rows}))
                final.prerequisite_batch = paths + [str(measured)]
                with patch("report.run_values", return_value={"timing_valid": True}):
                    self.assertEqual(prerequisites(final, config, measurement=True), [])
                with patch("runner.final_gate", return_value=["missing raw evidence"]):
                    self.assertTrue(prerequisites(final, config))
                (root / "B1/checks.json").write_text(json.dumps({"final_handoff_eligible": False, "runs": []}))
                self.assertTrue(prerequisites(final, config))
                (root / "B1/checks.json").write_text(json.dumps({"final_handoff_eligible": True, "runs": []}))
                stale = read_json(root / "B1/manifest.json")
                stale["source_sha256"][next(iter(spec))] = "0" * 64
                (root / "B1/manifest.json").write_text(json.dumps(stale))
                self.assertTrue(prerequisites(final, config))

    def test_contract_intervals_not_summed_as_total(self):
        manifest, events = examples()
        result = reassignment_check(manifest, events, "run-b2", ["business-0", "business-1"])
        self.assertEqual(result["N_RA"], 2)
        self.assertEqual(result["total_preparation_ms"], 7)
        self.assertEqual(sum(r["T_RA"] for r in result["contracts"]), 10)
        self.assertEqual(result["logical_boundary_transitions"], 1)
        self.assertEqual((result["api_requests"], result["api_batches"], result["api_retries"]), (2, 2, 0))

    def test_missing_contract_dependency_raw_mapping_or_wrong_clock(self):
        for mutation in (lambda m, e: m["required_contracts"].pop(),
                         lambda m, e: m["required_contracts"][0].update(dependencies=["missing"]),
                         lambda m, e: m["required_contracts"][0].pop("assigned_evidence"),
                         lambda m, e: m.update(api_calls=[]),
                         lambda m, e: m["api_calls"][0].update(retry_of=m["api_calls"][0]["call_id"]),
                         lambda m, e: e[0].update(clock_id="different-clock")):
            manifest, events = examples()
            mutation(manifest, events)
            with self.assertRaises(CheckError):
                reassignment_check(manifest, events, "run-b2", ["business-0", "business-1"])

    def test_cross_contract_clocks_do_not_create_global_interval(self):
        manifest, events = examples()
        for event in events[5:]:
            event["clock_id"] = "other-contract-clock"
        result = reassignment_check(manifest, events, "run-b2", ["business-0", "business-1"])
        self.assertIsNone(result["total_preparation_ms"])
        for event in events:
            event["observation"] = {"clock_id": "common-observer", "mono_ns": event["mono_ns"]}
        result = reassignment_check(manifest, events, "run-b2", ["business-0", "business-1"])
        self.assertEqual((result["total_preparation_ms"], result["total_preparation_basis"]), (7, "observer-receipt"))

    def test_reversed_order_rejected(self):
        manifest, events = examples()
        next(e for e in events if e["event_type"] == "TARGET_USABLE")["mono_ns"] = "1"
        with self.assertRaises(CheckError):
            reassignment_check(manifest, events, "run-b2", ["business-0", "business-1"])

    def test_b2_is_mandatory_and_requires_a_b1_evidence(self):
        plan = case_plan("B2")
        self.assertEqual(sum(c["test_id"] == "AC-12" for c in plan), 30)
        self.assertEqual({c["test_id"] for c in plan}, {"AC-01", "AC-02", "AC-12"})
        self.assertTrue(prerequisites(Namespace(prerequisite_batch=[]), {"model": "B2"}))
