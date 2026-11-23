import itertools
import time
from threading import Lock
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey, ForeignKeyConstraint, Index,
    Integer, MetaData, String, Table, UniqueConstraint, select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

metadata = MetaData()
runs = Table("runs", metadata, Column("run_id", String(128), primary_key=True))


def run_key():
    return Column("run_id", String(128), ForeignKey("runs.run_id"), primary_key=True)


def bundle_fk():
    return ForeignKeyConstraint(("run_id", "bundle_id"), ("bundles.run_id", "bundles.bundle_id"))


principals = Table(
    "principals", metadata, run_key(), Column("principal_id", String(128), primary_key=True),
    Column("actor", String(8)), Column("observer", Boolean, nullable=False),
    CheckConstraint("(observer AND actor IS NULL) OR "
                    "(NOT observer AND actor IS NOT NULL AND actor IN ('BANK_A','BANK_B','BANK_C','CCP'))"),
)
bundles = Table(
    "bundles", metadata, run_key(), Column("bundle_id", String(128), primary_key=True),
    Column("security_id", String(128), nullable=False),
    Column("quantity", String, nullable=False), Column("reference_amount", String, nullable=False),
    Column("version", Integer, nullable=False, server_default="0"),
    CheckConstraint("bundle_id ~ '^[A-Za-z0-9._:-]{1,120}$'"),
    CheckConstraint("quantity ~ '^[1-9][0-9]*$'"),
    CheckConstraint("reference_amount ~ '^(0|[1-9][0-9]*)$'"),
    CheckConstraint("version BETWEEN 0 AND 3"),
)
agreements = Table(
    "agreements", metadata, run_key(), Column("agreement_id", String(128), primary_key=True),
    Column("bundle_id", String(128), nullable=False), Column("kind", String(4), nullable=False),
    Column("terms", JSONB, nullable=False), bundle_fk(),
    CheckConstraint("kind IN ('REPO','LOAN')"),
    UniqueConstraint("run_id", "agreement_id", "bundle_id"),
)
contracts = Table(
    "contracts", metadata, run_key(), Column("contract_id", String(128), primary_key=True),
    Column("agreement_id", String(128), nullable=False),
    Column("bundle_id", String(128), nullable=False),
    Column("kind", String(4), nullable=False), Column("parties", JSONB, nullable=False),
    ForeignKeyConstraint(("run_id", "agreement_id", "bundle_id"),
                         ("agreements.run_id", "agreements.agreement_id", "agreements.bundle_id")),
    CheckConstraint("kind IN ('REPO','LOAN')"),
    UniqueConstraint("run_id", "bundle_id", "kind"),
)
rights = Table(
    "rights", metadata, run_key(), Column("bundle_id", String(128), primary_key=True),
    Column("created_version", Integer, primary_key=True), Column("holder", String(8), nullable=False),
    Column("status", String(9), nullable=False), Column("allowed_commands", JSONB, nullable=False),
    Column("source_record_id", String(128)), bundle_fk(),
    CheckConstraint("(created_version = 0 AND holder = 'BANK_A' AND source_record_id IS NULL) OR "
                    "(created_version = 1 AND holder = 'BANK_B' AND source_record_id IS NOT NULL) OR "
                    "(created_version = 2 AND holder = 'BANK_C' AND source_record_id IS NOT NULL)"),
    CheckConstraint("status IN ('AVAILABLE','CONSUMED','LOCKED')"),
    CheckConstraint("status <> 'LOCKED' OR created_version = 2"),
    CheckConstraint("status = 'AVAILABLE' OR allowed_commands = '[]'::jsonb"),
    CheckConstraint("jsonb_typeof(allowed_commands) = 'array'"),
)
Index("one_available_right", rights.c.run_id, rights.c.bundle_id, unique=True,
      postgresql_where=rights.c.status == "AVAILABLE")
