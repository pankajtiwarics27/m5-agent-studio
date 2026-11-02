#!/usr/bin/env python3
"""Inventory the visual and interaction polish layer (UIUX-P5-05).

    python3 scripts/uiux_polish_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_polish_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_polish_inventory.py --json     # print the inventory

The last story of the polish phase is not a new surface. It is the answer to two
questions a reader asks without ever saying them: *did the thing I pointed at notice
me*, and *did the thing I pressed move*.

Before this story the stylesheet answered neither consistently. Thirty of fifty-five
control kinds changed nothing on hover, twenty-eight of fifty-five changed nothing when
pressed, two of the pane's main inputs had no focus ring at all, and every corner and
every duration was a hand-typed number - `8px` here, `9px` two rules down, `150ms` here,
`0.35s` there - so "the corners are consistent" was a claim nobody could check quickly.

The layer this story added is small and the rules below are what keeps it small:

* a radius is a `--radius-*` token, or `0`, or `50%`; a duration is a `--dur-*` token or
  `0`; `transition: all` is gone, because it animates properties nobody chose;
* every control a reader can point at answers the pointer, and answers with something
  other than a transform - so a machine that asked for reduced motion loses nothing;
* pressing changes the surface (`--bg-press` and the `:active` rules), so the press is
  not a guess;
* anything focusable draws the shared ring, and the one place a control's own ring is
  removed is a place an ancestor draws it instead (`:focus-within`) - the keyboard story
  made that decision for the two composers deliberately;
* a disabled control says so: `cursor: not-allowed`, and never faded so far that its
  label stops being readable.

The dynamic half of this story is `scripts/polish_walk.mjs`, which is the only thing that
measures a rendered page: it takes focus through CDP's forced `:focus-visible`, moves a
real pointer, holds the mouse down for `:active`, blocks clicks so measuring changes
nothing, walks both themes, and captures the four states this page lists. This inventory
cannot run a browser; it checks the source those measurements are made against.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "polish_walk.mjs"
MEDIA = PROJECT_ROOT / "docs" / "media" / "polish"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Polish.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-polish.md"

STORY = "UIUX-P5-05"

# The classes the walk measures for a press, and the ones it measures for a pointer. Kept
# here as literals rather than derived from the walk so the two have to agree on purpose:
# a class that loses its `:active` rule is a finding, not a silently shorter list.
PRESSABLE = (
    ".btn",
    ".btn-xs",
    ".chip",
    ".step-card",
    ".starter-card",
    ".harness-perm-badge",
    ".mode-tab",
    ".toast-dismiss",
    ".evidence-copy",
    ".cmd-item",
    ".skill-pill",
    ".harness-timeline-row",
    ".start-recent-row",
    "summary",
    "select",
)
HOVERABLE = (
    "button",
    "select",
    "summary",
    "input",
    "textarea",
    "label:has(> input)",
)
HOVERABLE_PATTERNS = {
    "button": r"(?<![\w.-])button(?![\w-])",
    "select": r"(?<![\w.-])select(?![\w-])",
    "summary": r"(?<![\w.-])summary(?![\w-])",
    "input": r"(?<![\w.-])input(?![\w-])",
    "textarea": r"(?<![\w.-])textarea(?![\w-])",
    "label:has(> input)": r"label[^\s,]*:has\(",
}

# The reduced-motion block neutralises every animation by shrinking it to a hundredth of a
# millisecond rather than to `none`. Anything at or below this is that neutraliser, not a
# duration somebody chose.
MOTION_NEUTRALISED_MS = 0.02

# Values a radius may keep without reading a token: nothing at all, and a circle. A
# percentage is not a corner scale, it is "as round as this element is tall".
RADIUS_LITERALS = ("0", "0px", "50%")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def style_blocks(html: str) -> list[tuple[str, int]]:
    """Every ``<style>`` body, with the line it starts on.

    Both blocks are read. The first one still carries the superseded colour literals the
    token block overrides - that duplication is deliberate and documented - but no corner
    and no duration lives there any more, and this check is what keeps it that way.
    """
    blocks = []
    for match in re.finditer(r"<style[^>]*>(.*?)</style>", html, re.S):
        blocks.append((match.group(1), _line(html, match.start(1))))
    return blocks


def iter_rules(css: str, first_line: int = 1) -> list[dict[str, str | int]]:
    """Flat rules with their declarations, at-rule context and line number.

    A brace-matching walk rather than a regex: `@media` blocks nest, and a regex that
    flattens them reports a media-query condition as if it were a declaration. One buffer
    serves both roles - the text before a `{` is that block's selector, and the text before
    the matching `}` is its body - which is why an at-rule's own text never leaks into the
    selector of the rule inside it.
    """
    rules: list[dict[str, str | int]] = []
    frames: list[dict[str, object]] = []
    buffer: list[str] = []
    for index, char in enumerate(css):
        if char == "{":
            context = " ".join(str(frame["selector"]) for frame in frames)
            frames.append({"selector": "".join(buffer).strip(), "context": context, "start": index})
            buffer = []
        elif char == "}":
            if frames:
                frame = frames.pop()
                body = " ".join("".join(buffer).split())
                buffer = []
                if body:
                    rules.append(
                        {
                            "selector": str(frame["selector"]),
                            "body": body,
                            "context": str(frame["context"]),
                            "line": first_line + _line(css, int(frame["start"])) - 1,
                        }
                    )
        else:
            buffer.append(char)
    return rules


def declarations(body: str) -> list[tuple[str, str]]:
    """``property: value`` pairs from one rule body, comments already gone."""
    pairs = []
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        prop, _, value = chunk.partition(":")
        prop = prop.strip().lower()
        value = " ".join(value.split())
        if prop:
            pairs.append((prop, value))
    return pairs


def frontend_css(html: str) -> list[dict[str, str | int]]:
    """All rules from all style blocks, with comments stripped."""
    rules: list[dict[str, str | int]] = []
    for css, first_line in style_blocks(html):
        stripped = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), css, flags=re.S)
        rules.extend(iter_rules(stripped, first_line))
    return rules


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def radius_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    findings = []
    for rule in rules:
        for prop, value in declarations(str(rule["body"])):
            if "border-radius" not in prop:
                continue
            rest = re.sub(r"var\(--radius-[a-z-]+\)", " ", value)
            leftovers = [token for token in rest.replace("/", " ").split() if token not in RADIUS_LITERALS]
            if leftovers:
                findings.append(
                    {
                        "check": "radius-token",
                        "detail": f"line {rule['line']}: `{rule['selector']}` sets {prop}: {value}",
                    }
                )
    return findings


def motion_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    findings = []
    for rule in rules:
        for prop, value in declarations(str(rule["body"])):
            if prop not in ("transition", "transition-duration", "animation", "animation-duration"):
                continue
            if prop in ("transition", "transition-property") and re.search(r"\ball\b", value):
                findings.append(
                    {
                        "check": "no-transition-all",
                        "detail": f"line {rule['line']}: `{rule['selector']}` animates every property",
                    }
                )
                continue
            rest = re.sub(r"var\(--[a-z0-9-]+\)", " ", value)
            leftovers = []
            for found in re.findall(r"(?<![\w-])(\d*\.?\d+)(m?)s\b", rest):
                amount = float(found[0]) / (1000.0 if found[1] else 1.0) * 1000.0
                if amount > MOTION_NEUTRALISED_MS:
                    leftovers.append(f"{found[0]}{found[1]}s")
            if leftovers:
                findings.append(
                    {
                        "check": "motion-token",
                        "detail": f"line {rule['line']}: `{rule['selector']}` times {prop}: {value}",
                    }
                )
    return findings


def _selectors(rules: list[dict[str, str | int]], pattern: str) -> list[dict[str, str | int]]:
    return [rule for rule in rules if re.search(pattern, str(rule["selector"]))]


def press_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    findings = []
    active = " ".join(str(rule["selector"]) for rule in _selectors(rules, r":active(?![\w-])"))
    for name in PRESSABLE:
        if name == "summary" or name == "select":
            pattern = rf"(?<![\w-]){name}(?![\w-])"
            if not re.search(pattern, active):
                findings.append({"check": "press-coverage", "detail": f"no `:active` rule answers `{name}`"})
            continue
        if name not in active:
            findings.append({"check": "press-coverage", "detail": f"no `:active` rule answers `{name}`"})
    press_rules = [rule for rule in _selectors(rules, r":active(?![\w-])") if "--bg-press" in str(rule["body"])]
    if not press_rules:
        findings.append(
            {"check": "press-token", "detail": "`--bg-press` is declared but no `:active` rule presses anything with it"}
        )
    return findings


def hover_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    findings = []
    # Compound by compound, not rule by rule: `select:hover, input[type="text"]:hover-not` is a
    # hover for the select and for nothing else, and reading the whole list would credit the input
    # with an answer it does not have.
    hover = " ".join(
        compound
        for rule in _selectors(rules, r":hover(?![\w-])")
        for compound in str(rule["selector"]).split(",")
        if re.search(r":hover(?![\w-])", compound)
    )
    # A hover on the label around a checkbox is not a hover on the checkbox: `:has()` names the
    # element it depends on, so those parents are removed before the tag names are looked for.
    hover_without_has = re.sub(r":has\([^)]*\)", " ", hover)
    for name in HOVERABLE:
        haystack = hover if name.startswith("label") else hover_without_has
        if not re.search(HOVERABLE_PATTERNS[name], haystack):
            findings.append({"check": "hover-coverage", "detail": f"no `:hover` rule answers `{name}`"})
    # A hover that only moves the element is a hover a reduced-motion reader cannot see.
    for rule in _selectors(rules, r":hover(?![\w-])"):
        body = str(rule["body"])
        painted = re.search(r"(background|border-color|color|box-shadow|filter|outline)", body)
        movement = re.search(r"transform", body)
        if movement and not painted:
            findings.append(
                {
                    "check": "hover-not-movement",
                    "detail": f"line {rule['line']}: hovering `{rule['selector']}` only moves it",
                }
            )
    return findings


def primary_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    """The primary action's hover has to be its own rule, and its own direction."""
    findings = []
    resting = [
        rule
        for rule in rules
        if ".btn-primary" in str(rule["selector"])
        and ":hover" not in str(rule["selector"])
        and ":disabled" not in str(rule["selector"])
    ]
    hovering = [rule for rule in rules if ".btn-primary:hover" in str(rule["selector"])]
    if not hovering:
        findings.append({"check": "primary-hover", "detail": "`.btn-primary` has no hover rule of its own"})
    if any("--accent-hi" in str(rule["body"]) for rule in resting):
        findings.append(
            {
                "check": "primary-hover",
                "detail": "a resting `.btn-primary` rule already paints `--accent-hi`, so hovering changes nothing",
            }
        )
    if hovering and not any("--accent-hi" in str(rule["body"]) for rule in hovering):
        findings.append(
            {"check": "primary-hover", "detail": "`.btn-primary:hover` does not move to `--accent-hi`"}
        )
    return findings


