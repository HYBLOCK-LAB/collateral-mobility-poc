import asyncio
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import storage as db
from workflow import LOAN, REPO, Workflow, error, keys_for

platform = os.environ["PLATFORM"]
database = os.environ["DB_NAME"]
username = os.environ["DB_USER"]
engine = create_engine(
    URL.create(
        "postgresql+psycopg",
        username=username,
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "5432")),
        database=database,
    ),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 3},
)

logger = logging.getLogger(__name__)
tokens = {
    principal: os.environ[variable] for principal, variable in {
        "bank-a": "POC_BANK_A_TOKEN", "bank-b": "POC_BANK_B_TOKEN",
        "bank-c": "POC_BANK_C_TOKEN", "experiment-observer": "POC_OBSERVER_TOKEN",
    }.items()
}
internal_token = os.environ["MODEL_A_INTERNAL_TOKEN"]
if not all(tokens.values()) or not internal_token or len(set(tokens.values()) | {internal_token}) != 5:
    raise ValueError("Configure five distinct nonempty API tokens")
workflow = Workflow(engine, platform, os.getenv("MODEL_A_X_URL"), internal_token)
schema_dir = Path(os.getenv("MODEL_A_SCHEMA_DIR", Path(__file__).parent / "schemas"))
command_schema = Draft202012Validator(json.loads((schema_dir / "command.schema.json").read_text(encoding="utf-8")))
command_type, submitting_actor = (REPO, "BANK_A") if platform == "X" else (LOAN, "BANK_B")
identifier = re.compile(r"[A-Za-z0-9._:-]{1,128}")


async def recovery_loop(stopped):
    while not stopped.is_set():
        try:
            await asyncio.to_thread(workflow.recover)
        except SQLAlchemyError:
            logger.warning("Recovery could not query pending requests")
        try:
            await asyncio.wait_for(stopped.wait(), timeout=1)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(db.metadata.create_all, engine)
    stopped = asyncio.Event()
    recovery = asyncio.create_task(recovery_loop(stopped))
    try:
        yield
    finally:
        stopped.set()
        await recovery
        workflow.peer.close()
        engine.dispose()


app = FastAPI(title=f"Model A / {platform}", lifespan=lifespan)


class ApiError(Exception):
    def __init__(self, code, status, request_id=None):
        self.code, self.status, self.request_id = code, status, request_id


def valid_id(value):
    return isinstance(value, str) and identifier.fullmatch(value) is not None


def error_response(request, exc):
    run_id = request.path_params.get("run_id")
    request_id = exc.request_id or request.path_params.get("request_id")
    return JSONResponse({
        "run_id": run_id if valid_id(run_id) else None,
        "request_id": request_id if valid_id(request_id) else None,
        "error": error(exc.code),
    }, status_code=exc.status)


@app.exception_handler(ApiError)
async def handle_api_error(request, exc):
    return error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request, exc):
    request_id = exc.body.get("request_id") if isinstance(exc.body, dict) else None
    return error_response(request, ApiError("INVALID_REQUEST", 400, request_id))


@app.exception_handler(SQLAlchemyError)
async def handle_db_error(request, _exc):
    logger.warning("Could not complete a database operation")
    return error_response(request, ApiError("TEMPORARILY_UNAVAILABLE", 503,
                                           getattr(request.state, "request_id", None)))


def bearer(request):
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    return credential.encode("utf-8") if scheme.lower() == "bearer" else b""


def authorize(connection, request, run_id, request_id=None, actor=None, observer=False):
    provided = bearer(request)
    principal_id = next((name for name, token in tokens.items()
                         if hmac.compare_digest(provided, token.encode("utf-8"))), None)
    if principal_id is None:
        raise ApiError("UNAUTHENTICATED", 401, request_id)
    if not db.require_run(connection, run_id):
        raise ApiError("NOT_FOUND", 404, request_id)
    principal = db.one(connection, db.principals, run_id=run_id, principal_id=principal_id)
    if principal is None or principal["observer"] != observer or (actor and principal["actor"] != actor):
        raise ApiError("FORBIDDEN", 403, request_id)
    return principal["actor"]


def require_internal(request, run_id):
    if not hmac.compare_digest(bearer(request), internal_token.encode("utf-8")):
        raise ApiError("UNAUTHENTICATED", 401)
    if not valid_id(run_id):
        raise ApiError("INVALID_REQUEST", 400)


