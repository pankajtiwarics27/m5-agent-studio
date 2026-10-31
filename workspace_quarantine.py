"""Workspace quarantine and trusted reconstruction (`M5-P5-01`, ADR-0017).

Recovering from suspected contaminated execution has one non-negotiable rule: the
recovered workspace may contain only what an operator explicitly accepted, and no
byte of the affected workspace may be destroyed on the way there.

Three operations, in the order they are used:

1. :func:`freeze_workspace` copies every byte of the affected workspace into a
   read-only quarantine tree, hashes it, and writes a manifest. The frozen tree is
   ``0500`` directories with ``0400`` files, so a later mistake cannot edit the
   evidence it is supposed to preserve.
2. :func:`plan_reconstruction` compares that frozen tree against a trusted baseline
   and classifies every difference as **accepted** (the operator named it) or
   **ambiguous** (nobody named it). Nothing is trusted by default.
3. :func:`reconstruct_workspace` builds a *new* workspace from the trusted baseline
   plus the accepted changes, and writes the ambiguous ones to a separate
   ``<destination>.ambiguous`` tree with an index, so they are preserved for a human
   without being silently promoted into the recovered workspace.

Verification is reported, not assumed: :func:`classify_recoverability` answers
``unresolvable_locally`` whenever the host itself may be compromised and no
qualified isolation is available to re-run verification under, which is the honest
answer on a host whose isolation has not been qualified.

Stdlib only; no dependency on `app.py` so the module can be inlined into the
standalone bundle and used from a recovery shell when the app will not start.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import artifact_lineage

WQ_SCHEMA_VERSION = 1
WQ_DIRNAME = "workspace_quarantine"
WQ_MANIFEST_NAME = "manifest.json"
WQ_TREE_NAME = "tree"
WQ_RECONSTRUCTIONS_DIRNAME = "reconstructions"
WQ_RELEASE_NAME = "release.json"
WQ_AMBIGUOUS_SUFFIX = ".ambiguous"

# Never quarantine or reconstruct these: they are either the quarantine itself, a
# version-control database whose bytes cannot be meaningfully copied file-by-file,
# or a socket/device that has no bytes at all.
_SKIP_DIR_NAMES = frozenset({".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


class WorkspaceQuarantineError(RuntimeError):
    """Base error for quarantine and reconstruction failures."""


class WorkspaceQuarantineIntegrityError(WorkspaceQuarantineError):
    """The frozen tree, manifest, or an operator instruction failed verification."""


@dataclass(frozen=True, slots=True)
class FrozenFile:
    """One entry of a frozen workspace, addressed by an inert relative path."""

    rel_path: str
    kind: str  # "file" | "symlink"
    sha256: str
    size: int
    link_target: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "link_target": self.link_target,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> FrozenFile:
        return cls(
            rel_path=str(raw.get("rel_path") or ""),
            kind=str(raw.get("kind") or "file"),
            sha256=str(raw.get("sha256") or ""),
            size=int(raw.get("size") or 0),
            link_target=str(raw.get("link_target") or ""),
        )


@dataclass(frozen=True, slots=True)
class QuarantineManifest:
    """Immutable record of a frozen workspace and the bytes preserved with it."""

    quarantine_id: str
    workspace_root: str
    reason: str
    created_at: float
    files: tuple[FrozenFile, ...]
    tree_sha256: str
    schema_version: int = WQ_SCHEMA_VERSION

    def paths(self) -> list[str]:
        return [entry.rel_path for entry in self.files]

    def get(self, rel_path: str) -> FrozenFile | None:
        clean = _wq_clean_rel(rel_path)
        for entry in self.files:
            if entry.rel_path == clean:
                return entry
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "quarantine_id": self.quarantine_id,
            "workspace_root": self.workspace_root,
            "reason": self.reason,
            "created_at": self.created_at,
            "tree_sha256": self.tree_sha256,
            "files": [entry.to_mapping() for entry in self.files],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> QuarantineManifest:
        version = int(raw.get("schema_version") or 0)
        if version != WQ_SCHEMA_VERSION:
            raise WorkspaceQuarantineIntegrityError(
                f"Unsupported quarantine schema_version {version!r}; expected {WQ_SCHEMA_VERSION}."
            )
        files_raw = raw.get("files")
        if not isinstance(files_raw, Sequence):
            raise WorkspaceQuarantineIntegrityError("Quarantine manifest has no file list.")
        return cls(
            quarantine_id=str(raw.get("quarantine_id") or ""),
            workspace_root=str(raw.get("workspace_root") or ""),
            reason=str(raw.get("reason") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            files=tuple(FrozenFile.from_mapping(item) for item in files_raw),
            tree_sha256=str(raw.get("tree_sha256") or ""),
            schema_version=version,
        )


@dataclass(frozen=True, slots=True)
class ReconstructionPlan:
    """What a reconstruction would do, before it does anything.

    ``accepted`` restores from the frozen tree, ``ambiguous`` is preserved outside
    the reconstructed workspace, and ``unchanged`` needs no action because the
    baseline already matches.
    """

    quarantine_id: str
    baseline_root: str
    accepted: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unchanged: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "quarantine_id": self.quarantine_id,
            "baseline_root": self.baseline_root,
            "accepted": list(self.accepted),
            "ambiguous": list(self.ambiguous),
            "unchanged": list(self.unchanged),
        }


@dataclass(frozen=True, slots=True)
class ReconstructionResult:
    """The outcome of a reconstruction, including what was deliberately withheld."""

    quarantine_id: str
    destination: str
    ambiguous_destination: str
    restored: tuple[str, ...]
    ambiguous: tuple[str, ...]
    isolated_verification: str
    recoverability: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "quarantine_id": self.quarantine_id,
            "destination": self.destination,
            "ambiguous_destination": self.ambiguous_destination,
            "restored": list(self.restored),
            "ambiguous": list(self.ambiguous),
            "isolated_verification": self.isolated_verification,
            "recoverability": self.recoverability,
        }


def _wq_now() -> float:
    return time.time()


def _wq_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wq_tree_digest(files: Iterable[FrozenFile]) -> str:
    h = hashlib.sha256()
    for entry in sorted(files, key=lambda item: item.rel_path):
        h.update(f"{entry.rel_path}\x00{entry.kind}\x00{entry.sha256}\x00{entry.link_target}\n".encode("utf-8"))
    return h.hexdigest()


def _wq_clean_rel(rel_path: str) -> str:
    """Normalise a relative path and refuse anything that could leave the tree."""
    text = str(rel_path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text:
        raise WorkspaceQuarantineError("Empty relative path.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts) or not parts:
        raise WorkspaceQuarantineError(f"Refusing relative path that escapes the workspace: {rel_path!r}.")
    if os.path.isabs(text) or text.startswith("/"):
        raise WorkspaceQuarantineError(f"Refusing absolute path: {rel_path!r}.")
    return "/".join(parts)


def _wq_iter_entries(root: Path) -> list[tuple[str, Path, str]]:
    """Yield ``(rel_path, absolute_path, kind)`` without following symlinks."""
    entries: list[tuple[str, Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIR_NAMES)
        for name in list(dirnames):
            candidate = base / name
            if candidate.is_symlink():
                rel = candidate.relative_to(root).as_posix()
                entries.append((rel, candidate, "symlink"))
                dirnames.remove(name)
        for name in sorted(filenames):
            candidate = base / name
            rel = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                entries.append((rel, candidate, "symlink"))
            elif candidate.is_file():
                entries.append((rel, candidate, "file"))
    return entries


def _wq_restrict_file(path: Path, mode: int = 0o400) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # pragma: no cover - platform without chmod semantics
        pass


def _wq_make_read_only(root: Path) -> None:
    """Make a frozen tree read-only, deepest first so the walk can finish."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            _wq_restrict_file(Path(dirpath) / name)
        for name in dirnames:
            _wq_restrict_file(Path(dirpath) / name, 0o500)
    _wq_restrict_file(root, 0o500)


