#!/usr/bin/env python3
"""Refusals, replays, contention and recovery of the B2 reassignment
(REASSIGN_FOR_LENDING) on the running adapters and Canton (b2 topology).

    . scripts/env.sh && ../../.venv/bin/python -I selftest/b2_stage3_selftest.py [--part 1|2] [VARIANT ...]

A development check, not the common runner and not an acceptance result:
acceptance §8 leaves fault injection and manual recovery out of the accepted
range, and the B2 cases of the common runner (AC-01, AC-02, AC-12) are not run
here. It runs with the repository's .venv, as xyz_stage3_selftest does, and
uses its calls, resends and judgments (tests/conformance/checks.py). Each case
has its own run_id and one X-Test-ID (B2-REASSIGN-<VARIANT>); its S0 contracts
are made by b2_selftest's preparation, which stands in for the common loader.
The flow around the case is b2_selftest's: X (CONFIRM_REPO, the declared
contracts.json), the reassignment as BANK_B, then Y and Z.

Part 1, the adapters as they run:

  reject-absent         in S0: REJECTED PRECONDITION_NOT_MET, S0 stays; the
                        same request again gets the same rejection with
                        replayed=true and its query the original rejection
  reject-version        after X, expected_version 0: VERSION_CONFLICT, the same
  reject-missing-auth   no Authorization header: 401 UNAUTHENTICATED
  reject-invalid-auth   an unknown token: 401 UNAUTHENTICATED
  reject-forbidden      as BANK_A: 403 FORBIDDEN
  reject-after-reassign another request after the reassignment: REJECTED
                        PRECONDITION_NOT_MET (the right is not on S1)
  reject-after-loan     another request after Y: REJECTED RIGHT_UNAVAILABLE or
                        PRECONDITION_NOT_MET, S2 stays
  replay                the committed reassignment sent twice more: replayed
                        true, one INTERNAL_COMMITTED; the same request ID with
                        another body: 409 REQUEST_ID_CONFLICT
  race-other x10        two requests with other IDs released together: one
                        COMMITTED, the other REJECTED (RIGHT_UNAVAILABLE or
                        PRECONDITION_NOT_MET); the two must overlap on the
                        adapter's clock (history_check)
  race-same x3          the same request twice at once: both COMMITTED with
                        the same effect, one INTERNAL_COMMITTED
  dual-use              on S1, Y is not ready and its CONFIRM_LOAN is
                        REJECTED; after the reassignment, an unassignment of
                        the right from S1 sent straight to P_X is refused by
                        the ledger, and P_X shows the right on S2 only
  restart-decided       after a reassignment and a rejected one, scripts/
                        adapter.sh stop and start: queries return them with
                        replayed=false, resends with replayed=true, and the
                        evidence written before is kept

The rejection and authorization cases check that no reassignment event was
logged and that contracts.json still declares the right without evidence;
then the reassignment commits (replayed=false where the request ID is the
same) and Y and Z follow.

Part 2, X's participant reached through selftest/ledger_relay.py: the
adapters are restarted with CANTON_LEDGER_URL_P_X set to the relay (README
§4), and restarted again without it at the end. The relay drops one call
once, read whole and not passed on:

  unassign-request-lost        the unassign call: the right stays on S1
  assign-request-lost-restart  the assign call, after the unassignment: the
                               right is on no synchronizer, and the adapters
                               are restarted before the request is sent again

After the lost call the first answer is 503 UNKNOWN and so is one query.
Where the right is in the middle of the reassignment, the snapshot of X shows
the request as REASSIGNING (blocks_use), Y is not ready and its CONFIRM_LOAN
and another reassignment of the bundle are REJECTED. Across the restart the
evidence written before must be kept. Sent again, the request commits with
replayed=true. Lost answers (the participant acted, the adapter did not hear)
and refused connections are not run here.

Each case states what the common judgments give over its run (reassignment:
runner.reassign's checks, as b2_selftest.reassigned; history: history_check);
a case passes when the adapters answered as above and both judgments are as
stated. Across a restart the reassignment's events are on two adapter clocks,
and that judgment is stated as EvidenceMissing there (see CASES). The report goes
to logs/b2-stage3-selftest/<batch>.json with the relay's log, without
credentials. Exit status 0 when every selected case passed, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
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

# -I leaves the script's directory off sys.path (as in authz_check).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from xyz_stage3_selftest import RestartCase, ZCase, line  # noqa: E402
from xy_stage3_selftest import IMPL_DIR, summarize  # noqa: E402

# tests/conformance is on sys.path from here on (xy_stage3_selftest).
from b2_selftest import B2Selftest  # noqa: E402
from checks import (  # noqa: E402
    CheckError,
    EvidenceMissing,
    canonical,
    check_result,
    history_check,
    parse_json,
    validate,
)
from ledger_relay import REASSIGN_PATH, LedgerRelay  # noqa: E402
from runner import HTTP_SECONDS, OBSERVE_SECONDS  # noqa: E402
from xy_selftest import Failed, http  # noqa: E402

RELAY_PORT = 4519
RELAY_PARTICIPANT = "P_X"
LEDGER_URL_ENV = "CANTON_LEDGER_URL_" + RELAY_PARTICIPANT
REASSIGNMENT_EVENTS = {
    "UNASSIGNMENT_STARTED",
    "UNASSIGNMENT_COMPLETED",
    "ASSIGNMENT_STARTED",
    "ASSIGNMENT_COMPLETED",
    "SOURCE_UNUSABLE",
    "TARGET_USABLE",
}
# The adapter's active-contract filter (server.WILDCARD).
WILDCARD = [
    {
        "identifierFilter": {
            "WildcardFilter": {"value": {"includeCreatedEventBlob": False}}
        }
    }
]
ACS_PATH = "/v2/state/active-contracts-page"

PASS = "PASS"
CLOCKS_DIFFER = "EvidenceMissing: contract reassignment clocks differ"
ON_S1 = [{"kind": "JsActiveContract", "synchronizer": "S1"}]
ON_S2 = [{"kind": "JsActiveContract", "synchronizer": "S2"}]
UNASSIGNED = [{"kind": "JsIncompleteUnassigned", "synchronizer": None}]


def plain(variant: str, repeats: int = 1, **expect) -> list[dict]:
    return [
        {
            "variant": variant,
            "part": 1,
            "repeat": n,
            "expect": {"reassignment": PASS, "history": PASS} | expect,
        }
        for n in range(1, repeats + 1)
    ]


def fault(
    variant: str,
    operation: str,
    ledger: list,
    during: bool,
    restart: bool,
    reassignment: str,
) -> list[dict]:
    """A call the relay does not pass on, then the request sent again."""
    return [
        {
            "variant": variant,
            "part": 2,
            "repeat": 1,
            "fault": operation,
            "ledger": ledger,
            "during": during,
            "restart": restart,
            "expect": {"reassignment": reassignment, "history": PASS},
        }
    ]


CASES = (
    plain("reject-absent")
    + plain("reject-version")
    + plain("reject-missing-auth")
    + plain("reject-invalid-auth")
    + plain("reject-forbidden")
    + plain("reject-after-reassign")
    + plain("reject-after-loan")
    + plain("replay")
    + plain("race-other", 10)
    + plain("race-same", 3)
    + plain("dual-use")
    + plain("restart-decided")
    # The unassign never reached the ledger: sent again, it is made as if new.
    + fault("unassign-request-lost", "UNASSIGN", ON_S1, False, False, PASS)
    # The unassignment was made and the assign call lost: the right is between
    # the synchronizers until the request, sent again after a restart, assigns
    # it. The unassignment's and the assignment's events are on two clocks.
    + fault(
        "assign-request-lost-restart", "ASSIGN", UNASSIGNED, True, True, CLOCKS_DIFFER
    )
)
VARIANTS = tuple(dict.fromkeys(c["variant"] for c in CASES))


def case_of(spec: dict) -> dict:
    return {
        "test_id": "B2-REASSIGN-" + spec["variant"].upper(),
        "fixture": "f0-normal",
    } | spec


def matches(wanted: str, got: str) -> bool:
    return got == wanted if wanted == PASS else got.startswith(wanted)


def unwrap(value) -> dict:
    """A Ledger API JSON value, with or without its {"value": ...} wrapper."""
    if not isinstance(value, dict):
        return {}
    inner = value.get("value", value)
    return inner if isinstance(inner, dict) else {}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class B2Case(ZCase, B2Selftest):
    """One case on its own run: ZCase's calls and evidence with b2_selftest's
    preparation (each contract on its synchronizer) and declared set."""

    model = "B2"
    BEFORE = RestartCase.BEFORE
    restart = RestartCase.restart

    def __init__(self, run_id: str, case: dict, state: Path, relay: LedgerRelay | None):
        super().__init__(run_id, case)
        self.state_dir = state
        self.relay = relay
        self.reassign_cmd = copy.deepcopy(self.reassignment)

    def read_logs(self, state: Path):
        self.adapter_log(state, (self.BEFORE, "adapter-{p}.out"))

    def extend(self):
        """A new observation window for the next step (a restart is not
        observation time)."""
        self.deadline = time.monotonic() + OBSERVE_SECONDS

    # --- calls ----------------------------------------------------------------

    def variant_of(self, suffix: str, **changes) -> dict:
        """Another reassignment request: its own request ID."""
        command = copy.deepcopy(self.reassign_cmd)
        command["request_id"] += suffix
        command.update(changes)
        return command

    async def reassign(self, command=None, **options):
        options.setdefault("role", "bank-b")
        return await self.send("X", command or self.reassign_cmd, **options)

    async def reassign_refusal(self, command, codes, **options):
        options.setdefault("role", "bank-b")
        return await self.refusal("X", command, codes=codes, **options)

    async def first(self, command, role="bank-b"):
        """One POST, without send()'s queries and resends."""
        command = copy.deepcopy(command)
        status, body = await self.call("POST", "X", "commands", role, command=command)
        if body is None:
            raise EvidenceMissing("the first POST got no answer")
        check_result(body, self.run_id, command, self.actors[role], status, None)
        if body.get("outcome") in ("COMMITTED", "REJECTED"):
            self.finals.append((command, body))
        self.detail["first"] = self.answer(status, body)
        return status, body

    async def query(self, command, role="bank-b"):
        """One query of the request: not a replay."""
        status, body = await self.call(
            "GET", "X", "commands/" + command["request_id"], role
        )
        if body is None:
            raise EvidenceMissing("result query failed")
        check_result(body, self.run_id, command, self.actors[role], status, False)
        return status, body

    @staticmethod
    def answer(status, body) -> dict:
        return {
            "status": status,
            "outcome": body.get("outcome"),
            "code": (body.get("error") or {}).get("code"),
            "replayed": body.get("replayed"),
        }

    async def resend(self, expect_replayed: bool):
        """The reassignment sent again as the runner does (send()); a POST
        that commits says whether it was a replay."""
        self.extend()
        mark, started = len(self.calls), time.monotonic()
        result = await self.reassign()
        post = self.calls[mark]
        self.detail["resend"] = {
            "ms": round((time.monotonic() - started) * 1000, 1),
            "calls": len(self.calls) - mark,
            "post": {k: post.get(k) for k in ("status", "outcome", "code", "replayed")},
        }
        self.require_committed(result)
        if post.get("outcome") == "COMMITTED" and post.get("replayed") is not (
            expect_replayed
        ):
            raise CheckError(
                f"the resent reassignment answered replayed={post.get('replayed')}"
            )
        return result

    async def readiness_once(self, platform):
        command = self.commands[platform]
        params = {
            "command_type": command["command_type"],
            "bundle_id": command["bundle_id"],
        }
        for name in ("agreement_id", "margin_call_id"):
            if name in command["payload"]:
                params[name] = command["payload"][name]
        status, body = await self.call(
            "GET", platform, "readiness", self.roles[platform], params=params
        )
        if body is None:
            raise EvidenceMissing("readiness observation failed")
        validate(body, "readiness.schema.json")
        if status != 200 or "ready" not in body:
            raise EvidenceMissing("readiness HTTP/error response")
        return body

    async def pending_rows(self) -> list:
        status, body = await self.call("GET", "X", "snapshot", "observer")
        if status != 200 or not isinstance(body, dict):
            raise EvidenceMissing(f"snapshot X: {status}")
        return body["pending_operations"]

    # --- the ledger, straight from P_X (not through the relay) ---------------

    def contract_id(self) -> str:
        if self.selected is None:
            raise Failed("no declared set read")
        return self.selected["required_contracts"][0]["contract_id"]

    def ledger_view(self) -> list[dict]:
        """Where P_X shows the declared right, as BANK_B sees it."""
        cid = self.contract_id()
        party = self.parties["BANK_B"]
        names = {v: k for k, v in self.synchronizers.items()}
        query: dict = {
            "eventFormat": {
                "filtersByParty": {party: {"cumulative": WILDCARD}},
                "verbose": False,
            },
            "maxPageSize": 500,
            "includeIncompleteReassignments": True,
        }
        seen = []
        while True:
            status, page = http("POST", self.ledger["P_X"] + ACS_PATH, body=query)
            if status != 200 or not isinstance(page, dict):
                raise Failed(f"P_X active contracts: {status} {str(page)[:300]}")
            for entry in page.get("activeContracts", []):
                for kind, value in entry.get("contractEntry", {}).items():
                    value = unwrap(value)
                    # As the adapter reads them (server.incomplete_event).
                    created = unwrap(
                        unwrap(value.get("assignedEvent")).get("createdEvent")
                        if kind == "JsIncompleteAssigned"
                        else value.get("createdEvent")
                    )
                    if created.get("contractId") == cid:
                        seen.append(
                            {
                                "kind": kind,
                                "synchronizer": names.get(value.get("synchronizerId")),
                            }
                        )
            token = page.get("nextPageToken")
            if not token:
                return seen
            query["pageToken"] = token

    async def ledger_is(self, wanted: list, what: str):
        seen = await asyncio.to_thread(self.ledger_view)
        self.detail.setdefault("ledger", {})[what] = seen
        if seen != wanted:
            raise CheckError(f"P_X shows the right {seen} {what}, not {wanted}")

    def direct_unassign(self) -> dict:
        """An unassignment of the right from S1 sent straight to P_X, after
        the right moved to S2: the ledger must refuse it."""
        party = self.parties["BANK_B"]
        body = {
            "reassignmentCommands": {
                "submitter": party,
                "commandId": "b2-stage3-dualuse-" + uuid.uuid4().hex,
                "userId": "b2-stage3-selftest",
                "commands": [
                    {
                        "command": {
                            "UnassignCommand": {
                                "value": {
                                    "contractId": self.contract_id(),
                                    "source": self.synchronizers["S1"],
                                    "target": self.synchronizers["S2"],
                                }
                            }
                        }
                    }
                ],
            },
            "eventFormat": {"filtersByParty": {party: {}}},
        }
        status, reply = http("POST", self.ledger["P_X"] + REASSIGN_PATH, body=body)
        found = {"status": status}
        if isinstance(reply, dict):
            found.update(
                code=reply.get("code"), cause=str(reply.get("cause", ""))[:300]
            )
        return found

    # --- evidence -------------------------------------------------------------

    def run_dir(self) -> Path:
        return self.evidence / self.run_id

    def run_events(self) -> list[dict]:
        path = self.run_dir() / "events.jsonl"
        if not path.is_file():
            raise EvidenceMissing(f"{path} missing")
        return [parse_json(x) for x in path.read_text().splitlines() if x]

    def no_reassignment(self, what: str):
        """A refused request logged no reassignment event and left the
        declared set as X wrote it."""
        logged = [
            e["event_type"]
            for e in self.run_events()
            if e["event_type"] in REASSIGNMENT_EVENTS
        ]
        if logged:
            raise CheckError(f"{what} logged reassignment events {logged}")
        if self.manifest_path().is_file():
            self.declared()

    def evidence_mark(self) -> dict:
        run = self.run_dir()
        return {
            "events": (run / "events.jsonl").read_bytes(),
            "files": {
                str(p.relative_to(run)): sha256(p)
                for p in sorted(run.rglob("*"))
                if p.is_file() and p.name not in ("events.jsonl", "contracts.json")
            },
            "selection": canonical(self.declared_selection()),
        }

    def declared_selection(self) -> list:
        from b2_selftest import selection

        return selection(json.loads(self.manifest_path().read_text()))

    def evidence_kept(self, mark: dict):
        """What was logged and written before the restart is still there:
        events.jsonl only grew, every raw file is unchanged, and contracts.json
        declares the same contracts."""
        run = self.run_dir()
        events = (run / "events.jsonl").read_bytes()
        changed = [
            p
            for p, digest in mark["files"].items()
            if not (run / p).is_file() or sha256(run / p) != digest
        ]
        self.detail["evidence_kept"] = {
            "events_bytes": [len(mark["events"]), len(events)],
            "files_before": len(mark["files"]),
            "changed_or_lost": changed,
        }
        if (
            not events.startswith(mark["events"])
            or changed
            or canonical(self.declared_selection()) != mark["selection"]
        ):
            raise CheckError("evidence written before the restart changed")

    async def restarted(self) -> dict:
        """The adapters restarted (RestartCase.restart), the relay kept."""
        mark = await asyncio.to_thread(self.evidence_mark)
        await self.client.aclose()
        await asyncio.to_thread(self.restart)
        if self.relay is not None:
            ready = json.loads((self.state_dir / "adapter-X.ready").read_text())
            if ready.get("ledger") != self.relay.url or not ready.get(
                "ledger_override"
            ):
                raise Failed("the restarted X adapter does not reach the relay")
        return mark

    # --- steps ------------------------------------------------------------------

    async def repo_confirmed(self):
        self.require_committed(await self.send("X"))
        await self.state(1)
        self.declared()

    async def reassigned_once(self, replay=None):
        self.extend()
        result = await self.reassign(replay=replay)
        self.require_committed(result)
        await self.state(1)
        return result

    async def onward(self, after_y=None):
        """Y and Z after the reassignment, as b2_selftest."""
        self.extend()
        await self.ready("Y")
        self.require_committed(await self.send("Y"))
        await self.state(2)
        if after_y is not None:
            await after_y()
        self.extend()
        await self.ready("Z")
        self.require_committed(await self.send("Z"))
        await self.state(3)

    async def advance(self, version):
        await self.repo_confirmed()
        if version >= 1:
            await self.reassigned_once()
        if version >= 3:
            await self.onward()

    async def rejected_twice(self, command, codes, version):
        """A rejection, the same request again and its query (interface §6.3)."""
        first = await self.reassign_refusal(command, codes)
        await self.state(version)
        again = await self.reassign_refusal(
            command, (first["error"]["code"],), replay=True
        )
        _, original = await self.query(command)
        if original.get("outcome") != "REJECTED" or (
            again["error"],
            original["error"],
        ) != (first["error"], first["error"]):
            raise CheckError("the resent or queried rejection differs from the first")
        await self.state(version)
        self.detail.setdefault("rejections", []).append(
            {"request_id": command["request_id"], "code": first["error"]["code"]}
        )
        return first

    # --- part 1 -------------------------------------------------------------------

    async def scenario(self, variant):
        self.extend()
        if variant == "reject-absent":
            command = self.variant_of("-absent")
            await self.rejected_twice(command, ("PRECONDITION_NOT_MET",), 0)
            self.no_reassignment(variant)
            await self.advance(3)
        elif variant == "reject-version":
            await self.repo_confirmed()
            command = self.variant_of("-version", expected_version=0)
            await self.rejected_twice(command, ("VERSION_CONFLICT",), 1)
            self.no_reassignment(variant)
            await self.reassigned_once(replay=False)
            await self.onward()
        elif variant in (
            "reject-missing-auth",
            "reject-invalid-auth",
            "reject-forbidden",
        ):
            await self.repo_confirmed()
            if variant == "reject-missing-auth":
                options, codes = {"token_override": ""}, ("UNAUTHENTICATED",)
            elif variant == "reject-invalid-auth":
                options = {"token_override": "invalid-" + uuid.uuid4().hex}
                codes = ("UNAUTHENTICATED",)
            else:
                options, codes = {"role": "bank-a"}, ("FORBIDDEN",)
            await self.reassign_refusal(
                self.reassign_cmd, codes, preacceptance=True, **options
            )
            await self.state(1)
            self.no_reassignment(variant)
            # Refused before acceptance: the request ID was not kept.
            await self.reassigned_once(replay=False)
            await self.onward()
        elif variant == "reject-after-reassign":
            await self.advance(1)
            await self.rejected_twice(
                self.variant_of("-other"), ("PRECONDITION_NOT_MET",), 1
            )
            await self.onward()
        elif variant == "reject-after-loan":
            await self.advance(1)

            async def after_loan():
                await self.rejected_twice(
                    self.variant_of("-other"),
                    ("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"),
                    2,
                )

            await self.onward(after_loan)
        elif variant == "replay":
            await self.repo_confirmed()
            await self.reassigned_once(replay=False)
            for _ in range(2):
                self.require_committed(await self.reassign(replay=True))
            await self.state(1)
            other = copy.deepcopy(self.reassign_cmd)
            other["expected_version"] = 2
            await self.reassign_refusal(
                other, ("REQUEST_ID_CONFLICT",), preacceptance=True
            )
            _, original = await self.query(self.reassign_cmd)
            self.require_committed(original)
            self.detail["internal_commits"] = self.reassign_cmd["request_id"]
            await self.onward()
        elif variant == "race-other":
            await self.repo_confirmed()
            await self.race_other()
            await self.onward()
        elif variant == "race-same":
            await self.repo_confirmed()
            await self.race_same()
            await self.onward()
        elif variant == "dual-use":
            await self.dual_use()
        elif variant == "restart-decided":
            await self.restart_decided()
        elif "fault" in self.case:
            await self.lost()
        else:
            raise CheckError(f"unknown B2 scenario {variant}")

    async def together(self, commands):
        barrier = asyncio.Event()

        async def call(command):
            await barrier.wait()
            return await self.reassign(command)

        tasks = [asyncio.create_task(call(c)) for c in commands]
        barrier.set()
        # Both calls finish before a failure of either is raised.
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[dict] = []
        for r in gathered:
            if isinstance(r, BaseException):
                raise r
            if r is None:
                raise EvidenceMissing("a race call ended without a result")
            results.append(r)
        self.detail["race"] = [
            {
                "request_id": c["request_id"],
                "outcome": r.get("outcome"),
                "code": (r.get("error") or {}).get("code"),
                "replayed": r.get("replayed"),
            }
            for c, r in zip(commands, results)
        ]
        return results

    async def race_other(self):
        first = copy.deepcopy(self.reassign_cmd)
        second = self.variant_of("-other")
        results = await self.together((first, second))
        won = [i for i, r in enumerate(results) if r.get("outcome") == "COMMITTED"]
        if len(won) != 1:
            raise CheckError("race requires exactly one committed reassignment")
        loser = results[1 - won[0]]
        if loser.get("outcome") != "REJECTED" or (loser.get("error") or {}).get(
            "code"
        ) not in ("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"):
            raise CheckError("unexpected race loser")
        self.reassign_cmd = (first, second)[won[0]]
        self.race_requests = [first["request_id"], second["request_id"]]
        await self.state(1)

    async def race_same(self):
        command = copy.deepcopy(self.reassign_cmd)
        results = await self.together((command, copy.deepcopy(command)))
        for r in results:
            self.require_committed(r)
        if results[0]["effect"] != results[1]["effect"]:
            raise CheckError("the same request committed with two effects")
        self.race_requests = [command["request_id"]]
        self.detail["internal_commits"] = command["request_id"]
        await self.state(1)

    async def dual_use(self):
        await self.repo_confirmed()
        # On S1: Y cannot use the right.
        ready = await self.readiness_once("Y")
        self.detail["readiness_Y_on_S1"] = ready
        if ready["ready"] or ready.get("blocking_reasons") != ["RIGHT_UNAVAILABLE"]:
            raise CheckError(f"Y readiness with the right on S1: {ready}")
        early = copy.deepcopy(self.commands["Y"])
        early["request_id"] += "-early"
        result = await self.send("Y", early)
        self.detail["loan_on_S1"] = self.answer(200, result)
        if result.get("outcome") != "REJECTED" or (result.get("error") or {}).get(
            "code"
        ) not in ("RIGHT_UNAVAILABLE", "PRECONDITION_NOT_MET"):
            raise CheckError(f"CONFIRM_LOAN with the right on S1: {result}")
        await self.state(1)
        await self.reassigned_once()
        # On S2: S1 cannot use it any more.
        refused = await asyncio.to_thread(self.direct_unassign)
        self.detail["unassign_from_S1_after_move"] = refused
        if refused["status"] == 200:
            raise CheckError("the ledger unassigned the right from S1 after it moved")
        await self.ledger_is(ON_S2, "after the reassignment")
        await self.onward()

    async def restart_decided(self):
        await self.advance(1)
        other = self.variant_of("-other")
        rejected = await self.reassign_refusal(other, ("PRECONDITION_NOT_MET",))
        await self.state(1)
        mark = await self.restarted()
        for command, role in (
            (self.commands["X"], "bank-a"),
            (self.reassign_cmd, "bank-b"),
        ):
            status, body = await self.call(
                "GET", "X", "commands/" + command["request_id"], role
            )
            if body is None:
                raise EvidenceMissing("result query failed")
            check_result(body, self.run_id, command, self.actors[role], status, False)
            self.require_committed(body)
        self.require_committed(await self.reassign(replay=True))
        _, original = await self.query(other)
        again = await self.reassign_refusal(
            other, (rejected["error"]["code"],), replay=True
        )
        if original.get("outcome") != "REJECTED" or (
            original["error"],
            again["error"],
        ) != (rejected["error"], rejected["error"]):
            raise CheckError("the rejection after the restart differs from the first")
        await self.state(1)
        self.evidence_kept(mark)
        await self.onward()

    # --- part 2 -------------------------------------------------------------------

    def relay_or_fail(self) -> LedgerRelay:
        if self.relay is None:
            raise Failed("part 2 runs with the relay")
        return self.relay

    async def lost(self):
        """A lost unassign or assign call, then the request again."""
        relay = self.relay_or_fail()
        operation = self.case["fault"]
        await self.repo_confirmed()
        command = self.reassign_cmd
        relay.arm(operation)
        faulted = time.monotonic()
        status, body = await self.first(command)
        if relay.pending():
            relay.disarm()
            raise EvidenceMissing(f"the relay did not drop the {operation} call")
        if status != 503 or body.get("outcome") != "UNKNOWN":
            raise CheckError(f"first answer {self.detail['first']}, not 503 UNKNOWN")
        status, body = await self.query(command)
        self.detail["query"] = self.answer(status, body)
        if status != 503 or body.get("outcome") != "UNKNOWN":
            raise CheckError(f"query {self.detail['query']}, not 503 UNKNOWN")
        rows = await self.pending_rows()
        self.detail["pending_after_fault"] = rows
        await self.ledger_is(self.case["ledger"], "after the fault")
        if self.case["during"]:
            await self.blocked_during(rows)
        mark = await self.restarted() if self.case["restart"] else None
        self.detail["seconds_fault_to_resend"] = round(time.monotonic() - faulted, 1)
        await self.resend(expect_replayed=True)
        if mark is not None:
            self.evidence_kept(mark)
        await self.state(1)
        await self.ledger_is(ON_S2, "after the resend")
        await self.onward()

    async def blocked_during(self, rows):
        """In the middle of the reassignment nothing can use the right."""
        request_id = self.reassign_cmd["request_id"]
        if not any(
            r["request_id"] == request_id
            and r["kind"] == "REASSIGNING"
            and r["blocks_use"]
            for r in rows
        ):
            raise CheckError(f"X shows no REASSIGNING row for {request_id}: {rows}")
        ready = await self.readiness_once("Y")
        self.detail["readiness_Y_during"] = ready
        if ready["ready"]:
            raise CheckError("Y was ready with the right in a reassignment")
        loan = copy.deepcopy(self.commands["Y"])
        loan["request_id"] += "-during"
        result = await self.send("Y", loan)
        self.detail["loan_during"] = self.answer(200, result)
        if result.get("outcome") != "REJECTED":
            raise CheckError(f"CONFIRM_LOAN during the reassignment: {result}")
        other = await self.reassign_refusal(
            self.variant_of("-during"), ("RIGHT_UNAVAILABLE",)
        )
        self.detail["reassign_during"] = other["error"]

    # --- judgments --------------------------------------------------------------

    def history(self):
        events = self.run_events()
        if not events:
            raise EvidenceMissing("event log empty")
        for event in events:
            validate(event, "event.schema.json")
            if (
                event["run_id"] != self.run_id
                or event["model"] != self.model
                or event["test_id"] not in (self.test_id, "PREPARE")
            ):
                raise CheckError("event identity mismatch")
        judged = {}
        try:
            self.detail["metrics"] = self.reassigned()
            judged["reassignment"] = PASS
        except (EvidenceMissing, CheckError) as error:
            judged["reassignment"] = f"{type(error).__name__}: {error}"
        try:
            history_check(events, self.finals, self.race_requests)
            judged["history"] = PASS
        except (EvidenceMissing, CheckError) as error:
            judged["history"] = f"{type(error).__name__}: {error}"
        self.detail["judged"] = judged
        self.detail["events"] = dict(Counter(e["event_type"] for e in events))
        self.detail["unassign_calls_sent"] = sum(
            e["event_type"] == "MESSAGE_SENT"
            and e.get("message_kind") == "REQUEST"
            and (e.get("message_id") or "").startswith("unassign:")
            for e in events
        )
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
        expect = self.case["expect"]
        if expect is not None:
            differ = {
                name: {"stated": wanted, "judged": judged[name]}
                for name, wanted in expect.items()
                if not matches(wanted, judged[name])
            }
            if differ:
                raise CheckError(f"judgments differ from the case: {differ}")

    async def execute(self, state: Path) -> dict:
        entry = await super().execute(state)
        if self.relay is not None and self.relay.pending():
            self.relay.disarm()
            entry["relay_left_armed"] = True
        entry["expect"] = self.case["expect"]
        return entry


