"""End-to-end verification for M5-P4-02: Portable handoff and conservative switching.

Covers all 4 implementation tasks and all 3 acceptance criteria:
- Task 1: Persist portable core and harness-private extensions (`$HARNESS_STORE_DIR/handoffs/state.json`)
- Task 2: Validate handoff references, source versions and permissions
- Task 3: Use selective semantic confirmation for high-risk transitions
- Task 4: Fence old worker and bound retries
- AC 1: No KV cache portability assumption
- AC 2: Switch preserves budgets and grants validity
- AC 3: Failed handoff leaves safe paused state
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import pytest

import app as studio
import execution_budget
import routing_policy as rp

AUTH_TOKEN = "t" * 40


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "studio_test.db"

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)

    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_mcp_effect_store(None)
    studio._reset_heavy_model_admission(
        studio.model_admission.HeavyModelAdmission(
            studio.model_admission.HeavyModelLimits(
                memory_bytes=24 * 1024**3,
                request_bytes=18 * 1024**3,
                slots=1,
            )
        )
    )
    studio.init_db()
    return ws


def _client() -> TestClient:
    return TestClient(
        studio.app,
        base_url="http://127.0.0.1:8787",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )


def _create_session(
    ws: Path,
    *,
    policy: str = "standard",
    lead_model: str = "local-alpha-27b",
    critic_model: str = "local-critic-21b",
    title: str = "Implement verified module feature",
) -> dict[str, Any]:
    return studio.create_harness_session_db(
        title=title,
        workspace_dir=str(ws),
        use_worktree=False,
        policy=policy,
        lead_model=lead_model,
        critic_model=critic_model,
    )


def test_portable_core_and_harness_extensions_persisted_and_no_kv_cache_assumption(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 1 & AC 1: Portable core and harness-private extensions persist durably; KV-cache
    portability requests are rejected fail-closed and non-portable message attributes are stripped."""
    sess = _create_session(isolated_env)
    sid = sess["id"]

    # Seed transcript containing non-portable KV-cache, prompt-cache, thinking_signature, and token_ids fields.
    studio._save_harness_messages(
        sid,
        [
            {"role": "system", "content": "old system prompt", "kv_cache_id": "kv-sys-99"},
            {
                "role": "user",
                "content": "Update src/module.py cleanly",
                "cache_control": {"type": "ephemeral"},
                "prompt_cache_key": "pc-key-1",
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret chain", "thinking_signature": "sig-opaque"},
                    {"type": "text", "text": "Inspecting src/module.py", "cache_control": {"type": "ephemeral"}},
                ],
                "previous_response_id": "resp_opaque_123",
                "ssd_cache_ref": "omlx://ssd/block-7",
                "token_ids": [101, 202, 303],
            },
        ],
    )

    tc = _client()

    # 1. Explicit KV-cache portability requests fail closed (`409 kv_cache_portability_forbidden`) on BOTH routes.
    bad_resp = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "assume_kv_cache_portable": True,
        },
    )
    assert bad_resp.status_code == 409
    assert bad_resp.json()["detail"]["code"] == "kv_cache_portability_forbidden"
    assert bad_resp.json()["detail"]["task_state"] == "paused"
    assert studio.get_harness_session_db(sid)["lead_model"] == "local-alpha-27b"
    assert studio.get_harness_session_db(sid)["status"] == "paused"

    bad_patch = tc.patch(
        f"/api/harness/sessions/{sid}",
        json={
            "lead_model": "local-beta-32b",
            "kv_cache_id": "kv-transfer-attempt",
        },
    )
    assert bad_patch.status_code == 409
    assert bad_patch.json()["detail"]["code"] == "kv_cache_portability_forbidden"
    assert studio.get_harness_session_db(sid)["lead_model"] == "local-alpha-27b"

    # Reset switch budget after the 2 rejected negative-test attempts and execute a clean switch.
    # Because prior failed switches > 0 existed before reset, reset clears consecutive_failures to 0.
    reset_r = tc.post(f"/api/harness/sessions/{sid}/handoffs/reset-budget", json={"max_switch_attempts": 3})
    assert reset_r.status_code == 200

    ok_resp = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "target_critic_model": "local-critic-v2"},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    payload = ok_resp.json()
    assert payload["status"] == "switched"
    assert payload["session"]["lead_model"] == "local-beta-32b"
    assert payload["session"]["critic_model"] == "local-critic-v2"

    hof = payload["handoff"]
    core = hof["portable_core"]
    ext = hof["harness_extensions"]
    assert core["kv_cache_portable"] is False
    assert core["kv_cache_handling"] == "discarded_recomputed_from_text"
    for expected_stripped in (
        "cache_control",
        "kv_cache_id",
        "previous_response_id",
        "prompt_cache_key",
        "ssd_cache_ref",
        "thinking_signature",
        "token_ids",
    ):
        assert expected_stripped in core["stripped_non_portable_fields"]
    assert len(core["core_sha256"]) == 64
    assert len(ext["extensions_sha256"]) == 64
    assert ext["policy"] == "standard"

    # Persisted `.messages.json` on disk is scrubbed of all non-portable keys.
    saved_msgs = studio._load_harness_messages(sid)
    serialized_msgs = json.dumps(saved_msgs)
    for forbidden in ("kv-sys-99", "pc-key-1", "resp_opaque_123", "omlx://ssd/block-7", "sig-opaque"):
        assert forbidden not in serialized_msgs

    # Reload a fresh PortableHandoffStore from disk to confirm crash-durable separation of core & extensions.
    reloaded = rp.PortableHandoffStore(studio.HARNESS_STORE_DIR).get_latest_handoff(sid, statuses=("committed",))
    assert reloaded is not None
    assert reloaded.portable_core.core_sha256 == core["core_sha256"]
    assert reloaded.harness_extensions.extensions_sha256 == ext["extensions_sha256"]

    # Also verify `stream_any_model` strips non-portable wire attributes before calling the backend.
    seen_wire_msgs: list[dict[str, str]] = []

    async def _fake_omlx(
        _client_obj: Any,
        _model: str,
        wire_messages: list[dict[str, str]],
        *_args: Any,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        seen_wire_msgs.extend(wire_messages)
        yield {"type": "content", "text": "ok"}

    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_omlx)

    async def _consume() -> None:
        async for _ in studio.stream_any_model(
            None,  # type: ignore[arg-type]
            "local-beta-32b",
            [{"role": "user", "content": "hi", "kv_cache_id": "leak", "previous_response_id": "r1"}],  # type: ignore[dict-item]
        ):
            pass

    asyncio.run(_consume())
    assert seen_wire_msgs == [{"role": "user", "content": "hi"}]


