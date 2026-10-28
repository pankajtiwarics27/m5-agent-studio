"""Verified, deterministic publication bundles for reproducible model benchmarks.

The publisher packages caller-produced AS-059 calibration files; it does not
run a model or claim that packaged data represents any hardware beyond its
pinned inputs. Raw profile records are integrity-checked before inclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hardware_capabilities import HardwareProfile
from model_calibration import ModelPerformanceProfile, load_profile
from performance_profiles import BenchmarkMethodology, ProfileKey

PUBLICATION_SCHEMA_VERSION = 1
_MAX_CAVEATS = 20
_MAX_CAVEAT_LENGTH = 500
_MAX_PROFILES = 64
_MAX_PROFILE_BYTES = 16 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_WARMUP_CAVEAT = "AS-059 discards warmup runs; the configured warmup count is declared but not verifiable from saved profile samples."
_PROMPT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_FAILURES = (
    re.compile(r"probe failure: [A-Za-z_][A-Za-z0-9_.]{0,95}"),
)
_SAFE_FAILURE_TEXT = frozenset({"inconsistent timing: output tokens without a first-token timestamp"})
_HARDWARE_FIELDS = frozenset(
    {
        "schema_version",
        "operating_system",
        "architecture",
        "accelerators",
        "physical_memory_bytes",
        "available_memory_bytes",
        "memory_pressure",
        "installed_runtimes",
        "permissions",
    }
)
_ACCELERATOR_FIELDS = frozenset({"kind", "vendor", "name", "memory_bytes", "capabilities"})
_RUNTIME_FIELDS = frozenset({"name", "version", "capabilities"})


class BenchmarkPublicationError(ValueError):
    """Raised when publication inputs are incomplete, unsafe, or incomparable."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _integer(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchmarkPublicationError(f"{field} must be an integer greater than or equal to {minimum}.")
    return value


