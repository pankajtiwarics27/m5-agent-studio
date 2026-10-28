from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from benchmark_publication import (
    BenchmarkPublicationBundle,
    BenchmarkPublicationConfig,
    BenchmarkPublicationError,
    build_publication_bundle,
    load_publication_config,
    verify_bundle_mapping,
    write_bundle_atomic,
)
from model_calibration import CalibrationInputs, CalibrationSample, ModelPerformanceProfile, calibrate, save_profile
from performance_profiles import BenchmarkMethodology

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / "scripts" / "publish_benchmark_results.py"
HARDWARE = {"schema_version": 1, "operating_system": "Linux", "architecture": "x86_64"}
PROMPT_DIGEST = "sha256:" + "ef" * 32


def _config(*, warm: int = 2, cold: int = 1) -> BenchmarkPublicationConfig:
    return BenchmarkPublicationConfig(
        methodology=BenchmarkMethodology("publication-test", 1),
        warmup_runs=1,
        warm_repeats=warm,
        cold_repeats=cold,
        caveats=("Synthetic fixture only; not a live benchmark.", "Target-runtime performance is not claimed."),
    )


def _profile(
    model_id: str,
    digest_letter: str,
    *,
    hardware: dict | None = None,
    prompt_digest: str | None = PROMPT_DIGEST,
    error: str | None = None,
) -> ModelPerformanceProfile:
    inputs = CalibrationInputs(
        model_id=model_id,
        model_digest="sha256:" + digest_letter * 64,
        model_revision="rev-1",
        quantization="Q4",
        runtime="fixture-runtime",
        runtime_version="1.0",
        context_length=2048,
        prompt_set_digest=prompt_digest,
        hardware=HARDWARE if hardware is None else hardware,
    )
    samples = [
        CalibrationSample(sequence=1, kind="warm", ok=True, decode_tokens_per_s=25, peak_memory_bytes=1024),
        CalibrationSample(sequence=2, kind="warm", ok=True, decode_tokens_per_s=27, peak_memory_bytes=1050),
    ]
    samples.append(
        CalibrationSample(
            sequence=3,
            kind="cold",
            ok=error is None,
            decode_tokens_per_s=20 if error is None else None,
            peak_memory_bytes=1200 if error is None else None,
            error=error,
        )
    )
    return calibrate(inputs, tuple(samples))


def _write_profile(directory: Path, name: str, profile: ModelPerformanceProfile) -> Path:
    path = directory / name
    save_profile(path, profile)
    return path


def test_bundle_preserves_verified_raw_profiles_and_configuration(tmp_path: Path) -> None:
    beta = _write_profile(tmp_path, "beta.jsonl", _profile("beta", "b"))
    alpha = _write_profile(tmp_path, "alpha.jsonl", _profile("alpha", "a"))
    before = alpha.read_bytes()
    bundle = build_publication_bundle((beta, alpha), _config(), publisher_script=PUBLISHER)
    payload = bundle.to_mapping()

    assert [item.model_id for item in bundle.profiles] == ["alpha", "beta"]
    assert bundle.config_digest == _config().digest
    assert bundle.hardware_digest == bundle.profiles[0].profile_key.hardware_digest
    assert bundle.os_id == "Linux x86_64"
    assert bundle.publisher_script_digest == "sha256:" + hashlib.sha256(PUBLISHER.read_bytes()).hexdigest()
    calibration_module = ROOT / "model_calibration.py"
    assert bundle.calibration_module_digest == "sha256:" + hashlib.sha256(calibration_module.read_bytes()).hexdigest()
    assert bundle.profiles[0].raw_profile_jsonl.encode("utf-8") == before
    assert bundle.profiles[0].raw_sha256 == "sha256:" + hashlib.sha256(before).hexdigest()
    assert verify_bundle_mapping(payload) == bundle.digest
    assert payload["config"]["calibration_plan"] == {"warmup_runs": 1, "warm_repeats": 2, "cold_repeats": 1}
    assert any("warmup runs" in caveat for caveat in payload["config"]["caveats"])

    output = tmp_path / "published" / "bundle.json"
    assert write_bundle_atomic(output, bundle) == output
    serialized = json.loads(output.read_text(encoding="utf-8"))
    assert verify_bundle_mapping(serialized) == bundle.digest
    assert list(output.parent.iterdir()) == [output]


def test_example_config_loads_as_a_plan_not_as_result_evidence() -> None:
    config = load_publication_config(ROOT / "benchmarks" / "benchmark-config.example.json")
    assert config.methodology.name == "m5-agent-studio-calibration"
    assert (config.warmup_runs, config.warm_repeats, config.cold_repeats) == (1, 3, 1)
    assert any("Template only" in caveat for caveat in config.caveats)


