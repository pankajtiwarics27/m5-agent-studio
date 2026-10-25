from __future__ import annotations

import asyncio

import httpx
import pytest

import app as studio


def test_real_loopback_socket_uses_pinned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(studio, "STUDIO_PRIVATE_URL_HOSTS", {"localhost"})
        async with server:
            async with httpx.AsyncClient(transport=studio._PinnedAsyncHTTPTransport(), follow_redirects=False) as client:
                response = await client.get(f"http://localhost:{port}/probe", timeout=4)
                assert response.status_code == 200
                assert response.text == "ok"
        server.close()
        await server.wait_closed()

    asyncio.run(run())
