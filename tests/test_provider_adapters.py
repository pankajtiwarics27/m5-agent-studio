from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import app as studio


_PROVIDER_SECRET = "fixture-provider-secret-do-not-leak"
_TOOL_MARKUP = '<tool_call>{"name":"lookup","arguments":{"id":"42"}}</tool_call>'


def _openai_sse(payloads: list[dict[str, Any]]) -> bytes:
    frames = [f"data: {json.dumps(payload)}\n\n" for payload in payloads]
    return ("".join(frames) + "data: [DONE]\n\n").encode()


def _anthropic_sse(payloads: list[dict[str, Any]]) -> bytes:
    return b"".join(("event: message\ndata: " + json.dumps(payload) + "\n\n").encode() for payload in payloads)


def _provider(wire: str) -> dict[str, Any]:
    return {
        "alias": wire,
        "wire": studio.PROVIDER_PRESETS[wire]["wire"],
        "base_url": "https://provider.example.test/v1",
        "effective_key": _PROVIDER_SECRET,
    }


def _set_route(monkeypatch: pytest.MonkeyPatch, provider: dict[str, Any]) -> None:
    monkeypatch.setattr(studio, "parse_model_route", lambda _spec: ("cloud", provider, "fixture-model"))


def _response_for(wire: str) -> httpx.Response:
    if wire == "openai":
        body = _openai_sse(
            [
                {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
                {"choices": [{"delta": {"content": f"Answer. {_TOOL_MARKUP}"}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                    "usage": {"completion_tokens": 4},
                },
            ]
        )
    else:
        body = _anthropic_sse(
            [
                {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "thinking"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": f"Answer. {_TOOL_MARKUP}"}},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
            ]
        )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.mark.parametrize("wire", ["openai", "anthropic"], ids=["openai-compatible", "anthropic-messages"])
def test_provider_adapters_share_stream_tool_and_request_contract(
    wire: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(wire)
    _set_route(monkeypatch, provider)
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response_for(wire)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a test record.",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        }
    ]
    messages = studio._normalize_gateway_messages_and_tools([{"role": "user", "content": "Look up 42."}], tools)

    async def collect() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return [
                event
                async for event in studio.stream_any_model(
                    client, "fixture-route", messages, temperature=0.2, max_tokens=128, reasoning_mode="off"
                )
            ]

    events = asyncio.run(collect())
    assert len(seen) == 1
    assert _PROVIDER_SECRET not in str(seen[0].url)
    assert _PROVIDER_SECRET.encode() not in seen[0].content
    assert seen[0].headers["content-type"] == "application/json"
    if wire == "openai":
        assert seen[0].url.path == "/v1/chat/completions"
        assert seen[0].headers["authorization"] == f"Bearer {_PROVIDER_SECRET}"
        request_messages = json.loads(seen[0].content)["messages"]
        assert "lookup" in request_messages[0]["content"]
    else:
        assert seen[0].url.path == "/v1/messages"
        assert seen[0].headers["x-api-key"] == _PROVIDER_SECRET
        request_body = json.loads(seen[0].content)
        assert "lookup" in request_body["system"]
        assert request_body["messages"][0]["content"] == "Look up 42."

    reasoning = "".join(str(event["text"]) for event in events if event["type"] == "reasoning")
    content = "".join(str(event["text"]) for event in events if event["type"] == "content")
    usage = next(event for event in events if event["type"] == "usage")
    cleaned, tool_calls = studio.extract_tool_calls(content)

    assert reasoning == "thinking"
    assert cleaned == "Answer."
    assert tool_calls == [{"name": "lookup", "arguments": {"id": "42"}}]
    assert usage["completion_tokens"] == 4
    assert usage["finish_reason"] == "tool_calls"
    assert {"decode_tps", "prefill_tps", "ttft", "completion_tokens", "reasoning_tokens", "cached_tokens", "finish_reason", "elapsed_s"} <= usage.keys()
    assert _PROVIDER_SECRET not in repr(events)


@pytest.mark.parametrize("wire", ["openai", "anthropic"], ids=["openai-compatible", "anthropic-messages"])
def test_provider_adapters_share_safe_http_error_contract(wire: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(wire)
    _set_route(monkeypatch, provider)
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": _PROVIDER_SECRET}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            async for _ in studio.stream_any_model(client, "fixture-route", [{"role": "user", "content": "hi"}]):
                pass

    with pytest.raises(studio.ProviderAdapterError) as error:
        asyncio.run(run())
    assert isinstance(error.value, studio.ProviderAdapterError)
    assert isinstance(error.value, RuntimeError)
    assert error.value.provider_alias == provider["alias"]
    assert error.value.category == "http_error"
    assert error.value.status_code == 429
    assert _PROVIDER_SECRET not in str(error.value)


@pytest.mark.parametrize("wire", ["openai", "anthropic"], ids=["openai-compatible", "anthropic-messages"])
def test_provider_adapters_share_safe_stream_error_contract(wire: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(wire)
    _set_route(monkeypatch, provider)
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    if wire == "openai":
        payload = _openai_sse([{"error": {"message": _PROVIDER_SECRET}}])
    else:
        payload = _anthropic_sse([{"type": "error", "error": {"message": _PROVIDER_SECRET}}])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            async for _ in studio.stream_any_model(client, "fixture-route", [{"role": "user", "content": "hi"}]):
                pass

    with pytest.raises(studio.ProviderAdapterError) as error:
        asyncio.run(run())
    assert error.value.category == "stream_error"
    assert error.value.status_code is None
    assert _PROVIDER_SECRET not in str(error.value)


@pytest.mark.parametrize("wire", ["openai", "anthropic"], ids=["openai-compatible", "anthropic-messages"])
def test_provider_adapters_share_safe_transport_error_contract(wire: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(wire)
    _set_route(monkeypatch, provider)
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(_PROVIDER_SECRET, request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            async for _ in studio.stream_any_model(client, "fixture-route", [{"role": "user", "content": "hi"}]):
                pass

    with pytest.raises(studio.ProviderAdapterError) as error:
        asyncio.run(run())
    assert error.value.category == "transport_error"
    assert _PROVIDER_SECRET not in str(error.value)


def test_unknown_provider_wire_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = {"alias": "fixture-unknown", "wire": "unknown-wire", "base_url": "https://provider.example.test/v1", "effective_key": ""}
    _set_route(monkeypatch, provider)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            async for _ in studio.stream_any_model(client, "fixture-route", [{"role": "user", "content": "hi"}]):
                pass

    with pytest.raises(RuntimeError, match="Unsupported provider wire: unknown-wire"):
        asyncio.run(run())


async def _allow_url(*_args: object, **_kwargs: object) -> None:
    return None
