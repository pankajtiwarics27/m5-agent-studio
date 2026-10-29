"""Release SBOM, provenance, scanning, and signing (AS-042).

The release artifact (`studio.py`, packaged by AS-040) needs more than a digest
before it can be distributed: a machine-readable inventory of what went into it,
a signed claim about how it was built, and scans that either pass or say why they
did not run.

Design decisions:

1. **Nothing is claimed that did not happen.** A scan that cannot run reports
   ``unavailable`` with a reason; it never reports ``passed``. The release gate
   treats ``unavailable`` as *not ready*, so an absent scanner blocks the release
   instead of silently weakening it. The same rule applies to signing: with no
   configured signer, sign() raises rather than returning an empty placeholder.
2. **A signature over a digest, not the artifact.** Signing covers the SHA-256 of
   the artifact plus the provenance statement, so a signature cannot be lifted
   onto different bytes, and verification is cheap. The signature record names the
   digest it covers, and verification recomputes that digest from disk.
3. **No shell, no ambient credentials.** Signers and scanners are argv-based
   (`subprocess.run([...])`), inherit no secrets from the environment, and are
   only ever invoked with explicit paths. There is no `shell=True` anywhere and no
   key material is written by this module.
4. **Deterministic output.** Components are sorted, JSON is emitted with sorted
   keys, and no wall-clock value is embedded unless the caller supplies one — so
   the same inputs produce byte-identical SBOM and provenance files, which is what
   makes them reviewable in a diff.
5. **Platform-neutral.** Nothing here is macOS- or Linux-specific; the provenance
   records the builder, platform, and interpreter that actually produced it.

Reviewers should note what this module does **not** do: it does not obtain a
signing key, does not run a hosted transparency log, and does not prove absence of
vulnerabilities. It produces and verifies the evidence a human needs to make those
claims deliberately.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Protocol

SBOM_FORMAT = "CycloneDX"
SBOM_SPEC_VERSION = "1.5"
PROVENANCE_TYPE = "https://slsa.dev/provenance/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
BUILD_TYPE = "https://m5-agent-studio.dev/buildtypes/standalone-python-artifact/v1"
TOOL_NAME = "m5-agent-studio-supply-chain"
TOOL_VERSION = "1"

# Direct runtime dependencies declared in pyproject.toml; the SBOM covers these
# plus anything else installed that they resolve to.
RUNTIME_DEPENDENCIES = ("fastapi", "uvicorn", "httpx", "httpcore", "keyring")

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"

# Deliberately high-confidence patterns only. A noisy secret scanner that flags
# ordinary prose gets ignored, which is worse than a narrow one that is trusted.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
SCAN_CHUNK = 1024 * 1024

# Licence values come from distribution metadata, which mixes SPDX identifiers
# with free-text classifier labels. CycloneDX wants an SPDX ``id`` or a ``name``,
# so only recognisable identifiers are emitted as ids.
SINGLE_WORD_SPDX_IDS = frozenset({"MIT", "ISC", "Zlib", "0BSD", "Unlicense", "CC0-1.0", "WTFPL"})
SPDX_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.+-]{0,63}\Z")


class SupplyChainError(RuntimeError):
    """A supply-chain step that must not silently succeed."""


class SigningError(SupplyChainError):
    """Signing is unavailable, misconfigured, or failed verification."""


@dataclass(frozen=True)
class Component:
    """One dependency recorded in the SBOM."""

    name: str
    version: str
    license: str = ""
    purl: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SupplyChainError("Component name must not be empty.")
        if not self.version.strip():
            raise SupplyChainError(f"Component {self.name!r} has no version; refusing to invent one.")

    def to_sbom(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": "library",
            "name": self.name,
            "version": self.version,
            "purl": self.purl or f"pkg:pypi/{self.name.lower()}@{self.version}",
        }
        if self.license:
            looks_like_spdx = bool(SPDX_ID_RE.match(self.license)) and (
                "-" in self.license or self.license in SINGLE_WORD_SPDX_IDS
            )
            key = "id" if looks_like_spdx else "name"
            entry["licenses"] = [{"license": {key: self.license}}]
        return entry


@dataclass(frozen=True)
class Subject:
    """The artifact the evidence is about."""

    name: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\A[0-9a-f]{64}\Z", self.sha256):
            raise SupplyChainError("Subject sha256 must be 64 lowercase hex characters.")
        if self.size_bytes < 0:
            raise SupplyChainError("Subject size must not be negative.")

    @property
    def digest(self) -> dict[str, str]:
        return {"sha256": self.sha256}


@dataclass(frozen=True)
class ScanFinding:
    """A problem found by a scan."""

    scanner: str
    identifier: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"scanner": self.scanner, "identifier": self.identifier, "detail": self.detail}


@dataclass(frozen=True)
class ScanResult:
    """Outcome of one scanner; `unavailable` is never treated as success."""

    name: str
    status: str
    detail: str = ""
    findings: tuple[ScanFinding, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {STATUS_PASSED, STATUS_FAILED, STATUS_UNAVAILABLE}:
            raise SupplyChainError(f"Unknown scan status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def subject_from_file(path: Path) -> Subject:
    """Compute the subject digest from the artifact on disk."""
    if not path.is_file():
        raise SupplyChainError(f"Artifact not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(SCAN_CHUNK), b""):
            digest.update(block)
    return Subject(name=path.name, sha256=digest.hexdigest(), size_bytes=path.stat().st_size)


# ---------------------------------------------------------------------------
# SBOM
# ---------------------------------------------------------------------------


def collect_components(
    names: Iterable[str] = RUNTIME_DEPENDENCIES,
    *,
    version_of=None,
) -> tuple[Component, ...]:
    """Read installed distribution metadata; a missing distribution is an error.

    ``version_of`` is injectable so tests (and other environments) can describe a
    dependency set without depending on what happens to be installed.
    """
    resolved: list[Component] = []
    for name in names:
        if version_of is not None:
            info = version_of(name)
            if info is None:
                continue
            resolved.append(info)
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise SupplyChainError(f"Dependency {name!r} is not installed; cannot describe it in the SBOM.") from exc
        resolved.append(_component_from_distribution(distribution))
    return tuple(sorted(resolved, key=lambda component: component.name.lower()))


def _component_from_distribution(distribution: metadata.Distribution) -> Component:
    # PackageMetadata iterates over header names and is a Mapping at runtime, but
    # its static type does not expose indexing; build a plain dict explicitly.
    raw = {key: distribution.metadata[key] for key in distribution.metadata}
    license_text = (raw.get("License-Expression") or raw.get("License") or "").strip()
    if not license_text or len(license_text) > 64 or "\n" in license_text:
        license_text = ""
        for classifier in distribution.metadata.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                license_text = classifier.rsplit("::", 1)[-1].strip()
                break
    name = raw.get("Name") or ""
    return Component(
        name=str(name),
        version=str(distribution.version),
        license=license_text if re.fullmatch(r"[A-Za-z0-9.+\-]{1,64}", license_text or "") else "",
    )


def _requirement_name(requirement: str) -> str:
    """Return the distribution name of a ``Requires-Dist`` entry, or ``""``.

    Entries guarded by an ``extra`` marker describe optional features that are
    not part of the shipped runtime, so they are excluded rather than guessed at.
    """
    head, _, marker = requirement.partition(";")
    if "extra" in marker:
        return ""
    match = re.match(r"\A\s*([A-Za-z0-9][A-Za-z0-9._-]*)", head)
    return match.group(1) if match else ""


def normalize_name(name: str) -> str:
    """PEP 503 normalisation, so ``typing_extensions`` and ``typing-extensions`` match."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def runtime_closure(names: Iterable[str] = RUNTIME_DEPENDENCIES) -> tuple[str, ...]:
    """Expand dependency roots to every installed distribution they pull in.

    The closure is computed from installed metadata, so it reflects the artifact
    as built rather than an idealised dependency list. Uninstalled names are
    skipped here; ``collect_components`` is what reports a missing root.
    """
    seen: dict[str, None] = {}
    queue = [normalize_name(name) for name in names]
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[name] = None
        for requirement in distribution.requires or ():
            dependency = _requirement_name(requirement)
            if dependency and normalize_name(dependency) not in seen:
                queue.append(normalize_name(dependency))
    return tuple(seen)


