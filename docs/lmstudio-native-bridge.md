# LC-06: Dormant first-class LM Studio instance adapter

Status: **offline implementation under review; not installed support or activation**.
Modules: [`lmstudio_lifecycle.py`](../lmstudio_lifecycle.py),
[`lmstudio_protocol.py`](../lmstudio_protocol.py).
Related: [plan](cross-runtime-model-lifecycle-plan.md),
[feasibility](model-lifecycle-capability-feasibility.md),
[LC-02](model-lifecycle-coordinator.md), [LC-03](shared-memory-admission.md),
[proposed ADR-0036](adr/0036-dormant-lmstudio-instance-boundary.md).

## Scope and non-activation

The adapter composes the dormant coordinator/memory ledger with native v1 model
control and separately qualified exact-instance compatible-wire inference. It has
no production HTTP transport, environment/qualification/activity/proof producer,
live authorizer, automatic registration, startup work, settings mutation, SDK,
model download or process control. Existing generic provider inference and the
standalone/app remain unchanged. oMLX, Ollama and LM Studio remain choices;
no model is forced onto a different engine or converted. Hardware test8 remains
paused/unpassed. No native engine or hardware endpoint was contacted.

This is not completion of all-path enforcement, request-fit, recovery, populated
migration, native quality/speed qualification or independent review. ADR-0036 is
proposed/not-reviewed. No historical roadmap/acceptance/security ledger is advanced.

## Pinned research and deliberate protocol choice

The documentation contract is revision
`8c7159be3ffe364af10aa8ae6f5a7570e9402089` of `lmstudio-ai/docs`, re-fetched for
this implementation. This pin is **documentation, not LM Studio executable source,
installed build identity, an approved upgrade, or a universal version allowlist**.
The API is documented from 0.4.0; this implementation requires an explicitly
qualified stable three-part version at least 0.4.0. A version comparison alone
never qualifies anything; unknown formats, missing native APIs and capabilities
fail without trying v0, `/v1/models`, SDK discovery or another protocol.

- [Native list](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/list.md)
  distinguishes artifact `key`, loaded `id`, effective `config.context_length`,
  other reported settings, format and selected variant. File size and architectural
  `max_context_length` are not active memory/context facts. List does not provide
  artifact digests, stream counts, server identity, ownership, JIT settings or
  late-operation/physical-release proof.
- [Load](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/load.md)
  accepts a model key and supported load parameters, returns an instance ID and
  final config when `echo_load_config:true`. Reported list `parallel` is not a
  documented load argument/echo field. The adapter never invents a setter for it.
  llama.cpp-only knobs cannot silently become MLX settings.
- [Unload](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/unload.md)
  takes an exact instance ID, not an artifact-wide unload-all operation. The
  documented response does not establish authoritative cancellation/physical
  terminality after a timeout; no cancel endpoint is invented.
- [Endpoint comparison](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/2_rest/index.mdx)
  says native `/api/v1/chat` lacks custom tools and assistant-history input.
  [Compatible chat](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/3_openai-compat/chat-completions.md)
  is a better candidate for preserving the assembled conversation. Its generic
  `model-identifier` example is **not proof that an arbitrary installed instance
  ID routes exactly without JIT**. Exact instance routing and request/template/
  tool/reasoning/overflow preservation remain mandatory installed capabilities.
  An echoed response model is not an execution receipt.
- [Authentication](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/0_core/authentication.mdx)
  is optional by default upstream; this adapter requires separate current evidence
  that authentication is required and an authenticated/pinned transport.
