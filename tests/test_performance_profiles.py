from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from model_calibration import CalibrationInputs, CalibrationSample, ModelPerformanceProfile, calibrate
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from performance_profiles import (
    BenchmarkMethodology,
    ProfileKey,
    ProfileRecord,
    ProfileStoreError,
    ProfileVault,
    ProfileVaultError,
    os_id_from_hardware,
)

HARDWARE = {
    "schema_version": 1,
    "operating_system": "macOS",
    "architecture": "arm64",
    "memory_pressure": "nominal",
}
METH = BenchmarkMethodology(name="studio-local", version=1)


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sample(sequence: int = 1, ttft: float = 100.0) -> CalibrationSample:
    return CalibrationSample(
        sequence=sequence,
        kind="warm",
        ok=True,
        ttft_ms=ttft,
        prefill_tokens_per_s=1000.0,
        decode_tokens_per_s=50.0,
        peak_memory_bytes=1234,
        prompt_tokens=100,
        output_tokens=50,
    )


def _profile(
    *,
    model_digest: str = "sha256:m1",
    quantization: str | None = "Q4_K_M",
    runtime_version: str | None = "0.5.7",
    hardware: dict | None = None,
    temperature: float = 0.0,
    ttft: float = 100.0,
    with_hardware: bool = True,
) -> ModelPerformanceProfile:
    inputs = CalibrationInputs(
        model_id="cal:1",
        model_digest=model_digest,
        runtime="ollama",
        context_length=8192,
        temperature=temperature,
        quantization=quantization,
        runtime_version=runtime_version,
        hardware=dict(HARDWARE if hardware is None else hardware) if with_hardware else None,
    )
    return calibrate(inputs, (_sample(1, ttft),))


def _seal(entry: dict) -> str:
    payload = {key: value for key, value in entry.items() if key != "record_digest"}
    payload["record_digest"] = _digest(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(f"{json.dumps(e, sort_keys=True, separators=(',', ':'))}\n" for e in entries), encoding="utf-8")


def test_append_versions_history_and_rereads_cleanly(tmp_path: Path) -> None:
    profile = _profile()
    vault = ProfileVault(tmp_path)
    outcome = vault.append(profile, METH)
    assert outcome.created is True
    assert outcome.record.version == 1
    assert outcome.record.previous_digest == "sha256:genesis"
    assert len(list(tmp_path.glob("profiles-*.jsonl"))) == 1

    key = ProfileKey.derive(profile, methodology=METH)
    latest = vault.get(key)
    assert latest is not None and latest.profile.digest == profile.digest
    assert latest.profile.inputs.hardware == dict(HARDWARE)
    assert vault.versions(key) == (1,)
    summary = vault.keys()[0]
    assert summary.key.digest == key.digest and summary.retained == 1 and summary.next_version == 2

    second = vault.append(_profile(ttft=140.0), METH)
    assert second.created is True and second.record.version == 2
    assert second.record.previous_digest == outcome.record.digest  # the chain links record digests
    assert vault.versions(key) == (1, 2)
    assert vault.get(key, 1).profile.digest == profile.digest
    assert vault.latest_for_profile(_profile(ttft=140.0), METH).digest == second.record.digest


def test_append_is_an_idempotent_no_op_for_identical_profiles(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path)
    profile = _profile()
    vault.append(profile, METH)
    path = next(tmp_path.glob("profiles-*.jsonl"))
    before = path.read_bytes()

    again = vault.append(profile, METH)
    assert again.created is False and again.record.version == 1
    rebuilt = calibrate(profile.inputs, profile.samples)
    assert vault.append(rebuilt, METH).created is False  # content-addressed, not object-identity
    assert path.read_bytes() == before


def test_history_is_pinned_per_setup_but_temperature_lives_in_the_profile(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path)
    vault.append(_profile(), METH)
    other_build = _profile(runtime_version="0.9.9")
    vault.append(other_build, METH)

    base_key = ProfileKey.derive(_profile(), methodology=METH)
    other_key = ProfileKey.derive(other_build, methodology=METH)
    assert vault.get(base_key).profile.inputs.runtime_version == "0.5.7"
    assert vault.get(other_key).profile.digest == other_build.digest
    assert len(vault.keys()) == 2

    # Temperature is not one of the six key components: it stays inside the
    # profile's own pinned inputs, so the key files it into the same history
    # while each version carries its exact setup.
    hot = _profile(temperature=1.0)
    outcome = vault.append(hot, METH)
    assert outcome.record.version == 2
    assert outcome.record.profile.inputs.digest != vault.get(base_key, 1).profile.inputs.digest


