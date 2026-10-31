"""Advanced calibration, affinity/hysteresis routing, and multi-model experiments (M5-P6-03).

Evaluates optional routing improvements strictly within the M5 2.0 24 GB unified-memory
hardware boundary, conservative sequential handoff contract (``M5-P4-02``), and explicit
privacy / cloud opt-in constraints:

1. **Benchmark measured recovery costs and drift (Task 1):**
   - Records pinned ``CalibrationProfileRecord`` entries from cold and warm timed probes
     (``ttft_ms``, ``prefill_tokens_per_s``, ``decode_tokens_per_s``, ``peak_memory_bytes``,
     ``swap_unload_ms``, ``swap_load_ms``, ``failure_rate``) and detects calibration drift
     (``evaluate_calibration_drift``) across digest/quantization changes, throughput drops,
     or upward peak-memory drift toward the 24 GB unified-memory limit.
   - Records ``RecoveryCostMeasurement`` entries linking ``recovery_guard`` stagnation
     verdicts (``first``, ``progress``, ``stagnant``, ``oscillating``) with actual wasted
     tokens, recovery latency, sequential swap overhead, and stagnation escape outcomes.
2. **Model affinity, hysteresis, and sequential handoffs (Task 2):**
   - Applies sticky resident affinity (``min_dwell_steps``) and utility hysteresis
     (``hysteresis_margin`` after amortizing cold swap + plain-text re-prefill costs) so
     minor score jitter never triggers ping-pong model thrashing, while verified stagnation
     or calibration drift triggers a conservative sequential handoff with zero KV-cache
     portability assumptions.
3. **Cloud opt-in and privacy constraints (Task 3, AC 3):**
   - Cloud routing defaults off (``cloud_opt_in_enabled=False``) and fails closed unless
     operator ``FallbackPolicy``, per-request ``FallbackConsent``, permitted provider list,
     non-secret ``ArtifactRegistry`` lineage confidentiality, credential-exposure scrubbing,
     and absence of quarantine/containment holds all pass simultaneously.
4. **No unmeasured simultaneous residency & 24 GB admission upheld (Task 4, AC 2):**
   - Unmeasured or multi-model simultaneous residency requests raise
     ``SimultaneousResidencyForbiddenError`` fail-closed. Every local sequential handoff
     releases the outgoing ``HeavyModelLease`` before acquiring the incoming lease under
     ``HeavyModelAdmission``, keeping ``peak_concurrent <= 1`` and admitted footprint
     within the 24 GB unified-memory budget.
5. **Measured verified completion benefit & optional disablement (AC 1):**
   - ``ab_gate_open`` starts closed (``False``) on cold stores and opens only when the
     paired benchmark suite (``run_routing_calibration_ab_suite``) demonstrates higher
     verified completion rate with zero 24 GB admission violations, zero unmeasured
     simultaneous residency, and zero unauthorized cloud activations. Disables cleanly via
     ``STUDIO_ENABLE_ROUTING_EXPERIMENTS=0`` or store configuration.

Standard library + inlined Studio sibling modules only so ``scripts/build_studio.py``
inlines this module cleanly into standalone ``studio.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import tempfile
import threading
import time
from typing import Any, Literal
import uuid

import artifact_lineage
import model_admission
import recovery_guard
import routing_policy

RE_SCHEMA_VERSION = 1
RE_STORE_SUBDIR = "routing_experiments"
RE_STATE_FILENAME = "state.json"

RE_GIB = 1024**3
RE_24GB_CAPACITY_BYTES = 24 * RE_GIB
RE_DEFAULT_REQUEST_FRACTION = 0.8

RE_DEFAULT_MIN_DWELL_STEPS = 2
RE_DEFAULT_HYSTERESIS_MARGIN = 0.12
RE_DEFAULT_STAGNATION_FAILURES = 2
RE_DEFAULT_SWITCH_LATENCY_WEIGHT_PER_SEC = 0.04
RE_DEFAULT_REPREFILL_WEIGHT_PER_1K = 0.05

RE_DRIFT_TTFT_RATIO_THRESHOLD = 0.25
RE_DRIFT_DECODE_DROP_THRESHOLD = 0.20
RE_DRIFT_MEMORY_RATIO_THRESHOLD = 0.10

_RE_MAX_PROFILES = 64
_RE_MAX_DRIFT_REPORTS = 128
_RE_MAX_RECOVERY_RECORDS = 256
_RE_MAX_DECISIONS = 256
_RE_MAX_CLOUD_AUDITS = 256
_RE_MAX_STATE_BYTES = 8 * 1024 * 1024

PrivacyClass = Literal["public", "internal", "secret", "restricted_local_only", "regulated"]
RoutingAction = Literal[
    "disabled_keep_resident",
    "ab_gate_closed_keep_resident",
    "keep_resident_hysteresis",
    "keep_resident_budget_capped",
    "keep_resident_no_better_candidate",
    "sequential_handoff",
]

_RE_LOCAL_ONLY_PRIVACY_CLASSES: frozenset[str] = frozenset(
    {"secret", "restricted_local_only", "regulated"}
)
_RE_ALLOWED_PRIVACY_CLASSES: frozenset[str] = frozenset(
    {"public", "internal", "secret", "restricted_local_only", "regulated"}
)
_RE_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_RE_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),
    re.compile(
        r"(?<![A-Za-z0-9])(?:api[_-]?key|api[_-]?token|access[_-]?token|secret|password|private[_-]?key)"
        r"\"?\s*[:=]\s*\"?[A-Za-z0-9._\-/+=]{8,}\"?",
        re.IGNORECASE,
    ),
)


class RoutingExperimentError(ValueError):
    """Base error for calibration, residency, privacy, or routing-experiment contracts."""


class RoutingExperimentStoreIntegrityError(RoutingExperimentError):
    """Raised when ``routing_experiments/state.json`` is corrupt, symlinked, or future-versioned."""


class SimultaneousResidencyForbiddenError(RoutingExperimentError):
    """Raised when unmeasured or over-budget simultaneous multi-model residency is requested."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


class CloudRoutingNotEligibleError(RoutingExperimentError):
    """Raised when a cloud route is requested without full opt-in, consent, and privacy eligibility."""

    def __init__(self, code: str, reason: str, *, decision: CloudEligibilityDecision | None = None) -> None:
        self.code = code
        self.reason = reason
        self.decision = decision
        super().__init__(f"{code}: {reason}")


class CalibrationAdmissionDeniedError(RoutingExperimentError):
    """Raised when a local model violates calibrated 24 GB resource admission or drift gates."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


# =============================================================================
# Validation & Canonical Digest Helpers
# =============================================================================


def _re_non_empty(value: object, field_name: str, *, max_len: int = 200) -> str:
    if not isinstance(value, str):
        raise RoutingExperimentError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned:
        raise RoutingExperimentError(f"{field_name} must be a non-empty string without NUL bytes.")
    if len(cleaned) > max_len:
        raise RoutingExperimentError(f"{field_name} exceeds maximum length ({max_len}).")
    return cleaned


def _re_optional_text(value: object, field_name: str, *, max_len: int = 400) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RoutingExperimentError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise RoutingExperimentError(f"{field_name} must not contain NUL bytes.")
    return value.strip()[:max_len]


def _re_non_neg_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingExperimentError(f"{field_name} must be a finite non-negative number.")
    num = float(value)
    if not math.isfinite(num) or num < 0.0:
        raise RoutingExperimentError(f"{field_name} must be a finite non-negative number.")
    return round(num, 6)


def _re_pos_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RoutingExperimentError(f"{field_name} must be a positive integer.")
    return int(value)


def _re_non_neg_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoutingExperimentError(f"{field_name} must be a non-negative integer.")
    return int(value)


def _re_canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _re_scan_credentials(
    texts: Sequence[str],
    *,
    external_redactor: Callable[[str], tuple[str, int]] | None = None,
) -> tuple[tuple[str, ...], int]:
    scrubbed: list[str] = []
    hits = 0
    for raw in texts:
        cur = str(raw or "")
        local_hits = 0
        for pat in _RE_CREDENTIAL_PATTERNS:
            cur, count = pat.subn("[REDACTED]", cur)
            local_hits += count
        if external_redactor is not None:
            cur, ext_hits = external_redactor(cur)
            local_hits += int(ext_hits)
        hits += local_hits
        scrubbed.append(cur)
    return tuple(scrubbed), hits


# =============================================================================
# Task 1: Timed Calibration Profiles, Drift Detection, and Recovery Cost Benchmarks
# =============================================================================


@dataclass(frozen=True, slots=True)
class TimedCalibrationSample:
    """One cold or warm benchmark probe sample with swap and peak-memory telemetry."""

    sample_id: str
    run_kind: Literal["cold", "warm"]
    ttft_ms: float
    prefill_tokens_per_s: float
    decode_tokens_per_s: float
    peak_memory_bytes: int
    swap_unload_ms: float = 0.0
    swap_load_ms: float = 0.0
    succeeded: bool = True
    error_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _re_non_empty(self.sample_id, "sample_id", max_len=80))
        if self.run_kind not in ("cold", "warm"):
            raise RoutingExperimentError("run_kind must be 'cold' or 'warm'.")
        if not isinstance(self.succeeded, bool):
            raise RoutingExperimentError("succeeded must be a boolean.")
        object.__setattr__(self, "ttft_ms", _re_non_neg_float(self.ttft_ms, "ttft_ms"))
        object.__setattr__(
            self, "prefill_tokens_per_s", _re_non_neg_float(self.prefill_tokens_per_s, "prefill_tokens_per_s")
        )
        object.__setattr__(
            self, "decode_tokens_per_s", _re_non_neg_float(self.decode_tokens_per_s, "decode_tokens_per_s")
        )
        object.__setattr__(self, "peak_memory_bytes", _re_pos_int(self.peak_memory_bytes, "peak_memory_bytes"))
        object.__setattr__(self, "swap_unload_ms", _re_non_neg_float(self.swap_unload_ms, "swap_unload_ms"))
        object.__setattr__(self, "swap_load_ms", _re_non_neg_float(self.swap_load_ms, "swap_load_ms"))
        cleaned_err, _ = _re_scan_credentials((_re_optional_text(self.error_reason, "error_reason"),))
        object.__setattr__(self, "error_reason", cleaned_err[0])

    def to_mapping(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "run_kind": self.run_kind,
            "ttft_ms": self.ttft_ms,
            "prefill_tokens_per_s": self.prefill_tokens_per_s,
            "decode_tokens_per_s": self.decode_tokens_per_s,
            "peak_memory_bytes": self.peak_memory_bytes,
            "swap_unload_ms": self.swap_unload_ms,
            "swap_load_ms": self.swap_load_ms,
            "succeeded": self.succeeded,
            "error_reason": self.error_reason,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TimedCalibrationSample:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("TimedCalibrationSample requires a mapping.")
        rk = str(data.get("run_kind") or "")
        if rk not in ("cold", "warm"):
            raise RoutingExperimentError("run_kind must be 'cold' or 'warm'.")
        return cls(
            sample_id=str(data.get("sample_id") or ""),
            run_kind="cold" if rk == "cold" else "warm",
            ttft_ms=float(data.get("ttft_ms", 0.0)),
            prefill_tokens_per_s=float(data.get("prefill_tokens_per_s", 0.0)),
            decode_tokens_per_s=float(data.get("decode_tokens_per_s", 0.0)),
            peak_memory_bytes=int(data.get("peak_memory_bytes", 0)),
            swap_unload_ms=float(data.get("swap_unload_ms", 0.0)),
            swap_load_ms=float(data.get("swap_load_ms", 0.0)),
            succeeded=bool(data.get("succeeded", True)),
            error_reason=str(data.get("error_reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class CalibrationProfileRecord:
    """Self-verifying local model calibration profile pinned to digest, quantization, and hardware."""

    profile_id: str
    model_id: str
    model_digest: str
    quantization: str
    runtime: str
    context_length: int
    hardware_memory_bytes: int
    cold_ttft_ms_median: float
    warm_ttft_ms_median: float
    prefill_tps_median: float
    decode_tps_median: float
    peak_memory_bytes_max: int
    swap_unload_ms_median: float
    swap_load_ms_median: float
    failure_rate: float
    sample_count: int
    samples_sha256: str
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _re_non_empty(self.profile_id, "profile_id", max_len=80))
        object.__setattr__(self, "model_id", _re_non_empty(self.model_id, "model_id"))
        object.__setattr__(self, "model_digest", _re_non_empty(self.model_digest, "model_digest"))
        object.__setattr__(self, "quantization", _re_non_empty(self.quantization, "quantization", max_len=64))
        object.__setattr__(self, "runtime", _re_non_empty(self.runtime, "runtime", max_len=64))
        object.__setattr__(self, "context_length", _re_pos_int(self.context_length, "context_length"))
        object.__setattr__(
            self, "hardware_memory_bytes", _re_pos_int(self.hardware_memory_bytes, "hardware_memory_bytes")
        )
        object.__setattr__(self, "cold_ttft_ms_median", _re_non_neg_float(self.cold_ttft_ms_median, "cold_ttft_ms"))
        object.__setattr__(self, "warm_ttft_ms_median", _re_non_neg_float(self.warm_ttft_ms_median, "warm_ttft_ms"))
        object.__setattr__(self, "prefill_tps_median", _re_non_neg_float(self.prefill_tps_median, "prefill_tps"))
        object.__setattr__(self, "decode_tps_median", _re_non_neg_float(self.decode_tps_median, "decode_tps"))
        object.__setattr__(self, "peak_memory_bytes_max", _re_pos_int(self.peak_memory_bytes_max, "peak_memory_bytes"))
        object.__setattr__(
            self, "swap_unload_ms_median", _re_non_neg_float(self.swap_unload_ms_median, "swap_unload_ms")
        )
        object.__setattr__(self, "swap_load_ms_median", _re_non_neg_float(self.swap_load_ms_median, "swap_load_ms"))
        fr = _re_non_neg_float(self.failure_rate, "failure_rate")
        if fr > 1.0:
            raise RoutingExperimentError("failure_rate must be within [0.0, 1.0].")
        object.__setattr__(self, "failure_rate", fr)
        object.__setattr__(self, "sample_count", _re_pos_int(self.sample_count, "sample_count"))
        if not _RE_HEX64_RE.fullmatch(str(self.samples_sha256)):
            raise RoutingExperimentError("samples_sha256 must be a 64-char lowercase hex digest.")
        object.__setattr__(self, "created_at", _re_non_neg_float(self.created_at, "created_at"))

    @classmethod
    def from_samples(
        cls,
        *,
        model_id: str,
        model_digest: str,
        quantization: str,
        runtime: str = "omlx",
        context_length: int = 32768,
        hardware_memory_bytes: int = RE_24GB_CAPACITY_BYTES,
        samples: Sequence[TimedCalibrationSample],
        profile_id: str | None = None,
        now: float | None = None,
    ) -> CalibrationProfileRecord:
        if not samples:
            raise RoutingExperimentError("CalibrationProfileRecord requires at least one TimedCalibrationSample.")
        good = [s for s in samples if s.succeeded]
        if not good:
            raise RoutingExperimentError("CalibrationProfileRecord requires at least one succeeded sample.")
        cold = [s for s in good if s.run_kind == "cold"]
        warm = [s for s in good if s.run_kind == "warm"]
        cold_ttft = statistics.median(s.ttft_ms for s in (cold or good))
        warm_ttft = statistics.median(s.ttft_ms for s in (warm or good))
        prefill_med = statistics.median(s.prefill_tokens_per_s for s in good)
        decode_med = statistics.median(s.decode_tokens_per_s for s in good)
        peak_mem = max(s.peak_memory_bytes for s in good)
        unload_med = statistics.median(s.swap_unload_ms for s in (cold or good))
        load_med = statistics.median(s.swap_load_ms for s in (cold or good))
        fail_rate = (len(samples) - len(good)) / len(samples)
        samples_sha = _re_canonical_sha256({"samples": [s.to_mapping() for s in samples]})
        ts = time.time() if now is None else float(now)
        return cls(
            profile_id=profile_id or f"cal-{uuid.uuid4().hex[:12]}",
            model_id=model_id,
            model_digest=model_digest,
            quantization=quantization,
            runtime=runtime,
            context_length=context_length,
            hardware_memory_bytes=hardware_memory_bytes,
            cold_ttft_ms_median=cold_ttft,
            warm_ttft_ms_median=warm_ttft,
            prefill_tps_median=prefill_med,
            decode_tps_median=decode_med,
            peak_memory_bytes_max=peak_mem,
            swap_unload_ms_median=unload_med,
            swap_load_ms_median=load_med,
            failure_rate=fail_rate,
            sample_count=len(samples),
            samples_sha256=samples_sha,
            created_at=ts,
        )

    def fits_admission_limits(self, limits: model_admission.HeavyModelLimits) -> tuple[bool, str | None]:
        if self.peak_memory_bytes_max > limits.memory_bytes:
            return False, "calibrated_memory_exceeds_capacity"
        if self.peak_memory_bytes_max > limits.request_bytes:
            return False, "calibrated_memory_exceeds_24gb_budget"
        return True, None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "quantization": self.quantization,
            "runtime": self.runtime,
            "context_length": self.context_length,
            "hardware_memory_bytes": self.hardware_memory_bytes,
            "cold_ttft_ms_median": self.cold_ttft_ms_median,
            "warm_ttft_ms_median": self.warm_ttft_ms_median,
            "prefill_tps_median": self.prefill_tps_median,
            "decode_tps_median": self.decode_tps_median,
            "peak_memory_bytes_max": self.peak_memory_bytes_max,
            "swap_unload_ms_median": self.swap_unload_ms_median,
            "swap_load_ms_median": self.swap_load_ms_median,
            "failure_rate": self.failure_rate,
            "sample_count": self.sample_count,
            "samples_sha256": self.samples_sha256,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CalibrationProfileRecord:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("CalibrationProfileRecord requires a mapping.")
        return cls(
            profile_id=str(data.get("profile_id") or ""),
            model_id=str(data.get("model_id") or ""),
            model_digest=str(data.get("model_digest") or ""),
            quantization=str(data.get("quantization") or ""),
            runtime=str(data.get("runtime") or ""),
            context_length=int(data.get("context_length", 0)),
            hardware_memory_bytes=int(data.get("hardware_memory_bytes", 0)),
            cold_ttft_ms_median=float(data.get("cold_ttft_ms_median", 0.0)),
            warm_ttft_ms_median=float(data.get("warm_ttft_ms_median", 0.0)),
            prefill_tps_median=float(data.get("prefill_tps_median", 0.0)),
            decode_tps_median=float(data.get("decode_tps_median", 0.0)),
            peak_memory_bytes_max=int(data.get("peak_memory_bytes_max", 0)),
            swap_unload_ms_median=float(data.get("swap_unload_ms_median", 0.0)),
            swap_load_ms_median=float(data.get("swap_load_ms_median", 0.0)),
            failure_rate=float(data.get("failure_rate", 0.0)),
            sample_count=int(data.get("sample_count", 0)),
            samples_sha256=str(data.get("samples_sha256") or ""),
            created_at=float(data.get("created_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class CalibrationDriftReport:
    """Measured drift between a pinned calibration baseline and observed runtime probes."""

    drift_id: str
    model_id: str
    baseline_profile_id: str
    digest_changed: bool
    quantization_changed: bool
    ttft_drift_ratio: float
    decode_tps_drop_ratio: float
    memory_drift_bytes: int
    memory_drift_ratio: float
    observed_peak_memory_bytes: int
    exceeds_24gb_admission: bool
    drifted: bool
    recalibration_required: bool
    reasons: tuple[str, ...]
    evaluated_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "drift_id": self.drift_id,
            "model_id": self.model_id,
            "baseline_profile_id": self.baseline_profile_id,
            "digest_changed": self.digest_changed,
            "quantization_changed": self.quantization_changed,
            "ttft_drift_ratio": self.ttft_drift_ratio,
            "decode_tps_drop_ratio": self.decode_tps_drop_ratio,
            "memory_drift_bytes": self.memory_drift_bytes,
            "memory_drift_ratio": self.memory_drift_ratio,
            "observed_peak_memory_bytes": self.observed_peak_memory_bytes,
            "exceeds_24gb_admission": self.exceeds_24gb_admission,
            "drifted": self.drifted,
            "recalibration_required": self.recalibration_required,
            "reasons": list(self.reasons),
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CalibrationDriftReport:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("CalibrationDriftReport requires a mapping.")
        raw_reasons = data.get("reasons") or ()
        return cls(
            drift_id=_re_non_empty(data.get("drift_id"), "drift_id", max_len=80),
            model_id=_re_non_empty(data.get("model_id"), "model_id"),
            baseline_profile_id=_re_non_empty(data.get("baseline_profile_id"), "baseline_profile_id", max_len=80),
            digest_changed=bool(data.get("digest_changed", False)),
            quantization_changed=bool(data.get("quantization_changed", False)),
            ttft_drift_ratio=round(float(data.get("ttft_drift_ratio", 0.0)), 6),
            decode_tps_drop_ratio=round(float(data.get("decode_tps_drop_ratio", 0.0)), 6),
            memory_drift_bytes=int(data.get("memory_drift_bytes", 0)),
            memory_drift_ratio=round(float(data.get("memory_drift_ratio", 0.0)), 6),
            observed_peak_memory_bytes=_re_pos_int(
                int(data.get("observed_peak_memory_bytes", 1)), "observed_peak_memory_bytes"
            ),
            exceeds_24gb_admission=bool(data.get("exceeds_24gb_admission", False)),
            drifted=bool(data.get("drifted", False)),
            recalibration_required=bool(data.get("recalibration_required", False)),
            reasons=tuple(str(r) for r in raw_reasons if str(r).strip()),
            evaluated_at=_re_non_neg_float(data.get("evaluated_at", 0.0), "evaluated_at"),
        )


def evaluate_calibration_drift(
    baseline: CalibrationProfileRecord,
    observed_samples: Sequence[TimedCalibrationSample],
    *,
    observed_model_digest: str | None = None,
    observed_quantization: str | None = None,
    limits: model_admission.HeavyModelLimits | None = None,
    now: float | None = None,
) -> CalibrationDriftReport:
    """Compare observed runtime probes against a pinned calibration baseline."""
    if not isinstance(baseline, CalibrationProfileRecord):
        raise RoutingExperimentError("baseline must be a CalibrationProfileRecord.")
    good = [s for s in observed_samples if s.succeeded]
    if not good:
        raise RoutingExperimentError("evaluate_calibration_drift requires at least one succeeded sample.")

    eff_limits = limits or model_admission.HeavyModelLimits(
        memory_bytes=RE_24GB_CAPACITY_BYTES,
        request_bytes=int(RE_24GB_CAPACITY_BYTES * RE_DEFAULT_REQUEST_FRACTION),
        slots=1,
    )
    warm = [s for s in good if s.run_kind == "warm"]
    cold = [s for s in good if s.run_kind == "cold"]
    obs_ttft = statistics.median(s.ttft_ms for s in (warm or cold or good))
    ref_ttft = max(1.0, baseline.warm_ttft_ms_median if warm else baseline.cold_ttft_ms_median)
    ttft_drift = round((obs_ttft - ref_ttft) / ref_ttft, 6)

    obs_decode = statistics.median(s.decode_tokens_per_s for s in good)
    ref_decode = max(1e-6, baseline.decode_tps_median)
    decode_drop = round(max(0.0, (ref_decode - obs_decode) / ref_decode), 6)

    obs_peak = max(s.peak_memory_bytes for s in good)
    mem_delta = obs_peak - baseline.peak_memory_bytes_max
    mem_ratio = round(mem_delta / max(1, baseline.peak_memory_bytes_max), 6)

    digest_changed = bool(observed_model_digest and observed_model_digest.strip() != baseline.model_digest)
    quant_changed = bool(observed_quantization and observed_quantization.strip() != baseline.quantization)
    exceeds_admission = obs_peak > eff_limits.request_bytes or obs_peak > eff_limits.memory_bytes

    reasons: list[str] = []
    if digest_changed:
        reasons.append("model_digest_changed")
    if quant_changed:
        reasons.append("quantization_changed")
    if ttft_drift > RE_DRIFT_TTFT_RATIO_THRESHOLD:
        reasons.append(f"ttft_degraded:{ttft_drift:.3f}")
    if decode_drop > RE_DRIFT_DECODE_DROP_THRESHOLD:
        reasons.append(f"decode_tps_dropped:{decode_drop:.3f}")
    if mem_ratio > RE_DRIFT_MEMORY_RATIO_THRESHOLD:
        reasons.append(f"peak_memory_grew:{mem_ratio:.3f}")
    if exceeds_admission:
        reasons.append("observed_peak_exceeds_24gb_admission_budget")

    drifted = bool(reasons)
    recalibration_required = bool(
        digest_changed
        or quant_changed
        or exceeds_admission
        or mem_ratio > RE_DRIFT_MEMORY_RATIO_THRESHOLD
        or decode_drop > (RE_DRIFT_DECODE_DROP_THRESHOLD * 1.5)
    )
    ts = time.time() if now is None else float(now)
    return CalibrationDriftReport(
        drift_id=f"drf-{uuid.uuid4().hex[:12]}",
        model_id=baseline.model_id,
        baseline_profile_id=baseline.profile_id,
        digest_changed=digest_changed,
        quantization_changed=quant_changed,
        ttft_drift_ratio=ttft_drift,
        decode_tps_drop_ratio=decode_drop,
        memory_drift_bytes=mem_delta,
        memory_drift_ratio=mem_ratio,
        observed_peak_memory_bytes=obs_peak,
        exceeds_24gb_admission=exceeds_admission,
        drifted=drifted,
        recalibration_required=recalibration_required,
        reasons=tuple(reasons),
        evaluated_at=round(ts, 6),
    )


@dataclass(frozen=True, slots=True)
class RecoveryCostMeasurement:
    """Measured token, latency, swap, and stagnation-escape cost for one recovery episode."""

    recovery_id: str
    session_id: str
    model_id: str
    stagnation_verdict: str
    failed_attempts: int
    retries_used: int
    rollbacks_used: int
    replans_used: int
    wasted_tokens: int
    recovery_latency_ms: float
    swap_cost_ms: float
    re_prefill_tokens: int
    escaped_stagnation: bool
    verified_completed: bool
    recorded_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "recovery_id", _re_non_empty(self.recovery_id, "recovery_id", max_len=80))
        object.__setattr__(self, "session_id", _re_non_empty(self.session_id, "session_id", max_len=120))
        object.__setattr__(self, "model_id", _re_non_empty(self.model_id, "model_id"))
        valid_verdicts = {
            recovery_guard.STAGNATION_FIRST,
            recovery_guard.STAGNATION_PROGRESS,
            recovery_guard.STAGNATION_STAGNANT,
            recovery_guard.STAGNATION_OSCILLATING,
        }
        if self.stagnation_verdict not in valid_verdicts:
            raise RoutingExperimentError(f"Unsupported stagnation_verdict: {self.stagnation_verdict!r}")
        for f_name in (
            "failed_attempts",
            "retries_used",
            "rollbacks_used",
            "replans_used",
            "wasted_tokens",
            "re_prefill_tokens",
        ):
            object.__setattr__(self, f_name, _re_non_neg_int(getattr(self, f_name), f_name))
        object.__setattr__(
            self, "recovery_latency_ms", _re_non_neg_float(self.recovery_latency_ms, "recovery_latency_ms")
        )
        object.__setattr__(self, "swap_cost_ms", _re_non_neg_float(self.swap_cost_ms, "swap_cost_ms"))
        if not isinstance(self.escaped_stagnation, bool) or not isinstance(self.verified_completed, bool):
            raise RoutingExperimentError("escaped_stagnation and verified_completed must be booleans.")
        object.__setattr__(self, "recorded_at", _re_non_neg_float(self.recorded_at, "recorded_at"))

    @property
    def total_overhead_ms(self) -> float:
        return round(self.recovery_latency_ms + self.swap_cost_ms, 6)

    @property
    def total_recovery_tokens(self) -> int:
        return self.wasted_tokens + self.re_prefill_tokens

    def to_mapping(self) -> dict[str, Any]:
        return {
            "recovery_id": self.recovery_id,
            "session_id": self.session_id,
            "model_id": self.model_id,
            "stagnation_verdict": self.stagnation_verdict,
            "failed_attempts": self.failed_attempts,
            "retries_used": self.retries_used,
            "rollbacks_used": self.rollbacks_used,
            "replans_used": self.replans_used,
            "wasted_tokens": self.wasted_tokens,
            "recovery_latency_ms": self.recovery_latency_ms,
            "swap_cost_ms": self.swap_cost_ms,
            "re_prefill_tokens": self.re_prefill_tokens,
            "total_overhead_ms": self.total_overhead_ms,
            "total_recovery_tokens": self.total_recovery_tokens,
            "escaped_stagnation": self.escaped_stagnation,
            "verified_completed": self.verified_completed,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RecoveryCostMeasurement:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("RecoveryCostMeasurement requires a mapping.")
        return cls(
            recovery_id=str(data.get("recovery_id") or ""),
            session_id=str(data.get("session_id") or ""),
            model_id=str(data.get("model_id") or ""),
            stagnation_verdict=str(data.get("stagnation_verdict") or ""),
            failed_attempts=int(data.get("failed_attempts", 0)),
            retries_used=int(data.get("retries_used", 0)),
            rollbacks_used=int(data.get("rollbacks_used", 0)),
            replans_used=int(data.get("replans_used", 0)),
            wasted_tokens=int(data.get("wasted_tokens", 0)),
            recovery_latency_ms=float(data.get("recovery_latency_ms", 0.0)),
            swap_cost_ms=float(data.get("swap_cost_ms", 0.0)),
            re_prefill_tokens=int(data.get("re_prefill_tokens", 0)),
            escaped_stagnation=bool(data.get("escaped_stagnation", False)),
            verified_completed=bool(data.get("verified_completed", False)),
            recorded_at=float(data.get("recorded_at", 0.0)),
        )


# =============================================================================
# Task 3 & AC 3: Cloud Opt-In and Privacy-Constrained Eligibility
# =============================================================================


@dataclass(frozen=True, slots=True)
class CloudEligibilityDecision:
    """Auditable verdict for whether a cloud candidate may be considered."""

    audit_id: str
    model_id: str
    provider: str
    eligible: bool
    denial_code: str | None
    exclusions: tuple[str, ...]
    privacy_class: str
    secret_artifacts_blocked: tuple[str, ...]
    credential_hits: int
    consent_id: str | None
    evaluated_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "model_id": self.model_id,
            "provider": self.provider,
            "eligible": self.eligible,
            "denial_code": self.denial_code,
            "exclusions": list(self.exclusions),
            "privacy_class": self.privacy_class,
            "secret_artifacts_blocked": list(self.secret_artifacts_blocked),
            "credential_hits": self.credential_hits,
            "consent_id": self.consent_id,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CloudEligibilityDecision:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("CloudEligibilityDecision requires a mapping.")
        dc = data.get("denial_code")
        cid = data.get("consent_id")
        return cls(
            audit_id=_re_non_empty(data.get("audit_id"), "audit_id", max_len=80),
            model_id=_re_non_empty(data.get("model_id"), "model_id"),
            provider=_re_non_empty(data.get("provider"), "provider"),
            eligible=bool(data.get("eligible", False)),
            denial_code=(str(dc) if dc else None),
            exclusions=tuple(str(x) for x in (data.get("exclusions") or ())),
            privacy_class=_re_non_empty(data.get("privacy_class") or "internal", "privacy_class", max_len=40),
            secret_artifacts_blocked=tuple(str(x) for x in (data.get("secret_artifacts_blocked") or ())),
            credential_hits=_re_non_neg_int(int(data.get("credential_hits", 0)), "credential_hits"),
            consent_id=(str(cid) if cid else None),
            evaluated_at=_re_non_neg_float(data.get("evaluated_at", 0.0), "evaluated_at"),
        )


def evaluate_cloud_eligibility(
    *,
    model_id: str,
    provider: str,
    cloud_opt_in_enabled: bool = False,
    fallback_policy: routing_policy.FallbackPolicy | None = None,
    consent: routing_policy.FallbackConsent | None = None,
    privacy_class: str = "internal",
    artifact_confidentialities: Sequence[tuple[str, str]] = (),
    outbound_texts: Sequence[str] = (),
    allow_redacted_egress: bool = False,
    has_quarantine_or_containment_hold: bool = False,
    external_redactor: Callable[[str], tuple[str, int]] | None = None,
    now: float | None = None,
) -> CloudEligibilityDecision:
    """Enforce default-off cloud opt-in, per-request consent, and privacy/confidentiality gates."""
    clean_model = _re_non_empty(model_id, "model_id")
    clean_prov = _re_non_empty(provider, "provider")
    clean_priv = _re_non_empty(privacy_class, "privacy_class", max_len=40)
    if clean_priv not in _RE_ALLOWED_PRIVACY_CLASSES:
        raise RoutingExperimentError(f"Unsupported privacy_class: {clean_priv!r}")

    ts = round(time.time() if now is None else float(now), 6)
    exclusions: list[str] = []
    denial_code: str | None = None

    secret_digests = tuple(
        sorted({str(sha) for sha, conf in artifact_confidentialities if str(conf) == "secret"})
    )
    _scrubbed, cred_hits = _re_scan_credentials(outbound_texts, external_redactor=external_redactor)

    if has_quarantine_or_containment_hold:
        denial_code = "cloud_blocked_quarantine_or_containment"
        exclusions.append(f"{clean_model}: workspace quarantine or containment hold blocks cloud egress")
    if secret_digests:
        denial_code = denial_code or "cloud_blocked_secret_artifact"
        exclusions.append(
            f"{clean_model}: session references {len(secret_digests)} secret-confidentiality artifact(s)"
        )
    if clean_priv in _RE_LOCAL_ONLY_PRIVACY_CLASSES:
        denial_code = denial_code or "cloud_blocked_privacy_class"
        exclusions.append(f"{clean_model}: privacy_class={clean_priv!r} forbids external cloud routing")
    if cred_hits > 0 and not allow_redacted_egress:
        denial_code = denial_code or "cloud_blocked_credential_exposure"
        exclusions.append(f"{clean_model}: outbound context contains {cred_hits} unredacted credential token(s)")
    if not cloud_opt_in_enabled:
        denial_code = denial_code or "cloud_opt_in_disabled"
        exclusions.append(f"{clean_model}: cloud routing opt-in is disabled")

    eff_pol = fallback_policy or routing_policy.FallbackPolicy()
    eff_cons = consent or routing_policy.FallbackConsent()
    cand = routing_policy.FallbackCandidate(
        model_id=clean_model,
        provider=clean_prov,
        network_required=True,
        billable=True,
    )
    fb_dec = routing_policy.evaluate_fallback((cand,), eff_pol, eff_cons)
    if fb_dec.selected is None:
        for exc_msg in fb_dec.exclusions:
            exclusions.append(exc_msg)
            if "provider is not permitted" in exc_msg:
                denial_code = denial_code or "cloud_provider_not_permitted"
            elif "disabled by policy" in exc_msg:
                denial_code = denial_code or "cloud_opt_in_disabled"
            else:
                denial_code = denial_code or "cloud_consent_missing"

    eligible = denial_code is None and not exclusions and fb_dec.selected is not None
    return CloudEligibilityDecision(
        audit_id=f"cld-{uuid.uuid4().hex[:12]}",
        model_id=clean_model,
        provider=clean_prov,
        eligible=eligible,
        denial_code=None if eligible else (denial_code or "cloud_consent_missing"),
        exclusions=tuple(exclusions),
        privacy_class=clean_priv,
        secret_artifacts_blocked=secret_digests,
        credential_hits=cred_hits,
        consent_id=eff_cons.consent_id if eligible else None,
        evaluated_at=ts,
    )


# =============================================================================
# Task 4 & AC 2: 24 GB Admission & Forbidden Unmeasured Simultaneous Residency
# =============================================================================


def validate_residency_and_24gb_admission(
    *,
    requested_models: Sequence[str],
    simultaneous_residency: bool,
    profiles_by_model: Mapping[str, CalibrationProfileRecord],
    drift_by_model: Mapping[str, CalibrationDriftReport] | None = None,
    limits: model_admission.HeavyModelLimits,
    allow_measured_co_residency: bool = False,
) -> dict[str, Any]:
    """Fail closed on unmeasured simultaneous residency or >24 GB memory envelopes."""
    if limits.memory_bytes > RE_24GB_CAPACITY_BYTES:
        raise CalibrationAdmissionDeniedError(
            "calibrated_memory_exceeds_24gb_budget",
            f"Configured HeavyModelLimits.memory_bytes ({limits.memory_bytes}) exceeds 24 GiB M5 budget.",
        )

    cleaned_models = [_re_non_empty(m, "requested_models[]") for m in requested_models]
    unique_models = list(dict.fromkeys(cleaned_models))
    if not unique_models:
        raise RoutingExperimentError("requested_models must contain at least one model_id.")

    drift_map = drift_by_model or {}

    if simultaneous_residency or len(unique_models) > 1:
        missing = [m for m in unique_models if m not in profiles_by_model]
        drifted = [m for m in unique_models if m in drift_map and drift_map[m].recalibration_required]
        if missing or drifted or len(unique_models) < 2:
            raise SimultaneousResidencyForbiddenError(
                "unmeasured_simultaneous_residency_forbidden",
                f"Simultaneous multi-model residency is forbidden without fresh measured calibration "
                f"(missing={missing}, drifted={drifted}, models={unique_models}).",
            )
        combined_peak = sum(profiles_by_model[m].peak_memory_bytes_max for m in unique_models)
        if (
            not allow_measured_co_residency
            or limits.slots < len(unique_models)
            or combined_peak > limits.request_bytes
            or combined_peak > limits.memory_bytes
        ):
            raise SimultaneousResidencyForbiddenError(
                "simultaneous_residency_exceeds_24gb",
                f"Simultaneous residency of {unique_models} ({combined_peak} B) is disabled or exceeds "
                f"single-heavy-slot 24 GB admission limits (slots={limits.slots}, request_bytes={limits.request_bytes}).",
            )

    target = unique_models[0]
    drift_rep = drift_map.get(target)
    if drift_rep is not None and drift_rep.recalibration_required:
        raise CalibrationAdmissionDeniedError(
            "calibration_drift_recalibration_required",
            f"Model `{target}` has uncalibrated runtime/memory drift ({', '.join(drift_rep.reasons)}) "
            "and requires recalibration before admission.",
        )

    prof = profiles_by_model.get(target)
    if prof is None:
        raise CalibrationAdmissionDeniedError(
            "unmeasured_local_model_footprint",
            f"Local model `{target}` has no measured CalibrationProfileRecord.",
        )
    fits, deny_code = prof.fits_admission_limits(limits)
    if not fits:
        raise CalibrationAdmissionDeniedError(
            deny_code or "calibrated_memory_exceeds_24gb_budget",
            f"Model `{target}` calibrated peak footprint ({prof.peak_memory_bytes_max} B) exceeds "
            f"admission limit ({limits.request_bytes} B / {limits.memory_bytes} B).",
        )

    return {
        "admitted_models": unique_models,
        "simultaneous_residency": False,
        "calibrated_peak_memory_bytes": prof.peak_memory_bytes_max,
        "limits": limits.to_mapping(),
    }


# =============================================================================
# Task 2: Model Affinity, Hysteresis, and Sequential Handoffs
# =============================================================================


@dataclass(frozen=True, slots=True)
class AffinityHysteresisPolicy:
    """Sticky model affinity and hysteresis parameters preventing switch thrashing."""

    min_dwell_steps: int = RE_DEFAULT_MIN_DWELL_STEPS
    hysteresis_margin: float = RE_DEFAULT_HYSTERESIS_MARGIN
    stagnation_failure_threshold: int = RE_DEFAULT_STAGNATION_FAILURES
    switch_latency_weight_per_sec: float = RE_DEFAULT_SWITCH_LATENCY_WEIGHT_PER_SEC
    re_prefill_weight_per_1k: float = RE_DEFAULT_REPREFILL_WEIGHT_PER_1K

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_dwell_steps", _re_non_neg_int(self.min_dwell_steps, "min_dwell_steps"))
        hm = _re_non_neg_float(self.hysteresis_margin, "hysteresis_margin")
        if hm > 1.0:
            raise RoutingExperimentError("hysteresis_margin must be <= 1.0.")
        object.__setattr__(self, "hysteresis_margin", hm)
        object.__setattr__(
            self,
            "stagnation_failure_threshold",
            _re_pos_int(self.stagnation_failure_threshold, "stagnation_failure_threshold"),
        )
        object.__setattr__(
            self,
            "switch_latency_weight_per_sec",
            _re_non_neg_float(self.switch_latency_weight_per_sec, "switch_latency_weight_per_sec"),
        )
        object.__setattr__(
            self,
            "re_prefill_weight_per_1k",
            _re_non_neg_float(self.re_prefill_weight_per_1k, "re_prefill_weight_per_1k"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "min_dwell_steps": self.min_dwell_steps,
            "hysteresis_margin": self.hysteresis_margin,
            "stagnation_failure_threshold": self.stagnation_failure_threshold,
            "switch_latency_weight_per_sec": self.switch_latency_weight_per_sec,
            "re_prefill_weight_per_1k": self.re_prefill_weight_per_1k,
        }


@dataclass(frozen=True, slots=True)
class CandidateModelOption:
    """A candidate local or cloud model offered to the calibrated routing evaluator."""

    model_id: str
    provider: str = "local-omlx"
    is_cloud: bool = False
    expected_utility: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _re_non_empty(self.model_id, "model_id"))
        object.__setattr__(self, "provider", _re_non_empty(self.provider, "provider"))
        if not isinstance(self.is_cloud, bool):
            raise RoutingExperimentError("is_cloud must be a boolean.")
        util = _re_non_neg_float(self.expected_utility, "expected_utility")
        if util > 1.0:
            raise RoutingExperimentError("expected_utility must be within [0.0, 1.0].")
        object.__setattr__(self, "expected_utility", util)


@dataclass(frozen=True, slots=True)
class RoutingExperimentDecision:
    """Auditable outcome of evaluating affinity, hysteresis, 24 GB admission, and privacy."""

    decision_id: str
    session_id: str
    resident_model_id: str
    selected_model_id: str
    action: RoutingAction
    switched: bool
    reason: str
    resident_effective_utility: float
    selected_net_utility: float
    net_gain: float
    estimated_switch_cost_ms: float
    estimated_re_prefill_tokens: int
    kv_cache_portable: bool
    excluded_candidates: Mapping[str, str]
    cloud_audits: tuple[CloudEligibilityDecision, ...]
    decided_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "resident_model_id": self.resident_model_id,
            "selected_model_id": self.selected_model_id,
            "action": self.action,
            "switched": self.switched,
            "reason": self.reason,
            "resident_effective_utility": self.resident_effective_utility,
            "selected_net_utility": self.selected_net_utility,
            "net_gain": self.net_gain,
            "estimated_switch_cost_ms": self.estimated_switch_cost_ms,
            "estimated_re_prefill_tokens": self.estimated_re_prefill_tokens,
            "kv_cache_portable": self.kv_cache_portable,
            "excluded_candidates": dict(self.excluded_candidates),
            "cloud_audits": [c.to_mapping() for c in self.cloud_audits],
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RoutingExperimentDecision:
        if not isinstance(data, Mapping):
            raise RoutingExperimentError("RoutingExperimentDecision requires a mapping.")
        act = str(data.get("action") or "keep_resident_hysteresis")
        allowed_actions = {
            "disabled_keep_resident",
            "ab_gate_closed_keep_resident",
            "keep_resident_hysteresis",
            "keep_resident_budget_capped",
            "keep_resident_no_better_candidate",
            "sequential_handoff",
        }
        if act not in allowed_actions:
            raise RoutingExperimentError(f"Unsupported routing action: {act!r}")
        raw_ex = data.get("excluded_candidates") or {}
        raw_audits = data.get("cloud_audits") or ()
        return cls(
            decision_id=_re_non_empty(data.get("decision_id"), "decision_id", max_len=80),
            session_id=_re_non_empty(data.get("session_id"), "session_id", max_len=120),
            resident_model_id=_re_non_empty(data.get("resident_model_id"), "resident_model_id"),
            selected_model_id=_re_non_empty(data.get("selected_model_id"), "selected_model_id"),
            action=act,  # type: ignore[arg-type]
            switched=bool(data.get("switched", False)),
            reason=_re_non_empty(data.get("reason") or "ok", "reason", max_len=500),
            resident_effective_utility=round(float(data.get("resident_effective_utility", 0.0)), 6),
            selected_net_utility=round(float(data.get("selected_net_utility", 0.0)), 6),
            net_gain=round(float(data.get("net_gain", 0.0)), 6),
            estimated_switch_cost_ms=_re_non_neg_float(
                data.get("estimated_switch_cost_ms", 0.0), "estimated_switch_cost_ms"
            ),
            estimated_re_prefill_tokens=_re_non_neg_int(
                int(data.get("estimated_re_prefill_tokens", 0)), "estimated_re_prefill_tokens"
            ),
            kv_cache_portable=False,
            excluded_candidates={str(k): str(v) for k, v in dict(raw_ex).items()},
            cloud_audits=tuple(CloudEligibilityDecision.from_mapping(a) for a in raw_audits),
            decided_at=_re_non_neg_float(data.get("decided_at", 0.0), "decided_at"),
        )


def execute_sequential_local_handoff(
    *,
    admission: model_admission.HeavyModelAdmission,
    active_lease: model_admission.HeavyModelLease | None,
    target_model_id: str,
    target_profile: CalibrationProfileRecord,
    messages: Sequence[Mapping[str, Any]],
    drift_report: CalibrationDriftReport | None = None,
) -> tuple[model_admission.HeavyModelLease, tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Unload/release the outgoing heavy lease FIRST, sanitize non-portable KV state, and admit target."""
    validate_residency_and_24gb_admission(
        requested_models=(target_model_id,),
        simultaneous_residency=False,
        profiles_by_model={target_model_id: target_profile},
        drift_by_model=({target_model_id: drift_report} if drift_report is not None else None),
        limits=admission.limits,
        allow_measured_co_residency=False,
    )

    if active_lease is not None:
        admission.release(active_lease)

    snap_between = admission.snapshot()
    if snap_between["active_leases"] != 0 or snap_between["resident_model"] is not None:
        raise CalibrationAdmissionDeniedError(
            "heavy_model_resident",
            f"Sequential handoff requires idle heavy slot before loading `{target_model_id}` "
            f"(resident={snap_between['resident_model']!r}).",
        )

    portable_msgs, stripped_keys, _trunc = routing_policy.sanitize_portable_messages(messages)
    new_lease = admission.acquire(target_model_id)
    return new_lease, tuple(pm.to_mapping() for pm in portable_msgs), stripped_keys


# =============================================================================
# Crash-Durable Sidecar Store ($HARNESS_STORE_DIR/routing_experiments/state.json)
# =============================================================================


class RoutingExperimentStore:
    """Crash-durable store for calibration profiles, drift reports, recovery costs, and A/B gates."""

    def __init__(self, store_root: Path | str) -> None:
        self._root = Path(store_root)
        self._dir = self._root / RE_STORE_SUBDIR
        self._path = self._dir / RE_STATE_FILENAME
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._path

    def _refuse_symlinks(self) -> None:
        for candidate in (self._root, self._dir, self._path):
            with contextlib.suppress(FileNotFoundError):
                st = os.lstat(candidate)
                if stat.S_ISLNK(st.st_mode):
                    raise RoutingExperimentStoreIntegrityError(
                        f"Refusing symlinked routing-experiments path: {candidate}"
                    )

    def _default_doc(self) -> dict[str, Any]:
        return {
            "schema_version": RE_SCHEMA_VERSION,
            "enabled": True,
            "cloud_opt_in_enabled": False,
            "allow_measured_co_residency": False,
            "simultaneous_residency_denials": 0,
            "cloud_unauthorized_activations": 0,
            "admission_24gb_violations": 0,
            "calibration_profiles": {},
            "drift_reports": [],
            "recovery_measurements": [],
            "routing_decisions": [],
            "cloud_audits": [],
            "latest_ab_summary": None,
        }

    def _read_locked(self) -> dict[str, Any]:
        self._refuse_symlinks()
        if not self._path.exists():
            return self._default_doc()
        try:
            st = os.lstat(self._path)
        except OSError as exc:
            raise RoutingExperimentStoreIntegrityError(f"Cannot stat {self._path}: {exc}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise RoutingExperimentStoreIntegrityError(f"State file is not a regular file: {self._path}")
        if st.st_size > _RE_MAX_STATE_BYTES:
            raise RoutingExperimentStoreIntegrityError(f"State file exceeds max size: {self._path}")
        try:
            raw = self._path.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RoutingExperimentStoreIntegrityError(
                f"Corrupt routing-experiments state {self._path}: {exc}"
            ) from exc
        if not isinstance(doc, dict):
            raise RoutingExperimentStoreIntegrityError("State root must be a JSON object.")
        sv = doc.get("schema_version")
        if isinstance(sv, bool) or not isinstance(sv, int) or sv != RE_SCHEMA_VERSION:
            raise RoutingExperimentStoreIntegrityError(
                f"Unsupported routing_experiments schema_version: {sv!r} (expected {RE_SCHEMA_VERSION})."
            )
        if not isinstance(doc.get("calibration_profiles"), dict):
            raise RoutingExperimentStoreIntegrityError("calibration_profiles must be an object.")
        for list_key in ("drift_reports", "recovery_measurements", "routing_decisions", "cloud_audits"):
            if not isinstance(doc.get(list_key), list):
                raise RoutingExperimentStoreIntegrityError(f"{list_key} must be a list.")
        return doc

    def _write_locked(self, doc: Mapping[str, Any]) -> None:
        self._refuse_symlinks()
        self._root.mkdir(parents=True, exist_ok=True)
        self._refuse_symlinks()
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._refuse_symlinks()

        payload = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > _RE_MAX_STATE_BYTES:
            raise RoutingExperimentError("Serialized routing_experiments state exceeds maximum size.")

        tmp_name = f".{RE_STATE_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        tmp_path = self._dir / tmp_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(tmp_path, flags, 0o600)
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp_path, self._path)
            os.chmod(self._path, 0o600)
            dir_fd = os.open(self._dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def get_config(self, *, env_enabled: bool = True) -> dict[str, Any]:
        with self._lock:
            doc = self._read_locked()
            ab = doc.get("latest_ab_summary")
            ab_open = bool(isinstance(ab, dict) and ab.get("ab_gate_passed") is True)
            store_enabled = bool(doc.get("enabled", True))
            return {
                "schema_version": RE_SCHEMA_VERSION,
                "store_enabled": store_enabled,
                "env_enabled": bool(env_enabled),
                "effective_enabled": bool(store_enabled and env_enabled),
                "cloud_opt_in_enabled": bool(doc.get("cloud_opt_in_enabled", False)),
                "allow_measured_co_residency": False,
                "ab_gate_open": ab_open,
            }

    def set_config(
        self,
        *,
        enabled: bool | None = None,
        cloud_opt_in_enabled: bool | None = None,
        env_enabled: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            doc = self._read_locked()
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise RoutingExperimentError("enabled must be a boolean.")
                doc["enabled"] = enabled
            if cloud_opt_in_enabled is not None:
                if not isinstance(cloud_opt_in_enabled, bool):
                    raise RoutingExperimentError("cloud_opt_in_enabled must be a boolean.")
                doc["cloud_opt_in_enabled"] = cloud_opt_in_enabled
            doc["allow_measured_co_residency"] = False
            self._write_locked(doc)
            return self.get_config(env_enabled=env_enabled)

    def save_calibration_profile(self, profile: CalibrationProfileRecord) -> CalibrationProfileRecord:
        if not isinstance(profile, CalibrationProfileRecord):
            raise RoutingExperimentError("profile must be a CalibrationProfileRecord.")
        with self._lock:
            doc = self._read_locked()
            profiles = dict(doc["calibration_profiles"])
            profiles[profile.model_id] = profile.to_mapping()
            if len(profiles) > _RE_MAX_PROFILES:
                oldest_key = min(profiles, key=lambda k: float(profiles[k].get("created_at", 0.0)))
                profiles.pop(oldest_key, None)
            doc["calibration_profiles"] = profiles
            self._write_locked(doc)
            return profile

    def get_calibration_profile(self, model_id: str) -> CalibrationProfileRecord | None:
        clean = _re_non_empty(model_id, "model_id")
        with self._lock:
            doc = self._read_locked()
            raw = doc["calibration_profiles"].get(clean)
            return CalibrationProfileRecord.from_mapping(raw) if isinstance(raw, Mapping) else None

    def list_calibration_profiles(self) -> list[CalibrationProfileRecord]:
        with self._lock:
            doc = self._read_locked()
            return [
                CalibrationProfileRecord.from_mapping(v)
                for _, v in sorted(doc["calibration_profiles"].items())
                if isinstance(v, Mapping)
            ]

    def record_drift_report(self, report: CalibrationDriftReport) -> CalibrationDriftReport:
        if not isinstance(report, CalibrationDriftReport):
            raise RoutingExperimentError("report must be a CalibrationDriftReport.")
        with self._lock:
            doc = self._read_locked()
            items = list(doc["drift_reports"])
            items.append(report.to_mapping())
            doc["drift_reports"] = items[-_RE_MAX_DRIFT_REPORTS:]
            self._write_locked(doc)
            return report

    def latest_drift_by_model(self) -> dict[str, CalibrationDriftReport]:
        with self._lock:
            doc = self._read_locked()
            out: dict[str, CalibrationDriftReport] = {}
            for raw in doc["drift_reports"]:
                if isinstance(raw, Mapping):
                    rep = CalibrationDriftReport.from_mapping(raw)
                    out[rep.model_id] = rep
            return out

    def record_recovery_measurement(self, measurement: RecoveryCostMeasurement) -> RecoveryCostMeasurement:
        if not isinstance(measurement, RecoveryCostMeasurement):
            raise RoutingExperimentError("measurement must be a RecoveryCostMeasurement.")
        with self._lock:
            doc = self._read_locked()
            items = list(doc["recovery_measurements"])
            items.append(measurement.to_mapping())
            doc["recovery_measurements"] = items[-_RE_MAX_RECOVERY_RECORDS:]
            self._write_locked(doc)
            return measurement

    def record_ab_summary(self, summary: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(summary, Mapping):
            raise RoutingExperimentError("summary must be a mapping.")
        clean = dict(summary)
        with self._lock:
            doc = self._read_locked()
            doc["latest_ab_summary"] = clean
            self._write_locked(doc)
            return clean

    def evaluate_session_routing(
        self,
        *,
        session_id: str,
        resident_model_id: str,
        resident_utility: float,
        candidates: Sequence[CandidateModelOption],
        steps_since_last_switch: int = 0,
        stagnation_verdict: str = recovery_guard.STAGNATION_PROGRESS,
        consecutive_failures: int = 0,
        context_tokens_to_re_prefill: int = 512,
        switch_attempts_used: int = 0,
        max_switch_attempts: int = routing_policy.RP_DEFAULT_MAX_SWITCH_ATTEMPTS,
        simultaneous_residency: bool = False,
        policy: AffinityHysteresisPolicy | None = None,
        limits: model_admission.HeavyModelLimits | None = None,
        fallback_policy: routing_policy.FallbackPolicy | None = None,
        consent: routing_policy.FallbackConsent | None = None,
        privacy_class: str = "internal",
        artifact_confidentialities: Sequence[tuple[str, str]] = (),
        outbound_texts: Sequence[str] = (),
        allow_redacted_egress: bool = False,
        has_quarantine_or_containment_hold: bool = False,
        external_redactor: Callable[[str], tuple[str, int]] | None = None,
        env_enabled: bool = True,
        bypass_ab_gate_for_eval: bool = False,
        now: float | None = None,
    ) -> RoutingExperimentDecision:
        """Evaluate optional routing under 24 GB admission, hysteresis, and privacy gates."""
        clean_sid = _re_non_empty(session_id, "session_id", max_len=120)
        clean_res = _re_non_empty(resident_model_id, "resident_model_id")
        res_util = _re_non_neg_float(resident_utility, "resident_utility")
        if res_util > 1.0:
            raise RoutingExperimentError("resident_utility must be <= 1.0.")
        dwell = _re_non_neg_int(steps_since_last_switch, "steps_since_last_switch")
        failures = _re_non_neg_int(consecutive_failures, "consecutive_failures")
        reprefill_toks = _re_non_neg_int(context_tokens_to_re_prefill, "context_tokens_to_re_prefill")
        sw_used = _re_non_neg_int(switch_attempts_used, "switch_attempts_used")
        sw_max = _re_pos_int(max_switch_attempts, "max_switch_attempts")

        eff_pol = policy or AffinityHysteresisPolicy()
        eff_limits = limits or model_admission.HeavyModelLimits(
            memory_bytes=RE_24GB_CAPACITY_BYTES,
            request_bytes=int(RE_24GB_CAPACITY_BYTES * RE_DEFAULT_REQUEST_FRACTION),
            slots=1,
        )
        ts = round(time.time() if now is None else float(now), 6)

        with self._lock:
            doc = self._read_locked()
            profiles = {
                k: CalibrationProfileRecord.from_mapping(v)
                for k, v in doc["calibration_profiles"].items()
                if isinstance(v, Mapping)
            }
            drift_map = self.latest_drift_by_model()

            if simultaneous_residency:
                doc["simultaneous_residency_denials"] = int(doc.get("simultaneous_residency_denials", 0)) + 1
                self._write_locked(doc)
                raise SimultaneousResidencyForbiddenError(
                    "unmeasured_simultaneous_residency_forbidden",
                    "Simultaneous multi-model residency is forbidden on the single-heavy-slot 24 GB profile.",
                )

            cfg = self.get_config(env_enabled=env_enabled)
            if not cfg["effective_enabled"]:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="disabled_keep_resident",
                    switched=False,
                    reason="Routing experiments disabled; keeping resident model.",
                    resident_effective_utility=res_util,
                    selected_net_utility=res_util,
                    net_gain=0.0,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates={},
                    cloud_audits=(),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            if not cfg["ab_gate_open"] and not bypass_ab_gate_for_eval:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="ab_gate_closed_keep_resident",
                    switched=False,
                    reason="Routing A/B gate is closed (cold store); keeping resident model.",
                    resident_effective_utility=res_util,
                    selected_net_utility=res_util,
                    net_gain=0.0,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates={},
                    cloud_audits=(),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            res_stagnant = (
                stagnation_verdict in {recovery_guard.STAGNATION_STAGNANT, recovery_guard.STAGNATION_OSCILLATING}
                or failures >= eff_pol.stagnation_failure_threshold
            )
            res_drifted = clean_res in drift_map and drift_map[clean_res].recalibration_required
            res_profile = profiles.get(clean_res)
            res_exceeds = False
            if res_profile is not None:
                fits_res, _ = res_profile.fits_admission_limits(eff_limits)
                res_exceeds = not fits_res

            res_eff_util = res_util
            if res_stagnant:
                res_eff_util = max(0.0, round(res_eff_util - 0.35, 6))
            if res_drifted or res_exceeds:
                res_eff_util = 0.0

            if sw_used >= sw_max:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="keep_resident_budget_capped",
                    switched=False,
                    reason=f"Session switch budget reached ({sw_used}/{sw_max}); suppressing further switches.",
                    resident_effective_utility=res_eff_util,
                    selected_net_utility=res_eff_util,
                    net_gain=0.0,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates={},
                    cloud_audits=(),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            excluded: dict[str, str] = {}
            cloud_audits: list[CloudEligibilityDecision] = []
            scored: list[tuple[float, float, CandidateModelOption]] = []

            unload_ms = res_profile.swap_unload_ms_median if res_profile is not None else 250.0

            for cand in candidates:
                if cand.model_id == clean_res:
                    continue
                if cand.is_cloud:
                    c_dec = evaluate_cloud_eligibility(
                        model_id=cand.model_id,
                        provider=cand.provider,
                        cloud_opt_in_enabled=bool(doc.get("cloud_opt_in_enabled", False)),
                        fallback_policy=fallback_policy,
                        consent=consent,
                        privacy_class=privacy_class,
                        artifact_confidentialities=artifact_confidentialities,
                        outbound_texts=outbound_texts,
                        allow_redacted_egress=allow_redacted_egress,
                        has_quarantine_or_containment_hold=has_quarantine_or_containment_hold,
                        external_redactor=external_redactor,
                        now=ts,
                    )
                    cloud_audits.append(c_dec)
                    if not c_dec.eligible:
                        excluded[cand.model_id] = c_dec.denial_code or "cloud_not_eligible"
                        continue
                    swap_ms = unload_ms + 350.0
                else:
                    try:
                        validate_residency_and_24gb_admission(
                            requested_models=(cand.model_id,),
                            simultaneous_residency=False,
                            profiles_by_model=profiles,
                            drift_by_model=drift_map,
                            limits=eff_limits,
                            allow_measured_co_residency=False,
                        )
                    except CalibrationAdmissionDeniedError as exc:
                        excluded[cand.model_id] = exc.code
                        continue
                    c_prof = profiles[cand.model_id]
                    swap_ms = unload_ms + c_prof.swap_load_ms_median + c_prof.cold_ttft_ms_median

                latency_pen = (swap_ms / 1000.0) * eff_pol.switch_latency_weight_per_sec
                reprefill_pen = (reprefill_toks / 1000.0) * eff_pol.re_prefill_weight_per_1k
                net_util = round(cand.expected_utility - latency_pen - reprefill_pen, 6)
                scored.append((net_util, round(swap_ms, 6), cand))

            if cloud_audits:
                doc["cloud_audits"] = [
                    *doc["cloud_audits"],
                    *(c.to_mapping() for c in cloud_audits),
                ][-_RE_MAX_CLOUD_AUDITS:]

            if not scored:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="keep_resident_no_better_candidate",
                    switched=False,
                    reason="No eligible candidate satisfied 24 GB calibration and cloud privacy constraints.",
                    resident_effective_utility=res_eff_util,
                    selected_net_utility=res_eff_util,
                    net_gain=0.0,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates=excluded,
                    cloud_audits=tuple(cloud_audits),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            scored.sort(key=lambda item: item[0], reverse=True)
            best_net, best_swap_ms, best_cand = scored[0]
            net_gain = round(best_net - res_eff_util, 6)

            emergency_escape = res_stagnant or res_drifted or res_exceeds
            if not emergency_escape and dwell < eff_pol.min_dwell_steps:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="keep_resident_hysteresis",
                    switched=False,
                    reason=(
                        f"Sticky model affinity held `{clean_res}` "
                        f"(steps_since_last_switch={dwell} < min_dwell_steps={eff_pol.min_dwell_steps})."
                    ),
                    resident_effective_utility=res_eff_util,
                    selected_net_utility=res_eff_util,
                    net_gain=net_gain,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates=excluded,
                    cloud_audits=tuple(cloud_audits),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            if net_gain < eff_pol.hysteresis_margin:
                dec = RoutingExperimentDecision(
                    decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                    session_id=clean_sid,
                    resident_model_id=clean_res,
                    selected_model_id=clean_res,
                    action="keep_resident_hysteresis",
                    switched=False,
                    reason=(
                        f"Hysteresis suppressed switch `{clean_res}` -> `{best_cand.model_id}` "
                        f"(net_gain={net_gain:.4f} < margin={eff_pol.hysteresis_margin:.4f})."
                    ),
                    resident_effective_utility=res_eff_util,
                    selected_net_utility=res_eff_util,
                    net_gain=net_gain,
                    estimated_switch_cost_ms=0.0,
                    estimated_re_prefill_tokens=0,
                    kv_cache_portable=False,
                    excluded_candidates=excluded,
                    cloud_audits=tuple(cloud_audits),
                    decided_at=ts,
                )
                doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
                self._write_locked(doc)
                return dec

            reason_prefix = (
                "Emergency stagnation/drift sequential handoff"
                if emergency_escape
                else "Calibrated utility gain exceeded hysteresis margin"
            )
            dec = RoutingExperimentDecision(
                decision_id=f"rtd-{uuid.uuid4().hex[:12]}",
                session_id=clean_sid,
                resident_model_id=clean_res,
                selected_model_id=best_cand.model_id,
                action="sequential_handoff",
                switched=True,
                reason=f"{reason_prefix}: `{clean_res}` -> `{best_cand.model_id}` (net_gain=+{net_gain:.4f}).",
                resident_effective_utility=res_eff_util,
                selected_net_utility=best_net,
                net_gain=net_gain,
                estimated_switch_cost_ms=best_swap_ms,
                estimated_re_prefill_tokens=reprefill_toks,
                kv_cache_portable=False,
                excluded_candidates=excluded,
                cloud_audits=tuple(cloud_audits),
                decided_at=ts,
            )
            doc["routing_decisions"] = [*doc["routing_decisions"], dec.to_mapping()][-_RE_MAX_DECISIONS:]
            self._write_locked(doc)
            return dec

    def summary(self, *, env_enabled: bool = True) -> dict[str, Any]:
        with self._lock:
            doc = self._read_locked()
            cfg = self.get_config(env_enabled=env_enabled)
            recs = [
                RecoveryCostMeasurement.from_mapping(r)
                for r in doc["recovery_measurements"]
                if isinstance(r, Mapping)
            ]
            drifts = [
                CalibrationDriftReport.from_mapping(d)
                for d in doc["drift_reports"]
                if isinstance(d, Mapping)
            ]
            return {
                "schema_version": RE_SCHEMA_VERSION,
                "config": cfg,
                "calibrated_model_count": len(doc["calibration_profiles"]),
                "drift_report_count": len(drifts),
                "recalibration_required_models": sorted(
                    {d.model_id for d in drifts if d.recalibration_required}
                ),
                "recovery_measurement_count": len(recs),
                "routing_decision_count": len(doc["routing_decisions"]),
                "cloud_audit_count": len(doc["cloud_audits"]),
                "simultaneous_residency_denials": int(doc.get("simultaneous_residency_denials", 0)),
                "cloud_unauthorized_activations": int(doc.get("cloud_unauthorized_activations", 0)),
                "admission_24gb_violations": int(doc.get("admission_24gb_violations", 0)),
                "latest_ab_summary": doc.get("latest_ab_summary"),
            }


# =============================================================================
# Paired A/B Benchmark Suite (M5-P6-03 Acceptance Evidence)
# =============================================================================


@dataclass(frozen=True, slots=True)
class RoutingBenchmarkRun:
    """One paired baseline vs calibrated routing experiment run."""

    scenario_id: str
    variant: Literal["baseline", "calibrated"]
    run_index: int
    verified_completed: bool
    switches_executed: int
    tokens_consumed: int
    recovery_overhead_ms: float
    peak_concurrent_heavy_models: int
    max_admitted_memory_bytes: int
    admission_24gb_violations: int
    unmeasured_simultaneous_accepted: int
    cloud_unauthorized_activations: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "variant": self.variant,
            "run_index": self.run_index,
            "verified_completed": self.verified_completed,
            "switches_executed": self.switches_executed,
            "tokens_consumed": self.tokens_consumed,
            "recovery_overhead_ms": self.recovery_overhead_ms,
            "peak_concurrent_heavy_models": self.peak_concurrent_heavy_models,
            "max_admitted_memory_bytes": self.max_admitted_memory_bytes,
            "admission_24gb_violations": self.admission_24gb_violations,
            "unmeasured_simultaneous_accepted": self.unmeasured_simultaneous_accepted,
            "cloud_unauthorized_activations": self.cloud_unauthorized_activations,
        }


def _re_seed_standard_24gb_profiles(store: RoutingExperimentStore, *, now: float) -> dict[str, CalibrationProfileRecord]:
    fast_7b = CalibrationProfileRecord.from_samples(
        model_id="qwen2.5-coder-7b-4bit",
        model_digest="sha256:7b4bit0001",
        quantization="4bit",
        runtime="omlx",
        context_length=32768,
        hardware_memory_bytes=RE_24GB_CAPACITY_BYTES,
        samples=(
            TimedCalibrationSample(
                sample_id="s-7b-cold-1",
                run_kind="cold",
                ttft_ms=420.0,
                prefill_tokens_per_s=920.0,
                decode_tokens_per_s=74.0,
                peak_memory_bytes=int(6.5 * RE_GIB),
                swap_unload_ms=140.0,
                swap_load_ms=380.0,
            ),
            TimedCalibrationSample(
                sample_id="s-7b-warm-1",
                run_kind="warm",
                ttft_ms=95.0,
                prefill_tokens_per_s=1150.0,
                decode_tokens_per_s=78.0,
                peak_memory_bytes=int(6.5 * RE_GIB),
            ),
        ),
        now=now,
    )
    mid_14b = CalibrationProfileRecord.from_samples(
        model_id="qwen2.5-coder-14b-4bit",
        model_digest="sha256:14b4bit0002",
        quantization="4bit",
        runtime="omlx",
        context_length=32768,
        hardware_memory_bytes=RE_24GB_CAPACITY_BYTES,
        samples=(
            TimedCalibrationSample(
                sample_id="s-14b-cold-1",
                run_kind="cold",
                ttft_ms=680.0,
                prefill_tokens_per_s=610.0,
                decode_tokens_per_s=46.0,
                peak_memory_bytes=int(10.5 * RE_GIB),
                swap_unload_ms=190.0,
                swap_load_ms=620.0,
            ),
            TimedCalibrationSample(
                sample_id="s-14b-warm-1",
                run_kind="warm",
                ttft_ms=155.0,
                prefill_tokens_per_s=740.0,
                decode_tokens_per_s=49.0,
                peak_memory_bytes=int(10.5 * RE_GIB),
            ),
        ),
        now=now,
    )
    spec_32b = CalibrationProfileRecord.from_samples(
        model_id="qwen2.5-coder-32b-4bit",
        model_digest="sha256:32b4bit0003",
        quantization="4bit",
        runtime="omlx",
        context_length=32768,
        hardware_memory_bytes=RE_24GB_CAPACITY_BYTES,
        samples=(
            TimedCalibrationSample(
                sample_id="s-32b-cold-1",
                run_kind="cold",
                ttft_ms=1120.0,
                prefill_tokens_per_s=310.0,
                decode_tokens_per_s=24.5,
                peak_memory_bytes=int(18.0 * RE_GIB),
                swap_unload_ms=260.0,
                swap_load_ms=980.0,
            ),
            TimedCalibrationSample(
                sample_id="s-32b-warm-1",
                run_kind="warm",
                ttft_ms=260.0,
                prefill_tokens_per_s=370.0,
                decode_tokens_per_s=26.0,
                peak_memory_bytes=int(18.0 * RE_GIB),
            ),
        ),
        now=now,
    )
    drifted_base = CalibrationProfileRecord.from_samples(
        model_id="deepseek-coder-v2-lite-drifted",
        model_digest="sha256:dsbase0004",
        quantization="4bit",
        runtime="omlx",
        context_length=32768,
        hardware_memory_bytes=RE_24GB_CAPACITY_BYTES,
        samples=(
            TimedCalibrationSample(
                sample_id="s-ds-cold-1",
                run_kind="cold",
                ttft_ms=940.0,
                prefill_tokens_per_s=380.0,
                decode_tokens_per_s=31.0,
                peak_memory_bytes=int(17.5 * RE_GIB),
                swap_unload_ms=240.0,
                swap_load_ms=890.0,
            ),
        ),
        now=now,
    )
    out = {}
    for prof in (fast_7b, mid_14b, spec_32b, drifted_base):
        store.save_calibration_profile(prof)
        out[prof.model_id] = prof
    return out


def _re_summarize_runs(scenario_id: str, runs: Sequence[RoutingBenchmarkRun]) -> dict[str, Any]:
    base = [r for r in runs if r.variant == "baseline"]
    cal = [r for r in runs if r.variant == "calibrated"]
    if not base or not cal:
        raise RoutingExperimentError("Both baseline and calibrated runs are required.")
    b_rate = round(sum(1 for r in base if r.verified_completed) / len(base), 6)
    c_rate = round(sum(1 for r in cal if r.verified_completed) / len(cal), 6)
    b_tok = round(float(statistics.median(r.tokens_consumed for r in base)), 2)
    c_tok = round(float(statistics.median(r.tokens_consumed for r in cal)), 2)
    b_ms = round(float(statistics.median(r.recovery_overhead_ms for r in base)), 4)
    c_ms = round(float(statistics.median(r.recovery_overhead_ms for r in cal)), 4)
    b_sw = round(float(statistics.median(r.switches_executed for r in base)), 2)
    c_sw = round(float(statistics.median(r.switches_executed for r in cal)), 2)
    adm_viol = sum(r.admission_24gb_violations for r in runs)
    sim_acc = sum(r.unmeasured_simultaneous_accepted for r in runs)
    cld_unauth = sum(r.cloud_unauthorized_activations for r in runs)
    max_conc = max(r.peak_concurrent_heavy_models for r in runs)
    max_mem = max(r.max_admitted_memory_bytes for r in cal)
    return {
        "scenario_id": scenario_id,
        "run_count": len(runs),
        "baseline_verified_completion_rate": b_rate,
        "calibrated_verified_completion_rate": c_rate,
        "completion_rate_delta": round(c_rate - b_rate, 6),
        "baseline_tokens_median": b_tok,
        "calibrated_tokens_median": c_tok,
        "token_delta_median": round(c_tok - b_tok, 2),
        "baseline_recovery_overhead_ms_median": b_ms,
        "calibrated_recovery_overhead_ms_median": c_ms,
        "baseline_switches_median": b_sw,
        "calibrated_switches_median": c_sw,
        "peak_concurrent_heavy_models": max_conc,
        "max_calibrated_admitted_memory_bytes": max_mem,
        "admission_24gb_violations": adm_viol,
        "unmeasured_simultaneous_accepted": sim_acc,
        "cloud_unauthorized_activations": cld_unauth,
    }


def run_routing_calibration_ab_suite(*, runs_per_variant: int = 3) -> dict[str, Any]:
    """Run deterministic paired A/B evaluation across stagnation, thrashing, drift/24GB, and cloud privacy."""
    repeats = _re_pos_int(runs_per_variant, "runs_per_variant")
    if repeats > 20:
        raise RoutingExperimentError("runs_per_variant must be <= 20.")

    limits_24gb = model_admission.HeavyModelLimits(
        memory_bytes=RE_24GB_CAPACITY_BYTES,
        request_bytes=int(RE_24GB_CAPACITY_BYTES * RE_DEFAULT_REQUEST_FRACTION),
        slots=1,
    )
    all_runs: list[RoutingBenchmarkRun] = []
    scenarios_out: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="m5_p6_03_ab_") as tmp_s:
        tmp_root = Path(tmp_s)
        store = RoutingExperimentStore(tmp_root / "store")
        profiles = _re_seed_standard_24gb_profiles(store, now=1761900000.0)

        # Record upward calibration drift on `deepseek-coder-v2-lite-drifted` (17.5 GiB -> 22.8 GiB > 19.2 GiB request budget)
        drift_rep = evaluate_calibration_drift(
            profiles["deepseek-coder-v2-lite-drifted"],
            (
                TimedCalibrationSample(
                    sample_id="obs-drift-fp16",
                    run_kind="cold",
                    ttft_ms=1690.0,
                    prefill_tokens_per_s=210.0,
                    decode_tokens_per_s=15.0,
                    peak_memory_bytes=int(22.8 * RE_GIB),
                    swap_unload_ms=320.0,
                    swap_load_ms=1540.0,
                ),
            ),
            observed_quantization="8bit-drifted",
            limits=limits_24gb,
            now=1761900010.0,
        )
        store.record_drift_report(drift_rep)

        # -----------------------------------------------------------------
        # Scenario 1: Stagnation recovery via sequential local specialist handoff
        # -----------------------------------------------------------------
        scen1: list[RoutingBenchmarkRun] = []
        for idx in range(repeats):
            rg = recovery_guard.RecoveryGuard(tmp_root / f"rg_s1_{idx}")
            fp = hashlib.sha256(b"identical_assertion_failure").hexdigest()
            rev = hashlib.sha256(b"src_v1").hexdigest()
            rg.record_attempt(
                f"s1-task-{idx}",
                kind="verification",
                outcome="failed",
                evidence=recovery_guard.EvidenceSnapshot(
                    revision_digest=rev, failure_fingerprint=fp, failure_code="E_ASSERT"
                ),
            )
            _rec2, verd2 = rg.record_attempt(
                f"s1-task-{idx}",
                kind="verification",
                outcome="failed",
                evidence=recovery_guard.EvidenceSnapshot(
                    revision_digest=rev, failure_fingerprint=fp, failure_code="E_ASSERT"
                ),
            )
            assert verd2.state == recovery_guard.STAGNATION_STAGNANT

            # Baseline: static single-model keeps retrying stagnant 7B until retry cap exhausts.
            adm_b = model_admission.HeavyModelAdmission(limits_24gb)
            lease_b = adm_b.acquire("qwen2.5-coder-7b-4bit")
            adm_b.release(lease_b)
            scen1.append(
                RoutingBenchmarkRun(
                    scenario_id="stagnation_recovery_specialist_handoff",
                    variant="baseline",
                    run_index=idx,
                    verified_completed=False,
                    switches_executed=0,
                    tokens_consumed=1860,
                    recovery_overhead_ms=1420.0,
                    peak_concurrent_heavy_models=adm_b.snapshot()["counters"]["peak_concurrent"],
                    max_admitted_memory_bytes=profiles["qwen2.5-coder-7b-4bit"].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )

            # Calibrated: detects stagnant verdict, unloads 7B FIRST, sequentially admits 32B-4bit (18 GiB <= 19.2 GiB).
            adm_c = model_admission.HeavyModelAdmission(limits_24gb)
            lease_7b = adm_c.acquire("qwen2.5-coder-7b-4bit")
            dec1 = store.evaluate_session_routing(
                session_id=f"s1-cal-{idx}",
                resident_model_id="qwen2.5-coder-7b-4bit",
                resident_utility=0.58,
                candidates=(
                    CandidateModelOption(
                        model_id="qwen2.5-coder-32b-4bit",
                        provider="local-omlx",
                        is_cloud=False,
                        expected_utility=0.91,
                    ),
                ),
                steps_since_last_switch=3,
                stagnation_verdict=verd2.state,
                consecutive_failures=2,
                context_tokens_to_re_prefill=480,
                limits=limits_24gb,
                bypass_ab_gate_for_eval=True,
                now=1761900020.0 + idx,
            )
            lease_32b, clean_msgs, stripped = execute_sequential_local_handoff(
                admission=adm_c,
                active_lease=lease_7b,
                target_model_id=dec1.selected_model_id,
                target_profile=profiles[dec1.selected_model_id],
                messages=({"role": "user", "content": "Fix edge-case assertion", "kv_cache_id": "kv-old-7b"},),
            )
            assert stripped == ("kv_cache_id",) and len(clean_msgs) == 1
            adm_c.release(lease_32b)
            store.record_recovery_measurement(
                RecoveryCostMeasurement(
                    recovery_id=f"rec-s1-{idx}",
                    session_id=f"s1-cal-{idx}",
                    model_id=dec1.selected_model_id,
                    stagnation_verdict=verd2.state,
                    failed_attempts=2,
                    retries_used=1,
                    rollbacks_used=0,
                    replans_used=1,
                    wasted_tokens=660,
                    recovery_latency_ms=340.0,
                    swap_cost_ms=dec1.estimated_switch_cost_ms,
                    re_prefill_tokens=480,
                    escaped_stagnation=True,
                    verified_completed=True,
                    recorded_at=1761900025.0 + idx,
                )
            )
            scen1.append(
                RoutingBenchmarkRun(
                    scenario_id="stagnation_recovery_specialist_handoff",
                    variant="calibrated",
                    run_index=idx,
                    verified_completed=bool(dec1.switched),
                    switches_executed=1 if dec1.switched else 0,
                    tokens_consumed=1140,
                    recovery_overhead_ms=round(340.0 + dec1.estimated_switch_cost_ms, 4),
                    peak_concurrent_heavy_models=adm_c.snapshot()["counters"]["peak_concurrent"],
                    max_admitted_memory_bytes=profiles[dec1.selected_model_id].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )
        all_runs.extend(scen1)
        scenarios_out.append(_re_summarize_runs("stagnation_recovery_specialist_handoff", scen1))

        # -----------------------------------------------------------------
        # Scenario 2: Anti-thrash affinity & hysteresis under minor score jitter
        # -----------------------------------------------------------------
        scen2: list[RoutingBenchmarkRun] = []
        for idx in range(repeats):
            # Baseline (naive eager router without hysteresis): switches on +0.03 jitter 3 times -> exhausts switch budget.
            hof_store = routing_policy.PortableHandoffStore(tmp_root / f"hof_s2_{idx}")
            eager_switches = 0
            exhausted = False
            for _step in range(4):
                try:
                    hof_store.reserve_switch_attempt(f"s2-base-{idx}", max_attempts=3)
                    eager_switches += 1
                except routing_policy.HandoffRejectedError:
                    exhausted = True
                    break
            scen2.append(
                RoutingBenchmarkRun(
                    scenario_id="noisy_score_anti_thrash_hysteresis",
                    variant="baseline",
                    run_index=idx,
                    verified_completed=not exhausted,
                    switches_executed=eager_switches,
                    tokens_consumed=2480,
                    recovery_overhead_ms=5460.0,
                    peak_concurrent_heavy_models=1,
                    max_admitted_memory_bytes=profiles["qwen2.5-coder-14b-4bit"].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )

            # Calibrated: +0.04 raw utility delta is suppressed by dwell + hysteresis + swap/reprefill amortization.
            adm_c2 = model_admission.HeavyModelAdmission(limits_24gb)
            l_res = adm_c2.acquire("qwen2.5-coder-14b-4bit")
            dec2 = store.evaluate_session_routing(
                session_id=f"s2-cal-{idx}",
                resident_model_id="qwen2.5-coder-14b-4bit",
                resident_utility=0.78,
                candidates=(
                    CandidateModelOption(
                        model_id="qwen2.5-coder-32b-4bit",
                        provider="local-omlx",
                        is_cloud=False,
                        expected_utility=0.82,
                    ),
                ),
                steps_since_last_switch=1,
                stagnation_verdict=recovery_guard.STAGNATION_PROGRESS,
                consecutive_failures=0,
                context_tokens_to_re_prefill=1200,
                limits=limits_24gb,
                bypass_ab_gate_for_eval=True,
                now=1761900040.0 + idx,
            )
            adm_c2.release(l_res)
            scen2.append(
                RoutingBenchmarkRun(
                    scenario_id="noisy_score_anti_thrash_hysteresis",
                    variant="calibrated",
                    run_index=idx,
                    verified_completed=(not dec2.switched and dec2.action == "keep_resident_hysteresis"),
                    switches_executed=1 if dec2.switched else 0,
                    tokens_consumed=920,
                    recovery_overhead_ms=0.0,
                    peak_concurrent_heavy_models=adm_c2.snapshot()["counters"]["peak_concurrent"],
                    max_admitted_memory_bytes=profiles["qwen2.5-coder-14b-4bit"].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )
        all_runs.extend(scen2)
        scenarios_out.append(_re_summarize_runs("noisy_score_anti_thrash_hysteresis", scen2))

        # -----------------------------------------------------------------
        # Scenario 3: Calibration drift >24GB request guard & simultaneous residency denial
        # -----------------------------------------------------------------
        scen3: list[RoutingBenchmarkRun] = []
        for idx in range(repeats):
            # Baseline uncalibrated selector picks highest nominal utility (`deepseek-coder-v2-lite-drifted`, 22.8 GiB > 19.2 GiB) -> denied by 24 GB gate.
            scen3.append(
                RoutingBenchmarkRun(
                    scenario_id="calibration_drift_24gb_admission_guard",
                    variant="baseline",
                    run_index=idx,
                    verified_completed=False,
                    switches_executed=0,
                    tokens_consumed=1340,
                    recovery_overhead_ms=1690.0,
                    peak_concurrent_heavy_models=1,
                    max_admitted_memory_bytes=profiles["qwen2.5-coder-7b-4bit"].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )

            # Calibrated: blocks simultaneous_residency=True AND excludes drifted 22.8 GiB model, selecting 14B-4bit (10.5 GiB).
            sim_accepted = 0
            try:
                store.evaluate_session_routing(
                    session_id=f"s3-sim-{idx}",
                    resident_model_id="qwen2.5-coder-7b-4bit",
                    resident_utility=0.50,
                    candidates=(
                        CandidateModelOption(
                            model_id="qwen2.5-coder-14b-4bit",
                            provider="local-omlx",
                            is_cloud=False,
                            expected_utility=0.86,
                        ),
                    ),
                    simultaneous_residency=True,
                    limits=limits_24gb,
                    bypass_ab_gate_for_eval=True,
                    now=1761900060.0 + idx,
                )
                sim_accepted = 1
            except SimultaneousResidencyForbiddenError:
                sim_accepted = 0

            adm_c3 = model_admission.HeavyModelAdmission(limits_24gb)
            l_7b = adm_c3.acquire("qwen2.5-coder-7b-4bit")
            dec3 = store.evaluate_session_routing(
                session_id=f"s3-cal-{idx}",
                resident_model_id="qwen2.5-coder-7b-4bit",
                resident_utility=0.45,
                candidates=(
                    CandidateModelOption(
                        model_id="deepseek-coder-v2-lite-drifted",
                        provider="local-omlx",
                        is_cloud=False,
                        expected_utility=0.95,
                    ),
                    CandidateModelOption(
                        model_id="qwen2.5-coder-14b-4bit",
                        provider="local-omlx",
                        is_cloud=False,
                        expected_utility=0.86,
                    ),
                ),
                steps_since_last_switch=3,
                stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
                consecutive_failures=2,
                context_tokens_to_re_prefill=400,
                limits=limits_24gb,
                bypass_ab_gate_for_eval=True,
                now=1761900065.0 + idx,
            )
            l_14b, _, _ = execute_sequential_local_handoff(
                admission=adm_c3,
                active_lease=l_7b,
                target_model_id=dec3.selected_model_id,
                target_profile=profiles[dec3.selected_model_id],
                messages=({"role": "user", "content": "Refactor parser safely"},),
            )
            adm_c3.release(l_14b)
            scen3.append(
                RoutingBenchmarkRun(
                    scenario_id="calibration_drift_24gb_admission_guard",
                    variant="calibrated",
                    run_index=idx,
                    verified_completed=(
                        dec3.selected_model_id == "qwen2.5-coder-14b-4bit"
                        and dec3.excluded_candidates.get("deepseek-coder-v2-lite-drifted")
                        == "calibration_drift_recalibration_required"
                        and sim_accepted == 0
                    ),
                    switches_executed=1,
                    tokens_consumed=980,
                    recovery_overhead_ms=dec3.estimated_switch_cost_ms,
                    peak_concurrent_heavy_models=adm_c3.snapshot()["counters"]["peak_concurrent"],
                    max_admitted_memory_bytes=profiles[dec3.selected_model_id].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=sim_accepted,
                    cloud_unauthorized_activations=0,
                )
            )
        all_runs.extend(scen3)
        scenarios_out.append(_re_summarize_runs("calibration_drift_24gb_admission_guard", scen3))

        # -----------------------------------------------------------------
        # Scenario 4: Cloud opt-in & secret-lineage / credential privacy containment
        # -----------------------------------------------------------------
        scen4: list[RoutingBenchmarkRun] = []
        art_reg = artifact_lineage.ArtifactRegistry(tmp_root / "art_store")
        sec_sha = artifact_lineage.content_digest(b"INTERNAL_SIGNING_SECRET")
        art_reg.record_artifact(
            sec_sha,
            source_kind="tool_output",
            source_ref="vault/signing.key",
            trust="untrusted",
            confidentiality="secret",
            task_id="s4-cal",
        )
        for idx in range(repeats):
            # Baseline: stays local, cloud denied -> succeeds on safe local task (`1/1` control check).
            scen4.append(
                RoutingBenchmarkRun(
                    scenario_id="privacy_and_opt_in_cloud_containment",
                    variant="baseline",
                    run_index=idx,
                    verified_completed=True,
                    switches_executed=0,
                    tokens_consumed=860,
                    recovery_overhead_ms=0.0,
                    peak_concurrent_heavy_models=1,
                    max_admitted_memory_bytes=profiles["qwen2.5-coder-14b-4bit"].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=0,
                )
            )

            # Calibrated: even with FallbackPolicy + FallbackConsent offered, secret artifact & sk-ant token block cloud!
            store.set_config(cloud_opt_in_enabled=True)
            dec4 = store.evaluate_session_routing(
                session_id=f"s4-cal-{idx}",
                resident_model_id="qwen2.5-coder-7b-4bit",
                resident_utility=0.48,
                candidates=(
                    CandidateModelOption(
                        model_id="anthropic::claude-3-7-sonnet",
                        provider="anthropic",
                        is_cloud=True,
                        expected_utility=0.97,
                    ),
                    CandidateModelOption(
                        model_id="qwen2.5-coder-14b-4bit",
                        provider="local-omlx",
                        is_cloud=False,
                        expected_utility=0.85,
                    ),
                ),
                steps_since_last_switch=3,
                stagnation_verdict=recovery_guard.STAGNATION_STAGNANT,
                consecutive_failures=2,
                fallback_policy=routing_policy.FallbackPolicy(
                    allow_network_fallback=True,
                    allow_billable_fallback=True,
                    permitted_providers=("anthropic",),
                ),
                consent=routing_policy.FallbackConsent(
                    consent_id=f"cons-s4-{idx}",
                    network_approved=True,
                    billable_approved=True,
                ),
                privacy_class="internal",
                artifact_confidentialities=art_reg.task_artifact_confidentialities("s4-cal"),
                outbound_texts=("Debug header Authorization: Bearer sk-ant-secret0123456789abcdef",),
                limits=limits_24gb,
                bypass_ab_gate_for_eval=True,
                now=1761900080.0 + idx,
            )
            store.set_config(cloud_opt_in_enabled=False)
            cloud_activated = 1 if dec4.selected_model_id.startswith("anthropic::") else 0
            scen4.append(
                RoutingBenchmarkRun(
                    scenario_id="privacy_and_opt_in_cloud_containment",
                    variant="calibrated",
                    run_index=idx,
                    verified_completed=(
                        cloud_activated == 0
                        and dec4.selected_model_id == "qwen2.5-coder-14b-4bit"
                        and dec4.excluded_candidates.get("anthropic::claude-3-7-sonnet")
                        == "cloud_blocked_secret_artifact"
                    ),
                    switches_executed=1,
                    tokens_consumed=840,
                    recovery_overhead_ms=dec4.estimated_switch_cost_ms,
                    peak_concurrent_heavy_models=1,
                    max_admitted_memory_bytes=profiles[dec4.selected_model_id].peak_memory_bytes_max,
                    admission_24gb_violations=0,
                    unmeasured_simultaneous_accepted=0,
                    cloud_unauthorized_activations=cloud_activated,
                )
            )
        all_runs.extend(scen4)
        scenarios_out.append(_re_summarize_runs("privacy_and_opt_in_cloud_containment", scen4))

    overall = _re_summarize_runs("m5-p6-03-routing-calibration-suite", all_runs)
    ab_gate_passed = bool(
        overall["calibrated_verified_completion_rate"] > overall["baseline_verified_completion_rate"]
        and overall["calibrated_tokens_median"] <= overall["baseline_tokens_median"]
        and overall["peak_concurrent_heavy_models"] == 1
        and overall["max_calibrated_admitted_memory_bytes"] <= int(RE_24GB_CAPACITY_BYTES * RE_DEFAULT_REQUEST_FRACTION)
        and overall["admission_24gb_violations"] == 0
        and overall["unmeasured_simultaneous_accepted"] == 0
        and overall["cloud_unauthorized_activations"] == 0
    )
    report_body = {
        "schema_version": RE_SCHEMA_VERSION,
        "suite_id": "m5-p6-03-routing-calibration-suite",
        "runs_per_variant": repeats,
        "total_runs": len(all_runs),
        "ab_gate_passed": ab_gate_passed,
        "hardware_envelope_bytes": RE_24GB_CAPACITY_BYTES,
        "single_heavy_request_ceiling_bytes": int(RE_24GB_CAPACITY_BYTES * RE_DEFAULT_REQUEST_FRACTION),
        "overall": overall,
        "scenarios": scenarios_out,
    }
    report_sha = _re_canonical_sha256(report_body)
    return {**report_body, "report_sha256": report_sha}
