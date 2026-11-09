# Verification critical path — banked result and the next experiment

This page records what the duplicate-work optimisation actually bought, then
publishes the plan for the next increment. Policy for when to run which checks
lives in [verification-efficiency.md](verification-efficiency.md) and
[AGENTS.md](../AGENTS.md) §5; this page owns the measurement.

## 1. Banked result: duplicate verification removed (PR #288, merged as `eab31d7`)

`push` and `pull_request` both ran the same commit with the same jobs. The old
concurrency group keyed on `github.ref`, so `refs/heads/X` and
`refs/pull/N/merge` never superseded each other. `ci-preflight` now skips the
heavy jobs for a pushed branch that already has an open pull request (failing
open if the lookup breaks), and concurrency is keyed per event lane so a cheap
scoped push can never cancel the full-suite pull request run.

| revision | push run | pull_request run | job-minutes | pull request wall clock |
| --- | --- | --- | --- | --- |
| before (`b273a45`) | `42687013895` 43.7 | `42687023006` 41.9 | 85.6 | 13.6 min |
| before (`6e02146c`) | `42681851797` 45.9 | `42681862794` 42.0 | 87.9 | 13.4 min |
| after (`92ec749`) | `42695169572` 0.1 (skipped) | `42695177203` 44.9 | **45.0 (−48%)** | 13.3 min |

Every wall clock in this page is `run_started_at` → `updated_at`; every
job-minute is the sum of job `started_at` → `completed_at`. Quote the pull
request run for wall clock and both runs for job-minutes, or the comparison is
not like for like.

Coverage was not traded for the saving: `5248 passed, 35 skipped` on each of
Python 3.11, 3.12 and 3.13, plus the full browser-safety job. The `main` push
after the merge (`42698201085`, `eab31d7`) cost 43.9 job-minutes and 13.5
minutes wall clock — deliberately, because the merged tree is not the pull
request head tree and that run is the post-merge confirmation.

**Residual cost, measured on this page's own branch.** The saving applies to a
push made while a pull request is already open. The first push of a branch
happens before its pull request exists, so nobody can delegate it: this branch
cost `42701605403` 41.1 job-minutes for that first push plus `42701630354` 42.9
for the pull request — the full 84 for one revision, because the pull request
was opened after the push had started. Compare like with like: quote the 48%
against a follow-up revision, and never quote a first-push pair as a saving.

**What did not change is the part that now matters: a pull request still takes
about 13 minutes to prove itself.** Runner usage fell; the required verification
critical path did not. That is the next problem, and it is a different problem.

## 2. Ownership

- **Single owner of the next increment: the session on
  `arena/0b0b7c57-m5-agent-studio` (PR #286).** That branch already carries the
  only in-flight `ci.yml` work: a fail-closed event planner, five parallel
  browser-safety shards behind the existing `Browser safety regressions`
  identity, and planner regressions. Its measured shards are cited in §4.
- **This page and its session own measurement and independent review, not the
  workflow.** The `arena/bd2b9f81` session writes no `ci.yml` change while PR
  #286 is open, and reviews that session's measurements instead of reproducing
  them on a competing branch. Two sessions editing one workflow is the failure
  mode this section exists to prevent.
- **Green checks are not review approval.** A required check proves the tree
  passes the gates it was given; it does not prove the claims in a report, the
  fairness of a comparison, or that coverage was preserved. Every number
  published here is traceable to a run id, and an independent reviewer reads
  the diff and re-derives the figures before anything is merged or banked.
- **Preconditions before PR #286 starts the pytest work** (all are rebase-time
  fixes, not new scope):
  1. Rebase onto `eab31d7`. PR #286 is `CONFLICTING` today and its branch
     predates the merged preflight job.
  2. Split the AP-003 story commits out of that pull request. Story work and
     CI-platform work do not travel together, and the acceptance criterion for
     this experiment must not be judged on a mixed diff.
  3. Reconcile the branch planner with the merged `ci-preflight` instead of
     running both. Whichever survives must keep: fail-open pull request lookup,
     per-lane concurrency, `main` never cancelled, unchanged required check
     names, and `set +e` around the pytest step.
  4. Land the browser shards alone, measure them alone (§6), then start the
     pytest sharding work.
- If PR #286 closes without the work, ownership returns to exactly one new
  owner before any further `ci.yml` edit. Never two at once.

### 2.1 Reconciliation requirements for PR #286

Reviewed at `e45ec2e` against `eab31d7`. That branch predates the merge, so a
rebase must not silently undo the merged guarantees:

**Must survive the rebase** (each is pinned by a test or a run):

