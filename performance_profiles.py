"""Versioned model performance profiles: an immutable, keyed profile vault.

AS-060 persists the :class:`~model_calibration.ModelPerformanceProfile`
values produced by AS-059 into a vault where each profile history is
filed under a key of exactly the six components the roadmap names:
hardware snapshot, operating system, model digest, quantization, runtime
version, and benchmark methodology. History per key is versioned and
immutable - a committed version is never rewritten; a new measurement
becomes version 2, 3, ... - so a comparison or recommendation layer can
pin "the numbers this machine produced for this exact model build"
instead of trusting a mutable record.

Keys are *derived* from the profile at append time, never supplied
freely: the model digest, quantization, runtime version and hardware
snapshot come from the profile's own pinned :class:`CalibrationInputs`,
so a stored profile can never be filed under a label it does not
actually match. For lookups, ``ProfileKey.for_lookup`` composes the same
key from an AS-017 registry descriptor. Re-loading re-verifies every
line digest, the version chain, the header counts, the key's agreement
with each stored profile, and recomputes each profile from its stored
samples via ``model_calibration.calibrate`` before handing it back.

Like the calibration module it builds on, the vault stores no
wall-clock timestamps and no prompt content, and it runs nothing: it is
pure file state with a single-writer discipline (temp file plus
``os.replace`` per write).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_calibration import SCHEMA_VERSION as CALIBRATION_SCHEMA_VERSION
from model_calibration import CalibrationInputs, CalibrationSample, ModelPerformanceProfile, calibrate
from model_registry import ModelDescriptor

VAULT_SCHEMA_VERSION = 1
_MAX_COMPONENT_LENGTH = 128
_MAX_OS_LENGTH = 64
_UNSPECIFIED = "unspecified"
_GENESIS = "sha256:genesis"
_FILE_PREFIX = "profiles-"


class ProfileVaultError(ValueError):
    """Raised when profile vault inputs or key contracts are invalid."""


class ProfileStoreError(RuntimeError):
    """Raised when vault files are corrupt, inconsistent, or foreign."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _line(payload: Mapping[str, Any]) -> str:
    merged = dict(payload)
    merged["record_digest"] = _digest(payload)
    return json.dumps(merged, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _component(value: object, field: str, *, max_length: int = _MAX_COMPONENT_LENGTH) -> str:
    """Validate one bounded, already-normalized key component."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result:
        raise ProfileVaultError(f"{field} must not be empty.")
    if len(result) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ProfileVaultError(f"{field} must be bounded text without control characters.")
    if result == _UNSPECIFIED:
        raise ProfileVaultError(f"{field} must not use the reserved value {_UNSPECIFIED!r}.")
    return result


def _normalize_optional(value: object, field: str) -> str:
    if value is None:
        return _UNSPECIFIED
    return _component(value, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileVaultError(f"{field} must be a positive integer.")
    return value


def _header_int(header: Mapping[str, Any], field: str) -> int:
    value = header.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileStoreError(f"header field {field} must be a non-negative integer.")
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkMethodology:
    """The declared benchmark methodology a profile was measured under.

    The methodology is the sixth key component: profiles measured under
    different methodologies never share a history, and the pinned
    ``calibration_schema`` refuses files written for a different AS-059
    profile layout.
    """

    name: str
    version: int
    calibration_schema: int = CALIBRATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _component(self.name, "methodology name", max_length=_MAX_COMPONENT_LENGTH))
        object.__setattr__(self, "version", _positive_int(self.version, "methodology version"))
        if isinstance(self.calibration_schema, bool) or not isinstance(self.calibration_schema, int):
            raise TypeError("calibration_schema must be an integer.")
        if self.calibration_schema != CALIBRATION_SCHEMA_VERSION:
            raise ProfileVaultError(
                f"methodology calibration_schema must be {CALIBRATION_SCHEMA_VERSION} for profiles of this schema."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": VAULT_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "calibration_schema": self.calibration_schema,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BenchmarkMethodology:
        if not isinstance(value, Mapping):
            raise TypeError("methodology must be an object.")
        if value.get("schema_version") != VAULT_SCHEMA_VERSION:
            raise ProfileVaultError("unsupported methodology schema_version.")
        try:
            return cls(
                name=value["name"],
                version=value["version"],
                calibration_schema=value["calibration_schema"],
            )
        except KeyError as exc:
            raise ProfileVaultError(f"methodology record is missing {exc.args[0]!r}.") from exc

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def _hardware_digest(hardware: Mapping[str, Any] | None) -> str:
    return _digest(dict(hardware)) if hardware is not None else _digest({})


@dataclass(frozen=True, slots=True)
class ProfileKey:
    """The six-component identity one profile history is filed under."""

    hardware_digest: str
    os_id: str
    model_digest: str
    quantization: str
    runtime_version: str
    methodology_digest: str

    def __post_init__(self) -> None:
        for field in ("hardware_digest", "model_digest", "methodology_digest"):
            object.__setattr__(self, field, _component(getattr(self, field), field))
        for field in ("quantization", "runtime_version"):
            object.__setattr__(self, field, _normalize_optional(getattr(self, field), field))
        object.__setattr__(self, "os_id", _component(self.os_id, "os_id", max_length=_MAX_OS_LENGTH))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": VAULT_SCHEMA_VERSION,
            "hardware_digest": self.hardware_digest,
            "os_id": self.os_id,
            "model_digest": self.model_digest,
            "quantization": self.quantization,
            "runtime_version": self.runtime_version,
            "methodology_digest": self.methodology_digest,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProfileKey:
        if not isinstance(value, Mapping):
            raise TypeError("profile key must be an object.")
        if value.get("schema_version") != VAULT_SCHEMA_VERSION:
            raise ProfileVaultError("unsupported profile key schema_version.")
        try:
            return cls(
                hardware_digest=value["hardware_digest"],
                os_id=value["os_id"],
                model_digest=value["model_digest"],
                quantization=value["quantization"],
                runtime_version=value["runtime_version"],
                methodology_digest=value["methodology_digest"],
            )
        except KeyError as exc:
            raise ProfileVaultError(f"profile key is missing {exc.args[0]!r}.") from exc

    @classmethod
    def derive(
        cls,
        profile: ModelPerformanceProfile,
        *,
        methodology: BenchmarkMethodology,
        os_id: str | None = None,
    ) -> ProfileKey:
        """File a profile under the key its own pinned inputs describe."""
        if not isinstance(profile, ModelPerformanceProfile):
            raise TypeError("profile must be a ModelPerformanceProfile value.")
        if not isinstance(methodology, BenchmarkMethodology):
            raise TypeError("methodology must be a BenchmarkMethodology value.")
        inputs = profile.inputs
        if inputs.hardware is None:
            raise ProfileVaultError(
                "profile inputs carry no hardware snapshot; vault keys are hardware-keyed, so a snapshot is required."
            )
        return cls(
            hardware_digest=_hardware_digest(inputs.hardware),
            os_id=os_id if os_id is not None else os_id_from_hardware(inputs.hardware),
            model_digest=inputs.model_digest,
            quantization=_normalize_optional(inputs.quantization, "quantization"),
            runtime_version=_normalize_optional(inputs.runtime_version, "runtime_version"),
            methodology_digest=methodology.digest,
        )

    @classmethod
    def for_lookup(
        cls,
        descriptor: ModelDescriptor,
        *,
        hardware: Mapping[str, Any],
        methodology: BenchmarkMethodology,
        os_id: str | None = None,
        runtime_version: str | None = None,
    ) -> ProfileKey:
        """Compose the vault key for a registry descriptor and a machine.

        Uses the same field sources ``derive`` reads off a profile, so a
        lookup key finds exactly the histories that an append of a
        profile pinned to this descriptor and hardware would file under.
        """
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor value.")
        if not isinstance(hardware, Mapping):
            raise TypeError("hardware must be a mapping snapshot.")
        return cls(
            hardware_digest=_hardware_digest(hardware),
            os_id=os_id if os_id is not None else os_id_from_hardware(hardware),
            model_digest=descriptor.digest,
            quantization=_normalize_optional(descriptor.quantization, "quantization"),
            runtime_version=_normalize_optional(runtime_version, "runtime_version"),
            methodology_digest=methodology.digest,
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())

    @property
    def filename(self) -> str:
        return f"{_FILE_PREFIX}{self.digest.removeprefix('sha256:')}.jsonl"


def os_id_from_hardware(snapshot: Mapping[str, Any]) -> str:
    """Compose an os_id from a HardwareProfile-style snapshot."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("hardware snapshot must be a mapping.")
    operating_system = snapshot.get("operating_system")
    architecture = snapshot.get("architecture")
    if not isinstance(operating_system, str) or not isinstance(architecture, str):
        raise ProfileVaultError("hardware snapshot has no operating_system/architecture; pass os_id explicitly.")
    return _component(f"{operating_system.strip()} {architecture.strip()}".strip(), "os_id", max_length=_MAX_OS_LENGTH)


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    """One immutable committed version inside a key's history.

    ``previous_digest`` is the record digest of the predecessor version
    (``sha256:genesis`` for version 1), chaining the history so deleted
    or reordered middle entries break the chain.
    """

    version: int
    previous_digest: str
    profile: ModelPerformanceProfile

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _positive_int(self.version, "version"))
        if not isinstance(self.profile, ModelPerformanceProfile):
            raise TypeError("profile must be a ModelPerformanceProfile value.")

    @property
    def profile_digest(self) -> str:
        return self.profile.digest

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "previous_digest": self.previous_digest,
            "profile": self.profile.to_mapping(),
            "profile_digest": self.profile_digest,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class AppendOutcome:
    """Result of a vault append: the record and whether it was created."""

    record: ProfileRecord
    created: bool


