from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

import app


class BlockingLocalSSE(httpx.AsyncByteStream):
    def __init__(self, first_frame: bytes) -> None:
        self.first_frame = first_frame
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first_frame
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def test_local_stream_consumer_close_closes_mock_http_body_before_return(monkeypatch):
    frame = (
        "data: " + json.dumps({"choices": [{"delta": {"content": "local"}}]}) + "\n\n"
    ).encode()
    body = BlockingLocalSSE(frame)

    async def online(_client: httpx.AsyncClient) -> bool:
        return True

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    monkeypatch.setattr(
        app, "parse_model_route", lambda _spec: ("omlx", None, "fixture-model")
    )
    monkeypatch.setattr(app, "_omlx_is_online", online)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            source = app.stream_any_model(
                client,
                "fixture-model",
                [{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=32,
                reasoning_mode="off",
            )
            first = await anext(source)
            assert first == {"type": "content", "text": "local"}
            await source.aclose()
            assert body.closed

    asyncio.run(run())
