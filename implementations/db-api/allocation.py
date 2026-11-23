"""Z allocation and the authoritative C-bank right lock on Y."""

import logging
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import storage as db
from workflow import MARGIN, Workflow, bundle_keys, keys_for, result

logger = logging.getLogger(__name__)


def valid_message_id(value):
    if not isinstance(value, str):
        return False
    try:
        return UUID(value).hex == value
    except ValueError:
        return False


def right_authority(connection, run_id, bundle_id, expected=None):
    keys = dict(run_id=run_id, bundle_id=bundle_id)
    bundle = db.one(connection, db.bundles, **keys)
    version = bundle["version"] if bundle else None
    right = db.one(connection, db.rights, **keys, created_version=2)
    if right is None:
        return version, "PRECONDITION_NOT_MET", None
    if right["status"] != "AVAILABLE":
        return version, "RIGHT_UNAVAILABLE", None
    if expected is not None and expected != version:
        return version, "VERSION_CONFLICT", None
    loan = db.one(connection, db.contracts, **keys, contract_id=right["source_record_id"], kind="LOAN")
    if version != 2 or loan is None or right["holder"] != "BANK_C":
        return version, "PRECONDITION_NOT_MET", None
    if MARGIN not in right["allowed_commands"]:
        return version, "USE_NOT_PERMITTED", None
    return version, None, right


def lock_right(connection, workflow, context, command):
    """Persist Y's decision and lock evidence before acknowledging it."""
    run_id, request_id = context["run_id"], context["request_id"]
    keys = dict(run_id=run_id, request_id=request_id)
    prior = db.one(connection, db.allocation_locks, **keys)
    if prior is not None:
        return dict(prior) if prior["command"] == command else None

    _, denial, right = right_authority(connection, run_id, context["bundle_id"], command["expected_version"])
    decision = dict(keys, bundle_id=context["bundle_id"], command=command, denial=denial,
                    source_record_id=None, message_id=None)
    if right is not None:
        source = right["source_record_id"]
        decision.update(source_record_id=source, message_id=uuid4().hex)
        db.change(connection, db.rights, dict(bundle_keys(context), created_version=2),
                  status="LOCKED", allowed_commands=[])
        workflow.log.write(
            connection, context, "RIGHT_LOCKED",
            right_id=db.right_id(context["bundle_id"], 2), holder="BANK_C",
            predecessor_right_id=db.right_id(context["bundle_id"], 1),
            source_record_id=source, right_status="LOCKED",
        )
        workflow.message(connection, context, "MESSAGE_SENT", decision["message_id"],
                         "Y", "Z", "EVENT", category="PROTOCOL")
    connection.execute(db.allocation_locks.insert().values(**decision))
    return decision


@dataclass
class ReadinessEvidence:
    in_flight: int = 0
    generated: int = 0
    queued: int = 0
    writing: int = 0
    committed: int = 0
    event_ids: list[str] = field(default_factory=list)
    failure: str | None = None
    sealed: bool = False


