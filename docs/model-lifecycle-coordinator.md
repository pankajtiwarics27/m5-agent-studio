# LC-02: Dormant durable lifecycle coordinator

Status: **implemented kernel under review, not active runtime control**.
Module: [`model_lifecycle_coordinator.py`](../model_lifecycle_coordinator.py).
Related: [program plan](cross-runtime-model-lifecycle-plan.md),
[capability blockers](model-lifecycle-capability-feasibility.md),
[LC-01 observations](local-runtime-lifecycle.md), and
[proposed storage ADR](adr/0032-dormant-lifecycle-journal-namespace.md).

## Boundary

This module provides a serialized, durable policy kernel, not a native engine
adapter or an inference gateway. There is no HTTP client, process supervisor,
model loader, background pump, startup hook, public API/config setting, or cloud
fallback. `app.py` does not import or construct it. All verification uses fake
fleets, temporary state and controlled test processes; test8 remains paused.

`TransitionStep` is a **persisted intent, not authorization to execute it**.
`RequestLease` reserves one logical request slot, not permission to infer.
Before a future consumer dispatches either, it must independently pass native
capability/ownership qualification, session/handoff/grant checks, combined memory
admission (LC-03), exact request-fit admission (LC-07), and all-path integration
(LC-08/09). No installed runtime satisfies those gates merely by passing these tests.

## Owner namespace and process fencing

Explicit construction opens a private owner namespace containing fixed files
`owner.lock` and `journal.sqlite3`. There is no per-session/workspace database
argument. `owner_namespace_path(home, platform_name)` is a pure helper selecting
a standard owner state location on Linux or macOS; it reads neither the home
directory nor environment automatically. Future app integration must use one
canonical owner/machine namespace for **every** workspace and worker.

POSIX `flock(LOCK_EX|LOCK_NB)` is held until close/process death. Unsupported
platforms/lock primitives fail closed, not to a process-local boolean. Directory
permissions must be owner-only; lock/database files must be owner-only regular
files with one link, owned by the current UID. Symlink leaf namespaces and
symlink/hard-linked files are refused. Inode replacement, permission relaxation,
closed ownership, or inherited use from a forked child fences subsequent calls.
A child close cannot unlock its parent's open-file description. Never unlink a
lock file to solve contention: that can create two lock inodes/owners.

The scope is **cooperating processes sharing this namespace**. A caller choosing
a different root, different UID/host, older uncoordinated Studio, external native
client, malicious same-UID filesystem mutation, or filesystems without reliable
POSIX locking is outside the guarantee. Private path validation does not provide
a sandbox against the owner. Native access isolation is still unqualified.
This is not an enforced machine-wide singleton over arbitrary applications.
Linux process-lock tests do not certify installed macOS engines or Windows support.

Every open increments a durable owner epoch and creates a new observer clock ID.
Every transition has a generation and random operation ID. Every dispatched
step and lease carries the owning epoch/generation. Closing or restarting never
makes a stale callback current, and logical fencing cannot cancel physical work.

## Persistence and compatibility

The kernel reuses `runtime_persistence.run_write_transaction` for synchronous,
short `BEGIN IMMEDIATE` transactions, bounded contention handling and connection
cleanup. No transaction spans an external effect. A claimed intent is returned
only after its dispatch record commits. Callers must not retry the physical
operation merely because they did not receive/complete that intent.

The dormant namespace now uses application-tagged **schema v3**;
[LC-03](shared-memory-admission.md) adds combined model/action accounting. Tables are
owner metadata, domain bindings/residency, requests, request leases, operations,
a sequenced metadata-only journal, memory policy and memory reservations. Journal entries retain owner epoch and
domain generation. Model references/digests, profile IDs, opaque evidence refs,
stages and counters are stored; prompts, tool arguments/results, credentials,
URLs with authentication, and model output are not collected by the module.
Callers must supply opaque reference IDs, never hide raw content in a label.

