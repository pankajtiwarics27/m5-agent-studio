# LC-08 coordinated integration — in progress

## Current status first

Latest bounded follow-up: [Council/Debate adapter audit](lc08-workflow-adapter-audit.md).
It records the static-owner coverage matrix, coordinated reasoning-only retry
finding, iterator-close fix and offline evidence. Other workflow owners and
production/native qualification remain open.

For a consolidated source/test-backed snapshot at merged PR262 (`fc6a304`), read
[LC08 current status and gap review](lc08-current-status.md). Cold-to-warm inference,
optional explicit load/unload actors, selected cross-runtime/control races and
atomic native/fit-counter interruption holds now exist **dormantly**. Production
composition, all-workflow consumption and installed/independent qualification
remain incomplete; legacy is still the default and test8 is paused/unpassed.

The sections below are chronological checkpoint records, not a cumulative present
claim. Later continuations supersede earlier statements that cold actors, controls
or race tests are absent. Similarly, pending CI/full-suite and parked-P7 notes
refer to their checkpoint: PR262's final local/CI result and main's restored P7
assertions are summarized in the current snapshot. No historical number is fresh
validation of a later head.

## Checkpoint scope (not story completion)

`runtime_dispatch_boundary.py` supplies an immutable internal routing fence used
by `app.py` and inlined by `scripts/build_studio.py`. Its default is **legacy**, so
this change does not activate native adapters, change routing/settings, claim
cross-runtime safety for legacy mode, load/download a model, take over terminal
`ollama serve`, or resume test8. No production backend, qualifier, native transport,
counter, live permit producer or mode-switch setting is installed.

The generated [entry inventory](runtime-dispatch-entry-points.md) includes common
inference, explicit controls, workflow cleanup, MCP summon, child/critic, model
switch probes, GSD/SDLC and calibration callback paths. It is static evidence,
not proof of native reachability, installed behavior or runtime qualification.
[ADR-0038](adr/0038-coordinated-dispatch-integration.md) is proposed, not reviewed.

## Implemented fence

- Coordinated common dispatch runs **before** legacy message sanitization,
  character trimming, hard-coded output/reasoning caps, demo/online probes and
  process-local heavy-model slots. It owns an immutable bounded JSON copy of all
  application messages/tools/temperature/output/reasoning options, without
  modifying them. This copy is **not** the final provider wire/count proof.
- Internal backend composition explicitly binds oMLX/Ollama/LM Studio aliases.
  Generic local/private/proxy providers are not silently treated as qualified
  cloud/native engines. Existing genuinely remote provider routing keeps its
  original security boundary. No provider/model/port implies qualification.
- Explicit load/unload/unload-all/cache controls and workflow unload callbacks
  route through this boundary before legacy probes/demo branches. Missing backend
  yields fixed 503 diagnostics. Only bounded canonical status replies are accepted;
  raw native bodies/`ok` ACKs cannot masquerade as confirmed completion. Workflow
  cleanup refuses pending/uncertain/refused status instead of advancing.
- Direct legacy stream helpers and generic pinned-HTTP runtime effects are fenced
  even with an installed backend. Unknown/failed executors cannot fall through to
  legacy I/O, cloud substitution, demo, trimming or blind retry. No generic HTTP
  client is passed to the composed backend. The pinned transport gate runs before
  DNS/native I/O and does not weaken original outbound validation/pinning.
- Coordinated combo execution and unowned auto-eviction explicitly refuse until
  full consent/fit/domain-aware orchestration exists. This is not a claim those
  features already work in coordinated mode. Legacy default is unchanged.
- Consumer close/cancellation propagates to the backend iterator; the fence does
  not invent native cancellation, request completion, lease release or settlement.
- Standalone carries the same fence and a private namespaced native module
  bundle. Isolated-copy tests exercise the warm concrete executor through real
  dormant kernels with fake producers, without repository sibling imports. This
  is not installed native qualification; no backend/producer is registered.

## Required next work / release blocks

LC-08 is **not complete**. Current gaps, evidence anchors and a bounded next
increment are consolidated in [the gap review](lc08-current-status.md). In brief:

1. Per-workflow complete-input and canonical consumer proof remains incomplete.
   Common dispatch/gateway tests and static inventory do not prove each Harness,
   Hall, GSD/SDLC, child/critic, MCP, cleanup or probe/calibration owner's behavior.
2. No production actor/backend/qualifier/transport/counter/live-proof producer is
   installed. Existing cold inference and optional load/unload actors are dormant
   compositions with synthetic qualification evidence, not missing foundations.
3. Selected explicit-control/inference and cross-runtime races are implemented;
   remaining all-path, external-effect and recovery races still need proof.
   oMLX target/inference, already-absent success, unload-all/cache, coordinated
   combos and unowned eviction remain unsupported/refused.
4. LC09 session/checkpoint/grant/budget/recovery and populated-schema migration,
   LC10 migration/progress UI, LC11 hardware/release/independent qualification
   stay separately gated. CI is not acceptance or activation authority.

The next recommended increment is an executable per-owner workflow/input/consumer
coverage matrix, then a bounded offline audit of one non-gateway workflow. No
acceptance ledger, screenshot approval or independent security signoff is advanced;
test8 remains paused/unpassed. The earlier baseline/fence validation below is
historical; current completed implementation evidence is linked in the snapshot.

## Validation recorded for this checkpoint

- Initial new fence suite: **50 passed** offline, including complete-input
  forwarding, unavailable backend, fixed control errors, pending cleanup,
  generic-HTTP refusal before DNS, direct helper refusal and isolated standalone.
- Existing admission/chat-tool/request-fit/Ollama/LM Studio bridge regression:
  **440 passed**, one upstream Starlette/httpx warning, before the final static
  inventory addition. Broader/final checks will be recorded when executed.
- Initial configured mypy passed for **72 sources**; no production/native
  qualification follows. No live native endpoint was contacted.

### Follow-up checks and evidence refresh

- The fence suite now has **59 passing tests**, adding private-DNS resolution
  after explicit private-network opt-in, unknown/encoded GET paths, owned
  composition containers, rejected boolean backend configuration, and existing
  HTTP token/origin authorization in front of composed controls.
- **496 combined fence/admission/chat-tool/request-fit/native-bridge tests** passed
  before the three added HTTP authorization cases (one upstream warning).
- Five disposable mutations were caught: common-dispatch bypass, control before
  online probe, dropped resolved-private-IP guard, ignored cleanup confirmation,
  and boolean backend installation. Mutation copies never changed working sources.
- The first broad run finished **4,201 passed, 17 failed, 32 skipped, 1 warning**
  in **791.96 seconds**. Failures were source-bound generated inventory/evidence
  drift and a historical UI release test that compared its implementation pin to
  all future HEAD changes. They are not reported as a clean full-suite run.
- Regenerated affected inventories/authorization trace/preservation package through
  their generators, preserving historical unbound approval and unchanged browser
  assets. Fresh existing UIV2-29 backend safety execution passed **300 tests plus
  16 isolation checks**; live container isolation remains unqualified/blocked.
  Derived performance/release records were regenerated without replacing visual
  evidence, CI records, approval references, source pins or acceptance ledgers.
- The historical UI release test now audits **its recorded implementation range**
  instead of forbidding every subsequent backend change forever. Its existing
  current browser-source digest checks and historical visual/CI approval checks
  remain intact. No screenshots were recaptured or newly approved.
- Targeted repaired inventory/performance/release/fence suites: **248 passed**,
  one upstream warning, before the three new HTTP cases. Existing app and
  standalone isolated startup/auth/origin smoke checks passed. Ruff, configured
  mypy (72 sources), ADR/docs, governance, deferred security-record consistency,
  frontend syntax and baseline verification passed. Full history was fetched to
  make the pinned baseline available in this initially shallow checkout.

These are historical fence checkpoint results, not LC-08 completion,
architecture acceptance or permission to activate/merge. Subsequent warm executor
and gateway/standalone integration is recorded below; cold orchestration and
remaining all-path consumption remain open as listed above.

## Warm concrete executor checkpoint

`native_dispatch_executor.py` now supplies a concrete **warm-only** consumer of
actual LC-02/03 leases, LC-07 fit service, and Ollama/LM Studio bridge inference.
It reconstructs final provider bytes from the complete owned application input
and compares them against the separately authorized native request. Counting and
inference use identical bytes/digest; mismatched output/temperature/reasoning or
omitted messages refuse rather than mutate the approved native profile. The fit
callback must be the actual matching service, not a generic true assumption.