def test_handoff_validates_references_source_versions_and_permissions_fail_closed(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 2 & AC 3: Invalid references, symlink/traversal escapes, drifted source file versions,
    permission escalations, and unconsented cloud fallbacks fail closed into a safe paused state."""
    sess = _create_session(isolated_env, policy="plan")
    sid = sess["id"]
    mod_file = isolated_env / "src" / "module.py"
    sha_v1 = hashlib.sha256(mod_file.read_bytes()).hexdigest()

    # Record a committed managed edit at sha_v1 so handoff automatically tracks `src/module.py`.
    studio._edit_ledger().record(
        session_id=sid,
        rel_path="src/module.py",
        tool="write_file",
        pre_sha256=None,
        post_sha256=sha_v1,
        expected_sha256=None,
        observed_sha256=None,
        mode=0o644,
        bytes_written=len(mod_file.read_bytes()),
        task_id=sid,
    )

    tc = _client()

    # 1. Path traversal reference fails closed (`handoff_invalid_reference`) -> safe paused state.
    r_trav = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "referenced_files": ["../escape.py"], "max_switch_attempts": 10},
    )
    assert r_trav.status_code == 409
    assert r_trav.json()["detail"]["code"] == "handoff_invalid_reference"
    assert studio.get_harness_session_db(sid)["status"] == "paused"

    # 2. Symlink escape inside workspace fails closed (`handoff_invalid_reference`).
    outside = isolated_env.parent / "outside_secret.txt"
    outside.write_text("top-secret", encoding="utf-8")
    (isolated_env / "src" / "link.py").symlink_to(outside)
    r_sym = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "referenced_files": ["src/link.py"], "max_switch_attempts": 10},
    )
    assert r_sym.status_code == 409
    assert r_sym.json()["detail"]["code"] == "handoff_invalid_reference"
    (isolated_env / "src" / "link.py").unlink()

    # 3. Untracked source-version drift on `src/module.py` fails closed (`handoff_source_version_mismatch`).
    mod_file.write_text("VALUE = 999  # external drift\n", encoding="utf-8")
    r_drift = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "max_switch_attempts": 10},
    )
    assert r_drift.status_code == 409
    assert r_drift.json()["detail"]["code"] == "handoff_source_version_mismatch"
    assert studio.get_harness_session_db(sid)["lead_model"] == "local-alpha-27b"
    assert studio.get_harness_session_db(sid)["status"] == "paused"

    # Restore `src/module.py` to `sha_v1`.
    mod_file.write_text("VALUE = 1\n", encoding="utf-8")

    # 4. Silent policy escalation (`plan` -> `autonomous`) inside `/switch-model` is refused.
    r_esc = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "policy": "autonomous", "max_switch_attempts": 10},
    )
    assert r_esc.status_code == 409
    assert r_esc.json()["detail"]["code"] == "handoff_permission_escalation_forbidden"
    assert studio.get_harness_session_db(sid)["policy"] == "plan"

    # 5. Local -> Cloud switch without explicit network & billable consent fails closed (`handoff_routing_consent_denied`).
    monkeypatch.setattr(studio.keyring, "set_password", lambda *_a, **_k: None)
    monkeypatch.setattr(studio.keyring, "get_password", lambda *_a, **_k: "sk-test-cloud")
    studio.upsert_cloud_provider(
        alias="cloudprov",
        name="Cloud Provider",
        wire="openai",
        base_url="https://93.184.216.34/v1",
        api_key="sk-test-cloud",
        models=["gpt-secure"],
    )
    r_no_consent = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "cloudprov::gpt-secure", "max_switch_attempts": 10},
    )
    assert r_no_consent.status_code == 409
    assert r_no_consent.json()["detail"]["code"] == "handoff_routing_consent_denied"

    # 6. With explicit FallbackPolicy + FallbackConsent + valid semantic confirmation, switch commits cleanly!
    # First inspect staged core_sha256 by computing expected confirmation or probing via target model mock.
    async def _confirming_stream(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        prompt_blob = msgs[-1]["content"]
        bundle = json.loads(prompt_blob.split("bundle:\n", 1)[1])
        reply = json.dumps(
            {
                "core_sha256": bundle["core_sha256"],
                "policy": bundle["policy"],
                "next_step": bundle["next_step"],
                "referenced_paths": bundle["referenced_paths"],
                "ack_no_kv_cache": True,
                "restated_objective": bundle["objective"],
            }
        )
        yield {"type": "content", "text": reply}

    monkeypatch.setattr(studio, "stream_any_model", _confirming_stream)
    tc.post(f"/api/harness/sessions/{sid}/handoffs/reset-budget", json={"max_switch_attempts": 5})

    r_cloud_ok = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "cloudprov::gpt-secure",
            "fallback_policy": {
                "allow_network_fallback": True,
                "allow_billable_fallback": True,
                "permitted_providers": ["cloudprov"],
            },
            "consent": {
                "consent_id": "consent-cloud-001",
                "network_approved": True,
                "billable_approved": True,
            },
        },
    )
    assert r_cloud_ok.status_code == 200, r_cloud_ok.text
    assert r_cloud_ok.json()["session"]["lead_model"] == "cloudprov::gpt-secure"
    assert r_cloud_ok.json()["handoff"]["portable_core"]["file_references"][0]["sha256"] == sha_v1

    # 7. Post-handoff tampering on disk prevents `/resume` and `/api/harness/run` from activating stale handoff!
    mod_file.write_text("VALUE = 777  # post-handoff tamper\n", encoding="utf-8")
    r_run_tampered = tc.post(
        "/api/harness/run",
        json={"session_id": sid, "prompt": "continue work"},
    )
    assert r_run_tampered.status_code == 409
    assert r_run_tampered.json()["detail"]["code"] == "handoff_source_version_mismatch"


def test_switch_preserves_budgets_and_grants_validity_without_renewal_or_reset(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2: Model switch preserves all task/recovery budget counters and active session/task
    scoped grant validity (no TTL extension, no use reset), revokes old-epoch grants, and refuses
    budget laundering when already exceeded."""
    sess = _create_session(isolated_env, policy="standard")
    sid = sess["id"]
    sm = studio.get_task_state_machine()

    spec = execution_budget.TaskBudgetSpec(
        max_steps=12,
        max_tool_calls=30,
        max_tokens=50_000,
        max_elapsed_s=600.0,
        max_recoveries=2,
    )
    sm.ensure_root_task(
        sid,
        session_id=sid,
        title=sess["title"],
        workspace_dir=str(isolated_env),
        policy="standard",
        budget_spec=spec,
        override_budget_spec=True,
    )
    sm.budgets.record_usage(
        sid,
        steps_delta=4,
        tool_calls_delta=9,
        tokens_delta=12_345,
        elapsed_delta_s=42.5,
    )
    sm.budgets.consume_recovery(sid)
    budget_pre = sm.budgets.get_budget(sid).to_dict()

    # Claim active worker lease epoch 1 so we can also test epoch-pinned vs session-scoped grants.
    coord = studio.get_worker_lease_coordinator(isolated_env)
    claim = coord.acquire_or_takeover(
        sid,
        f"wrk-{studio._PROCESS_BOOT_ID}-ep1",
        session_id=sid,
        workspace_dir=str(isolated_env),
        policy="standard",
    )
    assert claim.epoch_record.epoch == 1

    grant_store = studio.get_scoped_grant_store()
    grant_store.ensure_session_policy(sid, "standard")
    session_grant = grant_store.issue_grant(
        session_id=sid,
        capabilities=["workspace_write", "workspace_read"],
        resource_scopes=["path:src/module.py"],
        ttl_seconds=300.0,
        max_uses=5,
        current_policy="standard",
    )
    # Consume 2 of 5 uses before switching.
    for _ in range(2):
        dec = grant_store.validate_and_consume(
            grant_id=session_grant.grant_id,
            session_id=sid,
            task_id=sid,
            epoch=1,
            tool_name="write_file",
            args={"path": "src/module.py", "content": "x"},
            action_capabilities=("workspace_write",),
            action_scopes=("path:src/module.py",),
        )
        assert dec.allowed is True

    epoch_grant = grant_store.issue_grant(
        session_id=sid,
        epoch=1,
        capabilities=["code_execution"],
        resource_scopes=[f"workspace:{isolated_env}"],
        ttl_seconds=300.0,
        current_policy="standard",
    )
    revoked_grant = grant_store.issue_grant(
        session_id=sid,
        capabilities=["workspace_read"],
        resource_scopes=["path:src/module.py"],
        ttl_seconds=300.0,
        current_policy="standard",
    )
    grant_store.revoke_grant(revoked_grant.grant_id, session_id=sid, reason="operator_revoked")

    pre_session_grant = grant_store.get_grant(session_grant.grant_id)
    assert pre_session_grant is not None
    assert pre_session_grant.uses_consumed == 2

    tc = _client()

    # Requiring the revoked grant must fail closed (`handoff_grant_invalid`).
    r_bad_grant = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "required_grant_ids": [revoked_grant.grant_id],
            "max_switch_attempts": 5,
        },
    )
    assert r_bad_grant.status_code == 409
    assert r_bad_grant.json()["detail"]["code"] == "handoff_grant_invalid"

    # Provide valid target-model semantic confirmation for high-risk switch (`steps_used=4`, `recoveries_used=1`).
    async def _confirming_stream(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        bundle = json.loads(msgs[-1]["content"].split("bundle:\n", 1)[1])
        yield {
            "type": "content",
            "text": json.dumps(
                {
                    "core_sha256": bundle["core_sha256"],
                    "policy": bundle["policy"],
                    "next_step": bundle["next_step"],
                    "referenced_paths": bundle["referenced_paths"],
                    "ack_no_kv_cache": True,
                    "restated_objective": bundle["objective"],
                }
            ),
        }

    monkeypatch.setattr(studio, "stream_any_model", _confirming_stream)

    r_ok = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "required_grant_ids": [session_grant.grant_id],
            "max_switch_attempts": 5,
        },
    )
    assert r_ok.status_code == 200, r_ok.text
    out = r_ok.json()

    # 1. Task and recovery budgets are preserved identically (zero reset/inflation).
    assert out["budget"] == budget_pre
    assert sm.budgets.get_budget(sid).to_dict() == budget_pre

    # 2. Session-scoped grant remains valid with identical expiry and remaining uses (`2/5` consumed),
    #    while epoch-1-pinned grant was revoked upon fencing epoch 1!
    post_session_grant = grant_store.get_grant(session_grant.grant_id)
    assert post_session_grant is not None
    assert post_session_grant.status == "active"
    assert post_session_grant.expires_at == pre_session_grant.expires_at
    assert post_session_grant.uses_consumed == 2
    assert post_session_grant.max_uses == 5

    post_epoch_grant = grant_store.get_grant(epoch_grant.grant_id)
    assert post_epoch_grant is not None
    assert post_epoch_grant.status == "revoked"

    # Incoming worker in epoch 2 can consume the preserved session grant (`uses_consumed` -> 3).
    claim2 = coord.acquire_or_takeover(
        sid,
        f"wrk-{studio._PROCESS_BOOT_ID}-ep2",
        session_id=sid,
        workspace_dir=str(isolated_env),
        policy="standard",
    )
    assert claim2.epoch_record.epoch == 2
    dec2 = grant_store.validate_and_consume(
        grant_id=session_grant.grant_id,
        session_id=sid,
        task_id=sid,
        epoch=2,
        tool_name="write_file",
        args={"path": "src/module.py", "content": "y"},
        action_capabilities=("workspace_write",),
        action_scopes=("path:src/module.py",),
    )
    assert dec2.allowed is True
    assert dec2.grant is not None and dec2.grant.uses_consumed == 3
    coord.release(sid, 2, f"wrk-{studio._PROCESS_BOOT_ID}-ep2", next_state="paused")

    # 3. Exhausting step budget (`13 > 12`) cannot be laundered via another model switch!
    with pytest.raises(execution_budget.BudgetExceeded):
        sm.budgets.record_usage(sid, steps_delta=9)
    r_launder = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-gamma-14b", "max_switch_attempts": 5},
    )
    assert r_launder.status_code == 409
    assert r_launder.json()["detail"]["code"] == "handoff_budget_already_exceeded"