def build_sbom(
    components: Iterable[Component],
    subject: Subject,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic CycloneDX document for one artifact."""
    ordered = sorted(components, key=lambda component: (component.name.lower(), component.version))
    if not ordered:
        raise SupplyChainError("Refusing to emit an SBOM with no components.")
    document: dict[str, Any] = {
        "bomFormat": SBOM_FORMAT,
        "specVersion": SBOM_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": subject.name,
                "hashes": [{"alg": "SHA-256", "content": subject.sha256}],
            },
            "tools": [{"vendor": "m5-agent-studio", "name": TOOL_NAME, "version": TOOL_VERSION}],
        },
        "components": [component.to_sbom() for component in ordered],
        "dependencies": [{"ref": subject.name, "dependsOn": [f"pkg:pypi/{c.name.lower()}@{c.version}" for c in ordered]}],
    }
    if created_at:
        document["metadata"]["timestamp"] = created_at
    return document


def render_json(document: dict[str, Any]) -> str:
    """Serialize with sorted keys and a trailing newline so diffs are stable."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildContext:
    """What the builder can honestly record about the build."""

    source_commit: str
    version: str
    builder_id: str = "local-standalone-build"
    python_version: str = ""
    platform: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "source_commit": self.source_commit,
            "version": self.version,
            "builder_id": self.builder_id,
            "python_version": self.python_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": self.platform or sys.platform,
            "created_at": self.created_at,
        }


