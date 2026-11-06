"""UIV2-23: tool discovery, risk, approval and the test-call surface.

The browser walk in `scripts/mcp_tools_walk.mjs` is what proves the experience on a screen. This
file proves the rules the walk depends on hold in the server itself: a tool is classified from
what the server says about it (and treated as a write when it says nothing), a refused call is
refused with a structured reason instead of an exception, and a write cannot be run from the test
surface without the operator confirming that specific call.
"""

from __future__ import annotations

import importlib.util
import json
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


# ── the inventory, its artifact and the evidence it claims ──────────────────

INVENTORY = PROJECT_ROOT / "scripts" / "uiux_mcp_tools_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Tools.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-3-mcp-tools.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "mcp-tools-uiv2-23"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")


def _load_inventory() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_mcp_tools_inventory", INVENTORY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def inventory() -> ModuleType:
    return _load_inventory()


@pytest.fixture(scope="module")
def payload(inventory: ModuleType) -> dict:
    return inventory.build_inventory()


@pytest.fixture(scope="module")
def inventory_module() -> ModuleType:
    return _load_inventory()


@pytest.fixture
def sources(inventory_module: ModuleType):
    """`build_inventory` over the real sources, with edits made and then undone."""
    originals = {path: path.read_text(encoding="utf-8") for path in (FRONTEND, APP)}

    class Sources:
        def findings(self, *replacements):
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            for path, old, new in replacements:
                edited = path.read_text(encoding="utf-8")
                assert old in edited, f"{path.name} no longer contains {old[:70]!r}"
                path.write_text(edited.replace(old, new, 1), encoding="utf-8")
            try:
                return {entry["check"] for entry in inventory_module.build_inventory()["findings"]}
            finally:
                for path, original in originals.items():
                    path.write_text(original, encoding="utf-8")

    return Sources()


def test_the_inventory_matches_the_source(inventory: ModuleType, payload: dict) -> None:
    assert JSON_PATH.read_text(encoding="utf-8") == inventory.dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload: dict) -> None:
    assert payload["findings"] == []
    assert payload["check_count"] == len(payload["checks"]) >= 40
    assert all(entry["passed"] for entry in payload["checks"])


def test_the_inventory_names_the_four_counters_and_the_four_risks(payload: dict) -> None:
    assert [counter["id"] for counter in payload["counters"]] == ["configured", "discovered", "available", "denied"]
    assert [risk["id"] for risk in payload["risks"]] == ["read", "write", "write_idempotent", "destructive"]


def test_every_refusal_the_page_can_name_is_in_the_inventory(payload: dict) -> None:
    kinds = {entry["kind"] for entry in payload["denials"]}
    assert kinds == {"server_denied", "tool_denied", "unknown_tool", "stdio_gate", "approval_required", "paused_idempotency"}


def test_the_captures_are_the_ones_the_story_claims() -> None:
    for viewport in VIEWPORTS:
        for theme in THEMES:
            assert (MEDIA / f"mcp-tools-list-{viewport}-{theme}.png").exists()
            assert (MEDIA / f"mcp-tools-consent-{viewport}-{theme}.png").exists()
            assert (MEDIA / "before-uiv2-23" / f"mcp-tools-list-{viewport}-{theme}.png").exists()
    assert len(sorted(MEDIA.glob("*.png"))) >= 120
    assert len(sorted((MEDIA / "before-uiv2-23").glob("*.png"))) >= 100


def test_the_ledger_records_the_story_as_done() -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-23")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["progress_percent"] == 100
    assert tracking["commit_sha"]
    assert tracking["visual_evidence"] and tracking["acceptance_evidence"]
    assert tracking["known_regressions"] == []


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_tool_that_is_read_only_without_the_server_saying_so_is_a_finding(sources) -> None:
    findings = sources.findings((FRONTEND, 'if (tool.read_only) return "read";', ""))
    assert "a tool is read-only only when the server says so, and a silent tool is a write" in findings


def test_a_row_without_its_policy_sentence_is_a_finding(sources) -> None:
    findings = sources.findings((FRONTEND, "<strong>When a model asks</strong>", "<strong>When asked</strong>"))
    assert "each row states the risk, the state, what a call does, what happens when a model asks, and its arguments" in findings


def test_a_consent_that_does_not_name_the_tool_is_a_finding(sources) -> None:
    findings = sources.findings(
        (FRONTEND, "I understand this is ${MCP_RISK_NOUNS[risk]} and I want to run ${tool.name} once.", "I understand this writes."),
    )
    assert "anything that is not a read needs a consent that names the tool and cannot be pre-ticked" in findings


def test_a_page_that_always_sends_confirm_write_is_a_finding(sources) -> None:
    findings = sources.findings(
        (FRONTEND, "...(confirmWrite ? { confirm_write: true } : {})", "...(risk !== \"read\" ? { confirm_write: true } : {})"),
    )
    assert "the request carries confirm_write only because the box was ticked" in findings


def test_a_428_drawn_as_a_result_is_a_finding(sources) -> None:
    findings = sources.findings((FRONTEND, "if (r.status === 428 && d.confirmation_required) {", "if (false) {"))
    assert "a 428 is drawn as a refusal that says nothing ran, not as a result" in findings


def test_a_counter_dropped_from_the_summary_is_a_finding(sources) -> None:
    findings = sources.findings((FRONTEND, "denied by the allowlist", "denied"))
    assert "configured, discovered, available and denied are four numbers, each named" in findings


def test_a_denial_that_hides_which_refusal_it_was_is_a_finding(sources) -> None:
    findings = sources.findings((FRONTEND, "const kind = (d.denial && d.denial.kind) || \"denied\";", "const kind = \"denied\";"))
    assert "a 403 is drawn as a denial, with the kind named and the way out given" in findings


def test_an_authorization_echo_that_is_not_written_is_a_finding(sources) -> None:
    findings = sources.findings((APP, '"grant_source": "operator_one_call"', '"grant_source": "model"'))
    assert "a call that runs says what authorized it, instead of implying it" in findings


def test_a_tool_without_its_denied_flag_is_a_finding(sources) -> None:
    findings = sources.findings(
        (APP, 't_copy["denied"] = bool(t_copy.get("name")) and t_copy["name"] not in en_set', 't_copy["allowed"] = True'),
    )
    assert "every tool the API returns states its own risk and whether it is allowed" in findings


def test_a_tightened_auto_policy_is_a_finding(sources) -> None:
    findings = sources.findings((APP, '    if ap == "auto":\n        return False', '    if ap == "auto":\n        return True'))
    assert "the auto approval policy still means what it meant" in findings


def test_a_missing_mcp_route_is_a_finding(sources) -> None:
    findings = sources.findings((APP, '@app.post("/api/mcp/servers/{alias}/sync")', '@app.post("/api/mcp/servers/{alias}/refresh")'))
    assert "the six MCP routes are still there" in findings