This is not the active `studio_history.db`; Studio's runtime schema remains v3.
No migration runs on startup or import. First creation applies the private v3
DDL/version tag atomically. An interrupted initialization leaves an unrecognized
journal and fails on reopen rather than silently treating it as empty. Existing
future/foreign/unversioned/corrupt/incomplete or semantically malformed journals
are rejected without repair/downgrade. A future upgrade from a populated private
schema requires a separately reviewed backup/migration; v1/v2 are preserved and
refused, not silently upgraded or recreated. A populated v1/v2-to-v3 migration is
not implemented and blocks activation for those existing journals. Never clear this database to unblock a model.

This storage separation is a **proposed** implementation choice, not acceptance
of ADR-0031/0032. It avoids coupling machine-wide ownership to a session-selected
application database. The journal records coordinator effects, not a second task
progress/event bus; later session integration must reuse `task_events.py`.

## Queue and resident/request separation

`register_domain` binds an explicit, nonempty tuple of runtime identities to one
memory domain. Bindings are immutable once registered; changing/omitting one is
not a way to forget an external resident. A newly registered domain starts in
`reconciling`, even when its journal has never seen a model.

`enqueue` records an opaque request ID, exact target (runtime ID, artifact ID,
artifact digest and approved-profile fingerprint), and absolute deadline in
caller-supplied integer milliseconds. The same ID/payload returns existing state;
changed payload is an idempotency conflict. Target bindings must explicitly be
local, local-only, and coordinated/isolated declarations; these declarations
still do not establish qualified native control or grants. Unknown does not
silently become local. Distinct runtime aliases are not automatically deduplicated.

Pending requests are bounded per domain (default 32, configurable internally up
to 1,024). The first policy is FIFO and one active request; new same-model work
cannot jump ahead of an older conflicting target. Contiguous same-target waiters
reuse a single load/warm resident, without sharing prompts/results. There is no
cache-affinity starvation optimization or automatic model-quality substitution.

Request completion releases **only its request lease**. Resident model/digest,
profile and exact native instance ID remain durable and warm. Queued expiry or
cancellation frees only that waiter's queue position. Cancelling admitted work
marks cancellation intent and retains its slot until terminal confirmation.
`mark_request_uncertain` also holds the entire domain after an inference
disconnect, without freeing its lease or replaying admitted work.
Cancelling the last waiter of a dispatched load does not stop that load, clear
its reservation or start another model. Already dispatched operations can finish
safely and retain a warm model even if every original waiter has gone away.

Durable deadlines use a caller-supplied clock watermark; regression fails closed
rather than extending expiry with an untrusted earlier time. The module reads
no wall clock. Queued expiry never proves an upstream operation terminal, and
an admitted lease is not automatically freed on its request deadline.
Queue bounds cover pending work, not unbounded historical journal retention;
a retention/compaction policy must preserve idempotency and uncertain holds.

## Transition protocol

A cold target yields `load -> verify`. Switching an existing resident yields
`drain -> unload -> load -> verify`. With an active request lease, no switch
step is claimed. `drain` confirmation is a future trusted session-boundary
attestation; no session snapshot/permission logic is implemented by this kernel.

Each stage is claimed once, after a durable dispatch record. Repeated polling
while it is dispatched returns no new step. `note_pending` records an ACK/202
without changing the phase, deadline, reservation, or source residency. It is
not interchangeable with `complete_step`.

Unload completion needs a new, complete, reachable, fresh view of **all bound
runtimes**, zero known active work, no loading/unloading/unknown entries, no
resident, and separate terminality/headroom attestations. Only then can a target
load intent be claimed. A fixed sleep, HTTP success, empty catalog, or one empty
snapshot while a load may finish later cannot advance the kernel.

Terminal load confirmation advances only to `verify`, never to inference.
Verification requires exactly one matching native instance, artifact digest,
load-profile fingerprint and effective context, plus explicit instance-ownership
evidence and domain quiescence/headroom. A matching model name is not ownership.
The accepted exact native instance ID is persisted and included in request
leases and later source-unload intents. Required hidden/helper visibility is a
native qualification gate, not inferred from the number of API entries.

A verified profile is not exact per-request fit; every future request still
needs LC-07 admission. This kernel does not perform tokenization, infer sizing
from disk bytes, or implement LC-03's combined model/tool memory budget.