def validate_command(body, supported_type):
    request_id = body.get("request_id") if isinstance(body, dict) else None
    if (not command_schema.is_valid(body) or body["command_type"] != supported_type
            or type(body["expected_version"]) is not int):
        raise ApiError("INVALID_REQUEST", 400, request_id)
    record_id = body["payload"]["repo_id" if supported_type == REPO else "loan_id"]
    longest_suffix = ":repurchase-payment" if supported_type == REPO else ":collateral-return"
    if not valid_id(db.right_id(body["bundle_id"], 2)) or not valid_id(record_id + longest_suffix):
        raise ApiError("INVALID_REQUEST", 400, request_id)
    return record_id


def command_context(request, run_id, body, record_id, actor):
    test_id = request.headers.get("X-Test-ID", "MANUAL")
    attempt_id = request.headers.get("X-Attempt-ID", uuid4().hex)
    if not valid_id(test_id) or not valid_id(attempt_id):
        raise ApiError("INVALID_REQUEST", 400, body["request_id"])
    return {
        "run_id": run_id, "request_id": body["request_id"], "bundle_id": body["bundle_id"],
        "record_id": record_id, "command_type": body["command_type"], "actor": actor,
        "test_id": test_id, "attempt_id": attempt_id,
    }


@app.post("/runs/{run_id}/commands")
def submit(request: Request, run_id: str, body: Any = Body(None)):
    record_id = validate_command(body, command_type)
    request_id = body["request_id"]
    request.state.request_id = request_id
    if not valid_id(run_id):
        raise ApiError("INVALID_REQUEST", 400, request_id)
    with engine.begin() as connection:
        actor = authorize(connection, request, run_id, request_id, actor=submitting_actor)
        context = command_context(request, run_id, body, record_id, actor)
        workflow.log.write(connection, context, "COMMAND_RECEIVED")
    reply, conflict = workflow.begin(context, body)
    if conflict:
        with engine.begin() as connection:
            workflow.log.write(connection, context, "COMMAND_FINISHED", error_code=conflict)
        raise ApiError(conflict, 409, request_id)
    if reply["outcome"] == "PENDING":
        replayed = reply["replayed"]
        reply = dict(workflow.advance(keys_for(context)), replayed=replayed)
    if reply["outcome"] == "COMMITTED":
        workflow.publish_commit_observation(keys_for(context))
    with engine.begin() as connection:
        workflow.log.write(connection, context, "COMMAND_FINISHED", outcome=reply["outcome"],
                           error_code=reply["error"]["code"] if reply["error"] else None)
    return JSONResponse(reply, status_code=202 if reply["outcome"] == "PENDING" else 200)


@app.get("/runs/{run_id}/commands/{request_id}")
def lookup(request: Request, run_id: str, request_id: str):
    if not valid_id(run_id) or not valid_id(request_id):
        raise ApiError("INVALID_REQUEST", 400)
    with engine.connect() as connection:
        actor = authorize(connection, request, run_id, request_id)
        row = db.one(connection, db.requests, run_id=run_id, actor=actor, request_id=request_id)
        if row is None:
            exists = db.rows(connection, db.requests, run_id=run_id, request_id=request_id)
            raise ApiError("FORBIDDEN" if exists else "NOT_FOUND", 403 if exists else 404, request_id)
    reply = row["result"]
    return JSONResponse(reply, status_code=202 if reply["outcome"] == "PENDING" else 200)


def consistent_read():
    return engine.connect().execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)


@app.get("/runs/{run_id}/snapshot")
def snapshot(request: Request, run_id: str):
    if not valid_id(run_id):
        raise ApiError("INVALID_REQUEST", 400)
    with consistent_read() as connection:
        authorize(connection, request, run_id, observer=True)
        contracts = db.rows(connection, db.contracts, run_id=run_id)
        agreement_ids = {row["contract_id"]: row["agreement_id"] for row in contracts}
        pending = [row for row in db.rows(connection, db.requests, run_id=run_id) if row["phase"] != "DONE"]
        pending.extend(db.rows(connection, db.transfers, run_id=run_id, state="RESERVED"))
        view = {
            "run_id": run_id, "platform": platform,
            "bundles": [db.public_bundle(row) for row in db.rows(connection, db.bundles, run_id=run_id)],
            "rights": [db.public_right(row) for row in db.rows(connection, db.rights, run_id=run_id)],
            "contracts": [{**{key: row[key] for key in ("contract_id", "agreement_id", "bundle_id", "parties")},
                           "type": row["kind"], "status": "ACTIVE"} for row in contracts],
            "obligations": [
                {**{key: row[key] for key in ("obligation_id", "debtor", "creditor", "contract_id", "target")},
                 "type": row["kind"], "status": "OUTSTANDING", "agreement_id": agreement_ids[row["contract_id"]]}
                for row in db.rows(connection, db.obligations, run_id=run_id)
            ],
            "margin_calls": [], "allocations": [],
            "pending_operations": [{"request_id": row["request_id"], "bundle_id": row["bundle_id"],
                                    "kind": "PROCESSING", "blocks_use": True} for row in pending],
        }
    with engine.begin() as connection:
        view["snapshot_seq"] = db.next_snapshot_sequence(connection, run_id)
    return view


