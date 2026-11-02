"""Saved comparison and export continuity (UIUX-P4-06).

A comparison was twenty minutes of work on screen and nothing else. `reload` deleted it. The server
did save a `runs` row — `debate_done` carries its id, and `save_run` returns `0` when the write fails
— but the pane never said so, could not reach it, and the only way to take a comparison out of the
page was to read the screen.

These tests read the inventory, break each rule in a copy of the source so a rule that stopped
looking fails here rather than in production, check the fixture can produce the states the story is
about, and check the captures are the ones the walk wrote.
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
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_continuity_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_continuity_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Continuity.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-continuity.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-continuity"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
HISTORY_EXPORT = PROJECT_ROOT / "history_privacy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_compare_continuity_inventory", INVENTORY)
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

    def _make(html_transform=None, app_transform=None, seed_transform=None, walk_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        seed = SEED.read_text(encoding="utf-8")
        walk = WALK.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
            app = changed
        if seed_transform:
            changed = seed_transform(seed)
            assert changed != seed, "the transform did not change the fixture, so the test proves nothing"
            seed = changed
        if walk_transform:
            changed = walk_transform(walk)
            assert changed != walk, "the transform did not change the walk, so the test proves nothing"
            walk = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        seed_path = tmp_path / "council_readiness_seed.py"
        walk_path = tmp_path / "compare_continuity_walk.mjs"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        seed_path.write_text(seed, encoding="utf-8")
        walk_path.write_text(walk, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        monkeypatch.setattr(inventory, "SEED", seed_path)
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
    assert payload["check_count"] == len(payload["checks"]) >= 20
    assert payload["findings"] == []


def test_the_four_sources_are_the_only_sources(payload: dict):
    assert payload["sources"] == ["live", "browser", "server", "detached"]
    assert payload["surface"] == "#debateRecord"
    assert payload["storage"]["key"] == "m5_debate_comparison"
    assert payload["storage"]["cap_chars"] == 60000


def test_the_server_copy_is_read_from_the_export_that_already_existed(payload: dict):
    assert payload["server_record"] == {
        "route": "GET /api/history/export",
        "table": "tables.runs",
        "match": "id and mode=debate",
        "redacted": True,
    }
    assert payload["routes"] == ["/api/debate/stream", "/api/history/export"]


def test_the_doc_is_generated_by_the_inventory(payload: dict):
    assert DOC_PATH.exists()
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)
    stored = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert stored["story"] == "UIUX-P4-06"
    assert stored["check_count"] == payload["check_count"]


# ── rules that must keep looking ────────────────────────────────────────────


def test_a_bar_with_no_labelled_region_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            '<div id="debateRecord" class="debate-record" role="region"',
            '<div id="debateRecord" class="debate-record"',
            1,
        )
    )
    assert "the pane states where this comparison came from, in a labelled block of its own" in _checks(broken)


def test_a_restored_copy_that_passes_for_a_live_run_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("not the server's record, and not a live run", "fresh", 1)
    )
    assert "the four sources are named, and a restored comparison never passes for a live one" in _checks(broken)


def test_an_unsaved_run_drawn_as_saved_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "<strong>The server did not save this run</strong>",
            "<strong>Saved</strong>",
            1,
        )
    )
    assert "the run's id comes from the response, and a zero id is stated as not saved" in _checks(broken)


def test_a_restored_comparison_that_looks_re_runnable_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("so Start would compare something else", "so Start is ready", 1)
    )
    assert "the bar says which models a restored comparison used, and when the list no longer offers them" in _checks(broken)


def test_a_copy_written_only_on_success_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("debateRecordWrite();", "debateRecordWriteMaybe();", 1)
    )
    assert "the comparison is written when a run ends, whatever way it ended" in _checks(broken)


def test_a_silently_shortened_copy_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("snapshot.truncated = dropped > 0;", "snapshot.truncated = false;", 1)
    )
    assert "the copy is capped, and the truncation is recorded rather than hidden" in _checks(broken)


def test_a_restore_that_forgets_it_holds_the_copy_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'debateLocalKept = source === "browser" ? true : debateLocalKept;',
            "debateLocalKept = false;",
            1,
        )
    )
    assert "the copy is read back on load, and the reader is told it is this browser's" in _checks(broken)


def test_forget_that_claims_the_server_lost_it_too_is_a_finding(sources):
    broken = sources(html_transform=lambda html: html.replace('debateSource = "detached";', 'debateSource = "server";', 1))
    assert "forgetting says what it did not touch, and leaves the pane stating it keeps nothing" in _checks(broken)


def test_a_record_read_back_from_anywhere_else_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'const tables = bundle && bundle.tables ? bundle.tables : {};',
            "const tables = bundle;",
            1,
        )
    )
    assert "the server's copy is read from the export that already exists, by id and mode" in _checks(broken)


def test_a_read_back_that_hides_the_redaction_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "This was <strong>read back from the server's redacted export</strong>",
            "This was <strong>read back from the server</strong>",
            1,
        )
    )
    assert "the read-back is labelled as the redacted export it is" in _checks(broken)


def test_a_reply_status_that_ignores_the_stored_usage_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace('if (String(usage.finish_reason || "") === "length") return "cut";', "", 1)
    )
    assert "a recorded reply's status is derived from the usage the server stored" in _checks(broken)


def test_an_export_that_merges_the_answers_is_a_finding(sources):
    broken = sources(html_transform=lambda html: html.replace("## Arbiter verdict", "## Comparison", 1))
    assert "the markdown export keeps every reply and the arbiter under their own headings" in _checks(broken)


def test_an_export_that_picks_a_winner_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("## Opinion ", "## The winner is Opinion ", 1)
    )
    assert "the export picks no winner, and says so" in _checks(broken)


def test_an_export_with_no_provenance_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("This copy was kept by this browser", "This comparison", 1)
    )
    assert "the export states which copy it is, and whether the server saved it" in _checks(broken)


def test_a_json_export_without_usage_is_a_finding(sources):
    broken = sources(html_transform=lambda html: html.replace("usage: panel.u || null,", "usage: null,", 1))
    assert "the JSON export carries one entry per panel with its usage, and its source" in _checks(broken)


def test_a_silent_export_failure_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace("The clipboard refused; the comparison is on screen", "Copied", 1)
    )
    assert "the export is offered as a copy and as a download, and a refusal is stated" in _checks(broken)


def test_a_new_route_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            "const tables = bundle && bundle.tables ? bundle.tables : {};",
            'await fetch("/api/debate/reset", { method: "POST" });\n      const tables = bundle && bundle.tables ? bundle.tables : {};',
            1,
        )
    )
    assert "the pane touches only the two routes it is allowed to" in _checks(broken)


def test_an_unescaped_prompt_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'escapeHtml(missing.join(", "))',
            'missing.join(", ")',
            1,
        )
    )
    assert "the bar escapes what it draws" in _checks(broken)


def test_a_second_place_claiming_a_live_run_is_a_finding(sources):
    broken = sources(
        html_transform=lambda html: html.replace(
            'debateLocalKept = source === "browser" ? true : debateLocalKept;',
            'debateLocalKept = source === "browser" ? true : debateLocalKept; debateSource = "live";',
            1,
        )
    )
    assert "only a run sets the source back to live" in _checks(broken)


def test_a_server_that_stops_saving_the_run_is_a_finding(sources):
    broken = sources(app_transform=lambda app: app.replace('save_run("debate"', 'save_run("debate_x"', 1))
    assert "the server still saves the comparison it saved before, and the export still carries it" in _checks(broken)


# ── the fixture and the walk ────────────────────────────────────────────────


def test_a_fixture_that_cannot_produce_an_unsaved_run_is_a_finding(sources):
    broken = sources(seed_transform=lambda seed: seed.replace('"--slow-model"', '"--not-a-flag"', 1))
    assert "the fixture can produce a saved run and an unsaved one" in _checks(broken)


def test_a_walk_that_does_not_reload_is_a_finding(sources):
    broken = sources(
        walk_transform=lambda walk: walk.replace("await page.reload(", "await page.goto(", 2)
    )
    assert "the walk reloads, reads the server's copy, forgets it and exports" in _checks(broken)


def test_a_missing_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the captures the doc lists exist and were written by the walk" in _checks(broken)


def test_the_fixture_can_answer_with_two_resident_models():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_continuity", SEED)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    server, state, url = module.start_runtime(resident_model=module.FITS_MODEL)
    try:
        loaded = [mid for mid, meta in state.snapshot().items() if meta["loaded"]]
        assert len(loaded) == 2, loaded
        assert url.startswith("http://127.0.0.1:")
    finally:
        server.shutdown()


def test_the_fixture_can_be_slow_so_a_run_is_stopped_before_it_saves():
    source = SEED.read_text(encoding="utf-8")
    assert "slow_model" in source
    assert "slow" in source.lower()
    # A slow reply is what makes the stopped-and-unsaved state reachable at all.
    assert "time.sleep" in source or "sleep(" in source


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source or "refuses to report" in source
    for token in (
        "debateComparisonMarkdown",
        "debateComparisonJson",
        "GET /api/history/export",
        "compare-record-restored",
        "compare-record-server",
        "compare-record-forgotten",
    ):
        assert token in source, token


# ── the evidence this story ships ───────────────────────────────────────────


def test_the_captures_exist_and_are_the_widths_the_doc_claims(payload: dict):
    readme = (MEDIA / "README.md").read_text(encoding="utf-8")
    doc = DOC_PATH.read_text(encoding="utf-8")
    for name in payload["captures"]:
        path = MEDIA / name
        assert path.exists(), name
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width = struct.unpack(">I", data[16:20])[0]
        assert (width == 480) if "narrow" in name else (400 < width <= 1440), name
        assert name in readme, name
        assert name in doc, name
    assert "Nothing here was drawn by hand" in readme


def test_the_doc_states_the_sources_the_storage_and_the_refusals():
    doc = DOC_PATH.read_text(encoding="utf-8")
    for source in ("live", "browser", "server", "detached"):
        assert source in doc, source
    assert "m5_debate_comparison" in doc
    assert "tables.runs" in doc
    assert "60,000" in doc
    assert "nothing here picks a winner" in doc
    assert "compare_continuity_walk.mjs" in doc


def test_the_ledger_marks_the_story():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next(item for item in ledger["stories"] if item["id"] == "UIUX-P4-06")
    tracking = story["tracking"]
    assert tracking["status"] == "done"
    assert tracking["evidence"] and tracking["test_results"]
    assert tracking["commit_sha"]
    assert ledger["session_state"]["next_recommended_story"] != "UIUX-P4-06"
