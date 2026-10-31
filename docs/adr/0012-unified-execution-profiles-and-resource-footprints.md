---
id: ADR-0012
title: unified-execution-profiles-and-resource-footprints
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0012: Unified execution profiles, conservative footprints, and admission barriers

## Context

`M5-P3-02` requires consistent permission and resource rules across managed in-process
tools and subprocess/external operations:

1. **No shell bypass of managed policy:** Managed tools (`read_file`, `write_file`,
   `edit_file`, `grep_search`) already rejected workspace traversal, symlinks, `.git`
   hooks/config, and secret paths through `sandbox_boundary.confined_path` and
   `_check_secret_file`, whereas subprocess surfaces (`run_bash`, `ctx_execute_python`,
   `ctx_run_block`, and `_run_skill_script`) relied on a narrow unquoted token regex
   that missed quoted secret filenames (`cat ".env"`), globs (`.en*`, `*.key`), ANSI-C
   escapes, brace expansions, Python string expressions, `.git/hooks/*`, `.git/config`,
   workspace symlinks, and parent/home escapes (`../`, `~/`, `$HOME`).
2. **Unknown side effects reserve conservative scope:** Managed reads and single-file
   edits know their exact target path before execution, while arbitrary shell commands,
   Python snippets, skill scripts, multi-file rollbacks, MCP calls, and unknown tools
   have unbounded or statically unknowable workspace side effects.
3. **Conflicting action admission prevented:** `DurableActionBoundary` (`ADR-0002`)
   validated policy, task state, epoch leases, and replay keys, and `resource_admission`
   provided atomic slot/memory leases, but tool actions acquired neither path/workspace
   shared-vs-exclusive barriers nor subprocess/mutation capacity slots during execution.
4. **Post-execution expansion reconciliation:** Even with static inspection, a
   subprocess in `host` or container mode can compute filenames dynamically at runtime
   (for example from byte codes) or expand across multiple workspace files. In addition,
   `app._sandbox_command` duplicated container CLI construction separately from
   `sandbox_boundary.py` (`open_risks[3]`).

## Decision

Introduce `execution_profiles.py` (stdlib-only, inlined into `studio.py`) and wire it
directly into `execution_spine.DurableActionBoundary`, `app._run_authorized_tool`,
`app.authorize_tool_invocation`, `app._run_skill_script`, and `sandbox_boundary.py`:

- **Verified `CapabilityDescriptor` derived solely from runtime-owned metadata:**
  `resolve_capability_descriptor` classifies every tool's `action_class`,
  `execution_surface` (`managed`, `subprocess`, `external`), honest `isolation_posture`
  (`in_process_confined`, `container_qualified`, `container_hardened_unverified`,
  `host_opt_in_unsandboxed`, or `disabled`), `verified_isolation` flag, filesystem /
  network / process capabilities, secret and VCS-hook posture (`forbidden`), and
  `side_effect_certainty` (`none`, `exact`, `bounded_read`, `unknown_conservative`).
  Untrusted model-supplied hints inside tool arguments are ignored, and container mode
  never claims `verified_isolation=True` unless `container_qualified=True` has been
  established by qualification evidence (`ADR-0010`).
- **Unified pre-admission policy parity (`evaluate_unified_policy`):**
  Managed path tools, `run_bash`, `ctx_execute_python`, `ctx_run_block`, and
  `skill_execute_script` are evaluated against the same secret-file, targeted-glob,
  protected VCS control/hook (`.git/hooks/*`, `.git/config`, dangerous `git config`),
  workspace symlink, and workspace confinement (`..`, `~`, `$HOME`, outside absolute
  path) rules using quote-aware shell tokenization, ANSI-C/brace expansion, and Python
  AST constant folding before any subprocess is spawned.
