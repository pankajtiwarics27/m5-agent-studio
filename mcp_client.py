"""MCP wire-protocol boundary: adaptive HTTP transport, parser, and session state.

AS-012 boundary extraction. This module owns everything protocol-level about
talking to a Streamable-HTTP MCP server:

1. Fast-path: Stateless ``2026-07-28`` (``MCP-Protocol-Version`` + ``_meta`` +
   ``Mcp-Method`` headers) or a cached ``2025`` ``Mcp-Session-Id``.
2. Automatic fallback: ``2025-06-18`` ``initialize`` + ``Mcp-Session-Id`` +
   ``notifications/initialized`` handshake (plus an ``/sse`` -> ``/mcp`` URL
   retry on HTTP 405).

The transport is injected as a single ``post(url, headers, body, timeout)``
callable returning a :class:`TransportResponse`, so the negotiation state
machine can be exercised with scripted mock transports (no sockets, no DNS)
while the application keeps its own validating, redirect-rejecting transport.

URL validation, redirect rejection, and network I/O stay in the application:
this module never opens a connection.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

CLIENT_NAME = "m5-studio"
STATELESS_PROTOCOL_VERSION = "2026-07-28"
STATEFUL_PROTOCOL_VERSION = "2025-06-18"


class McpTransportError(RuntimeError):
    """Raised for MCP protocol/transport failures (subset of RuntimeError for compatibility)."""


class McpRpcError(McpTransportError):
    """Raised when an MCP server returns an explicit JSON-RPC ``error`` object."""


class McpAmbiguousOutcomeError(McpTransportError):
    """Raised when an outbound ``tools/call`` outcome is ambiguous (HTTP 5xx or garbled 200)."""


@dataclass(frozen=True)
class TransportResponse:
    """Transport-agnostic response view; the application adapts its HTTP client to this."""

    status_code: int
    text: str
    content_type: str
    headers: Mapping[str, str]

    @classmethod
    def from_httpx(cls, resp: Any) -> "TransportResponse":
        return cls(
            status_code=int(resp.status_code),
            text=resp.text,
            content_type=(resp.headers.get("content-type") or "").lower(),
            headers={str(k): str(v) for k, v in resp.headers.items()},
        )


Post = Callable[[str, Mapping[str, str], dict[str, Any], float], Awaitable[TransportResponse]]


class SessionStore:
    """Explicit per-alias session-id cache; replaces the previous module global."""

    def __init__(self, backing: dict[str, str] | None = None) -> None:
        self._sessions: dict[str, str] = backing if backing is not None else {}

    def get(self, alias: str) -> str:
        return self._sessions.get(alias, "")

    def set(self, alias: str, session_id: str) -> None:
        self._sessions[alias] = session_id

    def clear(self, alias: str) -> None:
        self._sessions.pop(alias, None)


# ---------------------------------------------------------------------------
# Pure protocol pieces
# ---------------------------------------------------------------------------


def parse_mcp_payload(text: str, content_type: str, status_code: int, expected_id: int) -> dict[str, Any]:
    """Parse a JSON or SSE JSON-RPC response body and return its ``result`` object."""
    ctype = (content_type or "").lower()
    raw_text = (text or "").strip()
    if "text/event-stream" in ctype or raw_text.startswith("event:") or raw_text.startswith("data:"):
        chosen: dict[str, Any] | None = None
        for line in raw_text.splitlines():
            s = line.strip()
            if not s.startswith("data:"):
                continue
            d_str = s[5:].strip()
            if not d_str or d_str == "[DONE]":
                continue
            try:
                pkt = json.loads(d_str)
            except json.JSONDecodeError:
                continue
            if isinstance(pkt, dict):
                if pkt.get("id") == expected_id:
                    chosen = pkt
                    break
                if "result" in pkt or "error" in pkt:
                    chosen = pkt
        if chosen is None:
            raise McpTransportError(f"MCP SSE stream contained no JSON-RPC result: {raw_text[:240]}")
        obj = chosen
    else:
        try:
            obj = json.loads(raw_text)
        except Exception as exc:
            raise McpTransportError(f"Invalid MCP JSON response (HTTP {status_code}): {raw_text[:240]}") from exc

    if not isinstance(obj, dict):
        raise McpTransportError(f"Invalid MCP JSON-RPC envelope (HTTP {status_code}): {raw_text[:240]}")
    if obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else ""
        raise McpRpcError(f"MCP RPC error ({code}): {msg}")
    if "result" not in obj:
        raise McpTransportError(f"MCP JSON-RPC envelope missing result (HTTP {status_code}): {raw_text[:240]}")
    return obj.get("result") or {}


def build_headers(
    server: Mapping[str, Any],
    method: str,
    tool_name: str = "",
    client_version: str = "",
    action_id: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Method": method,
    }
    if tool_name:
        headers["Mcp-Name"] = tool_name
    if action_id and method == "tools/call":
        headers["Idempotency-Key"] = action_id
    for k, v in (server.get("headers") or {}).items():
        headers[str(k)] = str(v)
    tok = (server.get("effective_token") or "").strip()
    ah = (server.get("auth_header") or "Authorization").strip()
    if tok and ah.lower() != "none":
        if ah.lower() == "authorization" and not tok.lower().startswith(("bearer ", "basic ", "token ")):
            headers["Authorization"] = f"Bearer {tok}"
        else:
            headers[ah] = tok
    return headers


def build_stateless_body(method: str, params: Mapping[str, Any] | None, request_id: int, client_version: str) -> dict[str, Any]:
    meta_params = {
        **(dict(params) if params else {}),
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": STATELESS_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": CLIENT_NAME, "version": client_version},
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": meta_params}


def build_initialize_body(client_version: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": STATEFUL_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": client_version},
        },
    }


def extract_session_id(headers: Mapping[str, str]) -> str:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    return lowered.get("mcp-session-id", "")


def retry_url_after_405(url: str) -> str | None:
    """Legacy ``/sse`` endpoints answer 405; retry once against ``/mcp``."""
    if url.rstrip("/").endswith("/sse"):
        return re.sub(r"/sse/?$", "/mcp", url)
    return None


# ---------------------------------------------------------------------------
# Adaptive negotiation state machine
# ---------------------------------------------------------------------------


def _raise_for_auth(resp: TransportResponse, url: str) -> None:
    if resp.status_code in (401, 403):
        raise McpTransportError(f"MCP HTTP {resp.status_code} Unauthorized at `{url}` — check API token / Authorization header.")


def _looks_like_session_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(k in lowered for k in ("init", "session", "protocol", "version", "-32600", "-32602"))


async def adaptive_call(
    post: Post,
    *,
    url: str,
    server: Mapping[str, Any],
    method: str,
    params: Mapping[str, Any] | None = None,
    timeout: float = 25.0,
    sessions: SessionStore | None = None,
    client_version: str = "",
    action_id: str = "",
) -> dict[str, Any]:
    """Execute one MCP RPC through the adaptive stateless/stateful transport.

    ``post`` must apply the application's outbound URL validation and redirect
    rejection for every request (including retries issued from here).
    For ``method == 'tools/call'``, HTTP 5xx or malformed HTTP 200 responses raise
    :class:`McpAmbiguousOutcomeError` immediately rather than re-dispatching a
    second ``tools/call`` after handshake.
    """
    sessions = sessions or SessionStore()
    alias = str(server.get("alias") or "")
    is_tool_call = method == "tools/call"
    tool_name = str((params or {}).get("name") or "") if is_tool_call else ""
    base_headers = build_headers(
        server, method, tool_name=tool_name, client_version=client_version, action_id=action_id
    )
    cached_sid = sessions.get(alias)

    req_id = 2
    if cached_sid:
        h1 = {**base_headers, "Mcp-Session-Id": cached_sid}
        body1: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": dict(params) if params else {}}
    else:
        h1 = {**base_headers, "MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION}
        body1 = build_stateless_body(method, params, req_id, client_version)

    resp1 = await post(url, h1, body1, timeout)
    retry_target = retry_url_after_405(url) if resp1.status_code == 405 else None
    if retry_target:
        url = retry_target
        resp1 = await post(url, h1, body1, timeout)

    if resp1.status_code == 200:
        try:
            return parse_mcp_payload(resp1.text, resp1.content_type, resp1.status_code, req_id)
        except McpRpcError as rpc_err:
            if not _looks_like_session_error(str(rpc_err)):
                raise
        except McpTransportError as parse_err:
            if is_tool_call:
                raise McpAmbiguousOutcomeError(str(parse_err)) from parse_err
            raise
    _raise_for_auth(resp1, url)
    if is_tool_call and resp1.status_code >= 500:
        raise McpAmbiguousOutcomeError(
            f"MCP HTTP {resp1.status_code} on `{method}` (`{tool_name}`): remote outcome is uncertain ({resp1.text[:200]})"
        )

    # Fallback to the 2025 stateful `initialize` + `Mcp-Session-Id` handshake.
    sessions.clear(alias)
    init_headers = build_headers(server, "initialize", client_version=client_version)
    init_resp = await post(url, init_headers, build_initialize_body(client_version), timeout)
    if init_resp.status_code >= 400:
        raise McpTransportError(f"MCP HTTP {init_resp.status_code} from `{url}`: {init_resp.text[:260]}")
    parse_mcp_payload(init_resp.text, init_resp.content_type, init_resp.status_code, 1)

    sid = extract_session_id(init_resp.headers)
    req_headers = build_headers(
        server, method, tool_name=tool_name, client_version=client_version, action_id=action_id
    )
    notif_headers = build_headers(server, "notifications/initialized", client_version=client_version)
    if sid:
        sessions.set(alias, sid)
        req_headers["Mcp-Session-Id"] = sid
        notif_headers["Mcp-Session-Id"] = sid

    try:
        await post(url, notif_headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, 10.0)
    except Exception:
        pass

    final_resp = await post(
        url,
        req_headers,
        {"jsonrpc": "2.0", "id": 3, "method": method, "params": dict(params) if params else {}},
        timeout,
    )
    if final_resp.status_code >= 400:
        if is_tool_call and final_resp.status_code >= 500:
            raise McpAmbiguousOutcomeError(
                f"MCP HTTP {final_resp.status_code} on `{method}` (`{tool_name}`): remote outcome is uncertain ({final_resp.text[:200]})"
            )
        raise McpTransportError(f"MCP HTTP {final_resp.status_code} on `{method}`: {final_resp.text[:260]}")
    try:
        return parse_mcp_payload(final_resp.text, final_resp.content_type, final_resp.status_code, 3)
    except McpRpcError:
        raise
    except McpTransportError as parse_err:
        if is_tool_call:
            raise McpAmbiguousOutcomeError(str(parse_err)) from parse_err
        raise
