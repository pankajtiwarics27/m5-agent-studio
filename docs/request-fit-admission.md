# Dormant full request-fit admission (LC-07)

## Status and scope

This is a reusable, **unactivated** preparation/count/dispatch contract, not a
claim that served workflows are fixed. `request_fit.py` and
`request_fit_admission.py` are packaged and typed, but neither is imported by
`app.py` or inlined into `studio.py`. There is no installed counter,
qualification producer, native transport, live permit producer or app consumer.
All positive counts in tests are explicit synthetic assumptions. The oMLX,
Ollama and LM Studio choices remain available through existing served routing.
No runtime settings, native inference internals, quality policy, context size,
server ownership or authentication defaults change.

[LC-07/08 in the plan](cross-runtime-model-lifecycle-plan.md) separate reusable
preparation from actual all-path consumption. Origin fixtures cover chat,
Autonomous Turn, Harness, Council, Debate, GSD, SDLC, critic, child and model
switch; they do **not** establish those served paths call this API. LC-08 must
wire final assembly at each entry point and prove no input was omitted before
preparation, including the standalone artifact. Existing character trimming and
legacy direct dispatch remain served until that integration; they are not fit
proof and must not be reused as the coordinated-mode implementation.

Test8 is still paused/unpassed (7,326 input tokens versus 4,096 context, HTTP 400,
zero generated tokens). Nothing here authorizes a hardware retry, activation,
merge, architecture acceptance or independent security signoff.

## Immutable assembled request

`prepare_request` accepts **already-final provider body bytes**, a request lease,
an approved `ModelMemoryProfile`, native profile digest, renderer/tokenizer
revisions, explicit output accounting and generation reserves. It preserves the
bytes exactly, including Unicode, ordering, whitespace, complete tool exchanges,
security/system input, tools, history and current user input. It does not render,
summarize, estimate, tokenize, prune tools, choose a fallback or fetch media.

The body is bounded to 4 MiB and parsed with duplicate keys, nonfinite values and
excessive nesting refused. Supported wire labels are `omlx_openai`,
`ollama_native`, and `lmstudio_openai`. Reject unknown top-level fields, stateful
history/integration references, missing required message content, incomplete tool
pairs, mismatched native selector/context/output settings, permissive truncation
or shifting, and unknown output limits. This intentionally narrow contract does
not advertise all provider fields as supported. Inline image structures are
represented without decoding or fetching; remote URLs, unowned attachments and
unsupported audio/video and native media fields outside the supported content
representation are refused. Ollama native preparation remains text-only, matching
its dormant inference bridge; it does not flatten image parts. Presence of media requires a counter
qualified for that complete representation or a qualified upper bound.

The immutable object binds body SHA-256, byte size, wire/profile identity, lease
owner epoch/generation/instance/target and conservative feature/component labels.
Optional `AssemblyNote` references locate skills/memory/handoff/objective content
inside the body; they cannot attest that upstream assembly included everything.
Verification rederives integrity and structure rather than trusting a changed
frozen dataclass. Notes and component counts are informational, not independent
token quantities to add to the full input count.

## Two-stage admission and counting

1. **Before disrupting a source:** a future orchestrator must validate approved
   target context/memory and installed capabilities/renderer/tokenizer identity.
   A matching local tokenizer must already be installed with a bounded admitted
   footprint. No download, hidden model load, source unload or tokenizer probe
   is performed by preparation. This story supplies the contracts, not that
   cross-runtime orchestrator (LC-08/09).
2. **After the target is safely the sole resident:** the dormant service requires
   an active exact `RequestLease`, warm held `MemoryLease`, live policy permit,
   fresh native resident/context/ownership/control evidence and current memory
   observations. Unknown/queued/stale/revoked/changed identity cannot count.
   Counter qualification must be installed-scoped to runtime, version and profile,
   covering `full_request_counter`, `template_parity`, `strict_input`,
   `reserve_semantics` and `bounded_count_workspace`. `may_load` must be exactly
   false; workspace must fit the already-admitted model peak, and the counter
   must preserve the request and match output accounting. Missing qualification
   is refusal before observation/counter effects.
3. A durable `request_fit_count_sent` journal marker commits before the counter
   callback, outside SQLite I/O. Its key is the request lease. Competing/restarted
   callers cannot replay it. A failed marker write causes zero counter effects.
4. The receipt must bind exact body digest, pre-count resident identity, full
   feature coverage, template-inclusive count, exact/qualified-upper-bound
   confidence, fresh post-dispatch timestamp and terminal completion. Fresh
   **post-count** native/context/memory evidence must still match. Policy,
   owner/generation, cancellation/deadline and memory admission are checked again.
5. A fit ticket is process-local, exact-byte/profile/lease scoped, valid for at
   most five seconds and no longer than permit/qualification/observation validity.
   The cache prunes expired tickets and refuses capacity overflow (256 entries)
   rather than evicting valid authority. Restart requires new admission, never
   replay of a sent count. This bound is not a hardware tier or token/context cap.

