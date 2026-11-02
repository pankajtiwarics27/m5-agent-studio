"""Cross-tab terminology and consistent statuses (UIUX-P5-01).

Every pane named its own states. The same failed run read `FAILED` in the harness badge, `ERROR` a
few lines later, `Check failed` on Start and `failed` in Compare; a stop read `CANCELLED` in one pane
and `stopped by you` in another; a running workflow was drawn green on one path and busy on another,
as `WORKFLOW RUNNING` and `SDLC RUNNING`; and the memory rail shouted its states because the heading
above them was uppercase, so the vocabulary said `ok` and the reader saw `OK`.

These tests read the inventory, break each rule in a copy of the source so a rule that stopped
looking fails here rather than in production, and check the captures are the ones the walk wrote.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "terminology_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_terminology_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Terminology.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-terminology.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "terminology"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_terminology_inventory", INVENTORY)
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
    """Point the inventory at a copy, so a rule can be broken in it."""

    def _make(html_transform=None, walk_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        walk = WALK.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if walk_transform:
            changed = walk_transform(walk)
            assert changed != walk, "the transform did not change the walk, so the test proves nothing"
            walk = changed
        html_path = tmp_path / "index.html"
        walk_path = tmp_path / "terminology_walk.mjs"
        html_path.write_text(html, encoding="utf-8")
        walk_path.write_text(walk, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "WALK", walk_path)
        return inventory.build_inventory()

    return _make


def _checks(broken: dict) -> set[str]:
    return {finding["check"] for finding in broken["findings"]}


# ── the artifact itself ──────────────────────────────────────────────────────


def test_the_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_inventory_is_clean_and_complete(payload: dict):
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 18
    assert payload["findings"] == []
    assert payload["state_count"] >= 20


def test_every_word_is_prose_and_every_tone_is_one_the_component_accepts(payload: dict):
    assert payload["tones"] == ["ok", "warn", "danger", "info", "busy", "neutral"]
    for state, entry in payload["vocabulary"].items():
        assert entry["label"] == entry["label"].lower(), state
        assert entry["tone"] in payload["tones"], state


def test_the_same_outcome_no_longer_has_two_words(payload: dict):
    states = payload["server_states"]
    assert states["error"] == "failed"
    assert states["cancelled"] == "stopped"
    assert payload["vocabulary"]["failed"]["label"] == "failed"
    assert payload["vocabulary"]["stopped"]["label"] == "stopped by you"


def test_the_non_status_exceptions_are_stated_with_a_reason(payload: dict):
    assert payload["not_a_status"]
    for word, reason in payload["not_a_status"].items():
        assert reason.strip(), word
    assert "LOADED" in payload["not_a_status"]


def test_the_doc_is_generated_by_the_inventory(payload: dict):
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)
    assert json.loads(JSON_PATH.read_text(encoding="utf-8"))["story"] == "UIUX-P5-01"


# ── rules that must keep looking ────────────────────────────────────────────


def test_a_shouted_word_back_in_the_table_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '    "waiting-approval": { label: "waiting approval", tone: "warn" },',
            '    "waiting-approval": { label: "WAITING APPROVAL", tone: "warn" },',
            1,
        )
    )
    assert "the vocabulary is lowercase prose, not shouted" in _checks(broken)


def test_two_states_sharing_one_word_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '    "waiting-approval": { label: "waiting approval", tone: "warn" },',
            '    "waiting-approval": { label: "queued", tone: "warn" },',
            1,
        )
    )
    assert "no state is spelled twice, and no two states share a word" in _checks(broken)


def test_a_pane_naming_its_own_word_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'setStatus(summary, "danger", uiStatus("failed").label);',
            'setStatus(summary, "danger", "Check failed");',
            1,
        )
    )
    assert "every status word drawn anywhere is in the vocabulary or stated as not a status" in _checks(broken)


def test_a_word_drawn_in_the_wrong_tone_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'setStatus("harnessActiveStatusBadge", "busy", uiStatus("running").label);',
            'setStatus("harnessActiveStatusBadge", "ok", uiStatus("running").label);',
            1,
        )
    )
    assert "a word carries its tone wherever it is drawn, so a green `running` cannot come back" in _checks(broken)


def test_a_shouted_chip_outside_the_table_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<span id="dockerStateBadge" class="status" data-tone="neutral">checking</span>',
            '<span id="dockerStateBadge" class="status" data-tone="neutral">CHECKING</span>',
            1,
        )
    )
    assert "no status is shouted in capitals while the rest of the vocabulary is prose" in _checks(broken)


def test_an_error_state_renamed_away_from_failed_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('    error: "failed",', '    error: "unavailable",', 1)
    )
    assert "one outcome has one word: `error` is drawn as `failed`, `cancelled` as `stopped by you`" in _checks(broken)


def test_an_unknown_state_mapped_onto_a_known_word_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '      return { cls: "neutral", label: status.replace(/_/g, " ") };',
            '      return { cls: "neutral", label: "ready" };',
            1,
        )
    )
    found = _checks(broken)
    assert "a state the table has never seen keeps the server's own word instead of a wrong one" in found


def test_a_pane_that_stops_drawing_from_the_table_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("function uiStatus(state) {", "function uiStatusHidden(state) {", 1)
    )
    found = _checks(broken)
    assert "`uiStatus()` is the only way a caller gets a word, and an unknown state is a bug" in found


def test_the_compare_pane_spelling_its_own_words_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '    "not-run": { label: "not run", tone: "neutral" },\n  };\n  // One badge per state',
            '    "not-run": { label: "pending", tone: "neutral" },\n  };\n  // One badge per state',
            1,
        )
    )
    found = _checks(broken)
    assert "the Compare pane's ten states are the shared vocabulary, word for word and tone for tone" in found


def test_a_session_row_with_its_own_words_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "const stIcon = harnessStatusBadge(s).label;",
            'const stIcon = s.status === "completed" ? "Done" : s.status;',
            1,
        )
    )
    found = _checks(broken)
    assert "the harness session rows and the harness badge say the same word for the same state" in found


def test_a_waiting_approval_shouted_again_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'setStatus("harnessActiveStatusBadge", uiStatus("waiting-approval").tone, uiStatus("waiting-approval").label);',
            'setStatus("harnessActiveStatusBadge", "warn", "WAITING APPROVAL");',
            1,
        )
    )
    found = _checks(broken)
    assert "a waiting approval is one phrase, on the badge and on every approval card" in found


def test_a_finished_run_renamed_again_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'document.getElementById("councilTotalStatus").textContent = uiStatus("completed").label;',
            'document.getElementById("councilTotalStatus").textContent = "Done";',
            1,
        )
    )
    found = _checks(broken)
    assert "the same roll-up word is used for a finished stage and a finished run" in found


# ── the walk and its captures ───────────────────────────────────────────────


def test_a_walk_that_skips_a_pane_is_a_finding(sources):
    broken = sources(walk_transform=lambda walk: walk.replace('{ tab: "chat", shot: "terminology-chat" },', "", 1))
    assert "the walk visits all six panes by keyboard and reads the words off the DOM" in _checks(broken)


def test_a_missing_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the captures the doc lists exist and were written by the walk" in _checks(broken)


def test_the_captures_are_the_widths_the_readme_claims(payload: dict):
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    for name in payload["captures"]:
        path = MEDIA / name
        assert path.exists(), name
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == ((480, 900) if "narrow" in name else (1440, 900)), name
        assert name in readme, name
    assert "Nothing here was drawn by hand" in readme


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    for token in (
        "This script refuses to report a walk",
        "harnessStatusBadge",
        "DEBATE_STATUS",
        "uiStatus(\"no-such-state\")",
        "textTransform",
        "terminology-narrow",
    ):
        assert token in source, token


def test_the_doc_states_the_vocabulary_the_boundary_and_the_server_states():
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "UI_STATUS" in doc
    assert "stopped by you" in doc
    assert "What is not a status" in doc or "not a status" in doc
    assert "LOADED" in doc
    assert "terminology_walk.mjs" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P5-01")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P5-01"
