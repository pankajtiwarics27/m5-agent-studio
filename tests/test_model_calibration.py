from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from model_calibration import (
    CalibrationInputs,
    CalibrationSample,
    CalibrationStoreError,
    ModelCalibrationError,
    ModelPerformanceProfile,
    RawRun,
    calibrate,
    derive_sample,
    load_profile,
    run_calibration,
    save_profile,
)
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance


def _descriptor(model_id: str = "cal:1", *, digest: str = "sha256:m1", runtime: str = "ollama") -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=digest,
        source=runtime,
        revision="rev-9",
        runtime=runtime,
        quantization="Q4_K_M",
        limits=ModelLimits(context_tokens=8192),
        provenance=ModelProvenance(runtime, model_id, "verified"),
    )


def _inputs(**overrides: object) -> CalibrationInputs:
    values: dict[str, object] = {
        "model_id": "cal:1",
        "model_digest": "sha256:m1",
        "runtime": "ollama",
        "context_length": 8192,
    }
    values.update(overrides)
    return CalibrationInputs(**values)  # type: ignore[arg-type]


def _warm_run(sequence: int, ttft: float, decode_ms: float, *, prompt: int = 100, output: int = 50, memory: int = 1234) -> RawRun:
    start = 1000.0
    first = start + ttft
    return RawRun(
        sequence=sequence,
        kind="warm",
        started_at_ms=start,
        first_token_at_ms=first,
        finished_at_ms=first + decode_ms,
        prompt_tokens=prompt,
        output_tokens=output,
        peak_memory_bytes=memory,
    )


def test_derive_sample_computes_ttft_and_throughputs() -> None:
    run = _warm_run(1, 250.0, 1000.0)  # 100 prompt tokens in 250ms; 50 tokens in 1s
    sample = derive_sample(run)
    assert sample.ok is True
    assert sample.ttft_ms == 250.0
    assert sample.prefill_tokens_per_s == 400.0
    assert sample.decode_tokens_per_s == 50.0
    assert sample.peak_memory_bytes == 1234
    assert sample.prompt_tokens == 100 and sample.output_tokens == 50
    assert sample.error is None
    assert json.dumps(sample.to_mapping())


def test_derive_sample_handles_degenerate_and_failed_runs_without_raising() -> None:
    instant = derive_sample(
        RawRun(sequence=1, kind="warm", started_at_ms=5.0, first_token_at_ms=5.0, finished_at_ms=5.0, prompt_tokens=10, output_tokens=0)
    )
    assert instant.ok is True and instant.ttft_ms == 0.0
    assert instant.prefill_tokens_per_s is None and instant.decode_tokens_per_s is None  # no invented infinity

    no_first_token = derive_sample(RawRun(sequence=2, kind="warm", started_at_ms=0.0, finished_at_ms=10.0, output_tokens=4))
    assert no_first_token.ok is False
    assert no_first_token.error == "inconsistent timing: output tokens without a first-token timestamp"

    failed = derive_sample(RawRun(sequence=3, kind="cold", started_at_ms=0.0, finished_at_ms=9.0, error="model load timed out"))
    assert failed.kind == "cold" and failed.ok is False and failed.ttft_ms is None

    with pytest.raises(ModelCalibrationError, match="warmup runs are discarded"):
        derive_sample(RawRun(sequence=4, kind="warmup", started_at_ms=0.0, finished_at_ms=1.0))


