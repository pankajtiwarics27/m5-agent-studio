# LC-03: Dormant shared-memory accounting

Status: **implementation for review, not active admission or qualified safety**.
Module: [`shared_memory_admission.py`](../shared_memory_admission.py).
Related: [plan](cross-runtime-model-lifecycle-plan.md),
[LC-02 journal](model-lifecycle-coordinator.md),
[proposed ADR-0033](adr/0033-dormant-shared-memory-accounting.md).

## Scope and authority

No app import/startup, HTTP/config setting, sampler, engine/process control,
download, network discovery, context/RAM increase or cloud substitution is added.
Existing execution policy, grants, barriers, subprocess containment and native
optimizations are unchanged. Hardware test8 stays paused; offline fakes do not
qualify the owner's installed runtimes. This module consumes trusted attestations;
it cannot authenticate them or prove physical upper bounds from typed fields.

`SharedMemoryAdmission` shares LC-02's fenced process lock, epoch and short SQLite
transaction boundary; it does not create an independent process-local pool.
One physical memory domain may be configured per owner journal in this first
version. Arbitrary roots/UIDs/external native clients remain outside coordination.
Distinct/discrete-memory domains need a reviewed topology/qualification extension,
not separate pools claiming the same physical headroom.

Reservations and `dispatch` decisions are **not execution permission**. Future
consumers must pass existing session/policy/grant/barrier/slot checks, installed
runtime and bound qualification, full-request fit and all-path integration before
performing an effect. The module has no effect runner. Served paths do not yet
consult this ledger: LC-08 integration is deferred, not claimed here.

## Profiles and action accounting

`ModelMemoryProfile` pins the exact LC-02 target/digest/load-profile fingerprint
and approved context. Explicit nonnegative bounds cover weights, KV, workspace,
runtime, auxiliaries and load transients; their positive sum is reserved. A zero
must be justified by the future qualified producer, not assumed by an omitted
field. Disk size, architectural context and one runtime's configured budget are
not peak physical demand. Load-transient capacity is conservatively retained
while warm; phase-aware reclaim is future qualification work.

Model reservations require a current persisted LC-02 **load** intent; dispatch
rechecks that exact intent, stage and deadline. A request completion neither
releases the warm resident nor its memory. Only one model reservation may remain
active in the domain. Qualified physical release must settle both lifecycle and
memory state before another target can reserve its load.

Actions accept the existing `execution_profiles.ResourceFootprint` and its
`ResourceRequest`, but require a separate explicit `ActionMemoryBound` tied to the
entire footprint digest. A declared 2-GiB subprocess footprint is **not proof**
of an enforced host-process upper bound. No bound means denial. The reserved peak
must be at least the declared footprint, and may be greater. Subprocess/mutation
slot counters are not extra physical pools and are not added to action memory.
No paths or tool arguments are stored in the action binding, only the footprint
hash, counts and opaque qualification reference. References must not contain
secrets or content. The existing controller still owns its own slots/barriers;
its release does not settle this durable memory hold.

`expand_action` requires a newly qualified bound and reserves growth before any
allocation. It cannot shrink a hold, silently resize context, expand permissions
or repair already exceeded bounds. Legacy grants/barriers/containment must also
permit the expansion. Unknown/unbounded host tools cannot be made safe merely by
attaching a reference string; native/OS bound enforcement remains unqualified.

## Physical envelope and avoiding double counting

Each sample must describe the **same physical domain**, clock, owner epoch,
ledger revision and lifecycle generation. It supplies total bytes, bytes usable
without swap or reclaiming the tracked working set, normal pressure, and complete
non-overlapping physical attribution for every held reservation (including
unmaterialized reservations with zero bytes). No collector exists yet. Ordinary
process RSS sums, reclaimable model mappings, independent engine pools, or an
unreviewed `available` counter must not be relabeled as this evidence.

For each held allocation, let `P` be its qualified peak and `A` its currently
attributed physical allocation. New demand `N` is allowed only when both hold:

- `sum(P) + N <= configured shared limit`
- `sum(P - A) + N + explicit OS/application safety reserve <= usable available bytes`

