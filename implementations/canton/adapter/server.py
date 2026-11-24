"""Platform adapter of the Canton B1 and B2 models (implementations/canton/README.md §2).

One process per platform: X (Repo, on participant P_X), Y (securities
lending, on P_Y) and Z (CCP margin, on P_Z). It serves the common HTTP
interface (spec/interface.md §4) and reaches its participant through the JSON
Ledger API v2 on localhost.

Stage 1: start-up (DAR upload, warm-up transaction), Bearer authentication,
request validation against shared/schemas, platform permissions, the error
responses of interface §9 and the implementation event log
(tests/conformance/README.md §14).

Stage 2 (X→Y normal path, README §3): CONFIRM_REPO on X and CONFIRM_LOAN on
Y exercise the pre-agreement's choice on the ledger, the result query, the Y
readiness and the snapshot. Every read takes the run's active contracts from
the participant at that moment.

Stage 3 (README §3 "결과 상태와 멱등성"): rejections with the common codes,
idempotent replay per (run, authenticated actor, request id), request id
conflicts, contention and recovery. Committed requests are kept by the
ledger (the trade contract names its request); rejected requests, and on B2
the reassignments, are kept in
$CANTON_STATE_DIR/requests/<platform>/<run_id>.jsonl; requests in progress
and submissions whose ledger answer was lost are kept in this process only.

Z (README §3 "Z: CCP 증거금 배정·잠금"): ALLOCATE_MARGIN on Z exercises the
margin call's choice, which locks BANK_C's right in place, creates the
allocation and satisfies the margin call; the Z readiness and the snapshot's
margin calls and allocations. The ledger must have been bootstrapped with the
b1-xyz topology (ids.json); the adapter refuses to start on another one.

B2 (README §3 B2): on a ledger bootstrapped with the b2 topology (S1 and S2)
the same adapters run X on S1 and Y and Z on S2. X also takes
REASSIGN_FOR_LENDING from BANK_B: it declares the right Y needs in
<run_id>/contracts.json once CONFIRM_REPO creates it, unassigns it from S1
and assigns it to S2 through the Ledger API, keeps each call's raw request
and response under <run_id>/reassignment/, and logs the reassignment events
of event.schema with SOURCE_UNUSABLE when S1 gives the right up and
TARGET_USABLE when P_X shows it on S2.

Participant calls (README §3 "Participant 호출 기록", measurement.md §8.3): every
call a command makes to the participant (the submission, the state reads that
decide and recheck it, the reads for LOCAL_APPLIED) is logged as a
MESSAGE_SENT request and a MESSAGE_RECEIVED response under the command's
run, test and request fields. The state reads that answer a result query, a
readiness query or a snapshot are logged the same way under that GET's
X-Test-ID/X-Attempt-ID and query fields, with no command events; a GET without
X-Test-ID logs none. Calls made at start-up are not logged.

    .venv/bin/python -I adapter/server.py --platform X

Environment (scripts/adapter.sh provides it through scripts/env.sh):
  CANTON_STATE_DIR     ids.json from the Canton bootstrap; adapter-<P>.ready;
                       requests/<P>/ (rejection and reassignment records)
  POC_BANK_A_TOKEN, POC_BANK_B_TOKEN, POC_BANK_C_TOKEN, POC_OBSERVER_TOKEN
                       local credentials; their values are never logged
  CANTON_EVIDENCE_DIR  root of <run_id>/events.jsonl, and on B2 of
                       <run_id>/contracts.json and <run_id>/reassignment/
                       (default: logs/)
  CANTON_RESPONSE_WAIT seconds a submission waits for its final result before
                       it answers PENDING (default 4.0, interface §6.2)
"""

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import signal
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientSession,
    ClientTimeout,
    TraceConfig,
    web,
)
from jsonschema import Draft202012Validator

IMPL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = IMPL_DIR.parent.parent / "shared/schemas"
DAR = IMPL_DIR / "daml/collateral/.daml/dist/collateral-0.4.0.dar"
PACKAGE = "collateral"
# The ledger topologies this DAR and PLATFORMS belong to (ids.json, written by
# canton/bootstrap-b1-xy.canton as "b1-xyz" and by canton/bootstrap-b2.canton).
# The X→Y evidence ledgers are "b1-xy".
B1_TOPOLOGY = "b1-xyz"
B2_TOPOLOGY = "b2"
# B2 (README §2): the synchronizer each platform's contracts and transactions
# are on. X stays on S1; Y and Z work on S2.
HOME = {"X": "S1", "Y": "S2", "Z": "S2"}
WARMUP_TEMPLATE = f"#{PACKAGE}:Collateral.Warmup:Warmup"
# Business templates by module:entity (README §3). The ledger reports a
# contract's template as <package id>:<module>:<entity>.
RIGHT = "Collateral.Right:UseRight"
REPO_AGREEMENT = "Collateral.Agreement:RepoAgreement"
LOAN_AGREEMENT = "Collateral.Agreement:LoanAgreement"
TRADE = "Collateral.Record:TradeContract"
OBLIGATION = "Collateral.Record:Obligation"
MARGIN_CALL = "Collateral.Margin:MarginCall"
ALLOCATION = "Collateral.Margin:Allocation"
WARMUP_COUNT = 2
VETTING_TIMEOUT = 60  # seconds for every participant's vetting to reach S1
LEDGER_TIMEOUT = ClientTimeout(total=60)
ACS_PAGE_SIZE = 500
# How long the confirming adapter looks for the new right among its
# participant's active contracts before it gives up on LOCAL_APPLIED. It is
# part of the submission's response time, so it stays well below the
# response wait.
LOCAL_WAIT = 2.0
LOCAL_POLL = 0.05
SUBMIT_PATH = "/v2/commands/submit-and-wait-for-transaction"
REASSIGN = "REASSIGN_FOR_LENDING"
REASSIGN_PATH = "/v2/commands/submit-and-wait-for-reassignment"
# B2: how long the X adapter looks for a reassigned contract among its
# participant's active contracts on S2 before it gives up on TARGET_USABLE.
USABLE_WAIT = LOCAL_WAIT
# Participant calls logged as messages (README §3 "Participant 호출 기록"), by
# the purpose that starts their message IDs: the submission (and its
# resubmissions), the state read that decides a command, the reads again while
# no right of the bundle is visible, the reads that explain an unconfirmed
# submission, the reads that look for the new right for LOCAL_APPLIED, and the
# state reads that answer the common runner's result, readiness and snapshot
# GETs. The categories of local-check and of the GET reads are this
# implementation's, not an approved common rule.
MESSAGE_PURPOSES = {
    "submit": "BUSINESS_INTEGRATION",
    "state": "BUSINESS_INTEGRATION",
    "poll": "BUSINESS_INTEGRATION",
    "recheck": "BUSINESS_INTEGRATION",
    "local-check": "OPERATIONAL",
    # B2 REASSIGN_FOR_LENDING: the unassignment and assignment of a contract,
    # and the reads that look for it on S2 for TARGET_USABLE (like local-check).
    "unassign": "BUSINESS_INTEGRATION",
    "assign": "BUSINESS_INTEGRATION",
    "usable-check": "OPERATIONAL",
    "result": "HARNESS",
    "readiness": "HARNESS",
    "snapshot": "HARNESS",
}
# The GET reads: no command is received or finished for them.
QUERY_PURPOSES = ("result", "readiness", "snapshot")
# interface §6.2: the response wait is one experiment condition for every
# model. 4 s keeps the answer inside the common runner's 5 s HTTP timeout.
RESPONSE_WAIT_ENV = "CANTON_RESPONSE_WAIT"
RESPONSE_WAIT_DEFAULT = 4.0
# The prefix of the variable that points a participant's calls at another
# address, e.g. CANTON_LEDGER_URL_P_X (ledger_url).
LEDGER_URL_ENV = "CANTON_LEDGER_URL_"
# B2: the MESSAGE_SENT message_id of an unassign or assign call (Call.call_id),
# and the contracts.json reference each call's completion event fills.
CALL_MESSAGE = re.compile(r"(unassign|assign):[0-9a-f]{32}:req")
EVIDENCE_OF = {"UNASSIGN": "unassigned_evidence", "ASSIGN": "assigned_evidence"}
COMPLETION_OF = {
    "UNASSIGN": ("UNASSIGNMENT_COMPLETED", "JsUnassignedEvent"),
    "ASSIGN": ("ASSIGNMENT_COMPLETED", "JsAssignmentEvent"),
}
# When no right of the bundle is visible, the participant may not have applied
# the preceding trade yet: read again for this long before PRECONDITION_NOT_MET.
ABSENT_WAIT = 1.0
# After a ledger refusal: how long to wait for the other submissions of the
# bundle to get their answer, then how long to read the state again for a
# reason the refusal is explained by.
SETTLE_WAIT = 2.0
# How long a submission whose ledger answer was lost is followed (resubmitting
# the same command id and reading the state) before it is reported UNKNOWN.
TRACK_WAIT = 10.0
RESUBMIT_PAUSE = 1.0
# Canton refuses a command id it already accepted or is processing; the change
# id deduplication of the ledger (README §3).
DEDUPLICATED = ("DUPLICATE_COMMAND", "SUBMISSION_ALREADY_IN_FLIGHT")
# gRPC status codes of a ledger error reply that refuses the submission as
# asked, so the ledger did not and will not act on it: INVALID_ARGUMENT,
# NOT_FOUND, ALREADY_EXISTS, PERMISSION_DENIED, FAILED_PRECONDITION, ABORTED
# (contention), OUT_OF_RANGE, UNIMPLEMENTED, UNAUTHENTICATED. Any other reply
# (DEADLINE_EXCEEDED, INTERNAL, UNAVAILABLE, a reply without a code) leaves the
# outcome unknown: the transaction may still commit (README §3).
FIRM_REFUSALS = frozenset({3, 5, 6, 7, 9, 10, 11, 12, 16})
WILDCARD = [
    {
        "identifierFilter": {
            "WildcardFilter": {"value": {"includeCreatedEventBlob": False}}
        }
    }
]
MAX_BODY = 64 * 1024
OBSERVER = "OBSERVER"
INSTITUTIONS = ("BANK_A", "BANK_B", "BANK_C")
TOKEN_ENV = {
    "POC_BANK_A_TOKEN": "BANK_A",
    "POC_BANK_B_TOKEN": "BANK_B",
    "POC_BANK_C_TOKEN": "BANK_C",
    "POC_OBSERVER_TOKEN": OBSERVER,
}
# Platform -> participant, default port, the institution whose party submits
# the warm-up, the commands it provides with the one institution allowed to
# submit each (interface §4, business.md §2), the readiness query key
# (interface §7; readiness is provided on Y and Z only), the parties its
# participant hosts (README §2) and the templates whose presence makes a run
# prepared on the platform (README §3).
PLATFORMS = {
    "X": {
        "participant": "P_X",
        "port": 8201,
        "warmup": "BANK_A",
        "commands": {"CONFIRM_REPO": "BANK_A"},
        "readiness": None,
        "hosted": ("BANK_A", "BANK_B"),
        "prepared_by": (REPO_AGREEMENT, RIGHT),
    },
    "Y": {
        "participant": "P_Y",
        "port": 8202,
        "warmup": "BANK_B",
        "commands": {"CONFIRM_LOAN": "BANK_B"},
        "readiness": "agreement_id",
        "hosted": ("BANK_B", "BANK_C"),
        "prepared_by": (LOAN_AGREEMENT,),
    },
    "Z": {
        "participant": "P_Z",
        "port": 8203,
        "warmup": "BANK_C",
        "commands": {"ALLOCATE_MARGIN": "BANK_C"},
        "readiness": "margin_call_id",
        "hosted": ("BANK_C", "CCP"),
        "prepared_by": (MARGIN_CALL,),
    },
}


class Confirmation(NamedTuple):
    """How a command is carried out on the ledger: the choice exercised on a
    prepared contract and the record contract the choice creates."""

    agreement: str  # template of the prepared contract (pre-agreement, margin call)
    choice: str
    reference: str  # payload key naming that contract (and the readiness key)
    reference_field: str  # its field in that contract and in the record
    record_key: str  # payload key naming the new record
    record_argument: str  # the choice argument that carries it
    record_type: str  # common record_type
    record: str  # template of the record contract
    record_field: str  # the record contract's field naming it
    controller: str  # field of the prepared contract naming the submitter

    def made(self, arg: dict) -> bool:
        """Whether a record contract was created by this command: trade
        contracts of both kinds share one template."""
        return self.record != TRADE or arg["contractType"] == self.record_type


CONFIRMATIONS = {
    "CONFIRM_REPO": Confirmation(
        REPO_AGREEMENT, "ConfirmRepo", "agreement_id", "agreementId",
        "repo_id", "repoId", "REPO", TRADE, "tradeId", "seller",
    ),
    "CONFIRM_LOAN": Confirmation(
        LOAN_AGREEMENT, "ConfirmLoan", "agreement_id", "agreementId",
        "loan_id", "loanId", "LOAN", TRADE, "tradeId", "lender",
    ),
    "ALLOCATE_MARGIN": Confirmation(
        MARGIN_CALL, "AllocateMargin", "margin_call_id", "marginCallId",
        "allocation_id", "allocationId", "ALLOCATION", ALLOCATION, "allocationId",
        "provider",
    ),
}
# HTTP status of the pre-acceptance and query errors (interface §13). An
# unexpected adapter error means the result cannot be determined now: 503, the
# status tests/conformance/checks.py check_result expects for INTERNAL_ERROR.
STATUS = {
    "INVALID_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "REQUEST_ID_CONFLICT": 409,
    "INTERNAL_ERROR": 503,
    "TEMPORARILY_UNAVAILABLE": 503,
    "OUTCOME_UNKNOWN": 503,
}
# HTTP status of a result object by outcome (checks.py check_result).
OUTCOME_STATUS = {"COMMITTED": 200, "REJECTED": 200, "PENDING": 202, "UNKNOWN": 503}
COMMAND_TYPES = (
    "CONFIRM_REPO",
    "CONFIRM_LOAN",
    "ALLOCATE_MARGIN",
    "REASSIGN_FOR_LENDING",
)
ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")  # common.schema.json $defs/id
EVENT_NULLABLE = (
    "request_id",
    "attempt_id",
    "bundle_id",
    "record_id",
    "platform",
    "actor",
    "outcome",
    "error_code",
    "version_before",
    "version_after",
)

log = logging.getLogger("adapter")


def dar_main_package(dar: Path) -> str:
    """Package id of the DAR's main package, from its manifest (Main-Dalf)."""
    if not dar.is_file():
        raise SystemExit(f"{dar} missing (scripts/adapter.sh start builds it)")
    manifest = zipfile.ZipFile(dar).read("META-INF/MANIFEST.MF").decode()
    manifest = re.sub(r"\r?\n ", "", manifest)  # unfold continuation lines
    found = re.search(r"^Main-Dalf: .*-([0-9a-f]{64})\.dalf\s*$", manifest, re.M)
    if found is None:
        raise SystemExit(f"no Main-Dalf package id in {dar}")
    return found.group(1)


def valid_id(value) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


