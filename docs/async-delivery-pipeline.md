# Asynchronous feature delivery and verified-tree CI reuse

## Status and intent

This is the delivery-process plan requested after AP-005. It separates feature
construction from landing work, removes avoidable repeated full-suite triggers,
and defines a fail-closed path for reusing an already verified tree. It does not
weaken a required check, enable automatic merging, authorize another product
story, or change `.github/workflows/ci.yml` by itself.

The immediate runbook can be used without a CI change. The tested-tree reuse
work is a separate bounded CI implementation and qualification effort.

## 1. Measured problem

AP-005 exposed three different kinds of duplicate work. All four runs were
green, but only one changed substantive runtime code.

| Run | Event and revision | Wall clock | Successful job-minutes | Why it ran |
|---|---|---:|---:|---|
| [`42795066633`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42795066633) | pull request at `514f65e` | 6m 12s | 52.0 | Draft high-risk full gate |
| [`42795985996`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42795985996) | pull request at the same `514f65e` | 6m 23s | 50.4 | Marking the unchanged PR ready |
| [`42797113026`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42797113026) | pull request at `3c57cb1` | 6m 39s | 52.5 | Evidence/ledger-only review transition after the first green gate |
| [`42798276827`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42798276827) | `main` merge `acec9ae` | 6m 10s | 50.1 | Post-merge full gate |

The four runs consumed **25m 24s of CI wall time and 205.0 successful
job-minutes**. The ready event repeated the exact head. The final PR head
`3c57cb1` and merge commit `acec9ae` also have the exact same Git tree
`216c9357acc2c045d3860288dea6b21e7a67eafb`; only the commit identity and
parents differ.

This does not mean every post-merge run is redundant. A changed base, conflict
resolution, generated merge result, workflow change, missing evidence, or
failed lookup must still run the full gate. It does show that event count is a
poor proxy for distinct candidates.

## 2. Decision: builder and lander are separate roles

Use two sessions when another authorised story is available:

- The **builder** owns the feature branch, implementation, focused iteration,
  generated artifacts, acceptance mapping, and one final candidate.
- The **lander** owns current-head CI observation, failure routing, explicit
  merge, post-merge verification, and the completion-record handoff.

A role is not a second writer. Only one session may write a feature branch at a
time. Arena sessions have fixed branches, so a lander on another branch must
not silently push fixes to the builder's branch. It either returns a diagnosis
to the builder or performs the repository's explicit preserved-work takeover
protocol and opens a replacement PR.

The builder may start another **independent, owner-authorised and
dependency-ready** story after publishing the handoff. It may perform read-only
reconnaissance for a dependent story, but cannot claim or implement it before
its dependency and authority gates are satisfied. Parallel sessions do not
expand product authority.

## 3. Immediate runbook — no CI gate change required

### 3.1 Builder

1. Open the draft coordination PR early, before repeated implementation pushes.
   This lets the PR event lane own checks and keeps feature-push runs cheap.
2. During iteration, run the smallest owning tests and generators, then the
   repository selector required by `AGENTS.md`. Diagnose a red with the failing
   node or smallest relevant group; do not rerun a full suite to make it green.
3. Assemble **one final candidate** before requesting review. It includes:
   - source, tests, migrations, documentation, and generated artifacts;
   - the story's acceptance mapping and `in_review` transition;
   - local commands and actual results;
   - authority, migration, rollback, and unresolved-gate statements.
4. Do not commit a later metadata-only change merely to insert CI run IDs. Run
   IDs do not change the candidate and belong in the PR handoff comment. Merged
   evidence belongs in the small post-merge closeout PR.
5. Push the final candidate and mark the PR ready immediately; do **not** wait
   for a draft full run and then trigger the same tree again. Same-PR
   concurrency may cancel a just-started superseded event, while one final
   ready run proceeds.
6. Post the handoff template from §8 with an exact head SHA. After the handoff,
   the builder stops polling and may move to other authorised work.

A high-risk draft can still select the full gate. The builder avoids multiplying
that cost by pushing stable checkpoints rather than every small edit, and by
not waiting for a same-head draft result before marking the final candidate
ready.