def test_raw_run_validation() -> None:
    with pytest.raises(ModelCalibrationError, match="first_token_at_ms must lie between"):
        RawRun(sequence=0, kind="warm", started_at_ms=10.0, first_token_at_ms=5.0, finished_at_ms=20.0)
    with pytest.raises(ModelCalibrationError, match="finished_at_ms must not precede"):
        RawRun(sequence=0, kind="warm", started_at_ms=10.0, finished_at_ms=9.0)
    with pytest.raises(ModelCalibrationError, match="kind must be one of"):
        RawRun(sequence=0, kind="hot", started_at_ms=0.0, finished_at_ms=1.0)  # type: ignore[arg-type]
    with pytest.raises(ModelCalibrationError, match="finite non-negative number"):
        RawRun(sequence=0, kind="warm", started_at_ms=-1.0, finished_at_ms=1.0)
    with pytest.raises(ModelCalibrationError, match="prompt_tokens must be a non-negative integer"):
        RawRun(sequence=0, kind="warm", started_at_ms=0.0, finished_at_ms=1.0, prompt_tokens=-2)
    with pytest.raises(TypeError, match="run must be a RawRun"):
        derive_sample({"sequence": 0})  # type: ignore[arg-type]


def test_probe_error_text_is_sanitized_and_bounded() -> None:
    noisy = RawRun(sequence=1, kind="warm", started_at_ms=0.0, finished_at_ms=1.0, error="line1\n\x00secret-body " + "x" * 600)
    assert noisy.error is not None
    assert "\n" not in noisy.error and "\x00" not in noisy.error
    assert len(noisy.error) <= 500
    assert noisy.error.endswith("…")


def test_inputs_pin_registry_identity_and_digest() -> None:
    pinned = CalibrationInputs.for_model(
        _descriptor(),
        runtime_version="0.4.7",
        prompt_set_digest="sha256:prompts",
        hardware={"operating_system": "macOS", "architecture": "arm64"},
        temperature=0.2,
    )
    mapping = pinned.to_mapping()
    assert mapping["model_digest"] == "sha256:m1"
    assert mapping["model_revision"] == "rev-9"  # from the descriptor
    assert mapping["quantization"] == "Q4_K_M"
    assert mapping["context_length"] == 8192  # default from descriptor limits
    assert mapping["runtime_version"] == "0.4.7"
    assert mapping["hardware"]["architecture"] == "arm64"
    assert pinned.digest == CalibrationInputs.for_model(  # deterministic and deep on content
        _descriptor(), runtime_version="0.4.7", prompt_set_digest="sha256:prompts",
        hardware={"architecture": "arm64", "operating_system": "macOS"}, temperature=0.2,
    ).digest
    other = CalibrationInputs.for_model(_descriptor(), runtime_version="0.4.8", prompt_set_digest="sha256:prompts", temperature=0.2)
    assert other.digest != pinned.digest  # a runtime version bump is a different calibration identity

    with pytest.raises(ModelCalibrationError, match="temperature must be within"):
        _inputs(temperature=2.5)
    with pytest.raises(ModelCalibrationError, match="context_length must be a positive integer"):
        _inputs(context_length=0)
    with pytest.raises(TypeError, match="hardware must be a mapping"):
        _inputs(hardware=["not", "a", "mapping"])
    with pytest.raises(TypeError, match="descriptor must be a ModelDescriptor"):
        CalibrationInputs.for_model("cal:1")  # type: ignore[arg-type]
    roundtrip = CalibrationInputs.from_mapping(pinned.to_mapping())
    assert roundtrip.digest == pinned.digest


def test_calibrate_aggregates_median_spread_and_failure_rate() -> None:
    samples = tuple(
        derive_sample(_warm_run(sequence, ttft, 1000.0, memory=mem))
        for sequence, (ttft, mem) in enumerate([(100.0, 10), (140.0, 12), (120.0, 11)], start=1)
    )
    failures = (
        CalibrationSample(sequence=9, kind="cold", ok=False, error="probe failure: TimeoutError"),
        CalibrationSample(sequence=10, kind="warm", ok=False, error="probe failure: TimeoutError"),
    )
    profile = calibrate(_inputs(), samples + failures)

    ttft = profile.aggregate("ttft_ms", "warm")
    assert ttft.count == 3 and ttft.median == 120.0 and ttft.spread == 40.0 and ttft.minimum == 100.0 and ttft.maximum == 140.0
    memory = profile.aggregate("peak_memory_bytes", "warm")
    assert memory.median == 11.0 and memory.spread == 2.0
    cold = profile.aggregate("ttft_ms", "cold")
    assert cold.count == 0 and cold.median is None and cold.spread is None  # failed runs never feed timing metrics
    assert profile.measured_runs == 5 and profile.failed_runs == 2
    assert profile.failure_rate == 0.4
    assert profile.inputs.digest in json.dumps(profile.to_mapping())

    with pytest.raises(ModelCalibrationError, match="unknown metric"):
        profile.aggregate("latency", "warm")
    with pytest.raises(ModelCalibrationError, match="unknown run kind"):
        profile.aggregate("ttft_ms", "hot")
    with pytest.raises(TypeError, match="samples must be a tuple"):
        calibrate(_inputs(), [samples[0]])  # type: ignore[list-item]
    with pytest.raises(ModelCalibrationError, match="duplicate sample sequences"):
        calibrate(_inputs(), (samples[0], samples[0]))


