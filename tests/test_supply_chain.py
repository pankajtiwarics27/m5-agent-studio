from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import supply_chain as sc

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_supply_chain.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("release_supply_chain", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(directory: Path, payload: str = "print('hi')\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "studio.py"
    target.write_text(payload, encoding="utf-8")
    return target


def _components() -> tuple[sc.Component, ...]:
    return (
        sc.Component(name="fastapi", version="0.162.0", license="MIT"),
        sc.Component(name="httpx", version="0.28.1", license="BSD-3-Clause"),
    )


class _Signer:
    """A deterministic stand-in for a real key-holding signer."""

    name = "test-signer"

    def __init__(self, *, verifies: bool = True, signature: str = "c2ln") -> None:
        self.verifies = verifies
        self.signature = signature
        self.signed: list[str] = []

    def sign(self, digest_hex: str) -> str:
        self.signed.append(digest_hex)
        return self.signature

    def verify(self, digest_hex: str, signature: str) -> bool:
        return self.verifies and signature == self.signature


# ---------------------------------------------------------------------------
# Subject and hashing
# ---------------------------------------------------------------------------


def test_subject_matches_sha256_of_the_artifact(tmp_path: Path) -> None:
    import hashlib

    artifact = _artifact(tmp_path, "payload\n")
    subject = sc.subject_from_file(artifact)

    assert subject.name == "studio.py"
    assert subject.sha256 == hashlib.sha256(b"payload\n").hexdigest()
    assert subject.size_bytes == len(b"payload\n")
    assert subject.digest == {"sha256": subject.sha256}


def test_subject_for_a_missing_artifact_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(sc.SupplyChainError, match="Artifact not found"):
        sc.subject_from_file(tmp_path / "absent.py")


def test_subject_rejects_a_non_sha256_digest() -> None:
    with pytest.raises(sc.SupplyChainError, match="64 lowercase hex"):
        sc.Subject(name="studio.py", sha256="deadbeef", size_bytes=1)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


def test_components_are_sorted_and_resolved_from_metadata() -> None:
    components = sc.collect_components(("httpx", "fastapi"))
    names = [component.name for component in components]

    assert names == sorted(names, key=str.lower)
    assert names == ["fastapi", "httpx"]
    assert all(component.version for component in components)


def test_component_may_be_described_without_the_real_environment() -> None:
    described = {"fastapi": sc.Component(name="fastapi", version="1.2.3", license="MIT")}
    components = sc.collect_components(("fastapi", "absent"), version_of=described.get)

    assert components == (sc.Component(name="fastapi", version="1.2.3", license="MIT"),)


def test_a_missing_dependency_is_an_error_not_an_omission() -> None:
    with pytest.raises(sc.SupplyChainError, match="not installed"):
        sc.collect_components(("m5-agent-studio-definitely-not-installed",))


def test_requirement_names_normalise_and_skip_extras() -> None:
    assert sc.normalize_name("Typing_Extensions") == "typing-extensions"
    assert sc.normalize_name("jaraco.classes") == "jaraco-classes"
    assert sc._requirement_name("click>=8.0") == "click"
    assert sc._requirement_name("h11 (>=0.8)") == "h11"
    assert sc._requirement_name('httpx; extra == "cli"') == ""
    assert sc._requirement_name("") == ""


def test_runtime_closure_includes_transitive_distributions() -> None:
    closure = sc.runtime_closure(("fastapi",))

    assert "fastapi" in closure
    assert "starlette" in closure
    assert len(closure) > 1
    assert len(set(closure)) == len(closure)


def test_runtime_closure_tolerates_an_uninstalled_root() -> None:
    assert sc.runtime_closure(("m5-agent-studio-definitely-not-installed",)) == ()


# ---------------------------------------------------------------------------
# SBOM
# ---------------------------------------------------------------------------


def test_sbom_is_a_cyclonedx_document_about_the_artifact(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))
    document = sc.build_sbom(_components(), subject, created_at="2026-10-29T00:00:00Z")

    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.5"
    assert document["version"] == 1
    assert document["metadata"]["timestamp"] == "2026-10-29T00:00:00Z"
    assert document["metadata"]["component"]["name"] == "studio.py"
    assert document["metadata"]["component"]["hashes"] == [{"alg": "SHA-256", "content": subject.sha256}]
    assert [item["name"] for item in document["components"]] == ["fastapi", "httpx"]


def test_sbom_component_entries_carry_purl_and_licence() -> None:
    entry = sc.Component(name="fastapi", version="0.162.0", license="MIT").to_sbom()

    assert entry["type"] == "library"
    assert entry["purl"] == "pkg:pypi/fastapi@0.162.0"
    assert entry["licenses"] == [{"license": {"id": "MIT"}}]


def test_free_text_licence_labels_are_names_not_fake_spdx_ids() -> None:
    entry = sc.Component(name="fastapi", version="0.162.0", license="Apache Software License").to_sbom()

    assert entry["licenses"] == [{"license": {"name": "Apache Software License"}}]


def test_sbom_omits_the_timestamp_when_it_is_not_supplied(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))
    document = sc.build_sbom(_components(), subject)

    assert "timestamp" not in document["metadata"]
    assert sc.render_json(document) == sc.render_json(sc.build_sbom(_components(), subject))


