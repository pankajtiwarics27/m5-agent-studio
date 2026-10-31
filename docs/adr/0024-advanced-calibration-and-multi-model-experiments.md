---
id: ADR-0024
title: advanced-calibration-and-multi-model-experiments
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0024: Calibrated recovery-cost routing, affinity/hysteresis, sequential 24 GB admission, and privacy-gated cloud opt-in

## Context

Owner-authorized optional Phase `P6` story `M5-P6-03` (*Advanced calibration and multi-model
experiments*) evaluates optional routing improvements on M5 Agentic Studio's 24 GB unified-memory
Apple Silicon target (`M5-P4-01`, `model_admission.py`), building on conservative portable handoffs
(`M5-P4-02`, `routing_policy.py`), bounded child delegation (`M5-P4-03`, `child_delegation.py`),
and the security release gate (`M5-P5-03`, `security_release_gate.py`).

Naive multi-model routing creates four concrete failure modes on 24 GB local hardware:

1. **Uncalibrated drift and memory over-commit:** Runtime or quantization updates can increase a
   local model's peak footprint above the single-heavy-request ceiling (`19.2 GiB` on `24 GiB`),
   causing OOM aborts or unified-memory thrashing if routing relies on stale nominal model labels.
2. **Switch thrashing under minor score jitter:** Because `ADR-0018` forbids KV-cache or opaque
   state transfer across models, every local model switch unloads the resident model, loads cold
   weights, and re-prefills plain-text context. Eager switching on marginal score differences
   burns tokens, adds multi-second swap latency, and exhausts per-session switch retry budgets.
3. **Unmeasured simultaneous residency:** Attempting to keep two local models co-resident without
   verified single-slot headroom violates `HeavyModelAdmission` (`ADR-0011`).
4. **Silent cloud leakage:** Routing stagnant local sessions to cloud models without checking
   default-off operator opt-in, per-request network/billing consent, `ArtifactRegistry` `"secret"`
   confidentiality lineage, credential exposure, and workspace quarantine holds risks exfiltrating
   sensitive local code or secrets.

## Decision

We introduce stdlib-only `routing_experiments.py` (inlined into `studio.py`, persisted in
`$HARNESS_STORE_DIR/routing_experiments/state.json` under the shared 8-clause crash-durable JSON
sidecar contract) and wire it into `app.py` (`/api/harness/routing-experiments*`,
`/api/harness/sessions/{session_id}/task`, and `_execute_session_model_switch`):

1. **Pinned cold/warm calibration, drift detection, and recovery-cost benchmarking (`Task 1`):**
   - `CalibrationProfileRecord.from_samples` aggregates timed `cold` and `warm` probes (`ttft_ms`,
     `prefill_tokens_per_s`, `decode_tokens_per_s`, `peak_memory_bytes`, `swap_unload_ms`,
     `swap_load_ms`) while counting failed probes toward `failure_rate` without polluting timing
     medians.
   - `evaluate_calibration_drift` flags digest/quantization changes, TTFT/decode degradation, and
     upward peak-memory growth (`recalibration_required=True`), blocking local routing when
     observed memory exceeds `HeavyModelLimits`.
   - `RecoveryCostMeasurement` records stagnation verdicts (`recovery_guard`), wasted tokens,
     recovery latency, sequential swap overhead, and plain-text re-prefill tokens.
2. **Sticky model affinity, utility hysteresis, and sequential handoffs (`Task 2`):**
   - `AffinityHysteresisPolicy` enforces `min_dwell_steps = 2` and `hysteresis_margin = 0.12`
     after subtracting amortized cold swap latency and plain-text re-prefill token cost, holding
     the resident model during minor score jitter while triggering `sequential_handoff` on
     verified stagnation (`stagnant` / `oscillating`) or calibration drift.
   - `execute_sequential_local_handoff` releases the outgoing `HeavyModelLease` first, asserts an
     idle heavy slot (`active_leases == 0`), strips all non-portable KV-cache/opaque keys via
     `routing_policy.sanitize_portable_messages`, and acquires the incoming lease sequentially.
3. **Forbidden unmeasured simultaneous residency & 24 GB admission (`Task 4`, `AC 2`):**
   - `validate_residency_and_24gb_admission`, `/api/harness/routing-experiments/{config,evaluate}`,
     and `/api/harness/sessions/{id}/switch-model` reject `simultaneous_residency=True` or
     multi-model co-residency fail-closed (`SimultaneousResidencyForbiddenError`:
     `unmeasured_simultaneous_residency_forbidden` / `simultaneous_residency_exceeds_24gb`).
4. **Default-off cloud opt-in and privacy-constrained eligibility (`Task 3`, `AC 3`):**
   - `evaluate_cloud_eligibility` defaults `cloud_opt_in_enabled=False` and denies cloud candidates
     unless operator `FallbackPolicy`, per-request `FallbackConsent`, permitted provider,
     non-restricted `privacy_class`, absence of `"secret"` `ArtifactRegistry` task lineage,
     zero unredacted credentials (`redact_text`), and absence of quarantine/containment holds all
     pass simultaneously. `_execute_session_model_switch` also blocks local-to-cloud session
     switches (`handoff_cloud_privacy_denied`) whenever `"secret"` task artifacts exist.
5. **Measured verified completion gate & clean disablement (`AC 1`):**
   - Cold stores default `ab_gate_open=False` until `run_routing_calibration_ab_suite` (`24` paired
     runs across `4` scenarios) passes (`calibrated_verified_completion_rate = 1.0` vs
     `baseline_verified_completion_rate = 0.25`, median tokens `950.0` vs `1600.0`,
     `peak_concurrent_heavy_models = 1`, `admission_24gb_violations = 0`,
     `unmeasured_simultaneous_accepted = 0`, `cloud_unauthorized_activations = 0`).
   - Disables cleanly via `STUDIO_ENABLE_ROUTING_EXPERIMENTS=0` or store config.

## Alternatives

1. **Enable simultaneous residency of a small router/critic local model alongside a heavy coder
   model:** Rejected because unmeasured or multi-slot co-residency on 24 GB unified memory risks
   unified-memory paging and violates `ADR-0011`'s single-heavy-slot invariant.
2. **Switch eagerly to whichever candidate has the highest nominal benchmark score:** Rejected
   because ignoring swap unload/load latency and non-portable KV-cache re-prefill costs causes
   ping-pong thrashing and exhausts per-session switch budgets (`Scenario 2`).
3. **Allow per-request `FallbackConsent` alone to enable cloud routing without checking session
   artifact confidentiality:** Rejected because `"secret"` workspace or tool-output artifacts must
   never leave the local machine even when general cloud fallback consent was granted (`AC 3`).

## Tradeoffs

- Optional calibrated routing stays dormant on cold stores (`ab_gate_closed_keep_resident`) and
  can be disabled via environment or API without affecting baseline single-model execution.
- Upward memory drift on local models fails closed until recalibrated within the 24 GB budget.
- Every cloud eligibility check and simultaneous-residency refusal is durably audited in
  `$HARNESS_STORE_DIR/routing_experiments/state.json`.

## Review Status

Accepted on 2026-10-31 for optional Phase `P6` story `M5-P6-03`. Independent human review is
not reviewed (`review_status: not-reviewed`, `reviewed_by: none`, `reviewed_on: none`).
