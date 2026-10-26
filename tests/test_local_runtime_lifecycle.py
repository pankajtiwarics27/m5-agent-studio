from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from local_runtime_lifecycle import LifecycleError, OmlxLifecycleAdapter


def _transport_for(responses: dict[tuple[str, str], tuple[int, Mapping[str, Any]]]):
    calls: list[tuple[str, str, float]] = []

    async def transport(method: str, path: str, timeout: float) -> tuple[int, Mapping[str, Any]]:
        calls.append((method, path, timeout))
        return responses[(method, path)]

    return transport, calls


def test_load_unload_and_telemetry_are_normalized() -> None:
    transport, calls = _transport_for(
        {
            ("POST", "/admin/api/models/model%2Fone/load"): (200, {"status": "loaded"}),
            ("POST", "/v1/models/model%2Fone/unload"): (200, {}),
            ("GET", "/admin/api/models"): (
                200,
                {
                    "models": [
                        {"id": "z-model", "loaded": False},
                        {"id": "a-model", "loaded": True, "active_requests": 2, "actual_size": 123},
                    ],
                    "memory": {"used_bytes": 123, "max_bytes": 456, "pressure": "nominal"},
                },
            ),
        }
    )
    adapter = OmlxLifecycleAdapter(transport)

    async def run() -> tuple[Any, Any, Any]:
        return await adapter.load("model/one"), await adapter.unload("model/one"), await adapter.telemetry()

    loaded, unloaded, telemetry = asyncio.run(run())
    assert loaded.status == "loaded"
    assert unloaded.status == "unloaded"
    assert [model.model_id for model in telemetry.models] == ["a-model", "z-model"]
    assert telemetry.memory_used_bytes == 123
    assert telemetry.memory_max_bytes == 456
    assert calls[0] == ("POST", "/admin/api/models/model%2Fone/load", 120.0)


def test_failures_are_safe_and_malformed_telemetry_is_rejected() -> None:
    async def unavailable(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        raise OSError("secret upstream body")

    with pytest.raises(LifecycleError, match="load: runtime unavailable") as error:
        asyncio.run(OmlxLifecycleAdapter(unavailable).load("model"))
    assert "secret upstream body" not in str(error.value)

    async def malformed(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        return 200, {"models": [{"id": "model", "active_requests": -1}]}

    with pytest.raises(LifecycleError, match="telemetry: invalid active_requests"):
        asyncio.run(OmlxLifecycleAdapter(malformed).telemetry())


def test_non_success_status_and_cancellation_propagate() -> None:
    async def rejected(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        return 503, {"detail": "private upstream detail"}

    with pytest.raises(LifecycleError, match="unload: runtime returned HTTP 503"):
        asyncio.run(OmlxLifecycleAdapter(rejected).unload("model"))

    async def cancelled(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        await asyncio.sleep(60)
        return 200, {}

    async def run_cancelled() -> None:
        await asyncio.wait_for(OmlxLifecycleAdapter(cancelled).load("model"), timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_cancelled())


def test_invalid_model_id_and_timeout_fail_before_transport() -> None:
    async def transport(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        raise AssertionError("transport should not run")

    with pytest.raises(ValueError, match="model_id"):
        asyncio.run(OmlxLifecycleAdapter(transport).load(" "))
    with pytest.raises(ValueError, match="request_timeout"):
        OmlxLifecycleAdapter(transport, request_timeout=0)
