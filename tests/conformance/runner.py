"""Common conformance checks, fixed experiments, evidence capture and offline reports."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import uuid
import copy
from datetime import datetime, timezone
from pathlib import Path

import httpx

from checks import (ROOT, CheckError, EvidenceMissing, canonical, check_result, expected_state,
                    history_check, initial_state, parse_json, read_json, state_check, validate,
                    validate_config, validate_fixture, reassignment_check)

HTTP_SECONDS = 5
READINESS_SECONDS = .01
RESULT_SECONDS = .05
RETRY_SECONDS = 1
OBSERVE_SECONDS = 60
FAULT_SECONDS = 10
CREDENTIAL_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "DSN")


def environment_info():
    return {"python": sys.version, "os": platform.platform(), "cpu": platform.processor(),
            "logical_cpus": os.cpu_count(), "dependencies": {p: importlib.metadata.version(p)
            for p in ("httpx", "jsonschema", "referencing", "matplotlib")}}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def secret_values(config=None):
    names = set(config.get("auth_env", {}).values()) if isinstance(config, dict) and isinstance(
        config.get("auth_env"), dict) else set()
    names.update(k for k in os.environ if any(s in k.upper() for s in CREDENTIAL_MARKERS))
    return sorted({os.environ[k] for k in names if isinstance(k, str) and os.environ.get(k)},
                  key=len, reverse=True)


def redact(value, secrets):
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
            value = value.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    return value


def write_json(path, value, secrets):
    path.write_text(json.dumps(redact(value, secrets), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


async def snapshot_get(client, url, token):
    # httpx's phase timeouts alone do not bound a slowly streaming response.
    async with asyncio.timeout(HTTP_SECONDS):
        return await client.get(url, headers={"Authorization": "Bearer " + token,
                                             "X-Test-ID": "PREPARE", "X-Attempt-ID": uuid.uuid4().hex})


async def collect_snapshots(config, platforms, run_id, token, batch, secrets):
    snapshots = {}
    async with httpx.AsyncClient(timeout=HTTP_SECONDS, trust_env=False,
                                follow_redirects=False) as client:
        for name in platforms:
            url = config["platforms"][name].rstrip("/") + f"/runs/{run_id}/snapshot"
            response = await snapshot_get(client, url, token)
            write_json(batch / "evidence" / f"snapshot-{name}.json", {
                "status_code": response.status_code, "body": response.text,
                "received_mono_ns": str(time.monotonic_ns()),
                "clock_id": "observer-" + run_id,
                "method": "GET", "platform": name, "suffix": "snapshot",
                "test_id": response.request.headers["X-Test-ID"],
                "attempt_id": response.request.headers["X-Attempt-ID"]}, secrets)
            if response.status_code != 200:
                raise RuntimeError(f"snapshot {name}: HTTP {response.status_code}")
            snapshots[name] = parse_json(response.text)
    return snapshots


def run(args, *, batch=None, run_id=None, quiet=False):
    # mkdir is exclusive; a collision never changes an earlier batch or run.
    run_id = run_id or "run-" + uuid.uuid4().hex
    batch_id = "batch-" + uuid.uuid4().hex
    batch = batch or Path(args.output).resolve() / batch_id
    batch.mkdir(parents=True, exist_ok=False)
    (batch / "evidence").mkdir()
    secrets = secret_values()
    manifest = {"batch_id": batch_id, "run_id": run_id, "scope": "prepare-only",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "platforms_observed": args.platforms, "observer_auth": args.observer_auth,
                "python": sys.version, "os": platform.platform(),
                "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                "dependencies": environment_info()["dependencies"],
                "source_sha256": {str(p.relative_to(ROOT)): digest(p) for directory in
                                  ("spec", "shared", "tests/conformance")
                                  for p in sorted((ROOT / directory).rglob("*"))
                                  if p.is_file() and p.suffix in (".py", ".json", ".md", ".txt")
                                  and p.name != "config.local.json" and not p.name.endswith(".local.json")},
                "spec_frozen": False, "model": None,
                "evidence_dir_collection": "NOT_RUN: implementation log format not connected",
                "measurement": "NOT_RUN: preparation is outside performance measurement"}
    checks = {"scope": "prepare-only", "run_id": run_id, "verdict": "INCONCLUSIVE",
              "final_handoff_eligible": False, "checks": [],
              "acceptance": {f"AC-{i:02}": "NOT_RUN" for i in range(1, 13)},
              "limitations": ["No transaction/acceptance/performance test executed",
                              "Snapshot cannot prove agreement/principal loading or authorization isolation",
                              "Sequential local snapshots are not an atomic global state"]}
    stage = "INPUT"
    verdict = "FAIL"
    try:
        config_path = Path(args.config).resolve()
        fixture_path = Path(args.fixture).resolve()
        manifest["config_sha256"] = digest(config_path)
        manifest["fixture"] = {"path": str(fixture_path), "sha256": digest(fixture_path)}
        config = read_json(config_path)
        validate_config(config)
        secrets = secret_values(config)
        # Credentials belong in environment variables, including for prepare_command.
        if any(secret in json.dumps(config) for secret in secrets):
            raise CheckError("config contains an environment credential value")
        manifest["config"] = config
        manifest["model"] = config["model"]
        fixture = read_json(fixture_path)
        validate_fixture(fixture)
        if any(secret in json.dumps(fixture) for secret in secrets):
            raise CheckError("fixture contains an environment credential value")
        write_json(batch / "evidence/fixture.json", fixture, secrets)
        token = os.environ.get(config["auth_env"]["experiment-observer"])
        if not token or "\r" in token or "\n" in token:
            raise CheckError("observer credential missing/invalid")
        checks["checks"].append({"id": stage, "verdict": "PASS"})
        stage = "PREPARE"
        verdict = "INCONCLUSIVE"
        write_json(batch / "manifest.json", manifest, secrets)
        # This subprocess is the caller-configured loader, never a shell command.
        with (batch / "evidence/prepare.incomplete").open("x"):
            completed = subprocess.run(
                [*config["prepare_command"], "--run-id", run_id, "--fixture", str(fixture_path)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        write_json(batch / "evidence/prepare.json", {
            "returncode": completed.returncode, "stdout": completed.stdout,
            "stderr": completed.stderr}, secrets)
        (batch / "evidence/prepare.incomplete").unlink()
        if digest(fixture_path) != manifest["fixture"]["sha256"]:
            raise CheckError("fixture changed during preparation")
        try:
            prepared = parse_json(completed.stdout)
        except (ValueError, TypeError):
            raise CheckError("prepare stdout is not one JSON object") from None
        if (not isinstance(prepared, dict) or completed.returncode != 0
                or prepared.get("run_id") != run_id or prepared.get("status") != "PREPARED"):
            raise CheckError("prepare exit/status/run_id mismatch or failure")
        checks["checks"].append({"id": stage, "verdict": "PASS",
                                 "evidence": ["evidence/prepare.json"]})
        stage = "INITIAL_STATE"
        snapshots = asyncio.run(collect_snapshots(config, args.platforms, run_id, token,
                                                batch, secrets))
        verdict = "FAIL"
        initial_state(fixture, snapshots, run_id, args.platforms)
        checks["checks"].append({"id": stage, "verdict": "PASS", "evidence": [
            f"evidence/snapshot-{p}.json" for p in args.platforms]})
        checks["verdict"] = "PASS"
    except KeyboardInterrupt:
        checks["checks"].append({"id": stage, "verdict": "INCONCLUSIVE", "reason": "INTERRUPTED"})
    except Exception as error:
        # Third-party exception messages may echo request headers or URLs.
        reason = str(error) if isinstance(error, CheckError) else type(error).__name__
        checks["checks"].append({"id": stage, "verdict": verdict, "reason": reason})
        checks["verdict"] = verdict
    finally:
        write_json(batch / "manifest.json", manifest, secrets)
        write_json(batch / "checks.json", checks, secrets)
        if manifest["model"]:
            event = {key: None for key in ("request_id", "attempt_id", "bundle_id", "record_id",
                                          "platform", "actor", "outcome", "error_code",
                                          "version_before", "version_after")}
            event.update(event_id="event-" + uuid.uuid4().hex, event_type="RUN_FINISHED",
                         model=manifest["model"], test_id="PREPARE", run_id=run_id,
                         producer_id="conformance-runner", clock_id="observer-" + run_id,
                         sequence=0, mono_ns=str(time.monotonic_ns()), verdict=checks["verdict"])
            validate(event, "event.schema.json")
            (batch / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    if not quiet:
        print(json.dumps({"batch": str(batch), "run_id": run_id, "scope": "prepare-only",
                          "verdict": checks["verdict"], "final_handoff_eligible": False}))
    return 0 if checks["verdict"] == "PASS" else 1


class Execution:
    """One isolated scenario; only the target implementation can change business state."""

    def __init__(self, config, fixture, path, run_id, test_id, platforms):
        self.config, self.fixture, self.path, self.run_id = config, fixture, path, run_id
        self.test_id, self.platforms = test_id, platforms
        self.secrets = secret_values(config)
        self.clock = "observer-" + run_id
        # Preparation already verified S0; an untouched peer may still expose it.
        self.events, self.results, self.previous = [], [], [expected_state(fixture, 0)]
        self.preacceptance_errors = []
        self.race_requests = []
        self.reassigned = False
        self.reassignment = None
        self.deadline = time.monotonic() + OBSERVE_SECONDS
        self.counter = 0
        self.roles = {"X": "bank-a", "Y": "bank-b", "Z": "bank-c"}
        self.actors = {"bank-a": "BANK_A", "bank-b": "BANK_B", "bank-c": "BANK_C"}
        self.commands = {p: read_json(ROOT / f"shared/fixtures/commands/{name}.json")
                         for p, name in (("X", "repo"), ("Y", "loan"), ("Z", "margin"))}
        self.client = httpx.AsyncClient(timeout=HTTP_SECONDS, follow_redirects=False, trust_env=False)

    def event(self, kind, platform=None, command=None, attempt=None, result=None, **fields):
        event = {key: None for key in ("request_id", "attempt_id", "bundle_id", "record_id",
                                      "platform", "actor", "outcome", "error_code",
                                      "version_before", "version_after")}
        event.update(event_id="event-" + uuid.uuid4().hex, event_type=kind,
                     model=self.config["model"], test_id=self.test_id, run_id=self.run_id,
                     producer_id="conformance-runner", clock_id=self.clock,
                     sequence=len(self.events), mono_ns=str(time.monotonic_ns()), platform=platform)
        if command:
            event.update(request_id=command["request_id"], attempt_id=attempt,
                         bundle_id=command["bundle_id"], command_type=command["command_type"],
                         actor=self.actors[self.roles[platform]])
        if result:
            event.update(outcome=result.get("outcome"),
                         error_code=(result.get("error") or {}).get("code"))
            if result.get("effect"):
                event.update({k: result["effect"][k] for k in
                              ("record_id", "version_before", "version_after")})
        event.update(fields)
        validate(event, "event.schema.json")
        self.events.append(event)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(redact(event, self.secrets)) + "\n")
        return event

    async def request(self, method, platform, suffix, role, *, command=None, params=None,
                      token_override=None, base_url=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceMissing("observation deadline reached")
        attempt = "attempt-" + uuid.uuid4().hex
        token = token_override if token_override is not None else os.environ.get(
            self.config["auth_env"][role])
        if token is None:
            raise EvidenceMissing("required role credential missing")
        headers = {"Authorization": "Bearer " + token} if token else {}
        # Correlation is diagnostic only; authorization still comes from the credential.
        headers["X-Attempt-ID"] = attempt
        headers["X-Test-ID"] = self.test_id
        url = (base_url or self.config["platforms"][platform]).rstrip("/")
        url += f"/runs/{self.run_id}/" + suffix
        self.counter += 1
        prefix = f"call-{self.counter:04}-{attempt}"
        sent = str(time.monotonic_ns())
        can_event = command and all(isinstance(command.get(k), str) for k in
                                    ("request_id", "bundle_id", "command_type"))
        if method == "POST" and can_event:
            self.event("COMMAND_SUBMITTED", platform, command, attempt, actor=self.actors[role])
        try:
            async with asyncio.timeout(min(HTTP_SECONDS, remaining)):
                response = await self.client.request(method, url, headers=headers,
                                                     json=command, params=params)
        except (httpx.TimeoutException, TimeoutError, httpx.TransportError) as error:
            write_json(self.path / "evidence" / (prefix + ".json"), {
                "method": method, "platform": platform, "suffix": suffix,
                "attempt_id": attempt, "command": command, "params": params,
                "sent_mono_ns": sent, "clock_id": self.clock,
                "error": type(error).__name__}, self.secrets)
            if can_event and isinstance(error, (httpx.TimeoutException, TimeoutError)):
                self.event("CALL_TIMEOUT", platform, command, attempt, outcome="UNKNOWN",
                           error_code="OUTCOME_UNKNOWN")
            return None, None, attempt
        received = str(time.monotonic_ns())
        write_json(self.path / "evidence" / (prefix + ".json"), {
            "method": method, "platform": platform, "suffix": suffix, "attempt_id": attempt,
            "command": command, "params": params, "status_code": response.status_code,
            "body": response.text, "sent_mono_ns": sent, "received_mono_ns": received,
            "clock_id": self.clock}, self.secrets)
        parsed = parse_json(response.text)
        if (method == "POST" and response.status_code in (400, 401, 403, 409)
                and isinstance(parsed, dict) and "outcome" not in parsed and parsed.get("error")):
            self.preacceptance_errors.append(parsed)
        return response.status_code, parsed, attempt

    async def submit(self, platform, command=None, *, role=None, replay=None, recover=True,
                     token_override=None, base_url=None):
        command = copy.deepcopy(command or self.commands[platform])
        role = role or self.roles[platform]
        status, body, attempt = await self.request("POST", platform, "commands", role,
                                                 command=command, token_override=token_override,
                                                 base_url=base_url)
        retries, last_retry = 0, time.monotonic()
        while True:
            if body is not None:
                check_result(body, self.run_id, command, self.actors[role], status, replay)
                temporary = "outcome" not in body and status in (404, 503)
                if body.get("outcome") in ("COMMITTED", "REJECTED") or "outcome" not in body and not temporary:
                    if body.get("outcome") in ("COMMITTED", "REJECTED"):
                        kind = "COMMIT_OBSERVED" if body["outcome"] == "COMMITTED" else "REJECTION_OBSERVED"
                        if not any(e["event_type"] == kind and e["request_id"] == command["request_id"]
                                   for e in self.events):
                            self.event(kind, platform, command, attempt, body, actor=self.actors[role])
                        self.results.append([command, body])
                    return body
            if not recover:
                return body
            await asyncio.sleep(RESULT_SECONDS)
            status, body, attempt = await self.request(
                "GET", platform, "commands/" + command["request_id"], role)
            self.event("RESULT_CHECKED", platform, command, attempt, body)
            # A 404 does not prove that the original request failed; retry its unchanged content.
            if body and (body.get("error") or {}).get("code") == "NOT_FOUND":
                if retries < 3 and time.monotonic() - last_retry >= RETRY_SECONDS:
                    self.event("COMMAND_RETRIED", platform, command, attempt)
                    status, body, attempt = await self.request("POST", platform, "commands", role,
                                                             command=command)
                    retries += 1
                    last_retry = time.monotonic()
                else:
                    body = None
            # A query is not a replay; only replay submissions must assert the replay flag.
            replay = None

    async def ready(self, platform, *, expected=None):
        if platform == "Y" and self.config["model"] == "B2" and expected is not False:
            await self.reassign()
        command = self.commands[platform]
        params = {"command_type": command["command_type"], "bundle_id": command["bundle_id"]}
        params.update({k: v for k, v in command["payload"].items() if k in
                       ("agreement_id", "margin_call_id")})
        while True:
            status, body, _ = await self.request("GET", platform, "readiness", self.roles[platform],
                                                params=params)
            if body is None:
                raise EvidenceMissing("readiness observation failed")
            validate(body, "readiness.schema.json")
            if status != 200 or "ready" not in body:
                raise EvidenceMissing("readiness HTTP/error response")
            if any(body[k] != v for k, v in {"run_id": self.run_id, "platform": platform,
                                           "bundle_id": command["bundle_id"],
                                           "command_type": command["command_type"]}.items()):
                raise CheckError("readiness identity mismatch")
            if expected is not None:
                if body["ready"] and body["observed_version"] != command["expected_version"]:
                    raise CheckError("readiness version mismatch")
                if body["ready"] is not expected:
                    raise CheckError("readiness condition mismatch")
                return body
            if body["ready"]:
                if body["observed_version"] != command["expected_version"]:
                    raise CheckError("readiness version mismatch")
                if not any(e["event_type"] == "READY_OBSERVED" and e["platform"] == platform
                           for e in self.events):
                    self.event("READY_OBSERVED", platform, bundle_id=command["bundle_id"])
                return body
            await asyncio.sleep(READINESS_SECONDS)

    async def snapshots(self):
        snapshots = {}
        for platform in self.platforms:
            status, body, _ = await self.request("GET", platform, "snapshot", "experiment-observer")
            if status != 200 or body is None:
                raise EvidenceMissing("snapshot observation failed")
            snapshots[platform] = body
        return snapshots

    async def state(self, version):
        expected = expected_state(self.fixture, version,
                                  repo_id=self.commands["X"]["payload"]["repo_id"],
                                  loan_id=self.commands["Y"]["payload"]["loan_id"],
                                  allocation_id=self.commands["Z"]["payload"]["allocation_id"],
                                  margin_call_id=self.commands["Z"]["payload"]["margin_call_id"])
        while True:
            snapshots = await self.snapshots()
            try:
                state_check(snapshots, self.run_id, expected, self.previous, self.platforms)
                self.previous.append(expected)
                return snapshots
            except EvidenceMissing:
                if time.monotonic() >= self.deadline:
                    raise
                await asyncio.sleep(RESULT_SECONDS)

    @staticmethod
    def committed(result):
        if not result or result.get("outcome") in (None, "PENDING", "UNKNOWN"):
            raise EvidenceMissing("committed result not yet established")
        if not result or result.get("outcome") != "COMMITTED":
            raise CheckError("expected committed result")

    async def advance(self, version):
        for number, platform in enumerate(("X", "Y", "Z"), 1):
            if number > version:
                break
            if platform != "X":
                await self.ready(platform)
            self.committed(await self.submit(platform))
            await self.state(number)

    async def reassign(self):
        if self.reassigned:
            return
        source = (ROOT / self.config["evidence_dir"] / self.run_id).resolve()
        manifest_path = source / "contracts.json"
        if not manifest_path.is_file():
            raise EvidenceMissing("B2 required contracts.json not connected")
        manifest = read_json(manifest_path)
        # Capture the selected set before issuing the reassignment request.
        write_json(self.path / "evidence/reassignment-selection.json", manifest, self.secrets)
        command = read_json(ROOT / "shared/fixtures/commands/reassignment.json")
        self.committed(await self.submit("X", command, role="bank-b"))
        events = collect_history(self)
        expected = expected_state(self.fixture, 1)
        refs = [r["right_id"] for r in expected["rights"] if r["status"] == "AVAILABLE"]
        # Output references may be appended by the collector; the selected IDs/reasons cannot change.
        final_manifest = read_json(manifest_path)
        selection = lambda m: [{k: v for k, v in c.items() if k not in
                                ("unassigned_evidence", "assigned_evidence")}
                               for c in m.get("required_contracts", [])]
        if (canonical(selection(manifest)) != canonical(selection(final_manifest))
                or manifest.get("synchronizers") != final_manifest.get("synchronizers")):
            raise CheckError("required contract set changed during reassignment")
        self.reassignment = reassignment_check(final_manifest, events, self.run_id, refs)
        references = [contract[name] for contract in final_manifest["required_contracts"]
                      for name in ("unassigned_evidence", "assigned_evidence")]
        references += [call["evidence"] for call in final_manifest["api_calls"]]
        for reference in references:
            path = (source / reference["path"]).resolve()
            if not path.is_relative_to(source) or not path.is_file() or digest(path) != reference["sha256"]:
                raise EvidenceMissing("raw Canton reassignment evidence missing/hash mismatch")
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                raise EvidenceMissing("raw Canton event empty")
            write_json(self.path / "evidence" / ("raw-" + reference["sha256"] + ".json"),
                       {"source": str(path), "sha256": reference["sha256"], "body": raw}, self.secrets)
        self.reassignment["command_submissions"] = sum(e["event_type"] == "COMMAND_SUBMITTED"
            and e.get("command_type") == "REASSIGN_FOR_LENDING" for e in self.events)
        write_json(self.path / "evidence/reassignment.json", {
            "manifest": final_manifest, "metrics": self.reassignment, "events": events}, self.secrets)
        await self.state(1)
        self.reassigned = True

    async def measured_flow(self):
        # Snapshot polling between commit and readiness would alter the measured boundary.
        self.previous = [expected_state(self.fixture, 0)]
        for number, platform in enumerate(("X", "Y", "Z"), 1):
            if platform != "X":
                await self.ready(platform)
            self.committed(await self.submit(platform))
            if number < 3:
                self.previous.append(expected_state(self.fixture, number))
        await self.state(3)

    async def race(self, platform, identical=False):
        first = copy.deepcopy(self.commands[platform])
        second = copy.deepcopy(first)
        if not identical:
            second["request_id"] += "-other"
            key = "loan_id" if platform == "Y" else "allocation_id"
            second["payload"][key] += "-other"
            if platform == "Z":
                second["payload"]["margin_call_id"] = self.fixture["margin_calls"][1]["margin_call_id"]
        barrier = asyncio.Event()

        async def call(command):
            await barrier.wait()
            return await self.submit(platform, command)

        tasks = [asyncio.create_task(call(c)) for c in (first, second)]
        barrier.set()
        results = await asyncio.gather(*tasks)
        successes = [i for i, result in enumerate(results) if result.get("outcome") == "COMMITTED"]
        if identical:
            if len(successes) != 2 or results[0]["effect"] != results[1]["effect"]:
                raise CheckError("same-request race did not resolve to one original effect")
        else:
            if len(successes) != 1:
                raise CheckError("race requires exactly one committed request")
            loser = results[1-successes[0]]
            if loser.get("outcome") != "REJECTED" or (loser.get("error") or {}).get("code") not in (
                    "VERSION_CONFLICT", "RIGHT_UNAVAILABLE"):
                raise CheckError("unexpected race loser")
            self.commands[platform] = (first, second)[successes[0]]
        self.race_requests = [first["request_id"], second["request_id"]]
        await self.state(2 if platform == "Y" else 3)

    async def scenario_xy(self, variant):
        if variant == "normal":
            await self.advance(2)
        elif variant == "no-reuse":
            await self.advance(1)
            result = await self.submit("Y")
            if result.get("outcome") != "REJECTED" or result["error"]["code"] != "USE_NOT_PERMITTED":
                raise CheckError("expected reuse refusal")
            await self.state(1)
        elif variant == "replay-Y":
            await self.advance(2)
            for _ in range(2):
                self.committed(await self.submit("Y", replay=True))
            await self.state(2)
        elif variant in ("race-same", "race-loan"):
            await self.advance(1)
            await self.race("Y", identical=variant == "race-same")
        else:
            raise CheckError("unknown X/Y scenario")

    async def refusal(self, platform, command=None, codes=(), preacceptance=False, **kwargs):
        result = await self.submit(platform, command, **kwargs)
        if not result or (result.get("error") or {}).get("code") not in codes:
            raise CheckError("expected refusal code missing")
        if result.get("outcome") != (None if preacceptance else "REJECTED"):
            raise CheckError("refusal outcome does not match acceptance boundary")
        return result

    async def unchanged_readiness(self, platform, version, expected):
        await self.advance(version)
        before = await self.state(version)
        for _ in range(2):
            await self.ready(platform, expected=expected)
        after = await self.snapshots()
        for name in self.platforms:
            if canonical({k: v for k, v in before[name].items() if k != "snapshot_seq"}) != canonical(
                    {k: v for k, v in after[name].items() if k != "snapshot_seq"}):
                raise CheckError("readiness changed business state")

    async def recovery(self):
        from fault_proxy import ResponseRelay
        await self.advance(1)
        await self.ready("Y")
        self.event("FAULT_ACTIVATED", "Y")
        async with ResponseRelay(self.config["platforms"]["Y"], FAULT_SECONDS,
                                 lambda kind: self.event(kind, "Y")) as relay:
            self.committed(await self.submit("Y", base_url=relay.url))
            await self.state(2)
            self.event("RECOVERY_COMPLETED", "Y")
            self.committed(await self.submit("Y", replay=True))
            await self.state(2)
            if "Z" in self.platforms:
                await self.ready("Z")
                self.committed(await self.submit("Z"))
                await self.state(3)
        write_json(self.path / "evidence/fault-response.json", {
            "response": relay.response, "error": relay.error}, self.secrets)
        if not relay.response or relay.response["status_code"] != 200:
            raise EvidenceMissing("first relayed response unavailable")
        first = parse_json(relay.response["body"])
        check_result(first, self.run_id, self.commands["Y"], "BANK_B", 200)
        if first.get("outcome") != "COMMITTED":
            raise EvidenceMissing("first relayed response did not prove commitment")
        kinds = [e["event_type"] for e in self.events]
        if not all(kind in kinds for kind in ("RESPONSE_HELD", "FAULT_RELEASED", "CALL_TIMEOUT")):
            raise EvidenceMissing("response hold/timeout/release missing")
        held = next(int(e["mono_ns"]) for e in self.events if e["event_type"] == "RESPONSE_HELD")
        released = next(int(e["mono_ns"]) for e in self.events if e["event_type"] == "FAULT_RELEASED")
        if released - held < int(FAULT_SECONDS * 1e9):
            raise EvidenceMissing("fault hold shorter than specified")

    async def scenario(self, variant):
        if variant == "normal":
            await self.advance(3 if "Z" in self.platforms else 2)
        elif variant.startswith("ready-"):
            _, platform, version = variant.split("-")
            await self.unchanged_readiness(platform, int(version), int(version) > 0)
        elif variant.startswith(("missing-auth-", "invalid-auth-", "forbidden-")):
            platform = variant[-1]
            await self.advance({"X": 0, "Y": 1, "Z": 2}[platform])
            options = {}
            if variant.startswith("missing-auth"):
                options["token_override"] = ""
                codes = ("UNAUTHENTICATED",)
            elif variant.startswith("invalid-auth"):
                options["token_override"] = "invalid-" + uuid.uuid4().hex
                codes = ("UNAUTHENTICATED",)
            else:
                options["role"] = "bank-c" if platform == "X" else "bank-a"
                codes = ("FORBIDDEN",)
            await self.refusal(platform, codes=codes, preacceptance=True, **options)
            await self.state({"X": 0, "Y": 1, "Z": 2}[platform])
        elif variant.startswith("snapshot-auth-"):
            status, body, _ = await self.request("GET", variant[-1], "snapshot", "bank-a")
            validate(body, "snapshot.schema.json")
            if status != 403 or set(body) != {"run_id", "request_id", "error"} or body["error"]["code"] != "FORBIDDEN":
                raise CheckError("unauthorized snapshot exposed or wrong error")
            await self.state(0)
        elif variant.startswith("other-result-"):
            platform = variant[-1]
            version = {"X": 1, "Y": 2, "Z": 3}[platform]
            await self.advance(version)
            role = "bank-c" if platform == "X" else "bank-a"
            status, body, _ = await self.request("GET", platform,
                "commands/" + self.commands[platform]["request_id"], role)
            validate(body, "result.schema.json")
            if status != 403 or set(body) != {"run_id", "request_id", "error"} or body["error"]["code"] != "FORBIDDEN":
                raise CheckError("other actor result exposed or wrong error")
            await self.state(version)
        elif variant.startswith("invalid-"):
            filename = variant.removeprefix("invalid-")
            command = read_json(ROOT / f"shared/fixtures/invalid/{filename}.json")
            # Raw malformed requests bypass only client-side command validation.
            status, body, _ = await self.request("POST", "X", "commands", "bank-a", command=command)
            validate(body, "result.schema.json")
            if (status != 400 or set(body) != {"run_id", "request_id", "error"}
                    or body["error"]["code"] != "INVALID_REQUEST"):
                raise CheckError("malformed request not rejected")
            await self.state(0)
        elif variant == "wrong-platform":
            command = self.commands["X"]
            status, body, _ = await self.request("POST", "Y", "commands", "bank-b", command=command)
            validate(body, "result.schema.json")
            if (status != 400 or set(body) != {"run_id", "request_id", "error"}
                    or body["error"]["code"] != "INVALID_REQUEST"):
                raise CheckError("wrong-platform request not rejected")
            await self.state(0)
        elif variant in ("no-reuse", "collateral-not-provided", "collateral-condition-failed",
                          "no-ccp-use", "ineligible"):
            platform = "Y" if variant in ("no-reuse", "collateral-not-provided", "collateral-condition-failed") else "Z"
            version = 1 if platform == "Y" else 2
            await self.advance(version)
            code = "USE_NOT_PERMITTED" if variant in ("no-reuse", "no-ccp-use") else (
                "COLLATERAL_NOT_ELIGIBLE" if variant == "ineligible" else "PRECONDITION_NOT_MET")
            await self.refusal(platform, codes=(code,))
            await self.state(version)
        elif variant.startswith("absent-"):
            platform = variant[-1]
            command = copy.deepcopy(self.commands[platform])
            command["expected_version"] = 0
            await self.refusal(platform, command, codes=("PRECONDITION_NOT_MET", "RIGHT_UNAVAILABLE"))
            await self.state(0)
        elif variant == "stale-Y":
            await self.advance(1)
            command = copy.deepcopy(self.commands["Y"])
            command["expected_version"] = 0
            await self.refusal("Y", command, codes=("VERSION_CONFLICT",))
            await self.state(1)
        elif variant == "locked-Z":
            await self.advance(3)
            command = copy.deepcopy(self.commands["Z"])
            command["request_id"] += "-other"
            command["expected_version"] = 3
            command["payload"].update(allocation_id="allocation-other",
                                     margin_call_id=self.fixture["margin_calls"][1]["margin_call_id"])
            await self.refusal("Z", command, codes=("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"))
            await self.state(3)
        elif variant.startswith("replay-"):
            platform = variant[-1]
            version = {"X": 1, "Y": 2, "Z": 3}[platform]
            await self.advance(version)
            for _ in range(2):
                self.committed(await self.submit(platform, replay=True))
            await self.state(version)
        elif variant == "id-conflict":
            await self.advance(2)
            command = copy.deepcopy(self.commands["Y"])
            command["payload"]["loan_id"] += "-changed"
            result = await self.refusal("Y", command, codes=("REQUEST_ID_CONFLICT",), preacceptance=True)
            if "outcome" in result:
                raise CheckError("request conflict overwrote logical result")
            status, original, _ = await self.request("GET", "Y", "commands/" + command["request_id"], "bank-b")
            check_result(original, self.run_id, self.commands["Y"], "BANK_B", status)
            self.committed(original)
            await self.state(2)
        elif variant in ("race-same", "race-loan", "race-margin"):
            platform = "Z" if variant == "race-margin" else "Y"
            await self.advance(2 if platform == "Z" else 1)
            await self.race(platform, identical=variant == "race-same")
        elif variant == "recovery":
            await self.recovery()
        else:
            raise CheckError("unknown scenario")


def case_plan(model, stage="xyz"):
    cases = []

    def add(test_id, variant, fixture="f0-normal", repeats=1):
        cases.extend({"test_id": test_id, "variant": variant, "fixture": fixture,
                      "repeat": i+1, "warmup": False} for i in range(repeats))

    add("AC-01", "normal")
    for platform, version in (("Y", 0), ("Y", 1), ("Z", 2)):
        if stage == "xyz" or platform != "Z":
            add("AC-02", f"ready-{platform}-{version}")
    for platform in ("XYZ" if stage == "xyz" else "XY"):
        for variant in ("missing-auth", "invalid-auth", "forbidden", "snapshot-auth", "other-result"):
            add("AC-03", variant + "-" + platform)
    for filename in ("missing-bundle-id", "wrong-version-type", "extra-permission-field"):
        add("AC-03", "invalid-" + filename)
    add("AC-03", "wrong-platform")
    for fixture in ("no-reuse", "collateral-not-provided", "collateral-condition-failed",
                    "no-ccp-use", "ineligible"):
        if stage == "xyz" or fixture not in ("no-ccp-use", "ineligible"):
            add("AC-04", fixture, fixture)
    for variant in ("absent-Y", "absent-Z", "stale-Y", "locked-Z"):
        if stage == "xyz" or not variant.endswith("Z"):
            add("AC-05", variant, "two-margin-calls" if variant == "locked-Z" else "f0-normal")
    for platform in ("XYZ" if stage == "xyz" else "XY"):
        add("AC-06", "replay-" + platform)
    add("AC-07", "id-conflict")
    add("AC-08", "race-same", repeats=10)
    add("AC-09", "race-loan", repeats=10)
    if stage == "xyz":
        add("AC-10", "race-margin", "two-margin-calls", 10)
    add("AC-11", "recovery", repeats=5)
    if model == "B2":
        cases = [c for c in cases if c["test_id"] in ("AC-01", "AC-02")]
        add("AC-12", "normal", repeats=30)
    return cases


def collect_history(execution):
    source = (ROOT / execution.config["evidence_dir"] / execution.run_id / "events.jsonl").resolve()
    if not source.is_file():
        raise EvidenceMissing("implementation events.jsonl not connected")
    original = source.read_bytes()
    text = original.decode("utf-8")
    write_json(execution.path / "evidence/implementation-events.json", {"source": str(source),
               "sha256": hashlib.sha256(original).hexdigest(), "body": text}, execution.secrets)
    events = [parse_json(line) for line in text.splitlines() if line.strip()]
    # Readiness-only and preacceptance refusals can produce no business events.
    if not events and (execution.test_id not in ("AC-02", "AC-03") or execution.results):
        raise EvidenceMissing("implementation event log empty")
    for event in events:
        validate(event, "event.schema.json")
        if (event["run_id"] != execution.run_id or event["model"] != execution.config["model"]
                or event["test_id"] not in (execution.test_id, "PREPARE")):
            raise CheckError("implementation event identity mismatch")
    history_check(events, execution.results, execution.race_requests)
    return events


def export_implementation_events(command, execution):
    """Optional local collector; business processing has already finished."""
    if not command:
        return
    output = (ROOT / execution.config["evidence_dir"] / execution.run_id / "events.jsonl").resolve()
    auth_names = set(execution.config.get("auth_env", {}).values())
    # The collector needs read-only DB credentials, never seed DSNs or API tokens.
    environment = {k: v for k, v in os.environ.items()
                   if k in ("MODEL_A_X_EVIDENCE_DSN", "MODEL_A_Y_EVIDENCE_DSN")
                   or (k not in auth_names and not any(s in k.upper() for s in CREDENTIAL_MARKERS))}
    try:
        completed = subprocess.run(
            [*command, "--run-id", execution.run_id, "--output", str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=30, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvidenceMissing("implementation collector " + type(error).__name__) from None
    write_json(execution.path / "evidence/collection.json", {
        "command": command, "returncode": completed.returncode, "stdout": completed.stdout,
        "stderr": completed.stderr}, execution.secrets)
    if completed.returncode:
        raise EvidenceMissing("implementation collector failed; see evidence/collection.json")


async def execute_case(args, config, case, batch, index):
    run_id = "run-" + uuid.uuid4().hex
    path = batch / "evidence" / f"{index:04}-{case['test_id']}-{case['variant']}"
    prepare_args = copy.copy(args)
    prepare_args.config = args.config
    prepare_args.fixture = str(ROOT / f"shared/fixtures/initial/{case['fixture']}.json")
    row = dict(case, model=config["model"], run_id=run_id, verdict="INCONCLUSIVE",
               path=str(path.relative_to(batch)), schedule_index=index, reason=None, evidence_complete=False,
               body_started=False)
    if await asyncio.to_thread(run, prepare_args, batch=path, run_id=run_id, quiet=True):
        row["reason"] = "preparation failed; see preparation checks"
        row["preparation_verdict"] = read_json(path / "checks.json")["verdict"]
        row["functional_verdict"] = "NOT_RUN"
        return row
    (path / "checks.json").rename(path / "evidence/preparation-checks.json")
    (path / "events.jsonl").rename(path / "evidence/preparation-events.jsonl")
    execution = Execution(config, read_json(prepare_args.fixture), path, run_id,
                          case["test_id"], args.platforms)
    row["body_started"] = True
    row["functional_verdict"] = "INCONCLUSIVE"
    try:
        if case.get("measured_flow"):
            await execution.measured_flow()
        else:
            await execution.scenario(case["variant"])
        row["functional_verdict"] = "PASS"
    except EvidenceMissing as error:
        row["reason"] = str(error)
    except CheckError as error:
        row.update(verdict="FAIL", functional_verdict="FAIL", reason=str(error))
    except Exception as error:
        row["reason"] = type(error).__name__
    finally:
        evidence_errors = []
        try:
            await asyncio.to_thread(export_implementation_events, getattr(args, "collect_command", None), execution)
        except Exception as error:
            evidence_errors.append(error)
        try:
            collect_history(execution)
            if execution.reassignment and execution.reassignment["total_preparation_ms"] is None:
                raise EvidenceMissing("overall reassignment timing evidence is not comparable")
        except Exception as error:
            evidence_errors.append(error)
        if evidence_errors:
            row["diagnostic_errors"] = [str(e) if isinstance(e, CheckError) else type(e).__name__
                                        for e in evidence_errors]
        if row["functional_verdict"] == "PASS":
            if evidence_errors:
                error = evidence_errors[0]
                row.update(verdict="FAIL" if isinstance(error, CheckError) and not isinstance(error, EvidenceMissing)
                           else "INCONCLUSIVE", reason=row["diagnostic_errors"][0])
            else:
                row.update(verdict="PASS", evidence_complete=True)
        await execution.client.aclose()
        execution.event("RUN_FINISHED", verdict=row["verdict"])
        row["logical_results"] = [{"request_id": c["request_id"], "outcome": r["outcome"],
                                   "record_id": (r.get("effect") or {}).get("record_id")}
                                  for c, r in execution.results]
        row["command_results"] = execution.results
        row["preacceptance_errors"] = execution.preacceptance_errors
        row["race_requests"] = execution.race_requests
        row["reassignment"] = execution.reassignment
        row["evidence_sha256"] = {str(p.relative_to(path)): digest(p) for p in path.rglob("*")
                                  if p.is_file() and p.name != "checks.json"}
        write_json(path / "checks.json", row, execution.secrets)
    return row


def final_gate(model, rows, expected_plan, batch):
    missing = []
    required = {(c["test_id"], c["variant"], c["repeat"]) for c in expected_plan}
    actual = [(r["test_id"], r["variant"], r["repeat"]) for r in rows]
    if set(actual) != required or len(actual) != len(required):
        missing.append("required variants/repetitions missing or duplicated")
    if len({r["run_id"] for r in rows}) != len(rows):
        missing.append("run IDs reused")
    for row in rows:
        if row.get("model") != model:
            missing.append("row model mismatch")
        if row["verdict"] != "PASS" or not row.get("evidence_complete"):
            missing.append(f"{row['test_id']}/{row['variant']}/{row['repeat']}: not fully evidenced PASS")
        path = batch / row["path"]
        for name in ("manifest.json", "checks.json", "events.jsonl", "evidence/implementation-events.json"):
            if not (path / name).is_file():
                missing.append(f"{row['run_id']}: missing {name}")
        hashes = row.get("evidence_sha256", {})
        if not hashes:
            missing.append("evidence content hashes missing")
        for name, sha in hashes.items():
            evidence = (path / name).resolve()
            if not evidence.is_relative_to(path.resolve()) or not evidence.is_file() or digest(evidence) != sha:
                missing.append("evidence hash missing/mismatch")
        try:
            stored = read_json(path / "checks.json")
            if stored != row:
                missing.append("stored run verdict/content mismatch")
            prepared = read_json(path / "manifest.json")
            if prepared.get("run_id") != row["run_id"] or prepared.get("model") != model:
                missing.append("run manifest identity mismatch")
            observed = [parse_json(line) for line in (path / "events.jsonl").read_text().splitlines() if line.strip()]
            for event in observed:
                validate(event, "event.schema.json")
                if event["run_id"] != row["run_id"] or event["model"] != model:
                    missing.append("observer event identity mismatch")
            if not any(e["event_type"] == "RUN_FINISHED" and e.get("verdict") == "PASS" for e in observed):
                missing.append("successful run completion event missing")
            wrapper = read_json(path / "evidence/implementation-events.json")
            events = [parse_json(line) for line in wrapper["body"].splitlines() if line.strip()]
            results = row.get("command_results")
            if not isinstance(results, list):
                missing.append("command_results missing or not a list")
                continue
            if not events and (row["test_id"] not in ("AC-02", "AC-03") or results):
                missing.append("implementation history empty")
            if any(e["run_id"] != row["run_id"] or e["model"] != model for e in events):
                missing.append("implementation history identity mismatch")
            history_check(events, results, row.get("race_requests", []))
        except (OSError, ValueError, KeyError, TypeError):
            missing.append("implementation evidence unreadable/incomplete")
    return missing


def review_evidence(config, batch):
    """Read implementation-owned provenance; never manufacture a model baseline."""
    source = (ROOT / config["evidence_dir"] / "context.json").resolve()
    if not source.is_file():
        return ["implementation context.json (version/environment/baseline/change record) not connected"]
    try:
        context = read_json(source)
        required_env = {"os", "cpu", "memory", "runtime", "database_or_canton", "topology",
                        "resource_limits", "durability", "network", "service_restart"}
        if (not context.get("implementation_version") or not context.get("implementation_files")
                or not required_env <= context.get("environment", {}).keys()
                or any(context["environment"][k] in (None, "", "NOT_RUN") for k in required_env)):
            raise EvidenceMissing("implementation version/environment/source evidence incomplete")
        for name, sha in context["implementation_files"].items():
            path = (ROOT / name).resolve()
            if not path.is_relative_to(ROOT / "implementations") or digest(path) != sha:
                raise EvidenceMissing("implementation source hash mismatch")
        baseline = context["xy_baseline_commit"]
        result = subprocess.run(["git", "cat-file", "-t", baseline], cwd=ROOT,
                                capture_output=True, text=True)
        if result.returncode or result.stdout.strip() != "commit":
            raise EvidenceMissing("preserved model X/Y baseline commit unavailable")
        record = (ROOT / context["z_change_record"]).resolve()
        if not record.is_relative_to(ROOT) or not record.read_text().strip():
            raise EvidenceMissing("Z change record missing")
        write_json(batch / "evidence/implementation-context.json", {
            "context": context, "sha256": digest(source), "z_change_record": record.read_text(),
            "z_change_record_sha256": digest(record)}, secret_values(config))
    except Exception as error:
        return [str(error) if isinstance(error, CheckError) else "implementation context invalid"]
    return []


def prerequisites(args, config, *, measurement=False):
    if config["model"] != "B2":
        return []
    # Development checks may run early; final acceptance and T4 retain separate gates.
    if not measurement and not getattr(args, "final", True):
        return []
    found, measured = set(), set()
    for name in getattr(args, "prerequisite_batch", []):
        path = Path(name).resolve()
        try:
            manifest, checks = read_json(path / "manifest.json"), read_json(path / "checks.json")
            current_spec = {str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "spec").glob("*.md")}
            if any(manifest.get("source_sha256", {}).get(k) != v for k, v in current_spec.items()):
                continue
            if any(not (ROOT / file).is_file() or digest(ROOT / file) != sha
                   for file, sha in manifest.get("source_sha256", {}).items()):
                continue
            if manifest.get("scope") == "experiment":
                from report import run_values
                for model in ("A", "B1"):
                    rows = [r for r in checks.get("runs", []) if r["model"] == model and r.get("suite") == "T1"]
                    if (len(rows) == 33 and len({r["run_id"] for r in rows}) == 33
                            and sum(bool(r.get("warmup")) for r in rows) == 3
                            and not any(r["verdict"] == "FAIL" for r in rows)
                            and any(not r.get("warmup") and run_values(r, path)["timing_valid"] for r in rows)):
                        measured.add(model)
            elif (checks.get("final_handoff_eligible") and manifest["model"] in ("A", "B1")
                    and not final_gate(manifest["model"], checks["runs"],
                                       case_plan(manifest["model"]), path)):
                current_spec = {str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "spec").glob("*.md")}
                if all(manifest.get("source_sha256", {}).get(k) == v for k, v in current_spec.items()):
                    found.add(manifest["model"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if found != {"A", "B1"}:
        return ["B2 final validation requires evidenced final A/B1 validation at current spec"]
    if measurement and measured != {"A", "B1"}:
        return ["B2 T4 requires evidenced A/B1 T1 measurement batches at current spec"]
    return []


def validated_for_experiment(args, config):
    for name in getattr(args, "validation_batch", []):
        path = Path(name).resolve()
        try:
            manifest, checks = read_json(path / "manifest.json"), read_json(path / "checks.json")
            if (manifest.get("scope") != "conformance" or manifest.get("model") != config["model"]
                    or manifest.get("config") != config or not checks.get("final_handoff_eligible")
                    or final_gate(config["model"], checks["runs"], case_plan(config["model"]), path)):
                continue
            if any(not (ROOT / file).is_file() or digest(ROOT / file) != sha
                   for file, sha in manifest["source_sha256"].items()):
                continue
            source = ROOT / config["evidence_dir"] / "context.json"
            archived = read_json(path / "evidence/implementation-context.json")
            if not source.is_file() or digest(source) != archived["sha256"]:
                continue
            context = read_json(source)
            if any(digest(ROOT / file) != sha for file, sha in context["implementation_files"].items()):
                continue
            return []
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return [f"{config['model']}: current, fully evidenced final validation batch required before measurement"]


def append_run_events(batch, row):
    source = batch / row["path"] / "events.jsonl"
    if source.is_file():
        with (batch / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(source.read_text(encoding="utf-8"))


async def check_batch(args):
    config = read_json(args.config)
    validate_config(config)
    if args.model and args.model != config["model"]:
        raise CheckError("--model differs from config")
    batch = Path(args.output).resolve() / ("batch-" + uuid.uuid4().hex)
    (batch / "evidence").mkdir(parents=True, exist_ok=False)
    plan = case_plan(config["model"], args.stage)
    if args.case:
        plan = [c for c in plan if c["test_id"] in args.case]
    manifest = {"scope": "conformance", "model": config["model"], "stage": args.stage,
                "environment": environment_info(),
                "final_requested": args.final, "config": config,
                "source_sha256": {str(p.relative_to(ROOT)): digest(p) for directory in
                                  ("spec", "shared", "tests/conformance")
                                  for p in (ROOT / directory).rglob("*")
                                  if p.is_file() and p.suffix in (".py", ".md", ".json", ".txt")
                                  and p.name != "config.local.json" and not p.name.endswith(".local.json")},
                "plan": plan, "timing": {"http_seconds": HTTP_SECONDS, "readiness_seconds": READINESS_SECONDS,
                "result_seconds": RESULT_SECONDS, "retry_seconds": RETRY_SECONDS,
                "max_additional_retries": 3, "observe_seconds": OBSERVE_SECONDS,
                "fault_seconds": FAULT_SECONDS}, "spec_frozen": False}
    write_json(batch / "manifest.json", manifest, secret_values(config))
    rows = []
    blockers = prerequisites(args, config)
    interrupted = False
    try:
        if not blockers:
            for index, case in enumerate(plan):
                rows.append(await execute_case(args, config, case, batch, index))
                append_run_events(batch, rows[-1])
                write_json(batch / "checks.json", {"runs": rows, "final_handoff_eligible": False,
                           "status": "RUNNING"}, secret_values(config))
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        blockers.append("batch interrupted; remaining cases NOT_RUN")
    if args.final:
        blockers += final_gate(config["model"], rows, case_plan(config["model"]), batch)
        blockers += review_evidence(config, batch)
    acceptance = {}
    for index in range(1, 13):
        test_id = f"AC-{index:02}"
        group = [r for r in rows if r["test_id"] == test_id]
        required = [c for c in case_plan(config["model"]) if c["test_id"] == test_id]
        if not group:
            acceptance[test_id] = "NOT_APPLICABLE" if test_id == "AC-12" and config["model"] != "B2" else "NOT_RUN"
        elif any(r["verdict"] == "FAIL" for r in group):
            acceptance[test_id] = "FAIL"
        elif (args.stage != "xyz" or len(group) != len(required)
              or any(r["verdict"] != "PASS" for r in group)):
            acceptance[test_id] = "INCONCLUSIVE"
        else:
            acceptance[test_id] = "PASS"
    eligible = bool(args.final and not blockers and not interrupted)
    checks = {"scope": "conformance", "runs": rows, "acceptance": acceptance,
              "preparation_attempts": len(rows), "business_bodies_started": sum(r["body_started"] for r in rows),
              "blockers": blockers, "final_handoff_eligible": eligible,
              "status": "FINISHED" if not interrupted else "INTERRUPTED",
              "not_run": [c for c in plan if not any(all(r[k] == c[k] for k in
                          ("test_id", "variant", "repeat")) for r in rows)]}
    write_json(batch / "checks.json", checks, secret_values(config))
    from report import report_batch
    report_batch(batch)
    print(json.dumps({"batch": str(batch), "final_handoff_eligible": eligible,
                      "preparation_attempts": len(rows), "business_bodies_started": checks["business_bodies_started"],
                      "not_run": len(checks["not_run"]), "blocker_count": len(blockers), "blockers": blockers[:5]}))
    return 0 if eligible or (not args.final and rows and not blockers and not interrupted
                            and all(r["verdict"] == "PASS" for r in rows)) else 1


def experiment_plan(suite, model):
    if suite == "T1" or suite == "T4":
        if suite == "T4" and model != "B2":
            raise CheckError("T4 applies only to B2")
        return [{"test_id": "AC-12" if model == "B2" else "AC-01", "variant": "normal",
                 "fixture": "f0-normal", "repeat": i+1, "warmup": i < 3,
                 "measured_flow": True, "suite": "T4" if model == "B2" else "T1"}
                for i in range(33)]
    if suite == "T2":
        return [dict(c, suite="T2") for c in case_plan(model)
                if c["test_id"] in ("AC-08", "AC-09", "AC-10")]
    if suite == "T3":
        return [dict(c, suite="T3") for c in case_plan(model) if c["test_id"] == "AC-11"]
    raise CheckError("unknown experiment suite")


async def experiment_batch(args):
    configs = [read_json(path) for path in args.config]
    for config in configs:
        validate_config(config)
    if len({c["model"] for c in configs}) != len(configs):
        raise CheckError("duplicate model configs")
    batch = Path(args.output).resolve() / ("batch-" + uuid.uuid4().hex)
    (batch / "evidence").mkdir(parents=True, exist_ok=False)
    plans = [experiment_plan(args.suite, c["model"]) for c in configs]
    if any(not plan for plan in plans):
        raise CheckError("suite not defined for this model; B2 T1 includes reassignment")
    order = []
    for iteration in range(max(map(len, plans))):
        for i in (range(len(configs)) if iteration % 2 == 0 else reversed(range(len(configs)))):
            if iteration < len(plans[i]):
                order.append({"config_index": i, "case": plans[i][iteration]})
    # Freeze the attempted schedule; failures are never replenished.
    secrets = sorted({s for c in configs for s in secret_values(c)}, key=len, reverse=True)
    manifest = {"scope": "experiment", "suite": args.suite, "configs": configs,
                "environment": environment_info(),
                "order": order, "attempts": len(order), "spec_frozen": False,
                "timing": {"http_seconds": HTTP_SECONDS, "observe_seconds": OBSERVE_SECONDS,
                           "readiness_seconds": READINESS_SECONDS, "result_seconds": RESULT_SECONDS,
                           "retry_seconds": RETRY_SECONDS, "fault_seconds": FAULT_SECONDS},
                "source_sha256": {str(p.relative_to(ROOT)): digest(p) for directory in ("spec", "shared", "tests/conformance")
                                  for p in (ROOT / directory).rglob("*") if p.is_file()
                                  and p.suffix in (".py", ".md", ".json", ".txt")
                                  and p.name != "config.local.json" and not p.name.endswith(".local.json")}}
    write_json(batch / "manifest.json", manifest, secrets)
    rows, blockers = [], []
    try:
        for index, item in enumerate(order):
            i = item["config_index"]
            config = configs[i]
            missing = prerequisites(args, config, measurement=True) + validated_for_experiment(args, config)
            if missing:
                blockers.extend(missing)
                continue
            subargs = copy.copy(args)
            subargs.config = args.config[i]
            rows.append(await execute_case(subargs, config, item["case"], batch, index))
            append_run_events(batch, rows[-1])
            write_json(batch / "checks.json", {"scope": "experiment", "runs": rows,
                       "final_handoff_eligible": False, "status": "RUNNING"}, secrets)
    except (KeyboardInterrupt, asyncio.CancelledError):
        blockers.append("interrupted; unfinished attempts are NOT_RUN")
    checks = {"scope": "experiment", "runs": rows, "blockers": sorted(set(blockers)),
              "preparation_attempts": len(rows),
              "business_bodies_started": sum(r.get("body_started", False) for r in rows),
              "not_run": len(order)-len(rows), "final_handoff_eligible": False,
              "status": "FINISHED" if len(rows) == len(order) else "INCOMPLETE"}
    write_json(batch / "checks.json", checks, secrets)
    from report import report_batch
    report_batch(batch)
    print(json.dumps({"batch": str(batch), "planned_attempts": len(order), "preparation_attempts": len(rows),
                      "business_bodies_started": checks["business_bodies_started"],
                      "not_run": checks["not_run"], "final_handoff_eligible": False}))
    return 0 if len(rows) == len(order) and all(r["verdict"] == "PASS" for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Preparation check only; never final acceptance")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--fixture", required=True)
    prepare.add_argument("--platforms", nargs="+", choices=("X", "Y", "Z"), default=["X", "Y", "Z"])
    prepare.add_argument("--observer-auth", choices=("bearer",), required=True,
                         help="Explicit transport binding; confirm with the real implementation")
    prepare.add_argument("--output", default=str(ROOT / "results"))
    check = commands.add_parser("check", help="Selected cases or complete final acceptance")
    check.add_argument("--config", required=True)
    check.add_argument("--model", choices=("A", "B1", "B2"))
    check.add_argument("--stage", choices=("xy", "xyz"), default="xyz")
    check.add_argument("--case", nargs="+", choices=tuple(f"AC-{i:02}" for i in range(1, 13)))
    check.add_argument("--final", action="store_true")
    check.add_argument("--observer-auth", choices=("bearer",), required=True)
    check.add_argument("--output", default=str(ROOT / "results"))
    check.add_argument("--collect-command", nargs="+",
                       help="Local read-only event collector argv; receives --run-id and --output after each case")
    check.add_argument("--prerequisite-batch", action="append", default=[])
    experiment = commands.add_parser("experiment", help="Fixed attempts; never top up failures")
    experiment.add_argument("--config", nargs="+", required=True)
    experiment.add_argument("--suite", choices=("T1", "T2", "T3", "T4"), required=True)
    experiment.add_argument("--observer-auth", choices=("bearer",), required=True)
    experiment.add_argument("--prerequisite-batch", action="append", default=[])
    experiment.add_argument("--validation-batch", action="append", default=[])
    experiment.add_argument("--output", default=str(ROOT / "results"))
    report = commands.add_parser("report", help="Rebuild derived outputs without rerunning a model")
    report.add_argument("--batch", required=True)
    args = parser.parse_args()
    if args.command == "report":
        from report import report_batch
        report_batch(Path(args.batch).resolve())
        return 0
    if args.command == "experiment":
        args.platforms = ["X", "Y", "Z"]
        try:
            return asyncio.run(experiment_batch(args))
        except (CheckError, OSError, ValueError):
            print("Invalid experiment configuration or unavailable input", file=sys.stderr)
            return 1
    if args.command == "check":
        if args.final and (args.case or args.stage != "xyz"):
            parser.error("--final cannot be combined with --case or --stage xy")
        args.platforms = ["X", "Y"] if args.stage == "xy" else ["X", "Y", "Z"]
        try:
            return asyncio.run(check_batch(args))
        except (CheckError, OSError, ValueError):
            print("Invalid configuration or unavailable input", file=sys.stderr)
            return 1
    if len(set(args.platforms)) != len(args.platforms):
        parser.error("duplicate platforms")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