## Timeout, cancellation and restart recovery

A dispatched step deadline or `mark_uncertain` moves the domain to `reconciling`
and retains the operation/source/target information. A late completion cannot
commit ready state. There is no automatic load retry or rollback load. Failure
of a target does not eagerly reload a source while target residency is unknown.

On reopen, every domain needs reconciliation; active operations become uncertain
and admitted requests/leases become interrupted. Queued requests remain durable,
but cannot advance until reconciliation. Old admitted requests are not replayed.
Only a later session-aware consumer can resume tasks under their original grants,
budgets, evidence and side-effect ledger. This module does not reset those systems.

`ReconciliationEvidence` is a trusted-consumer data record, **not a cryptographic
proof or an operator checkbox escape hatch**. Evidence must be newly scoped to
the current owner, domain generation, operation, phase, clock and current supplied
millisecond time. Samples also obey an internal maximum age (default 5 seconds),
even if a producer asks for a looser window. Old-boot/phase evidence cannot be
reused. A qualified producer must establish all old physical operations terminal
and actual headroom independently; freshness and epochs cannot establish either.
There is currently **no native producer, public setter or force-clear route**.

Recovery can retain only a previously journaled resident or in-progress target,
with explicit current native-instance ownership evidence, or accept a verified
empty domain. Unowned outside residents are conflicts, not adoption/eviction
candidates. An uncertain load with no authoritative native cancellation/status
mechanism remains blocked until supported operator/supervision recovery is proven.
The fake fleet's positive attestations are not evidence those mechanisms exist.

## Test evidence and remaining release gates

`tests/test_model_lifecycle_coordinator.py` covers ordering/warm reuse, FIFO and
same-target load sharing, queue bounds/idempotency/expiry, active-work cancellation,
202 vs release, timeout/late physical load, scoped-evidence replay, exact instance
binding/ownership, external conflicts, multi-thread claims, multi-process owner
exclusion, process crashes, fork fencing, restart at each dispatch boundary,
corrupt/future/partial initialization, clock regression, disk failure redaction,
private files and lock replacement. All engine actions are fake and assert no
persistence transaction is active when applied.

Four source mutations were tested in disposable temporary copies: freeing the
resident on request completion, treating ACK as release, clearing the domain on
timeout, and reissuing a dispatched step. Each was caught by an assertion in the
selected regression (not a collection or syntax failure). Temporary mutant code
and logs are not shipped.

The module is setuptools-registered, mypy-gated and mapped in test scopes. Its
source path is high-risk/full-suite-selected; no scoped selector may silently
omit full CI. It is deliberately not imported/inlined by the app/standalone yet.
Actual consumption must add isolated standalone integration tests and preserve
all current authentication, origin/rate/pinning, grants, budgets and task events.

Native adapters/cancellation certainty, secure qualification producers, approved
memory/context profiles, safe session-boundary implementation, all-entrypoint
wiring, UI/recovery controls, installed-runtime isolation and one-at-a-time
hardware acceptance are still unimplemented release gates. These tests do not
claim OOM prevention, engine fault immunity, production exclusivity or task-quality
parity. ADRs remain proposed and independent security acceptance remains unsigned.

### LC-02 local verification record (before LC-03)

On Linux/Python 3.11: **519 passed across 23 focused suites in 36.46s**, including
43 coordinator tests, with one upstream Starlette/httpx deprecation warning.
The four deliberate unsafe mutations were caught by the targeted assertions.
Repository Ruff, mypy (63 modules), documentation (243 files), ADR, governance,
existing security-signoff consistency, scope-map, README/onboarding verification
and source-manifest checks passed. Standalone, route matrix and subsystem
inventory were regenerated without drift. No approved UI capture was replaced.

An initial documentation check caught a proposed root README link changing its
onboarding verification. That unrelated README edit was removed; the development
document is indexed here without rewriting accepted UI/onboarding evidence.
Full new-head Python 3.11/3.12/3.13 and browser PR CI is still required, not claimed
from this focused run. No live model, owner hardware or independent security
review was performed, and no runtime capability was certified.