### 3.2 Lander

1. Confirm the PR number, head SHA, base SHA, ready state, mergeability, and
   required checks against the handoff. A changed head invalidates the handoff.
2. Select the newest pull-request run for that exact head. Use notifications or
   one blocking wait in the dedicated landing session; do not poll in the
   feature session and do not run shell sleep loops.
3. If green, recheck the exact head and merge the explicit PR number using the
   repository's required merge method. Never use automatic merging.
4. If red, classify it before any rerun:
   - **code/test failure:** identify the first failing node and return it to the
     branch owner;
   - **generated drift:** name the owning generator and changed artifact;
   - **infrastructure failure:** retain the run and explain why a rerun is
     justified; no blind retry-until-green;
   - **superseded run:** ignore it only after proving a newer exact-head run
     owns the gate.
5. After merge, record the merge SHA and main run. The lander owns remediation
   if main is red and opens the docs-only completion record after exact-main
   evidence is green.

### 3.3 Fix ownership

The preferred red-CI loop is builder fixes → one new final head → lander resumes.
If the builder session is unavailable, the lander must preserve every commit in
an explicit takeover on its own fixed branch, open a replacement PR, and close
the old PR as superseded. Two sessions never race writes on one branch.

## 4. Evidence placement prevents a third full run

Record evidence at the boundary where it becomes true:

| Evidence | Location | Timing |
|---|---|---|
| Acceptance mapping, local tests, migration and rollback | Story ledger and PR body | Before final ready run |
| Pending exact-head CI requirement | Story ledger and PR body | Before final ready run |
| Exact run ID, head SHA and conclusion | PR comment/check summary | After CI, without a source commit |
| Merge SHA and exact-main result | Merged PR comment and closeout handoff | After merge |
| Final `done` status and next-story authority | Separate docs/ledger closeout PR | After required merged evidence and owner decision |

A result that does not exist yet is stated as pending. An agent never predicts a
run ID or writes “green” before the check completes.

## 5. CI increment 1: reuse a successful full run for the same candidate

The first implementation increment prevents an accidental ready/draft toggle or
handoff event from repeating an already successful full gate for the same PR
candidate.

A lightweight reuse job may satisfy the new event only when all of these are
proven through the GitHub Actions API and retained evidence:

1. same repository and pull-request number;
2. same head SHA, base SHA, tested checkout tree, and workflow revision;
3. same dependency locks, shard map, test-scope map, and changed-path digest;
4. prior run was a `pull_request` event and completed successfully;
5. source gate, all nine Python shards, all three identity checks, all five
   browser shards, and every aggregate required check succeeded;
6. evidence belongs to the current workflow, has not expired, and was not
   produced by a delegated push placeholder.

Missing, malformed, ambiguous, expired, or API-inaccessible evidence selects the
full gate. Reuse is never inferred from a green run conclusion alone.

This increment keeps current required check names. The current event publishes a
small verification-reuse check linking to the original run, then the stable
aggregate contexts pass only after that verification succeeds.

## 6. CI increment 2: exact-tree carry-forward after merge

Pull-request tests execute a merge candidate, while the final merge commit can
have a different commit SHA. Test results may be carried forward only when the
**tree and all verification inputs are identical**.

### 6.1 Attestation

After every successful full PR gate, a final job records a versioned attestation
containing at least:

```json
{
  "schema_version": 1,
  "repository": "owner/repository",
  "pull_request": 123,
  "run_id": 456,
  "workflow_revision": "sha256:...",
  "base_sha": "...",
  "head_sha": "...",
  "tested_checkout_sha": "...",
  "tested_tree_sha": "...",
  "changed_paths_sha256": "...",
  "dependency_inputs_sha256": "...",
  "test_maps_sha256": "...",
  "required_job_set_sha256": "..."
}
```

The attestation job runs only after every required full-gate job succeeds. Its
record is retained with the source run and printed in the run summary. A
follow-up workflow running from trusted default-branch code should independently
validate source-run job conclusions rather than trusting a branch-authored
claim.

