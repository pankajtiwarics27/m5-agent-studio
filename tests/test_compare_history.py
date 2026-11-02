"""Regression checks for the additive saved-Compare history browser."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")
WALK = (ROOT / "scripts" / "compare_history_walk.mjs").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    left = source.index(start)
    right = source.index(end, left + len(start))
    return source[left:right]


def test_history_browser_is_reachable_without_replacing_the_current_record_controls() -> None:
    assert '<section id="debateHistoryPanel"' in HTML
    assert 'role="region" aria-labelledby="debateHistoryTitle"' in HTML
    assert 'id="debateHistoryStatus"' in HTML
    assert 'role="status" aria-live="polite"' in HTML
    for control in (
        "debateHistoryFindBtn",
        "debateHistorySelect",
        "debateHistoryLoadBtn",
        "debateHistoryExportBtn",
    ):
        assert f'id="{control}"' in HTML
    # The existing current-run actions from merged P4-06 remain in place.
    for action in ("Copy as markdown", "Download .json", "Read the server&apos;s copy"):
        assert action in HTML


def test_history_reader_uses_only_the_existing_authorized_export_route() -> None:
    history_code = _between(HTML, "const DEBATE_HISTORY_LIST_LIMIT", "function debateBindHistoryControls")
    assert 'fetch("/api/history/export", { cache: "no-store" })' in history_code
    assert "GET /api/history/export" in history_code
    assert "POST /api/history" not in history_code
    assert "debateHistoryBusy" in history_code
    assert '@app.get("/api/history/export")' in APP


def test_history_picker_filters_modes_caps_and_keeps_every_opinion_and_synthesis_separate() -> None:
    history_code = _between(HTML, "const DEBATE_HISTORY_LIST_LIMIT", "function debateBindHistoryControls")
    assert "const DEBATE_HISTORY_LIST_LIMIT = 50" in history_code
    assert 'String(row.mode || "debate") === "debate"' in history_code
    assert ".sort((a, b) => Number(b.id) - Number(a.id))" in history_code
    assert "matches.slice(0, DEBATE_HISTORY_LIST_LIMIT)" in history_code
    assert "record.opinions.map" in history_code
    assert "synthesis: {" in history_code
    assert "judged: DEBATE_JUDGED_DEFAULTS.slice()" in history_code
    assert "The existing server record does not store custom criteria" in history_code
    assert "criteria_missing: true" in history_code


def test_labels_are_text_nodes_and_browser_walk_checks_no_model_or_history_writes() -> None:
    history_code = _between(HTML, "function debateHistoryOption", "function debateSnapshotFromHistoryRow")
    assert "new Option(debateHistoryOption(row), String(Number(row.id)))" in HTML
    assert "innerHTML" not in history_code
    assert "onerror=alert(1)" in WALK
    assert "no model or state-changing request was sent" in WALK
    assert "window.__historyStatus = 503" in WALK


def test_no_new_history_api_or_backend_route_was_added() -> None:
    history_code = _between(HTML, "const DEBATE_HISTORY_LIST_LIMIT", "function debateBindHistoryControls")
    ui_routes = re.findall(r'fetch\("(/api/[^" ]+)"', history_code)
    assert ui_routes == ["/api/history/export"]
    assert '@app.get("/api/history/export")' in APP
