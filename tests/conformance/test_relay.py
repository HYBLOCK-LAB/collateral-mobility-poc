import asyncio
import unittest

import httpx

from fault_proxy import ResponseRelay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_timeout_does_not_cancel_target_and_only_relay_is_held(self):
        received = []

        async def upstream(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            received.append(header.split(b"\r\n")[0])
            body = b'{"outcome":"COMMITTED"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        events = []
        async with ResponseRelay(url, .15, events.append) as relay:
            async with httpx.AsyncClient(trust_env=False) as client:
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(.04):
                        await client.post(relay.url + "/runs/test/commands", json={})
                # Recovery goes straight to the target, unaffected by the original hold.
                result = await client.get(url + "/runs/test/commands/original")
                self.assertEqual(result.json()["outcome"], "COMMITTED")
        self.assertEqual(events, ["RESPONSE_HELD", "FAULT_RELEASED"])
        self.assertEqual(relay.response["status_code"], 200)
        self.assertEqual(len(received), 2)
        self.assertIsNone(relay.error)
