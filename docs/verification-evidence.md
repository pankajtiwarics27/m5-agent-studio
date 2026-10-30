# Verification evidence and baseline-aware tests

Status: implemented in `M5-P2-04` (2026-10-30), decision recorded in
[ADR-0008](adr/0008-verification-evidence-and-baseline-classification.md). This
page documents observed behaviour of the shipped code: what a verified completion
now records, how a failure is attributed, and where the guarantees stop.

## Why this exists

`M5-P1-06` made coding-task completion depend on an independent test command
instead of the model's own claim. That gate answers *did the command exit zero*.
It cannot answer the follow-up questions a reviewer asks of the same evidence:

* which source revision produced that result — and could the workspace have moved
  underneath the run;
* is this failing test something the change introduced, or something already
  broken before the task started;
* was the command actually executed, or did the runtime refuse and print a
  message that happens to look like output.

`M5-P2-04` answers those three questions with recorded evidence instead of
inference, and refuses to verify when the answer cannot be tied to a revision.

## What a verified completion records

`workflow_state.TaskVerificationReport` (returned by
`DurableTaskStateMachine.verify_task_completion`, and persisted with
`complete_task`) carries:

| Field | Meaning |
|---|---|
| `source_revision` | canonical digest of the revision the authoritative command ran against |
| `revision` | the full revision record: `git_head`, bounded `git_dirty_paths`, per-file SHA-256s, `captured_at` |
| `environment` | `python` version, `platform`, `machine`, workspace `cwd`, `execution_surface` |
| `test_evidence` | transcript hash, exit code, bounded excerpt, denial marker, classified failures, `revision_changed` |
| `staged_test_evidence` | `{"targeted": ...}` when a cheaper first stage ran |
| `acceptance_coverage` | per declared criterion: the test ids, whether each was observed passing, and the missing ids |
| `failure_classification` | `passed`, `pre_existing_only`, `environment_blocked`, `introduced`, `denied`, `unclassified` |
| `denied` | true when the run was refused rather than executed |

Each run also emits an `EvidenceRef`: `test_verification` for the authoritative
command and `targeted_test_verification` for the first stage. Their summaries carry
the exit code, the classification, and the revision prefix, so a checkpoint points
at the same revision the report claims.

## The revision record

`verification_evidence.capture_source_revision` combines three observations into
one SHA-256 digest:

1. `git rev-parse HEAD` (empty string outside a Git checkout — a workspace without
   Git is still verifiable, it just cannot be pinned to a commit);
2. `git status --porcelain` paths, rendered as `PORCELAIN  path` lines, bounded to
   200 characters per path and 100 paths. Test-tool artifacts (`.pytest_cache/`,
   `__pycache__/`, `.mypy_cache/`, `.ruff_cache/`, `.tox/`, `.nox/`, `.venv/`,
   `node_modules/`, `htmlcov/`, `.harnesses/`, and `*.pyc`/`*.pyo`/`.coverage*`)
   are filtered out, so a passing run that writes a cache does not look like a
   source change;
3. the SHA-256 of every file the task declared as modified, or `null` when the
   file is absent. Files must be regular, non-symlink, confined to the workspace,
   NUL-free relative paths, at most 8 MiB each.

The revision is captured **before** and **after** the command. `revision_changed`
is true when the two digests differ, and with `require_revision_stability` (the
default) that is a hard failure: a result produced across a change cannot be tied
to the revision it claims to verify. `require_revision_stability=False` is the
explicit opt-out, and the report still records both digests.

`output_sha256` hashes the command, the exit code, the bounded output (64 KiB cap),
and both revision digests, so the transcript hash changes if the revision does.

## Failure classification

`verification_evidence.classify_test_output` parses pytest (`FAILED <id> - detail`)
and unittest (`FAIL: <test> (<where>)`) failure lines and classifies each one:

| Classification | When | Completion effect |
|---|---|---|
| `environmental` | the failure text carries an environment marker (missing module, missing command, `ENOSPC`, DNS failure) | fails as `verification_test_environment_blocked` |
| `pre_existing` | the test id matches one recorded in `pre_existing_failures` | fails as `verification_test_failed_pre_existing`, **unless** `allow_pre_existing_failures=True` |
| `introduced` | anything else | fails as `verification_test_failed_introduced` |

Pre-existing failures are always *classified* honestly; tolerating them is a
separate, explicit opt-in (`allow_pre_existing_failures`, default `false`). The
opt-in only helps when **every** failure is pre-existing — one introduced failure
in the same run still blocks completion. Output with a non-zero exit that contains
no parseable failure line becomes a single `introduced` record, so unparsed output
is never mistaken for a clean run.

## Denial is not a result