## LC08 explicit-load intent foundation (not a native command consumer)

`enqueue_load` persists a distinct `load` purpose in the same bounded FIFO as
inference. Existing `enqueue` remains inference-only; idempotency includes purpose,
so the same request ID cannot be relabelled. Native transition stages stay the
same. Only after matching verified residency can `claim_next` return a `LoadLease`
for the actual native instance. It is deliberately **not** a `RequestLease` or
its subclass, and does not authorize inference, token counting or native effects.

Both kinds share the one-active-slot constraint, blocking overlapping control /
inference admission across independent consumers. The historical internal
`snapshot.active_requests` field counts these exclusive admitted slots, not a
native stream counter. Native activity still requires separate qualified proof.

`finish_load` requires new complete, reachable, fresh, quiescent all-runtime
terminal/headroom and owned-instance evidence matching the existing exact target
and native instance. The `load_control` scope binds epoch, generation and this
unique **load lease ID** in `EvidenceScope.operation_id`, preventing evidence
reuse between same-model controls. An ACK, earlier load/verify proof or a warm
name match alone cannot finish the slot. No implicit reload or count occurs.
This kernel does not establish installed context/profile qualification or LC03
settlement: a future concrete command consumer must check those independently.

Cancelled or deadline-expired admitted controls retain their slot until physical
terminality is proven. A successful `finish_load` means the slot was released;
its durable request status is `cancelled`/`expired`, not `completed`, in those cases.
It is **not** a canonical API `confirmed` response. Model residency and model
memory remain held. `mark_load_uncertain` fences interrupted work for explicit
reconciliation. Restart interrupts admitted controls without replaying them and
preserves queued purpose; old epochs/leases cannot complete new-owner work.

SQL purpose fences in fit admission and both inference bridges reject a load
slot even if a trusted caller rewraps its fields as a `RequestLease`. Inference
cold-step consumption rejects load-purpose rows; a miswired warm scheduler holds
its unexpected load slot uncertain instead of treating it as inference. The
private standalone bundle exercises the same distinct-slot/proof completion.

Schema v3 adds `requests.purpose` with a closed `inference|load` constraint. The
active Studio history schema remains separately v3; application IDs/namespaces
are distinct. No namespace deletion, implicit migration, hold reset, native
activation, command implementation or architecture acceptance is authorized.
Unload-only/unload-all/cache semantics remain open. A dormant concrete Ollama /
LM Studio load consumer is now described in the [integration record](runtime-dispatch-integration.md),
without default producers or activation. No oMLX target/inference qualification is added.


The internal `_finish_load` seam lets the concrete native consumer check the actual
admitted model memory/sample/budget and finish the load slot in **one write**.
The standalone `finish_load` kernel API still establishes only its documented
proof-based slot completion, not installed qualification or LC03 authority.
No schema change beyond the preceding v3 purpose discriminator is introduced.

## LC08 unload-only release foundation (dormant; no native consumer)

The next kernel checkpoint adds `enqueue_unload` and a distinct `ReleaseStep`,
not an acquire-to-self or an empty/fake replacement model. Its immutable identity
binds the exact actual source/instance, request, domain, owner epoch, generation,
operation and existing dispatched model-memory reservation. It has **no target**.
`requests.target` on an unload row is only the expected-source selector.

A release claim requires that matching owned resident, its current dispatched
model hold, a ready memory domain and no active inference/load slot. Unmatched,
absent, uncertain, merely reserved or unbound sources do not create a release
operation or report successful absence. All three source runtimes are exercised
with fake kernel facts, not qualified installed native behavior.

The only stages are drain and unload. `complete_release` is separate from
`complete_step`; unload completion requires fresh, unload-operation-scoped,
complete/reachable/quiescent all-runtime absence, terminality and headroom proof.
Physical completion clears resident identity and marks the operation complete,
but **keeps** `domains.operation_id`, the switching barrier and the queued intent.
`claim_next` cannot redispatch that completed operation or start a replacement.
The actual model-memory bytes remain charged.

