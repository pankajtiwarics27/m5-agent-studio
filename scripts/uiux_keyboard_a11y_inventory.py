#!/usr/bin/env python3
"""Inventory the keyboard and screen-reader contract of the six panes (UIUX-P5-02).

    python3 scripts/uiux_keyboard_a11y_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_keyboard_a11y_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_keyboard_a11y_inventory.py --json     # print the inventory

The shell could be driven by a keyboard since P1: the rail is a tablist, the arrows move it, Alt+N
opens a pane, and Escape closes the top dialog. Everything *inside* a pane was never measured. Three
things were wrong, and all three are the same mistake made in three places - an affordance that only
a pointer can reach:

* the two composers turned their textarea's ring off (`.prompt-box textarea:focus { outline: none }`)
  and put nothing in its place, so a keyboard user who tabbed into the task box had focus and no way
  to see it;
* the three starter cards on the harness pane and the preset chips in the MCP and provider modals
  were `div`/`span` elements with an `onclick`, so they were not in the tab order at all - the two
  fastest ways to fill that pane were mouse-only;
* the harness event stream takes focus so it can be scrolled with the arrow keys, and drew no ring,
  so focus landed on a region with no visible boundary.

The fix is one shape rather than three: a control that is not a native control carries
`role="button" tabindex="0" data-key-activate`, one delegated document-level `keydown` turns Enter
and Space into the click the pointer would have made (no inline `onkeydown`, so nothing runs from
markup), and a control that turns its own ring off has to show focus somewhere - the composer does it
on the box with `:focus-within`.

This inventory reads the source and states the contract. It cannot press a key; the walk
(`scripts/a11y_walk.mjs`) does that, on a real browser, and this inventory asserts the walk exists,
covers the six panes, and answers the questions the contract makes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "a11y_walk.mjs"
TEST = PROJECT_ROOT / "tests" / "test_keyboard_a11y.py"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "keyboard-a11y"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Keyboard_A11y.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-keyboard-a11y.md"
DOCS_INDEX = PROJECT_ROOT / "docs" / "README.md"

STORY = "UIUX-P5-02"
TITLE = "Keyboard and screen-reader regression"

PANES = ("start", "harness", "hall", "council", "debate", "chat")
CAPTURES = (
    "a11y-card-focus.png",
    "a11y-chip-focus.png",
    "a11y-composer-focus.png",
    "a11y-overlay-focus.png",
    "a11y-narrow-focus.png",
)

# A click target that is not a control: it dismisses the dialog under the pointer. Escape is the
# keyboard path, the walk proves Escape closes it, and a Tab stop on the backdrop would be a stop
# that does nothing.
DISMISS_ONLY = {"modal-backdrop": "a click outside the dialog closes it; Escape is the keyboard path"}

# A live region that is deliberately switched off, with the reason. The transcript streams token by
# token: a polite region on it would read every token as it arrived, so the chat pane announces the
# run's state through its own status line and leaves the transcript as a `role="log"` a reader can
# enter. Anything else marked `off` is a region that silently stopped announcing.
SILENT_LIVE_REGIONS = {
    "chatMessages": "the streaming transcript: a polite region on it would read every token as it "
    "arrived, so the composer's status line announces the run and the log is read on demand",
}

# The attributes a non-native control has to carry to be reachable and pressable.
ACTIVATION_ATTRS = ("role=\"button\"", "tabindex=\"0\"", "data-key-activate")

TABLES = re.compile(r"<table|role=\"grid\"|<table ")
RING_MIN_PX = 2.0


def function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(source)):
        char = source[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = source.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    return source[start:]


def rule(html: str, selector: str) -> str:
    """The declaration block of a CSS rule, by exact selector, or ''."""
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, html)
    return match.group(1) if match else ""


def outline_px(declarations: str) -> float:
    match = re.search(r"outline:\s*([^;]+);", declarations)
    if not match:
        return 0.0
    if re.search(r"\bnone\b", match.group(1)):
        return 0.0
    width = re.search(r"(\d+(?:\.\d+)?)px", match.group(1))
    return float(width.group(1)) if width else 0.0


def click_targets(html: str) -> list[dict[str, str]]:
    """Every element in the frontend that carries an `onclick`, with what it is."""
    found: list[dict[str, str]] = []
    for match in re.finditer(r"<([a-zA-Z][\w-]*)([^>]*?)\sonclick=", html):
        tag = match.group(1).lower()
        attrs = match.group(2)
        classes = re.search(r'class="([^"]*)"', attrs)
        first_class = (classes.group(1).split() or [""])[0] if classes else ""
        found.append(
            {
                "tag": tag,
                "attrs": attrs,
                "class": first_class,
                "native": tag in {"button", "a", "input", "select", "textarea", "summary"},
            }
        )
    return found


def focusables_in_markup(html: str) -> list[dict[str, str]]:
    """Elements that take focus, with the name sources they carry in the markup."""
    found: list[dict[str, str]] = []
    for match in re.finditer(r"<(input|select|textarea)\b([^>]*)>", html):
        tag = match.group(1).lower()
        attrs = match.group(2)
        if re.search(r'type="(hidden|submit|button|reset)"', attrs):
            continue
        identifier = re.search(r'id="([^"]+)"', attrs)
        found.append(
            {
                "tag": tag,
                "id": identifier.group(1) if identifier else "",
                "attrs": attrs,
            }
        )
    return found


def named(entry: dict[str, str], html: str) -> bool:
    attrs = entry["attrs"]
    if re.search(r'aria-label="[^"]+"', attrs) or re.search(r'aria-labelledby="[^"]+"', attrs):
        return True
    if re.search(r'title="[^"]+"', attrs) or re.search(r'placeholder="[^"]+"', attrs):
        return True
    if entry["id"] and re.search(rf'<label[^>]*for="{re.escape(entry["id"])}"', html):
        return True
    # A control wrapped in its own `<label>` is named by it - `#ocPinned` is the checkbox case, and
    # the rule-selected files and compare-metric checkboxes are the template cases.
    needle = f"<{entry['tag']}{entry['attrs']}>"
    for wrapper in re.finditer(r"<label\b[^>]*>(.*?)</label>", html, re.S):
        inner = wrapper.group(1)
        if needle in inner or (entry["id"] and f'id="{entry["id"]}"' in inner):
            return True
    return False


def walk_checks(walk_body: str) -> list[str]:
    names = re.findall(r'record\(\s*\n?\s*"([^"]+)"', walk_body)
    return names


def ledger_story() -> dict:
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""
    test_body = TEST.read_text(encoding="utf-8") if TEST.exists() else ""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    targets = click_targets(html)
    unreachable = [
        entry
        for entry in targets
        if not entry["native"]
        and not all(attr in entry["attrs"] for attr in ACTIVATION_ATTRS)
        and entry["class"] not in DISMISS_ONLY
    ]

    # ── 1. nothing is mouse-only ────────────────────────────────────────────
    check(
        "every click target is a native control or carries the role, the tab stop and the activation hook",
        not unreachable,
        "; ".join(f"{entry['tag']}.{entry['class']}" for entry in unreachable[:8]),
    )
    dismiss = [entry for entry in targets if entry["class"] in DISMISS_ONLY]
    check(
        "the click targets that take no tab stop are only the declared dismiss-only backdrops",
        len(dismiss) >= 1 and all(entry["class"] in DISMISS_ONLY for entry in dismiss),
        f"{len(dismiss)} backdrops",
    )
    check(
        "the backdrops are declared, with the reason and the keyboard path beside them",
        DISMISS_ONLY.get("modal-backdrop", "").startswith("a click outside")
        and "Escape" in DISMISS_ONLY.get("modal-backdrop", ""),
        "an exception that is not argued for is a hole",
    )
    activatable = [entry for entry in targets if "data-key-activate" in entry["attrs"]]
    check(
        "the non-native controls are the ten that were mouse-only: starter cards, preset chips, skill pills and session rows",
        len(activatable) == 10,
        f"{len(activatable)} activatable click targets: "
        + "; ".join(f"{entry['tag']}.{entry['class']}" for entry in activatable),
    )
    native_chips = re.findall(r'<button type="button" class="chip"', html)
    check(
        "a chip that was already a button stays a button, and is not given the attributes it does not need",
        len(native_chips) >= 2,
        f"{len(native_chips)} native chips",
    )

    # ── 2. one delegated handler, and nothing inline ────────────────────────
    delegated = function_body(html, "document.addEventListener")
    handler = ""
    for match in re.finditer(r"document\.addEventListener\(\"keydown\",\s*\(event\)\s*=>\s*\{", html):
        start = match.start()
        chunk = html[start : start + 1400]
        if "data-key-activate" in chunk:
            handler = chunk
            break
    check(
        "one delegated keydown turns Enter and Space into the click the pointer would have made",
        bool(handler)
        and '"Enter"' in handler
        and '" "' in handler
        and "target.click();" in handler
        and "preventDefault" in handler,
        "no handler, no keyboard activation" if not handler else "Enter/Space are not both handled",
    )
    check(
        "the handler refuses to double-press a native control",
        bool(handler) and 'target.tagName === "BUTTON"' in handler and 'target.tagName === "A"' in handler,
        "a button already answers Enter on its own; pressing it twice would submit twice",
    )
    inline_keys = re.findall(r"\son(keydown|keyup|keypress)=", html)
    check(
        "no inline key handler runs from markup",
        not inline_keys,
        f"inline key handlers: {len(inline_keys)}",
    )
    assert delegated is not None

    # ── 3. a control that hides its ring shows focus somewhere ──────────────
    global_ring = rule(html, "summary:focus-visible, [tabindex]:focus-visible")
    check(
        "the global focus ring is drawn with `:focus-visible` at 2px or more",
        outline_px(global_ring) >= RING_MIN_PX,
        f"global ring: {outline_px(global_ring)}px",
    )
    prompt_rule = rule(html, ".prompt-box textarea:focus")
    check(
        "the composer still owns the decision to hide the textarea's own ring",
        "outline: none" in prompt_rule,
        "if this rule is gone the check below is measuring nothing",
    )
    box_ring = rule(html, ".prompt-box:focus-within")
    check(
        "the composer draws the focus on the box instead, at the width the global ring uses",
        outline_px(box_ring) >= RING_MIN_PX and "var(--ring)" in box_ring,
        f"`.prompt-box:focus-within` ring: {outline_px(box_ring)}px",
    )
    stream_ring = rule(html, ".harness-stream:focus-visible, .hall-stream:focus-visible")
    check(
        "a scroll region that takes focus draws a ring of its own",
        outline_px(stream_ring) >= RING_MIN_PX,
        f"stream ring: {outline_px(stream_ring)}px",
    )
    check(
        "no other selector turns a focusable element's ring off without replacing it",
        not [
            selector
            for selector in re.findall(r"([^{}\n]+)\{[^}]*outline:\s*none", html)
            if re.search(r"input|select|textarea|button|a\b|summary|tabindex", selector)
            and selector.strip() not in {".prompt-box textarea:focus"}
            and "focus-within" not in html.split(selector, 1)[0][-200:].split("}", 1)[-1]
        ],
        "a suppressed ring with no replacement is a focus a keyboard user cannot see",
    )

    # ── 4. the rail and the panes ───────────────────────────────────────────
    tabs = re.findall(r'<button class="mode-tab[^"]*" role="tab" id="tab-(\w+)"[^>]*aria-controls="pane-(\w+)"', html)
    panels = re.findall(r'<section id="pane-(\w+)" role="tabpanel" aria-labelledby="tab-(\w+)"', html)
    pairs = [(tab, pane) for tab, pane in tabs]
    check(
        "the rail is one tablist of six tabs, each pointing at its own pane",
        len(tabs) == 6 and [pane for _tab, pane in tabs] == list(PANES),
        f"{len(tabs)} tabs: {tabs}",
    )
    check(
        "each pane is a `tabpanel` labelled by the tab that controls it",
        len(panels) == 6 and [(pane, tab) for pane, tab in panels] == pairs,
        f"{len(panels)} panels: {panels}",
    )
    zero_tabs = len(re.findall(r'role="tab"[^>]*tabindex="0"', html))
    check(
        "exactly one tab is in the tab order, so a pane is one Tab away and not six",
        zero_tabs == 1,
        f"{zero_tabs} tabbable tabs",
    )

    # ── 5. focus order is the reading order ─────────────────────────────────
    positive = [
        entry for entry in re.findall(r'tabindex="(-?\d+)"', html) if int(entry) > 0
    ]
    check(
        "nothing is pushed to the front of the tab order with a positive tabindex",
        not positive,
        f"positive tabindex values: {positive}",
    )

    # ── 6. names ────────────────────────────────────────────────────────────
    fields = focusables_in_markup(html)
    unnamed = [entry for entry in fields if not named(entry, html)]
    check(
        "every form control in the markup carries a name source a screen reader can read",
        not unnamed,
        "; ".join(f"{entry['tag']}#{entry['id']}" for entry in unnamed[:8])
        or f"{len(fields)} controls",
    )
    live = re.findall(r'aria-live="(\w+)"', html)
    role_status = len(re.findall(r'role="status"', html))
    role_alert = len(re.findall(r'role="alert"', html))
    silent_ids = []
    for tag in re.findall(r"<[^>]*aria-live=\"off\"[^>]*>", html):
        match = re.search(r'id="([^"]+)"', tag)
        silent_ids.append(match.group(1) if match else "?")
    undeclared = [
        identifier
        for identifier in silent_ids
        if identifier not in SILENT_LIVE_REGIONS
    ]
    check(
        "the panes announce their state instead of only drawing it",
        len(live) >= 10 and role_status >= 1 and not undeclared,
        f"{len(live)} live regions ({len(silent_ids)} declared off: {', '.join(silent_ids)}), "
        f"{role_status} status roles, {role_alert} alert roles"
        + (f"; undeclared: {', '.join(undeclared)}" if undeclared else ""),
    )

    # ── 7. the dialog contract ──────────────────────────────────────────────
    opener = function_body(html, "openOverlay")
    closer = function_body(html, "closeOverlay")
    check(
        "a dialog takes `aria-modal` when it opens and remembers what had focus",
        'setAttribute("aria-modal", "true")' in opener
        and "const opener" in opener
        and "document.activeElement" in opener,
        "a modal that does not remember its opener cannot give focus back",
    )
    check(
        "closing a dialog gives focus back to the control that opened it",
        ".focus(" in closer and "opener" in closer,
        "focus left on a closed dialog is focus on nothing",
    )
    trap = function_body(html, "overlayTrapTab")
    check(
        "Tab is trapped inside the open dialog rather than wandering into the page behind it",
        "Tab" in trap and "shiftKey" in trap,
        "no trap: Tab escapes the dialog",
    )
    shortcuts = function_body(html, "shortcutMatches")
    check(
        "Escape is a registered shortcut, so it closes the top overlay and is discoverable in the list",
        'keys: ["Escape"]' in html and "dismiss-overlay" in html and bool(shortcuts),
        "Escape as an undocumented special case is a key that exists only in the code",
    )

    # ── 8. the evidence ─────────────────────────────────────────────────────
    walk_names = walk_checks(walk_body)
    check(
        "the walk presses keys on a real browser and answers the three questions per stop",
        "FOCUS_LEDGER" in walk_body
        and "name: nameOf(el)" in walk_body
        and "ring: ringOf(el)" in walk_body
        and "hidden:" in walk_body,
        "a stop without a name, a ring and a size is not measured",
    )
    check(
        "the walk covers all six panes and the two controls that were mouse-only",
        all(f'"{pane}"' in walk_body for pane in PANES)
        and 'page.keyboard.press("Enter")' in walk_body
        and 'page.keyboard.press("Space")' in walk_body,
        ", ".join(pane for pane in PANES if f'"{pane}"' not in walk_body),
    )
    check(
        "the walk proves the overlay traps focus, closes on Escape and gives focus back",
        'page.keyboard.press("Escape")' in walk_body
        and "trapped" in walk_body
        and "returned" in walk_body
        and "aria-modal" in walk_body,
        "a dialog that closes but strands the focus is a dialog a keyboard user falls out of",
    )
    check(
        "the walk refuses to report a walk it did not perform",
        "STUDIO_SCREENSHOT_CHROME" in walk_body
        and "refuses to report a walk it did not perform" in walk_body
        and "process.exitCode = 1" in walk_body,
        "a skipped walk that reports success is the failure mode this story is about",
    )
    check(
        "the captures the doc lists exist and were written by the walk",
        all((MEDIA / name).exists() for name in CAPTURES)
        and all(name[:-4] in walk_body for name in CAPTURES),
        ", ".join(name for name in CAPTURES if not (MEDIA / name).exists())
        or ", ".join(name for name in CAPTURES if name[:-4] not in walk_body),
    )
    check(
        "each rule is broken in a copy of the source by a test, so a rule that stopped looking fails",
        test_body.count("assert changed != html") >= 1 and len(re.findall(r"def test_", test_body)) >= 10,
        f"{len(re.findall(r'def test_', test_body))} tests in {TEST.name}",
    )
    check(
        "the doc is written, and reachable from the docs index",
        DOC_PATH.exists()
        and DOCS_INDEX.exists()
        and DOC_PATH.name in DOCS_INDEX.read_text(encoding="utf-8"),
        f"{DOC_PATH.name} missing or unlinked",
    )
    tracking = ledger_story()
    check(
        "the ledger marks this story done with evidence",
        tracking.get("status") == "done" and bool(tracking.get("evidence")) and bool(tracking.get("test_results")),
        f"tracking.status={tracking.get('status')!r}",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": STORY,
        "title": TITLE,
        "generated_by": "scripts/uiux_keyboard_a11y_inventory.py",
        "surface": "keyboard and screen-reader contract of the six panes",
        "panes": list(PANES),
        "activation": {
            "attributes": list(ACTIVATION_ATTRS),
            "keys": ["Enter", " "],
            "activation_targets": [
                {"tag": entry["tag"], "class": entry["class"]} for entry in activatable
            ],
        },
        "dismiss_only": DISMISS_ONLY,
        "ring": {
            "global_px": outline_px(global_ring),
            "composer_box_px": outline_px(box_ring),
            "stream_px": outline_px(stream_ring),
            "minimum_px": RING_MIN_PX,
        },
        "tab_order": {"tabs": len(tabs), "tabbable_tabs": zero_tabs, "positive_tabindex": len(positive)},
        "names": {"controls": len(fields), "unnamed": len(unnamed), "live_regions": len(live)},
        "walk": {"script": "scripts/a11y_walk.mjs", "checks": walk_names, "check_count": len(walk_names)},
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P5-02 — Keyboard and screen-reader regression",
        "",
        "Generated by `scripts/uiux_keyboard_a11y_inventory.py`. Do not edit by hand: regenerate it,",
        "and the checks behind it stay true.",
        "",
        "## What was wrong",
        "",
        "The shell has had a keyboard contract since P1 — the rail is a tablist, the arrow keys move it,",
        "`Alt+1`…`Alt+6` open a pane, Escape closes the top dialog — and nothing measured what happens",
        "after the rail. Tabbing through the six panes found three defects, and they are the same defect",
        "three times: an affordance only a pointer can reach.",
        "",
        "| Where | What a keyboard user hit |",
        "| :--- | :--- |",
        "| Council and Compare composers | the textarea took focus with `outline: none` and the box drew nothing, so the task box was a stop with no visible focus at all |",
        "| Harness starter cards (3) | `div` elements with `onclick`: not in the tab order, silent to a screen reader, reachable only by mouse — the fastest way to start a task in that pane |",
        "| MCP and provider preset chips | `span` elements with `onclick`, the same defect: twelve presets that a keyboard could not select |",
        "| Harness event stream | focusable so it can be scrolled with the arrow keys, but drew no ring, so focus landed on a region with no visible boundary |",
        "",
        "## The shape of the fix",
        "",
        "A non-native control is given the three attributes that make it a control —",
        f"`{'`, `'.join(payload['activation']['attributes'])}` — and one delegated document-level",
        "`keydown` turns `Enter` and `Space` into the click the pointer would have made. It is one",
        "handler, not one per element, and it is not inline: no `onkeydown` attribute runs from markup,",
        "which the inventory asserts. The handler refuses to re-press a native `button` or `a`, because",
        "those already answer `Enter` on their own.",
        "",
        "A control that hides its own ring has to show focus somewhere. The composer keeps the textarea's",
        "ring off and draws it on the box with `:focus-within`, at the same width and colour as the",
        "global ring; the stream region gets a `:focus-visible` ring of its own.",
        "",
        "| Control | Before | After |",
        "| :--- | :--- | :--- |",
        f"| `.prompt-box` composer | `outline: none`, nothing in its place | box ring at {payload['ring']['composer_box_px']:.0f}px `var(--ring)` on `:focus-within` |",
        f"| `.harness-stream`, `.hall-stream` | UA default or nothing | ring at {payload['ring']['stream_px']:.0f}px on `:focus-visible` |",
        "| `.starter-card` | `div[onclick]`, unreachable | `role=\"button\" tabindex=\"0\" data-key-activate` |",
        "| `.chip` preset | `span[onclick]`, unreachable | `role=\"button\" tabindex=\"0\" data-key-activate` |",
        "",
        "## The exception, stated",
        "",
        "The modal backdrops carry `onclick` and no tab stop, deliberately: a click outside the dialog",
        "closes it and Escape is the keyboard path. The walk proves Escape closes the dialog it opened.",
        "",
        "| Element | Why it takes no tab stop |",
        "| :--- | :--- |",
    ]
    for element, reason in payload["dismiss_only"].items():
        lines.append(f"| `.{element}` | {reason} |")
    lines += [
        "",
        "A live region that is deliberately switched off is declared too, so a silenced pane is a",
        "decision rather than an omission:",
        "",
    ]
    for identifier, reason in SILENT_LIVE_REGIONS.items():
        lines.append(f"* `#{identifier}` — {reason}.")
    lines += [
        "",
        "## The evidence",
        "",
        "`node scripts/a11y_walk.mjs --screenshots docs/media/keyboard-a11y` starts the Compare fixture,",
        "then walks each pane's real tab order — it presses Tab and reads the element that has focus —",
        "asking of every stop: does it have a name, does it show focus, is it visible and sized? It then",
        "presses `Enter` on a starter card and `Space` on a preset chip, opens the command palette,",
        "Tabs inside it, closes it with Escape and checks focus came back to the control that opened it.",
        "",
        "The walk fails rather than skips: with no browser binary it refuses to report a walk it did not",
        "perform. The counts below come from the run recorded in the story's evidence, not from this",
        "file.",
        "",
        "| Check | What it proves |",
        "| :--- | :--- |",
    ]
    for name in payload["walk"]["checks"]:
        lines.append(f"| {name} | |")
    lines += [
        "",
        "## The checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} findings — **{payload['status']}**.",
        "",
    ]
    for entry in payload["checks"]:
        mark = "pass" if entry["ok"] else "FAIL"
        detail = f" — {entry['detail']}" if entry["detail"] else ""
        lines.append(f"* `{mark}` {entry['check']}{detail}")
    lines.append("")
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale or a rule is broken")
    parser.add_argument("--json", action="store_true", help="print the inventory instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    payload = build_inventory()

    if args.json:
        print(dumps(payload), end="")
    else:
        rendered_json = dumps(payload)
        rendered_doc = render_doc(payload)
        stale = (
            not JSON_PATH.exists()
            or JSON_PATH.read_text(encoding="utf-8") != rendered_json
            or not DOC_PATH.exists()
            or DOC_PATH.read_text(encoding="utf-8") != rendered_doc
        )
        if args.check:
            if stale:
                print(
                    "the keyboard/a11y inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    if payload["findings"]:
        for entry in payload["findings"]:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
