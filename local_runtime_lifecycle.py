"""Inert cross-runtime observation contracts and the legacy oMLX transport adapter.

Observations describe evidence, never ownership grants, load permissions or
physical release. Constructors and queries perform no I/O or clock discovery.
The legacy adapter remains unchanged and is not a verified release authority.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
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


# LC-01: additive, unactivated observation contracts. Keep helpers namespaced so
# future standalone inlining cannot shadow other boundaries' validation helpers.
RuntimeCapabilityState = Literal["supported", "unsupported", "unknown"]
RuntimeEvidenceBasis = Literal["unverified", "source", "offline", "installed"]
RuntimeFreshness = Literal["fresh", "stale", "unknown"]
RuntimeResidency = Literal["loaded", "loading", "unloading", "absent", "unknown"]


def _observation_text(value: object, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if (not isinstance(value, str) or not value or len(value) > 512
            or value.strip() != value or not value.isprintable()):
        raise ValueError(f"invalid {field}")


def _observation_choice(value: object, choices: tuple[str, ...], field: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"invalid {field}")


def _observation_integer(value: object, field: str, *, minimum: int = 0) -> None:
    if value is not None and (type(value) is not int or value < minimum):
        raise ValueError(f"invalid {field}")


def _observation_time(value: object, field: str) -> None:
    try:
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {field}")


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Explicit registry binding; wire format never implies locality or consent.

    Domain IDs are owner-declared bindings, not automatic host/URL deductions.
    Ownership mode records a declaration, not proof of isolation or a grant.
    """

    runtime_id: str
    backend: str
    wire_protocol: str = "unknown"
    location: Literal["local", "remote", "unknown"] = "unknown"
    egress: Literal["local-only", "may-egress", "unknown"] = "unknown"
    memory_domain_id: str | None = None
    ownership_mode: Literal["attached", "coordinated", "isolated", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        for name in ("runtime_id", "backend", "wire_protocol"):
            _observation_text(getattr(self, name), name)
        _observation_text(self.memory_domain_id, "memory_domain_id", optional=True)
        _observation_choice(self.location, ("local", "remote", "unknown"), "location")
        _observation_choice(self.egress, ("local-only", "may-egress", "unknown"), "egress")
        _observation_choice(self.ownership_mode, ("attached", "coordinated", "isolated", "unknown"), "ownership_mode")

    def declared_domain_relation(self, other: RuntimeIdentity) -> Literal["shared", "distinct", "unknown"]:
        """Compare declared bindings only, not actual physical independence."""
        if not isinstance(other, RuntimeIdentity):
            raise ValueError("invalid runtime identity")
        if self.memory_domain_id is None or other.memory_domain_id is None:
            return "unknown"
        return "shared" if self.memory_domain_id == other.memory_domain_id else "distinct"


@dataclass(frozen=True, slots=True)
class ObservationStamp:
    """Caller-supplied clock scope; persisted pre-restart samples become unknown."""

    observed_at: float | None = None
    clock_id: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at is not None:
            _observation_time(self.observed_at, "observed_at")
        _observation_text(self.clock_id, "clock_id", optional=True)

    def freshness(self, *, now: float, clock_id: str | None, max_age: float) -> RuntimeFreshness:
        _observation_time(now, "now")
        _observation_time(max_age, "max_age")
        _observation_text(clock_id, "clock_id", optional=True)
        if max_age == 0:
            raise ValueError("invalid max_age")
        if self.observed_at is None or self.clock_id is None or clock_id != self.clock_id:
            return "unknown"
        age = now - self.observed_at
        if age < 0:
            return "unknown"
        return "fresh" if age <= max_age else "stale"


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Scoped evidence; source review/offline fixtures never qualify installation."""

    name: str
    state: RuntimeCapabilityState = "unknown"
    basis: RuntimeEvidenceBasis = "unverified"
    runtime_id: str | None = None
    runtime_version: str | None = None
    profile_fingerprint: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _observation_text(self.name, "capability name")
        _observation_choice(self.state, ("supported", "unsupported", "unknown"), "capability state")
        _observation_choice(self.basis, ("unverified", "source", "offline", "installed"), "evidence basis")
        for name in ("runtime_id", "runtime_version", "profile_fingerprint"):
            _observation_text(getattr(self, name), name, optional=True)
        if not isinstance(self.evidence_refs, tuple) or len(self.evidence_refs) > 32:
            raise ValueError("invalid evidence_refs")
        for ref in self.evidence_refs:
            _observation_text(ref, "evidence reference")
        if self.state != "unknown" and (
            self.basis == "unverified" or self.runtime_version is None or not self.evidence_refs
        ):
            raise ValueError("capability claim requires scoped evidence")
        if self.basis == "installed" and self.state != "unknown" and self.runtime_id is None:
            raise ValueError("installed evidence requires runtime identity")

    def state_for_installed_scope(
        self, *, runtime_id: str | None, runtime_version: str | None, profile_fingerprint: str | None,
    ) -> RuntimeCapabilityState:
        """Classify matching recorded evidence, never authorize an operation.

        Caller must separately validate current identity, freshness, ownership,
        grants, residency, operation terminality and resource/request budgets.
        """
        if (self.basis != "installed" or runtime_id is None or self.runtime_id != runtime_id
                or runtime_version is None or profile_fingerprint is None
                or self.runtime_version != runtime_version
                or self.profile_fingerprint != profile_fingerprint):
            return "unknown"
        return self.state


@dataclass(frozen=True, slots=True)
class ModelInstanceObservation:
    """Artifact identity, resident instance and effective allocation are distinct."""

    artifact_id: str
    instance_id: str | None = None
    artifact_digest: str | None = None
    load_profile_fingerprint: str | None = None
    state: RuntimeResidency = "unknown"
    architectural_context_tokens: int | None = None
    effective_context_tokens: int | None = None
    artifact_size_bytes: int | None = None
    resident_memory_bytes: int | None = None
    memory_basis: Literal["unknown", "runtime-reported", "measured"] = "unknown"
    active_requests: int | None = None

    def __post_init__(self) -> None:
        _observation_text(self.artifact_id, "artifact_id")
        for name in ("instance_id", "artifact_digest", "load_profile_fingerprint"):
            _observation_text(getattr(self, name), name, optional=True)
        _observation_choice(self.state, ("loaded", "loading", "unloading", "absent", "unknown"), "residency state")
        for name in ("architectural_context_tokens", "effective_context_tokens"):
            _observation_integer(getattr(self, name), name, minimum=1)
        for name in ("artifact_size_bytes", "resident_memory_bytes", "active_requests"):
            _observation_integer(getattr(self, name), name)
        _observation_choice(self.memory_basis, ("unknown", "runtime-reported", "measured"), "memory_basis")
        if self.resident_memory_bytes is not None and self.memory_basis == "unknown":
            raise ValueError("resident memory requires explicit provenance")


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    """Inert observation snapshot, not a catalog or physical release verifier."""

    runtime: RuntimeIdentity
    runtime_version: str | None = None
    reachability: Literal["reachable", "unreachable", "unknown"] = "unknown"
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    stamp: ObservationStamp = ObservationStamp()
    instances: tuple[ModelInstanceObservation, ...] = ()
    capabilities: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.runtime, RuntimeIdentity) or not isinstance(self.stamp, ObservationStamp):
            raise ValueError("invalid runtime observation")
        _observation_text(self.runtime_version, "runtime_version", optional=True)
        _observation_choice(self.reachability, ("reachable", "unreachable", "unknown"), "reachability")
        _observation_choice(self.completeness, ("complete", "partial", "unknown"), "completeness")
        if not isinstance(self.instances, tuple) or not all(isinstance(x, ModelInstanceObservation) for x in self.instances):
            raise ValueError("invalid instances")
        ids = [x.instance_id for x in self.instances if x.instance_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate instance identity")
        if not isinstance(self.capabilities, tuple) or not all(isinstance(x, CapabilityEvidence) for x in self.capabilities):
            raise ValueError("invalid capabilities")
        if any(x.runtime_id is not None and x.runtime_id != self.runtime.runtime_id for x in self.capabilities):
            raise ValueError("capability evidence belongs to different runtime")
        scopes = [(x.name, x.runtime_version, x.profile_fingerprint) for x in self.capabilities]
        if len(scopes) != len(set(scopes)):
            raise ValueError("duplicate capability scope")

    def residency_at(
        self, *, now: float, clock_id: str | None, max_age: float,
    ) -> Literal["occupied", "empty_observed", "unknown"]:
        """Classify this snapshot only; empty_observed MUST NOT release a hold."""
        fresh = self.stamp.freshness(now=now, clock_id=clock_id, max_age=max_age)
        if self.reachability != "reachable" or fresh != "fresh":
            return "unknown"
        if any(x.state in ("loaded", "loading", "unloading")
               or (x.active_requests is not None and x.active_requests > 0)
               or (x.resident_memory_bytes is not None and x.resident_memory_bytes > 0)
               for x in self.instances):
            return "occupied"
        if self.completeness != "complete" or any(x.state == "unknown" for x in self.instances):
            return "unknown"
        return "empty_observed"
