"""One heavy resident local model at a time, enforced from real capacity.

`M5-P4-01` asks for measured model qualification plus admission that keeps a
single heavy model resident on a 24 GB Apple Silicon machine. `resource_admission`
already provides the atomic reservation primitive; this module turns it into a
*specific* policy for local inference:

* capacity is the machine's unified memory (detected, or supplied by the caller);
* one heavy request reserves ``MA_DEFAULT_REQUEST_FRACTION`` of that capacity, so a
  second simultaneous request cannot fit and is denied by the primitive itself
  rather than by a hand-rolled boolean;
* only one *distinct* model may be resident; asking for a different model while one
  is resident is denied with ``heavy_model_resident`` instead of stacking two
  models into unified memory;
* every denial carries a stable reason code and the snapshot that produced it, so
  the refusal is auditable and testable;
* a lease can be re-entered for nested work on the same model through
  :func:`hold_lease`, so a wrapper calling a wrapped stream does not deadlock
  against itself.

The module is I/O-thin: :func:`detect_unified_memory_bytes` reads the platform's
own memory figure, everything else is arithmetic under a lock. It never loads a
model, talks to a provider, or writes a file — the caller owns the lifetime of the
lease it is granted.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import platform
import re
import subprocess
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import resource_admission

MA_SCHEMA_VERSION = 1
MA_GIB = 1024**3
MA_DEFAULT_REQUEST_FRACTION = 0.8
MA_DEFAULT_CAPACITY_BYTES = 24 * MA_GIB
MA_ENV_CAPACITY = "STUDIO_HEAVY_MODEL_MEMORY_BYTES"
MA_ENV_REQUEST = "STUDIO_HEAVY_MODEL_REQUEST_BYTES"
MA_ENV_SLOTS = "STUDIO_HEAVY_MODEL_SLOTS"

DENY_SLOT_BUSY = "heavy_model_slot_busy"
DENY_MEMORY = "heavy_model_memory_exceeded"
DENY_RESIDENT = "heavy_model_resident"
DENIAL_REASONS = (DENY_SLOT_BUSY, DENY_MEMORY, DENY_RESIDENT)

_MA_MAX_TEXT = 200


class ModelAdmissionError(ValueError):
    """Raised when a limit or request is structurally invalid."""


class HeavyModelDenied(RuntimeError):
    """A heavy local model request was refused by policy."""

    def __init__(self, reason: str, model_id: str, snapshot: Mapping[str, Any]) -> None:
        if reason not in DENIAL_REASONS:
            raise ModelAdmissionError(f"unknown denial reason: {reason!r}")
        self.reason = reason
        self.model_id = model_id
        self.snapshot = dict(snapshot)
        super().__init__(
            f"heavy local model request for {model_id!r} denied ({reason}); "
            f"one heavy model is resident at a time"
        )

    @property
    def code(self) -> str:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "code": self.reason,
            "model_id": self.model_id,
            "message": str(self),
            "admission": dict(self.snapshot),
        }


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelAdmissionError(f"{field_name} must be an integer number of bytes.")
    if value <= 0:
        raise ModelAdmissionError(f"{field_name} must be positive.")
    return value


def _model_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelAdmissionError("model_id must be a non-empty string.")
    clean = value.strip()
    if len(clean) > _MA_MAX_TEXT:
        raise ModelAdmissionError("model_id is too long.")
    return clean


@dataclass(frozen=True, slots=True)
class HeavyModelLimits:
    """Capacity and per-request footprint for heavy local model work."""

    memory_bytes: int
    request_bytes: int
    slots: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "memory_bytes", _positive_int(self.memory_bytes, "limits.memory_bytes"))
        object.__setattr__(self, "request_bytes", _positive_int(self.request_bytes, "limits.request_bytes"))
        object.__setattr__(self, "slots", _positive_int(self.slots, "limits.slots"))
        if self.request_bytes > self.memory_bytes:
            raise ModelAdmissionError("limits.request_bytes must not exceed limits.memory_bytes.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "memory_bytes": self.memory_bytes,
            "request_bytes": self.request_bytes,
            "slots": self.slots,
        }


@dataclass(frozen=True, slots=True)
class HeavyModelLease:
    """An admitted heavy request; releasing it frees the slot and the model name."""

    lease_id: str
    model_id: str
    request_bytes: int
    slots: int = 1

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "model_id": self.model_id,
            "request_bytes": self.request_bytes,
            "slots": self.slots,
        }


# Nested work on the model already admitted by the caller reuses that lease.
_MA_CURRENT_LEASE: contextvars.ContextVar[HeavyModelLease | None] = contextvars.ContextVar(
    "m5_heavy_model_lease", default=None
)


def active_lease() -> HeavyModelLease | None:
    """The lease already held by this task, if any."""
    return _MA_CURRENT_LEASE.get()


@contextlib.contextmanager
def hold_lease(lease: HeavyModelLease) -> Iterator[HeavyModelLease]:
    """Bind ``lease`` to the current context so nested calls reuse it."""
    if not isinstance(lease, HeavyModelLease):
        raise TypeError("lease must be a HeavyModelLease value.")
    token = _MA_CURRENT_LEASE.set(lease)
    try:
        yield lease
    finally:
        _MA_CURRENT_LEASE.reset(token)


def detect_unified_memory_bytes() -> int | None:
    """Platform memory in bytes, or ``None`` when it cannot be read.

    macOS reports unified memory through ``hw.memsize``; Linux reports ``MemTotal``
    in ``/proc/meminfo``. Anything else returns ``None`` so the caller can fall back
    explicitly instead of guessing.
    """
    system = platform.system()
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5.0, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = (out.stdout or "").strip()
        return int(text) if text.isdigit() and int(text) > 0 else None
    if system == "Linux":
        try:
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        return int(line.split(":", 1)[1].split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return None
    return None


def limits_from_environment(
    env: Mapping[str, str] | None = None,
    *,
    detected_memory_bytes: int | None = None,
) -> HeavyModelLimits:
    """Build limits from the environment, falling back to detected memory.

    ``STUDIO_HEAVY_MODEL_MEMORY_BYTES`` overrides capacity, and
    ``STUDIO_HEAVY_MODEL_REQUEST_BYTES`` overrides the per-request footprint;
    ``STUDIO_HEAVY_MODEL_SLOTS`` overrides the slot count (default one). Values are
    validated, so a malformed setting fails before a model request is admitted.
    """
    source = os.environ if env is None else env
    capacity = detected_memory_bytes if detected_memory_bytes else detect_unified_memory_bytes()
    raw_capacity = str(source.get(MA_ENV_CAPACITY, "") or "").strip()
    if raw_capacity:
        if not re.fullmatch(r"[0-9]+", raw_capacity) or int(raw_capacity) <= 0:
            raise ModelAdmissionError(f"{MA_ENV_CAPACITY} must be a positive integer byte count.")
        memory_bytes = int(raw_capacity)
    else:
        memory_bytes = capacity or MA_DEFAULT_CAPACITY_BYTES
    raw_request = str(source.get(MA_ENV_REQUEST, "") or "").strip()
    if raw_request:
        if not re.fullmatch(r"[0-9]+", raw_request) or int(raw_request) <= 0:
            raise ModelAdmissionError(f"{MA_ENV_REQUEST} must be a positive integer byte count.")
        request_bytes = int(raw_request)
    else:
        request_bytes = max(1, int(memory_bytes * MA_DEFAULT_REQUEST_FRACTION))
    raw_slots = str(source.get(MA_ENV_SLOTS, "") or "").strip()
    slots = 1
    if raw_slots:
        if not re.fullmatch(r"[0-9]+", raw_slots) or int(raw_slots) <= 0:
            raise ModelAdmissionError(f"{MA_ENV_SLOTS} must be a positive integer.")
        slots = int(raw_slots)
    return HeavyModelLimits(memory_bytes=memory_bytes, request_bytes=request_bytes, slots=slots)


@dataclass
class _MaCounters:
    grants: int = 0
    releases: int = 0
    peak_concurrent: int = 0
    denials: dict[str, int] = field(default_factory=dict)

    def deny(self, reason: str) -> None:
        self.denials[reason] = self.denials.get(reason, 0) + 1

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grants": self.grants,
            "releases": self.releases,
            "peak_concurrent": self.peak_concurrent,
            "denials": dict(sorted(self.denials.items())),
        }


class HeavyModelAdmission:
    """Admit at most one heavy local model request at a time."""

    def __init__(self, limits: HeavyModelLimits) -> None:
        if not isinstance(limits, HeavyModelLimits):
            raise TypeError("limits must be a HeavyModelLimits value.")
        self.limits = limits
        self._scheduler = resource_admission.ResourceAdmissionScheduler(
            resource_admission.ResourceCapacity(memory_bytes=limits.memory_bytes, slots=limits.slots)
        )
        self._lock = threading.Lock()
        self._leases: dict[str, HeavyModelLease] = {}
        self._scheduler_leases: dict[str, resource_admission.AdmissionLease] = {}
        self._resident_model: str | None = None
        self._counters = _MaCounters()

    # -- policy ------------------------------------------------------------

    def _denial_reason(self, model_id: str) -> str | None:
        """Why ``model_id`` cannot be admitted right now, or ``None``."""
        if self._resident_model is not None and self._resident_model != model_id:
            return DENY_RESIDENT
        snapshot = self._scheduler.snapshot()
        if snapshot.available_slots < 1:
            return DENY_SLOT_BUSY
        if snapshot.available_memory_bytes < self.limits.request_bytes:
            return DENY_MEMORY
        return None

    def would_deny(self, model_id: str) -> str | None:
        """Non-reserving check, for a fast refusal before a stream starts."""
        clean = _model_id(model_id)
        with self._lock:
            return self._denial_reason(clean)

    # -- lifecycle ---------------------------------------------------------

    def _reserve(self, clean: str) -> tuple[HeavyModelLease | None, str | None]:
        """Reserve under the lock, returning ``(lease, denial_reason)``."""
        reason = self._denial_reason(clean)
        if reason is not None:
            self._counters.deny(reason)
            return None, reason
        scheduler_lease = self._scheduler.try_admit(
            resource_admission.ResourceRequest(memory_bytes=self.limits.request_bytes, slots=1)
        )
        if scheduler_lease is None:  # pragma: no cover - the reason check above mirrors it
            self._counters.deny(DENY_SLOT_BUSY)
            return None, DENY_SLOT_BUSY
        granted = HeavyModelLease(
            lease_id=scheduler_lease.lease_id,
            model_id=clean,
            request_bytes=self.limits.request_bytes,
            slots=1,
        )
        self._leases[granted.lease_id] = granted
        self._scheduler_leases[granted.lease_id] = scheduler_lease
        self._resident_model = clean
        self._counters.grants += 1
        self._counters.peak_concurrent = max(self._counters.peak_concurrent, len(self._leases))
        return granted, None

    def try_acquire(self, model_id: str) -> HeavyModelLease | None:
        """Reserve the heavy slot for ``model_id`` or return ``None``."""
        clean = _model_id(model_id)
        with self._lock:
            lease, _reason = self._reserve(clean)
            return lease

    def acquire(self, model_id: str) -> HeavyModelLease:
        """Reserve the heavy slot or raise :class:`HeavyModelDenied`."""
        clean = _model_id(model_id)
        with self._lock:
            lease, reason = self._reserve(clean)
            snapshot = None if lease is not None else self._snapshot_locked()
        if lease is not None:
            return lease
        raise HeavyModelDenied(reason or DENY_SLOT_BUSY, clean, snapshot or {})

    def release(self, lease: HeavyModelLease) -> bool:
        """Release a lease once; unknown or repeated releases are harmless."""
        if not isinstance(lease, HeavyModelLease):
            raise TypeError("lease must be a HeavyModelLease value.")
        with self._lock:
            known = self._leases.pop(lease.lease_id, None)
            scheduler_lease = self._scheduler_leases.pop(lease.lease_id, None)
            if known is None or scheduler_lease is None:
                return False
            self._scheduler.release(scheduler_lease)
            if not self._leases:
                self._resident_model = None
            self._counters.releases += 1
            return True

    # -- observation -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time policy view: capacity, usage, residency and counters."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        usage = self._scheduler.snapshot()
        return {
            "schema_version": MA_SCHEMA_VERSION,
            "limits": self.limits.to_mapping(),
            "capacity_bytes": usage.capacity.memory_bytes,
            "used_bytes": usage.used_memory_bytes,
            "available_bytes": usage.available_memory_bytes,
            "slots": self.limits.slots,
            "used_slots": usage.used_slots,
            "active_leases": usage.active_leases,
            "resident_model": self._resident_model,
            "active_models": sorted({lease.model_id for lease in self._leases.values()}),
            "policy": "one heavy local model at a time",
            "counters": self._counters.to_mapping(),
        }

    def to_mapping(self) -> dict[str, Any]:
        return self.snapshot()
