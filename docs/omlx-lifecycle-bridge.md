# LC-04: Unactivated oMLX lifecycle bridge

Status: **fake-tested implementation under review, not installed qualification**.
Module: [`omlx_lifecycle.py`](../omlx_lifecycle.py).
Related: [plan](cross-runtime-model-lifecycle-plan.md),
[LC-02](model-lifecycle-coordinator.md), [LC-03](shared-memory-admission.md),
[capability limits](model-lifecycle-capability-feasibility.md),
[proposed ADR-0034](adr/0034-unactivated-omlx-lifecycle-bridge.md).

## Scope and activation boundary

This is an explicitly constructed, unregistered adapter composing the shared
coordinator and memory ledger. It has no built-in HTTP client, credentials,
base URL, version probe, background loop, app import/startup, settings mutation,
download or automatic configuration. Existing public routes, inference internals,
security boundaries and native quality/speed settings are unchanged.

**The served sleep/suppressed-error helpers and legacy `OmlxLifecycleAdapter`
remain unchanged.** Their replacement belongs to LC-08 all-path integration,
not partial activation in LC-04. This module cannot be described as eliminating
served bypasses, completing session handoff or qualifying installed oMLX. Ollama
and LM Studio remain in the program; no model is forced onto oMLX or converted.
Hardware test8 remains paused, and no live engine/hardware test was performed.

No production transport, authorizer, qualification record or native proof
producer is supplied. Construction alone cannot authorize I/O: the default live
authorization hook is absent/deny. A future consumer must pass installed runtime,
source/profile/ownership and memory qualification, current session/grant/policy,
request-fit and all-path/handoff gates, plus required independent reviews.

## Pinned wire semantics, not a version allowlist

The source contract is revision `2165e75726ba83d9651dbaced61c97728da77bba`:

- [Public management routes](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py#L3973):
  `GET /v1/models/status`, `POST /v1/models/{model_id}/load` and `/unload`.
- [Pool status](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/engine_pool.py#L4754):
  loaded/loading, pinned/helper flags and runtime accounting, not physical proof.
- [Tokenization](https://github.com/jundot/omlx/blob/2165e75726ba83d9651dbaced61c97728da77bba/omlx/server.py#L8447):
  acquires an engine lease and can load; not a safe cold preflight.

The source pin is a reviewed wire shape, **not** a release recommendation,
minimum version, installed runtime identity or blanket support claim. Unknown
revisions/versions/capabilities cannot fall back to a compatible-looking route.
Public load uses existing server settings, not silently changed options: an
installed producer must qualify their exact fingerprint and effective context.

`OmlxQualification` requires exact runtime/target/digest/profile/context scope,
a fresh owner clock and recorded installed-support capabilities: authenticated
control, pinned transport, exclusive access, complete inventory/helper visibility,
stream quiescence, operation terminality/profile binding and blocking load when
applicable. Offline/source-basis capabilities never satisfy installed scope.
These are **trusted attestations**, not self-authenticating native proof.
No such installed producer exists here; all positive test records are invented
assumptions used to exercise the policy. Attached/external clients, arbitrary
namespaces, malicious same-UID mutation and automatic engine activity remain
qualification/exclusion limits, not promises made by a lock or declaration.

## Authorization, transport and native sends

An exact-step, expiring `OmlxPermit` is only a trusted-server attestation. Its
`authorized` flag is insufficient: an explicit synchronous live `authorize`
hook must return exactly `True`. It is checked again after preflight and before
native I/O, including post-send completion. The future hook must be bounded,
server-owned, check real grants/revocation/current policy and cause no native
side effects. No client JSON flag or model text may construct this authority.

The injected transport accepts fixed **relative** paths and a bounded timeout,
not a supplied URL or auth header. It must use existing registry authentication,
origin/IP pinning, no redirects, bounded JSON and no mutation retries. No network
transport is registered by this PR. Model IDs are encoded as one route component;
`.`/`..` routes are refused. Admin fallbacks are not attempted. Upstream bodies,
credentials, model paths and raw exceptions are never recorded or surfaced;
bounded HTTP status diagnostics remain available without implying terminality.

Before a native POST, the bridge validates current LC-02 intent/epoch/generation/
phase/deadline, runtime registry binding, live authorization, exact-instance native
activity and wire blockers. Loading requires the exact LC-03 profile/reservation
and current physical-memory sample; pinned/loading/helpers/distributed/external
allocations cannot be hidden by an empty or boolean-coerced catalog.

The LC-03 dispatch and `omlx_send_load` marker commit in **one transaction**.
Unload sends also receive an operation/stage journal marker. A second claimant
cannot reissue that effect, even after a lost response. Markers are metadata-only
rows in existing private schema v2: no schema bump, second store or long SQL
transaction. A storage failure before commit causes zero native POSTs. Every
transport/proof callback runs outside the transaction; crash after dispatch
retains a possibly executed effect rather than authorizing replay.

## Drain, ACK, verify and failure

`drain` needs scoped native activity for the exact source instance. Unknown or
positive active streams do not advance it. Unload rechecks a new current proof,
no active request lease, and wire visibility. There is no forced abort, unpin,
feature disabling, fixed sleep or inference warmup.

- **Unload 202** with the exact model/status means pending only.
- **Unload 200/ok** also means pending physical verification, never release.
- **400/404**, loading/busy **409**, auth or server errors are not universal success.
  Failed/ambiguous sends preserve holds and move the current operation to recovery;
  no target load is issued. ACKs never reset the operation deadline.
- Qualified blocking **load 200/ok** advances only to `verify`, never admission.
  The response may arrive after the preflight observation freshness window, but
  must still satisfy the current owner/operation deadline, permit expiry, live
  authorization and pinned protocol. A fresh qualified next-phase observation is
  required before readiness; an old preflight is never promoted into release proof.
- Cancellation/timeouts/disconnects retain memory and physical holds. Cancellation
  propagates after uncertainty is recorded, not as physical cancellation.
  If failure recording itself cannot write, the already committed dispatch still
  prevents replay; operator recovery must not delete the journal.

`confirm` reads a fresh status snapshot and gives its unique receipt to an
explicit trusted evidence producer. That producer must be read-only/non-loading,
collect no implicit remote metadata, preserve the current clock/scope and provide
qualified instance/digest/profile/**effective** context, quiescence, terminality,
physical headroom and complete inventory for **every bound runtime**. There is
no default producer. Its opaque receipt, runtime version and chronology must
match; LC-02 independently validates scope, ownership, terminality, freshness and
residency. A producer failure or contradictory wire state never releases a hold.

Pool status alone lacks instance/digest, active request counts and allocated
context. `model_context_length` remains architectural metadata; `actual_size`
remains reported accounting, not a memory bound or physical release. Missing
flags/counts, duplicate IDs and aliases fail closed rather than being collapsed.
Strict conservative blockers may deny a profile until its visibility is qualified;
they do not justify silently disabling required helpers. Shared memory is settled
separately only after both lifecycle and physical release requirements hold.

## Tokenizer, persistence and remaining gates

`tokenize` explicitly refuses with `tokenizer_admission_unavailable`, with zero
transport calls. LC-07 must supply qualified unchanged full-request preparation,
media handling and admitted counting; no cold tokenizer bypass is supplied.
There is no inference/stream translation or native cache/scheduler replacement.

Existing private v1 journals remain preserved/refused; the reviewed populated
migration/backup/rollback is still missing. No active Studio schema migration or
public configuration is added. The new module is packaged, type-checked and
full-suite scoped but deliberately not imported/inlined by the app/standalone;
real consumer integration must register and test isolated standalone consumption.
ADR-0031/0032/0033/0034 remain proposed/not-reviewed. No ledger/security signature,
UI baseline, architectural acceptance or test8 status is advanced. Installed
runtime, auth/exclusive-access, helper visibility, stream and physical-proof
qualification, LC-07/08/09 integration, full CI and independent review remain gates.

## Local verification record

Linux/Python 3.11: **628 passed across 25 focused suites in 40.97s**, including
69 new oMLX tests, all 43 LC-02 and all 40 LC-03 tests. One upstream
Starlette/httpx deprecation warning remains. Tests include child process exit at
durable native dispatch, real async timeout/cancellation, late effects, competing
sends, atomic storage rollback, exact context/receipt/instance checks and live
authorization revocation during preflight. No model endpoint was contacted.

Seven deliberate source mutations were caught by targeted tests: ACK unlocking
the next load, 400-as-success, repeated unload sends, skipping stream drain,
architectural-as-effective context, authorizing by a flag alone, and bypassing
memory dispatch. These disposable mutants/logs are not shipped.

Ruff/configured mypy (65 modules), docs (248 files), ADR/governance/security-record
consistency, scope mapping (178 test modules / 13 areas), source-manifest and
onboarding checks passed. Standalone, route matrix, subsystem inventory and
historical roadmap dashboard regenerated without drift. No UI capture changed.
Full local suite has not been rerun for LC-04. New-head full Python 3.11/3.12/3.13
and browser PR CI, independent review and installed qualification remain required,
not inferred from prior PRs or these fake-only results.
