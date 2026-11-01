"""Structural contracts for accessible modal, toast and command-palette behavior."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "static" / "index.html"


class ModalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_backdrop = False
        self.dialogs: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if "modal-backdrop" in (values.get("class") or "").split():
            self.in_backdrop = True
        if self.in_backdrop and values.get("role") == "dialog":
            self.dialogs.append({
                "aria-modal": values.get("aria-modal"),
                "aria-label": values.get("aria-label"),
                "aria-labelledby": values.get("aria-labelledby"),
            })

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.in_backdrop:
            # The backdrop owns only one dialog; its matching end tag is the one
            # immediately after the child dialog closes in this source markup.
            self.in_backdrop = False


def _source() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def test_every_modal_is_a_named_modal_dialog() -> None:
    parser = ModalParser()
    parser.feed(_source())
    assert len(parser.dialogs) == 11
    assert all(dialog["aria-label"] or dialog["aria-labelledby"] for dialog in parser.dialogs)
    # The shared controller sets aria-modal only while a backdrop is open.
    source = _source()
    assert 'dialog.setAttribute("aria-modal", "true")' in source
    assert 'entry.dialog.removeAttribute("aria-modal")' in source


def test_all_modal_open_and_close_paths_use_the_shared_focus_manager() -> None:
    source = _source()
    assert "function openOverlay(" in source
    assert "function closeOverlay(" in source
    assert "function closeTopOverlay(" in source
    assert "function overlayTrapTab(" in source
    assert "const overlayStack = []" in source
    assert 'dialog.setAttribute("aria-modal", "true")' in source
    assert "overlayInert(true)" in source
    toggles = list(re.finditer(r'\.classList\.(?:add|remove)\(["\']open["\']\)', source))
    assert len(toggles) == 3
    manager_start = source.index("function openOverlay(")
    manager_end = source.index("function closeTopOverlay(", manager_start)
    assert all(manager_start <= match.start() < manager_end for match in toggles)
    assert "entry.opener" in source
    assert "overlayStack.length - 1" in source


def test_toasts_have_a_named_live_region_and_keyboard_dismissal() -> None:
    source = _source()
    assert re.search(
        r'<div id="toastStack"[^>]*role="region"[^>]*aria-label="Notifications"[^>]*aria-live="polite"',
        source,
    )
    assert 'setAttribute("aria-label", "Dismiss notification")' in source
    assert 'addEventListener("focusin", pauseToast)' in source
    assert 'addEventListener("pointerenter", pauseToast)' in source
    assert "prefers-reduced-motion" in source
    assert 'kind === "error"' in source or 'kind === "err"' in source


def test_command_palette_uses_combobox_and_active_descendant_semantics() -> None:
    source = _source()
    assert 'id="cmdSearchInput"' in source
    fragment = source[source.index('id="cmdSearchInput"') - 120: source.index('id="cmdSearchInput"') + 500]
    for attr in ('role="combobox"', 'aria-controls="cmdPaletteList"', 'aria-expanded="false"', 'aria-autocomplete="list"'):
        assert attr in fragment
    assert 'setAttribute("aria-activedescendant"' in source
    assert 'setAttribute("aria-selected"' in source
    assert 'shortcutMatches(e, "dismiss-overlay")' in source
    assert 'keys: ["Escape"]' in source
    assert 'event.key !== "Tab"' in source