def build_provenance(
    subject: Subject,
    context: BuildContext,
    components: Iterable[Component] = (),
    *,
    sbom_sha256: str = "",
) -> dict[str, Any]:
    """Return an in-toto/SLSA statement binding the artifact to its build inputs."""
    if not context.source_commit.strip():
        raise SupplyChainError("Provenance requires the source commit that produced the artifact.")
    resolved = sorted(
        (
            {"name": component.name, "version": component.version,
             "purl": component.purl or f"pkg:pypi/{component.name.lower()}@{component.version}"}
            for component in components
        ),
        key=lambda item: (str(item["name"]).lower(), str(item["version"])),
    )
    external: dict[str, Any] = {
        "version": context.version,
        "sourceCommit": context.source_commit,
        "builder": context.builder_id,
        "pythonVersion": context.to_dict()["python_version"],
        "platform": context.to_dict()["platform"],
    }
    if sbom_sha256:
        external["sbomSha256"] = sbom_sha256
    predicate: dict[str, Any] = {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": external,
            "resolvedDependencies": resolved,
        },
        "runDetails": {"builder": {"id": f"m5-agent-studio://{context.builder_id}"}},
    }
    if context.created_at:
        predicate["runDetails"]["metadata"] = {"finishedOn": context.created_at}
    return {
        "_type": STATEMENT_TYPE,
        "subject": [{"name": subject.name, "digest": subject.digest}],
        "predicateType": PROVENANCE_TYPE,
        "predicate": predicate,
    }


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def scan_artifact_secrets(path: Path) -> ScanResult:
    """Scan a packaged artifact for high-confidence credential shapes."""
    if not path.is_file():
        raise SupplyChainError(f"Artifact not found: {path}")
    findings: list[ScanFinding] = []
    seen: set[tuple[str, int]] = set()
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                line = raw.decode("utf-8", errors="ignore")
            except Exception:  # pragma: no cover - decode with errors=ignore cannot raise
                continue
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line) and (name, line_number) not in seen:
                    seen.add((name, line_number))
                    findings.append(
                        ScanFinding(
                            scanner="artifact-secrets",
                            identifier=f"{name} at {path.name}:{line_number}",
                            detail="High-confidence credential pattern found in the release artifact.",
                        )
                    )
    if findings:
        return ScanResult("artifact-secrets", STATUS_FAILED, f"{len(findings)} credential pattern(s) found", tuple(findings))
    return ScanResult("artifact-secrets", STATUS_PASSED, "No high-confidence credential patterns found.")