- [JIT/TTL/Auto-Evict](https://github.com/lmstudio-ai/docs/blob/8c7159be3ffe364af10aa8ae6f5a7570e9402089/1_developer/0_core/ttl-and-auto-evict.md)
  documents default JIT/eviction/expiry behavior and excludes manual models from
  Auto-Evict's single-JIT-model behavior. That is not cross-runtime exclusivity.
  The adapter refuses enabled/unknown automatic loading/eviction or unexcluded
  expiry; it does **not** change the user's settings to obtain a pass.

No SDK dependency is needed to implement this REST candidate. The existing
[SDK/tokenization investigation](model-lifecycle-capability-feasibility.md#lm-studio)
remains unresolved: SDK conveniences do not fix REST routing/cancellation proof,
and `get_or_load`/implicit discovery are inappropriate here. No SDK or private RPC
is installed, added or adopted. Exact counters stay unavailable until LC-07.

## Trusted qualification versus native observations

Every effect requires matching installed-scope capabilities for native v1,
authenticated control, exact-instance routing, profile binding, strict input,
request semantics, exclusive control, complete visibility and terminality. Source
and offline evidence cannot satisfy installed scope. The runtime must be registered
as local-only, coordinated and LM Studio in the same declared domain.

`LmEnvironment` is a **separate trusted-server attestation** with a current owner
clock, explicit runtime/version/process identity, complete artifact-key/digest
bindings, required authentication, disabled JIT/Auto-Evict, excluded expiry and
exclusive control. These are not native list fields. Its defaults deliberately
block effects; no production producer is supplied. Format is wire metadata, not a
hardware/backend qualification. Unreported engine defaults, digest/variant/config
provenance and memory peaks must be qualified for the actual build/backend/model.

`LmActivity` separately attests exact current intent/instance, zero active streams,
ownership and excluded late effects. Inference completion additionally binds the
executed request digest to that exact instance and a post-EOF timestamp. Native
IDs may be user-controlled/reused across allocations; IDs/list snapshots alone do
not exclude GUI reloads or establish allocation lifetime. Actual instance routing,
incarnation/ownership and exclusive-control provenance must be qualified before
opt-in. Fake positive attestations test policy, not these proof mechanisms.

The synchronous live authorizer defaults absent/deny, must return exactly `True`,
and is rechecked after observations and at completion. It must be bounded,
side-effect-free and validate actual grants/session/policy/revocation. Inference
permits carry the exact prepared-body SHA-256; LC-07 must supply unchanged-request
fit, token/output/reasoning reserves and budget authority for that digest. A hash
is not a count or grant. No model text/client flag may construct these attestations.

The injected transport uses fixed relative paths, approved local host/IP pinning,
existing credentials, no redirects/retries and bounded, cancellation-safe I/O.
Read-only observation callers must separately authorize that metadata access;
constructing an injected bridge is not implicit outbound consent. No token, URL
or credential is supplied by these modules. Raw server errors are never surfaced.

## Native lifecycle and exact configuration

List parsing retains **all** loaded instances of LLMs, embedding and decision
models, including multiple/manual/helper allocations. Unknown/malformed lists,
duplicate IDs/keys, missing allocated config, ambiguous variants and extra
residents cannot become empty or be filtered away. Effects require an exact
qualified artifact/type/format/variant; aliases are not guessed. Multiple loaded
instances block the single-resident policy rather than being unloaded without
consent. No adoption/relabeling of external instances is provided.

The profile binds the LC-03 target/digest/approved context/memory peak and expected
reported final config. Load sends explicit approved context and applicable native
options with `echo_load_config:true`. It requires the exact expected echo, then
fresh verification of the returned instance and the full expected list config
(including `parallel` when qualified). Missing/default-changed/ignored knobs,
changed context, format or selected variant block use. Nothing estimates a memory
bound from file size, increases RAM/context automatically or changes native engine
parameters to obtain a fit. Test contexts/configs are invented, not recommendations.

Before mutation, validate exact coordinator intent, epoch/generation/stage/deadline,
registry binding, authority, current environment/activity and native residency.
LC-03 model dispatch and the per-intent/stage send marker commit atomically. Load
also journals the full approved-profile digest; its returned instance is journaled
with the accepted ACK. Inference/unload must match that owned load record and
warm reservation, even if a caller reuses the same target fingerprint. No prompt,
tool argument/result, credential or native error body enters the journal; native
instance identifiers and bounded metadata/hashes do. A failed transaction sends
nothing. A crash after commit—even before send—does not permit replay.

Load ACK advances only to verify, never inference. Unload targets the exact owned
instance and returns **pending release proof** even for a matching HTTP 200 ACK.
All other statuses, wrong IDs, malformed echo and auth failures retain possible
effects without retry/fallback. Confirmation requires a fresh observation-bound,
complete all-runtime exact-instance/config/ownership/terminal/physical proof.
An empty list cannot exclude a delayed load. Coordinator completion never frees
the model peak: LC-03 settlement remains separate.

Timeout, native error, cancellation, disconnect or mid-stream consumer close holds
possible work and memory; cancel is not physical cancellation. Storage failure can
prevent recording an uncertainty annotation, but cannot turn an existing durable
receipt/unreleased hold into replay authority. Recovery, journal retention and
populated migration remain LC-09/release work; manual rescue is not automated here.

## Inference contract and consumer limits

`prepare_inference` owns immutable text-only OpenAI-wire bytes with an **explicit
instance ID** in `model`. It does not substitute the artifact key, insert a new
context, trim anchors, flatten media, alter tool schemas/IDs/results or relax
reasoning. Native-v1 MCP/stateful history, TTL, media and unsupported top-level
fields are refused. Generation options remain the approved request's values;
full semantic/tool validation belongs to the qualified request/consumer boundary,
not the structural serializer. It must not be inferred from successful JSON parsing.

Inference rechecks the exact loaded-instance/config record, held warm memory peak
and fresh physical headroom before its own atomic no-replay send marker. It sends
only `/v1/chat/completions` and requires the qualified installed route to honor the
instance selector without JIT/reload. Unsupported routing fails before effects;
wrong response instance, context changes or uncertain terminal execution block
completion. No generic compatible-wire fallback is a success path.

The bounded SSE decoder validates JSON packets, response instance, single-choice
ordering, finish reason and `[DONE]`/EOF, preserving original JSON data bytes and
native tool-ID/argument fragments rather than using an ID-dropping XML assembler.
Content/reasoning packets can stream immediately. Once a tool delta occurs, it and
all following packets retain order and wait for closed transport, independent
post-EOF execution/terminal receipt, fresh matching inventory and current authority.
Only then does an atomic transaction release the **request slot**, not warm memory.

Outputs are native JSON packet bytes, **not canonical tool events or executable
tool authority**. LC-08 must provide the reviewed canonical consumer, validate and
assemble complete tools/results, enforce grants and preserve reasoning/usage/IDs.
Text/packets are untrusted provisional model output; never execute text/XML/partial
tools directly. Unknown usage fields are preserved, not fabricated. Native chat,
server MCP execution, SDK inference and non-streaming substitution are not used.
No served executor/UI is claimed to consume this dormant packet dialect today.

## Packaging and verification

The two typed modules are packaged and mapped to conservative full-suite scopes.
They are deliberately not app-imported or standalone-inlined; actual standalone
consumption belongs to LC-08. No active schema/config/public API is changed.

The two new suites exercise 148 offline cases: positive load/verify/inference and
warm retention; auth/default-deny/version/routing failures; multiple/manual/helper
instances; context, config, variant and instance changes; JIT/eviction/expiry
unknowns; late load/cancel; pending unload; proof scope; atomic rollback/no replay;
request digest/owned profile binding; preserved packet/tool identities and SSE
failure/EOF behavior. No hardware/installed qualification is claimed.

### Initial local verification

The initial focused run passed **456 tests** across the seven LC-02–06 suites,
including **148 new LC-06 cases**. Ruff, configured mypy (**69 modules**), docs
(**252 files**), ADR validation, test scopes (**182 modules / 13 areas**) and
`git diff --check` passed. Broader repository checks are recorded separately after
execution; these counts are not full-suite, installed/hardware or review evidence.

### Broader local verification at source commit `500830a`

- Full local pytest: **3,997 passed, 32 skipped, 1 warning** in **791.26 seconds**.
  Historical baseline objects were fetched before the run; baseline ancestry
  validation passed. The warning is the upstream Starlette/httpx deprecation.
  Skips are not passes or native/hardware/browser qualification.
- Nine disposable unsafe source mutations were caught by the new suites:
  accepting all 2xx ACKs, allowing JIT, bypassing the live authorizer, omitting
  memory admission, permitting send replay, swapping the owned profile,
  accepting wrong native-instance/config proof, changing request-fit digest
  approval, and publishing tool packets before terminal proof. The original
  checkout was not mutated; temporary files/logs are not committed.
- Ruff, configured mypy (69 modules), docs (252 files), ADR, conservative scopes
  (182 modules / 13 areas), tracked Python compilation, baseline, governance,
  deferred-security-record consistency, UI source manifest, frontend syntax,
  demo and screenshot-manifest validation passed. No new screenshot capture or
  approval was performed; no security acceptance is implied.
- Standalone, route matrix and subsystem inventory regenerated without drift;
  roadmap generator check passed. Both isolated app and standalone loopback
  startup/auth/origin smoke checks passed. This does not activate/integrate the
  dormant modules into either served artifact.
- The pushed source head's feature-branch CI completed successfully for Python
  3.11/3.12/3.13 and browser safety. Supply-chain build/verify was not repeated
  locally; the CI Python jobs include those checks. Current PR-head full CI is
  a separate merge gate; no merge authorization is implied by these results.

## LC-07 dispatch fit seam

Inference dispatch now additionally requires `admit_request` to return exactly
true for the request lease, body digest and native profile digest. The optional
constructor callback defaults to denial (`request_fit_required`); exceptions deny
as `request_fit_unavailable`. The [full request-fit contract](request-fit-admission.md)
explains ephemeral approval and separate native/policy/memory/terminal checks.
No production consumer/counter is installed. This preflight TTL is not checked
at stream completion; it must not invalidate a legitimate long response. The
existing bridge fixture's true callback is explicitly a fake assumption, not
installed count proof. No native activation or standalone wiring is implied.


## LC08 dormant explicit-load completion

The native permit union now explicitly includes the coordinator's distinct
`LoadLease` for `confirm_load` only. It has no inference digest. Native `infer`
and mutating `send` retain their original inference/transition intent checks;
rewrapping a load lease as an inference lease still fails the durable SQL purpose
fence. Installed native qualification and the live authorizer must explicitly
approve the current control permit; no default authorizer/producer is supplied.

Completion reads current native residency/catalog, checks the actual native load
profile/instance/context and obtains fresh load-lease-scoped all-runtime physical
proof. Sample and proof producers are bounded to five seconds outside writes.
The final real model-memory binding/status, combined budget/sample revision and
control-slot completion are checked in one write. Model memory is not settled or
released. A bare HTTP ACK or an empty/stale list cannot confirm the command.
See [LC08 integration](runtime-dispatch-integration.md) for composition, races and
limits. No future full-request fit, native activation, new HTTP route, settings
change, installed qualification or architecture/security acceptance is implied.

## Explicit release-step role (dormant LC08 continuation)

The native permit now explicitly accepts a distinct `ReleaseStep`, for source
**drain/unload only**, with its own current durable release gate and receipt
prefix. It has no replacement target and cannot authorize load/verify, inference,
counting or load-control completion. Before effects the queued unload request must
be live and match the actual current source/instance and dispatched source hold;
current native qualification/authorization, owned quiescent activity and actual
loaded-profile records still apply. Native unload ACK is not terminal/absence or
zero-attribution evidence. All-runtime physical proof precedes independent real
memory settlement and final kernel barrier retirement. Post-send cancelled/expired
cleanup retains its cancelled/expired request status, never API success.

This is concrete bridge/step-driver support, **not** an installed unload command
actor, HTTP redirection, native qualifier, transport/sample/evidence producer or
production activation. No polling/replay or already-absent success is implemented.

## Optional unload actor continuation

An explicitly supplied `NativeUnloadPlan` can now orchestrate this bridge's
release steps on the same actor lock as load/inference. Canonical confirmation
requires the exact completed live unload row, released actual source hold and
durable final retirement receipt, not ACK or a retired flag. Post-send cancelled/
expired cleanup cannot become confirmed or poison released memory. No default
plan, native producer, HTTP redirection or production activation is installed.
Already-absent success and unload-all/cache remain unavailable.
