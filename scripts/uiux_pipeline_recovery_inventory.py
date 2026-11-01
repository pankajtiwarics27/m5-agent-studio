#!/usr/bin/env python3
"""Inventory how a failed pipeline is stated, retried and recovered (UIUX-P3-07).

    python3 scripts/uiux_pipeline_recovery_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_pipeline_recovery_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_pipeline_recovery_inventory.py --json     # print the inventory

A run that failed used to be a toast: `Council error: HTTP 503` for four seconds, then nothing. The
seat it died on was not named, the output that had survived was not named, and there was no record
of it anywhere - `save_run()` writes a run only after the fourth seat returns, so a failure leaves
nothing behind, and reloading the page threw away the only copy of what the first seats produced.
The only way to try again was to retype the task, which may not have been the task that failed.

What is measured here:

* **the presentation**, one block built by `presentInto()` - so it cannot ship without a title, a
  cause, a `Kept:` line and at least one action - over six states: failed, stopped, dropped,
  recovered-from-checkpoint, recovered-from-the-server, and "on record";
* **the three copies**, kept apart on purpose: the server's run record (read back through the
  export the repository already mounts), this browser's checkpoint (`localStorage`, bounded, and
  never described as the server's), and nothing at all;
* **the retry**, which posts the payload that failed rather than re-reading the controls, and says
  that the server has no way to run one seat alone, so all four run again;
* **the salvage**, written before the page can go away, capped, and said to be a browser copy;
* **the honesty**, the strings that stop a recovered view passing for a live run and a failed one
  for a stopped one;
* **the preservation**, the stream's seven events, the run record's keys, and the routes it must
  leave alone - including that no route was added to read a run back.

It fails when a second recovery renderer appears, when a presentation branch can ship without a
cause or an action, when a retry re-reads the form it was supposed to ignore, when a browser copy
is described as the server's record, when a recorded run is invented for a run that was not
saved, when the pane calls a route the server does not mount, or when the stream and record shapes
change.
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
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Recovery.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-pipeline-recovery.md"

CALLED_PATH = re.compile("""[\"'`](/api/[^\"'`]*)[\"'`]""")

STREAM_EVENTS = ("skills_applied", "stage_start", "token", "unloading", "stage_done", "council_done", "error")
RUN_RECORD_KEYS = ("task", "total_elapsed_s", "stages")
PRESERVED_ROUTES = ("api_council_stream", "api_debate_stream", "api_history_export", "save_run")

# The states the block draws, and the string that proves each branch exists.
STATES = (
    ("failed", "The run failed at"),
    ("stopped", "Stopped at"),
    ("dropped", "The stream ended without a result"),
    ("recovered", "Showing a run this browser kept"),
    ("recorded", "This run is on record as"),
    ("checkpoint", "A run of this task was kept in this browser"),
)

