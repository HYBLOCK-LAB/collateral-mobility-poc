#!/usr/bin/env python3
"""Stage 3 check of Z refusals, replays, contention and recovery on the
running adapters and Canton (b1-xyz).

    . scripts/env.sh && ../../.venv/bin/python -I selftest/xyz_stage3_selftest.py [AC-10 ...] [--restart]

It runs with the repository's .venv (tests/conformance/requirements.txt), as
xy_stage3_selftest does. Test IDs as arguments select cases; the report then
says which were selected.

The cases are the Z cases of tests/conformance/runner.py case_plan("B1",
"xyz"), with its repeats and fixtures; the script stops when the plan differs
from the 25 cases below, or when the plan's other cases differ from
case_plan("B1", "xy") and AC-02 ready-Z-2 (those are xy_stage3_selftest's and
the other self-tests' cases; ready-Z-2 is xyz_selftest's
readiness-Z-after-loan):

  AC-03  missing-auth-Z, invalid-auth-Z: after the loan, ALLOCATE_MARGIN with no
         Authorization header / an unknown token is 401 UNAUTHENTICATED.
         forbidden-Z: the same as BANK_A is 403 FORBIDDEN. S2 stays.
         snapshot-auth-Z: the Z snapshot as BANK_A is 403 FORBIDDEN with only
         run_id, request_id and error; S0 stays. other-result-Z: after the
         allocation, its result as BANK_A is 403 FORBIDDEN the same way; S3
         stays.
  AC-04  no-ccp-use, ineligible: after the loan, ALLOCATE_MARGIN is REJECTED
         with USE_NOT_PERMITTED / COLLATERAL_NOT_ELIGIBLE and S2 stays. Beyond
         the runner: the same request sent again gets the same rejection with
         replayed=true, and its query the original rejection (interface §6.3).
  AC-05  absent-Z: ALLOCATE_MARGIN in S0 is REJECTED (PRECONDITION_NOT_MET or
         RIGHT_UNAVAILABLE), S0 stays. locked-Z (two-margin-calls): after the
         allocation, another request for the second margin call is REJECTED
         (RIGHT_UNAVAILABLE or PRECONDITION_NOT_MET) and S3 stays.
  AC-06  replay-Z: the committed allocation sent twice more returns the
         committed result with replayed=true; beyond the runner, the request
         has one INTERNAL_COMMITTED event.
  AC-10  race-margin x10 (two-margin-calls): after the loan, two allocation
         requests with other request IDs, allocation IDs and margin calls
         released together: one COMMITTED, the other REJECTED with
         VERSION_CONFLICT or RIGHT_UNAVAILABLE; S3 of the winner.
  AC-11  recovery x5: the loan answer is held by the relay past the client
         timeout, as in xy_stage3_selftest; then, as the runner does on the
         xyz stage, Z readiness and ALLOCATE_MARGIN commit to S3.

The runner submits Z right after S2 shows on the union of the snapshots,
without waiting for Z readiness (only advance and recovery wait for it); so
does this script. How often the Z adapter found no right of the bundle on its
first read is in the report (absent_first_read), not hidden by waiting.

--restart adds one more case at the end, apart from the 25 (an additional
check, kept apart from the required common checks): after an allocation and a
rejected second allocation (two-margin-calls), scripts/adapter.sh stop and
start restart the X, Y and Z adapters; then the query of each committed
request returns it with replayed=false, the allocation sent again returns it
with replayed=true, the rejected request's query and resend return the
original rejection, and S3 stays. The adapter output before the restart is
kept as logs/adapter-<P>.before-<run_id>.out in the state directory, because
adapter.sh start starts adapter-<P>.out anew.

Each case has its own run_id and one X-Test-ID. Its S0 contracts are made by
the Z self-test's preparation (xyz_selftest.XYZSelftest.prepare), which stands
in for the common loader (prepare.py xyz, research lead's PR #11, not on this
branch); like that loader it then creates the run's empty events.jsonl, so
that a case without business events has its log. Calls, resends and judgments
are as in xy_stage3_selftest, from
tests/conformance/checks.py; an empty event log is accepted only for AC-03
without final results, as runner.collect_history does.

This is not the common runner; its check --stage xyz runs from a separate
checkout as tests/conformance/B1_XYZ_RUNBOOK.md (PR #11) says. The report goes
to logs/xyz-stage3-selftest/<batch>.json, without credentials. Exit status 0
when every selected case (and the restart case, if asked for) passed, 1
otherwise.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

# -I leaves the script's directory off sys.path (as in authz_check).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from xy_stage3_selftest import ABSENT, IMPL_DIR, REFUSED, Case, summarize  # noqa: E402

# tests/conformance is on sys.path from here on (xy_stage3_selftest).
from checks import (  # noqa: E402
    CheckError,
    EvidenceMissing,
    check_result,
    expected_state,
    history_check,
    initial_state,
    parse_json,
    validate,
)
from runner import (  # noqa: E402
    HTTP_SECONDS,
    OBSERVE_SECONDS,
    RESULT_SECONDS,
    RETRY_SECONDS,
    case_plan,
)
from xy_selftest import Failed, XYSelftest  # noqa: E402
from xyz_selftest import XYZSelftest  # noqa: E402

TESTS = ("AC-03", "AC-04", "AC-05", "AC-06", "AC-10", "AC-11")
MODEL = "B1"
# The Z cases of case_plan("B1", "xyz"): (test_id, variant, fixture) -> repeats
EXPECTED = Counter(
    {
        ("AC-03", "missing-auth-Z", "f0-normal"): 1,
        ("AC-03", "invalid-auth-Z", "f0-normal"): 1,
        ("AC-03", "forbidden-Z", "f0-normal"): 1,
        ("AC-03", "snapshot-auth-Z", "f0-normal"): 1,
        ("AC-03", "other-result-Z", "f0-normal"): 1,
        ("AC-04", "no-ccp-use", "no-ccp-use"): 1,
        ("AC-04", "ineligible", "ineligible"): 1,
        ("AC-05", "absent-Z", "f0-normal"): 1,
        ("AC-05", "locked-Z", "two-margin-calls"): 1,
        ("AC-06", "replay-Z", "f0-normal"): 1,
        ("AC-10", "race-margin", "two-margin-calls"): 10,
        ("AC-11", "recovery", "f0-normal"): 5,
    }
)
Z_VARIANTS = ("no-ccp-use", "ineligible", "race-margin", "recovery")
RESTART = {
    "test_id": "RESTART-Z",
    "variant": "restart",
    "fixture": "two-margin-calls",
    "repeat": 1,
}


def key(case: dict) -> tuple[str, str, str]:
    return case["test_id"], case["variant"], case["fixture"]


def z_cases() -> list[dict]:
    """The Z cases of the runner's plan, checked against EXPECTED."""
    xyz = case_plan("B1", "xyz")
    cases = [
        c for c in xyz if c["variant"].endswith("-Z") or c["variant"] in Z_VARIANTS
    ]
    found = Counter(key(c) for c in cases)
    if found != EXPECTED:
        raise SystemExit(
            f"case_plan('B1', 'xyz') Z cases changed: +{dict(found - EXPECTED)} "
            f"-{dict(EXPECTED - found)}; update this script first"
        )
    # Recovery is in both plans; on the xyz stage it continues to Z. AC-02
    # ready-Z-2 is xyz_selftest's readiness-Z-after-loan, as the X/Y readiness
    # cases are xy_selftest's.
    rest = Counter(key(c) for c in xyz) - found
    xy = Counter(key(c) for c in case_plan("B1", "xy"))
    xy -= Counter({("AC-11", "recovery", "f0-normal"): 5})
    xy += Counter({("AC-02", "ready-Z-2", "f0-normal"): 1})
    if rest != xy:
        raise SystemExit(
            f"case_plan('B1', 'xyz') X/Y cases differ from 'xy': +{dict(rest - xy)} "
            f"-{dict(xy - rest)}; update this script first"
        )
    return cases