class ReadinessEventWriter:
    """Persist complete observation batches off the readiness request path."""

    def __init__(self, engine, log, capacity=128):
        self.engine = engine
        self.log = log
        self.pending = Queue(maxsize=capacity)
        self.stopping = Event()
        self.lock = Lock()
        self.runs = {}
        self.dropped = 0
        self.thread = Thread(target=self.run, name="z-readiness-events", daemon=True)

    def start(self):
        self.thread.start()

    def register(self, run_id):
        with self.lock:
            if run_id in self.runs or self.stopping.is_set() or not self.thread.is_alive():
                return False
            if len(self.runs) >= 256:
                old = next((key for key, state in self.runs.items()
                            if state.sealed and not (state.in_flight or state.queued or state.writing)), None)
                if old is None:
                    return False
                del self.runs[old]
            self.runs[run_id] = ReadinessEvidence()
            return True

    def begin_observation(self, run_id):
        with self.lock:
            state = self.runs.get(run_id)
            if state is not None:
                state.in_flight += 1
                if state.sealed:
                    state.failure = state.failure or "QUERY_AFTER_SEAL"

    def submit(self, run_id, observations, failure=None):
        with self.lock:
            state = self.runs.get(run_id)
            count = len(observations)
            if state is not None:
                state.in_flight -= 1
                state.failure = state.failure or failure
                state.generated += count
                if state.generated <= 4096:
                    state.event_ids.extend(e["event_id"] for e in observations)
                else:
                    state.failure = state.failure or "TRACKING_LIMIT"
            if not count:
                return
            if self.stopping.is_set():
                if state is not None:
                    state.failure = state.failure or "WRITER_STOPPED"
                return
            try:
                self.pending.put_nowait((run_id, observations))
            except Full:
                self.dropped += 1
                if state is not None:
                    state.failure = state.failure or "QUEUE_FULL"
            else:
                if state is not None:
                    state.queued += count

    def seal(self, run_id):
        with self.lock:
            state = self.runs.get(run_id)
            if state is None:
                return {"status": "INCOMPLETE", "reason": "UNKNOWN_RUN"}
            state.sealed = True
            progress = {
                "run_id": run_id, "producer_id": self.log.producer, "clock_id": self.log.clock,
                "in_flight": state.in_flight, "generated": state.generated,
                "queued": state.queued, "writing": state.writing, "committed": state.committed,
            }
            if self.stopping.is_set() or not self.thread.is_alive():
                return dict(progress, status="INCOMPLETE", reason="WRITER_STOPPED")
            if state.failure:
                return dict(progress, status="INCOMPLETE", reason=state.failure)
            if state.in_flight or state.queued or state.writing:
                return dict(progress, status="PENDING")
            if state.generated != state.committed or state.committed != len(state.event_ids):
                return dict(progress, status="INCOMPLETE", reason="COMMIT_COUNT_MISMATCH")
            return dict(progress, status="COMPLETE", event_ids=sorted(state.event_ids))

    def report_dropped(self):
        with self.lock:
            dropped, self.dropped = self.dropped, 0
        if dropped:
            logger.warning("Dropped %d Z readiness observation batches because the queue was full", dropped)

    def run(self):
        try:
            while not self.stopping.is_set() or not self.pending.empty():
                self.report_dropped()
                try:
                    run_id, observations = self.pending.get(timeout=0.1)
                except Empty:
                    continue
                count = len(observations)
                with self.lock:
                    state = self.runs.get(run_id)
                    if state is not None:
                        state.queued -= count
                        state.writing += count
                try:
                    with self.engine.begin() as connection:
                        for event in observations:
                            self.log.store(connection, event)
                except SQLAlchemyError:
                    with self.lock:
                        if state is not None:
                            state.failure = state.failure or "STORE_FAILED"
                    logger.warning("Could not store Z readiness message observations")
                else:
                    with self.lock:
                        if state is not None:
                            state.committed += count
                finally:
                    with self.lock:
                        if state is not None:
                            state.writing -= count
                    self.pending.task_done()
        finally:
            with self.lock:
                self.stopping.set()
            self.report_dropped()
            self.engine.dispose()

    def close(self):
        with self.lock:
            self.stopping.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            logger.warning("Z readiness message observations did not drain before shutdown")


