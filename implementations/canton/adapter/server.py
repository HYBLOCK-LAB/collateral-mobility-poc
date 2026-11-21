"""Platform adapter of the Canton B1 model (implementations/canton/README.md §2).

One process per platform: X (Repo, on participant P_X) and Y (securities
lending, on P_Y). It serves the common HTTP interface (spec/interface.md §4)
and reaches its participant through the JSON Ledger API v2 on localhost.

Stage 1: start-up (DAR upload, warm-up transaction), Bearer authentication,
request validation against shared/schemas, platform permissions, the error
responses of interface §9 and the implementation event log
(tests/conformance/README.md §14).

Stage 2 (X→Y normal path, README §3): CONFIRM_REPO on X and CONFIRM_LOAN on
Y exercise the pre-agreement's choice on the ledger, the result query, the Y
readiness and the snapshot. Every read takes the run's active contracts from
the participant at that moment; the adapter keeps no business state of its
own. Requests outside the normal path end in a TEMPORARY 503 INTERNAL_ERROR
until stage 3 implements them.

    .venv/bin/python -I adapter/server.py --platform X

Environment (scripts/adapter.sh provides it through scripts/env.sh):
  CANTON_STATE_DIR     ids.json from the Canton bootstrap; adapter-<P>.ready
  POC_BANK_A_TOKEN, POC_BANK_B_TOKEN, POC_BANK_C_TOKEN, POC_OBSERVER_TOKEN
                       local credentials; their values are never logged
  CANTON_EVIDENCE_DIR  root of <run_id>/events.jsonl (default: logs/)
"""

import argparse
import asyncio
import fcntl
import hmac
import json
import logging
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

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from jsonschema import Draft202012Validator

IMPL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = IMPL_DIR.parent.parent / "shared/schemas"
DAR = IMPL_DIR / "daml/collateral/.daml/dist/collateral-0.2.0.dar"
PACKAGE = "collateral"
WARMUP_TEMPLATE = f"#{PACKAGE}:Collateral.Warmup:Warmup"
# Business templates by module:entity (README §3). The ledger reports a
# contract's template as <package id>:<module>:<entity>.
RIGHT = "Collateral.Right:UseRight"
REPO_AGREEMENT = "Collateral.Agreement:RepoAgreement"
LOAN_AGREEMENT = "Collateral.Agreement:LoanAgreement"
TRADE = "Collateral.Record:TradeContract"
OBLIGATION = "Collateral.Record:Obligation"
WARMUP_COUNT = 2
VETTING_TIMEOUT = 60  # seconds for every participant's vetting to reach S1
LEDGER_TIMEOUT = ClientTimeout(total=60)
ACS_PAGE_SIZE = 500
# How long the confirming adapter looks for the new right among its
# participant's active contracts before it gives up on LOCAL_APPLIED.
LOCAL_WAIT = 5.0
LOCAL_POLL = 0.05
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
}


class Confirmation(NamedTuple):
    agreement: str  # template of the pre-agreement the trade is confirmed under
    choice: str
    record_key: str  # payload key naming the new trade
    record_argument: str  # the choice argument that carries it
    record_type: str  # common record_type and TradeContract.contractType


CONFIRMATIONS = {
    "CONFIRM_REPO": Confirmation(
        REPO_AGREEMENT, "ConfirmRepo", "repo_id", "repoId", "REPO"
    ),
    "CONFIRM_LOAN": Confirmation(
        LOAN_AGREEMENT, "ConfirmLoan", "loan_id", "loanId", "LOAN"
    ),
}
COMMAND_OF_RECORD = {c.record_type: kind for kind, c in CONFIRMATIONS.items()}
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
}
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


def outside_normal_path(message: str) -> Refusal:
    # TEMPORARY (stage 3): rejections, result states other than COMMITTED,
    # idempotent replay, contention and recovery are not implemented yet. Until
    # then such a request is answered as "result cannot be determined now"
    # instead of with a business error code the adapter has not verified.
    return Refusal("INTERNAL_ERROR", message + " (outside the stage 2 normal path)")


class LedgerUnreachable(Exception):
    """The participant did not answer, or answered with an error, so its state
    is unknown. Never read as "the contract does not exist" (interface §7)."""


