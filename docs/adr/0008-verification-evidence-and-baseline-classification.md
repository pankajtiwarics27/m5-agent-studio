---
id: ADR-0008
title: verification-evidence-and-baseline-classification
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0008: Verification evidence tied to a source revision

## Context

`M5-P1-06` completed the first version of the coding completion gate: a persisted
`TaskVerificationSpec` records which files must change, which user files must be
preserved, and which command proves the change, and
`DurableTaskStateMachine.verify_task_completion` runs that command with
`subprocess.run(..., shell=False)` before it will mark a task complete.

Three holes remained, and each one had a plausible way to look green:

1. **Revision drift.** The command ran at some point during completion. If the
   workspace changed between the last managed edit and the command — a concurrent
   user edit, a background job, a second agent in the same worktree — the exit code
   describes a tree that no longer exists. Nothing recorded which revision the
   result belonged to.
2. **Baseline misattribution.** A repository with a known-broken test fails the
   authoritative command for reasons the task did not cause. The gate had two bad
   options: reject an otherwise correct change, or let the operator paste a
   "these failures are fine" flag with no record of *which* failures those were.
3. **Refusal read as success.** When execution is disabled (no live runtime, `plan`
   policy, docker unavailable), the runtime prints an explanatory message. A
   command wrapper that surfaces that text and exits 0 produces a transcript that
   looks like a passing test run.

The story's acceptance criteria are exactly these: the result must be tied to a
source revision, a pre-existing failure must not be misattributed, and there must
be no success claim on sandbox denial.

## Decision

Verification evidence is a first-class record, not a byproduct of the gate.

1. **One evidence engine.** `verification_evidence.py` (standard library only,
   inlined into `studio.py`) owns revision capture, environment capture, command
   execution, failure classification, and denial detection. `workflow_state.py`
   consumes it; it does not re-implement shell handling or output parsing.
2. **The revision is a digest, not a label.** `git rev-parse HEAD` plus the bounded
   `git status --porcelain` path list (cache/coverage artifacts filtered out) plus
   the SHA-256 of each declared file. The digest is captured before and after the
   command, and both are recorded. `require_revision_stability` (default true)
   rejects a run whose revision moved; the opt-out is explicit and still records
   both digests.
3. **Classification is honest, tolerance is opt-in.** Pre-existing, environmental
   and introduced failures are always labelled from the transcript. Only
   `allow_pre_existing_failures=True` lets a run whose failures are *all*
   pre-existing verify. Anything unparsed on a non-zero exit becomes `introduced`,
   so the failure mode is "blocks completion", never "passes quietly".
4. **Denial beats exit code.** A refusal marker in the output forces the `denied`
   classification, `passed` is false, and completion fails with
   `verification_test_denied` even at exit code 0.
5. **Stages and coverage are declared, not guessed.** An optional
   `targeted_test_command` runs first and stops the run when it fails. An optional
   `acceptance_coverage` maps criteria to test ids, and a criterion is covered only
   when its test id appears in the transcript and is not among the failures;
   uncovered criteria fail completion.

## Alternatives

* **Compare `git status` before/after and store a text diff.** Rejected: diffs of an
  untracked or non-Git workspace are unbounded, and the completion gate needs a
  cheap equality check, not a patch. The full `git diff` hash already exists in the
  `patch_verification` evidence ref from `M5-P1-06`.
* **Re-run the failing tests at the baseline commit (stash, checkout, run).**
  Rejected as a default: it mutates the user's workspace, depends on a clean
  checkout, and can silently destroy uncommitted work — the exact failure class
  `M5-P2-01` and `M5-P2-03` exist to prevent. Baseline knowledge stays
  caller-supplied and recorded.
* **Treat any non-zero exit as "environmental" when the output mentions a missing
  module.** Rejected: a missing module is frequently the change's own bug (a new
  import that was never added to `pyproject.toml`). The marker list is narrow
  (module/command not found, `ENOSPC`, DNS) and the classification is recorded, so
  a reviewer can see the judgement rather than inherit a hidden one.
* **Snapshot the whole workspace before verification.** Rejected: `M5-P2-01` already
  captures pre-edit baselines; copying the tree for every completion attempt is
  disproportionate and would duplicate the snapshot store.

## Tradeoffs

* **Evidence grows the report.** A report now carries a revision record, an
  environment snapshot, classified failures and coverage entries. Serialization is
  bounded (output capped at 64 KiB, excerpt at 500 characters, failures at 50), and
  the preview endpoint computes it on demand instead of storing it in the session
  row.
* **The gate got stricter.** A workspace that changes during the run now fails
  where it previously could pass. That is the point of the story, but it does mean
  long-running suites in a shared worktree need `require_revision_stability=False`
  or a checkout nobody else writes to.
* **Coverage can fail closed on formatting.** A declared test id that the runner
  does not print (for example `pytest -q` without `-rA`) counts as uncovered. The
  fix is in the spec, not in a looser matcher: declare ids the runner prints, or
  widen the runner flags.
* **Transcript parsing is format-bound.** pytest and unittest formats are parsed;
  other runners yield one unparsed failure record, which blocks completion. Adding
  a runner means adding a parser and a test with a real transcript.

## Review Status

Not reviewed. Implemented and tested in this repository by the
`M5-P2-04` session; the security review gate in
[`../security-review-signoff.md`](../security-review-signoff.md) remains unsigned,
so no security qualification is claimed for this decision.
