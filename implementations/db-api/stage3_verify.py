"""Model A integration verification against two disposable PostgreSQL 17 databases."""

import argparse
import asyncio
import copy
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/conformance"))
from checks import EvidenceMissing, check_result, expected_state, history_check, state_check, validate
from fault_proxy import ResponseRelay

FIXTURES = ROOT / "shared/fixtures"
URLS = {"X": "http://127.0.0.1:18201", "Y": "http://127.0.0.1:18202"}
PORTS = {"X": 15432, "Y": 15433}
ACTORS = {"X": "BANK_A", "Y": "BANK_B"}
T3_TIMEOUT, T3_HOLD = 5, 10


def fixture(path):
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def jsonl(path, rows):
    with path.open("x", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def wait_for(probe, reason, seconds=12, interval=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(interval)
    raise EvidenceMissing(reason)


class Lab:
    def __init__(self, directory, wsl_restart):
        self.directory = directory
        self.wsl_restart = wsl_restart
        self.programs = {}
        self.gates = {}
        self.passwords = {p: os.environ[f"XY_PG_{p}_PASSWORD"] for p in PORTS}
        self.tokens = {name: secrets.token_hex(32) for name in
                       ("BANK_A", "BANK_B", "BANK_C", "observer", "internal", "invalid")}

        class Relay(BaseHTTPRequestHandler):
            def do_POST(handler):
                handler.forward()

            def do_GET(handler):
                handler.forward()

            def forward(handler):
                path = handler.path
                data = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                headers = {"Authorization": handler.headers.get("Authorization", ""),
                           "Content-Type": "application/json"}
                gate = self.gates.get(path)
                if gate == "before":
                    handler.cut()
                    return
                try:
                    with httpx.Client(timeout=1, trust_env=False) as client:
                        response = client.request(handler.command, URLS["X"] + path,
                                                  headers=headers, content=data)
                except httpx.RequestError:
                    handler.cut()
                    return
                if gate == "after":
                    handler.cut()
                    return
                try:
                    handler.send_response(response.status_code)
                    handler.send_header("Content-Type", "application/json")
                    handler.send_header("Content-Length", str(len(response.content)))
                    handler.end_headers()
                    handler.wfile.write(response.content)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def cut(handler):
                handler.close_connection = True
                try:
                    handler.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        self.relay = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        Thread(target=self.relay.serve_forever, daemon=True).start()

    def connection(self, platform):
        name = f"model_a_{platform.lower()}"
        return psycopg.connect(host="127.0.0.1", port=PORTS[platform], dbname=name,
                               user=name, password=self.passwords[platform], connect_timeout=3)

    def start(self, platform):
        name = f"model_a_{platform.lower()}"
        with self.connection(platform) as connection:
            actual, version = connection.execute(
                "SELECT current_database(), current_setting('server_version_num')"
            ).fetchone()
            assert actual == name and int(version) // 10000 == 17
        env = dict(os.environ, PLATFORM=platform, DB_HOST="127.0.0.1", DB_PORT=str(PORTS[platform]),
                   DB_NAME=name, DB_USER=name, DB_PASSWORD=self.passwords[platform],
                   POC_BANK_A_TOKEN=self.tokens["BANK_A"], POC_BANK_B_TOKEN=self.tokens["BANK_B"],
                   POC_BANK_C_TOKEN=self.tokens["BANK_C"], POC_OBSERVER_TOKEN=self.tokens["observer"],
                   MODEL_A_INTERNAL_TOKEN=self.tokens["internal"],
                   MODEL_A_X_URL=f"http://127.0.0.1:{self.relay.server_port}",
                   MODEL_A_SCHEMA_DIR=str(ROOT / "shared/schemas"), PYTHONDONTWRITEBYTECODE="1")
        with (self.directory / f"api-{platform}.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                 "--port", URLS[platform].rsplit(":", 1)[1], "--no-access-log"],
                cwd=Path(__file__).parent, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        self.programs[platform] = process

        def healthy():
            if process.poll() is not None:
                raise EvidenceMissing(f"{platform} API exited with {process.returncode}")
            try:
                response = httpx.get(URLS[platform] + "/health/db", timeout=1, trust_env=False)
                return response.status_code == 200 and response.json()["database"] == name
            except httpx.RequestError:
                return False

        wait_for(healthy, f"{platform} API health check did not succeed")

    def stop(self, platform):
        process = self.programs.pop(platform)
        process.kill()
        process.wait(timeout=5)

    def close(self):
        for platform in list(self.programs):
            self.stop(platform)
        self.relay.shutdown()
        self.relay.server_close()

    def restart_database(self, platform):
        if not self.wsl_restart:
            raise EvidenceMissing("Disposable WSL database restart was not enabled")
        path = "/tmp/model-a-xy-pg17"
        program = f"{path}/pkg/usr/lib/postgresql/17/bin/pg_ctl"
        data = f"{path}/{platform.lower()}/data"
        command = ["wsl", "-d", "Ubuntu", "--", "runuser", "-u", "nobody", "--", "env",
                   f"LD_LIBRARY_PATH={path}/pkg/usr/lib/x86_64-linux-gnu", program,
                   "-D", data, "-m", "fast", "-w", "restart"]
        subprocess.run(command, check=True, timeout=30)


class Run:
    def __init__(self, lab, test_id, variant, input_name):
        self.lab = lab
        self.test_id = test_id
        self.variant = variant
        self.run_id = f"{test_id.lower()}-{variant}-{uuid4().hex[:10]}"
        self.fixture = fixture(f"initial/{input_name}.json")
        self.input_name = input_name
        self.repo = fixture("commands/repo.json")
        self.loan = fixture("commands/loan.json")
        self.http = []
        self.snapshots = []
        self.faults = []
        self.outcomes = {}
        self.races = []
        self.directory = lab.directory / self.run_id
        self.directory.mkdir()

    def seed_s0(self):
        # Test-only S0 setup. Contracts and subsequent rights come from HTTP commands.
        for platform, kind in (("X", "REPO"), ("Y", "LOAN")):
            with self.lab.connection(platform) as connection:
                connection.execute("INSERT INTO runs(run_id) VALUES (%s)", (self.run_id,))
                for principal in self.fixture["principals"]:
                    connection.execute("INSERT INTO principals VALUES (%s,%s,%s,%s)",
                                       (self.run_id, principal["principal_id"], principal["actor"], principal["observer"]))
                for bundle in self.fixture["bundles"]:
                    bid = bundle["bundle_id"]
                    connection.execute("INSERT INTO bundles(run_id,bundle_id,security_id,quantity,reference_amount) VALUES (%s,%s,%s,%s,%s)",
                                       (self.run_id, bid, bundle["security_id"], bundle["quantity"]["value"],
                                        bundle["reference_value"]["amount"]))
                    if platform == "X":
                        connection.execute(
                            "INSERT INTO rights(run_id,bundle_id,created_version,holder,status,allowed_commands) "
                            "VALUES (%s,%s,0,%s,'AVAILABLE',%s)",
                            (self.run_id, bid, bundle["initial_holder"], Jsonb(["CONFIRM_REPO"])),
                        )
                for agreement in self.fixture["agreements"]:
                    if agreement["type"] == kind:
                        terms = {k: v for k, v in agreement.items() if k not in ("agreement_id", "type", "bundle_id")}
                        connection.execute("INSERT INTO agreements VALUES (%s,%s,%s,%s,%s)",
                                           (self.run_id, agreement["agreement_id"], agreement["bundle_id"], kind, Jsonb(terms)))

    def request(self, platform, path, schema, *, method="GET", role=None,
                command=None, params=None, url=None, timeout=5):
        attempt = uuid4().hex
        headers = {"X-Test-ID": self.test_id, "X-Attempt-ID": attempt}
        if role is not None:
            headers["Authorization"] = f"Bearer {self.lab.tokens[role]}"
        observed = {"attempt_id": attempt, "platform": platform, "method": method, "path": path,
                    "auth_role": role, "command": command, "params": params,
                    "sent_ns": str(time.monotonic_ns())}
        try:
            response = httpx.request(method, f"{url or URLS[platform]}/runs/{self.run_id}/{path}",
                                     headers=headers, json=command, params=params, timeout=timeout, trust_env=False)
        except httpx.RequestError as exc:
            observed.update(received_ns=str(time.monotonic_ns()), transport_error=type(exc).__name__)
            self.http.append(observed)
            raise
        body = response.json()
        observed.update(received_ns=str(time.monotonic_ns()), status=response.status_code, response=body)
        self.http.append(observed)
        validate(body, schema + ".schema.json")
        assert body["run_id"] == self.run_id
        return response.status_code, body

    def post(self, platform, command=None, *, replay=None, url=None, timeout=5):
        command = command or (self.repo if platform == "X" else self.loan)
        status, body = self.request(platform, "commands", "result", method="POST",
                                    role=ACTORS[platform], command=command, url=url, timeout=timeout)
        check_result(body, self.run_id, command, ACTORS[platform], status, replay)
        if body.get("outcome") in ("COMMITTED", "REJECTED"):
            self.outcomes[command["request_id"]] = (command, body)
        return body

    def resolved(self, platform, command=None, first=None):
        command = command or (self.repo if platform == "X" else self.loan)
        if first and first.get("outcome") in ("COMMITTED", "REJECTED"):
            return first

        def result():
            status, body = self.request(platform, "commands/" + command["request_id"], "result",
                                        role=ACTORS[platform])
            check_result(body, self.run_id, command, ACTORS[platform], status)
            if body.get("outcome") in ("COMMITTED", "REJECTED"):
                self.outcomes[command["request_id"]] = (command, body)
                return body

        return wait_for(result, "Original request did not reach a final outcome")

    def observe(self):
        snapshots = {p: self.request(p, "snapshot", "snapshot", role="observer")[1] for p in URLS}
        self.snapshots.extend(snapshots.values())
        return snapshots

    def state(self, version, *, loan_id="loan-001"):
        snapshots = self.observe()
        state_check(snapshots, self.run_id, expected_state(self.fixture, version, loan_id=loan_id),
                    [expected_state(self.fixture, i) for i in range(version)], platforms=("X", "Y"))
        return snapshots

    def ready(self, expected):
        status, body = self.request("Y", "readiness", "readiness", role="BANK_B", params={
            "command_type": self.loan["command_type"], "bundle_id": self.loan["bundle_id"],
            "agreement_id": self.loan["payload"]["agreement_id"],
        })
        assert status == 200 and body["ready"] is expected, body

    def confirm_repo(self):
        reply = self.post("X", replay=False)
        assert reply["outcome"] == "COMMITTED", reply
        return reply

    def events(self):
        result = []
        for platform in URLS:
            with self.lab.connection(platform) as connection:
                result.extend(row[0] for row in connection.execute(
                    "SELECT payload FROM events WHERE run_id=%s "
                    "ORDER BY payload->>'producer_id', (payload->>'sequence')::bigint", (self.run_id,),
                ))
        return result

    def event_pair(self, platform, request_id):
        return [event for event in self.events()
                if event["platform"] == platform and event["request_id"] == request_id
                and event["event_type"] in ("INTERNAL_COMMITTED", "LOCAL_APPLIED")]

    def assert_pending(self, consumed):
        state = self.observe()
        b = next(row for row in state["X"]["rights"] if row["holder"] == "BANK_B")
        assert b["status"] == ("CONSUMED" if consumed else "AVAILABLE")
        assert bool(state["X"]["pending_operations"]) is (not consumed)
        assert state["Y"]["bundles"][0]["version"] == 0 and not state["Y"]["rights"]
        assert any(row["blocks_use"] and row["request_id"] == self.loan["request_id"]
                   for row in state["Y"]["pending_operations"])

    @contextmanager
    def gate(self, phase, edge):
        path = f"/internal/runs/{self.run_id}/transfers/{phase}"
        self.lab.gates[path] = edge
        self.faults.append({"fault": f"disconnect-{phase}-{edge}", "start_ns": str(time.monotonic_ns())})
        try:
            yield
        finally:
            del self.lab.gates[path]
            self.faults[-1]["end_ns"] = str(time.monotonic_ns())

    @contextmanager
    def block_write(self, platform, table, operation, condition):
        name = sql.Identifier("xy_fault_" + uuid4().hex[:12])
        table_id = sql.Identifier(table)
        body = sql.SQL("BEGIN IF NEW.run_id = {} AND {} THEN RAISE EXCEPTION 'test-only blocked write'; END IF; RETURN NEW; END;")
        body = body.format(sql.Literal(self.run_id), sql.SQL(condition)).as_string()
        with self.lab.connection(platform) as connection:
            connection.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}").format(name, sql.Literal(body)))
            connection.execute(sql.SQL("CREATE TRIGGER {} BEFORE {} ON {} FOR EACH ROW EXECUTE FUNCTION {}()").format(
                name, sql.SQL(operation), table_id, name))
        self.faults.append({"fault": "blocked database write", "platform": platform,
                            "table": table, "condition": condition, "start_ns": str(time.monotonic_ns())})
        try:
            yield
        finally:
            with self.lab.connection(platform) as connection:
                connection.execute(sql.SQL("DROP TRIGGER {} ON {}").format(name, table_id))
                connection.execute(sql.SQL("DROP FUNCTION {}()").format(name))
            self.faults[-1]["end_ns"] = str(time.monotonic_ns())


def without_sequences(state):
    return {p: {k: v for k, v in body.items() if k != "snapshot_seq"} for p, body in state.items()}


def refusal(run, platform, variant):
    command = run.repo if platform == "X" else run.loan
    if platform == "Y" or variant == "other-result":
        run.confirm_repo()
    version = 1 if platform == "Y" or variant == "other-result" else 0
    if variant == "other-result" and platform == "Y":
        assert run.resolved("Y", first=run.post("Y"))["outcome"] == "COMMITTED"
        version = 2
    before = run.state(version)
    wrong_role = "BANK_C" if platform == "X" else "BANK_A"
    if variant == "snapshot-auth":
        status, body = run.request(platform, "snapshot", "snapshot", role="BANK_A")
        wanted = (403, "FORBIDDEN")
    elif variant == "other-result":
        status, body = run.request(platform, "commands/" + command["request_id"], "result", role=wrong_role)
        wanted = (403, "FORBIDDEN")
    else:
        role = {"missing-auth": None, "invalid-auth": "invalid", "forbidden": wrong_role}[variant]
        status, body = run.request(platform, "commands", "result", method="POST", role=role, command=command)
        wanted = (403, "FORBIDDEN") if variant == "forbidden" else (401, "UNAUTHENTICATED")
    assert (status, body["error"]["code"]) == wanted and set(body) == {"run_id", "request_id", "error"}
    assert without_sequences(before) == without_sequences(run.state(version))


def invalid(run, variant):
    command = fixture(f"invalid/{variant}.json") if variant != "wrong-platform" else run.repo
    platform = "X" if variant != "wrong-platform" else "Y"
    before = run.state(0)
    status, body = run.request(platform, "commands", "result", method="POST", role=ACTORS[platform], command=command)
    assert status == 400 and body["error"]["code"] == "INVALID_REQUEST"
    assert without_sequences(before) == without_sequences(run.state(0))


def condition(run, version, expected, *, stale=False):
    if version:
        run.confirm_repo()
    before = run.state(version)
    command = dict(run.loan, expected_version=0) if version == 0 or stale else run.loan
    reply = run.resolved("Y", command, run.post("Y", command))
    assert reply["outcome"] == "REJECTED" and reply["error"]["code"] in expected, reply
    assert without_sequences(before) == without_sequences(run.state(version))


def replay(run, platform):
    original = run.confirm_repo() if platform == "X" else None
    if platform == "Y":
        run.confirm_repo()
        original = run.resolved("Y", first=run.post("Y", replay=False))
    assert original["outcome"] == "COMMITTED"
    version = 1 if platform == "X" else 2
    before = run.state(version)
    for _ in range(2):
        assert run.post(platform, replay=True) == dict(original, replayed=True)
    assert without_sequences(before) == without_sequences(run.state(version))


def conflict(run):
    run.confirm_repo()
    original = run.resolved("Y", first=run.post("Y"))
    assert original["outcome"] == "COMMITTED"
    before = run.state(2)
    changed = dict(run.loan, payload=dict(run.loan["payload"], loan_id="conflicting-loan"))
    reply = run.post("Y", changed)
    assert "outcome" not in reply and reply["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert run.resolved("Y") == original
    assert without_sequences(before) == without_sequences(run.state(2))


def race(run, identical):
    run.confirm_repo()
    run.state(1)
    other = run.loan if identical else dict(run.loan, request_id="race-other",
                                             payload=dict(run.loan["payload"], loan_id="race-other-loan"))
    barrier = Barrier(2, timeout=3)

    def submit(command):
        barrier.wait()
        return run.resolved("Y", command, run.post("Y", command))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, (run.loan, other)))
    successes = [body for body in results if body["outcome"] == "COMMITTED"]
    if identical:
        assert len(successes) == 2 and successes[0]["effect"] == successes[1]["effect"]
    else:
        assert len(successes) == 1, results
        loser = next(body for body in results if body["outcome"] != "COMMITTED")
        assert loser["outcome"] == "REJECTED" and loser["error"]["code"] in ("RIGHT_UNAVAILABLE", "VERSION_CONFLICT")
    run.state(2, loan_id=successes[0]["effect"]["record_id"])
    run.races = [run.loan["request_id"], other["request_id"]]


def t3(run):
    run.confirm_repo()
    run.ready(True)

    async def observed():
        def note(kind):
            run.faults.append({"event": kind, "mono_ns": str(time.monotonic_ns())})

        note("FAULT_ACTIVATED")
        async with ResponseRelay(URLS["Y"], T3_HOLD, note) as relay:
            try:
                await asyncio.to_thread(run.post, "Y", url=relay.url, timeout=T3_TIMEOUT)
            except httpx.TimeoutException:
                note("CALL_TIMEOUT")
            else:
                raise EvidenceMissing("First submission did not time out under the response hold")
            reply = await asyncio.to_thread(run.resolved, "Y")
            assert reply["outcome"] == "COMMITTED"
            await asyncio.to_thread(run.state, 2)
            note("RECOVERY_COMPLETED")
            replayed = await asyncio.to_thread(run.post, "Y", replay=True)
            assert replayed == dict(reply, replayed=True)
        if not relay.response or relay.response["status_code"] != 200:
            raise EvidenceMissing("First held response did not provide committed evidence")
        original = json.loads(relay.response["body"])
        assert original == reply
        run.faults.append({"event": "VERIFIED_HELD_RESPONSE", "response": original, "status": 200})
        times = {r["event"]: int(r["mono_ns"]) for r in run.faults if "mono_ns" in r}
        assert times["RESPONSE_HELD"] < times["CALL_TIMEOUT"] < times["RECOVERY_COMPLETED"]
        assert times["FAULT_RELEASED"] - times["RESPONSE_HELD"] >= T3_HOLD * 1e9

    asyncio.run(observed())


def recovery(run, variant):
    run.confirm_repo()
    before = run.state(1)
    consumed = variant in ("lost-confirm", "restart-X-api", "restart-Y-db")
    edge = "after" if consumed else "before"
    before_restart = None
    with run.gate("confirm", edge):
        assert run.post("Y")["outcome"] == "PENDING"
        run.assert_pending(consumed)
        if variant.endswith("-api"):
            side = variant.split("-")[1]
            if side == "X":
                before_restart = run.events()
            run.lab.stop(side)
            run.lab.start(side)
            run.assert_pending(consumed)
            assert run.observe()[side]["snapshot_seq"] > before[side]["snapshot_seq"]
        elif variant.endswith("-db"):
            run.lab.restart_database(variant.split("-")[1])
            run.assert_pending(consumed)
    result = run.resolved("Y")
    assert result["outcome"] == "COMMITTED"
    assert run.post("Y", replay=True) == dict(result, replayed=True)
    run.state(2)
    if before_restart is not None:
        assert_x_restart_causality(run, before_restart)


def assert_x_restart_causality(run, before_restart):
    request = run.loan["request_id"]
    old = [row for row in before_restart if row["request_id"] == request]
    consumed = [row for row in old if row["event_type"] == "RIGHT_CONSUMED" and row["platform"] == "X"]
    published = [row for row in old if row["event_type"] == "MESSAGE_SENT" and row.get("message_kind") == "EVENT"]
    if len(consumed) != 1 or len(published) != 1:
        raise EvidenceMissing("The pre-restart right consumption and publication were not both retained")
    consumed, published = consumed[0], published[0]
    assert published["message_category"] == "PROTOCOL"
    assert consumed["clock_id"] == published["clock_id"] and consumed["sequence"] < published["sequence"]
    message_id = published["message_id"]
    assert not any(row["event_type"] == "MESSAGE_RECEIVED" and row.get("message_id") == message_id for row in old)

    rows = [row for row in run.events() if row["request_id"] == request]
    physical_sent = {
        row["message_id"] for row in rows if row["platform"] == "X" and row["event_type"] == "MESSAGE_SENT"
        and row.get("message_kind") == "RESPONSE" and row["clock_id"] != consumed["clock_id"]
    }
    created = next(row for row in rows if row["event_type"] == "RIGHT_CREATED")
    if not any(row["event_type"] == "MESSAGE_RECEIVED" and row["platform"] == "Y"
               and row.get("message_id") == message_id and row.get("message_kind") == "EVENT"
               and row["clock_id"] == created["clock_id"] and row["sequence"] < created["sequence"]
               for row in rows):
        raise EvidenceMissing("The durable confirmation was not received before successor creation")
    if not any(row["event_type"] == "MESSAGE_RECEIVED" and row.get("message_id") in physical_sent
               and row["platform"] == "Y" and row.get("message_kind") == "RESPONSE"
               for row in rows):
        raise EvidenceMissing("The restarted X process did not supply a separately identified HTTP response")
    assert message_id not in physical_sent
    assert sum(row["event_type"] == "MESSAGE_SENT" and row.get("message_id") == message_id for row in rows) == 1


def outbox_restart(run, platform):
    if platform == "Y":
        run.confirm_repo()
    command = run.repo if platform == "X" else run.loan
    request_id = command["request_id"]
    version = 1 if platform == "X" else 2
    with run.block_write(platform, "events", "INSERT", "NEW.payload->>'event_type' = 'LOCAL_APPLIED'"):
        first = run.post(platform)
        assert first.get("outcome") == "PENDING" or first["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
        result = run.resolved(platform)
        assert result["outcome"] == "COMMITTED"
        before = run.state(version)
        assert run.event_pair(platform, request_id) == []
        with run.lab.connection(platform) as connection:
            pending = connection.execute(
                "SELECT count(*) FROM commit_observations WHERE run_id=%s AND request_id=%s",
                (run.run_id, request_id),
            ).fetchone()[0]
        assert pending == 1
        run.lab.stop(platform)
    run.lab.start(platform)
    observations = wait_for(lambda: run.event_pair(platform, request_id), "Durable event pair was not published")
    assert sorted(row["event_type"] for row in observations) == ["INTERNAL_COMMITTED", "LOCAL_APPLIED"]
    assert run.resolved(platform) == result
    after = run.state(version)
    assert after[platform]["snapshot_seq"] > before[platform]["snapshot_seq"]
    assert without_sequences(before) == without_sequences(after)
    with run.lab.connection(platform) as connection:
        assert connection.execute(
            "SELECT count(*) FROM commit_observations WHERE run_id=%s", (run.run_id,),
        ).fetchone()[0] == 0


def normal(run):
    before = run.state(0)
    run.ready(False)
    run.ready(False)
    assert without_sequences(before) == without_sequences(run.state(0))
    original = run.confirm_repo()
    assert run.resolved("X") == original
    before = run.state(1)
    run.ready(True)
    run.ready(True)
    assert without_sequences(before) == without_sequences(run.state(1))
    loan = run.resolved("Y", first=run.post("Y", replay=False))
    assert loan["outcome"] == "COMMITTED" and run.resolved("Y") == loan
    run.state(2)


def phase2_fault(run, consumed):
    run.confirm_repo()
    target = ("Y", "rights", "INSERT", "NEW.created_version = 2") if consumed else (
        "X", "transfers", "UPDATE", "NEW.state = 'CONSUMED'")
    with run.block_write(*target):
        first = run.post("Y")
        assert first.get("outcome") == "PENDING" or first["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
        run.assert_pending(consumed)
    result = run.resolved("Y")
    assert result["outcome"] == "COMMITTED"
    assert run.post("Y", replay=True) == dict(result, replayed=True)
    run.state(2)


def phase2_abort(run):
    run.confirm_repo()
    with run.gate("reserve", "before"):
        assert run.post("Y")["outcome"] == "PENDING"
        # Simulate a change after acceptance in this supplemental isolated run.
        with run.lab.connection("Y") as connection:
            connection.execute(
                "UPDATE agreements SET terms=jsonb_set(terms,'{collateral_condition,provided}','false'::jsonb) "
                "WHERE run_id=%s AND kind='LOAN'", (run.run_id,),
            )
    result = run.resolved("Y")
    assert result["outcome"] == "REJECTED" and result["error"]["code"] == "PRECONDITION_NOT_MET"
    run.state(1)
    with run.lab.connection("X") as connection:
        assert connection.execute("SELECT state FROM transfers WHERE run_id=%s AND request_id=%s",
                                  (run.run_id, run.loan["request_id"])).fetchone() == ("RELEASED",)


def plan():
    for platform in URLS:
        for variant in ("missing-auth", "invalid-auth", "forbidden", "other-result", "snapshot-auth"):
            yield "AC-03", f"{variant}-{platform}", "f0-normal", partial(refusal, platform=platform, variant=variant)
    for variant in ("missing-bundle-id", "wrong-version-type", "extra-permission-field", "wrong-platform"):
        yield "AC-03", variant, "f0-normal", partial(invalid, variant=variant)
    for variant in ("no-reuse", "collateral-not-provided", "collateral-condition-failed"):
        code = "USE_NOT_PERMITTED" if variant == "no-reuse" else "PRECONDITION_NOT_MET"
        yield "AC-04", variant, variant, partial(condition, version=1, expected=(code,))
    yield "AC-05", "absent-Y", "f0-normal", partial(condition, version=0, expected=("PRECONDITION_NOT_MET", "RIGHT_UNAVAILABLE"))
    yield "AC-05", "stale-Y", "f0-normal", partial(condition, version=1, expected=("VERSION_CONFLICT",), stale=True)
    for platform in URLS:
        yield "AC-06", f"replay-{platform}", "f0-normal", partial(replay, platform=platform)
    yield "AC-07", "conflict", "f0-normal", conflict
    for test_id, identical in (("AC-08", True), ("AC-09", False)):
        for i in range(1, 11):
            yield test_id, f"race-{i:02}", "f0-normal", partial(race, identical=identical)
    for i in range(1, 6):
        yield "AC-11", f"hold-{i:02}", "f0-normal", t3
    for variant in ("lost-confirm", "restart-Y-api", "restart-X-api", "restart-X-db", "restart-Y-db"):
        yield "A-RECOVERY", variant, "f0-normal", partial(recovery, variant=variant)
    for platform in URLS:
        yield "A-OBSERVATION", f"outbox-restart-{platform}", "f0-normal", partial(outbox_restart, platform=platform)
    yield "XY-REGRESSION", "normal", "f0-normal", normal
    yield "XY-REGRESSION", "reserved-write-error", "f0-normal", partial(phase2_fault, consumed=False)
    yield "XY-REGRESSION", "consumed-write-error", "f0-normal", partial(phase2_fault, consumed=True)
    yield "XY-REGRESSION", "abort", "f0-normal", phase2_abort


def execute(run, scenario):
    functional = verdict = "NOT_RUN"
    error = None
    events = []
    try:
        run.seed_s0()
        scenario(run)
        functional = "PASS"
        if run.outcomes and any(body["outcome"] == "COMMITTED" for _, body in run.outcomes.values()):
            def observed():
                rows = run.events()
                committed = {req for req, (_, body) in run.outcomes.items() if body["outcome"] == "COMMITTED"}
                return rows if committed <= {e["request_id"] for e in rows if e["event_type"] == "LOCAL_APPLIED"} else None

            events = wait_for(observed, "Local commit observations did not drain")
        else:
            events = run.events()
        history_check(events, list(run.outcomes.values()), run.races)
        for request_id, (_, body) in run.outcomes.items():
            if body["outcome"] == "COMMITTED":
                assert sum(row["request_id"] == request_id and row["event_type"] == "RIGHT_CREATED"
                           for row in events) == 1
                assert sum(row["request_id"] == request_id and row["event_type"] == "RIGHT_CONSUMED"
                           for row in events) == 1
        verdict = "PASS"
    except EvidenceMissing as exc:
        verdict, error = "INCONCLUSIVE", str(exc)
    except (psycopg.OperationalError, httpx.RequestError, subprocess.SubprocessError) as exc:
        verdict, error = "INCONCLUSIVE", type(exc).__name__
    except Exception as exc:
        verdict, error = "FAIL", f"{type(exc).__name__}: {exc}"
        if functional == "NOT_RUN":
            functional = "FAIL"
    finally:
        if not events:
            try:
                events = run.events()
            except psycopg.Error:
                pass
        jsonl(run.directory / "events.jsonl", events)
        jsonl(run.directory / "snapshots.jsonl", run.snapshots)
        jsonl(run.directory / "http.jsonl", run.http)
        jsonl(run.directory / "faults.jsonl", run.faults)
    result = {"test_id": run.test_id, "variant": run.variant, "run_id": run.run_id,
              "fixture": run.input_name, "functional_verdict": functional, "verdict": verdict,
              "event_count": len(events), "reason": error}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New directory for sanitized evidence")
    parser.add_argument("--wsl-db-restarts", action="store_true",
                        help="Enable restart of the two disposable WSL clusters at the documented paths")
    parser.add_argument("--case", action="append", help="Choose a case ID without changing its repetitions")
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    lab = Lab(args.output, args.wsl_db_restarts)
    cases = [case for case in plan() if not args.case or case[0] in args.case]
    if not cases:
        parser.error("No matching cases")
    try:
        for platform in URLS:
            lab.start(platform)
        results = [execute(Run(lab, test_id, variant, input_name), verify)
                   for test_id, variant, input_name, verify in cases]
        (args.output / "summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n",
                                                  encoding="utf-8")
        return int(any(result["verdict"] != "PASS" for result in results))
    finally:
        lab.close()


if __name__ == "__main__":
    sys.exit(main())