Observation and counter callbacks run outside write transactions. The synchronous
permit authorizer must be trusted memory-only code; do not perform I/O or nested
coordinator transactions inside it. Existing
machine-domain ownership and shared-memory pressure holds remain authoritative;
counting reserves no second independently loaded model or unaccounted workspace.
The count timeout cannot establish native cancellation or physical release.
Uncertain count, changed post-count evidence, timeout or cancellation keeps the
request/memory hold and durable no-replay marker. Recovery/settlement is separate,
not an automatic retry. A fully terminal over-context refusal also does not
release, resize or settle the resident lease by itself.

## Fit arithmetic and diagnostics

A template-inclusive full-input count is compared against the **actual allocated
context**, which must equal the approved memory context. Do not add template
estimates or sum overlapping component counts. With output accounting `included`,
reasoning is a subset of the output limit; with `additional`, the reasoning
reserve is added and the native thinking limit must match. Input plus the
explicit total generation reserve must fit; equality is allowed. Unknown,
negative, boolean or estimated token quantities are not proof.

`RequestFitDenied.to_mapping()` exposes a fixed code, `retryable: false` and safe
numeric context/count/reserve/confidence/component fields. Shared-memory refusals
map to `memory_unavailable` while preserving committed pressure pauses; unexpected
service/storage failures map to `admission_unavailable`. Neither exposes native exception
text, request bytes, model paths, tool arguments or credentials. Unknown counts
remain null; no fabricated estimate fills them in. A 7,326/4,096 refusal reports
those counts without sending inference. No blind retry, 8K/16K replacement,
anchor truncation, cloud substitution or quality change is introduced. Runtime
reject-without-truncation is defense-in-depth, not preflight proof.

## Separate native inference dispatch gate

The dormant Ollama/LM Studio bridge constructors now accept optional
`admit_request(lease, body_digest, native_profile_digest) -> bool`. It must return
**exactly true** at fenced inference send. Absent approval refuses with
`request_fit_required`; exceptions refuse with `request_fit_unavailable`. No
load/verify/unload operation uses this hook. `RequestFitAdmission.authorizes` is
a memory-only callback safe inside the existing transaction; it does not replace
live policy, native instance/profile/memory/terminal or send-marker checks.

The fit ticket is checked at **dispatch**, not stream completion. A legitimately
long response must not fail just because its preflight ticket expired mid-stream.
Completion still needs the bridge's independent current native/policy/terminal
evidence. Existing bridge tests that bypass counting explicitly label their
always-true fit callback as a fake test assumption. Integration tests separately
exercise absent/approved/changed-byte/expired tickets and long streams for both
bridges. oMLX has no inference bridge consumer in this story and remains denied
without future all-path integration.

## Native source limitations

No official route has been installed-qualified as a full-request counter here.
At pinned oMLX commit
[`2165e75726ba83d9651dbaced61c97728da77bba`](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py#L8394),
`/tokenize` and `/v1/tokenize` acquire an engine lease and call
`get_engine_for_model`: they **can load** and are not harmless cold preflight.
The route rejects chat media/custom templates. The helper constructs a smaller
chat request, merges server template/model settings (including tool-result
limits), and tokenizes rendered messages/tools; equivalence for arbitrary final
inference settings and unchanged input is not established. Its
[request schema](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/api/openai_models.py#L606)
is not the complete generation-body schema. Merely exposing a count or
`max_model_len` is not allocated-context/full-input proof. This endpoint is not
registered, and any qualification admitting possible loading is rejected.

Prior pinned research for [Ollama](ollama-native-bridge.md) and
[LM Studio](lmstudio-native-bridge.md) establishes no qualified exact full-request
REST tokenizer. SDK naming, metadata maxima, messages-only counts and
post-inference usage do not close that gap. No SDK, private/debug renderer,
heuristic character estimator, network fetch or fallback counter is adopted.
Installed facts remain trusted producer attestations, not self-authenticating
proofs. Cooperating-owner locks do not isolate arbitrary GUI/root/same-UID
clients. See [ADR-0037](adr/0037-dormant-full-request-fit.md).

## Validation evidence

Initial focused validation: **138 new request-fit tests**, plus **173 existing
Ollama/LM Studio bridge tests** (311 combined, all passed). Configured mypy passed
for 71 source files; the scope map validated 184 test modules in 13 areas.
Tests are offline with temporary coordinator/memory stores and synthetic count,
media-bound, installed-capability and resident evidence, not native qualification.
Broader regression evidence is recorded separately after execution.


### Follow-up hardening

Final focused validation now includes **157 new tests**: unexpected service/storage
exceptions remain sanitized, shared-memory pressure still commits its pause, and
unknown message fields, native/unowned media fields, server-executed tool modes
and non-function calls cannot masquerade as text-only coverage. These checks
reject rather than strip or convert fields. No production qualification follows.
Nine isolated source mutations (overflow, potentially loading counter, missing
template inclusion, wrong body, missing post-count memory check, ticket expiry,
both native fit gates and count replay) were caught by the new suite. Mutation
copies/logs were disposable and never changed the working sources.