A synchronous trusted planner may supply only an already-admitted native job;
it is not a cold transition or arbitrary transport callback. Absent planner,
oMLX inference, explicit controls and cold orchestration remain unavailable here.
No planner/producer/backend is registered. LM Studio `off` additionally requires
an explicit installed-scoped `reasoning_mode_off` assertion; other requested
reasoning levels refuse because a matching native wire control is not established.
No settings are changed or inferred from a model name.

The canonical packet accumulator keeps reasoning separate, assembles bounded
native JSON tool fragments without inventing IDs/arguments, rejects unadvertised
or partial calls, and withholds tool/usage events until the concrete bridge is
fully exhausted, including independent terminal checks. Missing counts/rates stay
unknown. Consumer close reaches the concrete bridge; fit tickets are revoked, not
used to invent memory release. At the initial executor checkpoint, application
canonical consumption and standalone bundling were still pending. The next
section records their subsequent, limited warm integration.

Initial executor tests: **27 passed**, through real dormant coordinator/memory /
fit/native bridge kernels with explicit fake installed/native/counter facts. They
cover positive exact-byte send, changed/unqualified/overflow zero inference,
reasoning changes and canonical malformed-fragment/refusal cases. These are not
installed native qualification or story completion.


## Coordinated native gateway and standalone warm integration

The existing authenticated `/v1/chat/completions` route now branches to canonical
consumption **before** legacy normalization only for coordinated native targets.
Remote/legacy routing remains unchanged. Raw messages, full paired tool history,
system/security anchors, tool schemas and requested generation settings reach the
same exact-byte native preparation/count path. Structural IDs are not regenerated;
LM Studio fragmented argument JSON retains its original string, including whitespace.
Reasoning remains separate from content and is never parsed for tool execution.
Native Ollama prepared history omits OpenAI's `type` tag: LC-07's conversation
classification now accepts that omission only for the native Ollama wire. A
present non-function tag and a missing OpenAI tag remain invalid.

Coordinated native gateway input is intentionally strict: supported top-level keys
are `model`, `messages`, `tools`, `temperature`, `max_tokens`,
`max_completion_tokens`, `stream`, `reasoning_effort`. Unknown semantics (for
example `tool_choice` or `response_format`), a non-boolean `stream`, or conflicting
output-limit aliases return fixed HTTP 400 instead of being silently discarded.
Existing defaults are forwarded, not clamped into an approved profile; the
executor refuses if the actual requested profile is not qualified/identical.
Count/profile/native failures return fixed HTTP 503 diagnostics in non-streaming
mode, or an SSE error without fabricated completion/`[DONE]` on streaming failure.
Unknown native usage stays null/absent rather than prompt-token zero, estimated
counts or invented throughput. No new route, setting, auth or consent exception
is introduced. Tool authorization/execution is not granted by a native packet.

The canonical capability ContextVar is internal to the gateway consumer. Legacy
text tool consumers refuse advertised tools before native planning and unexpected
structural events explicitly (`canonical_consumer_required`); they do not silently
lose IDs or execute reasoning/text as structural authority. Closing a gateway
stream explicitly closes every intermediate iterator down to the real bridge
and resets both per-request ContextVars in the owning task. Unproven native
terminal completion retains uncertainty and peak memory; it publishes neither
held calls, usage nor a successful gateway finish.

`build_studio.py` embeds lifecycle/fit/protocol/executor source into a private,
per-application module package. Sibling imports are AST-rewritten to that package;
modules are registered before execution for dataclass identities. The actual
inlined application persistence and dispatch objects are reused as shims: no
second transaction tracker or mismatching dispatch exception identity. Public
sibling imports are not globally replaced. An isolated copied artifact test
blocks public native sibling imports, verifies the shared transaction guard, and
executes both warm providers through dormant real kernels plus explicit fake
native/counter producers. No cold acquisition or installed qualification follows.
The documentation/build coverage checks include this namespaced bundle, not only
flat inlining.

Expanded executor suite: **52 tests**. Focused executor/request-fit/native-protocol
run: **348 passed**. This includes full history/ID preservation for both gateway
response modes, withheld tools on terminal failure, cancellation propagation,
unknown usage, unsupported-option refusal, legacy consumer pre-planning refusal,
count replay, queued-control serialization and isolated standalone execution.
All provider, memory and counter observations in these tests are explicit fakes.
Full regression and CI results are recorded separately when available.

Pre-full-run combined fence/executor/admission/chat-tool/request-fit/native-bridge
regression: **551 passed**, one upstream Starlette/httpx warning. Ruff, configured
mypy (73 sources), scope mapping (186 modules/13 areas), docs/ADR/governance,
deferred-security-record consistency, baseline, frontend syntax and isolated app
and standalone startup/token/origin smokes passed. Generated source-bound records
were refreshed through their generators; backend safety was freshly rerun:
**300 tests plus 16 isolation checks passed**, with live container isolation still
unqualified/blocked. No visual approval/source pin or acceptance ledger advanced.

### Full offline regression after warm gateway/standalone integration

At `b90e86c7fb796d813e6d534ec9b10e17389576ac`, the first full run completed
**4,272 passed, 1 failed, 32 skipped**, one upstream warning, in 806.80 seconds.
The failure was the historical Council model-default mutation replacing the first
matching assignment anywhere in `app.py`, which now hits the new gateway rather
than the intended summon handler. The test now selects `api_hall_summon` by AST
range, verifies the intended assignment exists exactly once and leaves the new
gateway unchanged. The actual Council missing-model refusal is unchanged. Its
focused suite passed **20 tests**.

A fresh full rerun with that test repair completed **4,273 passed, 32 skipped,
1 warning in 806.79 seconds** on local Python 3.11. The 32 skips are 31 browser
cases requiring explicit Chrome/Puppeteer configuration and the explicitly
opt-in live-container test. Browser CI and Python 3.12/3.13 CI are **not** claimed
by this local run. The warning is upstream Starlette/httpx deprecation.

GitHub check inspection returned HTTP 401, so CI could not be inspected and the
final test/evidence repair could not be published pending reconnection. Warm
implementation through `b90e86c` had already been pushed to draft PR256 before
the connection failed. This validation does not close the remaining LC-08 cold
orchestration/canonical-workflow work, qualify installed producers, authorize
merge/native activation or resume test8.

### Reconciliation with PR255 / main

After publication of `859e1bd`, its Python 3.11/3.12/3.13 and browser-safety CI
checks passed. GitHub access was restored and that commit was published, resolving
the earlier connection/publication blocker. The subsequent owner merge request
encountered conflicts with main's PR255 integration at
`8425082`; the owner then authorized preparing a reconciled head, not merging a
new head without fresh CI and approval.

The reconciliation preserves main's Harness/frontend changes, release-evidence
scope and release-review implementation/tests. No current-browser approval,
historical implementation head or visual evidence was copied over from the
older LC08 base. Conflicting generated JSON/docs were rebuilt using their
existing generators against the combined sources rather than choosing stale
values from either side. The standalone artifact was rebuilt with main's frontend
and LC08's namespaced native kernels. No native executor, mode, setting, qualifier,
producer or cold-control behavior was activated as part of conflict resolution.

Before the reconciliation commit, focused fence/executor/request-fit/Council /
release-review/source-manifest/authorization regression passed **345 tests**, one
upstream warning. Fresh backend safety execution passed **302 tests and 16
isolation checks**, with unprobed live isolation still unqualified/blocked. Ruff,
mypy (73 sources), docs/ADR/scope/governance/security-record checks, baseline,
frontend syntax, and app/standalone isolated startup/token/origin smokes passed.
The main frontend, release-review code/tests, accepted release evidence and
cross-tab/visual-roadmap references match `origin/main` exactly. Full combined
regression and fresh CI for the reconciled head are pending at this checkpoint;
pre-reconciliation green CI is not reused as proof for the new merge result.
LC08 remains incomplete, test8 paused, and no merge is performed here.

## LC08 continuation after PR256: bounded cold-step consumption

PR256 merged the previous dormant checkpoint as `324aea4f05fa9f1af49852a92b2d807395424703`.
Its reconciled head `4355c43` passed 4,280 local tests (32 skipped, one upstream
warning) and Python 3.11/3.12/3.13 plus browser-safety CI. That approval was
consumed by the merge, not carried forward to this continuation branch.

`native_transition_driver.py` adds an internal bounded **cold inference step
consumer**, not a polling scheduler, completed explicit-control route, native
producer or activation switch:

- `ColdAdmission` derives the full feature inventory from the owned application
  input, matches the immutable native/fit profile and generation settings, and
  checks installed-scoped native and non-loading counter metadata. LM Studio
  additionally requires qualified reasoning-off, matching counter/native process
  identity/version and text-only history. No fake instance ID is serialized and
  no count is inferred before LM Studio has an actual admitted instance.
