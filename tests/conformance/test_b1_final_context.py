import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import b1_final_context as context
from checks import CheckError


class B1ContextTests(unittest.TestCase):
    def test_exclusive_redacted_output_preserves_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "context.json"
            context.exclusive_json(path, {"diagnostic": "secret-value"}, ["secret-value"])
            before = path.read_bytes()
            self.assertEqual(json.loads(before), {"diagnostic": "[REDACTED]"})
            with self.assertRaises(FileExistsError):
                context.exclusive_json(path, {"replacement": True}, [])
            self.assertEqual(path.read_bytes(), before)

    def test_source_mismatch_is_rejected_before_context_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "implementations/canton/adapter/server.py"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"modified source")
            with patch.object(context, "ROOT", root), patch.object(context, "capture", side_effect=[
                    "", "implementations/canton/adapter/server.py"]), patch.object(
                    context.subprocess, "run", return_value=type("Git", (), {"stdout": b"pinned source"})()):
                with self.assertRaisesRegex(CheckError, "differs from pinned"):
                    context.source_hashes()

    def test_missing_source_inventory_is_rejected(self):
        with patch.object(context, "capture", side_effect=["", ""]):
            with self.assertRaisesRegex(CheckError, "source list missing"):
                context.source_hashes()

    def test_wrong_checkout_and_model_are_rejected(self):
        with patch.object(context.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(CheckError, "B1 XYZ"):
                context.collect({"model": "A", "platforms": dict.fromkeys("XYZ")}, "limits", "policy", "network")
            with patch.dict(context.os.environ, {"CANTON_IMPL_DIR": "/different/checkout"}):
                with self.assertRaisesRegex(CheckError, "this checkout"):
                    context.collect({"model": "B1", "platforms": dict.fromkeys("XYZ")}, "limits", "policy", "network")

    def test_existing_context_stops_before_any_host_or_service_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "context.json"
            original.write_text("original evidence")
            config = root / "config.json"
            config.write_text(json.dumps({"evidence_dir": str(root)}))
            argv = ["context", "--config", str(config), "--resource-policy", "observed limits",
                    "--restart-policy", "observed policy", "--network-notes", "observed network"]
            with patch.object(context.sys, "argv", argv), patch.object(context, "validate_config"), patch.object(
                    context, "collect") as collect:
                self.assertEqual(context.main(), 1)
                collect.assert_not_called()
            self.assertEqual(original.read_text(), "original evidence")
