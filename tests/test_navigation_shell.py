"""UIUX-P1-02: the rail must be a tablist a keyboard user can actually drive.

The shell is six panes of one page, not six pages, so the rail is a tablist and the
contract that matters is small enough to check exactly: every tab points at a pane,
every pane points back, exactly one tab is selected, exactly one tab is in the tab
order, and the shortcut a tab advertises is the shortcut that works.

Three of these caught real defects rather than confirming a design. The rail used
`aria-current="page"` instead of a tablist, so assistive technology announced six
buttons the way it announces a menu. Nothing bounded the tab order, so reaching the
chat pane with the keyboard meant six presses through a rail that already had arrow
keys. And `Alt+6` was advertised on the Start tab and in the ledger's registry while
the handler mapped only keys 1 to 5 — a shortcut that existed in the title and
nowhere else.

The behavioural half lives in `scripts/keyboard_walk.mjs`, which needs a browser.
These tests cover what the source can prove; that script covers what only a browser
can, and the story records its output.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "static" / "index.html"
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
WALK = ROOT / "scripts" / "keyboard_walk.mjs"

TAB = re.compile(r"<button\b[^>]*class=\"mode-tab[^\"]*\"[^>]*>", re.DOTALL)
PANE = re.compile(r"<section\b[^>]*class=\"pane[^\"]*\"[^>]*>", re.DOTALL)


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _attrs(tag: str) -> dict[str, str]:
    found = {}
    for name, value in re.findall(r"([a-zA-Z-]+)=\"([^\"]*)\"", tag):
        found[name] = value
    return found


def _tabs() -> list[dict[str, str]]:
    return [_attrs(tag) for tag in TAB.findall(_html())]


def _panes() -> list[dict[str, str]]:
    return [_attrs(tag) for tag in PANE.findall(_html())]


def test_the_rail_is_a_tablist_with_one_selected_and_one_tabbable_tab() -> None:
    tabs = _tabs()
    assert len(tabs) == 6, "the rail is the six panes"
    assert sum(1 for tab in tabs if tab.get("aria-selected") == "true") == 1
    assert sum(1 for tab in tabs if tab.get("tabindex") == "0") == 1
    selected = [tab for tab in tabs if tab.get("aria-selected") == "true"][0]
    assert selected.get("tabindex") == "0", "the selected tab must be the one Tab reaches"
    assert all(tab.get("role") == "tab" for tab in tabs)


def test_the_tablist_has_an_accessibility_name_and_orientation() -> None:
    html = _html()
    assert re.search(r'<div class="mode-tabs" role="tablist" aria-label="[^"]+"', html), (
        "a tablist without an accessible name announces six anonymous tabs"
    )
    assert re.search(r'role="tablist"[^>]*aria-orientation="(vertical|horizontal)"', html)


def test_every_tab_points_at_a_pane_and_every_pane_points_back() -> None:
    tabs, panes = _tabs(), _panes()
    pane_by_id = {pane.get("id"): pane for pane in panes}
    tab_ids = {tab.get("id") for tab in tabs}
    assert len(tab_ids) == len(tabs), "tab ids must be unique"

    for tab in tabs:
        target = tab.get("aria-controls")
        assert target in pane_by_id, f"{tab.get('id')} controls {target}, which is not a pane"
        pane = pane_by_id[target]
        assert pane.get("role") == "tabpanel", f"{target} is not a tabpanel"
        assert pane.get("aria-labelledby") == tab.get("id"), (
            f"{target} is not labelled by {tab.get('id')}"
        )
    assert len(pane_by_id) == len(tabs), "every pane must be a tab's target"


def test_the_pane_that_ships_open_is_the_selected_tab() -> None:
    tabs, panes = _tabs(), _panes()
    selected = [tab for tab in tabs if tab.get("aria-selected") == "true"][0]
    open_panes = [pane.get("id") for pane in panes if "active" in (pane.get("class") or "").split()]
    assert open_panes == [selected.get("aria-controls")], (
        "the class that paints the pane and the ARIA state that describes it must agree"
    )


def test_the_advertised_shortcut_is_the_dom_attribute() -> None:
    tabs = _tabs()
    for tab in tabs:
        shortcut = tab.get("data-shortcut") or ""
        assert re.fullmatch(r"Alt\+[1-6]", shortcut), f"{tab.get('id')} advertises {shortcut!r}"
        assert shortcut in (tab.get("title") or ""), (
            f"{tab.get('id')} shows {tab.get('title')!r} but binds {shortcut}"
        )
    assert len({tab["data-shortcut"] for tab in tabs}) == len(tabs), "two tabs share a shortcut"


def test_the_shortcuts_match_the_ledger_registry() -> None:
    """The ledger and the markup describe one shell; the test refuses a split brain.

    The registry is what the sign-off package publishes to the owner, so a shortcut
    that exists in the rail but not the registry (or the reverse) is a documentation
    defect even when the app behaves correctly.
    """
    registry = {entry["dom_id"]: entry for entry in json.loads(LEDGER.read_text())["tab_registry"]}
    for tab in _tabs():
        dom_id = tab.get("data-tab")
        assert dom_id in registry, f"{dom_id} is not in the ledger's tab registry"
        assert registry[dom_id]["keyboard_shortcut"] == tab.get("data-shortcut"), (
            f"{dom_id}: rail says {tab.get('data-shortcut')}, ledger says "
            f"{registry[dom_id]['keyboard_shortcut']}"
        )


def test_the_handler_reads_shortcuts_from_the_dom_instead_of_restating_them() -> None:
    """The defect this story found: two lists that could disagree, and did.

    `Alt+6` was in the title and the registry while the handler carried its own
    map of keys 1-5. A handler that reads `data-shortcut` cannot drift that way.
    """
    html = _html()
    assert 'querySelectorAll(".mode-tab[data-shortcut]")' in html, (
        "the shortcut handler must read the tabs, not a second list"
    )
    assert not re.search(r'"1"\s*:\s*"harness"', html), "a hard-coded shortcut map is back"


def test_switching_a_tab_updates_both_paint_and_state() -> None:
    html = _html()
    start = html.index("function switchTab(")
    body = html[start : html.index("function toggleMonitorRail", start)]
    for needle in ('aria-selected', "tabIndex", "classList.toggle(\"active\"", ".pane"):
        assert needle in body, f"switchTab no longer maintains {needle}"


def test_the_rail_handles_arrow_home_and_end_keys() -> None:
    html = _html()
    for key in ("ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"):
        assert key in html, f"the rail has no {key} handling"
    assert 'addEventListener("keydown", tabRailKeydown)' in html, "the rail listener is not wired"


def test_the_walk_script_is_present_and_parses() -> None:
    assert WALK.exists(), "the keyboard walk is the story's behavioural evidence"
    if not shutil.which("node"):
        pytest.skip("Node.js is not installed")
    node = subprocess.run(
        ["node", "--check", str(WALK)], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert node.returncode == 0, node.stderr
    text = WALK.read_text(encoding="utf-8")
    assert "refuses to report a keyboard walk it did not perform" in text, (
        "the walk must fail closed when no browser is available"
    )
