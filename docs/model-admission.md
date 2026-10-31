# Heavy local model admission

A 24 GB Apple Silicon machine can hold one large local model resident, not two.
`M5-P4-01` therefore requires that a second simultaneous heavy load is rejected by
policy. This page describes the policy that does it, where it is enforced, and
what it deliberately does not claim.

## The policy in one paragraph

`model_admission.HeavyModelAdmission` wraps `resource_admission`'s atomic
reservation primitive with a policy specific to local inference: capacity is the
machine's unified memory, one heavy request reserves 80 % of it, exactly one slot
exists, and only one *distinct* model may be resident. A second concurrent local
request cannot fit — the primitive refuses it — and the refusal carries a stable
code plus the snapshot that produced it. Cloud providers do not consume a slot:
they use no local unified memory.

## Enforcement points

| Where | Behaviour |
|---|---|
| `app.stream_any_model` (the single local-stream funnel, used by direct chat, the council, debates, harness critic turns and workflows) | Acquires the slot before the local stream starts and releases it when the stream finishes, fails or is abandoned by the consumer. A denial raises `HeavyModelDenied` inside the stream. |
| `POST /api/chat/stream`, `POST /v1/chat/completions` | Pre-flight `429` with the policy snapshot, so the common case refuses *before* an SSE stream opens. |
| `GET /api/models/admission` | Read-only view: capacity, per-request footprint, slots in use, resident model, counters. |

Nested work on the model already admitted by the caller reuses that lease
(`model_admission.hold_lease`), so a wrapper calling a wrapped stream cannot
deadlock against itself. Asking for a *different* model while one is held is
refused with `heavy_model_resident` instead of stacking two models.

## Refusal codes

| Code | Meaning |
|---|---|
| `heavy_model_slot_busy` | The single heavy slot is in use (the normal "another local request is running" answer). |
| `heavy_model_memory_exceeded` | The request does not fit in the configured capacity. |
| `heavy_model_resident` | A different model is resident; one heavy resident model at a time. |

The `429` body carries `detail.code`, `detail.message` and `detail.admission`, the
same snapshot `GET /api/models/admission` returns, so a client can show why it was
refused and when the slot frees up.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `STUDIO_HEAVY_MODEL_MEMORY_BYTES` | detected unified memory, else 24 GiB | Capacity in bytes. |
| `STUDIO_HEAVY_MODEL_REQUEST_BYTES` | 80 % of capacity | Per-request reservation. |
| `STUDIO_HEAVY_MODEL_SLOTS` | `1` | Concurrent heavy requests. Raising it does not allow two *distinct* resident models; the residency rule still applies. |

Malformed values fail closed: the policy refuses to be constructed rather than
silently admitting work under an unintended capacity.

## What this does not claim

- **No measurement of the 80 % constant.** The 80 % figure is a policy constant,
  not a measured model footprint. The 24 GB pressure criterion and one resident
  4-bit model's load/swap behaviour *were* measured on an Apple M5 on 2026-10-31
  ([`m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md)),
  and that record qualifies `M5-P4-01`; it does not turn 80 % into a per-model
  sizing rule, and it records the probe run without a model digest or a
  per-process peak. See [`qualification-suite.md`](qualification-suite.md).
- **No switching.** Loading a second model deliberately (sequential handoff) is
  `M5-P4-02`'s job. Here a second model is refused, not queued.
- **No fairness or queueing.** A refused request is refused; it is not parked and
  retried. Clients retry when the slot frees.
- **Single-process policy.** The counters and leases live in one application
  process, matching the worker-lease model documented in the roadmap's open risks.
- **No load-path enforcement.** `POST /api/models/{id}/load` calls oMLX directly;
  whether a second *resident* model may be loaded there is a switching decision
  (`M5-P4-02`), not something this policy silently changes.

## Tests

`tests/test_model_admission.py` (34 tests) pins the policy without a model: limit
validation and environment parsing, capacity and residency denials, release and
re-acquire, `would_deny` being non-reserving, one winner out of eight racing
threads, re-entrant leases, the funnel admitting one stream and refusing the
second, the lease being released when a consumer abandons the stream, and the
`429` pre-flight on both chat endpoints.

`scripts/qualify_environment.py` verifies the wiring from the outside:
`p4.admission_wired` drives `app.stream_any_model` with a fake backend and expects
admission, refusal while resident, and admission again after release.
