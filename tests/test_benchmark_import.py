from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import benchmark_import as bi
from benchmark_publication import BenchmarkPublicationConfig, build_publication_bundle
from model_calibration import CalibrationInputs, CalibrationSample, calibrate, save_profile
from performance_profiles import BenchmarkMethodology, ProfileVault

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_import.py"
PUBLISHER = ROOT / "scripts" / "publish_benchmark_results.py"
HARDWARE = {"schema_version": 1, "operating_system": "Linux", "architecture": "x86_64"}
OTHER_HARDWARE = {"schema_version": 1, "operating_system": "macOS", "architecture": "arm64"}
PROMPT_DIGEST = "sha256:" + "ef" * 32


def _profile(model_id: str, letter: str, *, hardware: dict | None = None, warm: int = 2, cold: int = 1):
    inputs = CalibrationInputs(
        model_id=model_id,
        model_digest="sha256:" + letter * 64,
        model_revision="rev-1",
        quantization="Q4",
        runtime="fixture-runtime",
        runtime_version="1.0",
        context_length=2048,
        prompt_set_digest=PROMPT_DIGEST,
        hardware=HARDWARE if hardware is None else hardware,
    )
    samples = [
        CalibrationSample(sequence=index + 1, kind="warm", ok=True, decode_tokens_per_s=20 + index, peak_memory_bytes=1024)
        for index in range(warm)
    ]
    samples += [
        CalibrationSample(sequence=warm + index + 1, kind="cold", ok=True, decode_tokens_per_s=10, peak_memory_bytes=1024)
        for index in range(cold)
    ]
    return calibrate(inputs, tuple(samples))


def _config() -> BenchmarkPublicationConfig:
    return BenchmarkPublicationConfig(
        methodology=BenchmarkMethodology("import-test", 1),
        warmup_runs=1,
        warm_repeats=2,
        cold_repeats=1,
        caveats=("Synthetic fixture only; not a live benchmark.",),
    )


def _bundle(tmp_path: Path, *, hardware: dict | None = None, models: tuple[tuple[str, str], ...] = (("model-a", "a"), ("model-b", "b"))):
    paths = []
    for model_id, letter in models:
        path = tmp_path / f"{model_id}.jsonl"
        save_profile(path, _profile(model_id, letter, hardware=hardware))
        paths.append(path)
    return build_publication_bundle(paths, _config(), publisher_script=PUBLISHER)


def _envelope(tmp_path: Path, **overrides: Any) -> dict:
    bundle = overrides.pop("bundle", None) or _bundle(tmp_path)
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "license": {"id": "CC-BY-4.0", "url": "https://example.invalid/license", "attribution": "Example Lab"},
        "contributor": {"name": "Example Lab", "url": "https://example.invalid"},
        "hardware": HARDWARE,
        "bundle": bundle.to_mapping(),
    }
    envelope.update(overrides)
    return envelope


def _write(tmp_path: Path, envelope: dict, name: str = "contribution.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def _contribution(tmp_path: Path, **overrides: Any) -> bi.Contribution:
    return bi.validate_envelope(_envelope(tmp_path, **overrides))


# ---------------------------------------------------------------------------
# License
# ---------------------------------------------------------------------------


def test_license_requires_attribution_when_the_license_demands_it() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="requires an attribution"):
        bi.License(id="CC-BY-4.0")


def test_cc0_does_not_require_attribution() -> None:
    assert bi.License(id="CC0-1.0").to_mapping()["attribution"] == ""


def test_license_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="not accepted"):
        bi.License(id="Proprietary-EULA", attribution="Someone")


def test_license_url_must_be_http() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="http"):
        bi.License(id="CC0-1.0", url="file:///etc/passwd")


def test_license_rejects_unknown_fields() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="unsupported fields"):
        bi.License.from_mapping({"id": "CC0-1.0", "royalty": "1%"})


def test_license_id_is_required() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="license.id is required"):
        bi.License.from_mapping({})


# ---------------------------------------------------------------------------
# Contributor
# ---------------------------------------------------------------------------


def test_contributor_is_a_name_and_optional_url() -> None:
    contributor = bi.Contributor.from_mapping({"name": "  Example   Lab ", "url": "https://example.invalid"})

    assert contributor.to_mapping() == {"name": "Example Lab", "url": "https://example.invalid"}


def test_contributor_rejects_contact_details() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="unsupported fields"):
        bi.Contributor.from_mapping({"name": "Someone", "email": "someone@example.invalid"})