class ZCase(Case, XYZSelftest):
    """One Z case of the plan on its own run (runner.Execution's scenarios).

    Case (xy_stage3_selftest) brings the runner's call, resend and evidence
    steps for X and Y; XYZSelftest brings the three platforms, BANK_C's
    credential and the margin calls of the preparation."""

    roles = {"X": "bank-a", "Y": "bank-b", "Z": "bank-c"}
    # The model the adapters' events carry (b2_stage3_selftest: B2).
    model = MODEL

    def __init__(self, run_id: str, case: dict):
        super().__init__(run_id, case)
        self.commands["Z"] = copy.deepcopy(self.margin)

    def prepare(self):
        done = super().prepare()
        # As the loader does (prepare.py): the run's log exists, without events.
        log = self.evidence / self.run_id / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8"):
            pass
        return done

    # --- calls, as runner.Execution.request / submit ----------------------

    async def call(
        self,
        method,
        platform,
        suffix,
        role,
        *,
        command=None,
        params=None,
        base_url=None,
        token_override=None,
    ):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceMissing("observation deadline reached")
        attempt = "attempt-" + uuid.uuid4().hex
        token = self.tokens[role] if token_override is None else token_override
        # An empty override sends no Authorization header (runner.request).
        headers = {"Authorization": "Bearer " + token} if token else {}
        headers.update({"X-Attempt-ID": attempt, "X-Test-ID": self.test_id})
        url = (base_url or self.base[platform]).rstrip("/")
        url += f"/runs/{self.run_id}/" + suffix
        entry = {
            "method": method,
            "platform": platform,
            "path": suffix,
            "role": role,
            "attempt_id": attempt,
            "relay": base_url is not None,
        }
        if token_override is not None:
            entry["auth"] = "invalid token" if token_override else "no header"
        self.calls.append(entry)
        started = time.monotonic()
        try:
            async with asyncio.timeout(min(HTTP_SECONDS, remaining)):
                response = await self.client.request(
                    method, url, headers=headers, json=command, params=params
                )
        except (httpx.TimeoutException, TimeoutError, httpx.TransportError) as error:
            entry.update(
                ms=round((time.monotonic() - started) * 1000, 1),
                error=type(error).__name__,
                timeout=isinstance(error, (httpx.TimeoutException, TimeoutError)),
            )
            return None, None
        body = parse_json(response.text)
        entry.update(
            ms=round((time.monotonic() - started) * 1000, 1),
            status=response.status_code,
        )
        if isinstance(body, dict):
            entry.update(
                outcome=body.get("outcome"),
                code=(body.get("error") or {}).get("code"),
                replayed=body.get("replayed"),
            )
        return response.status_code, body

    async def send(
        self,
        platform,
        command=None,
        *,
        replay=None,
        base_url=None,
        role=None,
        token_override=None,
    ):
        command = copy.deepcopy(command or self.commands[platform])
        role = role or self.roles[platform]
        status, body = await self.call(
            "POST",
            platform,
            "commands",
            role,
            command=command,
            base_url=base_url,
            token_override=token_override,
        )
        retries, last_retry = 0, time.monotonic()
        while True:
            if body is not None:
                check_result(
                    body, self.run_id, command, self.actors[role], status, replay
                )
                temporary = "outcome" not in body and status in (404, 503)
                if body.get("outcome") in ("COMMITTED", "REJECTED") or (
                    "outcome" not in body and not temporary
                ):
                    if body.get("outcome") in ("COMMITTED", "REJECTED"):
                        self.finals.append((command, body))
                    return body
            await asyncio.sleep(RESULT_SECONDS)
            # Queries and resends carry the role's credential (runner.submit).
            status, body = await self.call(
                "GET", platform, "commands/" + command["request_id"], role
            )
            # A 404 does not prove that the original request failed; resend it unchanged.
            if body and (body.get("error") or {}).get("code") == "NOT_FOUND":
                if retries < 3 and time.monotonic() - last_retry >= RETRY_SECONDS:
                    status, body = await self.call(
                        "POST", platform, "commands", role, command=command
                    )
                    retries += 1
                    last_retry = time.monotonic()
                else:
                    body = None
            # A query is not a replay.
            replay = None

    async def refusal(
        self,
        platform,
        command=None,
        codes=(),
        preacceptance=False,
        replay=None,
        **options,
    ):
        result = await self.send(platform, command, replay=replay, **options)
        if not result or (result.get("error") or {}).get("code") not in codes:
            raise CheckError(f"expected refusal code {codes}, got {result}")
        if result.get("outcome") != (None if preacceptance else "REJECTED"):
            raise CheckError("refusal outcome does not match acceptance boundary")
        return result

    async def ready(self, platform):
        command = self.commands[platform]
        params = {
            "command_type": command["command_type"],
            "bundle_id": command["bundle_id"],
        }
        for name in ("agreement_id", "margin_call_id"):
            if name in command["payload"]:
                params[name] = command["payload"][name]
        while True:
            status, body = await self.call(
                "GET", platform, "readiness", self.roles[platform], params=params
            )
            if body is None:
                raise EvidenceMissing("readiness observation failed")
            validate(body, "readiness.schema.json")
            if status != 200 or "ready" not in body:
                raise EvidenceMissing("readiness HTTP/error response")
            if body["ready"]:
                if body["observed_version"] != command["expected_version"]:
                    raise CheckError("readiness version mismatch")
                return body
            await asyncio.sleep(RESULT_SECONDS)

    async def state(self, version):
        z = self.commands["Z"]["payload"]
        self.record_ids = {
            "repo_id": self.commands["X"]["payload"]["repo_id"],
            "loan_id": self.commands["Y"]["payload"]["loan_id"],
            "allocation_id": z["allocation_id"],
            "margin_call_id": z["margin_call_id"],
        }
        # XYSelftest.state, not Case.state, which names only the X/Y records.
        return await asyncio.to_thread(XYSelftest.state, self, version)

    async def advance(self, version):
        for number, platform in enumerate(self.platforms, 1):
            if number > version:
                break
            if platform != "X":
                await self.ready(platform)
            self.require_committed(await self.send(platform))
            await self.state(number)

    async def forbidden_answer(self, suffix, what):
        status, body = await self.call("GET", "Z", suffix, "bank-a")
        if body is None:
            raise EvidenceMissing(f"{what} observation failed")
        validate(body, f"{what}.schema.json")
        if (
            status != 403
            or set(body) != {"run_id", "request_id", "error"}
            or body["error"]["code"] != "FORBIDDEN"
        ):
            raise CheckError(
                f"{what} exposed to BANK_A or wrong error: {status} {body}"
            )

    async def original(self, platform, command):
        """The query of a request by its own role: not a replay."""
        role = self.roles[platform]
        status, body = await self.call(
            "GET", platform, "commands/" + command["request_id"], role
        )
        if body is None:
            raise EvidenceMissing("result query failed")
        check_result(body, self.run_id, command, self.actors[role], status, False)
        return body

    # --- scenarios (runner.Execution.scenario, Z variants) ----------------

    async def scenario(self, variant):
        z = self.commands["Z"]
        if variant in ("missing-auth-Z", "invalid-auth-Z", "forbidden-Z"):
            await self.advance(2)
            if variant == "missing-auth-Z":
                options, codes = {"token_override": ""}, ("UNAUTHENTICATED",)
            elif variant == "invalid-auth-Z":
                options = {"token_override": "invalid-" + uuid.uuid4().hex}
                codes = ("UNAUTHENTICATED",)
            else:
                options, codes = {"role": "bank-a"}, ("FORBIDDEN",)
            await self.refusal("Z", codes=codes, preacceptance=True, **options)
            await self.state(2)
        elif variant == "snapshot-auth-Z":
            await self.forbidden_answer("snapshot", "snapshot")
            await self.state(0)
        elif variant == "other-result-Z":
            await self.advance(3)
            await self.forbidden_answer("commands/" + z["request_id"], "result")
            await self.state(3)
        elif variant in ("no-ccp-use", "ineligible"):
            await self.advance(2)
            code = (
                "USE_NOT_PERMITTED"
                if variant == "no-ccp-use"
                else "COLLATERAL_NOT_ELIGIBLE"
            )
            first = await self.refusal("Z", codes=(code,))
            await self.state(2)
            again = await self.refusal("Z", codes=(code,), replay=True)
            original = await self.original("Z", z)
            if original.get("outcome") != "REJECTED" or (
                again["error"],
                original["error"],
            ) != (first["error"], first["error"]):
                raise CheckError(
                    "the resent or queried rejection differs from the first"
                )
            await self.state(2)
        elif variant == "absent-Z":
            command = copy.deepcopy(z)
            command["expected_version"] = 0
            await self.refusal(
                "Z", command, codes=("PRECONDITION_NOT_MET", "RIGHT_UNAVAILABLE")
            )
            await self.state(0)
        elif variant == "locked-Z":
            await self.advance(3)
            await self.refusal(
                "Z",
                self.second_allocation(),
                codes=("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"),
            )
            await self.state(3)
        elif variant == "replay-Z":
            await self.advance(3)
            for _ in range(2):
                self.require_committed(await self.send("Z", replay=True))
            await self.state(3)
            self.detail["internal_commits"] = z["request_id"]
        elif variant == "race-margin":
            await self.advance(2)
            await self.race_margin()
        elif variant == "recovery":
            # Case.recovery: X, the relayed loan and its replay, then Z
            # (runner.recovery on the xyz stage).
            await super().recovery()
            await self.ready("Z")
            self.require_committed(await self.send("Z"))
            await self.state(3)
        else:
            raise CheckError(f"unknown Z scenario {variant}")

    def second_allocation(self):
        """runner locked-Z: another request for the second margin call."""
        command = copy.deepcopy(self.commands["Z"])
        command["request_id"] += "-other"
        command["expected_version"] = 3
        command["payload"].update(
            allocation_id="allocation-other",
            margin_call_id=self.fixture["margin_calls"][1]["margin_call_id"],
        )
        return command

    async def race_margin(self):
        first = copy.deepcopy(self.commands["Z"])
        second = copy.deepcopy(first)
        second["request_id"] += "-other"
        second["payload"]["allocation_id"] += "-other"
        second["payload"]["margin_call_id"] = self.fixture["margin_calls"][1][
            "margin_call_id"
        ]
        barrier = asyncio.Event()

        async def call(command):
            await barrier.wait()
            return await self.send("Z", command)

        tasks = [asyncio.create_task(call(c)) for c in (first, second)]
        barrier.set()
        # Both calls finish before a failure of either is raised.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
            if r is None:
                raise EvidenceMissing("a race call ended without a result")
        self.detail["race"] = [
            {
                "request_id": c["request_id"],
                "margin_call_id": c["payload"]["margin_call_id"],
                "outcome": r.get("outcome"),
                "code": (r.get("error") or {}).get("code"),
                "replayed": r.get("replayed"),
            }
            for c, r in zip((first, second), results)
        ]
        successes = [
            i for i, r in enumerate(results) if r.get("outcome") == "COMMITTED"
        ]
        if len(successes) != 1:
            raise CheckError("race requires exactly one committed request")
        loser = results[1 - successes[0]]
        if loser.get("outcome") != "REJECTED" or (loser.get("error") or {}).get(
            "code"
        ) not in ("VERSION_CONFLICT", "RIGHT_UNAVAILABLE"):
            raise CheckError("unexpected race loser")
        self.commands["Z"] = (first, second)[successes[0]]
        self.race_requests = [first["request_id"], second["request_id"]]
        await self.state(3)

    # --- evidence -----------------------------------------------------------

    def history(self):
        path = self.evidence / self.run_id / "events.jsonl"
        if not path.is_file():
            raise EvidenceMissing(f"{path} missing")
        events = [parse_json(line) for line in path.read_text().splitlines() if line]
        # Preacceptance refusals and read-only cases can leave no business events.
        if not events and (self.test_id != "AC-03" or self.finals):
            raise EvidenceMissing("event log empty")
        for event in events:
            validate(event, "event.schema.json")
            if (
                event["run_id"] != self.run_id
                or event["model"] != self.model
                or event["test_id"] not in (self.test_id, "PREPARE")
            ):
                raise CheckError("event identity mismatch")
        history_check(events, self.finals, self.race_requests)
        request_id = self.detail.get("internal_commits")
        if request_id is not None:
            internal = [
                e
                for e in events
                if e["event_type"] == "INTERNAL_COMMITTED"
                and e["request_id"] == request_id
            ]
            if len(internal) != 1:
                raise CheckError(
                    f"{request_id} has {len(internal)} INTERNAL_COMMITTED events"
                )
        self.detail["events"] = dict(Counter(e["event_type"] for e in events))

    def adapter_log(self, state: Path, names=("adapter-{p}.out",)):
        """What the adapters logged for this run: requests that found no
        right of the bundle on the first read, and ledger refusals."""
        absent, refused = [], []
        for p in self.platforms:
            for name in names:
                path = state / "logs" / name.format(p=p, run_id=self.run_id)
                if not path.is_file():
                    continue
                for line in path.read_text(errors="replace").splitlines():
                    if f" {self.run_id} " not in line:
                        continue
                    if m := ABSENT.search(line):
                        absent.append({"platform": p, "reads": int(m[1]), "then": m[2]})
                    if m := REFUSED.search(line):
                        refused.append(
                            {"platform": p, "status": int(m[1]), "code": m[2]}
                        )
        self.detail["absent_first_read"] = absent
        self.detail["ledger_refusals"] = refused

    def read_logs(self, state: Path):
        self.adapter_log(state)

    async def execute(self, state: Path) -> dict:
        started = time.monotonic()
        verdict, problem = "PASS", None
        try:
            await asyncio.to_thread(self.prepare)
            snapshots = await asyncio.to_thread(self.snapshots)
            initial_state(self.fixture, snapshots, self.run_id, self.platforms)
            self.previous.append(expected_state(self.fixture, 0))
            await self.scenario(self.case["variant"])
            self.history()
        except EvidenceMissing as error:
            verdict, problem = "INCONCLUSIVE", f"{type(error).__name__}: {error}"
        except (Failed, CheckError, KeyError, TypeError, ValueError) as error:
            verdict, problem = "FAIL", f"{type(error).__name__}: {error}"
        finally:
            await self.client.aclose()
        self.read_logs(state)
        entry = {k: self.case[k] for k in ("test_id", "variant", "fixture", "repeat")}
        entry.update(
            run_id=self.run_id,
            verdict=verdict,
            ms=round((time.monotonic() - started) * 1000, 1),
        )
        if problem:
            entry["problem"] = problem
        entry.update(detail=self.detail, calls=self.calls)
        return entry


