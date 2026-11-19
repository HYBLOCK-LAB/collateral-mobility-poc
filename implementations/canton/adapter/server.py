"""Platform adapter of the Canton B1 model (implementations/canton/README.md §2).

One process per platform: X (Repo, on participant P_X) and Y (securities
lending, on P_Y). It serves the common HTTP interface (spec/interface.md §4)
and reaches its participant through the JSON Ledger API v2 on localhost.

Stage 1 scope: start-up (DAR upload, warm-up transaction), Bearer
authentication, request validation against shared/schemas, platform
permissions, the error responses of interface §9 and the implementation event
log (tests/conformance/README.md §14). Business processing is stage 2. Until
then no run is loaded on the ledger, so every authorised request for a run
ends in 404 NOT_FOUND; the adapter never answers on the ledger's behalf.

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

from aiohttp import ClientSession, ClientTimeout, web
from jsonschema import Draft202012Validator

IMPL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DIR = IMPL_DIR.parent.parent / "shared/schemas"
DAR = IMPL_DIR / "daml/collateral/.daml/dist/collateral-0.1.0.dar"
WARMUP_TEMPLATE = "#collateral:Collateral.Warmup:Warmup"
WARMUP_COUNT = 2
VETTING_TIMEOUT = 60  # seconds for every participant's vetting to reach S1
LEDGER_TIMEOUT = ClientTimeout(total=60)
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
# submit each (interface §4, business.md §2), and the readiness query key
# (interface §7; readiness is provided on Y and Z only).
PLATFORMS = {
    "X": {
        "participant": "P_X",
        "port": 8201,
        "warmup": "BANK_A",
        "commands": {"CONFIRM_REPO": "BANK_A"},
        "readiness": None,
    },
    "Y": {
        "participant": "P_Y",
        "port": 8202,
        "warmup": "BANK_B",
        "commands": {"CONFIRM_LOAN": "BANK_B"},
        "readiness": "agreement_id",
    },
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
        async with self.session.request(
            method, self.ledger_url + path, **kwargs
        ) as reply:
            raw = await reply.read()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = raw[:500].decode(errors="replace")
        return reply.status, body

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

    def unknown_run(self, run_id: str) -> Refusal:
        # Stage 1: neither the loader nor the business templates exist yet, so no
        # run is on the ledger. Stage 2 replaces this with the ledger lookup.
        return Refusal(
            "NOT_FOUND", f"run {run_id} is not prepared on platform {self.platform}"
        )

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
            raise self.unknown_run(run_id)
        except Exception as error:
            # Every COMMAND_RECEIVED gets its FINISHED, with the code the errors
            # middleware will answer (INTERNAL_ERROR for anything unexpected).
            if attributable:
                code = error.code if isinstance(error, Refusal) else "INTERNAL_ERROR"
                self.events.write(
                    "COMMAND_FINISHED", run_id, test_id, error_code=code, **fields
                )
            raise

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
        raise self.unknown_run(run_id)

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
        raise self.unknown_run(run_id)

    async def get_snapshot(self, request: web.Request) -> web.Response:
        run_id = self.identify(request)
        role = self.authenticate(request)
        run_id = self.require_run_id(run_id)
        if role != OBSERVER:
            raise Refusal(
                "FORBIDDEN", "only the experiment observer may read the snapshot"
            )
        raise self.unknown_run(run_id)

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
