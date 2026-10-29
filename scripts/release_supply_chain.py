#!/usr/bin/env python3
"""Produce and verify release evidence: SBOM, provenance, scans, signature (AS-042).

    python scripts/release_supply_chain.py build  --version 9.0.1 --output dist/evidence
    python scripts/release_supply_chain.py verify --version 9.0.1 --output dist/evidence
    python scripts/release_supply_chain.py gate   --version 9.0.1 --output dist/evidence

`build` writes an evidence directory next to the artifact:

    <output>/sbom.cdx.json           CycloneDX 1.5 inventory (deterministic order)
    <output>/provenance.intoto.jsonl in-toto/SLSA statement binding artifact -> build
    <output>/scans.json              every scan with an explicit status
    <output>/signature.json          detached signature over the artifact digest
    <output>/evidence.json           gate verdict and a summary of the above

The commands mean different things on purpose:

    build    produce evidence; exits 0 whenever evidence was written
    verify   the evidence still describes this artifact and the scans ran
    gate     the release decision: SBOM + provenance + scans + a real signature

A scan that cannot run is recorded as `unavailable` and a release is never
signed without a key, so `gate` stays closed until both are provisioned. Nothing
here needs the network, root, or a shell.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_manager as rm  # noqa: E402
import supply_chain as sc  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = PROJECT_ROOT / "studio.py"
DEFAULT_OUTPUT = PROJECT_ROOT / "dist" / "evidence"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise sc.SupplyChainError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise sc.SupplyChainError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise sc.SupplyChainError(f"{path} must contain a JSON object.")
    return raw


def _evidence_paths(output: Path) -> dict[str, Path]:
    return {
        "sbom": output / "sbom.cdx.json",
        "provenance": output / "provenance.intoto.jsonl",
        "scans": output / "scans.json",
        "signature": output / "signature.json",
        "evidence": output / "evidence.json",
    }


def _default_version() -> str:
    """Read the project version, so releases default to what was actually built."""
    import tomllib

    try:
        document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise sc.SupplyChainError(f"Cannot read the project version: {exc}") from exc
    return str(document.get("project", {}).get("version", ""))


def _release_version(version: str) -> str:
    """Validate the release version with the packaging module's own rule."""
    return rm.validate_version(version or _default_version())


def _components() -> tuple[sc.Component, ...]:
    return sc.collect_components(sc.runtime_closure())


def cmd_build(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact).expanduser()
    output = Path(args.output).expanduser()
    paths = _evidence_paths(output)
    version = _release_version(args.version)
    subject = sc.subject_from_file(artifact)
    components = _components()
    created_at = "" if args.deterministic else _now()

    sbom = sc.build_sbom(components, subject, created_at=created_at or None)
    _write(paths["sbom"], sc.render_json(sbom))
    sbom_digest = sc.hash_bytes(paths["sbom"].read_bytes())

    context = sc.BuildContext(
        source_commit=args.source_commit or _git_commit(),
        version=version,
        created_at=created_at,
    )
    provenance = sc.build_provenance(subject, context, components, sbom_sha256=sbom_digest)
    _write(paths["provenance"], sc.render_json(provenance))

    scans = (sc.scan_artifact_secrets(artifact), sc.scan_dependencies(components=components))
    _write(paths["scans"], sc.render_json({"artifact": subject.name, "scans": [scan.to_dict() for scan in scans]}))

    signer = sc.resolve_signer()
    signature = None
    signature_error = ""
    try:
        signature = sc.sign_release(subject, signer=signer)
    except sc.SigningError as exc:
        signature_error = str(exc)
    if signature is not None:
        _write(paths["signature"], sc.render_json(signature.to_dict()))
    else:
        paths["signature"].unlink(missing_ok=True)

    gate = sc.evaluate_gate(
        subject=subject,
        components=components,
        scans=scans,
        provenance=provenance,
        signature=signature,
        expected_version=version,
    )
    summary = {
        "version": version,
        "artifact": subject.name,
        "sha256": subject.sha256,
        "size_bytes": subject.size_bytes,
        "components": len(components),
        "sbom_sha256": sbom_digest,
        "source_commit": context.source_commit,
        "scans": {scan.name: scan.status for scan in scans},
        "signer": signer.name,
        "signature_error": signature_error,
        "gate": gate.to_dict(),
        "files": sorted(path.name for path in paths.values() if path.exists()),
    }
    _write(paths["evidence"], sc.render_json(summary))
    print(sc.render_json(summary), end="")
    return 0


def _load_evidence(output: Path) -> tuple[dict[str, Path], dict, dict, dict, dict | None]:
    paths = _evidence_paths(output)
    if not paths["evidence"].is_file():
        raise sc.SupplyChainError(f"No evidence directory at {output}; run the build command first.")
    evidence = _read_json(paths["evidence"])
    for name in ("sbom", "provenance", "scans"):
        if not paths[name].is_file():
            raise sc.SupplyChainError(f"Evidence file missing: {paths[name]}")
    signature_raw = _read_json(paths["signature"]) if paths["signature"].is_file() else None
    return paths, evidence, _read_json(paths["scans"]), _read_json(paths["provenance"]), signature_raw