- `enqueue` validates readiness **before** queue/generation/probe/native effects.
  Unknown or loading counters, incomplete template coverage, expired native
  qualifications, unapproved profile/output/reasoning changes all refuse. Shared
  pure native qualification checks are reused by existing bridge send gates.
  `request_fit.request_components` shares the pure feature classifier with final
  preparation; it is explicitly not body validation, a count or fit approval.
- Each `advance` requires the real currently dispatched coordinator step, matching
  live queued request/domain/target/deadline, the exact concrete native job and
  permit, shared coordinator/memory identity, and repeated target readiness.
  Source drain/unload uses the source bridge/profile; target load/verify uses the
  approved target qualification. All sample/proof producers run outside writes.
- Load reserves the actual target peak, refreshes the memory sample, and uses the
  concrete native bridge's once-only send marker. ACK merely advances to verify.
  Verify uses the bridge's all-runtime/instance/profile/ownership terminal proof.
- Unload ACK does **not** release or settle anything. Native all-runtime physical
  confirmation must finish first; LC03 then independently requires terminality
  and attributed zero for the original model lease before settlement. Only then
  can target reservation/load proceed. Interrupted/invalid stages retain holds,
  mark uncertainty where still owned, and do not replay, infer release or retry.
- `TransitionResult` records only completion of that one stage, never a generic
  `status=confirmed` that a workflow/control endpoint could mistake for the whole
  operation being done. Warm exact-body preparation, LC07 count/fit and native
  inference still run separately after a real request lease/instance is admitted.

Packaging, mypy/scopes and the private standalone module bundle include the new
module. The isolated artifact test now blocks its public sibling fallback and
executes both providers' cold load/verify with real dormant kernels and fake
producers, as well as the prior warm inference checks. No default actor, policy
permit, memory observation, counter, native transport or backend is installed.

Initial cold driver evidence: **45 new tests passed**. Combined cold/warm executor,
request-fit, concrete native bridge and routing-fence regression: **494 passed**,
one upstream Starlette/httpx warning. Tests include
empty-domain cold load through warm exact-fit/native inference, both directions
of cross-runtime switching with an active-request/queued-load race, source ACK
held behind independent physical proof/zero attribution, malformed/unqualified
readiness with no queue/probe/source changes, replay/cancellation/authority/expiry,
and retained memory after failed proof. Native/installed/count/physical facts are
explicit fakes, never hardware qualification. Ruff, mypy (74 sources) and scope
mapping (187 test modules/13 areas) passed. Full regression/CI for this new head
remain to be recorded when executed.

**Still open:** app-level cold scheduling/lease-to-warm-executor composition;
explicit load, unload-only, unload-all/cache controls and their races; oMLX step
consumption; cancellation/recovery orchestration and remaining canonical workflow
consumers. The current coordinator models source-to-target transitions, not a
completed unload-only control API. This driver deliberately does not invent such
an API or fake a target to force eviction. No story closure, native activation,
architecture/security acceptance, settings changes, merge permission or test8
resumption follows from these tests.


Continuation environment/checkpoint notes: the fresh checkout initially lacked
full Git history, so baseline and historical release-evidence reads were blocked.
Full history was fetched; pinned baseline verification and the regenerated release
review then passed with zero findings. Docs/ADR and app/standalone isolated
startup/token/origin smoke checks passed. No historical evidence/approval was
replaced to bypass the shallow-checkout failure.

## Bounded inference scheduling and cold-to-warm handoff

At `f099531`, the preceding driver checkpoint passed **4,325 local tests** with
32 opt-in/browser skips and one upstream warning. Python 3.11/3.12/3.13 and browser
CI passed; three disposable safety mutations were caught. These are prior-head
results, not validation of the scheduler changes below.

`native_inference_scheduler.py` now composes cold steps and warm exact-fit/native
inference as an internal `CoordinatedDispatchBackend`-compatible consumer. No
backend/actor instance is installed by default, no settings/mode change occurs,
and its explicit `command` remains unavailable.

- A trusted synchronous plan supplies immutable `ColdAdmission`, domain/deadline,
  concrete transition-job and admitted warm-job factories. The actor never mints
  native permits, qualifiers, counters or physical observations. It rejects plans
  for a different owned call or warm handoffs with a different request lease,
  memory, native qualification, fit profile or counter qualification.
- One actor serializes its streams until native terminal completion or consumer
  close. Lock waiting is bounded by the request deadline and at most 60 seconds;
  it adds no inference timeout/output cap. Waiting actors are in-memory, not a
  durable session queue. Cancellation/deadline before entry does not enqueue or
  cancel the active owner. Readiness is repeated after waiting and at each stage.
- `LifecycleCoordinator.claim_next` gains optional internal `request_id` fencing,
  backward compatible with existing callers. FIFO request selection and matching
  are checked in the same write as claim; it never skips an earlier waiter or
  advances an existing operation for a different target. A pre-query followed by
  an unfenced claim would race with cancellation/other consumers and is rejected
  as an alternative. This fence is not native-operation authority.
- The actor takes at most drain/unload/load/verify/request, without polling,
  background execution, pending-operation replay or automatic retry. Busy behind
  unrelated work it refuses and cancels only its own queued waiter. Other actors,
  threads and controls remain subject to the actual coordinator/memory gates, not
  just the in-process lock.
- Source memory must be actually dispatched, match the native target and remain
  held through physical confirmation and attributed-zero settlement. Target peak
  reservation/verification yields a real admitted instance/request lease before
  invoking the existing warm executor. Same-target sequential requests reuse
  actual warm memory rather than issuing another load.
- Every intermediate native iterator is explicitly closed on consumer close.
  Incomplete admitted work retains domain/memory uncertainty, never fabricated
  terminality, release or retry. A close after independently verified terminal
  usage does not poison warm residency. Failures after step claim but before a
  job producer returns also require reconciliation rather than stranding/replaying
  the stage. Even a fully counted context refusal conservatively retains the
  admitted request/model hold for explicit recovery; this actor does not invent
  a general cancellation/recovery policy.

The existing native gateway is exercised through actual common dispatch, the
scheduler, real dormant cold/warm/fit/bridge kernels and explicit fake native
producers, in both response modes. No generic HTTP client or fallback path reaches
the actor. The private standalone bundle and its isolated no-sibling-fallback test
now also exercise complete scheduled cold-to-warm inference for both providers.
Packaging, types and scoped tests include the scheduler.

The fake count fixture's journal assertion is now scoped to the counted lease,
not the first count anywhere in the database. This preserves the once-only marker
check while allowing multiple actual queued/request histories in the same domain.

Initial scheduler evidence: **34 new scheduler tests** and **2 new coordinator
claim-fence tests**, including concurrency, cross-runtime switching, waiter cancel
and deadline, interrupted active close, verified-terminal close, foreign queue /
cancelled-target operations, unsupported plans/controls, warm-handoff refusal,
producer failure and oversize exact-count refusal without retry/trimming/model
substitution. The final combined scheduler/coordinator/cold/warm/fit/bridge/fence
run passed **573 tests**, one upstream warning, including the final exact-fit
refusal and post-wait expiry cases. Ruff, mypy75 and scope
mapping (188 modules/13 areas) passed; full regression and CI for this head are
pending until executed. No installed native qualification follows from fake facts.

**Remaining LC08 work:** explicit load/unload-only/unload-all/cache control
orchestration and complete races, full oMLX target/inference support, remaining canonical
workflow consumers, production composition readiness and integration with LC09
session/grant/budget/recovery semantics. This is bounded inference composition,
not a durable task scheduler or completion of those controls. Tests1–7 acceptance
and paused/unpassed test8 are unchanged; no merge or native activation authorized.


Scheduler pre-full-run checks: freshly executed backend safety passed **302 tests
and 16 isolation checks**, with live container isolation still unqualified/blocked.
Regenerated source manifest, preservation/performance/release records have no
findings. Ruff, mypy75, docs/ADR and both isolated app/standalone startup/token /
origin smokes passed. A generator command exceeded the tool wait while its child
continued; the child finished, its output was checked with zero findings, and the
remaining checks were run separately, not concurrently rerun. No visual source
approval/pin or acceptance ledger was advanced.


### Source-only oMLX continuation (not oMLX inference support)

`OmlxSourceTransition` now consumes the existing LC04 bridge for **drain/unload
only**, allowing an already-owned oMLX resident to switch to an independently
qualified Ollama or LM Studio target through the bounded inference scheduler.
The driver's real-step, coordinator/memory owner, request/deadline and pre-source
metadata-readiness gates remain mandatory. No model acquisition, counter, native
inference or tokenization support is added for an oMLX target. No synthetic fit
profile, instance identity or architectural context inference is introduced.

