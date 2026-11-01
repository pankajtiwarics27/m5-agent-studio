#!/usr/bin/env python3
"""Inventory what each pipeline stage was given and what it left behind (UIUX-P3-06).

    python3 scripts/uiux_pipeline_artifacts_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_pipeline_artifacts_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_pipeline_artifacts_inventory.py --json     # print the inventory

The four-seat pipeline is a chain, and the pane never said so. The architect writes a spec, the
builder is handed it, the critic is handed both, the judge is handed all three - but that chain
lives only in `api_council_stream`'s input lambdas, and the screen drew four cards carrying a
state and a tok/s. So a finished run could not answer the two questions a reader has about a
pipeline: what did each seat actually receive, and what did it hand on? A stage that produced
nothing looked exactly like a stage that produced something short, and a run that was stopped
looked like a run that was still going.

What is measured here:

* **the chain**, mirrored in `PIPELINE_SEATS` and compared against the server's own lambdas -
  because a mirror that is trusted instead of checked is a mirror that drifts;
* **the handoff row**, drawn on every stage card: what the seat reads, how many characters of
  upstream output it was handed, and which seat it feeds;
* **the artifact row**, drawn under the stage's output: the characters it produced, its tokens
  and tok/s, the reasoning it kept, and - when its output is empty - that it produced nothing;
* **the run line**, stated once before the cards: how many seats finished, how much output the
  run produced, how many handoffs carried content, and whether the server saved it at all;
* **the honesty of the sizes**, which count this pane's own stream and never pretend to know
  the prompt the server assembles around them;
* **the preservation**, the stream's seven events, the run record's keys and the routes it must
  leave alone.

It fails when the mirrored chain stops matching the server, when a stage card loses one of its
two rows, when an empty seat is drawn as a quiet one, when a stopped run is drawn as a finished
one, when an unsaved run is given a run id, when a value reaches the page unescaped, when a
second stage renderer appears, and when this story invents an endpoint the server does not mount.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Artifacts.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-pipeline-artifacts.md"

CALLED_PATH = re.compile("""[\"'`](/api/[^\"'`]*)[\"'`]""")

# The stream the council route speaks. Seven events; none of them may disappear, because the
# rows above are drawn from `stage_start`, `token` and `stage_done`, and the run line from
# `council_done`.
STREAM_EVENTS = ("skills_applied", "stage_start", "token", "unloading", "stage_done", "council_done", "error")

# The keys `save_run("council", …)` writes, and the reason the pane may draw a run as unsaved.
RUN_RECORD_KEYS = ("task", "total_elapsed_s", "stages")

# Routes this story must leave exactly as they were.
PRESERVED_ROUTES = ("api_council_stream", "api_debate_stream", "api_history_export", "save_run")

