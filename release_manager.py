"""Versioned install, update, and rollback for the standalone Studio artifact (AS-040).

The packaged artifact is the deterministic single-file `studio.py` built by
`scripts/build_studio.py` (AS-014). This module owns the local lifecycle around
it — nothing here is macOS-specific, and nothing here needs a network, root, or
a shell:

* every install is immutable under ``versions/<version>/`` and carries its own
  ``manifest.json`` (size + SHA-256 + provenance);
* activation is a single atomic pointer replacement
  (``current.json``), so an interrupted update leaves the previous release live;
* ``previous.json`` records the last activation, so rollback is one atomic swap
  that can be repeated;
* every activation re-verifies the digest, and verification of a tampered or
  truncated artifact fails closed instead of half-working.

Design decisions:

1. **Content-addressed trust, not filenames.** A digest mismatch aborts before
   any pointer changes. Rollback verifies the *rollback target* too, so a
   corrupted older release is refused rather than activated.
2. **No silent downgrades.** `update()` refuses to activate an older version
   unless the caller passes ``allow_downgrade=True``; that is what `rollback()`
   is for, and it keeps the two operations auditable.
3. **Never destructive.** Superseded version directories are retained so a
   rollback target exists; pruning is a separate, explicit operation.
4. **Platform-neutral paths.** The layout is created wherever the caller points
   it, including on macOS and Windows, and uses only `pathlib`/`os.replace`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_ROOT = Path.home() / ".local" / "share" / "m5-agent-studio"
ARTIFACT_NAME = "studio.py"
MANIFEST_NAME = "manifest.json"
CURRENT_POINTER = "current.json"
PREVIOUS_POINTER = "previous.json"
VERSIONS_DIR = "versions"
STAGING_PREFIX = ".staging-"
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
CHUNK = 1024 * 1024


class ReleaseError(RuntimeError):
    """A packaging, install, or rollback operation that must not proceed."""


@dataclass(frozen=True)
class ReleaseManifest:
    """Everything needed to re-verify one immutable release."""

    version: str
    sha256: str
    size_bytes: int
    created_at: str
    python_requires: str = ">=3.11"
    source_commit: str = ""

    def __post_init__(self) -> None:
        if not VERSION_RE.match(self.version):
            raise ReleaseError(f"Version must look like MAJOR.MINOR.PATCH, got {self.version!r}.")
        if not SHA256_RE.match(self.sha256):
            raise ReleaseError("Manifest sha256 must be 64 lowercase hex characters.")
        if self.size_bytes < 0:
            raise ReleaseError("Manifest size_bytes must not be negative.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "artifact": ARTIFACT_NAME,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "python_requires": self.python_requires,
            "source_commit": self.source_commit,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReleaseManifest:
        try:
            return cls(
                version=str(raw["version"]),
                sha256=str(raw["sha256"]).lower(),
                size_bytes=int(raw["size_bytes"]),
                created_at=str(raw["created_at"]),
                python_requires=str(raw.get("python_requires", ">=3.11")),
                source_commit=str(raw.get("source_commit", "")),
            )
        except KeyError as exc:
            raise ReleaseError(f"Manifest is missing required field: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise ReleaseError(f"Manifest field has an invalid type: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> ReleaseManifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReleaseError(f"Cannot read manifest {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"Manifest {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ReleaseError(f"Manifest {path} must contain a JSON object.")
        return cls.from_dict(raw)


@dataclass(frozen=True)
class InstallLayout:
    """Directory layout for one Studio installation root."""

    root: Path

    @property
    def versions(self) -> Path:
        return self.root / VERSIONS_DIR

    @property
    def current_pointer(self) -> Path:
        return self.root / CURRENT_POINTER

    @property
    def previous_pointer(self) -> Path:
        return self.root / PREVIOUS_POINTER

    def version_dir(self, version: str) -> Path:
        if not VERSION_RE.match(version):
            raise ReleaseError(f"Version must look like MAJOR.MINOR.PATCH, got {version!r}.")
        return self.versions / version

    def artifact_path(self, version: str) -> Path:
        return self.version_dir(version) / ARTIFACT_NAME

    def manifest_path(self, version: str) -> Path:
        return self.version_dir(version) / MANIFEST_NAME

    def current_artifact(self) -> Path:
        pointer = read_pointer(self.current_pointer)
        if pointer is None:
            raise ReleaseError(f"No active release under {self.root}; run install first.")
        return self.artifact_path(pointer.version)


@dataclass(frozen=True)
class Pointer:
    """The active (or previous) release, stored as a portable JSON record."""

    version: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"version": self.version, "artifact": ARTIFACT_NAME, "sha256": self.sha256}


def validate_version(version: str) -> str:
    """Return a validated version string, rejecting traversal, padding, and noise.

    Surrounding whitespace is rejected rather than stripped: a version arrives from
    argv, an environment variable, or a manifest, and silent normalization would
    hide a caller bug or an attempt to smuggle a different string into a path.
    """
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ReleaseError(f"Version must look like MAJOR.MINOR.PATCH, got {version!r}.")
    return version


def version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = validate_version(version).split(".")
    return int(major), int(minor), int(patch)


def sha256_file(path: Path) -> str:
    """Hash a file in chunks; raises ReleaseError when it is missing."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                digest.update(block)
    except OSError as exc:
        raise ReleaseError(f"Cannot read artifact {path}: {exc}") from exc
    return digest.hexdigest()


