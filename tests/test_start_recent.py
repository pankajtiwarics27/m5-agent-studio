"""Start's recent work and task handoff (UIUX-P2-02).

Start shows the five newest Harness sessions and offers a composer that carries a task
into the Harness without running it. The parts a screenshot cannot pin are exactly the
ones that keep it honest: that every state the server writes has words, that Start
issues no write against the session API, that the list is bounded, that a row is a
real control, that the launcher makes no request, and that its note quotes the
preflight so a task is not carried into a Run that will refuse.

Each rule is broken in a copy of the markup, because a check that passes because it
stopped looking is the same defect it was written to catch.
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
WALK = PROJECT_ROOT / "scripts" / "start_recent_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Recent_Work.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-recent-work.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_recent_work_inventory", PROJECT_ROOT / "scripts" / "uiux_recent_work_inventory.py"
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
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
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
        [sys.executable, "scripts/uiux_recent_work_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# Recent work and the task handoff")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P2-02"
    assert data["source"]["route"] == "/api/harness/sessions"


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── every state the server writes has words ──────────────────────────────────


def test_the_table_covers_every_harness_status_the_server_writes(payload):
    coverage = payload["status_coverage"]
    assert coverage, "no harness status found in app.py, so this proves nothing"
    assert len(coverage) >= 5
    for row in coverage:
        assert row["tone"], row
        assert row["label"], row
    assert {"idle", "running", "waiting_approval", "completed"} <= {row["status"] for row in coverage}


def test_the_status_extraction_is_scoped_to_harness_sessions():
    """A status column on another table must not be demanded of the harness table."""
    app_source = APP.read_text(encoding="utf-8")
    statuses = {entry["status"] for entry in inventory.server_statuses(app_source)}
    # hall_sessions defaults to 'active'; it is not a harness state.
    assert "active" not in statuses
    assert "idle" in statuses


# ── Start reads, it does not own ─────────────────────────────────────────────


def test_start_reads_only_the_session_list(payload):
    assert payload["frontend"]["routes"] == ["/api/harness/sessions"]
    assert payload["frontend"]["methods"] == []
    assert payload["frontend"]["launcher"]["fetches_nothing"] is True
    assert payload["frontend"]["launcher"]["runs_nothing"] is True


def test_the_list_is_bounded(payload):
    limit = payload["frontend"]["limit"]
    assert limit["declared"] and limit["applied"]
    assert int(limit["value"]) == payload["totals"]["recent_limit"]
    assert 1 <= int(limit["value"]) <= 10


def test_a_row_is_a_control_painted_by_the_shared_table(payload):
    renderer = payload["frontend"]["renderer"]
    assert renderer["button_rows"]
    assert renderer["no_inline_onclick"]
    assert renderer["uses_state_table"]
    assert renderer["paints_status_via_setStatus"]
    assert renderer["no_badge_class"]
    assert renderer["escapes_text"]


def test_both_states_of_the_list_present_why_and_what_to_do(payload):
    kinds = {call["kind"] for call in payload["presentations"]}
    assert kinds == {"empty", "error"}
    for call in payload["presentations"]:
        assert call["has_cause"] and call["has_context"], call
        assert call["actions"], call


def test_the_handoff_does_not_run_anything(payload):
    launcher = payload["frontend"]["launcher"]
    assert launcher["opens_harness"]
    assert launcher["writes_composer"]
    assert launcher["focuses_composer"]


def test_the_handoff_note_quotes_the_preflight(payload):
    hint = payload["frontend"]["hint"]
    assert hint["reads_preflight"] and hint["states_ready"] and hint["names_next_step"]
    assert hint["described_by"] and hint["element"]
    assert hint["refreshed_on_recheck"]


def test_start_loads_the_list_and_refreshes_on_the_way_in(payload):
    assert payload["frontend"]["startup"]["loads_on_start"]
    assert payload["frontend"]["startup"]["refreshes_on_tab"]
    assert all(payload["frontend"]["styles"].values()), [
        name for name, ok in payload["frontend"]["styles"].items() if not ok
    ]


# ── the walk exists and knows what to prove ──────────────────────────────────


def test_the_walk_creates_its_own_state_and_cleans_up():
    source = WALK.read_text(encoding="utf-8")
    assert 'method: "POST"' in source
    assert 'method: "DELETE"' in source
    assert "harnessPromptInput" in source
    assert "harnessSessions.length" in source, "the walk must prove no session was created"
    assert "request.abort" in source, "the walk never refuses the session list"
    assert 'page.keyboard.press("Enter")' in source


def test_the_evidence_is_described_where_it_lives():
    text = (PROJECT_ROOT / "docs" / "media" / "start" / "README.md").read_text(encoding="utf-8")
    assert "start_recent_walk.mjs" in text
    for name in ("recent.png", "handoff.png", "empty.png"):
        assert name in text


# ── negative cases: each rule must be able to fail ───────────────────────────


def test_a_status_without_a_tone_is_caught(sources):
    payload = sources(
        app_transform=lambda text: text.replace(
            '_update_harness_session_state(session_id, status="running")',
            '_update_harness_session_state(session_id, status="teleporting")',
            1,
        )
    )
    assert "status-without-tone" in _checks(payload)


def test_a_write_against_the_session_api_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'const response = await fetch("/api/harness/sessions");',
            'const response = await fetch("/api/harness/sessions", { method: "POST" });',
            1,
        )
    )
    assert "client-owns-sessions" in _checks(payload)


def test_an_unbounded_list_is_caught(sources):
    payload = sources(html_transform=lambda text: text.replace("slice(0, START_RECENT_LIMIT)", "", 1))
    assert "unbounded-list" in _checks(payload)


def test_a_row_that_is_not_a_button_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'const row = document.createElement("button");',
            'const row = document.createElement("div");',
            1,
        )
    )
    assert "row-not-a-control" in _checks(payload)


def test_a_row_with_its_own_vocabulary_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      const badge = harnessStatusBadge(session);",
            "      const badge = { cls: \"info\", label: String(session.status).toUpperCase() };",
            1,
        )
    )
    assert "row-not-a-control" in _checks(payload)


def test_an_empty_state_without_a_cause_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '        cause: "no Harness session has been started on this machine",',
            '        why: "no Harness session has been started on this machine",',
            1,
        )
    )
    assert "presentation-incomplete" in _checks(payload)


def test_a_launcher_that_runs_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    composer.value = text;\n    composer.dispatchEvent(new Event(\"input\"));",
            "    composer.value = text;\n    fetch(\"/api/harness/sessions\", { method: \"POST\", body: JSON.stringify({ title: text }) });\n    composer.dispatchEvent(new Event(\"input\"));",
            1,
        )
    )
    assert "launcher-runs" in _checks(payload) or "client-owns-sessions" in _checks(payload)


def test_a_note_that_ignores_the_preflight_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    const ready = Boolean(onboardingReport && onboardingReport.ready);",
            "    const ready = true;",
            1,
        )
    )
    assert "launcher-hint-hollow" in _checks(payload)


def test_a_list_that_is_never_loaded_is_caught(sources):
    payload = sources(html_transform=lambda text: text.replace("  loadStartRecent();\n", "", 1))
    assert "not-loaded" in _checks(payload)
