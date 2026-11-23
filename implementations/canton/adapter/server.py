"""Platform adapter of the Canton B1 model (implementations/canton/README.md §2).

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

Stage 3 (README §3 "결과 상태와 멱등성"): business rejections with the common
codes, idempotent replay per (run, authenticated actor, request id), request
id conflicts, contention and recovery. Committed requests are kept by the
ledger (the trade contract names its request); rejected requests are kept in
$CANTON_STATE_DIR/requests/<platform>/<run_id>.jsonl; requests in progress
and submissions whose ledger answer was lost are kept in this process only.

Z (README §3 "Z: CCP 증거금 배정·잠금"): ALLOCATE_MARGIN on Z exercises the
margin call's choice, which locks BANK_C's right in place, creates the
allocation and satisfies the margin call; the Z readiness and the snapshot's
margin calls and allocations. The ledger must have been bootstrapped with the
b1-xyz topology (ids.json); the adapter refuses to start on another one.

    .venv/bin/python -I adapter/server.py --platform X

Environment (scripts/adapter.sh provides it through scripts/env.sh):
  CANTON_STATE_DIR     ids.json from the Canton bootstrap; adapter-<P>.ready;
                       requests/<P>/ (rejection records)
  POC_BANK_A_TOKEN, POC_BANK_B_TOKEN, POC_BANK_C_TOKEN, POC_OBSERVER_TOKEN
                       local credentials; their values are never logged
  CANTON_EVIDENCE_DIR  root of <run_id>/events.jsonl (default: logs/)
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
    web,
)
from jsonschema import Draft202012Validator

IMPL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = IMPL_DIR.parent.parent / "shared/schemas"
DAR = IMPL_DIR / "daml/collateral/.daml/dist/collateral-0.4.0.dar"
PACKAGE = "collateral"
# The ledger topology this DAR and PLATFORMS belong to (ids.json, written by
# canton/bootstrap-b1-xy.canton). The X→Y evidence ledgers are "b1-xy".
TOPOLOGY = "b1-xyz"
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
# interface §6.2: the response wait is one experiment condition for every
# model. 4 s keeps the answer inside the common runner's 5 s HTTP timeout.
RESPONSE_WAIT_ENV = "CANTON_RESPONSE_WAIT"
RESPONSE_WAIT_DEFAULT = 4.0
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
MODEL = "B1"
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


class Active(NamedTuple):
    """An active contract of the run: Canton contract id and create argument."""

    cid: str
    arg: dict


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
    hence O_APPEND under flock. The time, the sequence number and the write
    happen without an await in between, so on the single event-loop thread no
    other request can take a later time with an earlier sequence. The line is
    written (not fsynced) before the HTTP response is sent.
    """

    def __init__(self, root: Path, platform: str, boot_id: str):
        self.root, self.platform = root, platform
        self.producer = f"canton-adapter-{platform}"
        self.clock = f"canton-adapter-{platform}-{boot_id}"
        self.sequence = 0

    def write(self, event_type: str, run_id: str, test_id: str, **fields) -> None:
        event: dict[str, object] = {key: None for key in EVENT_NULLABLE}
        event.update(fields)
        event.update(
            event_id="event-" + uuid.uuid4().hex,
            event_type=event_type,
            model=MODEL,
            test_id=test_id,
            run_id=run_id,
            platform=self.platform,
            producer_id=self.producer,
            clock_id=self.clock,
            sequence=self.sequence,
            mono_ns=str(time.monotonic_ns()),
            utc_time=datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
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
        self.sequence += 1


class Adapter:
    def __init__(self, platform: str, port: int):
        self.platform, self.port = platform, port
        self.spec = PLATFORMS[platform]
        self.commands = self.spec["commands"]
        self.actors = set(self.commands.values())
        self.boot_id = secrets.token_hex(6)
        state_dir = os.environ.get("CANTON_STATE_DIR")
        if not state_dir:
            raise SystemExit("CANTON_STATE_DIR not set (source scripts/env.sh)")
        self.state_dir = Path(state_dir)
        ids = json.loads((self.state_dir / "ids.json").read_text())
        # Checked before anything is uploaded: the X→Y evidence ledgers keep
        # the package version their evidence was produced with (README §4).
        if ids.get("topology") != TOPOLOGY:
            raise SystemExit(
                f"{self.state_dir} holds a {ids.get('topology')} ledger, not "
                f"{TOPOLOGY}: start it from the commit that bootstrapped it (README §4)"
            )
        self.ledger_url = ids["json_api"][self.spec["participant"]]
        self.parties = ids["parties"]
        self.names = {party: name for name, party in self.parties.items()}
        # interface §13: a response counter per platform and run (this process).
        self.snapshot_seq: dict[str, int] = {}
        self.synchronizer = ids["synchronizers"]["S1"]
        # The vetting list names participants without the PAR:: prefix of ids.json.
        self.participants = {
            name: uid.removeprefix("PAR::") for name, uid in ids["participants"].items()
        }
        self.package_id = dar_main_package(DAR)
        self.tokens = self.load_tokens()
        evidence = os.environ.get("CANTON_EVIDENCE_DIR") or str(IMPL_DIR / "logs")
        self.events = EventLog(Path(evidence), platform, self.boot_id)
        schema = json.loads((SCHEMA_DIR / "command.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        self.command_schema = Draft202012Validator(schema)
        self.ready_file = self.state_dir / f"adapter-{platform}.ready"
        self.response_wait = response_wait()
        # Request records by (run_id, actor, request_id) (interface §6.3).
        # Rejections are kept on disk until the run's results are checked;
        # the task deciding a key, the submissions waiting for the ledger and
        # the submissions whose answer was lost live in this process.
        self.requests_dir = self.state_dir / "requests" / platform
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.rejections: dict[str, dict[tuple[str, str], dict]] = {}
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

    async def ledger(self, method: str, path: str, **kwargs) -> tuple[int, object]:
        try:
            async with self.session.request(
                method, self.ledger_url + path, **kwargs
            ) as reply:
                raw = await reply.read()
        except ClientConnectorError as error:
            raise LedgerNotReached(f"{method} {path}: {error!r}") from error
        except (ClientError, asyncio.TimeoutError) as error:
            # Sent, or possibly sent: the participant may have acted on it.
            raise LedgerUnreachable(f"{method} {path}: {error!r}") from error
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = raw[:500].decode(errors="replace")
        return reply.status, body

    def party_filter(self) -> dict:
        """Every contract a party hosted on this platform's participant sees."""
        return {
            "filtersByParty": {
                self.parties[name]: {"cumulative": WILDCARD}
                for name in self.spec["hosted"]
            },
            "verbose": False,
        }

    async def active_contracts(self, run_id: str) -> dict[str, list[Active]]:
        """The run's active business contracts on this participant, by template.

        One snapshot of the active contract set at a single offset: the first
        page fixes the offset (ledger end), and its page token carries it to the
        following pages. A token is only valid with the query that produced it,
        so the later requests repeat that query and add the token.
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
        while True:
            status, page = await self.ledger(
                "POST", "/v2/state/active-contracts-page", json=query
            )
            if status != 200 or not isinstance(page, dict):
                raise LedgerUnreachable(f"active contracts: {status} {page}")
            for entry in page.get("activeContracts", []):
                active = entry.get("contractEntry", {}).get("JsActiveContract")
                if active is None:  # one synchronizer: no reassignment entries
                    raise LedgerUnreachable(f"unexpected contract entry {list(entry)}")
                event = active["createdEvent"]
                if event.get("packageName") != PACKAGE:
                    continue
                entity = event["templateId"].split(":", 1)[1]
                argument = event["createArgument"]
                if (
                    entity in found
                    and argument.get("runId") == run_id
                    and event["contractId"] not in seen
                ):
                    seen.add(event["contractId"])
                    found[entity].append(Active(event["contractId"], argument))
            token = page.get("nextPageToken")
            if not token:
                return found
            query["pageToken"] = token

    async def prepared_run(self, run_id: str) -> dict[str, list[Active]]:
        """The run's active contracts, or the refusal when they cannot be read or
        the run was not prepared on this platform (README §3)."""
        try:
            found = await self.active_contracts(run_id)
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

    async def prepare_ledger(self) -> list[float]:
        """Upload and vet the DAR, wait until every participant has vetted it on S1,
        then run the warm-up transactions; returns their ms."""
        status, body = await self.ledger(
            "POST",
            "/v2/dars",
            params={"vetAllPackages": "true"},
            data=DAR.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
        )
        if status != 200:
            raise SystemExit(
                f"DAR upload to {self.spec['participant']} failed: {status} {body}"
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
            status, body = await self.ledger(
                "POST",
                "/v2/commands/submit-and-wait-for-transaction",
                json={
                    "commands": {
                        "commands": [command],
                        "commandId": f"warmup-{self.platform}-{self.boot_id}-{i}",
                        "userId": f"adapter-{self.platform}",
                        "actAs": [party],
                    }
                },
            )
            if status != 200:
                raise SystemExit(f"warm-up transaction {i} refused: {status} {body}")
            timings.append(round((time.monotonic() - started) * 1000, 1))
        return timings

    async def wait_vetted(self) -> None:
        """A transaction is refused unless every participant hosting one of its
        informees has vetted the package. BANK_B is on P_X and P_Y, BANK_C on
        P_Y and P_Z, and each adapter uploads only to its own participant, so
        the adapters wait here for each other's upload instead of racing."""
        query = {
            "packageMetadataFilter": {"packageIds": [self.package_id]},
            "topologyStateFilter": {"synchronizerIds": [self.synchronizer]},
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
                entry["participantId"]
                for entry in body.get("vettedPackages", [])
                if any(
                    pkg.get("packageId") == self.package_id
                    for pkg in entry.get("packages", [])
                )
            }
            missing = sorted(
                name for name, uid in self.participants.items() if uid not in vetted
            )
            if not missing:
                return
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"package {self.package_id[:12]} not vetted on S1 by {missing} "
                    f"after {VETTING_TIMEOUT}s (are the other adapters starting?)"
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

    def rejections_of(self, run_id: str) -> dict[tuple[str, str], dict]:
        """The run's rejected requests on this platform, read from disk once."""
        table = self.rejections.get(run_id)
        if table is None:
            table = {}
            path = self.requests_dir / f"{run_id}.jsonl"
            if path.exists():
                data = path.read_bytes()
                end = data.rfind(b"\n") + 1
                if end < len(data):
                    # A record cut short (the adapter stopped while writing it)
                    # was never answered, since reject answers only after the
                    # whole line is synced: it is dropped and its request is
                    # decided again when it is resent.
                    log.warning("%s: dropping a rejection record cut short", path)
                    os.truncate(path, end)
                    data = data[:end]
                for line in data.decode().splitlines():
                    record = json.loads(line)
                    if record.get("run_id") == run_id:
                        table[(record["actor"], record["request_id"])] = record
            self.rejections[run_id] = table
        return table

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
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # Loaded first: a line an earlier process cut short is removed before
        # this one is appended after it.
        table = self.rejections_of(run_id)
        with open(self.requests_dir / f"{run_id}.jsonl", "a") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
        table[(actor, request_id)] = record
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
        (Canton locks the right a transaction uses while it is confirmed), and
        requests answered UNKNOWN that the ledger may still commit: the state
        shows no trade of theirs and they still pass the state check."""
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
        (interface §6.3): a rejection kept on disk, the trade contract a
        commit created, or a submission whose answer was lost."""
        run_id, role, request_id = key
        kind = command["command_type"]
        content = canonical(command)
        rejection = self.rejections_of(run_id).get((role, request_id))
        if rejection is not None:
            if rejection["content"] != content:
                raise self.conflict(request_id)
            return 200, self.rejected_of(rejection, replayed=True)
        found = await self.prepared_run(run_id)
        record = self.record_of(found, role, request_id)
        if record is not None:
            if not record_matches(record, command):
                raise self.conflict(request_id)
            return 200, self.result_of(run_id, record, replayed=True)
        earlier = self.unknown.get(key)
        if earlier is not None and canonical(earlier) != content:
            raise self.conflict(request_id)
        verdict = self.judge(found, command, role)
        if verdict.absent:
            # The participant may not have applied the preceding trade yet.
            reads, deadline = 1, time.monotonic() + ABSENT_WAIT
            while verdict.absent and time.monotonic() < deadline:
                await asyncio.sleep(LOCAL_POLL)
                found = await self.prepared_run(run_id)
                reads += 1
                record = self.record_of(found, role, request_id)
                if record is not None:
                    if not record_matches(record, command):
                        raise self.conflict(request_id)
                    return 200, self.result_of(run_id, record, replayed=True)
                verdict = self.judge(found, command, role)
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
                    status, reply = await self.ledger("POST", SUBMIT_PATH, json=body)
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
                    key, command, others, replayed, judge=not confirmed
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
                found = await self.prepared_run(run_id)
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
            if await self.locally_applied(run_id, locked["contractId"]):
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
            if await self.locally_applied(run_id, successor["contractId"]):
                self.events.write(
                    "LOCAL_APPLIED",
                    run_id,
                    test_id,
                    contract_id=successor["contractId"],
                    **effect,
                    **fields,
                )

    async def locally_applied(self, run_id: str, right_cid: str) -> bool:
        """Whether this participant's active contracts show the new right
        (measurement.md §4.1 LOCAL_APPLIED). The transaction is committed either
        way, so a failed check only leaves the event out; it is not an error."""
        deadline = time.monotonic() + LOCAL_WAIT
        while True:
            try:
                found = await self.active_contracts(run_id)
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
        # Read only: the record kept for the key, else the task still deciding it.
        # A task that ends while it is read has written its record by then:
        # read once more.
        for _ in range(2):
            work = self.tasks.get(key)
            busy = work is not None and not work.task.done()
            rejection = self.rejections_of(run_id).get((role, request_id))
            if rejection is not None:
                return web.json_response(self.rejected_of(rejection, replayed=False))
            found = await self.prepared_run(run_id)
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
        found = await self.prepared_run(run_id)
        bundle_id = query["bundle_id"]
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
            c.arg
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
        reasons = []
        collateral = agreements[0]["collateral"]
        if not (collateral["provided"] and collateral["conditionsMet"]):
            reasons.append("PRECONDITION_NOT_MET")  # acceptance.md AC-04
        held = [
            c.arg
            for c in found[RIGHT]
            if c.arg["bundle"]["bundleId"] == bundle_id
            and c.arg["holder"] == self.parties[role]
        ]
        available = [a for a in held if a["status"] == "AVAILABLE"]
        if len(available) > 1:
            raise preparation_conflict(f"{role} holds more than one available right")
        if not held:
            # The preceding trade has not given the submitter a right yet.
            reasons.append("PRECONDITION_NOT_MET")
        elif not available:
            reasons.append("RIGHT_UNAVAILABLE")
        elif kind not in available[0]["allowedCommands"]:
            reasons.append("USE_NOT_PERMITTED")
        return reasons, available

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
            c.arg
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
        call, user = calls[0], self.parties[role]
        rights = [c.arg for c in found[RIGHT] if c.arg["bundle"]["bundleId"] == bundle_id]
        reasons = []
        if not rights:
            reasons.append("PRECONDITION_NOT_MET")  # the loan has not reached Z
        if any(a["status"] == "LOCKED" for a in rights):
            reasons.append("RIGHT_UNAVAILABLE")  # already allocated
        if call["status"] != "OPEN" or call["provider"] != user:
            reasons.append("PRECONDITION_NOT_MET")
        if not (call["wholeBundleAccepted"] and call["eligibilityPassed"]):
            reasons.append("COLLATERAL_NOT_ELIGIBLE")
        held = [a for a in rights if a["holder"] == user]
        available = [a for a in held if a["status"] == "AVAILABLE"]
        if len(available) > 1:
            raise preparation_conflict(f"{role} holds more than one available right")
        if not held:
            reasons.append("PRECONDITION_NOT_MET")
        elif not available:
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
        found = await self.prepared_run(run_id)
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
        self.session = ClientSession(timeout=LEDGER_TIMEOUT)
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