class AllocationWorkflow(Workflow):
    def __init__(self, engine, y_url, internal_token, readiness_event_engine):
        super().__init__(engine, "Z", y_url or "http://api-y:8000", internal_token)
        self.readiness_writer = ReadinessEventWriter(readiness_event_engine, self.log)

    def query_y_readiness(self, context):
        run_id = context["run_id"]
        self.readiness_writer.begin_observation(run_id)
        observations = []
        failure = None

        def observe(event_type, kind, source, destination):
            observations.append(self.log.capture(
                context, event_type, message_id=uuid4().hex, message_kind=kind,
                message_category="BUSINESS_INTEGRATION", source_id=source, destination_id=destination,
            ))

        def trace(name, _info):
            if name == "http11.send_request_headers.complete" and not observations:
                observe("MESSAGE_SENT", "REQUEST", "Z", "Y")

        try:
            response = self.peer.get(
                f"/internal/runs/{context['run_id']}/ccp-rights/{context['bundle_id']}",
                extensions={"trace": trace},
            )
            # HTTPX has read the full response before returning, including on HTTP errors.
            if not observations:
                failure = "REQUEST_TRACE_MISSING"
            observe("MESSAGE_RECEIVED", "RESPONSE", "Y", "Z")
            return response
        finally:
            self.readiness_writer.submit(run_id, observations, failure)

    def blocking_reason(self, connection, run_id, bundle_id, margin_call_id):
        keys = dict(run_id=run_id, bundle_id=bundle_id)
        bundle = db.one(connection, db.bundles, **keys)
        if bundle is None:
            return None, "PRECONDITION_NOT_MET"
        if bundle["version"] == 3 or db.rows(connection, db.requests, **keys, phase="RESERVE"):
            return bundle, "RIGHT_UNAVAILABLE"
        call = db.one(connection, db.margin_calls, **keys, margin_call_id=margin_call_id)
        if call is None or call["status"] != "OPEN":
            return bundle, "PRECONDITION_NOT_MET"
        if not (call["whole_bundle_accepted"] and call["eligibility_passed"]):
            return bundle, "COLLATERAL_NOT_ELIGIBLE"
        return bundle, None

    def begin(self, context, command):
        keys = keys_for(context)
        with self.engine.begin() as connection:
            db.one(connection, db.runs, lock=True, run_id=context["run_id"])
            prior = db.one(connection, db.requests, **keys)
            if prior is not None:
                if prior["command"] != command:
                    return None, "REQUEST_ID_CONFLICT"
                return dict(prior["result"], replayed=True), None
            _, code = self.blocking_reason(connection, context["run_id"], context["bundle_id"],
                                           command["payload"]["margin_call_id"])
            if not code and any(row["phase"] != "DONE" or row["result"]["outcome"] == "COMMITTED"
                                for row in db.rows(connection, db.requests, run_id=context["run_id"],
                                                   record_id=context["record_id"])):
                code = "PRECONDITION_NOT_MET"
            reply = result(context, "REJECTED", code) if code else result(context, "PENDING")
            connection.execute(db.requests.insert().values(
                **keys, bundle_id=context["bundle_id"], record_id=context["record_id"],
                command=command, context=context, result=reply, phase="DONE" if code else "RESERVE",
            ))
            return reply, None

    def notify_y(self, context, command):
        message_id = uuid4().hex
        with self.engine.begin() as connection:
            self.message(connection, context, "MESSAGE_SENT", message_id, "Z", "Y", "REQUEST")
        response = self.peer.post(f"/internal/runs/{context['run_id']}/allocation-lock",
                                  json={"command": command, "context": context, "message_id": message_id})
        response.raise_for_status()
        answer = response.json()
        if not isinstance(answer, dict) or not isinstance(answer.get("decision"), dict):
            raise ValueError("Unrecognized allocation response")
        decision = answer["decision"]
        for key in ("run_id", "request_id", "bundle_id"):
            if decision.get(key) != context[key]:
                raise ValueError("Mismatched allocation lock")
        if decision.get("command") != command:
            raise ValueError("Mismatched allocation command")
        denial = decision["denial"]
        response_id = answer["message_id"]
        if not valid_message_id(response_id):
            raise ValueError("Invalid response ID")
        if denial is None:
            confirmation = decision["message_id"]
            if (not valid_message_id(confirmation)
                    or not isinstance(decision.get("source_record_id"), str)):
                raise ValueError("Invalid lock confirmation")
        elif denial not in ("PRECONDITION_NOT_MET", "RIGHT_UNAVAILABLE", "VERSION_CONFLICT", "USE_NOT_PERMITTED"):
            raise ValueError("Unexpected allocation denial")
        with self.engine.begin() as connection:
            self.message(connection, context, "MESSAGE_RECEIVED", response_id, "Y", "Z", "RESPONSE")
            if denial is None:
                self.message(connection, context, "MESSAGE_RECEIVED", confirmation,
                             "Y", "Z", "EVENT", category="PROTOCOL")
        return denial

    def advance(self, request_keys):
        with self.engine.connect() as connection:
            request = db.one(connection, db.requests, **request_keys)
        if request["phase"] == "DONE":
            return request["result"]
        context, command = request["context"], request["command"]
        try:
            denial = self.notify_y(context, command)
        except (httpx.HTTPError, ValueError, KeyError):
            return request["result"]
        try:
            with self.engine.begin() as connection:
                db.one(connection, db.runs, lock=True, run_id=context["run_id"])
                current = db.one(connection, db.requests, **request_keys)
                if current["phase"] == "DONE":
                    return current["result"]
                if denial is None:
                    payload = command["payload"]
                    keys = bundle_keys(context)
                    connection.execute(db.allocations.insert().values(
                        **keys, allocation_id=payload["allocation_id"], margin_call_id=payload["margin_call_id"],
                    ))
                    db.change(connection, db.margin_calls, dict(keys, margin_call_id=payload["margin_call_id"]),
                              status="SATISFIED")
                    db.change(connection, db.bundles, keys, version=3)
                    reply = result(context, "COMMITTED")
                    connection.execute(db.commit_observations.insert().values(**request_keys))
                else:
                    reply = result(context, "REJECTED", denial)
                db.change(connection, db.requests, request_keys, phase="DONE", result=reply)
                return reply
        except SQLAlchemyError:
            logger.warning("Allocation confirmation still pending")
            return request["result"]

    def recover(self):
        super().recover()
        with self.engine.connect() as connection:
            pending = connection.execute(select(db.requests.c.run_id, db.requests.c.actor, db.requests.c.request_id)
                                         .where(db.requests.c.phase == "RESERVE")).mappings().all()
        for keys in pending:
            self.advance(dict(keys))