The existing independent LC03 `settle` must release that exact source reservation
with fresh terminal/attributed-zero evidence. `finish_unload` then checks its
**durable released status**, current operation/epoch/generation/source/request,
a ready memory domain and no other live model reservation in the same write
that retires the barrier. There is no caller-supplied "memory settled" boolean.
Its true result means barrier retirement, not an API `confirmed` response:
cancelled/expired intents retain those statuses. It never loads another model.
`mark_release_uncertain` also fences physically complete but unsettled releases;
old epochs/retired operations cannot poison successor work. Restart requires
explicit reconciliation and preserves conservative memory accounting; no replay,
polling or LC09 recovery completion is implemented.

Private schema **v4** adds the unload purpose and constrained acquire/release
operation goal, nullable target **only for release**, and durable request/hold
references. Acquire records still require real targets and no release references.
Populated v1/v2/v3 namespaces are refused without modifying their database bytes;
there is no implicit migration, deletion or reset. The unrelated active Studio
history schema remains v3. A reviewed backup/migration path is still required
before any activation.

Existing Ollama/LM Studio/oMLX permits explicitly remain incompatible with this
new intent. Miswired existing load/inference actors refuse it and fence it
uncertain without a native call. The private standalone bundle exercises physical
completion, retained barrier, real memory settlement and final retirement through
its own namespaced coordinator/memory kernels. Native unload command consumers,
unload-all/cache, already-absent success policy, routes/settings, default producers,
installed qualification and production activation remain **unimplemented**.

### Native release gate continuation (no schema change or command activation)

Ollama and LM Studio now explicitly support `ReleaseStep` as source-only native
permit intent; oMLX unload-only still refuses. `_active_release` is separate from
`_active_step`: it checks exact current dispatched release stage and request,
source/instance/hold, epoch/generation, ready memory state and empty admitted slots.
Before effects, request cancellation/expiry denies. Only an acknowledged already-
sent stage may use the native finishing path after cancellation/expiry, and the
final request status still cannot become completed. Native receipt prefixes are
disjoint from acquire-stage receipts. Inference, load-control and acquire checks
are not relaxed or repurposed.

The concrete step consumer captures the real source hold/instance/generation,
validates actual native loaded-profile records before completing drain, confirms
full-runtime physical absence, independently settles the exact model reservation,
then invokes `finish_unload`. Its `ReleaseResult.retired` denotes only barrier
retirement, including cancelled/expired cleanup; it is not canonical `confirmed`.
Native step support does not install an unload command actor or HTTP route.
Private schema remains v4; prior-version preservation/refusal and the absent
reviewed backup/migration gate remain unchanged. See the
[integration continuation](runtime-dispatch-integration.md#concrete-native-release-step-continuation-ollamalm-studio-still-no-command-actor).

### Optional unload actor continuation

An explicitly composed `NativeUnloadPlan` can now consume the existing source-only
release primitive on the shared native actor for Ollama/LM Studio. It does not
alter kernel schemas or weaken native/LC03 proof. At most two owned FIFO stages
are claimed; only durable exact request/operation/source-model retirement and its
`release_settled` receipt can produce canonical `confirmed`. Physical proof alone,
a returned retired flag or an empty runtime cannot. Cancelled/expired cleanup
retains its noncompleted status and cannot poison subsequent source acquisition.
Historical completion is checked independently of a successor's current slot.
No default plan or public route is enabled; already-absent/unmatched sources,
oMLX unload-only and unload-all/cache remain unsupported. See the
[integration actor continuation](runtime-dispatch-integration.md#optional-native-unload-command-actor-continuation-still-dormant).

### oMLX source-only actor continuation

The same `ReleaseStep`/durable-retirement protocol can now be explicitly consumed
by an oMLX source-only native job, with distinct LC04 qualification plus actual
source-memory profile and original native-load record. No acquire target or
request-fit metadata is invented. Release send/ACK markers are separate from
acquire; source absence and independent held-memory zero settlement remain required.
Optional oMLX actor composition is not default installation or HTTP redirection.
Schema/migration policy remains unchanged. See the
[source-only integration](runtime-dispatch-integration.md#omlx-source-only-unload-command-continuation-dormant).