class RestartCase(ZCase):
    """The --restart case: results and rejections across an adapter restart."""

    BEFORE = "adapter-{p}.before-{run_id}.out"

    def __init__(self, run_id: str, state: Path):
        super().__init__(run_id, RESTART)
        self.state_dir = state

    async def scenario(self, variant):
        await self.advance(3)
        locked = self.second_allocation()
        rejected = await self.refusal(
            "Z", locked, codes=("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET")
        )
        await self.state(3)
        await asyncio.to_thread(self.restart)
        for platform in self.platforms:
            self.require_committed(
                await self.original(platform, self.commands[platform])
            )
        self.require_committed(await self.send("Z", replay=True))
        original = await self.original("Z", locked)
        again = await self.refusal(
            "Z", locked, codes=(rejected["error"]["code"],), replay=True
        )
        if original.get("outcome") != "REJECTED" or (
            original["error"],
            again["error"],
        ) != (rejected["error"], rejected["error"]):
            raise CheckError("the rejection after the restart differs from the first")
        await self.state(3)

    def restart(self):
        """adapter.sh stop and start (X, Y and Z), keeping the old output."""
        logs = self.state_dir / "logs"
        before = {}
        for p in self.platforms:
            ready = self.state_dir / f"adapter-{p}.ready"
            before[p] = json.loads(ready.read_text())["clock_id"]
            out = logs / f"adapter-{p}.out"
            if out.is_file():
                shutil.copy2(out, logs / self.BEFORE.format(p=p, run_id=self.run_id))
        steps = {}
        for action in ("stop", "start"):
            started = time.monotonic()
            done = subprocess.run(
                ["bash", str(IMPL_DIR / "scripts/adapter.sh"), action],
                cwd=IMPL_DIR,
                capture_output=True,
                text=True,
                timeout=300,
            )
            steps[action] = {
                "returncode": done.returncode,
                "seconds": round(time.monotonic() - started, 1),
            }
            if done.returncode != 0:
                self.detail["restart"] = {"steps": steps}
                raise Failed(
                    f"adapter.sh {action} exited {done.returncode}: "
                    f"{done.stderr.strip()[-400:]}"
                )
        after = {}
        for p in self.platforms:
            ready = json.loads((self.state_dir / f"adapter-{p}.ready").read_text())
            self.base[p] = ready["url"]
            after[p] = ready["clock_id"]
        if any(after[p] == before[p] for p in self.platforms):
            raise Failed("an adapter kept its clock across the restart")
        self.detail["restart"] = {
            "steps": steps,
            "clock_before": before,
            "clock_after": after,
        }
        # The restart is not observation time; old connections are gone.
        self.deadline = time.monotonic() + OBSERVE_SECONDS
        self.client = httpx.AsyncClient(
            timeout=HTTP_SECONDS, follow_redirects=False, trust_env=False
        )

    def read_logs(self, state: Path):
        self.adapter_log(state, (self.BEFORE, "adapter-{p}.out"))


