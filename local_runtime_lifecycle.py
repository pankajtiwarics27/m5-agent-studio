"""Provider-neutral lifecycle adapter for a local oMLX-compatible runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote


class RuntimeTransport(Protocol):
    def __call__(self, method: str, path: str, timeout: float) -> Awaitable[tuple[int, Mapping[str, Any]]]: ...


class LifecycleError(RuntimeError):
    """Safe runtime failure that does not expose upstream response bodies."""

    def __init__(self, operation: str, detail: str = "runtime operation failed") -> None:
        self.operation = operation
        super().__init__(f"{operation}: {detail}")


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    operation: str
    model_id: str
    status: str


@dataclass(frozen=True, slots=True)
class RuntimeModelState:
    model_id: str
    loaded: bool
    active_requests: int = 0
    memory_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeTelemetry:
    models: tuple[RuntimeModelState, ...]
    memory_used_bytes: int | None
    memory_max_bytes: int | None
    pressure: str


def _model_id(value: str) -> str:
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError("model_id must be a non-empty string without NUL characters.")
    return normalized


def _non_negative(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleError("telemetry", f"invalid {field}")
    return value


class OmlxLifecycleAdapter:
    """Adapt load, unload, and telemetry endpoints behind an injected transport."""

    def __init__(self, transport: RuntimeTransport, *, request_timeout: float = 30.0) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive.")
        self._transport = transport
        self._request_timeout = request_timeout

    async def _request(self, operation: str, method: str, path: str, timeout: float | None = None) -> Mapping[str, Any]:
        try:
            status, payload = await self._transport(method, path, timeout or self._request_timeout)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise LifecycleError(operation, "runtime unavailable") from exc
        if status < 200 or status >= 300:
            raise LifecycleError(operation, f"runtime returned HTTP {status}")
        if not isinstance(payload, Mapping):
            raise LifecycleError(operation, "runtime returned an invalid response")
        return payload

    async def load(self, model_id: str) -> LifecycleResult:
        normalized = _model_id(model_id)
        payload = await self._request(
            "load",
            "POST",
            f"/admin/api/models/{quote(normalized, safe='')}/load",
            timeout=max(self._request_timeout, 120.0),
        )
        status = payload.get("status", "loaded")
        if not isinstance(status, str) or not status.strip():
            raise LifecycleError("load", "runtime returned an invalid status")
        return LifecycleResult("load", normalized, status.strip())

    async def unload(self, model_id: str) -> LifecycleResult:
        normalized = _model_id(model_id)
        payload = await self._request(
            "unload",
            "POST",
            f"/v1/models/{quote(normalized, safe='')}/unload",
        )
        status = payload.get("status", "unloaded")
        if not isinstance(status, str) or not status.strip():
            raise LifecycleError("unload", "runtime returned an invalid status")
        return LifecycleResult("unload", normalized, status.strip())

    async def telemetry(self) -> RuntimeTelemetry:
        payload = await self._request("telemetry", "GET", "/admin/api/models")
        raw_models = payload.get("models", ())
        if isinstance(raw_models, (str, bytes)) or not isinstance(raw_models, Sequence):
            raise LifecycleError("telemetry", "runtime returned invalid models")
        models: list[RuntimeModelState] = []
        for raw in raw_models:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
                raise LifecycleError("telemetry", "runtime returned an invalid model record")
            models.append(
                RuntimeModelState(
                    model_id=raw["id"].strip(),
                    loaded=bool(raw.get("loaded")),
                    active_requests=_non_negative(raw.get("active_requests", 0), "active_requests"),
                    memory_bytes=_non_negative(raw.get("memory_bytes", raw.get("actual_size", 0)), "memory_bytes"),
                )
            )
        memory = payload.get("memory")
        if memory is not None and not isinstance(memory, Mapping):
            raise LifecycleError("telemetry", "runtime returned invalid memory data")
        memory = memory or {}
        used = memory.get("used_bytes")
        maximum = memory.get("max_bytes")
        return RuntimeTelemetry(
            models=tuple(sorted(models, key=lambda item: item.model_id)),
            memory_used_bytes=None if used is None else _non_negative(used, "memory.used_bytes"),
            memory_max_bytes=None if maximum is None else _non_negative(maximum, "memory.max_bytes"),
            pressure=str(memory.get("pressure", "unknown")),
        )
