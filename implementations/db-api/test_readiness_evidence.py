import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import readiness_evidence
from readiness_evidence import EvidenceError, call_helper, collect


class ReadinessCollectionTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.args = SimpleNamespace(run_id="run-test", output=directory / "events.jsonl")
        self.proof = {"run_id": "run-test", "status": "COMPLETE", "producer_id": "z-first",
                      "clock_id": "clock-first", "in_flight": 0, "queued": 0, "writing": 0,
                      "generated": 1, "committed": 1, "event_ids": ["event-first"]}
        self.row = {"run_id": "run-test", "platform": "Z", "request_id": None,
                    "command_type": "ALLOCATE_MARGIN", "event_type": "MESSAGE_SENT",
                    "message_category": "BUSINESS_INTEGRATION", "event_id": "event-first",
                    "producer_id": "z-first", "clock_id": "clock-first"}
        self.control = self.enterContext(patch("readiness_evidence.control"))
        self.helper = self.enterContext(patch("readiness_evidence.call_helper", side_effect=self.export))

    def export(self, _args, _mode, _flag, candidate):
        candidate.write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        return {"run_id": "run-test", "status": "COLLECTED", "event_count": 1}

    def diagnostic(self):
        records = [p for p in self.args.output.parent.glob("*.readiness-*.json")
                   if not p.name.endswith(".publish-failure.json")]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(list(self.args.output.parent.glob("*.readiness-*.jsonl"))), 1)
        return json.loads(records[0].read_bytes())

    def run_cli(self):
        token_file = self.args.output.parent / "control-token"
        token_file.write_text(uuid4().hex, encoding="ascii")
        script = Path(readiness_evidence.__file__)
        argv = [str(script), "collect", "http://127.0.0.1:8103", str(token_file),
                str(script.with_name("collect_events.py")), "xyz", "--run-id", self.args.run_id,
                "--output", str(self.args.output)]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as stdout:
            status = readiness_evidence.main()
        return status, json.loads(stdout.getvalue())

    def test_waits_for_commit_and_publishes_existing_collector_format(self):
        self.control.side_effect = [self.proof | {"status": "PENDING", "writing": 1},
                                    self.proof, self.proof]
        link = os.link

        def publish_after_diagnostic(candidate, output):
            self.assertFalse(output.exists())
            self.assertEqual(self.diagnostic()["status"], "VERIFIED")
            link(candidate, output)

        with patch("readiness_evidence.os.link", side_effect=publish_after_diagnostic):
            self.assertEqual(self.run_cli(), (0, {"run_id": "run-test", "status": "COLLECTED", "event_count": 1}))
        self.helper.assert_called_once()
        self.assertEqual(json.loads(self.args.output.read_bytes()), self.row)
        self.assertEqual(self.diagnostic()["status"], "VERIFIED")

    def test_required_diagnostic_write_failure_never_publishes(self):
        self.control.return_value = self.proof
        open_path = Path.open

        def fail_diagnostic(path, mode="r", *args, **kwargs):
            if mode == "x" and path.suffix == ".json":
                raise PermissionError("cannot write diagnosis")
            return open_path(path, mode, *args, **kwargs)

        with patch.object(Path, "open", fail_diagnostic), patch("readiness_evidence.os.link") as link:
            self.assertEqual(self.run_cli(), (1, {"status": "FAILED", "reason": "DIAGNOSTIC_WRITE_FAILED"}))
        link.assert_not_called()
        self.assertFalse(self.args.output.exists())
        candidate, = self.args.output.parent.glob("*.readiness-*.jsonl")
        self.assertEqual(json.loads(candidate.read_bytes()), self.row)

    def test_failed_publication_preserves_verified_candidate_and_failure_diagnostic(self):
        self.control.return_value = self.proof
        with patch("readiness_evidence.os.link", side_effect=PermissionError):
            self.assertEqual(self.run_cli(), (1, {"status": "FAILED", "reason": "FINAL_PUBLICATION_FAILED"}))
        self.assertFalse(self.args.output.exists())
        self.assertEqual(self.diagnostic()["status"], "VERIFIED")
        failure, = self.args.output.parent.glob("*.publish-failure.json")
        self.assertEqual(json.loads(failure.read_bytes())["status"], "FAILED")

    def test_concurrent_final_output_creation_is_never_overwritten(self):
        self.control.return_value = self.proof
        link = os.link

        def conflicting_publish(candidate, output):
            output.write_text("evidence from the other publisher", encoding="utf-8")
            link(candidate, output)

        with patch("readiness_evidence.os.link", side_effect=conflicting_publish):
            self.assertEqual(self.run_cli(), (1, {"status": "FAILED", "reason": "FINAL_PUBLICATION_FAILED"}))
        self.assertEqual(self.args.output.read_text(encoding="utf-8"), "evidence from the other publisher")
        self.assertEqual(self.diagnostic()["status"], "VERIFIED")

    def test_control_client_cleanup_finishes_before_publication(self):
        self.control.return_value = self.proof
        http_client = MagicMock()
        http_client.__exit__.side_effect = OSError("connection cleanup failed")
        with patch("readiness_evidence.httpx.Client", return_value=http_client), patch(
                "readiness_evidence.os.link") as link:
            self.assertEqual(self.run_cli(), (1, {"status": "FAILED", "reason": "OSError"}))
        link.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_timeout_preserves_diagnostics_without_publishing(self):
        self.control.return_value = self.proof | {"status": "PENDING", "writing": 1}
        with patch("readiness_evidence.PROOF_TIMEOUT", 0):
            with self.assertRaisesRegex(EvidenceError, "READINESS_PROOF_TIMEOUT"):
                collect(self.args, None)
        self.assertFalse(self.args.output.exists())
        self.assertEqual(self.diagnostic()["last_proof"]["writing"], 1)

    def test_complete_response_after_deadline_is_not_accepted(self):
        self.control.return_value = self.proof
        with patch("readiness_evidence.PROOF_TIMEOUT", 0):
            with self.assertRaisesRegex(EvidenceError, "READINESS_PROOF_TIMEOUT"):
                collect(self.args, None)
        self.assertFalse(self.args.output.exists())
        self.assertEqual(self.diagnostic()["last_proof"]["status"], "COMPLETE")

    def test_loss_and_unknown_run_stop_final_collection(self):
        for reason in ("STORE_FAILED", "QUEUE_FULL", "UNKNOWN_RUN", "WRITER_STOPPED"):
            with self.subTest(reason=reason):
                directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
                self.args.output = directory / "events.jsonl"
                self.control.return_value = {"status": "INCOMPLETE", "reason": reason}
                with self.assertRaisesRegex(EvidenceError, reason):
                    collect(self.args, None)
                self.assertFalse(self.args.output.exists())
                self.assertEqual(self.diagnostic()["last_proof"]["reason"], reason)

    def test_database_export_must_contain_exact_committed_events(self):
        self.control.return_value = self.proof
        self.row["event_id"] = "unexpected-event"
        with self.assertRaisesRegex(EvidenceError, "COMMITTED_READINESS_EVENTS_MISSING_OR_UNEXPECTED"):
            collect(self.args, None)
        self.assertFalse(self.args.output.exists())
        self.assertEqual(self.diagnostic()["status"], "FAILED")

    def test_changed_proof_after_export_is_rejected(self):
        self.control.side_effect = [self.proof, {"status": "INCOMPLETE", "reason": "UNKNOWN_RUN"}]
        with self.assertRaisesRegex(EvidenceError, "READINESS_PROOF_CHANGED_DURING_COLLECTION"):
            collect(self.args, None)
        self.assertFalse(self.args.output.exists())
        self.assertEqual(self.diagnostic()["status"], "FAILED")

    def test_existing_final_output_is_never_overwritten(self):
        self.args.output.write_text("original evidence", encoding="utf-8")
        with self.assertRaisesRegex(EvidenceError, "OUTPUT_ALREADY_EXISTS"):
            collect(self.args, None)
        self.assertEqual(self.args.output.read_text(encoding="utf-8"), "original evidence")
        self.control.assert_not_called()
        self.helper.assert_not_called()

    def test_stock_collector_argv_and_credential_separation(self):
        helper = Path("implementations/db-api/collect_events.py")
        args = SimpleNamespace(helper=helper, stage="xyz", run_id="run-test")
        keys = ("MODEL_A_Z_EVIDENCE_DSN", "MODEL_A_Z_SEED_DSN", "MODEL_A_READINESS_EVIDENCE_TOKEN_FILE",
                "POC_BANK_C_TOKEN")
        result = {"run_id": "run-test", "status": "COLLECTED", "event_count": 1}
        with patch.dict(os.environ, dict.fromkeys(keys, "test-only")), patch(
            "readiness_evidence.subprocess.run", return_value=SimpleNamespace(
                stdout=json.dumps(result), returncode=0)) as child:
            self.assertEqual(call_helper(args, "COLLECTED", "--output", self.args.output), result)
        self.assertEqual(child.call_args.args[0][-6:], [str(helper), "xyz", "--run-id", "run-test",
                                                      "--output", str(self.args.output)])
        environment = child.call_args.kwargs["env"]
        self.assertIn(keys[0], environment)
        self.assertTrue(all(key not in environment for key in keys[1:]))
