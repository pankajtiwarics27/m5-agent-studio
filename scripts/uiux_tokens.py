#!/usr/bin/env python3
"""Define and check the design tokens the rest of P1 builds on (UIUX-P1-01).

    python3 scripts/uiux_tokens.py            # regenerate JSON + docs
    python3 scripts/uiux_tokens.py --check    # fail if either drifted, or a token rule broke
    python3 scripts/uiux_tokens.py --json     # print the token set

The frontend already had a redesign layer with two themes. What it did not have
was a way to tell a token from a typo: `var(--nope)` renders as nothing, silently,
and a colour that exists in the dark theme but not the light one fails only when a
user switches themes. So this script parses the token blocks out of
`static/index.html` and fails the build on four things a human reviewer cannot
check by eye across 4,700 lines:

* **a reference with no token** — `var(--x)` where `--x` is defined nowhere;
* **a token with no reference** — a value nobody reads, which is a wish rather
  than a token, kept alive by nothing;
* **an inconsistent theme** — the light theme must define exactly the colour
  tokens the default theme defines, and the density block only the tokens density
  is allowed to move;
* **a contrast regression** — every text token is measured against every surface
  token in every theme, and must clear the floor for its role.

Two deliberate limits. The parser reads the `m5-redesign-system` block and the
token declarations in it, not the superseded legacy block, so this artifact
describes one source of truth instead of two that disagree. And the checks prove
*declarations agree*, not that any element actually looks right: a token can be
referenced by a rule that never matches, which is what the screenshots and the
preservation matrix are for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
TOKENS_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Tokens.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-tokens.md"

SCHEMA_VERSION = "1.0.0"
STORY = "UIUX-P1-01"
STYLE_BLOCK_ID = "m5-redesign-system"

#: Groups are declared here, not inferred: a token that belongs to no group is a
#: token nobody decided about, and the script refuses it rather than filing it
#: under "other". Adding a token therefore means deciding what it is.
GROUPS: dict[str, tuple[str, ...]] = {
    "colour": (
        "bg", "bg-elev", "bg-card", "bg-hover", "bg-code", "border", "border-hi",
        "text", "text-dim", "text-faint", "accent", "primary-text",
        "cyan", "emerald", "amber", "rose", "purple", "ring",
    ),
    "typography": (
        "text-2xs", "text-xs", "text-sm", "text-base",
        "weight-medium", "weight-semibold", "lh-tight", "lh-base", "tracking-tight",
    ),
    "space": ("space-1", "space-2", "space-3", "space-4", "space-5"),
    "radius": ("radius-sm", "radius-md", "radius-lg"),
    "density": ("control-min-h", "control-min-h-sm", "strip-pad-y", "card-pad", "row-gap"),
    "motion": ("ease-out", "dur-fast"),
    "shell": ("sans", "mono", "header-height"),
}
GROUP_OF = {f"--{name}": group for group, names in GROUPS.items() for name in names}

#: Text roles and the contrast floor each must clear against every surface token.
#: A number with a reason: body text aims well past AA, secondary text is AA, and
#: decorative text still has to be readable rather than "faint by design".
TEXT_ROLES: dict[str, float] = {"text": 7.0, "text-dim": 4.5, "text-faint": 4.5}
SURFACES = ("bg", "bg-elev", "bg-card")
#: Text drawn on the accent fill (primary buttons) — one pair, same AA floor.
FILL_PAIRS: tuple[tuple[str, str, float], ...] = (("primary-text", "accent", 4.5),)

DECLARATION = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*(.+?);?\s*$")
REFERENCE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


class TokenError(Exception):
    """A token rule the tree violates."""


def _strip_comments(text: str) -> str:
    """Remove `/* ... */` comments so prose about a token is not read as a reader.

    The token block documents itself in comments, and a comment that mentions
    `var(--example)` must not make `--example` look referenced — or, worse, make an
    undefined name look defined. Strings are left alone: a rule that builds a
    `var()` inside a JS template literal is still a real reader.
    """
    return COMMENT.sub("", text)


def _style_blocks(html: str) -> dict[str, str]:
    html = _strip_comments(html)
    blocks = {}
    for match in re.finditer(r"<style([^>]*)>(.*?)</style>", html, re.DOTALL):
        attrs, body = match.group(1), match.group(2)
        id_match = re.search(r'id="([^"]+)"', attrs)
        blocks[id_match.group(1) if id_match else ""] = body
    return blocks


def _block_body(style: str, selector: str) -> str:
    """Return the declarations of one top-level selector block."""
    start = style.find(selector + " {")
    if start == -1:
        raise TokenError(f"the token block {selector!r} is missing from the style layer")
    open_brace = style.index("{", start)
    depth = 0
    for index in range(open_brace, len(style)):
        char = style[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return style[open_brace + 1 : index]
    raise TokenError(f"the token block {selector!r} is not closed")


def _declarations(body: str, where: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("/*", "*", "/*")) or stripped.startswith("}"):
            continue
        match = DECLARATION.match(line)
        if not match:
            # Only properties that look like tokens are interesting here; a stray
            # `color-scheme: dark` is not a token and must not be invented as one.
            if "--" in stripped:
                raise TokenError(f"cannot read the token declaration {stripped!r} in {where}")
            continue
        name, value = match.group(1), match.group(2).strip()
        if name in tokens:
            raise TokenError(f"{name} is declared twice in {where}")
        tokens[name] = value
    return tokens


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    if not match:
        raise TokenError(
            f"the colour token value {value!r} is not a 6-digit hex colour, so its contrast "
            "cannot be measured; add the parser support before adding the value"
        )
    digits = match.group(1)
    return tuple(int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _relative_luminance(value: str) -> float:
    def channel(raw: float) -> float:
        return raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(v) for v in _hex_to_rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    first, second = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _length_px(value: str) -> float | None:
    match = re.fullmatch(r"(-?[0-9.]+)px", value.strip())
    return float(match.group(1)) if match else None


def _describe(value: str) -> str:
    px = _length_px(value)
    if px is not None:
        return f"{value} ({px:g}px)"
    return value


def read_tokens() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    blocks = _style_blocks(html)
    if STYLE_BLOCK_ID not in blocks:
        raise TokenError(f"the style block #{STYLE_BLOCK_ID} is missing from {FRONTEND.name}")
    style = blocks[STYLE_BLOCK_ID]

    default = _declarations(_block_body(style, ":root"), ":root")
    light = _declarations(_block_body(style, 'body[data-theme="light"]'), 'body[data-theme="light"]')
    compact = _declarations(
        _block_body(style, 'body[data-density="compact"]'), 'body[data-density="compact"]'
    )

    unclassified = sorted(name for name in default if name not in GROUP_OF)
    if unclassified:
        raise TokenError(
            "these tokens belong to no declared group, so nothing documents what they are for: "
            + ", ".join(unclassified)
        )
    missing = sorted(name for name in GROUP_OF if name not in default)
    if missing:
        raise TokenError(
            "these tokens are in a group but not declared in :root: " + ", ".join(missing)
        )

    references: dict[str, int] = {}
    for match in REFERENCE.finditer(_strip_comments(html)):
        references[match.group(1)] = references.get(match.group(1), 0) + 1

    undefined = sorted(name for name in references if name not in default)
    if undefined:
        raise TokenError(
            "these var() references name no declared token and render as nothing: "
            + ", ".join(f"{name} ({references[name]}x)" for name in undefined)
        )
    orphans = sorted(name for name in default if not references.get(name))
    if orphans:
        raise TokenError(
            "these tokens are declared but nothing reads them, so they are wishes rather than "
            "tokens: " + ", ".join(orphans)
        )

    colour = {f"--{name}" for name in GROUPS["colour"]}
    if set(light) - colour:
        raise TokenError(
            "the light theme declares non-colour tokens; a theme may only re-point colour: "
            + ", ".join(sorted(set(light) - colour))
        )
    absent = sorted(colour - set(light))
    if absent:
        raise TokenError(
            "the light theme is missing colour tokens the default theme defines, so switching "
            "themes silently falls back to dark values: " + ", ".join(absent)
        )
    density = {f"--{name}" for name in GROUPS["density"]}
    if set(compact) != density:
        extra = sorted(set(compact) - density)
        absent_density = sorted(density - set(compact))
        raise TokenError(
            "the density block must re-point exactly the density tokens"
            + (f"; it also re-points {', '.join(extra)}" if extra else "")
            + (f"; it does not re-point {', '.join(absent_density)}" if absent_density else "")
        )

    themes = {"dark": default, "light": {**default, **light}}
    measured: list[dict[str, object]] = []
    failures: list[str] = []
    for theme, values in themes.items():
        for role, floor in TEXT_ROLES.items():
            foreground = str(values[f"--{role}"])
            for surface in SURFACES:
                background = str(values[f"--{surface}"])
                ratio = contrast_ratio(foreground, background)
                measured.append(
                    {
                        "theme": theme,
                        "foreground": f"--{role}",
                        "background": f"--{surface}",
                        "ratio": round(ratio, 2),
                        "floor": floor,
                        "passes": ratio >= floor,
                    }
                )
                if ratio < floor:
                    failures.append(
                        f"--{role} on --{surface} in the {theme} theme is {ratio:.2f}:1, "
                        f"below the {floor:.1f}:1 floor"
                    )
        for foreground_name, background_name, floor in FILL_PAIRS:
            foreground = str(values[f"--{foreground_name}"])
            background = str(values[f"--{background_name}"])
            ratio = contrast_ratio(foreground, background)
            measured.append(
                {
                    "theme": theme,
                    "foreground": f"--{foreground_name}",
                    "background": f"--{background_name}",
                    "ratio": round(ratio, 2),
                    "floor": floor,
                    "passes": ratio >= floor,
                }
            )
            if ratio < floor:
                failures.append(
                    f"--{foreground_name} on --{background_name} in the {theme} theme is "
                    f"{ratio:.2f}:1, below the {floor:.1f}:1 floor"
                )
    if failures:
        raise TokenError("contrast regressions: " + "; ".join(failures))

    tokens = []
    for group, names in GROUPS.items():
        for name in names:
            key = f"--{name}"
            tokens.append(
                {
                    "name": key,
                    "group": group,
                    "value": default[key],
                    "theme_overrides": {"light": light[key]} if key in light else {},
                    "density_override": compact.get(key),
                    "references": references[key],
                }
            )

    smallest = min(row["ratio"] for row in measured)
    return {
        "schema_version": SCHEMA_VERSION,
        "story": STORY,
        "statement": (
            "The design tokens of the M5 Studio shell: colour, typography, space, radius, density, "
            "motion and shell metrics, defined once in static/index.html and checked for orphaned "
            "tokens, undefined references, theme parity and contrast. Checks prove declarations "
            "agree; they do not prove a screen looks right."
        ),
        "source": {
            "frontend": "static/index.html",
            "style_block": f"#{STYLE_BLOCK_ID}",
            "style_block_sha256": hashlib.sha256(style.encode("utf-8")).hexdigest(),
            "superseded_blocks": [
                "the first <style> block declares an older palette that this one overrides; "
                "it is kept only so a stale rule cannot resolve to an undefined name"
            ],
        },
        "theme_selectors": {
            "dark": ":root (default)",
            "light": 'body[data-theme="light"]',
            "density": 'body[data-density="compact"]',
        },
        "contrast_floors": {"text_roles": TEXT_ROLES, "surfaces": list(SURFACES), "fill_pairs": [
            {"foreground": f"--{fg}", "background": f"--{bg}", "floor": floor}
            for fg, bg, floor in FILL_PAIRS
        ]},
        "totals": {
            "tokens": len(tokens),
            "groups": len(GROUPS),
            "references": sum(references.values()),
            "theme_overrides": len(light),
            "density_overrides": len(compact),
            "contrast_pairs": len(measured),
            "lowest_contrast_ratio": round(smallest, 2),
        },
        "groups": {group: list(names) for group, names in GROUPS.items()},
        "tokens": tokens,
        "contrast": measured,
        "checks": [
            "every var(--token) resolves to a declared token",
            "every declared token is referenced at least once",
            "every token belongs to a declared group",
            "the light theme defines exactly the colour tokens the default theme defines",
            "the density block re-points exactly the density tokens",
            "every text token clears its floor against every surface, in every theme",
        ],
        "limits": [
            "A referenced token can still be referenced by a rule that never matches; this artifact "
            "does not render the page.",
            "Contrast is measured on the token values, not on what a browser composites: "
            "translucent overlays and images are outside the measurement.",
            "The legacy token block is parsed by nothing here; it is superseded, not verified.",
        ],
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    lines = [
        "# M5 2.0 design tokens",
        "",
        f"Generated by `python3 scripts/uiux_tokens.py` — **never hand-edit this page**.",
        f"It is the `{STORY}` token layer: what the shell may use, and what stops a token from",
        "quietly disappearing.",
        "",
        f"* {totals['tokens']} tokens in {totals['groups']} groups, "
        f"{totals['references']} `var()` references",
        f"* Themes: dark (default) and `body[data-theme=\"light\"]`, "
        f"{totals['theme_overrides']} colour overrides",
        f"* Density: `body[data-density=\"compact\"]` re-points {totals['density_overrides']} tokens",
        f"* {totals['contrast_pairs']} contrast pairs measured; lowest is "
        f"{totals['lowest_contrast_ratio']}:1",
        "",
        "## What this page is, and is not",
        "",
        "The tokens are the values later P1 stories build on. They are declared once, in",
        f"`static/index.html`'s `{payload['source']['style_block']}` block, and this page is",
        "derived from that block rather than written beside it.",
        "",
        "The checks prove that declarations agree with each other and with their readers:",
        "",
    ]
    lines += [f"* {check}" for check in payload["checks"]]
    lines += [
        "",
        "They do not prove a screen looks right. A token can be referenced by a rule that never",
        "matches, contrast is measured on declared values rather than on what the browser",
        "composites, and nothing here renders a page. The screenshots and the preservation",
        "matrix cover what this cannot.",
        "",
        "## Groups",
        "",
        "| Group | Tokens | Density re-point | Light theme re-point |",
        "|---|---|---|---|",
    ]
    for group, names in payload["groups"].items():
        rows = [token for token in payload["tokens"] if token["group"] == group]
        density = sum(1 for row in rows if row["density_override"])
        light = sum(1 for row in rows if row["theme_overrides"])
        lines.append(f"| {group} | {', '.join(f'`{n}`' for n in names)} | {density} | {light} |")

    lines += ["", "## The tokens", ""]
    for group in payload["groups"]:
        lines += [f"### {group.capitalize()}", "", "| Token | Value | Light theme | Compact | Readers |", "|---|---|---|---|---|"]
        for token in payload["tokens"]:
            if token["group"] != group:
                continue
            light = token["theme_overrides"].get("light", "—")
            compact = token["density_override"] or "—"
            lines.append(
                f"| `{token['name']}` | `{token['value']}` | `{light}` | `{compact}` | "
                f"{token['references']} |"
            )
        lines.append("")

    lines += [
        "## Contrast",
        "",
        "Measured with the WCAG relative-luminance formula on the declared hex values. Floors: "
        + ", ".join(f"`--{role}` ≥ {floor:.1f}:1" for role, floor in TEXT_ROLES.items())
        + ", and the primary-button pair ≥ "
        f"{FILL_PAIRS[0][2]:.1f}:1.",
        "",
        "| Theme | Foreground | Background | Ratio | Floor |",
        "|---|---|---|---|---|",
    ]
    for row in payload["contrast"]:
        lines.append(
            f"| {row['theme']} | `{row['foreground']}` | `{row['background']}` | "
            f"{row['ratio']}:1 | {row['floor']:.1f}:1 |"
        )

    lines += [
        "",
        "## Adding a token",
        "",
        "1. Decide its group. A token in no group fails the generator, because a value nobody",
        "   classified is a value nobody designed.",
        "2. Declare it in `:root`, and in `body[data-theme=\"light\"]` if it is a colour.",
        "3. Read it from a rule. A token with no reader fails, so the scale grows when a rule",
        "   needs it rather than in anticipation.",
        "4. Regenerate with `python3 scripts/uiux_tokens.py` and keep the contrast table green.",
        "",
        "## Limits",
        "",
    ]
    lines += [f"* {limit}" for limit in payload["limits"]]
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Define and check the M5 2.0 design tokens.")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the token set and exit")
    parser.add_argument("--quiet", action="store_true", help="say nothing on success")
    args = parser.parse_args(argv)

    try:
        payload = read_tokens()
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload_json = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    payload_doc = render_doc(payload)

    if args.json:
        print(payload_json, end="")
        return 0

    totals = payload["totals"]
    summary = (
        f"{totals['tokens']} tokens in {totals['groups']} groups, {totals['references']} references, "
        f"{totals['contrast_pairs']} contrast pairs (lowest {totals['lowest_contrast_ratio']}:1)"
    )

    if args.check:
        drifted = []
        for path, expected in ((TOKENS_PATH, payload_json), (DOC_PATH, payload_doc)):
            if not path.exists():
                print(f"error: {_rel(path)} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(_rel(path))
        if drifted:
            print(
                "error: the design tokens have drifted from the stylesheet: "
                + ", ".join(drifted)
                + "\n       Regenerate it (the change belongs to the story):"
                + "\n           python3 scripts/uiux_tokens.py",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            print(f"Design tokens are up to date: {summary}")
        return 0

    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(payload_json, encoding="utf-8")
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(payload_doc, encoding="utf-8")
    if not args.quiet:
        print(f"Wrote {_rel(TOKENS_PATH)} and {_rel(DOC_PATH)}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
