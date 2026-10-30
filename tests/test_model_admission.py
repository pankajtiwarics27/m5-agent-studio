"""Tests for heavy local model admission (M5-P4-01).

`M5-P4-01` requires that a second simultaneous heavy load is rejected under
policy rather than by luck. These tests pin the policy object, its environment
parsing and its two enforcement points: the local streaming funnel
(`app.stream_any_model`) and the ``429`` pre-flight on the user-facing chat
endpoints. Everything here is arithmetic and fake streams — no model is loaded and
no measurement is implied.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
import model_admission
from model_admission import (
    DENY_MEMORY,
    DENY_RESIDENT,
    DENY_SLOT_BUSY,
    HeavyModelAdmission,
    HeavyModelDenied,
    HeavyModelLease,
    HeavyModelLimits,
    ModelAdmissionError,
    hold_lease,
    limits_from_environment,
)

GIB = 1024**3
LOCAL_MODEL = "Qwen3.8-27B-oQ4e-mtp"


def _limits(memory_gib: float = 24, slots: int = 1) -> HeavyModelLimits:
    memory = int(memory_gib * GIB)
    return HeavyModelLimits(memory_bytes=memory, request_bytes=int(memory * 0.8), slots=slots)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_limits_reject_structurally_invalid_values() -> None:
    with pytest.raises(ModelAdmissionError):
        HeavyModelLimits(memory_bytes=0, request_bytes=1)
    with pytest.raises(ModelAdmissionError):
        HeavyModelLimits(memory_bytes=GIB, request_bytes=0)
    with pytest.raises(ModelAdmissionError):
        HeavyModelLimits(memory_bytes=GIB, request_bytes=GIB, slots=0)
    with pytest.raises(ModelAdmissionError):
        HeavyModelLimits(memory_bytes=GIB, request_bytes=2 * GIB)
    with pytest.raises(ModelAdmissionError):
        HeavyModelLimits(memory_bytes=True, request_bytes=1)  # type: ignore[arg-type]


def test_limits_from_environment_defaults_to_80_percent_of_detected_memory() -> None:
    limits = limits_from_environment({}, detected_memory_bytes=24 * GIB)
    assert limits.memory_bytes == 24 * GIB
    assert limits.request_bytes == int(24 * GIB * model_admission.MA_DEFAULT_REQUEST_FRACTION)
    assert limits.slots == 1


def test_limits_from_environment_falls_back_to_the_documented_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override and no readable memory figure: a documented default, not a guess."""
    monkeypatch.setattr(model_admission, "detect_unified_memory_bytes", lambda: None)
    limits = limits_from_environment({}, detected_memory_bytes=None)
    assert limits.memory_bytes == model_admission.MA_DEFAULT_CAPACITY_BYTES
    assert limits.request_bytes == int(
        model_admission.MA_DEFAULT_CAPACITY_BYTES * model_admission.MA_DEFAULT_REQUEST_FRACTION
    )


def test_limits_from_environment_uses_detected_memory_when_none_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_admission, "detect_unified_memory_bytes", lambda: 64 * GIB)
    assert limits_from_environment({}).memory_bytes == 64 * GIB


def test_limits_from_environment_honours_overrides() -> None:
    limits = limits_from_environment(
        {
            model_admission.MA_ENV_CAPACITY: str(32 * GIB),
            model_admission.MA_ENV_REQUEST: str(20 * GIB),
            model_admission.MA_ENV_SLOTS: "2",
        },
        detected_memory_bytes=16 * GIB,
    )
    assert (limits.memory_bytes, limits.request_bytes, limits.slots) == (32 * GIB, 20 * GIB, 2)


@pytest.mark.parametrize("bad", ["-1", "0", "12g", "1.5", "abc", ""])
def test_limits_from_environment_refuses_malformed_settings(bad: str) -> None:
    env = {model_admission.MA_ENV_CAPACITY: bad}
    if bad == "":
        # An empty value is treated as unset, not as an error.
        assert limits_from_environment(env, detected_memory_bytes=8 * GIB).memory_bytes == 8 * GIB
        return
    with pytest.raises(ModelAdmissionError):
        limits_from_environment(env, detected_memory_bytes=8 * GIB)