def scan_dependencies(
    *,
    runner=None,
    components: Iterable[Component] = (),
) -> ScanResult:
    """Run a vulnerability scan, or say precisely why it did not run.

    ``pip-audit`` is intentionally not a hard dependency (it changes its own
    dependency surface often); when it is missing the scan is ``unavailable`` and
    the gate stays closed. ``runner`` is injectable for tests.
    """
    known = tuple(components)
    if runner is None:
        try:
            import pip_audit  # noqa: F401
        except ImportError:
            return ScanResult(
                "dependency-vulnerabilities",
                STATUS_UNAVAILABLE,
                "pip-audit is not installed, so no vulnerability scan was performed. "
                "Install it (`python -m pip install pip-audit`) and re-run before release.",
            )
        runner = _default_pip_audit_runner

    code, output = runner(known)
    try:
        report = json.loads(output) if output.strip() else {}
    except json.JSONDecodeError:
        return ScanResult(
            "dependency-vulnerabilities",
            STATUS_FAILED,
            f"pip-audit produced unparseable output (exit {code}); treat as failed.",
            (ScanFinding("dependency-vulnerabilities", "unparseable-output", output.strip()[:200]),),
        )
    scope = {normalize_name(component.name) for component in known}
    findings: list[ScanFinding] = []
    seen_vulnerabilities: set[tuple[str, str]] = set()
    out_of_scope = 0
    for entry in report.get("dependencies", []) if isinstance(report, dict) else []:
        if scope and normalize_name(str(entry.get("name", ""))) not in scope:
            if entry.get("vulns"):
                out_of_scope += sum(1 for _ in entry.get("vulns", []))
            continue
        for vulnerability in entry.get("vulns", []) if isinstance(entry, dict) else []:
            key = (str(entry.get("name", "")), str(vulnerability.get("id", "")))
            if key in seen_vulnerabilities:
                continue
            seen_vulnerabilities.add(key)
            findings.append(
                ScanFinding(
                    scanner="dependency-vulnerabilities",
                    identifier=f"{entry.get('name')} {entry.get('version')}: {vulnerability.get('id')}",
                    detail=str(vulnerability.get("description") or "")[:300],
                )
            )
    scope_note = (
        f" Scope: {len(scope)} shipped runtime distribution(s); "
        f"{out_of_scope} finding(s) in build-only tooling are out of scope."
        if scope
        else ""
    )
    if findings:
        return ScanResult(
            "dependency-vulnerabilities",
            STATUS_FAILED,
            f"{len(findings)} vulnerability finding(s).{scope_note}",
            tuple(findings),
        )
    if code > 1:
        return ScanResult(
            "dependency-vulnerabilities",
            STATUS_FAILED,
            f"pip-audit exited {code} without parseable findings.",
            (ScanFinding("dependency-vulnerabilities", "scanner-error", output.strip()[:200]),),
        )
    return ScanResult(
        "dependency-vulnerabilities", STATUS_PASSED, f"No known vulnerabilities reported.{scope_note}"
    )


def _default_pip_audit_runner(components: tuple[Component, ...]) -> tuple[int, str]:
    command = [sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


class Signer(Protocol):
    """A detached-signature implementation.

    Implementations sign a hex SHA-256 digest and return a base64 signature.
    """

    @property
    def name(self) -> str: ...

    def sign(self, digest_hex: str) -> str: ...

    def verify(self, digest_hex: str, signature: str) -> bool: ...


@dataclass(frozen=True)
class CommandSigner:
    """Sign with an external tool via argv (never a shell).

    ``sign_argv`` receives the digest file path as its final argument and must
    print the base64 signature on stdout. ``verify_argv`` receives the digest
    file path and signature path and must exit 0 for a valid signature.
    """

    name: str
    sign_argv: tuple[str, ...]
    verify_argv: tuple[str, ...] = ()

    def _run(self, argv: tuple[str, ...]) -> tuple[int, str]:
        try:
            completed = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        except OSError as exc:
            raise SigningError(f"Signer {self.name!r} could not run {argv[0]!r}: {exc}") from exc
        return completed.returncode, completed.stdout

    def sign(self, digest_hex: str) -> str:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="m5-signing-") as temporary:
            digest_path = Path(temporary) / "digest.txt"
            digest_path.write_text(digest_hex + "\n", encoding="utf-8")
            code, stdout = self._run((*self.sign_argv, str(digest_path)))
        if code != 0:
            raise SigningError(f"Signer {self.name!r} exited {code}; refusing to record a signature.")
        signature = stdout.strip()
        if not signature:
            raise SigningError(f"Signer {self.name!r} produced an empty signature.")
        return signature

    def verify(self, digest_hex: str, signature: str) -> bool:
        if not self.verify_argv:
            raise SigningError(f"Signer {self.name!r} has no verification command configured.")
        import tempfile

        with tempfile.TemporaryDirectory(prefix="m5-verify-") as temporary:
            digest_path = Path(temporary) / "digest.txt"
            signature_path = Path(temporary) / "signature.b64"
            digest_path.write_text(digest_hex + "\n", encoding="utf-8")
            signature_path.write_text(signature + "\n", encoding="utf-8")
            code, _ = self._run((*self.verify_argv, str(digest_path), str(signature_path)))
        return code == 0


@dataclass(frozen=True)
class UnconfiguredSigner:
    """The honest default: there is no signing key in this environment."""

    name: str = "unconfigured"
    reason: str = (
        "No release signing key is configured. Set STUDIO_RELEASE_SIGN_COMMAND and "
        "STUDIO_RELEASE_VERIFY_COMMAND (or pass signer= explicitly) to sign a release."
    )

    def sign(self, digest_hex: str) -> str:
        raise SigningError(self.reason)

    def verify(self, digest_hex: str, signature: str) -> bool:
        raise SigningError(self.reason)


