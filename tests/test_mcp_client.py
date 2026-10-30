"""Contract + mock tests for the extracted MCP wire-protocol boundary.

`mcp_client.adaptive_call` is exercised with a scripted fake transport: no
sockets, no DNS, no TLS. Each test pins one leg of the negotiation state
machine (stateless fast-path, SSE parsing, 405 `/sse` retry, `2025-06-18`
`initialize` handshake, session reuse, and every failure surface). The final
test runs the real application adapter (`studio._mcp_http_rpc`) over an
httpx.MockTransport to prove the adapter -> module integration end to end.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import app as studio
from mcp_client import (
    McpAmbiguousOutcomeError,
    McpTransportError,
    SessionStore,
    TransportResponse,
    adaptive_call,
    build_headers,
    build_stateless_body,
    extract_session_id,
    parse_mcp_payload,
)


def ok(
    result: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    content_type: str = "application/json",
    text: str = "",
    headers: dict[str, str] | None = None,
) -> TransportResponse:
    return TransportResponse(
        status_code=status_code,
        text=text if text else json.dumps({"jsonrpc": "2.0", "id": 2, "result": result or {}}),
        content_type=content_type,
        headers=headers or {},
    )


class ScriptedPost:
    """Fake transport: returns queued responses (or raises) and records every call."""

    def __init__(self, *responses: TransportResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, url: str, headers: Any, body: dict[str, Any], timeout: float) -> TransportResponse:
        self.calls.append({"url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SERVER: dict[str, Any] = {
    "alias": "fixture",
    "url": "https://mcp.example.test/mcp",
    "headers": {},
    "effective_token": "",
    "auth_header": "Authorization",
}


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_stateless_body_carries_meta_and_client_info() -> None:
    body = build_stateless_body("tools/list", {"cursor": "a"}, 7, "9.0.1")
    assert body["jsonrpc"] == "2.0" and body["id"] == 7 and body["method"] == "tools/list"
    assert body["params"]["cursor"] == "a"
    meta = body["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientInfo"] == {"name": "m5-studio", "version": "9.0.1"}
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {}


def test_build_headers_defaults_bearer_and_preserves_prefixed_scheme() -> None:
    base = build_headers(SERVER, "tools/list")
    assert base["Mcp-Method"] == "tools/list"
    assert "Authorization" not in base

    bearer = build_headers({**SERVER, "effective_token": "abc"}, "tools/list")
    assert bearer["Authorization"] == "Bearer abc"

    prefixed = build_headers({**SERVER, "effective_token": "Bearer abc"}, "tools/list")
    assert prefixed["Authorization"] == "Bearer abc"

    custom = build_headers({**SERVER, "effective_token": "abc", "auth_header": "X-Key"}, "tools/call", tool_name="read")
    assert custom["X-Key"] == "abc"
    assert custom["Mcp-Name"] == "read"

    none_hdr = build_headers({**SERVER, "effective_token": "abc", "auth_header": "none"}, "tools/list")
    assert "Authorization" not in none_hdr and "none" not in none_hdr.values()


def test_parse_mcp_payload_json_and_sse_variants() -> None:
    assert parse_mcp_payload('{"jsonrpc":"2.0","id":2,"result":{"x":1}}', "application/json", 200, 2) == {"x": 1}
    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":9,"result":{"y":2}}\n\ndata: [DONE]\n'
    assert parse_mcp_payload(sse, "text/event-stream", 200, 9) == {"y": 2}
    with pytest.raises(McpTransportError, match="no JSON-RPC result"):
        parse_mcp_payload("data: [DONE]\n", "text/event-stream", 200, 2)
    with pytest.raises(McpTransportError, match="Invalid MCP JSON response"):
        parse_mcp_payload("<html>nope</html>", "application/json", 200, 2)
    with pytest.raises(McpTransportError, match=r"MCP RPC error \(-32601\)"):
        parse_mcp_payload('{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"no such tool"}}', "application/json", 200, 2)


def test_extract_session_id_is_case_insensitive() -> None:
    assert extract_session_id({"Mcp-Session-Id": "s1"}) == "s1"
    assert extract_session_id({"mcp-session-id": "s2"}) == "s2"
    assert extract_session_id({"Content-Type": "application/json"}) == ""


# ---------------------------------------------------------------------------
# Negotiation state machine (scripted fake transport)
# ---------------------------------------------------------------------------


def test_stateless_fast_path_success_stores_no_session() -> None:
    post = ScriptedPost(ok({"tools": [{"name": "read"}]}))
    sessions = SessionStore()
    result = run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=sessions, client_version="9.0.1"))
    assert result == {"tools": [{"name": "read"}]}
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["headers"]["MCP-Protocol-Version"] == "2026-07-28"
    assert call["headers"]["Mcp-Method"] == "tools/list"
    assert call["body"]["params"]["_meta"]["io.modelcontextprotocol/clientInfo"]["version"] == "9.0.1"
    assert sessions.get("fixture") == ""


def test_sse_success_on_stateless_path() -> None:
    sse_body = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n'
    post = ScriptedPost(ok(text=sse_body, content_type="text/event-stream"))
    result = run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))
    assert result == {"ok": True}


def test_405_on_sse_endpoint_retries_once_against_mcp() -> None:
    post = ScriptedPost(ok(status_code=405), ok({"tools": []}))
    url = "https://mcp.example.test/sse"
    run(adaptive_call(post, url=url, server={**SERVER, "url": url}, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))
    assert [c["url"] for c in post.calls] == ["https://mcp.example.test/sse", "https://mcp.example.test/mcp"]
    assert post.calls[0]["body"] == post.calls[1]["body"]


def test_session_error_falls_back_to_initialize_handshake_and_caches_sid() -> None:
    init_ok = TransportResponse(
        status_code=200,
        text=json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}),
        content_type="application/json",
        headers={"Mcp-Session-Id": "sid-123"},
    )
    post = ScriptedPost(
        ok(text='{"jsonrpc":"2.0","id":2,"error":{"code":-32600,"message":"Server not initialized"}}'),
        init_ok,
        ok(status_code=202, text=""),
        ok({"tools": [{"name": "read"}]}),
    )
    sessions = SessionStore()
    result = run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=sessions, client_version="9.0.1"))
    assert result == {"tools": [{"name": "read"}]}
    assert len(post.calls) == 4
    init_call, notif_call, final_call = post.calls[1], post.calls[2], post.calls[3]
    assert init_call["body"]["method"] == "initialize"
    assert init_call["body"]["params"]["protocolVersion"] == "2025-06-18"
    assert notif_call["body"]["method"] == "notifications/initialized"
    assert notif_call["headers"]["Mcp-Session-Id"] == "sid-123"
    assert final_call["headers"]["Mcp-Session-Id"] == "sid-123"
    assert "_meta" not in final_call["body"]["params"]
    assert sessions.get("fixture") == "sid-123"


def test_cached_session_skips_handshake_on_next_call() -> None:
    cached_post = ScriptedPost(ok({"tools": []}))
    sessions = SessionStore()
    sessions.set("fixture", "sid-abc")
    run(adaptive_call(cached_post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=sessions, client_version="9.0.1"))
    assert len(cached_post.calls) == 1
    call = cached_post.calls[0]
    assert call["headers"]["Mcp-Session-Id"] == "sid-abc"
    assert "MCP-Protocol-Version" not in call["headers"]
    assert "_meta" not in call["body"]["params"]


def test_handshake_initialize_http_failure_raises() -> None:
    post = ScriptedPost(
        ok(text='{"jsonrpc":"2.0","id":2,"error":{"code":-32600,"message":"Server not initialized"}}'),
        ok(status_code=500, text="boom"),
    )
    with pytest.raises(McpTransportError, match="MCP HTTP 500"):
        run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))


def test_final_call_http_failure_raises_method_context() -> None:
    post = ScriptedPost(
        ok(text='{"jsonrpc":"2.0","id":2,"error":{"code":-32600,"message":"Server not initialized"}}'),
        ok({"protocolVersion": "2025-06-18"}, headers={"Mcp-Session-Id": "s"}),
        ok(status_code=202, text=""),
        ok(status_code=502, text="bad gateway"),
    )
    with pytest.raises(McpTransportError, match="MCP HTTP 502 on `tools/list`"):
        run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))


def test_unauthorized_surfaces_without_handshake() -> None:
    post = ScriptedPost(ok(status_code=401, text="unauthorized"))
    with pytest.raises(McpTransportError, match="MCP HTTP 401 Unauthorized"):
        run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))
    assert len(post.calls) == 1


def test_non_session_rpc_error_is_raised_immediately() -> None:
    post = ScriptedPost(ok(text='{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"Unknown tool"}}'))
    with pytest.raises(McpTransportError, match="Unknown tool"):
        run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))
    assert len(post.calls) == 1


def test_transport_exception_from_post_propagates() -> None:
    post = ScriptedPost(httpx.ConnectError("dns fail"))
    with pytest.raises(httpx.ConnectError):
        run(adaptive_call(post, url=SERVER["url"], server=SERVER, method="tools/list", sessions=SessionStore(), client_version="9.0.1"))


# ---------------------------------------------------------------------------
# Integration: application adapter + module over httpx.MockTransport
# ---------------------------------------------------------------------------


async def _allow_url(*_args: object, **_kwargs: object) -> None:
    return None


def test_app_adapter_stateful_handshake_and_session_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "_validate_outbound_url", _allow_url)
    monkeypatch.setattr(studio, "_MCP_HTTP_SESSIONS", {})
    srv = {"alias": "fixture-mcp", "url": "https://mcp.example.test/mcp", "headers": {}, "effective_token": "", "auth_header": "Authorization"}
    state = {"initializes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        if method == "initialize":
            state["initializes"] += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}}, headers={"Mcp-Session-Id": "sid-77"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if request.headers.get("Mcp-Session-Id") == "sid-77":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "read"}]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32600, "message": "Server not initialized"}})

    async def run_call() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            return await studio._mcp_http_rpc(client, srv, "tools/list")

    first = asyncio.run(run_call())
    assert first == {"tools": [{"name": "read"}]}
    assert state["initializes"] == 1
    assert studio._MCP_HTTP_SESSIONS["fixture-mcp"] == "sid-77"

    second = asyncio.run(run_call())
    assert second == {"tools": [{"name": "read"}]}
    assert state["initializes"] == 1  # session reuse: no second handshake


def test_tools_call_http_5xx_raises_ambiguous_without_handshake_retry() -> None:
    """M5-P1-03: HTTP 5xx on `tools/call` must never fall back to handshake + second `tools/call`."""
    post = ScriptedPost(
        TransportResponse(
            status_code=502,
            text="Bad Gateway after remote write",
            content_type="text/plain",
            headers={},
        )
    )
    with pytest.raises(McpAmbiguousOutcomeError, match="HTTP 502"):
        run(
            adaptive_call(
                post,
                url=SERVER["url"],
                server=SERVER,
                method="tools/call",
                params={"name": "create_ticket", "arguments": {"title": "p1"}},
                sessions=SessionStore(),
                client_version="9.0.1",
                action_id="act-mcp-502",
            )
        )
    assert len(post.calls) == 1
    assert post.calls[0]["headers"]["Idempotency-Key"] == "act-mcp-502"


def test_tools_call_malformed_http_200_raises_ambiguous_without_second_dispatch() -> None:
    """M5-P1-03: Truncated/non-JSON-RPC HTTP 200 on `tools/call` marks outcome ambiguous without re-POSTing."""
    post = ScriptedPost(
        TransportResponse(
            status_code=200,
            text='{"truncated_session_stream": ',
            content_type="application/json",
            headers={},
        )
    )
    with pytest.raises(McpAmbiguousOutcomeError):
        run(
            adaptive_call(
                post,
                url=SERVER["url"],
                server=SERVER,
                method="tools/call",
                params={"name": "mutate_row", "arguments": {"id": 9}},
                sessions=SessionStore(),
                client_version="9.0.1",
            )
        )
    assert len(post.calls) == 1


def test_idempotency_key_header_attached_only_to_tools_call() -> None:
    call_hdrs = build_headers(SERVER, "tools/call", tool_name="write_doc", action_id="act-77")
    list_hdrs = build_headers(SERVER, "tools/list", action_id="act-77")
    assert call_hdrs["Idempotency-Key"] == "act-77"
    assert "Idempotency-Key" not in list_hdrs

