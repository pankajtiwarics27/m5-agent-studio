#!/usr/bin/env python3
"""Inventory the responsive and touch-target contract of the six panes (UIUX-P5-03).

    python3 scripts/uiux_responsive_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_responsive_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_responsive_inventory.py --json     # print the inventory

The panes were built at 1440 px and never measured below it. Narrowing them found two defects that
share a cause - a layout that believes its own content is the width it has:

* **A `<select>` is as wide as its longest `<option>`.** The Start pane's recent-folder picker held a
  `/tmp/...` path, the Hall model picker and the Compare arbiter picker held model labels, and each
  pushed its row past the pane. The pane hides its overflow, so the right third of the row - buttons
  included - was drawn outside the viewport with no way to scroll to it.
* **`margin: 0 auto` on a flex item turns off the cross-axis stretch.** The Start shell sized itself
  to its own max-content (589 px) inside a 360 px pane for exactly that reason, so even after the
  select was fixed the whole shell stayed too wide.

The third defect is about fingers rather than widths: a row of controls sat under the 24 px floor of
WCAG 2.5.8 - chips at 23 px, `summary` disclosures at 21 px, the permission badge at 21 px, "copy with
basis" at 18 px, and one button whose inline `min-height: 20px` had escaped the density tokens
entirely. `--target-min` is now the floor, and density does not move it.

This inventory states the layout contract in source. It cannot resize a window; the walk
(`scripts/responsive_walk.mjs`) does that, at five widths, in all six panes, with a touch tap.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "responsive_walk.mjs"
TEST = PROJECT_ROOT / "tests" / "test_responsive.py"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "responsive"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Responsive.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-responsive.md"
DOCS_INDEX = PROJECT_ROOT / "docs" / "README.md"

STORY = "UIUX-P5-03"
TITLE = "Responsive and touch-target regression"

PANES = ("start", "harness", "hall", "council", "debate", "chat")

#: The widths the walk measures, in the order it measures them.
WIDTHS = (1440, 1024, 768, 480, 360)

#: WCAG 2.5.8 Target Size (Minimum), and the token that carries it.
TARGET_MIN = 24

#: Every `max-width` breakpoint in the stylesheet. A new one is a deliberate act, not a drift: the
#: inventory fails until it is added here with the reason.
BREAKPOINTS = {
    1439: "UIV2-05: the contextual inspector becomes a 320px drawer; the central pane keeps its width",
    1279: "UIV2-05: the nav collapses to a 64px icon rail; labels hide, button titles keep the names. UIV2-08: the harness session/workspace list folds away here too, per the approved reference",
    1024: "UIV2-P7-07: with the toolbar folded into the header, the workspace caption runs out of room at these widths; Settings, Search and Inspector fold to their icons and keep aria-labels, titles, handlers and focus rings",
    880: "the hall sidebar folds to a horizontal strip and the council seat grid goes to two columns (the harness sidebar already folded at 1279)",
    860: "the hall shell goes to one column",
    820: "the role grid goes to two columns",
    799: "UIV2-05: single column with one scrollable tab row under the header; the header never wraps past 64px",
    600: "phones: header, tabs, composers and the full-screen modals",
    390: "small phones: the tightest header and rail",
}

#: Controls that used to be under the floor and now read the token.
TARGET_READERS = (
    ".chip",
    ".chk-lbl",
    "summary",
    ".evidence-copy",
    ".harness-perm-badge",
)

#: The rules that let a select and its flex row shrink instead of pushing the pane.
SHRINKERS = {
    "select { min-width: 0; max-width: 100%; }":
        "a select may not be wider than the row it is in",
    ".start-workspace-actions select { flex: 1 1 200px; }":
        "the recent-folder picker joins the row instead of sizing it",
    ".council-settings-content > * { min-width: 0; }":
        "the Hall settings column may shrink below the model label",
}

CAPTURES = (
    "responsive-1440-debate.png",
    "responsive-768-harness.png",
    "responsive-480-council.png",
    "responsive-360-start.png",
    "responsive-360-chat.png",
    "responsive-landscape-debate.png",
)


def rule(html: str, selector: str) -> str:
    """The declaration block of a CSS rule, by exact selector, or ''."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    return match.group(1) if match else ""


