import logging
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import storage as db

logger = logging.getLogger(__name__)
REPO = "CONFIRM_REPO"
LOAN = "CONFIRM_LOAN"
MARGIN = "ALLOCATE_MARGIN"


def error(code):
    return {"code": code, "message": code.replace("_", " ").capitalize()}


def result(context, outcome, code=None):
    reply = {key: context[key] for key in ("run_id", "request_id", "actor", "command_type")}
    effect = None
    if outcome == "COMMITTED":
        before, record_type = {
            REPO: (0, "REPO"), LOAN: (1, "LOAN"), MARGIN: (2, "ALLOCATION"),
        }[context["command_type"]]
        effect = {
            "bundle_id": context["bundle_id"], "record_id": context["record_id"],
            "version_before": before, "version_after": before + 1,
            "record_type": record_type,
        }
    return dict(reply, outcome=outcome, replayed=False, effect=effect, error=error(code) if code else None)


def keys_for(context):
    return {key: context[key] for key in ("run_id", "actor", "request_id")}


def bundle_keys(context):
    return {key: context[key] for key in ("run_id", "bundle_id")}


class Workflow:
    def __init__(self, engine, platform, x_url, internal_token):
        self.engine = engine
        self.log = db.EventLog(platform)
        self.peer = httpx.Client(base_url=x_url or "http://api-x:8000", timeout=1.5, trust_env=False,
                                 headers={"Authorization": f"Bearer {internal_token}"})

    def validate_agreement(self, connection, context, command):
        agreement = db.one(connection, db.agreements, run_id=context["run_id"],
                           agreement_id=command["payload"]["agreement_id"])
        kind = "REPO" if context["command_type"] == REPO else "LOAN"
        if agreement is None or agreement["bundle_id"] != context["bundle_id"] or agreement["kind"] != kind:
            return None, "PRECONDITION_NOT_MET"
        terms = agreement["terms"]
        if kind == "REPO":
            if terms["seller"] != context["actor"] or terms["buyer"] != "BANK_B":
                return None, "PRECONDITION_NOT_MET"
        else:
            collateral = terms["collateral_condition"]
            if (terms["lender"] != context["actor"] or terms["borrower"] != "BANK_C"
                    or not collateral["provided"] or not collateral["conditions_met"]
                    or collateral["collateral_id"] == context["bundle_id"]):
                return None, "PRECONDITION_NOT_MET"
        return agreement, None

    def begin(self, context, command):
        request_keys = keys_for(context)
        with self.engine.begin() as connection:
            # Serialize the short local decisions within a run. Never hold this lock across HTTP.
            db.one(connection, db.runs, lock=True, run_id=context["run_id"])
            prior = db.one(connection, db.requests, **request_keys)
            if prior is not None:
                if prior["command"] != command:
                    return None, "REQUEST_ID_CONFLICT"
                return dict(prior["result"], replayed=True), None
            bundle = db.one(connection, db.bundles, **bundle_keys(context))
            agreement, code = self.validate_agreement(connection, context, command)
            used_record = any(row["result"]["outcome"] in ("PENDING", "COMMITTED") for row in
                              db.rows(connection, db.requests, run_id=context["run_id"],
                                      record_id=context["record_id"]))
            if bundle is None or used_record:
                code = "PRECONDITION_NOT_MET"
            if not code and context["command_type"] == REPO:
                if self.loan_id_taken(connection, context):
                    code = "PRECONDITION_NOT_MET"
                elif bundle["version"] != command["expected_version"] or bundle["version"] != 0:
                    code = "VERSION_CONFLICT"
                else:
                    initial = db.one(connection, db.rights, **bundle_keys(context), created_version=0)
                    if not initial or initial["status"] != "AVAILABLE" or REPO not in initial["allowed_commands"]:
                        code = "RIGHT_UNAVAILABLE"
            if code:
                reply = result(context, "REJECTED", code)
            elif context["command_type"] == LOAN:
                reply = result(context, "PENDING")
            else:
                db.change(connection, db.rights, dict(bundle_keys(context), created_version=0),
                          status="CONSUMED", allowed_commands=[])
                self.right_event(connection, context, 0, "CONSUMED", None)
                allowed = [LOAN] if agreement["terms"]["reuse_allowed"] else []
                reply = self.commit(connection, context, bundle, agreement, "REPO", "BANK_A", "BANK_B", allowed)
            connection.execute(db.requests.insert().values(
                **request_keys, bundle_id=context["bundle_id"], record_id=context["record_id"],
                command=command, context=context, result=reply,
                phase="RESERVE" if reply["outcome"] == "PENDING" else "DONE",
            ))
            if reply["outcome"] == "COMMITTED":
                connection.execute(db.commit_observations.insert().values(**request_keys))
        return reply, None

    def commit(self, connection, context, bundle, agreement, kind, first, second, allowed):
        reply = result(context, "COMMITTED")
        version = reply["effect"]["version_after"]
        record_id = context["record_id"]
        run_id = context["run_id"]
        agreement_id = agreement["agreement_id"]
        connection.execute(db.contracts.insert().values(
            run_id=run_id, contract_id=record_id, agreement_id=agreement_id,
            bundle_id=context["bundle_id"], kind=kind, parties=[first, second],
        ))

        def obligation(suffix, obligation_type, debtor, creditor, target):
            return {
                "run_id": run_id, "obligation_id": f"{record_id}:{suffix}",
                "kind": obligation_type, "debtor": debtor, "creditor": creditor,
                "contract_id": record_id, "target": target,
            }

        securities = {"security_id": bundle["security_id"], "quantity": db.quantity(bundle)}
        if kind == "REPO":
            obligations = [
                obligation("repurchase-payment", "REPO_REPURCHASE_PAYMENT", first, second,
                           {"agreement_id": agreement_id}),
                obligation("securities-return", "REPO_SECURITIES_RETURN", second, first, securities),
            ]
        else:
            obligations = [
                obligation("securities-return", "LOAN_SECURITIES_RETURN", second, first, securities),
                obligation("collateral-return", "LOAN_COLLATERAL_RETURN_OR_RELEASE", first, second, {
                    "collateral_id": agreement["terms"]["collateral_condition"]["collateral_id"],
                    "agreement_id": agreement_id,
                }),
            ]
        connection.execute(db.obligations.insert(), obligations)
        connection.execute(db.rights.insert().values(
            **bundle_keys(context), created_version=version, holder=second,
            status="AVAILABLE", allowed_commands=allowed, source_record_id=record_id,
        ))
        db.change(connection, db.bundles, bundle_keys(context), version=version)
        self.right_event(connection, context, version, "AVAILABLE", record_id)
        return reply

    def right_event(self, connection, context, version, status, source):
        bundle = context["bundle_id"]
        self.log.write(
            connection, context, "RIGHT_CREATED" if status == "AVAILABLE" else "RIGHT_CONSUMED",
            right_id=db.right_id(bundle, version), holder=("BANK_A", "BANK_B", "BANK_C")[version],
            predecessor_right_id=db.right_id(bundle, version - 1) if version else None,
            source_record_id=source, right_status=status,
        )

    def publish_commit_observation(self, request_keys):
        with self.engine.begin() as connection:
            if db.one(connection, db.commit_observations, lock=True, **request_keys) is None:
                return
            request = db.one(connection, db.requests, **request_keys)
            effect = request["result"]["effect"]
            # This clock observes a committed fact. A restart cannot recover the
            # previous process's commit-acknowledgment time.
            for event_type in ("INTERNAL_COMMITTED", "LOCAL_APPLIED"):
                self.log.write(connection, request["context"], event_type, outcome="COMMITTED",
                               version_before=effect["version_before"], version_after=effect["version_after"])
            connection.execute(db.commit_observations.delete().filter_by(**request_keys))

    def authority(self, connection, run_id, bundle_id, expected=None):
        keys = dict(run_id=run_id, bundle_id=bundle_id)
        bundle = db.one(connection, db.bundles, **keys)
        if bundle is None:
            return None, "PRECONDITION_NOT_MET", None
        version = bundle["version"]
        if expected is not None and expected != version:
            return version, "VERSION_CONFLICT", None
        right = db.one(connection, db.rights, **keys, created_version=1)
        if right is None:
            return version, "PRECONDITION_NOT_MET", None
        if right["status"] != "AVAILABLE" or db.one(connection, db.transfers, **keys, state="RESERVED"):
            return version, "RIGHT_UNAVAILABLE", None
        if LOAN not in right["allowed_commands"]:
            return version, "USE_NOT_PERMITTED", None
        return version, None, right

    def move_right(self, connection, context, command, phase):
        run_id, request_id = context["run_id"], context["request_id"]
        db.one(connection, db.runs, lock=True, run_id=run_id)
        keys = dict(run_id=run_id, request_id=request_id)
        transfer = db.one(connection, db.transfers, **keys)
        if transfer is not None and transfer["command"] != command:
            return None
        if phase == "RESERVE" and transfer is None:
            _, denial, _ = self.authority(connection, run_id, context["bundle_id"], command["expected_version"])
            if not denial and (self.loan_id_taken(connection, context) or db.one(
                connection, db.contracts, run_id=run_id, contract_id=context["record_id"],
            )):
                denial = "PRECONDITION_NOT_MET"
            transfer = dict(state="DENIED" if denial else "RESERVED", denial=denial)
            connection.execute(db.transfers.insert().values(
                **keys, bundle_id=context["bundle_id"], record_id=context["record_id"],
                command=command, **transfer,
            ))
        if transfer is None:
            return None
        state = transfer["state"]
        newly_consumed = False
        if state == "RESERVED" and phase == "CONFIRM":
            right_keys = dict(bundle_keys(context), created_version=1)
            right = db.one(connection, db.rights, **right_keys)
            db.change(connection, db.rights, right_keys, status="CONSUMED", allowed_commands=[])
            self.right_event(connection, context, 1, "CONSUMED", right["source_record_id"])
            state = "CONSUMED"
            newly_consumed = True
        elif state == "RESERVED" and phase == "ABORT":
            state = "RELEASED"
        if state != transfer["state"]:
            db.change(connection, db.transfers, keys, state=state)
        right = db.one(connection, db.rights, **bundle_keys(context), created_version=1)
        source_record_id = right["source_record_id"] if right else None
        answer = {"state": state, "denial": transfer["denial"], "source_record_id": source_record_id}
        if newly_consumed:
            # Publish the confirmation to the durable X→Y channel in the same
            # transaction as consumption. HTTP only transports that message.
            confirmation = dict(keys, message_id=uuid4().hex, source_record_id=source_record_id)
            connection.execute(db.transfer_confirmations.insert().values(**confirmation))
            self.message(connection, context, "MESSAGE_SENT", confirmation["message_id"],
                         "X", "Y", "EVENT", category="PROTOCOL")
            answer["confirmation"] = confirmation
        elif state == "CONSUMED":
            confirmation = db.one(connection, db.transfer_confirmations, **keys)
            if confirmation is not None:
                answer["confirmation"] = dict(confirmation)
        return answer

    def loan_id_taken(self, connection, context):
        return any(row["state"] in ("RESERVED", "CONSUMED") for row in db.rows(
            connection, db.transfers, run_id=context["run_id"], record_id=context["record_id"],
        ))

    def message(self, connection, context, event_type, message_id, source, destination, kind,
                category="BUSINESS_INTEGRATION"):
        self.log.write(connection, context, event_type, message_id=message_id, source_id=source,
                       destination_id=destination, message_kind=kind, message_category=category)

    def notify_x(self, context, command, phase):
        message_id = uuid4().hex
        with self.engine.begin() as connection:
            self.message(connection, context, "MESSAGE_SENT", message_id, "Y", "X", "REQUEST")
        response = self.peer.post(f"/internal/runs/{context['run_id']}/transfers/{phase.lower()}",
                                  json={"command": command, "context": context, "message_id": message_id})
        response.raise_for_status()
        answer = response.json()
        if not isinstance(answer, dict) or not isinstance(answer.get("state"), str):
            raise ValueError("Unrecognized transfer response")
        confirmation = answer.get("confirmation")
        if confirmation is not None and (
            not isinstance(confirmation, dict)
            or answer["state"] != "CONSUMED"
            or confirmation.get("run_id") != context["run_id"]
            or confirmation.get("request_id") != context["request_id"]
            or confirmation.get("source_record_id") != answer["source_record_id"]
            or not isinstance(confirmation.get("message_id"), str)
            or UUID(confirmation["message_id"]).hex != confirmation["message_id"]
        ):
            raise ValueError("Unrecognized durable confirmation")
        with self.engine.begin() as connection:
            self.message(connection, context, "MESSAGE_RECEIVED", answer["message_id"], "X", "Y", "RESPONSE")
            if confirmation is not None:
                self.message(connection, context, "MESSAGE_RECEIVED", confirmation["message_id"],
                             "X", "Y", "EVENT", category="PROTOCOL")
        return answer

    def advance(self, request_keys):
        # The same phase may be sent by the submitter and the recovery loop. X replies
        # from its durable record. Y only applies the response to the phase that sent it.
        for _ in range(3):
            with self.engine.connect() as connection:
                request = db.one(connection, db.requests, **request_keys)
            phase = request["phase"]
            if phase == "DONE":
                return request["result"]
            context, command = request["context"], request["command"]
            try:
                answer = self.notify_x(context, command, phase)
            except (httpx.HTTPError, ValueError, KeyError):
                return request["result"]
            with self.engine.begin() as connection:
                db.one(connection, db.runs, lock=True, run_id=context["run_id"])
                current = db.one(connection, db.requests, **request_keys)
                if current["phase"] != phase:
                    continue
                state = answer["state"]
                if phase == "RESERVE" and state == "RESERVED":
                    agreement, code = self.validate_agreement(connection, context, command)
                    if code:
                        db.change(connection, db.requests, request_keys, phase="ABORT", abort_code=code)
                    else:
                        db.change(connection, db.requests, request_keys, phase="CONFIRM",
                                  accepted_agreement=dict(agreement))
                    continue
                if phase == "RESERVE" and state == "DENIED":
                    reply = result(context, "REJECTED", answer["denial"])
                elif phase == "ABORT" and state == "RELEASED":
                    reply = result(context, "REJECTED", current["abort_code"])
                elif phase == "CONFIRM" and state == "CONSUMED":
                    reply = self.finish_loan(connection, context, current["accepted_agreement"],
                                             answer["source_record_id"])
                else:
                    logger.error("Unexpected transfer state %s for phase %s", state, phase)
                    return current["result"]
                db.change(connection, db.requests, request_keys, phase="DONE", result=reply, abort_code=None)
                if reply["outcome"] == "COMMITTED":
                    connection.execute(db.commit_observations.insert().values(**request_keys))
            return reply
        with self.engine.connect() as connection:
            return db.one(connection, db.requests, **request_keys)["result"]

    def finish_loan(self, connection, context, agreement, source_record_id):
        keys = bundle_keys(context)
        bundle = db.one(connection, db.bundles, **keys)
        connection.execute(db.rights.insert().values(
            **keys, created_version=1, holder="BANK_B", status="CONSUMED",
            allowed_commands=[], source_record_id=source_record_id,
        ))
        allowed = ["ALLOCATE_MARGIN"] if agreement["terms"]["ccp_use_allowed"] else []
        return self.commit(connection, context, bundle, agreement, "LOAN", "BANK_B", "BANK_C", allowed)

    def recover(self):
        with self.engine.connect() as connection:
            observations = connection.execute(select(db.commit_observations)).mappings().all()
        for observation in observations:
            self.publish_commit_observation(dict(observation))
        if self.log.platform != "Y":
            return
        with self.engine.connect() as connection:
            pending = connection.execute(select(db.requests.c.run_id, db.requests.c.actor,
                                                db.requests.c.request_id)
                                         .where(db.requests.c.phase != "DONE")).mappings().all()
        for request_keys in pending:
            try:
                self.advance(dict(request_keys))
            except SQLAlchemyError:
                logger.warning("Loan recovery could not apply a database transaction")
