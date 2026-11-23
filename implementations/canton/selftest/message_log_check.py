#!/usr/bin/env python3
"""Check the participant calls that runs logged as messages (README §3
"Participant 호출 기록", measurement.md §8.3).

    .venv/bin/python -I selftest/message_log_check.py [--json OUT] [--allow-unlogged] <path> ...

Each <path> is a file or a directory searched for events.jsonl (the adapters'
evidence directory, CANTON_EVIDENCE_DIR or logs/, or one run folder of it), for
implementation-events.json (the copy the common runner keeps in each case
folder of a batch) and for the runner's records of its calls (call-*.json and
snapshot-*.json, below). For each run it checks that the
MESSAGE_SENT/MESSAGE_RECEIVED events
  schema       pass event.schema.json, as every other event of the run does (an
               event that does not is reported and left out of the checks below)
  ids          are <purpose>:<32 hex>[:p<n>]:req|res with a purpose of the
               adapter's MESSAGE_PURPOSES, each ID once; state reads carry a page
               number, submissions (submit, and B2's unassign and assign) do not
  shape        SENT is a REQUEST from canton-adapter-<P> to that platform's
               participant, RECEIVED a RESPONSE back; the category is the
               purpose's
  attribution  carry the test, attempt, request, bundle and actor of a
               COMMAND_RECEIVED that the same adapter wrote before them; the
               reads that answer a runner GET (result, readiness, snapshot) carry
               no command events but the fields of their query: result the
               request and an institution, readiness the bundle, command type
               and an institution, snapshot none of them
  context      with the runner's records of the run, each record has an attempt
               ID of its own; each call's attempt is one of those calls on that
               platform: a submission or a command's read is the POST of its
               request, a GET read is the GET it answers (commands/<request_id>,
               readiness with its bundle and command type, snapshot), and a read
               a snapshot file records carries that file's test ID; and every
               runner GET the adapter answers after reading (200, 202, 404) has a
               logged read, except a result answered REJECTED, which may come
               from the rejection kept on disk, and a 404 for a path or method
               the adapter does not serve
  order        each RECEIVED follows the SENT of its call on the same clock; a
               read's pages are numbered from 1 and page n+1 is sent only after
               page n was received, for the same attempt and purpose
  submit       each INTERNAL_COMMITTED follows the RECEIVED of a submission of its
               request (the adapter writes it from the confirmed response): a
               submit, or for a B2 reassignment its assign
and lists the calls whose response was not logged (an answer lost or not read
whole, which the adapter's recovery handles: reported, not a failure).

The runner's records of a run are in an evidence folder of a batch (a case's
<case>/evidence, or a preparation batch's evidence): call-*.json for each call
it made to a platform, and snapshot-<P>.json (P a platform of the adapter) for
the initial-state snapshot GET of the preparation. A runner from 0b1ee43 on
traces that GET in the file (test_id PREPARE, attempt_id, method GET, platform
<P>, suffix snapshot) and an earlier one does not: a snapshot file without any
of these fields is counted as untraced and is not a record; one with only some
of them, with another method, platform or suffix, or without an integer
status_code, is a problem. The records of a folder are checked with the run of
the manifest.json above the folder in the implementation-events.json next to
them, even when that file holds no event of the run; the other runs of the file
are checked without records, and a folder without a manifest run_id with the
file's one run. A folder without that file (a preparation batch, or a case that
failed before the runner copied the adapter's events) is checked with the
adapter's <evidence dir>/<run_id>/events.jsonl, when that file is among the
paths, again even when it is empty (with each such file, when the paths hold
more than one). A folder that cannot be paired this way is listed as not
checked, which is not a failure, and only its records are checked. Every other
event file is checked without records. A file or folder reached through more
than one of the paths is read once.

An event the order checks need but that is missing from the run (the adapter
does not fail a call when writing its event fails) is reported as "not logged",
one left out for failing the schema as such, an event logged in the wrong order
as such. A run that committed but logged no
message at all was made before message logging, or with it broken: it fails
once, or with --allow-unlogged is listed as "none" and passes (for an evidence
directory that also holds older runs).

It prints, per run, the messages by category, purpose and kind, counting message
IDs as tests/conformance/report.py counts N_message, then the spread of each
count over the runs, the state reads (every purpose but the submissions) by the number
of pages they read, how many runner calls the context check matched (by the
GET they answer, command for the rest, snapshot-file for the snapshot GETs that
snapshot files record), the untraced snapshot files and the folders not
checked. --json writes the same counts and the problems to OUT, the folders
not checked under "(not checked)".
Exit status 0 when every check held, 1 otherwise, 2 when a path is empty or
missing or no run was found.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

IMPL_DIR = Path(__file__).resolve().parent.parent
ROOT = IMPL_DIR.parent.parent
sys.path.insert(0, str(ROOT / "tests/conformance"))
sys.path.insert(0, str(IMPL_DIR / "adapter"))
from checks import parse_json, validate  # noqa: E402
from server import (  # noqa: E402
    INSTITUTIONS,
    MESSAGE_PURPOSES,
    PLATFORMS,
    QUERY_PURPOSES,
)

MESSAGES = ("MESSAGE_SENT", "MESSAGE_RECEIVED")
MESSAGE_ID = re.compile(
    r"^(?P<stem>(?P<read>(?P<purpose>[a-z-]+):[0-9a-f]{32})(?::p(?P<page>[1-9][0-9]*))?)"
    r":(?P<end>req|res)$"
)
ATTRIBUTION = ("test_id", "attempt_id", "request_id", "bundle_id", "actor")
# Ledger submissions: one call each, no pages. A reassignment (B2) commits
# with its assign, after its unassign.
SUBMISSIONS = ("submit", "unassign", "assign")
COMMITTING = ("submit", "assign")
assert set(SUBMISSIONS) <= set(MESSAGE_PURPOSES)
PAGED = {purpose for purpose in MESSAGE_PURPOSES if purpose not in SUBMISSIONS}
# The fields a GET read carries besides its test and attempt (server.py query_trail).
QUERY_FIELDS = {
    "result": {"request_id", "actor"},
    "readiness": {"bundle_id", "command_type", "actor"},
    "snapshot": set(),
}
assert set(QUERY_FIELDS) == set(QUERY_PURPOSES)
# A runner GET the adapter answers with these statuses was answered after a read.
READ_STATUSES = (200, 202, 404)
# The 404 message of a path or method the adapter does not serve: no read.
NO_SUCH_CALL = "no such call on platform "
# The fields a traced snapshot file keeps of the GET that took it (runner.py
# collect_snapshots, from 0b1ee43 on).
SNAPSHOT_TRACE = ("test_id", "attempt_id", "method", "platform", "suffix")
# The key under which a record keeps the name of its file (not a runner field).
RECORD_FILE = "_file"


class Checked(NamedTuple):
    problems: list[str]
    counts: Counter  # (category, purpose, kind) -> messages
    unanswered: list[str]  # stems of calls without a logged response
    unlogged: int  # commits of a run that logged no message at all
    reads: Counter  # pages -> state reads (of any read purpose) of that many pages
    matched: Counter | None  # runner calls the context check matched, by kind;
    # None when no runner call records were given


class Unit(NamedTuple):
    """One run to check: its events in one event file, with the runner's records
    paired with that file (None when none are)."""

    run_id: str
    path: Path  # the event file
    events: list
    records: list | None  # call_records of the paired evidence folder
    untraced: int | None  # untraced snapshot files of that folder
    folder: Path | None  # that folder


def file_text(path: Path) -> str:
    """The event lines of one file: events.jsonl itself, or the body of the
    runner's implementation-events.json."""
    text = path.read_text()
    if path.name != "implementation-events.json":
        return text
    wrapper = parse_json(text)
    body = wrapper.get("body") if isinstance(wrapper, dict) else None
    if not isinstance(body, str):
        raise ValueError("no events body")
    return body


