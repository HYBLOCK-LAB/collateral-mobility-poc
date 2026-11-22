#!/usr/bin/env python3
"""Stage 3 check of X→Y refusals, replays, contention and recovery on the
running adapters and Canton.

    . scripts/env.sh && ../../.venv/bin/python -I selftest/xy_stage3_selftest.py [AC-08 ...]

It runs with the repository's .venv (tests/conformance/requirements.txt):
the recovery case uses the runner's response relay, fault_proxy.ResponseRelay.
Test IDs as arguments select cases; the report then says which were selected.

The cases are the AC-04..AC-11 cases of tests/conformance/runner.py
case_plan("B1", "xy"), with its repeats and fixtures:

  AC-04  no-reuse, collateral-not-provided, collateral-condition-failed:
         after the Repo, CONFIRM_LOAN is REJECTED with USE_NOT_PERMITTED /
         PRECONDITION_NOT_MET and S1 stays. Beyond the runner: the same request
         sent again gets the same rejection with replayed=true, and its query
         the original rejection (interface §6.3).
  AC-05  absent-Y: CONFIRM_LOAN in S0 is REJECTED (PRECONDITION_NOT_MET or
         RIGHT_UNAVAILABLE), S0 stays. stale-Y: in S1 with expected version 0
         it is REJECTED with VERSION_CONFLICT, S1 stays.
  AC-06  replay-X, replay-Y: the committed request sent twice more returns the
         committed result with replayed=true; beyond the runner, the request
         has one INTERNAL_COMMITTED event.
  AC-07  id-conflict: the committed loan request ID with another loan_id is
         409 REQUEST_ID_CONFLICT, and the original result stays.
  AC-08  race-same x10: two identical loan requests released together both
         end in the one committed effect.
  AC-09  race-loan x10: two loan requests with other request IDs and loan IDs
         released together: one COMMITTED, the other REJECTED with
         VERSION_CONFLICT or RIGHT_UNAVAILABLE.
  AC-11  recovery x5: the first loan answer is held by the relay past the
         client timeout; the client queries, the result is COMMITTED, and a
         replay returns it with replayed=true.

Each case has its own run_id and one X-Test-ID. Its S0 contracts are made by
the stage 2 self-test's preparation (xy_selftest.XYSelftest.prepare), which
stands in for the loader the research lead connects. Submissions follow the
runner's rule (runner.Execution.submit): a result that is not final is
queried every RESULT_SECONDS, and a query answered NOT_FOUND is followed by
resending the unchanged request at most three times, RETRY_SECONDS apart.

Judgments come from tests/conformance/checks.py: check_result on every
answer, state_check on every state, history_check on the run's events (with
the race requests: the two calls must overlap on the adapter's clock, or the
case is INCONCLUSIVE). The adapter logs (adapter-<P>.out) are read for how
often a loan request found no right of the bundle on its first read and for
the ledger's refusals of contended submissions.

This is not the common runner, which is NOT_RUN until the research lead
connects it. The report goes to logs/xy-stage3-selftest/<batch>.json, without
credentials. Exit status 0 when every selected case passed, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

SELFTEST_DIR = Path(__file__).resolve().parent
IMPL_DIR = SELFTEST_DIR.parent
ROOT = IMPL_DIR.parent.parent
sys.path.insert(0, str(ROOT / "tests/conformance"))
sys.path.insert(0, str(SELFTEST_DIR))
from checks import (  # noqa: E402
    CheckError,
    EvidenceMissing,
    check_result,
    expected_state,
    history_check,
    initial_state,
    parse_json,
    read_json,
    validate,
)
from fault_proxy import ResponseRelay  # noqa: E402
from runner import (  # noqa: E402
    FAULT_SECONDS,
    HTTP_SECONDS,
    OBSERVE_SECONDS,
    RESULT_SECONDS,
    RETRY_SECONDS,
    case_plan,
)
from xy_selftest import PLATFORMS, Failed, XYSelftest  # noqa: E402

TESTS = ("AC-04", "AC-05", "AC-06", "AC-07", "AC-08", "AC-09", "AC-11")
ROLES = {"X": "bank-a", "Y": "bank-b"}
ACTORS = {"bank-a": "BANK_A", "bank-b": "BANK_B"}
ABSENT = re.compile(
    r"no right of the bundle on the first read; after (\d+) reads: (\S+)"
)
REFUSED = re.compile(r"not confirmed \(attempt \d+\): (\d+) .*?'code': '([A-Z_]+)'")


class Case(XYSelftest):
    """One case of the plan on its own run (runner.Execution's scenarios)."""

    def __init__(self, run_id: str, case: dict):
        super().__init__()
        self.case = case
        self.test_id = case["test_id"]
        self.run_id = run_id
        self.fixture = read_json(
            ROOT / f"shared/fixtures/initial/{case['fixture']}.json"
        )
        self.commands = {"X": copy.deepcopy(self.repo), "Y": copy.deepcopy(self.loan)}
        self.calls: list[dict] = []
        self.finals: list[tuple[dict, dict]] = []  # runner.Execution.results
        self.race_requests: list[str] = []
        self.detail: dict = {}
        self.deadline = time.monotonic() + OBSERVE_SECONDS
        self.client = httpx.AsyncClient(
            timeout=HTTP_SECONDS, follow_redirects=False, trust_env=False
        )

    def adapter(
        self, method, platform, path, role, *, command=None, params=None, test_id=None
    ):
        # Every call of the case carries the case's test ID.
        return super().adapter(
            method,
            platform,
            path,
            role,
            command=command,
            params=params,
            test_id=self.test_id,
        )

    # --- calls, as runner.Execution.request / submit (here call / send) ---

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
    ):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceMissing("observation deadline reached")
        attempt = "attempt-" + uuid.uuid4().hex
        headers = {
            "Authorization": "Bearer " + self.tokens[role],
            "X-Attempt-ID": attempt,
            "X-Test-ID": self.test_id,
        }
        url = (base_url or self.base[platform]).rstrip("/")
        url += f"/runs/{self.run_id}/" + suffix
        entry = {
            "method": method,
            "platform": platform,
            "path": suffix,
            "attempt_id": attempt,
            "relay": base_url is not None,
        }
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

    async def send(self, platform, command=None, *, replay=None, base_url=None):
        command = copy.deepcopy(command or self.commands[platform])
        role = ROLES[platform]
        status, body = await self.call(
            "POST", platform, "commands", role, command=command, base_url=base_url
        )
        retries, last_retry = 0, time.monotonic()
        while True:
            if body is not None:
                check_result(body, self.run_id, command, ACTORS[role], status, replay)
                temporary = "outcome" not in body and status in (404, 503)
                if body.get("outcome") in ("COMMITTED", "REJECTED") or (
                    "outcome" not in body and not temporary
                ):
                    if body.get("outcome") in ("COMMITTED", "REJECTED"):
                        self.finals.append((command, body))
                    return body
            await asyncio.sleep(RESULT_SECONDS)
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

    async def ready(self, platform):
        command = self.commands[platform]
        params = {
            "command_type": command["command_type"],
            "bundle_id": command["bundle_id"],
            "agreement_id": command["payload"]["agreement_id"],
        }
        while True:
            status, body = await self.call(
                "GET", platform, "readiness", ROLES[platform], params=params
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
        self.record_ids = {
            "repo_id": self.commands["X"]["payload"]["repo_id"],
            "loan_id": self.commands["Y"]["payload"]["loan_id"],
        }
        return await asyncio.to_thread(super().state, version)

    @staticmethod
    def require_committed(result):
        if not result or result.get("outcome") in (None, "PENDING", "UNKNOWN"):
            raise EvidenceMissing("committed result not yet established")
        if result.get("outcome") != "COMMITTED":
            raise CheckError(f"expected committed result, got {result}")
        return result

    async def advance(self, version):
        for number, platform in enumerate(PLATFORMS, 1):
            if number > version:
                break
            if platform != "X":
                await self.ready(platform)
            self.require_committed(await self.send(platform))
            await self.state(number)

    async def refusal(
        self, platform, command=None, codes=(), preacceptance=False, replay=None
    ):
        result = await self.send(platform, command, replay=replay)
        if not result or (result.get("error") or {}).get("code") not in codes:
            raise CheckError(f"expected refusal code {codes}, got {result}")
        if result.get("outcome") != (None if preacceptance else "REJECTED"):
            raise CheckError("refusal outcome does not match acceptance boundary")
        return result

    # --- scenarios (runner.Execution.scenario, X/Y variants) ---------------

    async def scenario(self, variant):
        if variant in (
            "no-reuse",
            "collateral-not-provided",
            "collateral-condition-failed",
        ):
            await self.advance(1)
            code = (
                "USE_NOT_PERMITTED" if variant == "no-reuse" else "PRECONDITION_NOT_MET"
            )
            first = await self.refusal("Y", codes=(code,))
            await self.state(1)
            again = await self.refusal("Y", codes=(code,), replay=True)
            status, original = await self.call(
                "GET", "Y", "commands/" + self.commands["Y"]["request_id"], "bank-b"
            )
            check_result(
                original, self.run_id, self.commands["Y"], "BANK_B", status, False
            )
            if (again["error"], original["error"]) != (first["error"], first["error"]):
                raise CheckError(
                    "the resent or queried rejection differs from the first"
                )
            await self.state(1)
        elif variant == "absent-Y":
            command = copy.deepcopy(self.commands["Y"])
            command["expected_version"] = 0
            await self.refusal(
                "Y", command, codes=("PRECONDITION_NOT_MET", "RIGHT_UNAVAILABLE")
            )
            await self.state(0)
        elif variant == "stale-Y":
            await self.advance(1)
            command = copy.deepcopy(self.commands["Y"])
            command["expected_version"] = 0
            await self.refusal("Y", command, codes=("VERSION_CONFLICT",))
            await self.state(1)
        elif variant in ("replay-X", "replay-Y"):
            platform = variant[-1]
            version = {"X": 1, "Y": 2}[platform]
            await self.advance(version)
            for _ in range(2):
                self.require_committed(await self.send(platform, replay=True))
            await self.state(version)
            self.detail["internal_commits"] = self.commands[platform]["request_id"]
        elif variant == "id-conflict":
            await self.advance(2)
            command = copy.deepcopy(self.commands["Y"])
            command["payload"]["loan_id"] += "-changed"
            result = await self.refusal(
                "Y", command, codes=("REQUEST_ID_CONFLICT",), preacceptance=True
            )
            if "outcome" in result:
                raise CheckError("request conflict overwrote logical result")
            status, original = await self.call(
                "GET", "Y", "commands/" + command["request_id"], "bank-b"
            )
            check_result(original, self.run_id, self.commands["Y"], "BANK_B", status)
            self.require_committed(original)
            await self.state(2)
        elif variant in ("race-same", "race-loan"):
            await self.advance(1)
            await self.race(identical=variant == "race-same")
        elif variant == "recovery":
            await self.recovery()
        else:
            raise CheckError(f"unknown X/Y scenario {variant}")

    async def race(self, identical):
        first = copy.deepcopy(self.commands["Y"])
        second = copy.deepcopy(first)
        if not identical:
            second["request_id"] += "-other"
            second["payload"]["loan_id"] += "-other"
        barrier = asyncio.Event()

        async def call(command):
            await barrier.wait()
            return await self.send("Y", command)

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
                "outcome": r.get("outcome"),
                "code": (r.get("error") or {}).get("code"),
                "replayed": r.get("replayed"),
            }
            for c, r in zip((first, second), results)
        ]
        successes = [
            i for i, r in enumerate(results) if r.get("outcome") == "COMMITTED"
        ]
        if identical:
            if len(successes) != 2 or results[0]["effect"] != results[1]["effect"]:
                raise CheckError(
                    "same-request race did not resolve to one original effect"
                )
        else:
            if len(successes) != 1:
                raise CheckError("race requires exactly one committed request")
            loser = results[1 - successes[0]]
            if loser.get("outcome") != "REJECTED" or (loser.get("error") or {}).get(
                "code"
            ) not in ("VERSION_CONFLICT", "RIGHT_UNAVAILABLE"):
                raise CheckError("unexpected race loser")
            self.commands["Y"] = (first, second)[successes[0]]
        self.race_requests = [first["request_id"], second["request_id"]]
        await self.state(2)

    async def recovery(self):
        await self.advance(1)
        await self.ready("Y")
        marks: list[tuple[str, int]] = []
        async with ResponseRelay(
            self.base["Y"],
            FAULT_SECONDS,
            lambda kind: marks.append((kind, time.monotonic_ns())),
        ) as relay:
            self.require_committed(await self.send("Y", base_url=relay.url))
            await self.state(2)
            self.require_committed(await self.send("Y", replay=True))
            await self.state(2)
        self.detail["relay"] = {
            "response": relay.response,
            "error": relay.error,
            "marks": [k for k, _ in marks],
        }
        if not relay.response or relay.response["status_code"] != 200:
            raise EvidenceMissing("first relayed response unavailable")
        first = parse_json(relay.response["body"])
        check_result(first, self.run_id, self.commands["Y"], "BANK_B", 200)
        if first.get("outcome") != "COMMITTED":
            raise EvidenceMissing("first relayed response did not prove commitment")
        times = dict(marks)
        if not {"RESPONSE_HELD", "FAULT_RELEASED"} <= set(times):
            raise EvidenceMissing("response hold/release missing")
        if not any(c["relay"] and c.get("timeout") for c in self.calls):
            raise EvidenceMissing("the relayed call did not time out")
        if times["FAULT_RELEASED"] - times["RESPONSE_HELD"] < int(FAULT_SECONDS * 1e9):
            raise EvidenceMissing("fault hold shorter than specified")

    # --- evidence -----------------------------------------------------------

    def history(self):
        path = self.evidence / self.run_id / "events.jsonl"
        if not path.is_file():
            raise EvidenceMissing(f"{path} missing")
        events = [json.loads(line) for line in path.read_text().splitlines() if line]
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

    def adapter_log(self, state: Path):
        """What the adapters logged for this run: loan requests that found no
        right of the bundle on the first read, and ledger refusals."""
        absent, refused = [], []
        for p in PLATFORMS:
            path = state / "logs" / f"adapter-{p}.out"
            if not path.is_file():
                continue
            for line in path.read_text(errors="replace").splitlines():
                if f" {self.run_id} " not in line:
                    continue
                if m := ABSENT.search(line):
                    absent.append({"platform": p, "reads": int(m[1]), "then": m[2]})
                if m := REFUSED.search(line):
                    refused.append({"platform": p, "status": int(m[1]), "code": m[2]})
        self.detail["absent_first_read"] = absent
        self.detail["ledger_refusals"] = refused

    async def execute(self, state: Path) -> dict:
        started = time.monotonic()
        verdict, problem = "PASS", None
        try:
            await asyncio.to_thread(self.prepare)
            snapshots = await asyncio.to_thread(self.snapshots)
            initial_state(self.fixture, snapshots, self.run_id, PLATFORMS)
            self.previous.append(expected_state(self.fixture, 0))
            await self.scenario(self.case["variant"])
            self.history()
        except EvidenceMissing as error:
            verdict, problem = "INCONCLUSIVE", f"{type(error).__name__}: {error}"
        except (Failed, CheckError, KeyError, TypeError, ValueError) as error:
            verdict, problem = "FAIL", f"{type(error).__name__}: {error}"
        finally:
            await self.client.aclose()
        self.adapter_log(state)
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


