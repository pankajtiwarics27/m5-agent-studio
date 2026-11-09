# UIV2-25 — Pipeline stage-focused workspace and artifacts

**Inventory status:** clean · 28/28 rules clean.

The canonical surface is `#pane-council`, opened with `switchTab("council")` and labelled **Pipeline**. The existing four `WorkflowStage` records, controls, `/api/council/stream` payload, authorization, checkpoints and recovery actions are not replaced. The UI adds a selected-stage reader over the same `stageBuffers` and `councilRun` state.

## Reading order and visual contract

Task → compact stage setup → four-stage timeline (plus the preserved All Stages view) → measured run summary → failure/checkpoint recovery when present → selected output with logs and artifacts → disclosed run evidence. A routine saved-run record follows output; failure and checkpoint decisions precede it. Contract and handoff facts remain in each stage card under a native disclosure after the answer.

Colour accompanies an explicit status word/tone. Loading is an observed stage start/token, never a percentage. Empty stages name the absent artifact. Long answers retain their complete buffer in an internal vertical reader. The phone timeline becomes two columns with All Stages across the row; short desktops compact vertically rather than hiding output.

## State provenance matrix

| State | Actual source | What must be readable |
|---|---|---|
| `empty` | no councilRun and no stage card | task, input, absent artifact and no invented events |
| `loading` | observed stage_start/token without stage_done usage | active seat only; no percentage or completed claim |
| `populated` | stage_done usage and council_done saved run id | one selected answer plus actual artifact/activity; All Stages remains available |
| `long-content` | real long provider stream in the selected Judge buffer | internal vertical reader, full output retained, no horizontal overflow |
| `error` | provider exception through the existing error SSE event | failed Critic selected, terminal Failed, retry and checkpoint visible |
| `recovery` | browser checkpoint restored through applyRecoveredStages | provenance, original task/output, no POST, workspace focus without scroll |

## Rule findings

Every rule has a finding when broken. `tests/test_pipeline_workspace.py` breaks each rule in copied source; generated artifacts are refused by `--check` when source or documentation drifts.

| Rule | Result | Contract | Finding |
|---|---|---|---|
| `pane-anchor` | PASS | Pipeline remains #pane-council, opened by the existing council tab | — |
| `four-stage-timeline` | PASS | All Stages plus exactly the server's four stage buttons survive | — |
| `original-controls` | PASS | The original task, run, stop, model and reasoning controls are preserved | — |
| `task-first` | PASS | Task content leads setup, diagnostics and the selected-stage workspace | — |
| `setup-disclosure` | PASS | Stage setup is a closed native disclosure whose summary states its contents | — |
| `stage-keyboard` | PASS | One delegated strip handler navigates Arrow/Home/End without scrolling; Enter remains native | — |
| `roving-selection` | PASS | Selection is bounded to 0..4 and synchronizes aria-pressed, roving tabindex and the workspace | — |
| `selected-workspace` | PASS | The workspace holds selected output, logs and artifacts with the original stage container | — |
| `meaningful-empty` | PASS | No-run and not-run stages explain what exists instead of leaving an empty expanse | — |
| `workspace-state-model` | PASS | Empty/loading/populated/error/recovery derive from run state and real buffers, never fake progress | — |
| `observed-activity` | PASS | Activity entries originate in run/stream/recovery handlers and are filtered by selected stage | — |
| `bounded-activity` | PASS | The observed event ledger is bounded and the public test surface returns copies | — |
| `stable-output` | PASS | Identical output is not redrawn and workspace selection does not replace stage DOM | — |
| `artifact-handoff-preserved` | PASS | Measured artifacts, contracts, handoffs and deliberation evidence still render from the same buffers | — |
| `api-payload-preserved` | PASS | Runs keep the original four-seat payload and POST only the existing council stream | — |
| `failure-selection` | PASS | Failure, stop and drop select the affected seat even after another stage was selected | — |
| `truthful-terminal-state` | PASS | Normal close is not a drop, and failed/stopped/dropped overviews cannot keep saying Running | — |
| `recovery-priority` | PASS | Failure/checkpoint recovery leads output; routine saved-run confirmation follows it | — |
| `checkpoint-recovery` | PASS | Checkpoint restore draws the kept stages, starts no stream, and lands focus without scrolling | — |
| `evidence-disclosure` | PASS | Run evidence is progressively disclosed and the old walk opens the real summary | — |
| `contract-progressive-disclosure` | PASS | Contract and handoff stay in the same card, disclosed after the answer they describe | — |
| `long-output-reader` | PASS | Long output scrolls inside its reader and wraps instead of widening the page | — |
| `responsive-workspace` | PASS | Desktop, short-desktop and phone layouts preserve a no-horizontal-overflow selected workspace | — |
| `inspector-readable` | PASS | Inspector stage metadata is separated and recovered state is not mislabelled done | — |
| `walk-matrix` | PASS | The story walk captures every required state, viewport and theme and refuses missing evidence | — |
| `walk-real-fixture` | PASS | The walk starts its own seeded app/Chromium and exercises real keyboard, focus, scroll, payload and error paths | — |
| `seed-real-stream` | PASS | Slow/long/error states are additive provider behaviours through the server's real stream | — |
| `backend-stage-contract` | PASS | The authenticated backend route still constructs exactly four known stages and no invented stage | — |

## Reproduce the running-app evidence

```bash
python3 scripts/uiux_pipeline_workspace_inventory.py --check
pytest -q tests/test_pipeline_workspace.py tests/test_pipeline_artifacts.py tests/test_pipeline_recovery.py tests/test_council_deliberation.py
node scripts/pipeline_workspace_walk.mjs --out docs/media/pipeline-workspace-uiv2-25/after
node scripts/pipeline_artifacts_walk.mjs --json
node scripts/pipeline_recovery_walk.mjs --json
node scripts/council_deliberation_walk.mjs --json
```

The walk starts `scripts/pipeline_recovery_seed.py` and Chromium itself. It writes 48 captures (six states × four viewports × two themes), prints N/N checks, and records capture hashes and all assertions in `capture-manifest.json`. Slow, long and failing answers use the server's real provider/orchestrator/SSE path. Before captures are served from pre-change commit `605b977`; local PNG evidence and manifests are intentionally untracked to avoid adding more image binaries to Git.

## Source anchors

- `pane`: static/index.html line 3729
- `workspace`: static/index.html line 3800
- `stage_selection`: static/index.html line 10397
- `state_model`: static/index.html line 9725
- `activity`: static/index.html line 9676
- `stream_handlers`: static/index.html line 10518
- `recovery`: static/index.html line 10300

Rollback is reverting only the isolated UIV2-25 commit. It does not delete sessions, database state, MCP secrets, checkpoints or workspace files.
