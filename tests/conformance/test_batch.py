import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from checks import ROOT, CheckError, EvidenceMissing, read_json
from runner import execute_case, experiment_batch, validated_for_experiment
from test_runner import PREPARE, snapshot


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_scenario_still_exports_history_and_keeps_original_reason(self):
        for error, verdict in ((CheckError("original business failure"), "FAIL"),
                               (EvidenceMissing("original observation failure"), "INCONCLUSIVE"),
                               (RuntimeError("unexpected failure"), "INCONCLUSIVE")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = read_json(ROOT / "tests/conformance/config.example.json")
                config["prepare_command"] = [sys.executable, "-c", PREPARE]
                config_path = root / "config.json"
                config_path.write_text(json.dumps(config))
                args = Namespace(config=str(config_path), observer_auth="bearer", platforms=["X", "Y"],
                                 output=temp, collect_command=["diagnostic-collector"])
                case = {"test_id": "AC-01", "variant": "normal", "fixture": "f0-normal", "repeat": 1}

                async def snapshots(config, platforms, run_id, *args):
                    return {p: snapshot(run_id, p) for p in platforms}

                def history(execution):
                    (execution.path / "evidence/diagnostic.json").write_text('{}')
                    raise EvidenceMissing("incomplete diagnostic history")

                with patch.dict(os.environ, {"POC_OBSERVER_TOKEN": "self-test-credential"}), patch(
                        "runner.collect_snapshots", snapshots), patch("runner.Execution.scenario", new=AsyncMock(side_effect=error)), patch(
                        "runner.export_implementation_events", side_effect=EvidenceMissing("collector unsettled")) as export, patch(
                        "runner.collect_history", side_effect=history) as archive:
                    row = await execute_case(args, config, case, root, 0)
                export.assert_called_once()
                archive.assert_called_once()
                self.assertEqual(row["verdict"], verdict)
                self.assertEqual(row["reason"], str(error) if isinstance(error, CheckError) else "RuntimeError")
                self.assertFalse(row["evidence_complete"])
                self.assertIn("evidence/diagnostic.json", row["evidence_sha256"])
                self.assertEqual(len(row["diagnostic_errors"]), 2)

    async def test_prepare_and_body_are_connected_without_nested_event_loops(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = read_json(ROOT / "tests/conformance/config.example.json")
            config["prepare_command"] = [sys.executable, "-c", PREPARE]
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            args = Namespace(config=str(config_path), observer_auth="bearer", platforms=["X", "Y"], output=temp)
            case = {"test_id": "AC-01", "variant": "normal", "fixture": "f0-normal", "repeat": 1, "warmup": False}

            async def snapshots(config, platforms, run_id, *args):
                return {p: snapshot(run_id, p) for p in platforms}

            with patch.dict(os.environ, {"POC_OBSERVER_TOKEN": "self-test-credential"}), patch(
                    "runner.collect_snapshots", snapshots), patch("runner.Execution.scenario", new=AsyncMock()), patch(
                    "runner.collect_history", side_effect=EvidenceMissing("actual target logs unavailable")):
                row = await execute_case(args, config, case, root, 0)
            self.assertEqual(row["functional_verdict"], "PASS")
            self.assertEqual(row["verdict"], "INCONCLUSIVE")
            self.assertFalse(row["evidence_complete"])
            self.assertTrue((root / row["path"] / "evidence/preparation-checks.json").exists())

    async def test_fixed_schedule_alternates_models_and_does_not_replace_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            configs = []
            for model in ("A", "B1"):
                config = read_json(ROOT / "tests/conformance/config.example.json")
                config["model"] = model
                path = root / (model+".json")
                path.write_text(json.dumps(config))
                configs.append(str(path))
            args = Namespace(config=configs, output=temp, suite="T1", observer_auth="bearer",
                             platforms=["X", "Y", "Z"], validation_batch=[], prerequisite_batch=[])

            async def execute(args, config, case, batch, index):
                return dict(case, model=config["model"], run_id=f"self-test-{index}", path=f"run-{index}",
                            schedule_index=index, verdict="FAIL" if index == 0 else "PASS")

            with patch("runner.execute_case", new=AsyncMock(side_effect=execute)) as run, patch(
                    "runner.validated_for_experiment", return_value=[]), patch("report.report_batch"), contextlib.redirect_stdout(io.StringIO()):
                code = await experiment_batch(args)
            self.assertEqual(code, 1)
            self.assertEqual(run.await_count, 66)
            batch = next(root.glob("batch-*"))
            manifest = read_json(batch / "manifest.json")
            self.assertEqual([i["config_index"] for i in manifest["order"][:4]], [0, 1, 1, 0])
            self.assertEqual(sum(i["case"]["warmup"] for i in manifest["order"]), 6)
            self.assertEqual(read_json(batch / "checks.json")["not_run"], 0)

    async def test_measurement_requires_real_current_validation(self):
        args = Namespace(validation_batch=[])
        self.assertTrue(validated_for_experiment(args, {"model": "A"}))
