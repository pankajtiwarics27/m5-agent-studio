#!/usr/bin/env python3
"""Inventory and drift-check the selected-stage Pipeline workspace (UIV2-25).

    python3 scripts/uiux_pipeline_workspace_inventory.py          # write JSON + documentation
    python3 scripts/uiux_pipeline_workspace_inventory.py --check  # fail on drift or any finding
    python3 scripts/uiux_pipeline_workspace_inventory.py --json   # print the inventory

The rules below protect the existing four-stage server contract while requiring a useful
selected-stage reading surface: task first, factual states, bounded observed logs, disclosed
setup/evidence, stable output focus/scroll, visible failure/retry/checkpoint recovery, and a
real six-state browser capture matrix. Every rule is mutation-tested in tests/test_pipeline_workspace.py.
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
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "pipeline_workspace_walk.mjs"
SEED = PROJECT_ROOT / "scripts" / "pipeline_recovery_seed.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Pipeline_Workspace.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-5-pipeline-workspace.md"

STATES = ["empty", "loading", "populated", "long-content", "error", "recovery"]
VIEWPORTS = ["1640x900", "1440x900", "1280x800", "390x844"]
ORIGINAL_CONTROLS = [
    "councilPrompt", "councilRunBtn", "councilStopBtn", "c-arch", "c-coder", "c-critic",
    "c-judge", "c-unload", "c-reasoning", "step-card-0",
]


def _line(source: str, needle: str) -> int | None:
    index = source.find(needle)
    return None if index < 0 else source.count("\n", 0, index) + 1


def _slice(source: str, start: str, end: str) -> str:
    left = source.find(start)
    if left < 0:
        return ""
    right = source.find(end, left + len(start))
    return source[left:right] if right > left else source[left:]


def _sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def build_inventory(
    html: str | None = None,
    app: str | None = None,
    walk: str | None = None,
    seed: str | None = None,
) -> dict:
    html = FRONTEND.read_text(encoding="utf-8") if html is None else html
    app = APP.read_text(encoding="utf-8") if app is None else app
    walk = WALK.read_text(encoding="utf-8") if walk is None else walk
    seed = SEED.read_text(encoding="utf-8") if seed is None else seed

    pane = _slice(html, '<section id="pane-council"', '<!-- ================= TAB 3: DEBATE')
    workspace_markup = _slice(pane, '<section id="pipelineWorkspace"', '<details id="pipelineEvidenceDetails"')
    setup = _slice(pane, '<details id="pipelineSetup"', '</details>')
    evidence = _slice(pane, '<details id="pipelineEvidenceDetails"', '</details>')
    keydown = _slice(html, "function pipelineStageKeydown", "function scheduleStageUpdate")
    select = _slice(html, "function selectStageFilter", "function pipelineStageKeydown")
    model = _slice(html, "function pipelineWorkspaceModel", "function renderPipelineWorkspace")
    render_workspace = _slice(html, "function renderPipelineWorkspace", "function updatePipelineSetupSummary")
    record_event = _slice(html, "function recordPipelineEvent", "function renderPipelineActivity")
    activity = _slice(html, "function renderPipelineActivity", "function pipelineWorkspaceModel")
    schedule = _slice(html, "function scheduleStageUpdate", "function runCouncil")
    payload = _slice(html, "function pipelinePayload", "function pipelineStagesSnapshot")
    start_run = _slice(html, "function startPipelineRun", "function pipelineStreamHandlers")
    handlers = _slice(html, "function pipelineStreamHandlers", "function pipelineStreamFinalizer")
    finalizer = _slice(html, "function pipelineStreamFinalizer", "function syncDebateArbiter")
    recovery = _slice(html, "function renderPipelineRecovery", "function retryPipelineRun")
    restore = _slice(html, "function restorePipelineCheckpoint", "function discardPipelineCheckpoint")
    apply_recovered = _slice(html, "function applyRecoveredStages", "function restorePipelineCheckpoint")
    render_artifacts = _slice(html, "function renderCouncilArtifacts", "window.M5Deliberation")
    render_evidence = _slice(html, "function renderDeliberationEvidence", "function renderStageArtifact")
    inspector = _slice(html, 'if (v === "stage")', 'if (v === "artifacts")')
    api_route = _slice(app, '@app.post("/api/council/stream")', '@app.post("/api/debate/stream")')

    checks: list[dict] = []

    def add(check: str, rule: str, ok: bool, finding: str, evidence_text: str) -> None:
        checks.append({
            "check": check,
            "rule": rule,
            "passed": bool(ok),
            "finding": "" if ok else finding,
            "evidence": evidence_text,
        })

    add("pane-anchor", "Pipeline remains #pane-council, opened by the existing council tab",
        'id="pane-council"' in html and '<h2 class="pane-title">Pipeline</h2>' in pane and 'data-tab="council"' in html,
        "The Pipeline anchor or its existing tab registration was removed", 'id="pane-council"')

    stage_ids = re.findall(r'id="step-card-([0-9]+)"', pane)
    add("four-stage-timeline", "All Stages plus exactly the server's four stage buttons survive",
        stage_ids == ["0", "1", "2", "3", "4"] and "step-card-5" not in pane,
        f"The timeline is not All + stages 1..4: {stage_ids}", ", ".join(stage_ids))

    missing_controls = [control for control in ORIGINAL_CONTROLS if f'id="{control}"' not in pane]
    add("original-controls", "The original task, run, stop, model and reasoning controls are preserved",
        not missing_controls, f"Missing original controls: {missing_controls}", ", ".join(ORIGINAL_CONTROLS))

    positions = [pane.find(needle) for needle in ('id="councilPrompt"', 'id="pipelineSetup"', 'Run timeline', 'id="pipelineWorkspace"')]
    add("task-first", "Task content leads setup, diagnostics and the selected-stage workspace",
        all(position >= 0 for position in positions) and positions == sorted(positions),
        "Setup or diagnostics moved ahead of the task/workspace reading order", "prompt → setup → timeline → workspace")

    add("setup-disclosure", "Stage setup is a closed native disclosure whose summary states its contents",
        bool(re.search(r'<details id="pipelineSetup"(?![^>]*\bopen\b)', pane))
        and 'id="pipelineSetupState"' in setup and all(f'id="{item}"' in setup for item in ("c-arch", "c-coder", "c-critic", "c-judge", "c-unload", "c-reasoning"))
        and "function updatePipelineSetupSummary" in html,
        "Setup is permanently expanded, lost its state summary, or lost an original control", "#pipelineSetup / #pipelineSetupState")

    add("stage-keyboard", "One delegated strip handler navigates Arrow/Home/End without scrolling; Enter remains native",
        'id="pipelineStageStrip"' in pane and "onkeydown=" not in pane
        and 'getElementById("pipelineStageStrip").addEventListener("keydown"' in keydown
        and 'event.target.closest(".step-card")' in keydown
        and all(key in keydown for key in ('"ArrowLeft"', '"ArrowRight"', '"Home"', '"End"', "preventDefault()", "preventScroll: true")),
        "Stage keys are inline/duplicated, no longer delegated, or navigation now forces scrolling", "#pipelineStageStrip keydown delegation + native Enter")

    add("roving-selection", "Selection is bounded to 0..4 and synchronizes aria-pressed, roving tabindex and the workspace",
        all(token in select for token in ("Number(n)", "next < 0", "next > 4", 'setAttribute("aria-pressed"', "button.tabIndex", "renderPipelineWorkspace()")),
        "selectStageFilter no longer maintains an accessible, bounded selected stage", "selectStageFilter(n)")

    add("selected-workspace", "The workspace holds selected output, logs and artifacts with the original stage container",
        0 <= pane.find('id="pipelineRecovery"') < pane.find('id="pipelineWorkspace"')
        and all(needle in workspace_markup for needle in ('id="councilStagesContainer"', 'id="pipelineOutputMeta"', 'id="pipelineSelectedModel"', 'id="pipelineSelectedInput"', 'id="pipelineSelectedArtifact"', 'id="pipelineActivityLog"')),
        "The selected-stage output/context structure or original stage container is missing", "#pipelineWorkspace > output + Logs & artifacts")

    add("meaningful-empty", "No-run and not-run stages explain what exists instead of leaving an empty expanse",
        'id="pipelineStageEmpty"' in workspace_markup and "The run starts with the Architect" in pane
        and "No output artifact yet" in pane and "min-height: 190px" in html and "No run events yet" in activity,
        "The empty workspace or its factual next step was removed", "#pipelineStageEmpty[data-state]")

    state_tokens = ('state = "empty"', 'state = "loading"', 'state = "populated"', 'state = "error"', 'state = "recovery"')
    add("workspace-state-model", "Empty/loading/populated/error/recovery derive from run state and real buffers, never fake progress",
        all(token in model for token in state_tokens) and "buffer.u" in model and "buffer.m" in model and "run.endReason" in model
        and not any(token in model for token in ("setInterval", "setTimeout", "Math.random", "progressPercent")),
        "A workspace state is missing or progress is synthesized rather than read from buffers/events", ", ".join(STATES))

    add("observed-activity", "Activity entries originate in run/stream/recovery handlers and are filtered by selected stage",
        all(f'recordPipelineEvent("{kind}"' in handlers for kind in ("start", "output", "done", "unloading", "error", "stopped", "dropped"))
        and 'recordPipelineEvent("run"' in start_run and 'recordPipelineEvent("recovered"' in apply_recovered
        and "event.stage === selected" in activity and "pipelineOutputEvents.has" in handlers,
        "Activity can invent events or no longer follows the selected stage", "run/start/output/done/unloading/saved/error/stopped/dropped/recovered")

    add("bounded-activity", "The observed event ledger is bounded and the public test surface returns copies",
        "PIPELINE_EVENT_LIMIT = 80" in html and "slice(-PIPELINE_EVENT_LIMIT)" in record_event
        and "events: () => pipelineEvents.map(event => ({ ...event }))" in html,
        "The event ledger is unbounded or callers can mutate its source", "80 observed events")

    add("stable-output", "Identical output is not redrawn and workspace selection does not replace stage DOM",
        "stageContentSignature" in schedule and "if (stageContentSignature[k] !== contentSignature)" in schedule
        and "innerHTML" not in render_workspace and "replaceChildren" not in render_workspace,
        "Streaming/same-state redraw can drop Copy focus or output scroll position", "stageContentSignature + stable #councilStagesContainer")

    add("artifact-handoff-preserved", "Measured artifacts, contracts, handoffs and deliberation evidence still render from the same buffers",
        all(token in render_artifacts for token in ("renderDeliberationEvidence()", "renderPipelineWorkspace()"))
        and all(f"function {name}" in html for name in ("renderStageArtifact", "renderStageContract", "renderStageHandoff"))
        and all(f'id="stage-{name}' in html for name in ("artifact-", "contract-", "handoff-")),
        "An existing measured artifact/handoff surface was dropped", "renderCouncilArtifacts + renderDeliberationEvidence")

    payload_keys = ("task", "architect_model", "coder_model", "critic_model", "judge_model", "auto_unload", "reasoning_mode", "skills")
    add("api-payload-preserved", "Runs keep the original four-seat payload and POST only the existing council stream",
        all(f"{key}:" in payload or f"{key}," in payload for key in payload_keys) and 'consumeSSE("/api/council/stream", payload, pipelineStreamHandlers(), pipelineStreamFinalizer)' in start_run,
        "The four-seat request payload or existing stream route changed", ", ".join(payload_keys))

    add("failure-selection", "Failure, stop and drop select the affected seat even after another stage was selected",
        handlers.count("selectStageFilter(councilRun.failedStage)") >= 3
        and 'recordPipelineEvent("error"' in handlers and 'recordPipelineEvent("stopped"' in handlers and 'recordPipelineEvent("dropped"' in handlers,
        "A terminal handler leaves the reader on the wrong stage or omits its observed event", "error/stopped/closed → failedStage")

    add("truthful-terminal-state", "Normal close is not a drop, and failed/stopped/dropped overviews cannot keep saying Running",
        "const allDone = [1, 2, 3, 4].every" in handlers and "if (allDone) return" in handlers
        and all(label in finalizer for label in ('"Failed"', '"Ended"', '"Stopped"')) and "total.textContent" in finalizer,
        "Normal completion is logged as a drop or a terminal overview remains Running", "four usage records + terminal overview labels")

    add("recovery-priority", "Failure/checkpoint recovery leads output; routine saved-run confirmation follows it",
        'spec.state === "recorded"' in recovery and 'workspace.insertAdjacentElement("afterend", host)' in recovery
        and 'workspace.insertAdjacentElement("beforebegin", host)' in recovery and "presentInto(host" in recovery,
        "Routine diagnostics outrank output or failure/checkpoint recovery is hidden below it", "recorded afterend; other recovery beforebegin")

    add("checkpoint-recovery", "Checkpoint restore draws the kept stages, starts no stream, and lands focus without scrolling",
        "applyRecoveredStages(checkpoint.stages" in restore and "pipelineWorkspaceTitle" in restore and "preventScroll: true" in restore
        and not any(token in restore + apply_recovered for token in ("consumeSSE(", "fetch(", "startPipelineRun("))
        and 'PIPELINE_CHECKPOINT_KEY = "m5_pipeline_checkpoint"' in html,
        "Restore can re-run work, lost its checkpoint provenance, or drops keyboard focus/scroll", "restorePipelineCheckpoint → applyRecoveredStages")

    add("evidence-disclosure", "Run evidence is progressively disclosed and the old walk opens the real summary",
        bool(re.search(r'<details id="pipelineEvidenceDetails"[^>]* hidden', pane)) and "<summary>" in evidence
        and 'id="deliberationEvidence"' in evidence and "disclosure.hidden = false" in render_evidence
        and 'details.querySelector("summary")?.click()' in (PROJECT_ROOT / "scripts" / "council_deliberation_walk.mjs").read_text(encoding="utf-8"),
        "Evidence is permanently expanded, inaccessible, or opened by test-only DOM mutation", "#pipelineEvidenceDetails > summary")

    card = _slice(html, 'card.innerHTML = `<div class="stage-output-head"', "`;\n      container.appendChild(card)")
    add("contract-progressive-disclosure", "Contract and handoff stay in the same card, disclosed after the answer they describe",
        'class="stage-evidence"' in card and "Contract &amp; handoff" in card
        and 0 <= card.find('class="stage-output-body"') < card.find('class="stage-evidence"') < card.find('id="stage-contract-'),
        "Contract/handoff diagnostics again push the answer down or were removed", "output → Contract & handoff disclosure")

    add("long-output-reader", "Long output scrolls inside its reader and wraps instead of widening the page",
        ".stage-output-body { max-height: min(52vh, 560px)" in html and "overflow-y: auto" in html and "overflow-wrap: anywhere" in html
        and "scrollHeight > pane.output.body.clientHeight" in walk and "cards[4].tail.includes" in walk,
        "Long output can force page-level sideways scrolling or the walk only checks a truncated excerpt", "selected .stage-output-body + full-tail assertion")

    add("responsive-workspace", "Desktop, short-desktop and phone layouts preserve a no-horizontal-overflow selected workspace",
        "@media (min-width: 800px) and (max-height: 820px)" in html and "@media (max-width: 799px)" in html
        and "grid-template-columns: repeat(2, minmax(0, 1fr))" in html and "minmax(0, 1fr) 260px" in html
        and "pane.docOverflow === 0" in walk and "pane.timelineOverflow === 0" in walk,
        "A required responsive layout or its browser overflow assertion is missing", "1640/1440/1280/390 + short-height compaction")

    add("inspector-readable", "Inspector stage metadata is separated and recovered state is not mislabelled done",
        '.step-meta > span' in inspector and '.join(" · ")' in inspector and "/recovered|partial/i.test(meta)" in inspector
        and "syncPipelineInspector();" in select and render_artifacts.count("syncPipelineInspector();") >= 2
        and "const terminal = overview && councilRun" in inspector and 'terminal === "done" ? "done"' in inspector,
        "Inspector concatenates facts, mislabels recovery/terminal state, or is allowed to lag the timeline", "step-meta spans + terminal overview + immediate selection/stream/finalizer synchronization")

    add("walk-matrix", "The story walk captures every required state, viewport and theme and refuses missing evidence",
        all(f'"{state}"' in walk for state in STATES) and all(f'"{viewport}"' in walk for viewport in VIEWPORTS)
        and 'const THEMES = ["light", "dark"]' in walk and "capture-manifest.json" in walk
        and "shots.length !== expected" in walk and "checks.length - failures.length" in walk,
        "The browser producer no longer covers or enforces the full 6 × 4 × 2 capture matrix", "48 captures + N/N checks + hashed manifest")

    add("walk-real-fixture", "The walk starts its own seeded app/Chromium and exercises real keyboard, focus, scroll, payload and error paths",
        'resolve("scripts/pipeline_recovery_seed.py")' in walk and "puppeteer.launch" in walk and "resolveChrome" in walk
        and 'page.keyboard.press("Enter")' in walk and "streaming redraws retain focus" in walk
        and 'posts[postsBeforeFailure].path === "/api/council/stream"' in walk and 'page.on("pageerror"' in walk,
        "The walk can report a state without starting/driving the actual application", "self-start fixture + real Chromium + interactions")

    add("seed-real-stream", "Slow/long/error states are additive provider behaviours through the server's real stream",
        "original = studio.stream_any_model" in seed and "async for item in original" in seed
        and "await asyncio.sleep(2.0)" in seed and 'yield {**item, "text": LONG_REPLY}' in seed
        and '"slow_model": SLOW_MODEL' in seed and '"long_model": LONG_MODEL' in seed,
        "A fixture paints a state or bypasses the orchestrator/SSE path", "architect-loading-demo / judge-long-content-demo / critic-fails-demo")

    stage_constructors = re.findall(r"WorkflowStage\(([1-4]),", api_route)
    add("backend-stage-contract", "The authenticated backend route still constructs exactly four known stages and no invented stage",
        stage_constructors == ["1", "2", "3", "4"] and api_route.count("orchestrator.run_stages") == 1
        and all(f'body.get("{key}")' in api_route for key in payload_keys[:5]),
        f"The backend stage contract changed: {stage_constructors}", "WorkflowStage(1..4) + existing orchestrator")

    findings = [{"check": check["check"], "finding": check["finding"]} for check in checks if not check["passed"]]
    return {
        "story": "UIV2-25",
        "title": "Pipeline stage-focused workspace and artifacts",
        "status": "clean" if not findings else "findings",
        "check_count": len(checks),
        "source": {
            "frontend": {"path": "static/index.html", "sha256": _sha(html)},
            "backend": {"path": "app.py", "sha256": _sha(app)},
            "walk": {"path": "scripts/pipeline_workspace_walk.mjs", "sha256": _sha(walk)},
            "fixture": {"path": "scripts/pipeline_recovery_seed.py", "sha256": _sha(seed)},
        },
        "anchors": {
            "pane": _line(html, 'id="pane-council"'),
            "workspace": _line(html, 'id="pipelineWorkspace"'),
            "stage_selection": _line(html, "function selectStageFilter"),
            "state_model": _line(html, "function pipelineWorkspaceModel"),
            "activity": _line(html, "function recordPipelineEvent"),
            "stream_handlers": _line(html, "function pipelineStreamHandlers"),
            "recovery": _line(html, "function renderPipelineRecovery"),
        },
        "stage_model": {"overview": 0, "stages": [1, 2, 3, 4], "route": "/api/council/stream", "payload_keys": list(payload_keys)},
        "visual_matrix": {"states": STATES, "viewports": VIEWPORTS, "themes": ["light", "dark"], "required_captures": 48},
        "state_sources": [
            {"state": "empty", "source": "no councilRun and no stage card", "required_reading": "task, input, absent artifact and no invented events"},
            {"state": "loading", "source": "observed stage_start/token without stage_done usage", "required_reading": "active seat only; no percentage or completed claim"},
            {"state": "populated", "source": "stage_done usage and council_done saved run id", "required_reading": "one selected answer plus actual artifact/activity; All Stages remains available"},
            {"state": "long-content", "source": "real long provider stream in the selected Judge buffer", "required_reading": "internal vertical reader, full output retained, no horizontal overflow"},
            {"state": "error", "source": "provider exception through the existing error SSE event", "required_reading": "failed Critic selected, terminal Failed, retry and checkpoint visible"},
            {"state": "recovery", "source": "browser checkpoint restored through applyRecoveredStages", "required_reading": "provenance, original task/output, no POST, workspace focus without scroll"},
        ],
        "checks": checks,
        "findings": findings,
    }


def render_doc(payload: dict) -> str:
    rows = []
    for check in payload["checks"]:
        rows.append(f"| `{check['check']}` | {'PASS' if check['passed'] else 'FAIL'} | {check['rule']} | {check['finding'] or '—'} |")
    state_rows = [f"| `{row['state']}` | {row['source']} | {row['required_reading']} |" for row in payload["state_sources"]]
    return "\n".join([
        "# UIV2-25 — Pipeline stage-focused workspace and artifacts",
        "",
        f"**Inventory status:** {payload['status']} · {payload['check_count'] - len(payload['findings'])}/{payload['check_count']} rules clean.",
        "",
        "The canonical surface is `#pane-council`, opened with `switchTab(\"council\")` and labelled **Pipeline**. The existing four `WorkflowStage` records, controls, `/api/council/stream` payload, authorization, checkpoints and recovery actions are not replaced. The UI adds a selected-stage reader over the same `stageBuffers` and `councilRun` state.",
        "",
        "## Reading order and visual contract",
        "",
        "Task → compact stage setup → four-stage timeline (plus the preserved All Stages view) → measured run summary → failure/checkpoint recovery when present → selected output with logs and artifacts → disclosed run evidence. A routine saved-run record follows output; failure and checkpoint decisions precede it. Contract and handoff facts remain in each stage card under a native disclosure after the answer.",
        "",
        "Colour accompanies an explicit status word/tone. Loading is an observed stage start/token, never a percentage. Empty stages name the absent artifact. Long answers retain their complete buffer in an internal vertical reader. The phone timeline becomes two columns with All Stages across the row; short desktops compact vertically rather than hiding output.",
        "",
        "## State provenance matrix",
        "",
        "| State | Actual source | What must be readable |",
        "|---|---|---|",
        *state_rows,
        "",
        "## Rule findings",
        "",
        "Every rule has a finding when broken. `tests/test_pipeline_workspace.py` breaks each rule in copied source; generated artifacts are refused by `--check` when source or documentation drifts.",
        "",
        "| Rule | Result | Contract | Finding |",
        "|---|---|---|---|",
        *rows,
        "",
        "## Reproduce the running-app evidence",
        "",
        "```bash",
        "python3 scripts/uiux_pipeline_workspace_inventory.py --check",
        "pytest -q tests/test_pipeline_workspace.py tests/test_pipeline_artifacts.py tests/test_pipeline_recovery.py tests/test_council_deliberation.py",
        "node scripts/pipeline_workspace_walk.mjs --out docs/media/pipeline-workspace-uiv2-25/after",
        "node scripts/pipeline_artifacts_walk.mjs --json",
        "node scripts/pipeline_recovery_walk.mjs --json",
        "node scripts/council_deliberation_walk.mjs --json",
        "```",
        "",
        "The walk starts `scripts/pipeline_recovery_seed.py` and Chromium itself. It writes 48 captures (six states × four viewports × two themes), prints N/N checks, and records capture hashes and all assertions in `capture-manifest.json`. Slow, long and failing answers use the server's real provider/orchestrator/SSE path. Before captures are served from pre-change commit `605b977`; local PNG evidence and manifests are intentionally untracked to avoid adding more image binaries to Git.",
        "",
        "## Source anchors",
        "",
        *[f"- `{name}`: static/index.html line {line}" for name, line in payload["anchors"].items()],
        "",
        "Rollback is reverting only the isolated UIV2-25 commit. It does not delete sessions, database state, MCP secrets, checkpoints or workspace files.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = build_inventory()
    json_text = json.dumps(payload, indent=2) + "\n"
    doc_text = render_doc(payload)
    if args.json:
        print(json_text, end="")
        return 1 if payload["findings"] else 0
    if args.check:
        drift = []
        for path, text in ((JSON_PATH, json_text), (DOC_PATH, doc_text)):
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drift.append(str(path.relative_to(PROJECT_ROOT)))
        if drift:
            print("inventory drift: " + ", ".join(drift), file=sys.stderr)
        if payload["findings"]:
            for finding in payload["findings"]:
                print(f"FINDING {finding['check']}: {finding['finding']}", file=sys.stderr)
        if drift or payload["findings"]:
            return 1
        print(f"{payload['check_count']} checks, 0 findings — clean")
        return 0
    JSON_PATH.write_text(json_text, encoding="utf-8")
    DOC_PATH.write_text(doc_text, encoding="utf-8")
    print(f"wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}")
    print(f"{payload['check_count']} checks, {len(payload['findings'])} findings")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