- **Conservative `ResourceFootprint` and atomic `ResourceBarrier` admission:**
  `derive_execution_profile` assigns narrow `exact_paths` footprints (`workspace:{ws}`
  `shared` + `path:{ws}:{rel}` `shared`/`exclusive`) to single-file managed reads and
  writes, and `conservative_workspace` / `external_and_workspace` footprints
  (`workspace:{ws}` `exclusive` + conservative memory/subprocess/mutation slots) to all
  arbitrary shell, Python, skill-script, rollback, MCP, and unregistered tools.
  `UnifiedExecutionController` coordinates shared/exclusive barriers atomically with
  `resource_admission.ResourceAdmissionScheduler` leases across the full action lifetime
  in `DurableActionBoundary` and releases them in `finally` on completion, error,
  cancellation, or injected boundary fault. Conflicting concurrent admissions fail
  closed (`error_code="conflicting_action"`, `conflict_denied=True`) without blocking on
  locks or poisoning idempotent retries after the holder finishes.
- **Post-execution observation and reconciliation (`observe_and_reconcile`):**
  Under the held barrier, the coordinator snapshots non-secret workspace metadata
  before and after execution:
  - Valid mutations under a conservative footprint reconcile cleanly as
    `reconciled_expansion` into `$HARNESS_STORE_DIR/footprints/{session_id}.jsonl`.
  - Newly planted secret files, `.git/hooks/*` executables, or workspace-escaping
    symlinks (including runtime-obfuscated creations) are unlinked via `O_NOFOLLOW`
    quarantine and fail closed (`error_code="footprint_policy_violation"`).
  - Unauthorized mutations of pre-existing protected files or undeclared mutations by an
    `exact_paths` tool preserve existing user data, fail closed, and mark the action
    `uncertain=True` so `IntentLedger` blocks subsequent consequential actions on the
    task until operator reconciliation.
- **Single container CLI builder (`sandbox_boundary.build_container_invocation`):**
  `app._sandbox_command` delegates hardened container flag construction to
  `sandbox_boundary.build_container_invocation`, eliminating drift between `app.py`,
  `sandbox_boundary.py`, and `scripts/qualify_environment.py` (`p3.product.argv_parity`).

## Alternatives

- **Rely on container isolation alone instead of pre-admission shell/AST inspection.**
  Rejected: container isolation is currently unqualified in this environment (`M5-P3-01`
  blocked), optional `host` execution has no OS container boundary, and even inside a
  mounted `/workspace` container `.env` and `.git/hooks/*` live inside the bind mount
  unless policy blocks and reconciles them.
- **Queue conflicting tool actions on per-path/workspace asyncio locks.**
  Rejected: `M5-P3-02` explicitly requires preventing conflicting action admission rather
  than silently serializing stale edits or hiding contention behind unbounded waits.
- **Treat seemingly simple shell commands (`echo`, `ls`, `pytest`) as narrow or
  read-only footprints.**
  Rejected: shell commands execute arbitrary binaries, `conftest.py` hooks, and
  subshells whose side effects cannot be proven statically; reserving conservative
  exclusive workspace scope preserves soundness and satisfies Criterion 2.
- **Delete modified pre-existing protected files during post-execution quarantine.**
  Rejected: unlinking a pre-existing user `.env` or `.git/config` would destroy user
  state. Quarantining only newly planted artifacts while marking pre-existing mutations
  `uncertain=True` halts follow-up consequential actions without data loss.

## Tradeoffs

- `UnifiedExecutionController` enforces barriers and slot budgets within a single Studio
  process, matching the existing single-process worker-lease, event-stream, snapshot,
  and edit-journal model (`ADR-0005`–`ADR-0011`); cross-process filesystem locks remain
  outside scope.
- Static shell and Python AST policy inspection cannot decide arbitrary Turing-complete
  programs statically; runtime byte-code obfuscation in `host` mode can still touch a
  file momentarily before `observe_and_reconcile` quarantines planted artifacts or halts
  the task as `uncertain`. True kernel-level pre-execution isolation continues to depend
  on operator qualification of a live container runtime (`M5-P3-01`).
- Conservative `workspace:{ws}` `exclusive` footprints reject even read-only `read_file`
  calls on the same workspace while a `run_bash` command is running, trading maximum
  parallelism during shell runs for deterministic conflict prevention and consistent
  pre/post workspace diffs.
- Pre/post workspace metadata scans add bounded directory traversal overhead (capped at
  `20,000` entries while skipping heavy dependency caches and `.git/objects`), marking
  truncated scans explicitly rather than scanning unbounded trees.

## Review Status

Not reviewed. Accepted as the architectural record for `M5-P3-02`; independent human
review has not yet been performed.