@dataclass(frozen=True, slots=True)
class KeySummary:
    """Index entry for one stored key history."""

    key: ProfileKey
    first_version: int
    latest_version: int
    next_version: int
    trimmed_versions: int

    @property
    def retained(self) -> int:
        return self.latest_version - self.first_version + 1


def _reconstruct(entry: Mapping[str, Any]) -> ModelPerformanceProfile:
    """Rebuild and fully verify a stored profile carried by a version line."""
    payload = entry.get("profile")
    claimed = entry.get("profile_digest")
    if not isinstance(payload, dict):
        raise ProfileStoreError("stored profile record is not an object.")
    if _digest(payload) != claimed:
        raise ProfileStoreError("stored profile mapping does not hash to its profile_digest.")
    if payload.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        raise ProfileStoreError("profile was written under an unsupported calibration schema.")
    try:
        inputs = CalibrationInputs.from_mapping(payload["inputs"])
        samples = tuple(CalibrationSample.from_mapping(item) for item in payload["samples"])
    except ProfileStoreError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProfileStoreError(f"stored profile records are unreadable: {exc}") from exc
    if inputs.digest != payload.get("inputs_digest"):
        raise ProfileStoreError("stored profile inputs do not match their pinned digest.")
    rebuilt = calibrate(inputs, samples)
    if rebuilt.digest != claimed:
        raise ProfileStoreError("stored profile does not match recomputation from its samples.")
    return rebuilt