def test_rendered_json_is_key_sorted_and_newline_terminated() -> None:
    rendered = sc.render_json({"b": 1, "a": 2})

    assert rendered == '{\n  "a": 2,\n  "b": 1\n}\n'


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _context(**overrides) -> sc.BuildContext:
    fields = {"source_commit": "a" * 40, "version": "9.0.1"}
    fields.update(overrides)
    return sc.BuildContext(**fields)


def test_provenance_binds_the_artifact_digest_to_a_source_commit(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))
    statement = sc.build_provenance(subject, _context(), _components(), sbom_sha256="b" * 64)

    assert statement["_type"] == sc.STATEMENT_TYPE
    assert statement["predicateType"] == sc.PROVENANCE_TYPE
    assert statement["subject"] == [{"name": "studio.py", "digest": {"sha256": subject.sha256}}]
    external = statement["predicate"]["buildDefinition"]["externalParameters"]
    assert external["version"] == "9.0.1"
    assert external["sourceCommit"] == "a" * 40
    assert external["sbomSha256"] == "b" * 64
    assert statement["predicate"]["buildDefinition"]["buildType"] == sc.BUILD_TYPE
    assert [item["name"] for item in statement["predicate"]["buildDefinition"]["resolvedDependencies"]] == [
        "fastapi",
        "httpx",
    ]


def test_provenance_without_a_source_commit_is_refused(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))
    with pytest.raises(sc.SupplyChainError, match="source commit"):
        sc.build_provenance(subject, _context(source_commit=" "), _components())


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def test_secret_scan_passes_on_a_clean_artifact(tmp_path: Path) -> None:
    result = sc.scan_artifact_secrets(_artifact(tmp_path, "print('hello')\n"))

    assert result.name == "artifact-secrets"
    assert result.status == sc.STATUS_PASSED
    assert result.findings == ()