def _caveat(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("caveats must contain strings.")
    cleaned = " ".join("".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split())
    if not cleaned:
        raise BenchmarkPublicationError("caveats must not be empty.")
    if len(cleaned) > _MAX_CAVEAT_LENGTH:
        raise BenchmarkPublicationError(f"each caveat must be at most {_MAX_CAVEAT_LENGTH} characters.")
    return cleaned


def _validate_hardware(snapshot: Mapping[str, Any]) -> None:
    unknown = set(snapshot) - _HARDWARE_FIELDS
    if unknown:
        raise BenchmarkPublicationError("hardware snapshot contains unsupported fields; remove private metadata before publishing.")
    accelerators = snapshot.get("accelerators", ())
    if not isinstance(accelerators, Sequence) or isinstance(accelerators, (str, bytes)):
        raise BenchmarkPublicationError("hardware accelerators must be a sequence.")
    for item in accelerators:
        if not isinstance(item, Mapping) or set(item) - _ACCELERATOR_FIELDS:
            raise BenchmarkPublicationError("hardware accelerator contains unsupported fields.")
    runtimes = snapshot.get("installed_runtimes", ())
    if not isinstance(runtimes, Sequence) or isinstance(runtimes, (str, bytes)):
        raise BenchmarkPublicationError("hardware runtimes must be a sequence.")
    for item in runtimes:
        if not isinstance(item, Mapping) or set(item) - _RUNTIME_FIELDS:
            raise BenchmarkPublicationError("hardware runtime contains unsupported fields.")
    try:
        HardwareProfile.from_mapping(snapshot)
    except (TypeError, ValueError) as exc:
        raise BenchmarkPublicationError("hardware snapshot is invalid.") from exc


def _validate_profile_safety(profile: ModelPerformanceProfile) -> None:
    hardware = profile.inputs.hardware
    if hardware is None:
        raise BenchmarkPublicationError("profiles without a hardware snapshot cannot be published.")
    _validate_hardware(hardware)
    prompt_digest = profile.inputs.prompt_set_digest
    if prompt_digest is not None and not _PROMPT_DIGEST.fullmatch(prompt_digest):
        raise BenchmarkPublicationError("prompt_set_digest must be a SHA-256 digest, never prompt text.")
    for sample in profile.samples:
        error = sample.error
        if error is not None and error not in _SAFE_FAILURE_TEXT and not any(pattern.fullmatch(error) for pattern in _SAFE_FAILURES):
            raise BenchmarkPublicationError("sample error text is not a safe exception-only reason; redact it before publishing.")


@dataclass(frozen=True, slots=True)
class BenchmarkPublicationConfig:
    """The measured suite plan and caveats published with raw profiles."""

    methodology: BenchmarkMethodology
    warmup_runs: int
    warm_repeats: int
    cold_repeats: int
    caveats: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.methodology, BenchmarkMethodology):
            raise TypeError("methodology must be a BenchmarkMethodology value.")
        object.__setattr__(self, "warmup_runs", _integer(self.warmup_runs, "warmup_runs", minimum=0))
        object.__setattr__(self, "warm_repeats", _integer(self.warm_repeats, "warm_repeats", minimum=1))
        object.__setattr__(self, "cold_repeats", _integer(self.cold_repeats, "cold_repeats", minimum=0))
        if isinstance(self.caveats, (str, bytes)) or not isinstance(self.caveats, Sequence):
            raise TypeError("caveats must be a sequence of strings.")
        caveats = {_caveat(item) for item in self.caveats}
        if not caveats:
            raise BenchmarkPublicationError("at least one caveat is required.")
        if self.warmup_runs:
            caveats.add(_WARMUP_CAVEAT)
        cleaned = tuple(sorted(caveats))
        if len(cleaned) > _MAX_CAVEATS:
            raise BenchmarkPublicationError(f"at most {_MAX_CAVEATS} caveats may be published.")
        object.__setattr__(self, "caveats", cleaned)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "methodology": self.methodology.to_mapping(),
            "calibration_plan": {
                "warmup_runs": self.warmup_runs,
                "warm_repeats": self.warm_repeats,
                "cold_repeats": self.cold_repeats,
            },
            "caveats": list(self.caveats),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BenchmarkPublicationConfig:
        if not isinstance(value, Mapping):
            raise TypeError("publication config must be an object.")
        if value.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
            raise BenchmarkPublicationError("unsupported publication config schema_version.")
        methodology_raw = value.get("methodology")
        plan = value.get("calibration_plan")
        caveats = value.get("caveats")
        if not isinstance(methodology_raw, Mapping) or not isinstance(plan, Mapping):
            raise BenchmarkPublicationError("publication config must include methodology and calibration_plan objects.")
        if isinstance(caveats, (str, bytes)) or not isinstance(caveats, Sequence):
            raise BenchmarkPublicationError("publication config caveats must be a sequence of strings.")
        normalized_caveats = tuple(_caveat(item) for item in caveats)
        try:
            methodology = BenchmarkMethodology.from_mapping(methodology_raw)
            return cls(
                methodology=methodology,
                warmup_runs=plan["warmup_runs"],
                warm_repeats=plan["warm_repeats"],
                cold_repeats=plan["cold_repeats"],
                caveats=normalized_caveats,
            )
        except KeyError as exc:
            raise BenchmarkPublicationError("publication config is missing a required field.") from exc

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class PublishedProfile:
    """One validated profile and the exact original raw JSONL source."""

    model_id: str
    model_digest: str
    profile_digest: str
    inputs_digest: str
    profile_key: ProfileKey
    raw_sha256: str
    raw_profile_jsonl: str
    measured_runs: int
    failed_runs: int
    failure_rate: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "profile_digest": self.profile_digest,
            "inputs_digest": self.inputs_digest,
            "profile_key": self.profile_key.to_mapping(),
            "raw_sha256": self.raw_sha256,
            "raw_profile_jsonl": self.raw_profile_jsonl,
            "measured_runs": self.measured_runs,
            "failed_runs": self.failed_runs,
            "failure_rate": self.failure_rate,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkPublicationBundle:
    """A content-addressed bundle of raw profiles, config, and disclosures."""

    config: BenchmarkPublicationConfig
    config_digest: str
    calibration_module_digest: str
    publisher_script_digest: str
    hardware_digest: str
    os_id: str
    methodology_digest: str
    profiles: tuple[PublishedProfile, ...]

    def _payload_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "config": self.config.to_mapping(),
            "config_digest": self.config_digest,
            "calibration_module_digest": self.calibration_module_digest,
            "publisher_script_digest": self.publisher_script_digest,
            "context": {
                "hardware_digest": self.hardware_digest,
                "os_id": self.os_id,
                "methodology_digest": self.methodology_digest,
            },
            "profiles": [item.to_mapping() for item in self.profiles],
        }

    def to_mapping(self) -> dict[str, Any]:
        payload = self._payload_mapping()
        payload["bundle_digest"] = _digest(payload)
        return payload

    @property
    def digest(self) -> str:
        return _digest(self._payload_mapping())


