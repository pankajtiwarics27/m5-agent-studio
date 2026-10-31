# Portable Handoff and Conservative Model Switching

`routing_policy.py` (inlined into standalone `studio.py`) and `app.execute_conservative_model_switch_async`
implement `M5-P4-02` ([ADR-0018](adr/0018-portable-handoff-and-conservative-switching.md)):
sequential model transitions that never assume KV-cache portability, preserve task budgets and
scoped grant validity, fence stale workers before switching, validate workspace file versions
and routing permissions, apply selective semantic confirmation on high-risk transitions, and
leave failed switches in a safe `paused` (or `blocked`) state.

## Architecture and separation of concerns

Every model transition persists a `HandoffRecord` in `$HARNESS_STORE_DIR/handoffs/state.json`
(`PortableHandoffStore`, `0700` directory / `0600` file, `O_EXCL | O_NOFOLLOW` temp file,
temp + parent directory `os.fsync`, `os.replace`, covered by `tests/test_durable_state_contract.py`)
split into two independently hashed envelopes:

1. **`PortableHandoffCore` (`core_sha256`):**
   - Model-agnostic plain-text state safe to render into any local or cloud model prompt:
     `objective`, `next_step`, `completed_milestone_ids`, `pending_milestone_ids`, sanitized
     `portable_messages` (`role`, plain-text `content`), `truncated_messages`, SHA-256-verified
     `file_references` (`HandoffFileReference`), `checkpoint_id`, `checkpoint_payload_hash`,
     `evidence_refs`, and explicit KV-cache posture (`kv_cache_portable = False`,
     `kv_cache_handling = "discarded_recomputed_from_text"`, `stripped_non_portable_fields`).
2. **`HarnessPrivateExtensions` (`extensions_sha256`):**
   - Authoritative harness metadata never delegated to model self-report: `fenced_epoch`,
     `fenced_worker_ids`, `policy`, `session_policy_revision`, `global_policy_revision`,
     `validated_grants` (`HandoffGrantSnapshot`), `revoked_epoch_grant_ids`, `budget_snapshot`,
     `switch_attempts_used`, `max_switch_attempts`, `routing_decision`, `admission_transition`,
     `risk_level`, `risk_reasons`, and `semantic_confirmation`.

## Zero KV-cache portability (`AC 1`)

- `assert_no_kv_cache_portability_request` rejects any switch payload carrying KV-cache,
  prompt-cache, SSD-cache, token-ID, or provider-response-state transfer keys
  (`assume_kv_cache_portable`, `reuse_kv_cache`, `kv_cache_id`, `ssd_cache_ref`,
  `prompt_cache_key`, `previous_response_id`, `thinking_signature`, `token_ids`) with
  `409 kv_cache_portability_forbidden`.
- `sanitize_portable_messages` scrubs persisted session `.messages.json` transcripts on switch,
  and `sanitize_messages_for_wire` scrubs every outbound payload in `app.stream_any_model`.
- On turn execution (`POST /api/harness/run`), `render_portable_handoff_banner` injects a
  plain-text handoff header into the incoming model's system prompt so context is recomputed
  from verified text alone.

## Sequential switch pipeline (`Tasks 1-4`, `AC 2`, `AC 3`)

All session model changes — `POST /api/harness/sessions/{session_id}/switch-model`, model edits
in `PATCH /api/harness/sessions/{session_id}`, and inline `lead_model` / `critic_model`
overrides in `POST /api/harness/run` — execute in a strict fail-closed order:

1. **Fence old worker first (`Task 4`):** Sets `_HARNESS_ABORT_FLAGS`, calls
   `WorkerLeaseCoordinator.fence_and_halt(..., target_state="paused")`, terminates matching
   subprocesses via `ProcessSupervisor.contain_by_scope_async`, and revokes only grants pinned
   to the fenced epoch (`ScopedGrantStore.revoke_by_lifecycle_event`).
