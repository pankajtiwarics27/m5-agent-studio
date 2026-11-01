#!/usr/bin/env python3
"""Inventory what the Compare pane states: its criteria and each model's fate (UIUX-P4-04).

    python3 scripts/uiux_compare_criteria_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_compare_criteria_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_compare_criteria_inventory.py --json     # print the inventory

The Compare pane offered checkboxes, a question and a wall of cards. Nothing said what the
comparison was being judged on; a panel that failed raised a toast and left its card saying
"Streaming"; a comparison the reader stopped left the same word on screen; the panels behind a
failure kept saying "Waiting" as if their turn were still coming; and no row said which models
answered.

What is measured here:

* **the criteria**, stated before anything runs, each with what settles it and whether this pane
  can say it - three from the reply and the response's finish reason, `cost` with the reason it is
  empty (no price is configured for any model), and the reader's own criteria as theirs to judge;
* **the status vocabulary**, ten states and their tones, with a panel's outcome read from the
  response (`usage.demo`, `usage.finish_reason`, whether it wrote anything) rather than from the
  fact that an event arrived;
* **the endings**, which are attributed: a stop, a dropped stream and a server error each say what
  happened, and no panel may keep saying "running" after the stream is gone;
* **the refusal**, which is the point: the table reports what each reply carried and how it ended,
  and never which reply is better - it has no price and no reading of intent, and it says so;
* **the preservation**, the four controls the pane already had, the route it already posted, and
  the stream's events.

It fails when a criterion claims to be settled by something this pane cannot see, when `cost`
gains a figure, when a panel's state is drawn from the wrong source, when a stopped comparison
keeps saying "running", when the results table starts ranking answers, when a value reaches the
page unescaped, and when this story invents an endpoint the server does not mount.
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
WALK = PROJECT_ROOT / "scripts" / "compare_criteria_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-criteria"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Criteria.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-criteria.md"

STORY = "UIUX-P4-04"

CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

# The ten states a panel can be in, and nothing else.
STATUSES = (
    "queued",
    "running",
    "answered",
    "cut",
    "demo",
    "empty",
    "failed",
    "stopped",
    "lost",
    "not-run",
)

# The tones the shared status component accepts.
TONES = ("ok", "warn", "danger", "info", "busy", "neutral")

# The criteria the pane states, in order: the three it can settle, then the one it cannot price.
MEASURED = ("answered", "finished", "size")
UNSETTLEABLE = "cost"

# The controls this pane had before the story, which must still be there and still be wired.
PRESERVED_CONTROLS = (
    "debatePrompt",
    "debateRunBtn",
    "debateStopBtn",
    "debateModelChecks",
    "debateGrid",
    "debateSynthesisArea",
    "d-synth",
    "d-judge",
)

# What a comparison may never claim. The pane reports what a reply carried; it does not score it.
FORBIDDEN_CLAIMS = ("best", "winner", "highest score", "rank", "quality score")

# The stream events the compare route speaks; none of them may disappear.
STREAM_EVENTS = ("panel_start", "token", "panel_done", "synthesis_start", "synthesis_done", "debate_done")

# The captures the doc lists, and the walk must be able to produce.
CAPTURES = (
    "compare-criteria.png",
    "compare-partial-failure.png",
    "compare-stopped.png",
    "compare-offline.png",
    "compare-light.png",
    "compare-criteria-narrow.png",
)

SAFE_EXPRESSIONS = {"", "null", "undefined", "true", "false"}


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
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


def python_function(source: str, name: str) -> str:
    """The indented body of ``def name(...)`` in Python source, by dedent."""
    match = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M)
    if not match:
        return ""
    depth = 0
    index = match.start()
    while index < len(source):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    colon = source.find(":", index)
    if colon < 0:
        return ""
    body_start = source.find("\n", colon)
    if body_start < 0:
        return ""
    collected: list[str] = []
    for line in source[body_start + 1 :].splitlines():
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def element(source: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal in ``source``, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def unescaped_interpolations(markup: str) -> list[str]:
    """``${…}`` expressions inside markup that do not pass through `escapeHtml(` or `String(`."""
    offenders: list[str] = []
    for expression in _expressions(markup):
        if "${" in expression:
            offenders.extend(unescaped_interpolations(expression))
            continue
        stripped = expression.strip()
        if stripped in SAFE_EXPRESSIONS:
            continue
        if stripped.startswith(("escapeHtml(", "Number(", "String(", "Boolean(")):
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*Html", stripped):
            continue
        offenders.append(stripped)
    return offenders


def _expressions(markup: str) -> list[str]:
    """Every ``${…}`` body in ``markup``, brace-balanced."""
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


def ledger_story() -> dict:
    """The story's own tracking entry, so this artifact can fail if the ledger forgets it."""
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