obligations = Table(
    "obligations", metadata, run_key(), Column("obligation_id", String(128), primary_key=True),
    Column("kind", String(40), nullable=False), Column("debtor", String(8), nullable=False),
    Column("creditor", String(8), nullable=False), Column("contract_id", String(128), nullable=False),
    Column("target", JSONB, nullable=False),
    ForeignKeyConstraint(("run_id", "contract_id"), ("contracts.run_id", "contracts.contract_id")),
    UniqueConstraint("run_id", "contract_id", "kind"),
)
requests = Table(
    "requests", metadata, run_key(), Column("actor", String(8), primary_key=True),
    Column("request_id", String(128), primary_key=True),
    Column("bundle_id", String(128), nullable=False), Column("record_id", String(128), nullable=False),
    Column("command", JSONB, nullable=False), Column("result", JSONB, nullable=False),
    Column("context", JSONB, nullable=False), Column("phase", String(8), nullable=False),
    Column("abort_code", String(32)), Column("accepted_agreement", JSONB),
    CheckConstraint("phase IN ('RESERVE','CONFIRM','ABORT','DONE')"),
    CheckConstraint("(phase = 'DONE') = ((result->>'outcome') <> 'PENDING')"),
    CheckConstraint("(phase = 'ABORT') = (abort_code IS NOT NULL)"),
    CheckConstraint("phase <> 'CONFIRM' OR accepted_agreement IS NOT NULL"),
)
Index("one_request_per_reserved_record", requests.c.run_id, requests.c.record_id, unique=True,
      postgresql_where=requests.c.result["outcome"].astext.in_(("PENDING", "COMMITTED")))
commit_observations = Table(
    "commit_observations", metadata, run_key(), Column("actor", String(8), primary_key=True),
    Column("request_id", String(128), primary_key=True),
    ForeignKeyConstraint(("run_id", "actor", "request_id"),
                         ("requests.run_id", "requests.actor", "requests.request_id")),
)
snapshot_counters = Table(
    "snapshot_counters", metadata, run_key(), Column("sequence", BigInteger, nullable=False),
    CheckConstraint("sequence > 0"),
)
transfers = Table(
    "transfers", metadata, run_key(), Column("request_id", String(128), primary_key=True),
    Column("bundle_id", String(128), nullable=False), Column("record_id", String(128), nullable=False),
    Column("command", JSONB, nullable=False),
    Column("state", String(8), nullable=False), Column("denial", String(32)),
    CheckConstraint("state IN ('DENIED','RESERVED','CONSUMED','RELEASED')"),
    CheckConstraint("(state = 'DENIED') = (denial IS NOT NULL)"),
)
Index("one_reservation_per_bundle", transfers.c.run_id, transfers.c.bundle_id, unique=True,
      postgresql_where=transfers.c.state == "RESERVED")
Index("one_transfer_per_loan", transfers.c.run_id, transfers.c.record_id, unique=True,
      postgresql_where=transfers.c.state.in_(("RESERVED", "CONSUMED")))
transfer_confirmations = Table(
    "transfer_confirmations", metadata, run_key(),
    Column("request_id", String(128), primary_key=True),
    Column("message_id", String(32), nullable=False),
    Column("source_record_id", String(128), nullable=False),
    ForeignKeyConstraint(("run_id", "request_id"), ("transfers.run_id", "transfers.request_id")),
    UniqueConstraint("run_id", "message_id"),
)
events = Table(
    "events", metadata, run_key(), Column("event_id", String(128), primary_key=True),
    Column("payload", JSONB, nullable=False),
)

