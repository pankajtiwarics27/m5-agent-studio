#!/usr/bin/env python3
"""Implement and check the shared status and evidence components (UIUX-P1-03).

    python3 scripts/uiux_status_components.py            # regenerate JSON + docs
    python3 scripts/uiux_status_components.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_status_components.py --json     # print the component set

Two components, because two things kept going wrong in the same way.

**Status** answered "what state is this in" six different ways across the shell.
The redesign layer then flattened every `.badge` variant onto the same two
tokens, so `.badge.green` and `.badge.red` painted identically: the colour
vocabulary survived only in the words, and nothing stopped a seventh spelling.
The component is one element - a tone, a dot, a hairline and a label - written by
one function, `setStatus()`, which takes the tone and the label together so the
colour and the word cannot drift apart.

**Evidence** answers "what does this claim stand on". A number rendered with no
basis reads exactly like a measured one, and that was the honest-state problem
this program keeps running into: `measured`, `derived`, `unverified`,
`unavailable` are now the whole vocabulary, and the basis is a required
attribute rather than a sentence a writer remembers to add.

The checks are the ones a reviewer cannot do by eye across 4,700 lines:

* a tone or base named in markup but declared nowhere, or declared and never used;
* a colour literal in a tone rule - a tone must name a token, or a theme switch
  silently paints it wrong;
* a tone declared for the hairline but not for the dot, which renders a chip that
  looks finished and has lost its shape cue;
* a status surface still written the old way, `className = "badge " + ...`, which
  is how the label and the tone drift apart;
* a harness state mapped to a tone that does not exist, or to no label;
* an evidence block with no basis, or a basis outside the four.

Two deliberate limits. The checks prove the declarations agree and that every
state has a tone and a label; they do not prove the pixels look right - that is
`scripts/status_walk.mjs`, which renders every state in a browser and
measures the result. And the contrast numbers here are computed from the token
values, not sampled from a rendered screen: they describe the palette the rules
name, which is the thing a rule can get wrong.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
TOKENS_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Tokens.json"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Status_Components.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-status-and-evidence.md"
TOKENS_SCRIPT = PROJECT_ROOT / "scripts" / "uiux_tokens.py"

TONES = ("ok", "warn", "danger", "info", "busy", "neutral")
BASES = ("measured", "derived", "unverified", "unavailable")

# The surfaces this story converted. Each one reported a state; each one is now the
# component. A new state surface joins this tuple rather than inventing a chip.
STATUS_IDS = (
    "harnessActiveStatusBadge",
    "hallStatusBadge",
    "dockerStateBadge",
    "onboardingSummary",
    "sdlcProgressBadge",
)

# The floor for text, and the lower floor for a graphic that is redundant with text:
# the dot repeats the label, so a reader who cannot see it loses nothing.
TEXT_FLOOR = 4.5
GRAPHIC_FLOOR = 3.0


class StatusError(Exception):
    """The components and their rules disagree."""


def _tokens_module():
    spec = importlib.util.spec_from_file_location("uiux_tokens", TOKENS_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise StatusError(f"cannot load {TOKENS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _token_values() -> dict[str, dict[str, str]]:
    payload = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    themes: dict[str, dict[str, str]] = {"dark": {}, "light": {}}
    for token in payload["tokens"]:
        if token["group"] != "colour":
            continue
        name = token["name"].lstrip("-")
        themes["dark"][name] = token["value"]
        themes["light"][name] = token["theme_overrides"].get("light", token["value"])
    return themes


def read_markup(html: str) -> list[dict[str, object]]:
    """Every element that renders the status component."""
    found: list[dict[str, object]] = []
    for match in re.finditer(r"<span([^>]*\bclass=\"status\"[^>]*)>", html):
        attrs = match.group(1)
        ident = re.search(r'id="([^"]+)"', attrs)
        tone = re.search(r'data-tone="([^"]+)"', attrs)
        found.append(
            {
                "id": ident.group(1) if ident else None,
                "tone": tone.group(1) if tone else None,
                "live": 'role="status"' in attrs or 'aria-live="' in attrs,
                "line": html.count("\n", 0, match.start()) + 1,
            }
        )
    return found


def read_tone_rules(html: str) -> dict[str, dict[str, str]]:
    """The declarations behind each tone: the hairline and the dot."""
    rules: dict[str, dict[str, str]] = {}
    for match in re.finditer(r'\.status\[data-tone="(\w+)"\](\s*::before)?\s*\{([^}]*)\}', html):
        tone, before, body = match.group(1), match.group(2), match.group(3)
        rules.setdefault(tone, {})["dot" if before else "hairline"] = body.strip()
    return rules


def read_basis_rules(html: str) -> dict[str, str]:
    rules: dict[str, str] = {}
    for match in re.finditer(r'\.evidence\[data-basis="(\w+)"\]\s*\{([^}]*)\}', html):
        rules[match.group(1)] = match.group(2).strip()
    return rules


def read_states(html: str) -> list[dict[str, str]]:
    """The harness state table: the one place a run's state becomes words."""
    start = html.find("function harnessStatusBadge")
    if start < 0:
        raise StatusError("harnessStatusBadge is missing: the state table is the component's only source")
    tail = html[start : start + 4000]
    pairs = re.findall(r'(\w+):\s*\["(\w+)",\s*"([^"]+)"\]', tail)
    if not pairs:
        raise StatusError("harnessStatusBadge declares no state to tone mapping")
    return [{"state": state, "tone": tone, "label": label} for state, tone, label in pairs]