def _wq_ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover
        pass


def quarantine_root(store_root: Path | str) -> Path:
    return Path(store_root).expanduser() / WQ_DIRNAME


def freeze_workspace(
    workspace_root: Path | str,
    *,
    store_root: Path | str,
    reason: str,
    actor: str = "",
    quarantine_id: str | None = None,
) -> QuarantineManifest:
    """Copy every byte of `workspace_root` into a read-only quarantine tree.

    The copy is byte-exact: files are copied, symlinks are recorded as links and
    never followed (so a link pointing outside the workspace copies nothing), and
    the resulting tree is made read-only before the manifest is published. The
    original workspace is not modified in any way.
    """
    root = Path(workspace_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise WorkspaceQuarantineError(f"Workspace to quarantine is not a directory: {root}")
    if root.is_symlink():
        raise WorkspaceQuarantineError(f"Refusing to quarantine a symlinked workspace root: {root}")

    qid = quarantine_id or f"q-{uuid.uuid4().hex[:16]}"
    base = quarantine_root(store_root)
    _wq_ensure_private_dir(Path(store_root).expanduser())
    _wq_ensure_private_dir(base)
    record_dir = base / qid
    if record_dir.exists():
        raise WorkspaceQuarantineError(f"Quarantine {qid!r} already exists.")
    _wq_ensure_private_dir(record_dir)
    tree = record_dir / WQ_TREE_NAME
    _wq_ensure_private_dir(tree)

    files: list[FrozenFile] = []
    for rel_path, absolute, kind in _wq_iter_entries(root):
        destination = tree / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            target = os.readlink(absolute)
            # Recorded, never followed: copying the target's bytes would silently
            # promote content from outside the workspace into the evidence.
            files.append(
                FrozenFile(rel_path=rel_path, kind="symlink", sha256=_wq_sha256_bytes(target.encode()), size=0, link_target=target)
            )
            continue
        data = absolute.read_bytes()
        destination.write_bytes(data)
        files.append(
            FrozenFile(rel_path=rel_path, kind="file", sha256=_wq_sha256_bytes(data), size=len(data))
        )

    manifest = QuarantineManifest(
        quarantine_id=qid,
        workspace_root=str(root.resolve()),
        reason=str(reason or "unspecified"),
        created_at=_wq_now(),
        files=tuple(sorted(files, key=lambda item: item.rel_path)),
        tree_sha256=_wq_tree_digest(files),
    )

    _wq_make_read_only(tree)

    manifest_path = record_dir / WQ_MANIFEST_NAME
    payload = json.dumps(manifest.to_mapping(), sort_keys=True, indent=2) + "\n"
    fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    # The manifest stays owner-writable (0600) while the *frozen tree* is 0400/0500:
    # a release or a later reconstruction may need to annotate the record.
    _wq_restrict_file(manifest_path, 0o600)
    _wq_fsync_dir(record_dir)
    _wq_record_recovery_artifact(store_root, manifest)
    return manifest


def _wq_fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:  # pragma: no cover - platform without directory fds
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


def _wq_record_recovery_artifact(store_root: Path | str, manifest: QuarantineManifest) -> None:
    """Give the frozen tree lineage and pin it so retention cannot collect it (M5-P5-02).

    A quarantine is the only copy of contested bytes, so it is recorded as a
    deliberately untrusted, confidential artifact — somebody may have written it — and
    pinned under ``quarantine:<id>``. `artifact_lineage.plan_retention` refuses to evict
    a pinned artifact, and it independently protects artifacts of any quarantine that is
    still unreleased.
    """
    registry = artifact_lineage.ArtifactRegistry(store_root)
    registry.record_artifact(
        manifest.tree_sha256,
        source_kind="quarantine",
        source_ref=manifest.quarantine_id,
        trust="untrusted",
        confidentiality="secret",
        denied_operations=("export", "prompt"),
    )
    registry.pin(
        f"quarantine:{manifest.quarantine_id}",
        kind="recovery",
        # The pin names the protected *artifact* (the frozen tree digest), so retention
        # can resolve it directly instead of guessing what a quarantine id refers to.
        ref=manifest.tree_sha256,
        reason=f"frozen workspace {manifest.quarantine_id} awaiting reconstruction ({manifest.reason})",
    )


def quarantine_dir(store_root: Path | str, quarantine_id: str) -> Path:
    return quarantine_root(store_root) / _wq_clean_rel(quarantine_id)


def load_manifest(store_root: Path | str, quarantine_id: str) -> QuarantineManifest:
    """Read a manifest, refusing a symlinked or unreadable record."""
    path = quarantine_dir(store_root, quarantine_id) / WQ_MANIFEST_NAME
    if path.is_symlink():
        raise WorkspaceQuarantineIntegrityError(f"Refusing symlinked quarantine manifest: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceQuarantineError(f"Quarantine {quarantine_id!r} not found.") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceQuarantineIntegrityError(f"Quarantine manifest is not valid JSON: {path}") from exc
    if not isinstance(raw, Mapping):
        raise WorkspaceQuarantineIntegrityError(f"Quarantine manifest is not an object: {path}")
    return QuarantineManifest.from_mapping(raw)


def list_quarantines(store_root: Path | str) -> list[QuarantineManifest]:
    """All manifests in the store, newest first. A broken one is skipped, not raised."""
    base = quarantine_root(store_root)
    if not base.exists():
        return []
    found: list[QuarantineManifest] = []
    for record in sorted(base.iterdir()):
        if not record.is_dir() or not (record / WQ_MANIFEST_NAME).exists():
            continue
        try:
            found.append(load_manifest(store_root, record.name))
        except WorkspaceQuarantineError:
            continue
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def find_active_quarantine(store_root: Path | str, workspace_root: Path | str) -> QuarantineManifest | None:
    """The newest quarantine for `workspace_root` that has not been released.

    A returned manifest means the workspace is frozen: `app.py` consults this before
    admitting a mutation, so quarantine is not merely a directory on disk.
    """
    target = str(Path(workspace_root).expanduser().resolve())
    for manifest in list_quarantines(store_root):
        if manifest.workspace_root != target:
            continue
        if _wq_is_released(store_root, manifest.quarantine_id):
            continue
        return manifest
    return None


def _wq_is_released(store_root: Path | str, quarantine_id: str) -> bool:
    return (quarantine_dir(store_root, quarantine_id) / WQ_RELEASE_NAME).exists()


def recovery_protection(store_root: Path | str) -> dict[str, str]:
    """Artifact refs that retention must keep because a quarantine still owns them.

    `artifact_lineage.plan_retention` never evicts a ref it is handed here, and a freeze
    also records its own pin — so protection survives even if a caller forgets to pass
    this through. Both mechanisms exist on purpose: the pin is a record, this is a
    re-derivation from the quarantine store itself.
    """
    protection: dict[str, str] = {}
    for manifest in list_quarantines(store_root):
        if _wq_is_released(store_root, manifest.quarantine_id):
            continue
        protection[manifest.tree_sha256] = (
            f"unreleased quarantine {manifest.quarantine_id} still owns these bytes"
        )
    return protection


def release_quarantine(store_root: Path | str, quarantine_id: str, *, note: str = "") -> None:
    """Mark a quarantine released so mutations of the old workspace may resume.

    The frozen bytes are *not* deleted: releasing only lifts the freeze.
    """
    load_manifest(store_root, quarantine_id)
    record_dir = quarantine_dir(store_root, quarantine_id)
    payload = json.dumps({"quarantine_id": quarantine_id, "released_at": _wq_now(), "note": str(note or "")}, sort_keys=True)
    fd = os.open(record_dir / WQ_RELEASE_NAME, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    # Releasing lifts the retention pin; it never deletes the frozen bytes. A lineage
    # failure here is tolerated on purpose: an un-unpinned artifact stays protected, so
    # the failure direction is "kept", not "collected".
    try:
        artifact_lineage.ArtifactRegistry(store_root).unpin(f"quarantine:{quarantine_id}")
    except artifact_lineage.ArtifactLineageError:
        pass


def _wq_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_reconstruction(
    manifest: QuarantineManifest,
    *,
    baseline_root: Path | str,
    accepted_paths: Sequence[str],
) -> ReconstructionPlan:
    """Classify every difference from the trusted baseline as accepted or ambiguous.

    An accepted path that is not in the frozen tree is an error, not a guess: an
    operator naming a file that was never preserved has misread the evidence, and
    the reconstruction must stop rather than invent it.
    """
    baseline = Path(baseline_root).expanduser()
    if not baseline.is_dir():
        raise WorkspaceQuarantineError(f"Trusted baseline is not a directory: {baseline}")

    frozen = {entry.rel_path: entry for entry in manifest.files}
    accepted: list[str] = []
    for raw in accepted_paths:
        clean = _wq_clean_rel(raw)
        if clean not in frozen:
            raise WorkspaceQuarantineIntegrityError(
                f"Accepted path {clean!r} is not in quarantine {manifest.quarantine_id!r}; "
                "refusing to reconstruct something that was not preserved."
            )
        if clean not in accepted:
            accepted.append(clean)

    accepted_set = set(accepted)
    ambiguous: list[str] = []
    unchanged: list[str] = []
    for rel_path, entry in sorted(frozen.items()):
        candidate = baseline / rel_path
        identical = False
        if entry.kind == "file" and candidate.is_file() and not candidate.is_symlink():
            identical = candidate.stat().st_size == entry.size and _wq_file_sha256(candidate) == entry.sha256
        elif entry.kind == "symlink" and candidate.is_symlink():
            identified_target = os.readlink(candidate)
            identical = identified_target == entry.link_target
        if identical:
            unchanged.append(rel_path)
            continue
        if rel_path in accepted_set:
            continue
        ambiguous.append(rel_path)

    return ReconstructionPlan(
        quarantine_id=manifest.quarantine_id,
        baseline_root=str(baseline.resolve()),
        accepted=tuple(accepted),
        ambiguous=tuple(ambiguous),
        unchanged=tuple(unchanged),
    )


def classify_recoverability(*, host_compromise: bool, isolation_qualified: bool) -> str:
    """Can this workspace be recovered *and verified* on this host?

    A suspected host-level compromise cannot be verified from the compromised host:
    re-running the verification suite there proves only that the attacker allows it.
    Without a qualified isolation boundary the honest answer is that the question is
    unresolvable locally, and that is what this returns.
    """
    if host_compromise and not isolation_qualified:
        return "unresolvable_locally"
    return "recoverable_locally"


def reconstruct_workspace(
    manifest: QuarantineManifest,
    *,
    store_root: Path | str,
    baseline_root: Path | str,
    destination: Path | str,
    accepted_paths: Sequence[str],
    isolation_qualified: bool,
    host_compromise: bool = False,
) -> ReconstructionResult:
    """Build a new workspace from baseline + accepted changes, withholding the rest.

    Never overwrites `destination` (the caller's own work may live there), never
    promotes an unaccepted change, and keeps the ambiguous changes in a sibling tree
    rather than discarding them.
    """
    dest = Path(destination).expanduser()
    if dest.exists():
        raise WorkspaceQuarantineError(
            f"Reconstruction destination already exists: {dest}. Refusing to overwrite user work."
        )
    plan = plan_reconstruction(manifest, baseline_root=baseline_root, accepted_paths=accepted_paths)

    record_dir = quarantine_dir(store_root, manifest.quarantine_id)
    tree = record_dir / WQ_TREE_NAME
    if tree.is_symlink():
        raise WorkspaceQuarantineIntegrityError(f"Refusing symlinked quarantine tree: {tree}")

    # Verify the preserved bytes before trusting them: a quarantine that was edited
    # is not evidence.
    for rel_path in plan.accepted:
        entry = manifest.get(rel_path)
        if entry is None or entry.kind != "file":
            continue
        frozen_path = tree / rel_path
        if frozen_path.is_symlink() or not frozen_path.is_file():
            raise WorkspaceQuarantineIntegrityError(f"Frozen copy missing for accepted path {rel_path!r}.")
        if _wq_file_sha256(frozen_path) != entry.sha256:
            raise WorkspaceQuarantineIntegrityError(
                f"Frozen copy of {rel_path!r} does not match the manifest digest; the quarantine was modified."
            )

    baseline = Path(baseline_root).expanduser().resolve()
    shutil.copytree(baseline, dest, symlinks=True)

    for rel_path in plan.accepted:
        entry = manifest.get(rel_path)
        if entry is None:
            continue
        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "symlink":
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(entry.link_target)
            continue
        shutil.copyfile(tree / rel_path, target)

    ambiguous_dest = dest.with_name(dest.name + WQ_AMBIGUOUS_SUFFIX)
    if plan.ambiguous:
        if ambiguous_dest.exists():
            raise WorkspaceQuarantineError(f"Ambiguous-change destination already exists: {ambiguous_dest}")
        for rel_path in plan.ambiguous:
            entry = manifest.get(rel_path)
            if entry is None or entry.kind != "file":
                continue
            target = ambiguous_dest / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tree / rel_path, target)
        ambiguous_dest.mkdir(parents=True, exist_ok=True)
        (ambiguous_dest / "AMBIGUOUS.md").write_text(
            "# Changes withheld from the reconstructed workspace\n\n"
            f"Quarantine: `{manifest.quarantine_id}` (reason: {manifest.reason})\n\n"
            "These paths differ from the trusted baseline and were not accepted by an\n"
            "operator, so they were copied here instead of into the reconstruction.\n"
            "Review them, then re-run the reconstruction naming the ones you accept.\n\n"
            + "\n".join(f"- `{rel_path}`" for rel_path in plan.ambiguous)
            + "\n",
            encoding="utf-8",
        )

    verification = (
        "verified_under_qualified_isolation" if isolation_qualified else "unqualified_isolation"
    )
    result = ReconstructionResult(
        quarantine_id=manifest.quarantine_id,
        destination=str(dest),
        ambiguous_destination=str(ambiguous_dest) if plan.ambiguous else "",
        restored=plan.accepted,
        ambiguous=plan.ambiguous,
        isolated_verification=verification,
        recoverability=classify_recoverability(
            host_compromise=host_compromise, isolation_qualified=isolation_qualified
        ),
    )
    _wq_ensure_private_dir(record_dir / WQ_RECONSTRUCTIONS_DIRNAME)
    record = record_dir / WQ_RECONSTRUCTIONS_DIRNAME / f"r-{uuid.uuid4().hex[:12]}.json"
    fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(result.to_mapping(), sort_keys=True, indent=2) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return result


__all__ = [
    "FrozenFile",
    "QuarantineManifest",
    "ReconstructionPlan",
    "ReconstructionResult",
    "WQ_AMBIGUOUS_SUFFIX",
    "WQ_SCHEMA_VERSION",
    "WorkspaceQuarantineError",
    "WorkspaceQuarantineIntegrityError",
    "classify_recoverability",
    "find_active_quarantine",
    "freeze_workspace",
    "list_quarantines",
    "load_manifest",
    "plan_reconstruction",
    "quarantine_root",
    "recovery_protection",
    "reconstruct_workspace",
    "release_quarantine",
]