- Fail-closed pull request lookup: a lookup error runs the verification.
- Per-lane concurrency with `main` never cancelled. Observed live on
  `eab31d7`: main runs `42698201085` and `42697477881` finished side by side.
  The branch's `cancel-in-progress: true` for every event would cancel a
  running main verification when another main push arrives.
- Required identities `test (3.11)`, `test (3.12)`, `test (3.13)` and
  `Browser safety regressions` produced by PR, main, schedule and dispatch.
- Streamed pytest with `--durations`, the `::notice` summary, `PYTHONUNBUFFERED`
  and `set +e`; the branch predates all of it and falls back to a buffered log.
- The 25-minute test-job timeout (slowest measured leg 13.6 minutes).

**Worth keeping from the branch:** the five browser shards behind the unchanged
aggregate identity, and the 3.11-only consolidation of source gates *if*
dependency parity is proven per interpreter.

**Blocking discrepancies:**

- A planner and the merged `ci-preflight` would both run. Keep one mechanism.
- `pip install -e '.[dev,supply-chain]'` only on 3.11 while `.[dev]` runs
  elsewhere: dependency parity differs per interpreter, so per-version outcome
  identity (§7.1) must be demonstrated, not assumed.
- Release-evidence build/upload reduced to 3.11, which removes the
  `release-evidence-3.12` and `3.13` artifacts. Either keep them or record the
  reduction as a deliberate scope change.
- Renaming push-run jobs to `Branch test (…)` is defensible only if branch
  protection requires the plain names at pull request time. Branch protection is
  not readable with this repository token (HTTP 403), so that stays an stated
  assumption, not a verified fact.

## 3. Environment

Everything below is the environment the baseline was measured in; an experiment
measured anywhere else is not comparable.

- `ubuntu-latest` GitHub-hosted runner. The runner annotation still warns that
  `ubuntu-latest` migrates to Ubuntu 26 after 19 October 2026, so a migration
  week must be treated as a separate variable, not as a regression.
- Python 3.11, 3.12 and 3.13 via `actions/setup-python@v7` with pip caching;
  Node 22.22.3 for the browser job; `actions/checkout@v7` with `fetch-depth: 0`
  (pinned-baseline ancestry checks need the full history).
- No ImageMagick in CI, so the pixel pass over `docs/media/` that dominates the
  local profile costs nothing here. No Chromium in the test job; the browser job
  installs pinned Chromium and puppeteer-core from `tests/browser/package-lock.json`.
- pytest resolves from `pip install -e '.[dev,supply-chain]'`, `-q --tb=short
  -rf`, `PYTEST_ADDOPTS=--durations=15 --durations-min=1.0`,
  `PYTHONUNBUFFERED=1`, output streamed through `tee`.

## 4. Baseline (existing CI evidence, no new runs launched)

`main` @ `eab31d7`, run `42698201085`, event `push`, success. Queue time zero
(`created_at == run_started_at`); every minute below is execution.

| job | minutes | share of the 13.5-minute run |
| --- | --- | --- |
| `test (3.11)` | 13.4 | **critical path** |
| `test (3.12)` | 11.7 | parallel |
| `test (3.13)` | 10.6 | parallel |
| `Browser safety regressions` | 8.3 | parallel |
| `ci-preflight` | 0.1 | gate |

Inside the critical path, `test (3.11)`: **Run tests 12.6 min**, checkout 0.1,
setup-python 0.1, setup-node 0.0, install 0.2, mypy 0.1, lint 0.0, guards 0.0,
smoke 0.1, evidence/artifacts 0.2. The pytest step is 94% of the job, and the
job is the critical path — so the critical path is pytest.

The slowest leg is not always 3.11. Across the four baseline runs it was
`test (3.12)` three times (`42687013895` 13.5, `42687023006` 13.6,
`42681851797` 13.9) and `test (3.11)` once (`42681862794` 13.3, and 13.4 on
`42698201085`). Per-leg pytest time moves by up to 90 s between runs on the same
commit, so a shard partition must be balanced per version from that version's
own measurements, never from a single global split.

Pytest seconds per leg, read from the step's own `::notice` (5249 passed,
35 skipped on each):

| leg | run `42698201085` | run `42695177203` |
| --- | --- | --- |
| 3.11 | 752.4 s | 740.5 s |
| 3.12 | 649.6 s | 739.7 s |
| 3.13 | 587.3 s | 592.7 s |

