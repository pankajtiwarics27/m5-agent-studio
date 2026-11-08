# LC-08 coordinated integration — in progress

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

LC-08 is **not complete**. The backend protocol is an internal dependency seam,
not authority to perform native operations. The warm executor now uses real
coordinator/memory/fit/native kernels with explicit fake evidence in tests; the
initial fence tests still use fake backends for routing only. Still required:

1. Cold acquisition/control orchestration, with pure LC-07 metadata readiness
   before disrupting a source, actual sole-resident target binding/counting, and
   valid domain operations for every load. The current executor refuses controls
   and accepts only already-admitted warm jobs, never creating load authority.
2. Canonical consumption and complete-input inclusion proof across remaining
   workflows. The coordinated native gateway consumes structural calls; legacy
   text tool consumers explicitly refuse before native planning when advertising
   tools, and reject unexpected structural call events rather than dropping IDs.
3. Full explicit-control/inference and cross-runtime cold race tests, late effects,
   release and settlement. Current executor races cover same-lease count replay
   and a queued control blocked by an active real dormant native stream only.
4. Complete legacy/generic bypass/mutation coverage and final full CI. LC-09 still
   owns session handoff/checkpoint/grant/budget/recovery semantics; LC-10 migration
   UI and LC-11 hardware/release/security qualification stay separately gated.

No acceptance ledger, source screenshot approval or independent security signoff
is advanced. Tests1--7 acceptance is unchanged; test8 remains paused/unpassed.

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