def read_pointer(path: Path) -> Pointer | None:
    """Read a pointer file; a corrupt pointer is an error, not an empty slot."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Pointer {path} is unreadable or corrupt: {exc}") from exc
    if not isinstance(raw, dict) or "version" not in raw or "sha256" not in raw:
        raise ReleaseError(f"Pointer {path} is missing version/sha256.")
    return Pointer(version=validate_version(str(raw["version"])), sha256=str(raw["sha256"]).lower())


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a sibling temp file so readers never see a partial record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_manifest(
    artifact: Path,
    version: str,
    *,
    source_commit: str = "",
    python_requires: str = ">=3.11",
    created_at: str | None = None,
) -> ReleaseManifest:
    """Compute a manifest for an artifact on disk; never guesses a digest."""
    if not artifact.is_file():
        raise ReleaseError(f"Artifact not found: {artifact}")
    return ReleaseManifest(
        version=validate_version(version),
        sha256=sha256_file(artifact),
        size_bytes=artifact.stat().st_size,
        created_at=created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        python_requires=python_requires,
        source_commit=source_commit,
    )


def _verify_artifact(path: Path, manifest: ReleaseManifest) -> None:
    if not path.is_file():
        raise ReleaseError(f"Artifact missing: {path}")
    actual_size = path.stat().st_size
    if actual_size != manifest.size_bytes:
        raise ReleaseError(
            f"Artifact size mismatch for {manifest.version}: expected {manifest.size_bytes}, found {actual_size}."
        )
    actual_digest = sha256_file(path)
    if actual_digest != manifest.sha256:
        raise ReleaseError(
            f"Artifact digest mismatch for {manifest.version}: expected {manifest.sha256}, found {actual_digest}."
        )


def _stage_version(layout: InstallLayout, artifact: Path, manifest: ReleaseManifest) -> Path:
    """Copy the artifact into a staging dir and move it into place atomically."""
    layout.versions.mkdir(parents=True, exist_ok=True)
    staging = Path(
        f"{layout.root / (STAGING_PREFIX + manifest.version + '-' + str(os.getpid()))}"
    )
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        target_artifact = staging / ARTIFACT_NAME
        shutil.copyfile(artifact, target_artifact)
        _verify_artifact(target_artifact, manifest)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        destination = layout.version_dir(manifest.version)
        if destination.exists():
            # Re-installing the same version: replace it only after the new copy verified.
            shutil.rmtree(destination)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return layout.version_dir(manifest.version)


def _activate(layout: InstallLayout, manifest: ReleaseManifest) -> Pointer | None:
    """Point `current` at a verified release, remembering the old one as `previous`."""
    layout.root.mkdir(parents=True, exist_ok=True)
    previous = read_pointer(layout.current_pointer)
    if previous is not None and previous.version != manifest.version:
        _write_json_atomic(layout.previous_pointer, previous.to_dict())
    elif previous is not None:
        # Re-activating the same version keeps the existing rollback target intact.
        pass
    else:
        layout.previous_pointer.unlink(missing_ok=True)
    _write_json_atomic(layout.current_pointer, Pointer(manifest.version, manifest.sha256).to_dict())
    return previous


def install(
    layout: InstallLayout,
    artifact: Path,
    manifest: ReleaseManifest,
    *,
    activate: bool = True,
) -> dict[str, Any]:
    """Install one immutable release and (by default) activate it."""
    _verify_artifact(artifact, manifest)
    if artifact.name != ARTIFACT_NAME:
        raise ReleaseError(f"Artifact must be named {ARTIFACT_NAME}, got {artifact.name!r}.")
    _stage_version(layout, artifact, manifest)
    installed = layout.manifest_path(manifest.version)
    ReleaseManifest.load(installed)  # round-trip guard: the manifest on disk must parse
    result: dict[str, Any] = {
        "action": "install",
        "version": manifest.version,
        "sha256": manifest.sha256,
        "path": str(layout.version_dir(manifest.version)),
        "activated": False,
        "previous": None,
    }
    if activate:
        previous = _activate(layout, manifest)
        result["activated"] = True
        result["previous"] = previous.version if previous else None
    return result


def update(
    layout: InstallLayout,
    artifact: Path,
    manifest: ReleaseManifest,
    *,
    allow_downgrade: bool = False,
) -> dict[str, Any]:
    """Install and activate a release, refusing an unrequested downgrade."""
    current = read_pointer(layout.current_pointer)
    if current is not None and not allow_downgrade:
        if version_tuple(manifest.version) < version_tuple(current.version):
            raise ReleaseError(
                f"Refusing to downgrade from {current.version} to {manifest.version}; "
                "use rollback, or pass allow_downgrade=True deliberately."
            )
    result = install(layout, artifact, manifest, activate=True)
    result["action"] = "update"
    return result


def rollback(layout: InstallLayout) -> dict[str, Any]:
    """Swap `current` and `previous`, after verifying the rollback target."""
    current = read_pointer(layout.current_pointer)
    previous = read_pointer(layout.previous_pointer)
    if current is None:
        raise ReleaseError(f"No active release under {layout.root}; nothing to roll back from.")
    if previous is None:
        raise ReleaseError("No previous release recorded; rollback is unavailable.")
    manifest = ReleaseManifest.load(layout.manifest_path(previous.version))
    if manifest.sha256 != previous.sha256:
        raise ReleaseError(
            f"Recorded digest for {previous.version} does not match its manifest; refusing to roll back."
        )
    _verify_artifact(layout.artifact_path(previous.version), manifest)
    _write_json_atomic(layout.current_pointer, previous.to_dict())
    _write_json_atomic(layout.previous_pointer, current.to_dict())
    return {
        "action": "rollback",
        "version": previous.version,
        "from": current.version,
        "sha256": manifest.sha256,
        "path": str(layout.version_dir(previous.version)),
    }


def verify(layout: InstallLayout) -> list[str]:
    """Return integrity problems for the whole installation; empty means healthy."""
    problems: list[str] = []
    for pointer_name, pointer_path in (
        ("current", layout.current_pointer),
        ("previous", layout.previous_pointer),
    ):
        try:
            pointer = read_pointer(pointer_path)
        except ReleaseError as exc:
            problems.append(f"{pointer_name} pointer: {exc}")
            continue
        if pointer is None:
            if pointer_name == "current":
                problems.append("current pointer: no release is active")
            continue
        manifest_path = layout.manifest_path(pointer.version)
        try:
            manifest = ReleaseManifest.load(manifest_path)
        except ReleaseError as exc:
            problems.append(f"{pointer_name} {pointer.version}: {exc}")
            continue
        if manifest.version != pointer.version:
            problems.append(
                f"{pointer_name} {pointer.version}: manifest declares version {manifest.version}"
            )
        if manifest.sha256 != pointer.sha256:
            problems.append(f"{pointer_name} {pointer.version}: manifest digest differs from pointer")
            continue
        try:
            _verify_artifact(layout.artifact_path(pointer.version), manifest)
        except ReleaseError as exc:
            problems.append(f"{pointer_name} {pointer.version}: {exc}")
    return problems


def installed_versions(layout: InstallLayout) -> list[str]:
    """Return installed version directories in ascending order."""
    if not layout.versions.is_dir():
        return []
    versions = [
        entry.name
        for entry in layout.versions.iterdir()
        if entry.is_dir() and VERSION_RE.match(entry.name)
    ]
    return sorted(versions, key=version_tuple)


def status(layout: InstallLayout) -> dict[str, Any]:
    """Describe the installation without mutating it; a corrupt pointer is reported, not raised."""
    problems = verify(layout)
    try:
        current = read_pointer(layout.current_pointer)
    except ReleaseError:
        current = None
    try:
        previous = read_pointer(layout.previous_pointer)
    except ReleaseError:
        previous = None
    return {
        "root": str(layout.root),
        "current": current.version if current else None,
        "previous": previous.version if previous else None,
        "versions": installed_versions(layout),
        "artifact": str(layout.current_artifact()) if current else None,
        "healthy": not problems,
        "problems": problems,
    }


def prune(layout: InstallLayout, keep: int = 2) -> dict[str, Any]:
    """Delete superseded version directories, never the active or rollback target."""
    if keep < 1:
        raise ReleaseError("keep must be at least 1")
    current = read_pointer(layout.current_pointer)
    previous = read_pointer(layout.previous_pointer)
    protected = {pointer.version for pointer in (current, previous) if pointer is not None}
    keep_extra = max(keep - len(protected), 0)
    removable: list[str] = []
    for version in reversed(installed_versions(layout)):
        if version in protected:
            continue
        if keep_extra > 0:
            keep_extra -= 1
            continue
        removable.append(version)
    for version in removable:
        shutil.rmtree(layout.version_dir(version), ignore_errors=True)
    return {"action": "prune", "removed": removable, "kept": sorted(protected & set(installed_versions(layout)))}


def staged_leftovers(layout: InstallLayout) -> Iterable[Path]:
    """Yield abandoned staging directories, for diagnostics and cleanup tests."""
    if not layout.root.is_dir():
        return []
    return [entry for entry in layout.root.iterdir() if entry.name.startswith(STAGING_PREFIX)]