def cmd_verify(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser()
    artifact = Path(args.artifact).expanduser() if args.artifact else ARTIFACT
    paths, evidence, scans, provenance, signature_raw = _load_evidence(output)
    problems: list[str] = []
    notes: list[str] = []

    subject = sc.subject_from_file(artifact)
    if subject.sha256 != evidence.get("sha256"):
        problems.append(
            f"Artifact digest {subject.sha256} does not match the recorded digest {evidence.get('sha256')}."
        )

    digests = {
        str(item.get("digest", {}).get("sha256", ""))
        for item in provenance.get("subject", [])
        if isinstance(item, dict)
    }
    if subject.sha256 not in digests:
        problems.append("Provenance subject does not match the artifact digest.")

    recorded_sbom = evidence.get("sbom_sha256")
    if not recorded_sbom:
        problems.append("Evidence does not record the SBOM digest.")
    elif sc.hash_bytes(paths["sbom"].read_bytes()) != recorded_sbom:
        problems.append("SBOM file has changed since the evidence was produced.")
    else:
        external = (
            provenance.get("predicate", {}).get("buildDefinition", {}).get("externalParameters", {})
            if isinstance(provenance.get("predicate"), dict)
            else {}
        )
        if external.get("sbomSha256") != recorded_sbom:
            problems.append("Provenance does not bind the recorded SBOM digest.")

    if signature_raw is None:
        notes.append("Artifact is unsigned; `gate` will refuse to mark this release ready.")
    else:
        try:
            signature = sc.SignatureRecord.from_dict(signature_raw)
        except sc.SigningError as exc:
            problems.append(f"Signature record is invalid: {exc}")
        else:
            if signature.digest_hex != subject.sha256:
                problems.append("Signature covers a different digest than the artifact.")
            else:
                signer = sc.resolve_signer()
                if signer.name != signature.signer:
                    problems.append(
                        f"Configured signer {signer.name!r} differs from the recorded signer {signature.signer!r}."
                    )
                try:
                    if not signer.verify(subject.sha256, signature.signature):
                        problems.append(f"Signature did not verify with signer {signer.name!r}.")
                except sc.SigningError as exc:
                    problems.append(f"Signature could not be verified: {exc}")

    for scan in scans.get("scans", []):
        status = scan.get("status")
        if status != sc.STATUS_PASSED:
            problems.append(f"Scan {scan.get('name')!r} is {status}: {scan.get('detail')}")

    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if problems:
        print("Release evidence verification failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print(json.dumps({"verified": True, "artifact": subject.name, "sha256": subject.sha256}, indent=2))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser()
    artifact = Path(args.artifact).expanduser() if args.artifact else ARTIFACT
    _, evidence, scans, provenance, signature_raw = _load_evidence(output)
    subject = sc.subject_from_file(artifact)
    try:
        signature = sc.SignatureRecord.from_dict(signature_raw) if signature_raw else None
    except sc.SigningError as exc:
        raise sc.SupplyChainError(f"Recorded signature is invalid: {exc}") from exc
    scan_results = tuple(
        sc.ScanResult(str(scan.get("name", "")), str(scan.get("status", "")), str(scan.get("detail", "")))
        for scan in scans.get("scans", [])
        if isinstance(scan, dict)
    )
    gate = sc.evaluate_gate(
        subject=subject,
        components=_components(),
        scans=scan_results,
        provenance=provenance,
        signature=signature,
        expected_version=_release_version(args.version),
    )
    print(sc.render_json(gate.to_dict()), end="")
    if not gate.ready:
        print(
            f"Gate closed with {len(gate.reasons)} blocking reason(s); "
            f"evidence recorded digest {evidence.get('sha256')}.",
            file=sys.stderr,
        )
    return 0 if gate.ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("build", cmd_build, "Produce SBOM, provenance, scans, and signature"),
        ("verify", cmd_verify, "Re-check the recorded evidence against the artifact"),
        ("gate", cmd_gate, "Decide whether the release evidence is complete"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--version", default="", help="MAJOR.MINOR.PATCH (default: the pyproject version)"
        )
        command.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Evidence directory")
        command.add_argument("--artifact", default=str(ARTIFACT), help="Artifact under test")
        if name == "build":
            command.add_argument("--source-commit", default="", help="Override the recorded source commit")
            command.add_argument(
                "--deterministic",
                action="store_true",
                help="Omit timestamps so repeated runs are byte-identical",
            )
        command.set_defaults(func=handler)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (sc.SupplyChainError, sc.SigningError, rm.ReleaseError) as exc:
        print(f"release evidence: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
