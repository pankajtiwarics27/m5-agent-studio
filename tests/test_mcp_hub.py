"""UIV2-21: the MCP hub's catalog, connected-server dashboard and activity log.

The browser walk in `scripts/mcp_hub_walk.mjs` is what proves the hub on a screen — cards, search,
tabs, a real stdio sync, a real test call and a secret that never reaches the DOM. This file proves
that the rules the walk relies on are still in the source, that breaking one of them becomes a
finding rather than a silent regression, that the fixture the walk needs can speak the protocol,
that the captures are the ones the story claims, that nothing the story may not touch moved, and
that the ledger records it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "mcp_hub_walk.mjs"
FIXTURE = PROJECT_ROOT / "scripts" / "mcp_hub_fixture_server.py"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_mcp_hub_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_MCP_Hub.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-1-mcp-hub.md"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "mcp-hub"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
STATES = ("catalog", "search", "connected", "secrets", "activity", "loading", "long", "error")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("uiux_mcp_hub_inventory", INVENTORY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module() -> ModuleType:
    return _load_module()


@pytest.fixture(scope="module")
def payload(module: ModuleType) -> dict:
    return module.build_inventory()


@pytest.fixture
def sources():
    """`build_inventory` over the real sources, with edits made and then undone."""
    module = _load_module()
    originals = {FRONTEND: FRONTEND.read_text(encoding="utf-8")}

    class Sources:
        def findings(self, *replacements):
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            for path, old, new in replacements:
                edited = path.read_text(encoding="utf-8")
                assert old in edited, f"{path.name} no longer contains {old[:70]!r}"
                path.write_text(edited.replace(old, new, 1), encoding="utf-8")
            try:
                return {entry["check"] for entry in module.build_inventory()["findings"]}
            finally:
                for path, original in originals.items():
                    path.write_text(original, encoding="utf-8")

    return Sources()


# ── the artifact ─────────────────────────────────────────────────────────────


def test_the_inventory_matches_the_source(module, payload):
    assert JSON_PATH.read_text(encoding="utf-8") == module.dumps(payload)
    assert DOC_PATH.read_text(encoding="utf-8") == module.render_doc(payload)


def test_the_inventory_is_clean_and_complete(payload):
    assert payload["findings"] == []
    assert payload["check_count"] == len(payload["checks"]) >= 30
    assert all(entry["passed"] for entry in payload["checks"])


def test_the_hub_sections_are_named(payload):
    assert set(payload["sections"]) == {"catalog", "connected", "activity"}
    assert payload["sections"]["catalog"]["filters"] == ["All", "Remote", "Local", "Needs a key", "Connected"]
    assert payload["sections"]["activity"]["recorder"] == "recordMcpActivity"


def test_the_hub_keeps_every_control_and_route(payload):
    assert len(payload["controls"]) == 20
    assert len(payload["routes"]) == 6


# ── what the walk has to be able to do ───────────────────────────────────────


def test_the_walk_covers_every_state_and_records_its_checks():
    walk = WALK.read_text(encoding="utf-8")
    for state in STATES:
        assert state in walk, f"the walk never mentions the {state} state"
    assert walk.count("record(") >= 30
    assert "mcpCatalogGrid .mcp-card" in walk
    assert "secretInDom" in walk


def test_the_walk_opts_into_stdio_and_a_memory_keyring_only_for_its_own_fixture():
    walk = WALK.read_text(encoding="utf-8")
    assert 'STUDIO_ALLOW_MCP_STDIO: "1"' in walk
    assert "sitecustomize.py" in walk
    assert "keyring.set_password = _set" in walk
    # The opt-in belongs to the fixture process the walk spawns, never to the app's own code.
    assert "STUDIO_ALLOW_MCP_STDIO" not in FRONTEND.read_text(encoding="utf-8")


def test_the_fixture_speaks_the_protocol_the_app_calls():
    fixture = FIXTURE.read_text(encoding="utf-8")
    assert "tools/list" in fixture
    assert "tools/call" in fixture
    assert "isError" in fixture
    for tool in ("echo_note", "fail_always", "slow_echo", "big_output"):
        assert tool in fixture, f"the fixture no longer offers {tool}"


def test_the_walk_can_photograph_the_old_dialog_too():
    walk = WALK.read_text(encoding="utf-8")
    # A "before" set is only honest if the same instrument can run against the pre-hub dialog.
    assert "capture-only" in walk
    assert 'typeof switchMcpTab === "function"' in walk
    before = sorted((MEDIA / "before-uiv2-21").rglob("*.png"))
    assert len(before) >= 16


# ── the rules, broken one at a time ─────────────────────────────────────────


def test_a_chip_row_instead_of_cards_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<article class="mcp-card', '<div class="mcp-chip-row"'),
    )
    assert "a preset is a card, not a chip with a name" in findings


def test_a_card_without_a_key_requirement_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<span class="mcp-pill">${escapeHtml(auth)}</span>', ""),
    )
    assert "every card states its transport, trust, key requirement and policy" in findings


def test_a_trust_badge_that_reads_a_field_the_server_never_sends_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "if (preset.requires_token) return", "if (preset.trust) return"),
    )
    assert "trust is derived from the stored preset, never invented" in findings


def test_a_form_that_is_open_by_default_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<details class="mcp-hub-card mcp-editor" id="mcpEditor">', '<details class="mcp-hub-card mcp-editor" id="mcpEditor" open>'),
    )
    assert "the configuration form is a shut disclosure, not the first thing shown" in findings


def test_two_visible_panels_are_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "      panel.hidden = panel.id !==", "      panel.hidden = panel.id === "),
    )
    assert "switching a tab shows exactly one panel and moves the roving tabindex" in findings


def test_a_search_that_does_not_hide_rows_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "      row.hidden = !hit;", "      void hit;"),
    )
    assert "search narrows the cards and hides the rows that do not match" in findings


def test_a_secret_in_a_renderer_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, '<span class="mcp-pill">${escapeHtml(auth)}</span>', '<span class="mcp-pill">${escapeHtml(preset.auth_token)}</span>'),
    )
    assert "no renderer interpolates a secret into the page" in findings


def test_a_call_that_is_never_recorded_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "      recordMcpActivity({ alias, tool, ok: !!d.ok, ms: d.elapsed_ms,", "      void ({ alias, tool, ok: !!d.ok, ms: d.elapsed_ms,"),
    )
    assert "a test call is recorded with its outcome, its latency and a capped detail" in findings


def test_a_failed_call_that_is_not_recorded_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, "      recordMcpActivity({ alias, tool, ok: false, ms: 0, detail: e.message });", "      void e.message;"),
    )
    assert "a test call is recorded with its outcome, its latency and a capped detail" in findings


def test_an_unlabelled_search_box_is_a_finding(sources):
    findings = sources.findings(
        (FRONTEND, 'aria-label="Search MCP presets, connected servers and tools"', 'aria-label=""'),
    )
    assert "the hub has one search box that names what it searches" in findings


# ── what this story may not change ──────────────────────────────────────────


def test_the_stdio_launch_gate_is_untouched():
    app = APP.read_text(encoding="utf-8")
    assert app.count("STUDIO_ALLOW_MCP_STDIO") >= 2
    assert "MCP stdio process launch is disabled by default" in app


def test_the_six_mcp_routes_are_unchanged():
    app = APP.read_text(encoding="utf-8")
    for route in (
        '@app.get("/api/mcp/servers")',
        '@app.post("/api/mcp/servers")',
        '@app.post("/api/mcp/import-json")',
        '@app.delete("/api/mcp/servers/{alias}")',
        '@app.post("/api/mcp/servers/{alias}/sync")',
        '@app.post("/api/mcp/servers/{alias}/call")',
    ):
        assert route in app


def test_the_workspace_still_has_all_of_its_panes():
    html = FRONTEND.read_text(encoding="utf-8")
    for pane in ("pane-start", "pane-harness", "pane-hall", "pane-council", "pane-debate", "pane-chat"):
        assert f'id="{pane}"' in html


def test_the_top_strip_still_shows_the_mcp_pills():
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'id="activeMcpPills"' in html
    assert 'id="mcpToolsCountBadge"' in html


def test_no_react_or_new_dependency_is_introduced():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "react" not in pyproject.lower()
    html = FRONTEND.read_text(encoding="utf-8")
    assert "react-dom" not in html


# ── the evidence ────────────────────────────────────────────────────────────


def test_the_captures_are_on_disk_for_each_viewport_and_theme():
    pngs = sorted(MEDIA.glob("*.png"))
    assert len(pngs) >= 48
    for viewport in VIEWPORTS:
        for theme in THEMES:
            for state in ("catalog", "search", "connected", "connected-error", "secrets", "activity", "loading", "long", "activity-failed", "activity-health"):
                assert (MEDIA / f"mcp-hub-{state}-{viewport}-{theme}.png").exists(), (
                    f"missing mcp-hub-{state}-{viewport}-{theme}.png"
                )


def test_the_doc_states_each_section_and_state(payload):
    doc = DOC_PATH.read_text(encoding="utf-8")
    for section in payload["sections"]:
        assert section.lower() in doc.lower()
    assert "approval policy" in doc
    assert "search" in doc.lower()


def test_the_ledger_records_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(entry for entry in ledger["stories"] if entry["id"] == "UIV2-21")
    assert story["tracking"]["status"] in {"done", "in_progress"}
    assert story["tracking"]["attempt_count"] >= 1
    assert story["tracking"]["changed_files"]