def summarize(entries: list[dict]) -> dict:
    by_test: dict[str, Counter] = {}
    for e in entries:
        by_test.setdefault(e["test_id"], Counter())[e["verdict"]] += 1
    answers = Counter()
    for e in entries:
        for c in e["calls"]:
            if c["method"] == "POST":
                answers[
                    str(c.get("status") or c.get("error"))
                    + " "
                    + str(c.get("outcome") or c.get("code"))
                ] += 1
    absent = Counter(
        a["then"] for e in entries for a in e["detail"]["absent_first_read"]
    )
    refused = Counter(
        r["code"] for e in entries for r in e["detail"]["ledger_refusals"]
    )
    return {
        "by_test": {t: dict(c) for t, c in by_test.items()},
        "post_answers": dict(answers),
        "absent_first_read": dict(absent),
        "ledger_refusals": dict(refused),
    }


async def main(selected: list[str]) -> int:
    unknown = [t for t in selected if t not in TESTS]
    if unknown:
        raise SystemExit(f"unknown test IDs {unknown}; choose from {', '.join(TESTS)}")
    if not os.environ.get("CANTON_STATE_DIR"):
        raise SystemExit("CANTON_STATE_DIR is not set; run . scripts/env.sh first")
    state = Path(os.environ["CANTON_STATE_DIR"])
    cases = [c for c in case_plan("B1", "xy") if c["test_id"] in (selected or TESTS)]
    batch = secrets.token_hex(4)
    entries = []
    for number, case in enumerate(cases, 1):
        run_id = f"xy-stage3-{batch}-{number:02}"
        entry = await Case(run_id, case).execute(state)
        entries.append(entry)
        print(
            f"{entry['verdict']:<12} {case['test_id']} {case['variant']:<28} "
            f"#{case['repeat']:<2} {entry['ms']:>9} ms  {run_id}"
            + (f"  {entry['problem']}" if "problem" in entry else ""),
            flush=True,
        )
    passed = sum(e["verdict"] == "PASS" for e in entries)
    report = {
        "batch": batch,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "selected": selected or list(TESTS),
        "response_wait": os.environ.get("CANTON_RESPONSE_WAIT")
        or "4.0 (adapter default)",
        "summary": summarize(entries),
        "passed": passed,
        "planned": len(cases),
        "cases": entries,
    }
    out = IMPL_DIR / "logs/xy-stage3-selftest" / f"{batch}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(
        f"{passed}/{len(cases)} passed"
        + ("" if not selected else f" (selected {' '.join(selected)})")
        + f"; report {out}"
    )
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