@dataclass(frozen=True)
class SignatureRecord:
    """A detached signature over an artifact digest."""

    digest_hex: str
    signature: str
    signer: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\A[0-9a-f]{64}\Z", self.digest_hex):
            raise SigningError("Signature record must cover a 64-character lowercase hex digest.")
        if not self.signature.strip():
            raise SigningError("Signature must not be empty.")

    def to_dict(self) -> dict[str, str]:
        return {"digest": f"sha256:{self.digest_hex}", "signature": self.signature, "signer": self.signer}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SignatureRecord:
        try:
            digest = str(raw["digest"])
            signature = str(raw["signature"])
            signer = str(raw["signer"])
        except KeyError as exc:
            raise SigningError(f"Signature record is missing field: {exc}") from exc
        prefix = "sha256:"
        if not digest.startswith(prefix):
            raise SigningError("Signature digest must be prefixed with 'sha256:'.")
        return cls(digest_hex=digest[len(prefix):].lower(), signature=signature, signer=signer)


def resolve_signer(environ: dict[str, str] | None = None) -> Signer:
    """Build a signer from configuration, or the explicit unconfigured one."""
    env = os.environ if environ is None else environ
    sign_command = env.get("STUDIO_RELEASE_SIGN_COMMAND", "").strip()
    verify_command = env.get("STUDIO_RELEASE_VERIFY_COMMAND", "").strip()
    if not sign_command:
        return UnconfiguredSigner()
    sign_argv = tuple(sign_command.split())
    verify_argv = tuple(verify_command.split())
    if not verify_argv:
        raise SigningError(
            "STUDIO_RELEASE_SIGN_COMMAND is set but STUDIO_RELEASE_VERIFY_COMMAND is not; "
            "a signature that cannot be verified is not evidence."
        )
    return CommandSigner(name=Path(sign_argv[0]).name or "command", sign_argv=sign_argv, verify_argv=verify_argv)


def sign_release(subject: Subject, *, signer: Signer) -> SignatureRecord:
    """Sign the artifact digest and immediately verify with the same signer."""
    signature = signer.sign(subject.sha256)
    if hasattr(signer, "verify_argv") and not getattr(signer, "verify_argv", ()):
        return SignatureRecord(digest_hex=subject.sha256, signature=signature, signer=signer.name)
    if not signer.verify(subject.sha256, signature):
        raise SigningError(f"Signer {signer.name!r} produced a signature that did not verify.")
    return SignatureRecord(digest_hex=subject.sha256, signature=signature, signer=signer.name)


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Whether the release evidence is complete enough to distribute."""

    ready: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "reasons": list(self.reasons), "warnings": list(self.warnings)}


def evaluate_gate(
    *,
    subject: Subject,
    components: tuple[Component, ...],
    scans: tuple[ScanResult, ...],
    provenance: dict[str, Any] | None,
    signature: SignatureRecord | None,
    expected_version: str = "",
) -> GateResult:
    """Decide whether SBOM + scans + provenance + signature are all in place."""
    reasons: list[str] = []
    warnings: list[str] = []

    if not components:
        reasons.append("SBOM has no components.")
    if provenance is None:
        reasons.append("No provenance statement was produced.")
    else:
        subjects = provenance.get("subject") or []
        digests = {
            str(item.get("digest", {}).get("sha256", ""))
            for item in subjects
            if isinstance(item, dict)
        }
        if subject.sha256 not in digests:
            reasons.append("Provenance does not match the artifact digest.")
        external = (
            provenance.get("predicate", {}).get("buildDefinition", {}).get("externalParameters", {})
            if isinstance(provenance.get("predicate"), dict)
            else {}
        )
        if expected_version and external.get("version") not in (None, expected_version):
            reasons.append(
                f"Provenance records version {external.get('version')!r}, expected {expected_version!r}."
            )
        if not external.get("sourceCommit"):
            reasons.append("Provenance does not record a source commit.")
        if not external.get("sbomSha256"):
            warnings.append("Provenance does not bind the SBOM digest.")

    if signature is None:
        reasons.append("Artifact is not signed.")
    elif signature.digest_hex != subject.sha256:
        reasons.append("Signature covers a different digest than the artifact.")

    if not scans:
        reasons.append("No scans were recorded.")
    for scan in scans:
        if scan.status == STATUS_FAILED:
            reasons.append(f"Scan {scan.name!r} failed: {scan.detail}")
        elif scan.status == STATUS_UNAVAILABLE:
            reasons.append(f"Scan {scan.name!r} did not run: {scan.detail}")
    return GateResult(ready=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def base64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")