def test_for_lookup_finds_history_by_registry_descriptor(tmp_path: Path) -> None:
    descriptor = ModelDescriptor(
        model_id="cal:1",
        digest="sha256:m1",
        source="ollama",
        revision="rev-9",
        runtime="ollama",
        quantization="Q4_K_M",
        limits=ModelLimits(context_tokens=8192),
        provenance=ModelProvenance("ollama", "cal:1", "verified"),
    )
    vault = ProfileVault(tmp_path)
    profile = _profile()
    vault.append(profile, METH)

    lookup = ProfileKey.for_lookup(descriptor, hardware=dict(HARDWARE), methodology=METH, runtime_version="0.5.7")
    assert lookup.digest == ProfileKey.derive(profile, methodology=METH).digest
    assert vault.get(lookup) is not None
    stale = ProfileKey.for_lookup(descriptor, hardware=dict(HARDWARE), methodology=METH, runtime_version="0.9.9")
    assert vault.get(stale) is None  # runtime version is part of the key


def test_append_validates_profiles_methodologies_and_paths(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path)
    no_hw = _profile(with_hardware=False)
    with pytest.raises(ProfileVaultError, match="hardware snapshot"):
        vault.append(no_hw, METH)
    with pytest.raises(ProfileVaultError, match="pass os_id explicitly"):
        vault.append(_profile(hardware={"gpu": "metal"}), METH)
    with pytest.raises(TypeError, match="ModelPerformanceProfile"):
        vault.append("nope", METH)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="BenchmarkMethodology"):
        vault.append(_profile(), "nope")  # type: ignore[arg-type]
    with pytest.raises(ProfileVaultError, match="os_id disagrees"):
        vault.append(_profile(), METH, os_id="linux x86_64")
    with pytest.raises(ProfileVaultError, match="reserved"):
        ProfileKey(
            hardware_digest=_digest(HARDWARE),
            os_id="macOS arm64",
            model_digest="sha256:m1",
            quantization="unspecified",
            runtime_version=None,
            methodology_digest=METH.digest,
        )
    with pytest.raises(ProfileVaultError, match="must be a positive integer"):
        BenchmarkMethodology(name="studio-local", version=0)
    with pytest.raises(ProfileVaultError, match="calibration_schema must be"):
        BenchmarkMethodology(name="studio-local", version=1, calibration_schema=2)
    with pytest.raises(ProfileVaultError, match="non-empty path"):
        ProfileVault("   ")
    with pytest.raises(ProfileVaultError, match="positive integer"):
        ProfileVault(tmp_path, max_versions=0)
    with pytest.raises(TypeError, match="ProfileKey"):
        vault.get("not-a-key")  # type: ignore[arg-type]
    with pytest.raises(ProfileVaultError, match="positive integer"):
        vault.get(ProfileKey.derive(_profile(), methodology=METH), version=0)


def test_explicit_os_id_works_for_snapshots_without_os_fields(tmp_path: Path) -> None:
    profile = _profile(hardware={"gpu": "metal"})
    vault = ProfileVault(tmp_path)
    outcome = vault.append(profile, METH, os_id="unknown box")
    assert outcome.created
    key = ProfileKey.derive(profile, methodology=METH, os_id="unknown box")
    assert vault.get(key).profile.digest == profile.digest


def test_retention_trims_oldest_and_never_reuses_version_numbers(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path, max_versions=2)
    p1, p2, p3 = _profile(ttft=100.0), _profile(ttft=120.0), _profile(ttft=140.0)
    vault.append(p1, METH)
    vault.append(p2, METH)
    vault.append(p3, METH)

    key = ProfileKey.derive(p1, methodology=METH)
    assert vault.versions(key) == (2, 3)
    assert vault.get(key, 1) is None  # trimmed away
    summary = vault.keys()[0]
    assert (summary.first_version, summary.latest_version, summary.next_version, summary.trimmed_versions) == (2, 3, 4, 1)
    assert summary.retained == 2

    resurrected = vault.append(p1, METH)  # the dedup guard only knows the retained window
    assert resurrected.created is True and resurrected.record.version == 4
    assert vault.versions(key) == (3, 4)