LC04 has a different permit and qualification shape. The adapter binds its actual
`step`, native qualification target/context and separately supplied immutable
`ModelMemoryProfile` to the real active source reservation. Wrong owners, profiles,
permits, unqualified/native-busy or helper-unknown sources refuse without target
I/O. Required unload activity/proof/sample producers must be present before send.
The bridge's `pending` response is only normalized to the driver's ACK vocabulary:
HTTP 200/202, empty status or asynchronous unload acknowledgement cannot complete
release. Independent all-runtime physical proof, then independently attributed
zero memory settlement, are still separate mandatory gates. Proof production is
bounded to five seconds; this is not an inference deadline or force-abort policy.

Physical proof may advance the coordinator's unload stage before memory settlement
fails. In that case the source reservation remains charged and the paused memory
ledger prevents even a subsequently claimed target-load step from sending. No
claim that these two independent stores advance atomically is made. Cancellation,
hung/failed evidence and late effects retain uncertainty without polling/retry.

Offline integration and isolated private-bundle tests exercise real dormant
scheduler/driver/coordinator/memory/native/fit kernels with **fake installed facts**.
The source fixture's prior lifecycle-only load is not an oMLX inference/counter
qualification. No producer is installed, no native endpoint contacted, no setting
changed and no architecture/security or hardware acceptance advanced. Explicit
controls, oMLX targets/inference and remaining LC08 workflows stay open.

The previous scheduler checkpoint at `59f1cea` completed **4361 passed, 32 skipped,
one upstream warning**, plus green Python 3.11/3.12/3.13 and browser CI (run
`42491137954`). Its durable result is PR257 comment `6723091816`. New-head full
regression and CI results will be recorded separately after execution.

Source-only pre-full-run evidence: **57 new oMLX composition tests** cover both
targets, HTTP200/202 physical completion, absent target readiness, source scope /
profile/memory mismatches, busy/unknown drains, incomplete/failed/hung proof,
ACK-only refusal, pinned sources, live attribution, cancellation and no replay.
The combined scheduler/driver/executor/fit/coordinator/native/fence run passed
**612 tests**, one upstream warning. Both source-to-target combinations also ran
in the isolated private standalone bundle. Ruff, mypy75, 189-module scope mapping,
docs/ADR, app/standalone startup-auth-origin smokes and baseline checks passed.
Fresh backend safety: **302 tests, 16 isolation checks**; live isolation remains
unqualified/blocked. Regenerated preservation/performance/release evidence has
zero findings, without changing visual approval pins or accepting architecture.
Full regression / exact-head CI are pending at this commit, not inherited from
the earlier scheduler checkpoint.


### Explicit-load intent foundation (not command integration)

The coordinator now offers `enqueue_load`, a separately typed `LoadLease`, and
lease-scoped proof-based `finish_load` / `mark_load_uncertain`. Purpose is durable
and part of idempotency in private schema v3. Mixed load/inference work shares the
same FIFO and exclusive slot; no inference lease/count is fabricated to finish a
load. Fresh complete all-runtime native terminal and exact owned-instance proof
is still required even for an already-warm target. Slot completion does not settle
model memory or establish full inference fit; cancelled/expired controls do not
become completed commands. See the [coordinator contract](model-lifecycle-coordinator.md).

Fit admission, both native inference bridges and cold inference steps now fence
on durable inference purpose. Rewrapping a load slot's fields as `RequestLease`
cannot count/send. An unexpectedly miswired warm scheduler marks its load slot
uncertain rather than stranding or executing it. Private standalone checks cover
both target providers' distinct control slot and proof-based completion, with
explicit fake facts and unchanged charged model memory.

**At foundation checkpoint `3c1f055`, scope was foundation-only.** Commands still
refused at that head; no native load-command orchestrator, oMLX target support, unload-only,
unload-all or cache implementation is claimed. Older private v1/v2 journals are
preserved/refused; a reviewed populated migration is absent and blocks activation.
No active app database, settings, hardware acceptance or review status changes.

Pre-full-run evidence: **35 new offline tests**, and **687 combined** real dormant
coordinator/memory/native/fit/cold/warm/scheduler/fence tests passed, one upstream
warning. Ruff, mypy75, scope190/13 and isolated private-bundle tests passed. Full
regression and exact-head CI will be recorded separately after execution.

The preceding source-only oMLX head `1426970` finished **4418 passed / 32 skipped /
one warning** and green full Python3.11/3.12/3.13 + browser CI in run42494036883.
GitHub access was restored and final evidence was posted as PR257 comment6723486722.


### Dormant explicit native load consumer (Ollama / LM Studio only)

`NativeLoadPlan` is an optional, separately composed plan on the same
`NativeInferenceScheduler`. Without it, commands still refuse. Only `load` is
supported; unload-only, unload-all and cache controls remain unavailable. No
production plan/qualifier/transport/proof/sample producer or public setting is
installed. The existing model-control HTTP route remains oMLX-specific; it is
**not** silently redirected to an Ollama/LM Studio model. oMLX targets still refuse.

`LoadAdmission` validates the immutable empty-options load call against pure
installed native configuration/profile qualification before enqueue/probe/source
disruption, after lock waiting and at each stage. It does not create a synthetic
FitProfile, inference request, instance selector or token count. No inference
input exists for this command: confirmed model residency is **not** assurance that
a future full request fits. Future inference still reconstructs/counts its entire
unchanged input under the existing native/LC07 gates.

The consumer uses real `QueuedLoadRequest` purpose, at most five actual FIFO-fenced
claims (drain/unload/load/verify/load slot), existing concrete native transitions,
and the real dispatched model memory. Source physical release and independent
attributed-zero settlement still precede target reservation. The inference driver
continues to reject a load row passed as `QueuedColdRequest`; purpose cannot be
relabeled to get inference authority. Source-only oMLX release is supported, but
its qualification does not become target/inference qualification.

The final concrete `OllamaLoadCompletion` / `LmLoadCompletion` carries an actual
`LoadLease` native permit, actual model memory and independent proof/sample
producers. Both native bridges explicitly accept that permit only for read-only
load confirmation; native infer/send methods still require their original types.
Qualification, current control lease, native load-profile record and installed
runtime/catalog/instance/context checks precede and follow independent proof.
Native observation/physical proof validation is shared with existing confirmation,
not weakened or replaced by an ACK. Producer calls run outside transactions and
are bounded to five seconds. No native inference timeout/cap is added here.

Fresh model binding/status, combined model/action budget, pressure/headroom and
memory sample revision are checked in the **same final write** as proof-based load
slot completion. A tool reservation arriving during proof invalidates the earlier
sample and cannot produce a confirmed response. This atomic seam does not settle
model memory. Canonical `confirmed` requires a completed uncancelled/unexpired
load row after native completion, never a raw bridge ACK or boolean alone.

Controls and inference on the same actor share the lock through native terminality;
lock waiting stays bounded by request deadline / sixty seconds. Independent
actors, queues and tool reservations remain governed by actual coordinator/memory
transactions, not just that lock. Cancellation, expired readiness, late proof,
factory failures, missing/bad authority and incomplete completion retain uncertain
holds rather than replaying, polling, releasing memory or force-aborting a stream.
A successfully proved/released control does not poison the still-loaded model.

Pre-full-run evidence: **73 new offline tests**; initial combined run **760 passed,
one upstream warning**. The final shared native-proof refactor passed **246**
consumer/native tests. Cases cover cold/warm reuse and later full-fit inference,
control/inference races in both orders, independent actors, both Ollama/LM Studio
switching directions, owned oMLX source release, pressure, late tool reservation,
proof/instance/profile/version/scope mismatches, expiry/cancel, foreign FIFO,
generic bridge refusal, unsupported controls and oMLX target refusal. Both native
load consumers also run in the isolated private standalone bundle. Full regression
and exact-head CI will be recorded after execution, not inherited from older heads.

The preceding foundation head `3c1f055` completed **4453 passed / 32 skipped / one
warning** and full PR CI42496980469 plus push CI42496974814 all green. Durable
results: PR257 comment6723850924. LC08, production composition, full controls,
remaining canonical workflows and LC09 recovery/session/grant/budget integration
remain incomplete. No activation, settings/HTTP API change, native qualification,
merge, architecture/security acceptance or hardware test8 retry is authorized.

After the shared native-proof refactor and final bundle rebuild, the complete
combined suite again passed **760 tests**, one upstream warning. Final scoped
mapping is 191 modules/13 areas; Ruff, mypy75, docs/ADR, both startup-auth-origin
smokes and fresh backend safety302/16 passed. Applicable source, preservation,
performance and release records were regenerated with zero findings; standalone
is 4,214,158 bytes. No visual approval/pin was changed. Full/CI remain pending at
this chronological checkpoint until separately executed.