def label_reference(status: str) -> str:
    """How the badge reads the label off `DEBATE_STATUS`: dotted, or bracketed when it needs quoting."""
    if status.isidentifier():
        return f"DEBATE_STATUS.{status}.label"
    return 'DEBATE_STATUS["' + status + '"].label'


def statuses_of(block: str) -> list[str]:
    """The keys of the `DEBATE_STATUS` literal, in order."""
    return re.findall(r'^\s{4}"?([a-z-]+)"?:\s*\{\s*label:', block, re.M)


def tone_of(block: str, status: str) -> str:
    """The tone the status literal gives ``status``."""
    match = re.search(rf'"?{re.escape(status)}"?:\s*\{{\s*label: "[^"]*", tone: "([a-z]+)"', block)
    return match.group(1) if match else ""


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    seed = SEED.read_text(encoding="utf-8") if SEED.exists() else ""
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    criteria_meta = re.search(r"const DEBATE_CRITERIA = \[(.*?)\n  \];", html, re.S)
    criteria_block = criteria_meta.group(1) if criteria_meta else ""
    status_meta = re.search(r"const DEBATE_STATUS = \{(.*?)\n  \};", html, re.S)
    status_block = status_meta.group(1) if status_meta else ""

    met = function_body(html, "debateCriteriaMet")
    counts = function_body(html, "debateCounts")
    summary = function_body(html, "renderDebateSummary")
    results = function_body(html, "renderDebateResults")
    criteria_render = function_body(html, "renderDebateCriteria")
    run_control = function_body(html, "updateDebateRunControl")
    render = function_body(html, "renderDebate")
    run = function_body(html, "runDebate")
    end = function_body(html, "debateEnd")
    add = function_body(html, "addDebateCriterion")
    remove = function_body(html, "removeDebateCriterion")
    init = html[html.find("function initDebatePane()") :][:900]

    debate_functions = "".join(
        [met, counts, summary, results, criteria_render, run_control, render, run, end, add, remove]
    )

    route = python_function(server, "api_debate_stream")

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the criteria ────────────────────────────────────────────────────────
    check(
        "the criteria are stated before the run, in a labelled region drawn at load",
        'id="debateCriteria"' in html
        and 'role="region"' in element(html, "debateCriteria")
        and 'aria-label="Comparison criteria"' in element(html, "debateCriteria")
        and html.index('id="debateCriteria"') < html.index('id="debatePrompt"')
        and "renderDebateCriteria();" in init,
        "a criterion named after the results exist is a rationalization, not a criterion",
    )
    check(
        "the three criteria this pane can settle are the ones the reply and the runtime report",
        all(f'key: "{key}"' in criteria_block for key in MEASURED)
        and criteria_block.count('kind: "measured"') == 3
        and "the response's finish reason" in criteria_block
        and "the reply the server saved" in criteria_block,
        "a measured criterion must name the thing that measures it",
    )
    check(
        "cost is stated, and stated as unsettleable rather than guessed",
        f'key: "{UNSETTLEABLE}"' in criteria_block
        and 'kind: "not-stated"' in criteria_block
        and "no price is configured for any model" in criteria_block
        and not re.search(r"\$\s?\d", criteria_block + debate_functions)
        and "price_per" not in criteria_block + debate_functions,
        "a pane that has no price may not print money",
    )
    check(
        "the reader's criteria are listed as theirs to judge, and can be added and removed",
        "DEBATE_JUDGED_DEFAULTS" in html
        and "debateJudged.push(label)" in add
        and "debateJudged.splice(index, 1)" in remove
        and "yours to judge" in criteria_render,
        "the criteria a reply cannot settle are the ones only a reader can",
    )
    check(
        "Enter adds a criterion from one delegated listener, and the markup carries no inline handler",
        'onkeydown="' not in html
        and 'criteria.addEventListener("keydown"' in html
        and 'event.target.id === "debateCriterionInput"' in html,
        "an inline handler is a second place a key can be sent from; the strip is rewritten on every add",
    )
    check(
        "adding or removing a criterion keeps the focus in the add control",
        "renderDebateCriteria(true)" in add and "renderDebateCriteria(true)" in remove
        and "input.focus()" in criteria_render,
        "the strip is rewritten on every add, and a rewrite that drops focus makes it unusable",
    )

    # ── the statuses ────────────────────────────────────────────────────────
    tones = {status: tone_of(status_block, status) for status in STATUSES}
    check(
        "a panel has one of ten states, and each carries a tone the status component accepts",
        tuple(statuses_of(status_block)) == STATUSES
        and all(tones[status] in TONES for status in STATUSES)
        and all(
            f'data-tone="{tones[status]}">' in html and label_reference(status) in html
            for status in STATUSES
        )
        and "DEBATE_BADGE[slot.status] || DEBATE_BADGE.queued" in render,
        ", ".join(f"{status}={tones[status] or 'missing'}" for status in STATUSES),
    )
    check(
        "a panel's outcome is read from the response, not from an event having arrived",
        'usage.demo === true' in run
        and 'String(usage.finish_reason || "") === "length"' in run
        and "wrote ? \"answered\" : \"empty\"" in run,
        "a cut reply, a demo reply and an empty reply are three endings, not one",
    )
    check(
        "no panel keeps saying it is running after the stream is gone",
        'handlers.stopped' not in run
        and "stopped() { debateEnd(\"stopped\", \"\"); }" in run
        and "closed() { debateEnd(\"lost\", \"\"); }" in run
        and "debateEnd(\"failed\"" in run
        and run.count('debateEnd("lost", "")') >= 2
        and 'slot.status = "not-run"' in end
        and 'slot.status === "running"' in end,
        "the state that made this story: a stopped comparison that still said Streaming",
    )
    check(
        "the old status strings are gone from the pane",
        '"Streaming"' not in render
        and '"Waiting"' not in render
        and "_Waiting for its turn..._" in render,
        "a status word that survives the code that drew it is the defect",
    )
    check(
        "a failed panel quotes the failure and says nothing was saved for it",
        "did not answer" in render
        and "Nothing was saved for it." in render
        and "slot.error" in render,
        "a toast is not a state on a card",
    )
    check(
        "a stopped or cut-off panel keeps what it had written, and says which it was",
        "You stopped the comparison while this panel was running" in render
        and "The stream ended before this panel reported" in render
        and "renderMarkdown(slot.c || \"\")" in render,
        "the surviving text is the only useful thing a stopped panel has",
    )

    # ── the run line and the results ────────────────────────────────────────
    check(
        "the run line counts the panels by state",
        all(state in counts for state in STATUSES)
        and "counts.total + (counts.total === 1 ? \" panel\" : \" panels\")" in summary
        and "host.dataset.state" in summary,
        "one line, and every state in it is a count of something on screen",
    )
    check(
        "the results table states one row per panel and the four columns",
        ">Model</th>" in results and ">Status</th>" in results and ">Size</th>" in results
        and ">Criteria it met</th>" in results
        and "panels.map(slot =>" in results
        and 'role="region"' in element(html, "debateResults")
        and "host.hidden = false" in results,
        "a comparison without a row per model is a wall of cards",
    )
    check(
        "the results refuse to score: cost and judgement are named as not settled here",
        "Not settled here:" in results
        and "no price is configured for any model" in results
        and "yours to judge" in results
        and "not which reply is better" in results,
        "the pane reports what a reply carried; it does not rank answers",
    )
    check(
        "the pane claims no ranking or quality anywhere in this feature",
        all(claim not in debate_functions.lower() for claim in FORBIDDEN_CLAIMS),
        ", ".join(claim for claim in FORBIDDEN_CLAIMS if claim in debate_functions.lower()),
    )
    check(
        "a demo reply is credited with a size and nothing else",
        'if (slot.status === "demo") return text.length ? ["size"] : [];' in met
        and 'if (slot.status === "answered") met.push("finished");' in met,
        "a reply the server wrote is not a model answering or finishing",
    )
    check(
        "the results are hidden until a panel exists",
        "host.hidden = true" in results and "host.hidden = false" in results,
        "an empty table is a claim that something was compared",
    )

    # ── the control that used to do nothing ────────────────────────────────
    check(
        "Start is disabled on the same predicate that writes its hint",
        "button.disabled = !ready" in run_control
        and "Select at least one model to compare." in run_control
        and "Write the question to compare on." in run_control
        and "showToast(prompt ?" in run,
        "a button that silently does nothing is its own defect",
    )
    # The arbiter card is the surface the pane already had and P4-05 owns; the escaping rule is
    # applied to what this story draws, which is every card, the strip, the run line and the table.
    # The markup is built by concatenation, so the rule is concrete: each value drawn as text has to
    # be the argument of `escapeHtml(`, and no future edit may add a bare `${…}` on top of that.
    own_markup = debate_functions.split("const synthesis = document.getElementById(\"debateSynthesisArea\");")[0]
    escaped_values = (
        "escapeHtml(slot.model)",
        "escapeHtml(meta.label)",
        "escapeHtml(slot.status)",
        'escapeHtml(slot.error || "the provider failed")',
        "escapeHtml(criterion.label)",
        "escapeHtml(criterion.settles)",
        "escapeHtml(label)",
        "escapeHtml(criterion.why || \"\")",
        "escapeHtml(metText)",
    )
    missing = [token for token in escaped_values if token not in own_markup]
    check(
        "every value the pane draws from a model or an error is escaped",
        not missing and not unescaped_interpolations(own_markup),
        ", ".join(missing) or ", ".join(unescaped_interpolations(own_markup)),
    )
    check(
        "the pane posts only the route it already posted, and invents none",
        called_routes(debate_functions) == ["/api/debate/stream"],
        ", ".join(called_routes(debate_functions)),
    )

    # ── what the pane already had ──────────────────────────────────────────
    check(
        "every control the pane had is still there and still wired",
        all(element(html, control_id) or f'id="{control_id}"' in html for control_id in PRESERVED_CONTROLS)
        and "syncDebateArbiter()" in html
        and "debateSynthesisArea" in html
        and "stopActiveStream()" in element(html, "debateStopBtn") + html,
        ", ".join(control for control in PRESERVED_CONTROLS if f'id="{control}"' not in html),
    )
    check(
        "the compare route still speaks the same six events",
        all(f'_sse("{event}"' in route for event in STREAM_EVENTS),
        ", ".join(event for event in STREAM_EVENTS if f'_sse("{event}"' not in route),
    )

    # ── the evidence this story ships ──────────────────────────────────────
    check(
        "the fixture can produce a stopped comparison and a cut panel",
        "--slow-model" in seed
        and "slow_word_seconds" in seed
        and "--cap-model" in seed
        and "def _reply_words(model: str) -> str:" in seed,
        "a state a fixture cannot produce is a state no screenshot can show",
    )
    check(
        "the walk exercises every state this story adds",
        all(
            token in walk_body
            for token in (
                '"stopped"',
                '"cut"',
                '"demo"',
                '"failed"',
                "2 panels · 1 answered · 1 failed",
                "1 panel · 1 cut at the length limit",
                "1 panel · 1 stopped by you",
                "Not settled here",
                "yours to judge",
            )
        ),
        "the states have to be seen, not only asserted",
    )
    check(
        "the captures the doc lists exist and are the pane's own",
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
        "title": "Compare criteria and side-by-side results",
        "generated_by": "scripts/uiux_compare_criteria_inventory.py",
        "surfaces": {
            "criteria": "#debateCriteria",
            "run_line": "#debateSummary",
            "panels": "#debateGrid",
            "results": "#debateResults",
            "run_hint": "#debateRunHint",
        },
        "criteria": [
            {"key": "answered", "settled_by": "the panel's own reply", "kind": "measured"},
            {"key": "finished", "settled_by": "the response's finish reason", "kind": "measured"},
            {"key": "size", "settled_by": "the reply the server saved", "kind": "measured"},
            {
                "key": "cost",
                "settled_by": "a price for the model",
                "kind": "not-stated",
                "why": "no price is configured for any model, so this pane claims no money",
            },
            {"key": "judged", "settled_by": "you", "kind": "judged"},
        ],
        "statuses": list(STATUSES),
        "status_tones": {status: tone_of(status_block, status) for status in STATUSES},
        "refusals": [
            "cost - no price is configured for any model",
            "judgement - the reader's criteria are theirs to judge",
            "ranking - the table reports what each reply carried, not which reply is better",
        ],
        "stream_events": list(STREAM_EVENTS),
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-04 — Compare criteria and side-by-side results",
        "",
        "Generated by `scripts/uiux_compare_criteria_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the Compare pane now says",
        "",
        "| Where | What it says |",
        "| :--- | :--- |",
        "| the criteria strip | what this comparison is judged on, before it runs: what settles each"
        " criterion and whether this pane can say it |",
        "| the run hint | why Start is off (`Select at least one model…`, `Write the question…`) or what"
        " it will do (`Ready: 2 models, one question.`) |",
        "| each panel | one status from a fixed vocabulary, and a sentence saying what happened to it |",
        "| the run line | how many panels, and how many of each state |",
        "| the results table | one row per model: status, size, and which of the measured criteria it met |",
        "",
        "## The criteria",
        "",
        "| Criterion | Settled by | Can this pane say it? |",
        "| :--- | :--- | :--- |",
    ]
    for criterion in payload["criteria"]:
        if criterion["kind"] == "measured":
            answer = "yes — after each panel reports"
        elif criterion["kind"] == "not-stated":
            answer = f"no — {criterion['why']}"
        else:
            answer = "no — yours to judge"
        lines.append(f"| {criterion['key']} | {criterion['settled_by']} | {answer} |")
    lines += [
        "",
        "The pane also does not settle the two criteria it lists as the reader's, and it says so in",
        "the results table rather than inventing a score for them.",
        "",
        "## The statuses",
        "",
    ]
    meanings = {
        "queued": "the panel is behind the one running",
        "running": "the panel is streaming now",
        "answered": "the panel reported a reply the runtime finished",
        "cut": "the runtime stopped the reply at its length limit",
        "demo": "the local runtime was not answering, so the server wrote the reply itself",
        "empty": "the panel reported, and wrote nothing",
        "failed": "the provider failed; the message is quoted and nothing was saved for this panel",
        "stopped": "the reader stopped the comparison while this panel was running",
        "lost": "the stream ended before this panel reported",
        "not-run": "the comparison ended before this panel's turn",
    }
    for status in payload["statuses"]:
        lines.append(f"* `{status}` ({payload['status_tones'][status]}) — {meanings[status]}.")
    lines += [
        "",
        "## What it refuses to claim",
        "",
        *[f"* {refusal}." for refusal in payload["refusals"]],
        "",
        "## A known limit of the stream, drawn honestly",
        "",
        "The server sends one `error` for the whole comparison: `WorkflowOrchestrator.run_stages` lets",
        "a provider exception propagate, so a failure ends the run rather than failing one panel and",
        "carrying on. The pane attributes that error to the panel that was running and says the panels",
        "behind it never ran. A per-panel `stage_error` event is the orchestrator's change to make; this",
        "pane reports what the stream can tell it.",
        "",
        "## The captures",
        "",
        "All six come from `node scripts/compare_criteria_walk.mjs --screenshots docs/media/compare-criteria`,",
        "against four `scripts/council_readiness_seed.py` processes: plain, `--slow-model`, `--cap-model`,",
        "and `--offline`.",
        "",
        "| File | Size | What it shows |",
        "| :--- | :--- | :--- |",
        "| `compare-criteria.png` | 840 × 392 | the criteria strip, before any run |",
        "| `compare-partial-failure.png` | 908 × 473 | two panels, one answered and one failed:"
        " `2 panels · 1 answered · 1 failed`, the failure quoted on its own card, and the results table"
        " reading the survivor against the criteria |",
        "| `compare-stopped.png` | 908 × 333 | a comparison the reader stopped: `stopped by you`, the"
        " partial reply kept, and `answered, size` — not `finished` |",
        "| `compare-offline.png` | 908 × 567 | no runtime: `demo text`, credited with a size and nothing"
        " else |",
        "| `compare-light.png` | 908 × 880 | the criteria, run line and results in the light theme |",
        "| `compare-criteria-narrow.png` | 480 × 900 | the same blocks at a 480 px viewport, from the"
        " viewport rather than cropped |",
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
                    "the compare-criteria inventory has drifted from the source: "
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
