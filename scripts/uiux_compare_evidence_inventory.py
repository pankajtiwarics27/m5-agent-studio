#!/usr/bin/env python3
"""Inventory the Compare pane's results, verdict provenance and evidence rail (UIV2-17).

    python3 scripts/uiux_compare_evidence_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_compare_evidence_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_compare_evidence_inventory.py --json     # print the inventory

The approved target for this pane says *equal columns, evidence in the inspector*. The pane had
the columns and none of the evidence, and three things were wrong with what it did have:

* **a silence was drawn as a zero.** A panel the provider failed on, or one that never started,
  reported `0 ch` in the results table - a measured-looking zero for a reply that does not exist,
  in a pane whose provenance table refuses exactly that ("a zero would read as a measurement").
* **the verdict had no provenance.** The arbiter card said `Arbiter Verdict (<model>)` and a badge.
  It did not say that the model was the arbiter, over what question, that the replies were its
  evidence, or where its own figures came from - and a verdict a reader cannot trace is an opinion
  with a badge on it.
* **the inspector counted and pointed elsewhere.** The Evaluation view printed six counts and then
  "Scores and the arbiter verdict render in the Compare pane; this view summarizes them". The rail
  that the target calls the home of the evidence carried none of it, could not say that a panel had
  failed, and counted criteria against a run that had not happened.

What this inventory checks: the dash where a reply has no size, the labelled cells and the stacked
narrow layout that keep six columns readable on a phone, the verdict card and its provenance line,
the rail's evidence rows - read from the same `debateProvenanceOf()` the pane's table reads, so the
two cannot disagree - the reason attached to a reply that reported nothing, the no-run state, the
cost that is not invented, and the preservation of the P4-05 provenance table, the P4-03 results
table and the single debate-stream call site.

It fails when a silence becomes a zero, when the verdict stops naming where it came from, when the
rail stops repeating the pane, when a failure loses its reason, or when a preserved table changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK_PATH = PROJECT_ROOT / "scripts" / "compare_evidence_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Evidence.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-evidence.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "compare-evidence"

STORY = "UIV2-17"
TITLE = "Compare results and evidence inspector"

#: The columns each preserved table keeps. A story about evidence does not get to drop a column
#: from a table a reader already relies on.
RESULT_COLUMNS = ("Model", "Status", "Size", "Criteria it met")
PROVENANCE_COLUMNS = ("Model", "Elapsed", "First token", "Reply tokens", "Decode", "Cost")

#: What the walk has to assert, by the name it uses on the command line. The captures below are
#: only evidence if a run asserted the state they show.
WALK_STATES = (
    ("empty", "before a run the pane shows no table and invents no figure"),
    ("no-run-rail", "and the inspector says there is no run rather than counting nothing"),
    ("rows", "each reply is one row in the results and one row in the provenance table"),
    ("type", "the two answers are drawn at one type scale, since side by side is a claim about comparability"),
    ("size", "a reply with text has a size, and the size is the reply's own length"),
    ("verdict", "the verdict names the model that judged, that it judged this question, and what its evidence was"),
    ("arbiter-row", "and the arbiter is one row in the table, marked as the arbiter"),
    ("rail", "the inspector carries the evidence, one line per reply"),
    ("rail-basis", "and says where the figures came from, and that cost is not known"),
    ("error", "a failure is attributed to the row of the panel it happened to, and the other panel's outcome is stated"),
    ("dash", "a panel with no reply shows a dash, never 0 ch"),
    ("reason", "and its figures are refused with a reason rather than a zero"),
    ("loading", "while a panel is streaming, its figures are described as arriving with the reply"),
    ("stopped", "a stopped run is attributed, explained in both places, and leaves Start ready"),
    ("long", "a long reply scrolls inside its card, and the table reports its whole size"),
    ("narrow", "at 390x844 both tables fit the pane and nothing overflows sideways"),
)

#: The story's states, and what each one looks like. These names are the capture names.
STATES = (
    ("empty", "the pane before a comparison: both tables gone, and a rail that says there is no run"),
    ("populated", "two replies answered, one row each in both tables, at one type scale"),
    ("verdict", "the arbiter's verdict, with the model that judged it and the replies as its evidence"),
    ("error", "one reply beside a provider that failed: a dash where a size would be, and the reason"),
    ("loading", "a panel still streaming: its figures described as arriving with the reply"),
    ("stopped", "a run the reader stopped: attributed in the pane and in the rail, Start ready again"),
    ("long-content", "a reply long enough that its card scrolls, and a size the table reports in full"),
)


def code_only(source: str) -> str:
    """``source`` without its comment lines: prose about a rule is not the rule."""
    return "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))


def escaped_values(markup: str) -> list[str]:
    """Values drawn into markup without passing through `escapeHtml(` or `String(`."""
    return [
        match.group(1).strip()
        for match in re.finditer(r"\$\{([^{}]*)\}", markup)
        if not match.group(1).strip().startswith(("escapeHtml(", "Number(", "String(", "Boolean("))
    ]


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    code = code_only(html)
    seed = SEED.read_text(encoding="utf-8")
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    findings: list[dict[str, str]] = []
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})
        if not ok:
            findings.append({"check": name, "detail": detail})

    results_fn = re.search(r"function renderDebateResults\(\)[\s\S]*?\n  \}", code)
    provenance_fn = re.search(r"function renderDebateProvenance\(\)[\s\S]*?\n  \}", code)
    verdict_fn = re.search(r"function debateVerdictCard\(\)[\s\S]*?\n  \}", code)
    verdict_provenance_fn = re.search(r"function debateVerdictProvenance\(\)[\s\S]*?\n  \}", code)
    rail_fn = re.search(r'if \(v === "evaluation"\) \{[\s\S]*?\n      return;\n    \}', code)
    # Bound once, so a rule cannot read a group off a pattern that did not match.
    results_src = results_fn.group(0) if results_fn else ""
    provenance_src = provenance_fn.group(0) if provenance_fn else ""
    verdict_src = verdict_fn.group(0) if verdict_fn else ""
    verdict_prov_src = verdict_provenance_fn.group(0) if verdict_provenance_fn else ""
    rail_src = rail_fn.group(0) if rail_fn else ""

    # ── 1. a silence is not a zero ─────────────────────────────────────────────
    check(
        "a reply with no text has no size: the results table draws a dash, never 0 ch",
        bool(results_src)
        and 'const size = text.length ? String(text.length) + " ch" : "—";' in results_src
        and 'String(String(slot.c || "").length) + " ch"' not in code,
        "a zero for a reply that does not exist reads as a measurement",
    )
    check(
        "the results table still reports the size of a reply that does exist",
        bool(results_src) and 'escapeHtml(size)' in results_src,
        "the dash replaced the figure, not the column",
    )

    # ── 2. six columns on a phone: labelled cells, stacked rows ───────────────
    check(
        "every cell of the results table carries the label its column head gives it",
        bool(results_src)
        and all(f'data-label="{label}"' in results_src for label in RESULT_COLUMNS),
        ", ".join(label for label in RESULT_COLUMNS if not results_src or f'data-label="{label}"' not in results_src),
    )
    measured_columns = PROVENANCE_COLUMNS[1:-1]
    check(
        "every cell of the provenance table carries the label its column head gives it",
        bool(provenance_src)
        and "data-label=\"' + escapeHtml(label) + '" in provenance_src
        and all(f'cell("{label}"' in provenance_src for label in measured_columns)
        and 'data-label="Model"' in provenance_src
        and 'data-label="Cost"' in provenance_src,
        ", ".join(label for label in measured_columns if not provenance_src or f'cell("{label}"' not in provenance_src),
    )
    check(
        "below a phone width both tables stack instead of scrolling sideways",
        '@media (max-width: 600px)' in html
        and "content: attr(data-label)" in html
        and ".debate-results tbody td, .debate-provenance tbody td {" in html,
        "six columns in 390px is a clipped column, not a responsive table",
    )
    check(
        "the stacked table keeps its headings for a screen reader",
        "clip-path: inset(50%)" in html and "position: absolute" in html,
        "deleting the head would take the column names away from anyone not seeing the labels",
    )

    # ── 3. the verdict says where it came from ────────────────────────────────
    check(
        "the arbiter's answer is drawn as a verdict, and the card says so in its markup",
        bool(verdict_src) and 'data-role="verdict"' in verdict_src
        and "Arbiter verdict" in verdict_src,
        "a card with no role is a card a test cannot tell from an answer",
    )
    check(
        "the verdict names the model that judged it, and the catalogue row it came from",
        bool(verdict_prov_src)
        and "Judged this question as " in verdict_prov_src
        and "not in the current catalogue" in verdict_prov_src
        and "unifiedCatalog.find(item => item.id === model)" in verdict_prov_src,
        "a verdict from a model nobody can find is a verdict with no source",
    )
    check(
        "the verdict says the replies are its evidence",
        bool(verdict_prov_src) and "The replies above are its evidence." in verdict_prov_src,
        "evidence a reader is not told about is evidence they will not check",
    )
    check(
        "a demo verdict says the server wrote it, rather than carrying figures it never had",
        bool(verdict_prov_src) and "The server wrote this verdict, so it carries no model figures." in verdict_prov_src,
        "P4-05 refused invented figures for a demo reply; a verdict is not exempt",
    )
    check(
        "the verdict's own figures are drawn by the same badge the table reads",
        bool(verdict_src) and "debateArbiterBadge(debateSynth.u)" in verdict_src,
        "two places to compute one figure is two figures",
    )
    check(
        "the provenance line is escaped value by value",
        bool(verdict_src)
        and "data-verdict-provenance" in verdict_src
        and ".map(line => escapeHtml(line)).join" in verdict_src,
        "a model id and a catalogue label are user-controlled strings",
    )

    # ── 4. the rail carries the evidence ──────────────────────────────────────
    check(
        "the rail's Evaluation view reads the same usage the pane's table reads",
        bool(rail_src) and "debateProvenanceOf(slot)" in rail_src,
        "a rail that computes its own figures can disagree with the table in the same window",
    )
    check(
        "the rail draws one evidence line per reply, naming the model and its status",
        bool(rail_src)
        and 'class="insp-item debate-evidence"' in rail_src
        and 'data-evidence-model="' in rail_src
        and 'data-evidence-status="' in rail_src,
        "a count of replies is not evidence about a reply",
    )
    check(
        "a reply with no figures is given the reason in the rail, including the panel's own error",
        bool(rail_src)
        and "reading.note" in rail_src
        and "slot.error" in rail_src
        # the note has to be *joined* to the panel's own error, not merely near it
        and re.search(r"reading\.note\s*\+\s*\(\s*slot\.error", rail_src) is not None,
        "`—` with no reason is the silence this story exists to remove",
    )
    check(
        "the rail says where the figures came from, and that cost is not known",
        bool(rail_src)
        and "Read from the usage the server sent with each reply" in rail_src
        and "Cost: not known" in rail_src,
        "a rail that repeats figures also inherits their basis",
    )
    check(
        "the rail states the arbiter, or says none was requested",
        bool(rail_src) and 'inspKV("Verdict", debateSynth.m || "not requested")' in rail_src,
        "an absent verdict is a fact about the run, not an empty row",
    )
    check(
        "the rail can reach the rows it summarises",
        bool(rail_src)
        and "focusDebateProvenance(this)" in rail_src
        and "function focusDebateProvenance(button)" in code
        and 'host.setAttribute("tabindex", "-1")' in code,
        "a summary you cannot reach is a dead end",
    )
    check(
        "with no run the rail says so instead of counting nothing",
        bool(rail_src)
        and "if (!panels.length)" in rail_src
        and "No comparison yet." in rail_src
        and "Scores and the arbiter verdict render in the Compare pane" not in code,
        "the rail used to point at the pane for the evidence it now carries",
    )
    check(
        "the rail redraws with the run, not only when the tab changes",
        "if (inspectorView === \"evaluation\") refreshInspectorPanel();" in code,
        "evidence that only appears after a tab switch is evidence nobody sees",
    )

    # ── 5. nothing is invented ────────────────────────────────────────────────
    check(
        "no price, score or quality figure is invented anywhere in the pane",
        "Cost: not known" in code
        and "No price is configured for any model" in code
        and not re.search(r"quality_score|correctness_score|price_per_?token\s*=\s*\d", code),
        "the story's gate is that no number appears that the run did not report",
    )
    check(
        "the measured criteria are the only ones a reply can meet",
        "function debateCriteriaMet(slot)" in code
        and 'if (slot.status === "answered") met.push("finished");' in code
        and 'met.push("size")' in code
        and 'met.push("cost")' not in code,
        "a judged criterion met by a reply would be a score",
    )
    check(
        "a cost cell is a dash with its own explanation, in every row",
        bool(provenance_src)
        and 'data-label="Cost" title="No price is configured for any model"' in provenance_src,
        "an empty cell and an unknown cost look the same",
    )

    # ── 6. what the story preserved ───────────────────────────────────────────
    check(
        "the provenance table still has its six columns",
        all(f"<th scope=\"col\">{label}</th>" in (provenance_src if provenance_fn else "") for label in PROVENANCE_COLUMNS),
        ", ".join(label for label in PROVENANCE_COLUMNS if not provenance_src or f"<th scope=\"col\">{label}</th>" not in provenance_src),
    )
    check(
        "the results table still has its four columns",
        all(f"<th scope=\"col\">{label}</th>" in (results_src if results_fn else "") for label in RESULT_COLUMNS),
        ", ".join(label for label in RESULT_COLUMNS if not results_src or f"<th scope=\"col\">{label}</th>" not in results_src),
    )
    check(
        "both tables are still hidden until there is a run",
        bool(results_src) and bool(provenance_src)
        and "host.hidden = true;" in results_src
        and "host.hidden = true;" in provenance_src,
        "an empty table is a claim that something was compared",
    )
    check(
        "the two regions are still labelled for a screen reader",
        'role="region" aria-label="Comparison results"' in html
        and 'role="region" aria-label="Measured performance per reply"' in html,
        "a region with no name is a div",
    )
    check(
        "one summon call site: the pane still posts only the debate stream",
        code.count('consumeSSE("/api/debate/stream"') == 1,
        "a second call site would be a second place to keep in step",
    )

    # ── 7. the evidence this story ships ──────────────────────────────────────
    check(
        "the fixture can answer at length, so the long-content state is real",
        "\"--long-model\"" in seed
        and "LONG_REPLY" in seed
        and "LONG_MODEL = long_model" in seed
        and "_reply_words(model)" in seed,
        "a state no fixture can produce is a state that gets claimed instead of shown",
    )
    check(
        "the walk asserts every state the captures show",
        bool(walk) and all(needle in walk for _, needle in WALK_STATES),
        ", ".join(name for name, needle in WALK_STATES if not walk or needle not in walk),
    )
    check(
        "the walk needs no environment of its own",
        all(
            token in walk
            for token in (
                "STUDIO_SCREENSHOT_CHROME",
                "STUDIO_PUPPETEER_CORE",
                "--capture-only",
                "--states",
                "council_readiness_seed.py",
            )
        )
        and not re.search(r"/tmp/|/Users/", walk),
        "a walk that hard-codes a path is not reproducible",
    )

    return {
        "story": STORY,
        "title": TITLE,
        "status": "clean" if not findings else "findings",
        "check_count": len(checks),
        "checks": checks,
        "findings": findings,
        "states": [{"state": name, "what": what} for name, what in STATES],
        "columns": {"results": list(RESULT_COLUMNS), "provenance": list(PROVENANCE_COLUMNS)},
        "evidence": {
            "walk": "scripts/compare_evidence_walk.mjs",
            "media": "docs/media/compare-evidence",
            "viewports": ["1640x900", "1440x900", "1280x800", "390x844"],
            "themes": ["light", "dark"],
        },
    }


def render_doc(payload: dict) -> str:
    lines = [
        f"# Compare's results, verdict provenance and evidence rail ({STORY})",
        "",
        "Generated by `scripts/uiux_compare_evidence_inventory.py` — **do not hand-edit**.",
        "Run `python3 scripts/uiux_compare_evidence_inventory.py --check` to see whether this doc,",
        "the JSON beside it and the source still agree.",
        "",
        "## What was wrong",
        "",
        "1. **A silence was drawn as a zero.** A panel the provider failed on showed `0 ch` in the",
        "   results table — a measured-looking zero for a reply that does not exist, in a pane whose",
        "   provenance table refuses exactly that.",
        "2. **The verdict had no provenance.** The card said `Arbiter Verdict (<model>)` and a badge:",
        "   not that the model was the arbiter, not what it judged, not that the replies were its",
        "   evidence, and not where its figures came from.",
        "3. **The rail pointed at the pane.** The Evaluation view counted criteria and then said",
        "   \"Scores and the arbiter verdict render in the Compare pane\" — while the approved target",
        "   calls the inspector the home of the evidence. It could not say a panel had failed, and it",
        "   counted criteria against a run that had never happened.",
        "4. **Six columns would not fit a phone.** The provenance table kept `white-space: nowrap` on",
        "   every figure and overflowed its card by 65px at 390x844.",
        "",
        "## What the pane draws now",
        "",
        "| Table | Columns | Where the figures come from |",
        "| --- | --- | --- |",
        "| Results | " + ", ".join(payload["columns"]["results"]) + " | the reply's own text and the response's finish reason |",
        "| Provenance | " + ", ".join(payload["columns"]["provenance"]) + " | the `usage` the server sent with each reply |",
        "",
        "A reply with no text has no size: the cell is a dash. Below 600px both tables stack — every",
        "row becomes a block and every cell carries the label its column head gives it — with the",
        "`thead` clipped rather than deleted, so the column names stay for a screen reader.",
        "",
        "## The verdict",
        "",
        "The arbiter's answer is a card with `data-role=\"verdict\"`, and under its head a provenance",
        "line: *Judged this question as <model> (<catalogue row>). The replies above are its evidence.*",
        "A demo verdict says the server wrote it and carries no model figures; a verdict whose usage",
        "has not arrived says so. Its badge is the same `debateArbiterBadge()` the table reads.",
        "",
        "## The rail",
        "",
        "The Evaluation view reads `debateProvenanceOf()` — the same function the pane's table reads,",
        "so the two cannot disagree — and draws:",
        "",
        "* the criteria counts, as before;",
        "* `Panels`, as `answered of total`;",
        "* `Result columns`, and `Verdict` (the arbiter, or `not requested`);",
        "* one evidence line per reply: the model, its status, and either the four figures the",
        "  response carried or the reason it carried none — including the panel's own error text;",
        "* the basis of those figures, and that cost is not known because no price is configured;",
        "* a button that puts the reader on the provenance rows themselves.",
        "",
        "With no run, the rail says there is no comparison yet instead of counting zeroes.",
        "",
        "## The states this story ships",
        "",
        "| State | What it shows |",
        "| --- | --- |",
    ]
    for row in payload["states"]:
        lines.append(f"| `{row['state']}` | {row['what']} |")
    lines += [
        "",
        "Each state is asserted by `scripts/compare_evidence_walk.mjs` before its capture is",
        "written, at 1640x900, 1440x900, 1280x800 and 390x844 in light and dark. The long-content",
        "state needs a reply long enough to scroll, so the fixture gained `--long-model`; the stopped",
        "state needs a reply slow enough to stop, which is `--slow-model`; the failure state uses the",
        "catalogue's own keyless provider row, which fails without a network.",
        "",
        "## What this story does not do",
        "",
        "* it does not change what a comparison is judged on, who sits in it, or which models can be",
        "  picked — those are P4-03/P4-04 and UIV2-16;",
        "* it does not add a route, a migration or a column, and it does not price anything;",
        "* it does not move the evidence out of the pane: the two tables stay, and the rail repeats",
        "  them from the same source rather than replacing them.",
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_compare_evidence_inventory.py",
        "python3 scripts/uiux_compare_evidence_inventory.py --check",
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
                    "the compare-evidence inventory has drifted from the source: "
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