# --- the relay part -------------------------------------------------------------


def adapters(action: str) -> dict:
    started = time.monotonic()
    done = subprocess.run(
        ["bash", str(IMPL_DIR / "scripts/adapter.sh"), action],
        cwd=IMPL_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if done.returncode != 0:
        raise SystemExit(
            f"adapter.sh {action} exited {done.returncode}: {done.stderr.strip()[-400:]}"
        )
    return {"action": action, "seconds": round(time.monotonic() - started, 1)}


def keep_outputs(state: Path, name: str):
    """adapter.sh start writes adapter-<P>.out anew: keep the old one."""
    logs = state / "logs"
    for p in B2Case.platforms:
        out = logs / f"adapter-{p}.out"
        if out.is_file():
            shutil.copy2(out, logs / name.format(p=p))


def ready_of(state: Path) -> dict:
    return {
        p: {
            k: v
            for k, v in json.loads((state / f"adapter-{p}.ready").read_text()).items()
            if k in ("url", "ledger", "ledger_override", "clock_id")
        }
        for p in B2Case.platforms
    }


def relay_of(state: Path, log: Path) -> LedgerRelay:
    ids = json.loads((state / "ids.json").read_text())
    relay = LedgerRelay(ids["json_api"][RELAY_PARTICIPANT], RELAY_PORT, log)
    relay.start()
    return relay


def relay_up(state: Path, batch: str, relay: LedgerRelay) -> dict:
    """Restart the adapters with P_X calling through the relay. Called inside
    the part's try, so relay_down puts them back even when this fails."""
    keep_outputs(state, f"adapter-{{p}}.before-relay-{batch}.out")
    os.environ[LEDGER_URL_ENV] = relay.url
    steps = [adapters("stop"), adapters("start")]
    ready = ready_of(state)
    wrong = [
        p
        for p, r in ready.items()
        if (r.get("ledger_override") is True) != (p == "X")
        or (p == "X" and r.get("ledger") != relay.url)
    ]
    if wrong:
        raise SystemExit(f"adapters {wrong} do not reach the ledger as set: {ready}")
    return {"steps": steps, "ready": ready}


def relay_down(state: Path, batch: str, relay: LedgerRelay) -> dict:
    """Restart the adapters without the relay, then stop it (also when the
    restart fails)."""
    os.environ.pop(LEDGER_URL_ENV, None)
    try:
        keep_outputs(state, f"adapter-{{p}}.relay-{batch}.out")
        steps = [adapters("stop"), adapters("start")]
    finally:
        relay.stop()
    ready = ready_of(state)
    if any(r.get("ledger_override") for r in ready.values()):
        raise SystemExit(f"an adapter still reaches the relay: {ready}")
    return {"steps": steps, "ready": ready}


def source() -> dict:
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=IMPL_DIR, capture_output=True, text=True, timeout=30
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "changed": git("status", "--porcelain", "--untracked-files=all").splitlines(),
    }