# Where the run line's own sentence lives, so the check reads the same words the page draws.
DOCUMENTED_SEATS = (
    (1, "architect", ()),
    (2, "coder", ("architect",)),
    (3, "critic", ("architect", "coder")),
    (4, "judge", ("architect", "coder", "critic")),
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


def css_rule(source: str, selector: str) -> str:
    """The declaration block of ``selector`` in the pane's own stylesheet."""
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", source)
    return match.group(1) if match else ""


def server_chain(server: str) -> dict[int, list[str]]:
    """Each stage's input lambda, read as the `c['…']` keys it reads out of the output map."""
    body = python_function(server, "api_council_stream")
    start = body.find("stages_cfg: list[WorkflowStage] = [")
    if start < 0:
        return {}
    block = body[start:]
    end = block.find("\n            ]")
    if end >= 0:
        block = block[:end]
    reads: dict[int, list[str]] = {}
    for line in block.splitlines():
        stage_match = re.search(r"WorkflowStage\((\d+),\s*\"([a-z]+)\",", line)
        if not stage_match:
            continue
        reads[int(stage_match.group(1))] = sorted(set(re.findall(r"c\['([^']+)'\]", line)))
    return reads


def server_keys(server: str) -> dict[int, str]:
    """Each stage's key, in the order the server declares them."""
    body = python_function(server, "api_council_stream")
    keys: dict[int, str] = {}
    for match in re.finditer(r"WorkflowStage\((\d+),\s*\"([a-z]+)\",", body):
        keys[int(match.group(1))] = match.group(2)
    return keys


def pane_chain(html: str) -> dict[int, list[str]]:
    """`PIPELINE_SEATS`, read as the same shape: stage number to the keys it declares it reads."""
    start = html.find("const PIPELINE_SEATS = [")
    if start < 0:
        return {}
    end = html.find("\n  ];", start)
    block = html[start:end if end >= 0 else start + 2000]
    chain: dict[int, list[str]] = {}
    for match in re.finditer(r"\{ stage: (\d+), key: \"([a-z]+)\",[^}]*?reads: \[([^\]]*)\] \}", block):
        reads = re.findall(r"\"([a-z]+)\"", match.group(3))
        chain[int(match.group(1))] = sorted(reads)
    return chain


def mounted_routes(server: str) -> set[str]:
    """Every route the server mounts, with each path parameter collapsed to `{}`."""
    found = set()
    for match in re.finditer(r'@app\.(?:get|post|put|patch|delete)\("(/[^"]*)"\)', server):
        found.add(re.sub(r"\{[^}]*\}", "{}", match.group(1)))
    return found


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal the pane builds, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def pipeline_section(html: str) -> str:
    """The P3-06 block: from its banner comment to the next story's banner.

    Bounded by the *next* story rather than by the card mount, because P3-07 inserted its own
    block in between; a section that swallowed its successor would report that successor's route
    and its renderer as this story's.
    """
    start = html.find("// ── UIUX-P3-06:")
    if start < 0:
        return ""
    end = html.find("/* ── UIUX-P3-07:", start)
    if end < 0:
        end = html.find("function ensureStageCardMounted(", start)
    return html[start:end] if end > start else html[start : start + 30000]


def _without_lookups(source: str) -> str:
    """``source`` with the template literals inside ``getElementById(`…`)`` blanked out.

    Those templates build an element id; nothing in them reaches the page.
    """
    out: list[str] = []
    index = 0
    needle = "getElementById(`"
    while True:
        start = source.find(needle, index)
        if start < 0:
            out.append(source[index:])
            return "".join(out)
        open_tick = start + len(needle) - 1
        cursor = open_tick + 1
        while cursor < len(source):
            if source[cursor] == "`":
                break
            cursor += 1
        out.append(source[index:open_tick])
        index = cursor + 1


def _expressions(source: str) -> list[str]:
    """Every `${…}` in ``source``, with its braces balanced so nesting does not cut it short."""
    found: list[str] = []
    index = 0
    while True:
        start = source.find("${", index)
        if start < 0:
            return found
        depth = 0
        cursor = start + 1
        while cursor < len(source):
            char = source[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        found.append(source[start + 2 : cursor])
        index = cursor


def unescaped_interpolations(source: str) -> list[str]:
    """`${…}` expressions that reach markup without `escapeHtml(` or a numeric coercion.

    A nested template literal is not judged as a whole: its own `${…}` segments are what
    reach the page, so the scan recurses into it.
    """
    offenders: list[str] = []
    for expression in _expressions(_without_lookups(source)):
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


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    orchestration_body = ORCHESTRATION.read_text(encoding="utf-8")

    chain_pane = pane_chain(html)
    chain_server = server_chain(server)
    keys_server = server_keys(server)

    handoff = function_body(html, "renderStageHandoff")
    reduction = function_body(html, "pipelineHandoff")
    artifact = function_body(html, "renderStageArtifact")
    summary = function_body(html, "renderCouncilArtifacts")
    rows = function_body(html, "updateStageRows")
    mount = function_body(html, "ensureStageCardMounted")
    schedule = function_body(html, "scheduleStageUpdate")
    council = function_body(html, "runCouncil")
    run_start = function_body(html, "startPipelineRun")
    reset_source = council + run_start
    # The run's lifecycle - the reset, the stream handlers and the finalizer - moved out of
    # `runCouncil` when P3-07 needed to start a run from a recorded payload. The rules below are
    # about what the pane does, not about which function holds the line, so they read all of it.
    lifecycle = (
        council
        + run_start
        + function_body(html, "pipelineStreamHandlers")
        + function_body(html, "pipelineStreamFinalizer")
    )
    section = pipeline_section(html)

    artifacts_host = element(html, "councilArtifacts")
    council_body = python_function(server, "api_council_stream")

    checks: list[dict] = []

    def check(name: str, ok: bool, detail_text: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail_text})

    # ── the chain ─────────────────────────────────────────────────────────────
    check(
        "the pipeline chain is declared once, as four seats",
        html.count("const PIPELINE_SEATS = [") == 1
        and sorted(chain_pane) == [1, 2, 3, 4]
        and [keys_server.get(stage) for stage in (1, 2, 3, 4)] == ["architect", "coder", "critic", "judge"],
        f"pane={chain_pane} server keys={keys_server}",
    )
    check(
        "the pane's chain is the server's chain",
        chain_pane == chain_server
        and {stage: tuple(chain_pane.get(stage, ())) for stage in chain_pane} == {stage: reads for stage, _, reads in DOCUMENTED_SEATS},
        f"pane={chain_pane} server={chain_server}",
    )
    check(
        "the chain the pane mirrors is read from the lambdas, not asserted about them",
        all(f"c['{key}']" in council_body for key in ("architect", "coder", "critic"))
        and "lambda _c: task" in council_body,
        "the first seat's input is the task alone; the others read the output map",
    )

    # ── the handoff row ───────────────────────────────────────────────────────
    check(
        "every stage card carries a handoff row and an artifact row",
        "stage-handoff-${s}" in mount and "stage-artifact-${s}" in mount and bool(handoff) and bool(artifact),
        "a card without either row is a card that hides its own links",
    )
    check(
        "the handoff row names what the seat reads and what it feeds",
        "reads" in handoff and "feeds" in handoff and "pipelineUpstreamList(seat)" in handoff and "handoff.feeds.short" in handoff,
        handoff[:200],
    )
    check(
        "the handoff row measures the upstream it was handed, not the task",
        "pipelineChars(" in html
        and "buffer.c.length" in function_body(html, "pipelineChars")
        and "upstream.reduce((total, entry) => total + entry.chars, 0)" in reduction,
        "a handoff size that counts the task says nothing about the handoff",
    )
    check(
        "the tail seat says it ends the run instead of pointing nowhere",
        "ends the run" in handoff,
        "the judge feeds nobody, and an empty arrow would read as an unstarted stage",
    )
    check(
        "the rows are redrawn on the stream's frame, guarded against churn",
        "updateStageRows(k);" in schedule
        and "stageRowSignature[stage] === signature" in rows
        and "renderStageHandoff(stage)" in rows
        and "renderStageArtifact(stage)" in rows,
        "one innerHTML rewrite per token is a pane that stops streaming smoothly",
    )

    # ── the artifact row ──────────────────────────────────────────────────────
    check(
        "the artifact row states the output it produced, in the pane's own units",
        "stage-artifact-key" in artifact
        and "chars" in artifact
        and "completion_tokens" in artifact
        and "decode_tps" in artifact,
        artifact[:200],
    )
    check(
        "an empty seat is drawn as empty, not as a quiet one",
        'host.dataset.empty = empty ? "true" : "false"' in artifact
        and "produced nothing" in artifact
        and 'data-empty="true"' in html,
        "a stage that returned nothing used to look like a stage that had just started",
    )
    check(
        "the reasoning a seat kept is stated with its size",
        "reasoning" in artifact and "reasoning_tokens" in artifact,
        artifact[:200],
    )
    check(
        "the collapsed card states the artifact size too",
        all(f'id="st-chars-{stage}"' in html for stage in (1, 2, 3, 4))
        and "st-chars-${stage}" in rows
        and bool(css_rule(html, ".step-chars")),
        "the stepper is the only stage view when a filter is on",
    )

    # ── the run line ──────────────────────────────────────────────────────────
    check(
        "the run is stated once, above the cards, as a live region",
        "role=\"status\"" in artifacts_host and "aria-live=\"polite\"" in artifacts_host and "hidden" in artifacts_host,
        artifacts_host,
    )
    check(
        "the run line says how many seats finished, how much was produced and how many handoffs landed",
        "finished.length" in summary and "PIPELINE_SEATS.length" in summary and "totalChars" in summary and "handoffs" in summary,
        summary[:220],
    )
    # P3-07 gave the endings three words of their own (`Failed` / `Ended` / `Stopped`) and moved
    # the decision into a single `endedWord`; a stop must still be one of them and must still be
    # drawn as `partial`, never as a finished run.
    ended_word = re.search(r'const endedWord = [^;]+;', summary)
    check(
        "a stopped run is stated as stopped",
        "councilRun.stopped = !allDone;" in lifecycle
        and bool(ended_word)
        and '"Stopped"' in (ended_word.group(0) if ended_word else "")
        and "host.dataset.state = state" in summary
        and '"partial"' in summary,
        "a stop that reads as a finish is the lie this story exists to remove",
    )
    check(
        "a stopped run says it was not saved, and a saved one is named by the id the server gave",
        "A stopped pipeline is not saved" in summary
        and "councilRun.savedRunId = Number(d.run_id || 0)" in lifecycle
        and "councilRun.savedRunId > 0" in summary
        and "save_run(\"council\"" in council_body,
        "save_run returns 0 when it cannot write; a zero drawn as a run id is an invented record",
    )
    check(
        "an error is stated, and it is not turned into a stop",
        "councilRun.error = d.message" in lifecycle
        and "councilRun.error" in summary
        and "councilRun.error ? \"failed\"" in summary,
        "an error is not the operator's stop, and the surface must not call it one",
    )
    check(
        "the sizes are the pane's own stream, said so on the page",
        "not the prompt the server assembles" in summary and "escapeHtml" in summary,
        "the pane can count what it streamed; it cannot see the template around it",
    )

    # ── the renderer is one ───────────────────────────────────────────────────
    check(
        "there is one renderer for a stage's markdown",
        html.count("stage-md-${k}`).innerHTML") == 1 and html.count("stage-md-${s}`).innerHTML") == 0,
        "a second renderer is a second truth about the same stage",
    )
    check(
        "every value the rows draw is escaped",
        not unescaped_interpolations(handoff + artifact + summary),
        ", ".join(unescaped_interpolations(handoff + artifact + summary)),
    )

    # ── a fresh run starts clean ──────────────────────────────────────────────
    check(
        "a new run clears the rows instead of inheriting them",
        "stageRowSignature = {};" in reset_source
        and "councilRun = {" in reset_source
        and "taskChars: task.length" in reset_source
        and 'document.getElementById("councilStagesContainer").innerHTML = "";' in reset_source
        and 'renderCouncilArtifacts();' in reset_source,
        "a row left over from the last run would describe a run that is not on screen",
    )
    check(
        "no run means no run line",
        "if (!councilRun) {" in summary and "host.hidden = true" in summary,
        summary[:160],
    )

    # ── nothing invented ──────────────────────────────────────────────────────
    section_routes = called_routes(section)
    check(
        "the story adds no endpoint",
        section_routes == [] and "/api/runs" not in mounted_routes(server),
        f"calls={section_routes}",
    )
    # `stage_start`, `token` and `unloading` reach the browser through the orchestrator's own
    # names, so the route forwards them by kind rather than naming them at the `_sse` call.
    forwarded = ("stage_start", "token", "unloading")
    direct = tuple(event for event in STREAM_EVENTS if event not in forwarded)
    passthrough = next(
        (line for line in council_body.splitlines() if "event.kind in" in line),
        "",
    )
    check(
        "the council route speaks the same seven events",
        all(f'_sse("{event}"' in council_body for event in direct)
        and all(f'"{event}"' in passthrough for event in forwarded),
        ", ".join(
            [event for event in direct if f'_sse("{event}"' not in council_body]
            + [event for event in forwarded if f'"{event}"' not in passthrough]
        ),
    )
    # The record `save_run("council", …)` writes, read as its own dict literal: `council_done`
    # also carries a `total_elapsed_s`, so searching the whole route would pass on that alone.
    record_match = re.search(r'save_run\("council", task\[:100\], \{(.*?)\}\)', council_body, re.S)
    record_keys = set(re.findall(r'"([a-z_]+)":', record_match.group(1))) if record_match else set()
    check(
        "the run record still carries what it carried",
        record_keys == set(RUN_RECORD_KEYS),
        f"record keys={sorted(record_keys)}",
    )
    stage_lines = {
        int(match.group(1)): line
        for line in council_body.splitlines()
        for match in [re.search(r"WorkflowStage\((\d+),", line)]
        if match
    }
    # UIUX-P4-02 widened `stage_start` with the facts this story could not carry: a seat's
    # budget, the reasoning mode it was given and whether it was handed the skill playbook. The
    # rule is unchanged in spirit - a per-stage claim must be the server's, not a mirror - so it
    # now reads the widened event instead of forbidding the claim outright.
    contract_from_stream = all(f'"{field}"' in orchestration_body for field in ("max_tokens", "reasoning_mode")) and "stage.meta" in orchestration_body
    skills_from_stream = "stageBuffers[d.stage].k" in html and "d.skills" in html
    check(
        "the per-stage inputs are claimed only from the stream that now carries them",
        "skill_block" not in stage_lines.get(3, "missing")
        and "skill_block" in stage_lines.get(1, "")
        and "skill_block" in stage_lines.get(4, "")
        and contract_from_stream
        and skills_from_stream,
        "the critic is the one seat without skills, the route says so on `stage_start`, and the "
        "pane must read that event rather than mirror the route's configuration",
    )
    check(
        "the routes this story must leave alone are still mounted",
        all(re.search(rf"^(?:async )?def {name}\(", server, re.M) for name in PRESERVED_ROUTES),
        ", ".join(name for name in PRESERVED_ROUTES if not re.search(rf"^(?:async )?def {name}\(", server, re.M)),
    )
    check(
        "the pane draws the chain from the stream and posts only the council route",
        section_routes == [] and "/api/council/stream" in called_routes(run_start),
        f"council calls={called_routes(council)}",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": "UIUX-P3-06",
        "title": "Pipeline stages: dependencies and artifacts",
        "generated_by": "scripts/uiux_pipeline_artifacts_inventory.py",
        "chain": {
            "declared_in_pane": chain_pane,
            "declared_in_server": chain_server,
            "keys": keys_server,
        },
        "rows": {
            "handoff_fields": ["reads", "upstream chars", "feeds"],
            "artifact_fields": ["chars", "tokens", "tok/s", "reasoning", "empty"],
            "run_fields": ["seats finished", "total chars", "handoffs", "saved run id or not saved"],
        },
        "stream_events": list(STREAM_EVENTS),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    chain = payload["chain"]
    lines = [
        "# UIUX-P3-06 — Pipeline stages: dependencies and artifacts",
        "",
        "Generated by `scripts/uiux_pipeline_artifacts_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the pane now says",
        "",
        "| Where | What it says |",
        "| :--- | :--- |",
        "| stage card, under the header | what the seat reads, how many characters of upstream output it was handed, and the seat it feeds |",
        "| stage card, under the output | the characters, tokens, tok/s and reasoning the seat produced — or that it produced nothing |",
        "| collapsed step card | the character count of that seat's artifact |",
        "| above the cards | how many seats finished, total characters, handoffs that carried content, and whether the server saved the run |",
        "",
        "## The chain, as both sides declare it",
        "",
        "| Stage | Key | Reads from the output map |",
        "| :--- | :--- | :--- |",
    ]
    for stage in (1, 2, 3, 4):
        reads = chain["declared_in_server"].get(stage, [])
        lines.append(
            f"| {stage} | `{chain['keys'].get(stage, '?')}` | {', '.join(f'`{key}`' for key in reads) if reads else 'the task only'} |"
        )
    lines += [
        "",
        "Those reads are what `api_council_stream`'s input lambdas actually interpolate; the pane's",
        "`PIPELINE_SEATS` is checked against them on every run of this inventory, so the mirror cannot",
        "drift quietly.",
        "",
        "## What it refuses to claim",
        "",
        "* the size of the *prompt* — the pane can only count what it streamed, and the page says so;",
        "* a run id for a run the server did not save — `save_run()` returns `0` when it cannot write,",
        "  and a stopped pipeline is never saved, because the record is written after the last stage;",
        "* a skills or reasoning claim per stage — those are the server's inputs and the stream does",
        "  not carry them;",
        "* a checkpoint, a retry or a resume in this pane: those belong to UIUX-P3-07.",
        "",
        "## The stream it reads",
        "",
        "`" + "`, `".join(payload["stream_events"]) + "`",
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
                    "the pipeline-artefact inventory has drifted from the source: "
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