def verify_bundle_mapping(value: Mapping[str, Any]) -> str:
    """Verify the bundle-level canonical digest; raw profiles retain line seals."""
    if not isinstance(value, Mapping):
        raise TypeError("bundle must be an object.")
    claimed = value.get("bundle_digest")
    payload = {key: item for key, item in value.items() if key != "bundle_digest"}
    if not isinstance(claimed, str) or claimed != _digest(payload):
        raise BenchmarkPublicationError("benchmark bundle digest mismatch.")
    if value.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
        raise BenchmarkPublicationError("unsupported benchmark bundle schema_version.")
    return claimed


def _load_raw_profile(path: Path) -> tuple[ModelPerformanceProfile, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BenchmarkPublicationError(f"profile input is unavailable ({type(exc).__name__}).") from exc
    if not raw or len(raw) > _MAX_PROFILE_BYTES:
        raise BenchmarkPublicationError("profile input is empty or exceeds the publication size limit.")
    try:
        raw_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkPublicationError("profile input is not UTF-8 JSONL.") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="m5-benchmark-verify-") as temp_dir:
            staged = Path(temp_dir) / "profile.jsonl"
            staged.write_bytes(raw)
            profile = load_profile(staged)
    except Exception as exc:
        raise BenchmarkPublicationError(f"profile failed integrity verification ({type(exc).__name__}).") from exc
    if not isinstance(profile, ModelPerformanceProfile):
        raise BenchmarkPublicationError("profile input did not produce a calibration profile.")
    if not raw_text.endswith("\n"):
        raise BenchmarkPublicationError("profile JSONL must end with a newline for stable publication.")
    return profile, raw


