# LC-05: Unactivated native Ollama lifecycle and inference

Status: **dormant fake-tested implementation, not installed qualification**.
Modules: [`ollama_lifecycle.py`](../ollama_lifecycle.py),
[`ollama_protocol.py`](../ollama_protocol.py).
Related: [plan](cross-runtime-model-lifecycle-plan.md),
[coordinator](model-lifecycle-coordinator.md),
[shared memory](shared-memory-admission.md),
[feasibility](model-lifecycle-capability-feasibility.md),
[proposed ADR-0035](adr/0035-unactivated-ollama-native-bridge.md).

## Activation and trust boundary

This is explicitly constructed, injected code, **not a served provider**. No
production HTTP transport, qualification/terminality producer, live authorizer,
configuration, app import, background work, process management, model download,
server restart, automatic fallback or native activation is supplied. The user's
terminal-run `ollama serve` is not a Studio-owned process. Current Ollama discovery
and `/v1/chat/completions` inference stay unchanged. oMLX and LM Studio remain
separate choices; no model conversion or oMLX-only routing is introduced.

Mutations and inference deny by default without an explicit synchronous live
server authorizer returning exactly `True`, rechecked after preflight and at
completion. Permits/installed capability records are trusted attestations, not
self-authenticating grants or native facts. The future bounded, side-effect-free
authorizer must validate actual policy/session/grant revocation, counters and
budgets. For inference it receives the **exact prepared-body SHA-256** in the
permit; a different body cannot use that request-fit approval. Hash equality is
not a token count or full-request fit proof; LC-07 must supply those producers.

The injected transport must enforce approved local URL/IP pinning, authentication,
no redirects, no retries, bounded responses and cancellation-safe closure. Only
fixed relative native paths and JSON bytes cross this interface. Explicit metadata
reads are not implicitly authorized by a permit: their caller/transport must
separately authorize them. There is no automatically constructed transport or
startup probe. No public config/API, authentication/origin/session change, active
SQLite schema change or populated migration is made.

## Pinned upstream evidence and real limitations

Reviewed contract revision: `d63b702609c4300a2ca07be2bdb0adb9834725e5`.
This is a **source shape**, not a recommended upgrade, minimum installed version,
build attestation or native support claim. Source/offline capabilities cannot
satisfy installed scope. Qualification requires matching runtime, version, source
contract, exact target/digest/profile and installed capabilities for native chat,
explicit local source, profile identity, strict input, reasoning/tool identity,
complete visibility, exclusive control and terminality. No installed record is
created here. Positive fixtures invent these assumptions to exercise refusals.

Sources, fetched from the pinned revision without contacting an engine:

- [API structs and option parsing](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/api/types.go):
  native messages have `thinking`, complete object-valued tool arguments, IDs,
  `tool_call_id` and `tool_name`. Unknown options may only warn/skip; typed tool
  schemas discard unsupported keywords. The translator rejects such losses.
