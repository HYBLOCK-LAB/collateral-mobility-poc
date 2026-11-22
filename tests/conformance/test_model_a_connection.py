"""Mapping and collector-hook tests; actual PostgreSQL verification is separate."""

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from checks import ROOT, CheckError, EvidenceMissing, read_json
from runner import Execution, collect_history, export_implementation_events, secret_values


class CollectorHookTests(unittest.TestCase):
    def test_collector_excludes_seed_and_api_credentials_and_redacts_dsns(self):
        credentials = {"MODEL_A_X_SEED_DSN": "postgresql://seed:seed-pass@localhost/model_a_x",
                       "MODEL_A_X_EVIDENCE_DSN": "postgresql://reader:read-pass@localhost/model_a_x",
                       "POC_BANK_A_TOKEN": "private-api-token", "CUSTOM_AUTH": "custom-api-token"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, credentials):
            config = {"evidence_dir": temp, "auth_env": {"bank-a": "CUSTOM_AUTH"}}
            execution = SimpleNamespace(config=config, run_id="run-test", path=Path(temp),
                                        secrets=secret_values(config))
            (Path(temp) / "evidence").mkdir()
            code = "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(list(credentials)) + "})); print(" + repr(list(credentials.values())) + ")"
            export_implementation_events([sys.executable, "-c", code], execution)
            record = read_json(Path(temp) / "evidence/collection.json")
            inherited = json.loads(record["stdout"].splitlines()[0])
            self.assertIsNone(inherited["MODEL_A_X_SEED_DSN"])
            self.assertIsNone(inherited["POC_BANK_A_TOKEN"])
            self.assertIsNone(inherited["CUSTOM_AUTH"])
            self.assertEqual(inherited["MODEL_A_X_EVIDENCE_DSN"], "[REDACTED]")
            self.assertTrue(all(value not in json.dumps(record) for value in credentials.values()))
            self.assertEqual(os.environ["MODEL_A_X_SEED_DSN"], credentials["MODEL_A_X_SEED_DSN"])

    def test_empty_file_is_valid_only_for_ac03_without_accepted_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "run-test").mkdir()
            (path / "run-test/events.jsonl").write_text("")
            (path / "evidence").mkdir()
            execution = SimpleNamespace(config={"evidence_dir": temp, "model": "A"},
                                        run_id="run-test",path=path,secrets=[],test_id="AC-03",
                                        results=[],race_requests=[])
            self.assertEqual(collect_history(execution), [])
            execution.test_id = "AC-02"
            self.assertEqual(collect_history(execution), [])
            execution.results = [(None, None)]
            with self.assertRaises(EvidenceMissing):
                collect_history(execution)
            execution.results = []
            execution.test_id = "AC-01"
            with self.assertRaises(EvidenceMissing):
                collect_history(execution)
            execution.test_id = "AC-03"
            (path / "run-test/events.jsonl").unlink()
            with self.assertRaises(EvidenceMissing):
                collect_history(execution)

    def test_collector_receives_run_and_output_and_failure_is_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            execution = SimpleNamespace(config={"evidence_dir": temp}, run_id="run-test",
                                        path=Path(temp), secrets=["private-value"])
            (Path(temp) / "evidence").mkdir()
            command = [sys.executable, "-c",
                       "import json,sys; print(json.dumps(sys.argv[1:])); print('private-value')"]
            export_implementation_events(command, execution)
            record = read_json(Path(temp) / "evidence/collection.json")
            args = json.loads(record["stdout"].splitlines()[0])
            self.assertEqual(args, ["--run-id", "run-test", "--output", str((Path(temp)/"run-test/events.jsonl").resolve())])
            self.assertNotIn("private-value", json.dumps(record))
            with self.assertRaises(EvidenceMissing):
                export_implementation_events([sys.executable, "-c", "raise SystemExit(1)"], execution)


class PreparedReplicaTests(unittest.IsolatedAsyncioTestCase):
    async def test_untouched_y_can_keep_previously_verified_s0_after_x_repo(self):
        from checks import expected_state
        with tempfile.TemporaryDirectory() as temp:
            fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
            config = read_json(ROOT / "tests/conformance/config.example.json")
            execution = Execution(config, fixture, Path(temp), "run-test", "AC-01", ["X", "Y"])
            s0, s1 = expected_state(fixture, 0), expected_state(fixture, 1)
            def snapshot(platform, state):
                return {"run_id":"run-test","platform":platform,"snapshot_seq":1,**state}
            y = snapshot("Y", s0)
            for key in ("rights","contracts","obligations","margin_calls"):
                y[key]=[]
            async def snapshots(): return {"X":snapshot("X",s1),"Y":y}
            execution.snapshots=snapshots
            try:
                await execution.state(1)
            finally:
                await execution.client.aclose()


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "Model A psycopg dependency not installed")
class ModelAMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("model_a_prepare", ROOT / "implementations/db-api/prepare.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_all_approved_xy_fixtures_preserve_terms_without_business_effects(self):
        for path in (ROOT / "shared/fixtures/initial").glob("*.json"):
            fixture = read_json(path)
            before = copy.deepcopy(fixture)
            x = self.module.seed_rows(fixture, "X")
            y = self.module.seed_rows(fixture, "Y")
            self.assertEqual(fixture, before)
            self.assertEqual(set(x), {"principals", "bundles", "agreements", "rights"})
            self.assertEqual(y["rights"], [])
            self.assertEqual(x["rights"], [(b["bundle_id"], "BANK_A") for b in fixture["bundles"]])
            for rows in (x, y):
                for agreement_id, bundle_id, kind, terms in rows["agreements"]:
                    self.assertIn(dict(agreement_id=agreement_id,bundle_id=bundle_id,type=kind,**terms),
                                  fixture["agreements"])

    def test_unrepresentable_fixture_is_rejected_without_coercion(self):
        fixture = read_json(ROOT / "shared/fixtures/initial/f0-normal.json")
        fixture["bundles"][0]["initial_holder"] = "BANK_B"
        with self.assertRaises(CheckError):
            self.module.seed_rows(fixture, "X")
        for run_id in ("..", "bad/id", ""):
            with self.assertRaises(CheckError):
                self.module.valid_run(run_id)

    def test_unsettled_collection_preserves_diagnostic_events_without_success(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        spec = importlib.util.spec_from_file_location("model_a_collector", ROOT / "implementations/db-api/collect_events.py")
        collector = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"prepare": self.module}):
            spec.loader.exec_module(collector)
        event = read_json(ROOT / "shared/fixtures/events/commit-observed.json")
        event.update(run_id="run-test", model="A", platform="X")

        @contextmanager
        def connect(platform, purpose):
            self.assertEqual(purpose, "EVIDENCE")
            connection = MagicMock()
            def execute(sql, params=None):
                reply = MagicMock()
                reply.fetchone.return_value = (1,) if ("FROM runs" in sql or "FROM requests" in sql) else None
                reply.fetchall.return_value = [(event,)] if platform == "X" else []
                return reply
            connection.execute.side_effect = execute
            yield connection
            self.assertIn("READ ONLY", connection.execute.call_args_list[0].args[0])

        with tempfile.TemporaryDirectory() as temp, patch.object(collector, "connect", connect):
            output = Path(temp) / "events.jsonl"
            with self.assertRaises(collector.Unsettled):
                collector.collect_once("run-test", output)
            self.assertEqual(json.loads(output.read_text()), event)