def test_selective_semantic_confirmation_skips_low_risk_and_enforces_high_risk(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3 & AC 3: Low-risk switches skip semantic confirmation; high-risk switches require it
    and fail closed into a safe paused state when the target model hallucinates or omits fields."""
    tc = _client()
    probe_calls: list[str] = []

    # 1. Low-risk switch (brand-new session, 0 steps, 0 edits, local -> local) does NOT call stream_any_model.
    async def _unexpected_stream(*_a: Any, **_k: Any) -> AsyncIterator[dict[str, Any]]:
        probe_calls.append("called")
        yield {"type": "content", "text": "{}"}

    monkeypatch.setattr(studio, "stream_any_model", _unexpected_stream)
    low_sess = _create_session(isolated_env, title="Fresh untouched task")
    r_low = tc.post(
        f"/api/harness/sessions/{low_sess['id']}/switch-model",
        json={"target_lead_model": "local-beta-32b"},
    )
    assert r_low.status_code == 200
    assert probe_calls == []
    assert r_low.json()["handoff"]["harness_extensions"]["risk_level"] == "low"
    assert r_low.json()["handoff"]["harness_extensions"]["semantic_confirmation"]["status"] == "skipped_low_risk"

    # 2. High-risk switch (referenced file + context truncation):
    high_sess = _create_session(isolated_env, title="High risk stateful refactor")
    hsid = high_sess["id"]
    studio._save_harness_messages(
        hsid,
        [
            {"role": "user", "content": "Refactor src/module.py safely"},
            {"role": "assistant", "content": "word " * 40},
            {"role": "user", "content": "word " * 40},
            {"role": "assistant", "content": "latest status summary"},
        ],
    )

    # 2a. Target model hallucinates an unauthorized file `src/hallucinated.py` -> rejected & paused!
    async def _hallucinating_stream(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        bundle = json.loads(msgs[-1]["content"].split("bundle:\n", 1)[1])
        yield {
            "type": "content",
            "text": json.dumps(
                {
                    "core_sha256": bundle["core_sha256"],
                    "policy": bundle["policy"],
                    "next_step": bundle["next_step"],
                    "referenced_paths": ["src/module.py", "src/hallucinated.py"],
                    "ack_no_kv_cache": True,
                    "restated_objective": bundle["objective"],
                }
            ),
        }

    monkeypatch.setattr(studio, "stream_any_model", _hallucinating_stream)
    r_halluc = tc.post(
        f"/api/harness/sessions/{hsid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "referenced_files": ["src/module.py"],
            "target_context_tokens": 30,
            "max_switch_attempts": 4,
        },
    )
    assert r_halluc.status_code == 409
    assert r_halluc.json()["detail"]["code"] == "handoff_semantic_confirmation_failed"
    assert studio.get_harness_session_db(hsid)["lead_model"] == "local-alpha-27b"
    assert studio.get_harness_session_db(hsid)["status"] == "paused"
    assert studio.get_task_state_machine().store.get_task(hsid).state == "paused"  # type: ignore[union-attr]

    # 2b. Target model confirms exact authoritative bundle -> switch commits!
    async def _accurate_stream(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        bundle = json.loads(msgs[-1]["content"].split("bundle:\n", 1)[1])
        yield {
            "type": "content",
            "text": "```json\n"
            + json.dumps(
                {
                    "core_sha256": bundle["core_sha256"],
                    "policy": bundle["policy"],
                    "next_step": bundle["next_step"],
                    "referenced_paths": bundle["referenced_paths"],
                    "ack_no_kv_cache": True,
                    "restated_objective": bundle["objective"],
                }
            )
            + "\n```",
        }

    monkeypatch.setattr(studio, "stream_any_model", _accurate_stream)
    r_high_ok = tc.post(
        f"/api/harness/sessions/{hsid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "referenced_files": ["src/module.py"],
            "target_context_tokens": 30,
            "max_switch_attempts": 4,
        },
    )
    assert r_high_ok.status_code == 200, r_high_ok.text
    hof_ok = r_high_ok.json()["handoff"]
    assert hof_ok["portable_core"]["truncated_messages"] >= 1
    assert hof_ok["harness_extensions"]["risk_level"] == "high"
    assert hof_ok["harness_extensions"]["semantic_confirmation"]["status"] == "verified"
    assert hof_ok["harness_extensions"]["semantic_confirmation"]["mode"] == "target_model_probe"


def test_old_worker_fenced_and_switch_retries_bounded_with_safe_pause(
    isolated_env: Path,
) -> None:
    """Task 4 & AC 3: Switching models fences any active worker lease so stale commits fail closed,
    and enforces bounded per-session switch retries."""
    sess = _create_session(isolated_env, policy="autonomous")
    sid = sess["id"]
    coord = studio.get_worker_lease_coordinator(isolated_env)
    stale_worker = f"wrk-{studio._PROCESS_BOOT_ID}-stale1"
    claim = coord.acquire_or_takeover(
        sid,
        stale_worker,
        session_id=sid,
        workspace_dir=str(isolated_env),
        policy="autonomous",
    )
    assert claim.epoch_record.epoch == 1
    assert claim.task.state == "running"

    tc = _client()

    # Attempt 1 (fails intentionally via invalid reference) STILL fences old worker epoch 1 immediately!
    r1 = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "referenced_files": ["missing_file_1.py"],
            "max_switch_attempts": 2,
        },
    )
    assert r1.status_code == 409
    assert r1.json()["detail"]["code"] == "handoff_invalid_reference"

    # Old worker epoch 1 is now fenced in SQLite AND in ContainmentStore; stale tool calls fail closed!
    ep1 = studio.get_task_state_machine().store.get_epoch(sid, 1)
    assert ep1 is not None and ep1.status == "fenced"
    stale_sess = {
        "id": sid,
        "workspace_dir": str(isolated_env),
        "worktree_dir": str(isolated_env),
        "policy": "autonomous",
        "task_id": sid,
        "epoch": 1,
        "worker_id": stale_worker,
    }
    stale_out = asyncio.run(
        studio._execute_harness_tool(
            None,
            stale_sess,
            "write_file",
            {"path": "src/stale_write.py", "content": "forbidden"},
            action_id="act-stale-after-switch-fence",
            call_index=1,
        )
    )
    assert stale_out["ok"] is False
    assert not (isolated_env / "src" / "stale_write.py").exists()

    # Attempt 2 consumes the 2nd of 2 switch attempts.
    r2 = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "local-beta-32b",
            "referenced_files": ["missing_file_2.py"],
            "max_switch_attempts": 2,
        },
    )
    assert r2.status_code == 409

    # Attempt 3 (even with valid inputs and an attempt to pass a larger cap!) is refused by bounded retries!
    r3 = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "max_switch_attempts": 99},
    )
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "handoff_retry_budget_exceeded"
    assert studio.get_harness_session_db(sid)["status"] == "paused"
    assert studio.get_task_state_machine().store.get_task(sid).state == "paused"  # type: ignore[union-attr]

    # Explicit operator budget reset re-enables switching.
    reset_resp = tc.post(
        f"/api/harness/sessions/{sid}/handoffs/reset-budget",
        json={"max_switch_attempts": 2},
    )
    assert reset_resp.status_code == 200
    assert reset_resp.json()["switch_budget"]["switch_attempts_used"] == 0


def test_sequential_heavy_local_model_switching_unloads_idle_and_refuses_busy_slot(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5-P4-01 + M5-P4-02: Sequential local switching refuses to stack a second heavy model
    while a stream holds the resident slot, and proactively unloads idle resident models once free."""
    sess = _create_session(isolated_env, lead_model="local-alpha-27b")
    sid = sess["id"]
    tc = _client()

    adm = studio._heavy_model_admission()
    busy_lease = adm.acquire("local-alpha-27b")
    try:
        r_busy = tc.post(
            f"/api/harness/sessions/{sid}/switch-model",
            json={"target_lead_model": "local-beta-32b", "max_switch_attempts": 4},
        )
        assert r_busy.status_code == 409
        assert r_busy.json()["detail"]["code"] == "handoff_heavy_model_busy"
        assert studio.get_harness_session_db(sid)["lead_model"] == "local-alpha-27b"
        assert studio.get_harness_session_db(sid)["status"] == "paused"
    finally:
        adm.release(busy_lease)

    unloaded_targets: list[str] = []

    async def _spy_unload(_client_obj: Any, keep_model_id: str) -> list[str]:
        unloaded_targets.append(keep_model_id)
        return ["local-alpha-27b"]

    async def _confirming_stream(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        bundle = json.loads(msgs[-1]["content"].split("bundle:\n", 1)[1])
        yield {
            "type": "content",
            "text": json.dumps(
                {
                    "core_sha256": bundle["core_sha256"],
                    "policy": bundle["policy"],
                    "next_step": bundle["next_step"],
                    "referenced_paths": bundle["referenced_paths"],
                    "ack_no_kv_cache": True,
                    "restated_objective": bundle["objective"],
                }
            ),
        }

    monkeypatch.setattr(studio, "_unload_other_omlx_models", _spy_unload)
    monkeypatch.setattr(studio, "stream_any_model", _confirming_stream)

    r_seq = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={"target_lead_model": "local-beta-32b", "max_switch_attempts": 4},
    )
    assert r_seq.status_code == 200, r_seq.text
    assert unloaded_targets == ["local-beta-32b"]
    adm_trans = r_seq.json()["handoff"]["harness_extensions"]["admission_transition"]
    assert adm_trans["unloaded_models"] == ["local-alpha-27b"]
    assert adm_trans["target_local_model"] == "local-beta-32b"


def test_standalone_studio_bundle_portable_handoff_parity(tmp_path: Path) -> None:
    """Single-file `studio.py` bundle enforces portable handoff & conservative switching outside repo."""
    repo_root = Path(__file__).resolve().parents[1]
    bundle_dir = tmp_path / "standalone_bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "studio.py", bundle_dir / "studio.py")

    script = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path.cwd()))
        import studio

        ws = Path.cwd() / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        studio.WORKSPACE_ROOT = ws
        studio.HARNESS_STORE_DIR = Path.cwd() / "store"
        studio.DB_PATH = Path.cwd() / "db.sqlite"
        studio.init_db()

        sess = studio.create_harness_session_db(
            title="Standalone Switch",
            workspace_dir=str(ws),
            use_worktree=False,
            policy="standard",
            lead_model="m-one",
            critic_model="c-one",
        )
        sid = sess["id"]

        # KV cache portability violation fails closed into paused state.
        try:
            asyncio.run(
                studio.execute_conservative_model_switch_async(
                    None,
                    sid,
                    target_lead_model="m-two",
                    raw_options={"reuse_kv_cache": True},
                )
            )
            raise AssertionError("Expected HandoffRejectedError")
        except studio.routing_policy.HandoffRejectedError as exc:
            assert exc.code == "kv_cache_portability_forbidden"
            assert exc.task_state == "paused"

        studio.get_portable_handoff_store().reset_switch_budget(sid)
        res = asyncio.run(
            studio.execute_conservative_model_switch_async(
                None,
                sid,
                target_lead_model="m-two",
                referenced_files=["a.txt"],
                semantic_confirmation=None,
            )
        )
        raise AssertionError("High-risk referenced_files without confirmation must fail closed")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=str(bundle_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # Wait: the last statement expects HandoffRejectedError because `referenced_files=["a.txt"]` classifies as high-risk!
    assert "handoff_semantic_confirmation_failed" in (proc.stderr or "")

    script_ok = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path.cwd()))
        import studio

        ws = Path.cwd() / "ws"
        studio.WORKSPACE_ROOT = ws
        studio.HARNESS_STORE_DIR = Path.cwd() / "store"
        studio.DB_PATH = Path.cwd() / "db.sqlite"
        studio.init_db()
        sess = studio.create_harness_session_db(
            title="Standalone Low Risk Switch",
            workspace_dir=str(ws),
            use_worktree=False,
            policy="standard",
            lead_model="m-one",
            critic_model="c-one",
        )
        out = asyncio.run(
            studio.execute_conservative_model_switch_async(
                None,
                sess["id"],
                target_lead_model="m-two",
            )
        )
        assert out["status"] == "switched"
        assert out["handoff"]["portable_core"]["kv_cache_portable"] is False
        print("STANDALONE_PORTABLE_HANDOFF_OK")
        """
    )
    proc_ok = subprocess.run(
        [sys.executable, "-I", "-c", script_ok],
        cwd=str(bundle_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc_ok.returncode == 0, proc_ok.stderr
    assert "STANDALONE_PORTABLE_HANDOFF_OK" in proc_ok.stdout


def test_inline_turn_switch_banner_unresolved_side_effects_and_session_purge(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline `POST /api/harness/run` model switch injects portable handoff banner into incoming
    model's system prompt; unresolved side effects refuse switching; session delete purges handoffs."""
    sess = _create_session(isolated_env, policy="standard", lead_model="local-alpha-27b")
    sid = sess["id"]

    with _client() as tc:
        # 1. Uncertain external MCP effect blocks model switching (`handoff_unresolved_side_effects`).
        ledger = studio.get_durable_action_ledger(isolated_env)
        coord = studio.get_worker_lease_coordinator(isolated_env)
        claim = coord.acquire_or_takeover(
            sid,
            f"wrk-{studio._PROCESS_BOOT_ID}-u1",
            session_id=sid,
            workspace_dir=str(isolated_env),
            policy="standard",
        )
        coord.release(sid, claim.epoch_record.epoch, f"wrk-{studio._PROCESS_BOOT_ID}-u1", next_state="paused")
        sem = studio.mcp_client.McpToolSemantics(
            alias="srv_ext",
            tool_name="deploy_release",
            transport="stdio",
            read_only=False,
            destructive=True,
            idempotency_supported=False,
            idempotency_status="unsupported",
            idempotency_mechanism="none",
        )
        studio.get_mcp_effect_store().stage_dispatch(
            action_id="act-mcp-unc-1",
            session_id=sid,
            task_id=sid,
            epoch=claim.epoch_record.epoch,
            semantics=sem,
            arguments={"env": "staging"},
            bound_idempotency_key=None,
        )
        studio.get_mcp_effect_store().record_outcome(
            "act-mcp-unc-1",
            status="uncertain",
            error="socket closed mid-call",
        )

        r_mcp_block = tc.post(
            f"/api/harness/sessions/{sid}/switch-model",
            json={"target_lead_model": "local-beta-32b", "max_switch_attempts": 5},
        )
        assert r_mcp_block.status_code == 409
        assert r_mcp_block.json()["detail"]["code"] == "handoff_unresolved_side_effects"
        assert studio.get_harness_session_db(sid)["status"] == "blocked"

        # Resolve the uncertain MCP effect, return task to `paused`, and reset switch budget.
        studio.get_mcp_effect_store().resolve_operator(
            "act-mcp-unc-1",
            resolution="not_applied",
            reason="verified no external mutation",
        )
        studio.get_task_state_machine().transition_task(
            sid,
            "paused",
            allow_inactive_epoch=True,
            reason="operator_reconciled_mcp_effect",
        )
        assert ledger is not None
        tc.post(f"/api/harness/sessions/{sid}/handoffs/reset-budget", json={"max_switch_attempts": 5})

        # 2. Inline model switch via `POST /api/harness/run` commits handoff and injects banner into system prompt!
        captured_sys_prompts: list[str] = []

        async def _turn_stream(
            _client_obj: Any,
            model_spec: str,
            msgs: list[dict[str, str]],
            **_kw: Any,
        ) -> AsyncIterator[dict[str, Any]]:
            assert model_spec == "local-beta-32b"
            if msgs and msgs[0].get("role") == "system":
                captured_sys_prompts.append(msgs[0]["content"])
            yield {"type": "content", "text": "Completed switched step cleanly."}

        monkeypatch.setattr(studio, "stream_any_model", _turn_stream)
        r_run = tc.post(
            "/api/harness/run",
            json={
                "session_id": sid,
                "lead_model": "local-beta-32b",
                "prompt": "Continue feature implementation after model switch",
                "max_steps": 1,
            },
        )
        assert r_run.status_code == 200
        assert any(
            "[PORTABLE HANDOFF — KV CACHE RECOMPUTED FROM PLAIN TEXT]" in sp
            and "kv_cache_portable: false" in sp
            for sp in captured_sys_prompts
        )

        # 3. `GET /api/harness/sessions/{sid}/handoffs` returns the active handoff; `DELETE` purges it!
        r_list = tc.get(f"/api/harness/sessions/{sid}/handoffs")
        assert r_list.status_code == 200
        assert r_list.json()["active_handoff"] is not None
        assert len(r_list.json()["handoffs"]) >= 1

        r_del = tc.delete(f"/api/harness/sessions/{sid}")
        assert r_del.status_code == 200
        assert studio.get_portable_handoff_store().list_handoffs(session_id=sid) == ()

