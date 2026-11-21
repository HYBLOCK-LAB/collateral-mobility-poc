"""Read Model A DB events into the runner's evidence file without changing DB state."""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from prepare import connect, valid_run
from checks import CheckError, validate


class Unsettled(CheckError):
    pass


def collect_once(run_id, output):
    valid_run(run_id)
    events = []
    for platform in ("X", "Y"):
        with connect(platform, "EVIDENCE") as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if not connection.execute("SELECT 1 FROM runs WHERE run_id = %s", (run_id,)).fetchone():
                raise CheckError("run missing on Model A database")
            for table, condition in (("requests", "phase <> 'DONE'"),
                                     ("transfers", "state = 'RESERVED'"),
                                     ("commit_observations", "TRUE")):
                # Table and condition come only from the fixed list above.
                if connection.execute(f"SELECT 1 FROM {table} WHERE run_id = %s AND {condition} LIMIT 1",
                                      (run_id,)).fetchone():
                    raise Unsettled("implementation has unsettled operations/observations")
            rows = connection.execute(
                "SELECT payload FROM events WHERE run_id = %s "
                "ORDER BY payload->>'producer_id', payload->>'clock_id', (payload->>'sequence')::bigint",
                (run_id,)).fetchall()
            for (event,) in rows:
                validate(event, "event.schema.json")
                if event["run_id"] != run_id or event["model"] != "A" or event["platform"] != platform:
                    raise CheckError("implementation event identity mismatch")
                events.append(event)
    if len({e["event_id"] for e in events}) != len(events):
        raise CheckError("duplicate implementation event ID")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as target:
            pending = Path(target.name)
            for event in events:
                target.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.replace(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return len(events)


def collect(run_id, output):
    # Recovery may publish its commit observation after the result becomes visible.
    deadline = time.monotonic() + 10
    while True:
        try:
            return collect_once(run_id, output)
        except Unsettled:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = collect(args.run_id, args.output)
    except Exception as error:
        reason = str(error) if isinstance(error, CheckError) else type(error).__name__
        print(json.dumps({"status": "FAILED", "reason": reason}))
        return 1
    print(json.dumps({"run_id": args.run_id, "status": "COLLECTED", "event_count": count}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
