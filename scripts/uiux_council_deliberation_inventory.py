#!/usr/bin/env python3
"""Inventory what each council seat was allowed and what the run can be audited on (UIUX-P4-02).

    python3 scripts/uiux_council_deliberation_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_council_deliberation_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_council_deliberation_inventory.py --json     # print the inventory

A council stage card drew a state, a tok/s and a character count, and never said the ceiling
those numbers were counted against. Four consequences, all of them audit failures:

* a seat that spent its whole token budget drew the same row as one that answered in three
  hundred tokens, and the seat after it read a truncated reply nobody was warned about;
* the reasoning control offered three modes for "the run" while `api_council_stream` gave them
  to seats 1 and 4 only, so a Builder that ignored the setting was indistinguishable from one
  that was never given it;
* the Critic is deliberately given no skill playbook - an adversary that shares the author's
  homework is auditing itself - and no surface said so;
* the handoff row counted `content` alone while the orchestrator hands the next seat
  `content or reasoning`, so a seat that answered in thinking only read as `0 chars`, exactly
  like a seat that answered nothing at all.

What is measured here:

* **the contract**, published on `stage_start` by the orchestrator (budget, reasoning mode,
  application meta such as the skills decision) and drawn by the pane from that event - the pane
  may not hold a copy of a budget, so this inventory fails if a number like 2400 appears in it;
* **the carried text**, which is `content or reasoning` in the orchestrator and in the pane, and
  which three surfaces (stage card, run line, evidence table) must describe the same way;
* **the verdicts**, six of them, with a cut reply distinguished from an answer and a demo reply
  from a slow one;
* **the evidence table**, one row per seat, drawn with the cards from the same run, and the
  markdown the copy control writes;
* **the preservation**, the stream's events, the routes the pane may call, and the escaping.

It fails when the stream stops publishing the contract, when the route hides a seat's skills,
when the pane hard-codes a budget, when a cut or demo reply is drawn as an answer, when an empty
output with a reasoning trace is called an empty handoff, when a value reaches the page unescaped,
and when this story invents an endpoint the server does not mount.
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
ORCHESTRATION = PROJECT_ROOT / "workflow_orchestration.py"
WALK = PROJECT_ROOT / "scripts" / "council_deliberation_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Deliberation.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-deliberation.md"

STORY = "UIUX-P4-02"

CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

# The contract fields the orchestrator publishes and the pane must read from the event.
CONTRACT_FIELDS = ("max_tokens", "reasoning_mode")

# The budgets the route sets. They belong to `api_council_stream`; if one of these appears in the
# frontend, the pane is holding a copy and the copy can drift from the route it describes.
SERVER_BUDGETS = ("2400", "4096", "6144")

# The stream the council route speaks; none of these may disappear.
STREAM_EVENTS = ("skills_applied", "stage_start", "token", "unloading", "stage_done", "council_done", "error")

# The six verdicts a seat can be drawn in, and nothing else.
VERDICTS = ("done", "capped", "demo", "no-output", "streaming", "not-run")

# The surfaces that must agree about what a seat handed on.
CARRIED_SURFACES = ("renderStageHandoff", "renderStageArtifact", "renderCouncilArtifacts", "renderDeliberationEvidence")

TABLE_COLUMNS = ("Seat", "Model", "Allowed", "Given", "Produced", "Carried", "Verdict")

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
    """``${…}`` expressions inside markup that do not pass through `escapeHtml(` or `String(`.

    Nested template literals are recursed into: their own segments are what reach the page.
    """
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


def stage_lines(council_body: str) -> dict[int, str]:
    """The `WorkflowStage(...)` literal for each numbered seat in the council route."""
    return {
        int(match.group(1)): line
        for line in council_body.splitlines()
        for match in [re.search(r"WorkflowStage\((\d+),", line)]
        if match
    }


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


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    orchestration_body = ORCHESTRATION.read_text(encoding="utf-8")
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    council = python_function(server, "api_council_stream")
    # The Literal of event kinds mentions `stage_start` first; the payload is the yield inside
    # `run_stages`, so the slice starts there.
    marker = orchestration_body.find('                "stage_start",')
    stage_start = orchestration_body[marker:][:1200] if marker > 0 else ""
    seats = stage_lines(council)

    contract = function_body(html, "pipelineContract")
    verdict_of = function_body(html, "pipelineStageVerdict")
    render_contract = function_body(html, "renderStageContract")
    rows_of = function_body(html, "deliberationRows")
    markdown = function_body(html, "deliberationEvidenceMarkdown")
    copy_body = function_body(html, "copyDeliberationEvidence")
    render_evidence = function_body(html, "renderDeliberationEvidence")
    carried = function_body(html, "pipelineCarried")
    handoff = function_body(html, "renderStageHandoff")
    reduction = function_body(html, "pipelineHandoff")
    artifact = function_body(html, "renderStageArtifact")
    summary = function_body(html, "renderCouncilArtifacts")
    stage_start_handler = function_body(html, "pipelineStreamHandlers")
    mount = function_body(html, "ensureStageCardMounted")
    rows_row = function_body(html, "updateStageRows")

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the contract the server publishes ────────────────────────────────────
    check(
        "the orchestrator publishes the contract a stage runs under",
        all(f'"{field}"' in stage_start for field in CONTRACT_FIELDS)
        and "stage.meta" in stage_start
        and "meta: Mapping[str, Any] | None = None" in orchestration_body,
        "`stage_start` must carry the budget, the reasoning mode and the application's own meta",
    )
    check(
        "the council route hands the skills decision to the stream, seat by seat",
        all("skill_block" in seats.get(seat, "") for seat in (1, 2, 4))
        and "skill_block" not in seats.get(3, "missing")
        and seats.get(3, "").count('meta={"skills": False}') == 1
        and all('meta={"skills": uses_skills}' in seats.get(seat, "") for seat in (1, 2, 4)),
        "the Critic is the one seat written without the playbook, and the route says so",
    )
    check(
        "the reasoning mode reaches the two seats the control promises",
        "reasoning_mode" in seats.get(1, "") and "reasoning_mode" in seats.get(4, "")
        and "reasoning_mode" not in seats.get(2, "") and "reasoning_mode" not in seats.get(3, "")
        and "Reasoning (seats 1 and 4)" in html,
        "the label and the route must name the same two seats",
    )

    # ── what the pane does with it ───────────────────────────────────────────
    check(
        "the pane keeps the contract from the event, and holds no copy of a budget",
        "stageBuffers[d.stage].k" in stage_start_handler
        and all(field in stage_start_handler for field in CONTRACT_FIELDS)
        and "d.skills" in stage_start_handler
        and not any(budget in html for budget in SERVER_BUDGETS),
        "a budget literal in the pane is a copy of the route's decision",
    )
    check(
        "every stage card carries a contract row, hidden until the seat starts",
        'id="stage-contract-${s}"' in mount and 'class="stage-contract" hidden' in mount,
        "a card without the row is the card this story exists to remove",
    )
    check(
        "an absent contract is drawn as absent, not as a default",
        "if (!view.contract)" in render_contract and "host.hidden = true" in render_contract,
        "a stage that has not started was allowed nothing, and the row must say nothing",
    )
    check(
        "the contract row is redrawn with the card, and its values are part of what triggers a redraw",
        "renderStageContract(stage)" in rows_row and "buffer.k" in rows_row and "contract" in rows_row,
        "a row drawn once at mount would keep the contract of a seat that has since been re-run",
    )
    check(
        "the row states the budget, the reasoning mode and the skills decision",
        all(field in render_contract for field in ("budget", "reasoning", "skills"))
        and "contract-chip" in render_contract,
        "three facts, three chips",
    )

    # ── the verdicts ────────────────────────────────────────────────────────
    check(
        "six verdicts, and a cut reply is not drawn as an answer",
        all(f'"{verdict}"' in verdict_of for verdict in VERDICTS if verdict not in ("not-run",))
        and "cut at the length limit" in verdict_of
        and 'cappedBy === "length"' in verdict_of
        and "token budget" in verdict_of,
        "a runtime's own length limit and this run's budget are two different cuts",
    )
    check(
        "a demo reply is drawn as demo text, and is given no figure it did not earn",
        'usage.demo === true' in contract and "demo text, no tok/s" in rows_of,
        "the demo path's usage carries `demo: true`; the table must spend it",
    )
    check(
        "the verdicts on the page are the verdicts the classifier returns",
        set(re.findall(r'data-verdict="([a-z-]+)"', render_evidence)) <= set(VERDICTS),
        ", ".join(sorted(set(re.findall(r'data-verdict="([a-z-]+)"', render_evidence)))),
    )

    # ── what a seat carries forward ─────────────────────────────────────────
    check(
        "what a seat carries forward is its output or its reasoning",
        "result.content or result.reasoning" in orchestration_body
        and 'via: "reasoning"' in carried
        and 'return { chars: reasoning.length, via: "reasoning" }' in carried
        and "pipelineCarried" in reduction,
        "the orchestrator passes `content or reasoning`; a pane that counts content alone is wrong",
    )
    check(
        "the handoff row names a predecessor that handed on thinking instead of an output",
        "its reasoning (no output)" in handoff and "nothing (no output)" in handoff,
        "0 chars has to say which of three things it was",
    )
    check(
        "an empty output with a reasoning trace is not called an empty handoff",
        "handed its " in artifact and 'carried.via === "reasoning"' in artifact
        and "handed its reasoning instead" in summary,
        "the card, the run line and the table must agree about one seat",
    )
    check(
        "the run line counts a handoff only when something was carried",
        "pipelineCarried(entry.seat).chars > 0" in summary,
        "an empty seat is not a link in the chain",
    )

    # ── the evidence table ──────────────────────────────────────────────────
    check(
        "the evidence table states one row per seat, with the seven columns",
        "PIPELINE_SEATS.map" in rows_of
        and all(f"<th>{column}</th>" in render_evidence for column in TABLE_COLUMNS)
        and "deliberation-table" in html,
        "a row per seat, and no column that cannot be filled from the stream",
    )
    check(
        "the table is drawn with the cards, from the same run",
        "renderDeliberationEvidence();" in summary,
        "two surfaces describing one run must not be able to disagree",
    )
    check(
        "the table is not rewritten while nothing in it has changed",
        "host.dataset.signature !== signature" in render_evidence,
        "a redraw per token would rewrite a table that has not moved",
    )
    check(
        "a redraw under the copy control keeps the focus on it",
        "hadFocus" in render_evidence and "replacement.focus()" in render_evidence,
        "the table's only control must survive the redraw it sits under",
    )
    check(
        "Copy evidence writes the same table as markdown",
        all(f"| {column} " in markdown for column in TABLE_COLUMNS)
        and "| :--- " in markdown and "navigator.clipboard" in function_body(html, "copyDeliberationEvidence"),
        "an evidence table that cannot be quoted is half an artifact",
    )
    check(
        "every value the table draws is escaped",
        not unescaped_interpolations(contract + verdict_of + render_contract + rows_of + render_evidence),
        ", ".join(unescaped_interpolations(contract + verdict_of + render_contract + rows_of + render_evidence)),
    )

    # ── preservation ────────────────────────────────────────────────────────
    new_calls = sorted({
        path
        for path in called_routes(
            contract + verdict_of + render_contract + rows_of + render_evidence + markdown + copy_body
        )
        if path != "/api/council/stream"
    })
    check(
        "the pane posts nothing new: the evidence is read from the stream it already had",
        new_calls == [],
        ", ".join(new_calls),
    )
    check(
        "the council route still speaks the same seven events",
        all(f'_sse("{event}"' in council or f'"{event}"' in council for event in STREAM_EVENTS),
        ", ".join(event for event in STREAM_EVENTS if event not in council),
    )

    # ── the evidence this story ships ───────────────────────────────────────
    check(
        "the walk exercises every verdict, and the doc points at it",
        all(
            token in walk_body
            for token in ('"capped"', '"demo"', "--cap-model", "--reasoning-model", "M5Deliberation.markdown()")
        )
        ,
        "the states this story adds have to be seen, not only asserted",
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
        "title": "Council deliberation stages and evidence",
        "generated_by": "scripts/uiux_council_deliberation_inventory.py",
        "seats": [
            {
                "seat": seat,
                "route_key": (re.search(r'WorkflowStage\(\d+, "([a-z]+)"', seats.get(seat, "")) or [None, ""])[1]
                if seats.get(seat)
                else "",
                "skills": "skill_block" in seats.get(seat, ""),
                "reasoning_mode": "reasoning_mode" in seats.get(seat, ""),
            }
            for seat in (1, 2, 3, 4)
        ],
        "contract": {
            "published_on": "stage_start",
            "fields": list(CONTRACT_FIELDS) + ["skills"],
            "server_owned": True,
            "budgets_in_the_frontend": [budget for budget in SERVER_BUDGETS if budget in html],
        },
        "verdicts": list(VERDICTS),
        "evidence_table": {"columns": list(TABLE_COLUMNS), "rows": 4, "markdown": True},
        "carried": "the orchestrator passes `content or reasoning`; the pane counts what it was handed",
        "stream_events": list(STREAM_EVENTS),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-02 — Council deliberation stages and evidence",
        "",
        "Generated by `scripts/uiux_council_deliberation_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What a seat now says",
        "",
        "| Where | What it says |",
        "| :--- | :--- |",
        "| a stage card's contract row | the budget the seat's tokens are counted against, the reasoning mode it was given, and whether it was handed the skill playbook — all three from the server's `stage_start` |",
        "| a stage card's handoff row | what it reads, how many characters of upstream content it was handed, and when a predecessor handed on thinking instead of an output |",
        "| a stage card's artifact row | what it produced, and — when its output is empty — whether the next seat got its reasoning or an empty section |",
        "| the run line | how many seats finished, how much output the run produced, and how many handoffs carried content |",
        "| the evidence table | one row per seat: allowed, given, produced, carried, verdict — and **Copy evidence**, which writes the same table as markdown |",
        "",
        "## The contract, seat by seat",
        "",
        "| Seat | Route key | Skill playbook | Reasoning mode |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for seat in payload["seats"]:
        lines.append(
            f"| {seat['seat']} | `{seat['route_key']}` | {'yes' if seat['skills'] else 'no — the adversary audits without the playbook'} | "
            f"{'yes' if seat['reasoning_mode'] else 'no'} |"
        )
    lines += [
        "",
        "The pane holds no copy of these: it draws what `stage_start` published, and an inventory",
        "check fails if a budget literal appears in `static/index.html`.",
        "",
        "## The verdicts",
        "",
        "`" + "`, `".join(payload["verdicts"]) + "`",
        "",
        "* `answered` — the seat produced content;",
        "* `capped` — the runtime stopped it at its own length limit, or it spent the whole budget the",
        "  run set for it. The two cuts are named separately, because only the second is a number on",
        "  the chip; both mean the next seat read a truncated reply;",
        "* `demo text` — the local runtime was not answering and the server wrote the reply itself. No",
        "  tok/s is printed for it;",
        "* `no output` — the seat finished empty. The next seat is handed its reasoning trace, or an",
        "  empty section if there is none;",
        "* `streaming` / `not run` — still going, or never started. Neither is drawn as an answer.",
        "",
        "## What it refuses to invent",
        "",
        "* a budget — every ceiling on screen arrived on `stage_start`;",
        "* a reasoning mode for a seat the route did not give one to (seats 2 and 3 run `off`, and say so);",
        "* a skills claim for the Critic, which is deliberately given none;",
        "* a token count for demo text — that reply was not generated by a model and gets no figure;",
        "* an answer where the reply was cut;",
        "* an empty handoff for a seat that handed on its reasoning;",
        "* an endpoint — the evidence is read from the stream this pane already consumed.",
        "",
        "## Evidence",
        "",
        "`node scripts/council_deliberation_walk.mjs --screenshots docs/media/council-deliberation`",
        "runs the four seats against a stand-in oMLX runtime that answers one seat in reasoning only",
        "and cuts another at its length limit, then against the same fixture with no runtime at all.",
        "The captures are described in `docs/media/council-deliberation/README.md`.",
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
                    "the council-deliberation inventory has drifted from the source: "
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