`detect_denial` scans the output for runtime refusal markers (`code execution is
disabled`, ``denied by `plan` ``, `direct executor bypass blocked`, `requires an
explicit approval in standard policy`, `external mcp tools are disabled`, `sandbox
runtime unavailable`, `cannot connect to the docker daemon`, `... podman socket`).
A match forces classification `denied`, `passed` is false, and completion fails as
`verification_test_denied` — even when the process exited 0. A refusal that prints
success-like text is exactly the case this guards.

## Staged checks

`TaskVerificationSpec.targeted_test_command` (optional) runs first, against the
same revision files:

1. denial, timeout, start failure, or a revision change stops immediately;
2. a non-zero exit stops the run with `verification_targeted_test_failed` and the
   broader command is **not** executed (verified by test: the broader command's
   side-effect file is absent);
3. otherwise the broader `test_command` runs and its evidence is the one that
   decides completion.

Both stages are recorded, with separate `EvidenceRef`s, so the cheap check that
failed is still visible in the checkpoint.

## Acceptance coverage

`TaskVerificationSpec.acceptance_coverage` accepts `(criterion, test_ids)` pairs.
Coverage is derived from the transcript, not from the model's summary: a declared
test id is covered only when it appears verbatim in the bounded output **and** is
not among the classified failures. When any declared criterion is uncovered,
completion fails with `verification_acceptance_coverage_missing` and the detail
names the criterion and the missing ids.

This is a transcript-level check. It records that the named test was observed
passing; it does not prove the test exercises the criterion. Declaring criteria
whose tests never print their ids in the selected output mode therefore fails
closed rather than passing silently.

## API surface

`POST /api/harness/run` (and the SDLC/GSD entry points that share the completion
gate) accept the contract under `verification`:

```json
{
  "require_managed_edit": true,
  "required_modified_files": ["pkg/calc.py"],
  "test_command": ["python", "-m", "pytest", "-q"],
  "targeted_test_command": ["python", "-m", "pytest", "-q", "tests/test_calc.py"],
  "pre_existing_failures": ["tests/test_legacy.py::test_known_broken"],
  "allow_pre_existing_failures": false,
  "require_revision_stability": true,
  "acceptance_coverage": [
    {"criterion": "discount rounds half up", "test_ids": ["tests/test_calc.py::test_discount"]}
  ]
}
```

`GET /api/harness/sessions/{session_id}/task` also returns a read-only
`verification_evidence` preview computed from the session worktree: schema version,
environment snapshot, current revision digest and file digests, plus the
`revision_required_files` / `revision_missing_files` lists. It is `null` when the
revision cannot be captured (for example the worktree is gone), and it never
executes the verification command.

## Failure codes

| Code | Cause |
|---|---|
| `revision_capture_failed` | the workspace revision could not be recorded at all |
| `verification_test_denied` | the runtime refused to execute (either stage) |
| `verification_test_timeout` | the command exceeded `timeout_s` |
| `verification_test_exec_error` | the command could not be started |
| `verification_targeted_test_exec_error` | the first stage could not be recorded or started |
| `verification_targeted_test_failed` | the first stage failed; the broader stage was skipped |
| `workspace_changed_during_verification` | the revision moved while the command ran |
| `verification_test_environment_blocked` | every failure was environmental |
| `verification_test_failed_pre_existing` | pre-existing failures without the opt-in |
| `verification_test_failed_introduced` | at least one introduced failure |
| `verification_test_failed` | non-zero exit, unclassified |
| `verification_test_output_mismatch` | a required output marker was missing |
| `verification_acceptance_coverage_missing` | a declared criterion was not demonstrated |

## Test evidence

`tests/test_verification_evidence.py` pins these behaviours (31 tests): revision
stability and content binding, artifact filtering, absent-file handling, path
refusal, classification of pre-existing/environmental/introduced failures, denial
at exit 0, transcript-to-revision binding, staged fail-fast and pass-through,
acceptance-coverage pass and fail-closed, spec round-trip, API parsing, and that
the standalone `studio.py` bundle inlines the module.

Full suite at the time of writing: `pytest -q` → `903 passed, 1 skipped`
(the skip is the live container-runtime test that needs explicit opt-in).

## Limits

* Classification reads the transcript. A test runner whose output format is not
  pytest- or unittest-shaped yields an unparsed `introduced` record, which blocks
  completion instead of passing.
* `pre_existing_failures` is caller-supplied baseline knowledge. This story records
  and honours it; it does not execute a baseline run of its own before the task.
  Supplying a wrong baseline misattributes failures, so the opt-in to tolerate
  them is off by default.
* Revision capture is bounded (100 dirty paths, 200 characters per path, 8 MiB per
  file). A workspace changing outside those bounds is not observed.
* The environment snapshot records interpreter and platform facts; it is not a
  hermetic environment digest. A different package set with the same interpreter
  is not detected.
* The verification command runs with `shell=False`, a sanitized environment, and a
  timeout, inside the current workspace. It inherits the runtime's isolation
  posture — if execution is denied, the result is `denied`, never "passed".
