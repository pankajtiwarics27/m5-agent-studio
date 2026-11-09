"""UIV2-P7-09: the Council pane has to stay reachable at a 195px effective viewport.

At 390x844 with 200% zoom the CSS viewport is 195px. The accessibility walk measured three
controls outside a clipped ancestor, all in this one pane:

* ``#hallClearAllBtn`` — ``.hall-side-head`` laid out 359px of content in a 195px box inside
  ``.hall-sidebar { overflow: hidden }``, so a destructive control was unreachable by pointer and
  out of view for keyboard focus.
* ``#hallSeatCoderStateAction`` / ``#hallSeatJudgeStateAction`` — ``.council-seat-grid`` kept two
  columns at 195px, leaving each seat cell ~90px, narrower than its own readiness text and action.

The second fix was first written as a ``max-width: 390px`` rule, which also caught a 390px phone
*without* zoom - where the pane is full width, two columns are ~175px, and nothing is squeezed. It
doubled an open "Council role models" panel above the transcript there (measured at 390x844: the
Hall transcript moved 480.19px down, two extra seat rows less the 48px the folded header
reclaimed). The rule now keys on ``max-width: 340px``: still narrower than the 195px effective
viewport it exists for, and still covering 320px phones, but leaving 360px and 390px phones with
two columns. Both halves are pinned here so the rule cannot silently drift back up.

Both are pinned here. The first fix attempt targeted ``.role-grid`` and changed nothing: that
selector is the *Pipeline* pane's grid, and the tab naming (Council=hall, Pipeline=council) is
exactly the trap that makes this worth a test. ``.council-seat-grid`` is the Council seat grid.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "static" / "index.html"
HTML = FRONTEND.read_text(encoding="utf-8")


def _narrow_blocks(width: int = 390) -> list[str]:
    """Every @media (max-width: <width>px) block, in source order."""
    blocks, i = [], 0
    needle = f"@media (max-width: {width}px)"
    while True:
        start = HTML.find(needle, i)
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


NARROW = "\n".join(_narrow_blocks(390))
#: The width the seat grid actually collapses at (UIV2-P7-09, revised).
NARROWEST = "\n".join(_narrow_blocks(340))


def test_the_sidebar_head_scrolls_its_actions_instead_of_clipping_them() -> None:
    rule = re.search(r"\.hall-side-head \{([^}]*)\}", NARROW)
    assert rule, "the Council sidebar head needs a narrow rule"
    assert "overflow-x: auto" in rule.group(1)
    assert "overflow-y: hidden" in rule.group(1)
    # Without this the flex child shrinks and there is nothing to scroll to.
    assert re.search(r"\.hall-side-head > div \{[^}]*min-width: max-content", NARROW)


def test_the_council_seat_grid_drops_to_one_column_at_the_narrowest_width() -> None:
    rule = re.search(r"\.council-seat-grid \{([^}]*)\}", NARROWEST)
    assert rule and "grid-template-columns: minmax(0, 1fr)" in rule.group(1), (
        "at 195px two seat columns leave each cell narrower than its own text and action"
    )
    # The width has to stay wide enough to cover the 195px effective viewport the rule exists for,
    # and narrow enough to leave a 360px phone on two columns. 195 <= width < 360.
    widths = [int(width) for width in re.findall(r"@media \(max-width: (\d+)px\)", HTML)]
    collapsing = [
        width for width in widths
        if re.search(
            r"\.council-seat-grid \{[^}]*grid-template-columns: minmax\(0, 1fr\)",
            "\n".join(_narrow_blocks(width)),
        )
    ]
    assert collapsing, "some breakpoint has to collapse the seat grid"
    assert all(195 <= width < 360 for width in collapsing), collapsing


def test_a_phone_without_zoom_keeps_two_seat_columns() -> None:
    """The 390px block must not carry a seat-grid rule again: that is the 480.19px regression."""
    assert not re.search(r"\.council-seat-grid \{[^}]*grid-template-columns", NARROW), (
        "a 390px phone is full width with ~175px seat cells; collapsing it doubled the panel above "
        "the transcript (480.19px at 390x844)"
    )


def test_the_rule_names_the_council_grid_and_not_the_pipeline_grid() -> None:
    """The tab naming invites this exact mistake; the fix must target the Council grid."""
    assert "#pane-council .council-seat-grid" not in NARROWEST
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