def test_detect_unified_memory_reads_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_admission.platform, "system", lambda: "Linux")
    detected = model_admission.detect_unified_memory_bytes()
    assert detected is None or detected > 0


def test_detect_unified_memory_returns_none_on_an_unknown_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_admission.platform, "system", lambda: "Plan9")
    assert model_admission.detect_unified_memory_bytes() is None


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def test_first_request_is_admitted_and_the_second_is_refused() -> None:
    admission = HeavyModelAdmission(_limits())
    lease = admission.acquire(LOCAL_MODEL)
    assert isinstance(lease, HeavyModelLease)
    assert admission.snapshot()["resident_model"] == LOCAL_MODEL

    with pytest.raises(HeavyModelDenied) as excinfo:
        admission.acquire(LOCAL_MODEL)
    assert excinfo.value.code == DENY_SLOT_BUSY
    assert excinfo.value.to_mapping()["admission"]["resident_model"] == LOCAL_MODEL

    assert admission.snapshot()["counters"]["denials"] == {DENY_SLOT_BUSY: 1}
    assert admission.snapshot()["counters"]["peak_concurrent"] == 1


def test_a_different_model_is_refused_while_one_is_resident() -> None:
    admission = HeavyModelAdmission(_limits(slots=2))
    admission.acquire(LOCAL_MODEL)
    with pytest.raises(HeavyModelDenied) as excinfo:
        admission.acquire("Llama-3.3-70B")
    assert excinfo.value.code == DENY_RESIDENT


def test_memory_capacity_is_enforced_independently_of_slots() -> None:
    limits = HeavyModelLimits(memory_bytes=8 * GIB, request_bytes=8 * GIB, slots=4)
    admission = HeavyModelAdmission(limits)
    admission.acquire(LOCAL_MODEL)
    assert admission.would_deny(LOCAL_MODEL) == DENY_MEMORY
    with pytest.raises(HeavyModelDenied) as excinfo:
        admission.acquire(LOCAL_MODEL)
    assert excinfo.value.code == DENY_MEMORY


def test_release_frees_the_slot_and_is_idempotent() -> None:
    admission = HeavyModelAdmission(_limits())
    lease = admission.acquire(LOCAL_MODEL)
    assert admission.release(lease) is True
    assert admission.release(lease) is False
    snapshot = admission.snapshot()
    assert snapshot["resident_model"] is None
    assert snapshot["active_leases"] == 0
    assert snapshot["used_bytes"] == 0
    assert admission.try_acquire("Llama-3.3-70B") is not None


def test_would_deny_does_not_reserve_anything() -> None:
    admission = HeavyModelAdmission(_limits())
    assert admission.would_deny(LOCAL_MODEL) is None
    snapshot = admission.snapshot()
    assert snapshot["active_leases"] == 0
    assert snapshot["counters"]["grants"] == 0
    assert snapshot["counters"]["denials"] == {}


def test_try_acquire_returns_none_instead_of_raising() -> None:
    admission = HeavyModelAdmission(_limits())
    assert admission.try_acquire(LOCAL_MODEL) is not None
    assert admission.try_acquire(LOCAL_MODEL) is None


def test_snapshot_reports_capacity_policy_and_counters() -> None:
    admission = HeavyModelAdmission(_limits())
    lease = admission.acquire(LOCAL_MODEL)
    snapshot = admission.snapshot()
    assert snapshot["schema_version"] == model_admission.MA_SCHEMA_VERSION
    assert snapshot["policy"] == "one heavy local model at a time"
    assert snapshot["limits"] == {"memory_bytes": 24 * GIB, "request_bytes": int(24 * GIB * 0.8), "slots": 1}
    assert snapshot["active_models"] == [LOCAL_MODEL]
    assert snapshot["used_slots"] == 1
    assert snapshot["counters"]["grants"] == 1
    admission.release(lease)
    assert admission.snapshot()["counters"]["releases"] == 1


