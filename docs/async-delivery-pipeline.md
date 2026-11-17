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

## 9. AP-005 closeout landing record

AP-005 implementation PR #296 is merged. Its final PR head was
`3c57cb16a51cc0f014a4f4767fa4d437f71c4a2e`; substantive implementation snapshot
`514f65e07ab8961053539c0521c2cf5c912f3115`; merge commit
`acec9aebe4f86f968c077541392a2bfc3cd71fd7`. Final PR CI
[`42797113026`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42797113026)
and push aggregate
[`42797098242`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42797098242)
passed on the final head. Exact-merge main CI
[`42798276827`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42798276827)
passed the source, Python 3.11/3.12/3.13 identity, browser, and aggregate matrix.
The AP ledger at that merge still said `in_review`, so this docs/ledger-only PR
records the completion after merge.

Delivery-process PR #297 then merged as `63f674fb402f0e09dc56b3d3429ddf4cfcdd834c`.
Its post-merge main run
[`42800439626`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42800439626)
passed on that exact main SHA. The final PR head and merge tree were identical
(`216c9357acc2c045d3860288dea6b21e7a67eafb`).

This closeout preserves the AP-005 acceptance map, schema-v4 additive migration
and populated-v3 rollback evidence, and the offline/no-activation limitations.
The owner authorized AP-006 as the next single dependency-ready story after this
closeout PR merges. Do not mix AP-006 implementation into this PR. AP-007 through
AP-032, runtime activation, real external actions, and owner/independent/security
or release approvals remain outside this authorization.

The AP-005 closeout PR #298 was landed by the delegated landing session
(`arena/25975e0f-m5-agent-studio`) after its exact head
`84d6d415491862c2fab46e0f5ee94a68056fdc19` passed the current-head
documentation/ledger and browser-safety gate in run
[`42802006871`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42802006871).
It merged at `2026-11-10T10:50:23Z` as `69092eda00d7e42de06a3ced3c054e3b7d7f2393`,
and the exact-main run
[`42802513165`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42802513165)
passed in full. AP-005 is therefore `done` on `main`; AP-006 was subsequently
authorized, delivered and landed (see below), and AP-007 through AP-032 remain
unauthorized.

The AP-005 status record is terminal at this point: this landing record's own
merge SHA, merge time, and post-merge main run are recorded in its pull-request
comment, because a commit cannot contain its own merge identity. No further AP-005
completion PR is required, and that precedent applies to later AP deliveries.

## 10. AP-006 landing record

AP-006 (artifact identity, completeness and confined references) is the second
delivery landed under this runbook, and the first full ready-for-review
implementation delivery it carried end to end.

- Builder: `arena/930387b7-m5-agent-studio`, PR #299, handoff with
  `writer_state: released` at exact head
  `accfda81c814c961390875b61cb07a078469a5d7` on a base that had merged current
  `main` (`353afa76599e7d545c053ab21c363a33595d88a7`).
- Gate: ready-for-review `pull_request` run
  [`42805493859`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42805493859)
  passed on the exact head — source gates, nine Python shards over 3.11/3.12/3.13,
  shard-identity checks, five browser-safety shards, browser-safety regressions and
  the `test` aggregates. The same-head run `42805454781` was cancelled by same-PR
  concurrency and is recorded as superseded, not as a failure.
- Landed by `arena/fe410bc3-m5-agent-studio` at `2026-11-10T11:18:50Z` as
  `5872cc612cb1cf521cbfbe5a32ef5dbaaed40116` after re-verifying the unchanged head,
  `MERGEABLE`/`CLEAN` state and an unchanged base. Exact-main run
  [`42806442354`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42806442354)
  passed on that merge SHA.
- No takeover was needed, no commit was written to the released builder branch, and
  the builder's own local results stay attributed to the builder while the gate and
  main results stay attributed to CI.

AP-006 remains an offline, local capability: no endpoint, route, control, provider,
model, network, browser, account or background worker was added or activated.
ADR-0040 remains `review_status: not-reviewed`, and AP-007 through AP-032 remain
unauthorized. As with AP-005, this landing record's own merge identity is recorded in
its pull-request comment rather than in a further commit.