def test_load_detects_tampering_corruption_and_foreign_files(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path)
    vault.append(_profile(), METH)
    vault.append(_profile(ttft=140.0), METH)
    path = next(tmp_path.glob("profiles-*.jsonl"))
    key = ProfileKey.derive(_profile(), methodology=METH)
    line_texts = path.read_text(encoding="utf-8").splitlines()

    def pristine() -> list[dict]:
        return [json.loads(text) for text in line_texts]

    # A plausible edit with stale line digest: caught by the per-line seal.
    stale = pristine()
    stale[2]["profile"]["failure_rate"] = 0.5
    _write(path, stale)
    with pytest.raises(ProfileStoreError, match="digest mismatch"):
        vault.get(key)

    # An edit that reseals its own line is caught one level deeper: the
    # profile mapping no longer hashes to its content-addressed digest.
    resealed = pristine()
    resealed[2]["profile"]["failure_rate"] = 0.5
    resealed[2]["record_digest"] = _digest({k: v for k, v in resealed[2].items() if k != "record_digest"})
    _write(path, resealed)
    with pytest.raises(ProfileStoreError, match="profile_digest"):
        vault.get(key)

    # A deep edit that also fixes the profile digest fails the full
    # recomputation of every aggregate from the stored samples.
    deep = pristine()
    tampered_profile = deep[2]["profile"]
    tampered_profile["samples"][0]["ttft_ms"] = 3.0
    deep[2]["profile_digest"] = _digest(tampered_profile)
    deep[2]["record_digest"] = _digest({k: v for k, v in deep[2].items() if k != "record_digest"})
    _write(path, deep)
    with pytest.raises(ProfileStoreError, match="recomputation"):
        vault.get(key)

    # Dropping the newest version breaks the header count reconciliation.
    _write(path, pristine()[:2])
    with pytest.raises(ProfileStoreError, match="header counts"):
        vault.get(key)

    # A renamed file is refused because the name must carry the key digest.
    _write(path, pristine())
    other_key = ProfileKey.derive(_profile(model_digest="sha256:zz"), methodology=METH)
    renamed = tmp_path / other_key.filename
    path.rename(renamed)
    with pytest.raises(ProfileStoreError, match="does not match its key digest"):
        vault.keys()
    renamed.unlink()

    (tmp_path / f"profiles-{'0' * 64}.jsonl").write_text("junk\n", encoding="utf-8")
    with pytest.raises(ProfileStoreError, match="not valid JSON"):
        vault.keys()
    (tmp_path / f"profiles-{'0' * 64}.jsonl").unlink()

    # With the key's file gone, append restarts history at the genesis
    # version rather than trusting leftover fragments; restoring the sealed
    # file resumes versioning, and record digests prove purely content-based.
    outcome = vault.append(_profile(ttft=160.0), METH)
    assert outcome.created is True and outcome.record.version == 1
    assert outcome.record.previous_digest == "sha256:genesis"
    path.unlink()
    _write(path, pristine())
    resumed = vault.append(_profile(ttft=180.0), METH)
    assert resumed.record.version == 3
    assert resumed.record.previous_digest == vault.get(key, 2).digest


def test_key_disagreement_after_full_digest_rewrite_is_refused(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path)
    vault.append(_profile(), METH)
    path = next(tmp_path.glob("profiles-*.jsonl"))
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[1])

    payload = {key: value for key, value in entry.items() if key != "record_digest"}
    profile_map = payload["profile"]
    profile_map["inputs"]["model_digest"] = "sha256:swapped"
    profile_map["inputs_digest"] = _digest(profile_map["inputs"])
    payload["profile_digest"] = _digest(profile_map)
    payload["record_digest"] = _digest(payload)  # reseal the line: the content is now fully self-consistent
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    _write(path, [header, payload])
    with pytest.raises(ProfileStoreError, match="no longer agrees with its key"):
        vault.get(ProfileKey.derive(_profile(), methodology=METH))


def test_records_and_keys_are_canonical_values() -> None:
    record = ProfileRecord(version=1, previous_digest="sha256:genesis", profile=_profile())
    assert set(record.to_mapping()) == {"version", "previous_digest", "profile", "profile_digest"}
    assert record.profile_digest == _profile().digest
    assert record.digest == _digest(record.to_mapping())
    with pytest.raises(ProfileVaultError, match="positive integer"):
        ProfileRecord(version=0, previous_digest="x", profile=_profile())
    with pytest.raises(TypeError, match="ModelPerformanceProfile"):
        ProfileRecord(version=1, previous_digest="x", profile="nope")  # type: ignore[arg-type]

    key = ProfileKey.derive(_profile(), methodology=METH)
    assert re.fullmatch(r"profiles-[0-9a-f]{64}\.jsonl", key.filename)
    assert ProfileKey.from_mapping(key.to_mapping()) == key
    alt = BenchmarkMethodology(name="studio-local", version=2)
    assert ProfileKey.derive(_profile(), methodology=alt).digest != key.digest

    with pytest.raises(ProfileVaultError, match="schema_version"):
        ProfileKey.from_mapping({**key.to_mapping(), "schema_version": 9})
    with pytest.raises(ProfileVaultError, match="missing"):
        ProfileKey.from_mapping({"schema_version": 1})
    with pytest.raises(ProfileVaultError, match="schema_version"):
        BenchmarkMethodology.from_mapping({"schema_version": 9, "name": "x", "version": 1, "calibration_schema": 1})
    with pytest.raises(TypeError):
        os_id_from_hardware("nope")
    with pytest.raises(ProfileVaultError, match="pass os_id explicitly"):
        os_id_from_hardware({"gpu": "metal"})
    assert os_id_from_hardware(HARDWARE) == "macOS arm64"

    meth = BenchmarkMethodology.from_mapping(METH.to_mapping())
    assert meth.digest == METH.digest


def test_empty_vault_lookups_are_quiet(tmp_path: Path) -> None:
    vault = ProfileVault(tmp_path / "not-created-yet")
    key = ProfileKey.derive(_profile(), methodology=METH)
    assert vault.get(key) is None
    assert vault.versions(key) == ()
    assert vault.keys() == ()
    assert vault.latest_for_profile(_profile(), METH) is None