def test_concurrent_threads_admit_exactly_one_request() -> None:
    admission = HeavyModelAdmission(_limits())
    barrier = threading.Barrier(8)
    results: list[HeavyModelLease | None] = []
    lock = threading.Lock()

    def _race() -> None:
        barrier.wait()
        lease = admission.try_acquire(LOCAL_MODEL)
        with lock:
            results.append(lease)

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    granted = [lease for lease in results if lease is not None]
    assert len(granted) == 1
    assert admission.snapshot()["counters"]["peak_concurrent"] == 1


def test_hold_lease_exposes_the_active_lease_and_resets_it() -> None:
    admission = HeavyModelAdmission(_limits())
    lease = admission.acquire(LOCAL_MODEL)
    assert model_admission.active_lease() is None
    with hold_lease(lease) as held:
        assert held is lease
        assert model_admission.active_lease() is lease
    assert model_admission.active_lease() is None


def test_model_id_validation_and_unknown_denial_reason() -> None:
    admission = HeavyModelAdmission(_limits())
    with pytest.raises(ModelAdmissionError):
        admission.acquire("   ")
    with pytest.raises(ModelAdmissionError):
        HeavyModelDenied("not-a-reason", LOCAL_MODEL, {})


# ---------------------------------------------------------------------------
# App wiring: the streaming funnel
# ---------------------------------------------------------------------------


@pytest.fixture()
def studio_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HeavyModelAdmission:
    """Point the app at a scratch database and a known admission policy."""
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "admission.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 600)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    studio.init_db()
    admission = HeavyModelAdmission(_limits())
    studio._reset_heavy_model_admission(admission)
    try:
        yield admission
    finally:
        studio._reset_heavy_model_admission(None)


def _fake_local_stream(*_args: object, **_kwargs: object) -> AsyncIterator[dict[str, Any]]:
    async def _gen() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "content", "text": "ok"}

    return _gen()


def _collect(model: str) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        async for item in studio.stream_any_model(None, model, [{"role": "user", "content": "hi"}], 0.0, 16, "off"):  # type: ignore[arg-type]
            items.append(item)
        return items

    return asyncio.run(_run())


def test_funnel_admits_one_local_stream_and_refuses_the_second(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_local_stream)
    lease = studio_env.acquire(LOCAL_MODEL)

    with pytest.raises(HeavyModelDenied) as excinfo:
        _collect(LOCAL_MODEL)
    assert excinfo.value.code == DENY_SLOT_BUSY

    studio_env.release(lease)
    assert [item["text"] for item in _collect(LOCAL_MODEL)] == ["ok"]


def test_funnel_releases_the_lease_after_the_stream_finishes(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_local_stream)
    _collect(LOCAL_MODEL)
    snapshot = studio_env.snapshot()
    assert snapshot["active_leases"] == 0
    assert snapshot["resident_model"] is None
    assert snapshot["counters"]["grants"] == 1
    assert snapshot["counters"]["releases"] == 1


def test_funnel_releases_the_lease_when_the_consumer_abandons_the_stream(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_local_stream)

    async def _start_then_close() -> None:
        generator = studio.stream_any_model(None, LOCAL_MODEL, [{"role": "user", "content": "hi"}], 0.0, 16, "off")  # type: ignore[arg-type]
        await generator.__anext__()
        assert studio_env.snapshot()["active_leases"] == 1
        await generator.aclose()

    asyncio.run(_start_then_close())
    assert studio_env.snapshot()["active_leases"] == 0


def test_funnel_reuses_a_lease_held_for_the_same_model(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_local_stream)
    lease = studio_env.acquire(LOCAL_MODEL)

    async def _run() -> list[dict[str, Any]]:
        with hold_lease(lease):
            return [
                item
                async for item in studio.stream_any_model(None, LOCAL_MODEL, [{"role": "user", "content": "hi"}], 0.0, 16, "off")  # type: ignore[arg-type]
            ]

    assert [item["text"] for item in asyncio.run(_run())] == ["ok"]
    assert studio_env.snapshot()["counters"]["grants"] == 1


