"""Start's system health and the project workspace (UIUX-P2-03).

A health card is easy to draw and easy to lie with. The parts a screenshot cannot pin
are the ones that keep it honest: that it renders the telemetry payload the monitor
rail already fetched instead of polling a second time, that every number comes out of
that payload, that a demo payload is labelled ``unverified`` with a claim saying which
rows are still real, that the card keeps its shape when there is nothing to read, and
that the workspace is one setting written through the Harness's own route and followed
by a fresh preflight.

Each rule is broken in a copy of the markup, because a rule that passes because it
stopped looking is the defect it was written to catch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "start_health_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Start_Health.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-start-health.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_start_health_inventory",
        PROJECT_ROOT / "scripts" / "uiux_start_health_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, (
                "the transform did not change the markup, so the test proves nothing"
            )
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, (
                "the transform did not change the server source, so the test proves nothing"
            )
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ──────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_start_health_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Start: system health and the project workspace")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-03"
    assert data["source"]["routes"] == ["/api/telemetry", "/api/workspace/directory"]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the numbers, and where they came from ────────────────────────────────────


def test_the_card_draws_four_rows(payload):
    assert payload["health_rows"] == [
        "Model runtime",
        "Unified memory",
        "Containers",
        "Project workspace",
    ]
    assert payload["totals"]["health_rows"] == 4
    assert payload["frontend"]["health"]["workspace_row"] is True


def test_the_card_reads_the_payload_the_rail_fetched(payload):
    health = payload["frontend"]["health"]
    assert health["reads_last_payload"] is True
    assert health["no_poll"] is True, "the card must not poll: two clocks, two answers"
    assert payload["frontend"]["telemetry"]["feeds_card"] is True


def test_every_number_comes_out_of_the_payload(payload):
    health = payload["frontend"]["health"]
    assert health["runtime_from_payload"] is True
    assert health["memory_from_payload"] is True
    assert health["containers_from_payload"] is True


def test_the_tones_are_the_shared_vocabulary(payload):
    tones = payload["tones_used"]
    assert tones, "the card declares no tones at all"
    for tone in tones:
        assert tone in inventory.TONES, tone
    assert payload["frontend"]["health"]["uses_status_component"] is True


def test_the_read_time_is_a_measurement_not_a_field(payload):
    assert payload["frontend"]["health"]["real_read_time"] is True
    assert "read_at" not in payload["server"]["telemetry_fields"]


# ── the basis: the point of the card ─────────────────────────────────────────


def test_all_three_bases_are_reachable(payload):
    assert payload["bases_used"] == ["measured", "unverified", "unavailable"]
    assert payload["frontend"]["health"]["uses_evidence_component"] is True


def test_the_demo_payload_is_labelled(payload):
    """The demo tour has no model runtime, so this is the state most readers will see."""
    assert payload["server"]["simulated_sources"] == ["simulated_demo"]
    assert payload["frontend"]["health"]["simulated_branch"] is True
    assert payload["frontend"]["health"]["claim_names_real_rows"] is True


def test_the_card_keeps_its_shape_when_it_cannot_read(payload):
    assert payload["frontend"]["health"]["absent_keeps_shape"] is True
    assert payload["frontend"]["telemetry"]["clears_on_failure"] is True


# ── the workspace is one setting ─────────────────────────────────────────────


def test_the_workspace_is_written_through_the_harness_route(payload):
    workspace = payload["frontend"]["workspace"]
    assert workspace["writes_through_saver"] is True
    assert workspace["no_direct_fetch"] is True
    assert workspace["shared_input_list"] is True
    assert payload["server"]["workspace_route"] == "/api/workspace/directory"
    assert payload["server"]["workspace_route_line"]


def test_a_change_is_followed_by_a_fresh_preflight(payload):
    assert payload["frontend"]["workspace"]["rereads_preflight"] is True


def test_a_failure_is_presented_and_says_what_holds(payload):
    workspace = payload["frontend"]["workspace"]
    assert workspace["error_presentation"] is True
    assert workspace["error_says_what_holds"] is True
    assert workspace["error_offers_retry"] is True
    assert payload["frontend"]["server"]["saver_returns_outcome"] is True


def test_both_cards_are_painted_at_startup(payload):
    assert payload["frontend"]["startup"] is True


# ── the walk proves what the story claims ────────────────────────────────────


def test_the_walk_checks_the_card_against_the_payload():
    walk = WALK.read_text(encoding="utf-8")
    assert 'fetch("/api/telemetry")' in walk, (
        "the walk must read the payload itself, not the card's wording"
    )
    assert "simulated_demo" in walk
    assert 'basis === "unverified"' in walk or '=== "unverified"' in walk


def test_the_walk_changes_the_workspace_and_restores_it():
    walk = WALK.read_text(encoding="utf-8")
    assert "startWorkspaceBtn" in walk
    assert "harnessWorkspaceInput" in walk, (
        "the walk must prove the Harness field changed too"
    )
    assert "onboarding/preflight" in walk
    # The restore lives in `finally`, so a crashed walk cannot leave the demo elsewhere.
    finally_block = walk.split("} finally {", 1)[-1]
    assert "/api/workspace/directory" in finally_block
    assert "original" in finally_block


def test_the_walk_refuses_both_routes_to_reach_the_failure_states():
    walk = WALK.read_text(encoding="utf-8")
    assert "breakers.telemetry" in walk and "breakers.workspace" in walk
    assert "unavailable" in walk
    assert "Retry" in walk


# ── negative cases: each rule must be able to fail ───────────────────────────


def test_a_card_that_polls_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const host = document.getElementById("startHealthRows");',
            '    fetch("/api/telemetry");\n    const host = document.getElementById("startHealthRows");',
            1,
        )
    )
    assert "health-polls" in _checks(payload)


def test_a_rail_that_stops_feeding_the_card_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    lastTelemetry = d;\n    startHealthReadAt = Date.now();\n    renderStartHealth();",
            "    lastTelemetry = d;\n    startHealthReadAt = Date.now();",
            1,
        )
    )
    assert "health-disconnected" in _checks(payload)


def test_a_card_that_keeps_a_stale_payload_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      lastTelemetry = null;\n      startHealthReadAt = 0;",
            "      startHealthReadAt = 0;",
            1,
        )
    )
    assert "health-stale" in _checks(payload)


def test_an_invented_number_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace("mem.omlx_gb || 0", "4.8", 1)
    )
    assert "health-invented" in _checks(payload)


def test_a_blind_card_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'const simulated = Boolean(d && d.telemetry_source === "simulated_demo");',
            "const simulated = false;",
            1,
        )
    )
    assert "basis-blind" in _checks(payload)


def test_a_claim_that_overreaches_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "is still this machine's", "is the same thing"
        )
    )
    assert "claim-misleading" in _checks(payload)


def test_a_tone_outside_the_vocabulary_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'tone: "neutral" });', 'tone: "warning" });', 1
        )
    )
    assert "tone-unknown" in _checks(payload)


def test_a_made_up_read_time_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "const readAt = startHealthReadAt ?",
            "const readAt = d.read_at ?",
            1,
        )
    )
    assert "invented-field" in _checks(payload)


def test_a_doubled_recovery_button_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    const actions = document.getElementById("startHealthActions");',
            "    const actions = null;",
            1,
        )
    )
    assert "duplicate-recovery" in _checks(payload)


def test_a_collapsing_row_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      rows.push({ label: "Containers", value: "not measured yet", tone: "neutral" });\n',
            "",
            1,
        )
    )
    assert "shape-collapses" in _checks(payload)


def test_a_card_that_bypasses_the_status_component_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '      setStatus(pill, row.tone, row.tone === "ok" ? "OK"',
            '      pill.textContent = row.tone; void (row.tone === "ok" ? "OK"',
            1,
        )
    )
    assert "status-bypassed" in _checks(payload)


def test_a_card_without_evidence_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    evidenceNote(\n      "startHealthEvidence",',
            '    void (\n      "startHealthEvidence",',
            1,
        )
    )
    assert "evidence-bypassed" in _checks(payload)


def test_a_pane_that_writes_its_own_request_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const result = await saveWorkspaceDirectory(target);",
            '    const result = await fetch("/api/workspace/directory", { method: "POST" });',
            1,
        )
    )
    assert "workspace-split" in _checks(payload)


def test_a_change_without_a_fresh_preflight_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    await refreshOnboarding();\n    await loadStartRecent({ force: true, silent: true });",
            "    await loadStartRecent({ force: true, silent: true });",
            1,
        )
    )
    assert "workspace-split" in _checks(payload)


def test_a_lost_outcome_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      return { ok: true, dir: d.workspace_dir,",
            "      void { ok: true, dir: d.workspace_dir,",
            1,
        )
    )
    assert "outcome-lost" in _checks(payload)


def test_a_swallowed_failure_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    if (!result || !result.ok) {", "    if (false) {", 1
        )
    )
    assert "workspace-split" in _checks(payload)


def test_a_second_workspace_setting_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(', "startWorkspaceInput"', "", 1)
    )
    assert "workspace-split" in _checks(payload)


def test_an_unpainted_card_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "  renderStartWorkspace();\n  renderStartHealth();\n",
            "",
            1,
        )
    )
    assert "not-painted" in _checks(payload)