def test_contributor_name_must_not_be_blank_or_control_characters() -> None:
    with pytest.raises(bi.BenchmarkImportError, match="must not be empty"):
        bi.Contributor(name="   ")
    with pytest.raises(bi.BenchmarkImportError, match="control characters"):
        bi.Contributor(name="bad\x07name")


# ---------------------------------------------------------------------------
# Envelope schema
# ---------------------------------------------------------------------------


def test_a_valid_envelope_is_accepted_and_stays_untrusted(tmp_path: Path) -> None:
    contribution = _contribution(tmp_path)

    assert contribution.trust == bi.TRUST_UNTRUSTED
    assert [profile.model_id for profile in contribution.profiles] == ["model-a", "model-b"]
    assert all(profile.trust == bi.TRUST_UNTRUSTED for profile in contribution.profiles)
    assert contribution.os_id == "Linux x86_64"
    assert contribution.license.id == "CC-BY-4.0"
    assert contribution.envelope_digest.startswith("sha256:")


def test_envelope_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["signature"] = "trust me"

    with pytest.raises(bi.BenchmarkImportError, match="unsupported fields"):
        bi.validate_envelope(envelope)


def test_envelope_rejects_an_unsupported_schema_version(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["schema_version"] = 2

    with pytest.raises(bi.BenchmarkImportError, match="schema_version"):
        bi.validate_envelope(envelope)


def test_envelope_requires_a_hardware_snapshot(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["hardware"] = "M5 24GB"

    with pytest.raises(bi.BenchmarkImportError, match="hardware must be an object"):
        bi.validate_envelope(envelope)


def test_hardware_with_private_metadata_is_refused(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["hardware"] = {**HARDWARE, "serial_number": "C02XYZ"}

    with pytest.raises(bi.BenchmarkImportError, match="not acceptable for import"):
        bi.validate_envelope(envelope)


def test_contribution_must_be_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(bi.BenchmarkImportError, match="not UTF-8 JSON"):
        bi.load_contribution(path)


def test_missing_contribution_is_reported(tmp_path: Path) -> None:
    with pytest.raises(bi.BenchmarkImportError, match="unavailable"):
        bi.load_contribution(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_a_tampered_bundle_digest_is_detected(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"]["bundle_digest"] = "sha256:" + "0" * 64

    with pytest.raises(bi.BenchmarkImportError, match="provenance is invalid"):
        bi.validate_envelope(envelope)


def test_a_tampered_raw_profile_is_detected(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"]["bundle_digest"] = None
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    profile = envelope["bundle"]["profiles"][0]
    raw = profile["raw_profile_jsonl"]
    mutated = raw[:9] + ("X" if raw[9] != "X" else "Y") + raw[10:]
    assert mutated != raw
    profile["raw_profile_jsonl"] = mutated
    envelope["bundle"]["bundle_digest"] = _digest({key: value for key, value in envelope["bundle"].items()})

    with pytest.raises(bi.BenchmarkImportError, match="raw JSONL does not match its raw_sha256"):
        bi.validate_envelope(envelope)


def test_a_profile_without_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["profiles"][0]["raw_profile_jsonl"] = (
        envelope["bundle"]["profiles"][0]["raw_profile_jsonl"].rstrip("\n")
    )
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="must end with a newline"):
        bi.validate_envelope(envelope)


def test_config_digest_must_match_the_config(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["config_digest"] = "sha256:" + "1" * 64
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="does not hash to its config_digest"):
        bi.validate_envelope(envelope)


def test_methodology_digest_must_match_the_config(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["context"]["methodology_digest"] = "sha256:" + "2" * 64
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="methodology does not hash"):
        bi.validate_envelope(envelope)


def test_profiles_must_not_repeat_a_model_identity(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["profiles"].append(dict(envelope["bundle"]["profiles"][0]))
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="repeats a model identity"):
        bi.validate_envelope(envelope)


def test_model_identity_must_match_the_raw_profile(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["profiles"][0]["model_id"] = "model-z"
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="model_id disagrees"):
        bi.validate_envelope(envelope)


# ---------------------------------------------------------------------------
# Hardware identity
# ---------------------------------------------------------------------------


def test_claimed_hardware_must_match_the_published_context(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["hardware"] = OTHER_HARDWARE

    with pytest.raises(bi.BenchmarkImportError, match="does not hash to the hardware_digest"):
        bi.validate_envelope(envelope)


def test_profiles_must_be_measured_on_the_claimed_machine(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, hardware=OTHER_HARDWARE)
    envelope = _envelope(tmp_path, bundle=bundle, hardware=HARDWARE)

    with pytest.raises(bi.BenchmarkImportError, match="does not hash to the hardware_digest"):
        bi.validate_envelope(envelope)


def test_os_id_must_agree_with_the_hardware_snapshot(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["context"]["os_id"] = "Windows amd64"
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="disagrees with the claimed hardware"):
        bi.validate_envelope(envelope)


def test_sample_counts_must_match_the_published_plan(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["bundle"].pop("bundle_digest")
    from benchmark_publication import _digest

    envelope["bundle"]["config"]["calibration_plan"]["warm_repeats"] = 9
    envelope["bundle"]["config_digest"] = _digest(dict(envelope["bundle"]["config"]))
    envelope["bundle"]["bundle_digest"] = _digest(dict(envelope["bundle"]))

    with pytest.raises(bi.BenchmarkImportError, match="sample counts do not match"):
        bi.validate_envelope(envelope)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path) -> bi.TrustRegistry:
    return bi.TrustRegistry(tmp_path / "registry")


def test_import_records_every_profile_as_untrusted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    states = registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")

    assert {state.trust for state in states} == {bi.TRUST_UNTRUSTED}
    assert registry.path.is_file()
    stored = json.loads(registry.path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["imported_at"] == "2026-10-29T00:00:00Z"
    assert {profile["trust"] for profile in stored["profiles"]} == {bi.TRUST_UNTRUSTED}


def test_import_is_idempotent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    contribution = _contribution(tmp_path)
    registry.import_contribution(contribution, imported_at="2026-10-29T00:00:00Z")
    before = registry.path.read_text(encoding="utf-8")

    registry.import_contribution(contribution, imported_at="2026-10-30T00:00:00Z")

    assert registry.path.read_text(encoding="utf-8") == before


def test_two_contributions_can_coexist(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")
    second = _bundle(tmp_path / "second", models=(("model-c", "c"),))
    registry.import_contribution(_contribution(tmp_path, bundle=second), imported_at="2026-10-29T00:00:00Z")

    assert {state.model_id for state in registry.states()} == {"model-a", "model-b", "model-c"}


def test_an_empty_registry_reports_nothing(tmp_path: Path) -> None:
    assert _registry(tmp_path).states() == ()
    assert _registry(tmp_path).licence_notice() == ""


def test_licence_notice_collects_attribution(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")

    assert registry.licence_notice() == "- Example Lab (CC-BY-4.0)"


def test_a_tampered_registry_fails_loudly(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")
    text = registry.path.read_text(encoding="utf-8")
    registry.path.write_text(text.replace('"trust":"untrusted"', '"trust":"locally-verified"'), encoding="utf-8")

    with pytest.raises(bi.BenchmarkImportError, match="digest mismatch"):
        registry.states()


def test_an_unknown_registry_record_kind_is_refused(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")
    record = json.loads(registry.path.read_text(encoding="utf-8").splitlines()[0])
    record.pop("record_digest")
    record["kind"] = "blessing"
    from benchmark_import import _digest

    registry.path.write_text(
        json.dumps({**record, "record_digest": _digest(record)}, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(bi.BenchmarkImportError, match="unknown record kind"):
        registry.states()


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_promotion_requires_a_local_measurement(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    states = registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")
    vault = ProfileVault(tmp_path / "vault")

    with pytest.raises(bi.ImportTrustError, match="no local measurement"):
        registry.promote(states[0].profile_digest, vault=vault)


def test_promotion_rejects_a_different_local_measurement(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    contribution = _contribution(tmp_path)
    states = {
        state.model_id: state
        for state in registry.import_contribution(contribution, imported_at="2026-10-29T00:00:00Z")
    }
    vault = ProfileVault(tmp_path / "vault")
    # Same model and hardware, different measured numbers: the key matches but the
    # profile digest does not, so promotion must refuse rather than guess.
    save_profile(tmp_path / "other-a.jsonl", _profile("model-a", "a", warm=3))
    from model_calibration import load_profile

    vault.append(load_profile(tmp_path / "other-a.jsonl"), _config().methodology)

    with pytest.raises(bi.ImportTrustError, match="differs from the imported profile"):
        registry.promote(states["model-a"].profile_digest, vault=vault)


def test_promotion_succeeds_against_a_matching_local_measurement(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    contribution = _contribution(tmp_path)
    states = {
        state.model_id: state
        for state in registry.import_contribution(contribution, imported_at="2026-10-29T00:00:00Z")
    }
    vault = ProfileVault(tmp_path / "vault")
    save_profile(tmp_path / "local-a.jsonl", _profile("model-a", "a"))
    from model_calibration import load_profile

    vault.append(load_profile(tmp_path / "local-a.jsonl"), _config().methodology)

    promoted = registry.promote(
        states["model-a"].profile_digest, vault=vault, promoted_at="2026-10-29T00:00:00Z"
    )

    assert promoted.trust == bi.TRUST_LOCAL
    assert promoted.is_trusted is True
    assert promoted.evidence_digest.startswith("sha256:")
    reloaded = bi.TrustRegistry(tmp_path / "registry").states()
    assert {state.model_id: state.trust for state in reloaded} == {
        "model-a": bi.TRUST_LOCAL,
        "model-b": bi.TRUST_UNTRUSTED,
    }


def test_promotion_of_an_unknown_profile_is_refused(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")

    with pytest.raises(bi.ImportTrustError, match="has not been imported"):
        registry.promote("sha256:" + "9" * 64, vault=ProfileVault(tmp_path / "vault"))


def test_promotion_is_idempotent(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    states = {
        state.model_id: state
        for state in registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")
    }
    vault = ProfileVault(tmp_path / "vault")
    save_profile(tmp_path / "local-a.jsonl", _profile("model-a", "a"))
    from model_calibration import load_profile

    vault.append(load_profile(tmp_path / "local-a.jsonl"), _config().methodology)
    first = registry.promote(states["model-a"].profile_digest, vault=vault, promoted_at="2026-10-29T00:00:00Z")
    lines = registry.path.read_text(encoding="utf-8").count("\n")
    second = registry.promote(states["model-a"].profile_digest, vault=vault, promoted_at="2026-10-31T00:00:00Z")

    assert first == second
    assert registry.path.read_text(encoding="utf-8").count("\n") == lines


def test_promotion_requires_a_vault(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    states = registry.import_contribution(_contribution(tmp_path), imported_at="2026-10-29T00:00:00Z")

    with pytest.raises(TypeError, match="ProfileVault"):
        registry.promote(states[0].profile_digest, vault=str(tmp_path))  # type: ignore[arg-type]


def test_imported_key_round_trips(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    contribution = _contribution(tmp_path)
    registry.import_contribution(contribution, imported_at="2026-10-29T00:00:00Z")
    profile = contribution.profiles[0]

    key = registry.imported_key(profile.profile_digest)

    assert key is not None
    assert key.digest == profile.profile_key.digest
    assert registry.imported_key("sha256:" + "8" * 64) is None


# ---------------------------------------------------------------------------
# Authoring an envelope
# ---------------------------------------------------------------------------


def test_build_envelope_composes_a_valid_contribution(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    envelope = bi.build_envelope(
        bundle.to_mapping(),
        hardware=HARDWARE,
        license=bi.License(id="CC0-1.0"),
        contributor=bi.Contributor(name="Example Lab"),
    )

    contribution = bi.validate_envelope(envelope)
    assert contribution.license.id == "CC0-1.0"
    assert contribution.trust == bi.TRUST_UNTRUSTED


def test_build_envelope_refuses_hardware_that_did_not_produce_the_bundle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    with pytest.raises(bi.BenchmarkImportError, match="does not hash to the hardware_digest"):
        bi.build_envelope(
            bundle.to_mapping(),
            hardware=OTHER_HARDWARE,
            license=bi.License(id="CC0-1.0"),
            contributor=bi.Contributor(name="Example Lab"),
        )


def test_build_envelope_refuses_an_unverified_bundle(tmp_path: Path) -> None:
    payload = _bundle(tmp_path).to_mapping()
    payload["bundle_digest"] = "sha256:" + "0" * 64

    with pytest.raises(bi.BenchmarkImportError, match="provenance is invalid"):
        bi.build_envelope(
            payload,
            hardware=HARDWARE,
            license=bi.License(id="CC0-1.0"),
            contributor=bi.Contributor(name="Example Lab"),
        )


def test_write_envelope_refuses_an_invalid_contribution(tmp_path: Path) -> None:
    with pytest.raises(bi.BenchmarkImportError):
        bi.write_envelope(tmp_path / "out.json", {"schema_version": 1})

    assert not (tmp_path / "out.json").exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=180, cwd=ROOT
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    contribution = _write(tmp_path, _envelope(tmp_path))
    vault = ProfileVault(tmp_path / "vault")
    save_profile(tmp_path / "local-a.jsonl", _profile("model-a", "a"))
    from model_calibration import load_profile

    vault.append(load_profile(tmp_path / "local-a.jsonl"), _config().methodology)
    return contribution, vault.directory


def test_cli_validate_reports_untrusted_profiles(tmp_path: Path) -> None:
    contribution, _ = _fixture(tmp_path)

    result = _cli("validate", "--contribution", str(contribution))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["trust"] == "untrusted"
    assert {profile["trust"] for profile in payload["profiles"]} == {"untrusted"}


def test_cli_import_then_list_then_promote(tmp_path: Path, capsys) -> None:
    contribution, vault = _fixture(tmp_path)
    registry = tmp_path / "registry"

    imported = _cli("import", "--contribution", str(contribution), "--registry", str(registry))
    assert imported.returncode == 0, imported.stderr
    digest = next(
        entry["profile_digest"] for entry in json.loads(imported.stdout)["profiles"] if entry["model_id"] == "model-a"
    )

    listed = _cli("list", "--registry", str(registry), "--json")
    assert listed.returncode == 0, listed.stderr
    assert {entry["trust"] for entry in json.loads(listed.stdout)["profiles"]} == {"untrusted"}

    promoted = _cli(
        "promote",
        "--registry",
        str(registry),
        "--profile-digest",
        digest,
        "--vault",
        str(vault),
    )
    assert promoted.returncode == 0, promoted.stderr
    assert json.loads(promoted.stdout)["trust"] == "locally-verified"


def test_cli_wrap_writes_a_contribution_a_second_party_can_validate(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle.to_mapping()), encoding="utf-8")
    hardware_path = tmp_path / "hardware.json"
    hardware_path.write_text(json.dumps(HARDWARE), encoding="utf-8")
    out = tmp_path / "wrapped.json"

    wrapped = _cli(
        "wrap",
        "--bundle",
        str(bundle_path),
        "--hardware",
        str(hardware_path),
        "--license",
        "CC-BY-4.0",
        "--attribution",
        "Example Lab",
        "--contributor",
        "Example Lab",
        "--out",
        str(out),
    )

    assert wrapped.returncode == 0, wrapped.stderr
    validated = _cli("validate", "--contribution", str(out))
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["license"]["id"] == "CC-BY-4.0"


def test_cli_wrap_refuses_mismatched_hardware(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_bundle(tmp_path).to_mapping()), encoding="utf-8")
    hardware_path = tmp_path / "hardware.json"
    hardware_path.write_text(json.dumps(OTHER_HARDWARE), encoding="utf-8")

    result = _cli(
        "wrap",
        "--bundle",
        str(bundle_path),
        "--hardware",
        str(hardware_path),
        "--license",
        "CC0-1.0",
        "--contributor",
        "Example Lab",
        "--out",
        str(tmp_path / "wrapped.json"),
    )

    assert result.returncode == 1
    assert "benchmark import:" in result.stderr


def test_cli_list_without_a_registry_is_empty(tmp_path: Path) -> None:
    result = _cli("list", "--registry", str(tmp_path / "absent"))

    assert result.returncode == 0
    assert "No imported benchmarks" in result.stdout


def test_cli_rejects_an_invalid_contribution(tmp_path: Path) -> None:
    envelope = _envelope(tmp_path)
    envelope["license"] = {"id": "All-Rights-Reserved"}
    path = _write(tmp_path, envelope, "bad.json")

    result = _cli("validate", "--contribution", str(path))

    assert result.returncode == 1
    assert "benchmark import:" in result.stderr


def test_cli_promote_without_evidence_fails(tmp_path: Path) -> None:
    contribution, _ = _fixture(tmp_path)
    registry = tmp_path / "registry"
    imported = _cli("import", "--contribution", str(contribution), "--registry", str(registry))
    digest = next(
        entry["profile_digest"] for entry in json.loads(imported.stdout)["profiles"] if entry["model_id"] == "model-a"
    )

    result = _cli(
        "promote", "--registry", str(registry), "--profile-digest", digest, "--vault", str(tmp_path / "empty-vault")
    )

    assert result.returncode == 1
    assert "no local measurement" in result.stderr
