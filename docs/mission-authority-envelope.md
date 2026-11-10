# Mission authority and root-budget envelope (AP-004)

AP-004 extends the existing action-admission path; it does **not** add a second scheduler,
grant store, mission database, or production runtime. The authoritative path remains:

```text
application consumer
  -> app._run_authorized_tool
  -> execution_spine.DurableActionBoundary
  -> policy + execution profile + scoped grant/revocation checks
  -> mission generation/identity + hierarchical tool-budget admission
  -> durable intent
  -> executor under ActiveAdmissionScope
```

This is offline enforcement only. It does not activate a provider, account, browser, media
service, billable action, filesystem grant, background worker, or AP-005+ behavior.

## Server-generated envelope

`execution_spine.MissionAuthorityEnvelope` is an immutable typed value with:

- authoritative `session_id`, leaf `task_id`, and root `root_task_id`;
- captured session policy revision; and
- captured global policy revision.

`app.capture_mission_authority_envelope` derives all five values from durable
`runtime_tasks` and `ScopedGrantStore`; request bodies and model tool arguments are never
parsed as envelope authority. Mission/root/revision argument names are explicitly included
in `MODEL_UNTRUSTED_AUTHORITY_KEYS`.

Task-bound queue owners capture once when tool calls are parsed, before approval waits, and
thread that same object through harness waves, SDLC sprints, child execution, outbound MCP,
fixed-endpoint research HTTP, skill scripts, and provider-switch evidence. A session/global
revision change therefore invalidates the queued object instead of silently refreshing it at
dispatch. Live revocation checks still run independently, so a scope revocation denies even
when the captured generation has not changed.

Taskless Direct Chat and legacy standalone bridge calls retain their existing policy/profile
admission and do not fabricate a runtime task; when an inbound bridge call is task-bound, it
accepts only the server-threaded typed envelope and uses the same generation/budget fence.
Legacy durable task rows with an empty historical `session_id` remain readable; any non-empty
durable session identity is authoritative and must match.

## Final admission coordinator

`DurableActionBoundary` invokes `MissionEnvelopeCoordinator.admit_mission_action` only after:

1. action-class and grant-provenance validation;
2. task-state and lease-epoch fencing;
3. central policy authorization; and
4. unified execution-profile, barrier, security-incident, scoped-grant, and revocation
   admission.

It runs before durable effect intent and before executor dispatch. A denial is still written
to both the local tool audit and durable action ledger. Missing/mismatched envelopes, stale
session/global generations, stale epochs, cancellation, corrupt/unavailable state, and
exhausted leaf/root budgets fail closed with structured error codes. The active admission
token exposes the validated root and revisions to inner executors but does not make them
client-controlled.

## Atomic leaf/root tool budget

`DurableTaskBudgetManager.charge_hierarchical_tool_call` walks the durable parent chain and,
in one SQLite write transaction:

- verifies the leaf, session, immutable root, and optional leaf epoch;
- parses both budget records and checks cancellation;
- computes both post-charge candidates before writing either row;
- charges a root action exactly once; or
- charges a child leaf and root together.

If either candidate exceeds its tool-call budget, neither row changes. Child join no longer
rolls up `tool_calls_used`, because the root was charged at admission; joins continue to roll
up steps, tokens, and elapsed time. Task-bound workflow loops likewise no longer add a second
manual tool-call debit. Child policy/tool/path/grant subset checks remain live, so central
root charging cannot widen child scopes.

Retries and replacement providers use the same durable root row. They can consume another
attempt but cannot reset counters, substitute a different root, renew grants, enlarge
resource scopes, or reuse an envelope after a policy generation change. Portable handoffs
persist before/after mission-envelope evidence and assert unchanged root/global generation,
expected session generation, budget state, and grant bounds.

## Actual consumers and fixed HTTP reads

API/Harness tools, outbound and inbound MCP, skill scripts, browser MCP, and media MCP already
converge on `_run_authorized_tool`. AP-004 threads the envelope through those consumers.
GitHub and DuckDuckGo inspiration reads previously called `httpx` directly; they now use two
server-only fixed-endpoint tools (`inspiration_http_github` and
`inspiration_http_duckduckgo`) with the same policy/profile/revocation/generation/budget/audit
boundary and an inner `require_active_admission` anti-bypass check. They are not exposed in
model tool schemas, cannot select arbitrary URLs or methods, and make no startup request.
Unknown tools remain denied.