def read_writers(html: str) -> dict[str, int]:
    """How the component is written, and whether the old way survives anywhere."""
    return {
        "set_status_calls": len(re.findall(r"\bsetStatus\(", html)) - 1,  # minus the definition
        "legacy_badge_writers": len(re.findall(r'className\s*=\s*"badge', html)),
        "evidence_calls": len(re.findall(r"\bevidenceNote\(", html)) - 1,
        "evidence_blocks": len(re.findall(r'class="evidence"', html)),
    }


def _declared_property(body: str) -> str:
    return body.split(":", 1)[1].strip().rstrip(";") if ":" in body else ""


def build_components() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    tokens = _token_values()
    contrasts = _tokens_module()
    tone_rules = read_tone_rules(html)
    basis_rules = read_basis_rules(html)
    markup = read_markup(html)
    states = read_states(html)
    writers = read_writers(html)

    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. Vocabulary in both directions.
    written_tones = {str(item["tone"]) for item in markup if item["tone"]}
    written_tones |= {state["tone"] for state in states}
    for tone in sorted(written_tones - set(TONES)):
        fail("tone-undeclared", f'{tone!r} is rendered but is not one of {", ".join(TONES)}')
    for tone in sorted(set(TONES) - written_tones):
        fail("tone-unused", f'{tone!r} is declared with no surface that can show it')
    for tone in sorted(set(TONES) - set(tone_rules)):
        fail("tone-unstyled", f'{tone!r} has no rule, so it renders as the base chip')

    # 2. Both halves of a tone, and no colour literals.
    for tone, rules in sorted(tone_rules.items()):
        for part in ("hairline", "dot"):
            if part not in rules:
                fail("tone-incomplete", f'{tone!r} declares no {part}, so the cue is half-drawn')
                continue
            value = _declared_property(rules[part])
            if not value:
                fail("tone-empty", f'{tone!r} {part} declares nothing')
            elif not value.startswith("var(--"):
                fail("tone-literal", f'{tone!r} {part} uses {value!r}; a tone must name a token')

    # 3. The surfaces that had a status now have the component.
    by_id = {item["id"]: item for item in markup if item["id"]}
    for ident in STATUS_IDS:
        if ident not in by_id:
            fail("surface-unconverted", f"#{ident} reports a state but does not use the component")
        elif not by_id[ident]["tone"]:
            fail("surface-toneless", f"#{ident} uses the component with no tone")
    if not any(item["live"] for item in markup):
        fail("announce-missing", "no status is a live region, so a change is silent")

    # 4. One writer.
    if writers["legacy_badge_writers"]:
        fail(
            "legacy-writer",
            f'{writers["legacy_badge_writers"]} place(s) still write `className = "badge ..."`, '
            "which sets the words and the colour on separate lines",
        )
    if writers["set_status_calls"] < 8:
        fail("writer-thin", "setStatus() has fewer call sites than the surfaces it replaced")

    # 5. The state table.
    if len(states) < 9:
        fail("state-table-short", f"the state table maps {len(states)} states; the harness has ten")
    for state in states:
        if state["tone"] not in TONES:
            fail("state-tone-unknown", f'{state["state"]!r} maps to undeclared tone {state["tone"]!r}')
        if not state["label"].strip():
            fail("state-label-empty", f'{state["state"]!r} maps to no words')

    # 6. Evidence.
    for basis in sorted(set(re.findall(r'data-basis="([^"]+)"', html))):
        if basis not in BASES:
            fail("basis-undeclared", f'{basis!r} is used but is not one of {", ".join(BASES)}')
    for basis in sorted(set(BASES) - set(basis_rules)):
        fail("basis-unstyled", f'{basis!r} has no rule, so an unverified claim looks measured')
    for basis, body in sorted(basis_rules.items()):
        value = _declared_property(body)
        if value and not value.startswith("var(--"):
            fail("basis-literal", f'{basis!r} uses {value!r}; a basis must name a token')
    if not writers["evidence_blocks"]:
        fail("evidence-absent", "the component exists but no claim stands on it")

    # 7. Contrast, measured from the tokens the rules name.
    contrast: list[dict[str, object]] = []
    for tone, rules in sorted(tone_rules.items()):
        token_name = _declared_property(rules.get("dot", "x: var(--nope)")).removeprefix("var(--").rstrip(")")
        for theme, values in tokens.items():
            if token_name not in values or "bg-card" not in values:
                fail("contrast-unmeasured", f"{tone}/{theme}: {token_name!r} is not a colour token")
                continue
            ratio = contrasts.contrast_ratio(values[token_name], values["bg-card"])
            contrast.append(
                {"theme": theme, "tone": tone, "token": f"--{token_name}", "ratio": round(ratio, 2)}
            )
            if ratio < GRAPHIC_FLOOR:
                fail(
                    "contrast-graphic",
                    f"{theme}/{tone}: --{token_name} is {ratio:.2f}:1 on --bg-card, below {GRAPHIC_FLOOR}:1",
                )
    labels: list[dict[str, object]] = []
    for theme, values in tokens.items():
        ratio = contrasts.contrast_ratio(values["text"], values["bg-card"])
        labels.append({"theme": theme, "pair": "--text on --bg-card", "ratio": round(ratio, 2)})
        if ratio < TEXT_FLOOR:
            fail("contrast-label", f"{theme}: the label is {ratio:.2f}:1 on --bg-card, below {TEXT_FLOOR}:1")

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P1-03",
        "statement": (
            "One status component (tone, dot, hairline, label) written by one function, and one "
            "evidence component that makes the basis of a claim part of the claim. Checks prove the "
            "vocabulary is closed and every state has words; they do not prove a screen looks right."
        ),
        "source": {"frontend": "static/index.html", "tokens": "product/M5_2_0_UIUX_Tokens.json"},
        "vocabulary": {
            "tones": list(TONES),
            "bases": list(BASES),
            "tone_meaning": {
                "ok": "finished and verified",
                "warn": "needs a decision, or waiting on one",
                "danger": "failed, blocked or refused",
                "info": "in progress or ready",
                "busy": "running now",
                "neutral": "idle, cancelled, or not yet known",
            },
        },
        "surfaces": markup,
        "state_matrix": states,
        "writers": writers,
        "contrast": {"labels": labels, "tone_graphics": contrast, "text_floor": TEXT_FLOOR, "graphic_floor": GRAPHIC_FLOOR},
        "totals": {
            "tones": len(TONES),
            "bases": len(BASES),
            "status_surfaces": len(markup),
            "live_regions": sum(1 for item in markup if item["live"]),
            "states": len(states),
            "set_status_calls": writers["set_status_calls"],
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "declarations agree and every state has a tone and a word; a rendering that looks wrong is the browser walk's job",
            "contrast is computed from the token values the rules name, not sampled from a screen",
            "the tone is redundant with the label, so the floors differ: text 4.5:1, the dot 3:1",
        ],
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    vocabulary = payload["vocabulary"]
    lines = [
        "# Status and evidence components",
        "",
        "Generated by `python3 scripts/uiux_status_components.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## Vocabulary",
        "",
        "A status is a tone, a dot, a hairline and a label. The label carries the state; the tone",
        "repeats it. That order matters: the label is `--text`, which measures "
        f"{payload['contrast']['labels'][0]['ratio']}:1 on `--bg-card` in the dark theme and "
        f"{payload['contrast']['labels'][1]['ratio']}:1 in the light theme, while the tone paints the dot and the",
        "hairline where a colour can be decorative.",
        "",
        "| Tone | Means |",
        "|---|---|",
    ]
    for tone in vocabulary["tones"]:
        lines.append(f"| `{tone}` | {vocabulary['tone_meaning'][tone]} |")
    lines += [
        "",
        "An evidence block adds one required attribute, the basis, drawn from four words: "
        + ", ".join(f"`{basis}`" for basis in vocabulary["bases"])
        + ".",
        "",
        "## The state table",
        "",
        "`harnessStatusBadge()` is the one place a run's state becomes words. Every row below renders",
        "through `setStatus()`, so the state, the tone and the label cannot disagree.",
        "",
        "| State | Tone | Label |",
        "|---|---|---|",
    ]
    for row in payload["state_matrix"]:
        lines.append(f"| `{row['state']}` | `{row['tone']}` | {row['label']} |")
    lines += [
        "",
        f"{totals['states']} states, {totals['tones']} tones, {totals['bases']} bases, "
        f"{totals['status_surfaces']} surfaces using the component, {totals['live_regions']} of them live regions.",
        "",
        "## Surfaces",
        "",
        "| Element | Tone | Announced | Line |",
        "|---|---|---|---|",
    ]
    for item in payload["surfaces"]:
        ident = f"`#{item['id']}`" if item["id"] else "(unnamed)"
        lines.append(f"| {ident} | `{item['tone']}` | {'yes' if item['live'] else 'no'} | {item['line']} |")
    lines += [
        "",
        "## Writers",
        "",
        "| Measure | Count |",
        "|---|---|",
        f"| `setStatus()` call sites | {payload['writers']['set_status_calls']} |",
        f"| `evidenceNote()` call sites | {payload['writers']['evidence_calls']} |",
        f"| Evidence blocks in markup | {payload['writers']['evidence_blocks']} |",
        f"| Legacy `className = \"badge …\"` writers | {payload['writers']['legacy_badge_writers']} |",
        "",
        "The last row is the point of the story. A writer that sets the class and the text on separate",
        "lines can leave a chip saying green `FAILED` after a one-line edit; a writer that passes the",
        "tone and the label to one function cannot.",
        "",
        "## Measured contrast",
        "",
        f"Text floor {payload['contrast']['text_floor']}:1, redundant-graphic floor "
        f"{payload['contrast']['graphic_floor']}:1. Ratios are computed from the token values each rule names.",
        "",
        "| Theme | Pair | Ratio |",
        "|---|---|---|",
    ]
    for row in payload["contrast"]["labels"]:
        lines.append(f"| {row['theme']} | `{row['pair']}` | {row['ratio']}:1 |")
    for row in payload["contrast"]["tone_graphics"]:
        lines.append(f"| {row['theme']} | `{row['tone']}` dot — `{row['token']}` on `--bg-card` | {row['ratio']}:1 |")
    lines += [
        "",
        "The dark `--accent` measures "
        f"{next(row['ratio'] for row in payload['contrast']['tone_graphics'] if row['tone'] == 'busy' and row['theme'] == 'dark')}:1"
        " on `--bg-card`. It is used for the `busy` dot and hairline and nowhere as text, which is why the",
        "label is not painted with the tone.",
        "",
        "## Checks and limits",
        "",
        "`--check` fails on: a tone or basis rendered but undeclared, declared but unused, declared with",
        "no rule; a tone rule that names a raw colour instead of a token; a tone drawn for the hairline",
        "but not the dot; a converted surface that lost the component or its tone; no live region; a",
        "legacy `className = \"badge …\"` writer; a state mapped to an unknown tone or to no words; an",
        "evidence basis outside the four; a claim with no basis; a contrast regression.",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "The rendering proof is `node scripts/status_walk.mjs`, which drives a real browser: it draws",
        "every tone and every state, checks the dot is visible and the label is the words, and confirms a",
        "live region hears nothing when the same state is written twice.",
        "",
        "## Regenerating",
        "",
        "1. Edit the component in `static/index.html`.",
        "2. `python3 scripts/uiux_status_components.py`.",
        "3. `node scripts/status_walk.mjs` against a running demo, then regenerate the sign-off.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the component set")
    args = parser.parse_args(argv)

    payload = build_components()
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
            print("error: the status and evidence artifacts have drifted from the stylesheet:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_status_components.py")
            return 1
        if payload["findings"]:
            print("error: the components do not agree with their rules:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"status and evidence components OK: {payload['totals']['states']} states, "
            f"{payload['totals']['tones']} tones, {payload['totals']['status_surfaces']} surfaces, "
            f"{payload['totals']['set_status_calls']} setStatus call sites, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['states']} states, {payload['totals']['tones']} tones, "
        f"{payload['totals']['status_surfaces']} surfaces, {len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