margin_calls = Table(
    "margin_calls", metadata, run_key(), Column("margin_call_id", String(128), primary_key=True),
    Column("bundle_id", String(128), nullable=False), bundle_fk(),
    Column("provider", String(8), nullable=False), Column("ccp", String(8), nullable=False),
    Column("whole_bundle_accepted", Boolean, nullable=False),
    Column("eligibility_passed", Boolean, nullable=False),
    Column("status", String(9), nullable=False),
    CheckConstraint("provider = 'BANK_C' AND ccp = 'CCP'"),
    CheckConstraint("status IN ('OPEN','SATISFIED')"),
    UniqueConstraint("run_id", "margin_call_id", "bundle_id"),
)
allocations = Table(
    "allocations", metadata, run_key(), Column("allocation_id", String(128), primary_key=True),
    Column("margin_call_id", String(128), nullable=False),
    Column("bundle_id", String(128), nullable=False),
    ForeignKeyConstraint(("run_id", "margin_call_id", "bundle_id"),
                         ("margin_calls.run_id", "margin_calls.margin_call_id", "margin_calls.bundle_id")),
    UniqueConstraint("run_id", "bundle_id"),
    UniqueConstraint("run_id", "margin_call_id"),
)
allocation_locks = Table(
    "allocation_locks", metadata, run_key(), Column("request_id", String(128), primary_key=True),
    Column("bundle_id", String(128), nullable=False),
    Column("command", JSONB, nullable=False),
    Column("denial", String(32)),
    Column("source_record_id", String(128)),
    Column("message_id", String(32)),
    CheckConstraint("(denial IS NULL AND source_record_id IS NOT NULL AND message_id IS NOT NULL) OR "
                    "(denial IS NOT NULL AND source_record_id IS NULL AND message_id IS NULL)"),
    UniqueConstraint("run_id", "message_id"),
)
Index("one_ccp_lock_per_bundle", allocation_locks.c.run_id, allocation_locks.c.bundle_id,
      unique=True, postgresql_where=allocation_locks.c.denial.is_(None))


def rows(connection, table, **keys):
    return connection.execute(select(table).filter_by(**keys)).mappings().all()


def one(connection, table, lock=False, **keys):
    query = select(table).filter_by(**keys)
    if lock:
        query = query.with_for_update()
    return connection.execute(query).mappings().one_or_none()


def change(connection, table, keys, **values):
    connection.execute(table.update().filter_by(**keys).values(**values))


def require_run(connection, run_id):
    return one(connection, runs, run_id=run_id) is not None


def next_snapshot_sequence(connection, run_id):
    return connection.execute(
        insert(snapshot_counters).values(run_id=run_id, sequence=1)
        .on_conflict_do_update(
            index_elements=[snapshot_counters.c.run_id],
            set_={"sequence": snapshot_counters.c.sequence + 1},
        )
        .returning(snapshot_counters.c.sequence)
    ).scalar_one()


def right_id(bundle_id, version):
    return f"{bundle_id}:right:{version}"


def quantity(bundle):
    return {"value": bundle["quantity"], "unit": "KRW_FACE_VALUE"}


def public_bundle(bundle):
    return {
        "bundle_id": bundle["bundle_id"], "security_id": bundle["security_id"],
        "quantity": quantity(bundle), "version": bundle["version"],
        "reference_value": {"amount": bundle["reference_amount"], "currency": "KRW"},
    }


def public_right(right):
    bundle, version = right["bundle_id"], right["created_version"]
    return {
        "right_id": right_id(bundle, version), "bundle_id": bundle, "created_version": version,
        "holder": right["holder"], "status": right["status"],
        "allowed_commands": right["allowed_commands"], "source_record_id": right["source_record_id"],
        "predecessor_right_id": right_id(bundle, version - 1) if version else None,
    }


class EventLog:
    def __init__(self, platform):
        self.platform = platform
        self.producer = f"model-a-{platform}:{uuid4().hex}"
        self.clock = uuid4().hex
        self.sequence = itertools.count(1)
        self.lock = Lock()

    def write(self, connection, context, kind, **fields):
        self.store(connection, self.capture(context, kind, **fields))

    def capture(self, context, kind, **fields):
        event = dict.fromkeys((
            "request_id", "attempt_id", "bundle_id", "record_id", "actor",
            "outcome", "error_code", "version_before", "version_after",
        ))
        with self.lock:
            event.update(sequence=next(self.sequence), mono_ns=str(time.monotonic_ns()))
        event.update(
            context, event_id=uuid4().hex, event_type=kind, model="A",
            platform=self.platform, producer_id=self.producer, clock_id=self.clock, **fields,
        )
        return event

    def store(self, connection, event):
        connection.execute(events.insert().values(
            run_id=event["run_id"], event_id=event["event_id"], payload=event,
        ))