That sentence records the state at the AP-006 landing. Afterwards the safe
fail-closed offline subset of AP-007 was claimed on PR #303 under a scoped owner
instruction that the AP ledger quotes, and it subsequently landed (see below). It is
not a full AP-007 authorization: U06 is still pending, and AP-008 through AP-032
remain unauthorized.

## 11. AP-007 landing record (offline fail-closed subset; U06 pending)

AP-007's safe fail-closed offline subset is the third delivery landed under this
runbook — the first one landed while its story's owner decision is still pending,
under an explicit in-session owner decision recorded before the merge
(2026-11-15T16:20Z): land PR #303 while U06 remains pending.

- Builder: `arena/cbf9f2b0-m5-agent-studio`, PR #303. The subset implements a
  mission-scoped stop with measured cleanup latency, a late-result fence on the
  live action path (a result arriving after the ledger marked the action uncertain
  no longer settles it, for every task), bounded no-progress/repair/deadline/
  memory/storage limits committed before refusal, an isolation requirement with no
  host fallback, and a truthful recovery report; docs/bounded-mission-execution.md
  and ADR-0041 describe them. It is behaviour in six production modules
  (`workflow_state.py`, `execution_budget.py`, `side_effect_ledger.py`,
  `execution_spine.py`, `process_supervision.py`, `app.py`) plus a rebuilt
  `studio.py` — not tests only.
- Gate: ready-for-review `pull_request` run
  [`43734238718`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43734238718)
  passed at the released exact head `b1b2e4d747f9916a8ffd77f1126c90f8288294c8` —
  plan, source/release-independent gates, nine Python shards over 3.11/3.12/3.13,
  three shard-identity checks, five browser-safety shards, browser-safety regressions
  and the `test` aggregates. The same head had earlier passed the draft-era run
  [`42834904831`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42834904831)
  and the feature-lane push run
  [`42834895636`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/42834895636).
  The superseded head `4f79822` produced a failing `pull_request` run `42830912254`
  whose only failing job (`Python shard identity (3.12)`) was an artifact-service
  download error with all shards green; it was recorded as superseded and not as a
  code or test failure.
- Landed by `arena/776b872b-m5-agent-studio` (owner's explicit in-session merge
  authorization) at `2026-11-15T16:24:38Z` as
  `bf1fa6aefa24913ed7a88ceb12775b484dbc7cc5` via `gh pr merge 303 --merge`, after
  re-verifying the unchanged released head, `MERGEABLE`/`CLEAN` state and a CLEAN
  merge state. Exact-main push run
  [`43735318772`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43735318772)
  passed on that merge SHA (`test` 3.11/3.12/3.13 and `Browser safety regressions`
  all green).
- The remainder of AP-007 is undelivered and stays blocked by U06: execution on a
  qualified isolation backend, installed-runtime qualification, any production caller
  of the mission stop, the recovery report or the observation recorder, and any
  activation. No route, endpoint, UI control, table or schema version was added by
  the subset (schema stays v4). `blocked_by_decisions` remains `["U06"]` and the
  story is recorded `blocked`, not `done`; `authorized_downstream_stories` remains
  AP-002…AP-006 and `runtime_activation_authorized` stays `false`.
