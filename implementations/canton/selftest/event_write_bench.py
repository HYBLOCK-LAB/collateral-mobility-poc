#!/usr/bin/env python3
"""Time EventLog.write, the adapters' event write (README §3 "Participant 호출
기록", 기록 비용).

    .venv/bin/python -I selftest/event_write_bench.py [--writes N] [--processes P ...]

For each P (default 1 and 3), P processes, each one EventLog of its own platform,
append N message events (default 5000) to the same <tmp>/<run_id>/events.jsonl,
as the X, Y and Z adapters do in one run. They start together, so with 3 they
write back to back and contend for the file's flock: an upper bound for a run,
where the adapters write between participant calls. Prints the time of one
write (median, p95, p99, max, in µs) per P. Uses a fresh temporary directory and
removes it; the adapters and their evidence are not touched. Exits 1 if a
writer fails or does not report within TIMEOUT seconds.
"""

from __future__ import annotations

import argparse
import multiprocessing
import queue
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

IMPL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(IMPL_DIR / "adapter"))
TIMEOUT = 120  # seconds for all writers to report; the default writes take ~1s


def worker(root, platform, run_id, writes, barrier, results) -> None:
    from server import PLATFORMS, EventLog

    events = EventLog(Path(root), platform, uuid.uuid4().hex[:12])
    participant = PLATFORMS[platform]["participant"]
    fields = {
        "request_id": "req-bench",
        "attempt_id": "attempt-bench",
        "bundle_id": "bundle-bench",
        "actor": "BANK_A",
        "message_kind": "REQUEST",
        "message_category": "BUSINESS_INTEGRATION",
        "source_id": events.producer,
        "destination_id": participant,
    }
    times = []
    barrier.wait()
    for _ in range(writes):
        stem = f"state:{uuid.uuid4().hex}:p1"
        start = time.perf_counter_ns()
        events.write(
            "MESSAGE_SENT", run_id, "BENCH", message_id=f"{stem}:req", **fields
        )
        times.append(time.perf_counter_ns() - start)
    results.put(times)


def main() -> int:
    parser = argparse.ArgumentParser(description="Time the adapters' event write.")
    parser.add_argument("--writes", type=int, default=5000, help="writes per process")
    parser.add_argument("--processes", type=int, nargs="+", default=[1, 3])
    args = parser.parse_args()
    platforms = ("X", "Y", "Z")
    with tempfile.TemporaryDirectory(prefix="event-write-bench-") as root:
        for count in args.processes:
            if not 1 <= count <= len(platforms):
                print(f"--processes {count}: 1 to {len(platforms)}", file=sys.stderr)
                return 2
            run_id = f"run-bench-{count}-{uuid.uuid4().hex[:8]}"
            barrier = multiprocessing.Barrier(count, timeout=TIMEOUT)
            results = multiprocessing.Queue()
            processes = [
                multiprocessing.Process(
                    target=worker,
                    args=(root, p, run_id, args.writes, barrier, results),
                )
                for p in platforms[:count]
            ]
            for process in processes:
                process.start()
            times, reported = [], 0
            deadline = time.monotonic() + TIMEOUT
            while reported < count:
                try:
                    times += results.get(timeout=1)
                    reported += 1
                except queue.Empty:
                    failed = [p.exitcode for p in processes if p.exitcode]
                    if failed or time.monotonic() > deadline:
                        for process in processes:
                            process.terminate()
                            process.join()
                        print(
                            f"{count - reported} writer(s) did not report"
                            f" (exit codes {[p.exitcode for p in processes]})",
                            file=sys.stderr,
                        )
                        return 1
            for process in processes:
                process.join()
                if process.exitcode != 0:
                    print(f"a writer exited {process.exitcode}", file=sys.stderr)
                    return 1
            lines = (Path(root) / run_id / "events.jsonl").read_text().splitlines()
            if len(lines) != count * args.writes:
                print(f"{len(lines)} lines, not {count * args.writes}", file=sys.stderr)
                return 1
            q = statistics.quantiles(times, n=100)
            print(
                f"{count} process(es) x {args.writes} writes: median"
                f" {statistics.median(times) / 1e3:.1f}µs p95 {q[94] / 1e3:.1f}µs"
                f" p99 {q[98] / 1e3:.1f}µs max {max(times) / 1e3:.1f}µs"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
