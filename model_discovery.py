"""Cross-runtime model discovery and runtime-owned install contracts.

AS-053 aggregates AS-020 local-runtime discovery snapshots into one
deterministic catalog and defines an inert install contract: planning an
install yields the native runtime command argv, and re-discovery proves the
model landed. Newly installed models appear in the catalog without any app
code changes — discovery is driven entirely by runtime-reported model data.

This module performs no network access, no subprocess execution, and no model
loading; transports and command execution belong to the caller.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from local_provider_adapters import LocalRuntimeSnapshot
from model_registry import ModelDescriptor, ModelRegistry

InstallObservationStatus = Literal["installed", "already_present", "not_installed"]

# Runtimes whose native CLI performs model installation. Other runtimes manage
# models through their own import flows, so app-driven install is refused.
SUPPORTED_INSTALL_RUNTIMES: frozenset[str] = frozenset({"ollama"})
_INSTALL_COMMANDS: Mapping[str, tuple[str, str]] = MappingProxyType({"ollama": ("ollama", "pull")})
_DEFAULT_TAGS: Mapping[str, str] = MappingProxyType({"ollama": "latest"})

_MAX_REFERENCE_LENGTH = 256
_MAX_REFERENCE_PATH_SEGMENTS = 6
_REFERENCE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}(:[A-Za-z0-9][A-Za-z0-9._\-]{0,127})?$")
_UNSAFE_REFERENCE_CHARACTERS = frozenset(" \t\n\r\\'\"`;&|<>()$")


class ModelDiscoveryError(ValueError):
    """Raised when a discovery aggregation or install request is invalid."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result:
        raise ModelDiscoveryError(f"{field} must not be empty.")
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ModelDiscoveryError(f"{field} must not contain control characters.")
    return result


def _model_reference(value: object, field: str = "model_reference") -> str:
    reference = _text(value, field)
    if len(reference) > _MAX_REFERENCE_LENGTH:
        raise ModelDiscoveryError(f"{field} must be at most {_MAX_REFERENCE_LENGTH} characters.")
    if "@" in reference:
        raise ModelDiscoveryError(f"{field} must not pin a digest; install by name or tag.")
    if "://" in reference or _UNSAFE_REFERENCE_CHARACTERS.intersection(reference):
        raise ModelDiscoveryError(f"{field} must not contain URLs, whitespace, or shell metacharacters.")
    segments = reference.split("/")
    if len(segments) > _MAX_REFERENCE_PATH_SEGMENTS:
        raise ModelDiscoveryError(f"{field} must have at most {_MAX_REFERENCE_PATH_SEGMENTS} path segments.")
    for segment in segments:
        if segment in {".", ".."} or not _REFERENCE_SEGMENT.match(segment):
            raise ModelDiscoveryError(f"{field} must use runtime-safe name[:tag] segments.")
    return reference


