---
id: ADR-0018
title: portable-handoff-and-conservative-switching
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0018: Portable model handoff, zero KV-cache portability, and conservative sequential switching

## Context

`M5-P4-02` requires M5 Agentic Studio to support safe sequential model switches without losing
task authority, inflating execution budgets, renewing or bypassing scoped grants, or assuming
KV-cache portability across models or providers.

Before this decision:

1. `PATCH /api/harness/sessions/{session_id}` and inline model overrides in
   `POST /api/harness/run` mutated `harness_sessions.lead_model` and `critic_model` directly
   inside SQLite without fencing active worker leases, checking unresolved consequential side
   effects or uncertain MCP writes, validating referenced workspace file digests, or enforcing
   cloud fallback consent.
2. Provider- and runtime-specific attributes (`kv_cache_id`, `ssd_cache_ref`, `cache_control`,
   `prompt_cache_key`, `previous_response_id`, `thinking_signature`, `token_ids`) could remain
   attached to persisted session transcripts or outbound wire messages across model changes.
3. Heavy local models on 24 GB unified memory (`M5-P4-01`, `model_admission.py`) allow at most
   one active resident heavy model slot, so switching between local models requires idle-slot
   verification and sequential unloading rather than concurrent co-residency.
4. High-risk switches (mid-task switches after consequential actions, modified workspace files,
   context-window truncation, recovery usage, or local-to-cloud boundaries) risked silent intent
   drift or hallucinated file state if the incoming model resumed execution immediately with
   tools enabled.

## Decision

We extend `routing_policy.py` (stdlib-only, inlined into `studio.py`) and wire every session
model transition (`POST /api/harness/sessions/{session_id}/switch-model`,
`PATCH /api/harness/sessions/{session_id}`, inline `POST /api/harness/run` model changes, and
`combo` failover in `stream_any_model`) through a conservative sequential switch coordinator:

1. **Zero KV-cache portability (`AC 1`):**
   - `assert_no_kv_cache_portability_request` rejects any switch option requesting KV/prompt/SSD
     cache transfer or opaque provider state reuse (`409 kv_cache_portability_forbidden`).
   - `sanitize_portable_messages` and `sanitize_messages_for_wire` strip all non-portable keys
     recursively from transcripts and wire payloads so every incoming model recomputes state
     purely from plain-text messages and the portable handoff banner (`kv_cache_portable=False`,
     `kv_cache_handling="discarded_recomputed_from_text"`).
2. **Crash-durable Portable Core + Harness-Private Extensions (`Task 1`):**
   - `PortableHandoffStore` persists `HandoffRecord` entries and per-session `SessionSwitchBudget`
     counters atomically in `$HARNESS_STORE_DIR/handoffs/state.json` (`0700` directory, `0600`
     file, `O_EXCL | O_NOFOLLOW` temp write, temp + directory `os.fsync`, `os.replace`, symlink
     and corruption/future-schema fail-closed checks).
   - `PortableHandoffCore` (`core_sha256`) stores only model-agnostic task intent, milestone
     state, sanitized messages, SHA-256-verified `HandoffFileReference` entries, and checkpoint
     digests. `HarnessPrivateExtensions` (`extensions_sha256`) stores runtime authority metadata
     (fenced epoch/workers, policy revisions, validated grant snapshots, task budget snapshot,
     routing consent decision, local admission transition, risk classification, and semantic
     confirmation outcome).
3. **Old-worker fencing and bounded switch retries (`Task 4`):**
   - Every switch attempt first sets the session abort flag, calls
     `WorkerLeaseCoordinator.fence_and_halt` + `ProcessSupervisor.contain_by_scope_async` on
     any active worker epoch, and revokes epoch-pinned grants via
     `ScopedGrantStore.revoke_by_lifecycle_event` before evaluating or committing the switch.
   - `PortableHandoffStore.reserve_switch_attempt` enforces a durable per-session retry cap
     (default `3`, configurable via `STUDIO_MAX_MODEL_SWITCH_ATTEMPTS`, never relaxable once
     exhausted without explicit `POST /api/harness/sessions/{session_id}/handoffs/reset-budget`).
4. **Reference, version, permission, budget, and grant preservation (`Task 2`, `AC 2`):**
   - `collect_and_validate_file_references` confines all referenced paths inside the session
     workspace (`O_NOFOLLOW`), rejects symlinks and path traversal, and verifies current file
     SHA-256 digests against `EditTransactionLedger`, `ObservedVersionIndex`, and caller
     expectations (`handoff_source_version_mismatch`). `_verify_active_session_handoff` re-checks
     these digests before `/resume` and `/api/harness/run` activate a switched session.
   - Switching refuses workspace quarantine holds, unresolved consequential side effects,
     uncertain MCP writes, active containment holds, exhausted task budgets, silent policy
     escalations, unconsented local-to-cloud routing (`evaluate_fallback`), and busy heavy-model
     slots (`HeavyModelAdmission.would_deny`), while proactively unloading idle oMLX models.
   - `TaskBudgetState` counters and active session/task-scoped grants (`expires_at`,
     `uses_consumed`, `max_uses`, `session_policy_revision`) are preserved unchanged across the
     switch and verified before commit.
5. **Selective semantic confirmation (`Task 3`, `AC 3`):**
   - `classify_switch_risk` skips confirmation on low-risk switches (untouched zero-step local
     sessions with no modified files or context truncation) and requires structured JSON
     confirmation (`core_sha256`, `policy`, `next_step`, exact `referenced_paths`,
     `ack_no_kv_cache=True`, and non-empty `restated_objective`) via explicit attestation or a
     zero-tool target-model probe on high-risk switches.
   - Any failure at any stage persists `HandoffRecord(status="failed_paused")`, preserves the
     prior model binding, and leaves both the durable task and session in `paused` (or `blocked`
     when unresolved external/side-effect holds exist).

## Alternatives

1. **Allow direct SQLite updates to `lead_model` / `critic_model` and rely on prompt history:**
   Rejected because in-flight workers can still commit stale actions, non-portable cache keys
   leak across providers, drifted files go undetected, and local-to-cloud switches bypass
   consent policy.
2. **Require target-model semantic confirmation on every switch, including brand-new idle
   sessions:** Rejected because an extra model round-trip before step 1 adds latency and failure
   surface where zero consequential state, file references, or context truncations exist.
3. **Store handoff state only inside SQLite:** Rejected because handoff failure paths must
   record retry consumption and safe-pause diagnostics even when a switch aborts outside a normal
   turn transaction, matching the five-store crash-durable JSON sidecar contract.

## Tradeoffs

- High-risk switches incur one bounded, zero-tool JSON confirmation call against the target
  model (unless an operator supplies a verified confirmation attestation), trading small switch
  latency for deterministic detection of hallucinated paths, wrong policy assumptions, or
  broken model endpoints before any tool is exposed.
- Strict retry bounds (`max_switch_attempts = 3` by default) halt repeated failing switches into
  `paused` until an operator inspects `/handoffs` and explicitly calls `/handoffs/reset-budget`.
- Heavy-model sequential switching remains single-process in `model_admission.py`, matching the
  documented single-process Studio deployment boundary (`ADR-0011`).

## Review Status

Accepted on 2026-10-31 for story `M5-P4-02`. This ADR is not reviewed by an independent
reviewer (`review_status: not-reviewed`, `reviewed_by: none`, `reviewed_on: none`).
