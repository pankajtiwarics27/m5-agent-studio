"""UIV2-P7-07: the header is one row, and the chrome that left it is still reachable.

There is no browser in this environment, so the fold's invariants are pinned structurally.
Each assertion here corresponds to something that would be a real defect, not a style
preference:

* the workspace toolbar must be a child of the header (that is the fold);
* it must not keep the bar treatment that made it a second row;
* the caption must stay a childless text node (three browser checks assert `children === 0`);
* Theme and Telemetry must still exist, in the Settings overlay, with their ids and handlers
  intact, so every existing reader still resolves;
* Settings must open through the shared overlay controller, not a hand-rolled class toggle.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "static" / "index.html"
HTML = FRONTEND.read_text(encoding="utf-8")


class Tree(HTMLParser):
    """Records the parent chain of every id, and the children of every id."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.parents: dict[str, list[tuple[str, str | None]]] = {}
        self.children: dict[str, int] = {}
        self._open_ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.parents[element_id] = list(self.stack)
            for ancestor in self._open_ids:
                self.children[ancestor] = self.children.get(ancestor, 0) + 1
        if tag in {"br", "img", "input", "meta", "link", "hr"}:
            return
        self.stack.append((tag, element_id))
        if element_id:
            self._open_ids.append(element_id)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for element_id in self.stack[i:]:
                    if element_id[1] and self._open_ids and self._open_ids[-1] == element_id[1]:
                        self._open_ids.pop()
                del self.stack[i:]
                break


TREE = Tree()
TREE.feed(HTML)


# ── the fold ─────────────────────────────────────────────────────────────────────

def test_the_workspace_toolbar_is_a_child_of_the_header() -> None:
    """This is the fold: the toolbar is no longer a second bar under the header."""
    # The toolbar is a div with a class, not an id, so find it by its opening tag.
    header = HTML[HTML.index('<header class="topbar">') : HTML.index("</header>")]
    assert '<div class="workspace-toolbar">' in header
    assert '<div class="workspace-toolbar" ' not in HTML[HTML.index("</header>") :]


def test_the_folded_toolbar_gives_up_its_bar_treatment() -> None:
    rules = re.findall(r"\.topbar > \.workspace-toolbar \{([^}]*)\}", HTML)
    body = next((r for r in rules if "flex: 1 1 auto" in r), None)
    assert body is not None, "the folded toolbar needs its own layout rule"
    for declaration in ("min-height: 0", "padding: 0", "border-bottom: 0"):
        assert declaration in body, declaration


def test_the_narrow_row_does_not_wrap_inside_a_fixed_height_header() -> None:
    """At <=390px the header is overflow-y:hidden, so a wrapping toolbar would be clipped."""
    narrow = HTML[HTML.index("@media (max-width: 390px)") :]
    assert ".topbar > .workspace-toolbar { flex-wrap: nowrap; }" in narrow


def test_the_caption_is_still_a_childless_text_node() -> None:
    """task_workspace_walk.mjs asserts children === 0; a chip with a span would break it."""
    tag = re.search(r'<span id="workspaceCaption"[^>]*>(.*?)</span>', HTML, re.S)
    assert tag, "workspaceCaption must still exist"
    assert tag.group(1).strip() == "Workspace: not selected"
    assert "<" not in tag.group(1)


# ── what left the header is still reachable ─────────────────────────────────────

def test_theme_and_telemetry_live_in_the_settings_overlay() -> None:
    for element_id in ("themeToggleBtn", "telemetryPopoverBtn"):
        assert ("div", "settingsModal") in TREE.parents[element_id], element_id


def test_the_header_no_longer_carries_theme_or_telemetry_or_density() -> None:
    header = HTML[HTML.index('<header class="topbar">') : HTML.index("</header>")]
    for gone in ("themeToggleBtn", "telemetryPopoverBtn", "densityToggleBtn"):
        assert gone not in header, gone


def test_the_settings_trigger_is_in_the_header_and_opens_through_the_controller() -> None:
    header = HTML[HTML.index('<header class="topbar">') : HTML.index("</header>")]
    assert 'id="settingsBtn"' in header
    assert 'onclick="openSettings()"' in header
    assert 'function openSettings() { openOverlay("settingsModal"' in HTML


def test_the_theme_handlers_still_find_their_controls() -> None:
    """applyTheme/toggleTheme write to #themeToggleBtn and #themeToggleLabel by id."""
    assert 'document.getElementById("themeToggleBtn")' in HTML
    assert 'id="themeToggleLabel"' in HTML
    assert ("button", "themeToggleBtn") in TREE.parents["themeToggleLabel"]