def test_publication_config_validation_refusals() -> None:
    with pytest.raises(BenchmarkPublicationError, match="at least one caveat"):
        BenchmarkPublicationConfig(BenchmarkMethodology("x", 1), 0, 1, 0, ())
    with pytest.raises(BenchmarkPublicationError, match="warmup_runs"):
        BenchmarkPublicationConfig(BenchmarkMethodology("x", 1), True, 1, 0, ("caveat",))
    with pytest.raises(BenchmarkPublicationError, match="cold_repeats"):
        BenchmarkPublicationConfig(BenchmarkMethodology("x", 1), 0, 1, -1, ("caveat",))
    with pytest.raises(BenchmarkPublicationError, match="unsupported publication config"):
        BenchmarkPublicationConfig.from_mapping({"schema_version": 99})
    with pytest.raises(BenchmarkPublicationError, match="caveats must be a sequence"):
        BenchmarkPublicationConfig.from_mapping(
            {
                "schema_version": 1,
                "methodology": BenchmarkMethodology("x", 1).to_mapping(),
                "calibration_plan": {"warmup_runs": 0, "warm_repeats": 1, "cold_repeats": 0},
                "caveats": "not a list",
            }
        )


def test_profile_sample_counts_must_match_the_published_plan(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, "profile.jsonl", _profile("alpha", "a"))
    with pytest.raises(BenchmarkPublicationError, match="sample counts"):
        build_publication_bundle((path,), _config(warm=3), publisher_script=PUBLISHER)


def test_incomparable_contexts_and_duplicate_builds_are_refused(tmp_path: Path) -> None:
    first = _write_profile(tmp_path, "first.jsonl", _profile("alpha", "a"))
    other_hardware = {"schema_version": 1, "operating_system": "OtherOS", "architecture": "x86_64"}
    second = _write_profile(tmp_path, "second.jsonl", _profile("beta", "b", hardware=other_hardware))
    with pytest.raises(BenchmarkPublicationError, match="incomparable"):
        build_publication_bundle((first, second), _config(), publisher_script=PUBLISHER)

    duplicate = _write_profile(tmp_path, "duplicate.jsonl", _profile("alpha", "a"))
    with pytest.raises(BenchmarkPublicationError, match="duplicate model identity"):
        build_publication_bundle((first, duplicate), _config(), publisher_script=PUBLISHER)


def test_tampered_raw_profile_is_rejected(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, "profile.jsonl", _profile("alpha", "a"))
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace('"record_digest":"sha256:', '"record_digest":"sha256:0', 1), encoding="utf-8")
    with pytest.raises(BenchmarkPublicationError, match="integrity verification"):
        build_publication_bundle((path,), _config(), publisher_script=PUBLISHER)


def test_private_prompt_hardware_and_failure_details_are_refused(tmp_path: Path) -> None:
    prompt = _write_profile(tmp_path, "prompt.jsonl", _profile("prompt", "a", prompt_digest="do not publish this prompt"))
    with pytest.raises(BenchmarkPublicationError, match="prompt_set_digest"):
        build_publication_bundle((prompt,), _config(), publisher_script=PUBLISHER)

    unsafe_hardware = {**HARDWARE, "serial_number": "private-device-id"}
    hardware = _write_profile(tmp_path, "hardware.jsonl", _profile("hardware", "b", hardware=unsafe_hardware))
    with pytest.raises(BenchmarkPublicationError, match="unsupported fields"):
        build_publication_bundle((hardware,), _config(), publisher_script=PUBLISHER)

    unsafe_error = _write_profile(tmp_path, "error.jsonl", _profile("error", "c", error="provider response contained private prompt"))
    with pytest.raises(BenchmarkPublicationError, match="exception-only"):
        build_publication_bundle((unsafe_error,), _config(), publisher_script=PUBLISHER)


def test_bundle_digest_detects_edits_and_output_is_atomic(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, "profile.jsonl", _profile("alpha", "a"))
    bundle = build_publication_bundle((profile,), _config(), publisher_script=PUBLISHER)
    tampered = bundle.to_mapping()
    tampered["profiles"][0]["failure_rate"] = 0.5
    with pytest.raises(BenchmarkPublicationError, match="bundle digest mismatch"):
        verify_bundle_mapping(tampered)

    with pytest.raises(TypeError, match="bundle must be"):
        write_bundle_atomic(tmp_path / "invalid.json", cast(BenchmarkPublicationBundle, "not a bundle"))
    with pytest.raises(BenchmarkPublicationError, match="profile_paths must be"):
        build_publication_bundle((), _config(), publisher_script=PUBLISHER)
    with pytest.raises(BenchmarkPublicationError, match="publisher script is unavailable"):
        build_publication_bundle((profile,), _config(), publisher_script=tmp_path / "missing.py")


def test_cli_publishes_a_local_bundle_without_remote_calls(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path, "profile.jsonl", _profile("alpha", "a"))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config().to_mapping()), encoding="utf-8")
    output = tmp_path / "release" / "bundle.json"
    result = subprocess.run(
        [sys.executable, str(PUBLISHER), "--config", str(config_path), "--profile", str(profile), "--output", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Published verified bundle sha256:" in result.stdout
    assert verify_bundle_mapping(json.loads(output.read_text(encoding="utf-8")))

    original = profile.read_bytes()
    refused = subprocess.run(
        [sys.executable, str(PUBLISHER), "--config", str(config_path), "--profile", str(profile), "--output", str(profile)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 2
    assert profile.read_bytes() == original
    assert "BenchmarkPublicationError" in refused.stderr