def call_records(folder: Path) -> tuple[list, int]:
    """The runner's records of its calls in one evidence folder of a batch: each
    call-*.json and each traced snapshot-<P>.json, with the number of snapshot
    files that carry no trace. A file that cannot be read, or a snapshot file
    traced in part or not as the snapshot GET of its platform, is kept as a
    problem string."""
    records: list = []
    untraced = 0
    snapshots = (folder / f"snapshot-{platform}.json" for platform in sorted(PLATFORMS))
    files = sorted(folder.glob("call-*.json")) + [p for p in snapshots if p.exists()]
    for path in files:
        try:
            record = parse_json(path.read_text())
            if not isinstance(record, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as error:
            records.append(f"{path}: {error}")
            continue
        if path.name.startswith("snapshot-"):
            if all(key not in record for key in SNAPSHOT_TRACE):
                untraced += 1
                continue
            want = {
                "method": "GET",
                "platform": path.stem.removeprefix("snapshot-"),
                "suffix": "snapshot",
            }
            wrong = [key for key, value in want.items() if record.get(key) != value]
            wrong += [
                key
                for key in ("test_id", "attempt_id")
                if not isinstance(record.get(key), str) or not record[key]
            ]
            status = record.get("status_code")
            if not isinstance(status, int) or isinstance(status, bool):
                wrong.append("status_code")
            if wrong:
                records.append(
                    f"{path}: snapshot trace "
                    + ", ".join(f"{key} {record.get(key)!r}" for key in wrong)
                )
                continue
        record[RECORD_FILE] = path.name
        records.append(record)
    return records, untraced


def folder_run_id(folder: Path) -> str | None:
    """The run of an evidence folder of a batch: the run_id of the manifest.json
    above it (a case folder's, or a preparation batch's)."""
    try:
        manifest = parse_json((folder.parent / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    run_id = manifest.get("run_id") if isinstance(manifest, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


def routed_away(body) -> bool:
    """Whether a 404 body is the one the adapter gives a path or method it does
    not serve (server.py errors middleware), which answers before any read."""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return isinstance(message, str) and message.startswith(NO_SUCH_CALL)


def answers(call: dict, purpose: str, event: dict) -> bool:
    """Whether a runner call is the one a message of this purpose was made for."""
    if call.get("platform") != event["platform"]:
        return False
    if purpose not in QUERY_PURPOSES:
        command = call.get("command")
        return (
            call.get("method") == "POST"
            and call.get("suffix") == "commands"
            and isinstance(command, dict)
            and command.get("request_id") == event["request_id"]
        )
    if call.get("method") != "GET":
        return False
    if purpose == "result":
        return call.get("suffix") == f"commands/{event['request_id']}"
    if purpose == "snapshot":
        return call.get("suffix") == "snapshot"
    params = call.get("params")
    return (
        call.get("suffix") == "readiness"
        and isinstance(params, dict)
        and params.get("bundle_id") == event["bundle_id"]
        and params.get("command_type") == event["command_type"]
    )


def get_kind(call: dict) -> str | None:
    """The GET read purpose a runner GET call asks for, if any."""
    suffix = call.get("suffix")
    if call.get("method") != "GET" or not isinstance(suffix, str):
        return None
    if suffix.startswith("commands/"):
        return "result"
    return suffix if suffix in ("readiness", "snapshot") else None


def run_events(paths: list[Path]) -> dict[Path, dict[str, list]]:
    """Every event of the given files, by file and run_id (a file without events
    has no run). A file or a line that cannot be read as an event is kept as a
    problem string, under the run "<unreadable>"."""
    files: dict[Path, dict[str, list]] = {}
    for path in paths:
        runs = files.setdefault(path, defaultdict(list))
        try:
            text = file_text(path)
        except (OSError, ValueError) as error:  # CheckError is a ValueError
            runs["<unreadable>"].append(f"{path}: {error}")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = parse_json(line)
                if not isinstance(event, dict):
                    raise ValueError("not a JSON object")
            except ValueError as error:
                runs["<unreadable>"].append(f"{path}:{number}: {error}")
                continue
            runs[event.get("run_id", "<no run_id>")].append(event)
    return files


def check_run(
    events: list, allow_unlogged: bool = False, calls: list | None = None
) -> Checked:
    """The checks above for the events of one run; the context check only
    with the runner's records of the run (call_records)."""
    problems = [e for e in events if isinstance(e, str)]
    problems += [c for c in calls or () if isinstance(c, str)]
    by_attempt: dict = {}
    for call in calls or ():
        if not isinstance(call, dict):
            continue
        attempt = call.get("attempt_id")
        if not isinstance(attempt, str) or not attempt:
            problems.append(f"runner call {call.get('method')} {call.get('suffix')}: no attempt_id")
        elif attempt in by_attempt:
            problems.append(f"runner calls share attempt {attempt}")
        else:
            by_attempt[attempt] = call
    valid, invalid = [], set()  # invalid: message IDs of events left out
    for event in events:
        if isinstance(event, str):
            continue
        try:
            validate(event, "event.schema.json")
        except ValueError as error:
            problems.append(f"{event.get('event_id')}: {error}")
            invalid.add(event.get("message_id"))
            continue
        valid.append(event)
    # One adapter process is one clock; a restarted adapter starts a new one.
    order = sorted(valid, key=lambda e: (e["clock_id"], e["sequence"]))
    messages = [e for e in order if e["event_type"] in MESSAGES]
    commits = sum(e["event_type"] == "INTERNAL_COMMITTED" for e in order)
    if commits and not messages:
        if not allow_unlogged:
            problems.append(
                f"{commits} INTERNAL_COMMITTED but no participant message logged"
                " (a run made before message logging?)"
            )
        return Checked(problems, Counter(), [], commits, Counter(), None)
    logged = {e["message_id"] for e in messages}
    command_attempts = {
        e["attempt_id"] for e in order if e["event_type"].startswith("COMMAND_")
    }
    matched: Counter = Counter()
    context_seen: set[str] = set()  # attempts the context check has matched
    read_attempts: set[str] = set()  # attempts of logged GET reads

    def absent(message_id: str) -> str:
        return "failed the schema" if message_id in invalid else "was not logged"

    submissions = defaultdict(set)  # clock -> (request_id, kind) of submit messages
    for event in messages:
        if event["message_id"].split(":", 1)[0] in COMMITTING:
            submissions[event["clock_id"]].add(
                (event["request_id"], event["event_type"])
            )
    commands = defaultdict(set)  # clock -> attributions of COMMAND_RECEIVED so far
    submitted = defaultdict(set)  # clock -> request_ids of received submissions
    sent, answered, seen = {}, set(), set()  # sent: stem -> SENT event
    last_page: dict[str, int] = {}  # read -> highest page sent
    counts: Counter = Counter()
    for event in order:
        kind, clock, producer = (
            event["event_type"],
            event["clock_id"],
            event["producer_id"],
        )
        attribution = tuple(event.get(key) for key in ATTRIBUTION)
        if kind == "COMMAND_RECEIVED":
            commands[clock].add(attribution)
            continue
        if kind == "INTERNAL_COMMITTED":
            request_id = event["request_id"]
            if request_id not in submitted[clock]:
                if (request_id, "MESSAGE_RECEIVED") in submissions[clock]:
                    why = "the RECEIVED of its submission comes after it"
                elif (request_id, "MESSAGE_SENT") in submissions[clock]:
                    why = "the RECEIVED of its submission was not logged"
                else:
                    why = "no submission of its request was logged"
                problems.append(
                    f"{event['event_id']}: INTERNAL_COMMITTED of {request_id}: {why}"
                )
            continue
        if kind not in MESSAGES:
            continue
        name = event["message_id"]
        where = f"{producer} {name}"
        match = MESSAGE_ID.match(name)
        if match is None or match["purpose"] not in MESSAGE_PURPOSES:
            problems.append(f"{where}: not <purpose>:<hex>[:p<n>]:req|res")
            continue
        if name in seen:
            problems.append(f"{where}: message ID logged twice")
            continue
        seen.add(name)
        purpose, stem, page = match["purpose"], match["stem"], match["page"]
        request = kind == "MESSAGE_SENT"
        if (purpose in PAGED) != (page is not None):
            problems.append(f"{where}: page number missing or on a submission")
        if (match["end"] == "req") != request:
            problems.append(f"{where}: {kind} with a {match['end']} ID")
        platform = event["platform"]
        participant = PLATFORMS.get(platform, {}).get("participant")
        if producer != f"canton-adapter-{platform}" or participant is None:
            problems.append(f"{where}: producer of platform {platform!r}")
        want = {
            "message_kind": "REQUEST" if request else "RESPONSE",
            "message_category": MESSAGE_PURPOSES[purpose],
            "source_id": producer if request else participant,
            "destination_id": participant if request else producer,
        }
        for key, value in want.items():
            if event.get(key) != value:
                problems.append(f"{where}: {key} {event.get(key)!r}, not {value!r}")
        if purpose in QUERY_PURPOSES:
            for key in ("request_id", "bundle_id", "command_type", "actor"):
                if (event.get(key) is not None) != (key in QUERY_FIELDS[purpose]):
                    problems.append(f"{where}: {key} {event.get(key)!r} on a {purpose} read")
            if event["actor"] is not None and event["actor"] not in INSTITUTIONS:
                problems.append(f"{where}: actor {event['actor']!r}")
            if event["attempt_id"] is None or event["attempt_id"] in command_attempts:
                problems.append(f"{where}: attempt of a command or none")
            read_attempts.add(event["attempt_id"])
        elif attribution not in commands[clock]:
            problems.append(f"{where}: no earlier COMMAND_RECEIVED of its command")
        if calls is not None:
            call = by_attempt.get(event["attempt_id"])
            if call is None:
                problems.append(f"{where}: no runner call with its attempt")
            elif not answers(call, purpose, event):
                command = call.get("command")
                asked = call.get("params") or (
                    command.get("request_id") if isinstance(command, dict) else None
                )
                problems.append(
                    f"{where}: its attempt is the runner's {call.get('method')}"
                    f" {call.get('platform')} {call.get('suffix')}"
                    + (f" {asked}" if asked else "")
                )
            else:
                # The initial-state snapshot a snapshot file records (test PREPARE).
                taken = call.get(RECORD_FILE, "").startswith("snapshot-")
                if taken and event["test_id"] != call["test_id"]:
                    problems.append(
                        f"{where}: test {event['test_id']!r}, not the"
                        f" {call['test_id']!r} of {call[RECORD_FILE]}"
                    )
                if event["attempt_id"] not in context_seen:
                    context_seen.add(event["attempt_id"])
                    matched["snapshot-file" if taken else get_kind(call) or "command"] += 1
        counts[event["message_category"], purpose, event["message_kind"]] += 1
        if request:
            sent[stem] = event
            number = int(page) if page is not None else 1
            if page is not None:
                last_page[match["read"]] = max(number, last_page.get(match["read"], 0))
            before = f"{match['read']}:p{number - 1}"
            if number > 1 and before not in answered:
                if f"{before}:req" not in logged:
                    why = f"page {number - 1} {absent(f'{before}:req')}"
                elif f"{before}:res" not in logged:
                    why = f"the RECEIVED of page {number - 1} {absent(f'{before}:res')}"
                else:
                    why = f"sent before page {number - 1} was received"
                problems.append(f"{where}: {why}")
            if (
                number > 1
                and before in sent
                and any(
                    sent[before].get(key) != event.get(key)
                    for key in ATTRIBUTION + ("command_type", "clock_id")
                )
            ):
                problems.append(f"{where}: page of another command or clock")
            continue
        origin = sent.get(stem)
        if origin is None:
            if f"{stem}:req" in logged:
                problems.append(f"{where}: RECEIVED before the SENT of its call")
            else:
                problems.append(f"{where}: the SENT of its call {absent(f'{stem}:req')}")
        elif any(
            origin.get(key) != event.get(key)
            for key in ATTRIBUTION + ("command_type", "clock_id")
        ):
            problems.append(f"{where}: RECEIVED on another clock or command")
        answered.add(stem)
        if purpose in COMMITTING:
            submitted[clock].add(event["request_id"])
    unanswered = sorted(stem for stem in sent if stem not in answered)
    for call in by_attempt.values():
        kind, status = get_kind(call), call.get("status_code")
        if kind is None or status not in READ_STATUSES:
            continue
        try:
            body = parse_json(call.get("body") or "null")
        except ValueError:
            body = None
        if kind == "result" and status == 200:
            if isinstance(body, dict) and body.get("outcome") == "REJECTED":
                continue
        if status == 404 and routed_away(body):
            continue
        if call.get("attempt_id") not in read_attempts:
            problems.append(
                f"runner GET {call.get('platform')} {call.get('suffix')}"
                f" ({status}, {call['attempt_id']}): no read logged"
            )
    return Checked(
        problems,
        counts,
        unanswered,
        0,
        Counter(last_page.values()),
        matched if calls is not None else None,
    )


def spread(values: list[int]) -> str:
    return f"min {min(values)} median {statistics.median(values):g} max {max(values)}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the participant calls that runs logged as messages."
    )
    parser.add_argument(
        "paths", nargs="+", help="evidence directories, run folders, batches or files"
    )
    parser.add_argument("--json", type=Path, help="write counts and problems here")
    parser.add_argument(
        "--allow-unlogged",
        action="store_true",
        help="pass runs that committed but logged no message (runs made before message logging)",
    )
    args = parser.parse_args()
    given: dict[Path, Path] = {}  # each file or folder reached, as first reached

    def first(path: Path) -> Path:
        return given.setdefault(path.resolve(), path)

    files: list[Path] = []
    folders: set[Path] = set()  # evidence folders holding runner records
    for name in args.paths:
        if not name:
            print("empty path (CANTON_EVIDENCE_DIR not set?)", file=sys.stderr)
            return 2
        path = Path(name)
        if path.is_file():
            files.append(first(path))
        elif path.is_dir():
            files += map(first, sorted(path.rglob("events.jsonl")))
            files += map(first, sorted(path.rglob("implementation-events.json")))
            for pattern in ("call-*.json", "snapshot-*.json"):
                folders.update(first(record.parent) for record in path.rglob(pattern))
        else:
            print(f"{name}: no such file or directory", file=sys.stderr)
            return 2
    events = run_events(list(dict.fromkeys(files)))
    folders.update(
        first(p.parent) for p in events if p.name == "implementation-events.json"
    )
    units: list[Unit] = []
    unchecked: list[dict] = []  # folders of records without an event file to check
    done: set[tuple[str, Path]] = set()
    untraced = 0
    for folder in sorted(folders):
        records, untraced_here = call_records(folder)
        untraced += untraced_here
        run_id = folder_run_id(folder)
        copy = first(folder / "implementation-events.json")
        pairs: list[tuple[str, Path]] = []
        reason = "no event file of the run among the paths"
        if copy in events:
            named = [run for run in events[copy] if not run.startswith("<")]
            run = run_id or (named[0] if len(named) == 1 else None)
            pairs = [(run, copy)] if run else []
            reason = "no run_id above the folder, and not one run in its events"
        elif run_id is not None:  # the adapter's <evidence dir>/<run_id>/events.jsonl
            pairs = [
                (run_id, path)
                for path in events
                if path.name == "events.jsonl" and path.parent.name == run_id
            ]
        if not pairs:
            unchecked.append(
                {
                    "folder": str(folder),
                    "run_id": run_id,
                    "reason": reason,
                    "runner_records": sum(isinstance(r, dict) for r in records),
                    "untraced_snapshots": untraced_here,
                    "problems": [r for r in records if isinstance(r, str)],
                }
            )
            continue
        for run, path in pairs:
            found = events[path].get(run, [])
            units.append(Unit(run, path, found, records, untraced_here, folder))
            done.add((run, path))
    for path, runs in events.items():
        for run, found in runs.items():
            if (run, path) not in done:
                units.append(Unit(run, path, found, None, None, None))
    if not units:
        print("no events.jsonl or implementation-events.json found", file=sys.stderr)
        return 2
    seen_runs = Counter(unit.run_id for unit in units)
    seen_files = Counter((unit.run_id, unit.path) for unit in units)
    report, failed, unlogged = {}, 0, 0
    for unit in sorted(units, key=lambda u: (u.run_id, u.path, str(u.folder))):
        run_id, path = unit.run_id, unit.path
        label = run_id if seen_runs[run_id] == 1 else f"{run_id} ({path})"
        if seen_files[run_id, path] > 1:  # one event file paired with several folders
            label = f"{run_id} ({path}, records {unit.folder})"
        checked = check_run(unit.events, args.allow_unlogged, unit.records)
        failed += bool(checked.problems)
        unlogged += bool(checked.unlogged)
        report[label] = {
            "problems": checked.problems,
            "messages": {"/".join(key): n for key, n in sorted(checked.counts.items())},
            "unanswered": checked.unanswered,
            "unlogged_commits": checked.unlogged,
            "reads_by_pages": {str(n): k for n, k in sorted(checked.reads.items())},
            "runner_calls_matched": None
            if checked.matched is None
            else dict(sorted(checked.matched.items())),
            "untraced_snapshots": unit.untraced,
        }
        line = " ".join(
            f"{purpose}/{kind[:3]}={n}"
            for (_, purpose, kind), n in sorted(
                checked.counts.items(), key=lambda i: i[0][1:]
            )
        )
        if checked.unlogged:
            line = f"(no messages, {checked.unlogged} commits)"
        status = "FAIL" if checked.problems else "none" if checked.unlogged else "ok  "
        print(
            f"{status} {label:<44} {line or '(no messages)'}"
            + (f" unanswered={len(checked.unanswered)}" if checked.unanswered else "")
        )
        for problem in checked.problems[:20]:
            print(f"       {problem}")
        if len(checked.problems) > 20:
            print(f"       ... {len(checked.problems) - 20} more")
    failed_folders = 0
    for entry in unchecked:
        failed_records = bool(entry["problems"])
        failed_folders += failed_records
        print(
            f"{'FAIL' if failed_records else '----'} {entry['run_id'] or '<no run_id>':<44}"
            f" not checked: {entry['reason']} ({entry['folder']})"
        )
        for problem in entry["problems"][:20]:
            print(f"       {problem}")
        if len(entry["problems"]) > 20:
            print(f"       ... {len(entry['problems']) - 20} more")
    with_messages = [r for r in report.values() if r["messages"]]
    print(f"\n{len(report)} runs, {len(with_messages)} with messages, {failed} failed")
    if unlogged:
        print(f"  runs that committed but logged no message: {unlogged}")
    for key in sorted({k for r in with_messages for k in r["messages"]}):
        values = [r["messages"].get(key, 0) for r in with_messages]
        print(f"  {key:<40} total {sum(values):>6}  per run {spread(values)}")
    reads = Counter()
    for r in report.values():
        reads.update({int(n): k for n, k in r["reads_by_pages"].items()})
    if reads:
        print(
            "  state reads by pages: "
            + " ".join(f"{n}p={k}" for n, k in sorted(reads.items()))
        )
    lost = sum(len(r["unanswered"]) for r in report.values())
    print(f"  calls without a logged response: {lost}")
    contexts = [r["runner_calls_matched"] for r in report.values()]
    if any(c is not None for c in contexts):
        total = Counter()
        for c in contexts:
            total.update(c or {})
        print(
            f"  runner calls matched ({sum(c is not None for c in contexts)} runs with"
            " call records): "
            + (" ".join(f"{k}={n}" for k, n in sorted(total.items())) or "none")
        )
    else:
        print("  runner calls matched: not checked (no call records)")
    if untraced:
        print(f"  snapshot files without a trace (a runner before 0b1ee43): {untraced}")
    if unchecked:
        print(
            f"  evidence folders whose records were not checked against events: {len(unchecked)}"
            + (f" ({failed_folders} with problems in the records)" if failed_folders else "")
        )
    if args.json:
        out: dict = dict(report)
        if unchecked:
            out["(not checked)"] = unchecked
        args.json.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return 1 if failed or failed_folders else 0


if __name__ == "__main__":
    sys.exit(main())