The measured `A` is already excluded from available memory, so it is not charged
a second time. Unmaterialized/future growth `P - A` is still reserved. Attribution
must cover exactly the held IDs, without duplicates; `A <= P`, and total attributed
bytes cannot exceed total physical use. An allocation appearing before persisted
dispatch is uncertainty, not a credit. These arithmetic checks cannot prove that
a producer really measured disjoint physical pages; provenance is a release gate.

Unknown/stale capacity, wrong clocks/scopes, unsafe pressure and exceeded bounds
fail closed. A current unsafe capacity/pressure/attribution observation persists
a paused state **before** its denial is surfaced, retaining all holds. Old
favorable samples cannot clear that pause. A budget or request-fit denial never
increases budgets, reduces evidence/context, evicts a resident or substitutes a
cloud provider. The freshness cap comes from LC-02 (default five seconds); that
is a configurable logical ceiling, not a qualified physical sampling interval.

## Dispatch, completion and recovery

Every mutation increments a durable revision. Concurrent contenders using the
same sample cannot both spend it. Exact work-ID/binding deduplication does not
replay a dispatch; released IDs cannot be reused. Expansions invalidate old tokens.
All fake external effects occur after a committed dispatch, outside transactions.

Cancellation, timeout, inference disconnect and logical fencing never prove
physical termination. `mark_uncertain` holds the domain. `settle` requires a
current complete sample, attributed zero and explicit terminal attestation.
Model release additionally requires LC-02 to have retired the original operation
and to record no resident. Releasing another action is not automatic proof that
all uncertainty is resolved; a paused domain needs explicit reconciliation.

On restart, all held bounds survive as uncertain, revisions advance, clocks and
epochs change, and memory domains pause for reconciliation. Every held ID requires
an outcome: retain only with qualified live ownership, or release only after
terminal zero-allocation proof. Model retention also requires LC-02's reconciled
matching resident. Adopted tokens carry the new epoch and are already dispatched,
never automatically reissued. Base lifecycle reconciliation remains independently
required even if action memory was reconciled first.

## Storage compatibility and release gates

New private journals are **schema v2** with `memory_domains` and
`memory_reservations` added to LC-02. Initial DDL/version tags are atomic. The
active Studio database remains schema v3. Old private v1, unknown/future/corrupt
or incomplete journals are preserved and refused, without resetting holds or
modifying their bytes. There is no automatic populated migration or backup
implementation: reviewed backup/migration and rollback evidence are mandatory
before activation for an existing v1 namespace. Never delete state to unblock it.
The original schema-v1 proposal is historical; ADR-0031/0032/0033 remain proposed
and not-reviewed. No roadmap/independent security/acceptance status is advanced.

Further gates: native pressure/capacity/attribution collectors, current installed
version/profile qualification, actual tool/process memory enforcement, external
client isolation assumptions, real all-path pairing with grants/barriers and
standalone consumers, bounded retention/migration, full CI and independent review.
The modules are packaged, type-checked and marked full-suite risk, but not inlined
into `studio.py` until real consumer integration is designed and tested. No
machine-wide OOM prevention, crash freedom, native qualification, production
readiness, session handoff completion or test8 pass is claimed.

## Local verification record

Linux/Python 3.11: **559 passed across 24 focused suites in 39.71s**, including
40 new accounting tests and all 43 LC-02 tests. There was one upstream
Starlette/httpx deprecation warning. Six deliberate mutations were caught:
ignoring pending growth, treating pools independently, double-charging an
attributed resident, cancellation-style release, discarding a pressure pause,
and inferring an action upper bound from its default footprint.

Ruff and configured mypy (64 modules), docs (245 files), ADR/governance/security
record consistency, scope mapping (176 test modules / 13 areas), onboarding and
source-manifest checks passed. Standalone, route matrix, subsystem inventory and
historical roadmap dashboard regenerated without drift. No UI baseline changed.
Full local suite has not been rerun for LC-03; new-head Python 3.11/3.12/3.13 and
browser PR CI remain required. No live model or independent/hardware acceptance
is claimed. Existing security-record consistency is not independent approval.

A future producer must collect physical evidence after obtaining the exact
current scope; it must never retag old observations across revisions. A typed
sample, `complete` flag or qualification reference is not an authentication or
physical-proof mechanism. Server-only provenance and collector qualification
remain activation gates.
