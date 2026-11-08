# Local runtime lifecycle adapter

AS-019 defines `OmlxLifecycleAdapter` in
[`local_runtime_lifecycle.py`](../local_runtime_lifecycle.py). It isolates
load, unload, failure, and telemetry semantics behind an injected transport;
the adapter does not import an SDK, select a model, or assume a chip or memory
tier.

## Contract

- `load(model_id)` calls the runtime administration endpoint with a bounded
  load timeout and returns a normalized result.
- `unload(model_id)` calls the runtime unload endpoint and returns a normalized
  result.
- `telemetry()` validates and sorts model state, active requests, memory
  counters, and pressure into immutable records.
- Transport failures, non-success statuses, and malformed payloads become
  `LifecycleError` messages containing only a safe operation/status category;
  cancellation propagates so callers can stop work promptly.

## Evidence

`tests/test_local_runtime_lifecycle.py` uses an in-memory async transport to
verify endpoint paths, encoded model IDs, load/unload behavior, telemetry
normalization, unavailable-runtime failures, malformed responses, and
cancellation propagation.

## LC-01 observation contracts (dormant)

The same module now defines immutable, runtime-neutral records for the proposed
[cross-runtime lifecycle program](cross-runtime-model-lifecycle-plan.md).
These are **facts and evidence records, not an active coordinator**. The existing
oMLX transport adapter and all served load/inference paths are unchanged.
ADR-0031 remains [proposed, not reviewed](adr/0031-shared-domain-model-lifecycle-authority.md).

- `RuntimeIdentity` separates a configured runtime ID, extensible backend and
  wire-protocol names, declared locality/egress, shared-memory-domain binding,
  and declared ownership mode. Defaults are unknown. In particular, the legacy
  `cloud` wire label does not prove egress, and a local/loopback connection does
  not prove there is none. No endpoint, hostname, architecture or RAM tier is
  inferred or discovered. Ownership declarations are not grants/isolation proof.
- `ObservationStamp` uses a caller-supplied time and observer-clock identity.
  Queries supply `now`, clock identity and a positive age limit; they do not read
  the clock. Missing fields, a different boot/clock, or a sample from the future
  are unknown; expired observations are stale. The producer must use a unique
  observer clock/boot scope and invalidate it after restart or clock reset.
- `CapabilityEvidence` records supported/unsupported/unknown separately from
  unverified/source/offline/installed evidence. A claim needs versioned evidence
  references; installed claims also need a runtime ID. The installed-scope query
  returns unknown unless runtime ID, version and approved profile fingerprint
  match exactly and the basis is installed. Source review/fixture success never
  promotes itself to installed support, and neither does a newer version number.
  This class records claims; it does not authenticate evidence or authorize work.
- `ModelInstanceObservation` distinguishes artifact ID/digest, native instance
  ID, load-profile fingerprint, architectural maximum and effective allocated
  context. Disk size cannot populate resident memory; explicit memory provenance
  is required for a supplied resident byte count. Memory can remain unknown.
- `RuntimeInventory` records reachability, completeness, clock scope, instances
  and capability evidence. Duplicate native instance IDs or conflicting duplicate
  capability scopes are refused. Different instances of one artifact remain
  separate; aliases are not automatically deduplicated. Evidence explicitly bound
  to a different runtime is rejected. Lists/mutable collections are not accepted
  in immutable tuple fields, and numeric booleans/strings are never coerced.

`declared_domain_relation` compares configured domain bindings only. Different
bindings do not establish physical independence; the future authority must
validate resource-domain configuration. Missing bindings produce unknown, never
independent admission pools inferred from different provider names.

`residency_at` returns `occupied`, `empty_observed` or `unknown`. Loading/unloading,
positive active work, or positive resident-memory evidence is conservatively
occupied. Incomplete inventories without positive occupancy, unknown entries,
unreachable runtimes, stale samples and cross-clock samples are not empty.
**Even a fresh, complete `empty_observed` snapshot cannot release a reservation.**
It says nothing about a queued/timed-out load finishing later, physical allocator
quiescence, total headroom or access by other clients. LC-02/03 and native adapter
qualification must establish those independently. No `ready`, grant, load, retry,
release or activation method is added by these records.

### Compatibility, packaging and limits

This extends an existing setuptools-registered, mypy-gated module; no dependency,
HTTP/configuration surface, schema migration or historical roadmap status changes.
The existing source/test scope registrations already cover this module and
`test_model_*.py`; new tests are `tests/test_model_runtime_observations.py`.
Constructors/query methods perform no network, disk, environment, subprocess,
model load or automatic clock call. They accept explicitly supplied evidence;
there is no wire-response parser or observational endpoint invocation in LC-01.

No served path consumes these records yet. Like the pre-existing adapter, this
contract module is not currently imported/inlined into `app.py`/`studio.py`.
Standalone generation therefore remains unchanged; future activation must add
standalone inclusion alongside its actual app integration rather than silently
claiming that import-only code makes switching safe. Native wire normalization,
capability qualification, durable coordination and context admission remain later
stories. Existing legacy telemetry, including its `actual_size` fallback, is not
promoted into these provenance-sensitive records or a physical-release proof.

### LC-01 evidence

Tests cover unknown defaults across oMLX/Ollama/LM Studio and an arbitrary future
backend, domain aliases, immutable/malformed data, clock restart/staleness,
version/profile/runtime-scoped evidence, architectural vs allocated context,
disk vs resident memory, multiple native instances, loading/unloading, partial/
unreachable inventories and invalid duplicate identities. The legacy transport
suite still checks exactly its previous paths/results/cancellation behavior.
No native runtime was contacted or qualified. Manual test8 remains paused.

Local verification for the LC-01 change: **453 passed across 21 focused suites
in 26.78s**, including 72 new observation-contract cases and the unchanged legacy
adapter cases; one upstream Starlette/httpx deprecation warning. Repository Ruff,
mypy (62 modules), docs (241 files), ADR, governance, existing signoff consistency,
test-scope map and source-manifest checks passed. The standalone and both source
inventories were regenerated without a diff. Existing UIV2-29 artifact validation
passed; it was a check of the existing evidence, not a new browser/hardware run.
Full multi-Python/browser CI for this new implementation head is a separate PR
gate; the earlier full-suite/CI results on LC-00 are not claimed for LC-01.