async def main(arguments: list[str]) -> int:
    parts, selected, rest = {1, 2}, [], list(arguments)
    while rest:
        argument = rest.pop(0)
        if argument == "--part":
            parts = {int(rest.pop(0))}
        else:
            selected.append(argument)
    unknown = [v for v in selected if v not in VARIANTS] + [
        p for p in parts if p not in (1, 2)
    ]
    if unknown:
        raise SystemExit(f"unknown {unknown}; variants: {', '.join(VARIANTS)}")
    if not os.environ.get("CANTON_STATE_DIR"):
        raise SystemExit("CANTON_STATE_DIR is not set; run . scripts/env.sh first")
    if os.environ.get(LEDGER_URL_ENV):
        raise SystemExit(f"{LEDGER_URL_ENV} is set; part 2 sets it itself")
    state = Path(os.environ["CANTON_STATE_DIR"])
    cases = [
        case_of(c)
        for c in CASES
        if c["part"] in parts and (not selected or c["variant"] in selected)
    ]
    batch = secrets.token_hex(4)
    out_dir = IMPL_DIR / "logs/b2-stage3-selftest"
    out_dir.mkdir(parents=True, exist_ok=True)
    relay_log = out_dir / f"{batch}-relay.jsonl"
    started_utc = datetime.now(timezone.utc).isoformat()
    entries, relay_steps = [], {}
    for part in (1, 2):
        chosen = [c for c in cases if c["part"] == part]
        if not chosen:
            continue
        relay = relay_of(state, relay_log) if part == 2 else None
        try:
            if relay is not None:
                relay_steps["up"] = relay_up(state, batch, relay)
            for case in chosen:
                run_id = f"b2-stage3-{batch}-{len(entries) + 1:02}"
                entry = await B2Case(run_id, case, state, relay).execute(state)
                entry["part"] = part
                entries.append(entry)
                print(line(entry, case), flush=True)
        finally:
            if relay is not None:
                relay_steps["down"] = relay_down(state, batch, relay)
    passed = sum(e["verdict"] == "PASS" for e in entries)
    judged = Counter(
        f"{name} {e['detail'].get('judged', {}).get(name, 'not judged')}"
        for e in entries
        for name in ("reassignment", "history")
    )
    report = {
        "batch": batch,
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Development check of the B2 reassignment; not the common runner "
            "and not an acceptance (AC) result. A case's PASS means its stated "
            "answers and judgments held, not that the common checks passed."
        ),
        "source": source(),
        "parts": sorted(parts),
        "selected": selected or list(VARIANTS),
        "response_wait": os.environ.get("CANTON_RESPONSE_WAIT")
        or "4.0 (adapter default)",
        "http_seconds": HTTP_SECONDS,
        "relay": {
            "port": RELAY_PORT,
            "participant": RELAY_PARTICIPANT,
            "log": relay_log.name if relay_log.exists() else None,
            **relay_steps,
        },
        "summary": summarize(entries),
        "judged": dict(sorted(judged.items())),
        "passed": passed,
        "planned": len(cases),
        "cases": entries,
    }
    out = out_dir / f"{batch}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"summary": report["summary"], "judged": report["judged"]}))
    print(f"{passed}/{len(cases)} passed; report {out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
