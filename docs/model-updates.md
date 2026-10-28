# Model update and rollback lifecycle

AS-057 defines `ModelRevisionLedger` in
[`model_updates.py`](../model_updates.py): the lifecycle layer that turns
AS-053 discovery output into safe upgrade handling — a model upgrade can be
**pinned**, **rolled back**, and **invalidated** without destroying state or
silently serving a bad revision.

## Identity and observation

Revisions are tracked per `(runtime, model_id)` so two runtimes serving the
same model id never conflate or cross-overwrite. `observe()` merges one
discovery result (a `ModelCatalog` or a plain descriptor sequence): the first
usable digest of a model is adopted as active; further digests become pending
"update available" records. Adoption of a revision whose provenance
verification is `rejected` is refused — as an update and as a model's first
revision — and recorded as an `adoption_refused` event instead.

A consulted, available runtime that stops reporting the active digest marks
it **missing** (transient outages never destroy active state; reappearance
records `restored`). A runtime absent from the observation marks nothing —
absence of information is not evidence of removal.

## Pin, adopt, roll back

`pin()` freezes the active revision: automatic adoption (when the ledger is
constructed with `auto_adopt_updates=True`) and explicit `adopt_update()` are
both refused while the pin holds. `adopt_update()` promotes an observed
pending revision — defaulting to the most recently observed, accepting an
`expected_previous_digest` guard so a caller can prove it acted on current
knowledge. `rollback()` restores the newest history entry, skipping
quarantined digests; the displaced revision returns to pending so
re-adoption stays an explicit, auditable act. Any pin that no longer matches
the active revision is released automatically during rollback or
invalidation — a pin never claims a stale guarantee.

`invalidate()` quarantines one digest: it is removed from pending, refused
by adoption, and skipped by every fallback path. If the quarantined digest
was active, the ledger falls back to the newest valid history entry, or
refuses to serve any active revision when none exists — stopping is safer
than continuing with suspect state. Lifting a quarantine requires an
explicit `clear_invalidation()` with a reason; the digest becomes adoptable
again only after the runtime re-reports it.

## Audit

Every transition appends a `RevisionEvent` (sequence number, kind, digests,
reason) to a bounded log trimmed oldest-first with an overflow count;
per-model history is bounded too. No wall-clock timestamps are stored, so
event ordering is the caller's ordering of `observe`/transition calls and
replays are deterministic. `status_for(descriptor)` gives dependent
artifacts — AS-056 scores, reassembled transcripts, caches — the staleness
verdict for the exact identity they pinned: `active`, `missing`,
`superseded`, `observed`, `invalidated`, or `unknown`.

The ledger is pure state: it runs no installer, loads no model, and contacts
no runtime; applying a revision to reality stays with AS-053 install
observations and the runtime itself.

## Evidence

`tests/test_model_updates.py` (14 tests) covers first-adoption and
refusals, update queuing with explicit and automatic adoption, pin
enforcement and automatic release, optimistic-guard rejection, rollback
with quarantined-history skipping, quarantine blocking adoption until
cleared and re-observed, active-revision invalidation with fallback and
with refuse-to-serve, non-destructive missing/restore cycles,
consulted-runtime scoping, per-runtime independence for shared model ids,
bounded history and event overflow with stable sequence numbers, every
input-validation path, and an end-to-end catalog-driven walk
(discover → pin → refuse drift → adopt → roll back → quarantine) asserting
the full event sequence.