def test_the_telemetry_trigger_still_points_at_the_same_popover() -> None:
    tag = re.search(r"<button[^>]*id=\"telemetryPopoverBtn\"[^>]*>", HTML)
    assert tag
    assert 'aria-controls="telemetryPopover"' in tag.group(0)
    assert 'onclick="openTelemetryFromSettings()"' in tag.group(0)
    # Closing Settings first keeps two overlays from stacking their dismissal rules.
    assert "closeOverlay(\"settingsModal\");" in HTML


def test_the_settings_overlay_is_a_named_dialog_like_every_other() -> None:
    modal = HTML[HTML.index('id="settingsModal"') :]
    modal = modal[: modal.index("<!-- ==") if "<!-- ==" in modal else len(modal)]
    assert 'role="dialog"' in modal
    assert 'aria-label="Settings"' in modal

# ── narrow: the header is a scroll container, and that has consequences ─────────

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


def test_the_tools_panel_escapes_the_headers_overflow_clip() -> None:
    """At <=390px .topbar is overflow-y:hidden, so an absolute panel inside it is clipped away.

    The panel is anchored to .workspace-toolbar, which is now a header child. Without fixed
    positioning, tapping Tools & integrations on a phone opens a panel that is entirely
    outside the clipped box - the control would silently do nothing. This is the one place
    where the fold could take a feature away rather than just move it.
    """
    narrow = "\n".join(_narrow_blocks())
    rule = re.search(r"\.studio-tools > \.skills-strip \{([^}]*)\}", narrow)
    assert rule, "the narrow tools panel needs its own rule"
    assert "position: fixed" in rule.group(1)
    # fixed needs an explicit anchor: the panel used to be anchored to its summary's box.
    assert "top: calc(var(--header-height)" in rule.group(1)
    assert "max-height: calc(100dvh" in rule.group(1)


def test_the_folded_toolbar_keeps_its_width_instead_of_compressing() -> None:
    """The header pins flex-shrink:0 on its children; the base rule asks this one to shrink.

    At 195px an unopposed shrink:1 with min-width:0 collapses the toolbar and lets its nowrap
    contents paint over Search and Settings. It must be overridden, and the override has to
    come after the base rule because a media query adds no specificity.
    """
    narrow = "\n".join(_narrow_blocks())
    rules = re.findall(r"\.topbar > \.workspace-toolbar \{([^}]*)\}", narrow)
    rule = next((r for r in rules if "flex: 0 0 auto" in r), None)
    assert rule is not None, "the narrow folded toolbar needs a width-holding rule"
    assert "flex: 0 0 auto" in rule
    assert "flex-wrap: nowrap" in rule
    # The override must follow the base layout rule in the sheet, or it loses on source order.
    # Compare the two rules themselves, not bare declarations that appear elsewhere too.
    narrow_rule = HTML.rindex("@media (max-width: 390px)")
    base_rule = HTML.index(".topbar > .workspace-toolbar { flex: 1 1 auto")
    assert narrow_rule > base_rule, "the override must follow the base rule in the sheet"


def test_the_narrow_override_does_not_undo_the_pinned_wrap_literal() -> None:
    """The wrap literal is a pinned string; the override only adds a more specific sibling."""
    assert ".workspace-toolbar { flex-wrap: wrap; }" in HTML

def test_the_narrow_panel_override_is_the_last_word_on_that_selector() -> None:
    """A media query adds no specificity, so the override has to come last in the sheet.

    `.studio-tools > .skills-strip` is declared four times. Placing the 390px override before
    any of them loses to a later `position: absolute` at equal weight - which is exactly what
    happened on the first attempt, and why this test exists. It was caught by rendering the
    page in a real 390px viewport and reading `getComputedStyle(...).position`.
    """
    override = HTML.rindex("position: fixed; top: calc(var(--header-height) + 4px)")
    competing = [m.start() for m in re.finditer(r"\.studio-tools > \.skills-strip \{", HTML)]
    assert competing, "the panel selector must still exist"
    assert override > max(competing), "the 390px override must follow every competing declaration"


def test_the_narrow_toolbar_override_is_also_last() -> None:
    override = HTML.rindex(".topbar > .workspace-toolbar { flex: 0 0 auto")
    base = HTML.index(".topbar > .workspace-toolbar { flex: 1 1 auto")
    assert override > base