def ring_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    """A ring may be removed from a control only where an ancestor draws it instead."""
    findings = []
    composed = [
        str(rule["selector"])
        for rule in _selectors(rules, r":focus-within")
        if re.search(r"outline:\s*[1-9]\d*px", str(rule["body"]))
    ]
    for rule in _selectors(rules, r":focus-visible|:focus(?!-)"):
        body = str(rule["body"])
        if not re.search(r"outline:\s*none", body):
            continue
        selector = str(rule["selector"])
        owner = selector.split(":focus")[0].strip()
        owner_class = re.search(r"\.([a-z][\w-]*)", owner)
        if not owner_class:
            findings.append(
                {"check": "ring-removal", "detail": f"line {rule['line']}: `{selector}` removes the ring with no box to carry it"}
            )
            continue
        if not any(owner_class.group(1) in name for name in composed):
            findings.append(
                {
                    "check": "ring-removal",
                    "detail": f"line {rule['line']}: `{selector}` removes the ring and no `:focus-within` rule draws it on `.{owner_class.group(1)}`",
                }
            )
    return findings


def disabled_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    findings = []
    disabled = _selectors(rules, r":disabled")
    # Checked per tag rather than once: a single `.btn:disabled` rule saying `not-allowed` says
    # nothing about a disabled field, and a field that keeps the pointer cursor invites a press
    # that the server will refuse.
    for tag in ("button", "input", "textarea", "select"):
        owners = [
            rule
            for rule in disabled
            if re.search(rf"(?<![\w.-]){tag}(?![\w-])", str(rule["selector"]))
        ]
        if not any("not-allowed" in str(rule["body"]) for rule in owners):
            findings.append(
                {
                    "check": "disabled-honest",
                    "detail": f"no `:disabled` rule covering `{tag}` says `cursor: not-allowed`",
                }
            )
    for rule in disabled:
        for prop, value in declarations(str(rule["body"])):
            if prop != "opacity":
                continue
            numbers = re.findall(r"\d*\.?\d+", value)
            if numbers and float(numbers[0]) < 0.4:
                findings.append(
                    {
                        "check": "disabled-honest",
                        "detail": f"line {rule['line']}: a disabled control is faded to {value}, which hides its reason",
                    }
                )
    pointer = [
        rule for rule in rules if re.search(r"cursor:\s*pointer", str(rule["body"]))
    ]
    if not pointer:
        findings.append({"check": "pointer", "detail": "nothing sets `cursor: pointer`"})
    elif not any(".btn" in str(rule["selector"]) or "button" in str(rule["selector"]) for rule in pointer):
        findings.append(
            {"check": "pointer", "detail": "no button or `.btn` rule says `cursor: pointer`"}
        )
    return findings