def test_secret_scan_reports_a_high_confidence_credential(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    result = sc.scan_artifact_secrets(artifact)

    assert result.status == sc.STATUS_FAILED
    assert result.findings
    assert "aws" in result.findings[0].identifier.lower()
    assert result.findings[0].scanner == "artifact-secrets"


def test_secret_scan_findings_do_not_leak_the_secret(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, 'KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    result = sc.scan_artifact_secrets(artifact)

    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(result.to_dict())


def test_vulnerability_scan_passes_on_an_clean_report() -> None:
    def runner(_components):
        return 0, json.dumps({"dependencies": [{"name": "fastapi", "version": "0.162.0", "vulns": []}]})

    result = sc.scan_dependencies(runner=runner, components=_components())

    assert result.status == sc.STATUS_PASSED
    assert "Scope: 2" in result.detail


def test_vulnerability_scan_fails_on_a_finding() -> None:
    def runner(_components):
        return 1, json.dumps(
            {
                "dependencies": [
                    {
                        "name": "fastapi",
                        "version": "0.162.0",
                        "vulns": [{"id": "PYSEC-2026-1", "description": "example"}],
                    }
                ]
            }
        )

    result = sc.scan_dependencies(runner=runner, components=_components())

    assert result.status == sc.STATUS_FAILED
    assert result.findings[0].identifier == "fastapi 0.162.0: PYSEC-2026-1"


def test_vulnerability_scan_ignores_build_only_tooling_out_of_scope() -> None:
    def runner(_components):
        return 1, json.dumps(
            {"dependencies": [{"name": "pip", "version": "23.0.1", "vulns": [{"id": "PYSEC-2023-228"}]}]}
        )

    result = sc.scan_dependencies(runner=runner, components=_components())

    assert result.status == sc.STATUS_PASSED
    assert "1 finding(s) in build-only tooling are out of scope" in result.detail


def test_unparseable_scanner_output_is_a_failure_not_a_pass() -> None:
    result = sc.scan_dependencies(runner=lambda _components: (0, "not json"), components=_components())

    assert result.status == sc.STATUS_FAILED
    assert result.findings[0].identifier == "unparseable-output"


def test_scanner_crash_is_a_failure() -> None:
    result = sc.scan_dependencies(runner=lambda _components: (3, "{}"), components=_components())

    assert result.status == sc.STATUS_FAILED
    assert result.findings[0].identifier == "scanner-error"


def test_scan_is_unavailable_when_pip_audit_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pip_audit", None)

    result = sc.scan_dependencies(components=_components())

    assert result.status == sc.STATUS_UNAVAILABLE
    assert "pip-audit is not installed" in result.detail


def test_a_scan_status_outside_the_contract_is_refused() -> None:
    with pytest.raises(sc.SupplyChainError, match="status"):
        sc.ScanResult("artifact-secrets", "probably-fine")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_unconfigured_signer_refuses_to_sign() -> None:
    signer = sc.resolve_signer({})

    assert isinstance(signer, sc.UnconfiguredSigner)
    assert signer.name == "unconfigured"
    with pytest.raises(sc.SigningError, match="No release signing key is configured"):
        signer.sign("a" * 64)
    with pytest.raises(sc.SigningError):
        signer.verify("a" * 64, "c2ln")


def test_signing_without_a_verify_command_is_refused() -> None:
    with pytest.raises(sc.SigningError, match="cannot be verified"):
        sc.resolve_signer({"STUDIO_RELEASE_SIGN_COMMAND": "/usr/bin/sign"})


def test_configured_signer_uses_argv_not_a_shell() -> None:
    signer = sc.resolve_signer(
        {
            "STUDIO_RELEASE_SIGN_COMMAND": "/usr/bin/cosign sign-blob --yes",
            "STUDIO_RELEASE_VERIFY_COMMAND": "/usr/bin/cosign verify-blob",
        }
    )

    assert isinstance(signer, sc.CommandSigner)
    assert signer.name == "cosign"
    assert signer.sign_argv == ("/usr/bin/cosign", "sign-blob", "--yes")
    assert signer.verify_argv == ("/usr/bin/cosign", "verify-blob")


def test_command_signer_round_trips_through_an_external_tool(tmp_path: Path) -> None:
    script = tmp_path / "stub_sign.py"
    script.write_text(
        "import sys\nprint('stub-' + open(sys.argv[-1]).read().strip())\n",
        encoding="utf-8",
    )
    verify = tmp_path / "stub_verify.py"
    verify.write_text(
        "import sys\n"
        "digest = open(sys.argv[-2]).read().strip()\n"
        "signature = open(sys.argv[-1]).read().strip()\n"
        "raise SystemExit(0 if signature == 'stub-' + digest else 1)\n",
        encoding="utf-8",
    )
    signer = sc.CommandSigner(
        name="stub",
        sign_argv=(sys.executable, str(script)),
        verify_argv=(sys.executable, str(verify)),
    )

    signature = signer.sign("b" * 64)

    assert signature == "stub-" + "b" * 64
    assert signer.verify("b" * 64, signature) is True
    assert signer.verify("c" * 64, signature) is False


def test_command_signer_failure_is_never_recorded_as_a_signature(tmp_path: Path) -> None:
    script = tmp_path / "failing.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    signer = sc.CommandSigner(name="failing", sign_argv=(sys.executable, str(script)))

    with pytest.raises(sc.SigningError, match="exited 2"):
        signer.sign("b" * 64)


def test_command_signer_with_empty_output_is_refused(tmp_path: Path) -> None:
    script = tmp_path / "silent.py"
    script.write_text("pass\n", encoding="utf-8")
    signer = sc.CommandSigner(name="silent", sign_argv=(sys.executable, str(script)))

    with pytest.raises(sc.SigningError, match="empty signature"):
        signer.sign("b" * 64)


def test_missing_signer_binary_is_reported(tmp_path: Path) -> None:
    signer = sc.CommandSigner(name="absent", sign_argv=(str(tmp_path / "no-such-tool"),))

    with pytest.raises(sc.SigningError, match="could not run"):
        signer.sign("b" * 64)


def test_sign_release_records_the_digest_it_signed(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))
    record = sc.sign_release(subject, signer=_Signer())

    assert record.digest_hex == subject.sha256
    assert record.signer == "test-signer"
    assert record.to_dict() == {"digest": f"sha256:{subject.sha256}", "signature": "c2ln", "signer": "test-signer"}


def test_sign_release_refuses_a_signature_that_does_not_verify(tmp_path: Path) -> None:
    subject = sc.subject_from_file(_artifact(tmp_path))

    with pytest.raises(sc.SigningError, match="did not verify"):
        sc.sign_release(subject, signer=_Signer(verifies=False))


def test_signature_record_validation() -> None:
    with pytest.raises(sc.SigningError, match="hex digest"):
        sc.SignatureRecord(digest_hex="abc", signature="c2ln", signer="s")
    with pytest.raises(sc.SigningError, match="must not be empty"):
        sc.SignatureRecord(digest_hex="a" * 64, signature="  ", signer="s")
    with pytest.raises(sc.SigningError, match="missing field"):
        sc.SignatureRecord.from_dict({"digest": "sha256:" + "a" * 64})
    with pytest.raises(sc.SigningError, match="prefixed"):
        sc.SignatureRecord.from_dict({"digest": "a" * 64, "signature": "c2ln", "signer": "s"})


def test_signature_record_round_trips() -> None:
    record = sc.SignatureRecord(digest_hex="a" * 64, signature="c2ln", signer="s")

    assert sc.SignatureRecord.from_dict(record.to_dict()) == record


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _gate(**overrides) -> sc.GateResult:
    subject = sc.Subject(name="studio.py", sha256="a" * 64, size_bytes=10)
    provenance = sc.build_provenance(subject, _context(), _components(), sbom_sha256="b" * 64)
    arguments = {
        "subject": subject,
        "components": _components(),
        "scans": (sc.ScanResult("artifact-secrets", sc.STATUS_PASSED),),
        "provenance": provenance,
        "signature": sc.SignatureRecord(digest_hex=subject.sha256, signature="c2ln", signer="s"),
    }
    arguments.update(overrides)
    return sc.evaluate_gate(**arguments)


def test_gate_opens_only_when_every_piece_is_present() -> None:
    result = _gate()

    assert result.ready is True
    assert result.reasons == ()


def test_gate_stays_closed_without_a_signature() -> None:
    result = _gate(signature=None)

    assert result.ready is False
    assert "Artifact is not signed." in result.reasons


def test_gate_stays_closed_when_a_scan_could_not_run() -> None:
    skipped = sc.ScanResult("dependency-vulnerabilities", sc.STATUS_UNAVAILABLE, "pip-audit missing")
    result = _gate(scans=(sc.ScanResult("artifact-secrets", sc.STATUS_PASSED), skipped))

    assert result.ready is False
    assert any("did not run" in reason for reason in result.reasons)


def test_gate_stays_closed_when_a_scan_failed() -> None:
    finding = sc.ScanFinding("artifact-secrets", "aws-access-key", "line 1")
    failed = sc.ScanResult("artifact-secrets", sc.STATUS_FAILED, "1 credential", (finding,))
    result = _gate(scans=(failed,))

    assert result.ready is False
    assert any("failed" in reason for reason in result.reasons)


def test_gate_requires_scans_sbom_and_provenance() -> None:
    assert _gate(scans=()).ready is False
    assert _gate(components=()).ready is False
    assert _gate(provenance=None).ready is False


def test_gate_rejects_provenance_for_another_artifact() -> None:
    other = sc.Subject(name="studio.py", sha256="c" * 64, size_bytes=10)
    provenance = sc.build_provenance(other, _context(), _components())
    result = _gate(provenance=provenance)

    assert result.ready is False
    assert "Provenance does not match the artifact digest." in result.reasons


def test_gate_rejects_a_version_mismatch() -> None:
    result = _gate(expected_version="9.9.9")

    assert result.ready is False
    assert any("expected '9.9.9'" in reason for reason in result.reasons)


def test_gate_warns_when_provenance_does_not_bind_the_sbom() -> None:
    result = _gate()

    assert result.warnings == ()
    unbound = _gate(expected_version="9.0.1")
    assert unbound.ready is True


def test_gate_warns_about_an_unbound_sbom_digest() -> None:
    subject = sc.Subject(name="studio.py", sha256="a" * 64, size_bytes=10)
    provenance = sc.build_provenance(subject, _context(), _components())
    result = _gate(subject=subject, provenance=provenance)

    assert result.ready is True
    assert "Provenance does not bind the SBOM digest." in result.warnings


def test_gate_rejects_a_signature_over_a_different_digest() -> None:
    result = _gate(signature=sc.SignatureRecord(digest_hex="d" * 64, signature="c2ln", signer="s"))

    assert result.ready is False
    assert "Signature covers a different digest than the artifact." in result.reasons


def test_gate_result_serialises_its_reasons() -> None:
    payload = _gate(signature=None).to_dict()

    assert payload["ready"] is False
    assert payload["reasons"] == ["Artifact is not signed."]
    assert payload["warnings"] == []


# ---------------------------------------------------------------------------
# Release evidence CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli(monkeypatch: pytest.MonkeyPatch):
    module = _load_cli()
    clean = sc.ScanResult("dependency-vulnerabilities", sc.STATUS_PASSED, "no findings")
    monkeypatch.setattr(sc, "scan_dependencies", lambda **_kwargs: clean)
    return module


def _build(cli, tmp_path: Path, artifact: Path, *extra: str) -> Path:
    output = tmp_path / "evidence"
    code = cli.main(
        ["build", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact),
         "--source-commit", "a" * 40, "--deterministic", *extra]
    )
    assert code == 0
    return output


