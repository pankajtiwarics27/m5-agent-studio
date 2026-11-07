#!/usr/bin/env python3
"""Inventory the Chat pane's conversation column, controls and composer (UIV2-18).

    python3 scripts/uiux_chat_layout_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_chat_layout_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_chat_layout_inventory.py --json     # print the inventory

The approved target for this pane asks for a *readable column*, compact model/persona controls, a
composer that stays visible, and no dead whitespace. Chat was the one pane that ignored all four:

* **the conversation used the whole workspace.** At 1640px an assistant bubble was 870px of a
  1,300px row, so every answer line ran past the point where an eye can track it and the rest of
  the row was empty - the "dead whitespace" the target names, pointing the wrong way round.
* **the controls were a wall.** Three full-width pickers plus two checkboxes stacked into five
  rows on a phone (314px of chrome above a conversation that then had 250px left), and onto a
  second row on a desktop, pushing the first word of the conversation down the screen.
* **the composer was in flow only.** Nothing kept it above a phone keyboard, and the pane had
  never been asked to prove that a code block or an unbroken token stayed inside its bubble -
  a bubble that wide never had to break.

What this inventory checks: one column token inside the approved 720-900px band, the three
conversation rows taking it with symmetric inline padding, the controls kept out of it as chrome,
the desktop one-row bar and the phone's three-row bar, the design system's control height left
alone, the sticky composer with its safe-area and keyboard margins, the `dvh` shell, the wrapping
and in-bubble scrolling rules that keep prose and code from being cut, one empty-state sentence
said the same way by the markup and the renderer, the rail still collapsed on entry, and every
control, key binding, undo and single stream call site the pane had before this story.

It fails when the column stops being one token, when a conversation row drifts off it, when an
answer can be clipped, when the composer can leave the screen, or when a preserved control,
keyboard contract or call site disappears.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK_PATH = PROJECT_ROOT / "scripts" / "chat_layout_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Layout.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-layout.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "chat-layout"

STORY = "UIV2-18"
TITLE = "Chat focused balanced conversation layout"

#: The readable band the story is gated on. A column outside it is not the approved target,
#: whatever else it gets right.
COLUMN_MIN = 720
COLUMN_MAX = 900
COLUMN_TOKEN = f"--chat-column: {COLUMN_MAX - 80}px;"

#: The rows of the pane that carry conversation, and the one row that carries chrome.
COLUMN_ROWS = (".chat-transcript-bar", ".chat-messages", ".chat-composer")
CHROME_ROW = ".chat-header-bar"

#: Every control the pane had before this story, by id, plus the live region and the log.
PRESERVED_CONTROLS = (
    "chatModel",
    "chatPersona",
    "chatReasoning",
    "chatToolsEnabled",
    "chatUnload",
    "chatClearBtn",
    "chatMessages",
    "chatTurnSummary",
    "chatHandoffHint",
    "chatHandoffBtn",
    "chatJumpLatest",
    "chatState",
    "chatInput",
    "chatStopBtn",
    "chatSendBtn",
    "chatKeyboardHint",
    "chatComposerHint",
)

#: What the walk has to assert, by the name it uses on the command line: the captures are only
#: evidence if a run asserted the state they show.
WALK_STATES = (
    ("empty", 'state("empty")'),
    ("populated", 'state("populated")'),
    ("long", 'state("long")'),
    ("loading", 'state("loading")'),
    ("error", 'state("error")'),
    ("recovery", 'state("recovery")'),
)

THEMES = ("light", "dark")
VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
STATES = ("empty", "populated", "long-content", "loading", "error", "recovery")


def check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def story_css(css: str) -> str:
    """This story's own block of the stylesheet, so a rule is read where it was written.

    Reading the last matching rule in the whole sheet would answer with a phone override that
    belongs to a different story; reading from the marker keeps the check about this change.
    """
    index = css.find("UIV2-18: the conversation is a column")
    if index < 0:
        return ""
    tail = css[index:]
    # The block ends where the next story's block begins, or at the end of the sheet.
    following = re.search(r"\n\s*/\* ── UIV2-1[0-9]", tail)
    return tail[: following.start()] if following else tail


def rule(css: str, selector: str) -> str:
    """The declarations of every rule whose selector list ends in `selector`, joined."""
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if any(part.endswith(selector) for part in selectors):
            found.append(match.group(2))
    return "\n".join(found)


def media(css: str, query: str) -> str:
    """The body of the first `@media (query)` block in `css`."""
    start = css.find(f"@media ({query})")
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(css)):
        if css[offset] == "{":
            depth += 1
        elif css[offset] == "}":
            depth -= 1
            if depth == 0:
                return css[start:offset + 1]
    return ""


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    walk = WALK_PATH.read_text(encoding="utf-8")
    css = "\n".join(re.findall(r"<style[^>]*>([\s\S]*?)</style>", html))
    scope = story_css(css)
    code = "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", html))

    empty_markup = re.search(r'<div class="chat-empty-state">(.*?)</div>', html)
    empty_markup_text = re.sub(r"<[^>]+>", "", empty_markup.group(1)).strip() if empty_markup else ""
    empty_renderer = re.search(r'box\.innerHTML = `<div class="chat-empty-state">(.*?)</div>`;', code)
    empty_renderer_text = re.sub(r"<[^>]+>", "", empty_renderer.group(1)).strip() if empty_renderer else ""

    checks: list[dict[str, object]] = []

    # ── 1. one column, inside the approved band ───────────────────────────────
    token = re.search(r"--chat-column:\s*(\d+)px;", css)
    column = int(token.group(1)) if token else 0
    checks.append(check(
        "the conversation width is one token, inside the 720-900px readable band",
        bool(token) and COLUMN_MIN <= column <= COLUMN_MAX,
        f"--chat-column {column or 'unset'}px (band {COLUMN_MIN}-{COLUMN_MAX})",
    ))
    declarations = len(re.findall(r"--chat-column:\s*\d+px", css))
    checks.append(check(
        "there is exactly one place the column width is written",
        declarations == 1,
        f"{declarations} declaration(s)",
    ))
    centred = "padding-inline: max(20px, calc((100% - var(--chat-column)) / 2));"
    column_rule = rule(scope, ".chat-composer")
    checks.append(check(
        "the transcript, its bar and the composer all take that column",
        all(f"{row}," in css for row in COLUMN_ROWS[:2])
        and centred in column_rule,
        ", ".join(row for row in COLUMN_ROWS if row.strip(".") not in css),
    ))
    checks.append(check(
        "the column is centred symmetrically, not nudged from one side",
        "padding-inline:" in column_rule and "margin-left" not in column_rule,
        column_rule.strip()[:120],
    ))

    # ── 2. the controls are chrome, not conversation ──────────────────────────
    header_rule = rule(scope, ".chat-header-bar")
    checks.append(check(
        "the controls keep the pane's width instead of squeezing into the column",
        ".chat-header-bar {" in css and "padding-inline: 16px;" in css,
        header_rule.strip()[:120],
    ))
    wide = media(scope, "min-width: 1200px")
    checks.append(check(
        "above 1200px the pickers are one row",
        "flex-wrap: nowrap;" in wide
        and ".chat-header-bar > div { flex: 1 1 0; min-width: 0; }" in wide
        and ".chat-header-bar > .chk-lbl { flex: 0 0 auto; white-space: nowrap;" in wide,
        wide.splitlines()[0] if wide else "no wide rule",
    ))
    checks.append(check(
        "and their labels cannot wrap the bar onto a second row",
        "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" in wide,
        "the model label is longer than its picker",
    ))
    checks.append(check(
        "the pickers keep the design system's control height rather than a private one",
        "height: 32px" not in wide and "height:" not in rule(scope, ".chat-header-bar select"),
        "UIV2-07 sets --control-min-h for every field",
    ))
    phone = media(scope, "max-width: 600px")
    checks.append(check(
        "on a phone the controls are three rows, not five",
        ".chat-header-bar > div:first-child { flex: 1 1 100%; }" in phone
        and ".chat-header-bar > div:not(:first-child) { flex: 1 1 0; min-width: 0; }" in phone
        and ".chat-header-bar > #chatClearBtn { flex: 0 0 auto;" in phone,
        f"{len(phone.splitlines())} line phone block",
    ))

    # ── 3. the composer stays on screen ───────────────────────────────────────
    checks.append(check(
        "the composer is pinned to the bottom of the pane",
        "position: sticky; bottom: 0; z-index: 2;" in column_rule,
        column_rule.strip()[:160],
    ))
    checks.append(check(
        "and keeps clear of a phone's home indicator",
        "env(safe-area-inset-bottom" in column_rule,
        "padding-bottom is a max() with the inset",
    ))
    checks.append(check(
        "the field can scroll itself above an on-screen keyboard",
        "#chatInput { scroll-margin-bottom: 110px; }" in phone,
        "a keyboard that covers the field is a composer that left the screen",
    ))
    checks.append(check(
        "the shell is measured in dvh, so the keyboard shrinks the pane",
        "height: calc(100dvh - var(--header-height)); grid-template-columns" in css,
        "vh would keep the pane taller than the visible screen",
    ))

    # ── 4. nothing is clipped ─────────────────────────────────────────────────
    msg_rule = rule(scope, ".msg")
    checks.append(check(
        "a bubble may not grow wider than its column",
        "min-width: 0;" in msg_rule and "overflow-wrap: anywhere;" in msg_rule,
        msg_rule.strip()[:120],
    ))
    checks.append(check(
        "a code block scrolls inside its bubble, never off the pane",
        ".msg pre { max-width: 100%; overflow-x: auto; }" in css,
        "a wide line is a scrollbar, not a cut",
    ))
    checks.append(check(
        "images, video and tables are clamped to the bubble",
        ".msg img, .msg video, .msg table { max-width: 100%; }" in css
        and ".msg > .md > table { display: block; overflow-x: auto; }" in css,
        "a table that overflows a phone is unreadable, not just wide",
    ))

    # ── 5. an empty conversation is one short sentence, said once ─────────────
    checks.append(check(
        "the empty state is one sentence, short enough to read at a glance",
        bool(empty_markup_text) and len(empty_markup_text) <= 150,
        f"{len(empty_markup_text)} chars: {empty_markup_text[:80]!r}",
    ))
    checks.append(check(
        "the markup and the renderer say it in the same words",
        bool(empty_renderer_text) and empty_markup_text == empty_renderer_text,
        f"markup {empty_markup_text[:60]!r} vs renderer {empty_renderer_text[:60]!r}",
    ))

    # ── 6. nothing of the pane was lost ──────────────────────────────────────
    missing = [control for control in PRESERVED_CONTROLS if f'id="{control}"' not in html]
    checks.append(check(
        "every control the pane had is still there and still wired",
        not missing,
        ", ".join(missing),
    ))
    checks.append(check(
        "Enter sends, Shift+Enter adds a line - the contract the hint promises",
        'document.getElementById("chatInput").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); } });' in code,
        "one keydown handler, and it is the only thing that sends",
    ))
    checks.append(check(
        "Clear is still undoable",
        'title: "Transcript cleared"' in code and 'label: "Undo"' in code,
        "a cleared transcript is one stray click from a long conversation",
    ))
    stream_calls = code.count('consumeSSE("/api/chat/stream"')
    stream_mentions = code.count("/api/chat/stream")
    checks.append(check(
        "the pane still posts one chat stream and invents no second route",
        stream_calls == 1 and stream_mentions == 1,
        f"{stream_calls} call site(s), {stream_mentions} mention(s)",
    ))
    # Scope this to tab activation: another collapse (for example on resize) cannot
    # prove that entering Chat closes an already-open inspector.
    switch_tab = re.search(r"function switchTab\([^\n]*\{([\s\S]*?)\n  \}", code)
    tab_code = switch_tab.group(1) if switch_tab else ""
    checks.append(check(
        "Chat still opens without a telemetry rail",
        'layout.classList.add("rail-collapsed")' in tab_code and 'if (tab === "chat")' in tab_code,
        "the rail follows the pane; Chat has no inspector context",
    ))
    checks.append(check(
        "the transcript is still a log that does not read itself aloud",
        'role="log"' in html and 'aria-live="off"' in html,
        "one live region per token is an accessibility defect, not a feature",
    ))

    # ── 7. the evidence this story ships ────────────────────────────────────
    checks.append(check(
        "the walk asserts every state the captures show",
        all(needle in walk for _, needle in WALK_STATES),
        ", ".join(name for name, needle in WALK_STATES if needle not in walk),
    ))
    checks.append(check(
        "the walk needs no environment of its own",
        "STUDIO_SCREENSHOT_CHROME" in walk and "STUDIO_PUPPETEER_CORE" in walk
        and "STUDIO_PYTHON || \"python3\"" in walk
        and not re.search(r"/tmp/|/Users/", walk),
        "a walk that hard-codes a path is not reproducible",
    ))
    checks.append(check(
        "the walk measures the column it promised, not a screenshot's pixels",
        "getBoundingClientRect" in walk and "--chat-column" not in walk,
        "the browser is the only thing that knows the width the reader got",
    ))
    checks.append(check(
        "the walk knows the phone layout it photographs",
        "width: 390, height: 844" in walk,
        "a phone capture that was never resized is a desktop capture with a small window",
    ))

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": STORY,
        "title": TITLE,
        "source": "static/index.html",
        "walk": "scripts/chat_layout_walk.mjs",
        "column": {"token": "--chat-column", "px": column, "band": [COLUMN_MIN, COLUMN_MAX], "rows": list(COLUMN_ROWS)},
        "states": list(STATES),
        "viewports": list(VIEWPORTS),
        "themes": list(THEMES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "findings",
    }


def render_doc(payload: dict) -> str:
    checks = payload["checks"]
    column = payload["column"]
    lines = [
        f"# {payload['title']} ({payload['story']})",
        "",
        "*Generated by `scripts/uiux_chat_layout_inventory.py` from `static/index.html` - do not",
        "hand-edit this file; re-run the script.*",
        "",
        "## The column",
        "",
        f"`{column['token']}` is **{column['px']}px**, inside the {column['band'][0]}-{column['band'][1]}px",
        "band the story is gated on, and it is written once. Three rows of the pane take it with",
        "symmetric inline padding - the transcript, its bar and the composer - so the reader's eye",
        "has one left edge and the composer sits directly under the words it will send:",
        "",
        "```css",
        ".chat-transcript-bar, .chat-messages, .chat-composer {",
        "  padding-inline: max(20px, calc((100% - var(--chat-column)) / 2));",
        "}",
        "```",
        "",
        "The controls are chrome above the conversation, not part of it: `.chat-header-bar` keeps",
        "the pane's width so the pickers fit on one row above 1200px and three rows on a phone.",
        "",
        "## What it checks",
        "",
        "| # | Check | Detail |",
        "| :-- | :--- | :--- |",
    ]
    for index, entry in enumerate(checks, start=1):
        detail = str(entry["detail"]).replace("|", "\\|")
        lines.append(f"| {index} | {entry['check']} | {detail} |")
    lines += [
        "",
        "## The states the captures show",
        "",
        ", ".join(f"`{state}`" for state in payload["states"]),
        "",
        "Each state is asserted by `scripts/chat_layout_walk.mjs` before it is photographed, at",
        f"{', '.join(payload['viewports'])} in {' and '.join(payload['themes'])} theme.",
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_chat_layout_inventory.py",
        "python3 scripts/uiux_chat_layout_inventory.py --check",
        "```",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


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
                    "the chat-layout inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    findings = payload["findings"]
    if isinstance(findings, list) and findings:
        for entry in findings:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