## Unload-only kernel continuation (private schema v4; dormant)

The preceding explicit native-load checkpoint `2172b4a` subsequently completed
**4526 passed / 32 skipped / one warning** (1256.24s), with exact-head PR CI
42499922167 and push CI42499918241 green on Python3.11/3.12/3.13 plus browser.
PR257 comment6724236422 records that evidence; it supersedes the chronological
pending-full notes above, not the validation of this new continuation.

This checkpoint adds a distinct release-only coordinator primitive, **not** a
native unload consumer. `ReleaseStep` binds the actual owned source/instance and
its actual dispatched model hold without a replacement target. A release goal
never enters load/verify. Full-runtime unload-scoped physical terminal/absence
proof marks physical completion but preserves the domain switching/operation
barrier. The real source hold stays charged until independent LC03 terminal,
attributed-zero settlement. Only `finish_unload`, checking its exact durable
released reservation and current operation identity in the final write, retires
the barrier. Cancellation/expiry never become completed controls; physical success
alone cannot authorize the next load. Uncertainty and restart require explicit
reconciliation rather than native replay.

Private schema v4 preserves/refuses earlier populated versions. No migration,
namespace reset, application history schema change or activation is introduced.
Existing native permits do not accept the new type. Existing load/inference
actors fence miswired release intent uncertain before their native job factories.
No unload command/plan, unload-all/cache, already-absent success policy, HTTP
redirection, defaults, transports, qualifiers or native counters are installed.
See the [kernel contract](model-lifecycle-coordinator.md#lc08-unload-only-release-foundation-dormant-no-native-consumer).

Focused offline evidence: **57 new tests** cover three fake source runtimes,
physical-vs-memory separation, partial/ACK/stale/wrong-scope proofs, nonzero and
nonterminal settlement, paused/unknown memory, forged identities, FIFO/purpose
fencing, active control/inference slots, cancellation/expiry, concurrent finish,
late completion, all restart phases, SQL goal constraints, older-version database
preservation, native permit refusal and both existing actor purposes. The private
standalone kernel test is extended as well. No live runtime/hardware qualification,
LC08 completion, LC09 recovery completion, independent acceptance, merge or test8
retry is claimed. Full regression and exact-head CI for this increment must be
recorded only after execution.

Final pre-full evidence for this continuation: the combined 17-module lifecycle,
native, dispatch, fit and persistence suite passed **924 tests**, one upstream
warning (41.25s). Ruff, mypy75, scope mapping192/13, docs/ADR, baseline/governance/
deferred-security consistency, JavaScript and both app/standalone authenticated
startup-origin smokes passed. The standalone is 4,226,687 bytes; fresh backend
safety302/16 and regenerated source/preservation/performance/release records report
zero findings, with live isolation still unqualified/blocked. Full regression and
exact-head CI are not inherited from `2172b4a` and remain pending here.

## Concrete native release-step continuation (Ollama/LM Studio; still no command actor)

The merged unload-only kernel checkpoint `c88a39c` completed **4583 passed /32
skipped /one warning**, with PR CI42503114905 and push CI42503111157 green;
PR257 comment6724660872 supersedes its chronological pre-full notes above.
That evidence is historical and is not reused for the current source head.

This continuation explicitly extends the two native permit intent unions to
`ReleaseStep`, with a **separate** current release-operation gate. The new role
is source drain/unload only: no target, inference body/count, acquire step,
load/verify authority or request-fit shortcut is fabricated. Durable checks bind
the active dispatched release goal, exact request/purpose/deadline, actual owned
source/instance and dispatched source hold, owner epoch/generation, ready memory
domain, and absence of active inference/control slots. Native configuration,
current authorization, activity/ownership/late-effect exclusion, actual loaded
profile records and native observed source/version/config remain required.
Separate `release_drain` / `release_unload` receipt vocabulary prevents borrowing
acquire receipts. ACK still cannot prove physical unload or release memory.

`UnloadAdmission` validates pure native configuration readiness before queue or
probe, without pretending the action is a load or constructing a fit profile.
`QueuedUnloadRequest` captures the actual source reservation, native instance and
generation, fencing replacement/residency races rather than evicting whatever
later occupies the same name. `OllamaRelease` and `LmRelease` are distinct concrete
jobs, rejected by the existing acquire driver. `advance_release` validates their
actual bridge/coordinator/memory/qualification and consumes one real dispatched
source stage. It reuses the native wire/profile/proof gates, then requires existing
full-runtime physical absence/terminal proof, independently samples and settles
the actual model hold, and only then calls the kernel's durable final barrier
retirement. Proof/sample production is bounded; cancellation/errors preserve
uncertainty/charges, with no polling, replay, implicit recovery or target load.

After a native send, cancelled/expired intent may receive physical completion and
zero settlement to retire its barrier safely. `ReleaseResult.retired` is **not**
canonical API success: cancelled/expired request status remains unchanged. No
canonical unload actor/plan is added; `NativeInferenceScheduler.command` still
refuses unload. Existing load/inference actors retain their unexpected-release
fences. oMLX unload-only permits, already-absent success policy, unload-all/cache,
public HTTP redirection, producers, installed qualification and activation remain
open. Existing oMLX source-to-target release remains a separate acquire flow.

Initial evidence: **78 new offline tests**, **1002 passed /one upstream warning**
in the combined18-module lifecycle/native/dispatch/fit/persistence suite (45.46s).
The isolated standalone executes both actual native release bridge/driver paths,
with fake installed/native facts and transports only. Final full regression and
exact-head CI must be recorded separately after execution; no LC08/LC09 completion,
review acceptance, hardware test8 retry, migration or merge is implied.

Pre-full validation at the final source snapshot: Ruff and mypy75, scope mapping
193 modules/13 areas, docs/ADR, fresh backend302/16, generated source/preservation/
performance/release checks, baseline (after fetching missing shallow history),
governance/deferred-security consistency, JavaScript, demo/screenshot manifest and
both app/standalone startup/auth/origin smokes passed. Standalone4,242,427 bytes.
Release review reports zero findings; live isolation remains unqualified/blocked.
These are author-run checks, not native qualification or independent acceptance.

## Optional native unload-command actor continuation (still dormant)

The preceding release-step checkpoint `cd8683a` subsequently passed **4661 tests,
32 skips,one warning**, with PR CI42506183587 and push CI42506129464 green.
PR259 comment6725073022 supersedes its chronological pending-full notes. Those
results do not validate the current continuation.

`NativeUnloadPlan` is now an optional trusted composition seam on the existing
native actor. No plan is installed by default. Explicit Ollama/LM Studio unload
commands share the same bounded lock wait and actor lock as load/inference, then
capture the actual current source hold/instance/generation through the real driver.
Only the owned FIFO release intent is consumed, at most **drain then unload**;
there is no polling, implicit target load or acquire-to-self. Wrong purpose claims
are fenced rather than converted into release authority. The concrete existing
release driver still requires native configuration/authorization/profile/activity,
full-runtime physical proof and independently sampled actual model-memory zero
settlement before durable final barrier retirement.

Canonical `confirmed` is not a bridge ACK, a `ReleaseResult.retired` boolean, an
empty observed runtime, or a completed row alone. The actor verifies the exact
uncancelled/unexpired unload request, release operation/source/instance/epoch/
generation, released actual source-model hold and unique durable `release_settled`
receipt after barrier retirement. Historical completion stays valid if a different
actor has already claimed a successor; the old callback cannot poison that work.
Cancellation/expiry after send may retire resources safely but cannot become API
success or poison released memory. Interruptions before real retirement preserve
uncertainty, charged holds and explicit reconciliation. Lock-wait cancellation
must neither enqueue work nor release another lock owner's lock.

No default plan/qualifier/authorizer/native client/proof/sample producer, HTTP
model-control redirection, settings/server change or activation is added. The
existing HTTP model controls remain oMLX-specific/unredirected. Already-absent or
unmatched source is refused, not fabricated success. oMLX unload-only,
unload-all/cache, remaining canonical workflows and LC09 recovery/session/grant/
budget completion remain open. Private schema staysv4; migration/reset and
architecture/security acceptance are not introduced.

Initial evidence: **83 new fake/offline tests;1085 passed,one upstream warning** in
the combined19-module lifecycle/native/dispatch/fit/persistence suite (51.88s).
Coverage includes both actors in the isolated private standalone bundle, same-
actor load/inference ordering, independent-actor held-memory/FIFO races, false
retirement/missing receipt, plan/readiness/factory/proof/sample failures,
post-send cancel/expiry, lock interruption, wrong purposes and unsupported controls.
Full regression and exact-head CI must be recorded separately after execution.

Pre-full final-source checks: Ruff, mypy75, scopes194 modules/13 areas, docs/ADR,
baseline/governance/deferred-security consistency, JavaScript, demo/screenshot
manifest, both authenticated startup/origin smokes and fresh backend302/16 passed.
The standalone is4,251,726 bytes; generated source/preservation/performance/release
checks pass with zero findings and unchanged visual pins. Live isolation remains
unqualified/blocked. Five disposable mutations caught skipped durable completion,
ignored cancelled status, missing retirement receipt, bypassed lock acquisition and
stranded pre-driver factory failures (two failures each). No source worktree was
mutated by the fault harness. Full regression and exact-head CI remain pending
at this chronological checkpoint; this is not independent acceptance/activation.

## oMLX source-only unload-command continuation (dormant)

The preceding optional Ollama/LM Studio actor checkpoint `da31140` passed **4744
local tests,32 skips,one warning**, with PR CI42509113652 and push CI42509109538
green. PR259 comment6725456811 supersedes its pre-full notes. Those results remain
historical, not evidence for this new source snapshot.

`OmlxUnloadAdmission` is a distinct source-only readiness type. It carries the
LC04 native qualification and the **actual separately bound model-memory profile**,
not a fabricated Ollama/LM profile, FitProfile, token count or target selector.
Native runtime/profile/context/capability and fresh clock scope are checked before
queue/probe; the driver independently checks the supplied clock, observation-age
policy and exact runtime against its coordinator, plus actual resident/instance/
source reservation. Blocking-load capability is deliberately not inferred or
required for a source-only unload. No oMLX target load/inference is qualified.

The oMLX permit/activity/observation intent union explicitly accepts `ReleaseStep`,
with the separate current release-operation gate. Concrete `OmlxRelease` jobs
compose on the existing optional unload actor and remain incompatible with the
acquire driver. A native source-profile digest is durably recorded with an actual
oMLX load send; release requires that original record to match the held source
profile before drain and mutation. Missing/historical unmatched records refuse;
no record repair, namespace deletion or reset is introduced. Source-only job
profile and actual memory binding must also match exactly.

Release send/ACK receipt vocabulary is disjoint from acquire. A 200 or 202
unloading ACK only records pending work and is required before release confirmation;
it never proves physical terminality or zero memory. Current authorization,
quiescent native activity/helper visibility, pinned/helper/distributed/external/
loading refusals, fresh native status and bound all-runtime absence/terminal proof
remain mandatory. Physical completion retains the barrier until independent real
source-hold attributed-zero settlement and durable final retirement. Post-send
cancelled/expired cleanup may retire resources but cannot produce canonical success.
Errors/cancellation retain charged uncertainty without polling or replay.

No plan, transport, qualifier, authorizer, proof/sample producer or HTTP redirection
is installed. oMLX load/inference on the generic actor, already-absent success,
unload-all/cache, remaining canonical workflows, LC09 recovery and production
activation remain open. Private schema staysv4; a reviewed populated migration is
still absent. Existing oMLX HTTP controls remain unchanged/unredirected.

Initial evidence: **45 new offline tests;1130 passed,one upstream warning** in the
combined20-module lifecycle/native/dispatch/fit/persistence suite (52.21s), including
real private standalone oMLX release composition. Facts/transports are synthetic,
not installed/native hardware qualification. Full regression and exact-head CI
must be recorded separately after execution, not inherited from earlier heads.

Pre-full oMLX source-only checkpoint: all five disposable mutations were caught
(original native profile record, owner clock, release ACK, native physical-proof
version, actual source attributed zero), one failing test each; worktree source
was not mutated. Ruff/mypy75, scope195/13, docs/ADR, baseline/governance/deferred
security, JavaScript and both app/standalone auth/origin smokes passed. Bundle
4,259,968bytes; fresh backend302/16, source64/6/121, preservation21 capabilities/
272 controls/50 routes, performance21/21 and release7/7 with2200 visual/15Compare
passed, zero findings. Demo/screenshot6/6 and visual pins unchanged. Live isolation
remains unqualified/blocked; full regression and exact-head CI still pending at
this chronological note. No activation/acceptance/merge is claimed.

## Exact durable explicit-load confirmation (dormant hardening)

Merged PR259 at0637760 passed **4789 local tests,32 skips,one warning** with
exact-head PR CI42511757760 and push CI42511753659 green. PR259 comment6725824019
supersedes that head's chronological pre-full notes. This is historical evidence,
not validation for the present continuation.

An offline adversarial audit reproduced a narrower explicit-load gap in both
providers: a concrete completion callback returningTrue plus a request row marked
completed could produce canonical confirmed without durable native completion.
The actor now verifies an exact durable completion chain, like the unload actor,
without expanding load authority or qualifying new target types.

Ollama/LM Studio native load completion writes a provider-specific
`ollama_load_verified`/`lmstudio_load_verified` receipt in the **same transaction**
as terminal control-slot completion, only after its existing current authorization,
loaded native profile/instance, full-runtime physical proof and independent memory
budget checks. The journal operation-id field scopes this event to the actual
control-lease ID. Its reference is a bounded SHA-256 identity fingerprint of the
complete control lease and actual model-memory lease. The shared pure hash helper
is neither a proof generator nor an authorizer. Generic kernel `finish_load`, a
callback boolean or a completed request row cannot substitute for native checks.
Receipt-write failure rolls back request/slot completion and retains charged
uncertainty. Cancelled/expired native cleanup may have a verification receipt;
that is not API success.

Before a completion producer runs, the actor requires its own queued request,
domain, target and owner epoch. After the native callback, canonical success
requires that exact uncancelled,unexpired completed load request and original
deadline, released control lease with matching epoch/generation, exact actual model
hold/profile, and exactly one matching provider-scoped receipt. Missing, duplicated
or cross-scope receipts refuse without fallback or retry. No schema change,
backfill, historical receipt synthesis or history reset is introduced.

Confirmation is point-in-time completion, **not a continuing promise of residency
or readiness**. A separately owned successor may already have admitted inference,
become uncertain, unloaded the old hold or loaded a successor. An unchanged old
verified identity remains historically valid even if its actual hold is now
uncertain/released; the actor must not require the domain to remain idle, probe
again, cancel that successor, or pause its new memory. A foreign FIFO control
returned by a miswired claim is refused before producer/native I/O and left owned
by its original caller.

Initial evidence: **48 new offline tests;1178 passed,one upstream warning** in the
combined21-module suite (55.78s), including atomic receipt-write rollback, generic
kernel completion refusal and historical successor races. Private standalone
coverage also rejects missing native receipts for both providers. Native facts
and producers remain fakes; full regression and exact-head CI are recorded
separately when executed. No routes/default producers/settings/transport activation,
oMLX target load/inference, already-absent policy, unload-all/cache or LC09 recovery
is added. LC08 remains incomplete; independent acceptance is not advanced.


Pre-full load-confirmation checkpoint: five disposable mutations were caught
(skipped durable confirmation, active slot accepted, duplicate receipt accepted,
constant identity hash, foreign FIFO control producer), two failures each; no
worktree source mutation. Ruff/mypy75, scope196/13, docs/ADR, baseline/governance/
deferred security, JavaScript, app and standalone auth/origin smokes passed.
Standalone4,264,025bytes; fresh backend302/16 and source/preservation/performance/
release/demo/screenshot checks passed with zero findings. The initial release
artifact generation was blocked by missing shallow Git history; fetching complete
history and regenerating resolved that evidence lookup, without changing approval
pins. Live isolation remains unqualified/blocked. Full regression and fresh
exact-head CI remain pending at this chronological note, not inherited from
merged PR259. No independent acceptance or activation is claimed.

## Claimed lease ownership before warm handoff and refusal cleanup

The preceding durable-load checkpoint5ed0809 passed **4837 local tests,32 skips,
one warning**, exact-head PR CI42514617279 and push CI42514591565 green. PR260
comment6726200217 supersedes its chronological pre-full notes. Those are historical
results, not validation for this continuation.

An offline fault audit reproduced inference accepting another FIFO caller's real
request lease for its warm producer, and inference/unload wrong-purpose refusal
marking a foreign admitted request/control uncertain. The actor now shares a
small request/control **lease identity fence** across load, inference and unload.
It requires concrete request/control lease type, exact queued request/domain/
target and current owner epoch **before** invoking a warm/completion producer,
assigning cleanup ownership or marking an unexpected lease uncertain. Refusing
another caller's claim cancels only the actor's own queued waiter; the foreign
admitted slot and ready model hold remain unchanged, without probe/count/send.
An owned wrong-purpose slot still follows the existing conservative hold path.

The fence is consumer identity validation, not current native/fit/lease authority
or a replacement for their generation/instance/profile/freshness checks. The warm
handoff separately requires its fit service to use the scheduler's **actual**
coordinator and memory-owner objects, not merely compatible objects or an equal
lease from a copied owner. Failure before count/native execution retains only the
owned request's uncertainty and charge. No new recovery, operation ownership,
claim semantics or inference terminal-completion policy is introduced.

Initial evidence: **32 new offline tests;1210 passed,one upstream warning** in the
combined22-module suite (59.62s). The initial18-case audit reproduced14 failures
(foreign producer/cleanup and owner-object faults), with4 existing load-fence cases
passing. Cases include all three actors/both request and control purposes, real
foreign inference send refusal, scope/subclass mismatch, exact owner objects and
owned wrong-purpose holds. Private isolated standalone rejects the real foreign
inference lease for both providers without public native sibling fallback.
Native facts/transports remain synthetic; full regression and exact-head CI must
be recorded separately. No routes/default producers/settings/transport activation,
schema change, oMLX target/inference, already-absent success, unload-all/cache,
LC09 recovery or independent acceptance is added. LC08 remains incomplete.


Pre-full claimed-lease checkpoint: all five disposable mutations were caught
(foreign request identity:10 failures; domain, epoch, fit coordinator and memory
owner:two failures each), without worktree source mutation. Ruff/mypy75, scope197/
13, docs/ADR, compile, baseline/governance/deferred security, JavaScript, app and
standalone auth/origin smokes passed. The new ownership test module is explicitly
mapped to models.providers. Standalone4,265,001bytes; fresh backend302/16,
source64/6/121, preservation21/272/50, performance21/21 and release7/7 with2200
visual/15Compare passed, zero findings, demo/screenshot6/6 and approval pins
unchanged. Live isolation remains unqualified/blocked. Full regression and fresh
exact-head CI are pending at this chronological note; earlier5ed0809 results are
not current-source validation. No acceptance or activation is claimed.

### Integration with concurrent merged UI/P7 work

Main advanced to5fb978b (merged PR258) while the f5a4d38 checkpoint was validating,
creating generated-artifact conflicts and preventing a current PR CI run. Its
partial local full run was deliberately stopped; it is **not** a passed regression.
Merge main into the same assigned branch, preserve static/index.html and UI/P7
changes exactly from main, and rebuild the native standalone plus affected source,
preservation, backend and performance records rather than hand-editing conflicts.
The combined standalone is4,272,345bytes; scope199/13 and docs259 files. Focused
P7/native ownership/load/standalone tests:155 passed (11.17s). Both app/standalone
smokes, source/preservation/performance, JavaScript and demo/screenshot checks pass.

The inherited `P7-PARK-01` register in
[parked P7 work](uiux-2-0-p7-parked.md) deliberately retires21 release-review tests
and instructs retaining historical release-review artifacts, not regenerating
current approval. A diagnostic regeneration exposed3 expected stale source/metric
findings; restore those two artifacts exactly from main, as that register requires.
Do not relabel old release evidence as current or silently unpark/change assertions.
UIV2-30 --check remains deliberately blocked under that inherited owner decision;
this is not a new waiver or fresh release acceptance. Live isolation remains
unqualified/blocked. Full regression and new exact combined-head CI must be rerun;
earlier source-only results do not validate the merged tree.

## Claimed operation/FIFO identity before transition consumers

The previous integrated checkpoint130552c passed **4871 local tests,53 skips,
one warning**; PR CI42517972609 and push CI42517967305 were green. PR260
comment6726656408 supersedes its chronological pending notes. Those are historical
results, not validation of this continuation. The local rehydrated files matched
that remote tree exactly before branch/index metadata was restored; no working
content was discarded.

A follow-on offline audit reproduced a transition-level ownership gap. Acquire
claims carry a target but no durable requesting-owner column; a same-target
queued waiter could previously reach another FIFO caller's stage. Actor producer
failure and wrong-purpose refusal could also mark a foreign acquire/release stage
uncertain. The initial16-case audit reproduced12 failures, with4 refusals already
safe. This is trusted composition fault injection, not an activated remote exploit.

The driver now supplies a **read-only claim identity/FIFO check**. It requires the
exact concrete queued/step types, queued request/domain/target/deadline, actual
current queued FIFO head, dispatched active operation/stage/epoch/generation,
source/instance and current domain operation identity. Acquire additionally checks
its exact durable target and acquire goal; release checks its durable request and
source-memory reference with no target. A shared target alone is not ownership.

Each actor invokes this check before transition/release producers, wrong-purpose
marking or cleanup ownership. Direct acquire/release drivers repeat it in the SAME
current-gate transaction, before effectful native/current checks. Foreign claims
refuse without probe/count/send, memory pause, stage mutation or cancellation of
the foreign request; only the actor's own queued waiter is cancelled. No stale
callback is permission to expire another owner's operation.

This is identity validation, **not** native authority or a new persistent acquire
owner/grant. Native purpose/cancellation/deadline, physical proof, fit, source
memory/profile and settlement gates remain separate and mandatory. It adds no
operation handoff, polling, cancellation recovery, implicit coalescence or schema
change. Existing genuinely owned producer failures retain conservative holds.

Initial evidence: **38 new offline tests;1248 passed,one upstream warning** in the
combined23-module lifecycle/native/dispatch/fit/persistence suite (126.17s).
Coverage includes all three actors, both providers and operation goals, same-target
foreign FIFO, direct-driver repeat checks, stale foreign stages, forged operation
identity and concrete-type refusals. Private standalone rejects same-target foreign
acquire claims for both load and inference, without public native sibling fallback.
All installed/native facts and transports remain synthetic. Full regression and
exact-head CI are recorded separately after execution. No activation/defaults,
HTTP redirection, oMLX target/inference, unload-all/cache, already-absent policy,
LC09 recovery or independent acceptance is added. Inherited P7-PARK-01 remains
unchanged; historical UIV2-30 review artifacts are not regenerated or relabelled.


Pre-full operation-ownership checkpoint: five disposable mutations caught skipped
actor pre-producer checks (14 failures), missing atomic driver repeats (two),
ignored FIFO head (six), ignored source/instance (two), and ignored release source
hold (two). A harness substitution initially produced invalid Python in a lambda;
that harness-only error was repaired and all five valid mutations rerun. Source
worktree was never mutated by the harness. Ruff/mypy75, compile, scopes200/13,
docs259/ADR, baseline/governance/deferred security, frontend JavaScript and both
app/standalone auth/origin smokes passed. Standalone4,276,877bytes; fresh backend
302/16, source64/6/121, preservation21/272/50, performance21/21 and demo/screenshot
6/6 passed. Inherited P7-PARK-01/release-review artifacts remain untouched, not a
current green approval. Full regression and fresh exact-head CI remain pending
at this chronological note; no activation or independent acceptance is claimed.

## Atomic model interruption holds, not stale callback ownership

The preceding81656c2 checkpoint passed **4909 local tests,53 skips,one warning**;
PR CI42543358826 and push CI42543353459 were green. PR260 comment6730019432
supersedes its chronological pending notes. Those are historical results, not
validation of this new source.

A further offline audit found that split failure handling could ignore a failed
current-intent mark and still pause model memory after the old stage/slot advanced.
The initial10-case run had8 actual memory-poisoning failures and2 fixture permit
construction errors; those fixture errors were repaired, not counted as safety
regressions. The completed tests exercise old native callbacks, a real load moving
to verify, and real verify completing before an independent successor is claimed.

`SharedMemoryAdmission.hold_model` now checks exact current intent/resource scope
and writes coordinator uncertainty plus actual model uncertainty/paused ledger in
**one fenced transaction**. Actor and acquire/release-driver cleanup and all three
native bridges use this seam instead of split writes. It creates no native action,
producer or recovery authority. The coordinator's current-step metadata selector
is factored from its deadline gate; native active-step semantics stay unchanged.
A completed/stale request or acquire stage returnsFalse before touching old memory
or a successor. Genuine current request/load slots, active or already uncertain
acquire stages and still-owned release barriers remain held even when their
request was cancelled/expired. Repeated failures never retire a slot or charge.

Ownership is not always a phase check. A targetless release owns its exact durable
request/source-memory barrier across drain/unload until retirement. Similarly,
a physically proved acquire-source unload may advance to load before independent
source-attributed zero settlement. Its exact still-charged source hold remains
fenceable only with the same current operation/epoch/generation/source/target and
durable complete_unload receipt. A released hold or completed successor is not
unpaid settlement and cannot be paused by the old callback. This exception keeps
nonzero/failed settlement conservative, without polling, implicit recovery or a
new target hold. Physical proof, ACK and cancellation still do not prove zero.

Actual model ID/binding/work/epoch/domain/kind/target are validated before writes;
release additionally requires the exact source-memory ID. A failed memory/journal
write rolls back both halves, avoiding a partially held slot or ledger. No model
sample or native operation is performed under the transaction. This is accounting
and failure containment, not dispatch/native qualification or schema migration.

Initial evidence: **56 new offline tests;1304 passed,one upstream warning** in the
combined24-module suite (66.07s), including current cancelled/expired holds, atomic
rollback, wrong model identity, release barrier advancement and unpaid versus
retired source memory, plus current/stale source-only oMLX callbacks. Private standalone exercises both real current holds and
late native callback refusal without public sibling fallback. Native facts and
transports remain synthetic. Full regression and exact-head CI are recorded
separately after execution. No defaults/HTTP redirection/native activation,
oMLX target/inference, already-absent/unload-all/cache policy, LC09 recovery or
independent acceptance is added. Schema staysv4; inherited P7-PARK-01 and its
historical release-review artifacts remain unchanged.

Five valid isolated mutations were caught: ignore current slot (2failures), ignore
acquire stage/dispatch (2), split the fenced hold with an early commit (8), drop
unpaid-source fencing (1) and reuse a released source hold (1). Each ran against
a temporary shadow module, leaving tracked kernels unchanged. These prove test
sensitivity to those local defects, not hardware or independent acceptance.
Bundle4,277,676bytes; app/standalone auth/startup smokes, standalone sibling-import
isolation, Ruff/mypy75sources, docs259files/ADRs, scope map201modules/13areas,
source/preservation/backend/performance contracts and screenshots6/6 pass.

### Main integration after96e602d (supersedes parked status above)

Exact96e602d push CI42547042617 passed, including all Python and browser jobs.
PR CI42547047347 passed browser checks but failed all three Python jobs at
`test_committed_contract_and_handoff_are_exactly_generated`. The actual merge
included main's newly merged PR261 (`c52fac9`, implementation14b1b1f): the owner
had met P7-PARK-01's unblock condition, imported real5fb978b browser/CI evidence
and restored all21 release-review assertions. This was not reflected in this
branch's still-parked local tree. The old full local run was stopped before
completion rather than conflated with validation of the integrated source.

Integrating main reproduces **1failed,20passed** in the now-live review module.
The only derived mismatch is its embedded isolation-smoke temporary workspace
path: main's `sny7xz1t` versus this branch's committed backend run `n_6txc80`.
Regenerating the derived release-review JSON resolves this single field; no
browser evidence import, approval SHA, pass claim, pin, assertion, skip or policy
is altered. The imported evidence, measured roadmap, restored tests and resolved
parking register are inherited unchanged from main. Production security remains
blocked; historical review is not LC08 native or independent acceptance.

The restored review/performance/new interruption tests pass **120/120**; all21
restored assertions are enabled. Full integrated regression and exact-head CI
must be recorded separately after execution. This integration does not merge
PR260, enable native dispatch or complete LC08/LC09.

## Fit-counter interruption accounting after merged PR260

PR260 merged as38a5778 after exact8a1b269 PR42551265711 and push42551259213
completed green on Python3.11/3.12/3.13 and Browser safety. This is historical CI
proof, not proof of this continuation. The separate old local full run has no
completed result in this checkout and is not claimed. Main's PR261 restored
P7-PARK-01 assertions; that resolution and its real imported browser/CI pins stay
intact. No review is re-parked and no native/independent acceptance follows.

The full-request fit-counter failure handler still had split coordinator/model
writes. Offline baseline **15failed**:12 late counter/receipt/observation/cancel
callbacks paused a successor's live model after independently finished request
scope, and3 injected memory-write failures committed coordinator uncertainty
without the matching ledger write. The independently finished request in these
tests is fixture-supplied terminal proof, not physical release inferred from count.
Counter facts remain synthetic, including the generic source-only fit fixture.

`RequestFitAdmission._hold` now reuses `SharedMemoryAdmission.hold_model` with the
exact fit request lease and actual model-memory lease. One fenced transaction
checks still-current scope and model identity, then holds both halves, or rolls
back both. No valid current scope means no successor pause. Current cancelled/
expired requests and actual task cancellation remain charged/active/reconciling;
no new count, inference, observation, settlement or retry runs in the hold.
This does not treat a count receipt as inference/native/physical-completion proof,
issue a fit approval on failure, or change counter qualification, request bytes,
fit arithmetic, TTL, grant, default/routing/schema or native activation policy.

Evidence: **51 new offline tests** across generic fit, Ollama and LM Studio
fixtures; focused4-module suite **237passed in61.53s**, including the private
standalone using actual embedded fit/native kernels, current/stale task
cancellation and no public sibling fallback. These51test cases include
27current live/cancelled/expired counter failures,6real task cancellation cases,
12late successor callbacks,3atomic rollback and3single-fenced-write checks.
Three isolated temporary-module mutations are caught: restore split cleanup
(15failures), drop the hold (30failures,3intentional stale-case passes) and commit
the coordinator half early (3failures). Tracked sources were not mutated for this
experiment. Combined/full suite and exact-head CI results are recorded only after
execution. LC08/LC09 remain incomplete; no hardware/live/independent acceptance,
oMLXtarget/inference, absence/all/cache/recovery or native activation is added.

Combined25-module lifecycle/native/fit suite: **1355passed,1upstream warning in
236.39s**. The command wait timed out while pytest continued; the completed log,
not the timeout, supplies that result. Generated-contract suite108passed in21.01s.
Bundle4,277,493bytes; backend302/16, source64/6/121, preservation21/272/50,
performance21/21/11, restored review7/7/2200/15 with0findings, screenshots6/6,
app/standalone auth/origin/startup smokes, Ruff/mypy75, docs259/ADRs and scope
map202/13 pass. The initial shallow checkout lacked the real pinned historical
baseline/browser-evidence commits; fetching its actual history fixed baseline
and review ancestry checks without changing any pin or test. The regenerated
review differs only in its embedded backend isolation-smoke temporary workspace
path, as required by the newly recorded backend run. Imported release evidence,
measured roadmap, restored assertions and parking resolution are untouched.


## Bounded Hall HTTP summon continuation

Following merged PR264, the [Hall audit](lc08-hall-summon-audit.md) records
one additional bounded owner, full assembled input, retry/owned-close regressions
and save/pin-before-cleanup semantics. Three original-source regressions reproduce;
21 final offline Hall cases include real dormant warm native/fit kernels with
synthetic facts, not installed qualification. The owner matrix leaves26owners
inventory-only. Legacy defaults, native dormancy, LC09 scope, proposed ADR status,
paused test8 and independent acceptance are unchanged.


## Bounded MCP stdio summon continuation

Following merged PR265, the [MCP audit](lc08-mcp-summon-audit.md) records one
additional bounded owner, leaving25inventory-only owners. A reproduced consumer
error left the dispatch source open until finalization; explicit closing repairs
that ownership gap without changing matter/chronicle assembly, defaults or MCP's
cleanup-before-append ordering.22offline cases include admission/audit and stdio
framing plus dormant warm native/fit kernels with synthetic facts, not installed
qualification. HTTP Hall's session guards and retention semantics are not copied
into MCP. Legacy/default dormancy, LC09 scope, proposed ADR status, paused test8
and independent acceptance are unchanged.


## Bounded workspace suggestion continuation

Following merged PR266, the [workspace suggestion audit](lc08-workspace-suggestion-audit.md)
records one more assessed owner, leaving24inventory-only owners. Rejection of
overlong model text previously left the owned source open until finalization;
explicit closing now completes within the existing timeout.22offline cases
cover consent, complete accepted input, no-action/output refusal, context reset,
interruption and dormant warm native/fit kernels with synthetic facts. No folder
is created or workspace switched by a suggestion; original limits/options and
legacy defaults remain unchanged. LC09, paused test8, proposed ADR and independent
acceptance boundaries are not advanced.


## Bounded legacy cleanup failure handling

After merged PR267, a focused audit of `_unload_omlx_model` reproduced normal
return after every HTTP status and swallowed transport failures in its online
legacy branch. It now rejects redirects/non-2xx and fails privately on transport
errors, preserving cancellation; coordinated dispatch still fails closed. See the
[legacy cleanup audit](lc08-legacy-cleanup-audit.md). A 2xx plus the existing delay
is not measured physical release. Offline/demo behavior, legacy defaults, direct
unload route owners and hardware/acceptance status are unchanged. The matrix has
29 owners/54 static sites, six bounded assessments and23inventory-only owners.