@dataclass(frozen=True, slots=True)
class RuntimeAvailability:
    """Per-runtime discovery outcome, including a safe error when unavailable."""

    runtime: str
    available: bool
    model_count: int
    error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "available": self.available,
            "model_count": self.model_count,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Deterministic union of every discovered model descriptor.

    Duplicate ``(model_id, digest)`` records collapse to one entry (the first
    runtime in name order wins and the model id is recorded as a conflict), so
    ``registry()`` never raises for a catalog built by ``discover_models``.
    """

    descriptors: tuple[ModelDescriptor, ...]
    runtimes: tuple[RuntimeAvailability, ...]
    conflicts: tuple[str, ...] = ()

    def model_ids(self) -> tuple[str, ...]:
        return tuple(sorted({descriptor.model_id for descriptor in self.descriptors}))

    def find(
        self,
        model_id: str,
        *,
        runtime: str | None = None,
        digest: str | None = None,
    ) -> tuple[ModelDescriptor, ...]:
        normalized = _text(model_id, "model_id")
        matches = [descriptor for descriptor in self.descriptors if descriptor.model_id == normalized]
        if runtime is not None:
            matches = [descriptor for descriptor in matches if descriptor.runtime == runtime]
        if digest is not None:
            matches = [descriptor for descriptor in matches if descriptor.digest == digest]
        return tuple(matches)

    def ambiguous_model_ids(self) -> tuple[str, ...]:
        """Model identities that runtimes report with more than one digest."""
        digests: dict[str, set[str]] = {}
        for descriptor in self.descriptors:
            digests.setdefault(descriptor.model_id, set()).add(descriptor.digest)
        return tuple(sorted(model_id for model_id, values in digests.items() if len(values) > 1))

    def registry(self) -> ModelRegistry:
        return ModelRegistry(self.descriptors)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "descriptors": [descriptor.to_mapping() for descriptor in self.descriptors],
            "runtimes": [runtime.to_mapping() for runtime in self.runtimes],
            "conflicts": list(self.conflicts),
            "ambiguous_model_ids": list(self.ambiguous_model_ids()),
        }


class DiscoverySource(Protocol):
    """Structural contract satisfied by AS-020 ``LocalModelAdapter`` subclasses."""

    runtime_name: str

    async def discover(self) -> LocalRuntimeSnapshot: ...


async def _safe_discover(source: DiscoverySource) -> LocalRuntimeSnapshot:
    try:
        snapshot = await source.discover()
    except asyncio.CancelledError:
        raise
    except Exception:
        # One failing runtime must not sink the fan-out; report a safe,
        # fixed-category error without embedding upstream exception text.
        return LocalRuntimeSnapshot(source.runtime_name, False, error="discovery failed")
    if snapshot.runtime != source.runtime_name:
        return LocalRuntimeSnapshot(source.runtime_name, False, error="discovery source contract violation")
    return snapshot


async def discover_models(sources: Sequence[DiscoverySource]) -> ModelCatalog:
    """Aggregate discovery snapshots from local runtimes into one catalog."""
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of discovery sources.")
    if not sources:
        raise ModelDiscoveryError("at least one discovery source is required.")
    names: list[str] = []
    for source in sources:
        name = getattr(source, "runtime_name", None)
        if not isinstance(name, str) or not name.strip():
            raise ModelDiscoveryError("every discovery source must declare a non-empty runtime_name.")
        names.append(name)
    if len(set(names)) != len(names):
        raise ModelDiscoveryError("discovery sources must use unique runtime names.")

    snapshots = await asyncio.gather(*(_safe_discover(source) for source in sources))
    by_runtime: dict[str, LocalRuntimeSnapshot] = {snapshot.runtime: snapshot for snapshot in snapshots}

    entries: list[RuntimeAvailability] = []
    catalog: dict[tuple[str, str], ModelDescriptor] = {}
    conflicts: set[str] = set()
    for runtime_name in sorted(names):
        snapshot = by_runtime[runtime_name]
        per_runtime: dict[tuple[str, str], ModelDescriptor] = {}
        for descriptor in snapshot.models:
            key = (descriptor.model_id, descriptor.digest)
            existing = per_runtime.get(key)
            if existing is not None and existing != descriptor:
                conflicts.add(descriptor.model_id)
                continue
            per_runtime[key] = descriptor
        for key, descriptor in per_runtime.items():
            seen = catalog.get(key)
            if seen is None:
                catalog[key] = descriptor
            elif seen != descriptor:
                conflicts.add(descriptor.model_id)
        entries.append(
            RuntimeAvailability(
                runtime=snapshot.runtime,
                available=snapshot.available,
                model_count=len(snapshot.models),
                error=snapshot.error,
            )
        )
    return ModelCatalog(
        descriptors=tuple(sorted(catalog.values(), key=lambda item: (item.model_id, item.digest))),
        runtimes=tuple(entries),
        conflicts=tuple(sorted(conflicts)),
    )


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """A validated, explicit user intent to install one model via a runtime."""

    runtime: str
    model_reference: str

    def __post_init__(self) -> None:
        runtime = _text(self.runtime, "runtime")
        if runtime not in SUPPORTED_INSTALL_RUNTIMES:
            accepted = ", ".join(sorted(SUPPORTED_INSTALL_RUNTIMES))
            raise ModelDiscoveryError(f"runtime must be one of: {accepted}.")
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "model_reference", _model_reference(self.model_reference))


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Inert argv for the runtime-native install command; never executed here."""

    runtime: str
    model_reference: str
    argv: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"runtime": self.runtime, "model_reference": self.model_reference, "argv": list(self.argv)}


def plan_install(request: InstallRequest) -> InstallPlan:
    if not isinstance(request, InstallRequest):
        raise TypeError("request must be an InstallRequest value.")
    if request.runtime not in _INSTALL_COMMANDS:  # guard for command-table drift
        raise ModelDiscoveryError(f"runtime `{request.runtime}` does not support app-driven install.")
    model_reference = request.model_reference
    if ":" not in model_reference.rsplit("/", 1)[-1]:
        model_reference = f"{model_reference}:{_DEFAULT_TAGS[request.runtime]}"
    command = _INSTALL_COMMANDS[request.runtime]
    return InstallPlan(
        runtime=request.runtime,
        model_reference=model_reference,
        argv=(*command, model_reference),
    )


@dataclass(frozen=True, slots=True)
class InstallObservation:
    """Outcome of comparing catalogs captured before and after an install."""

    status: InstallObservationStatus
    runtime: str
    model_reference: str
    descriptor: ModelDescriptor | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "runtime": self.runtime,
            "model_reference": self.model_reference,
            "descriptor": self.descriptor.to_mapping() if self.descriptor is not None else None,
        }


def observe_install(before: ModelCatalog, after: ModelCatalog, plan: InstallPlan) -> InstallObservation:
    """Classify whether the planned model is now discoverable from its runtime.

    This is the app-code-free acceptance path: any model the runtime begins to
    report appears in the re-discovered catalog, so an install is confirmed by
    data, never by editing discovery or catalog code.
    """
    for name, value in (("before", before), ("after", after)):
        if not isinstance(value, ModelCatalog):
            raise TypeError(f"{name} must be a ModelCatalog value.")
    if not isinstance(plan, InstallPlan):
        raise TypeError("plan must be an InstallPlan value.")
    already = before.find(plan.model_reference, runtime=plan.runtime)
    landed = after.find(plan.model_reference, runtime=plan.runtime)
    if already:
        status: InstallObservationStatus = "already_present"
        descriptor = already[0]
    elif landed:
        status = "installed"
        descriptor = landed[0]
    else:
        status = "not_installed"
        descriptor = None
    return InstallObservation(
        status=status,
        runtime=plan.runtime,
        model_reference=plan.model_reference,
        descriptor=descriptor,
    )