- No takeover was needed, no commit was written to the released builder branch, and
  the builder's own local results stay attributed to the builder while the gate and
  main results stay attributed to CI. The U06 call-path audit page
  (`docs/autonomous-production-u06-call-path-audit.md`) was out of date on G-1 and
  G-6 after this landing; it has since been regenerated at `972ea74` (see the audit
  page's revision history), so its gap table and questions now reflect the landed
  subset.

As with AP-005 and AP-006, this landing record's own merge identity is recorded in
its pull-request comment rather than in a further commit.

## 12. AP-007-F1 landing record (late-result fence limited to mission-owned work)

AP-007-F1 is a follow-up change to the landed AP-007 subset, not a story. The
owner asked for it after PR #303 had merged: limit the late-result fence to
mission-owned work. The builder session also landed it, on the owner's in-session
instruction "continue and merge", which was recorded on the pull request before
the merge.

- Builder: `arena/586e7d3c-m5-agent-studio`, PR #307. `side_effect_ledger.py`
  now fences a late result only when the action's task is a task of a mission
  plan or a descendant of one. For every other action the late result is
  recorded, as before AP-007. The running application creates no mission plan, so
  the fence does not fire there.
  [ADR-0042](adr/0042-late-result-fence-limited-to-mission-owned-work.md) records
  the decision and its cost: outside missions a late result again overwrites an
  `uncertain` verdict. §11 describes the fence "for every task", as PR #303
  merged it.
- Base change before the merge: the ready-event run
  [`43790341153`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43790341153)
  had passed on the earlier head `4255d5f82831a7ef650ee41046df15ebdf3d5dbe` when
  PR #305 merged into `main` as `afeef1694b8e33b5aad4036871ba3e4a6980e52d`. PR #305
  changes `.github/workflows/ci.yml` and regenerates two records. The earlier run
  covered the previous base and workflow revision, so it is recorded as
  superseded and was not reused as the gate. `main` was merged into the branch
  without conflict, every generator `--check` passed on the merged tree, and the
  new head got its own run.
- Gate: `pull_request` run
  [`43796886611`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43796886611)
  passed at the exact head `bbcf0a2b8cefa3edea3bbd1d1a7e9f24c37b57c1` — plan,
  source/release-independent gates, nine Python shards over 3.11/3.12/3.13, three
  shard-identity checks, five browser-safety shards, browser-safety regressions
  and the `test` aggregates.
- Landed by `arena/586e7d3c-m5-agent-studio` at `2026-11-16T03:20:57Z` as
  `e28cccdac003975a8717a7f28f7783f38c1aa97a` via `gh pr merge 307 --merge`, pinned
  to that head, after re-verifying the unchanged head, the unchanged base and the
  `MERGEABLE`/`CLEAN` state. The merge commit and the tested head have the same
  tree. Exact-main push run
  [`43797648148`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43797648148)
  passed on that merge SHA (`test` 3.11/3.12/3.13 and `Browser safety regressions`
  all green).
- Superseded on the earlier head: run `43790130871` started while the pull request
  was a draft and was cancelled by same-PR concurrency when the ready event
  arrived. Its aggregate jobs show `failure` because the shards they wait for were
  cancelled. It is recorded as superseded and not as a code or test failure.
- Unchanged by this landing: U06 is pending and AP-007 is recorded `blocked`, not
  `done`; `authorized_downstream_stories` remains AP-002…AP-006 and
  `runtime_activation_authorized` stays `false`. ADR-0042 is
  `review_status: not-reviewed`.
- The U06 call-path audit page
  (`docs/autonomous-production-u06-call-path-audit.md`) is out of date on the
  fence scope after this landing: it describes the fence for every task and says
  that this holds until PR #307 lands. Its maintenance rule assigns the
  regeneration to its owner, and this record does not edit the page.
- Owner input recorded after this landing: PR #309 (merged as
  `9d18fef823d461ca5622a20d504a2ab080dcadc7`) records the owner's answers to the
  six U06 questions as input, not as a resolution. One answer says that the
  AP-007 offline fail-closed subset is not accepted as the refusal foundation and
  that rework is required. This landing record is not an acceptance of the
  subset, and AP-007-F1 is not that rework. The answers are in the
  [operating envelope](autonomous-production-operating-envelope.md); they
  authorize no story. This record was merged with `main` after PR #309 and
  reconciled against it.

As with the earlier landings, this record's own merge identity is recorded in its
pull-request comment rather than in a further commit.

## 13. AP-007-F2 landing record (proven sandbox admission for mission-owned consequential work)

