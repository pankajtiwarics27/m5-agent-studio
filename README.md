# M5 Agent Studio

> Local-first AI developer platform and agent harness (repository package
> `m5-agent-studio`, product name **M5 Agentic Studio**, version `v9.0.1`).
> This README is the **ground-truth current-state document** required by the
> M5 2.0 roadmap's readme contract: every claim below was verified against the
> code or a passing test in this checkout on 2026-10-30, or is explicitly
> labeled unverified. Planned capabilities are not presented as product
> capabilities. Evidence: [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md),
> [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md).

[![CI](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/pankajtiwarics27/m5-agent-studio/actions/workflows/ci.yml)

**Start with the guides:** [Quickstart](docs/quickstart.md) ·
[Architecture](docs/architecture.md) ·
[Supported hardware](docs/supported-hardware.md) ·
[Privacy](docs/privacy.md) ·
[Migration guide](docs/migration-guide.md) ·
[All documentation](docs/README.md)

---

## What is verified today

Verified in this checkout (commit `1e2e0bd876409a9e4eeb4b0128b403422bac7013`
plus the M5 2.0 change-sets through `M5-P1-07`, Linux x86_64, Python 3.11.2):

- **The application runs and its local auth path works.** `scripts/smoke_local.py`
  launches a disposable loopback instance of both `app.py` and the generated
  `studio.py` and verifies the token/security path — both exit 0.
- **The test suite passes:** **881 passed, 1 skipped** (the skip is the opt-in
  live container-runtime test, see below). `ruff`, `mypy` over 43 typed modules,
  `compileall`, and the Node-based frontend syntax check all pass.
- **Generated artifacts are reproducible:** `studio.py`,
  `docs/route-security-matrix.md`, `docs/subsystem-dependency-inventory.md`
  regenerate byte-for-byte; the roadmap dashboard matches its JSON.
- **Storage is SQLite Schema v3 with WAL, FTS5, and short-transaction
  persistence discipline (ADR-0003, since `M5-P1-01`):** `runtime_persistence.py`
  executes additive versioned migrations (`SCHEMA_VERSION = 3`) inside
  `BEGIN IMMEDIATE` after an owner-only (`0600`) pre-upgrade snapshot, routes
  every `app.py` and `context_store.py` SQLite read/write through `run_read` /
  `run_write_transaction` with bounded busy retries and fail-closed
  busy/disk-full/corruption errors, prevents active write transactions from
  spanning tool execution, subprocesses, model inference, or keyring I/O, and
  defines durable spine tables (`runtime_tasks`, `runtime_task_epochs`,
  `runtime_task_transitions`, `runtime_actions`, `runtime_task_checkpoints`) —
  pinned by 15 persistence tests (`tests/test_runtime_persistence.py`). Default
  DB: `studio_history.db` beside the entry point; override with
  `STUDIO_DB_PATH` (`app.py` L269).
- **Root tasks, child milestones, hash-verified checkpoints, and task/recovery
  budgets persist across restarts (`M5-P1-05`):** `workflow_state.py`
  (`DurableTaskStateMachine`), `workflow_checkpoints.py`
  (`DurableTaskCheckpointStore`), and `execution_budget.py`
  (`DurableTaskBudgetManager`) are wired into `init_db()`, harness/GSD/SDLC
  workflows, and `/api/harness/sessions/{session_id}/{task,pause,cancel,resume}`
  to persist root task and child milestone transitions, defer mid-flight pauses
  to safe tool boundaries while fencing cancellations without claiming premature
  completion, verify SHA-256 checkpoint and `EvidenceRef` digests before resume,
  reconcile staged/incomplete transitions on startup, and enforce cumulative
  step/tool/token/time and crash-recovery budgets across restarts — pinned by 9
  end-to-end and crash-recovery tests (`tests/test_durable_task_state_machine.py`).
- **Single-agent coding tasks complete with authoritative patch + test evidence,
  action-boundary crash recovery, and false-completion rejection (`M5-P1-06`):**
  `/api/harness/run` and `DurableTaskStateMachine.complete_task` enforce a
  universal unrecovered-shell/test failure guard plus optional persisted
  `TaskVerificationSpec` contracts (managed-edit ledger provenance, Git diff and
  modified-file SHA-256 deltas, protected user-file preservation, post-edit test
  ordering, and independent `subprocess.run(..., shell=False)` test execution
  emitting `patch_verification` and `test_verification` `EvidenceRef`s);
  `failure_injection.ActionBoundaryFaultInjector` is wired into
  `DurableActionBoundary` and harness completion (`before_intent`,
  `after_intent_before_effect`, `after_effect_before_result_commit`,
  `before_task_completion`) alongside session `known_action_args` crash
  reconciliation so mid-action crashes neither lose user work nor duplicate side
  effects — pinned by 8 vertical-slice tests
  (`tests/test_single_agent_vertical_slice.py`). Note: model streaming in CI
  uses deterministic scripted streams; resident local-oMLX coding runs and live
  container sandboxing remain unverified in the Linux CI environment.
- **The frontend is a single-file vanilla JS UI** (`static/index.html`,
  4,576 lines; no framework build step).
- **Tool, skill, smart-block, and MCP operations cross an authoritative action
  admission gate and durable intent/result ledger** (`_run_authorized_tool`
  delegates to `execution_spine.DurableActionBoundary` +
  `side_effect_ledger.DurableActionLedger`, tightened in `M5-P1-02` and
  `M5-P1-03` and `M5-P1-04`) with deterministic action classes (`read_only` vs
  `managed_edit`, `consequential`, `external`), grant provenance (no permission
  inferred from model output), transactional single-owner worker leases with
  monotonic `runtime_task_epochs` and heartbeat/sleep-wake/crash fencing,
  pre-takeover reconciliation holding replacement execution `blocked` when
  unresolved effects remain, durable `runtime_actions` intent rows committed
  before side effects (`never_started`, `in_flight`, `committed`, `uncertain`
  plus `denied`/`failed`) with atomic `BEGIN IMMEDIATE` epoch/worker checks,
  missing-result reconciliation to `uncertain` (never `failed`), action-type
  replay classification (`idempotent`, `reconcilable`, `non_idempotent`)
  blocking blind replay of uncertain non-idempotent writes, durable
  `tool_audit_events` rows, a pre-execute write-transaction guard, and scoped
  `require_active_admission` anti-bypass enforcement on inner executors
  (`tests/test_action_admission_gate.py`, `tests/test_durable_intent_ledger.py`,
  `tests/test_worker_leases_and_fencing.py`).
- **Task events are a durable, cursor-addressable stream and the UI reloads from
  it (`M5-P1-07`):** `task_events.py` (stdlib only, inlined into `studio.py`)
  gives each session a monotonic `seq` with a durable sidecar, idempotent
  terminal/approval records (`dedupe_key`), atomic pruning, and reads that answer
  with an explicit verdict (`snapshot_required`, `resync_reason`,
  `missing_ranges`, `truncated`). Three authenticated routes expose it:
  `GET /api/harness/sessions/{session_id}/events?cursor=`,
  `GET /api/harness/sessions/{session_id}/events/stream` (SSE frames whose `id` is
  the sequence, `Last-Event-ID` resume, `resync_required` when a cursor cannot be
  resumed), and `GET /api/harness/sessions/{session_id}/state` (task row, budget,
  hash-verified checkpoint, event summary, and `pending`/`expired`/`recent`
  approval reconciliation). The UI rebuilds every selected session from that
  snapshot, re-renders sequence-keyed so a replay cannot duplicate a completion,
  and forces a snapshot reload on any gap; an approval id with a durable decision
  is rejected with `409` even after a restart. Pinned by 16 tests
  (`tests/test_task_events.py`) and documented in
  [`docs/task-events.md`](docs/task-events.md) (ADR-0005).
- **Pre-edit Git baselines, selective content-addressed snapshots, sensitive-file
  in-place preservation, and disk-full safe pause protect user work (`M5-P2-01`):**
  `workspace_snapshots.py` and `sandbox_boundary.py` (`confined_path`) are wired
  into `app.py` and `studio.py` across session creation, turn start, managed
  `write_file`/`edit_file` tools, worktree `/apply`, `run_bash` destructive-Git
  blocking, and `/api/harness/sessions/{session_id}/snapshots` (`GET`, `POST`,
  and `POST .../{snapshot_id}/restore`). Baselines record `HEAD` commit/tree,
  staged index deltas, unstaged working-tree edits/deletions, and relevant
  untracked user files into owner-only (`0600`) SHA-256 content-addressed blobs
  while lazily capturing clean target files before first edit, preserving
  ignored/sensitive files (`.env`, `.secrets`, keys, credentials) in place
  without copying secret bytes into CAS, blocking `git reset --hard`,
  `git clean -f`, and blanket `git checkout -- .` / `git restore .`, refusing
  worktree `/apply` overwrites of uncommitted user edits (`409`), and
  transitioning tasks into a safe `paused` state on `ENOSPC` / disk-full without
  partial file writes — pinned by 8 tests (`tests/test_workspace_snapshots.py`).
  Multi-file inverse-patch rollback arrived in `M5-P2-03`.
- **Managed edits are version-aware and every applied edit is a durable
  transaction (`M5-P2-02`):** an edit resolves exactly one expected content
  version — the caller's `expected_sha256`, else the digest `read_file` handed
  this session, else the bytes this runtime last wrote — and refuses with
  `error_code: "stale_version"` instead of overwriting a file that changed
  underneath it. The expectation is re-checked through the same directory
  descriptor immediately before the atomic rename, so a save landing mid-edit is
  still caught and the temporary file is discarded. `read_file` now reports the
  digest it delivered (`sha256:` line plus a `sha256` field), and `write_file` /
  `edit_file` return `sha256`, `previous_sha256` and the recorded
  `edit_transaction`. `edit_transactions.py` (stdlib only, inlined into
  `studio.py`) appends one owner-only transaction per edit to
  `.harnesses/edits/<session>.jsonl` (`O_APPEND` + `fsync`, `edit_id`, monotonic
  `seq`, pre/post digests, expected/observed digests, mode, size) and keeps the
  observed versions in a bounded atomic JSON index so the expectation survives a
  restart. Absolute, `..`-traversal, symlinked and secret paths are refused with
  `error_code: "unauthorized_path"` before anything is written; conflicts and
  applied edits are published as `edit/conflict` and `edit/applied` task events.
  Pinned by 19 tests (`tests/test_edit_transactions.py`) and documented in
  [`docs/version-aware-edits.md`](docs/version-aware-edits.md) (ADR-0006).
- **Multi-file edits are journalled and can be rolled back without a destructive
  Git reset (`M5-P2-03`):** every managed `write_file` / `edit_file` also appends a
  step to a durable edit batch (`.harnesses/edits/batches/`, `0700`/`0600`, atomic
  replace) holding the per-file pre/post digests, both CAS blobs, the previous mode
  and a step state — recorded *before* the bytes land, so an interrupted multi-file
  edit is recoverable in either direction. `rollback_edits` plans per path from the
  bytes on disk (`already_reverted`, `delete_created`, `revert`, `revert_merged`,
  `conflict`), restores through compare-and-swap writes against the digest the batch
  wrote, and applies a line-level three-way inverse patch when the user edited the
  file afterwards, so unrelated user edits survive. A single conflicting path means
  **nothing is written**: the batch is left `unresolved` with per-path reasons.
  `reconcile_task_actions` reports `in_flight`/`uncertain` managed-edit actions, and
  a rollback refuses while one exists unless the caller acknowledges it (recorded in
  the batch notes and the `edit/rollback` task event); an unreadable ledger blocks it
  too. Per-step durable marking makes an interrupted rollback resumable
  (`partially_rolled_back`), and no Git command is ever run. Pinned by 27 tests
  (`tests/test_edit_rollback.py`) and documented in
  [`docs/edit-rollback.md`](docs/edit-rollback.md) (ADR-0007).
- **A verified completion now carries revision-anchored evidence, and failures are
  attributed instead of assumed (`M5-P2-04`):** `verification_evidence.py` (stdlib
  only, inlined into `studio.py`) captures the revision the authoritative test
  command ran against — `git HEAD`, the bounded dirty-path list with cache/coverage
  artifacts filtered out, and each declared file's SHA-256 — before *and* after the
  run, so a workspace that moves mid-run fails as
  `workspace_changed_during_verification` instead of passing on a stale tree. The
  transcript hash covers the command, exit code, bounded output and both revision
  digests; the environment snapshot (interpreter, platform, cwd, execution surface)
  is recorded with it. Failures are parsed from the transcript and classified
  `pre_existing` / `environmental` / `introduced`; pre-existing failures only
  tolerate verification under an explicit `allow_pre_existing_failures` opt-in, and
  unparseable non-zero output becomes an `introduced` record rather than a clean
  run. Refusal markers (`code execution is disabled`, sandbox unavailable, plan
  denial) force `denied` and fail completion **even at exit code 0**. An optional
  `targeted_test_command` runs first and stops the broader stage when it fails, and
  an optional `acceptance_coverage` declaration fails completion when a named test
  id was not observed passing. `GET /api/harness/sessions/{session_id}/task` exposes
  a read-only revision preview. Pinned by 31 tests
  (`tests/test_verification_evidence.py`) and documented in
  [`docs/verification-evidence.md`](docs/verification-evidence.md) (ADR-0008).
- **Repeated failures are remembered and bounded instead of retried forever
  (`M5-P2-05`):** `recovery_guard.py` (stdlib only, inlined into `studio.py`) keeps
  one durable record per task (`.harnesses/sessions/recovery/`, `0700`/`0600`,
  atomic replace) holding every verification/recovery/rollback attempt with an
  evidence snapshot — source revision, transcript hash, failure fingerprint, failure
  code, environment — plus retry/replan/rollback counters. An attempt whose evidence
  repeats the latest failure is classified `stagnant` and, after
  `max_identical_attempts` (default 2), escalates; a revisited abandoned state is
  `oscillating` and escalates immediately. Escalation sets `pause_requested` on the
  task budget, so the execution boundary treats the task as paused and a harness
  turn that fails verification releases its lease into `paused` with reason
  `stagnation_detected` instead of `failed`. `prepare_resume` refuses a resume whose
  workspace conditions equal the recorded failure's (`409`), resumed turns spend
  `retry` (or `replan` after escalation) only once the worker lease is held, and
  `rollback_edits` spends `rollback` before writing; exhaustion is persisted before
  the refusal. `GET /api/harness/sessions/{session_id}/task` exposes the read-only
  `recovery` summary. Pinned by 34 tests (`tests/test_recovery_guard.py`) and
  documented in [`docs/recovery-guard.md`](docs/recovery-guard.md) (ADR-0009).
- **The two environment-blocked stories ship their evidence tool and stay blocked
  (`M5-P3-01`, `M5-P4-01`):** `scripts/qualify_environment.py` (standard library
  only, with `scripts/qualify_environment.sh` as a thin wrapper) is the operator-run
  suite that CI cannot be. `--sandbox` probes a live runtime with the **product's**
  own path (`app._execute_limited` under `STUDIO_EXECUTION_MODE=sandbox`) and an
  adversarial matrix carried in the product's full hardening set (`--network=none`,
  `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
  `--pull=never`, `HOME`/`TMPDIR` pinned to `/tmp`, `--ipc=private`,
  `--ulimit nofile`, a `noexec` tmpfs and the same `sh -lc exec` form): outside
  paths and traversal (with the errno class recorded, so absence and denial stay
  different facts), a workspace symlink pointing outside, direct and host-loopback
  egress, raw sockets, host credential stores and `HOME`, a 3 GiB allocation under
  `--memory 512m`, a surviving descendant, and `scripts/isolation_smoke.py --live`.
  `p3.product.argv_parity` compares the suite's argv against `app._sandbox_command`
  **on the machine running it**, so product drift fails the owner's run instead of
  silently qualifying stale flags. `--hardware` records the machine fingerprint,
  checks that `resource_admission` admits exactly one heavy request at 80 % of
  detected memory and refuses the second, checks that routing confidence without a
  measured profile is `unknown`, drives the real `app.stream_any_model` funnel to
  prove that a second heavy local stream is refused while one is resident, and —
  only when asked with `--pressure-gb` / `--model-command` — measures swap growth
  under a touched allocation and the timings of a caller-supplied model probe,
  optionally persisted as a `model_calibration` profile. Every check records `pass` / `fail` / `skip` /
  `blocked` with a reason: a required check that could not run makes its suite
  `blocked`, a failed one makes it `unqualified`, and only an all-passing run is
  `qualified`; exit codes follow (`0` / `1` / `2`). `unqualified` is reserved for
  *observed* violations: a missing image or a container that cannot see the shared
  workspace is `blocked` (a setup gap must never read as a security failure), and
  `--dry-run` prints every command without executing any of them. The record binds the repository
  revision, OS build, chip, RAM and detected runtime into JSON and Markdown. **No
  container-isolation or 24 GB claim is made anywhere in this repository**, both
  stories stay `blocked` in the roadmap until the owner produces a qualified record
  on the target machine. Pinned by 54 tests (`tests/test_qualify_environment.py`)
  and documented in [`docs/qualification-suite.md`](docs/qualification-suite.md)
  (ADR-0010).
- **A second heavy local model request is refused by policy in the serving path
  (`M5-P4-01`, code half — the story stays blocked on measurement):**
  `model_admission.py` (stdlib only, inlined into `studio.py`) wraps
  `resource_admission` with local-inference semantics — capacity is the machine's
  unified memory (detected, or `STUDIO_HEAVY_MODEL_MEMORY_BYTES`), one request
  reserves 80 % of it, there is one slot, and only one *distinct* model may be
  resident. `app.stream_any_model`, the single funnel every local stream passes
  through (direct chat, council, debates, harness critic turns, workflows), holds
  that lease for the stream lifetime and releases it in `finally`, so an abandoned
  or failed stream frees the slot; nested work on the same model re-enters the
  lease instead of deadlocking. `POST /api/chat/stream` and
  `POST /v1/chat/completions` pre-flight a `429` with the policy snapshot, and
  `GET /api/models/admission` exposes capacity, usage, residency and grant/denial
  counters. Denials are typed and stable (`heavy_model_slot_busy`,
  `heavy_model_memory_exceeded`, `heavy_model_resident`). Cloud providers consume
  no slot. **The 80 % reservation is a policy constant, not a measurement**: the
  24 GB pressure criterion is still unmeasured, sequential model switching is
  `M5-P4-02`, and the policy is per-process like the worker-lease model. Pinned by
  34 tests (`tests/test_model_admission.py`) and documented in
  [`docs/model-admission.md`](docs/model-admission.md) (ADR-0011).

- **The execution boundary is extracted and its coverage is inventoried
  (ADR-0002, since `M5-P0-06`; expanded in `M5-P1-02`–`M5-P1-04`):**
  `execution_spine.py` and `side_effect_ledger.py` form a stdlib-only seam
  (classify → grant/task/epoch/lease check → policy → durable intent & replay
  gate → scoped execute → durable outcome/reconciliation) with 15 spine
  contract tests (`tests/test_execution_spine.py`), 10 admission-gate
  regression tests (`tests/test_action_admission_gate.py`), 10 durable
  intent-ledger tests (`tests/test_durable_intent_ledger.py`), and 10 worker
  lease & fencing tests (`tests/test_worker_leases_and_fencing.py`). Coverage is
  **partial** —
  [`docs/m5-2-0-bypass-inventory.md`](docs/m5-2-0-bypass-inventory.md) is the
  maintained inventory of which entry points are outside the boundary; no
  complete security guarantee is claimed.
- **Skill-code execution is deny-by-default and admission-routed** (since
  `M5-P0-03`, wired into `DurableActionBoundary` in `M5-P1-02`): skill scripts
  run only with `STUDIO_ALLOW_SKILL_CODE=1`, non-`plan` policy, and a live
  execution mode, through `_run_authorized_tool` (`skill_execute_script`) —
  pinned by 9 containment tests (`tests/test_skill_execution_containment.py`)
  plus admission/audit regression tests (`tests/test_action_admission_gate.py`).
- **21 of 49 shipped Python modules are wired into the served app**
  (`context_store`, `mcp_client`, `onboarding_preflight`, `history_privacy`,
  `workflow_orchestration`, `execution_spine` since `M5-P0-06`,
  `runtime_persistence` since `M5-P1-01`, `side_effect_ledger` since `M5-P1-03`,
  `execution_budget`, `workflow_checkpoints`, `workflow_state` since
  `M5-P1-05`, `failure_injection` since `M5-P1-06`, `task_events` since
  `M5-P1-07`, `sandbox_boundary` + `workspace_snapshots` since `M5-P2-01`, and
  `edit_transactions` since `M5-P2-02`, `edit_rollback` since `M5-P2-03`, and
  `verification_evidence` since `M5-P2-04`, `recovery_guard` since
  `M5-P2-05`, and `resource_admission` + `model_admission` since `M5-P4-01`;
  5 of 42 at the time of the audit table). Together they govern
  schema upgrades, SQLite transactions, authoritative task-state / lease-epoch
  checks at admission (`M5-P1-02`), durable `runtime_actions`
  intent/result/replay tracking (`M5-P1-03`), single-owner worker leases,
  fencing, and pre-takeover reconciliation (`M5-P1-04`), durable root/child
  task state machines, safe pause/resume/cancel boundaries, hash-verified
  checkpoints with evidence refs, and persistent task/recovery budgets
  (`M5-P1-05`), verified coding-task completion gates plus deterministic
  action-boundary crash injection (`M5-P1-06`), the durable cursor-addressed
  task-event stream with reload/resync and approval reconciliation (`M5-P1-07`),
  pre-edit Git baseline capture, selective CAS snapshots, path confinement, and
  non-destructive baseline restore (`M5-P2-01`), version-aware managed edits
  with durable edit transactions (`M5-P2-02`), durable multi-file edit batches
  with version-checked rollback (`M5-P2-03`), and revision-anchored verification
  evidence with baseline-aware failure classification (`M5-P2-04`), and durable
  failure memory with bounded retry/replan/rollback budgets and safe pause
  (`M5-P2-05`), and one-heavy-local-model admission around the streaming funnel
  (`M5-P4-01`).
  The other 28 modules have passing unit tests but **no `app.py` call path**.
  Module presence is not integration. Full table:
  [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) §4.

Reported claims that this review **refuted**: `app.py` is ~2,200 lines
(actual at the `M5-P0-02` audit: **9,627**; **12,655** at the `M5-P2-01`
change-set), and `mcp_client` has no call sites (it is invoked at
`app.py` L4643 via `mcp_client.adaptive_call`).

## Requirements and verified setup

**Verified prerequisites** (used successfully in this review):

- Python `>=3.11` (tested on 3.11.2), `git`, and Node.js (for the frontend
  syntax check).
- Full git history for baseline verification. If the clone is shallow:

```bash
git fetch --unshallow origin
```

**Verified setup and checks** (exact commands, all run and green on 2026-10-30):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,supply-chain]'

.venv/bin/python scripts/verify_baseline.py     # pinned baseline ancestry
.venv/bin/ruff check .                          # lint
.venv/bin/mypy                                  # typed contract modules
.venv/bin/python -m compileall -q app.py scripts tests
.venv/bin/python scripts/check_frontend_syntax.py
.venv/bin/python -m pytest -q                   # 881 passed, 1 skipped
.venv/bin/python scripts/smoke_local.py         # loopback app, auth verified
.venv/bin/python scripts/smoke_local.py --standalone
```

**Documented, not verified in this environment** (macOS-only or tool-specific;
kept from the previous README because they are the project's stated macOS
quick start — they were *not* re-tested in the Linux review environment and
are not guarantees):

- Standalone run with [`uv`](https://docs.astral.sh/uv/): `chmod +x studio.py`
  then `uv run studio.py` (dependencies resolve via inline PEP 723 metadata).
- Split-source run with `uv run app.py`, or directly:
  `STUDIO_HOST=127.0.0.1 STUDIO_PORT=3000 python3 app.py` (the code defaults
  are host `127.0.0.1`, port `3000`, `app.py` L224/L227).
- First-run UX: open the **Start** tab (onboarding preflight reads local
  configuration only — code path `onboarding_preflight`, integrated).

## Verified usage

- **Run the test suite:** `.venv/bin/python -m pytest -q` → 881 passed,
  1 skipped, 1 warning.
- **Start a disposable authenticated instance (verified):**
  `.venv/bin/python scripts/smoke_local.py` starts `app.py` on a free loopback
  port in a temporary directory and exercises the auth/security path.
  `--standalone` does the same for `studio.py`.
- **Authentication model (verified in code and by smoke tests):** `GET /healthz`
  is the only unauthenticated route; everything under `/api/*` and `/v1/*`
  requires the bearer token. Default token file:
  `~/.config/m5-agent-studio/api-token` (`app.py` L267); override with
  `STUDIO_API_TOKEN_FILE`. Non-loopback binding requires an explicit
  `STUDIO_API_TOKEN` of at least 32 characters, and
  `STUDIO_AUTH_DISABLED=1` is accepted only on loopback (`app.py` L237-239).
- **History and privacy endpoints (verified in code):** `GET /api/history/export`,
  `POST /api/history/retention/run` backed by the integrated
  `history_privacy` module.
- **Durable harness task events (verified by tests):**
  `GET /api/harness/sessions/{session_id}/events?cursor=&limit=`,
  `GET /api/harness/sessions/{session_id}/events/stream` (SSE), and
  `GET /api/harness/sessions/{session_id}/state`. All require the bearer token
  like every other `/api/*` route. Behaviour, guarantees, and the single-process
  limitation are in [`docs/task-events.md`](docs/task-events.md).
- **Workspace baselines and selective CAS snapshots (verified by tests):**
  `GET /api/harness/sessions/{session_id}/snapshots`,
  `POST /api/harness/sessions/{session_id}/snapshots`, and
  `POST /api/harness/sessions/{session_id}/snapshots/{snapshot_id}/restore`
  (accepting `"baseline"`, `"latest"`, or an explicit snapshot ID).
- **OpenAI-compatible gateway:** `/v1/models`, `/v1/chat/completions`
  (routes at `app.py` L3275/L3363).
- The previous README's full configuration table (environment variables),
  HTTP/SSE endpoint list, UI shortcuts, tool catalog, and database schema
  reference remain accurate as code descriptions and are documented in
  [`docs/configuration-contracts.md`](docs/configuration-contracts.md),
  [`docs/architecture.md`](docs/architecture.md) and the git history of this
  file; they were spot-checked (settings parsing, route list) but not every
  row is re-verified here.

## Architecture observed in code

Observed structure (see [`docs/architecture.md`](docs/architecture.md) and the
generated [`docs/subsystem-dependency-inventory.md`](docs/subsystem-dependency-inventory.md)):

- **Backend:** one FastAPI process — `app.py` plus 44 small contract modules
  listed in `pyproject.toml`. `studio.py` is a **generated** standalone
  artifact (`scripts/build_studio.py` inlines 13 sibling modules —
  `runtime_persistence`, `side_effect_ledger`, `execution_budget`,
  `workflow_checkpoints`, `workflow_state`, `mcp_client`, `context_store`,
  `onboarding_preflight`, `failure_injection`, `execution_spine`, `task_events`,
  `sandbox_boundary`, and `workspace_snapshots` — plus `history_privacy` and
  `workflow_orchestration`, and embeds `static/index.html`); never hand-edit it.
- **Frontend:** `static/index.html`, vanilla HTML/CSS/JS, no build step.
- **Storage:** SQLite (`studio_history.db`, `SCHEMA_VERSION = 3`) in WAL mode
  with FTS5 tables (`opencontext_fts`, `ctx_vault_fts`) for BM25 context search
  and `runtime_persistence.py` enforcing short `BEGIN IMMEDIATE` write
  transactions, bounded busy retries, and fail-closed persistence errors, plus
  per-session `.harnesses/{id}.jsonl` task-event journals and `.harnesses/snapshots/`
  (`0600` SHA-256 content-addressed blobs and integrity-checked baseline manifests).
- **Request path:** `security_boundary` middleware (rate limit, origin checks,
  body limits; `app.py` L2348) → 74 registered routes → either model streaming
  (`stream_any_model`), the harness tool loop
  (`_execute_harness_tool` L7679 → `_run_authorized_tool` L7592 →
  `_execute_harness_tool_inner` L7182), or MCP calls (`_mcp_http_rpc` L4623 →
  `mcp_client.adaptive_call`; optional stdio client `_mcp_stdio_rpc` L4655).
- **Code execution:** tool commands (`run_bash`, `ctx_execute_python`,
  `ctx_run_block`) funnel through `_execute_limited` (L492) →
  `_start_code_process` (L458) with three modes: `disabled` (default),
  `sandbox` (pre-pulled container, networking disabled), `host` (requires
  `STUDIO_ALLOW_HOST_EXECUTION=1`). Harness agent sessions edit inside a git
  worktree (`_create_harness_worktree`, L6400) and are applied back explicitly
  (`_apply_worktree_to_main`, L7738).
- **Outbound network:** URL/IP pinning and redirect rejection
  (`_PinnedNetworkBackend` L723, `_validate_outbound_url` L688); private URLs
  denied unless `STUDIO_ALLOW_PRIVATE_URLS=1`.

### Critical invariants

Operational constraints preserved from the prior README and still enforced by
the current code — any engineer or agent extending this codebase must keep
them (spot-verified where noted):

1. **Never call oMLX `POST /admin/api/server/restart`**: it orphans child
   workers (`omlx#2701`). Unload via `POST /v1/models/{id}/unload`, or restart
   oMLX cleanly via `killall oMLX && open -a oMLX`.
2. **Never enable MTP on `Qwen3.6-35B-REAP-19B`**: `mtp_enabled: true` triggers
   `HTTP 409`. Keep MTP strictly on `Qwen3.8-27B-MLX-oQ4e-mtp`.
3. **`Qwen3.8-27B` `reasoning_effort` strict enum**: only `'low'`, `'medium'`,
   `'xhigh'` validate; cap `thinking_budget` so thinking tokens cannot starve
   output on 24 GB KV budgets.
4. **Anthropic extended thinking**: with `thinking: {"type": "enabled"}`,
   Anthropic requires `temperature: 1.0` and `budget_tokens >= 1024`.
5. **Guard model unloads with `is_local_omlx_model(model)`** (code at `app.py`
   L1443): cloud models and failover combos must never hit local unload
   endpoints.
6. **MCP Streamable HTTP `Accept` header**: always send
   `"Accept": "application/json, text/event-stream"` on MCP `POST`s —
   `2026-07-28` servers reject JSON-only headers with `HTTP 406`.
7. **Harness tool-turn counting**: match
   `m.get("role") == "user" and '<tool_response name="' in content` — never
   bare `<tool_response` across system messages (the system prompt contains an
   example).
8. **Async skill context unpacking**: `build_skills_context(...)` is `async`
   and returns `(skill_block, skill_summaries)` — always await and unpack both.
9. **Node `< 22` skill-installer fallback**: `npx skills@latest` crashes on
   Node `< 22`; preserve the pure-Python `git clone --depth 1` fallback in
   `/api/skills/install`.
10. **Preserve secret-path and UI accessibility rules**: resolve candidates
    before reading/indexing, reject symlinks that escape the workspace, and
    block `.env`, `.secrets`, `*.pem`, `*.key`, and other guarded names in
    resolved read/write/grep paths (allow `.env.example`); keep structural
    emojis out of `static/index.html` (use `UI_ICONS` inline SVGs), and use
    `showToast(...)` instead of `alert(...)`.
11. **Dual tool-call dialect support**: Qwen XML
    (`<tool_call><function=NAME>...`) and JSON (`{"name": ..., "arguments": ...}`)
    dialects — plus tool aliases (`glob_files`, `grep`, `read`, `bash`) — are
    parsed by `extract_tool_calls()` (`app.py` L7101) and
    `_normalize_tool_call()` (L7045) in both the Harness and Direct Chat.

## Tests and measured results

Full log with exit codes: [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md).
Summary (2026-10-30, Linux x86_64 container, 2 vCPU, ~4 GB RAM, Python 3.11.2):

| Check | Result |
|---|---|
| `pytest -q` | 881 passed, 1 skipped (opt-in live container test), 1 warning |
| `ruff check .` | clean |
| `mypy` | clean (typed contract modules) |
| `check_frontend_syntax.py` (Node) | pass |
| `smoke_local.py` / `--standalone` | pass (auth/security path verified) |
| `verify_baseline.py` | pass (`7904892d759bb66b817bb17b6f4f377874bd6e25`) after `git fetch --unshallow` |
| governance / security-signoff / docs / ADR / screenshots / demo-tour checks | pass |
| generated artifacts drift check | pass (byte-for-byte) |
| `pip-audit` | **exit 1** — 21 advisories in 2 packages: `pip` 23.0.1 and `setuptools` 66.1.1 (venv bootstrap tooling only). Declared runtime dependencies (`fastapi`, `uvicorn`, `httpx`, `httpcore`, `keyring`) have no reported advisories. |
| `scripts/isolation_smoke.py` | 16/16 observable checks pass; summary `isolation_status: unqualified` (no container runtime in the evidence environment — live container probes blocked) |

**Not measured (explicitly blocked, no numbers claimed):** peak unified-memory
and swap on a 24 GB Apple Silicon Mac; a resident-oMLX coding-task run;
SQLite latency under FTS5 load; live container-isolation behavior (no
Docker/Podman in the review environment). No benchmark or memory figure in
this README is a guarantee; treat all throughput statements anywhere in this
repository as environment-specific.

## Security and known limitations

**Deny-by-default controls verified in code:** execution mode defaults to
`disabled`; host execution requires `STUDIO_ALLOW_HOST_EXECUTION=1`;
MCP stdio launch requires `STUDIO_ALLOW_MCP_STDIO=1`; skill-code execution
requires `STUDIO_ALLOW_SKILL_CODE=1`; private-URL access requires
`STUDIO_ALLOW_PRIVATE_URLS=1`; policy-evaluation errors deny (fail closed).

**Known limitations and open containment work** (details:
[`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) §5):

1. **Skill-code execution is contained (closed 2026-10-30 by `M5-P0-03`).**
   The two skill-script subprocess paths (`build_skills_context`,
   `_fetch_online_inspiration`) now run only through `_run_skill_script`
   (`app.py`), which requires the `STUDIO_ALLOW_SKILL_CODE` opt-in **and** a
   live execution mode: `sandbox` routes through the container boundary,
   `host` requires `STUDIO_ALLOW_HOST_EXECUTION=1`, and `disabled` (default)
   never executes skill code — with **no silent host fallback** (the skill's
   static text still applies). Nine regression tests pin this contract
   (`tests/test_skill_execution_containment.py`). The earlier ungated
   `ui-ux-pro-max` path is no longer reachable without both opt-ins.
2. **Host mode is unrestricted by design.** `STUDIO_EXECUTION_MODE=host`
   (with `STUDIO_ALLOW_HOST_EXECUTION=1`) runs shell/Python as the Studio OS
   user with no filesystem/network/credential isolation (also stated in
   [`SECURITY.md`](SECURITY.md)). Autonomous execution must fail closed when
   isolation is unqualified; it must never silently fall back to host mode.
3. **A git worktree is not an operating-system sandbox.** Worktree isolation
   protects the main checkout from agent edits; it does not confine processes.
4. **`side_effect_ledger.py` (`M5-P1-03`) and `sandbox_boundary.confined_path`
   (`M5-P2-01`) are integrated, while container-command construction in `app.py`
   still uses inline helpers.** `DurableActionLedger` commits durable
   `runtime_actions` intent rows before admitted consequential actions,
   transitions missing results and ambiguous MCP/timeout outcomes to `uncertain`
   (never `failed`), forwards `Idempotency-Key` on outbound HTTP MCP
   `tools/call`, and refuses blind replay of `non_idempotent` or unverified
   `uncertain` writes; `_resolve_confined_path` and `workspace_snapshots.py`
   enforce `sandbox_boundary.confined_path` against traversal and symlink
   escapes. However, globally exactly-once external effects still cannot be
   guaranteed when a third-party MCP server ignores `Idempotency-Key`,
   `sandbox_boundary.sandbox_command` is not yet unified with `app.py`'s inline
   `_sandbox_command` (`M5-P3-01`–`M5-P3-03`), and not all subprocess descendants
   can be guaranteed terminable.
5. **Live container isolation is unqualified (smoke test 2026-10-30, `M5-P0-04`).**
   No Docker/Podman exists in the evidence environment; the live probes were
   **attempted and failed closed** (`RuntimeError: Sandbox runtime 'docker' is
   not installed or not on PATH`) with **no silent host fallback**. Everything
   observable without a container passes — skill-code gate denials, the
   host-bypass denial, path-confinement denials, and the sandbox invocation
   invariants (`--network=none`, read-only root, cap drop, single workspace
   mount) as actually constructed — 16/16 checks in
   [`scripts/isolation_smoke.py`](scripts/isolation_smoke.py). Container-level
   guarantees stay **unproven** until `scripts/isolation_smoke.py --live`
   passes on a machine with a working runtime; dependent execution remains
   blocked (fail closed). Evidence:
   [`docs/m5-2-0-isolation-smoke-evidence.md`](docs/m5-2-0-isolation-smoke-evidence.md).
6. **`pip-audit` reports 21 advisories** in venv bootstrap packages
   (`pip` 23.0.1, `setuptools` 66.1.1); runtime dependencies are clean.
7. **Independent security review (`AS-031`) is `completed`, not `verified`** —
   release remains blocked until an authorized reviewer signs the record
   ([`docs/security-review-signoff.md`](docs/security-review-signoff.md)).
   Never self-certify that gate.
8. **The execution boundary covers tool, smart-block, skill-script, and MCP
   invocation, not every effectful path (since `M5-P0-06`; SQLite discipline
   tightened in `M5-P1-01`; authoritative action admission wired in
   `M5-P1-02`; durable `runtime_actions` intent/result ledger wired in
   `M5-P1-03`; worker leases/fencing wired in `M5-P1-04`; durable task state
   machine, checkpoints, and budgets wired in `M5-P1-05`).** The maintained
   known-bypass inventory
   [`docs/m5-2-0-bypass-inventory.md`](docs/m5-2-0-bypass-inventory.md) lists
   what routes through the boundary, what is gated elsewhere (all SQLite reads
   and writes route through `runtime_persistence`), what is read-only, and what
   is still outside it (worktree git effects, `.harnesses/*.jsonl` sidecar
   files, unledgered filesystem writes). Completing coverage is follow-up work
   (`M5-P1-06`…`M5-P1-07`, `M5-P2-01`…`M5-P2-03`); partial coverage is never
   advertised as complete.

## Current implementation status

The owner confirmed on 2026-10-30 that
[`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json)
is the **authoritative execution ledger**; the mapping to the AS program is
recorded in
[`docs/m5-2-0-roadmap-reconciliation.md`](docs/m5-2-0-roadmap-reconciliation.md)
and the AS roadmap is retained unmodified (no history deleted). Multiple
implementation agents may work in parallel under the claim/review/merge
protocol in [`M5_2_0_AGENT_PROMPT.md`](M5_2_0_AGENT_PROMPT.md).

- **M5 2.0 autonomous-runtime program** —
  [`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json):
  34 stories across 7 phases. **Done with evidence: `M5-P0-01`…`M5-P0-07`
  (phase P0 100%), `M5-P1-01`** (SQLite Schema v3 migration and persistence
  discipline), **`M5-P1-02`** (authoritative action admission gate, task/epoch
  checks, action-class separation, and direct-executor anti-bypass),
  **`M5-P1-03`** (durable `runtime_actions` intent/result ledger integration,
  4-state `never_started`/`in_flight`/`committed`/`uncertain` separation,
  missing-result-to-`uncertain` reconciliation, and action-type replay
  classification), **`M5-P1-04`** (single-owner worker leases, monotonic epoch
  fencing, and pre-takeover reconciliation), **`M5-P1-05`** (durable root
  task and child milestone state machine, safe-boundary pause/resume/cancel,
  hash-verified checkpoints with evidence refs, incomplete-transition recovery,
  and persistent task/recovery budgets), **`M5-P1-06`** (single-agent
  verified vertical slice with authoritative patch + test verification gates,
  action-boundary crash injection, user-work preservation, duplicate-effect
  prevention, and false-completion rejection), **`M5-P1-07`** (phase P1 100% —
  durable cursor-addressed task-event stream, SSE replay/gap resync, and
  approval reconciliation), **`M5-P2-01`** (pre-edit Git baseline capture,
  selective SHA-256 CAS user snapshots, ignored/sensitive file preservation
  without indiscriminate copying, disk-full safe pause, and verified
  non-destructive restore), **and `M5-P2-02`** (version-aware managed edits:
  explicit/observed/last-written content expectations, compare-and-swap atomic
  replacement so a concurrent user edit conflicts instead of being overwritten,
  structured `unauthorized_path` refusals, and one durable edit transaction per
  applied edit), **and `M5-P2-03`** (durable multi-file edit batches, per-path
  inverse-patch rollback with version checks and a three-way merge around later
  user edits, refusal with an explicit `unresolved` state, action reconciliation
  before rollback, and resumable interrupted recovery), **and `M5-P2-04`**
  (revision-anchored verification evidence, staged targeted-then-broader checks,
  baseline-aware pre-existing/environmental/introduced failure classification,
  denial-is-not-a-result, and declared acceptance coverage that fails closed),
  **and `M5-P2-05`** (durable failure memory with evidence fingerprints,
  stagnation/oscillation detection, bounded retry/replan/rollback budgets that
  survive a crash, and recovery that requires changed evidence or conditions;
  phase P2 100%). `M5-P3-01` and `M5-P4-01` are the two stories still open: both
  now have their code and their evidence tooling (`scripts/qualify_environment.py`,
  [`docs/qualification-suite.md`](docs/qualification-suite.md)), and both stay
  `blocked` until a qualified record exists — a live Docker run for the sandbox and
  a 24 GB pressure measurement for the model story. `P4-01`'s serving-path half is
  implemented and tested ([`docs/model-admission.md`](docs/model-admission.md)).
  Planned work is not implemented product behavior.
- **AS program (Adaptive Runtime)** —
  [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json)
  with dashboard [`docs/product-roadmap-status.md`](docs/product-roadmap-status.md):
  63 stories (`AS-001`…`AS-063`); status vocabulary and update rules live in
  [`AGENTS.md`](AGENTS.md). Its per-module tests are the reason "tested" is
  high while "integrated" is low — see the audit before reading status as
  capability.
- Security-remediation depth beyond story status:
  [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Development and roadmap link

- Contributor/agent workflow, commit policy, and verification sequence:
  [`AGENTS.md`](AGENTS.md), [`CONTRIBUTING.md`](CONTRIBUTING.md),
  [`BASELINE.md`](BASELINE.md) (pinned baseline contract).
- Reproducible checks: the "verified setup and checks" block above is the
  verified subset of the CI sequence; the full sequence (artifact rebuild,
  supply-chain evidence, release gates) is documented in `AGENTS.md` §5 and
  exercised by CI on Python 3.11/3.12/3.13.
- Roadmaps and evidence:
  [`product/M5_2_0_Autonomous_Roadmap.json`](product/M5_2_0_Autonomous_Roadmap.json) ·
  [`product/Adaptive_Runtime_Product_Roadmap_v2.json`](product/Adaptive_Runtime_Product_Roadmap_v2.json) ·
  [`docs/m5-2-0-roadmap-reconciliation.md`](docs/m5-2-0-roadmap-reconciliation.md) ·
  [`docs/m5-2-0-integration-audit.md`](docs/m5-2-0-integration-audit.md) ·
  [`docs/m5-2-0-baseline-evidence.md`](docs/m5-2-0-baseline-evidence.md)
- Superseded input draft retained for provenance:
  [`README_GROUND_TRUTH_DRAFT.md`](README_GROUND_TRUTH_DRAFT.md).
- **README maintenance rule:** after every completed roadmap story, refresh
  this README using only observed and tested behavior; record regressions and
  limitations promptly; never promote an intended feature, a passing mock, or
  an untested sandbox claim into a production guarantee.

## License

MIT — see [`LICENSE`](LICENSE).
