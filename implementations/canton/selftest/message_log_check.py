#!/usr/bin/env python3
"""Check the participant calls that runs logged as messages (README §3
"Participant 호출 기록", measurement.md §8.3).

    .venv/bin/python -I selftest/message_log_check.py [--json OUT] [--allow-unlogged] <path> ...

Each <path> is a file or a directory searched for events.jsonl (the adapters'
evidence directory, CANTON_EVIDENCE_DIR or logs/, or one run folder of it) and
for implementation-events.json (the copy the common runner keeps in each case
folder of a batch). For each run it checks that the MESSAGE_SENT/MESSAGE_RECEIVED
events
  schema       pass event.schema.json, as every other event of the run does (an
               event that does not is reported and left out of the checks below)
  ids          are <purpose>:<32 hex>[:p<n>]:req|res with a purpose of the
               adapter's MESSAGE_PURPOSES, each ID once; state reads carry a page
               number, submissions do not
  shape        SENT is a REQUEST from canton-adapter-<P> to that platform's
               participant, RECEIVED a RESPONSE back; the category is the
               purpose's
  attribution  carry the test, attempt, request, bundle and actor of a
               COMMAND_RECEIVED that the same adapter wrote before them; the
               reads that answer a runner GET (result, readiness, snapshot) carry
               no command events but the fields of their query: result the
               request and an institution, readiness the bundle, command type
               and an institution, snapshot none of them
  context      in a batch case folder, each runner call (evidence/call-*.json)
               has an attempt ID of its own; each call's attempt is one of those
               calls on that platform: a submission or a command's read is the
               POST of its request, a GET read is the GET it answers
               (commands/<request_id>, readiness with its bundle and command
               type, snapshot); and every runner GET the adapter answers after
               reading (200, 202, 404) has a logged read, except a result
               answered REJECTED, which may come from the rejection kept on disk,
               and a 404 for a path or method the adapter does not serve
  order        each RECEIVED follows the SENT of its call on the same clock; a
               read's pages are numbered from 1 and page n+1 is sent only after
               page n was received, for the same attempt and purpose
  submit       each INTERNAL_COMMITTED follows the RECEIVED of a submission of its
               request (the adapter writes it from the confirmed response)
and lists the calls whose response was not logged (an answer lost or not read
whole, which the adapter's recovery handles: reported, not a failure).

An event the order checks need but that is missing from the run (the adapter
does not fail a call when writing its event fails) is reported as "not logged",
one left out for failing the schema as such, an event logged in the wrong order
as such. A run that committed but logged no
message at all was made before message logging, or with it broken: it fails
once, or with --allow-unlogged is listed as "none" and passes (for an evidence
directory that also holds older runs).

It prints, per run, the messages by category, purpose and kind, counting message
IDs as tests/conformance/report.py counts N_message, then the spread of each
count over the runs, the state reads (every purpose but submit) by the number
of pages they read, and how many runner calls the context check matched.
--json writes the same counts and the problems to OUT.
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
PAGED = {purpose for purpose in MESSAGE_PURPOSES if purpose != "submit"}
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


class Checked(NamedTuple):
    problems: list[str]
    counts: Counter  # (category, purpose, kind) -> messages
    unanswered: list[str]  # stems of calls without a logged response
    unlogged: int  # commits of a run that logged no message at all
    reads: Counter  # pages -> state reads (of any read purpose) of that many pages
    matched: Counter | None  # runner calls the context check matched, by kind;
    # None when no runner call records were given


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


def runner_calls(path: Path) -> list | None:
    """The runner's call records next to a batch's implementation-events.json,
    or None for an adapter evidence file. A record that cannot be read is kept
    as a problem string."""
    if path.name != "implementation-events.json":
        return None
    calls: list = []
    for record in sorted(path.parent.glob("call-*.json")):
        try:
            call = parse_json(record.read_text())
            if not isinstance(call, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as error:
            calls.append(f"{record}: {error}")
            continue
        calls.append(call)
    return calls


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


def run_events(paths: list[Path]) -> dict[tuple[str, Path], list]:
    """Every event of the given files, by (run_id, file). A file or a line that
    cannot be read as an event is kept as a problem string, under
    ("<unreadable>", file)."""
    runs: dict[tuple[str, Path], list] = defaultdict(list)
    for path in paths:
        try:
            text = file_text(path)
        except (OSError, ValueError) as error:  # CheckError is a ValueError
            runs["<unreadable>", path].append(f"{path}: {error}")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = parse_json(line)
                if not isinstance(event, dict):
                    raise ValueError("not a JSON object")
            except ValueError as error:
                runs["<unreadable>", path].append(f"{path}:{number}: {error}")
                continue
            runs[event.get("run_id", "<no run_id>"), path].append(event)
    return runs


def check_run(
    events: list, allow_unlogged: bool = False, calls: list | None = None
) -> Checked:
    """The checks above for the events of one run; the context check only
    with the runner's call records of the run (runner_calls)."""
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
        if event["message_id"].startswith("submit:"):
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
            elif event["attempt_id"] not in context_seen:
                context_seen.add(event["attempt_id"])
                matched[get_kind(call) or "command"] += 1
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
        if purpose == "submit":
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
    files: list[Path] = []
    for name in args.paths:
        if not name:
            print("empty path (CANTON_EVIDENCE_DIR not set?)", file=sys.stderr)
            return 2
        path = Path(name)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += sorted(path.rglob("events.jsonl"))
            files += sorted(path.rglob("implementation-events.json"))
        else:
            print(f"{name}: no such file or directory", file=sys.stderr)
            return 2
    runs = run_events(files)
    if not runs:
        print("no events.jsonl or implementation-events.json found", file=sys.stderr)
        return 2
    seen_runs = Counter(run_id for run_id, _ in runs)
    report, failed, unlogged = {}, 0, 0
    for run_id, path in sorted(runs):
        label = run_id if seen_runs[run_id] == 1 else f"{run_id} ({path})"
        checked = check_run(runs[run_id, path], args.allow_unlogged, runner_calls(path))
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
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