2. **Reserve switch retry budget (`Task 4`):** `PortableHandoffStore.reserve_switch_attempt`
   increments `switch_attempts_used` against `max_switch_attempts` (default `3`, env
   `STUDIO_MAX_MODEL_SWITCH_ATTEMPTS`). Once exhausted, further switches fail closed with
   `409 handoff_retry_budget_exceeded` until an operator calls
   `POST /api/harness/sessions/{session_id}/handoffs/reset-budget`.
3. **Safety holds and budget non-laundering (`Task 2`, `AC 2`):** Refuses switching when the
   workspace is frozen under `workspace_quarantine` (`handoff_workspace_quarantined`), when
   unresolved consequential actions, uncertain MCP effects, or containment holds exist
   (`handoff_unresolved_side_effects`), or when `TaskBudgetState` is already exceeded
   (`handoff_budget_already_exceeded`).
4. **Reference, version, and checkpoint verification (`Task 2`):** Verifies the latest
   checkpoint SHA-256 (`DurableTaskCheckpointStore.load_latest_checkpoint(verify_integrity=True)`)
   and validates every file in `EditTransactionLedger`, `ObservedVersionIndex`, and caller
   `referenced_files` via `collect_and_validate_file_references` (`O_NOFOLLOW`, workspace
   confinement, SHA-256 match). `_verify_active_session_handoff` re-runs this check before
   `/resume` and `/api/harness/run` activate a switched session.
5. **Permission, routing consent, and heavy-model sequential admission (`Task 2`, `M5-P4-01`):**
   Refuses silent policy escalation (`handoff_permission_escalation_forbidden`), enforces
   `evaluate_fallback` (`FallbackPolicy` + `FallbackConsent`) whenever a switch crosses onto a
   networked/billable cloud route (`handoff_routing_consent_denied`), unloads idle resident oMLX
   models before switching local targets, and refuses switches when `HeavyModelAdmission`
   reports an active busy lease (`handoff_heavy_model_busy`). `combo` failover in
   `stream_any_model` enforces the same fallback consent and idle local unload rules.
6. **Selective semantic confirmation (`Task 3`):** `classify_switch_risk` skips extra model
   calls for low-risk switches (`skipped_low_risk`) and requires verified JSON confirmation
   (`core_sha256`, `policy`, `next_step`, exact `referenced_paths`, `ack_no_kv_cache=true`,
   `restated_objective`) for high-risk switches (`handoff_semantic_confirmation_failed` on
   mismatch or hallucinated paths).
7. **Authority-preserving commit or safe pause (`AC 2`, `AC 3`):** Commits only after verifying
   `TaskBudgetState` and active session/task `ScopedGrant` records (`expires_at`,
   `uses_consumed`, `max_uses`) are unchanged. Any failure persists
   `HandoffRecord(status="failed_paused")`, preserves the previous model binding, and leaves
   the session in `paused` (or `blocked` when unresolved holds exist).

## HTTP endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/harness/sessions/{session_id}/handoffs` | Inspect committed/failed `HandoffRecord` history, active handoff, and `SessionSwitchBudget` |
| `POST /api/harness/sessions/{session_id}/switch-model` | Execute a conservative sequential model switch with optional `referenced_files`, `expected_file_versions`, `required_grant_ids`, `consent`, `fallback_policy`, `target_context_tokens`, and `semantic_confirmation` |
| `POST /api/harness/sessions/{session_id}/handoffs/reset-budget` | Operator endpoint to reset an exhausted per-session switch retry budget |

## Current limits

- Heavy-model slot residency (`model_admission.py`) and worker leases remain single-process
  controls; running multiple Studio processes against one oMLX server is outside the supported
  deployment contract.
- The 24 GB unified-memory pressure measurement (`M5-P4-01`) was taken by an operator run
  of `./scripts/qualify_environment.sh --hardware` on an Apple M5 on 2026-10-31
  ([record](m5-2-0-hardware-qualification-evidence.md)); it covers one model on one
  machine, and switching still inherits the single-process limits above.

## Verification

```bash
.venv/bin/python -m pytest -q tests/test_routing_policy.py tests/test_routing_consent_policy.py tests/test_portable_handoff_and_switching.py tests/test_durable_state_contract.py
```
