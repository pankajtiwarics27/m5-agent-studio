---
id: ADR-0011
title: heavy-local-model-admission
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0011: One heavy local model at a time, enforced in the streaming funnel

## Context

`M5-P4-01` requires that a second simultaneous heavy load is rejected under
policy, and that routing does not treat public priors as verified. The repository
already had two of the pieces and one gap:

1. `resource_admission` provides an atomic, thread-safe reservation primitive with
   leases, but nothing in the serving path used it — its guarantee was a module
   test, not a product behaviour;
2. `model_recommendation` already reports `unknown` confidence when no measured
   profile exists, so public priors are not presented as verified;
3. the local streaming path (`stream_any_model` → `_stream_omlx_completion`) would
   start any number of concurrent local requests, each of which can load a large
   model into unified memory. On a 24 GB machine two of those do not fit, and the
   failure mode is the operating system swapping or killing the process rather
   than an explainable refusal.

The story also cannot be *finished* here: its third criterion is a measured 24 GB
pressure result, which needs the owner's hardware. The question this ADR answers
is what to implement now, so that the remaining gap is a measurement rather than a
missing mechanism.

## Decision

Enforce heavy-model admission in the serving path, with the policy expressed once
and reused everywhere.

- **`model_admission.py` owns the policy.** It wraps `resource_admission` with
  local-inference semantics: capacity = unified memory (detected, or configured),
  one request = 80 % of capacity, one slot, one distinct resident model. Denials
  are typed (`HeavyModelDenied`) with stable codes (`heavy_model_slot_busy`,
  `heavy_model_memory_exceeded`, `heavy_model_resident`) and the snapshot that
  produced them.
- **The funnel is the enforcement point.** `app.stream_any_model` is the single
  path through which every local stream passes (direct chat, council, debate,
  harness critic turns, workflows), so the guard wraps that leaf. Cloud providers
  are untouched: they consume no local unified memory.
- **The lease lifetime is the stream lifetime**, released in `finally` so a failed
  or abandoned stream frees the slot. Nested work on the same model re-enters the
  lease through a context variable instead of deadlocking.
- **User-facing chat endpoints pre-flight.** `POST /api/chat/stream` and
  `POST /v1/chat/completions` return `429` with the policy snapshot before opening
  an SSE stream, because a refusal inside a stream is a worse answer than a
  refusal before it.
- **The policy is observable.** `GET /api/models/admission` exposes capacity,
  usage, residency and grant/denial counters, and the qualification suite drives
  the funnel to prove the wiring (`p4.admission_wired`, now required).
- **Measured evidence is still missing, and the story stays `blocked`.** The 80 %
  reservation is a policy constant, not a measured footprint. Nothing in this ADR
  asserts a measurement.

## Alternatives

- **Queue the second request instead of refusing it.** Rejected for this story:
  queueing hides the capacity limit and needs fairness, timeout and cancellation
  semantics that no acceptance criterion asks for. A refusal with a stable code
  and a retry hint is smaller and honest.
- **Serialize inside `_stream_omlx_completion`.** Rejected: the demo branch of that
  function runs when oMLX is offline and loads no model, so guarding there would
  serialize preview traffic for no memory reason. The funnel is the last point
  where "this is a local model" is already known.
- **Add a second boolean or semaphore in `app.py`.** Rejected: the repository
  already has an atomic primitive with lease identity; a hand-rolled flag would be
  a second, weaker source of truth and would not survive review of the
  concurrency story.
- **Enforce at `POST /api/models/{id}/load`.** Deferred to `M5-P4-02` (sequential
  switching): refusing a load there would silently change an explicit operator
  action, which belongs to the switching story rather than the inference policy.
- **Use a fixed byte figure (for example 18 GB).** Rejected: it would be a public
  prior dressed up as a measurement, which is exactly what the story forbids. The
  capacity is read from the machine, the fraction is documented as policy, and the
  measured profile remains the missing piece.

## Tradeoffs

- The policy lives in memory in one process, so a second application process would
  enforce its own limit. That matches the documented single-process worker-lease
  assumption and is listed with the other open risks rather than papered over.
- A refusal inside a non-pre-flighted path (council, debate, workflows) surfaces as
  a stream error rather than an HTTP status. The code and snapshot are preserved in
  the error, but client ergonomics differ from the two pre-flighted endpoints.
- Demo mode (oMLX offline) still passes through the funnel, so two simultaneous
  preview streams are serialized like real ones. That is conservative — the app
  cannot prove a local runtime is idle — but it is a behaviour change for preview
  traffic.
- The 80 % reservation is deliberately conservative: a model that would fit in
  isolation is refused while another heavy request runs. Erring toward refusal is
  the intended direction on a 24 GB machine.
- `p4.admission_wired` mutates module state (database path, completion backend,
  admission policy) inside a temporary directory while probing. It restores
  everything in a `finally` block, which is appropriate for a one-shot operator
  script and would not be for the server.

## Review Status

Not reviewed. This record accompanies the serving-path enforcement added for
`M5-P4-01`. The story's measured 24 GB pressure criterion was met by an operator
run on an Apple M5 on 2026-10-31
([`../m5-2-0-hardware-qualification-evidence.md`](../m5-2-0-hardware-qualification-evidence.md)),
which is a measurement of the machine, not a review of this decision. This ADR
has not been reviewed by anyone other than the authoring agent, and
`review_status` remains `not-reviewed`.
