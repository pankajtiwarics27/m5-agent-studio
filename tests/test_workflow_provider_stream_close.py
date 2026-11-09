from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest

import app


class BlockingSSE(httpx.AsyncByteStream):
    def __init__(self, first_frame: bytes) -> None:
        self.first_frame = first_frame
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first_frame
        self.entered.set()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_provider_stream_consumer_close_closes_owned_http_body_before_return(
    monkeypatch, wire
):
    if wire == "openai":
        frame = (
            "data: "
            + json.dumps({"choices": [{"delta": {"content": "first"}}]})
            + "\n\n"
        ).encode()
    else:
        frame = (
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "first"},
                }
            )
            + "\n\n"
        ).encode()
    body = BlockingSSE(frame)
    provider = {
        "alias": "fixture-provider",
        "wire": wire,
        "base_url": "https://provider.example.test/v1",
        "effective_key": "fixture-secret",
    }
    monkeypatch.setattr(
        app, "parse_model_route", lambda _spec: ("cloud", provider, "fixture-model")
    )

    async def allow_url(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app, "_validate_outbound_url", allow_url)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            source = app.stream_any_model(
                client,
                "fixture-route",
                [{"role": "user", "content": "hi"}],
                temperature=0.0,
                max_tokens=32,
                reasoning_mode="off",
            )
            first = await asyncio.wait_for(anext(source), timeout=5)
            assert first == {"type": "content", "text": "first"}
            await source.aclose()
            assert body.closed

    asyncio.run(run())
