#!/usr/bin/env python3
"""Inventory the Compare pane's question-first composition (UIV2-16).

    python3 scripts/uiux_compare_balanced_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_compare_balanced_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_compare_balanced_inventory.py --json     # print the inventory

Four things were wrong with the pane a reader met, and none of them was a missing widget:

* **the criteria were above the question.** A four-row table, its basis paragraph and an
  add-a-criterion field sat between the pane title and the prompt. At 1440x900 the fold held the
  table and the thing being compared was under it - the pane asked the reader to read the
  rubric before it asked them for the question.
* **the columns were the window's, not the run's.** `repeat(auto-fit, minmax(290px, 1fr))` made
  two columns only when the window happened to be wide enough, so two replies could be different
  widths, and the row was as tall as its tallest card with no cap on any of them.
* **one model looked like a failed comparison.** A single ticked model drew a single card, full
  width, saying nothing: a reader could not tell a lone answer from a second column that never
  filled.
* **the criteria mixed three different claims in one table.** "Answered at all" is settled by the
  reply; cost is settled by nobody; "fits my constraints" is the reader's. All three were rows in
  one table with the same weight.

What this inventory checks: the composition (question and picks first, criteria folded under
them), the fold's own count so folding hides nothing, the three groups and their own rails, the
column count taken from the run, the matching card heights and the scrolling body inside them,
the one-model state, and the preservation of what the pane already did (the P4-04 criteria list
with its add/remove and focus, the run control and the reasons it disables Start, the single
debate-stream call site).

It fails when the question can be pushed below the criteria again, when the fold stops saying
what it holds, when a measured criterion is drawn like a judged one, when the grid goes back to
`auto-fit`, when one model is left looking like two, or when a rule this story preserved has
gone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Balanced.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-balanced.md"
WALK_PATH = PROJECT_ROOT / "scripts" / "compare_balanced_walk.mjs"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "compare-balanced"

STORY = "UIV2-16"
TITLE = "Compare balanced question-first layout"

# The order the pane has to read in, top to bottom. Each entry is the id a reader meets.
ORDER = (
    ("debatePrompt", "the question"),
    ("debateModelChecks", "the models to compare"),
    ("debateCriteriaDetails", "the criteria, folded"),
    ("debateSummary", "what the run left"),
    ("debateGrid", "the answers"),
)

# The three claims the criteria make, and the rail each one draws.
GROUPS = (
    ("measured", "Settled by the run", "read from the replies and the runtime's own usage"),
    ("not-stated", "Not settled here", "named, and refused rather than guessed"),
    ("judged", "Yours to judge", "nothing in a reply decides these"),
)

# What the walk asserts, in its own words; a check here is a claim about a browser.
WALK_STATES = (
    ("order", "the question and the picks are the composer, and the composer comes first"),
    ("fold", "the question is fully above the fold"),
    ("folded", "the criteria panel is folded, so it cannot push the question down"),
    ("counts", "the folded summary counts what each group holds"),
    ("groups", "the criteria open into three named groups, in the order the run reads them"),
    ("rails", "what the run settles and what the reader judges are drawn apart"),
    ("add", "a criterion the reader adds opens the panel, stays focused, and is counted"),
    ("inspector", "the inspector's criteria counts follow the panel that changed"),
    ("inspector-back", "and the inspector is back to six criteria, two of them the reader's"),
    ("single", "one model is stated as a single answer, not as a comparison"),
    ("fixture", "the fixture serves two local models, so a comparison is two real answers"),
    ("pair", "two answers land in two columns of the same width, top and height"),
    ("answered", "both panels answered, so the capture is a comparison and not a failure"),
    ("scroll", "a long reply scrolls inside its own card instead of stretching the row"),
    ("narrow", "at 390x844 the answers stack in one column and nothing overflows sideways"),
)


def code_only(source: str) -> str:
    """``source`` without its comment lines: prose about a rule is not the rule."""
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    code = code_only(html)
    findings: list[dict[str, str]] = []
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})
        if not ok:
            findings.append({"check": name, "detail": detail})

    # 1. The composition: the question and the picks come first, the criteria under them.
    positions = [(html.find(f'id="{element_id}"'), element_id) for element_id, _ in ORDER]
    order_ok = all(position >= 0 for position, _ in positions) and all(
        positions[index][0] < positions[index + 1][0] for index in range(len(positions) - 1)
    )
    check(
        "the pane reads question, models, criteria, results, answers - in that order",
        order_ok,
        "the order is what the fold holds: " + " < ".join(element_id for _, element_id in positions),
    )
    check(
        "the question and the picks are one composer, and it is the pane's first region",
        '<div class="compare-composer" id="compareComposer"' in html
        and html.find('id="compareComposer"') < html.find('id="debateModelChecks"')
        and 'role="group" aria-label="The comparison to run"' in html,
        "a composer that does not hold the picks leaves the question looking optional",
    )
    check(
        "the model picks are labelled as a group, not as loose checkboxes",
        'id="debateModelChecks" class="compare-model-checks" role="group" aria-labelledby="debateModelPicksLabel"' in html
        and 'id="debateModelPicksLabel"' in html,
        "a wall of checkboxes with no name is what the pane had before P4-04",
    )

    # 2. The fold: a details element, closed, whose summary counts its groups.
    check(
        "the criteria are folded in a details element that starts closed",
        '<details id="debateCriteriaDetails" class="debate-criteria-details">' in html
        and 'id="debateCriteriaCounts"' in html
        and " document.getElementById(\"debateCriteriaDetails\").open = true" not in html
        and '.debate-criteria-details[open]' in html,
        "open by default is the oversized panel the story is about",
    )
    check(
        "the folded summary states how many criteria each group holds",
        "counts.textContent = counted.join" in code
        and "const counted = groups.map(group => String(group.rows.length)" in code,
        "folding is only honest when the fold says what it hides",
    )

    # 3. The three groups, drawn apart.
    check(
        "the criteria are drawn as three groups, in the order they are read",
        all(f'kind: "{kind}"' in code for kind, _, _ in GROUPS)
        and all(f'label: "{label}"' in code for _, label, _ in GROUPS)
        and code.find('kind: "measured"') < code.find('kind: "not-stated"') < code.find('kind: "judged"'),
        "a measured criterion and a judged one are different claims",
    )
    check(
        "each group carries its own rail, so the three are told apart before they are read",
        all(
            f'.debate-criteria-group[data-kind="{kind}"] {{ border-left-color: var(--{tone}); }}' in html
            for (kind, _, _), tone in zip(GROUPS, ("emerald", "amber", "cyan"))
        )
        and '<div class="debate-criteria-group" data-kind="\' + group.kind + \'">' in code,
        "three tables with one rail are one table with three headings",
    )
    check(
        "a measured criterion says what settles it, and a judged one says it is the reader's",
        'yes — after each panel reports' in code and "no — yours to judge" in code,
        "the two claims have to be readable as themselves, not as yes-and-no",
    )

    # 4. The P4-04 behaviour survives the fold.
    check(
        "adding a criterion opens the panel and keeps the input focused",
        "if (details) details.open = true;" in code
        and "const input = document.getElementById(\"debateCriterionInput\");" in code
        and 'id="debateCriterionInput"' in code
        and 'aria-label="A criterion you will judge yourself"' in html,
        "a focused input behind a closed details takes keystrokes nobody can see",
    )
    check(
        "the criteria region is labelled, drawn at load, and drawn by its own renderer",
        'id="debateCriteria" class="debate-criteria" role="region" aria-label="Comparison criteria"' in html
        and "renderDebateCriteria();" in html
        and "function renderDebateCriteria(" in html,
        "a criterion named after the results exist is a rationalization, not a criterion",
    )

    # 5. The columns come from the run, not from the window.
    check(
        "the grid takes its column count from the run",
        'grid.style.setProperty("--debate-cols", String(columns));' in code
        and 'grid.dataset.count = String(count);' in code
        and "grid-template-columns: repeat(var(--debate-cols, 1), minmax(0, 1fr));" in html
        and "auto-fit, minmax(290px, 1fr)" not in html,
        "auto-fit gives the answers the window's columns, which is what a comparison is not",
    )
    check(
        "the columns are capped at four, and a single answer takes the row",
        'const columns = Math.max(1, Math.min(count, 4));' in code
        and '.debate-grid[data-count="1"] { grid-template-columns: minmax(0, 1fr); }' in html,
        "five panels in five columns is a wall, and one panel in a third of the row is a mistake",
    )

    # 6. One model is stated as a single answer.
    check(
        "one model is named as a single answer rather than a comparison",
        'id="debateColumnsNote" class="debate-columns-note"' in html
        and "is a single answer, not a comparison" in code
        and "if (count === 1) {" in code,
        "a lone card and a comparison missing a column look identical without a word",
    )
    check(
        "the run control states the picks it will run",
        'id="debateModelsChosen"' in html
        and 'chosenLabel.textContent = models.length' in code
        and '"No model selected"' in code,
        "Start has to be answerable from the composer, not from the checkbox row",
    )

    # 7. Matching widths and heights, with the body scrolling inside the card.
    check(
        "cards in a row are the same height, and their bodies scroll rather than stretch the row",
        ".debate-grid { display: grid;" in html
        and "align-items: stretch;" in html
        and ".debate-grid > .stage-output-card { display: flex; flex-direction: column;" in html
        and "max-height: 46vh; overflow-y: auto;" in html,
        "a long reply must not decide how tall its neighbours are",
    )
    check(
        "the narrow layout stacks the answers instead of squeezing them",
        ".debate-grid { grid-template-columns: minmax(0, 1fr); }" in html,
        "two 160px columns are not a comparison either",
    )

    # 8. The inspector says who settles what.
    check(
        "the Evaluation inspector counts the criteria by group, not the panel's children",
        'groupCount("measured")' in code
        and 'groupCount("not-stated")' in code
        and 'groupCount("judged")' in code
        and 'crit.querySelectorAll(":scope > *")' not in code,
        "counting a table as one criterion told a reader nothing",
    )

    # 8b. The inspector is refreshed by the change, not by the tab switch. A reader who adds a
    # criterion while the Evaluation view is open used to keep reading the old counts.
    criteria_fn = re.search(r"function renderDebateCriteria\(focusAdd = false\)[\s\S]*?\n  \}", code)
    check(
        "a criterion added or removed re-reads the inspector that counts it",
        bool(criteria_fn)
        and "renderInspectorFor(" in criteria_fn.group(0)
        and "mode-tab.active" in criteria_fn.group(0),
        "two places that disagree about what the run is judged on are worse than one",
    )

    # 9. What must not have moved.
    check(
        "one summon call site: the pane still posts only the debate stream",
        code.count('consumeSSE("/api/debate/stream"') == 1,
        "a second call site would be a second place to keep in step",
    )
    check(
        "Start is still disabled with the reason it is disabled",
        'hint.textContent = "Select at least one model to compare.";' in code
        and 'hint.textContent = "Write the question to compare on.";' in code,
        "a disabled button with no reason is a dead end",
    )
    check(
        "a hidden label stays hidden",
        re.search(r"^\s*\.field-lbl\[hidden\] \{ display: none; \}$", html, re.M) is not None,
        "the arbiter hint said 'enable Arbiter Verdict' under a ticked box, because a label "
        "carries its own display",
    )

    # 10. The browser walk asserts what this inventory describes.
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    check(
        "a browser walk asserts what this inventory describes",
        bool(walk) and all(needle in walk for _, needle in WALK_STATES),
        "the captures below are only evidence if a walk asserted the states they show",
    )
    check(
        "the walk needs no environment of its own",
        all(
            token in walk
            for token in (
                "STUDIO_SCREENSHOT_CHROME",
                "STUDIO_PUPPETEER_CORE",
                "--base-url",
                "--screenshots",
                "council_readiness_seed.py",
            )
        ),
        "a walk that only runs on its author's machine is not evidence anyone else can re-make",
    )

    return {
        "story": STORY,
        "title": TITLE,
        "generated_by": "scripts/uiux_compare_balanced_inventory.py",
        "order": [{"id": element_id, "what": what} for element_id, what in ORDER],
        "groups": [{"kind": kind, "label": label, "note": note} for kind, label, note in GROUPS],
        "browser_walk": {
            "script": "scripts/compare_balanced_walk.mjs",
            "media": "docs/media/compare-balanced",
            "states": [{"state": state, "asserted": needle} for state, needle in WALK_STATES],
        },
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "blocked",
    }


# ── the doc ────────────────────────────────────────────────────────────────


def render_doc(payload: dict) -> str:
    lines = [
        f"# Compare's question-first composition ({STORY})",
        "",
        "Generated by `scripts/uiux_compare_balanced_inventory.py` — **do not hand-edit**.",
        "Run `python3 scripts/uiux_compare_balanced_inventory.py --check` to see whether this doc,",
        "the JSON beside it and the source still agree.",
        "",
        "## What was wrong",
        "",
        "1. **The rubric came before the question.** A four-row criteria table, its basis paragraph",
        "   and an add-a-criterion field sat between the pane title and the prompt. At 1440x900 the",
        "   fold held the table; the comparison itself was under it.",
        "2. **The columns belonged to the window.** `repeat(auto-fit, minmax(290px, 1fr))` made two",
        "   columns when the window was wide enough and one when it was not, so two replies could be",
        "   different widths, and nothing capped a card's height.",
        "3. **One model looked like a failed comparison.** A single ticked model drew one card, full",
        "   width, with no word about what it was: a lone answer and a comparison whose second column",
        "   never filled looked the same.",
        "4. **Three different claims shared one table.** \"Answered at all\" is settled by the reply,",
        "   cost by nobody, \"fits my constraints\" by the reader — all rows of equal weight.",
        "",
        "## The pane now",
        "",
        "| # | What the reader meets |",
        "| --- | --- |",
    ]
    for index, row in enumerate(payload["order"]):
        lines.append(f"| {index + 1} | `{row['id']}` — {row['what']} |")
    lines += [
        "",
        "The question and the model picks are one composer, and the composer is the pane's first",
        "region. The criteria are still named before the run, and are folded into a summary that",
        "counts what it holds, so a reader can open them without losing the question to them.",
        "",
        "## The three groups",
        "",
        "| Group | Its rail | What it says |",
        "| --- | --- | --- |",
    ]
    for group, tone in zip(payload["groups"], ("emerald", "amber", "cyan")):
        lines.append(f"| {group['label']} | `--{tone}` | {group['note']} |")
    lines += [
        "",
        "A criterion the reader adds still appears as *yours to judge*, still survives a redraw,",
        "and still can be removed — and now opens the panel it lives in, because a focused input",
        "behind a closed `<details>` takes keystrokes nobody can see.",
        "",
        "## The columns",
        "",
        "`--debate-cols` is set from the run — the panels, or the picks before there are panels —",
        "capped at four, and `data-count=\"1\"` collapses the row to one column. Cards in a row share",
        "their height (`align-items: stretch`), and each body caps at `46vh` and scrolls inside its",
        "own card, so a long reply cannot decide how tall its neighbour is. At 390x844 the row",
        "stacks, and nothing overflows sideways.",
        "",
        "## The one-model state",
        "",
        "One ticked model is stated in the composer (`1 model selected`) and under it, in the note",
        "above the answers: *this is a single answer, not a comparison*. The note goes when a second",
        "model is picked. A run of one model draws one full-width card and says the same thing.",
        "",
        "## What this story does not do",
        "",
        "* it does not change what a comparison is judged on, who sits in it, or how a reply is",
        "  scored — the criteria, the results table and the provenance table are the ones P4-04/05",
        "  and P4-03 built;",
        "* it does not add a route, a migration or a column;",
        "* it does not start a comparison the reader did not ask for, and Start is still disabled",
        "  with the reason it is disabled.",
        "",
        "## The browser walk",
        "",
        "```",
        "node scripts/compare_balanced_walk.mjs --base-url http://127.0.0.1:8000 --token <token> \\",
        "     --viewport 1440x900 --theme dark --screenshots docs/media/compare-balanced",
        "```",
        "",
        "Without `--base-url` it starts `scripts/council_readiness_seed.py --resident-model …`,",
        "so two local models can answer the same question. It asserts:",
        "",
    ]
    walk = payload.get("browser_walk")
    states = walk.get("states") if isinstance(walk, dict) else None
    for state in states if isinstance(states, list) else []:
        lines.append(f"* **{state['state']}** — {state['asserted']}")
    lines += [
        "",
        "The captures land in `docs/media/compare-balanced`, named",
        "`<state>-<viewport>-<theme>.png`, at 1640x900/1440x900/1280x800/390x844 in light and dark.",
        "",
        "## Checks",
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
                    "the compare-balanced inventory has drifted from the source: "
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