def build_publication_bundle(
    profile_paths: Sequence[Path | str],
    config: BenchmarkPublicationConfig,
    *,
    publisher_script: Path | str,
) -> BenchmarkPublicationBundle:
    """Verify profile files and build one same-context publication bundle."""
    if isinstance(profile_paths, (str, bytes)) or not isinstance(profile_paths, Sequence) or not profile_paths:
        raise BenchmarkPublicationError("profile_paths must be a non-empty sequence of files.")
    if len(profile_paths) > _MAX_PROFILES:
        raise BenchmarkPublicationError(f"at most {_MAX_PROFILES} profiles may be bundled.")
    if not isinstance(config, BenchmarkPublicationConfig):
        raise TypeError("config must be a BenchmarkPublicationConfig value.")
    try:
        script_bytes = Path(publisher_script).read_bytes()
    except (OSError, TypeError) as exc:
        raise BenchmarkPublicationError(f"publisher script is unavailable ({type(exc).__name__}).") from exc
    if not script_bytes or len(script_bytes) > 4 * 1024 * 1024:
        raise BenchmarkPublicationError("publisher script is empty or exceeds the size limit.")
    calibration_path = Path(__file__).resolve().with_name("model_calibration.py")
    try:
        calibration_bytes = calibration_path.read_bytes()
    except OSError as exc:
        raise BenchmarkPublicationError(f"calibration module is unavailable ({type(exc).__name__}).") from exc
    if not calibration_bytes or len(calibration_bytes) > 4 * 1024 * 1024:
        raise BenchmarkPublicationError("calibration module is empty or exceeds the size limit.")

    entries: list[PublishedProfile] = []
    identities: set[tuple[str, str]] = set()
    anchor: tuple[str, str, str] | None = None
    total_raw = 0
    for raw_path in profile_paths:
        if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
            raise TypeError("profile_paths must contain non-empty file paths.")
        profile, raw = _load_raw_profile(Path(raw_path))
        total_raw += len(raw)
        if total_raw > _MAX_BUNDLE_BYTES:
            raise BenchmarkPublicationError("raw profiles exceed the bundle size limit.")
        _validate_profile_safety(profile)
        inputs = profile.inputs
        warm_count = sum(sample.kind == "warm" for sample in profile.samples)
        cold_count = sum(sample.kind == "cold" for sample in profile.samples)
        if warm_count != config.warm_repeats or cold_count != config.cold_repeats:
            raise BenchmarkPublicationError("profile sample counts do not match the published calibration plan.")
        key = ProfileKey.derive(profile, methodology=config.methodology)
        context = (key.hardware_digest, key.os_id, key.methodology_digest)
        if anchor is None:
            anchor = context
        elif context != anchor:
            raise BenchmarkPublicationError("profiles use incomparable hardware, OS, or methodology contexts.")
        identity = (inputs.model_id, inputs.model_digest)
        if identity in identities:
            raise BenchmarkPublicationError("duplicate model identity in publication bundle.")
        identities.add(identity)
        entries.append(
            PublishedProfile(
                model_id=inputs.model_id,
                model_digest=inputs.model_digest,
                profile_digest=profile.digest,
                inputs_digest=inputs.digest,
                profile_key=key,
                raw_sha256=_sha256_bytes(raw),
                raw_profile_jsonl=raw.decode("utf-8"),
                measured_runs=profile.measured_runs,
                failed_runs=profile.failed_runs,
                failure_rate=profile.failure_rate,
            )
        )
    if anchor is None:
        raise BenchmarkPublicationError("no profiles were available to publish.")
    if total_raw > _MAX_BUNDLE_BYTES:
        raise BenchmarkPublicationError("raw profiles exceed the bundle size limit.")
    return BenchmarkPublicationBundle(
        config=config,
        config_digest=config.digest,
        calibration_module_digest=_sha256_bytes(calibration_bytes),
        publisher_script_digest=_sha256_bytes(script_bytes),
        hardware_digest=anchor[0],
        os_id=anchor[1],
        methodology_digest=anchor[2],
        profiles=tuple(sorted(entries, key=lambda item: (item.model_id, item.model_digest))),
    )


def write_bundle_atomic(path: Path | str, bundle: BenchmarkPublicationBundle) -> Path:
    """Write a canonical bundle via a same-directory temporary and replace."""
    if not isinstance(bundle, BenchmarkPublicationBundle):
        raise TypeError("bundle must be a BenchmarkPublicationBundle value.")
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise TypeError("output path must be a non-empty path.")
    target = Path(path)
    content = json.dumps(bundle.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_BUNDLE_BYTES:
        raise BenchmarkPublicationError("serialized benchmark bundle exceeds the size limit.")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def load_publication_config(path: Path | str) -> BenchmarkPublicationConfig:
    """Read the explicit JSON publication plan without exposing parse details."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise BenchmarkPublicationError(f"publication config is unavailable or invalid ({type(exc).__name__}).") from exc
    try:
        return BenchmarkPublicationConfig.from_mapping(raw)
    except (TypeError, ValueError, KeyError) as exc:
        raise BenchmarkPublicationError(f"publication config failed validation ({type(exc).__name__}).") from exc