### 6.2 Main decision

A main push may use the attested PR result only if:

- it is a two-parent merge associated with the recorded PR;
- first and second parents match the attested base and head;
- the final merge tree equals the tested checkout tree;
- workflow, dependencies, test maps, and required-job identities match;
- the source run and all required jobs are still queryable and successful; and
- no policy requires commit-SHA-specific evidence for that gate.

Main still creates commit-SHA-specific provenance and runs the small source/
generated-integrity checks that are intentionally bound to the merge commit.
Python and browser suites can be skipped only after the exact-tree verifier
passes. Any mismatch falls back to the full main matrix.

Scheduled and manually requested qualification remain full and never reuse a PR
attestation.

## 7. Qualification and rollout

Implement tested-tree reuse in a separate CI-only PR. It must not share a diff
with a product story.

1. **Unit fixtures:** same tree succeeds; changed head, base, merge tree,
   workflow, lockfile, shard map, missing job, expired artifact, wrong PR, and
   API failure all select full verification.
2. **Shadow mode:** for at least ten qualifying merges, compute and publish the
   reuse decision but continue running the full main gate. Every would-reuse
   run must be green; every non-identical candidate must fall back.
3. **Review:** compare event wall time, job-minutes, false-reuse count, fallback
   reasons, flaky outcomes, and any escaped regression. Do not enable reuse on
   projections.
4. **Enablement:** preserve required PR check names and nightly coverage. Turn
   on main reuse only after shadow evidence is reviewed and the repository
   owner accepts the policy change.
5. **Rollback:** one planner flag restores the existing full-main behavior; no
   test, branch-protection context, or evidence record is deleted.

Success targets, measured over the first twenty eligible feature deliveries:

- one completed authoritative full PR matrix per distinct final candidate;
- builder blocked on CI monitoring for less than two minutes after handoff;
- exact-tree merges use no second Python/browser matrix;
- median heavy CI consumption below 70 job-minutes per delivery, versus 205.0
  in the AP-005 sequence;
- zero false reuse decisions and no increase in unexplained failures or escaped
  regressions;
- full fallback whenever identity cannot be proven.

## 8. Landing-session handoff template

Copy this into the PR and the new Arena session. Fill facts only.

```text
Delivery role: landing only; do not start unrelated edits on this branch
Story / status / owner:
PR / branch / merge method:
Base SHA / exact head SHA / tested tree SHA if known:
Candidate scope and highest-risk paths:
Acceptance mapping location:
Local commands and actual results:
Generated artifacts and migration/rollback notes:
Required checks and current exact-head run URL:
Superseded runs to ignore, with reason:
Failure owner and fix path:
Authority and approval boundaries:
Merge preconditions:
Post-merge main run and closeout record required:
Next story authority/dependency status:
```

Useful read-only commands for the lander:

```bash
gh pr view <number> --json state,isDraft,headRefOid,mergeable,mergeStateStatus,statusCheckRollup
gh run list --branch <branch> --limit 10 --json databaseId,event,status,conclusion,headSha,url
gh run watch <run-id> --exit-status
gh pr checks <number>
```

The merge command is executed only after the exact head is green and must name
the PR explicitly. The handoff never carries credentials, approval tokens, or
an instruction to bypass branch protections.

## 9. Current AP-005 closeout handoff

AP-005 PR #296 merged at `2026-11-10T10:20:13Z` as
`acec9aebe4f86f968c077541392a2bfc3cd71fd7`. Exact-merge main CI run
[`42798276827`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42798276827)
passed the full source, Python 3.11/3.12/3.13 identity, browser, and aggregate
matrix. The AP ledger on that merge still says `in_review` because completion is
a separate post-merge record.

A new closeout session should make only the AP-005 completion/merged-evidence
updates, run the documentation/ledger gate, and open a small closeout PR. It
must not mix AP-006 implementation into that change. A separate builder session
may prepare the next story only after checking explicit owner authority and the
merged AP-005 dependency under the AP protocol.
