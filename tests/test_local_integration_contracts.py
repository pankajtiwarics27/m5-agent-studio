from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app as studio


def _sse(payloads: list[dict[str, object]]) -> bytes:
    return b"".join(("data: " + json.dumps(item) + "\n\n").encode() for item in payloads) + b"data: [DONE]\n\n"


def test_openai_compatible_local_provider_stream_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    async def allow_url(*_args: object, **_kwargs: object) -> None:
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
            {"choices": [{"delta": {"content": "hello"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 1}},
        ]))

    monkeypatch.setattr(studio, "_validate_outbound_url", allow_url)

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            provider = {"alias": "local-fixture", "base_url": "http://127.0.0.1:8000/v1", "effective_key": ""}
            return [item async for item in studio._stream_cloud_openai(client, provider, "fixture-model", [{"role": "user", "content": "hi"}], 0, 32)]

    chunks = asyncio.run(run())
    assert any(c.get("type") == "content" and c.get("text") == "hello" for c in chunks)
    assert chunks[-1]["type"] == "usage"
    assert len(seen) == 1 and seen[0].url.path.endswith("/chat/completions")


def test_omlx_local_completion_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    monkeypatch.setattr(studio, "OMLX_BASE", "http://127.0.0.1:8000")

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
            {"choices": [{"delta": {"content": "local reply"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}},
        ]))

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return [item async for item in studio._stream_omlx_completion(client, "fixture-model", [{"role": "user", "content": "hi"}], max_tokens=20)]

    chunks = asyncio.run(run())
    assert any(c.get("type") == "content" and c.get("text") == "local reply" for c in chunks)
    assert len(seen) == 2


def test_mcp_http_rpc_modern_contract_and_redirect_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    seen: list[httpx.Request] = []
    srv = {"alias": "fixture-mcp", "url": "https://mcp.example.test/mcp", "headers": {}, "effective_token": "", "auth_header": "Authorization"}

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read"}]}})

    async def run_ok() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler), follow_redirects=False) as client:
            return await studio._mcp_http_rpc(client, srv, "tools/list")

    result = asyncio.run(run_ok())
    assert result == {"tools": [{"name": "read"}]}
    assert seen[0].headers["MCP-Protocol-Version"] == "2026-07-28"

    redirect_calls: list[httpx.Request] = []

    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirect_calls.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9000/secret"})

    async def run_redirect() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler), follow_redirects=False) as client:
            await studio._mcp_http_rpc(client, srv, "tools/list")

    with pytest.raises(RuntimeError, match="Redirect from"):
        asyncio.run(run_redirect())
    assert len(redirect_calls) == 1


async def _allow_url(*_args: object, **_kwargs: object) -> None:
    return None
