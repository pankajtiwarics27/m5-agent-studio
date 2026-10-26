from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from capability_negotiation import TaskRequirements
from hardware_capabilities import HardwareProfile
from local_provider_adapters import LlamaCppAdapter, OllamaAdapter


def _transport_for(payload: Mapping[str, Any], path: str):
    calls: list[tuple[str, str, float]] = []

    async def transport(method: str, requested_path: str, timeout: float) -> tuple[int, Mapping[str, Any]]:
        calls.append((method, requested_path, timeout))
        assert requested_path == path
        return 200, payload

    return transport, calls


def test_ollama_adapter_discovers_and_negotiates_capabilities() -> None:
    transport, calls = _transport_for(
        {
            "models": [
                {
                    "name": "fixture:latest",
                    "digest": "sha256:ollama",
                    "size": 100,
                    "modified_at": "revision",
                    "details": {"families": ["chat", "streaming", "tools"], "context_length": 4096},
                }
            ]
        },
        "/api/tags",
    )
    adapter = OllamaAdapter(transport)
    snapshot = asyncio.run(adapter.discover())
    decisions = adapter.negotiate(snapshot, TaskRequirements(required_capabilities=("tools",)))

    assert snapshot.available is True
    assert snapshot.models[0].runtime == "ollama"
    assert decisions[0].feasible is True
    assert calls == [("GET", "/api/tags", 5.0)]


def test_llama_cpp_adapter_maps_openai_model_shape_and_rejects_missing_capability() -> None:
    transport, _ = _transport_for(
        {
            "data": [
                {
                    "id": "fixture.gguf",
                    "digest": "sha256:llama",
                    "context_length": 2048,
                    "capabilities": ["chat", "streaming"],
                }
            ]
        },
        "/v1/models",
    )
    adapter = LlamaCppAdapter(transport)
    snapshot = asyncio.run(adapter.discover())
    decisions = adapter.negotiate(snapshot, TaskRequirements(required_capabilities=("tools",)), HardwareProfile("OS", "arch"))

    assert snapshot.available is True
    assert snapshot.models[0].source == "llama.cpp"
    assert decisions[0].feasible is False
    assert "missing model capabilities: tools" in decisions[0].reasons


def test_unavailable_and_malformed_runtimes_return_safe_snapshots() -> None:
    async def unavailable(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        raise OSError("private runtime body")

    snapshot = asyncio.run(OllamaAdapter(unavailable).discover())
    assert snapshot.available is False
    assert snapshot.models == ()
    assert "private runtime body" not in (snapshot.error or "")

    async def malformed(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        return 200, {"models": [{"details": {}}]}

    malformed_snapshot = asyncio.run(OllamaAdapter(malformed).discover())
    assert malformed_snapshot.available is False
    assert "without an id" in (malformed_snapshot.error or "")


def test_cancellation_propagates_from_local_runtime() -> None:
    async def slow(_method: str, _path: str, _timeout: float) -> tuple[int, Mapping[str, Any]]:
        await asyncio.sleep(60)
        return 200, {}

    async def run() -> None:
        await asyncio.wait_for(OllamaAdapter(slow).discover(), timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
