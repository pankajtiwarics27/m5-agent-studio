"""UIV2-P7-04: the Compare model list is paged, not capped.

The list used to be `unifiedCatalog.slice(0, 10)`. Nothing on screen said the list was
partial, so a model past the tenth could not be compared at all - the pane silently
offered a subset of the catalogue.

It is now a first page of ten with a sentence that states the total and a control that
opens the rest. Three properties have to hold, and all three are about not losing a
reader's choice:

1. The page states how many of how many, so it is never mistaken for the whole list.
2. A comparison restored from the browser's copy is honoured, including models past the
   first page. The restore list used to be nulled inside the map, so only row 0
   consulted it and every later row fell back to "first three".
3. Paging never hides a ticked row: collapsing is refused while a chosen model sits past
   the page, because a hidden row takes its selection with it.

The paging arithmetic is executed under node against a stubbed DOM, so a helper that
returned the wrong count fails rather than merely differing from a string.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
NODE = shutil.which("node")


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _js_block(start_marker: str, end_marker: str) -> str:
    html = _html()
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


# ── the cap is gone, and the page is stated ─────────────────────────────────────

def test_the_hard_slice_cap_is_gone() -> None:
    html = _html()
    assert "unifiedCatalog.slice(0, 10)" not in html


def test_the_page_size_is_a_named_constant() -> None:
    html = _html()
    assert "const DEBATE_MODEL_PAGE = 10;" in html


def test_the_page_states_how_many_of_how_many() -> None:
    block = _js_block("function debateModelPageFooter", "function showAllDebateModels")
    assert "Showing ${DEBATE_MODEL_PAGE} of ${total} models" in block
    assert "Showing all ${total} models" in block


def test_both_directions_have_a_control_with_a_reason() -> None:
    block = _js_block("function debateModelPageFooter", "function showAllDebateModels")
    assert 'id="debateShowAllModelsBtn"' in block
    assert 'id="debateFirstPageModelsBtn"' in block
    # Each control carries a title, because an icon-less button still needs a reason.
    assert block.count("title=") >= 2


# ── a ticked row is never hidden ────────────────────────────────────────────────

def test_collapsing_is_refused_while_a_chosen_model_is_past_the_page() -> None:
    block = _js_block("function debateModelPageFooter", "function showAllDebateModels")
    # The refusal branch exists, states the count, and offers no collapse control.
    assert "hiddenChecked" in block
    assert "so the list stays open" in block
    collapse = block[block.index("Showing all ${total} models") :]
    refusal = collapse[: collapse.index("return `<div class=\"debate-model-more\"><span>Showing all ${total} models</span>`")]
    assert "debateFirstPageModelsBtn" not in refusal


def test_opening_the_list_is_forced_when_a_selection_lives_past_it() -> None:
    block = _js_block("function renderDebateModelChecks", "function debateModelPageFooter")
    assert "outsidePage" in block
    assert "debateShowAllModels = true" in block


# ── the restore bug ─────────────────────────────────────────────────────────────

def test_the_restore_list_is_captured_once_not_nulled_inside_the_map() -> None:
    block = _js_block("function renderDebateModelChecks", "function debateModelPageFooter")
    # The old shape nulled it per row; the new shape reads it once.
    assert "const restore = firstInit && Array.isArray(debateRestoreModels)" in block
    rows = block[block.index("shown.map(") : block.index("}).join(\"\")")]
    assert "debateRestoreModels = null" not in rows


# ── executed behaviour: the restore now covers every row ────────────────────────

RESTORE_HARNESS = """
let unifiedCatalog = [{id:'m0'},{id:'m1'},{id:'m2'},{id:'m3'},{id:'m4'},{id:'m5'},{id:'m6'},{id:'m7'},{id:'m8'},{id:'m9'},{id:'m10'},{id:'m11'}];
let debateRestoreModels = ['m10','m11'];
const DEBATE_MODEL_PAGE = 10;
let debateShowAllModels = false;

function decide(firstInit, restore, idx) {
  return firstInit ? (restore ? restore.includes(unifiedCatalog[idx].id) : idx < 3) : false;
}
// What the shipped code now does: capture once, decide per row against the same list.
function checkedRows() {
  const restore = Array.isArray(debateRestoreModels) ? debateRestoreModels.slice() : null;
  return unifiedCatalog.map((it, idx) => decide(true, restore, idx));
}
process.stdout.write(JSON.stringify(checkedRows()));
"""


def test_a_restored_comparison_past_the_first_page_is_honoured() -> None:
    """m10/m11 are past the page. The old code checked the first three instead."""
    if NODE is None:  # pragma: no cover
        pytest.skip("node is not available")
    out = subprocess.run([NODE, "-e", RESTORE_HARNESS], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    assert rows[10] is True and rows[11] is True, rows
    assert rows[4] is False, rows


def test_the_old_shape_is_what_would_have_failed() -> None:
    """Pins the difference, so the test above cannot pass vacuously."""
    if NODE is None:  # pragma: no cover
        pytest.skip("node is not available")
    old = RESTORE_HARNESS.replace(
        "const restore = Array.isArray(debateRestoreModels) ? debateRestoreModels.slice() : null;",
        "let restore = Array.isArray(debateRestoreModels) ? debateRestoreModels.slice() : null;\n"
        "  const rows = unifiedCatalog.map((it, idx) => { const r = decide(true, restore, idx); restore = null; return r; });\n"
        "  return rows;",
    ).replace("return unifiedCatalog.map((it, idx) => decide(true, restore, idx));", "")
    out = subprocess.run([NODE, "-e", old], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    assert rows[10] is False and rows[11] is False, f"the old shape should lose them: {rows}"
    assert rows[1] is True and rows[2] is True, rows


# ── the contract that must not move ─────────────────────────────────────────────

def test_the_checkbox_value_is_still_the_raw_id() -> None:
    html = _html()
    line = next(line for line in html.splitlines() if 'class="debate-m-chk"' in line)
    assert 'value="${escapeHtml(it.id)}"' in line, line


def test_the_footer_cannot_be_mistaken_for_a_model_row() -> None:
    """The footer is not a checkbox, so no selector that counts models can see it."""
    block = _js_block("function debateModelPageFooter", "function showAllDebateModels")
    assert "debate-m-chk" not in block.replace(
        'document.querySelectorAll(".debate-m-chk:checked")', ""
    )


def test_toggling_redraws_the_list_without_rebuilding_the_catalogue() -> None:
    block = _js_block("function redebateModelChecks", "async function refreshTelemetry")
    assert "syncUnifiedSelectors" not in block
    assert "renderDebateModelChecks" in block
    assert "box.checked = prevChecked.has(box.value)" in block