Inside `Browser safety regressions` (8.3 min): pinned-browser install 0.22,
backend authorization gate 1.45, browser screenshot/keyboard/refusal regressions
2.47, cross-tab visual gate 1.43, accessibility gate 1.23, stream performance
gate 1.15 — about 7.7 minutes of serial gates.

Already measured on PR #286's branch (run `42696688052`, not a new experiment):
`Browser safety shard (accessibility)` 1 m 46 s, `(backend)` 1 m 37 s,
`(performance)` 1 m 33 s behind the unchanged aggregate identity. That is the
existing evidence this plan reuses rather than re-deriving.

Reference points for job-minutes: pull request `42695177203` 44.9, manual
dispatch `42695269077` 47.9, `main` push `42698201085` 43.9.

## 5. Resource limits

- 4 vCPU / 16 GB per runner (recorded `cpu_count: 4`, AMD EPYC 7763). Every
  worker count must be justified against measurements on this shape, not the
  developer machine, and not nominal RAM.
- Job timeout is 25 minutes (`test`), 15 minutes (browser safety). Shards must
  stay far inside them; a shard that needs more than 10 minutes is mis-partitioned.
- Each additional shard repeats cheap work: checkout 0.1 + setup 0.1 + install
  0.2 + guards/mypy/lint ~0.3 ≈ **0.7–0.9 runner-minutes**. Eight extra shards
  therefore add roughly 6–7 job-minutes per run.
- Runner concurrency: three Python legs plus five browser shards plus an
  aggregate is 10 jobs at once. Queue time has been zero so far, but a 13-way
  fan-out is untested; measure queue time separately and do not attribute it to
  the suite.
- The metric for this increment is **critical-path wall clock**, not
  job-minutes. Sharding is expected to *raise* total runner-minutes while
  lowering wall clock. That trade is accepted only with the coverage and
  flakiness gates in §6 intact.

## 6. Isolation risks

In-process parallelism (`pytest-xdist`) is the risky option here and job-level
sharding is the boring one. Choose boring unless a measurement says otherwise.

| risk | why it bites in this repository | mitigation |
| --- | --- | --- |
| Repo-tree mutation | Several suites regenerate artifacts and assert `git diff --exit-code` (`test_uiux_release_gate`, `test_uiv2_performance_safety`, docs generators). Two workers in one checkout interleave and produce false reds. | Job-level shards with separate checkouts; never `xdist` over these modules. |
| Timing assertions | `test_stream_perf`, `test_uiv2_performance_safety`, `test_responsive` assert latency, layout and heap budgets. CPU contention from co-scheduled workers turns budgets into flakes. | Keep timing-sensitive areas whole in their own shard; no co-scheduling with heavy suites. |
| Loopback servers and ports | Tests start disposable servers (`demo_tour.py --_demo-server`, smoke scripts, SSE fixtures). Ephemeral binding is mostly safe, fixed defaults are not. | Separate runners remove the class entirely; if `xdist` is ever tried, prove port isolation first. |
| Environment and `PATH` shims | Tests monkeypatch `STUDIO_SCREENSHOT_*`, `M5_REQUIRE_BROWSER_TESTS`, `PATH` shims for `identify`/`git`. | Every shard inherits the identical environment; no shard may set `M5_REQUIRE_BROWSER_TESTS`. |
| Git history dependency | `verify_baseline`, `test_baseline`, `test_uiux_source_manifest`, `check_commit_messages` need real ancestry and `origin/main`. | Keep `fetch-depth: 0` in every shard; group those checks into one shard. |
| Coverage omission | A partition can silently drop or duplicate modules; a shard can pass while collecting nothing. | Commit the partition map; fail a shard that collects zero tests; assert the union of collected node ids equals the unsharded collection; assert `sum(passed)` matches the baseline. |
| Identity drift | Branch protection requires `test (3.11)`, `test (3.12)`, `test (3.13)`, `Browser safety regressions`. | Same pattern the browser shards already use: shards are additive, the required name is produced by an aggregate job that needs all shards. |
| Disk and `TMPDIR` | Chromium plus extracted libraries and evidence directories are hundreds of megabytes. | Per-job `RUNNER_TEMP`; do not share caches between shards beyond pip. |

## 7. Experiment plan and acceptance criterion

**Hypothesis.** The critical path is pytest, not the number of jobs. Partitioning
the pytest leg by measured duration, per Python version, behind an aggregate
identity, lowers time-to-green without dropping or duplicating a test.

**Steps.**

1. Build a deterministic partition from the committed area map
   (`tests/test_scopes.json`) plus the `--durations` evidence already in the run
   log, balancing measured seconds rather than test counts. Commit the map.
