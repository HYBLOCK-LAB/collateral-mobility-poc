import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from checks import ROOT, CheckError, EvidenceMissing, history_check, read_json
from runner import final_gate


def history_fixture():
    command = read_json(ROOT / "shared/fixtures/commands/repo.json")
    result = read_json(ROOT / "shared/fixtures/results/loan-committed.json")
    result.update(run_id="run-history", request_id=command["request_id"], actor="BANK_A",
                  command_type="CONFIRM_REPO", effect={"bundle_id": "bundle-001", "version_before": 0,
                    "version_after": 1, "record_type": "REPO", "record_id": "repo-001"})
    base = read_json(ROOT / "shared/fixtures/events/commit-observed.json")
    base.update(run_id="run-history", model="A", test_id="AC-02", producer_id="target-X",
                clock_id="target-X-clock", request_id=command["request_id"], actor="BANK_A",
                attempt_id="attempt-1", version_before=0, version_after=1, record_id="repo-001")
    rows = []
    for i, (kind, extra) in enumerate((
        ("RIGHT_CREATED", {"right_id": "bundle-001:right:0", "holder": "BANK_A", "right_status": "AVAILABLE"}),
        ("RIGHT_CONSUMED", {"right_id": "bundle-001:right:0", "holder": "BANK_A", "right_status": "CONSUMED"}),
        ("RIGHT_CREATED", {"right_id": "bundle-001:right:1", "holder": "BANK_B", "right_status": "AVAILABLE"}),
        ("INTERNAL_COMMITTED", {}), ("LOCAL_APPLIED", {}))):
        rows.append(base | {"event_id": f"history-{i}", "event_type": kind, "sequence": i,
                            "mono_ns": str(100+i), "platform": "X"} | extra)
    return command, result, rows


class HistoryTests(unittest.TestCase):
    def test_valid_transition_and_missing_internal_evidence(self):
        command, result, rows = history_fixture()
        history_check(rows, [(command, result)])
        with self.assertRaises(EvidenceMissing):
            history_check([e for e in rows if e["event_type"] != "INTERNAL_COMMITTED"], [(command, result)])

    def test_restoration_double_availability_and_wrong_status(self):
        command, result, rows = history_fixture()
        restored = rows[0] | {"event_id": "restored", "sequence": 5, "mono_ns": "105"}
        with self.assertRaises(CheckError):
            history_check(rows+[restored], [(command, result)])
        rows[1]["event_type"] = "RIGHT_CREATED"
        rows[1]["right_status"] = "AVAILABLE"
        with self.assertRaises(CheckError):
            history_check(rows, [(command, result)])

    def test_causal_message_can_bridge_clocks_without_subtracting_them(self):
        command, result, rows = history_fixture()
        created = rows[2]
        created.update(producer_id="target-Y", clock_id="target-Y-clock", sequence=1, mono_ns="1")
        with self.assertRaises(EvidenceMissing):
            history_check(rows, [(command, result)])
        sent = rows[3] | {"event_id": "sent", "event_type": "MESSAGE_SENT", "sequence": 2,
                         "mono_ns": "102", "message_id": "transfer-message", "message_kind": "EVENT",
                         "message_category": "BUSINESS_INTEGRATION", "source_id": "X", "destination_id": "Y"}
        received = sent | {"event_id": "received", "event_type": "MESSAGE_RECEIVED",
                           "producer_id": "target-Y", "clock_id": "target-Y-clock", "sequence": 0, "mono_ns": "0"}
        history_check(rows+[sent, received], [(command, result)])

    def test_same_request_overlap_needs_distinct_overlapping_attempts(self):
        command, result, rows = history_fixture()
        for i, (kind, attempt) in enumerate((("COMMAND_RECEIVED", "a"), ("COMMAND_RECEIVED", "b"),
                                            ("COMMAND_FINISHED", "a"), ("COMMAND_FINISHED", "b")), 5):
            rows.append(rows[3] | {"event_id": f"race-{i}", "sequence": i, "mono_ns": str(100+i),
                                  "event_type": kind, "attempt_id": attempt})
        history_check(rows, [(command, result)], [command["request_id"]]*2)
        rows[-3]["mono_ns"], rows[-2]["mono_ns"] = "109", "106"
        rows[-3]["sequence"], rows[-2]["sequence"] = 8, 6
        rows[-1]["sequence"], rows[-1]["mono_ns"] = 9, "110"
        with self.assertRaises(EvidenceMissing):
            history_check(rows, [(command, result)], [command["request_id"]]*2)

    def test_final_gate_accepts_complete_content_then_rejects_tampering(self):
        command, result, events = history_fixture()
        case = {"test_id": "AC-02", "variant": "ready-Y-1", "repeat": 1}
        with tempfile.TemporaryDirectory() as temp:
            batch = Path(temp)
            path = batch / "run"
            (path / "evidence").mkdir(parents=True)
            (path / "manifest.json").write_text(json.dumps({"run_id": "run-history", "model": "A"}))
            finish = events[-1] | {"event_type": "RUN_FINISHED", "verdict": "PASS"}
            (path / "events.jsonl").write_text(json.dumps(finish)+"\n")
            (path / "evidence/implementation-events.json").write_text(json.dumps({
                "body": "\n".join(json.dumps(e) for e in events)}))
            row = dict(case, run_id="run-history", model="A", path="run", verdict="PASS",
                       evidence_complete=True, command_results=[[command, result]], race_requests=[])
            row["evidence_sha256"] = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in path.rglob("*") if p.is_file()}
            (path / "checks.json").write_text(json.dumps(row))
            self.assertEqual(final_gate("A", [row], [case], batch), [])
            (path / "evidence/implementation-events.json").write_text("{}")
            self.assertTrue(final_gate("A", [row], [case], batch))

    def test_final_gate_empty_history_matches_collection_rules(self):
        command, _, events = history_fixture()
        for test_id, accepted, allowed in (("AC-02", [], True), ("AC-03", [], True),
                                           ("AC-01", [], False), ("AC-02", [[{}, {}]], False),
                                           ("AC-02", "missing", False), ("AC-03", None, False),
                                           ("AC-02", {}, False), ("AC-02", [[command, None]], False)):
            with self.subTest(test_id=test_id, accepted=accepted), tempfile.TemporaryDirectory() as temp:
                batch = Path(temp)
                path = batch / "run"
                (path / "evidence").mkdir(parents=True)
                (path / "manifest.json").write_text(json.dumps({"run_id": "run-history", "model": "A"}))
                finish = events[-1] | {"event_type": "RUN_FINISHED", "verdict": "PASS"}
                (path / "events.jsonl").write_text(json.dumps(finish)+"\n")
                (path / "evidence/implementation-events.json").write_text(json.dumps({"body": ""}))
                case = {"test_id": test_id, "variant": "empty", "repeat": 1}
                row = dict(case, run_id="run-history", model="A", path="run", verdict="PASS",
                           evidence_complete=True, command_results=accepted, race_requests=[])
                if accepted == "missing":
                    del row["command_results"]
                row["evidence_sha256"] = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in path.rglob("*") if p.is_file()}
                (path / "checks.json").write_text(json.dumps(row))
                self.assertEqual(not final_gate("A", [row], [case], batch), allowed)
