"""End-to-end verification for M5-P6-03: Advanced calibration and multi-model experiments.

Covers all 4 implementation tasks and all 3 acceptance criteria:
- Task 1: Benchmark measured recovery costs and drift
- Task 2: Test model affinity/hysteresis and sequential handoffs
- Task 3: Keep cloud opt-in and privacy constrained
- Task 4: Do not enable unmeasured simultaneous residency
- AC 1: Measured verified completion benefit
- AC 2: 24 GB resource admission upheld
- AC 3: Cloud never enabled without eligibility
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import app as studio
import artifact_lineage
import model_admission
import recovery_guard
import routing_experiments as rexp
import routing_policy
import workspace_quarantine

AUTH_TOKEN = "r" * 40
GIB = 1024**3


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "app_mod.py").write_text("X = 1\n", encoding="utf-8")

    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "studio_p6_03.db"

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.delenv("STUDIO_ENABLE_ROUTING_EXPERIMENTS", raising=False)

    studio._reset_routing_experiment_store(None)
    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_mcp_effect_store(None)
    studio._reset_heavy_model_admission(
        model_admission.HeavyModelAdmission(
            model_admission.HeavyModelLimits(
                memory_bytes=24 * GIB,
                request_bytes=int(24 * GIB * 0.8),
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


def _limits_24gb() -> model_admission.HeavyModelLimits:
    return model_admission.HeavyModelLimits(
        memory_bytes=24 * GIB,
        request_bytes=int(24 * GIB * 0.8),
        slots=1,
    )


def test_calibration_profiles_drift_detection_and_recovery_cost_benchmarking(tmp_path: Path) -> None:
    """Task 1: Pinned cold/warm calibration profiles, upward drift detection, and recovery cost records."""
    store = rexp.RoutingExperimentStore(tmp_path / "store")
    limits = _limits_24gb()

    prof = rexp.CalibrationProfileRecord.from_samples(
        model_id="qwen2.5-coder-14b-4bit",
        model_digest="sha256:base14b",
        quantization="4bit",
        runtime="omlx",
        context_length=32768,
        hardware_memory_bytes=24 * GIB,
        samples=(
            rexp.TimedCalibrationSample(
                sample_id="c1",
                run_kind="cold",
                ttft_ms=700.0,
                prefill_tokens_per_s=600.0,
                decode_tokens_per_s=48.0,
                peak_memory_bytes=int(10.5 * GIB),
                swap_unload_ms=180.0,
                swap_load_ms=610.0,
            ),
            rexp.TimedCalibrationSample(
                sample_id="w1",
                run_kind="warm",
                ttft_ms=150.0,
                prefill_tokens_per_s=740.0,
                decode_tokens_per_s=50.0,
                peak_memory_bytes=int(10.5 * GIB),
            ),
            rexp.TimedCalibrationSample(
                sample_id="f1",
                run_kind="warm",
                ttft_ms=9999.0,
                prefill_tokens_per_s=1.0,
                decode_tokens_per_s=1.0,
                peak_memory_bytes=int(10.5 * GIB),
                succeeded=False,
                error_reason="transient timeout Authorization: Bearer sk-secret0123456789abcdef",
            ),
        ),
        now=1761901000.0,
    )
    # Failed sample is counted in failure_rate (1/3) without polluting timing medians.
    assert prof.cold_ttft_ms_median == 700.0
    assert prof.warm_ttft_ms_median == 150.0
    assert round(prof.failure_rate, 4) == 0.3333
    fits, code = prof.fits_admission_limits(limits)
    assert fits is True and code is None
    store.save_calibration_profile(prof)

    # Stable probe shows zero drift.
    stable_rep = rexp.evaluate_calibration_drift(
        prof,
        (
            rexp.TimedCalibrationSample(
                sample_id="w2",
                run_kind="warm",
                ttft_ms=155.0,
                prefill_tokens_per_s=735.0,
                decode_tokens_per_s=49.0,
                peak_memory_bytes=int(10.6 * GIB),
            ),
        ),
        limits=limits,
    )
    assert stable_rep.drifted is False
    assert stable_rep.recalibration_required is False

    # Upward memory/quantization drift exceeding 19.2 GiB request ceiling triggers recalibration_required.
    drifted_rep = rexp.evaluate_calibration_drift(
        prof,
        (
            rexp.TimedCalibrationSample(
                sample_id="c-drift",
                run_kind="cold",
                ttft_ms=1500.0,
                prefill_tokens_per_s=240.0,
                decode_tokens_per_s=18.0,
                peak_memory_bytes=int(22.5 * GIB),
            ),
        ),
        observed_quantization="8bit",
        limits=limits,
    )
    assert drifted_rep.drifted is True
    assert drifted_rep.recalibration_required is True
    assert drifted_rep.exceeds_24gb_admission is True
    store.record_drift_report(drifted_rep)

    rec_cost = store.record_recovery_measurement(
        rexp.RecoveryCostMeasurement(
            recovery_id="rec-001",
            session_id="sess-001",
            model_id="qwen2.5-coder-14b-4bit",
            stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
            failed_attempts=2,
            retries_used=1,
            rollbacks_used=1,
            replans_used=1,
            wasted_tokens=640,
            recovery_latency_ms=410.0,
            swap_cost_ms=790.0,
            re_prefill_tokens=420,
            escaped_stagnation=True,
            verified_completed=True,
            recorded_at=1761901010.0,
        )
    )
    assert rec_cost.total_overhead_ms == 1200.0
    assert rec_cost.total_recovery_tokens == 1060

    reloaded = rexp.RoutingExperimentStore(tmp_path / "store")
    assert reloaded.summary()["recalibration_required_models"] == ["qwen2.5-coder-14b-4bit"]
    assert reloaded.summary()["recovery_measurement_count"] == 1


def test_affinity_hysteresis_and_sequential_handoffs_uphold_24gb_admission(tmp_path: Path) -> None:
    """Task 2 & AC 2: Sticky affinity/hysteresis prevents thrashing; stagnation triggers sequential
    unload-then-admit handoff with zero KV-cache portability and <=1 resident heavy model."""
    store = rexp.RoutingExperimentStore(tmp_path / "store")
    limits = _limits_24gb()
    profiles = rexp._re_seed_standard_24gb_profiles(store, now=1761902000.0)

    # 1. Cold store fails closed (ab_gate_open=False) and keeps resident without switching.
    cold_dec = store.evaluate_session_routing(
        session_id="sess-cold",
        resident_model_id="qwen2.5-coder-7b-4bit",
        resident_utility=0.40,
        candidates=(rexp.CandidateModelOption("qwen2.5-coder-32b-4bit", expected_utility=0.95),),
        stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
        consecutive_failures=2,
        limits=limits,
    )
    assert cold_dec.switched is False
    assert cold_dec.action == "ab_gate_closed_keep_resident"

    # Open A/B gate via genuine paired suite.
    ab = rexp.run_routing_calibration_ab_suite(runs_per_variant=2)
    assert ab["ab_gate_passed"] is True
    store.record_ab_summary(ab)

    # 2. Optional disablement (`enabled=False` or `env_enabled=False`) cleanly keeps resident.
    env_off = store.evaluate_session_routing(
        session_id="sess-env-off",
        resident_model_id="qwen2.5-coder-7b-4bit",
        resident_utility=0.40,
        candidates=(rexp.CandidateModelOption("qwen2.5-coder-32b-4bit", expected_utility=0.95),),
        stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
        consecutive_failures=2,
        limits=limits,
        env_enabled=False,
    )
    assert env_off.switched is False
    assert env_off.action == "disabled_keep_resident"

    # 3. Hysteresis suppresses minor score jitter (+0.04 raw utility) to prevent ping-pong thrashing.
    jitter_dec = store.evaluate_session_routing(
        session_id="sess-jitter",
        resident_model_id="qwen2.5-coder-14b-4bit",
        resident_utility=0.79,
        candidates=(rexp.CandidateModelOption("qwen2.5-coder-32b-4bit", expected_utility=0.83),),
        steps_since_last_switch=3,
        stagnation_verdict=recovery_guard.STAGNATION_PROGRESS,
        consecutive_failures=0,
        context_tokens_to_re_prefill=1000,
        limits=limits,
    )
    assert jitter_dec.switched is False
    assert jitter_dec.action == "keep_resident_hysteresis"

    # 4. Stagnation triggers sequential handoff to calibrated 32B-4bit (18 GiB <= 19.2 GiB).
    stag_dec = store.evaluate_session_routing(
        session_id="sess-stag",
        resident_model_id="qwen2.5-coder-7b-4bit",
        resident_utility=0.55,
        candidates=(rexp.CandidateModelOption("qwen2.5-coder-32b-4bit", expected_utility=0.92),),
        steps_since_last_switch=3,
        stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
        consecutive_failures=2,
        context_tokens_to_re_prefill=500,
        limits=limits,
    )
    assert stag_dec.switched is True
    assert stag_dec.action == "sequential_handoff"
    assert stag_dec.selected_model_id == "qwen2.5-coder-32b-4bit"
    assert stag_dec.kv_cache_portable is False

    # Execute sequential handoff on HeavyModelAdmission: outgoing lease released BEFORE target admitted.
    adm = model_admission.HeavyModelAdmission(limits)
    lease_a = adm.acquire("qwen2.5-coder-7b-4bit")

    # Trying to switch WITHOUT releasing an unrelated active lease fails closed (`heavy_model_resident`).
    with pytest.raises(rexp.CalibrationAdmissionDeniedError) as busy_exc:
        rexp.execute_sequential_local_handoff(
            admission=adm,
            active_lease=None,
            target_model_id="qwen2.5-coder-32b-4bit",
            target_profile=profiles["qwen2.5-coder-32b-4bit"],
            messages=({"role": "user", "content": "fix bug"},),
        )
    assert busy_exc.value.code == "heavy_model_resident"

    lease_b, clean_msgs, stripped = rexp.execute_sequential_local_handoff(
        admission=adm,
        active_lease=lease_a,
        target_model_id="qwen2.5-coder-32b-4bit",
        target_profile=profiles["qwen2.5-coder-32b-4bit"],
        messages=(
            {
                "role": "user",
                "content": "fix bug",
                "kv_cache_id": "kv-99",
                "prompt_cache_key": "pc-1",
                "thinking_signature": "sig-2",
            },
        ),
    )
    snap = adm.snapshot()
    assert snap["resident_model"] == "qwen2.5-coder-32b-4bit"
    assert snap["counters"]["peak_concurrent"] == 1
    assert snap["used_bytes"] <= 24 * GIB
    assert set(stripped) == {"kv_cache_id", "prompt_cache_key", "thinking_signature"}
    assert clean_msgs == ({"role": "user", "content": "fix bug"},)
    adm.release(lease_b)


def test_unmeasured_simultaneous_residency_forbidden_end_to_end(isolated_env: Path) -> None:
    """Task 4 & AC 2: Unmeasured or multi-model simultaneous residency is forbidden across module,
    config route, evaluate route, and session switch-model route."""
    store = studio.get_routing_experiment_store()
    profiles = rexp._re_seed_standard_24gb_profiles(store, now=1761903000.0)
    limits = _limits_24gb()

    # 1. Direct helper rejects unmeasured simultaneous residency AND measured co-residency on 1-slot 24 GB M5.
    with pytest.raises(rexp.SimultaneousResidencyForbiddenError) as unm_exc:
        rexp.validate_residency_and_24gb_admission(
            requested_models=("qwen2.5-coder-7b-4bit", "unmeasured-local-8b"),
            simultaneous_residency=True,
            profiles_by_model=profiles,
            limits=limits,
        )
    assert unm_exc.value.code == "unmeasured_simultaneous_residency_forbidden"

    with pytest.raises(rexp.SimultaneousResidencyForbiddenError) as co_exc:
        rexp.validate_residency_and_24gb_admission(
            requested_models=("qwen2.5-coder-7b-4bit", "qwen2.5-coder-14b-4bit"),
            simultaneous_residency=True,
            profiles_by_model=profiles,
            limits=limits,
        )
    assert co_exc.value.code == "simultaneous_residency_exceeds_24gb"

    tc = _client()

    # 2. Config endpoint refuses enabling co-residency.
    r_cfg = tc.post("/api/harness/routing-experiments/config", json={"allow_measured_co_residency": True})
    assert r_cfg.status_code == 409
    assert r_cfg.json()["detail"]["code"] == "unmeasured_simultaneous_residency_forbidden"

    # 3. Evaluate endpoint refuses simultaneous_residency=True and increments audit counter.
    r_eval = tc.post(
        "/api/harness/routing-experiments/evaluate",
        json={
            "session_id": "sess-sim",
            "resident_model_id": "qwen2.5-coder-7b-4bit",
            "simultaneous_residency": True,
            "candidates": [{"model_id": "qwen2.5-coder-14b-4bit", "expected_utility": 0.85}],
        },
    )
    assert r_eval.status_code == 409
    assert r_eval.json()["detail"]["code"] == "unmeasured_simultaneous_residency_forbidden"
    assert store.summary()["simultaneous_residency_denials"] == 1

    # 4. Session switch-model route also fails closed into paused on simultaneous_residency=True.
    sess = studio.create_harness_session_db(
        title="Verify simultaneous residency block",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="standard",
        lead_model="qwen2.5-coder-7b-4bit",
        critic_model="qwen2.5-coder-7b-4bit",
    )
    r_sw = tc.post(
        f"/api/harness/sessions/{sess['id']}/switch-model",
        json={"target_lead_model": "qwen2.5-coder-14b-4bit", "simultaneous_residency": True},
    )
    assert r_sw.status_code == 409
    assert r_sw.json()["detail"]["code"] == "unmeasured_simultaneous_residency_forbidden"
    assert studio.get_harness_session_db(sess["id"])["status"] == "paused"


def test_cloud_never_enabled_without_full_opt_in_consent_and_privacy_eligibility(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 3 & AC 3: Cloud routing fails closed across default-off opt-in, missing consent,
    unpermitted provider, secret artifact lineage, credential exposure, and quarantine holds."""
    full_pol = routing_policy.FallbackPolicy(
        allow_network_fallback=True,
        allow_billable_fallback=True,
        permitted_providers=("anthropic",),
    )
    full_cons = routing_policy.FallbackConsent(
        consent_id="consent-ok-01",
        network_approved=True,
        billable_approved=True,
    )

    # 1. Default opt-in disabled -> denied even with policy + consent.
    d1 = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=False,
        fallback_policy=full_pol,
        consent=full_cons,
    )
    assert d1.eligible is False and d1.denial_code == "cloud_opt_in_disabled"

    # 2. Opt-in enabled, missing per-request consent -> denied.
    d2 = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=routing_policy.FallbackConsent(),
    )
    assert d2.eligible is False and d2.denial_code == "cloud_consent_missing"

    # 3. Unpermitted provider -> denied.
    d3 = rexp.evaluate_cloud_eligibility(
        model_id="other::gpt-x",
        provider="other",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=full_cons,
    )
    assert d3.eligible is False and d3.denial_code == "cloud_provider_not_permitted"

    # 4. Secret artifact in session lineage -> unconditionally denied even with opt-in + consent!
    sec_sha = artifact_lineage.content_digest(b"PRIVATE_PRODUCTION_KEY")
    d4 = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=full_cons,
        artifact_confidentialities=((sec_sha, "secret"),),
    )
    assert d4.eligible is False and d4.denial_code == "cloud_blocked_secret_artifact"
    assert d4.secret_artifacts_blocked == (sec_sha,)

    # 5. Unredacted credential token in outbound text -> denied.
    d5 = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=full_cons,
        outbound_texts=("export GITHUB_TOKEN=ghp_0123456789abcdefghij0123456789",),
    )
    assert d5.eligible is False and d5.denial_code == "cloud_blocked_credential_exposure"

    # 6. Active quarantine/containment hold -> denied.
    d6 = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=full_cons,
        has_quarantine_or_containment_hold=True,
    )
    assert d6.eligible is False and d6.denial_code == "cloud_blocked_quarantine_or_containment"

    # 7. Clean internal context + opt-in + permitted provider + explicit consent -> eligible!
    d_ok = rexp.evaluate_cloud_eligibility(
        model_id="anthropic::claude-3-7-sonnet",
        provider="anthropic",
        cloud_opt_in_enabled=True,
        fallback_policy=full_pol,
        consent=full_cons,
        privacy_class="internal",
        artifact_confidentialities=((artifact_lineage.content_digest(b"public readme"), "public"),),
        outbound_texts=("Refactor pure sorting helper.",),
    )
    assert d_ok.eligible is True and d_ok.denial_code is None

    # 8. Live `/switch-model` route blocks local->cloud switch when session has a secret artifact!
    monkeypatch.setattr(studio.keyring, "set_password", lambda *_a, **_k: None)
    monkeypatch.setattr(studio.keyring, "get_password", lambda *_a, **_k: "sk-test-cloud")
    studio.upsert_cloud_provider(
        alias="anthropic",
        name="Anthropic Cloud",
        wire="openai",
        base_url="https://93.184.216.34/v1",
        api_key="sk-test-cloud",
        models=["claude-3-7-sonnet"],
    )
    sess = studio.create_harness_session_db(
        title="Secret session cannot switch to cloud",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="standard",
        lead_model="qwen2.5-coder-7b-4bit",
        critic_model="qwen2.5-coder-7b-4bit",
    )
    sid = sess["id"]
    artifact_lineage.ArtifactRegistry(studio.HARNESS_STORE_DIR).record_artifact(
        sec_sha,
        source_kind="tool_output",
        source_ref=".env.production",
        trust="untrusted",
        confidentiality="secret",
        task_id=sid,
    )
    tc = _client()
    r_priv_block = tc.post(
        f"/api/harness/sessions/{sid}/switch-model",
        json={
            "target_lead_model": "anthropic::claude-3-7-sonnet",
            "fallback_policy": {
                "allow_network_fallback": True,
                "allow_billable_fallback": True,
                "permitted_providers": ["anthropic"],
            },
            "consent": {
                "consent_id": "cons-secret-attempt",
                "network_approved": True,
                "billable_approved": True,
            },
        },
    )
    assert r_priv_block.status_code == 409
    assert r_priv_block.json()["detail"]["code"] == "handoff_cloud_privacy_denied"
    assert studio.get_harness_session_db(sid)["lead_model"] == "qwen2.5-coder-7b-4bit"
    assert studio.get_harness_session_db(sid)["status"] == "paused"
    assert workspace_quarantine.find_active_quarantine(studio.HARNESS_STORE_DIR, isolated_env) is None