class Refusal(Exception):
    """A request refused before acceptance: answered with the common error object."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


def preparation_conflict(message: str) -> Refusal:
    """The run's prepared contracts contradict each other (README §3): no
    business answer can be given, so the result cannot be determined."""
    return Refusal("INTERNAL_ERROR", message + " (inconsistent preparation data)")


class LedgerUnreachable(Exception):
    """The participant did not answer, or answered with an error, so its state
    is unknown. Never read as "the contract does not exist" (interface §7)."""


class LedgerNotReached(LedgerUnreachable):
    """No connection to the participant: the request was not sent."""


class Trail(NamedTuple):
    """The command or runner query a participant call is made for, and why: the
    run event fields its messages carry and the purpose of the call
    (MESSAGE_PURPOSES)."""

    run_id: str
    test_id: str
    fields: dict
    purpose: str


class Message(NamedTuple):
    """One participant call logged as messages: its trail, and the stem of the
    IDs of its request (<stem>:req) and response (<stem>:res).

    sent: given for a call that must not be sent unlogged (the B2
    reassignment calls, whose api_calls evidence names their MESSAGE_SENT).
    The MESSAGE_SENT event_id is appended to it, and when that event cannot
    be written the call stops before its request is written
    (MessageNotLogged)."""

    trail: Trail
    stem: str
    sent: list | None = None


class MessageNotLogged(Exception):
    """The MESSAGE_SENT of a call given Message.sent was not written: the
    request was not sent. Not an OSError, so aiohttp lets it through from
    the trace hook instead of turning it into a connection error, after it
    closes the connection."""


class Active(NamedTuple):
    """An active contract of the run: Canton contract id and create argument.

    B2 also reads where it is: the synchronizer it is assigned to, or
    incomplete=True while it is unassigned and not yet assigned (no
    synchronizer: it cannot be used on either), with the id of that
    reassignment when the ledger reports it."""

    cid: str
    arg: dict
    synchronizer: str | None = None
    incomplete: bool = False
    reassignment: str | None = None


class Call:
    """One B2 unassign or assign call (README §3 B2): the request body as it
    is sent and kept (without headers), the event_id of its MESSAGE_SENT once
    that is written, and the call it repeats, if any."""

    def __init__(
        self,
        operation: str,
        contract_id: str,
        retry_of: str | None = None,
        stem: str | None = None,
    ):
        """A new call, or with stem one an earlier process or attempt sent
        (Adapter.earlier_calls)."""
        self.operation, self.contract_id, self.retry_of = operation, contract_id, retry_of
        self.stem = stem or f"{operation.lower()}:{uuid.uuid4().hex}"
        self.request = b""
        self.sent: list[str] = []

    @property
    def call_id(self) -> str:
        """The message_id of its MESSAGE_SENT (reassignment_check api_calls)."""
        return f"{self.stem}:req"

    def file(self, part: str) -> str:
        """Its raw request or response file, relative to the run's evidence
        directory."""
        return f"reassignment/{self.stem.replace(':', '-')}-{part}.json"


class Verdict(NamedTuple):
    """The adapter's check of a new request against the run's state (README
    §3): a business rejection code, or the prepared contract (pre-agreement
    or margin call) and the right the transaction would use."""

    code: str | None
    message: str = ""
    absent: bool = False  # the actor holds no right of the bundle (yet)
    agreement: Active | None = None
    right: Active | None = None


class Work(NamedTuple):
    """The one task deciding a request key, and the command it was started with."""

    task: asyncio.Task
    command: dict


class Submission:
    """A request whose transaction was sent to the ledger and not yet settled
    (snapshot pending_operations)."""

    def __init__(self, bundle_id: str):
        self.bundle_id = bundle_id
        self.kind = "PROCESSING"
        self.answered = asyncio.Event()  # the ledger answered a submission of it


def canonical(command: dict) -> str:
    """The business content of a command, for the request id comparison."""
    return json.dumps(
        dict(command, expected_version=int(command["expected_version"])),
        sort_keys=True,
        separators=(",", ":"),
    )


class Record(NamedTuple):
    """The record contract (trade or allocation) a request committed."""

    kind: str  # the command type that created it
    arg: dict  # its create argument


def record_matches(record: Record, command: dict) -> bool:
    """Whether the record a request committed has the content of the command
    (command.schema: every field is fixed by the record contract)."""
    confirmation = CONFIRMATIONS[record.kind]
    arg = record.arg
    return (
        command["command_type"] == record.kind
        and arg["bundleId"] == command["bundle_id"]
        and int(arg["versionBefore"]) == int(command["expected_version"])
        and command["payload"]
        == {
            confirmation.reference: arg[confirmation.reference_field],
            confirmation.record_key: arg[confirmation.record_field],
        }
    )


def error_body(
    run_id: str | None, request_id: str | None, code: str, message: str
) -> dict:
    """The common error object (common.schema.json)."""
    return {
        "run_id": run_id,
        "request_id": request_id,
        "error": {"code": code, "message": message},
    }


def ledger_code(reply) -> str | None:
    """The Canton error code of a JSON Ledger API error reply."""
    return reply.get("code") if isinstance(reply, dict) else None


def firm_refusal(reply) -> bool:
    """Whether a ledger error reply says the submission was not acted on."""
    return isinstance(reply, dict) and reply.get("grpcCodeValue") in FIRM_REFUSALS


def response_wait() -> float:
    raw = os.environ.get(RESPONSE_WAIT_ENV) or str(RESPONSE_WAIT_DEFAULT)
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= 60):
        raise SystemExit(f"{RESPONSE_WAIT_ENV} must be seconds in (0, 60], got {raw!r}")
    return value


def ledger_url(participant: str, bootstrapped: str) -> tuple[str, bool]:
    """The participant JSON API this adapter calls, and whether it is not the
    one the bootstrap recorded. LEDGER_URL_ENV<participant> points it
    elsewhere, such as the self-test's fault relay in front of it (README §4);
    the common runs leave it unset (an empty value is unset)."""
    name = LEDGER_URL_ENV + participant
    raw = os.environ.get(name) or None
    if raw is None:
        return bootstrapped, False
    if not re.fullmatch(r"http://[A-Za-z0-9.-]+:[0-9]{1,5}", raw):
        raise SystemExit(f"{name} must be http://<host>:<port>, got {raw!r}")
    return raw, raw != bootstrapped


def read_json(path: Path):
    """A kept JSON file, or None when it is missing or not JSON."""
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def command_value(body, operation: str) -> dict:
    """The command value of a kept unassign or assign request body."""
    name = "UnassignCommand" if operation == "UNASSIGN" else "AssignCommand"
    try:
        value = body["reassignmentCommands"]["commands"][0]["command"][name]["value"]
    except (KeyError, IndexError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def bundle_row(bundle: dict, version: int) -> dict:
    """Collateral.Types.Bundle as a snapshot bundle (snapshot.schema.json)."""
    return {
        "bundle_id": bundle["bundleId"],
        "security_id": bundle["securityId"],
        "quantity": {
            "value": bundle["quantity"]["value"],
            "unit": bundle["quantity"]["unit"],
        },
        "reference_value": {
            "amount": bundle["referenceValue"]["amount"],
            "currency": bundle["referenceValue"]["currency"],
        },
        "version": version,
    }


def bundle_version(right: dict) -> int:
    """The bundle version a right shows. A consumed right means the transaction
    that consumed it raised the bundle to the next version, even when the
    successor is not visible; a locked right was raised in place by the margin
    allocation (README §3)."""
    return int(right["createdVersion"]) + (
        1 if right["status"] in ("CONSUMED", "LOCKED") else 0
    )


def target_row(target: dict) -> dict:
    """Collateral.Types.ObligationTarget (a variant) as an obligation target."""
    value = target["value"]
    if target["tag"] == "AgreementRef":
        return {"agreement_id": value["agreementId"]}
    if target["tag"] == "Securities":
        return {
            "security_id": value["securityId"],
            "quantity": {
                "value": value["quantity"]["value"],
                "unit": value["quantity"]["unit"],
            },
        }
    if target["tag"] == "CollateralRef":
        return {
            "collateral_id": value["collateralId"],
            "agreement_id": value["agreementId"],
        }
    raise ValueError(f"unknown obligation target {target['tag']}")


def parse_json(raw: bytes):
    """Strict JSON: duplicate keys and NaN/Infinity are refused, not silently resolved."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite number")

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant
    )


class EventLog:
    """Implementation events of one adapter process: one producer, one clock.

    Lines go to <root>/<run_id>/events.jsonl, which the X, Y and Z processes share,
    hence O_APPEND under flock. The time and the sequence number are taken
    together (reserve), so on the single event-loop thread no other request
    can take a later time with an earlier sequence. An event written with
    them at once (B1, and every B2 event but UNASSIGNMENT_STARTED) is also
    written without an await in between; B2's UNASSIGNMENT_STARTED takes them
    before the unassign call and is written after it, so the lines of one
    process are in sequence order only once sorted by sequence. The line is
    written (not fsynced) before the HTTP response is sent.
    """

    def __init__(self, root: Path, platform: str, boot_id: str, model: str):
        self.root, self.platform, self.model = root, platform, model
        self.producer = f"canton-adapter-{platform}"
        self.clock = f"canton-adapter-{platform}-{boot_id}"
        self.sequence = 0

    def reserve(self) -> dict:
        """The sequence number and times of an event that is written later
        (write(stamp=...)): B2 takes them when the unassignment starts and
        writes UNASSIGNMENT_STARTED once the ledger has accepted it, so an
        unassignment the ledger refused leaves no start event. The sequence
        numbers in between stay unused if the event is never written."""
        stamp = {
            "sequence": self.sequence,
            "mono_ns": str(time.monotonic_ns()),
            "utc_time": datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        }
        self.sequence += 1
        return stamp

    def write(
        self,
        event_type: str,
        run_id: str,
        test_id: str,
        stamp: dict | None = None,
        **fields,
    ) -> str:
        """Write one event; returns its event_id."""
        event: dict[str, object] = {key: None for key in EVENT_NULLABLE}
        event.update(fields)
        if stamp is None:
            stamp = self.reserve()
        event_id = "event-" + uuid.uuid4().hex
        event.update(
            event_id=event_id,
            event_type=event_type,
            model=self.model,
            test_id=test_id,
            run_id=run_id,
            platform=self.platform,
            producer_id=self.producer,
            clock_id=self.clock,
            **stamp,
        )
        line = memoryview((json.dumps(event, separators=(",", ":")) + "\n").encode())
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            directory / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            while line:
                line = line[os.write(fd, line) :]
        finally:
            os.close(fd)  # also releases the lock
        return event_id


