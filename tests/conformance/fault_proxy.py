"""One local response relay for AC-11; target processing survives client disconnect."""

import asyncio

import httpx


class ResponseRelay:
    def __init__(self, upstream, hold_seconds, event):
        self.upstream, self.hold_seconds, self.event = upstream, hold_seconds, event
        self.tasks = set()
        self.response = None
        self.error = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    def accept(self, reader, writer):
        task = asyncio.create_task(self.forward(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def forward(self, reader, writer):
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            length = int(next((v for k, v in headers.items() if k.lower() == "content-length"), "0"))
            if method != "POST" or not path.endswith("/commands") or not 0 <= length <= 1048576:
                raise ValueError("invalid relay request")
            body = await reader.readexactly(length)
            headers = {k: v for k, v in headers.items() if k.lower() not in
                       ("host", "connection", "content-length", "transfer-encoding")}
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
                async with asyncio.timeout(5):
                    response = await client.post(self.upstream.rstrip("/") + path,
                                                 headers=headers, content=body)
            self.response = {"status_code": response.status_code, "body": response.text}
            self.event("RESPONSE_HELD")
            await asyncio.sleep(self.hold_seconds)
            self.event("FAULT_RELEASED")
            encoded = response.content
            writer.write(f"HTTP/1.1 {response.status_code} Relayed\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.error = type(error).__name__
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*self.tasks)