def test_empty_profile_is_valid_with_zeroed_audit() -> None:
    profile = calibrate(_inputs(), ())
    assert profile.measured_runs == 0 and profile.failure_rate == 0.0
    assert all(item.count == 0 for item in profile.warm + profile.cold)


def _profile(samples: tuple[CalibrationSample, ...] | None = None) -> ModelPerformanceProfile:
    if samples is None:
        samples = (
            derive_sample(_warm_run(1, 100.0, 1000.0)),
            derive_sample(_warm_run(2, 120.0, 900.0)),
            CalibrationSample(sequence=3, kind="cold", ok=True, ttft_ms=400.0, prefill_tokens_per_s=250.0, decode_tokens_per_s=40.0, peak_memory_bytes=1300, prompt_tokens=100, output_tokens=36),
        )
    return calibrate(_inputs(), samples)


def test_store_roundtrip_reproduces_the_exact_profile(tmp_path: Path) -> None:
    original = _profile()
    path = save_profile(tmp_path / "cal.jsonl", original)
    loaded = load_profile(path)
    assert isinstance(loaded, ModelPerformanceProfile)
    assert loaded.digest == original.digest
    assert loaded.samples == original.samples
    assert loaded.aggregate("ttft_ms", "warm").median == original.aggregate("ttft_ms", "warm").median
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("{")
    assert len(lines) == 1 + len(original.samples)
    records = [json.loads(line) for line in lines]
    assert records[0]["record"] == "profile" and records[1]["record"] == "sample"