def rules_touching(html: str, needle: str) -> list[tuple[str, str]]:
    """Every (selector, declarations) pair whose selector mentions `needle`."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", html):
        selector = re.sub(r"\s+", " ", match.group(1)).strip()
        if needle in selector and not selector.startswith("@"):
            found.append((selector, match.group(2)))
    return found


def media_queries(html: str) -> dict[int, list[str]]:
    """Each `max-width` breakpoint with the selectors it carries."""
    found: dict[int, list[str]] = {}
    for match in re.finditer(r"@media \(max-width: (\d+)px\)\s*\{", html):
        width = int(match.group(1))
        start = match.end() - 1
        depth = 0
        end = start
        for offset in range(start, len(html)):
            if html[offset] == "{":
                depth += 1
            elif html[offset] == "}":
                depth -= 1
                if depth == 0:
                    end = offset
                    break
        block = html[start:end]
        selectors = sorted({sel.strip() for sel in re.findall(r"(?m)^\s*([.#a-zA-Z][^{}\n]*?)\s*\{", block)})
        found.setdefault(width, []).extend(selectors)
    return found


def inline_min_heights(html: str) -> list[dict[str, str]]:
    """Every inline `min-height` in the markup, with the element that carries it."""
    found: list[dict[str, str]] = []
    for match in re.finditer(r"<([a-zA-Z][\w-]*)([^>]*?)style=\"([^\"]*)\"", html):
        style = match.group(3)
        height = re.search(r"min-height:\s*(\d+(?:\.\d+)?)px", style)
        if not height:
            continue
        tag = match.group(1).lower()
        attrs = match.group(2)
        identifier = re.search(r'id="([^"]+)"', attrs)
        found.append(
            {
                "tag": tag,
                "id": identifier.group(1) if identifier else "",
                "px": height.group(1),
                "style": style[:70],
            }
        )
    return found


def walk_checks(walk_body: str) -> list[str]:
    return re.findall(r'record\(\s*\n?\s*`?([^`"]+?)`?,', walk_body)


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

    # ── 1. the target-size floor ────────────────────────────────────────────
    token = re.search(r"--target-min:\s*(\d+)px", html)
    check(
        f"the stylesheet carries one target-size floor, and it is {TARGET_MIN}px",
        bool(token) and int(token.group(1)) == TARGET_MIN,
        f"--target-min: {token.group(1) if token else 'missing'}px",
    )
    compact = rule(html, 'body[data-density="compact"]')
    check(
        "density does not move the floor: a compact layout may look tighter, not be harder to hit",
        "--target-min" not in compact,
        "compact density re-points the target size",
    )
    unread = [
        selector
        for selector in TARGET_READERS
        if not any("var(--target-min)" in body for _sel, body in rules_touching(html, selector))
    ]
    check(
        "every control that was under the floor reads the token",
        not unread,
        ", ".join(unread) or f"{len(TARGET_READERS)} readers",
    )
    undersized = [entry for entry in inline_min_heights(html) if float(entry["px"]) < TARGET_MIN]
    check(
        "no inline style drops a control back under the floor",
        not undersized,
        "; ".join(f"{entry['tag']}#{entry['id']} min-height:{entry['px']}px" for entry in undersized[:6]),
    )

    # ── 2. a select may not set the width of the pane ───────────────────────
    missing = [selector for selector in SHRINKERS if selector not in html]
    check(
        "a select and the flex row it sits in are allowed to shrink",
        not missing,
        ", ".join(missing),
    )
    shell = rule(html, ".onboarding-shell")
    check(
        "the Start shell has a definite width, so `margin: 0 auto` cannot size it to its content",
        "width: 100%" in shell and "max-width: 860px" in shell,
        shell.strip()[:80] or ".onboarding-shell has no rule",
    )
    arbiter = re.search(r'<div id="debateArbiterModel"[^>]*>', html)
    check(
        "the Compare arbiter picker is a flex item that may shrink below its model label",
        bool(arbiter) and "min-width:0" in arbiter.group(0),
        arbiter.group(0) if arbiter else "missing",
    )
    fixed_shells = re.findall(
        r"\.(onboarding|harness|hall|debate|chat)-shell\s*\{[^}]*?(?<!max-)(?<!min-)width:\s*(\d+)px",
        html,
    )
    check(
        "no pane shell is pinned to a pixel width that a narrow pane would clip",
        not fixed_shells,
        ", ".join(f"{name}-shell {width}px" for name, width in fixed_shells),
    )

    # ── 3. the breakpoints are a list, not an accident ──────────────────────
    found = media_queries(html)
    check(
        "the stylesheet has only documented breakpoints",
        set(found) == set(BREAKPOINTS),
        f"found {sorted(found)}, documented {sorted(BREAKPOINTS)}",
    )
    covered = {
        "mode-tab": any(any("mode-tab" in sel for sel in sels) for sels in found.values()),
        "harness-shell": any(any("harness" in sel for sel in sels) for sels in found.values()),
        "hall-shell": any(any("hall" in sel for sel in sels) for sels in found.values()),
        "debate-grid": any(any("debate" in sel for sel in sels) for sels in found.values()),
        "stepper": any(any("stepper" in sel for sel in sels) for sels in found.values()),
        "chat-composer": any(any("chat" in sel for sel in sels) for sels in found.values()),
    }
    check(
        "each pane's layout is addressed at a narrow breakpoint, not only the shared shell",
        all(covered.values()),
        ", ".join(name for name, ok in covered.items() if not ok),
    )
    stepper_rules = rules_touching(html, ".stepper")
    stepper_scrolls = any("overflow-x: auto" in body for _selector, body in stepper_rules)
    stepper_columns = [body for selector, body in stepper_rules if "grid-template-columns" in body]
    check(
        "the one strip that is allowed to overflow scrolls, so the cards past the edge are reachable",
        stepper_scrolls and bool(stepper_columns),
        f"{len(stepper_rules)} stepper rules, scrolls={stepper_scrolls}",
    )

    # ── 4. the evidence ─────────────────────────────────────────────────────
    walked_widths = [width for width in WIDTHS if f"width: {width}," in walk_body]
    check(
        "the walk measures all five widths in all six panes",
        len(walked_widths) == len(WIDTHS) and all(f'"{pane}"' in walk_body for pane in PANES),
        f"widths measured: {walked_widths}",
    )
    audit_start = walk_body.find("const AUDIT =")
    audit_body = walk_body[audit_start : walk_body.find("async function shoot")] if audit_start >= 0 else ""
    check(
        "the walk measures a hit area, not a CSS declaration",
        "getBoundingClientRect" in audit_body
        and "targetMin" in audit_body
        and 'closest("label")' in audit_body,
        "a control inside a label is hit through the label; the walk has to know that",
    )
    check(
        "the walk uses a finger as well as a mouse",
        "page.tap(" in walk_body and "hasTouch: true" in walk_body,
        "a touch-target story that only clicks with a mouse proves nothing",
    )
    check(
        "the walk rotates the phone and checks the sideways scroll did not come back",
        "740" in walk_body and "360" in walk_body and "landscape" in walk_body,
        "landscape is the width nobody re-tests",
    )
    check(
        "the walk names the strips that scroll, rather than silently excusing them",
        "scrollersSeen.add(name)" in walk_body
        and "scrollsTo" in walk_body
        and "scrollersSeen.size" in walk_body,
        "an exception that is not named is a hole",
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
        "assert changed != html" in test_body and len(re.findall(r"def test_", test_body)) >= 10,
        f"{len(re.findall(r'def test_', test_body))} tests in {TEST.name}",
    )
    check(
        "the doc is written, and reachable from the docs index",
        DOC_PATH.exists() and DOCS_INDEX.exists() and DOC_PATH.name in DOCS_INDEX.read_text(encoding="utf-8"),
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
        "generated_by": "scripts/uiux_responsive_inventory.py",
        "surface": "layout and hit areas of the six panes",
        "panes": list(PANES),
        "widths": list(WIDTHS),
        "target_min_px": TARGET_MIN,
        "target_readers": list(TARGET_READERS),
        "shrinkers": SHRINKERS,
        "breakpoints": {str(width): reason for width, reason in BREAKPOINTS.items()},
        "inline_min_heights": inline_min_heights(html),
        "walk": {
            "script": "scripts/responsive_walk.mjs",
            "checks": walk_checks(walk_body),
            "check_count": len(walk_checks(walk_body)),
        },
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P5-03 — Responsive and touch-target regression",
        "",
        "Generated by `scripts/uiux_responsive_inventory.py`. Do not edit by hand: regenerate it, and",
        "the checks behind it stay true.",
        "",
        "## What was wrong",
        "",
        "The panes were built at 1440 px. Narrowed to 480 and 360 they lost their right-hand third with",
        "no scrollbar to reach it, and a row of controls sat under the floor a finger needs.",
        "",
        "| Where | What it looked like | Cause |",
        "| :--- | :--- | :--- |",
        "| Start, the task row | the run button and two small buttons drawn past the right edge, clipped by the pane | a `<select>` is as wide as its longest `<option>`: the recent-folder picker held a `/tmp/...` path |",
        "| Start, the whole pane | the shell was 589 px wide inside a 360 px pane, so everything was drawn off-centre | `margin: 0 auto` on a flex item turns off the cross-axis stretch, so the shell sized to its own content |",
        "| Hall, the single-model settings | the model picker ran past the pane | the same select rule, inside a flex row whose item could not shrink |",
        "| Compare, the arbiter picker | the same | the same |",
        "| Chips, `summary` disclosures, the permission badge, \"copy with basis\" | 18–23 px tall, a coin toss for a fingertip | they never read a target-size token, because there wasn't one |",
        "| Harness, \"Set / Create\" | 20 px tall | an inline `min-height: 20px` that escaped the density tokens entirely |",
        "",
        "## The contract",
        "",
        f"* `--target-min: {payload['target_min_px']}px` is the floor for a hit area — the same",
        "  24 px WCAG 2.5.8 asks for. Density re-points spacing and control heights; it does not move",
        "  this, because density is how tight the layout looks and this is how small a thing a finger",
        "  may have to hit.",
        "* A `<select>` may never set the width of the row it is in: `min-width: 0` lets it shrink and",
        "  `max-width: 100%` stops it at the edge. The full text is still in the open list.",
        "* A pane shell has a definite width, so a centred shell cannot size itself to its content.",
        "* The one strip that is allowed to overflow — the pipeline stepper — scrolls, and the walk",
        "  names the scrollers it relied on rather than excusing them silently.",
        "",
        "| Rule | Why |",
        "| :--- | :--- |",
    ]
    for selector, reason in payload["shrinkers"].items():
        lines.append(f"| `{selector}` | {reason} |")
    lines += [
        "",
        "## The breakpoints",
        "",
        "| Width | What changes |",
        "| :--- | :--- |",
    ]
    for width, reason in payload["breakpoints"].items():
        lines.append(f"| ≤ {width}px | {reason} |")
    lines += [
        "",
        "A new breakpoint has to be added here with its reason before the inventory will accept it, so",
        "the responsive story cannot grow a private set of one-off widths.",
        "",
        "## The evidence",
        "",
        "`node scripts/responsive_walk.mjs --screenshots docs/media/responsive` starts the Compare",
        "fixture and, at 1440, 1024, 768, 480 and 360, walks all six panes and measures:",
        "",
        "* the document's sideways overflow (must be zero);",
        "* every visible control's position — on screen, or inside a strip that scrolls to it, with the",
        "  scrollers named in the output;",
        "* every visible control's hit area, taking the wrapping `<label>` when it is the bigger target;",
        "* whether a status chip is clipping its own text.",
        "",
        "It then taps with a finger: the rail, a starter card. It checks the composer at 360, rotates to",
        "740×360 and measures again. The counts below come from the run recorded in the story's evidence,",
        "not from this file.",
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
                    "the responsive inventory has drifted from the source: "
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
