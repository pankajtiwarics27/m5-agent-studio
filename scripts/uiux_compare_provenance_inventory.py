#!/usr/bin/env python3
"""Inventory the Compare pane's measured figures and where they come from (UIUX-P4-05).

    python3 scripts/uiux_compare_provenance_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_compare_provenance_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_compare_provenance_inventory.py --json     # print the inventory

The pane printed exactly one number: the arbiter's throughput, in a green badge, with nothing
saying where it came from - and a demo verdict carried 29.5 or 42.1 tok/s that no model ever ran
for. Latency, tokens and cost per model were absent, so a side-by-side comparison compared prose.

What is measured here:

* **the fields**: every value the pane prints is one of `elapsed_s`, `ttft`, `completion_tokens`,
  `reasoning_tokens`, `decode_tps`, and each of those is checked against the payload `app.py`
  actually yields (a field the server does not send cannot be printed by a pane that reads it);
* **the source**: no figure comes from the browser's clock - the pane may not call
  `performance.now`, `Date.now`, `new Date` or `Date.parse`, and the run's total comes from the
  `debate_done` event's `total_elapsed_s`, not from a timer started at the click;
* **the refusals**: a demo reply (whose numbers the demo path invented) is given none, a stage that
  never reported shows `—` and says why instead of a zero, and cost is named as not known with the
  reason, because no price is configured for any model;
* **the arbiter**: its card and its row state the same figures, both read from `synthesis_done`,
  and neither falls back to an unqualified tok/s badge;
* **the evidence**: the fixture can produce two answering panels, a demo reply and a stopped run,
  and the walk compares the page cell by cell with the frames the server sent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_provenance_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-provenance"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Provenance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-provenance.md"

STORY = "UIUX-P4-05"

# The fields the pane may print, and the only ones the response carries.
FIELDS = ("elapsed_s", "ttft", "completion_tokens", "reasoning_tokens", "decode_tps")

# The columns the table states, in order.
COLUMNS = ("Model", "Elapsed", "First token", "Reply tokens", "Decode", "Cost")

# Every unit the pane prints, and the field it belongs to.
UNITS = {"elapsed_s": "s", "ttft": "s", "completion_tokens": "tokens", "decode_tps": "tok/s"}

# What the browser's clock would look like if a figure came from it.
CLOCKS = ("performance.now(", "Date.now(", "new Date(", "Date.parse(", "performance.timeOrigin")

# The demo path's invented figures. A demo reply may not print them, anywhere.
DEMO_FIGURES = ("29.5", "42.1", "155.0")

# The captures the doc lists and the walk writes.
CAPTURES = (
    "compare-provenance.png",
    "compare-provenance-arbiter.png",
    "compare-provenance-demo.png",
    "compare-provenance-light.png",
    "compare-provenance-narrow.png",
)

SAFE_EXPRESSIONS = {"", "null", "undefined", "true", "false"}


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``."""
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


def usage_fields(app_source: str) -> set[str]:
    """Every key `app.py` puts in a usage event: the payload the pane is allowed to read."""
    found: set[str] = set()
    for match in re.finditer(r'"type":\s*"usage",(.*?)\n\s*\}', app_source, re.S):
        found.update(re.findall(r'"([a-z_]+)":', match.group(1)))
    return found


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal in ``source``, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer("""["'`](/api/[^"'`]*)["'`]""", source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1)).split("?")[0]
        if path not in found:
            found.append(path)
    return found


def unescaped_interpolations(markup: str) -> list[str]:
    """``${…}`` expressions in markup that do not pass through an escaping helper."""
    offenders: list[str] = []
    for expression in _expressions(markup):
        if "${" in expression:
            offenders.extend(unescaped_interpolations(expression))
            continue
        stripped = expression.strip()
        if stripped in SAFE_EXPRESSIONS:
            continue
        if stripped.startswith(("escapeHtml(", "Number(", "String(", "Boolean(", "renderMarkdown(")):
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*Html", stripped):
            continue
        offenders.append(stripped)
    return offenders