FUNCTIONS = (
    "pipelinePayload",
    "pipelineStagesSnapshot",
    "pipelineCheckpointBuild",
    "pipelineCheckpointWrite",
    "pipelineCheckpointRead",
    "pipelineCheckpointClear",
    "pipelineEndState",
    "applyRecoveredStages",
    "restorePipelineCheckpoint",
    "discardPipelineCheckpoint",
    "reloadSavedPipelineRun",
    "pipelineRecoverySpec",
    "renderPipelineRecovery",
    "clearPipelineView",
    "retryPipelineRun",
    "startPipelineRun",
    "pipelineStreamHandlers",
    "pipelineStreamFinalizer",
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


def recovery_section(html: str) -> str:
    """The P3-07 block: from its banner comment to the stage-card mount."""
    start = html.find("/* ── UIUX-P3-07:")
    if start < 0:
        return ""
    end = html.find("function ensureStageCardMounted(", start)
    return html[start:end] if end > start else html[start : start + 40000]


def returned_specs(body: str) -> list[str]:
    """Every ``return { … };`` block in a function, balanced by braces."""
    specs: list[str] = []
    for match in re.finditer(r"return \{", body):
        start = body.find("{", match.start())
        depth = 0
        for offset in range(start, len(body)):
            char = body[offset]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    specs.append(body[start : offset + 1])
                    break
    return specs


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


def _without_lookups(source: str) -> str:
    """``source`` with the template literals inside ``getElementById(`…`)`` blanked out."""
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


def unescaped_interpolations(source: str) -> list[str]:
    """`${…}` expressions that reach markup without `escapeHtml(` or a numeric coercion."""
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

    section = recovery_section(html)
    spec_source = function_body(html, "pipelineRecoverySpec")
    render = function_body(html, "renderPipelineRecovery")
    retry = function_body(html, "retryPipelineRun")
    start_run = function_body(html, "startPipelineRun")
    run_council = function_body(html, "runCouncil")
    handlers = function_body(html, "pipelineStreamHandlers")
    finalizer = function_body(html, "pipelineStreamFinalizer")
    checkpoint_build = function_body(html, "pipelineCheckpointBuild")
    reload_saved = function_body(html, "reloadSavedPipelineRun")
    restore = function_body(html, "restorePipelineCheckpoint")
    discard = function_body(html, "discardPipelineCheckpoint")
    apply_recovered = function_body(html, "applyRecoveredStages")
    summary = function_body(html, "renderCouncilArtifacts")

    host = element(html, "pipelineRecovery")
    council_body = python_function(server, "api_council_stream")
    mounted = mounted_routes(server)
    section_routes = called_routes(section)

    checks: list[dict] = []

    def check(name: str, ok: bool, detail_text: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail_text})

    # ── the surface ───────────────────────────────────────────────────────────
    check(
        "one recovery surface, between the run line and the seats",
        html.count('id="pipelineRecovery"') == 1
        and html.find('id="councilArtifacts"') < html.find('id="pipelineRecovery"') < html.find('id="councilStagesContainer"'),
        "the block must sit where a reader meets it after the run line and before the cards",
    )
    check(
        "the surface is a labelled group that starts hidden",
        'role="group"' in host and 'aria-label="Pipeline recovery"' in host and "hidden" in host,
        host,
    )
    check(
        "every function the surface needs exists once",
        all(html.count(f"function {name}(") == 1 for name in FUNCTIONS),
        ", ".join(name for name in FUNCTIONS if html.count(f"function {name}(") != 1),
    )
    check(
        "the block is built by the one presentation builder, not hand-written markup",
        "presentInto(host," in render
        and section.count("presentation-title") == 0
        and section.count("presentation-cause") == 0
        and section.count("createElement(\"button\")") == 0,
        "a second builder is a second truth about what a failure looks like",
    )
    check(
        "the block is drawn on boot, so a kept run survives a reload",
        "renderPipelineRecovery();" in html[html.find("presentBoot") : ] if "presentBoot" in html else html.count("  renderPipelineRecovery();") >= 2,
        "a checkpoint nobody renders is not a recovery",
    )

    # ── six states, each of them complete ─────────────────────────────────────
    specs = returned_specs(spec_source)
    complete = [
        block for block in specs
        if "kind:" in block and "title:" in block and "cause:" in block and "context:" in block and "actions: [" in block
    ]
    check(
        "every state the block can draw carries a title, a cause, a Kept: line and an action",
        len(specs) >= 4 and len(complete) == len(specs),
        f"{len(complete)} of {len(specs)} returned specs are complete",
    )
    missing_states = [name for name, needle in STATES if needle not in spec_source]
    check(
        "all six states are drawn from the same builder",
        not missing_states,
        ", ".join(missing_states),
    )
    check(
        "a state with nothing to say draws nothing",
        "return null;" in spec_source and "spec === null" in render or "if (!spec) {" in render,
        render[:200],
    )

    # ── the three copies, kept apart ─────────────────────────────────────────
    check(
        "the browser's copy is named, capped and never called the server's",
        'const PIPELINE_CHECKPOINT_KEY = "m5_pipeline_checkpoint"' in html
        and "PIPELINE_CHECKPOINT_MAX_CHARS" in checkpoint_build
        and "This is not the server's record: clearing this browser's site data loses it." in spec_source
        and "localStorage.setItem(PIPELINE_CHECKPOINT_KEY" in html,
        "a browser copy described as a record is a record that does not exist",
    )
    check(
        "a seat that had to be cut to fit the store says so",
        "truncation: { truncated, cap_chars: PIPELINE_CHECKPOINT_MAX_CHARS }" in checkpoint_build
        and "cut to fit" in spec_source,
        checkpoint_build[-200:],
    )
    check(
        "the salvage is written before the page can go away, and cleared when the run is on record",
        "pipelineCheckpointWrite(councilRun.endReason)" in finalizer
        and "pipelineCheckpointClear();" in finalizer
        and "if (councilRun.savedRunId) pipelineCheckpointClear();" in finalizer,
        finalizer[-400:],
    )
    check(
        "the server's record is read through the export the repository already mounts",
        "/api/history/export" in reload_saved
        and 'row.mode && row.mode !== "council"' in reload_saved
        and "payload_json" in reload_saved,
        reload_saved[:300],
    )
    check(
        "the pane says the export redacts credentials",
        "credentials redacted" in spec_source,
        "an export drawn as raw would be a claim about what it contains",
    )
    check(
        "a run that was not saved is never given a record",
        "councilRun.savedRunId = Number(d.run_id || 0);" in handlers
        and "councilRun.savedRunId > 0 && !councilRun.stopped" in spec_source
        and "no record to reload" in html,
        "a fabricated id is the one thing a recovery surface must never produce",
    )

    # ── retry posts what failed ──────────────────────────────────────────────
    check(
        "Retry posts the payload that failed instead of re-reading the controls",
        "pipelinePayload(task)" in run_council
        and "JSON.parse(JSON.stringify(from))" in retry
        and "consumeSSE(\"/api/council/stream\", payload, pipelineStreamHandlers()" in start_run
        and html.count("pipelinePayload(") == 2,
        f"runCouncil={run_council[:120]!r} retry={retry[:160]!r}",
    )
    check(
        "Retry says the server has no single-seat entry point",
        "all four seats run again" in retry
        and "all four seats re-run" in spec_source
        and "retry re-runs all four seats" in summary,
        retry[-260:],
    )
    check(
        "the run line says which attempt it is drawing",
        "Attempt " in summary and "attempt > 1" in summary,
        summary[:160],
    )
    check(
        "a recovered view says nothing was re-run, and that restoring re-runs nothing",
        "nothing was re-run" in summary and "does not re-run anything" in spec_source,
        "a restored copy drawn as a run is the same lie as a stop drawn as a finish",
    )
    check(
        "a restored card is marked, and a partial seat is not drawn as if it finished",
        'card.dataset.recovered = "true"' in apply_recovered
        and "Recovered · partial" in apply_recovered
        and 'Stopped mid-seat' in finalizer,
        apply_recovered[-300:],
    )
    check(
        "restoring re-runs nothing",
        "consumeSSE(" not in restore and "fetch(" not in restore,
        "a restore that posts is a retry wearing a different label",
    )

    # ── what happened, in the runtime's words ────────────────────────────────
    check(
        "a failure, a stop and a dropped stream are three different states",
        'councilRun.endReason = "failed";' in handlers
        and 'councilRun.endReason = "stopped";' in handlers
        and 'councilRun.endReason = "dropped";' in handlers,
        "three endings drawn as one is how a failure reads as a stop",
    )
    check(
        "the failed seat is the seat that was streaming",
        "councilRun.failedStage = councilRun.activeStage;" in handlers
        and "councilRun.activeStage = Number(d.stage);" in handlers,
        handlers[:300],
    )
    check(
        "the run line calls a failure a failure",
        'endsWord = councilRun.endReason === "failed" ? "Failed"' in summary.replace("endedWord", "endsWord")
        or 'councilRun.endReason === "failed" ? "Failed"' in summary,
        summary[:200],
    )
    check(
        "a failure is not drawn as a stop, or a stop as a failure",
        "A failed run is not saved" in summary and "A stopped pipeline is not saved" in summary,
        "the two sentences must both exist and must not be the same one",
    )
    check(
        "the cause is the server's message, not a summary of it",
        "councilRun.error = d.message;" in handlers and "failure.message" in spec_source,
        handlers[:300],
    )

    # ── escaping and focus ───────────────────────────────────────────────────
    markup_templates = "\n".join(
        match.group(0)
        for match in re.finditer(r"host\.innerHTML = `[^`]*`", section)
    )
    check(
        "nothing in the block writes unescaped markup",
        not unescaped_interpolations(markup_templates)
        and section.count("innerHTML = `") <= 1,
        ", ".join(unescaped_interpolations(markup_templates)) or "a template is written into innerHTML",
    )
    check(
        "the actions are reachable by id, so a keyboard user and a test find the same button",
        'button.dataset.actionId = action.id;' in render
        and all(f'id: "{name}"' in spec_source for name in ("retry", "forget", "restore", "reload-saved")),
        render[-260:],
    )
    check(
        "removing a block hands focus somewhere rather than dropping it",
        "host.focus();" in discard and "host.focus();" in function_body(html, "clearPipelineView"),
        discard[-200:],
    )

    # ── nothing invented, nothing broken ─────────────────────────────────────
    mountable = {route for route in mounted}
    check(
        "the story adds no endpoint: every route it calls was already mounted",
        all(any(route == mount or route.startswith(mount.rstrip("/") + "/") for mount in mountable) for route in section_routes)
        and "/api/runs" not in mounted
        and section_routes == ["/api/history/export"],
        f"calls={section_routes}",
    )
    check(
        "the council route speaks the same seven events",
        all(f'_sse("{event}"' in council_body for event in ("skills_applied", "stage_done", "council_done", "error"))
        and all(f'"{event}"' in council_body for event in ("stage_start", "token", "unloading")),
        "an event the pane draws from must still be sent",
    )
    check(
        "the run record still carries what it carried",
        all(f'"{key}":' in council_body for key in RUN_RECORD_KEYS),
        ", ".join(key for key in RUN_RECORD_KEYS if f'"{key}":' not in council_body),
    )
    check(
        "the routes this story must leave alone are still mounted",
        all(re.search(rf"^(?:async )?def {name}\(", server, re.M) for name in PRESERVED_ROUTES),
        ", ".join(name for name in PRESERVED_ROUTES if not re.search(rf"^(?:async )?def {name}\(", server, re.M)),
    )
    check(
        "the shared stream reader still distinguishes a stop from a drop",
        "handlers.stopped" in function_body(html, "consumeSSE") and "handlers.closed" in function_body(html, "consumeSSE"),
        "one reader for four panes; this story may not change what it means",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": "UIUX-P3-07",
        "title": "Pipeline retry, failure and checkpoint recovery",
        "generated_by": "scripts/uiux_pipeline_recovery_inventory.py",
        "states": [name for name, _ in STATES],
        "copies": {
            "server_record": "GET /api/history/export -> tables.runs[].payload_json (redacted)",
            "browser_checkpoint": "localStorage m5_pipeline_checkpoint, capped at 60000 characters",
            "nothing": "a failed or stopped run before anything is kept",
        },
        "stream_events": list(STREAM_EVENTS),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P3-07 — Pipeline retry, failure and checkpoint recovery",
        "",
        "Generated by `scripts/uiux_pipeline_recovery_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the pane now does",
        "",
        "| Where | What it does |",
        "| :--- | :--- |",
        "| a failed seat | names the seat, quotes the provider's own message, and states the output that survived |",
        "| a stopped run | says it was stopped, where, and what it kept |",
        "| a stream that ends without a result | says the connection dropped or the server stopped, and does not call it a stop |",
        "| Retry | re-posts the payload that failed - not the edited form - and says all four seats run again |",
        "| a reload | offers the copy this browser kept, without drawing it until asked |",
        "| Restore | draws the kept seats, marks them `recovered`, and re-runs nothing |",
        "| a saved run | says it is on record, with the id the server returned, and reads it back from the export |",
        "",
        "## The three copies, kept apart",
        "",
        f"* **the server's record** — {payload['copies']['server_record']}",
        f"* **this browser's checkpoint** — {payload['copies']['browser_checkpoint']}",
        f"* **nothing** — {payload['copies']['nothing']}",
        "",
        "## The states it draws",
        "",
        "`" + "`, `".join(payload["states"]) + "`",
        "",
        "## What it refuses to do",
        "",
        "* claim a run id for a run `save_run()` never wrote — a failed or stopped run says it is not on",
        "  record, and a copy is never described as the server's;",
        "* resume a pipeline at the seat that failed — the server runs four seats or none, so Retry says",
        "  it re-runs all four, and no copy is drawn as a live run;",
        "* add an endpoint to read runs back — the pane uses `GET /api/history/export`, which already",
        "  existed (and which redacts credentials);",
        "* prove the dropped-stream branch in a browser — a loopback stream cannot be dropped",
        "  deterministically, so the walk drives that state through the same renderer by hand and says",
        "  so; the wiring itself is checked here, in source.",
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
                    "the pipeline-recovery inventory has drifted from the source: "
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
