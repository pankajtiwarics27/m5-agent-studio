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

The dormant namespace now uses application-tagged **schema v2**;
[LC-03](shared-memory-admission.md) adds combined model/action accounting. Tables are
owner metadata, domain bindings/residency, requests, request leases, operations,
a sequenced metadata-only journal, memory policy and memory reservations. Journal entries retain owner epoch and
domain generation. Model references/digests, profile IDs, opaque evidence refs,
stages and counters are stored; prompts, tool arguments/results, credentials,
URLs with authentication, and model output are not collected by the module.
Callers must supply opaque reference IDs, never hide raw content in a label.

This is not the active `studio_history.db`; Studio's runtime schema remains v3.
No migration runs on startup or import. First creation applies the private v2
DDL/version tag atomically. An interrupted initialization leaves an unrecognized
journal and fails on reopen rather than silently treating it as empty. Existing
future/foreign/unversioned/corrupt/incomplete or semantically malformed journals
are rejected without repair/downgrade. A future upgrade from a populated private
schema requires a separately reviewed backup/migration; v1 is preserved and refused, not silently upgraded or recreated. A reviewed
populated v1-to-v2 migration is not implemented and blocks activation for existing
v1 journals. Never clear this database to unblock a model.

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