if platform == "Y":

    @app.get("/runs/{run_id}/readiness")
    def readiness(request: Request, run_id: str, bundle_id: str, agreement_id: str, command_type: str):
        if command_type != LOAN or not all(map(valid_id, (run_id, bundle_id, agreement_id))):
            raise ApiError("INVALID_REQUEST", 400)
        with consistent_read() as connection:
            actor = authorize(connection, request, run_id, actor="BANK_B")
            local = db.one(connection, db.bundles, run_id=run_id, bundle_id=bundle_id)
            context = dict(run_id=run_id, bundle_id=bundle_id, actor=actor, command_type=LOAN)
            _, local_block = workflow.validate_agreement(
                connection, context, {"payload": {"agreement_id": agreement_id}},
            )
        if local is None:
            raise ApiError("NOT_FOUND", 404)
        try:
            response = workflow.peer.get(f"/internal/runs/{run_id}/rights/{bundle_id}")
            response.raise_for_status()
            authority = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ApiError("TEMPORARILY_UNAVAILABLE", 503) from exc
        reasons = sorted({reason for reason in (authority["block"], local_block) if reason})
        return {"run_id": run_id, "platform": "Y", "bundle_id": bundle_id, "command_type": LOAN,
                "ready": not reasons, "observed_version": max(local["version"], authority["version"] or 0),
                "blocking_reasons": reasons}


if platform == "X":

    @app.get("/internal/runs/{run_id}/rights/{bundle_id}")
    def inspect_right(request: Request, run_id: str, bundle_id: str):
        require_internal(request, run_id)
        if not valid_id(bundle_id):
            raise ApiError("INVALID_REQUEST", 400)
        with consistent_read() as connection:
            if not db.require_run(connection, run_id):
                raise ApiError("NOT_FOUND", 404)
            version, code, _ = workflow.authority(connection, run_id, bundle_id)
        return {"version": version, "block": code}

    @app.post("/internal/runs/{run_id}/transfers/{operation}")
    def transfer(request: Request, run_id: str, operation: str, body: dict):
        require_internal(request, run_id)
        phase = operation.upper()
        if phase not in ("RESERVE", "CONFIRM", "ABORT") or not valid_id(body.get("message_id")):
            raise ApiError("INVALID_REQUEST", 400)
        command = body.get("command")
        record = validate_command(command, LOAN)
        diagnostic = body.get("context", {})
        if (not isinstance(diagnostic, dict) or not valid_id(diagnostic.get("test_id"))
                or not valid_id(diagnostic.get("attempt_id"))):
            raise ApiError("INVALID_REQUEST", 400)
        context = {
            "run_id": run_id, "request_id": command["request_id"], "bundle_id": command["bundle_id"],
            "actor": "BANK_B", "command_type": LOAN, "record_id": record,
            "test_id": diagnostic["test_id"], "attempt_id": diagnostic["attempt_id"],
        }
        with engine.begin() as connection:
            if not db.require_run(connection, run_id):
                raise ApiError("NOT_FOUND", 404, command["request_id"])
            workflow.message(connection, context, "MESSAGE_RECEIVED", body["message_id"], "Y", "X", "REQUEST")
            answer = workflow.move_right(connection, context, command, phase)
        if answer is None:
            raise ApiError("REQUEST_ID_CONFLICT", 409, command["request_id"])
        message_id = uuid4().hex
        with engine.begin() as connection:
            workflow.message(connection, context, "MESSAGE_SENT", message_id, "X", "Y", "RESPONSE")
        return dict(answer, message_id=message_id)


@app.get("/health/db")
def database_health() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            actual_database, actual_user, version = connection.execute(
                text("SELECT current_database(), current_user, current_setting('server_version')")
            ).one()
    except SQLAlchemyError:
        logger.warning("Platform %s could not query its PostgreSQL database", platform)
        raise HTTPException(status_code=503, detail="Database unavailable") from None

    if (actual_database, actual_user) != (database, username):
        logger.error("Unexpected database identity for platform %s", platform)
        raise HTTPException(status_code=503, detail="Unexpected database identity")

    return {
        "status": "ok",
        "platform": platform,
        "database": actual_database,
        "postgresql_version": version,
    }