def test_cli_build_writes_all_evidence_files(cli, tmp_path: Path) -> None:
    output = _build(cli, tmp_path, _artifact(tmp_path))

    for name in ("sbom.cdx.json", "provenance.intoto.jsonl", "scans.json", "evidence.json"):
        assert (output / name).is_file()
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["version"] == "9.0.1"
    assert evidence["scans"] == {"artifact-secrets": "passed", "dependency-vulnerabilities": "passed"}
    assert evidence["signer"] == "unconfigured"
    assert evidence["gate"]["ready"] is False


def test_cli_build_is_reproducible(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    first = _build(cli, tmp_path / "one", artifact)
    second = _build(cli, tmp_path / "two", artifact)

    for name in ("sbom.cdx.json", "provenance.intoto.jsonl", "scans.json", "evidence.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_cli_verify_accepts_untampered_evidence(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)

    assert cli.main(["verify", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 0


def test_cli_verify_rejects_a_modified_artifact(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)
    artifact.write_text("print('tampered')\n", encoding="utf-8")

    assert cli.main(["verify", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 1


def test_cli_verify_rejects_a_modified_sbom(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)
    sbom_path = output / "sbom.cdx.json"
    sbom_path.write_text(sbom_path.read_text(encoding="utf-8").replace("CycloneDX", "CycloneDX "), encoding="utf-8")

    assert cli.main(["verify", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 1


def test_cli_verify_rejects_an_unavailable_scan(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)
    scans_path = output / "scans.json"
    scans = json.loads(scans_path.read_text(encoding="utf-8"))
    scans["scans"][1] = {"name": "dependency-vulnerabilities", "status": "unavailable", "detail": "missing", "findings": []}
    scans_path.write_text(json.dumps(scans), encoding="utf-8")

    assert cli.main(["verify", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 1


def test_cli_gate_stays_closed_for_an_unsigned_release(cli, tmp_path: Path, capsys) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)
    capsys.readouterr()

    assert cli.main(["gate", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 1
    assert json.loads(capsys.readouterr().out)["reasons"] == ["Artifact is not signed."]


def test_cli_gate_opens_for_a_signed_release(cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact(tmp_path)
    output = _build(cli, tmp_path, artifact)
    signature = sc.SignatureRecord(
        digest_hex=sc.subject_from_file(artifact).sha256, signature="c2ln", signer="test-signer"
    )
    (output / "signature.json").write_text(json.dumps(signature.to_dict()), encoding="utf-8")
    monkeypatch.setattr(sc, "resolve_signer", lambda *a, **k: _Signer())

    assert cli.main(["verify", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 0
    assert cli.main(["gate", "--version", "9.0.1", "--output", str(output), "--artifact", str(artifact)]) == 0


def test_cli_commands_fail_clearly_without_evidence(cli, tmp_path: Path) -> None:
    assert cli.main(["verify", "--version", "9.0.1", "--output", str(tmp_path / "absent")]) == 1
    assert cli.main(["gate", "--version", "9.0.1", "--output", str(tmp_path / "absent")]) == 1


def test_cli_defaults_to_the_pyproject_version(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    output = tmp_path / "evidence"

    code = cli.main(
        ["build", "--output", str(output), "--artifact", str(artifact), "--source-commit", "a" * 40,
         "--deterministic"]
    )

    assert code == 0
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["version"] == "9.0.1"


def test_cli_rejects_an_invalid_version(cli, tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)

    assert cli.main(["build", "--version", "v9", "--output", str(tmp_path / "e"), "--artifact", str(artifact)]) == 1
