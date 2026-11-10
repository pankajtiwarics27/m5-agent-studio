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
(42802513165), so AP-006 is authorized as the next single story and remains
unclaimed; AP-007 through AP-032 remain unauthorized. This remains separate from
product runtime activation and real external actions.

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

All six real-owner decisions are currently `pending`. Each blocked-story list is
validated exactly against every story's `blocked_by_decisions` field in the AP
roadmap. Safe work describes what can be prepared; it does not authorize that
story or a live action.

| Decision | Current state | Exactly blocked AP stories | Safe work while pending |
|---|---|---|---|
| U01 — initial specialisation, audience, format and launch order | `pending` | AP-002, AP-021, AP-022, AP-023, AP-024, AP-030 | Generic contracts, fictional fixtures, adapter research and evaluation harnesses. Website-first remains a suggestion, not an owner selection. |
| U02 — installed versions, free resources and qualified headroom | `pending` | AP-002, AP-012, AP-013, AP-014, AP-021, AP-023, AP-026, AP-030 | Hardware-independent contracts, mocked profiles and an operator-run recipe. No download, load, performance or concurrent-residency claim follows from nominal capacity. |
| U03 — providers, data and spending; separate reasoning-model policy | `pending` | AP-002, AP-004, AP-009, AP-013, AP-014, AP-021, AP-023, AP-024, AP-026, AP-029 | Local mocks, source/licence research and explicit deny paths. Future authorised non-inference services remain representable; hosted reasoning remains separately unresolved. |
| U04 — folders, accounts, network and retention grants | `pending` | AP-002, AP-004, AP-009, AP-010, AP-012, AP-021, AP-023, AP-026, AP-029, AP-030 | Disposable relative paths and fictional accounts/destinations; grant, confinement, revocation and retention tests. |
| U05 — promotion classes, thresholds and review policy | `pending` | AP-002, AP-026, AP-028 | Shadow evaluations and candidate reports only. Every `automatic_promotion` flag remains false. |
| U06 — lifecycle/sandbox qualification and activation | `pending` | AP-002, AP-004, AP-007, AP-010, AP-012, AP-014, AP-021, AP-023, AP-030 | Call-path audits and offline refusal, cancellation and uncertain-effect tests. Legacy defaults and activation false remain binding. |

The JSON also names blocked **actions**, not only story numbers. It denies launch
claims, target-host capacity claims, real external data/spend, real account/path
access, automatic persistent promotion and unqualified execution respectively.
A decision moving to `resolved` must provide a non-empty resolution and remove
its blocker lists in the same reviewed revision; the validator rejects a partial
or contradictory transition.

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

U05 is pending, so the register's default is `deny_promotion` and every automatic
flag is false:

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
resolves the story, not U01–U06: those owner values remain explicitly pending,
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
qualification, real service grants, and owner resolution of U01–U06 remain
pending rather than being inferred from offline tests.