class Active(NamedTuple):
    """An active contract of the run: Canton contract id and create argument."""

    cid: str
    arg: dict


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

    Lines go to <root>/<run_id>/events.jsonl, which the X and Y processes share,
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
        except (ClientError, asyncio.TimeoutError) as error:
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
            for entity in (RIGHT, REPO_AGREEMENT, LOAN_AGREEMENT, TRADE, OBLIGATION)
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
        informees has vetted the package. BANK_B is on P_X and P_Y, and each
        adapter uploads only to its own participant, so the two adapters wait
        here for each other's upload instead of racing."""
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
                    f"after {VETTING_TIMEOUT}s (is the other adapter starting?)"
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

    def result_of(self, run_id: str, trade: dict) -> dict:
        """The COMMITTED result (interface §6.1) of the request that confirmed the
        trade, from the trade contract the confirmation created."""
        return {
            "run_id": run_id,
            "request_id": trade["requestId"],
            "actor": self.name_of(trade["actor"]),
            "command_type": COMMAND_OF_RECORD[trade["contractType"]],
            "outcome": "COMMITTED",
            "replayed": False,
            "effect": {
                "bundle_id": trade["bundleId"],
                "version_before": int(trade["versionBefore"]),
                "version_after": int(trade["versionAfter"]),
                "record_type": trade["contractType"],
                "record_id": trade["tradeId"],
            },
            "error": None,
        }

    def snapshot_of(self, run_id: str, found: dict[str, list[Active]]) -> dict:
        """Normalised records this participant sees (interface §8, snapshot
        schema). The pre-agreements are preparation data, not records."""
        rights = sorted(
            (self.right_row(c.arg) for c in found[RIGHT]),
            key=lambda row: (row["bundle_id"], row["created_version"]),
        )
        bundles: dict[str, dict] = {}
        for contract in found[RIGHT]:
            arg = contract.arg
            # A consumed right means the transaction that consumed it raised the
            # bundle to the next version, even when the successor is not visible.
            version = int(arg["createdVersion"]) + (
                1 if arg["status"] == "CONSUMED" else 0
            )
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
            # Z (margin calls, CCP allocation) is not part of B1 X→Y.
            "margin_calls": [],
            "allocations": [],
            # TEMPORARY (stage 3): a command waiting for the ledger's answer is
            # not reported as PROCESSING yet (interface §8.1).
            "pending_operations": [],
        }

    async def confirm(
        self, run_id: str, test_id: str, command: dict, fields: dict
    ) -> dict:
        """Exercise the pre-agreement's confirmation choice on the ledger and log
        what the committed transaction actually created (README §3, §4)."""
        kind, role = command["command_type"], fields["actor"]
        confirmation = CONFIRMATIONS[kind]
        payload, bundle_id = command["payload"], command["bundle_id"]
        record_id = payload[confirmation.record_key]
        found = await self.prepared_run(run_id)
        agreements = [
            c
            for c in found[confirmation.agreement]
            if c.arg["agreementId"] == payload["agreement_id"]
            and c.arg["bundleId"] == bundle_id
        ]
        if len(agreements) != 1:
            raise outside_normal_path("no pre-agreement for the bundle")
        user = self.parties[role]
        rights = [
            c
            for c in found[RIGHT]
            if c.arg["bundle"]["bundleId"] == bundle_id
            and c.arg["holder"] == user
            and c.arg["status"] == "AVAILABLE"
        ]
        if len(rights) != 1:
            raise outside_normal_path(f"{role} holds no available right of the bundle")
        # Version, permitted use, run and bundle are checked by the choice itself
        # (Collateral.Agreement.useRight), inside the transaction.
        exercise = {
            "ExerciseCommand": {
                "templateId": f"#{PACKAGE}:{confirmation.agreement}",
                "contractId": agreements[0].cid,
                "choice": confirmation.choice,
                "choiceArgument": {
                    "requestId": command["request_id"],
                    confirmation.record_argument: record_id,
                    "expectedVersion": str(command["expected_version"]),
                    "rightCid": rights[0].cid,
                },
            }
        }
        body = {
            "commands": {
                "commands": [exercise],
                # TEMPORARY (stage 3): a fresh id per attempt; deduplicating a
                # resubmitted request is part of the idempotency design.
                "commandId": f"confirm-{uuid.uuid4().hex}",
                "userId": f"adapter-{self.platform}",
                "actAs": [user],
            },
            "transactionFormat": {
                "eventFormat": self.party_filter(),
                "transactionShape": "TRANSACTION_SHAPE_ACS_DELTA",
            },
        }
        try:
            status, reply = await self.ledger(
                "POST", "/v2/commands/submit-and-wait-for-transaction", json=body
            )
        except LedgerUnreachable as error:
            # The transaction may still commit: the outcome is unknown, not failed.
            log.warning(
                "%s %s: no ledger answer: %s", kind, command["request_id"], error
            )
            raise outside_normal_path(
                "the ledger did not answer the submission"
            ) from error
        if status != 200 or not isinstance(reply, dict):
            log.warning(
                "%s %s not confirmed: %s %s",
                kind,
                command["request_id"],
                status,
                str(reply)[:2000],
            )
            raise outside_normal_path("the ledger did not confirm the transaction")
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
            "%s %s committed at offset %s",
            kind,
            command["request_id"],
            transaction["offset"],
        )
        trade = one(TRADE, tradeId=record_id)
        if trade is None:
            raise ValueError("the committed transaction has no single trade contract")
        # The trade contract proves the commit: log it before anything else of
        # the reply is read, so a later problem cannot drop the event.
        version_after = int(trade["createArgument"]["versionAfter"])
        effect = {
            "record_id": record_id,
            "version_before": int(trade["createArgument"]["versionBefore"]),
            "version_after": version_after,
        }
        self.events.write(
            "INTERNAL_COMMITTED",
            run_id,
            test_id,
            outcome="COMMITTED",
            contract_id=trade["contractId"],
            **effect,
            **fields,
        )
        # A right missing from the reply leaves its event out; it is not invented.
        consumed = one(RIGHT, rightId=rights[0].arg["rightId"], status="CONSUMED")
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
        return self.result_of(run_id, trade["createArgument"])

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
            result = await self.confirm(run_id, test_id, command, fields)
        except Exception as error:
            # Every COMMAND_RECEIVED gets its FINISHED, with the code the errors
            # middleware will answer (INTERNAL_ERROR for anything unexpected).
            if attributable:
                code = error.code if isinstance(error, Refusal) else "INTERNAL_ERROR"
                self.events.write(
                    "COMMAND_FINISHED", run_id, test_id, error_code=code, **fields
                )
            raise
        effect = result["effect"]
        self.events.write(
            "COMMAND_FINISHED",
            run_id,
            test_id,
            outcome=result["outcome"],
            record_id=effect["record_id"],
            version_before=effect["version_before"],
            version_after=effect["version_after"],
            **fields,
        )
        return web.json_response(result)

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
        found = await self.prepared_run(run_id)
        # Requests are kept per (run, authenticated actor, request id) on the
        # platform they were submitted to (interface §6.3).
        trades = [
            c.arg
            for c in found[TRADE]
            if c.arg["requestId"] == request_id
            and self.name_of(c.arg["actor"]) == role
            and COMMAND_OF_RECORD[c.arg["contractType"]] in self.commands
        ]
        if not trades:
            # TEMPORARY (stage 3): only committed requests leave a record.
            raise Refusal(
                "NOT_FOUND",
                f"no committed request {request_id} of {role} on platform {self.platform}",
            )
        if len(trades) > 1:
            raise outside_normal_path("the request confirmed more than one trade")
        return web.json_response(self.result_of(run_id, trades[0]))

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
        agreement_id = query[self.spec["readiness"]]
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
            raise outside_normal_path("more than one pre-agreement for the bundle")
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
            raise outside_normal_path(f"{role} holds more than one available right")
        if not held:
            # The preceding trade has not given the submitter a right yet.
            reasons.append("PRECONDITION_NOT_MET")
        elif not available:
            reasons.append("RIGHT_UNAVAILABLE")
        elif kind not in available[0]["allowedCommands"]:
            reasons.append("USE_NOT_PERMITTED")
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
        body = {
            "run_id": request.get("run_id"),
            "request_id": request.get("request_id"),
            "error": {"code": code, "message": message},
        }
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
