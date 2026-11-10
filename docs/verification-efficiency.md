# Verification efficiency — every agent session

## Purpose and authority

Run the smallest sufficient checks during iteration, escalate with impact and risk, and retain mandatory merge/release gates. This policy applies across programs. It changes neither story ownership nor CI/security/approval requirements. Use the approved risk-based CI routing; do not silently remove a required gate or treat a skipped gate as evidence.

## Session procedure

1. Inspect the diff, affected interfaces, acceptance criteria and current required checks. Record a short test plan before implementation. Reassess it when scope changes.
2. Use the existing selector and versioned map (`scripts/run_scoped_tests.py`, `tests/test_scopes.json`); do not invent a second selector. Follow current CLI help. Include uncommitted changes when iterating:

   ```bash
   python scripts/run_scoped_tests.py --base origin/main --head HEAD --worktree
   ```

   Check that the base is appropriate and available. Unknown, shared, high-risk or unmapped changes must retain the selector's full-suite fallback. A hand-picked subset can diagnose a failure but cannot replace that escalation.
3. During fixes, run the failing test or smallest relevant group. Once stable, run affected component/interface and neighbouring regression checks. Add positive, negative and failure/recovery cases appropriate to the change.
4. Before review, execute the required verification sequence in AGENTS.md and the owning program. Local scoped results are not full-suite evidence. Draft PRs may use the mapped scoped gate; known documentation-only PRs use the focused docs/governance/ledger gate. Implementation PRs run the full Python/browser gate when marked ready for review. High-risk, unmapped, workflow, test-infrastructure and runtime product-input changes always fail closed to full. Scheduled runs are additional coverage, not a substitute for the selected PR gate.
5. Record commands, outcomes, durations where available, selection rationale, omissions/blockers, reused evidence and remaining gates. Do not state that an unexecuted, mocked, scheduled or blocked check passed.

## Risk and test layers

- Documentation-only edits: CI validates published links, governance records and the generated roadmap dashboard, then runs `test_docs.py`, `test_adr_process.py`, `test_governance.py` and the AP decision/roadmap contract tests. The PR gate fails closed if changed paths are unavailable or include anything outside the explicit documentation/tracking allowlist; an empty selection is never a silent skip.
- Isolated logic or rendering: affected unit/component tests plus interface and representative UI regressions.
- Authentication, permissions, egress, persistence, shared state, streaming, cancellation, routing, model lifecycle or uncertain impact: broader integration and failure/recovery coverage; fail closed on selection uncertainty.
- Ordinary automated tests remain offline: fakes, temporary stores and disposable loopback servers, as required by AGENTS.md. Never contact live providers/models/MCP endpoints to speed or embellish normal CI.
- Real hardware/model/media qualification is separate, explicitly authorised work with resource and service grants. Preserve it where acceptance requires it; mock success is not physical memory-release, real-media quality or editorial acceptance.

## Safe acceleration

- Profile before optimising: record total time, slow tests, setup/server/fixture overhead, repeat runs, flaky retries and resource contention. For a suitable pytest run, `--durations=20` can expose slow tests without creating a second execution solely for timing.
- Cache dependencies/build inputs only with correct invalidation. Never cache mutable credentials, user data or approval decisions as shared fixtures.
- Reuse fixtures or servers only where isolation and cleanup are proven. Parallelise independent groups within measured CPU/memory/storage limits; serialise resource-heavy qualification when needed. Do not prescribe worker counts from nominal RAM.
- Reuse passing evidence only when relevant source, tests, dependency versions, configuration, commands/inputs and environment match. Record original SHA, current SHA, checksums or a defensible unchanged-scope comparison, original run and reason it remains valid. Do not relabel it as a fresh execution or substitute it for required current-head CI.
- Avoid duplicate full-suite runs on unchanged candidates for cosmetic handoffs. Do not cancel or skip an authoritative gate to save time. Distinguish working-tree results from committed-head results.
- Investigate flakes; any quarantine requires a tracked issue, owner-approved scope, replacement coverage and restoration criteria. No silent retries-until-green, weakened assertions or blanket skips.

## Improving selection and tracking results

Changes to selection maps/tooling need tests for mapping, shared/high-risk changes, unknown paths and fail-closed fallback. Compare selective results with full PR/nightly runs to identify missed failures and strengthen the map. Keep the full gate mandatory for ready implementation PRs, main, scheduled/manual runs, and every uncertain/high-risk change; any further relaxation needs measured evidence and separate review.

Track time to first useful failure, iteration and pre-merge time, setup versus execution cost, flake rate, selective-test misses and escaped regressions. Targets must follow measured baselines; faster runs alone are not proof of maintained quality. Profiling automation or CI changes are separate bounded implementation work, not implemented by this documentation.

## Handoff checklist

- Changed scope and risk:
- Selected tests and rationale:
- Commands, actual results and timings:
- Reused evidence and validity:
- Deferred/blocked checks and reason:
- Required current-head CI/release gates still pending:
