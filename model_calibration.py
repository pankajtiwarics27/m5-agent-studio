"""Local model calibration benchmarks: timed samples, warm/cold aggregates,
and a reproducible sample store.

AS-059 measures the benchmarking-safeguard metrics separately — time to
first token, prompt/prefill throughput, decode throughput, and peak memory —
from raw timed runs produced by a caller-supplied probe, always pinned to the
full calibration-input identity (model digest/revision, quantization, runtime
name/version, hardware snapshot, prompt-set digest, context length,
temperature, concurrency). Cold and warm runs aggregate separately; repeats
report median and spread; failed attempts count toward a failure rate but
never pollute timing metrics. Profiles persist as JSON-line records that
self-verify: reloading re-derives every aggregate from the stored samples and
refuses silent drift.

The module is I/O-thin by design: it never starts a model, loads a runtime,
or touches a provider. Only ``save_profile``/``load_profile`` and
``run_calibration`` (through the injected probe) interact with the outside
world, and every path is caller-scoped to an explicit local path. No prompt
content is ever stored — only its digest — matching the repository's
hash-only audit convention. Real-runtime timing capture is the probe's job
(the app wires a probe over the AS-019 lifecycle adapter and provider
adapters on target hardware).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from model_registry import ModelDescriptor

SCHEMA_VERSION = 1
RunKind = Literal["cold", "warm", "warmup"]
METRIC_NAMES = ("ttft_ms", "prefill_tokens_per_s", "decode_tokens_per_s", "peak_memory_bytes")
PROFILE_KINDS = ("warm", "cold")
_MAX_ERROR_LENGTH = 500
_ROUND_DIGITS = 6


class ModelCalibrationError(ValueError):
    """Raised when calibration data or aggregation inputs are invalid."""


class CalibrationStoreError(RuntimeError):
    """Raised when a stored calibration record is corrupt, foreign, or tampered with."""


def _round(value: float) -> float:
    return round(value, _ROUND_DIGITS)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(value: object, field: str, *, optional: bool = False, max_length: int = 128) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string.")
    result = value.strip()
    if not result:
        raise ModelCalibrationError(f"{field} must not be empty.")
    if len(result) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise ModelCalibrationError(f"{field} must be bounded text without control characters.")
    return result


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelCalibrationError(f"{field} must be a non-negative integer.")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelCalibrationError(f"{field} must be a positive integer.")
    return value


def _finite_ms(value: object, field: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelCalibrationError(f"{field} must be a finite number of milliseconds.")
    number = float(value)
    if math.isnan(number) or math.isinf(number) or number < 0:
        raise ModelCalibrationError(f"{field} must be a finite non-negative number of milliseconds.")
    return number


def _safe_reason(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None.")
    softened = "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value)
    cleaned = re.sub(r"\s+", " ", softened).strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_ERROR_LENGTH:
        cleaned = cleaned[: _MAX_ERROR_LENGTH - 1] + "…"
    return cleaned


@dataclass(frozen=True, slots=True)
class CalibrationInputs:
    """The pinned measurement context a profile may be compared within."""

    model_id: str
    model_digest: str
    runtime: str
    context_length: int
    temperature: float = 0.0
    concurrency: int = 1
    model_revision: str | None = None
    quantization: str | None = None
    runtime_version: str | None = None
    prompt_set_digest: str | None = None
    hardware: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_digest", _text(self.model_digest, "model_digest"))
        object.__setattr__(self, "runtime", _text(self.runtime, "runtime"))
        object.__setattr__(self, "context_length", _positive_int(self.context_length, "context_length"))
        if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
            raise TypeError("temperature must be a number.")
        temperature = float(self.temperature)
        if math.isnan(temperature) or math.isinf(temperature) or not 0.0 <= temperature <= 2.0:
            raise ModelCalibrationError("temperature must be within [0, 2].")
        object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "concurrency", _positive_int(self.concurrency, "concurrency"))
        for field in ("model_revision", "quantization", "runtime_version", "prompt_set_digest"):
            object.__setattr__(self, field, _text(getattr(self, field), field, optional=True))
        if self.hardware is not None and not isinstance(self.hardware, Mapping):
            raise TypeError("hardware must be a mapping snapshot or None.")

    @classmethod
    def for_model(
        cls,
        descriptor: ModelDescriptor,
        *,
        context_length: int | None = None,
        temperature: float = 0.0,
        concurrency: int = 1,
        runtime_version: str | None = None,
        prompt_set_digest: str | None = None,
        hardware: Mapping[str, Any] | None = None,
    ) -> CalibrationInputs:
        """Pin identity fields directly from the AS-017 registry descriptor."""
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor value.")
        return cls(
            model_id=descriptor.model_id,
            model_digest=descriptor.digest,
            model_revision=descriptor.revision,
            quantization=descriptor.quantization,
            runtime=descriptor.runtime,
            runtime_version=runtime_version,
            context_length=context_length if context_length is not None else descriptor.limits.context_tokens,
            temperature=temperature,
            concurrency=concurrency,
            prompt_set_digest=prompt_set_digest,
            hardware=dict(hardware) if hardware is not None else None,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "model_revision": self.model_revision,
            "quantization": self.quantization,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "context_length": self.context_length,
            "temperature": self.temperature,
            "concurrency": self.concurrency,
            "prompt_set_digest": self.prompt_set_digest,
            "hardware": dict(self.hardware) if self.hardware is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CalibrationInputs:
        if not isinstance(value, Mapping):
            raise TypeError("calibration inputs must be an object.")
        return cls(
            model_id=str(value["model_id"]),
            model_digest=str(value["model_digest"]),
            runtime=str(value["runtime"]),
            context_length=value["context_length"],
            temperature=value.get("temperature", 0.0),
            concurrency=value.get("concurrency", 1),
            model_revision=value.get("model_revision"),
            quantization=value.get("quantization"),
            runtime_version=value.get("runtime_version"),
            prompt_set_digest=value.get("prompt_set_digest"),
            hardware=value.get("hardware"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class RawRun:
    """One timed probe attempt in milliseconds; the probe owns collection."""

    sequence: int
    kind: RunKind
    started_at_ms: float
    finished_at_ms: float
    prompt_tokens: int = 0
    output_tokens: int = 0
    first_token_at_ms: float | None = None
    peak_memory_bytes: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _non_negative_int(self.sequence, "sequence"))
        if self.kind not in ("cold", "warm", "warmup"):
            raise ModelCalibrationError("kind must be one of: cold, warm, warmup.")
        started = _finite_ms(self.started_at_ms, "started_at_ms")
        finished = _finite_ms(self.finished_at_ms, "finished_at_ms")
        first = _finite_ms(self.first_token_at_ms, "first_token_at_ms", optional=True)
        assert started is not None and finished is not None
        if first is not None and not started <= first <= finished:
            raise ModelCalibrationError("first_token_at_ms must lie between start and finish.")
        if finished < started:
            raise ModelCalibrationError("finished_at_ms must not precede started_at_ms.")
        object.__setattr__(self, "started_at_ms", started)
        object.__setattr__(self, "finished_at_ms", finished)
        object.__setattr__(self, "first_token_at_ms", first)
        object.__setattr__(self, "prompt_tokens", _non_negative_int(self.prompt_tokens, "prompt_tokens"))
        object.__setattr__(self, "output_tokens", _non_negative_int(self.output_tokens, "output_tokens"))
        if self.peak_memory_bytes is not None:
            object.__setattr__(self, "peak_memory_bytes", _non_negative_int(self.peak_memory_bytes, "peak_memory_bytes"))
        object.__setattr__(self, "error", _safe_reason(self.error, "error"))


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """A measured run with derived metrics; failed runs carry ``ok=False``."""

    sequence: int
    kind: Literal["cold", "warm"]
    ok: bool
    ttft_ms: float | None = None
    prefill_tokens_per_s: float | None = None
    decode_tokens_per_s: float | None = None
    peak_memory_bytes: int | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": self.sequence,
            "kind": self.kind,
            "ok": self.ok,
            "ttft_ms": self.ttft_ms,
            "prefill_tokens_per_s": self.prefill_tokens_per_s,
            "decode_tokens_per_s": self.decode_tokens_per_s,
            "peak_memory_bytes": self.peak_memory_bytes,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CalibrationSample:
        if not isinstance(value, Mapping):
            raise TypeError("sample records must be objects.")
        return cls(
            sequence=value["sequence"],
            kind=value["kind"],
            ok=bool(value["ok"]),
            ttft_ms=value.get("ttft_ms"),
            prefill_tokens_per_s=value.get("prefill_tokens_per_s"),
            decode_tokens_per_s=value.get("decode_tokens_per_s"),
            peak_memory_bytes=value.get("peak_memory_bytes"),
            prompt_tokens=value.get("prompt_tokens", 0),
            output_tokens=value.get("output_tokens", 0),
            error=value.get("error"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def derive_sample(run: RawRun) -> CalibrationSample:
    """Derive TTFT and throughput metrics from one timed run.

    Degenerate intervals (first token equal to start or finish) leave the
    affected rate ``None`` instead of inventing an infinite throughput;
    semantic inconsistencies (output tokens with no first-token timestamp)
    mark the sample failed with a reason rather than raising — one bad
    measurement must not abort a whole calibration pass.
    """
    if not isinstance(run, RawRun):
        raise TypeError("run must be a RawRun value.")
    if run.kind == "warmup":
        raise ModelCalibrationError("warmup runs are discarded and never sampled.")
    if run.error is not None:
        return CalibrationSample(
            sequence=run.sequence, kind=run.kind, ok=False,
            peak_memory_bytes=run.peak_memory_bytes,
            prompt_tokens=run.prompt_tokens, output_tokens=run.output_tokens,
            error=run.error,
        )
    if run.output_tokens > 0 and run.first_token_at_ms is None:
        return CalibrationSample(
            sequence=run.sequence, kind=run.kind, ok=False,
            peak_memory_bytes=run.peak_memory_bytes,
            prompt_tokens=run.prompt_tokens, output_tokens=run.output_tokens,
            error="inconsistent timing: output tokens without a first-token timestamp",
        )
    ttft: float | None = None
    prefill: float | None = None
    decode: float | None = None
    if run.first_token_at_ms is not None:
        window = run.first_token_at_ms - run.started_at_ms
        ttft = _round(window)
        if window > 0 and run.prompt_tokens > 0:
            prefill = _round(run.prompt_tokens / (window / 1000.0))
        decode_window = run.finished_at_ms - run.first_token_at_ms
        if decode_window > 0 and run.output_tokens > 0:
            decode = _round(run.output_tokens / (decode_window / 1000.0))
    return CalibrationSample(
        sequence=run.sequence,
        kind=run.kind,
        ok=True,
        ttft_ms=ttft,
        prefill_tokens_per_s=prefill,
        decode_tokens_per_s=decode,
        peak_memory_bytes=run.peak_memory_bytes,
        prompt_tokens=run.prompt_tokens,
        output_tokens=run.output_tokens,
    )


@dataclass(frozen=True, slots=True)
class MetricAggregate:
    """Median and spread of one metric over the successful samples of a kind."""

    metric: str
    kind: Literal["cold", "warm"]
    count: int
    median: float | None
    spread: float | None
    minimum: float | None
    maximum: float | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "kind": self.kind,
            "count": self.count,
            "median": self.median,
            "spread": self.spread,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


def _aggregate(samples: Sequence[CalibrationSample], metric: str, kind: Literal["cold", "warm"]) -> MetricAggregate:
    values = [
        float(getattr(sample, metric))
        for sample in samples
        if sample.ok and sample.kind == kind and getattr(sample, metric) is not None
    ]
    if not values:
        return MetricAggregate(metric=metric, kind=kind, count=0, median=None, spread=None, minimum=None, maximum=None)
    return MetricAggregate(
        metric=metric,
        kind=kind,
        count=len(values),
        median=_round(statistics.median(values)),
        spread=_round(max(values) - min(values)),
        minimum=_round(min(values)),
        maximum=_round(max(values)),
    )


@dataclass(frozen=True, slots=True)
class ModelPerformanceProfile:
    """Aggregated warm/cold metrics pinned to one calibration-input identity."""

    inputs: CalibrationInputs
    samples: tuple[CalibrationSample, ...]
    warm: tuple[MetricAggregate, ...]
    cold: tuple[MetricAggregate, ...]
    failure_rate: float

    @property
    def measured_runs(self) -> int:
        return len(self.samples)

    @property
    def failed_runs(self) -> int:
        return sum(1 for sample in self.samples if not sample.ok)

    def aggregate(self, metric: str, kind: str) -> MetricAggregate:
        if metric not in METRIC_NAMES:
            raise ModelCalibrationError(f"unknown metric: {metric}.")
        if kind not in PROFILE_KINDS:
            raise ModelCalibrationError(f"unknown run kind: {kind}.")
        source = self.warm if kind == "warm" else self.cold
        return next(item for item in source if item.metric == metric)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "inputs": self.inputs.to_mapping(),
            "inputs_digest": self.inputs.digest,
            "samples": [sample.to_mapping() for sample in self.samples],
            "warm": [item.to_mapping() for item in self.warm],
            "cold": [item.to_mapping() for item in self.cold],
            "failure_rate": self.failure_rate,
            "measured_runs": self.measured_runs,
            "failed_runs": self.failed_runs,
        }

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


def calibrate(inputs: CalibrationInputs, samples: tuple[CalibrationSample, ...]) -> ModelPerformanceProfile:
    """Aggregate derived samples into a profile; recomputable from its parts."""
    if not isinstance(inputs, CalibrationInputs):
        raise TypeError("inputs must be a CalibrationInputs value.")
    if not isinstance(samples, tuple) or not all(isinstance(sample, CalibrationSample) for sample in samples):
        raise TypeError("samples must be a tuple of CalibrationSample values.")
    sequences = [sample.sequence for sample in samples]
    if len(set(sequences)) != len(sequences):
        raise ModelCalibrationError("duplicate sample sequences in calibration.")
    warm = tuple(_aggregate(samples, metric, "warm") for metric in METRIC_NAMES)
    cold = tuple(_aggregate(samples, metric, "cold") for metric in METRIC_NAMES)
    failures = sum(1 for sample in samples if not sample.ok)
    rate = _round(failures / len(samples)) if samples else 0.0
    return ModelPerformanceProfile(
        inputs=inputs,
        samples=samples,
        warm=warm,
        cold=cold,
        failure_rate=rate,
    )


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """What the runner asks the probe to perform for one attempt."""

    sequence: int
    kind: RunKind


Probe = Callable[[ProbeRequest], Awaitable[RawRun]]


async def run_calibration(
    inputs: CalibrationInputs,
    probe: Probe,
    *,
    warm_repeats: int = 3,
    cold_repeats: int = 1,
    warmup_runs: int = 1,
) -> ModelPerformanceProfile:
    """Drive a probe through warmup, measured warm, and measured cold phases.

    The probe owns model loading/unload cycling (e.g. the AS-019 lifecycle
    adapter with a timed chat call) and returns :class:`RawRun` timings.
    Probe exceptions become failed samples — the run never aborts mid-suite —
    while cancellation always propagates untouched.
    """
    if not isinstance(inputs, CalibrationInputs):
        raise TypeError("inputs must be a CalibrationInputs value.")
    if not callable(probe):
        raise TypeError("probe must be an async callable.")
    warm_repeats = _positive_int(warm_repeats, "warm_repeats")
    cold_repeats = _non_negative_int(cold_repeats, "cold_repeats")
    warmup_runs = _non_negative_int(warmup_runs, "warmup_runs")

    samples: list[CalibrationSample] = []
    sequence = 0
    plan: list[RunKind] = []
    plan.extend("warmup" for _ in range(warmup_runs))
    plan.extend("warm" for _ in range(warm_repeats))
    plan.extend("cold" for _ in range(cold_repeats))
    for kind in plan:
        sequence += 1
        request = ProbeRequest(sequence=sequence, kind=kind)
        try:
            run = await probe(request)
        except Exception as exc:
            if kind != "warmup":
                samples.append(
                    CalibrationSample(
                        sequence=sequence,
                        kind=kind,
                        ok=False,
                        error=_safe_reason(f"probe failure: {type(exc).__name__}", "error") or "probe failure",
                    )
                )
            continue
        if not isinstance(run, RawRun):
            raise ModelCalibrationError("probe must return RawRun values.")
        if run.sequence != sequence or run.kind != kind:
            raise ModelCalibrationError("probe returned a run that does not match the request.")
        if kind != "warmup":
            samples.append(derive_sample(run))
    return calibrate(inputs, tuple(samples))


# -- reproducible sample store ------------------------------------------------


def _record_line(payload: Mapping[str, Any]) -> str:
    body = _digest(payload)
    merged = dict(payload)
    merged["record_digest"] = body
    return json.dumps(merged, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validated_path(path: Path | str) -> Path:
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ModelCalibrationError("store path must be a non-empty path.")
    return Path(path)


def save_profile(path: Path | str, profile: ModelPerformanceProfile) -> Path:
    """Persist one profile as self-describing JSON lines (single writer).

    Layout: a ``profile`` header record, then one ``sample`` record per
    measurement. Every line carries a canonical digest of its own content;
    ``load_profile`` re-verifies line digests and recomputes all aggregates,
    refusing silently-tampered files.
    """
    if not isinstance(profile, ModelPerformanceProfile):
        raise TypeError("profile must be a ModelPerformanceProfile value.")
    target = _validated_path(path)
    header = {
        "record": "profile",
        "profile_digest": profile.digest,
        "inputs": profile.inputs.to_mapping(),
        "counts": {
            "measured_runs": profile.measured_runs,
            "failed_runs": profile.failed_runs,
            "failure_rate": profile.failure_rate,
        },
    }
    lines = [_record_line(header)]
    for sample in profile.samples:
        lines.append(_record_line({"record": "sample", "sample": sample.to_mapping(), "sample_digest": sample.digest}))
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    temp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(temp, target)
    return target


def load_profile(path: Path | str) -> ModelPerformanceProfile:
    """Load and verify a stored profile; raises on any integrity failure."""
    target = _validated_path(path)
    if not target.is_file():
        raise CalibrationStoreError("calibration file was not found.")
    header: Mapping[str, Any] | None = None
    samples: list[CalibrationSample] = []
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationStoreError(f"line {number} is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise CalibrationStoreError(f"line {number} is not an object.")
        claimed = payload.pop("record_digest", None)
        if claimed != _digest(payload):
            raise CalibrationStoreError(f"line {number} digest mismatch: stored record was tampered with.")
        record = payload.get("record")
        if record == "profile":
            if header is not None:
                raise CalibrationStoreError("duplicate profile header record.")
            header = payload
        elif record == "sample":
            try:
                sample = CalibrationSample.from_mapping(payload["sample"])
            except (KeyError, ModelCalibrationError, TypeError) as exc:
                raise CalibrationStoreError(f"line {number}: invalid sample record: {exc}") from exc
            if sample.digest != payload.get("sample_digest"):
                raise CalibrationStoreError(f"line {number}: sample digest mismatch.")
            samples.append(sample)
        else:
            raise CalibrationStoreError(f"line {number}: unknown record type {record!r}.")
    if header is None:
        raise CalibrationStoreError("missing profile header record.")
    try:
        inputs = CalibrationInputs.from_mapping(header["inputs"])
    except (KeyError, ModelCalibrationError, TypeError) as exc:
        raise CalibrationStoreError(f"profile header references invalid inputs: {exc}") from exc
    if header.get("counts") != {
        "measured_runs": len(samples),
        "failed_runs": sum(1 for sample in samples if not sample.ok),
        "failure_rate": _round(sum(1 for sample in samples if not sample.ok) / len(samples)) if samples else 0.0,
    }:
        raise CalibrationStoreError("profile header counts do not match the stored samples.")
    profile = calibrate(inputs, tuple(samples))
    if profile.digest != header.get("profile_digest"):
        raise CalibrationStoreError("stored profile digest does not match recomputation: file was tampered with or written by an incompatible version.")
    return profile