- [Chat handler](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/routes.go#L3019):
  empty messages + `keep_alive:0` expires a runner and returns `done_reason:unload`;
  empty messages otherwise schedule/load then return `done_reason:load`. Inference
  can itself load. Explicit `truncate:false` and `shift:false` disable their
  otherwise permissive defaults in the native/legacy execution branches. Thinking
  can be model-resolved; the Harmony `max` setting becomes `high`, so `max` is not
  accepted as identity-preserving here. Native errors never trigger a retry with
  thinking disabled or a different wire protocol.
- [Local-source resolution](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/internal/modelref/modelref.go)
  and [server resolver](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/model_resolver.go):
  an explicit `:local` suffix avoids cloud stubs/proxying. This bridge requires an
  unambiguous explicitly tagged artifact, appends `:local`, and rejects cloud,
  implicit tags and ambiguous suffixes instead of normalizing them silently.
- [Process handler](https://github.com/ollama/ollama/blob/d63b702609c4300a2ca07be2bdb0adb9834725e5/server/routes.go#L2813):
  `/api/ps` reports name/model, digest, runner, allocated `context_length`, expiry
  and accounting sizes. It does **not** report a native allocation-instance ID,
  active request count, physical-release or late-effect terminality authority.
  The separate observation reference is a local receipt nonce, never a fabricated
  native instance ID. Version text alone does not prove the installed executable.

Arbitrary native clients, automatic engine behavior, different owner roots and
malicious same-UID mutation remain outside cooperating-owner isolation. Native
inference routes by artifact/runner, not an exact instance handle: exclusive
control and separate fresh instance/stream evidence are mandatory qualification
gaps, not properties inferred from model names or a lock. No private debug renderer,
admin route or inference warmup is used as a cold token counter.

## Catalog, metadata and active residency

- `catalog()` reads `/api/tags` only. Installed artifacts and remote stubs are not
  warm residents. Catalog membership grants no load authority.
- `describe()` posts `/api/show` with explicit local source only. Architectural
  maximum, template or reported capabilities do not establish allocated context,
  a memory upper bound or request fit.
- `observe()` reads version and `/api/ps`, validates all returned rows, and retains
  only reported facts. Missing/duplicate rows, unknown context/runner, wrong
  digest, aliases, remote indications and additional allocations block use.
  Names must exactly match the qualified catalog identity; alias equivalence is
  not guessed. Current tests exercise context 4,096 vs approved 8,192 as refusal,
  not as a hardware recommendation or automatic context increase.

## Durable lifecycle and approved profile

The native profile binds LC-03's qualified memory peak/context, runner, approved
native options and thinking selection. `num_ctx` must equal that exact approved
context, and `num_predict` is an explicit positive, bounded output reservation.
Unknown, null, wrongly typed or nonfinite options are refused; no engine settings
are invented from RAM, artifact size or `/show`. Every load, inference **and unload**
request carries the same options/context/runner/thinking settings. Load/inference
use `keep_alive:-1` to express coordinated warm retention; unload uses zero.
There is no scheduler TTL tuning or automatic unload of a held stream.

Before POST: validate exact current intent/lease, owner epoch, generation,
phase/deadline, registry backend/local-only coordinated binding, live authority,
fresh exact-instance zero-stream/ownership/late-effect evidence and current native
residency. Load additionally requires no native resident and the exact reserved
memory profile/step; inference requires a held warm model reservation and fresh
physical headroom/attribution. No hidden context expansion or additional pool.

LC-03 dispatch and the per-intent/stage send marker commit **atomically** before
I/O. Load also journals a complete native-profile digest, which inference/unload
must match even if a caller reuses the same target fingerprint with changed
options. Inference has its own lease-scoped one-send marker. Only references and
hashes enter the existing private schema-v2 journal, not prompts, tool arguments,
results or native error bodies. A failed transaction sends nothing. Crash after
commit, even before actual send, does not authorize replay. Journal retention,
installed provenance and populated schema migration remain separate gates.

A qualified blocking load ACK only advances to fresh verify. Unload HTTP 200 with
`done_reason:unload` is **pending release proof**, not release. 202/204, redirects,
400/404, auth errors and malformed ACKs are not universal success. Confirm needs
fresh current native wire observations **and** observation-bound all-runtime,
exact-instance/ownership, operation-terminal and physical headroom evidence with
correct allocated context. Empty `/ps` cannot exclude a late load. The coordinator
still validates the exact post-dispatch evidence scope and registered fleet.
Memory settlement is separate: lifecycle completion never frees the warm peak.

Timeout, cancel, disconnect, native error, consumer close or uncertain proof
retains possible effects and the reservation/request slot; no automatic retry,
forced abort, sleep-based release or target load follows. A successful inference
requires validated EOF/done, closed transport, fresh separate terminal activity,
current matching residency and live authority before atomic request-slot release.
Warm model memory remains reserved. Cancellation during inference may finish as
cancelled only after the same positive terminal proof, never on cancel ACK alone.
Storage failure can prevent an uncertainty annotation; the committed send marker
and unreleased holds still do not become replay authority. Recovery needs LC-09.

## Pure canonical translation and deliberate refusals

`prepare_chat()` owns immutable serialized native bytes and their digest. It
preserves text/Unicode, system/user anchors, assistant reasoning and complete
assistant/tool exchanges, including parallel same-name calls and out-of-order
results matched by ID. Native result messages contain the original ID and tool
name. It does not trim history, flatten media, drop fields or synthesize tool IDs.
Malformed/duplicate arguments/IDs, unmatched results, unfinished exchanges and
unsupported message/tool/schema fields fail before any inference query/send.

This initial text-only dialect rejects images/audio, named non-tool messages,
arbitrary schema constraints, `strict` tools, forced/parallel-choice controls and
other unsupported options instead of pretending OpenAI parity. The supported
schema subset reflects pinned native structs; widening it needs reviewed evidence
and tests. Existing app tools may use unsupported constraints: **do not opt in
until their semantics can be preserved or explicitly refused before admission**.
Thinking `None` preserves the native model-qualified default; booleans and
low/medium/high remain unchanged. The profile/installed producer must attest how
the engine resolves these values, not infer parity from API field presence.

The bounded NDJSON decoder handles arbitrary byte/UTF-8 boundaries, errors,
missing/duplicate terminal records, malformed bodies and nonfinite JSON. Native
calls are whole objects, not OpenAI name/argument deltas. IDs remain distinct even
when indexes repeat; missing/duplicate IDs or unknown tools fail closed. No XML
execution frames or legacy ID-dropping assembler are used. Calls are withheld
until valid EOF and independent terminal confirmation, and never executed here.
Truncated tool generation is not an executable call. Byte/depth/count limits are
explicit refusals, not silent truncation or best-effort parsing.

Canonical events are `{type:content|reasoning,text:...}`, structured
`{type:tool_call,id,name,arguments,index?}`, and terminal `usage`. Usage preserves
native prompt/output counts (including zero), finish reason and nanosecond-based
rates/duration when reported. Native `stop` with calls maps to canonical
`tool_calls` while retaining `native_finish_reason`. Unknown reasoning/cache/TTFT
metrics remain `None`, never invented token counts or zero-performance claims.
These structured tool/unknown-metric events require LC-08 consumer integration;
the legacy served executor/UI are not claimed to consume this dialect today.

The compatible-wire regression runs the **existing** app adapter against
`httpx.MockTransport`, proving its `/v1/chat/completions` request remains unchanged
and lacks this native profile/strict-input control. Separate native fixtures test
whole-object tools, IDs, reasoning and usage; this is a semantic gap test, **not**
an installed quality, speed, context-fit or end-to-end parity benchmark.

## Packaging, verification and remaining gates

Both typed modules are listed in setuptools, mypy and conservative test scopes.
They are deliberately not imported/inlined by `app.py` or `studio.py`. Actual
standalone consumption and regeneration when activated belong to LC-08, not a
claim made by packaging dormant files.

Offline tests cover positive lifecycle/inference, catalog separation, matched
profiles/IDs/reasoning, default denial, capability/epoch/generation/instance/version
mismatch, revoked grants, ACK/status failures, pending unload, timeout/late load,
consumer cancellation, pressure-pausing, transaction rollback, no-replay crash
windows, request-fit digest binding and current observation-bound proof.
No live runtime/hardware test is run; hardware test8 stays paused/unpassed.

Activation still requires LC-06/07/08/09/11, actual transport/proof/authorization
producers, supported installed versions/profiles and tools, exact assembled-request
fit, all-path enforcement, recovery/migration, quality/performance comparisons and
required independent reviews. ADR-0035 is proposed/not-reviewed. No acceptance
ledger, security signoff or hardware checklist is advanced.

### Local implementation verification

The initial local gate ran the two new suites plus LC-02, LC-03 and LC-04:
**308 passed** (156 new LC-05 cases), with no native/network/model dependency.
Ruff, configured mypy (67 modules), docs (250 files), ADR validation, conservative
scope mapping (180 modules / 13 areas) and `git diff --check` passed. Broader
repository validation is recorded separately after execution; this count is not
full-suite, installed/hardware or independent-review evidence.

Broader local verification at source commit `051bf03`:

- Full pytest: **3,846 passed, 33 skipped, 2 failed** in 1,155.87 seconds. Both
  failures were missing historical Git objects in the initially shallow checkout
  (`test_baseline` and `test_uiv2_release_review`), not source/test logic. After
  `git fetch --unshallow origin main`, baseline ancestry validation passed and
  **all 22 tests** in those two suites passed. The entire 19-minute run was not
  repeated locally; this is not reported as a second all-green full run. The run
  also emitted one upstream Starlette/httpx deprecation warning. Skips are not
  passes or native/hardware qualification.
- Seven disposable source mutations were caught by the new suites: enabling
  truncation, accepting all 2xx ACKs, omitting shared-memory admission, allowing
  send replay, accepting changed load/inference profiles, bypassing the live
  authorizer, and replacing native tool IDs. Original checkout files were not
  mutated; temporary experiment files are not committed.
- Baseline, Ruff, mypy, docs, ADR, scopes, governance, deferred-security-record,
  UI source manifest, frontend syntax, demo, screenshot-manifest and tracked-Python
  compilation checks passed. Manifest validation does not recapture or accept new
  screenshots. Both isolated app and standalone startup/auth/origin smoke checks
  passed.
- Standalone, route matrix and subsystem inventory were regenerated without
  drift; the roadmap generator's check also passed. Dormant modules remain
  deliberately unconsumed by the served app/standalone.
- The pushed source commit's feature-branch CI completed successfully on Python
  3.11/3.12/3.13 and browser safety. PR-head CI remains a separate merge gate;
  no merge authorization is implied. Release supply-chain build/verify was not
  repeated locally; the CI Python jobs include that check.