def reduced_motion_findings(rules: list[dict[str, str | int]]) -> list[dict[str, str]]:
    """Every transition the layer added is still turned off for a reader who asked."""
    wrapper = "prefers-reduced-motion"
    if not any(wrapper in str(rule["context"]) for rule in rules):
        return [
            {
                "check": "reduced-motion",
                "detail": "no `prefers-reduced-motion` block neutralises the transitions the layer added",
            }
        ]
    # The neutraliser has to be universal: a block that only quiets one component leaves every
    # transition this layer added running for a reader who asked it not to.
    neutralised = [
        rule
        for rule in rules
        if wrapper in str(rule["context"])
        and "transition-duration" in str(rule["body"])
        and str(rule["selector"]).strip().startswith("*")
    ]
    if not neutralised:
        return [
            {
                "check": "reduced-motion",
                "detail": "no universal `prefers-reduced-motion` rule still neutralises transition durations",
            }
        ]
    return []


def walk_findings(walk_source: str) -> list[dict[str, str]]:
    findings = []
    required = {
        "forced focus": "forcePseudoState",
        "reduced motion": "prefers-reduced-motion",
        "captures": "--screenshots",
        "themes": "--theme",
        "page errors": "pageerror",
        "machine output": "--json",
    }
    for what, needle in required.items():
        if needle not in walk_source:
            findings.append({"check": "walk", "detail": f"`scripts/polish_walk.mjs` no longer measures {what}"})
    return findings


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    rules = frontend_css(html)
    walk_source = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    findings = []
    findings.extend(radius_findings(rules))
    findings.extend(motion_findings(rules))
    findings.extend(press_findings(rules))
    findings.extend(hover_findings(rules))
    findings.extend(primary_findings(rules))
    findings.extend(ring_findings(rules))
    findings.extend(disabled_findings(rules))
    findings.extend(reduced_motion_findings(rules))
    findings.extend(walk_findings(walk_source))

    radius_rules = [
        rule
        for rule in rules
        if any("border-radius" in prop for prop, _ in declarations(str(rule["body"])))
    ]
    motion_rules = [
        rule
        for rule in rules
        if any(prop.startswith("transition") or prop.startswith("animation") for prop, _ in declarations(str(rule["body"])))
    ]
    body = " ".join(str(rule["body"]) for rule in rules)
    captures = []
    if MEDIA.is_dir():
        for path in sorted(MEDIA.glob("*.png")):
            size = png_size(path)
            theme = "light" if "light" in path.name else "dark"
            captures.append(
                {
                    "file": f"docs/media/polish/{path.name}",
                    "theme": theme,
                    "width": size[0] if size else 0,
                    "height": size[1] if size else 0,
                    "bytes": path.stat().st_size,
                }
            )

    return {
        "story": STORY,
        "generated_by": "scripts/uiux_polish_inventory.py",
        "source": {
            "frontend": "static/index.html",
            "walk": "scripts/polish_walk.mjs",
            "style_blocks": len(style_blocks(html)),
            "rules": len(rules),
        },
        "tokens": {
            "radius": sorted(set(re.findall(r"var\((--radius-[a-z-]+)\)", body))),
            "motion": sorted(set(re.findall(r"var\((--dur-[a-z-]+)\)", body))),
            "press": sorted(set(re.findall(r"var\((--bg-press)\)", body))),
        },
        "layer": {
            "pressable": list(PRESSABLE),
            "hoverable": list(HOVERABLE),
            "press_rules": [str(rule["selector"]) for rule in _selectors(rules, r":active(?![\w-])")],
            "hover_rules": [str(rule["selector"]) for rule in _selectors(rules, r":hover(?![\w-])")],
            "ring_rules": [str(rule["selector"]) for rule in _selectors(rules, r"focus-visible|focus-within")],
            "composed_ring_rules": [
                str(rule["selector"]) for rule in _selectors(rules, r":focus-within")
            ],
            "disabled_rules": [str(rule["selector"]) for rule in _selectors(rules, r":disabled")],
        },
        "totals": {
            "radius_rules": len(radius_rules),
            "motion_rules": len(motion_rules),
            "press_rules": len(_selectors(rules, r":active(?![\w-])")),
            "hover_rules": len(_selectors(rules, r":hover(?![\w-])")),
            "captures": len(captures),
            "findings": len(findings),
        },
        "captures": captures,
        "findings": findings,
    }


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def render_doc(payload: dict) -> str:
    totals = payload["totals"]
    tokens = payload["tokens"]
    lines = [
        "# M5 2.0 polish layer",
        "",
        "Generated by `python3 scripts/uiux_polish_inventory.py` — **never hand-edit this page**.",
        "It is the `UIUX-P5-05` polish layer: what answers a pointer, what answers a press, what",
        "draws a ring, and what a disabled control says.",
        "",
        f"* {totals['radius_rules']} rules size a corner; {len(tokens['radius'])} radius tokens carry them",
        f"* {totals['motion_rules']} rules time something; {len(tokens['motion'])} duration tokens carry them",
        f"* {totals['hover_rules']} hover rules and {totals['press_rules']} press rules cover {len(payload['layer']['pressable'])} pressable kinds",
        f"* {totals['captures']} captures from the repository's own walk; {len(payload['findings'])} findings",
        "",
        "## What this page is, and is not",
        "",
        "The polish story added no surface. It made the existing one answer: a control that a",
        "reader points at changes something visible, a control they press moves, every focusable",
        "control draws the shared ring, and every corner and duration is a token instead of a",
        "number typed into a rule.",
        "",
        "This page is derived from the stylesheet and from the walk's source. It checks the layer's",
        "shape, not its behaviour - only `scripts/polish_walk.mjs` renders a page, moves a real",
        "pointer and forces `:focus-visible` through CDP. Read the numbers below as what the source",
        "promises; the walk's own summary is what the browser measured.",
        "",
        "## The layer",
        "",
        "| Concern | The rule | Where |",
        "|---|---|---|",
        "| Corners | every `border-radius` is a `--radius-*` token, `0`, or `50%` | `static/index.html`, both style blocks |",
        "| Motion | every duration is a `--dur-*` token or `0`; nothing animates `all` | `static/index.html` |",
        "| Pointer | every pressable kind has a `:hover` rule that paints, not only moves | `#m5-redesign-system` polish layer |",
        "| Press | `:active` moves the surface through `--bg-press` | `#m5-redesign-system` polish layer |",
        "| Ring | every focusable control draws `--ring`; a removed ring needs a `:focus-within` box | shared `:focus-visible` rule, `.prompt-box` |",
        "| Disabled | `cursor: not-allowed` and an opacity that keeps the label readable | shared `:disabled` rule |",
        "",
        "### Tokens the layer reads",
        "",
        "| Group | Tokens |",
        "|---|---|",
        f"| radius | {', '.join(f'`{name}`' for name in tokens['radius'])} |",
        f"| motion | {', '.join(f'`{name}`' for name in tokens['motion'])} |",
        f"| press | {', '.join(f'`{name}`' for name in tokens['press'])} |",
        "",
        "### The kinds that answer a press",
        "",
        ", ".join(f"`{name}`" for name in payload["layer"]["pressable"]),
        "",
        "A field a reader types into is measured for its ring and not for its press: the caret",
        "already answers the press, and forcing a colour change on a text field would be inventing",
        "feedback the platform gives better. A checkbox and a radio are drawn by the platform and",
        "answer with their own tick. Those are the only exemptions, and a third one appearing is a",
        "finding rather than a judgement call.",
        "",
        "## The captures",
        "",
        "| File | Theme | Size |",
        "|---|---|---|",
    ]
    for capture in payload["captures"]:
        lines.append(
            f"| [`{capture['file'].split('/')[-1]}`](media/polish/{capture['file'].split('/')[-1]}) | {capture['theme']} | {capture['width']}×{capture['height']} |"
        )
    if not payload["captures"]:
        lines.append("| — | — | — |")
    lines.extend(
        [
            "",
            "Both themes, at a 1440×900 desktop viewport and a 360×740 narrow one:",
            "",
            "```bash",
            "node scripts/polish_walk.mjs --theme dark  --screenshots docs/media/polish",
            "node scripts/polish_walk.mjs --theme light --screenshots docs/media/polish",
            "```",
            "",
            "## How to regenerate",
            "",
            "1. `python3 scripts/build_studio.py` after any `static/index.html` change - the studio",
            "   bundle embeds the frontend, and every other generated artifact reads the bundle.",
            "2. `python3 scripts/uiux_polish_inventory.py`.",
            "3. `node scripts/polish_walk.mjs --theme dark --screenshots docs/media/polish` and the",
            "   same with `--theme light`, against the seeded fixture the walk starts itself.",
            "",
            "## What this does not prove",
            "",
            "* A hover that is *visible* is not a hover that is *legible*: contrast is measured on",
            "  declared values in [`uiux-2-0-tokens.md`](uiux-2-0-tokens.md), not on what the browser",
            "  composites over a gradient or an image.",
            "* The walk measures two viewports. A control that only exists at another breakpoint is",
            "  covered by [`uiux-2-0-responsive.md`](uiux-2-0-responsive.md), which lists the",
            "  breakpoints the frontend declares and what each one hides.",
            "* Sixty checks in a browser do not replace a reader's eye. The captures are evidence of a",
            "  state, not of a feeling.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory the UIUX-P5-05 polish layer.")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted")
    parser.add_argument("--json", action="store_true", help="print the inventory")
    args = parser.parse_args(argv)

    payload = build_inventory()
    text = dumps(payload)
    doc = render_doc(payload)

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        problems = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is stale")
        if problems:
            print("error: the polish inventory has drifted from the source:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_polish_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the polish layer stopped answering:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"polish OK: {payload['totals']['radius_rules']} radius rules, "
            f"{payload['totals']['motion_rules']} timed rules, {payload['totals']['hover_rules']} hover rules, "
            f"{payload['totals']['press_rules']} press rules, {payload['totals']['captures']} captures, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['radius_rules']} radius rules, {payload['totals']['motion_rules']} timed rules, "
        f"{payload['totals']['captures']} captures, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
