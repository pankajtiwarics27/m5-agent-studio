"""The one flat frontend script initializes shared tables wherever they were declared, and
boot statements that ran earlier could touch one before its declaration was evaluated.

A comparison this browser kept is restored at load (`initDebatePane`), and the restore reaches
`uiStatus()`. The `UI_STATUS` table used to live in the pure-helper region near the bottom of
the file, so the restore died on a temporal-dead-zone ReferenceError
(`Cannot access 'UI_STATUS' before initialization`) before `refreshTelemetry()` ever ran:
every pane stayed on its placeholder ("Connecting…", empty model pickers) and the header pill
never updated. Fresh browser profiles never kept a comparison, so the crash was invisible until
one was restored. These pins hold the table above the boot path, hold the restore inside its
guard, and pin the standalone bundle (studio.py, generated from index.html) to the same order.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
STUDIO = PROJECT_ROOT / "studio.py"


def _inline_script() -> str:
    html = FRONTEND.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "static/index.html has no inline script"
    return max(blocks, key=len)


def test_ui_status_table_is_declared_above_the_debate_boot_path():
    script = _inline_script()
    table = script.index("const UI_STATUS = {")
    boot = script.index("(function initDebatePane")
    assert 0 < table < boot, "UI_STATUS must be initialized before the Debate boot IIFE runs"


def test_ui_status_table_precedes_the_stored_comparison_restore():
    script = _inline_script()
    table = script.index("const UI_STATUS = {")
    restore = script.index("debateApplySnapshot(keptComparison")
    assert 0 < table < restore, "the table must exist before the stored comparison is drawn"


def test_stored_comparison_restore_cannot_abort_the_boot():
    script = _inline_script()
    iife = script.index("(function initDebatePane")
    end = script.index("\n  })();", iife)
    body = script[iife:end]
    assert "try {" in body, "the restore must be guarded so a failure cannot strand the boot"
    assert "} catch (error)" in body
    assert "The stored comparison could not be restored at load" in body


def test_pure_marker_region_still_exists_for_the_node_tests():
    script = _inline_script()
    begin = script.index("/* M5-P1-07-PURE-BEGIN */")
    end = script.index("/* M5-P1-07-PURE-END */")
    assert begin < end


def test_the_standalone_bundle_carries_the_same_order():
    bundle = STUDIO.read_text(encoding="utf-8")
    table = bundle.index("const UI_STATUS = {")
    boot = bundle.index("(function initDebatePane")
    assert 0 < table < boot, "studio.py is generated from index.html; rebuild it after a frontend change"