def line(entry: dict, case: dict) -> str:
    return (
        f"{entry['verdict']:<12} {case['test_id']} {case['variant']:<28} "
        f"#{case['repeat']:<2} {entry['ms']:>9} ms  {entry['run_id']}"
        + (f"  {entry['problem']}" if "problem" in entry else "")
    )


async def main(arguments: list[str]) -> int:
    restart = "--restart" in arguments
    selected = [a for a in arguments if a != "--restart"]
    unknown = [t for t in selected if t not in TESTS]
    if unknown:
        raise SystemExit(f"unknown test IDs {unknown}; choose from {', '.join(TESTS)}")
    if not os.environ.get("CANTON_STATE_DIR"):
        raise SystemExit("CANTON_STATE_DIR is not set; run . scripts/env.sh first")
    state = Path(os.environ["CANTON_STATE_DIR"])
    cases = [c for c in z_cases() if c["test_id"] in (selected or TESTS)]
    batch = secrets.token_hex(4)
    entries = []
    for number, case in enumerate(cases, 1):
        run_id = f"xyz-stage3-{batch}-{number:02}"
        entry = await ZCase(run_id, case).execute(state)
        entries.append(entry)
        print(line(entry, case), flush=True)
    additional = []
    if restart:
        run_id = f"xyz-stage3-{batch}-restart"
        entry = await RestartCase(run_id, state).execute(state)
        additional.append(entry)
        print(line(entry, RESTART), flush=True)
    passed = sum(e["verdict"] == "PASS" for e in entries)
    report = {
        "batch": batch,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "selected": selected or list(TESTS),
        "restart": restart,
        "response_wait": os.environ.get("CANTON_RESPONSE_WAIT")
        or "4.0 (adapter default)",
        "summary": summarize(entries),
        "passed": passed,
        "planned": len(cases),
        "cases": entries,
        # Additional checks, apart from the planned cases and their summary.
        "additional": additional,
    }
    out = IMPL_DIR / "logs/xyz-stage3-selftest" / f"{batch}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(
        f"{passed}/{len(cases)} passed"
        + ("" if not selected else f" (selected {' '.join(selected)})")
        + ("" if not restart else f"; restart {additional[0]['verdict']}")
        + f"; report {out}"
    )
    ok = passed == len(cases) and all(e["verdict"] == "PASS" for e in additional)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