AP-007-F2 is a follow-up change to the landed AP-007 subset, not a story. The
owner's U06 answer 6, recorded through PR #309, does not accept the subset as the
refusal foundation and asks for rework. The builder session asked the owner for
the scope. The owner selected full alignment with answers 1 to 4, for
mission-owned work, and "Merge when the gate is green". On that second selection
the builder session also landed the change.

- Builder: `arena/0cf8f812-m5-agent-studio`, PR #312. `execution_budget.py` now
  asks for isolation proof for every consequential action of mission-owned work,
  whether or not a budget declares `isolation_required`, and judges the proof
  against a recorded acceptance: runtime, runtime version, image digest, earliest
  accepted qualification run and activated steps. No acceptance is recorded, so
  every such action is refused with `isolation_unavailable` before it runs.
  Browser-worker and native-lifecycle work of a mission is refused whatever its
  action class. Work that no mission owns is unchanged, and the running
  application creates no mission plan, so the requirement does not fire there.
  [ADR-0043](adr/0043-proven-sandbox-admission-for-mission-owned-consequential-work.md)
  records the decision, its cost and what is not delivered. §11 names "an
  isolation requirement with no host fallback", as PR #303 merged it: it applied
  only when a budget declared it, and nothing declares it.
- First exact-head run, red and not rerun: `pull_request` run
  [`43815203908`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43815203908)
  on the earlier head `b4c33ad2a4ae9cfe5a49f1d143d22c59db1e6d4c` failed in one job,
  `Browser safety shard (performance)`, and in the aggregate that waits for it.
  One render-time check measured a mean of 4.085 ms against a 4 ms budget. The
  same shard had measured 1.62 ms 28 minutes earlier on
  `a1bf088a67a503052418c182de513129014e68d5`, which differs only in the AP ledger,
  and both walks passed six of six local runs. The failure was classified on the
  pull request as an infrastructure failure, a slow runner, before any rerun. It
  was then not rerun, because `main` had moved. Nothing was changed to obtain a
  green result.
- Base change before the merge: PR #313 merged into `main` as
  `9a1c4d19e00192c54346085a8eff0f29fdca23f6` 30 seconds after that run had
  started. `main` was merged into the branch. No source file conflicted. Sixteen
  generated artifacts conflicted and were regenerated from the merged sources;
  none was merged by hand. The new head got its own run and a reissued handoff.
- Gate: `pull_request` run
  [`43820299201`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43820299201)
  passed at the exact head `4d75b2273c21e967f490e97e88d672f2f245832d` — plan,
  source/release-independent gates, nine Python shards over 3.11/3.12/3.13, three
  shard-identity checks, five browser-safety shards, browser-safety regressions
  and the `test` aggregates.
- Landed by `arena/0cf8f812-m5-agent-studio` at `2026-11-16T07:03:53Z` as
  `aa91fe96e72dc6fdaf21d4ceebfd66d9aec5f28d` via `gh pr merge 312 --merge`, pinned
  to that head, after re-verifying the unchanged head, the unchanged base and the
  `MERGEABLE`/`CLEAN` state. The merge commit and the tested head have the same
  tree. Exact-main push run
  [`43821138708`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43821138708)
  passed on that merge SHA (`test` 3.11/3.12/3.13 and `Browser safety regressions`
  all green).
- Superseded: run `43815203178` started on the earlier head while the pull request
  was a draft and was cancelled by same-PR concurrency when the ready event
  arrived. Its aggregate jobs show `failure` because the jobs they wait for did
  not complete; no test ran. Run `43818972907` passed on the merge of `main` and
  was superseded by the ledger commit that became the final head.
- Unchanged by this landing: U06 is pending and AP-007 is recorded `blocked`, not
  `done`; `authorized_downstream_stories` remains AP-002…AP-006 and
  `runtime_activation_authorized` stays `false`. ADR-0043 is
  `review_status: not-reviewed`.
- Not decided: whether the rework is accepted as the refusal foundation. The
  `done` status of AP-007-F2 records that its change merged. It is not the
  owner's acceptance. The answers that PR #309 recorded in the
  [operating envelope](autonomous-production-operating-envelope.md) are not
  edited; they still say that U06 needs the AP-007 rework first.