def _key_agrees(key: ProfileKey, profile: ModelPerformanceProfile) -> bool:
    inputs = profile.inputs
    return (
        key.hardware_digest == _hardware_digest(inputs.hardware)
        and key.model_digest == inputs.model_digest
        and key.quantization == _normalize_optional(inputs.quantization, "quantization")
        and key.runtime_version == _normalize_optional(inputs.runtime_version, "runtime_version")
    )


class ProfileVault:
    """A directory of immutable, keyed, versioned profile histories.

    ``append`` derives the key from the profile itself, refuses
    duplicate digests among the retained versions (a retry-safe idempotent
    no-op), and rewrites the key's file atomically. Every read re-verifies
    line digests, the version chain, header counts, the key's agreement
    with each stored profile, and recomputes each profile from its samples:
    a tampered vault fails loudly instead of serving plausible numbers.

    Retention is bounded by ``max_versions`` per key - the oldest versions
    are trimmed on append and counted in the header; the dedup guard only
    knows the retained window (documented).
    """

    def __init__(self, directory: Path | str, *, max_versions: int = 32) -> None:
        if not isinstance(directory, (str, Path)) or not str(directory).strip():
            raise ProfileVaultError("vault directory must be a non-empty path.")
        self.directory = Path(directory)
        self.max_versions = _positive_int(max_versions, "max_versions")

    def _path_for(self, key: ProfileKey) -> Path:
        return self.directory / key.filename

    def _read(
        self, path: Path, key: ProfileKey | None
    ) -> tuple[ProfileKey, dict[str, Any], tuple[ProfileRecord, ...]]:
        """Verify one history file end to end; raise on any integrity failure."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ProfileStoreError("vault file disappeared mid-read.") from exc
        entries: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProfileStoreError(f"{path.name}: line {number} is not valid JSON.") from exc
            if not isinstance(payload, dict):
                raise ProfileStoreError(f"{path.name}: line {number} is not an object.")
            claimed = payload.pop("record_digest", None)
            if claimed != _digest(payload):
                raise ProfileStoreError(f"{path.name}: line {number} digest mismatch: record was tampered with.")
            entries.append(payload)
        if not entries or entries[0].get("record") != "key":
            raise ProfileStoreError(f"{path.name}: missing key header record.")
        header = entries[0]
        if header.get("schema_version") != VAULT_SCHEMA_VERSION:
            raise ProfileStoreError(f"{path.name}: unsupported vault schema version.")
        try:
            stored_key = ProfileKey.from_mapping(header["key"])
        except ProfileStoreError as exc:
            raise ProfileStoreError(f"{path.name}: unreadable key header: {exc}") from exc
        if header.get("key_digest") != stored_key.digest:
            raise ProfileStoreError(f"{path.name}: key header does not hash to its key_digest.")
        if path.name != stored_key.filename:
            raise ProfileStoreError(f"{path.name}: file name does not match its key digest.")
        if key is not None and stored_key.digest != key.digest:
            raise ProfileStoreError(f"{path.name}: file belongs to a different key.")

        first = _header_int(header, "first_version")
        latest = _header_int(header, "latest_version")
        nxt = _header_int(header, "next_version")
        trimmed = _header_int(header, "trimmed_versions")
        if first < 1 or latest != nxt - 1 or trimmed != first - 1 or len(entries) - 1 != latest - first + 1:
            raise ProfileStoreError(f"{path.name}: header counts do not match the stored history.")

        records: list[ProfileRecord] = []
        expected_previous = _GENESIS if first == 1 else None
        for position, entry in enumerate(entries[1:], start=1):
            if entry.get("record") != "version":
                raise ProfileStoreError(f"{path.name}: line {position + 1} has an unknown record type.")
            version = entry.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version != first + position - 1:
                raise ProfileStoreError(f"{path.name}: history versions must be contiguous from first_version.")
            previous = entry.get("previous_digest")
            if not isinstance(previous, str) or (expected_previous is not None and previous != expected_previous):
                raise ProfileStoreError(f"{path.name}: version chain broken at entry {position}.")
            profile = _reconstruct(entry)
            if not _key_agrees(stored_key, profile):
                raise ProfileStoreError(f"{path.name}: stored profile no longer agrees with its key.")
            hardware = profile.inputs.hardware
            if hardware is not None and "operating_system" in hardware and "architecture" in hardware:
                if os_id_from_hardware(hardware) != stored_key.os_id:
                    raise ProfileStoreError(f"{path.name}: key os_id disagrees with the stored hardware snapshot.")
            record = ProfileRecord(version=version, previous_digest=previous, profile=profile)
            records.append(record)
            expected_previous = record.digest
        return stored_key, header, tuple(records)

    # -- public API ---------------------------------------------------------

    def append(
        self,
        profile: ModelPerformanceProfile,
        methodology: BenchmarkMethodology,
        *,
        os_id: str | None = None,
    ) -> AppendOutcome:
        """Commit a profile under its derived key; duplicate digests are no-ops."""
        if not isinstance(profile, ModelPerformanceProfile):
            raise TypeError("profile must be a ModelPerformanceProfile value.")
        if not isinstance(methodology, BenchmarkMethodology):
            raise TypeError("methodology must be a BenchmarkMethodology value.")
        key = ProfileKey.derive(profile, methodology=methodology, os_id=os_id)
        hardware = profile.inputs.hardware
        if hardware is not None and "operating_system" in hardware and "architecture" in hardware:
            if os_id_from_hardware(hardware) != key.os_id:
                raise ProfileVaultError("os_id disagrees with the profile's hardware snapshot.")
        path = self._path_for(key)

        latest = 0
        existing: tuple[ProfileRecord, ...] = ()
        if path.is_file():
            _stored_key, header, existing = self._read(path, key)
            latest = _header_int(header, "latest_version")
            for record in existing:
                if record.profile_digest == profile.digest:
                    return AppendOutcome(record=record, created=False)

        previous_digest = existing[-1].digest if existing else _GENESIS
        record = ProfileRecord(version=latest + 1, previous_digest=previous_digest, profile=profile)
        kept = list(existing) + [record]
        dropped = max(0, len(kept) - self.max_versions)
        kept = kept[dropped:]
        header_payload: dict[str, Any] = {
            "record": "key",
            "schema_version": VAULT_SCHEMA_VERSION,
            "key": key.to_mapping(),
            "key_digest": key.digest,
            "first_version": kept[0].version,
            "latest_version": record.version,
            "next_version": record.version + 1,
            "trimmed_versions": kept[0].version - 1,
        }
        lines = [_line(header_payload)]
        for entry in kept:
            lines.append(_line({"record": "version", **entry.to_mapping()}))
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        temp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        os.replace(temp, path)
        return AppendOutcome(record=record, created=True)

    def get(self, key: ProfileKey, version: int | None = None) -> ProfileRecord | None:
        """Return one retained version (latest when ``version`` is None)."""
        if not isinstance(key, ProfileKey):
            raise TypeError("key must be a ProfileKey value.")
        if version is not None:
            _positive_int(version, "version")
        path = self._path_for(key)
        if not path.is_file():
            return None
        _stored_key, _header, records = self._read(path, key)
        if version is None:
            return records[-1] if records else None
        for record in records:
            if record.version == version:
                return record
        return None

    def latest_for_profile(
        self,
        profile: ModelPerformanceProfile,
        methodology: BenchmarkMethodology,
        *,
        os_id: str | None = None,
    ) -> ProfileRecord | None:
        """The newest retained version filed under this profile's own key."""
        return self.get(ProfileKey.derive(profile, methodology=methodology, os_id=os_id))

    def versions(self, key: ProfileKey) -> tuple[int, ...]:
        """Retained version numbers for a key, oldest first; empty when unknown."""
        if not isinstance(key, ProfileKey):
            raise TypeError("key must be a ProfileKey value.")
        path = self._path_for(key)
        if not path.is_file():
            return ()
        _stored_key, _header, records = self._read(path, key)
        return tuple(record.version for record in records)

    def keys(self) -> tuple[KeySummary, ...]:
        """Fully verified summaries of every stored history, by key digest."""
        if not self.directory.is_dir():
            return ()
        summaries: list[KeySummary] = []
        for path in sorted(self.directory.glob(f"{_FILE_PREFIX}*.jsonl")):
            stored_key, header, _records = self._read(path, None)
            summaries.append(
                KeySummary(
                    key=stored_key,
                    first_version=_header_int(header, "first_version"),
                    latest_version=_header_int(header, "latest_version"),
                    next_version=_header_int(header, "next_version"),
                    trimmed_versions=_header_int(header, "trimmed_versions"),
                )
            )
        return tuple(summaries)


__all__ = [
    "AppendOutcome",
    "BenchmarkMethodology",
    "KeySummary",
    "ProfileKey",
    "ProfileRecord",
    "ProfileStoreError",
    "ProfileVault",
    "ProfileVaultError",
    "VAULT_SCHEMA_VERSION",
    "os_id_from_hardware",
]