## Compatibility, migration, and rollback

There is no schema migration. Existing `runtime_tasks.budget_json`, task/epoch rows, grants,
revocations, action ledgers, and audit rows remain readable. The only accounting migration is
behavioral: tool calls move from workflow-loop/join rollups to central admission. Steps,
tokens, elapsed time, recovery counters, pause/cancel flags, and verification contracts keep
their existing storage and update paths.

Rollback is a source revert of the additive envelope/coordinator, fixed HTTP adapters, and
central tool-call accounting. It must revert the removed manual tool-call increments at the
same time to avoid under-counting. Already persisted counters and audit history require no
conversion and must not be erased.

## Evidence

- `tests/test_mission_authority_envelope.py` exercises the FastAPI Harness consumer,
  fixed-endpoint HTTP consumers, outbound MCP, browser MCP, MoneyPrinter/media MCP, and skill
  script wrapper with positive and stale/denied cases. It also verifies atomic leaf/root
  charging, no charge on stale generations/revocation, and zero-budget dispatch prevention.
- `tests/test_execution_spine.py` pins durable mission-coordinator denial before dispatch.
- `tests/test_child_delegation.py` pins child authority subsets, root allocation limits,
  cancellation, overrun, and idempotent join behavior.
- `tests/test_portable_handoff_and_switching.py` pins budget/grant non-reset and persisted
  before/after root-generation evidence across provider changes.
- Existing execution-profile, scoped-grant, MCP reconciliation, MoneyPrinter, standalone
  bundle, startup/isolation, and full-suite checks remain required regression evidence.

## Post-merge completion record

PR #292 final head `b146d8d92be71197f64308785c39fd352cd7562e`
passed authoritative pull-request CI run `42776180763`: the source/release gate,
all nine Python 3.11/3.12/3.13 shards, node-set/outcome identity on all three
interpreters, all five browser-safety shards, and every aggregate check. The
same-head push run `42776174104` passed its open-PR-aware selector and branch
aggregates.

The repository owner merged PR #292 at 2026-11-10T07:42:17Z as
`a835231aeba9ab770f30bf460612f60d81e9f132`. Post-merge main CI run
`42777147806` passed the full matrix on that exact merge commit, including 5,323
nodes on each interpreter (5,288 passed and 35 intentional skips), all identity
checks, and all browser-safety shards. The implementation head is present in
`main` history and all five AP-004 acceptance criteria retain executable or
architecture evidence, so AP-004 is `done` under the repository convention.

Completion activates no provider, account, network destination, browser, media
service, filesystem grant, background worker, product runtime, or later AP
behavior. U03, U04, and U06 remain pending. AP-004 requires no screenshot or
playable-media acceptance and claims no independent-security, signing, or
release approval; existing program-wide gates remain binding.

After the merge and same-SHA post-merge CI were green, the owner instructed
“Continue.” That authorizes AP-005 as the next **single** story for offline
durable mission-task composition, dependency/join recovery, and populated-store
migration tests. AP-005 remains `not_started` and unclaimed until this completion
record merges and a separate fixed Arena branch/draft PR establishes its remote
claim. AP-006 through AP-032 and all real external effects remain unauthorized.

### Acceptance mapping

| AP-004 acceptance criterion | Delivered evidence |
|---|---|
| API, MCP, scripts, browser, and media share authority checks | Actual FastAPI Harness, task-bound inbound/outbound MCP, skill-script, browser-MCP, and MoneyPrinter/media consumers converge on `_run_authorized_tool` and are exercised in `tests/test_mission_authority_envelope.py`. |
| Child/retry/provider changes cannot reset or expand root budget/scopes | Atomic durable leaf/root charging, child subset checks, removed join/loop double debits, and provider-handoff before/after evidence pin the immutable root and grants. |
| Revocation/policy generation invalidates queued and stale work | Session/global generation, live revocation, cancellation, task identity, exhaustion, and raced-epoch cases deny before dispatch without partial debit. |
| Network-free startup and deny-by-default remain intact | Fixed HTTP tools are server-only, fixed URL/method, absent from model schemas, centrally admitted, and never called at startup; missing or malformed task-bound authority fails closed. |
| Positive/negative tests use actual application consumers | The AP-004 suite invokes application endpoints/wrappers with mocked transports/executors rather than certifying only standalone helpers or contacting live services. |