def _line_digest(payload: dict) -> str:
    import hashlib

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_store_rejects_tampering_corruption_and_foreign_files(tmp_path: Path) -> None:
    path = save_profile(tmp_path / "cal.jsonl", _profile())

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["sample"]["ttft_ms"] = 1.0  # plausible edit, digests left stale
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    with pytest.raises(CalibrationStoreError, match="digest mismatch"):
        load_profile(path)

    # An edit that even recomputes the sample and line digests is still
    # caught by the profile digest recomputed from samples + aggregates.
    original = _profile()
    path2 = save_profile(tmp_path / "cal2.jsonl", original)
    lines = path2.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["sample"]["ttft_ms"] = 2.0
    record["sample_digest"] = CalibrationSample.from_mapping(record["sample"]).digest
    payload = {key: value for key, value in record.items() if key != "record_digest"}
    record["record_digest"] = _line_digest(payload)
    lines[1] = json.dumps(record, sort_keys=True)
    path2.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    with pytest.raises(CalibrationStoreError, match="profile digest does not match recomputation"):
        load_profile(path2)

    (tmp_path / "junk.jsonl").write_text("not json at all\n", encoding="utf-8")
    with pytest.raises(CalibrationStoreError, match="not valid JSON"):
        load_profile(tmp_path / "junk.jsonl")
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(CalibrationStoreError, match="missing profile header"):
        load_profile(tmp_path / "empty.jsonl")
    (tmp_path / "unknown.jsonl").write_text(
        json.dumps({"record": "mystery", "record_digest": "sha256:x"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(CalibrationStoreError, match="digest mismatch"):
        load_profile(tmp_path / "unknown.jsonl")
    with pytest.raises(CalibrationStoreError, match="not found"):
        load_profile(tmp_path / "nope.jsonl")
    with pytest.raises(TypeError, match="ModelPerformanceProfile"):
        save_profile(tmp_path / "x.jsonl", "nope")  # type: ignore[arg-type]


def test_run_calibration_drives_probe_phases_and_tolerates_failures() -> None:
    requests: list[tuple[int, str]] = []

    async def probe(request) -> RawRun:
        requests.append((request.sequence, request.kind))
        if request.kind == "warmup":
            return RawRun(sequence=request.sequence, kind="warmup", started_at_ms=0.0, finished_at_ms=50.0)
        if request.kind == "warm" and request.sequence == 3:
            raise TimeoutError("decode stall")
        if request.kind == "cold":
            return RawRun(sequence=request.sequence, kind="cold", started_at_ms=0.0, first_token_at_ms=500.0, finished_at_ms=1500.0, prompt_tokens=200, output_tokens=40, peak_memory_bytes=2048)
        return _warm_run(request.sequence, 100.0 + request.sequence, 1000.0)

    profile = asyncio.run(run_calibration(_inputs(), probe, warm_repeats=3, cold_repeats=1, warmup_runs=2))
    assert [kind for _seq, kind in requests] == ["warmup", "warmup", "warm", "warm", "warm", "cold"]
    assert profile.measured_runs == 4  # warmups excluded entirely
    warm = profile.aggregate("ttft_ms", "warm")
    assert warm.count == 2  # the raising probe call became a failed sample
    assert profile.failure_rate == 0.25
    failed = next(sample for sample in profile.samples if not sample.ok)
    assert failed.error == "probe failure: TimeoutError"  # exception type only - no leaky detail text
    assert profile.aggregate("ttft_ms", "cold").median == 500.0
    assert isinstance(profile, ModelPerformanceProfile)


def test_run_calibration_enforces_probe_protocol_and_cancellation() -> None:
    async def liar(request) -> RawRun:  # returns a run not matching the request
        return _warm_run(99, 10.0, 10.0)

    with pytest.raises(ModelCalibrationError, match="does not match the request"):
        asyncio.run(run_calibration(_inputs(), liar, warm_repeats=1, cold_repeats=0, warmup_runs=0))

    async def not_a_run(request):
        return {"sequence": 1}

    with pytest.raises(ModelCalibrationError, match="must return RawRun"):
        asyncio.run(run_calibration(_inputs(), not_a_run))

    async def cancels(request) -> RawRun:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_calibration(_inputs(), cancels))

    with pytest.raises(TypeError, match="async callable"):
        asyncio.run(run_calibration(_inputs(), object()))  # type: ignore[arg-type]
    with pytest.raises(ModelCalibrationError, match="warm_repeats must be a positive integer"):
        asyncio.run(run_calibration(_inputs(), cancels, warm_repeats=0))


def test_cold_and_warm_separate_and_profile_digest_stability() -> None:
    cold = CalibrationSample(sequence=1, kind="cold", ok=True, ttft_ms=900.0, prefill_tokens_per_s=None, decode_tokens_per_s=30.0, peak_memory_bytes=999, prompt_tokens=0, output_tokens=30)
    warm = derive_sample(_warm_run(2, 100.0, 1000.0))
    profile = calibrate(_inputs(), (cold, warm))
    assert profile.aggregate("ttft_ms", "cold").median == 900.0
    assert profile.aggregate("decode_tokens_per_s", "cold").median == 30.0
    assert profile.aggregate("decode_tokens_per_s", "warm").median == 50.0
    assert profile.digest == calibrate(_inputs(), (cold, warm)).digest
    assert profile.digest != calibrate(_inputs(temperature=0.7), (cold, warm)).digest  # inputs pin the profile

    mapping = profile.to_mapping()
    assert set(mapping) == {"schema_version", "inputs", "inputs_digest", "samples", "warm", "cold", "failure_rate", "measured_runs", "failed_runs"}
    assert json.dumps(mapping)
