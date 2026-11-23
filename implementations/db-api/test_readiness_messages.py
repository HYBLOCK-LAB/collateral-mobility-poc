import json
import os
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

from allocation import AllocationWorkflow, ReadinessEventWriter


class ReadinessMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema_dir = Path(__file__).resolve().parents[2] / "shared/schemas"
        schemas = {path.stem.removesuffix(".schema"): json.loads(path.read_text(encoding="utf-8"))
                   for path in schema_dir.glob("*.schema.json")}
        registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema))
                                             for schema in schemas.values())
        cls.validators = {
            name: Draft202012Validator(schemas[name], registry=registry)
            for name in ("event", "readiness")
        }
        cls.tokens = {name: uuid4().hex for name in (
            "POC_BANK_A_TOKEN", "POC_BANK_B_TOKEN", "POC_BANK_C_TOKEN", "POC_OBSERVER_TOKEN",
            "MODEL_A_YZ_INTERNAL_TOKEN",
        )}
        directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        control_file = directory / "control"
        cls.control_token = uuid4().hex
        control_file.write_text(cls.control_token, encoding="ascii")
        with patch.dict(os.environ, cls.tokens | {
            "PLATFORM": "Z", "DB_NAME": "unused", "DB_USER": "unused", "DB_PASSWORD": "unused",
            "DB_HOST": "127.0.0.1", "MODEL_A_SCHEMA_DIR": str(schema_dir),
            "MODEL_A_READINESS_EVIDENCE_TOKEN_FILE": str(control_file),
        }):
            import app

        cls.api = app
        cls.addClassCleanup(app.workflow.peer.close)
        cls.addClassCleanup(app.engine.dispose)
        cls.addClassCleanup(app.readiness_event_engine.dispose)

    def setUp(self):
        self.writer = MagicMock()
        self.connection = self.writer.begin.return_value.__enter__.return_value
        self.business_engine = MagicMock()
        self.workflow = AllocationWorkflow(self.business_engine, "http://127.0.0.1",
                                           self.tokens["MODEL_A_YZ_INTERNAL_TOKEN"], self.writer)
        self.workflow.readiness_writer.start()
        self.addCleanup(self.workflow.readiness_writer.close)
        self.assertTrue(self.workflow.readiness_writer.register("run-readiness"))
        self.addCleanup(self.workflow.peer.close)
        self.client = TestClient(self.api.app)
        self.addCleanup(self.client.close)
        self.blocking_reason = self.enterContext(patch.object(self.workflow, "blocking_reason",
                                                              return_value=({"version": 0}, None)))
        self.enterContext(patch.object(self.api, "workflow", self.workflow))
        self.enterContext(patch.object(self.api, "consistent_read"))
        self.require_run = self.enterContext(patch.object(self.api.db, "require_run", return_value=True))
        self.enterContext(patch.object(self.api.db, "one", return_value={"actor": "BANK_C", "observer": False}))

    @contextmanager
    def y_peer(self, status=200, body=b'{"version":2,"block":null}', stall=None):
        received, release = Event(), Event()
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                received.set()
                if stall == "headers":
                    release.wait(2)
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if stall == "body":
                    release.wait(2)
                else:
                    self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                self.workflow.peer.base_url = httpx.URL(f"http://127.0.0.1:{server.server_port}")
                yield received, paths
            finally:
                release.set()
                server.shutdown()
                thread.join()

    def get_readiness(self, attempt="attempt-1", token=None, test_id="AC-02"):
        response = self.client.get("/runs/run-readiness/readiness", params={
            "bundle_id": "bundle-001", "margin_call_id": "margin-001", "command_type": "ALLOCATE_MARGIN",
        }, headers={
            "Authorization": "Bearer " + (self.tokens["POC_BANK_C_TOKEN"] if token is None else token),
            "X-Test-ID": test_id, "X-Attempt-ID": attempt,
        })
        self.validators["readiness"].validate(response.json())
        return response

    def events(self):
        self.workflow.readiness_writer.pending.join()
        events = []
        for call in self.connection.execute.call_args_list:
            statement = call.args[0]
            self.assertIs(statement.table, self.api.db.events)
            event = statement.compile().params["payload"]
            self.validators["event"].validate(event)
            self.assertTrue(all(secret not in json.dumps(event) for secret in self.tokens.values()))
            events.append(event)
        return events

    def assert_unavailable(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "TEMPORARILY_UNAVAILABLE")

    def test_success_records_one_pair_per_attempt(self):
        with self.y_peer() as (_, paths):
            for attempt in ("attempt-1", "attempt-2"):
                response = self.get_readiness(attempt=attempt)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ready"])
                self.assertEqual(response.json()["observed_version"], 2)
        self.assertEqual(paths, ["/internal/runs/run-readiness/ccp-rights/bundle-001"] * 2)
        events = self.events()
        self.assertEqual(len(events), 4)
        sealed = self.workflow.readiness_writer.seal("run-readiness")
        self.assertEqual(sealed["status"], "COMPLETE")
        self.assertEqual(sealed["event_ids"], sorted(e["event_id"] for e in events))
        self.assertEqual(len({event["message_id"] for event in events}), 4)
        self.assertEqual(len({event["clock_id"] for event in events}), 1)
        for attempt, sent, received in zip(("attempt-1", "attempt-2"), events[::2], events[1::2]):
            self.assertEqual([(e["event_type"], e["message_kind"], e["source_id"], e["destination_id"])
                              for e in (sent, received)],
                             [("MESSAGE_SENT", "REQUEST", "Z", "Y"), ("MESSAGE_RECEIVED", "RESPONSE", "Y", "Z")])
            self.assertLess(sent["sequence"], received["sequence"])
            self.assertLessEqual(int(sent["mono_ns"]), int(received["mono_ns"]))
            for event in (sent, received):
                self.assertEqual(event["attempt_id"], attempt)
                self.assertEqual(event["test_id"], "AC-02")
                self.assertEqual(event["message_category"], "BUSINESS_INTEGRATION")
                self.assertIsNone(event["request_id"])
                self.assertIsNone(event["record_id"])

    def test_http_errors_and_invalid_bodies_are_received(self):
        for status, body in ((404, b"missing"), (503, b"unavailable"), (200, b"not-json"),
                             (200, b'{"version":9,"block":null}')):
            with self.subTest(status=status, body=body):
                self.connection.reset_mock()
                with self.y_peer(status, body):
                    self.assert_unavailable(self.get_readiness())
                self.assertEqual([e["event_type"] for e in self.events()], ["MESSAGE_SENT", "MESSAGE_RECEIVED"])

    def test_timeouts_after_send_record_only_the_request(self):
        self.workflow.peer.timeout = httpx.Timeout(0.2)
        for stall in ("headers", "body"):
            with self.subTest(stall=stall):
                self.connection.reset_mock()
                with self.y_peer(stall=stall) as (received, _):
                    self.assert_unavailable(self.get_readiness())
                    self.assertTrue(received.is_set())
                self.assertEqual([e["event_type"] for e in self.events()], ["MESSAGE_SENT"])

    def test_connection_refused_records_nothing(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.workflow.peer.base_url = httpx.URL(f"http://127.0.0.1:{sock.getsockname()[1]}")
            self.assert_unavailable(self.get_readiness())
        self.writer.begin.assert_not_called()

    def test_failed_header_write_does_not_record_send(self):
        def fail(_path, *, extensions):
            trace = extensions["trace"]
            trace("http11.send_request_headers.started", {})
            error = httpx.WriteTimeout("header write incomplete")
            trace("http11.send_request_headers.failed", {"exception": error})
            raise error

        with patch.object(self.workflow.peer, "get", side_effect=fail):
            self.assert_unavailable(self.get_readiness())
        self.writer.begin.assert_not_called()

    def test_s3_and_authentication_rejection_do_not_call_y(self):
        self.blocking_reason.return_value = {"version": 3}, "RIGHT_UNAVAILABLE"
        with self.y_peer() as (_, paths):
            response = self.get_readiness()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["ready"])
            self.assertEqual(response.json()["observed_version"], 3)
            response = self.get_readiness(token="")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "UNAUTHENTICATED")
        self.assertEqual(paths, [])
        self.writer.begin.assert_not_called()

    def test_duplicate_trace_does_not_duplicate_messages(self):
        def respond(_path, *, extensions):
            trace = extensions["trace"]
            trace("http11.send_request_headers.started", {})
            trace("http11.send_request_headers.complete", {})
            trace("http11.send_request_headers.complete", {})
            return httpx.Response(200, json={"version": 2, "block": None},
                                  request=httpx.Request("GET", "http://127.0.0.1"))

        with patch.object(self.workflow.peer, "get", side_effect=respond):
            self.assertEqual(self.get_readiness(attempt="bad id", test_id="bad id").status_code, 200)
        events = self.events()
        self.assertEqual([e["event_type"] for e in events], ["MESSAGE_SENT", "MESSAGE_RECEIVED"])
        self.assertEqual({e["test_id"] for e in events}, {"MANUAL"})
        self.assertEqual(len({e["attempt_id"] for e in events}), 1)
        self.assertNotEqual(events[0]["attempt_id"], "bad id")

    def test_event_storage_error_does_not_change_success_or_failure(self):
        self.connection.execute.side_effect = SQLAlchemyError("hidden database detail")
        with self.assertLogs("allocation", level="WARNING") as warnings:
            with self.y_peer():
                self.assertTrue(self.get_readiness().json()["ready"])
            with self.y_peer(503):
                self.assert_unavailable(self.get_readiness())
            self.workflow.readiness_writer.pending.join()
        self.assertEqual(len(warnings.output), 2)
        self.assertNotIn("hidden database detail", "\n".join(warnings.output))
        self.assertEqual(self.workflow.readiness_writer.seal("run-readiness")["reason"], "STORE_FAILED")

    def test_readiness_returns_while_event_connection_pool_is_exhausted(self):
        pool = QueuePool(MagicMock(), pool_size=1, max_overflow=0, timeout=5)
        self.addCleanup(pool.dispose)
        checkout_started = Event()

        @contextmanager
        def acquire_connection():
            checkout_started.set()
            connection = pool.connect()
            try:
                yield self.connection
            finally:
                connection.close()

        self.writer.begin.side_effect = acquire_connection
        with self.y_peer(), ThreadPoolExecutor(max_workers=1) as requests:
            held = pool.connect()
            try:
                pending = requests.submit(self.get_readiness)
                self.assertTrue(checkout_started.wait(3))
                self.assertTrue(pending.result(timeout=2).json()["ready"])
                self.connection.execute.assert_not_called()
            finally:
                held.close()
        self.assertEqual(len(self.events()), 2)
        self.business_engine.begin.assert_not_called()

    def test_collection_can_precede_slow_commit(self):
        commit_started, release_commit = Event(), Event()
        visible_rows = []

        @contextmanager
        def delayed_commit():
            yield self.connection
            commit_started.set()
            if not release_commit.wait(5):
                raise SQLAlchemyError("synthetic slow commit")
            visible_rows.extend(call.args[0].compile().params["payload"]
                                for call in self.connection.execute.call_args_list)

        self.writer.begin.side_effect = delayed_commit
        with self.y_peer(), ThreadPoolExecutor(max_workers=1) as requests:
            try:
                pending = requests.submit(self.get_readiness)
                self.assertTrue(commit_started.wait(3))
                self.assertEqual(pending.result(timeout=2).status_code, 200)
                # A collector can only see committed rows, even after readiness has returned.
                self.assertEqual(visible_rows, [])
                proof = self.workflow.readiness_writer.seal("run-readiness")
                self.assertEqual((proof["status"], proof["writing"], proof["committed"]), ("PENDING", 2, 0))
            finally:
                release_commit.set()
        self.assertEqual(self.events(), visible_rows)
        self.assertEqual(len(visible_rows), 2)
        self.assertEqual(self.workflow.readiness_writer.seal("run-readiness")["status"], "COMPLETE")

    def test_full_queue_drops_a_batch_without_blocking_readiness(self):
        started, release = Event(), Event()
        writer = ReadinessEventWriter(self.writer, self.workflow.log, capacity=1)
        self.workflow.readiness_writer = writer
        writer.start()
        self.addCleanup(writer.close)
        self.assertTrue(writer.register("run-readiness"))

        def slow_write(_statement):
            started.set()
            if not release.wait(5):
                raise SQLAlchemyError("synthetic slow write")

        self.connection.execute.side_effect = slow_write
        with self.assertLogs("allocation", level="WARNING") as warnings:
            with self.y_peer(), ThreadPoolExecutor(max_workers=1) as requests:
                try:
                    self.assertEqual(requests.submit(self.get_readiness).result(timeout=2).status_code, 200)
                    self.assertTrue(started.wait(3))
                    for attempt in ("attempt-2", "attempt-3"):
                        response = requests.submit(self.get_readiness, attempt=attempt).result(timeout=2)
                        self.assertEqual(response.status_code, 200)
                finally:
                    release.set()
            events = self.events()
        self.assertEqual([e["attempt_id"] for e in events], ["attempt-1"] * 2 + ["attempt-2"] * 2)
        self.assertTrue(any("Dropped 1" in message for message in warnings.output))
        self.assertEqual(writer.seal("run-readiness")["reason"], "QUEUE_FULL")

    def test_sealed_run_rejects_late_evidence_and_restart_forgets_previous_run(self):
        writer = self.workflow.readiness_writer
        self.assertEqual(writer.seal("run-readiness")["status"], "COMPLETE")
        with self.y_peer():
            self.assertEqual(self.get_readiness().status_code, 200)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(writer.seal("run-readiness")["reason"], "QUERY_AFTER_SEAL")
        restarted = ReadinessEventWriter(self.writer, self.workflow.log)
        restarted.start()
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.seal("run-readiness"), {"status": "INCOMPLETE", "reason": "UNKNOWN_RUN"})

    def test_private_registration_only_allows_unseeded_runs_and_dedicated_token(self):
        base = "/internal/runs/fresh-run/readiness-evidence/"
        self.assertEqual(self.client.post(base + "register").status_code, 401)
        headers = {"Authorization": "Bearer " + self.control_token}
        self.assertEqual(self.client.post(base + "register", headers=headers).status_code, 409)
        unknown = self.client.post(base + "seal", headers=headers).json()
        self.assertEqual(unknown, {"status": "INCOMPLETE", "reason": "UNKNOWN_RUN"})
        self.require_run.return_value = False
        response = self.client.post(base + "register", headers=headers)
        self.assertEqual(response.json(), {"status": "REGISTERED", "run_id": "fresh-run"})
        complete = self.client.post(base + "seal", headers=headers).json()
        self.assertEqual((complete["status"], complete["event_ids"]), ("COMPLETE", []))


if __name__ == "__main__":
    unittest.main()
