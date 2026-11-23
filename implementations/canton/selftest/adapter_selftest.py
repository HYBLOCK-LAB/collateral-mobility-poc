#!/usr/bin/env python3
"""Stage 1 checks of the running platform adapters X and Y (scripts/adapter.sh start).

    . scripts/env.sh && .venv/bin/python -I selftest/adapter_selftest.py

What it checks, over HTTP only:
  refusals   the AC-03 shapes the runner sends (missing/invalid credential,
             forbidden actor, snapshot and other-result access, the three
             invalid fixtures, wrong platform), plus body, path and query errors
  unknown    authorised calls for a run that is not prepared answer 404 NOT_FOUND
             (stage 1 has no loader; stage 2 replaces this with ledger state)
  events     <evidence>/<run_id>/events.jsonl: event.schema.json, one
             COMMAND_RECEIVED/FINISHED pair per attributable POST carrying the
             sent X-Test-ID/X-Attempt-ID, nothing for unauthenticated or observer
             calls, strictly increasing sequence per producer
  messages   the participant calls of those POSTs and of the authorised GETs
             (message_log_check.py): only the state read that finds the run
             unprepared, logged as MESSAGE_SENT/RECEIVED under the POST's fields
             for every POST answered NOT_FOUND, and under the GET's X-Test-ID,
             X-Attempt-ID and query fields (result, readiness, snapshot) for
             every GET answered NOT_FOUND after that read; each call answered;
             no message for a refused call or for a GET without X-Test-ID
  burst      concurrent POSTs to X and Y appending to the same events.jsonl

Responses are judged with tests/conformance/checks.py (validate, check_result),
the same functions the runner uses. Credentials come from the POC_*_TOKEN
environment variables and are never written to the report.
The report goes to implementations/canton/logs/adapter-selftest/<run_id>.json.
Exit status 0 when every expectation held, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

IMPL_DIR = Path(__file__).resolve().parent.parent
ROOT = IMPL_DIR.parent.parent
sys.path.insert(0, str(ROOT / "tests/conformance"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from checks import CheckError, check_result, parse_json, read_json, validate  # noqa: E402
from message_log_check import check_run as check_messages  # noqa: E402

TOKEN_ENV = {
    "bank-a": "POC_BANK_A_TOKEN",
    "bank-b": "POC_BANK_B_TOKEN",
    "bank-c": "POC_BANK_C_TOKEN",
    "observer": "POC_OBSERVER_TOKEN",
}
ACTOR = {"bank-a": "BANK_A", "bank-b": "BANK_B", "bank-c": "BANK_C"}
ERROR_KEYS = {"run_id", "request_id", "error"}
BURST = 16
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def not_one_read(messages: list[dict]) -> str | None:
    """None when the messages of one GET are a single read, of as many ACS pages
    as the participant holds, with each page's request and response once (the
    page numbers themselves are checked by message_log_check); else what they are.
    """
    halves = defaultdict(list)  # <purpose>:<hex>:p<n> -> event types
    for e in messages:
        halves[e["message_id"].rsplit(":", 1)[0]].append(e["event_type"])
    reads = {stem.split(":p", 1)[0] for stem in halves}
    if len(reads) == 1 and all(
        sorted(kinds) == ["MESSAGE_RECEIVED", "MESSAGE_SENT"]
        for kinds in halves.values()
    ):
        return None
    return f"{len(reads)} reads, messages {dict(halves)}"


class Reply:
    def __init__(self, status: int, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def code(self):
        return (
            (self.body.get("error") or {}).get("code")
            if isinstance(self.body, dict)
            else None
        )


class Selftest:
    def __init__(self):
        state = Path(os.environ.get("CANTON_STATE_DIR", ""))
        self.base = {}
        for p in ("X", "Y"):
            ready = state / f"adapter-{p}.ready"
            if not ready.is_file():
                raise SystemExit(f"{ready} missing (scripts/adapter.sh start)")
            self.base[p] = json.loads(ready.read_text())["url"]
        missing = [name for name in TOKEN_ENV.values() if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} not set (. scripts/env.sh)")
        self.tokens = {role: os.environ[name] for role, name in TOKEN_ENV.items()}
        self.run_id = "selftest-" + secrets.token_hex(6)
        self.evidence = Path(os.environ.get("CANTON_EVIDENCE_DIR") or IMPL_DIR / "logs")
        self.repo = read_json(ROOT / "shared/fixtures/commands/repo.json")
        self.loan = read_json(ROOT / "shared/fixtures/commands/loan.json")
        self.results: list[dict] = []
        self.expected_events: list[dict] = []  # one entry per attributable POST
        self.expected_reads: list[dict] = []  # one entry per GET that reads the ledger
        self.counter = 0

    # --- HTTP ------------------------------------------------------------

    def call(
        self,
        method,
        platform,
        path,
        role=None,
        *,
        token=None,
        command=None,
        raw=None,
        params=None,
        test_id=None,
        attempt=True,
        run_id=None,
        send_test_id=True,
    ) -> tuple[Reply, dict]:
        """One request; returns the reply and the correlation headers that were
        sent (test_id None when X-Test-ID was left out)."""
        self.counter += 1
        url = self.base[platform] + f"/runs/{run_id or self.run_id}" + path
        if params is not None:
            url += "?" + urlencode(params)
        data = (
            raw
            if raw is not None
            else (json.dumps(command).encode() if command is not None else None)
        )
        request = urllib.request.Request(url, data=data, method=method)
        secret = token if token is not None else (self.tokens[role] if role else "")
        if secret:
            request.add_header("Authorization", "Bearer " + secret)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        sent = {
            "test_id": (test_id or f"SELFTEST-{self.counter:03}")
            if send_test_id
            else None,
            "attempt_id": f"attempt-selftest-{secrets.token_hex(8)}"
            if attempt
            else None,
        }
        if sent["test_id"]:
            request.add_header("X-Test-ID", sent["test_id"])
        if sent["attempt_id"]:
            request.add_header("X-Attempt-ID", sent["attempt_id"])
        try:
            with OPENER.open(request, timeout=30) as response:
                status, headers, payload = (
                    response.status,
                    response.headers,
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            status, headers, payload = error.code, error.headers, error.read()
        try:
            body = parse_json(payload.decode("utf-8"))
        except (ValueError, CheckError):
            body = None
        return Reply(status, headers, body), sent

    # --- expectations ----------------------------------------------------

    def record(self, name, problems, reply: Reply | None = None, detail=None):
        entry = {"check": name, "ok": not problems, "problems": problems}
        if reply is not None:
            entry.update(http=reply.status, code=reply.code)
        if detail is not None:
            entry["detail"] = detail
        self.results.append(entry)

    def refusal(
        self,
        name,
        method,
        platform,
        path,
        role=None,
        *,
        status,
        code,
        schema,
        command=None,
        echo=None,
        events=None,
        reads=None,
        **kwargs,
    ) -> Reply:
        """A pre-acceptance or query error answered with the common error object.

        echo: (run_id, request_id) the body must carry; check_result is used when
        a command was sent, as the runner does for every POST response.
        events: the actor whose COMMAND_RECEIVED/FINISHED pair this POST must leave.
        reads: the purpose and fields of the state read this GET must leave.
        """
        reply, sent = self.call(method, platform, path, role, command=command, **kwargs)
        problems = []
        try:
            validate(reply.body, schema)
            if command is not None and echo is None:
                check_result(
                    reply.body, self.run_id, command, ACTOR.get(role or ""), reply.status
                )
        except (CheckError, TypeError, KeyError) as error:
            problems.append(f"common checks: {error}")
        if reply.status != status or reply.code != code:
            problems.append(f"expected {status} {code}")
        if not isinstance(reply.body, dict) or set(reply.body) != ERROR_KEYS:
            problems.append("body keys are not exactly run_id, request_id, error")
        elif (
            echo is not None
            and (reply.body["run_id"], reply.body["request_id"]) != echo
        ):
            problems.append(
                f"echoed ids {reply.body['run_id']!r}, {reply.body['request_id']!r}"
                f" instead of {echo!r}"
            )
        if status == 401 and reply.headers.get("WWW-Authenticate") != "Bearer":
            problems.append("401 without WWW-Authenticate: Bearer")
        if reads is not None:
            self.expected_reads.append(
                {"check": name, "platform": platform, **reads, **sent}
            )
        if events and command is not None:
            self.expected_events.append(
                {
                    "check": name,
                    "platform": platform,
                    "actor": events,
                    "code": code,
                    "request_id": command["request_id"],
                    "bundle_id": command["bundle_id"],
                    "command_type": command.get("command_type"),
                    **sent,
                }
            )
        self.record(name, problems, reply)
        return reply

    # --- checks ----------------------------------------------------------

    def ac03(self):
        cmd = {"X": self.repo, "Y": self.loan}
        stranger = {"X": "bank-c", "Y": "bank-a"}
        for p in ("X", "Y"):
            common = dict(
                status=401, code="UNAUTHENTICATED", schema="result.schema.json"
            )
            self.refusal(
                f"missing-auth-{p}",
                "POST",
                p,
                "/commands",
                command=cmd[p],
                token="",
                **common,
            )
            self.refusal(
                f"invalid-auth-{p}",
                "POST",
                p,
                "/commands",
                command=cmd[p],
                token="invalid-" + secrets.token_hex(16),
                **common,
            )
            self.refusal(
                f"forbidden-{p}",
                "POST",
                p,
                "/commands",
                stranger[p],
                command=cmd[p],
                status=403,
                code="FORBIDDEN",
                schema="result.schema.json",
                events=ACTOR[stranger[p]],
            )
            self.refusal(
                f"snapshot-auth-{p}",
                "GET",
                p,
                "/snapshot",
                "bank-a",
                status=403,
                code="FORBIDDEN",
                schema="snapshot.schema.json",
                echo=(self.run_id, None),
            )
            self.refusal(
                f"other-result-{p}",
                "GET",
                p,
                "/commands/" + cmd[p]["request_id"],
                stranger[p],
                status=403,
                code="FORBIDDEN",
                schema="result.schema.json",
                echo=(self.run_id, cmd[p]["request_id"]),
            )
            self.refusal(
                f"get-without-auth-{p}",
                "GET",
                p,
                "/commands/" + cmd[p]["request_id"],
                token="",
                status=401,
                code="UNAUTHENTICATED",
                schema="result.schema.json",
                echo=(self.run_id, cmd[p]["request_id"]),
            )
        for name in (
            "missing-bundle-id",
            "wrong-version-type",
            "extra-permission-field",
        ):
            command = read_json(ROOT / f"shared/fixtures/invalid/{name}.json")
            self.refusal(
                f"invalid-{name}",
                "POST",
                "X",
                "/commands",
                "bank-a",
                command=command,
                status=400,
                code="INVALID_REQUEST",
                schema="result.schema.json",
                events="BANK_A" if "bundle_id" in command else None,
            )
        self.refusal(
            "wrong-platform",
            "POST",
            "Y",
            "/commands",
            "bank-b",
            command=self.repo,
            status=400,
            code="INVALID_REQUEST",
            schema="result.schema.json",
            events="BANK_B",
        )
        self.refusal(
            "observer-submit-X",
            "POST",
            "X",
            "/commands",
            "observer",
            command=self.repo,
            status=403,
            code="FORBIDDEN",
            schema="result.schema.json",
            echo=(self.run_id, self.repo["request_id"]),
        )

    def unknown_run(self):
        self.refusal(
            "unknown-run-post-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            command=self.repo,
            status=404,
            code="NOT_FOUND",
            schema="result.schema.json",
            events="BANK_A",
        )
        self.refusal(
            "unknown-run-post-Y",
            "POST",
            "Y",
            "/commands",
            "bank-b",
            command=self.loan,
            status=404,
            code="NOT_FOUND",
            schema="result.schema.json",
            events="BANK_B",
        )
        self.refusal(
            "unknown-run-post-no-correlation-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            command=self.repo,
            status=404,
            code="NOT_FOUND",
            schema="result.schema.json",
            events="BANK_A",
            attempt=False,
            test_id="bad id",
        )
        for p, role, cmd in (("X", "bank-a", self.repo), ("Y", "bank-b", self.loan)):
            self.refusal(
                f"unknown-run-result-{p}",
                "GET",
                p,
                "/commands/" + cmd["request_id"],
                role,
                status=404,
                code="NOT_FOUND",
                schema="result.schema.json",
                echo=(self.run_id, cmd["request_id"]),
                reads={
                    "purpose": "result",
                    "request_id": cmd["request_id"],
                    "actor": ACTOR[role],
                },
            )
            self.refusal(
                f"unknown-run-snapshot-{p}",
                "GET",
                p,
                "/snapshot",
                "observer",
                status=404,
                code="NOT_FOUND",
                schema="snapshot.schema.json",
                echo=(self.run_id, None),
                reads={"purpose": "snapshot"},
            )
            # The runner's initial-state snapshot sends no X-Test-ID: no messages.
            self.refusal(
                f"unknown-run-snapshot-no-test-id-{p}",
                "GET",
                p,
                "/snapshot",
                "observer",
                status=404,
                code="NOT_FOUND",
                schema="snapshot.schema.json",
                echo=(self.run_id, None),
                send_test_id=False,
            )
        params = {
            "command_type": "CONFIRM_LOAN",
            "bundle_id": self.loan["bundle_id"],
            "agreement_id": self.loan["payload"]["agreement_id"],
        }
        self.refusal(
            "unknown-run-readiness-Y",
            "GET",
            "Y",
            "/readiness",
            "bank-b",
            params=params,
            status=404,
            code="NOT_FOUND",
            schema="readiness.schema.json",
            echo=(self.run_id, None),
            reads={
                "purpose": "readiness",
                "bundle_id": params["bundle_id"],
                "command_type": params["command_type"],
                "actor": "BANK_B",
            },
        )
        self.refusal(
            "readiness-forbidden-Y",
            "GET",
            "Y",
            "/readiness",
            "bank-c",
            params=params,
            status=403,
            code="FORBIDDEN",
            schema="readiness.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "readiness-missing-param-Y",
            "GET",
            "Y",
            "/readiness",
            "bank-b",
            params={k: v for k, v in params.items() if k != "agreement_id"},
            status=400,
            code="INVALID_REQUEST",
            schema="readiness.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "readiness-extra-param-Y",
            "GET",
            "Y",
            "/readiness",
            "bank-b",
            params={**params, "margin_call_id": "margin-call-001"},
            status=400,
            code="INVALID_REQUEST",
            schema="readiness.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "readiness-not-provided-X",
            "GET",
            "X",
            "/readiness",
            "bank-a",
            params=params,
            status=404,
            code="NOT_FOUND",
            schema="readiness.schema.json",
            echo=(None, None),
        )

    def malformed(self):
        rid = self.repo["request_id"]
        self.refusal(
            "body-not-json-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            raw=b"{not json",
            status=400,
            code="INVALID_REQUEST",
            schema="result.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "body-duplicate-key-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            raw=b'{"request_id":"a","request_id":"b"}',
            status=400,
            code="INVALID_REQUEST",
            schema="result.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "body-array-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            raw=b"[]",
            status=400,
            code="INVALID_REQUEST",
            schema="result.schema.json",
            echo=(self.run_id, None),
        )
        big = json.dumps({**self.repo, "pad": "x" * 70_000}).encode()
        self.refusal(
            "body-too-large-X",
            "POST",
            "X",
            "/commands",
            "bank-a",
            raw=big,
            status=400,
            code="INVALID_REQUEST",
            schema="result.schema.json",
            echo=(self.run_id, None),
        )
        self.refusal(
            "body-too-large-without-auth-X",
            "POST",
            "X",
            "/commands",
            raw=big,
            token="",
            status=401,
            code="UNAUTHENTICATED",
            schema="result.schema.json",
            echo=(self.run_id, None),
        )
        for run_id, label in (("%2E%2E", "dotdot"), ("...", "dots")):
            self.refusal(
                f"run-id-{label}-X",
                "POST",
                "X",
                "/commands",
                "bank-a",
                command=self.repo,
                run_id=run_id,
                status=400,
                code="INVALID_REQUEST",
                schema="result.schema.json",
                echo=(None, rid),
            )
        self.refusal(
            "unknown-path-X",
            "GET",
            "X",
            "/nothing",
            "bank-a",
            status=404,
            code="NOT_FOUND",
            schema="result.schema.json",
            echo=(None, None),
        )
        self.refusal(
            "unknown-method-X",
            "DELETE",
            "X",
            "/commands/" + rid,
            "bank-a",
            status=404,
            code="NOT_FOUND",
            schema="result.schema.json",
            echo=(None, None),
        )
        # Nothing may have been written outside <evidence>/<run_id>/.
        stray = [
            p
            for p in (
                self.evidence / "events.jsonl",
                self.evidence.parent / "events.jsonl",
            )
            if p.exists()
        ]
        self.record("no-events-outside-run-dir", [f"{p} exists" for p in stray])

    def burst(self):
        jobs = [("X", "bank-a", self.repo), ("Y", "bank-b", self.loan)] * BURST
        problems = []
        with ThreadPoolExecutor(max_workers=2 * BURST) as pool:
            replies = list(
                pool.map(
                    lambda job: (
                        job,
                        *self.call(
                            "POST",
                            job[0],
                            "/commands",
                            job[1],
                            command=job[2],
                            test_id="SELFTEST-BURST",
                        ),
                    ),
                    jobs,
                )
            )
        for (p, role, cmd), reply, sent in replies:
            if reply.status != 404 or reply.code != "NOT_FOUND":
                problems.append(f"{p}: {reply.status} {reply.code}")
            self.expected_events.append(
                {
                    "check": "burst",
                    "platform": p,
                    "actor": ACTOR[role],
                    "code": "NOT_FOUND",
                    "request_id": cmd["request_id"],
                    "bundle_id": cmd["bundle_id"],
                    "command_type": cmd["command_type"],
                    **sent,
                }
            )
        self.record(f"burst-{2 * BURST}-concurrent-posts", problems)

    def events(self):
        path = self.evidence / self.run_id / "events.jsonl"
        problems, events = [], []
        if not path.is_file():
            self.record("events-file", [f"{path} missing"])
            return
        for number, line in enumerate(path.read_text().splitlines(), 1):
            try:
                event = parse_json(line)
                validate(event, "event.schema.json")
            except (ValueError, CheckError) as error:
                problems.append(f"line {number}: {error}")
                continue
            if event["run_id"] != self.run_id or event["model"] != "B1":
                problems.append(f"line {number}: run_id/model")
            events.append(event)
        last = {}
        for event in events:  # file order per producer must follow the sequence
            key = (event["producer_id"], event["clock_id"])
            if key in last and event["sequence"] <= last[key]:
                problems.append(
                    f"{key}: sequence {event['sequence']} after {last[key]}"
                )
            last[key] = event["sequence"]
        self.record("events-schema-and-sequence", problems)

        checked = check_messages(events)
        problems = checked.problems + [
            f"{stem}: no response logged" for stem in checked.unanswered
        ]
        for (_, purpose, _), _ in checked.counts.items():
            # an unprepared run is refused after one read
            if purpose not in ("state", "result", "readiness", "snapshot"):
                problems.append(f"{purpose} messages for an unprepared run")
        # Each POST answered NOT_FOUND was refused after the state read that
        # found the run unprepared; concurrent POSTs of one key may share it.
        reads = {
            (e["platform"], e["request_id"], e["event_type"])
            for e in events
            if e["event_type"] in ("MESSAGE_SENT", "MESSAGE_RECEIVED")
            and e["message_id"].startswith("state:")
        }
        missing = {
            (want["platform"], want["request_id"], kind)
            for want in self.expected_events
            if want["code"] == "NOT_FOUND"
            for kind in ("MESSAGE_SENT", "MESSAGE_RECEIVED")
        } - reads
        problems += [
            f"{p} {request_id}: no state {kind} logged"
            for p, request_id, kind in sorted(missing)
        ]
        # Each GET answered NOT_FOUND after its read left that read, with the
        # GET's test, attempt and query fields and nothing of a command.
        messages = defaultdict(list)
        for e in events:
            if e["event_type"] in ("MESSAGE_SENT", "MESSAGE_RECEIVED"):
                messages[e["attempt_id"]].append(e)
        for want in self.expected_reads:
            got = messages.pop(want["attempt_id"], [])
            wrong = not_one_read(got)
            if wrong:
                problems.append(f"{want['check']}: {wrong}")
            for e in got:
                if not e["message_id"].startswith(want["purpose"] + ":"):
                    problems.append(f"{want['check']}: message {e['message_id']}")
                if e["platform"] != want["platform"] or e["test_id"] != want["test_id"]:
                    problems.append(f"{want['check']}: platform or test_id")
                for key in ("request_id", "bundle_id", "command_type", "actor"):
                    if e.get(key) != want.get(key):
                        problems.append(
                            f"{want['check']}: {e['event_type']} {key}={e.get(key)!r}"
                        )
        posted = {want["attempt_id"] for want in self.expected_events}
        stray = [a for a in messages if a not in posted]
        # A POST without X-Attempt-ID logs under the attempt the adapter made.
        generated = sum(a.startswith("attempt-adapter-") for a in stray)
        allowed = sum(want["attempt_id"] is None for want in self.expected_events)
        extra = len(stray) - generated + max(generated - allowed, 0)
        if extra:  # a refused call, or a GET without X-Test-ID
            problems.append(f"messages for {extra} calls that should leave none")
        self.record(
            f"participant-messages ({len(self.expected_reads)} GETs that read)",
            problems,
            detail=f"{sum(checked.counts.values())} logged",
        )

        problems = []
        by_attempt = {}
        for event in events:
            if event["event_type"] not in ("MESSAGE_SENT", "MESSAGE_RECEIVED"):
                by_attempt.setdefault(event["attempt_id"], []).append(event)
        generated = [a for a in by_attempt if a and a.startswith("attempt-adapter-")]
        for want in self.expected_events:
            attempt = want["attempt_id"]
            if attempt is None:  # the adapter generates one when the header is absent
                attempt = generated.pop() if generated else None
            got = by_attempt.pop(attempt, [])
            kinds = [e["event_type"] for e in got]
            if kinds != ["COMMAND_RECEIVED", "COMMAND_FINISHED"]:
                problems.append(f"{want['check']}: events {kinds}")
                continue
            received, finished = got
            for e in got:
                for key in (
                    "platform",
                    "actor",
                    "request_id",
                    "bundle_id",
                    "command_type",
                ):
                    if e.get(key) != want[key]:
                        problems.append(
                            f"{want['check']}: {e['event_type']} {key}={e.get(key)!r}"
                        )
                wanted_test = (
                    want["test_id"] if want["test_id"] != "bad id" else "NO-TEST-ID"
                )
                if e["test_id"] != wanted_test:
                    problems.append(f"{want['check']}: test_id {e['test_id']!r}")
            if (
                received["error_code"] is not None
                or finished["error_code"] != want["code"]
            ):
                problems.append(
                    f"{want['check']}: error codes {received['error_code']!r},"
                    f" {finished['error_code']!r}"
                )
            if finished["outcome"] is not None or int(finished["mono_ns"]) < int(
                received["mono_ns"]
            ):
                problems.append(f"{want['check']}: FINISHED outcome or time order")
        if by_attempt:
            problems.append(
                f"unexpected events for {len(by_attempt)} calls"
                " (unauthenticated, observer or unattributable)"
            )
        self.record(
            f"events-per-call ({len(self.expected_events)} attributable POSTs)",
            problems,
        )

    def run(self) -> int:
        self.ac03()
        self.unknown_run()
        self.malformed()
        self.burst()
        self.events()
        failed = [r for r in self.results if not r["ok"]]
        for r in self.results:
            status = "ok  " if r["ok"] else "FAIL"
            http = f"{r.get('http', '')} {r.get('code') or ''}".strip()
            http = f"{http} {r.get('detail', '')}".strip()
            print(f"{status} {r['check']:<44} {http}")
            for problem in r["problems"]:
                print(f"       {problem}")
        report = IMPL_DIR / "logs/adapter-selftest" / f"{self.run_id}.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "finished": datetime.now(timezone.utc).isoformat(),
                    "adapters": self.base,
                    "events": str(self.evidence / self.run_id / "events.jsonl"),
                    "passed": len(self.results) - len(failed),
                    "failed": len(failed),
                    "results": self.results,
                },
                indent=2,
            )
            + "\n"
        )
        print(
            f"{len(self.results) - len(failed)}/{len(self.results)} passed; report {report}"
        )
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(Selftest().run())