- The U06 call-path audit page
  (`docs/autonomous-production-u06-call-path-audit.md`) is pinned to `972ea74`
  and describes the isolation gate as opt-in. After this landing its statements
  about the gate describe the earlier state. Its maintenance rule assigns the
  regeneration to its owner, and this record does not edit the page.
- Corrections: the ledger text that landed with PR #312 said things about the
  session's exchange with the owner that no record supports: quoted labels for
  options that were not selected, a second option of the merge question, a
  sentence that no next step had been offered, and a time. The completion record
  removes them and lists them in `record_corrections` of the ledger entry. The
  owner's two selections were quoted correctly throughout.
- Reconciled with `main` at `6e3b45376743c4bdf2ae2154e0b5cb658d32967f` (merge of
  PR #314), whose post-merge run
  [`43831402766`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/43831402766)
  passed. PR #314 changes none of the AP-007 sources.

As with the earlier landings, this record's own merge identity is recorded in its
pull-request comment rather than in a further commit.

## 14. AP-007-F3 landing record (qualification provenance and reported refusal)

AP-007-F3 is a follow-up change to the landed AP-007 subset, not a story.
ADR-0043, the decision record of AP-007-F2, lists what that rework did not
deliver. When the AP-007-F2 record had landed, nothing further was authorized,
so the builder session asked the owner what to do next. The owner selected
"Close the three offline gaps (recommended)" and "Merge when the gate is green".
On that second selection the builder session also landed the change. The ledger
entry quotes both questions and every option that was offered.

- Builder: `arena/282c6b34-m5-agent-studio`, PR #319. For mission-owned work, a
  backend qualification now counts as isolation proof only when its report
  stated its own generation time and was produced on the recorded target host,
  and when the dispatch is observed on that host. The recovery report of a live
  plan that still owes work carries the limitation
  `consequential_actions_refused`. `sandbox_boundary.py` records two more fields
  on a qualification, `report_time_stated` and `host_fingerprint`. They appear in
  the store file and in the JSON that the security-incident routes return, and
  the sandbox admission of the running application reads neither. No acceptance
  is recorded and nothing observes the host, so every consequential action of
  mission-owned work stays refused, as before.
  [ADR-0044](adr/0044-qualification-provenance-and-reported-refusal-for-mission-isolation.md)
  records the decision, its cost and what is not delivered: among other things,
  the host fingerprint does not tell apart two machines of the same model and
  memory size, and a qualification report is not authenticated. §13 lists what a
  recorded acceptance pins, as PR #312 merged it; an acceptance now also names
  the target host.
- Failing runs on the way, not rerun: tests failed on generated drift in two
  `pull_request` runs while the pull request was a draft. The implementation
  commit `7c0521a3c38e1539ed17c8b5bad3b776b19e8c75` changed three source modules
  and rebuilt `studio.py`, and it was pushed before the artifacts that are bound
  to those files were regenerated. Run
  [`44029651356`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44029651356)
  on that head was cancelled by the next push after four of its nine Python
  shards had failed. Run
  [`44030127978`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44030127978)
  on `bc606caaf660445c8607a55f0effdfe08059cc39` failed in all nine: per Python
  version, eleven tests of `tests/test_uiv2_performance_safety.py`,
  `tests/test_uiux_release_gate.py`, `tests/test_uiux_source_manifest.py` and
  `tests/test_uiv2_release_review.py`.
  `111c812c38b62a1c4b50e285b39c240435b0daf8` regenerated six artifacts with their
  owning generators, and run
  [`44031173682`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44031173682)
  passed on it. No test of the change failed. Nothing was changed to obtain a
  green result: the pull request changes none of those four test modules, no
  generator and not the workflow.
- Base: `main` stayed at `e57943a12d129403674fed94a9394c4c42b22879` from the claim
  to the merge. Nothing was merged into the branch.
- Gate: `pull_request` run
  [`44034997168`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44034997168)
  passed at the exact head `e51ec391c9f36bf0e522e348b81b901ef3038e74` — plan,
  source/release-independent gates, nine Python shards over 3.11/3.12/3.13, three
  shard-identity checks, five browser-safety shards, browser-safety regressions
  and the `test` aggregates.
- Landed by `arena/282c6b34-m5-agent-studio` at `2026-11-17T07:04:03Z` as
  `dc2f7b5bf4085a1f4d8f8b869a30a4406fa361a5` via `gh pr merge 319 --merge`, pinned
  to that head, after re-verifying the unchanged head, the unchanged base and the
  `MERGEABLE`/`CLEAN` state. The merge commit and the tested head have the same
  tree. Exact-main push run
  [`44035843331`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44035843331)
  passed on that merge SHA (`test` 3.11/3.12/3.13 and `Browser safety regressions`
  all green).
- Superseded: run `44034980516` started on the final head while the pull request
  was a draft and was cancelled by same-PR concurrency when the ready event
  arrived. Its aggregate jobs show `failure` because the jobs they wait for did
  not complete; no test ran. Run `44031173682` passed on the regenerated
  artifacts and was superseded by the ledger commit that became the final head.
- No independent review: PR #319 has no review on GitHub, and ADR-0044 is
  `review_status: not-reviewed`. The local evidence is the builder's own, on
  Linux without a container runtime and with fixture records. It does not show
  any behaviour on a qualified backend, on the target host or in an installed
  runtime, and it does not show the macOS readings of the qualification suite.
- Unchanged by this landing: U06 is pending and AP-007 is recorded `blocked`, not
  `done`; `authorized_downstream_stories` remains AP-002…AP-006 and
  `runtime_activation_authorized` stays `false`.
- Not decided: whether the rework, with or without this follow-up, is accepted as
  the refusal foundation. The `done` status of AP-007-F3 records that its change
  merged. It is not the owner's acceptance. The answers that PR #309 recorded in
  the [operating envelope](autonomous-production-operating-envelope.md) are not
  edited; they still say that U06 needs the AP-007 rework first.
- The U06 call-path audit page
  (`docs/autonomous-production-u06-call-path-audit.md`) is pinned to `972ea74`
  and describes the isolation gate as opt-in. Its statements about the gate
  describe the state before AP-007-F2. The owner was offered its regeneration as
  one of four options and selected another, and this record does not edit the
  page.
- Corrections: the handoff on PR #319 listed the first failing run only as
  cancelled, and it said that the documents had been pushed before the artifacts
  were regenerated. Tests failed in two runs, and the push that caused it was
  the source change. A comment on PR #319 corrects the handoff, and
  `record_corrections` of the ledger entry lists this and three smaller points.
  One of them is a commit message (`cda2e515698d3fce4fc3af8712debfeda1ba34b4`)
  that is wrong in one point; the ledger entry has said so since PR #319.
- Reconciled with `main` at `dc2f7b5bf4085a1f4d8f8b869a30a4406fa361a5`, the merge
  commit of PR #319 itself. Nothing had merged after it when this record was
  written, so its post-merge run is also the latest `main` run.

As with the earlier landings, this record's own merge identity is recorded in its
pull-request comment rather than in a further commit.

## 15. AP-007 completion record (authorized remainder AP-007-R1)

This is the separate docs/ledger closeout required after the AP-007-R1 observer
seam landed. The closeout records AP-007 `done` only when this PR merges; it does
not bundle AP-008 or treat owner authority as runtime activation.

The repository owner resolved U06 through PR #324, explicitly waived the fresh
target-host qualification run and accepted AP-007-F2/F3 as the refusal
foundation. That decision did not create an `IsolationAcceptance` or activate
anything. The owner then authorized the AP-007 remainder and AP-008 only after
AP-007 is recorded done, one story per branch/PR. This closeout records the
remainder; AP-008 remains `not_started` and is not claimed or implemented here.
AP-009 through AP-032, runtime activation and real external effects remain
unauthorized.

| Fact | Evidence |
|---|---|
| AP-007-R1 implementation | PR [#327](https://github.com/pankajtiwarics27/m5-agent-studio/pull/327), final head `047378a48bde7e810fc80681d9d25febff22f190`, based on `main` at `2009d6c8572c697d944109176618174000aae2d4`; merged as `6e357c4681b29b803b713d2f142979d9510fe78f` at `2026-11-17T13:27:32Z`. The implementation commit is `f3702b7634549bfd349a6818f6c7415994162690`. |
| Exact-head gate | [`44091638722`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44091638722), `pull_request` on the final head: success, 23 jobs succeeded, 4 skipped, 0 failed. |
| Exact-main gate | [`44092926116`](https://github.com/pankajtiwarics27/m5-agent-studio/actions/runs/44092926116), `push` on merge commit `6e357c4681b29b803b713d2f142979d9510fe78f`: success, 23 jobs succeeded, 4 skipped, 0 failed. |
| Completion-record base | This record is based on `main` at `6e357c4681b29b803b713d2f142979d9510fe78f`, the merge of PR #327. The closeout changes documentation and AP tracking only. |

### Acceptance evidence and limits

The AP-007 evidence mapping in `product/M5_Autonomous_Production_Roadmap.json`
remains authoritative. The earlier offline subset and AP-007-F1/F2/F3 records
cover mission stop/cleanup, late-result fencing, bounded limits, isolation
refusal and recovery-report behaviour. R1 closes the remaining observer gap:
`observe_host_fingerprint()` reads the qualification suite's five identity
values at call time, returns the canonical `sha256:` digest, and fails closed
with an empty string. Observer parity, per-field contribution, malformed or
unreadable inputs, Darwin reader timeout, and isolation-gate acceptance/refusal
are covered by the R1 tests. Fixture evidence is not a production dispatch or
runtime acceptance.

No production caller, recorded `IsolationAcceptance`, live dispatch, runtime
activation, or installed-runtime qualification was added or run. The current
sandbox is Linux x86_64 and is not the target Apple M5; the owner's explicit
fresh-run waiver accepts that evidence gap, and the 2026-10-31 M5-P3-01 record
remains the only machine evidence. `runtime_activation_authorized` stays
`false`; installed execution and all consequential actions without an accepted
proof remain refused. ADR-0040 through ADR-0045 remain `not-reviewed`. This
record is not independent security, release, screenshot, media, signing, or
owner approval. The recovery report records retained artifacts and actionable
limitations; no UI/route surfaces it here, which remains separate AP-008 work.
The U06 call-path audit remains at its owner-selected `972ea74` snapshot and is
not regenerated by this closeout; its historical scope is called out in that
audit's revision history.

### Local validation

Linux x86_64, Python 3.11.2, after the final documentation and ledger edits:

- `.venv/bin/python -m pytest -q tests/test_autonomous_production_decisions.py tests/test_autonomous_production_evaluation.py tests/test_docs.py tests/test_governance.py tests/test_adr_process.py tests/test_uiux_docs_verification.py tests/test_uiux_roadmap.py tests/test_uiux_release_gate.py` — 121 passed in 274.17s.
- `.venv/bin/python scripts/validate_docs.py` — passed, 299 files scanned.
- `.venv/bin/python scripts/validate_governance.py`, `scripts/validate_adrs.py`, and `scripts/validate_security_signoff.py` — passed.
- `.venv/bin/python scripts/generate_roadmap_status.py --check` — passed; dashboard current.
- `git diff --check` — passed.

The full runtime suite was not rerun for this docs/ledger-only closeout; the
PR's exact-head CI remains the authoritative gate for this change.

The closeout advances `AP-007` to `done` and adds `AP-008` to
`authorized_downstream_stories` under the owner's already-recorded sequential
authorization. AP-008 remains `not_started` until a fresh branch/PR claims it;
this branch does not begin that story. No later story is authorized.

As with earlier closeouts, this record's own merge identity and exact-main run
are recorded in its pull-request comment, not in a further commit.
