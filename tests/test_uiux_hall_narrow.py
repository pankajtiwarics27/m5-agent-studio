"""UIV2-P7-09: the Council pane has to stay reachable at a 195px effective viewport.

At 390x844 with 200% zoom the CSS viewport is 195px. The accessibility walk measured three
controls outside a clipped ancestor, all in this one pane:

* ``#hallClearAllBtn`` — ``.hall-side-head`` laid out 359px of content in a 195px box inside
  ``.hall-sidebar { overflow: hidden }``, so a destructive control was unreachable by pointer and
  out of view for keyboard focus.
* ``#hallSeatCoderStateAction`` / ``#hallSeatJudgeStateAction`` — ``.council-seat-grid`` kept two
  columns at 195px, leaving each seat cell ~90px, narrower than its own readiness text and action.

Both are pinned here. The first fix attempt targeted ``.role-grid`` and changed nothing: that
selector is the *Pipeline* pane's grid, and the tab naming (Council=hall, Pipeline=council) is
exactly the trap that makes this worth a test. ``.council-seat-grid`` is the Council seat grid.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "static" / "index.html"
HTML = FRONTEND.read_text(encoding="utf-8")


def _narrow_blocks() -> list[str]:
    """Every @media (max-width: 390px) block, in source order."""
    blocks, i = [], 0
    while True:
        start = HTML.find("@media (max-width: 390px)", i)
        if start == -1:
            return blocks
        depth, j = 0, HTML.index("{", start)
        for k in range(j, len(HTML)):
            if HTML[k] == "{":
                depth += 1
            elif HTML[k] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(HTML[start : k + 1])
                    i = k
                    break


NARROW = "\n".join(_narrow_blocks())


def test_the_sidebar_head_scrolls_its_actions_instead_of_clipping_them() -> None:
    rule = re.search(r"\.hall-side-head \{([^}]*)\}", NARROW)
    assert rule, "the Council sidebar head needs a narrow rule"
    assert "overflow-x: auto" in rule.group(1)
    assert "overflow-y: hidden" in rule.group(1)
    # Without this the flex child shrinks and there is nothing to scroll to.
    assert re.search(r"\.hall-side-head > div \{[^}]*min-width: max-content", NARROW)


def test_the_council_seat_grid_drops_to_one_column() -> None:
    assert re.search(r"\.council-seat-grid \{[^}]*grid-template-columns: minmax\(0, 1fr\)", NARROW), (
        "at 195px two seat columns leave each cell narrower than its own text and action"
    )


def test_the_rule_names_the_council_grid_and_not_the_pipeline_grid() -> None:
    """The tab naming invites this exact mistake; the fix must target the Council grid."""
    assert "#pane-council .council-seat-grid" not in NARROW
    assert ".council-seat-grid" in HTML
    # .role-grid belongs to the Pipeline pane and must not be repurposed for Council.
    hall = HTML[HTML.index('id="pane-hall"') : HTML.index('id="pane-council"')]
    assert 'class="council-seat-grid"' in hall
    assert 'class="role-grid"' not in hall


def test_the_imported_evidence_is_all_green() -> None:
    import json

    run = json.loads(
        (FRONTEND.parents[1] / "product" / "M5_2_0_UIUX_V2_Accessibility_Adversarial_Run.json").read_text()
    )
    totals = run.get("totals") or run.get("run", {}).get("totals")
    assert totals, "the accessibility run record must carry totals"
    assert totals["failed"] == 0, totals
    assert totals["passed"] == totals["checks"], totals
