"""Harness self-tests only: the local server/loader are not A or Canton models."""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import runner
from checks import ROOT, SCHEMAS, CheckError, initial_state, read_json, validate, validate_fixture
from jsonschema import Draft202012Validator


FIXTURE = ROOT / "shared/fixtures/initial/f0-normal.json"
S0 = read_json(ROOT / "shared/fixtures/expected/s0.json")
PREPARE = (
    "import json,sys,pathlib; "
    "a=sys.argv; assert pathlib.Path(a[a.index('--fixture')+1]).is_absolute(); "
    "print(json.dumps({'run_id':a[a.index('--run-id')+1],'status':'PREPARED'}))"
)


def snapshot(run_id, platform):
    return {"run_id": run_id, "platform": platform, "snapshot_seq": 0,
            "bundles": [{k: v for k, v in read_json(FIXTURE)["bundles"][0].items()
                         if k != "initial_holder"} | {"version": 0}],
            **{key: copy.deepcopy(S0[key]) for key in
               ("rights", "contracts", "obligations", "margin_calls", "allocations")},
            "pending_operations": []}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.requests = []
        self.snapshot_contexts = []
        self.mutate = lambda body: None
        self.http_status = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append((self.path, self.headers.get("Authorization")))
                test.snapshot_contexts.append((self.headers.get("X-Test-ID"), self.headers.get("X-Attempt-ID")))
                # Platform is supplied through a base URL path in each test config.
                body = snapshot(self.path.split("/")[3], self.path.split("/")[1])
                test.mutate(body)
                data = json.dumps(body).encode()
                self.send_response(test.http_status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = read_json(ROOT / "tests/conformance/config.example.json")
        self.config["platforms"] = {
            p: f"http://127.0.0.1:{self.server.server_port}/{p}" for p in "XYZ"}
        self.config["prepare_command"] = [sys.executable, "-c", PREPARE]
        self.config_path = self.path / "config.json"
        self.fixture_path = self.path / "fixture.json"
        self.fixture_path.write_text(FIXTURE.read_text())
        self.env = patch.dict(os.environ, {"POC_OBSERVER_TOKEN": "test-private-observer-credential",
                                          "POC_BANK_A_TOKEN": "test-private-bank-credential"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def execute(self, **kwargs):
        self.config_path.write_text(json.dumps(self.config))
        args = Namespace(config=str(self.config_path), fixture=str(self.fixture_path),
                         platforms=["X", "Y", "Z"], observer_auth="bearer",
                         output=str(self.path / "results"))
        for key, value in kwargs.items():
            setattr(args, key, value)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.run(args)
        result = json.loads(out.getvalue())
        batch = Path(result["batch"])
        return code, batch, read_json(batch / "checks.json")

    def test_schema_examples_and_invalid_inputs(self):
        for schema in SCHEMAS.values():
            Draft202012Validator.check_schema(schema)
        mapping = {"commands": "command", "results": "result", "readiness": "readiness",
                   "snapshots": "snapshot", "events": "event", "expected": "expectation",
                   "initial": "fixture"}
        for folder, schema in mapping.items():
            for path in (ROOT / "shared/fixtures" / folder).glob("*.json"):
                with self.subTest(path=path):
                    validate(read_json(path), schema + ".schema.json")
        for path in (ROOT / "shared/fixtures/invalid").glob("*.json"):
            with self.subTest(path=path), self.assertRaises(CheckError):
                validate(read_json(path), "command.schema.json")

    def test_prepare_success_and_evidence(self):
        code, batch, checks = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(checks["verdict"], "PASS")
        self.assertFalse(checks["final_handoff_eligible"])
        self.assertEqual(set(checks["acceptance"].values()), {"NOT_RUN"})
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(token == "Bearer test-private-observer-credential"
                            for _, token in self.requests))
        self.assertTrue(all(test_id == "PREPARE" for test_id, _ in self.snapshot_contexts))
        self.assertEqual(len({attempt for _, attempt in self.snapshot_contexts}), 3)
        for platform, (test_id, attempt) in zip("XYZ", self.snapshot_contexts):
            saved = read_json(batch / f"evidence/snapshot-{platform}.json")
            self.assertEqual((saved["test_id"], saved["attempt_id"]), (test_id, attempt))
            self.assertEqual((saved["method"], saved["platform"], saved["suffix"]), ("GET", platform, "snapshot"))
            self.assertRegex(attempt, r"^[0-9a-f]{32}$")
        validate(json.loads((batch / "events.jsonl").read_text()), "event.schema.json")
        manifest = read_json(batch / "manifest.json")
        self.assertEqual(manifest["fixture"]["sha256"], runner.digest(self.fixture_path))
        self.assertEqual(manifest["scope"], "prepare-only")

    def test_all_initial_fixture_variants(self):
        for path in (ROOT / "shared/fixtures/initial").glob("*.json"):
            with self.subTest(fixture=path.name):
                fixture = read_json(path)
                self.fixture_path.write_text(json.dumps(fixture))
                self.mutate = lambda body: body.update(margin_calls=fixture["margin_calls"])
                self.assertEqual(self.execute()[0], 0)

    def test_invalid_inputs_never_prepare(self):
        with patch.object(runner.subprocess, "run") as prepare:
            for key, bad in (("model", "C"), ("platforms", {}), ("auth_env", {}),
                             ("prepare_command", "echo unsafe"), ("evidence_dir", None)):
                original = self.config[key]
                self.config[key] = bad
                with self.subTest(key=key):
                    self.assertEqual(self.execute()[0], 1)
                self.config[key] = original
            self.fixture_path.write_text('{"fixture_id":"bad"}')
            self.assertEqual(self.execute()[0], 1)
            prepare.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_missing_observer_never_prepares(self):
        with patch.dict(os.environ, {"POC_OBSERVER_TOKEN": ""}), patch.object(
                runner.subprocess, "run") as prepare:
            self.assertEqual(self.execute()[0], 1)
            prepare.assert_not_called()

    def test_missing_loader_preserves_failure(self):
        self.config["prepare_command"] = [str(self.path / "absent-loader")]
        code, batch, checks = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(checks["checks"][-1]["reason"], "FileNotFoundError")
        self.assertTrue((batch / "evidence/prepare.incomplete").exists())
        self.assertEqual(self.requests, [])

    def test_fixture_reference_and_principal_validation(self):
        for change in (lambda f: f["agreements"][0].update(bundle_id="missing"),
                       lambda f: f["principals"][0].update(actor="BANK_C"),
                       lambda f: f["bundles"].append(f["bundles"][0])):
            fixture = read_json(FIXTURE)
            change(fixture)
            with self.assertRaises(CheckError):
                validate_fixture(fixture)
        self.fixture_path.write_text('{"fixture_id":"a","fixture_id":"b"}')
        with self.assertRaises(CheckError):
            read_json(self.fixture_path)

    def test_prepare_failures_stop_before_snapshot(self):
        scripts = ["print('not JSON')", "print('{}')", "print('[]')",
                   PREPARE + ";sys.exit(1)", PREPARE.replace("PREPARED", "FAILED"),
                   PREPARE.replace("a[a.index('--run-id')+1]", "'another-run'"),
                   PREPARE + ";print('{}')"]
        for script in scripts:
            with self.subTest(script=script):
                self.config["prepare_command"] = [sys.executable, "-c", script]
                code, batch, checks = self.execute()
                self.assertEqual(code, 1)
                self.assertEqual(checks["checks"][-1]["id"], "PREPARE")
                self.assertTrue((batch / "evidence/prepare.json").exists())
        self.assertEqual(self.requests, [])

    def test_corrupt_initial_states_fail(self):
        mutations = [lambda s: s["rights"][0].update(holder="BANK_B"),
                     lambda s: s["rights"][0].update(allowed_commands=["CONFIRM_LOAN"]),
                     lambda s: s["bundles"][0].update(version=1),
                     lambda s: s.update(rights=[]),
                     lambda s: s["rights"].append(s["rights"][0]),
                     lambda s: s.update(obligations=read_json(
                         ROOT / "shared/fixtures/expected/s1.json")["obligations"]),
                     lambda s: s.update(pending_operations=[{
                         "request_id": "unexpected", "bundle_id": "bundle-001",
                         "kind": "PROCESSING", "blocks_use": True}]),
                     lambda s: s.update(run_id="other"),
                     lambda s: s.update(platform="Z"),
                     lambda s: s["margin_calls"][0].update(eligibility_passed=False)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.mutate = mutation
                self.assertEqual(self.execute()[0], 1)

    def test_local_visibility_and_contradictory_replicas(self):
        fixture = read_json(FIXTURE)
        states = {p: snapshot("run-test", p) for p in "XYZ"}
        states["Y"].update(bundles=[], rights=[], margin_calls=[])
        states["X"]["margin_calls"] = []
        initial_state(fixture, states, "run-test", ["X", "Y", "Z"])
        states["Y"]["rights"] = copy.deepcopy(states["X"]["rights"])
        states["Y"]["rights"][0]["allowed_commands"] = []
        with self.assertRaises(CheckError):
            initial_state(fixture, states, "run-test", ["X", "Y", "Z"])

    def test_snapshot_http_failure_is_inconclusive(self):
        self.http_status = 403
        code, batch, checks = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(checks["verdict"], "INCONCLUSIVE")
        self.assertEqual(read_json(batch / "evidence/snapshot-X.json")["status_code"], 403)

    def test_credentials_redacted_in_loader_output(self):
        self.config["prepare_command"][-1] = (
            "import os,sys;print(os.environ['POC_OBSERVER_TOKEN']);"
            "print(os.environ['POC_BANK_A_TOKEN'],file=sys.stderr);sys.exit(1)")
        code, batch, _ = self.execute()
        self.assertEqual(code, 1)
        for path in batch.rglob("*"):
            if path.is_file():
                for secret in ("test-private-observer-credential", "test-private-bank-credential"):
                    self.assertNotIn(secret, path.read_text())
        self.assertIn("[REDACTED]", (batch / "evidence/prepare.json").read_text())

    def test_credentials_redacted_in_http_body(self):
        self.mutate = lambda body: body.update(debug=os.environ["POC_BANK_A_TOKEN"])
        code, batch, _ = self.execute()
        self.assertEqual(code, 1)
        self.assertNotIn(os.environ["POC_BANK_A_TOKEN"],
                         (batch / "evidence/snapshot-X.json").read_text())

    def test_xy_scope_does_not_require_z_visibility(self):
        self.mutate = lambda body: body.update(margin_calls=[])
        code, _, checks = self.execute(platforms=["X", "Y"])
        self.assertEqual(code, 0)
        self.assertFalse(checks["final_handoff_eligible"])
        self.assertEqual(len(self.requests), 2)

    def test_fresh_ids_and_collision_preserves_batch(self):
        _, first, _ = self.execute()
        _, second, _ = self.execute()
        self.assertNotEqual(first, second)
        self.assertNotEqual(read_json(first / "manifest.json")["run_id"],
                            read_json(second / "manifest.json")["run_id"])
        before = {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()}
        fake_uuid = type("Fixed", (), {"hex": first.name.removeprefix("batch-")})()
        with patch.object(runner.uuid, "uuid4", return_value=fake_uuid), self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual(before, {p.name: p.read_bytes() for p in first.iterdir() if p.is_file()})

    def test_total_http_deadline(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config["platforms"]["X"] = f"http://127.0.0.1:{server.server_port}"
        started = time.monotonic()
        with patch.object(runner, "HTTP_SECONDS", 0.1):
            code, _, checks = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(checks["verdict"], "INCONCLUSIVE")
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
