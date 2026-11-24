"""A fault relay between one adapter and its participant's JSON API, for the
B2 recovery self-test (b2_stage3_selftest).

The adapter is started with CANTON_LEDGER_URL_<participant> pointing here
(README §4); the relay passes every call to the participant, closing each
connection after its answer so that every call opens its own. Armed with
UNASSIGN or ASSIGN, it drops the next such reassignment call once: the
request is read whole and the connection closed without passing it on (the
adapter sent it, the participant never saw it).

It runs on its own thread and event loop, so the test's blocking calls do not
hold it up. What it did is kept in `log` (and in the file given), without
headers or bodies: time, method, path, the operation of a reassignment call,
status and action.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

REASSIGN_PATH = "/v2/commands/submit-and-wait-for-reassignment"
OPERATIONS = {"UnassignCommand": "UNASSIGN", "AssignCommand": "ASSIGN"}
MAX_BODY = 16 * 1024 * 1024
READ_SECONDS = 120


def operation_of(body: bytes) -> str | None:
    """UNASSIGN or ASSIGN for a reassignment call's body, else None."""
    try:
        command = json.loads(body)["reassignmentCommands"]["commands"][0]["command"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    names = [OPERATIONS[name] for name in command if name in OPERATIONS]
    return names[0] if len(names) == 1 else None


async def read_message(reader: asyncio.StreamReader, *, until_eof: bool):
    """One HTTP/1.1 message: its start line, headers and body. A response is
    read to its end (Connection: close is asked for), a request by its
    Content-Length."""
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    headers = [tuple(line.split(":", 1)) for line in lines[1:] if line]
    headers = [(name.strip(), value.strip()) for name, value in headers]
    length = next(
        (int(value) for name, value in headers if name.lower() == "content-length"),
        None,
    )
    if (
        any(name.lower() == "transfer-encoding" for name, _ in headers)
        and not until_eof
    ):
        raise ValueError("chunked request bodies are not relayed")
    if length is not None:
        if not 0 <= length <= MAX_BODY:
            raise ValueError(f"body of {length} bytes")
        body = await reader.readexactly(length)
    elif until_eof:
        body = await reader.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("response body too large")
    else:
        body = b""
    return lines[0], headers, body


def encode(start: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    kept = [
        (name, value)
        for name, value in headers
        if name.lower()
        not in ("connection", "keep-alive", "content-length", "transfer-encoding")
    ]
    kept += [("Content-Length", str(len(body))), ("Connection", "close")]
    head = start + "\r\n" + "".join(f"{n}: {v}\r\n" for n, v in kept) + "\r\n"
    return head.encode("iso-8859-1") + body


def dechunk(body: bytes) -> bytes:
    """A chunked response body, joined."""
    out, rest = b"", body
    while rest:
        size_line, _, rest = rest.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out, rest = out + rest[:size], rest[size + 2 :]
    return out


class LedgerRelay:
    def __init__(self, upstream: str, port: int, log_path: Path | None = None):
        parts = urlsplit(upstream)
        if parts.scheme != "http" or not parts.hostname or not parts.port:
            raise ValueError(f"upstream must be http://<host>:<port>, got {upstream!r}")
        self.upstream = (parts.hostname, parts.port)
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.log_path = log_path
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.armed: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: asyncio.base_events.Server | None = None
        self.thread: threading.Thread | None = None
        self.calls = 0

    # --- control, from the test's thread ---------------------------------

    def start(self) -> None:
        ready = threading.Event()
        failure: list[BaseException] = []

        def run():
            self.loop = asyncio.new_event_loop()
            try:
                self.loop.run_until_complete(self.listen())
            except BaseException as error:  # reported to start()
                failure.append(error)
                ready.set()
                return
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, name="ledger-relay", daemon=True)
        self.thread.start()
        ready.wait(10)
        if failure:
            raise failure[0]
        if self.server is None:
            raise RuntimeError("the relay did not start")
        self.note("listening", upstream=f"{self.upstream[0]}:{self.upstream[1]}")

    def arm(self, operation: str) -> None:
        if operation not in OPERATIONS.values():
            raise ValueError(f"unknown reassignment operation {operation}")
        with self.lock:
            self.armed = operation
        self.note("armed", operation=operation)

    def disarm(self) -> None:
        with self.lock:
            armed, self.armed = self.armed, None
        if armed is not None:
            self.note("disarmed", operation=armed)

    def pending(self) -> bool:
        """Whether an armed fault has not acted yet."""
        with self.lock:
            return self.armed is not None

    def stop(self) -> None:
        if self.loop is None or self.thread is None:
            return

        async def close():
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(close(), self.loop).result(30)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.note("stopped")

    def note(self, action: str, **fields) -> dict:
        entry = {
            "utc": datetime.now(timezone.utc).isoformat(),
            "mono": round(time.monotonic(), 3),
            "action": action,
            **{k: v for k, v in fields.items() if v is not None},
        }
        with self.lock:
            self.log.append(entry)
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(entry) + "\n")
        return entry

    # --- relay, on its own loop ---------------------------------------------

    async def listen(self) -> None:
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", self.port)

    async def accept(self, reader, writer) -> None:
        try:
            await self.forward(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as error:
            self.note("relay-error", error=type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def take(self, method: str, path: str, body: bytes) -> tuple[bool, str | None]:
        """Whether this call is the armed one (then disarmed), and its
        reassignment operation."""
        operation = (
            operation_of(body) if method == "POST" and path == REASSIGN_PATH else None
        )
        with self.lock:
            if operation is not None and operation == self.armed:
                self.armed = None
                return True, operation
        return False, operation

    async def forward(self, reader, writer) -> None:
        start, headers, body = await asyncio.wait_for(
            read_message(reader, until_eof=False), READ_SECONDS
        )
        method, target, _ = start.split(" ", 2)
        path = target.split("?", 1)[0]
        with self.lock:
            self.calls += 1
            number = self.calls
        drop, operation = self.take(method, path, body)
        fields = {
            "call": number,
            "method": method,
            "path": path,
            "operation": operation,
        }
        if drop:
            self.note("dropped-request", **fields)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                *self.upstream
            )
        except OSError as error:
            # Kept apart from a dropped call: the participant was not reached.
            self.note("upstream-unreachable", error=type(error).__name__, **fields)
            return
        try:
            out = [
                (n, v)
                for n, v in headers
                if n.lower() not in ("connection", "keep-alive")
            ]
            upstream_writer.write(encode(start, out, body))
            await upstream_writer.drain()
            status_line, reply_headers, reply = await asyncio.wait_for(
                read_message(upstream_reader, until_eof=True), READ_SECONDS
            )
        finally:
            upstream_writer.close()
        if any(
            n.lower() == "transfer-encoding" and "chunked" in v.lower()
            for n, v in reply_headers
        ):
            reply = dechunk(reply)
        status = int(status_line.split(" ", 2)[1])
        writer.write(encode(status_line, reply_headers, reply))
        await writer.drain()
        if operation is not None:
            self.note("passed", status=status, **fields)
