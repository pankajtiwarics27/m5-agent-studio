"""UIV2-23: tool discovery, risk, approval and the test-call surface.

The browser walk in `scripts/mcp_tools_walk.mjs` is what proves the experience on a screen. This
file proves the rules the walk depends on hold in the server itself: a tool is classified from
what the server says about it (and treated as a write when it says nothing), a refused call is
refused with a structured reason instead of an exception, and a write cannot be run from the test
surface without the operator confirming that specific call.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "app.py"
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "mcp_tools_walk.mjs"


def _load_studio() -> ModuleType:
    spec = importlib.util.spec_from_file_location("studio_uiv2_23", APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def studio() -> ModuleType:
    return _load_studio()


def _semantics(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        read_only=read_only,
        destructive=destructive,
        idempotency_supported=idempotent,
        requires_approval_override=False,
        idempotency_status="supported" if idempotent else ("not_applicable_read_only" if read_only else "unsupported"),
    )


# ── risk, from what the server says about a tool ────────────────────────────


def test_a_read_only_hint_is_the_only_thing_that_makes_a_tool_a_read(studio: ModuleType) -> None:
    assert studio.mcp_tool_risk_level(_semantics(read_only=True)) == "read"


def test_a_destructive_hint_outranks_every_write_kind(studio: ModuleType) -> None:
    assert studio.mcp_tool_risk_level(_semantics(destructive=True)) == "destructive"
    assert studio.mcp_tool_risk_level(_semantics(destructive=True, idempotent=True)) == "destructive"


def test_a_repeatable_write_is_named_as_one(studio: ModuleType) -> None:
    assert studio.mcp_tool_risk_level(_semantics(idempotent=True)) == "write_idempotent"


def test_a_tool_the_server_says_nothing_about_is_a_plain_write(studio: ModuleType) -> None:
    assert studio.mcp_tool_risk_level(_semantics()) == "write"


def test_every_risk_has_a_label_and_a_consequence_the_page_can_show(studio: ModuleType) -> None:
    for risk in ("read", "write", "write_idempotent", "destructive"):
        assert risk in studio.MCP_RISK_LABELS
        assert len(studio.MCP_RISK_CONSEQUENCES[risk]) > 20


# ── why a call cannot run, before its risk is even considered ───────────────


def _server(**overrides: Any) -> dict[str, Any]:
    base = {
        "alias": "notes",
        "transport": "http",
        "enabled": True,
        "tools": [{"name": "echo_note"}, {"name": "slow_echo"}],
        "enabled_tools": ["echo_note", "slow_echo"],
    }
    base.update(overrides)
    return base


def test_a_disabled_server_is_refused_as_a_disabled_server(studio: ModuleType) -> None:
    kind, message = studio.mcp_call_block_reason(_server(enabled=False), "echo_note")
    assert kind == "server_denied"
    assert "disabled" in message


def test_a_tool_the_server_never_returned_is_an_unknown_tool(studio: ModuleType) -> None:
    kind, _ = studio.mcp_call_block_reason(_server(), "never_heard_of_it")
    assert kind == "unknown_tool"


def test_a_tool_outside_the_allowlist_is_tool_denied(studio: ModuleType) -> None:
    kind, message = studio.mcp_call_block_reason(_server(enabled_tools=["echo_note"]), "slow_echo")
    assert kind == "tool_denied"
    assert "Allow it in Tools" in message


def test_an_allowed_tool_on_an_enabled_server_has_no_block(studio: ModuleType) -> None:
    assert studio.mcp_call_block_reason(_server(), "echo_note") is None


def test_the_stdio_gate_is_refused_before_a_local_command_is_launched(studio: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_ALLOW_MCP_STDIO", False)
    kind, message = studio.mcp_call_block_reason(_server(transport="stdio"), "echo_note")
    assert kind == "stdio_gate"
    assert "disabled by default" in message


def test_the_executor_s_own_refusals_are_classified(studio: ModuleType) -> None:
    assert studio.mcp_denial_kind("Denied: MCP server `notes` is disabled.") == "server_denied"
    assert studio.mcp_denial_kind("Denied: `slow_echo` is not in the server's enabled-tool allowlist.") == "tool_denied"
    assert studio.mcp_denial_kind("Denied: MCP stdio process launch is disabled by default.") == "stdio_gate"
    assert studio.mcp_denial_kind("something else entirely") == "denied"


# ── the route: refusals are structured, writes need confirming ──────────────


@pytest.fixture
def client(studio: ModuleType, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "t" * 40)
    executed: list[dict[str, Any]] = []

    async def _fake_execute(_http, alias, tool, args, **kwargs):  # noqa: ANN001 - mirrors the real signature
        executed.append({"alias": alias, "tool": tool, "args": args, **kwargs})
        return {"ok": True, "output": f"{tool} ran", "diff": "", "elapsed_ms": 7}

    monkeypatch.setattr(studio, "execute_mcp_tool_call", _fake_execute)
    headers = {"Authorization": f"Bearer {'t' * 40}"}
    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as test_client:
        test_client.executed = executed  # type: ignore[attr-defined]
        test_client.headers.update(headers)  # type: ignore[attr-defined]
        yield test_client


def _serve(studio: ModuleType, monkeypatch: pytest.MonkeyPatch, server: dict[str, Any], semantics: SimpleNamespace) -> None:
    monkeypatch.setattr(studio, "get_mcp_server", lambda alias, include_secret=False: dict(server))
    monkeypatch.setattr(studio, "_resolve_mcp_tool_semantics", lambda srv, tool: semantics)


def test_an_unknown_server_is_a_404(studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client) -> None:
    def _missing(alias: str, include_secret: bool = False):  # noqa: ANN001, ARG001
        raise KeyError(f"MCP server `{alias}` not found")

    monkeypatch.setattr(studio, "get_mcp_server", _missing)
    response = client.post("/api/mcp/servers/nope/call", json={"tool": "echo_note", "arguments": {}})
    assert response.status_code == 404


def test_a_tool_outside_the_allowlist_answers_with_the_kind_of_refusal(
    studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client
) -> None:
    _serve(studio, monkeypatch, _server(enabled_tools=["echo_note"]), _semantics(read_only=True))
    response = client.post("/api/mcp/servers/notes/call", json={"tool": "slow_echo", "arguments": {}})
    assert response.status_code == 403
    body = response.json()
    assert body["denial"]["kind"] == "tool_denied"
    assert body["denial"]["alias"] == "notes"
    assert body["denial"]["tool"] == "slow_echo"
    assert not client.executed  # type: ignore[attr-defined]


def test_a_write_without_confirmation_is_asked_to_confirm_and_nothing_runs(
    studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client
) -> None:
    _serve(studio, monkeypatch, _server(), _semantics())
    response = client.post("/api/mcp/servers/notes/call", json={"tool": "slow_echo", "arguments": {"note": "x"}})
    assert response.status_code == 428
    body = response.json()
    assert body["confirmation_required"]["risk"] == "write"
    assert body["confirmation_required"]["risk_label"] == studio.MCP_RISK_LABELS["write"]
    assert body["confirmation_required"]["message"]
    assert not client.executed  # type: ignore[attr-defined]


def test_a_confirmed_write_runs_and_the_response_says_what_authorized_it(
    studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client
) -> None:
    _serve(studio, monkeypatch, _server(), _semantics())
    response = client.post(
        "/api/mcp/servers/notes/call",
        json={"tool": "slow_echo", "arguments": {"note": "x"}, "confirm_write": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    authorization = body["authorization"]
    assert authorization["surface"] == "test call"
    assert authorization["grant_source"] == "operator_one_call"
    assert authorization["confirmed_write"] is True
    assert authorization["risk"] == "write"
    assert client.executed[0]["approved"] is True  # type: ignore[attr-defined]
    assert client.executed[0]["tool"] == "slow_echo"  # type: ignore[attr-defined]


def test_a_read_only_call_needs_no_confirmation(studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client) -> None:
    _serve(studio, monkeypatch, _server(), _semantics(read_only=True))
    response = client.post("/api/mcp/servers/notes/call", json={"tool": "echo_note", "arguments": {"note": "x"}})
    assert response.status_code == 200
    assert response.json()["authorization"]["confirmed_write"] is False


def test_an_ambiguous_side_effect_is_still_a_409(studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client) -> None:
    _serve(studio, monkeypatch, _server(), _semantics())

    async def _duplicate(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise studio.side_effect_ledger.DuplicateSideEffect("cannot be retried blindly")

    monkeypatch.setattr(studio, "execute_mcp_tool_call", _duplicate)
    response = client.post(
        "/api/mcp/servers/notes/call",
        json={"tool": "slow_echo", "arguments": {}, "confirm_write": True},
    )
    assert response.status_code == 409


def test_a_permission_error_at_execution_is_a_structured_denial(
    studio: ModuleType, monkeypatch: pytest.MonkeyPatch, client
) -> None:
    _serve(studio, monkeypatch, _server(), _semantics(read_only=True))

    async def _refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise PermissionError("Denied: MCP server `notes` is disabled.")

    monkeypatch.setattr(studio, "execute_mcp_tool_call", _refuse)
    response = client.post("/api/mcp/servers/notes/call", json={"tool": "echo_note", "arguments": {}})
    assert response.status_code == 403
    assert response.json()["denial"]["kind"] == "server_denied"


# ── the page and the walk that prove it ─────────────────────────────────────


def test_the_tools_tab_is_a_real_tab_with_its_own_panel() -> None:
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'id="mcpTabTools"' in html
    assert 'id="mcpPanelTools"' in html
    assert '["catalog", "connected", "tools", "activity"].includes(tab)' in html


def test_the_page_states_the_risk_before_it_posts_a_write() -> None:
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'id="mcpPlayConsent"' in html
    assert "confirm_write" in html
    assert "confirmation_required" in html


def test_the_walk_covers_every_state_the_story_claims() -> None:
    walk = WALK.read_text(encoding="utf-8")
    for state in ("empty", "list", "filters", "search", "confirm", "error", "loading", "result", "long", "denied", "recovery", "failure", "keyboard"):
        assert f'"{state}"' in walk
    assert "--capture-only" in walk
