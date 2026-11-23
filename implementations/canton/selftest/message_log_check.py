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
               COMMAND_RECEIVED that the same adapter wrote before them
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
count over the runs and the state reads (state, poll, recheck, local-check) by
the number of pages they read. --json writes the same counts and the problems
to OUT.
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
from server import MESSAGE_PURPOSES, PLATFORMS  # noqa: E402

MESSAGES = ("MESSAGE_SENT", "MESSAGE_RECEIVED")
MESSAGE_ID = re.compile(
    r"^(?P<stem>(?P<read>(?P<purpose>[a-z-]+):[0-9a-f]{32})(?::p(?P<page>[1-9][0-9]*))?)"
    r":(?P<end>req|res)$"
)
ATTRIBUTION = ("test_id", "attempt_id", "request_id", "bundle_id", "actor")
PAGED = {purpose for purpose in MESSAGE_PURPOSES if purpose != "submit"}


class Checked(NamedTuple):
    problems: list[str]
    counts: Counter  # (category, purpose, kind) -> messages
    unanswered: list[str]  # stems of calls without a logged response
    unlogged: int  # commits of a run that logged no message at all
    reads: Counter  # pages -> state reads (of any read purpose) of that many pages


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


def check_run(events: list, allow_unlogged: bool = False) -> Checked:
    """The checks above for the events of one run."""
    problems = [e for e in events if isinstance(e, str)]
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
        return Checked(problems, Counter(), [], commits, Counter())
    logged = {e["message_id"] for e in messages}

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
        if attribution not in commands[clock]:
            problems.append(f"{where}: no earlier COMMAND_RECEIVED of its command")
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
                    for key in ATTRIBUTION + ("clock_id",)
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
            origin.get(key) != event.get(key) for key in ATTRIBUTION + ("clock_id",)
        ):
            problems.append(f"{where}: RECEIVED on another clock or command")
        answered.add(stem)
        if purpose == "submit":
            submitted[clock].add(event["request_id"])
    unanswered = sorted(stem for stem in sent if stem not in answered)
    return Checked(problems, counts, unanswered, 0, Counter(last_page.values()))


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
        checked = check_run(runs[run_id, path], args.allow_unlogged)
        failed += bool(checked.problems)
        unlogged += bool(checked.unlogged)
        report[label] = {
            "problems": checked.problems,
            "messages": {"/".join(key): n for key, n in sorted(checked.counts.items())},
            "unanswered": checked.unanswered,
            "unlogged_commits": checked.unlogged,
            "reads_by_pages": {str(n): k for n, k in sorted(checked.reads.items())},
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
    if args.json:
        args.json.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