def test_funnel_refuses_a_second_model_while_one_is_held(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "_stream_omlx_completion", _fake_local_stream)
    lease = studio_env.acquire(LOCAL_MODEL)

    async def _run() -> None:
        with hold_lease(lease):
            with pytest.raises(HeavyModelDenied) as excinfo:
                async for _item in studio.stream_any_model(None, "Llama-3.3-70B", [{"role": "user", "content": "hi"}], 0.0, 16, "off"):  # type: ignore[arg-type]
                    pass
            assert excinfo.value.code == DENY_RESIDENT

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# App wiring: endpoints
# ---------------------------------------------------------------------------


def test_admission_endpoint_reports_the_policy(studio_env: HeavyModelAdmission) -> None:
    with TestClient(studio.app) as client:
        response = client.get("/api/models/admission")
    assert response.status_code == 200
    body = response.json()
    assert body["policy"] == "one heavy local model at a time"
    assert body["limits"]["slots"] == 1
    assert "counters" in body


def test_chat_stream_endpoint_returns_429_while_a_heavy_model_is_resident(
    studio_env: HeavyModelAdmission,
) -> None:
    lease = studio_env.acquire(LOCAL_MODEL)
    try:
        with TestClient(studio.app) as client:
            response = client.post(
                "/api/chat/stream",
                json={"model": LOCAL_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            )
    finally:
        studio_env.release(lease)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == DENY_SLOT_BUSY
    assert detail["admission"]["resident_model"] == LOCAL_MODEL


def test_openai_gateway_returns_429_while_a_heavy_model_is_resident(
    studio_env: HeavyModelAdmission,
) -> None:
    lease = studio_env.acquire(LOCAL_MODEL)
    try:
        with TestClient(studio.app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": LOCAL_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            )
    finally:
        studio_env.release(lease)
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == DENY_SLOT_BUSY


def test_preflight_ignores_models_that_do_not_route_local(
    studio_env: HeavyModelAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio, "is_local_omlx_model", lambda _spec: False)
    lease = studio_env.acquire("some-cloud-model")
    try:
        with TestClient(studio.app) as client:
            response = client.get("/api/models/admission")
    finally:
        studio_env.release(lease)
    assert response.status_code == 200
    studio._heavy_model_preflight("openai/gpt-4o")  # must not raise


def test_preflight_raises_http_429_with_the_policy_snapshot(studio_env: HeavyModelAdmission) -> None:
    lease = studio_env.acquire(LOCAL_MODEL)
    try:
        with pytest.raises(studio.HTTPException) as excinfo:
            studio._heavy_model_preflight(LOCAL_MODEL)
    finally:
        studio_env.release(lease)
    assert excinfo.value.status_code == 429
    detail = excinfo.value.detail
    assert detail["code"] == DENY_SLOT_BUSY
    assert "one heavy model at a time" in detail["message"]
    assert detail["admission"]["active_leases"] == 1


def test_admission_policy_is_built_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(model_admission.MA_ENV_CAPACITY, str(32 * GIB))
    monkeypatch.setenv(model_admission.MA_ENV_REQUEST, str(16 * GIB))
    studio._reset_heavy_model_admission(None)
    try:
        policy = studio._heavy_model_admission()
        assert policy.limits.memory_bytes == 32 * GIB
        assert policy.limits.request_bytes == 16 * GIB
        assert studio._heavy_model_admission() is policy
    finally:
        studio._reset_heavy_model_admission(None)
        monkeypatch.delenv(model_admission.MA_ENV_CAPACITY)
        monkeypatch.delenv(model_admission.MA_ENV_REQUEST)


def test_the_standalone_bundle_carries_the_same_policy() -> None:
    """studio.py must not import a sibling module to enforce the policy."""
    import json
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    if not (root / "studio.py").is_file():
        pytest.skip("studio.py has not been generated in this checkout")
    code = (
        "import json, studio\n"
        "policy = studio._heavy_model_admission()\n"
        "lease = policy.acquire('bundle-probe')\n"
        "second = policy.try_acquire('bundle-probe') is None\n"
        "policy.release(lease)\n"
        "print(json.dumps({"
        "'policy': policy.snapshot()['policy'], 'second_refused': second,"
        "'aliased': studio.model_admission is studio and studio.resource_admission is studio}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=root, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"policy": "one heavy local model at a time", "second_refused": True, "aliased": True}
