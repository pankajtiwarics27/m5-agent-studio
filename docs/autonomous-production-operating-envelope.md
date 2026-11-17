# AP-002 — Mission scope and operating envelope

**State:** done • **Claim base:** `fecd51faec7cf636d38c7ef5340a14d4f98f9f92` • **Delivered PR:** [#280](https://github.com/pankajtiwarics27/m5-agent-studio/pull/280) • **Merged:** 2026-11-09T17:23:52Z

**Completion record:** [#281](https://github.com/pankajtiwarics27/m5-agent-studio/pull/281) • **Closeout branch:** `arena/841521ae-m5-agent-studio`

## Authority and bounded outcome

After AP-001 completed, the owner explicitly authorized **AP-002 and AP-003
sequentially**. Both are now delivered: AP-002 through PR #280 and AP-003
through PR #284, each from its own fixed Arena branch and pull request. AP-004
was separately authorized and is now delivered through PR #292. After that
merge and its same-SHA post-merge CI passed, the owner's 2026-11-10 “Continue”
instruction authorized AP-005 as the next single story. AP-005 is now done:
PR #296 merged as `acec9aebe4f86f968c077541392a2bfc3cd71fd7`, and exact-merge
main CI run 42798276827 passed. The AP-005 closeout PR #298 then merged as
`69092eda00d7e42de06a3ced3c054e3b7d7f2393` with a green exact-main run
(42802513165). AP-006 is now done too: PR #299 merged as
`5872cc612cb1cf521cbfbe5a32ef5dbaaed40116` and its exact-main run 42806442354
passed. AP-007's safe fail-closed offline subset landed through PR #303 (merged as
`bf1fa6aefa24913ed7a88ceb12775b484dbc7cc5` on 2026-11-15, exact-merge main run
43735318772 green): the U06 safe work listed in this register, with activation kept
false. That landing was not a full AP-007 authorization and did not resolve U06. On
2026-11-17 the repository owner **resolved U06** by explicit recorded decision (agent
session `arena/0efd4d99-m5-agent-studio`): the fresh target-host qualification
requirement of the 2026-11-16 answer 2 is explicitly waived by owner override, and
the AP-007 rework (AP-007-F2, PR #312; AP-007-F3, PR #319) is accepted as the
refusal foundation the 2026-11-16 answer 6 required. The resolution activates
nothing: `runtime_activation_authorized` remains false, no `IsolationAcceptance` is
recorded, no dispatch is observed, installed-runtime qualification has not been run,
and the 2026-10-31 M5-P3-01 record is the only machine evidence. The owner
authorized the remainder of AP-007 and authorized AP-008 to start only after AP-007
is recorded done. AP-009 through AP-032, product runtime activation and real
external actions remain unauthorized. This remains separate from product runtime
activation and real external actions.

AP-002 adds an offline, versioned decision/configuration boundary. It does not
activate a provider, runtime, lifecycle coordinator, sandbox, browser, account,
folder, network destination, promotion path or spending authority. Product
runtime activation remains false. Unprovided values stay explicitly pending and
deny affected live actions without blocking unrelated offline work.

The authoritative records are:

- [`product/M5_Autonomous_Production_Operating_Envelope.json`](../product/M5_Autonomous_Production_Operating_Envelope.json)
  — U01–U06, owner hardware statement, inventory recipe, measured-evidence state
  and promotion boundaries;
- [`product/M5_Autonomous_Production_Policy_Examples.json`](../product/M5_Autonomous_Production_Policy_Examples.json)
  — clearly fictional local-deny, media and research/API/CLI/MCP examples;
- [`autonomous_production_decisions.py`](../autonomous_production_decisions.py)
  — strict offline parser, roadmap drift check and fictional-policy evaluator;
- [`product/M5_Autonomous_Production_Roadmap.json`](../product/M5_Autonomous_Production_Roadmap.json)
  — story authorization, claim and delivery state.

Neither AP JSON file is consumed by `app.py` or generated `studio.py`. A valid
example is not a runtime grant. Future implementation must translate an approved
scope through the existing `ScopedGrantStore`, routing consent, execution budget,
action boundary and side-effect ledger rather than bypass them.

## U01–U06 decision register

U01, U02, U03, U04, U05 and U06 are all `resolved` (2026-11-17, owner
decisions; session `arena/2655f148-m5-agent-studio` for U01–U05,
`arena/0efd4d99-m5-agent-studio` for U06). Each blocked-story list is validated
exactly against every story's `blocked_by_decisions` field in the AP roadmap,
and the same revision removes the resolved decisions' blocker lists. Safe work
describes what can be prepared; it does not authorize that story or a live
action. No resolution activates anything, grants a live action by itself, or
qualifies any evidence.

| Decision | State | Blocked stories | Safe work (pending) |
|---|---|---|---|
| U01 — initial specialisation, audience, format and launch order | `resolved` 2026-11-17 | — (removed) | See U01–U05 resolution below. Websites first, video second; audience at pilot time. |
| U02 — installed versions, free resources and qualified headroom | `resolved` 2026-11-17 | — (removed) | Owner statement only; inventory stays `not_measured` until the operator-run recipe records it. |
| U03 — providers, data and spending; separate reasoning-model policy | `resolved` 2026-11-17 | — (removed) | Public read-only HTTPS allowlist only; zero spend; no accounts; hosted inference still excluded. |
| U04 — folders, accounts, network and retention grants | `resolved` 2026-11-17 | — (removed) | All AP I/O confined to `~/M5_AP_Workspace`; versioned retention (deliverables kept, source snapshots ≤30 days, transient pruned on mission close). |
| U05 — promotion classes, thresholds and review policy | `resolved` 2026-11-17 | — (removed) | Automatic promotion stays OFF in every class; owner approval required; no flags enabled. |
| U06 — lifecycle/sandbox qualification and activation | `resolved` 2026-11-17 | — | Resolved by owner decision; see below. Legacy defaults and activation false remain binding until an activation step is recorded with its own evidence. |

The JSON also names blocked **actions**, not only story numbers. It denies launch
claims, target-host capacity claims, real external data/spend, real account/path
access, automatic persistent promotion and unqualified execution respectively.
A decision moving to `resolved` must provide a non-empty resolution and remove
its blocker lists in the same reviewed revision; the validator rejects a partial
or contradictory transition.

### U06 owner input recorded 2026-11-16 (not a resolution)

The repository owner answered the six U06 decision questions with explicit
selections in the owner session of 2026-11-16T03:34:32Z; the answers are
recorded at the owner's instruction by this session's agent and are
pre-decision owner input, not a resolution:

1. **Backends:** container backends only — the runtime/image pinned in the
   M5-P3-01 evidence — count as backend-qualified for AP purposes. Native
   lifecycle, browser worker and host/disabled execution remain refused for AP.
2. **Evidence:** a fresh qualification run on the target host
   (`scripts/qualify_environment.sh`) is required before U06 can be recorded as
   satisfied; the 2026-10-31 M5-P3-01 record alone is not sufficient for AP use.
3. **Scope:** consequential mission actions may never dispatch without sandbox
   admission — admission-then-dispatch, never a host-execution fallback;
   admission failure means refused (fail closed).
4. **Activation order:** contained execution → browser worker → native lifecycle;
   each step activates only with its own recorded evidence, and everything not
   yet activated remains refused throughout.
5. **Recording:** the repository owner records and signs the U06 resolution in
   the AP ledger via this agent session, drawn from these explicit answers. No
   ADR register entry is created or reviewed by this recording.
6. **Refusal foundation:** the AP-007 offline fail-closed subset is **not**
   accepted as the refusal foundation. Rework is required before activation
   questions can even be asked; U06 is recorded as needing AP-007 rework first.

None of this moves any authority: U06 remains `pending` with `resolution: null`,
the blocker lists and blocked actions are unchanged,
`runtime_activation_authorized` remains `false`, AP-007 remains recorded
`blocked` on U06 (and its remainder plus AP-008…AP-032 remain unauthorized),
and ADR-0040, ADR-0041 and ADR-0042 (the AP-007-F1 fence-scope decision, `review_status: not-reviewed`) remain `not-reviewed`. These answers are owner input
to be carried in the reviewed revision that eventually resolves U06 — they
authorize no activation, no dispatch and no story.

*Superseded in part by the 2026-11-17 resolution below: the fresh-run requirement of answer 2 is explicitly waived by owner decision, and answer 6's refusal is lifted by the owner's acceptance of the AP-007 rework (AP-007-F2/F3) as the refusal foundation. Answers 1, 3, 4 and 5 are retained.*

### U06 resolved 2026-11-17 (owner decision; supersedes and carries the 2026-11-16 answers)

The repository owner resolved U06 on 2026-11-17 (UTC), recorded via agent session
`arena/0efd4d99-m5-agent-studio` on the owner's explicit instruction (after the
blocker report in issue #322 and the instruction “take required steps to unblock AP
program and complete it asap”). The selections, made by the owner in that session
and recorded verbatim in issue #322 and in `authorization_history` of the AP
roadmap, are:

1. **Backends:** container backends only — the runtime/image pinned in the
   M5-P3-01 evidence. Native lifecycle, browser worker and host/disabled
   execution remain refused for AP. (2026-11-16 answer 1, retained.)
2. **Evidence:** the requirement of a fresh qualification run on the target host
   (`scripts/qualify_environment.sh`) before U06 could be recorded satisfied is
   **explicitly waived by the owner's 2026-11-17 decision, superseding the
   2026-11-16 answer 2**. The 2026-10-31 M5-P3-01 record (12/12 checks; Apple
   M5 arm64, macOS 26.6.1, Docker Desktop 29.5.3) remains the only machine
   evidence; no fresh run exists; the evidence gap is accepted by the owner on
   the record. This resolution is the owner's recorded decision, not a fresh
   measurement.
3. **Scope:** consequential mission actions dispatch only after proven sandbox
   admission — admission-then-dispatch, fail closed on refusal, never a
   host-execution fallback. (2026-11-16 answer 3, retained.)
4. **Activation order:** contained execution → browser worker → native
   lifecycle, each step activating only with its own recorded evidence, and
   everything not yet activated remains refused throughout. (2026-11-16
   answer 4, retained.)
5. **Recording:** the owner records and signs the U06 resolution in the AP
   ledger via this agent session, drawn from these explicit answers. No ADR
   register entry is created or reviewed by this recording. (2026-11-16
   answer 5, carried out.)
6. **Refusal foundation:** the AP-007 rework — AP-007-F2 (proven sandbox
   admission, PR #312) and AP-007-F3 (three offline gaps, PR #319) — is
   **accepted by the owner on 2026-11-17 as the refusal foundation**,
   superseding the 2026-11-16 answer 6 refusal. The acceptance activates
   nothing by itself.

Consequences, all binding: `runtime_activation_authorized` remains `false`;
no `IsolationAcceptance` is recorded; no dispatch is observed;
installed-runtime qualification has **not** been run on any host (this
session's sandbox is a Linux x86_64 host with no container runtime and is not
the Apple M5 target); ADR-0040 through ADR-0045 remain `not-reviewed`; no
owner-independent security, screenshot, media, signing or release approval is
implied; the blocker lists of U06 are removed in the same reviewed revision.
AP-007-R1's observer seam landed through PR #327, and the separate docs/ledger
completion record records AP-007 `done` for its authorized scope only. This
completion claims no production caller, `IsolationAcceptance`, live dispatch,
runtime activation, or installed-runtime qualification; the owner's fresh-run
waiver and accepted evidence gap remain explicit. AP-008 is the owner-authorized
next story after the completion record lands, but stays `not_started` until a
fresh branch/PR claims it; AP-009 and later remain unauthorized.

### U01–U05 resolved 2026-11-17 (owner decisions; supersedes “suggestion” notes)

Resolved by the repository owner in the 2026-11-17 session on explicit owner
selections; recorded via agent session `arena/2655f148-m5-agent-studio`, with
the owner selections verbatim in GitHub issue #330. No ADR register entry is
created or reviewed by this recording. Nothing here activates, qualifies or
grants anything beyond what each resolution says.

- **U01** — Websites first: the AP pilot and the AP-021/AP-022 direction target
  a website draft workflow; short-form video (AP-023 → AP-024) is the second
  specialisation and follows the website pilot. The audience, niche and exact
  format are chosen at pilot time and are not fixed by this recording; rubrics
  and benchmark fixtures stay generic until then. Website-first is an owner
  selection, not a suggestion. This does **not** grant launch approval,
  publishing/delivery authority or creative/editorial acceptance; non-website
  specialisations and guest-desktop (AP-030, optional) remain unselected.
- **U02** — Recorded as an **owner statement, not a measurement**: the AP
  pilot target host is Apple M5 / 24 GB unified memory / 1 TB storage (the
  2026-11-08 owner statement is unchanged). Nominal memory/storage are never
  treated as available or qualified; installed runtimes, available
  memory/storage and qualified headroom remain `not_measured` until the
  operator-run `scripts/qualify_environment.sh` recipe records them on the
  named target; sequential load/unload remains the intended architecture; no
  download, load, benchmark, performance or concurrent-residency claim follows.
- **U03** — PUBLIC read-only web research only: HTTPS `GET` to the standard
  public allowlist `github.com`, `codeload.github.com`, `api.github.com`,
  `en.wikipedia.org`, `pypi.org`, `registry.npmjs.org`. Zero spend; no accounts,
  signup, writes, posting, uploads or credentialed services; anything not
  listed is denied; reading a page is not installation approval and page
  instructions remain untrusted data. Hosted model inference stays excluded for
  every AP purpose; external-reasoning-model use stays a separate policy
  decision; media/credentialed/purchase decisions still require a new explicit
  register entry.
- **U04** — All AP reads/writes are confined to one dedicated folder,
  `~/M5_AP_Workspace`; nothing outside it is ever read, written or deleted for
  AP work; no accounts, credentials or browser profiles; network destinations
  limited to the U03 allowlist. Retention is versioned and binding:
  deliverables and verification evidence are kept at versioned scope; captured
  source snapshots are archived or removed after at most 30 days; transient
  downloads are pruned on mission close; nothing outside the folder is deleted
  by AP work; the owner may prune manually at review.
- **U05** — Automatic promotion stays **OFF** in every class. The owner
  declined the offered conservative `project_lesson` automatic-enablement and
  chose definition-with-all-flags-off. `default_effect` stays
  `deny_promotion`; every `automatic_promotion` flag stays `false`; review
  boundaries stand (shadow=none, project_lesson=owner,
  executable_skill_or_adapter=owner_and_security,
  authority_evaluator_or_runtime=independent_and_owner); promotions require an
  explicit owner approval ceremony with ledger evidence and a recorded
  rollback; no thresholds are auto-enabled; any future automatic class requires
  a new register entry naming class, thresholds and boundary first.

AP-008 remains the only story claimed (PR #329, in flight). Resolving these
decisions does not authorize AP-009 or any later story: each still needs its
normal story authorization recorded before claim work begins. The P2 spine
(AP-009 “source-backed web research”) is now dependency-ready as well as
decision-clear, and remains `not_started` until the owner authorizes it.

## Hardware and runtime inventory recipe

### Evidence separation

The owner-stated pilot target is recorded as **Apple M5 / 24 GB unified memory /
1 TB storage**. Its byte fields are nominal normalization only. They are not
observations of available memory, free storage, installed runtime versions,
qualified headroom, model fit or physical release.

`measured_inventory.status` is currently `not_measured`; every observation field
is therefore null. The validator rejects nominal values smuggled into a
`not_measured` record. A future record uses one of two distinct states:

- `observed_unqualified` — exact timestamp/collector, generic
  `HardwareProfile`, free storage and runtime versions are present, but qualified
  headroom and qualification record remain null;
- `qualified` — those observations plus a non-negative headroom value and the
  exact owning qualification record are present.

The schema accepts generic operating systems, architectures, accelerators and
runtime names. It does not hard-code M5 or infer compatibility from a version.

### Operator-run collection sequence

1. Run on the named target host with an operator-visible, local-only collector.
   Record collection time, collector version and configuration fingerprint.
2. Populate `hardware_capabilities.HardwareProfile/v1` from observations:
   operating system, architecture, physical/available memory, pressure,
   accelerators and every installed runtime's exact observed version.
3. Measure free storage independently. Never subtract guessed model sizes from
   the nominal 1 TB statement.
4. Run the owning installed qualification one runtime/model/configuration at a
   time. Record blocked, skipped, unavailable and stale outcomes; do not convert
   them to success.
5. Add qualified headroom only when the qualification record binds the host,
   Studio revision, runtime, model/digest, quantization, context and collector.
6. Re-run after any material host, runtime, model, context, configuration or
   free-resource change. Inventory never authorizes activation by itself.

The existing macOS profiler and `HardwareProfile` are reusable inputs, but they
do not currently supply free storage, complete runtime-version inventory or
qualified headroom. The current Linux sandbox is not substituted for the owner
host.

## Fictional grants and budgets

Every example is marked `fictional`, versioned and complete across capability,
provider, egress destination, account, read roots, write roots, spending,
expiry, use count, data classes and retention. Missing fields fail validation;
missing grants deny access.

| Fixture | Purpose | Resulting boundary |
|---|---|---|
| `fictional-local-offline-deny` | Local default with no external scope | Any external/provider/account/path/spend request returns `no_explicit_grants`. |
| `fictional-reviewed-media-export` | Demonstrates a future explicitly reviewed non-inference media service | Only the fictional provider, HTTPS destination, account, confined input/output paths, declared data classes and USD 25.00 ceiling match. It grants no reasoning model. |
| `fictional-scoped-research-adapters` | Demonstrates future API, CLI and MCP integration under one narrow research example | Zero spend; exact fictional provider/destination/account/paths/data/retention only. Discovery or adapter presence is not activation. |

`evaluate_policy()` is a pure example evaluator. It issues and consumes no real
grant, performs no network or filesystem operation, and does not mutate a
budget. It checks all requested scopes against one matching grant; combining
permissions from several partial grants cannot produce an allow. External
reasoning requests are denied unless a future record names a separate explicit
policy ID.

The example budget uses integer minor currency units instead of an unlabelled
float. A zero spending ceiling requires `currency: null`; a non-zero ceiling
requires a three-letter currency. The grant ceiling cannot exceed its enclosing
fixture budget.

## Promotion boundaries

U05 is resolved with automatic promotion OFF everywhere, so the register's
default stays `deny_promotion` and every automatic flag stays false:

| Class | Persistent effect | Minimum represented boundary |
|---|---|---|
| Shadow candidate report | none | none; it remains an ephemeral report and cannot promote itself |
| Project lesson | project retrieval/prompting | owner review |
| Executable skill or adapter | executable project behavior | owner and security review |
| Authority, evaluator or runtime | system authority | independent and owner review |

These are representation boundaries, not approvals. Existing evidence-backed
lesson and trusted-transformation gates remain authoritative; AP-002 does not
weaken or duplicate them.

## Compatibility, rollback and generated files

The change is additive. It introduces no API, database migration, UI control,
provider credential, executable adapter or background job. Rollback removes the
AP-002 module, two AP JSON records, tests and this page, then restores only the
AP tracking changes; AP-001 history and all owning-program ledgers remain.

`studio.py`, route/security matrices, subsystem inventories and UI manifests are
not canonical outputs of these files and must not drift. The AP ledger and AP
operating-envelope JSON are hand-maintained authority/evidence records, not
standalone generator outputs.

## Post-merge completion record

The repository owner explicitly requested the merge after reviewing the AP-002
handoff. Final PR head `af67df404f07caefb176d054672dbc80fd48c79e`
passed both push CI run `42661850062` and pull-request CI run `42661861110`:
Python 3.11, 3.12 and 3.13 plus Browser safety regressions all succeeded. PR
#280 merged at 2026-11-09T17:23:52Z as
`ce2373dd394ae1ecc86c81a58b043f7ff23e47d1`, with the delivered head in
`main` history.

The immediate post-merge push run `42664215927` was concurrency-cancelled when
the scheduled run `42665031438` superseded it on that same merge SHA. The
superseding run passed all four jobs. This was scheduling, not a failed test.

Completion-record PR #281 was refreshed from current main
`c0484cc64e4cd9934a0eb9d6775524073c217324` after PR #277 landed. That runtime
and generated-evidence change does not overlap the three AP-002 closeout files.
The refreshed scoped selection again passed all 518 tests before the closeout
head was pushed.

All five acceptance criteria below have versioned artifact, documentation and
test evidence, so AP-002 is `done` under the repository convention. Completion
resolves the story, not U01–U06 (at the time of that record; U06 was resolved
on 2026-11-17 first, and U01–U05 were all resolved on 2026-11-17 later the same
day — see the U01–U05 resolution above and the operating JSON records): those
owner values were explicitly pending,
target-host inventory remains `not_measured`, automatic promotion remains off,
and no provider, account, file, egress, spending, runtime or lifecycle authority
is activated. AP-003 was only the next authorized, separately claimable story
at this completion point; it later delivered through PR #284 and has its own
scope and evidence. AP-004 was authorized only after AP-003 delivery and the
later green merge sequence; that later authorization does not widen AP-002.
Completion-record PR #281 passed its current-head CI before landing.

## Acceptance mapping

| AP-002 acceptance criterion | Evidence |
|---|---|
| Primary/next specialisation and audience selected or explicitly pending | U01 is explicitly `pending`; its exact affected stories/actions and safe generic work are recorded. |
| Owner M5/24 GB/1 TB recorded; actual versions/resources/headroom separate and platform-independent | Owner statement, three-state inventory boundary, exact recipe and generic `HardwareProfile` validation above. Current measurements remain honestly `not_measured`. |
| Provider spend/egress/account/file grants explicit and versioned; absence denies | Three fictional fixtures plus strict all-fields parser and combined-scope policy tests. |
| Automatic promotion classes/review boundaries without inferred consent | U05 policy table; all automatic flags false and persistent effects require review. |
| Unresolved decisions map exact blocked stories/actions and safe work | Source-bound U01–U06 table; validator compares questions and blocker lists with the AP roadmap. |

## Verification

Run the offline register validator with:

```bash
python autonomous_production_decisions.py
python -m pytest -q tests/test_autonomous_production_decisions.py \
  tests/test_hardware_capabilities.py tests/test_scoped_grants.py \
  tests/test_routing_policy.py tests/test_routing_consent_policy.py \
  tests/test_execution_budget.py
```

The validator reads only the three repository JSON records. It does not inspect
the host, load a model, contact a provider, resolve DNS, access an account or
issue a grant. Full command outcomes and merged delivery evidence are recorded
in the AP ledger and completion record above. Target-host inventory and
qualification, real service grants, and owner resolution of U01–U06 is recorded in the
register (U06 on 2026-11-17; U01–U05 on 2026-11-17). Target-host inventory and
qualification claims remain unmeasured rather than being inferred from offline tests.