def _expressions(markup: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"\$\{", markup):
        start = match.end()
        depth = 1
        index = start
        while index < len(markup) and depth:
            char = markup[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        found.append(markup[start : index - 1])
    return found


def element(source: str, element_id: str) -> str:
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def ledger_story() -> dict:
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    app_source = APP.read_text(encoding="utf-8")
    seed = SEED.read_text(encoding="utf-8") if SEED.exists() else ""
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    figure = function_body(html, "debateFigure")
    is_demo = function_body(html, "debateIsDemo")
    no_figures = function_body(html, "debateNoFigures")
    reading = function_body(html, "debateProvenanceOf")
    render = function_body(html, "renderDebateProvenance")
    badge = function_body(html, "debateArbiterBadge")
    run = function_body(html, "runDebate")
    debate_functions = "".join(
        [figure, is_demo, no_figures, reading, render, badge, run, function_body(html, "renderDebate")]
    )
    payload = usage_fields(app_source)

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the block ───────────────────────────────────────────────────────────
    check(
        "the pane states a performance block of its own, drawn under the results",
        'id="debateProvenance"' in html
        and 'role="region"' in element(html, "debateProvenance")
        and 'aria-label="Measured performance per reply"' in element(html, "debateProvenance")
        and html.index('id="debateResults"') < html.index('id="debateProvenance"')
        and "renderDebateProvenance();" in function_body(html, "renderDebate"),
        "a figure with no block of its own is a badge with no basis",
    )
    check(
        "a figure is a figure the response carried: every field printed is one the app yields",
        all(field in reading for field in FIELDS)
        and all(field in payload for field in FIELDS),
        ", ".join(
            [f"not read by the pane: {field}" for field in FIELDS if field not in reading]
            + [f"not in payload: {field}" for field in FIELDS if field not in payload]
        ),
    )
    check(
        "the pane prints no figure the browser timed",
        not any(clock in debate_functions for clock in CLOCKS),
        ", ".join(clock for clock in CLOCKS if clock in debate_functions),
    )
    check(
        "the run's total elapsed time comes from the debate_done event, not from a timer",
        "debate_done(d) { debateRunElapsed = d.total_elapsed_s;" in run
        and "debateRunElapsed = null" in run
        and "let debateRunElapsed = null;" in html,
        "a total this page measured would be the network and the renderer, named as the run",
    )
    check(
        "a figure the response did not carry is a dash, never a zero",
        "number <= 0) return null" in figure
        and re.search(r'\(value \? escapeHtml\(value\) : "—"\)', render) is not None
        and "—" not in figure,
        "a zero reads as a measurement, and there was none",
    )
    check(
        "a stage that never reported says why, in its own terms",
        all(
            reason in no_figures
            for reason in (
                "the provider failed before it reported any figure",
                "you stopped the run before the runtime reported",
                "the stream ended before the runtime reported",
                "this panel never started, so nothing was measured",
                "the response carried no usage for this panel",
            )
        ),
        "a blank cell is not an explanation",
    )
    check(
        "a demo reply is given no figure, and the demo path's numbers appear nowhere",
        "debateIsDemo" in reading
        and 'if (debateIsDemo(usage) || state === "demo") return { note: debateNoFigures("demo") };' in reading
        and not any(number in debate_functions for number in DEMO_FIGURES),
        ", ".join(number for number in DEMO_FIGURES if number in debate_functions),
    )
    check(
        "the table states one row per reply, including the arbiter's, and the six columns",
        all(f'<th scope="col">{column}</th>' in render for column in COLUMNS)
        and "panels.concat(arbiter).map(panel =>" in render
        and "arbiter: true" in render,
        ", ".join(column for column in COLUMNS if f'<th scope="col">{column}</th>' not in render),
    )
    check(
        "the block names the fields it reads, and says this page times nothing",
        "elapsed_s" in render and "ttft" in render and "completion_tokens" in render and "decode_tps" in render
        and "this page times nothing" in render
        and "not a rate measured by the browser" in render,
        "a table of figures with no stated source is the badge again, in more columns",
    )
    check(
        "cost is stated as not known, with the reason, and per row as a dash",
        "Cost: not known." in render
        and "No price is configured for any model" in render
        and 'title="No price is configured for any model"' in render,
        "an absent price is a refusal, not a blank",
    )
    check(
        "the arbiter's card states its figures from its own usage, and never an unqualified tok/s",
        "debateArbiterBadge(debateSynth.u)" in html
        and "debateFigure(usage.decode_tps" in badge
        and "demo text · no figures" in badge
        and "figures not reported" in badge
        and "runtime reported it" in badge
        and "decode_tps+' tok/s'" not in html
        and "Synthesizing..." not in html,
        "the badge this story replaced, back in place",
    )
    check(
        "the run's total is stated on the block, and a run that failed claims none",
        "The run took " in render
        and "is not reported until the run ends" in render,
        "a failed run has no total, and the block says so rather than timing itself",
    )
    check(
        "every value the block draws is escaped, and it posts no route of its own",
        not unescaped_interpolations(render + badge + reading)
        and called_routes(render + badge + reading) == [],
        ", ".join(unescaped_interpolations(render + badge + reading)) or ", ".join(called_routes(render + badge + reading)),
    )

    # ── the fixture and the walk ────────────────────────────────────────────
    check(
        "the fixture can produce two answering panels, a stopped run and a demo reply",
        "--resident-model" in seed
        and "state.load(resident_model)" in seed
        and "slow_word_seconds" in seed
        and "--offline" in seed
        and '"resident_model": args.resident_model' in seed,
        "a comparison with one panel has no arbiter to measure, and no side to compare",
    )
    check(
        "the fixture reports the same fields the app forwards, so the walk can compare them",
        "time_to_first_token" in seed and "generation_tokens_per_second" in seed,
        "the fixture has to speak the runtime's shape or the app computes nothing",
    )
    check(
        "the walk compares the page with the frames the server sent, cell by cell",
        "__debateFrames" in walk_body
        and "response.clone()" in walk_body
        and "panel_done" in walk_body
        and "synthesis_done" in walk_body
        and "debate_done" in walk_body
        and "every figure on screen is the figure the response carried for that reply" in walk_body
        and "29\\.5|42\\.1" in walk_body,
        "a walk that reads only the page cannot tell a payload figure from a computed one",
    )
    check(
        "the captures the doc lists exist and were written by the walk",
        all((MEDIA / name).exists() for name in CAPTURES)
        and all(name[:-4] in walk_body for name in CAPTURES),
        ", ".join(name for name in CAPTURES if not (MEDIA / name).exists()),
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
        "title": "Compare measured performance and provenance",
        "generated_by": "scripts/uiux_compare_provenance_inventory.py",
        "surface": "#debateProvenance",
        "fields": [
            {
                "field": field,
                "column": column,
                "unit": UNITS.get(field, ""),
                "source": "the usage the server sent with panel_done / synthesis_done",
            }
            for field, column in zip(FIELDS, ("Elapsed", "First token", "Reply tokens", "", "Decode"))
            if field != "reasoning_tokens"
        ],
        "columns": list(COLUMNS),
        "refusals": [
            "cost - no price is configured for any model, so no reply is given one",
            "demo text - the reply is the server's, so it carries no measured figure",
            "failed, stopped, lost and never-started panels - no usage arrived, so the cell is a dash",
            "the client clock - this page times nothing",
        ],
        "stream_events": ["panel_done", "synthesis_done", "debate_done"],
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-05 — Measured performance and provenance (Compare)",
        "",
        "Generated by `scripts/uiux_compare_provenance_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the pane prints now, and where each figure comes from",
        "",
        "`#debateProvenance` sits under the results table and states, per reply:",
        "",
        "| Column | Field the response carried | Unit |",
        "| :--- | :--- | :--- |",
        "| Elapsed | `elapsed_s` | s |",
        "| First token | `ttft` | s |",
        "| Reply tokens | `completion_tokens` (and `reasoning_tokens`) | tokens |",
        "| Decode | `decode_tps` | tok/s |",
        "| Cost | — | refused: no price is configured for any model |",
        "",
        "Every one of those fields is in the `usage` object `app.py` forwards on `panel_done` and",
        "`synthesis_done`; the run's own total is `total_elapsed_s` from `debate_done`. The inventory",
        "reads those keys out of `app.py` and fails when the pane prints a field the server does not",
        "send, so the table cannot drift into printing something this page worked out.",
        "",
        "## What it refuses to print",
        "",
        *[f"* {refusal}." for refusal in payload["refusals"]],
        "",
        "A demo reply is the important case: the demo path emits `demo: true` and the invented",
        "figures 29.5 / 42.1 tok/s and 155 tok/s prefill, and `record_perf` still stores them (that",
        "write is the runtime program's to remove). This pane reads the flag and shows **no figure",
        "at all** for such a reply, saying why in the row underneath.",
        "",
        "## The arbiter",
        "",
        "The arbiter's card used to carry one number, `decode_tps`, in a green badge with no basis.",
        "It now carries the same figures as the arbiter's row in the table — decode, elapsed and",
        "tokens, from `synthesis_done` — under the title *\"As the runtime reported it in this reply's",
        "usage; this page times nothing.\"* A demo verdict says `demo text · no figures`, and a reply",
        "that reported no figures says `figures not reported` rather than a zero.",
        "",
        "## The captures",
        "",
        "All five come from `node scripts/compare_provenance_walk.mjs --screenshots docs/media/compare-provenance`,",
        "against three `scripts/council_readiness_seed.py` processes: plain, `--slow-model`, `--offline`,",
        "plus a fourth started with `--resident-model <the fits model>` so two panels answer and an",
        "arbiter verdict exists to measure.",
        "",
        "| File | Size | What it shows |",
        "| :--- | :--- | :--- |",
        "| `compare-provenance.png` | 868 × 461 | one panel answered and one failed: the answering row"
        " carries 0.25 s / 0.04 s / 58 tokens / 41.2 tok/s, the failed row carries five dashes and the"
        " sentence `the provider failed before it reported any figure`, and the footer says the run's"
        " total is not reported because the run did not finish |",
        "| `compare-provenance-arbiter.png` | 908 × 867 | two answering panels and an arbiter verdict:"
        " the card's badge and the arbiter's row state the same figures, and the footer gives the run's"
        " 3.83 s as the server measured it |",
        "| `compare-provenance-demo.png` | 908 × 752 | no runtime: the demo row is dashes, with the"
        " reason, and 29.5 / 42.1 appear nowhere |",
        "| `compare-provenance-light.png` | 868 × 272 | the same block and figures in the light theme |",
        "| `compare-provenance-narrow.png` | 480 × 900 | the six columns at a 480 px viewport, from the"
        " viewport rather than cropped |",
        "",
        "## What the walk proves that a screenshot cannot",
        "",
        "The walk installs a `fetch` tee before the page runs and parses the same SSE frames the app",
        "parses. It then compares each cell of the table with the `usage` the server sent for that",
        "reply — `elapsed_s`, `ttft`, `completion_tokens`, `reasoning_tokens`, `decode_tps` — and with",
        "`total_elapsed_s` for the run, formatting them with the same arithmetic the page uses. A",
        "figure this page computed from its own clock could not match a server figure to two decimals,",
        "which is why the check is an equality and not a range.",
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
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


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
                    "the compare-provenance inventory has drifted from the source: "
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