class Adapter:
    def __init__(self, platform: str, port: int):
        self.platform, self.port = platform, port
        self.spec = PLATFORMS[platform]
        self.boot_id = secrets.token_hex(6)
        state_dir = os.environ.get("CANTON_STATE_DIR")
        if not state_dir:
            raise SystemExit("CANTON_STATE_DIR not set (source scripts/env.sh)")
        self.state_dir = Path(state_dir)
        ids = json.loads((self.state_dir / "ids.json").read_text())
        # Checked before anything is uploaded: the X→Y evidence ledgers keep
        # the package version their evidence was produced with (README §4).
        topology = ids.get("topology")
        if topology not in (B1_TOPOLOGY, B2_TOPOLOGY):
            raise SystemExit(
                f"{self.state_dir} holds a {topology} ledger, not {B1_TOPOLOGY} "
                f"or {B2_TOPOLOGY}: start it from the commit that bootstrapped it "
                "(README §4)"
            )
        # B2 (README §2): the same adapters on two synchronizers. X also takes
        # the reassignment of BANK_B's right to S2 (REASSIGN_FOR_LENDING).
        self.b2 = topology == B2_TOPOLOGY
        self.model = "B2" if self.b2 else "B1"
        self.commands = dict(self.spec["commands"])
        if self.b2 and platform == "X":
            self.commands[REASSIGN] = "BANK_B"
        self.actors = set(self.commands.values())
        self.ledger_url, self.ledger_override = ledger_url(
            self.spec["participant"], ids["json_api"][self.spec["participant"]]
        )
        self.parties = ids["parties"]
        self.names = {party: name for name, party in self.parties.items()}
        # interface §13: a response counter per platform and run (this process).
        self.snapshot_seq: dict[str, int] = {}
        # Synchronizer ids by name ("S1", and "S2" on B2), and this platform's
        # own synchronizer: where it reads and submits.
        self.synchronizers: dict[str, str] = dict(ids["synchronizers"])
        self.sync_names = {sid: name for name, sid in self.synchronizers.items()}
        self.home = HOME[platform] if self.b2 else "S1"
        self.synchronizer = self.synchronizers[self.home]
        # The vetting list names participants without the PAR:: prefix of ids.json.
        self.participants = {
            name: uid.removeprefix("PAR::") for name, uid in ids["participants"].items()
        }
        self.package_id = dar_main_package(DAR)
        self.tokens = self.load_tokens()
        evidence = os.environ.get("CANTON_EVIDENCE_DIR") or str(IMPL_DIR / "logs")
        self.events = EventLog(Path(evidence), platform, self.boot_id, self.model)
        schema = json.loads((SCHEMA_DIR / "command.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        self.command_schema = Draft202012Validator(schema)
        self.ready_file = self.state_dir / f"adapter-{platform}.ready"
        self.response_wait = response_wait()
        # Request records by (run_id, actor, request_id) (interface §6.3).
        # Rejections, and on B2 X the reassignments, are kept on disk until
        # the run's results are checked; the task deciding a key, the
        # submissions waiting for the ledger and the submissions whose answer
        # was lost live in this process.
        self.requests_dir = self.state_dir / "requests" / platform
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.kept: dict[str, dict[tuple[str, str], dict]] = {}
        self.tasks: dict[tuple[str, str, str], Work] = {}
        self.inflight: dict[tuple[str, str, str], Submission] = {}
        self.unknown: dict[tuple[str, str, str], dict] = {}
        self.session: ClientSession  # created in serve(), inside the event loop

    @staticmethod
    def load_tokens() -> list[tuple[bytes, str]]:
        tokens = []
        for name, role in TOKEN_ENV.items():
            value = os.environ.get(name, "")
            if not value or value != value.strip() or any(c.isspace() for c in value):
                raise SystemExit(
                    f"{name} missing or malformed (scripts/adapter.sh tokens)"
                )
            tokens.append((value.encode(), role))
        if len({t for t, _ in tokens}) != len(tokens):
            raise SystemExit("the four POC_*_TOKEN values must differ")
        return tokens

    # --- ledger -----------------------------------------------------------

    async def ledger(
        self, method: str, path: str, message: Message | None = None, **kwargs
    ) -> tuple[int, object]:
        """One participant call and its parsed answer (ledger_raw)."""
        status, raw = await self.ledger_raw(method, path, message, **kwargs)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = raw[:500].decode(errors="replace")
        return status, body

    async def ledger_raw(
        self, method: str, path: str, message: Message | None = None, **kwargs
    ) -> tuple[int, bytes]:
        """One participant call. With a message, its request is logged when the
        connection is held and the request is handed to it for writing
        (request_sent: the attempt to send), its response when it has been read
        whole. A call that got no connection logs nothing; one whose answer was
        lost, or whose writing failed after that point (the connection closed in
        between), logs only the request, and is treated as possibly sent
        (LedgerUnreachable)."""
        try:
            async with self.session.request(
                method, self.ledger_url + path, trace_request_ctx=message, **kwargs
            ) as reply:
                raw = await reply.read()
            if message is not None:
                self.log_message("MESSAGE_RECEIVED", message)
        except ClientConnectorError as error:
            raise LedgerNotReached(f"{method} {path}: {error!r}") from error
        except (ClientError, asyncio.TimeoutError) as error:
            # Sent, or possibly sent: the participant may have acted on it.
            raise LedgerUnreachable(f"{method} {path}: {error!r}") from error
        return reply.status, raw

    async def request_sent(self, session, context, params) -> None:
        """aiohttp trace hook (on_request_headers_sent): the participant call's
        connection is held and its request is about to be written. aiohttp calls
        it before the headers reach the transport, and no later hook marks the
        write, so this is the attempt to send."""
        if context.trace_request_ctx is not None:
            self.log_message("MESSAGE_SENT", context.trace_request_ctx)

    def log_message(self, event_type: str, message: Message) -> None:
        """MESSAGE_SENT of a participant call's request or MESSAGE_RECEIVED of
        its response, under the run event fields of its command or query. A
        call made for no attributable command, or for a GET without X-Test-ID,
        logs nothing. A log failure does not fail the call: the participant may
        already have acted on it. The exception is the MESSAGE_SENT of a call
        given Message.sent, written before its request: when it fails the
        request is not sent (MessageNotLogged)."""
        trail = message.trail
        if not trail.test_id:
            return
        request = event_type == "MESSAGE_SENT"
        strict = request and message.sent is not None
        adapter, participant = self.events.producer, self.spec["participant"]
        try:
            event_id = self.events.write(
                event_type,
                trail.run_id,
                trail.test_id,
                message_id=f"{message.stem}:{'req' if request else 'res'}",
                message_kind="REQUEST" if request else "RESPONSE",
                message_category=MESSAGE_PURPOSES[trail.purpose],
                source_id=adapter if request else participant,
                destination_id=participant if request else adapter,
                **trail.fields,
            )
        except Exception as error:
            log.exception("%s of %s not logged", event_type, message.stem)
            if strict:
                raise MessageNotLogged(f"{event_type} of {message.stem}") from error
            return
        if strict:
            message.sent.append(event_id)

    def party_filter(self) -> dict:
        """Every contract a party hosted on this platform's participant sees."""
        return {
            "filtersByParty": {
                self.parties[name]: {"cumulative": WILDCARD}
                for name in self.spec["hosted"]
            },
            "verbose": False,
        }

    async def active_contracts(
        self, run_id: str, trail: Trail | None = None
    ) -> dict[str, list[Active]]:
        """The run's active business contracts on this participant, by template.

        One snapshot of the active contract set at a single offset: the first
        page fixes the offset (ledger end), and its page token carries it to the
        following pages. A token is only valid with the query that produced it,
        so the later requests repeat that query and add the token. With a
        trail, each page is one logged call: <purpose>:<read>:p<page>.
        """
        found: dict[str, list[Active]] = {
            entity: []
            for entity in (
                RIGHT,
                REPO_AGREEMENT,
                LOAN_AGREEMENT,
                TRADE,
                OBLIGATION,
                MARGIN_CALL,
                ALLOCATION,
            )
        }
        seen: set[str] = set()
        query: dict[str, object] = {
            "eventFormat": self.party_filter(),
            "maxPageSize": ACS_PAGE_SIZE,
        }
        # B2: a contract in the middle of a reassignment is left out of the
        # active contracts unless incomplete reassignments are asked for.
        # Asking for them keeps it in sight, marked incomplete.
        incomplete: dict[str, tuple[str, dict, str | None]] = {}
        if self.b2:
            query["includeIncompleteReassignments"] = True
        read = f"{trail.purpose}:{uuid.uuid4().hex}" if trail is not None else None
        pages = 0
        while True:
            pages += 1
            message = Message(trail, f"{read}:p{pages}") if trail is not None else None
            status, page = await self.ledger(
                "POST", "/v2/state/active-contracts-page", message, json=query
            )
            if status != 200 or not isinstance(page, dict):
                raise LedgerUnreachable(f"active contracts: {status} {page}")
            for entry in page.get("activeContracts", []):
                contract_entry = entry.get("contractEntry", {})
                active = contract_entry.get("JsActiveContract")
                if active is not None:
                    event = active["createdEvent"]
                    synchronizer = self.sync_names.get(active.get("synchronizerId"))
                    if self.b2 and synchronizer is None:
                        raise LedgerUnreachable(
                            f"active contract on an unknown synchronizer "
                            f"{active.get('synchronizerId')}"
                        )
                elif self.b2:
                    event, reassignment = self.incomplete_event(contract_entry)
                    synchronizer = None
                else:  # B1, one synchronizer: no reassignment entries
                    raise LedgerUnreachable(
                        f"unexpected contract entry {list(entry)}"
                    )
                if event.get("packageName") != PACKAGE:
                    continue
                entity = event["templateId"].split(":", 1)[1]
                argument = event["createArgument"]
                if entity not in found or argument.get("runId") != run_id:
                    continue
                cid = event["contractId"]
                if synchronizer is None and self.b2:
                    incomplete.setdefault(cid, (entity, argument, reassignment))
                elif cid not in seen:
                    seen.add(cid)
                    found[entity].append(
                        Active(cid, argument, synchronizer if self.b2 else None)
                    )
            token = page.get("nextPageToken")
            if not token:
                break
            query["pageToken"] = token
        # One contract is either assigned or in a reassignment; a contract the
        # read saw both ways counts as assigned. An active contract has an
        # entry for each synchronizer it is active on, and an incomplete
        # assigned entry does not say it is active on the target
        # (state_service.proto), so an assigned contract is never left marked
        # incomplete.
        for cid, (entity, argument, reassignment) in incomplete.items():
            if cid not in seen:
                found[entity].append(
                    Active(cid, argument, None, incomplete=True, reassignment=reassignment)
                )
        return found

    @staticmethod
    def incomplete_event(contract_entry: dict) -> tuple[dict, str | None]:
        """The created event of a contract in a reassignment (B2) and the
        reassignment id: JsIncompleteUnassigned carries the event next to the
        unassignment, JsIncompleteAssigned inside the assignment."""
        for kind, value in contract_entry.items():
            value = value.get("value", value) if isinstance(value, dict) else value
            if not isinstance(value, dict):
                break
            if kind == "JsIncompleteUnassigned" and "createdEvent" in value:
                unassigned = value.get("unassignedEvent") or {}
                unassigned = unassigned.get("value", unassigned)
                return value["createdEvent"], unassigned.get("reassignmentId")
            if kind == "JsIncompleteAssigned":
                assigned = value.get("assignedEvent", {})
                assigned = assigned.get("value", assigned)
                if "createdEvent" in assigned:
                    return assigned["createdEvent"], assigned.get("reassignmentId")
        raise LedgerUnreachable(f"unexpected contract entry {list(contract_entry)}")

    async def prepared_run(
        self, run_id: str, trail: Trail | None = None
    ) -> dict[str, list[Active]]:
        """The run's active contracts, or the refusal when they cannot be read or
        the run was not prepared on this platform (README §3)."""
        try:
            found = await self.active_contracts(run_id, trail)
        except LedgerUnreachable as error:
            log.warning(
                "participant %s not readable: %s", self.spec["participant"], error
            )
            raise Refusal(
                "TEMPORARILY_UNAVAILABLE",
                f"participant {self.spec['participant']} cannot be read now",
            ) from error
        if not any(found[entity] for entity in self.spec["prepared_by"]):
            raise Refusal(
                "NOT_FOUND", f"run {run_id} is not prepared on platform {self.platform}"
            )
        return found

    def name_of(self, party: str) -> str:
        name = self.names.get(party)
        if name is None:
            raise ValueError("a contract names a party outside ids.json")
        return name

    def away(self, contract: Active) -> bool:
        """B2: whether a contract cannot be used on this platform's
        synchronizer: it is assigned to the other one or in a reassignment.
        A transaction given such an input would be reassigned by the ledger
        on its own, which the adapter never lets happen (README §3 B2)."""
        return self.b2 and (contract.incomplete or contract.synchronizer != self.home)

    def away_right(self) -> Verdict:
        # absent: the right may be on its way here (a reassignment the
        # participant has not applied yet), so decide reads again for a while.
        return Verdict(
            "RIGHT_UNAVAILABLE",
            f"the right is not usable on {self.home} (another synchronizer or "
            "a reassignment)",
            absent=True,
        )

    async def prepare_ledger(self) -> list[float]:
        """Upload and vet the DAR, wait until every participant has vetted it on
        every synchronizer, then run the warm-up transactions; returns their ms.

        B2: a participant on two synchronizers cannot tell where to vet an
        upload without a synchronizer id (400
        PACKAGE_SERVICE_CANNOT_AUTODETECT_SYNCHRONIZER), so the DAR is uploaded
        once for S1 and once for S2."""
        targets: list[str | None] = (
            [self.synchronizers[name] for name in ("S1", "S2")] if self.b2 else [None]
        )
        dar = DAR.read_bytes()
        for synchronizer in targets:
            params = {"vetAllPackages": "true"}
            if synchronizer is not None:
                params["synchronizerId"] = synchronizer
            status, body = await self.ledger(
                "POST",
                "/v2/dars",
                params=params,
                data=dar,
                headers={"Content-Type": "application/octet-stream"},
            )
            if status != 200:
                where = "" if synchronizer is None else f" for {self.sync_names[synchronizer]}"
                raise SystemExit(
                    f"DAR upload to {self.spec['participant']}{where} failed: "
                    f"{status} {body}"
                )
        await self.wait_vetted()
        party = self.parties[self.spec["warmup"]]
        timings = []
        for i in range(WARMUP_COUNT):
            command = {
                "CreateAndExerciseCommand": {
                    "templateId": WARMUP_TEMPLATE,
                    "createArguments": {"party": party, "bootId": self.boot_id},
                    "choice": "Archive",
                    "choiceArgument": {},
                }
            }
            started = time.monotonic()
            commands: dict[str, object] = {
                "commands": [command],
                "commandId": f"warmup-{self.platform}-{self.boot_id}-{i}",
                "userId": f"adapter-{self.platform}",
                "actAs": [party],
            }
            if self.b2:  # on the platform's own synchronizer
                commands["synchronizerId"] = self.synchronizer
            status, body = await self.ledger(
                "POST",
                "/v2/commands/submit-and-wait-for-transaction",
                json={"commands": commands},
            )
            if status != 200:
                raise SystemExit(f"warm-up transaction {i} refused: {status} {body}")
            timings.append(round((time.monotonic() - started) * 1000, 1))
        return timings

    async def wait_vetted(self) -> None:
        """A transaction is refused unless every participant hosting one of its
        informees has vetted the package. BANK_B is on P_X and P_Y, BANK_C on
        P_Y and P_Z, and each adapter uploads only to its own participant, so
        the adapters wait here for each other's upload instead of racing.
        B2 waits for every participant on S1 and on S2."""
        query = {
            "packageMetadataFilter": {"packageIds": [self.package_id]},
            "topologyStateFilter": {
                "synchronizerIds": list(self.synchronizers.values())
            },
        }
        deadline = time.monotonic() + VETTING_TIMEOUT
        while True:
            status, body = await self.ledger(
                "POST", "/v2/package-vetting/list", json=query
            )
            if status != 200 or not isinstance(body, dict):
                raise SystemExit(f"package vetting query failed: {status} {body}")
            if body.get("nextPageToken"):
                raise SystemExit("package vetting list unexpectedly paged")
            vetted = {
                (entry["participantId"], entry.get("synchronizerId"))
                for entry in body.get("vettedPackages", [])
                if any(
                    pkg.get("packageId") == self.package_id
                    for pkg in entry.get("packages", [])
                )
            }
            if not self.b2:  # one synchronizer, asked for by id
                vetted = {(uid, self.synchronizer) for uid, _ in vetted}
            missing = sorted(
                f"{name}@{sync}" if self.b2 else name
                for name, uid in self.participants.items()
                for sync, sid in self.synchronizers.items()
                if (uid, sid) not in vetted
            )
            if not missing:
                return
            if time.monotonic() > deadline:
                where = "S1 and S2" if self.b2 else "S1"
                raise SystemExit(
                    f"package {self.package_id[:12]} not vetted on {where} by "
                    f"{missing} after {VETTING_TIMEOUT}s (are the other adapters "
                    "starting?)"
                )
            await asyncio.sleep(0.5)

    # --- request checks ---------------------------------------------------

    def authenticate(self, request: web.Request) -> str:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Refusal("UNAUTHENTICATED", "a Bearer credential is required")
        presented, role = token.encode(), None
        for (
            secret,
            name,
        ) in self.tokens:  # compare with every credential, not up to the match
            if hmac.compare_digest(presented, secret):
                role = name
        if role is None:
            raise Refusal("UNAUTHENTICATED", "the credential is not recognised")
        return role

    @staticmethod
    def identify(request: web.Request, request_id=None) -> str | None:
        """Record the ids the error body echoes; ids that cannot be read stay null.

        Runs before authentication: interface §13 answers every pre-acceptance
        error with the caller's own run_id/request_id, the 401 included.
        """
        run_id = request.match_info["run_id"]
        # All-dot ids match the common id pattern but would name a parent directory.
        if not valid_id(run_id) or set(run_id) == {"."}:
            run_id = None
        else:
            request["run_id"] = run_id
        if valid_id(request_id):
            request["request_id"] = request_id
        return run_id

    @staticmethod
    def require_run_id(run_id: str | None) -> str:
        if run_id is None:
            raise Refusal("INVALID_REQUEST", "run_id is not a valid identifier")
        return run_id

    @staticmethod
    def correlation(request: web.Request) -> tuple[str, str]:
        """Diagnostic X-Test-ID / X-Attempt-ID; they never grant anything."""
        test_id = request.headers.get("X-Test-ID")
        attempt = request.headers.get("X-Attempt-ID")
        return (
            test_id if valid_id(test_id) else "NO-TEST-ID",
            attempt if valid_id(attempt) else "attempt-adapter-" + uuid.uuid4().hex,
        )

    def query_trail(
        self, request: web.Request, run_id: str, purpose: str, **fields
    ) -> Trail:
        """The trail of the state reads a runner GET makes (README §3): the
        GET's X-Test-ID and X-Attempt-ID and the fields its query names. A GET
        without X-Test-ID logs nothing, and a run's events may carry only its
        case's test ID or PREPARE (tests/conformance/runner.py collect_history):
        the runner's initial-state snapshot sends PREPARE and an attempt ID from
        0b1ee43 on, and no X-Test-ID before."""
        if "X-Test-ID" not in request.headers:
            return Trail(run_id, "", {}, purpose)
        test_id, attempt = self.correlation(request)
        return Trail(run_id, test_id, {"attempt_id": attempt, **fields}, purpose)

    def check_command(self, role: str, command: dict) -> None:
        error = next(self.command_schema.iter_errors(command), None)
        if error is not None:
            # Name the field only: messages could echo supplied values.
            where = "/" + "/".join(map(str, error.absolute_path))
            raise Refusal(
                "INVALID_REQUEST",
                f"command does not match command.schema.json at {where}",
            )
        # The schema's integer also admits 1.0; the choice argument and the
        # request id comparison take the whole number.
        command["expected_version"] = int(command["expected_version"])
        kind = command["command_type"]
        if kind not in self.commands:
            raise Refusal(
                "INVALID_REQUEST", f"{kind} is not provided on platform {self.platform}"
            )
        if self.commands[kind] != role:
            raise Refusal(
                "FORBIDDEN",
                f"the credential may not submit {kind} on platform {self.platform}",
            )

    # --- business records (README §3) ------------------------------------

    def right_row(self, arg: dict) -> dict:
        return {
            "right_id": arg["rightId"],
            "bundle_id": arg["bundle"]["bundleId"],
            "holder": self.name_of(arg["holder"]),
            "created_version": int(arg["createdVersion"]),
            "status": arg["status"],
            "allowed_commands": list(arg["allowedCommands"]),
            "predecessor_right_id": arg.get("predecessorRightId"),
            "source_record_id": arg.get("sourceRecordId"),
        }

    def right_event(self, event: dict) -> dict:
        """The RIGHT_* event fields of a right the ledger created."""
        row = self.right_row(event["createArgument"])
        return {
            "right_id": row["right_id"],
            "right_status": row["status"],
            "holder": row["holder"],
            "predecessor_right_id": row["predecessor_right_id"],
            "source_record_id": row["source_record_id"],
            "contract_id": event["contractId"],
        }

    def result_of(self, run_id: str, record: Record, replayed: bool) -> dict:
        """The COMMITTED result (interface §6.1) of the request that created the
        record (trade or allocation), from the record contract itself."""
        confirmation = CONFIRMATIONS[record.kind]
        arg = record.arg
        return {
            "run_id": run_id,
            "request_id": arg["requestId"],
            "actor": self.name_of(arg["actor"]),
            "command_type": record.kind,
            "outcome": "COMMITTED",
            "replayed": replayed,
            "effect": {
                "bundle_id": arg["bundleId"],
                "version_before": int(arg["versionBefore"]),
                "version_after": int(arg["versionAfter"]),
                "record_type": confirmation.record_type,
                "record_id": arg[confirmation.record_field],
            },
            "error": None,
        }

    @staticmethod
    def outcome_of(
        run_id: str,
        request_id: str,
        actor: str,
        command_type: str,
        outcome: str,
        replayed: bool,
        error: tuple[str, str] | None = None,
    ) -> dict:
        """A result without business effect: REJECTED, PENDING or UNKNOWN."""
        return {
            "run_id": run_id,
            "request_id": request_id,
            "actor": actor,
            "command_type": command_type,
            "outcome": outcome,
            "replayed": replayed,
            "effect": None,
            "error": None if error is None else {"code": error[0], "message": error[1]},
        }

    def rejected_of(self, record: dict, replayed: bool) -> dict:
        return self.outcome_of(
            record["run_id"],
            record["request_id"],
            record["actor"],
            record["command_type"],
            "REJECTED",
            replayed,
            (record["code"], record["message"]),
        )

    def record_of(
        self, found: dict[str, list[Active]], actor: str, request_id: str
    ) -> Record | None:
        """The record contract (trade or allocation) a request of this platform
        created, if any.

        Requests are kept per (run, authenticated actor, request id) on the
        platform they were submitted to (interface §6.3)."""
        records = [
            Record(kind, c.arg)
            for kind in self.commands
            if kind in CONFIRMATIONS  # B2 REASSIGN_FOR_LENDING keeps no record
            for c in found[CONFIRMATIONS[kind].record]
            if CONFIRMATIONS[kind].made(c.arg)
            and c.arg["requestId"] == request_id
            and c.arg["actor"] == self.parties[actor]
        ]
        if len(records) > 1:
            raise preparation_conflict(
                f"request {request_id} created more than one record"
            )
        return records[0] if records else None

    # --- request records (interface §6.3) ---------------------------------

    def kept_of(self, run_id: str) -> dict[tuple[str, str], dict]:
        """The run's kept requests on this platform, read from disk once: the
        rejections, and on B2 X the reassignments. The last line of a key
        wins: a reassignment is kept as STARTED before its ledger calls and
        again as COMMITTED or REJECTED once it is decided, or WITHDRAWN when
        the ledger did not act on it (then no record is kept)."""
        table = self.kept.get(run_id)
        if table is None:
            table = {}
            path = self.requests_dir / f"{run_id}.jsonl"
            if path.exists():
                data = path.read_bytes()
                end = data.rfind(b"\n") + 1
                if end < len(data):
                    # A record cut short (the adapter stopped while writing it)
                    # was never answered, since keep returns only after the
                    # whole line is synced: it is dropped and its request is
                    # decided again when it is resent.
                    log.warning("%s: dropping a request record cut short", path)
                    os.truncate(path, end)
                    data = data[:end]
                for line in data.decode().splitlines():
                    record = json.loads(line)
                    if record.get("run_id") != run_id:
                        continue
                    if record.get("outcome") == "WITHDRAWN":
                        table.pop((record["actor"], record["request_id"]), None)
                    else:
                        table[(record["actor"], record["request_id"])] = record
            self.kept[run_id] = table
        return table

    def keep(self, key: tuple[str, str, str], record: dict) -> None:
        """Append a request record and sync it before the request goes on."""
        run_id, actor, request_id = key
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # Loaded first: a line an earlier process cut short is removed before
        # this one is appended after it.
        table = self.kept_of(run_id)
        with open(self.requests_dir / f"{run_id}.jsonl", "a") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
        if record.get("outcome") == "WITHDRAWN":
            table.pop((actor, request_id), None)
        else:
            table[(actor, request_id)] = record

    def withdraw(self, key: tuple[str, str, str], record: dict) -> None:
        """Drop a STARTED reassignment the ledger did not act on: it is refused
        as never accepted, and a resend is decided afresh."""
        self.keep(key, {**record, "outcome": "WITHDRAWN"})

    def kept_answer(self, record: dict, replayed: bool) -> dict:
        """The result of a decided kept request: its rejection, or the
        COMMITTED reassignment with its effect."""
        if record.get("outcome") != "COMMITTED":
            return self.rejected_of(record, replayed)
        return {
            "run_id": record["run_id"],
            "request_id": record["request_id"],
            "actor": record["actor"],
            "command_type": record["command_type"],
            "outcome": "COMMITTED",
            "replayed": replayed,
            "effect": record["effect"],
            "error": None,
        }

    def reject(
        self, key: tuple[str, str, str], command: dict, code: str, message: str
    ) -> tuple[int, dict]:
        """Keep a business rejection (written before it is answered) and answer it."""
        run_id, actor, request_id = key
        record = {
            "run_id": run_id,
            "actor": actor,
            "request_id": request_id,
            "command_type": command["command_type"],
            "content": canonical(command),
            "code": code,
            "message": message,
        }
        self.keep(key, record)
        self.unknown.pop(key, None)
        log.info(
            "%s %s %s rejected: %s %s",
            run_id,
            command["command_type"],
            request_id,
            code,
            message,
        )
        return 200, self.rejected_of(record, replayed=False)

    @staticmethod
    def conflict(request_id: str) -> Refusal:
        return Refusal(
            "REQUEST_ID_CONFLICT",
            f"request {request_id} was already submitted with other content",
        )

    def command_id(self, key: tuple[str, str, str]) -> str:
        """One Canton command id per request key: the ledger deduplicates a
        resubmission of the same request (README §3)."""
        digest = hashlib.sha256("|".join(key).encode()).hexdigest()
        return f"confirm-{self.platform}-{digest[:32]}"

    def snapshot_of(self, run_id: str, found: dict[str, list[Active]]) -> dict:
        """Normalised records this participant sees (interface §8, snapshot
        schema). The pre-agreements are preparation data, not records; the
        margin calls are listed, as common.schema has a row for them."""
        rights = sorted(
            (self.right_row(c.arg) for c in found[RIGHT]),
            key=lambda row: (row["bundle_id"], row["created_version"]),
        )
        bundles: dict[str, dict] = {}
        for contract in found[RIGHT]:
            arg = contract.arg
            version = bundle_version(arg)
            known = bundles.get(arg["bundle"]["bundleId"])
            if known is None or version > known["version"]:
                bundles[arg["bundle"]["bundleId"]] = bundle_row(arg["bundle"], version)
        contracts = [
            {
                "contract_id": a["tradeId"],
                "type": a["contractType"],
                "agreement_id": a["agreementId"],
                "bundle_id": a["bundleId"],
                "parties": [self.name_of(p) for p in a["parties"]],
                "status": "ACTIVE",
            }
            for a in (c.arg for c in found[TRADE])
        ]
        obligations = [
            {
                "obligation_id": a["obligationId"],
                "type": a["obligationType"],
                "debtor": self.name_of(a["debtor"]),
                "creditor": self.name_of(a["creditor"]),
                "contract_id": a["tradeId"],
                "agreement_id": a["agreementId"],
                "target": target_row(a["target"]),
                "status": a["status"],
            }
            for a in (c.arg for c in found[OBLIGATION])
        ]
        margin_calls = [
            {
                "margin_call_id": a["marginCallId"],
                "provider": self.name_of(a["provider"]),
                "ccp": self.name_of(a["ccp"]),
                "bundle_id": a["bundleId"],
                "whole_bundle_accepted": a["wholeBundleAccepted"],
                "eligibility_passed": a["eligibilityPassed"],
                "status": a["status"],
            }
            for a in (c.arg for c in found[MARGIN_CALL])
        ]
        allocations = [
            {
                "allocation_id": a["allocationId"],
                "margin_call_id": a["marginCallId"],
                "bundle_id": a["bundleId"],
                "provider": self.name_of(a["provider"]),
                "ccp": self.name_of(a["ccp"]),
                # An active allocation keeps the right locked (Collateral.Margin).
                "status": "LOCKED",
            }
            for a in (c.arg for c in found[ALLOCATION])
        ]
        seq = self.snapshot_seq.get(run_id, 0)
        self.snapshot_seq[run_id] = seq + 1
        return {
            "run_id": run_id,
            "platform": self.platform,
            "snapshot_seq": seq,
            "bundles": [bundles[b] for b in sorted(bundles)],
            "rights": rights,
            "contracts": sorted(contracts, key=lambda row: row["contract_id"]),
            "obligations": sorted(obligations, key=lambda row: row["obligation_id"]),
            "margin_calls": sorted(margin_calls, key=lambda row: row["margin_call_id"]),
            "allocations": sorted(allocations, key=lambda row: row["allocation_id"]),
            "pending_operations": self.pending_of_run(run_id, found),
        }

    def pending_of_run(self, run_id: str, found: dict[str, list[Active]]) -> list:
        """interface §8.1: requests sent to the ledger and not settled yet
        (Canton locks the right a transaction uses while it is confirmed, and
        a reassignment takes it off both synchronizers), and requests answered
        UNKNOWN that the ledger may still commit: the state shows no trade of
        theirs and they still pass the state check."""
        rows = [
            {
                "request_id": key[2],
                "bundle_id": submission.bundle_id,
                "kind": submission.kind,
                "blocks_use": True,
            }
            for key, submission in self.inflight.items()
            if key[0] == run_id
        ]
        rows += [
            {
                "request_id": key[2],
                "bundle_id": command["bundle_id"],
                "kind": "RECOVERING",
                "blocks_use": True,
            }
            for key, command in self.unknown.items()
            if key[0] == run_id
            and key not in self.inflight
            and self.record_of(found, key[1], key[2]) is None
            and self.may_commit(found, key, command)
        ]
        # B2 X: a reassignment answered UNKNOWN with its contract unassigned
        # and not assigned yet; no ledger call of it is going on until it is
        # resent (README §3 B2).
        incomplete = {c.cid for c in found[RIGHT] if c.incomplete}
        rows += [
            {
                "request_id": request_id,
                "bundle_id": json.loads(record["content"])["bundle_id"],
                "kind": "REASSIGNING",
                "blocks_use": True,
            }
            for (actor, request_id), record in self.kept_of(run_id).items()
            if record.get("outcome") == "STARTED"
            and (run_id, actor, request_id) not in self.inflight
            and record.get("contract_id") in incomplete
        ]
        return sorted(rows, key=lambda row: row["request_id"])

    def may_commit(
        self, found: dict[str, list[Active]], key: tuple[str, str, str], command: dict
    ) -> bool:
        """Whether a request answered UNKNOWN can still commit: once another
        trade has used or replaced the right it was sent with, the state check
        fails and the ledger would refuse it."""
        try:
            return self.judge(found, command, key[1]).code is None
        except Refusal:
            return True  # contradictory preparation data: keep it listed

    # --- confirmations (README §3 "결과 상태와 멱등성") ------------------------

    def judge(
        self, found: dict[str, list[Active]], command: dict, role: str
    ) -> Verdict:
        """Check a new request against the run's state in the order of the
        confirmation choice (Collateral.Agreement). The choice checks the same
        conditions again inside the transaction. ConfirmLoan's cosigner check
        is left to the ledger: a right the borrower cosigned only comes from a
        joint Ledger API submission, and its refusal is answered 503 (README §3)."""
        if command["command_type"] == "ALLOCATE_MARGIN":
            return self.judge_margin(found, command, role)
        confirmation = CONFIRMATIONS[command["command_type"]]
        payload, bundle_id = command["payload"], command["bundle_id"]
        agreements = [
            c
            for c in found[confirmation.agreement]
            if c.arg["agreementId"] == payload["agreement_id"]
            and c.arg["bundleId"] == bundle_id
        ]
        if not agreements:
            return Verdict("PRECONDITION_NOT_MET", "no pre-agreement for the bundle")
        if len(agreements) > 1:
            raise preparation_conflict("more than one pre-agreement for the bundle")
        agreement = agreements[0]
        if self.away(agreement):
            raise preparation_conflict(f"the pre-agreement is not on {self.home}")
        user = self.parties[role]
        if agreement.arg[confirmation.controller] != user:
            return Verdict(
                "PRECONDITION_NOT_MET", "the actor is not the confirming party"
            )
        collateral = agreement.arg.get("collateral")
        if collateral is not None and not (
            collateral["provided"] and collateral["conditionsMet"]
        ):
            return Verdict(
                "PRECONDITION_NOT_MET",
                "the separate loan collateral is not provided or not eligible",
            )
        held = [
            c
            for c in found[RIGHT]
            if c.arg["bundle"]["bundleId"] == bundle_id and c.arg["holder"] == user
        ]
        if not held:
            # The preceding trade has not given the actor a right (yet).
            return Verdict(
                "PRECONDITION_NOT_MET",
                "the actor holds no right of the bundle",
                absent=True,
            )
        available = [c for c in held if c.arg["status"] == "AVAILABLE"]
        if not available:
            return Verdict(
                "RIGHT_UNAVAILABLE", "the actor holds no available right of the bundle"
            )
        if len(available) > 1:
            raise preparation_conflict("the actor holds more than one available right")
        right = available[0]
        if self.away(right):
            return self.away_right()
        if int(right.arg["createdVersion"]) != int(command["expected_version"]):
            return Verdict(
                "VERSION_CONFLICT", "the bundle version is not the expected one"
            )
        if command["command_type"] not in right.arg["allowedCommands"]:
            return Verdict(
                "USE_NOT_PERMITTED", "the right does not permit this command"
            )
        return Verdict(None, agreement=agreement, right=right)

    def judge_margin(
        self, found: dict[str, list[Active]], command: dict, role: str
    ) -> Verdict:
        """Check an ALLOCATE_MARGIN request in the rejection order of README §3
        (the order Model A answers in). AllocateMargin checks the margin call,
        the right and the version again inside the transaction; the CCP
        cosigner check is left to the ledger, like ConfirmLoan's."""
        payload, bundle_id = command["payload"], command["bundle_id"]
        rights = [c for c in found[RIGHT] if c.arg["bundle"]["bundleId"] == bundle_id]
        user = self.parties[role]
        if not rights:
            # The loan has not reached this participant (yet).
            return Verdict(
                "PRECONDITION_NOT_MET",
                "the bundle is not known on this platform",
                absent=True,
            )
        if any(c.arg["status"] == "LOCKED" for c in rights):
            return Verdict(
                "RIGHT_UNAVAILABLE", "the bundle is already allocated to a margin call"
            )
        calls = [
            c
            for c in found[MARGIN_CALL]
            if c.arg["marginCallId"] == payload["margin_call_id"]
            and c.arg["bundleId"] == bundle_id
        ]
        if not calls:
            return Verdict("PRECONDITION_NOT_MET", "no margin call for the bundle")
        if len(calls) > 1:
            raise preparation_conflict("more than one margin call for the bundle")
        call = calls[0]
        if self.away(call):
            raise preparation_conflict(f"the margin call is not on {self.home}")
        if call.arg["status"] != "OPEN" or call.arg["provider"] != user:
            return Verdict(
                "PRECONDITION_NOT_MET", "the margin call is not open for the actor"
            )
        if not (call.arg["wholeBundleAccepted"] and call.arg["eligibilityPassed"]):
            return Verdict(
                "COLLATERAL_NOT_ELIGIBLE", "the bundle is not eligible as margin"
            )
        if any(
            c.arg["allocationId"] == payload["allocation_id"]
            for c in found[ALLOCATION]
        ):
            return Verdict(
                "PRECONDITION_NOT_MET", "the allocation id is already used"
            )
        held = [c for c in rights if c.arg["holder"] == user]
        if not held:
            # The loan has not given the actor a right on this participant (yet).
            return Verdict(
                "PRECONDITION_NOT_MET",
                "the actor holds no right of the bundle",
                absent=True,
            )
        available = [c for c in held if c.arg["status"] == "AVAILABLE"]
        if not available:
            return Verdict(
                "RIGHT_UNAVAILABLE", "the actor holds no available right of the bundle"
            )
        if len(available) > 1:
            raise preparation_conflict("the actor holds more than one available right")
        right = available[0]
        if self.away(right):
            return self.away_right()
        if int(right.arg["createdVersion"]) != int(command["expected_version"]):
            return Verdict(
                "VERSION_CONFLICT", "the bundle version is not the expected one"
            )
        loans = [
            c
            for c in found[TRADE]
            if c.arg["contractType"] == "LOAN"
            and c.arg["tradeId"] == right.arg.get("sourceRecordId")
            and int(c.arg["versionAfter"]) == int(right.arg["createdVersion"])
        ]
        if not loans:
            return Verdict(
                "PRECONDITION_NOT_MET", "the right does not come from the bundle's loan"
            )
        if command["command_type"] not in right.arg["allowedCommands"]:
            return Verdict(
                "USE_NOT_PERMITTED", "the right does not permit this command"
            )
        return Verdict(None, agreement=call, right=right)

    # --- B2 reassignment (README §3 B2) -------------------------------------

    def judge_reassign(
        self, found: dict[str, list[Active]], command: dict, role: str
    ) -> Verdict:
        """Check a new REASSIGN_FOR_LENDING against the run's state: the actor
        holds one available right of the bundle, at the expected version,
        assigned to the payload's source (command.schema fixes S1 → S2). The
        right moves as it is: what it permits is checked by the command that
        uses it on the target."""
        bundle_id, holder = command["bundle_id"], self.parties[role]
        rights = [
            c
            for c in found[RIGHT]
            if c.arg["bundle"]["bundleId"] == bundle_id and c.arg["holder"] == holder
        ]
        if not rights:
            # absent: the confirmation that gives the actor the right may not
            # be applied on this participant yet.
            return Verdict(
                "PRECONDITION_NOT_MET",
                f"{role} holds no right of the bundle",
                absent=True,
            )
        available = [c for c in rights if c.arg["status"] == "AVAILABLE"]
        if not available:
            return Verdict(
                "RIGHT_UNAVAILABLE", f"{role}'s right of the bundle is not available"
            )
        if len(available) > 1:
            raise preparation_conflict(
                f"{role} holds more than one available right of the bundle"
            )
        right = available[0]
        version = int(right.arg["createdVersion"])
        if version != int(command["expected_version"]):
            return Verdict(
                "VERSION_CONFLICT",
                f"the bundle is at version {version}, not {command['expected_version']}",
            )
        if right.incomplete:
            return Verdict("RIGHT_UNAVAILABLE", "the right is in a reassignment")
        source = command["payload"]["source_synchronizer"]
        if right.synchronizer != source:
            return Verdict("PRECONDITION_NOT_MET", f"the right is not on {source}")
        return Verdict(None, right=right)

    async def reassign(
        self,
        key: tuple[str, str, str],
        command: dict,
        found: dict[str, list[Active]],
        cid: str,
        test_id: str,
        fields: dict,
        resumed: bool,
    ) -> tuple[int, dict]:
        """REASSIGN_FOR_LENDING: one reassignment of the bundle at a time.
        Nothing is awaited between judge_reassign and the registration, so a
        second request of the bundle sees this one and is rejected."""
        run_id = key[0]
        bundle_id = command["bundle_id"]
        if any(
            k != key
            and k[0] == run_id
            and s.bundle_id == bundle_id
            and s.kind == "REASSIGNING"
            for k, s in self.inflight.items()
        ):
            if resumed:
                # It may have moved the contract already: not decided until
                # it is resent.
                return 503, self.unknown_of(key, command, replayed=True)
            return self.reject(
                key,
                command,
                "RIGHT_UNAVAILABLE",
                "another reassignment of the bundle is in progress",
            )
        submission = Submission(bundle_id)
        submission.kind = "REASSIGNING"
        self.inflight[key] = submission
        try:
            return await self.move(key, command, found, cid, test_id, fields, resumed)
        finally:
            submission.answered.set()
            if self.inflight.get(key) is submission:
                del self.inflight[key]

    def whereabouts(
        self, found: dict[str, list[Active]], key: tuple[str, str, str], cid: str
    ) -> Active | None:
        """The contract a reassignment request moves, as the state shows it.
        None when it is gone, or when another request of the run moved it:
        then this request did not, and is judged afresh."""
        run_id, role, request_id = key
        if any(
            other != (role, request_id)
            and record.get("contract_id") == cid
            and record.get("outcome") == "COMMITTED"
            for other, record in self.kept_of(run_id).items()
        ):
            return None
        return next((c for c in found[RIGHT] if c.cid == cid), None)

    async def move(
        self,
        key: tuple[str, str, str],
        command: dict,
        found: dict[str, list[Active]],
        cid: str,
        test_id: str,
        fields: dict,
        resumed: bool,
    ) -> tuple[int, dict]:
        """Unassign the right from the source and assign it to the target as
        the actor (BANK_B, hosted by P_X on both), then answer once P_X shows
        it usable on the target (README §3 B2).

        The request is kept as STARTED, naming the contract, before the first
        ledger call. A request kept so (resumed) goes on from where the state
        shows the contract: on the source it is judged again and unassigned,
        in the reassignment it is assigned, on the target it is committed.
        A call whose answer is lost, or that the ledger answered with an error
        it may still act on (a timeout, a server error), leaves the request
        STARTED and is answered UNKNOWN: resending it goes on the same way. A
        request the ledger did not act on (not sent, or firmly refused while
        the contract stays on the source) is withdrawn and refused, as if never
        accepted. The STARTED record names the attempt that kept it
        (started_by): when a resumed request commits, the calls and completion
        events from that attempt on are recorded in contracts.json
        (record_moved, earlier_calls). Events are logged as the ledger's
        answers show them; a step this process did not see logs nothing."""
        run_id, role, request_id = key
        payload = command["payload"]
        source, target = payload["source_synchronizer"], payload["target_synchronizer"]
        record = {
            "run_id": run_id,
            "actor": role,
            "request_id": request_id,
            "command_type": command["command_type"],
            "content": canonical(command),
        }
        moved = {
            "source_synchronizer": source,
            "target_synchronizer": target,
            **fields,
        }
        # This attempt's sent calls, each with the completion event it logged.
        sent: list[tuple[Call, str | None]] = []
        # The calls from the attempt that kept the request STARTED on are its
        # calls (earlier_calls): a resumed request keeps that attempt.
        since = fields.get("attempt_id")
        if resumed:
            since = (self.kept_of(run_id).get((role, request_id)) or {}).get("started_by")
        reassignment: str | None = None
        tried = False
        while True:
            contract = self.whereabouts(found, key, cid)
            if contract is not None and contract.incomplete:
                reassignment = contract.reassignment
                break
            if contract is not None and contract.synchronizer == target:
                # Assigned by an earlier attempt of this request.
                self.record_moved(key, cid, resumed, since, sent, None)
                return self.reassigned(key, command, record, cid, replayed=resumed)
            verdict = self.judge_reassign(found, command, role)
            if verdict.code is not None:
                return self.reject(key, command, verdict.code, verdict.message)
            if tried:
                self.withdraw(key, record)
                raise Refusal(
                    "TEMPORARILY_UNAVAILABLE",
                    "the ledger refused the unassignment for a reason the state "
                    "does not explain",
                )
            tried, cid = True, verdict.right.cid
            self.ensure_selection(run_id, cid, verdict.right.arg["rightId"])
            self.keep(
                key,
                {**record, "outcome": "STARTED", "contract_id": cid, "started_by": since},
            )
            stamp = self.events.reserve()
            call = self.reassignment_call(
                key,
                "UNASSIGN",
                cid,
                {
                    "contractId": cid,
                    "source": self.synchronizers[source],
                    "target": self.synchronizers[target],
                },
            )
            try:
                status, reply = await self.send_call(run_id, test_id, fields, call)
            except (MessageNotLogged, LedgerNotReached) as error:
                log.warning("%s %s: unassignment not sent: %s", run_id, request_id, error)
                self.withdraw(key, record)
                raise Refusal(
                    "TEMPORARILY_UNAVAILABLE",
                    f"participant {self.spec['participant']} cannot be reached now",
                ) from error
            except LedgerUnreachable as error:
                log.warning("%s %s: unassignment answer lost: %s", run_id, request_id, error)
                return 503, self.unknown_of(key, command, resumed)
            event = self.reassignment_event(status, reply, "JsUnassignedEvent")
            if (
                event is not None
                and event.get("contractId") == cid
                and event.get("reassignmentId")
            ):
                reassignment = event["reassignmentId"]
                ids = dict(moved, contract_id=cid, reassignment_id=reassignment)
                self.note("UNASSIGNMENT_STARTED", run_id, test_id, stamp, **ids)
                # Unassigned: the right cannot be used on the source any more,
                # and not yet on the target (measurement.md §9).
                self.note("SOURCE_UNUSABLE", run_id, test_id, **ids)
                sent.append(
                    (call, self.note("UNASSIGNMENT_COMPLETED", run_id, test_id, **ids))
                )
                break
            sent.append((call, None))
            log.warning(
                "%s %s: unassignment not confirmed: %s %s",
                run_id,
                request_id,
                status,
                str(reply)[:2000],
            )
            if not firm_refusal(reply):
                # A timeout or server error: the ledger may still unassign it,
                # so the request is not withdrawn. Followed like a lost answer
                # (as B1 submit does): a resend goes on from the state.
                return 503, self.unknown_of(key, command, resumed)
            found = await self.prepared_run(
                run_id, Trail(run_id, test_id, fields, "recheck")
            )
        if not reassignment:
            log.warning(
                "%s %s: %s is in a reassignment the ledger does not name",
                run_id,
                request_id,
                cid,
            )
            return 503, self.unknown_of(key, command, resumed)
        ids = dict(moved, contract_id=cid, reassignment_id=reassignment)
        self.note("ASSIGNMENT_STARTED", run_id, test_id, **ids)
        call = self.reassignment_call(
            key,
            "ASSIGN",
            cid,
            {
                "reassignmentId": reassignment,
                "source": self.synchronizers[source],
                "target": self.synchronizers[target],
            },
        )
        try:
            status, reply = await self.send_call(run_id, test_id, fields, call)
        except (MessageNotLogged, LedgerUnreachable) as error:
            # Unassigned and not assigned: resending the request assigns it.
            log.warning("%s %s: assignment not confirmed: %s", run_id, request_id, error)
            return 503, self.unknown_of(key, command, resumed)
        event = self.reassignment_event(status, reply, "JsAssignmentEvent")
        if event is None or (event.get("createdEvent") or {}).get("contractId") != cid:
            sent.append((call, None))
            log.warning(
                "%s %s: assignment not confirmed: %s %s",
                run_id,
                request_id,
                status,
                str(reply)[:2000],
            )
            # An earlier attempt may have assigned it (the ledger refuses an
            # assignment already completed).
            found = await self.prepared_run(
                run_id, Trail(run_id, test_id, fields, "recheck")
            )
            contract = self.whereabouts(found, key, cid)
            if contract is not None and contract.synchronizer == target:
                self.record_moved(key, cid, resumed, since, sent, reassignment)
                return self.reassigned(key, command, record, cid, replayed=resumed)
            return 503, self.unknown_of(key, command, resumed)
        sent.append((call, self.note("ASSIGNMENT_COMPLETED", run_id, test_id, **ids)))
        version = int(command["expected_version"])
        self.note(
            "INTERNAL_COMMITTED",
            run_id,
            test_id,
            outcome="COMMITTED",
            contract_id=cid,
            record_id=request_id,
            version_before=version,
            version_after=version,
            **fields,
        )
        if await self.usable(Trail(run_id, test_id, fields, "usable-check"), cid, target):
            self.note("TARGET_USABLE", run_id, test_id, **ids)
        self.record_moved(key, cid, resumed, since, sent, reassignment)
        return self.reassigned(key, command, record, cid, replayed=resumed)

    def reassigned(
        self,
        key: tuple[str, str, str],
        command: dict,
        record: dict,
        cid: str,
        replayed: bool,
    ) -> tuple[int, dict]:
        """Keep the committed reassignment, naming the contract it moved (so
        whereabouts tells another request of the run that it did not), then
        answer it. A failure to keep it is answered INTERNAL_ERROR (process):
        the request stays STARTED and a resend finds the contract on the
        target."""
        version = int(command["expected_version"])
        payload = command["payload"]
        record = dict(
            record,
            outcome="COMMITTED",
            contract_id=cid,
            effect={
                "bundle_id": command["bundle_id"],
                "version_before": version,
                "version_after": version,
                "record_type": "REASSIGNMENT",
                "record_id": key[2],
                "source_synchronizer": payload["source_synchronizer"],
                "target_synchronizer": payload["target_synchronizer"],
            },
        )
        self.keep(key, record)
        log.info("%s %s %s committed", key[0], command["command_type"], key[2])
        return 200, self.kept_answer(record, replayed)

    def reassignment_call(
        self, key: tuple[str, str, str], operation: str, cid: str, value: dict
    ) -> Call:
        """The Ledger API body of an unassign or assign call, submitted by the
        request's actor. Each call has its own command id: a resend is
        decided from the state, not deduplicated by the ledger."""
        call = Call(operation, cid)
        party = self.parties[key[1]]
        command = "UnassignCommand" if operation == "UNASSIGN" else "AssignCommand"
        body = {
            "reassignmentCommands": {
                "submitter": party,
                "commandId": f"{self.platform}-{call.stem.replace(':', '-')}",
                "userId": f"adapter-{self.platform}",
                "commands": [{"command": {command: {"value": value}}}],
            },
            "eventFormat": {"filtersByParty": {party: {}}},
        }
        call.request = json.dumps(body, separators=(",", ":")).encode()
        return call

    async def send_call(
        self, run_id: str, test_id: str, fields: dict, call: Call
    ) -> tuple[int, object]:
        """Send an unassign or assign call and keep its raw request and
        response next to the run's events. The request file is written before
        the call, so a call whose answer is lost keeps it. The MESSAGE_SENT is
        required (Message.sent): the api_calls evidence names it."""
        root = self.events.root / run_id
        self.save(root / call.file("request"), call.request)
        status, raw = await self.ledger_raw(
            "POST",
            REASSIGN_PATH,
            Message(
                Trail(run_id, test_id, fields, call.operation.lower()),
                call.stem,
                call.sent,
            ),
            data=call.request,
            headers={"Content-Type": "application/json"},
        )
        try:
            self.save(root / call.file("response"), raw)
        except OSError:
            # The ledger answered: a response that cannot be kept leaves its
            # evidence out (record_reassignment), not the answer.
            log.exception("%s %s: response not kept", run_id, call.stem)
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, raw[:500].decode(errors="replace")

    @staticmethod
    def reassignment_event(status: int, reply, kind: str) -> dict | None:
        """The unassigned (JsUnassignedEvent) or assigned (JsAssignmentEvent)
        event of a successful submit-and-wait-for-reassignment reply."""
        if status != 200 or not isinstance(reply, dict):
            return None
        reassignment = reply.get("reassignment")
        if not isinstance(reassignment, dict):
            return None
        for event in reassignment.get("events") or []:
            value = event.get(kind) if isinstance(event, dict) else None
            if isinstance(value, dict):
                value = value.get("value", value)
                if isinstance(value, dict):
                    return value
        return None

    async def usable(self, trail: Trail, cid: str, target: str) -> bool:
        """Whether this participant's active contracts show the contract
        assigned to the target (measurement.md §9 t_usable_S2): P_X hosts
        BANK_B, the right's holder, on S2. False when it is not seen within
        USABLE_WAIT; the reassignment is committed either way."""
        deadline = time.monotonic() + USABLE_WAIT
        while True:
            try:
                found = await self.active_contracts(trail.run_id, trail)
            except LedgerUnreachable as error:
                log.warning("TARGET_USABLE not checked: %s", error)
                return False
            if any(
                c.cid == cid and not c.incomplete and c.synchronizer == target
                for c in found[RIGHT]
            ):
                return True
            if time.monotonic() >= deadline:
                log.warning("TARGET_USABLE: %s not seen on %s", cid, target)
                return False
            await asyncio.sleep(LOCAL_POLL)

    def note(
        self, event_type: str, run_id: str, test_id: str, stamp=None, **fields
    ) -> str | None:
        """Log a reassignment event. The ledger has acted whatever happens to
        the event: a problem logging it leaves it out (None), it does not
        stop the reassignment."""
        try:
            return self.events.write(event_type, run_id, test_id, stamp, **fields)
        except Exception:
            log.exception("%s of %s not logged", event_type, fields.get("request_id"))
            return None

    @staticmethod
    def save(path: Path, data: bytes) -> None:
        """Write a file whole: aside, synced, then renamed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".tmp")
        with open(pending, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        pending.replace(path)

    def selection_path(self, run_id: str) -> Path:
        return self.events.root / run_id / "contracts.json"

    def ensure_selection(self, run_id: str, cid: str, right_id: str) -> None:
        """B2 X (README §3 B2): declare the contracts Y needs moved to S2
        (contracts.json, which the common runner reads before it submits
        REASSIGN_FOR_LENDING): the right Y's ConfirmLoan uses, created on S1
        by X's CONFIRM_REPO. It is the only one: the loan agreement is created
        on S2 when the run is prepared (selftest/b2_selftest.py for now: the
        common loader, prepare.py, prepares b1-xyz runs only). Written once per
        run, when CONFIRM_REPO commits (log_commit) or else when the
        reassignment starts."""
        path = self.selection_path(run_id)
        if path.exists():
            return
        manifest = {
            "run_id": run_id,
            "synchronizers": {
                name: self.synchronizers[name] for name in ("S1", "S2")
            },
            "required_contracts": [
                {
                    "contract_id": cid,
                    "reason": (
                        "input of Y's ConfirmLoan: the right BANK_B holds after "
                        "X's CONFIRM_REPO, created on S1; the loan agreement is "
                        "created on S2 at preparation"
                    ),
                    "business_refs": [right_id],
                    "dependencies": [],
                }
            ],
            "api_calls": [],
        }
        self.save(path, (json.dumps(manifest, indent=2) + "\n").encode())

    def record_moved(
        self,
        key: tuple[str, str, str],
        cid: str,
        resumed: bool,
        since: str | None,
        sent: list[tuple[Call, str | None]],
        reassignment: str | None,
    ) -> None:
        """Record a committed reassignment's calls and completion events in
        contracts.json: this attempt's, and for a resumed request first those
        of the attempts before it (earlier_calls). A call repeats the one
        before it of the same operation (retry_of). Committed whatever happens
        to its evidence: a problem recording it leaves the references out, it
        does not change the answer."""
        try:
            calls: list[Call] = []
            links: dict[str, tuple[Call, str]] = {}
            if resumed:
                calls, links = self.earlier_calls(key, cid, since, sent, reassignment)
            for call, completed in sent:
                if call.sent:
                    calls.append(call)
                if completed is not None:
                    links[EVIDENCE_OF[call.operation]] = (call, completed)
            last: dict[str, str] = {}
            for call in calls:
                call.retry_of = last.get(call.operation)
                last[call.operation] = call.call_id
            if calls or links:
                self.record_reassignment(key[0], cid, calls, links)
        except Exception:
            log.exception("%s %s: reassignment evidence not recorded", key[0], key[2])

    def earlier_calls(
        self,
        key: tuple[str, str, str],
        cid: str,
        since: str | None,
        sent: list[tuple[Call, str | None]],
        reassignment: str | None,
    ) -> tuple[list[Call], dict[str, tuple[Call, str]]]:
        """The calls earlier attempts of a resumed request sent, possibly in
        an earlier process, read back from the run's events in the order they
        were logged: each MESSAGE_SENT of this adapter for the request, from
        the attempt that kept it STARTED on (since; all of them when the
        record does not name it), whose request file is kept and is an
        unassignment of the request's contract or an assignment of its
        reassignment. Calls of an attempt withdrawn before that are not the
        request's (README §3 B2).

        A completion event of an earlier attempt (UNASSIGNMENT_COMPLETED,
        ASSIGNMENT_COMPLETED) is linked to the one call whose kept response
        shows it. What cannot be told apart is left out, and nothing is made
        up for a call whose answer was lost: it has no completion event."""
        run_id, role, request_id = key
        root = self.events.root / run_id
        mine = {call.call_id for call, _ in sent}
        own = {completed for _, completed in sent if completed is not None}
        known = {reassignment} if reassignment else set()
        logged: list[Call] = []
        done: dict[str, list[dict]] = {operation: [] for operation in COMPLETION_OF}
        started = since is None
        try:
            lines = (root / "events.jsonl").read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or (
                event.get("run_id"),
                event.get("request_id"),
                event.get("actor"),
            ) != (run_id, request_id, role):
                continue
            if not started:
                if event.get("attempt_id") != since:
                    continue
                started = True
            if event.get("producer_id") != self.events.producer:
                continue
            kind = event.get("event_type")
            if event.get("contract_id") == cid and event.get("reassignment_id"):
                known.add(event["reassignment_id"])
                for operation, (completion, _) in COMPLETION_OF.items():
                    if kind == completion and event.get("event_id") not in own:
                        done[operation].append(event)
            message = event.get("message_id")
            if (
                kind == "MESSAGE_SENT"
                and event.get("message_kind") == "REQUEST"
                and isinstance(message, str)
                and CALL_MESSAGE.fullmatch(message)
                and message not in mine
                and event.get("event_id")
            ):
                stem = message.removesuffix(":req")
                call = Call(stem.split(":", 1)[0].upper(), cid, stem=stem)
                call.sent = [event["event_id"]]
                logged.append(call)
        if since is None:
            log.warning(
                "%s %s: the STARTED record names no attempt; calls of every "
                "attempt are read",
                run_id,
                request_id,
            )
        elif not started:
            log.warning(
                "%s %s: attempt %s is not among the logged events; earlier "
                "calls are left out",
                run_id,
                request_id,
                since,
            )
        # An unassignment of the contract also names its reassignment in the
        # response, when one was kept.
        for call in logged:
            value = self.shows(root, call, cid, None) if call.operation == "UNASSIGN" else None
            if value and value.get("reassignmentId"):
                known.add(value["reassignmentId"])
        calls = []
        for call in logged:
            value = command_value(read_json(root / call.file("request")), call.operation)
            if call.operation == "UNASSIGN" and value.get("contractId") == cid:
                calls.append(call)
            elif call.operation == "ASSIGN" and value.get("reassignmentId") in known:
                calls.append(call)
        links: dict[str, tuple[Call, str]] = {}
        for operation, events in done.items():
            if len(events) != 1:
                continue
            event = events[0]
            shown = [
                call
                for call in calls
                if call.operation == operation
                and self.shows(root, call, cid, event["reassignment_id"])
            ]
            if len(shown) == 1:
                links[EVIDENCE_OF[operation]] = (shown[0], event["event_id"])
        return calls, links

    def shows(
        self, root: Path, call: Call, cid: str, reassignment: str | None
    ) -> dict | None:
        """The completion a kept response of the call shows for the contract
        (of the reassignment, when given): the unassigned or assigned event."""
        kind = COMPLETION_OF[call.operation][1]
        value = self.reassignment_event(200, read_json(root / call.file("response")), kind)
        if value is None:
            return None
        shown = value.get("contractId") if call.operation == "UNASSIGN" else (
            (value.get("createdEvent") or {}).get("contractId")
        )
        if shown != cid:
            return None
        if reassignment is not None and value.get("reassignmentId") != reassignment:
            return None
        return value

    def record_reassignment(
        self,
        run_id: str,
        cid: str,
        calls: list[Call],
        links: dict[str, tuple[Call, str]],
    ) -> None:
        """Add a committed reassignment's calls and raw evidence to
        contracts.json, leaving the selection as it was: for each completion
        event the response it came with (links), and each call's request. A
        call already listed stays as it is, so recording a request again (a
        resend of a committed one) adds only what is new; so does an evidence
        reference already recorded, and one that differs is logged and left
        as it was. A step whose event was not logged adds no reference."""
        path = self.selection_path(run_id)
        manifest = json.loads(path.read_text())
        root = self.events.root / run_id
        contract = next(
            (c for c in manifest["required_contracts"] if c["contract_id"] == cid),
            None,
        )
        if contract is None:
            raise ValueError(f"{cid} is not among the declared contracts")
        for name in EVIDENCE_OF.values():
            if name not in links:
                continue
            call, completed = links[name]
            response = call.file("response")
            kept = contract.get(name)
            if kept:
                if kept.get("event_id") != completed or kept.get("path") != response:
                    log.warning(
                        "%s %s: %s kept as %s, not %s",
                        run_id,
                        cid,
                        name,
                        kept.get("event_id"),
                        completed,
                    )
                continue
            contract[name] = {
                "event_id": completed,
                "path": response,
                "sha256": hashlib.sha256((root / response).read_bytes()).hexdigest(),
            }
        listed = {c.get("call_id") for c in manifest["api_calls"]}
        for call in calls:
            if not call.sent or call.call_id in listed:
                continue
            listed.add(call.call_id)
            request = call.file("request")
            manifest["api_calls"].append(
                {
                    "call_id": call.call_id,
                    "operation": call.operation,
                    "contract_ids": [call.contract_id],
                    "retry_of": call.retry_of,
                    "evidence": {
                        "path": request,
                        "sha256": hashlib.sha256((root / request).read_bytes()).hexdigest(),
                        "event_id": call.sent[0],
                    },
                }
            )
        self.save(path, (json.dumps(manifest, indent=2) + "\n").encode())

    # --- requests ---------------------------------------------------------

    async def answer(
        self, key: tuple[str, str, str], command: dict, test_id: str, fields: dict
    ) -> tuple[int, dict]:
        """One task decides a request key at a time (interface §6.3): a second
        submission of the key waits for it and is then decided afresh, so it
        finds the first one's record. Past the response wait the submission
        is answered PENDING and the task goes on."""
        run_id, role, request_id = key
        deadline = time.monotonic() + self.response_wait
        behind = False  # waited for another submission of the key
        while True:
            work = self.tasks.get(key)
            mine = work is None or work.task.done()
            if mine:
                task = asyncio.create_task(self.process(key, command, test_id, fields))
                work = Work(task, command)
                self.tasks[key] = work
                task.add_done_callback(
                    lambda _, k=key, w=work: (
                        self.tasks.pop(k) if self.tasks.get(k) is w else None
                    )
                )
            elif canonical(work.command) != canonical(command):
                raise self.conflict(request_id)
            else:
                behind = True
            try:
                status, body = await asyncio.wait_for(
                    asyncio.shield(work.task), max(deadline - time.monotonic(), 0)
                )
            except TimeoutError:
                return 202, self.pending_of(key, command, replayed=behind)
            if mine:
                return status, body

    def pending_of(
        self, key: tuple[str, str, str], command: dict, replayed: bool
    ) -> dict:
        return self.outcome_of(
            key[0], key[2], key[1], command["command_type"], "PENDING", replayed
        )

    def unknown_of(
        self, key: tuple[str, str, str], command: dict, replayed: bool
    ) -> dict:
        return self.outcome_of(
            key[0],
            key[2],
            key[1],
            command["command_type"],
            "UNKNOWN",
            replayed,
            ("OUTCOME_UNKNOWN", "the ledger's answer to the transaction was lost"),
        )

    async def process(
        self, key: tuple[str, str, str], command: dict, test_id: str, fields: dict
    ) -> tuple[int, dict]:
        try:
            return await self.decide(key, command, test_id, fields)
        except Refusal as refusal:
            return STATUS[refusal.code], error_body(
                key[0], key[2], refusal.code, refusal.message
            )
        except Exception:
            log.exception("%s %s failed", command["command_type"], key[2])
            return 503, error_body(
                key[0],
                key[2],
                "INTERNAL_ERROR",
                "the adapter failed to process the request",
            )

    async def decide(
        self, key: tuple[str, str, str], command: dict, test_id: str, fields: dict
    ) -> tuple[int, dict]:
        """The request's existing record comes before the state check
        (interface §6.3): a request kept on disk (a rejection, or on B2 X a
        reassignment), the trade contract a commit created, or a submission
        whose answer was lost. A reassignment kept as STARTED goes on from
        where the state shows its contract (README §3 B2)."""
        run_id, role, request_id = key
        kind = command["command_type"]
        content = canonical(command)
        kept = self.kept_of(run_id).get((role, request_id))
        if kept is not None:
            if kept["content"] != content:
                raise self.conflict(request_id)
            if kept.get("outcome") != "STARTED":
                return 200, self.kept_answer(kept, replayed=True)
        found = await self.prepared_run(run_id, Trail(run_id, test_id, fields, "state"))
        record = self.record_of(found, role, request_id)
        if record is not None:
            if not record_matches(record, command):
                raise self.conflict(request_id)
            return 200, self.result_of(run_id, record, replayed=True)
        if kept is not None:
            return await self.reassign(
                key, command, found, kept["contract_id"], test_id, fields, resumed=True
            )
        earlier = self.unknown.get(key)
        if earlier is not None and canonical(earlier) != content:
            raise self.conflict(request_id)
        judge = self.judge_reassign if kind == REASSIGN else self.judge
        verdict = judge(found, command, role)
        if verdict.absent:
            # The participant may not have applied the preceding trade yet.
            reads, deadline = 1, time.monotonic() + ABSENT_WAIT
            while verdict.absent and time.monotonic() < deadline:
                await asyncio.sleep(LOCAL_POLL)
                found = await self.prepared_run(
                    run_id, Trail(run_id, test_id, fields, "poll")
                )
                reads += 1
                record = self.record_of(found, role, request_id)
                if record is not None:
                    if not record_matches(record, command):
                        raise self.conflict(request_id)
                    return 200, self.result_of(run_id, record, replayed=True)
                verdict = judge(found, command, role)
            log.info(
                "%s %s %s: no right of the bundle on the first read; after %d reads: %s",
                run_id,
                kind,
                request_id,
                reads,
                verdict.code or "submitted",
            )
        if verdict.code is not None:
            return self.reject(key, command, verdict.code, verdict.message)
        if kind == REASSIGN:
            return await self.reassign(
                key, command, found, verdict.right.cid, test_id, fields, resumed=False
            )
        return await self.submit(
            key, command, verdict, test_id, fields, earlier is not None
        )

    def exercise_body(
        self, key: tuple[str, str, str], command: dict, verdict: Verdict
    ) -> dict:
        confirmation = CONFIRMATIONS[command["command_type"]]
        return {
            "commands": {
                "commands": [
                    {
                        "ExerciseCommand": {
                            "templateId": f"#{PACKAGE}:{confirmation.agreement}",
                            "contractId": verdict.agreement.cid,
                            "choice": confirmation.choice,
                            "choiceArgument": {
                                "requestId": command["request_id"],
                                confirmation.record_argument: command["payload"][
                                    confirmation.record_key
                                ],
                                "expectedVersion": str(command["expected_version"]),
                                "rightCid": verdict.right.cid,
                            },
                        }
                    }
                ],
                "commandId": self.command_id(key),
                "userId": f"adapter-{self.platform}",
                "actAs": [self.parties[key[1]]],
                # B2: on the platform's own synchronizer, where judge found
                # every input (README §3 B2).
                **({"synchronizerId": self.synchronizer} if self.b2 else {}),
            },
            "transactionFormat": {
                "eventFormat": self.party_filter(),
                "transactionShape": "TRANSACTION_SHAPE_ACS_DELTA",
            },
        }

    async def submit(
        self,
        key: tuple[str, str, str],
        command: dict,
        verdict: Verdict,
        test_id: str,
        fields: dict,
        earlier: bool,
    ) -> tuple[int, dict]:
        """Submit the confirmation under the request's command id. A refusal is
        explained by the state it leaves (the right taken by another request,
        or this request's trade); an answer that was lost is followed by
        resubmitting the same command id, which the ledger deduplicates."""
        run_id, role, request_id = key
        kind = command["command_type"]
        submission = Submission(command["bundle_id"])
        self.inflight[key] = submission
        # sent: the ledger may have acted on a submission of this request.
        sent, replayed = earlier, earlier
        # confirmed: the ledger answered that it committed this request.
        confirmed = False
        started, attempts = time.monotonic(), 0
        body = self.exercise_body(key, command, verdict)
        try:
            while True:
                attempts += 1
                code, firm = None, False
                try:
                    status, reply = await self.ledger(
                        "POST",
                        SUBMIT_PATH,
                        Message(
                            Trail(run_id, test_id, fields, "submit"),
                            f"submit:{uuid.uuid4().hex}",
                        ),
                        json=body,
                    )
                except LedgerUnreachable as error:
                    log.warning(
                        "%s %s %s: no ledger answer: %s",
                        run_id,
                        kind,
                        request_id,
                        error,
                    )
                    sent = sent or not isinstance(error, LedgerNotReached)
                    if not sent:
                        raise Refusal(
                            "TEMPORARILY_UNAVAILABLE",
                            f"participant {self.spec['participant']} cannot be reached now",
                        ) from error
                else:
                    submission.answered.set()
                    if status == 200 and isinstance(reply, dict):
                        if self.inflight.get(key) is submission:
                            del self.inflight[key]
                        sent = confirmed = True
                        try:
                            record = await self.committed(
                                run_id, test_id, command, verdict, reply, fields
                            )
                        except Exception:
                            # The ledger committed: a reply that cannot be read
                            # must not turn that into an error answer. The
                            # state shows the trade.
                            log.exception(
                                "%s %s %s committed, but its reply was not read",
                                run_id,
                                kind,
                                request_id,
                            )
                            # Still pending in the snapshot until the state
                            # shows the trade.
                            submission.kind = "RECOVERING"
                            self.inflight[key] = submission
                        else:
                            self.unknown.pop(key, None)
                            return 200, self.result_of(run_id, record, replayed)
                    else:
                        code, firm = ledger_code(reply), firm_refusal(reply)
                        log.warning(
                            "%s %s %s not confirmed (attempt %d): %s %s",
                            run_id,
                            kind,
                            request_id,
                            attempts,
                            status,
                            str(reply)[:2000],
                        )
                        if code in DEDUPLICATED:
                            sent = True
                            # Deduplicated on the first attempt: an earlier
                            # process submitted this request.
                            replayed = replayed or attempts == 1
                        elif not firm:
                            # The ledger may still commit it (a timeout or
                            # server error): followed like a lost answer.
                            sent = True
                others = firm and code not in DEDUPLICATED
                settled = await self.settle(
                    key,
                    command,
                    others,
                    replayed,
                    Trail(run_id, test_id, fields, "recheck"),
                    judge=not confirmed,
                )
                if settled is not None:
                    return settled
                if not sent:
                    raise Refusal(
                        "TEMPORARILY_UNAVAILABLE",
                        "the ledger refused the transaction for a reason the state "
                        "does not explain",
                    )
                submission.kind = "RECOVERING"
                if time.monotonic() - started >= TRACK_WAIT:
                    self.unknown[key] = command
                    log.warning(
                        "%s %s %s: outcome unknown after %d submissions",
                        run_id,
                        kind,
                        request_id,
                        attempts,
                    )
                    return 503, self.unknown_of(key, command, replayed)
                log.info(
                    "%s %s %s: outcome not known, resubmitting command id (attempt %d)",
                    run_id,
                    kind,
                    request_id,
                    attempts + 1,
                )
                await asyncio.sleep(RESUBMIT_PAUSE)
        finally:
            submission.answered.set()
            if self.inflight.get(key) is submission:
                del self.inflight[key]

    async def settle(
        self,
        key: tuple[str, str, str],
        command: dict,
        others: bool,
        replayed: bool,
        trail: Trail,
        judge: bool = True,
    ) -> tuple[int, dict] | None:
        """Explain an unconfirmed submission by the state: this request's trade
        (committed), or a business rejection the state now shows (not looked
        for when the ledger answered that it committed). None when the state
        explains nothing within SETTLE_WAIT."""
        run_id, role, request_id = key
        if others:
            # A contention refusal: let the other submissions of the bundle get
            # their answer, so the state shows who took the right.
            waits = [
                asyncio.ensure_future(s.answered.wait())
                for k, s in self.inflight.items()
                if k != key
                and k[0] == run_id
                and s.bundle_id == command["bundle_id"]
                and not s.answered.is_set()
            ]
            if waits:
                _, pending = await asyncio.wait(waits, timeout=SETTLE_WAIT)
                for wait in pending:
                    wait.cancel()
        deadline = time.monotonic() + SETTLE_WAIT
        while True:
            try:
                found = await self.prepared_run(run_id, trail)
            except Refusal as refusal:
                log.warning(
                    "%s %s: state not read: %s", run_id, request_id, refusal.message
                )
            else:
                record = self.record_of(found, role, request_id)
                if record is not None:
                    if not record_matches(record, command):
                        raise self.conflict(request_id)
                    return 200, self.result_of(run_id, record, replayed)
                verdict = self.judge(found, command, role) if judge else None
                if verdict and verdict.code is not None and not verdict.absent:
                    return self.reject(key, command, verdict.code, verdict.message)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(LOCAL_POLL)

    async def committed(
        self,
        run_id: str,
        test_id: str,
        command: dict,
        verdict: Verdict,
        reply: dict,
        fields: dict,
    ) -> Record:
        """Log what the committed transaction actually created (README §3, §4)
        and return the record contract (trade or allocation) it created."""
        kind = command["command_type"]
        confirmation = CONFIRMATIONS[kind]
        record_id = command["payload"][confirmation.record_key]
        transaction = reply["transaction"]
        created = [
            e["CreatedEvent"] for e in transaction["events"] if "CreatedEvent" in e
        ]

        def one(entity: str, **match) -> dict | None:
            hits = [
                e
                for e in created
                if e["templateId"].split(":", 1)[1] == entity
                and all(e["createArgument"].get(k) == v for k, v in match.items())
            ]
            if len(hits) == 1:
                return hits[0]
            log.warning(
                "%s %s: the committed transaction has %d %s %s",
                kind,
                command["request_id"],
                len(hits),
                entity,
                match,
            )
            return None

        log.info(
            "%s %s %s committed at offset %s",
            run_id,
            kind,
            command["request_id"],
            transaction["offset"],
        )
        record = one(confirmation.record, **{confirmation.record_field: record_id})
        if record is None:
            raise ValueError("the committed transaction has no single record contract")
        try:
            await self.log_commit(run_id, test_id, command, verdict, record, one, fields)
        except Exception:
            # The record is committed whatever happens to its events: a problem
            # logging them leaves them out, it does not change the answer.
            log.exception(
                "%s %s %s: events of the commit not fully logged",
                run_id,
                kind,
                command["request_id"],
            )
        return Record(kind, record["createArgument"])

    async def log_commit(
        self,
        run_id: str,
        test_id: str,
        command: dict,
        verdict: Verdict,
        record: dict,
        one,
        fields: dict,
    ) -> None:
        """The commit's events, in the order history_check requires."""
        bundle_id = command["bundle_id"]
        record_id = command["payload"][CONFIRMATIONS[command["command_type"]].record_key]
        # The record contract proves the commit: log it before anything else of
        # the reply is read, so a later problem cannot drop the event.
        version_after = int(record["createArgument"]["versionAfter"])
        effect = {
            "record_id": record_id,
            "version_before": int(record["createArgument"]["versionBefore"]),
            "version_after": version_after,
        }
        self.events.write(
            "INTERNAL_COMMITTED",
            run_id,
            test_id,
            outcome="COMMITTED",
            contract_id=record["contractId"],
            **effect,
            **fields,
        )
        # A right missing from the reply leaves its event out; it is not invented.
        if command["command_type"] == "ALLOCATE_MARGIN":
            # The right is locked in place: the same right ID, LOCKED, no
            # successor (README §3).
            locked = one(RIGHT, rightId=verdict.right.arg["rightId"], status="LOCKED")
            if locked is None:
                return
            self.events.write(
                "RIGHT_LOCKED",
                run_id,
                test_id,
                record_id=record_id,
                **self.right_event(locked),
                **fields,
            )
            if await self.locally_applied(
                Trail(run_id, test_id, fields, "local-check"), locked["contractId"]
            ):
                self.events.write(
                    "LOCAL_APPLIED",
                    run_id,
                    test_id,
                    contract_id=locked["contractId"],
                    **effect,
                    **fields,
                )
            return
        consumed = one(RIGHT, rightId=verdict.right.arg["rightId"], status="CONSUMED")
        successor = one(
            RIGHT, rightId=f"{bundle_id}:right:{version_after}", status="AVAILABLE"
        )
        # Consumption first: the choice consumes the right before it creates the
        # successor, and history_check requires that order on one clock.
        if consumed is not None:
            self.events.write(
                "RIGHT_CONSUMED",
                run_id,
                test_id,
                record_id=record_id,
                **self.right_event(consumed),
                **fields,
            )
        if successor is not None:
            self.events.write(
                "RIGHT_CREATED",
                run_id,
                test_id,
                record_id=record_id,
                **self.right_event(successor),
                **fields,
            )
            if self.b2 and REASSIGN in self.commands:
                # B2 X: the right Y uses is now on S1 (README §3 B2).
                try:
                    self.ensure_selection(
                        run_id,
                        successor["contractId"],
                        successor["createArgument"]["rightId"],
                    )
                except Exception:
                    log.exception("%s: reassignment selection not written", run_id)
            if await self.locally_applied(
                Trail(run_id, test_id, fields, "local-check"), successor["contractId"]
            ):
                self.events.write(
                    "LOCAL_APPLIED",
                    run_id,
                    test_id,
                    contract_id=successor["contractId"],
                    **effect,
                    **fields,
                )

    async def locally_applied(self, trail: Trail, right_cid: str) -> bool:
        """Whether this participant's active contracts show the new right
        (measurement.md §4.1 LOCAL_APPLIED). The transaction is committed either
        way, so a failed check only leaves the event out; it is not an error."""
        deadline = time.monotonic() + LOCAL_WAIT
        while True:
            try:
                found = await self.active_contracts(trail.run_id, trail)
            except LedgerUnreachable as error:
                log.warning("LOCAL_APPLIED not checked: %s", error)
                return False
            if any(c.cid == right_cid for c in found[RIGHT]):
                return True
            if time.monotonic() >= deadline:
                log.warning(
                    "new right %s not active after %ss", right_cid[:16], LOCAL_WAIT
                )
                return False
            await asyncio.sleep(LOCAL_POLL)

    # --- handlers ---------------------------------------------------------

    async def post_command(self, request: web.Request) -> web.Response:
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            raw = None
        command = None
        if raw is not None:
            try:
                command = parse_json(raw)
            except (ValueError, UnicodeDecodeError):
                pass
        if not isinstance(command, dict):
            command = None
        run_id = self.identify(request, command and command.get("request_id"))
        role = self.authenticate(request)
        run_id = self.require_run_id(run_id)
        if raw is None:
            raise Refusal("INVALID_REQUEST", f"the body exceeds {MAX_BODY} bytes")
        if command is None:
            raise Refusal("INVALID_REQUEST", "the body is not one JSON object")
        # COMMAND_RECEIVED/FINISHED need an institution, request and bundle; a call
        # without them (no credential, observer, unparsable body) is not attributable.
        attributable = (
            role in INSTITUTIONS
            and "request_id" in request
            and valid_id(command.get("bundle_id"))
        )
        test_id, fields = "", {}
        if attributable:
            test_id, attempt = self.correlation(request)
            fields = {
                "request_id": command["request_id"],
                "attempt_id": attempt,
                "bundle_id": command["bundle_id"],
                "actor": role,
            }
            if command.get("command_type") in COMMAND_TYPES:
                fields["command_type"] = command["command_type"]
            self.events.write("COMMAND_RECEIVED", run_id, test_id, **fields)
        try:
            self.check_command(role, command)
            # A command that passed check_command is attributable: the schema
            # requires request_id and bundle_id, and its submitter is a bank.
            key = (run_id, role, command["request_id"])
            status, body = await self.answer(key, command, test_id, fields)
        except asyncio.CancelledError:
            # The client left. The task deciding the request goes on and
            # writes its events; this call has no answer to log.
            if attributable:
                log.info(
                    "%s: the client left, processing continues", request["request_id"]
                )
            raise
        except Exception as error:
            # Every COMMAND_RECEIVED gets its FINISHED, with the code the errors
            # middleware will answer (INTERNAL_ERROR for anything unexpected).
            if attributable:
                code = error.code if isinstance(error, Refusal) else "INTERNAL_ERROR"
                self.events.write(
                    "COMMAND_FINISHED", run_id, test_id, error_code=code, **fields
                )
            raise
        finished: dict[str, object] = {}
        if body.get("outcome") is not None:
            finished["outcome"] = body["outcome"]
        if body.get("error") is not None:
            finished["error_code"] = body["error"]["code"]
        if body.get("effect") is not None:
            effect = body["effect"]
            finished.update(
                record_id=effect["record_id"],
                version_before=effect["version_before"],
                version_after=effect["version_after"],
            )
        self.events.write("COMMAND_FINISHED", run_id, test_id, **finished, **fields)
        return web.json_response(body, status=status)

    async def get_command(self, request: web.Request) -> web.Response:
        request_id = request.match_info["request_id"]
        run_id = self.identify(request, request_id)
        role = self.authenticate(request)
        run_id = self.require_run_id(run_id)
        if "request_id" not in request:
            raise Refusal("INVALID_REQUEST", "request_id is not a valid identifier")
        if role not in self.actors:
            raise Refusal(
                "FORBIDDEN",
                f"the credential has no requests on platform {self.platform}",
            )
        key = (run_id, role, request_id)
        trail = self.query_trail(
            request, run_id, "result", request_id=request_id, actor=role
        )
        # Read only: the record kept for the key, else the task still deciding it.
        # A task that ends while it is read has written its record by then:
        # read once more.
        for _ in range(2):
            work = self.tasks.get(key)
            busy = work is not None and not work.task.done()
            kept = self.kept_of(run_id).get((role, request_id))
            if kept is not None and kept.get("outcome") != "STARTED":
                return web.json_response(self.kept_answer(kept, replayed=False))
            if kept is not None:
                # A reassignment started and not decided: still going on, or
                # its outcome is unknown until it is resent (README §3 B2).
                # Nothing awaited since busy was read, so a task that ended
                # has already kept its decision.
                command = json.loads(kept["content"])
                if busy:
                    return web.json_response(
                        self.pending_of(key, command, replayed=False), status=202
                    )
                return web.json_response(
                    self.unknown_of(key, command, replayed=False), status=503
                )
            found = await self.prepared_run(run_id, trail)
            record = self.record_of(found, role, request_id)
            if record is not None:
                return web.json_response(self.result_of(run_id, record, replayed=False))
            work = self.tasks.get(key)
            if work is not None and not work.task.done():
                return web.json_response(
                    self.pending_of(key, work.command, replayed=False), status=202
                )
            if not busy:
                break
        command = self.unknown.get(key)
        if command is not None:
            return web.json_response(
                self.unknown_of(key, command, replayed=False), status=503
            )
        raise Refusal(
            "NOT_FOUND",
            f"no request {request_id} of {role} on platform {self.platform}",
        )

    async def get_readiness(self, request: web.Request) -> web.Response:
        run_id = self.identify(request)
        role = self.authenticate(request)
        run_id = self.require_run_id(run_id)
        query = request.query
        keys = {"command_type", "bundle_id", self.spec["readiness"]}
        if (
            set(query) != keys
            or len(query) != len(keys)
            or not all(valid_id(query[k]) for k in keys - {"command_type"})
        ):
            raise Refusal(
                "INVALID_REQUEST", f"readiness takes exactly {', '.join(sorted(keys))}"
            )
        kind = query["command_type"]
        if kind not in self.commands:
            raise Refusal(
                "INVALID_REQUEST", f"{kind} is not provided on platform {self.platform}"
            )
        if self.commands[kind] != role:
            raise Refusal("FORBIDDEN", f"the credential may not query {kind} readiness")
        # Read only (interface §7): nothing is reserved, consumed or confirmed.
        bundle_id = query["bundle_id"]
        trail = self.query_trail(
            request,
            run_id,
            "readiness",
            bundle_id=bundle_id,
            command_type=kind,
            actor=role,
        )
        found = await self.prepared_run(run_id, trail)
        reference = query[self.spec["readiness"]]
        if kind == "ALLOCATE_MARGIN":
            reasons, available = self.margin_readiness(found, bundle_id, reference, role)
        else:
            reasons, available = self.agreement_readiness(
                found, kind, bundle_id, reference, role
            )
        reasons = list(dict.fromkeys(reasons))
        return web.json_response(
            {
                "run_id": run_id,
                "platform": self.platform,
                "bundle_id": bundle_id,
                "command_type": kind,
                "ready": not reasons,
                "observed_version": int(available[0]["createdVersion"])
                if available
                else None,
                "blocking_reasons": reasons,
            }
        )

    def agreement_readiness(
        self,
        found: dict[str, list[Active]],
        kind: str,
        bundle_id: str,
        agreement_id: str,
        role: str,
    ) -> tuple[list[str], list[dict]]:
        """Readiness of a confirmation against a pre-agreement (today only Y
        CONFIRM_LOAN): the blocking reasons and the submitter's available
        rights of the bundle."""
        agreements = [
            c
            for c in found[CONFIRMATIONS[kind].agreement]
            if c.arg["agreementId"] == agreement_id and c.arg["bundleId"] == bundle_id
        ]
        if not agreements:
            # shared/README §5: a missing target is an error, not ready=false.
            raise Refusal(
                "NOT_FOUND",
                f"no agreement {agreement_id} for bundle {bundle_id} on platform "
                f"{self.platform}",
            )
        if len(agreements) > 1:
            raise preparation_conflict("more than one pre-agreement for the bundle")
        if self.away(agreements[0]):
            raise preparation_conflict(f"the pre-agreement is not on {self.home}")
        reasons = []
        collateral = agreements[0].arg["collateral"]
        if not (collateral["provided"] and collateral["conditionsMet"]):
            reasons.append("PRECONDITION_NOT_MET")  # acceptance.md AC-04
        held = [
            c
            for c in found[RIGHT]
            if c.arg["bundle"]["bundleId"] == bundle_id
            and c.arg["holder"] == self.parties[role]
        ]
        available = [c for c in held if c.arg["status"] == "AVAILABLE"]
        if len(available) > 1:
            raise preparation_conflict(f"{role} holds more than one available right")
        if not held:
            # The preceding trade has not given the submitter a right yet.
            reasons.append("PRECONDITION_NOT_MET")
        elif not available or self.away(available[0]):
            # B2: a right not (yet) on this synchronizer cannot be used here.
            reasons.append("RIGHT_UNAVAILABLE")
        elif kind not in available[0].arg["allowedCommands"]:
            reasons.append("USE_NOT_PERMITTED")
        return reasons, [c.arg for c in available]

    def margin_readiness(
        self,
        found: dict[str, list[Active]],
        bundle_id: str,
        margin_call_id: str,
        role: str,
    ) -> tuple[list[str], list[dict]]:
        """ALLOCATE_MARGIN readiness: every reason judge_margin would reject
        for, in its order; VERSION_CONFLICT needs an expected version and is
        not a readiness reason (readiness.schema)."""
        calls = [
            c
            for c in found[MARGIN_CALL]
            if c.arg["marginCallId"] == margin_call_id and c.arg["bundleId"] == bundle_id
        ]
        if not calls:
            # shared/README §5: a missing target is an error, not ready=false.
            raise Refusal(
                "NOT_FOUND",
                f"no margin call {margin_call_id} for bundle {bundle_id} on platform "
                f"{self.platform}",
            )
        if len(calls) > 1:
            raise preparation_conflict("more than one margin call for the bundle")
        if self.away(calls[0]):
            raise preparation_conflict(f"the margin call is not on {self.home}")
        call, user = calls[0].arg, self.parties[role]
        located = [c for c in found[RIGHT] if c.arg["bundle"]["bundleId"] == bundle_id]
        rights = [c.arg for c in located]
        reasons = []
        if not rights:
            reasons.append("PRECONDITION_NOT_MET")  # the loan has not reached Z
        if any(a["status"] == "LOCKED" for a in rights):
            reasons.append("RIGHT_UNAVAILABLE")  # already allocated
        if call["status"] != "OPEN" or call["provider"] != user:
            reasons.append("PRECONDITION_NOT_MET")
        if not (call["wholeBundleAccepted"] and call["eligibilityPassed"]):
            reasons.append("COLLATERAL_NOT_ELIGIBLE")
        held = [c for c in located if c.arg["holder"] == user]
        usable = [c for c in held if c.arg["status"] == "AVAILABLE"]
        available = [c.arg for c in usable]
        if len(available) > 1:
            raise preparation_conflict(f"{role} holds more than one available right")
        if not held:
            reasons.append("PRECONDITION_NOT_MET")
        elif not available or self.away(usable[0]):
            # B2: a right not (yet) on this synchronizer cannot be used here.
            reasons.append("RIGHT_UNAVAILABLE")
        else:
            right = available[0]
            if not any(
                c.arg["contractType"] == "LOAN"
                and c.arg["tradeId"] == right.get("sourceRecordId")
                and int(c.arg["versionAfter"]) == int(right["createdVersion"])
                for c in found[TRADE]
            ):
                reasons.append("PRECONDITION_NOT_MET")
            if "ALLOCATE_MARGIN" not in right["allowedCommands"]:
                reasons.append("USE_NOT_PERMITTED")
        return reasons, available

    async def get_snapshot(self, request: web.Request) -> web.Response:
        run_id = self.identify(request)
        role = self.authenticate(request)
        run_id = self.require_run_id(run_id)
        if role != OBSERVER:
            raise Refusal(
                "FORBIDDEN", "only the experiment observer may read the snapshot"
            )
        found = await self.prepared_run(
            run_id, self.query_trail(request, run_id, "snapshot")
        )
        return web.json_response(self.snapshot_of(run_id, found))

    @web.middleware
    async def errors(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except Refusal as refusal:
            code, message = refusal.code, refusal.message
        except (web.HTTPNotFound, web.HTTPMethodNotAllowed):
            code, message = "NOT_FOUND", f"no such call on platform {self.platform}"
        except Exception:
            log.exception("unexpected error in %s %s", request.method, request.path)
            code, message = "INTERNAL_ERROR", "unexpected adapter error"
        body = error_body(
            request.get("run_id"), request.get("request_id"), code, message
        )
        headers = {"WWW-Authenticate": "Bearer"} if code == "UNAUTHENTICATED" else None
        return web.json_response(body, status=STATUS[code], headers=headers)

    # --- lifecycle --------------------------------------------------------

    async def serve(self) -> None:
        trace = TraceConfig()
        trace.on_request_headers_sent.append(self.request_sent)
        self.session = ClientSession(timeout=LEDGER_TIMEOUT, trace_configs=[trace])
        runner = None
        try:
            warmup_ms = await self.prepare_ledger()
            log.info("warm-up on %s: %s ms", self.spec["participant"], warmup_ms)
            app = web.Application(middlewares=[self.errors], client_max_size=MAX_BODY)
            app.router.add_post("/runs/{run_id}/commands", self.post_command)
            app.router.add_get("/runs/{run_id}/commands/{request_id}", self.get_command)
            app.router.add_get("/runs/{run_id}/snapshot", self.get_snapshot)
            if self.spec["readiness"]:
                app.router.add_get("/runs/{run_id}/readiness", self.get_readiness)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", self.port).start()
            stop = asyncio.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            # Written aside and renamed: adapter.sh and the self-test read the
            # file as soon as it exists.
            pending = self.ready_file.with_name(self.ready_file.name + ".tmp")
            pending.write_text(
                json.dumps(
                    {
                        "platform": self.platform,
                        "pid": os.getpid(),
                        "port": self.port,
                        "url": f"http://127.0.0.1:{self.port}",
                        "ledger": self.ledger_url,
                        "ledger_override": self.ledger_override,
                        "producer_id": self.events.producer,
                        "clock_id": self.events.clock,
                        "warmup_ms": warmup_ms,
                    }
                )
                + "\n"
            )
            pending.replace(self.ready_file)
            log.info("platform %s listening on 127.0.0.1:%d", self.platform, self.port)
            await stop.wait()
        finally:
            self.ready_file.unlink(missing_ok=True)
            if runner is not None:
                await runner.cleanup()
            await self.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument(
        "--port",
        type=int,
        help="default: "
        + ", ".join(f"{spec['port']} for {p}" for p, spec in sorted(PLATFORMS.items())),
    )
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="print the platform's default port and exit (used by adapter.sh)",
    )
    args = parser.parse_args()
    if args.print_port:
        print(PLATFORMS[args.platform]["port"])
        return
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    adapter = Adapter(args.platform, args.port or PLATFORMS[args.platform]["port"])
    asyncio.run(adapter.serve())


if __name__ == "__main__":
    main()