def test_paired_ab_suite_and_routing_experiment_http_routes_end_to_end(isolated_env: Path) -> None:
    """AC 1, AC 2, AC 3: Paired 24-run A/B benchmark demonstrates verified completion benefit,
    24 GB admission compliance, zero unmeasured simultaneous residency, and zero unauthorized cloud."""
    tc = _client()

    # 1. Calibrate and drift-check via HTTP routes.
    r_cal = tc.post(
        "/api/harness/routing-experiments/calibrate",
        json={
            "model_id": "qwen2.5-coder-14b-4bit",
            "model_digest": "sha256:14bdigest",
            "quantization": "4bit",
            "runtime": "omlx",
            "samples": [
                {
                    "sample_id": "s1",
                    "run_kind": "cold",
                    "ttft_ms": 670.0,
                    "prefill_tokens_per_s": 620.0,
                    "decode_tokens_per_s": 47.0,
                    "peak_memory_bytes": int(10.5 * GIB),
                    "swap_unload_ms": 180.0,
                    "swap_load_ms": 600.0,
                }
            ],
        },
    )
    assert r_cal.status_code == 200, r_cal.text
    assert r_cal.json()["fits_24gb_admission"] is True

    r_drift = tc.post(
        "/api/harness/routing-experiments/drift-check",
        json={
            "model_id": "qwen2.5-coder-14b-4bit",
            "samples": [
                {
                    "sample_id": "s2",
                    "run_kind": "warm",
                    "ttft_ms": 160.0,
                    "prefill_tokens_per_s": 720.0,
                    "decode_tokens_per_s": 48.0,
                    "peak_memory_bytes": int(10.5 * GIB),
                }
            ],
        },
    )
    assert r_drift.status_code == 200, r_drift.text
    assert r_drift.json()["drift_report"]["recalibration_required"] is False

    # 2. Run paired A/B evaluation suite and verify acceptance criteria.
    r_ab = tc.post("/api/harness/routing-experiments/evaluate-ab", json={"runs_per_variant": 3})
    assert r_ab.status_code == 200, r_ab.text
    ab = r_ab.json()
    assert ab["ab_gate_passed"] is True
    overall = ab["overall"]
    assert overall["calibrated_verified_completion_rate"] == 1.0
    assert overall["baseline_verified_completion_rate"] == 0.25
    assert overall["completion_rate_delta"] == 0.75
    assert overall["calibrated_tokens_median"] < overall["baseline_tokens_median"]
    assert overall["peak_concurrent_heavy_models"] == 1
    assert overall["max_calibrated_admitted_memory_bytes"] <= int(24 * GIB * 0.8)
    assert overall["admission_24gb_violations"] == 0
    assert overall["unmeasured_simultaneous_accepted"] == 0
    assert overall["cloud_unauthorized_activations"] == 0

    # 3. Verify GET /api/harness/routing-experiments and task summary expose live state.
    r_get = tc.get("/api/harness/routing-experiments")
    assert r_get.status_code == 200
    assert r_get.json()["config"]["ab_gate_open"] is True
    assert r_get.json()["config"]["cloud_opt_in_enabled"] is False
    assert r_get.json()["config"]["allow_measured_co_residency"] is False

    sess = studio.create_harness_session_db(
        title="Inspect task routing experiments summary",
        workspace_dir=str(isolated_env),
        use_worktree=False,
        policy="standard",
        lead_model="qwen2.5-coder-14b-4bit",
        critic_model="qwen2.5-coder-14b-4bit",
    )
    r_task = tc.get(f"/api/harness/sessions/{sess['id']}/task")
    assert r_task.status_code == 200
    assert r_task.json()["routing_experiments"]["config"]["ab_gate_open"] is True