2. Shard `test (3.11)`, `test (3.12)`, `test (3.13)` into three shards each;
   keep the required names produced by an aggregate job that needs every shard.
3. Fail a shard that collects zero tests. Publish each shard's collected count
   and elapsed seconds as its own `::notice`.
4. Measure with the existing evidence channel (`--durations` in the log, step
   timings via the API). No dedicated timing run, no restarts, no reruns to
   improve the number.
5. Record the browser shards separately first, since PR #286 already has them.

**Order of work.** The browser shards land first because they are bounded and
already measured, not because they shorten the critical path: 8.3 minutes of
browser work sits under a 13.4-minute Python leg, so sharding it alone changes
the total by roughly nothing. Their value is a smaller, reversible change that
proves the aggregate-identity pattern. pytest is the measured critical path and
is therefore the increment that can actually move the number.

### 7.1 Test identity and outcome coverage

Totals are not coverage. A shard can pass while collecting nothing, a test can
be collected twice, and a skip can appear on one interpreter only — all three
survive a `passed`-count comparison. The experiment must show, **per Python
version**:

1. **Node id set identity.** `pytest --collect-only -q` before and after; the
   union of the shards' collected node ids must equal the unsharded collection
   for that interpreter, exactly. Compare sets, not counts, and store both.
2. **Duplicate detection.** The union must have no repeated node id; a test
   collected by two shards is a defect even when both pass.
3. **Outcome identity.** A per-test outcome map (`passed`/`failed`/`skipped`/
   `xfail`/`error`) diffed against the unsharded run at the same commit. A test
   that was `passed` and is now `skipped` is lost coverage, whatever the totals
   say.
4. **Skip identity.** The same skip reasons with the same counts per
   interpreter; a skip that appears only on 3.12 is a per-version regression.
5. **Non-empty shards.** A shard that collects zero tests fails, so an empty
   shard cannot report a green.
6. **No filters.** No `-k`, no `--deselect`, no `-m` narrowing, no new
   `pytest.mark.skip`, no changed `addopts`. `PYTEST_ADDOPTS` stays limited to
   `--durations`.

Every shard publishes its collected count, its elapsed seconds and its outcome
line in a `::notice`, and the run id is recorded next to the numbers.

### 7.2 Repeated qualification, retained attempts

Repeat the full qualification **only on the final candidate**: five consecutive
runs of the same tree (no intervening commits), no retries, and every attempt
kept — including failures, with the run id, the job, the failing node id and the
first divergence from the baseline outcome map. A failed attempt is reported and
investigated as a defect; deleting it or re-running until green is prohibited.
Intermediate iterations are measured once each; they are not qualified.

### 7.3 Reporting rule: observed versus projected

Observed results — run id, wall clock, job-minutes, per-shard seconds, identity
diff — are reported in one table. Anything not yet run is reported separately,
labelled a projection, and never mixed into the observed table or summarised as
a saving.

**Acceptance criterion — measured, not projected.**

- Critical path (run start → last required check green) improves by at least
  30% on the same class of run: from 13.5 minutes to **9.5 minutes or less**.
- Coverage is unchanged by §7.1: per-version node id sets, duplicates, outcomes
  and skips all identical to the unsharded baseline at the same commit.
- Flakiness does not increase: five consecutive runs at an unchanged tree with
  zero unexplained failures, all attempts retained. No retries until green.
- Required check names and branch-protection contexts are untouched.
- **No uncontrolled resource growth**: total job-minutes stay inside 1.3× the
  measured baseline for the same event class (≤ 58 for a pull request against
  44.9), queue time stays under one minute, and no single shard exceeds 10
  minutes. A critical-path win bought with unbounded runners fails this
  criterion, because the next run then waits in a queue instead.

**Projection, clearly labelled as such.** With per-leg pytest near 650–750 s
split three ways (~4.2 min) plus ~0.8 min of per-shard setup and gates, a shard
lands near 5 minutes. With the browser shards already measured at ~2 minutes,
the critical path becomes roughly 5–6 minutes, about 60% below baseline, at
roughly 50 job-minutes instead of 44. Those are projections; §4's measurements
are the baseline they will be judged against, and no saving is claimed until the
runs exist.

## 8. Non-goals

- No AP story work, no product behaviour, no ledger or evidence edits ride in
  this increment.
- No change to the local risk matrix in AGENTS.md §5.1, no removal of a required
  check, no change to branch protection.
- No `xdist` evaluation until job-level sharding has a measured result; the
  isolation table in §6 is the reason.
- No restart of a run to make a number look better, and no comparison of a
  sharded run against a baseline measured on a different environment.
