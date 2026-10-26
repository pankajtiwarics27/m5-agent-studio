"""Normalized, hardware-independent capability contracts.

This module defines data contracts only. It does not probe the operating system,
select a model, or assume a particular chip, vendor, memory tier, or runtime.
Platform-specific discovery belongs in a separate adapter and can construct a
``HardwareProfile`` from its observations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

HARDWARE_SCHEMA_VERSION = 1
MemoryPressure = Literal["unknown", "nominal", "warning", "critical"]
_MEMORY_PRESSURES = frozenset({"unknown", "nominal", "warning", "critical"})


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise ValueError(f"{field} must not be empty.")
    if "\x00" in result:
        raise ValueError(f"{field} must not contain a NUL character.")
    return result or None


def _bytes(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be a non-negative integer number of bytes.")
    if value < 0:
        raise ValueError(f"{field} must be a non-negative integer number of bytes.")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence of strings.")
    values = tuple(_text(item, field) for item in value)
    if any(item is None for item in values):
        raise ValueError(f"{field} must contain non-empty strings.")
    return tuple(sorted(set(values)))  # type: ignore[arg-type]


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object.")
    return value


@dataclass(frozen=True, slots=True)
class AcceleratorCapability:
    """One generic accelerator description with no vendor-specific enum."""

    kind: str
    vendor: str | None = None
    name: str | None = None
    memory_bytes: int | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(self.kind, "accelerator.kind"))
        object.__setattr__(self, "vendor", _text(self.vendor, "accelerator.vendor", optional=True))
        object.__setattr__(self, "name", _text(self.name, "accelerator.name", optional=True))
        object.__setattr__(self, "memory_bytes", _bytes(self.memory_bytes, "accelerator.memory_bytes", optional=True))
        object.__setattr__(self, "capabilities", _strings(self.capabilities, "accelerator.capabilities"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AcceleratorCapability:
        return cls(
            kind=value.get("kind"),
            vendor=value.get("vendor"),
            name=value.get("name"),
            memory_bytes=value.get("memory_bytes"),
            capabilities=value.get("capabilities", ()),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "vendor": self.vendor,
            "name": self.name,
            "memory_bytes": self.memory_bytes,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    """One installed runtime and the capabilities it advertises."""

    name: str
    version: str | None = None
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "runtime.name"))
        object.__setattr__(self, "version", _text(self.version, "runtime.version", optional=True))
        object.__setattr__(self, "capabilities", _strings(self.capabilities, "runtime.capabilities"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RuntimeCapability:
        return cls(
            name=value.get("name"),
            version=value.get("version"),
            capabilities=value.get("capabilities", ()),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Immutable normalized hardware capabilities observed by an adapter.

    Names and values are data supplied by the discovery adapter. The schema has
    no chip-name allowlist, so it applies equally to different architectures,
    vendors, accelerators, memory sizes, and operating systems.
    """

    operating_system: str
    architecture: str
    accelerators: tuple[AcceleratorCapability, ...] = ()
    physical_memory_bytes: int | None = None
    available_memory_bytes: int | None = None
    memory_pressure: MemoryPressure = "unknown"
    installed_runtimes: tuple[RuntimeCapability, ...] = ()
    permissions: tuple[str, ...] = ()
    schema_version: int = HARDWARE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "operating_system", _text(self.operating_system, "operating_system"))
        object.__setattr__(self, "architecture", _text(self.architecture, "architecture"))
        physical = _bytes(self.physical_memory_bytes, "physical_memory_bytes", optional=True)
        available = _bytes(self.available_memory_bytes, "available_memory_bytes", optional=True)
        if physical is not None and available is not None and available > physical:
            raise ValueError("available_memory_bytes must not exceed physical_memory_bytes.")
        object.__setattr__(self, "physical_memory_bytes", physical)
        object.__setattr__(self, "available_memory_bytes", available)
        if isinstance(self.schema_version, bool) or self.schema_version != HARDWARE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {HARDWARE_SCHEMA_VERSION}.")
        pressure = _text(self.memory_pressure, "memory_pressure")
        if pressure not in _MEMORY_PRESSURES:
            accepted = ", ".join(sorted(_MEMORY_PRESSURES))
            raise ValueError(f"memory_pressure must be one of: {accepted}.")
        object.__setattr__(self, "memory_pressure", pressure)
        accelerators = tuple(self.accelerators)
        if not all(isinstance(item, AcceleratorCapability) for item in accelerators):
            raise ValueError("accelerators must contain AcceleratorCapability values.")
        object.__setattr__(
            self,
            "accelerators",
            tuple(sorted(set(accelerators), key=lambda item: (item.kind, item.vendor or "", item.name or ""))),
        )
        runtimes = tuple(self.installed_runtimes)
        if not all(isinstance(item, RuntimeCapability) for item in runtimes):
            raise ValueError("installed_runtimes must contain RuntimeCapability values.")
        object.__setattr__(self, "installed_runtimes", tuple(sorted(set(runtimes), key=lambda item: (item.name, item.version or ""))))
        object.__setattr__(self, "permissions", _strings(self.permissions, "permissions"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> HardwareProfile:
        payload = _mapping(value, "hardware_profile")
        version = payload.get("schema_version", HARDWARE_SCHEMA_VERSION)
        accelerators_raw = payload.get("accelerators", ())
        runtimes_raw = payload.get("installed_runtimes", ())
        if isinstance(accelerators_raw, (str, bytes)) or not isinstance(accelerators_raw, Sequence):
            raise TypeError("accelerators must be a sequence of objects.")
        if isinstance(runtimes_raw, (str, bytes)) or not isinstance(runtimes_raw, Sequence):
            raise TypeError("installed_runtimes must be a sequence of objects.")
        accelerators = tuple(
            AcceleratorCapability.from_mapping(_mapping(item, "accelerator")) for item in accelerators_raw
        )
        runtimes = tuple(RuntimeCapability.from_mapping(_mapping(item, "runtime")) for item in runtimes_raw)
        return cls(
            operating_system=payload.get("operating_system"),
            architecture=payload.get("architecture"),
            accelerators=accelerators,
            physical_memory_bytes=payload.get("physical_memory_bytes"),
            available_memory_bytes=payload.get("available_memory_bytes"),
            memory_pressure=payload.get("memory_pressure", "unknown"),
            installed_runtimes=runtimes,
            permissions=payload.get("permissions", ()),
            schema_version=version,
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible representation."""
        return {
            "schema_version": self.schema_version,
            "operating_system": self.operating_system,
            "architecture": self.architecture,
            "accelerators": [item.to_mapping() for item in self.accelerators],
            "physical_memory_bytes": self.physical_memory_bytes,
            "available_memory_bytes": self.available_memory_bytes,
            "memory_pressure": self.memory_pressure,
            "installed_runtimes": [item.to_mapping() for item in self.installed_runtimes],
            "permissions": list(self.permissions),
        }
