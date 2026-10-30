"""Pre-edit Git baseline capture, selective CAS user snapshots, and non-destructive restore (M5-P2-01).

This stdlib-only module preserves user work before autonomous edits:

1. **Git baseline & dirty-state capture** (`capture_baseline`):
   - Records `HEAD` commit, `HEAD^{tree}`, symbolic branch, staged index changes
     (`git diff-index --cached` or unborn index entries), unstaged working-tree
     modifications/deletions (`git diff-files`), and relevant untracked user files
     (`git ls-files --others --exclude-standard`) using strictly read-only,
     fixed-argv Git plumbing (`shell=False`).
2. **Selective content-addressed storage** (`ContentAddressedBlobStore`):
   - Snapshots only necessary bytes (dirty staged/unstaged/untracked user files
     at baseline, plus clean target files lazily via `capture_target_before_edit`
     immediately before an edit mutates them) into owner-only (`0600`) SHA-256
     content-addressed blobs written via temp-file + `os.fsync` + `os.replace`.
3. **Ignored & sensitive file preservation without indiscriminate copying**:
   - Skips heavy build/cache trees (`node_modules`, `.venv`, `.git`, `.harnesses`,
     etc.) and **never** copies secret/sensitive files (`.env`, `.secrets`,
     private keys, credentials, tokens) or Git-ignored config files into CAS;
     instead records non-secret in-place metadata + streaming SHA-256 digests so
     sensitive files remain untouched in place and are verified intact on restore.
4. **Disk-full safe pause** (`SnapshotDiskFullError`):
   - Preflights free bytes and catches `ENOSPC` / `EDQUOT` across CAS and manifest
     writes, cleaning up temporary files and surfacing a structured signal so
     callers transition tasks into a safe `paused` state without partial edits.
5. **Verified non-destructive restore** (`restore_snapshot` / `verify_snapshot_state`):
   - Restores working-tree files atomically via directory-fd `O_NOFOLLOW` writes
     and reconciles `.git/index` entries via `git hash-object -w` +
     `git update-index` — **never** invoking `git reset --hard`, `git clean`, or
     `git checkout --force`.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sandbox_boundary

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024  # 2 MiB safety floor before CAS/manifest writes
DEFAULT_MAX_SNAPSHOT_FILE_BYTES = 16 * 1024 * 1024  # 16 MiB per snapshotted user file
DEFAULT_MAX_SCAN_ENTRIES = 25_000

_WSNAP_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WSNAP_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WSNAP_DISK_FULL_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    )
)

SENSITIVE_EXEMPT_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        ".env.test.example",
    }
)

SENSITIVE_EXACT_NAMES: frozenset[str] = frozenset(
    set(sandbox_boundary.SECRET_NAMES)
    | {
        ".secrets",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".dockerconfigjson",
        "application_default_credentials.json",
        "id_ecdsa",
        "id_dsa",
    }
)

SENSITIVE_DIR_NAMES: frozenset[str] = frozenset({".ssh", ".gnupg", ".aws"})
SENSITIVE_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

HEAVY_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".harnesses",
        ".sdlc",
        ".planning",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".tox",
        ".nox",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".turbo",
        "coverage",
    }
)

# Shell command segments that wipe uncommitted user work destructively.
_WSNAP_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\||\n|\r)+")
_WSNAP_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


class WorkspaceSnapshotError(RuntimeError):
    """Base fail-closed error for workspace baseline and snapshot operations."""


class SnapshotDiskFullError(WorkspaceSnapshotError):
    """Raised when disk space or quota is exhausted during snapshot or edit preflight."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "snapshot",
        target_path: str = "",
        errno_code: int = errno.ENOSPC,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.target_path = target_path
        self.errno_code = errno_code


class SnapshotIntegrityError(WorkspaceSnapshotError):
    """Raised when a snapshot manifest or CAS blob fails integrity verification."""


class SnapshotConflictError(WorkspaceSnapshotError):
    """Raised when an operation would overwrite concurrent user work or sensitive files."""


class DestructiveGitOperationError(WorkspaceSnapshotError):
    """Raised when a destructive Git reset/clean/checkout operation is attempted."""


def is_disk_full_os_error(exc: BaseException) -> bool:
    """Return True when ``exc`` represents disk-space or quota exhaustion."""
    if isinstance(exc, SnapshotDiskFullError):
        return True
    if isinstance(exc, OSError) and exc.errno in _WSNAP_DISK_FULL_FULL_SET():
        return True
    return False


def _WSNAP_DISK_FULL_FULL_SET() -> frozenset[int]:
    return _WSNAP_DISK_FULL_ERRNOS


def is_sensitive_filename(name: str) -> bool:
    """Return True if a single path component denotes a secret-bearing file."""
    low = (name or "").strip().lower()
    if not low or low in SENSITIVE_EXEMPT_NAMES:
        return False
    if low in SENSITIVE_EXACT_NAMES:
        return True
    if low.startswith(".env.") or low.startswith("credentials."):
        return True
    if low.endswith(SENSITIVE_SUFFIXES):
        return True
    return False


def is_sensitive_rel_path(rel_path: str) -> bool:
    """Return True if any directory or filename component in ``rel_path`` is sensitive."""
    raw = (rel_path or "").replace("\\", "/").strip("/")
    if not raw:
        return False
    parts = [p for p in raw.split("/") if p and p != "."]
    if not parts:
        return False
    for part in parts[:-1]:
        if part.lower() in SENSITIVE_DIR_NAMES or is_sensitive_filename(part):
            return True
    return is_sensitive_filename(parts[-1])


def detect_destructive_git_command(command: str) -> str | None:
    """Detect destructive workspace-wiping Git shell commands; return reason or None.

    Allows safe read-only or dry-run invocations (e.g., ``git clean -n``,
    ``git status``, ``git diff``, ``git checkout -b feature``) while blocking
    commands that discard uncommitted user work (``git reset --hard``,
    ``git reset --merge``, ``git clean -f``, ``git checkout --force``,
    ``git checkout -- .``, ``git restore .``).
    """
    text = (command or "").strip()
    if not text or "git" not in text:
        return None
    for raw_seg in _WSNAP_SEGMENT_SPLIT_RE.split(text):
        seg = raw_seg.strip().lstrip("(").rstrip(")")
        if not seg:
            continue
        tokens = [tok.strip("\"'") for tok in seg.split() if tok.strip("\"'")]
        while tokens and (
            _WSNAP_ENV_ASSIGN_RE.match(tokens[0])
            or tokens[0] in {"sudo", "env", "command"}
        ):
            tokens.pop(0)
        if not tokens:
            continue
        exe = os.path.basename(tokens[0])
        if exe != "git":
            continue
        # Skip global git flags such as -C <dir>, --git-dir=..., -c k=v
        idx = 1
        while idx < len(tokens):
            tok = tokens[idx]
            if tok in {"-C", "-c", "--git-dir", "--work-tree"}:
                idx += 2
                continue
            if tok.startswith(("--git-dir=", "--work-tree=", "-C", "-c")):
                idx += 1
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            break
        if idx >= len(tokens):
            continue
        sub = tokens[idx]
        rest = tokens[idx + 1 :]
        if sub == "reset":
            for arg in rest:
                if arg in {"--hard", "--merge"}:
                    return f"Blocked destructive Git command `git reset {arg}`; use non-destructive workspace snapshots instead."
        elif sub == "clean":
            has_dry_run = any(
                arg in {"-n", "--dry-run"}
                or (arg.startswith("-") and not arg.startswith("--") and "n" in arg[1:])
                for arg in rest
            )
            has_force = any(
                arg == "--force"
                or (arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:])
                for arg in rest
            )
            if has_force and not has_dry_run:
                return "Blocked destructive Git command `git clean -f`; untracked and ignored user files must be preserved."
        elif sub == "checkout":
            if any(arg in {"-f", "--force"} for arg in rest):
                return "Blocked destructive Git command `git checkout --force`; uncommitted user changes must not be discarded."
            if "--" in rest:
                dd_idx = rest.index("--")
                targets = rest[dd_idx + 1 :]
                if any(t in {".", "./", "*"} for t in targets):
                    return "Blocked blanket `git checkout -- .`; restore individual paths via workspace snapshots instead."
            elif rest == ["."]:
                return "Blocked blanket `git checkout .`; restore individual paths via workspace snapshots instead."
        elif sub == "restore":
            has_staged_only = "--staged" in rest and "--worktree" not in rest and "-W" not in rest
            if not has_staged_only and any(t in {".", "./", "*"} for t in rest if not t.startswith("-")):
                return "Blocked blanket `git restore .` on working tree; use verified workspace snapshot restore instead."
    return None


def assert_non_destructive_git_argv(argv: Sequence[str]) -> None:
    """Fail closed if an internal subprocess argv ever attempts destructive Git reset/clean."""
    if not argv:
        return
    reason = detect_destructive_git_command(" ".join(argv))
    if reason is not None:
        raise DestructiveGitOperationError(reason)


def _wsnap_validate_identifier(value: str, label: str) -> str:
    clean = (value or "").strip()
    if not _WSNAP_SAFE_ID_RE.match(clean) or ".." in clean:
        raise WorkspaceSnapshotError(f"Invalid {label}: {value!r}")
    return clean


def _wsnap_normalize_rel_path(root: Path, raw_path: str) -> tuple[str, tuple[str, ...]]:
    """Validate confinement under ``root`` via ``sandbox_boundary.confined_path`` and return POSIX rel path."""
    cleaned = (raw_path or "").replace("\\", "/").strip()
    if not cleaned or cleaned == ".":
        raise WorkspaceSnapshotError("Snapshot file path must reference a file inside the workspace, not root.")
    try:
        confined = sandbox_boundary.confined_path(root, cleaned)
        root_resolved = root.expanduser().resolve(strict=True)
        rel = confined.relative_to(root_resolved)
    except (sandbox_boundary.SandboxBoundaryError, ValueError, OSError) as exc:
        raise SnapshotConflictError(f"Path {raw_path!r} escapes workspace boundary: {exc}") from exc
    parts = tuple(p for p in rel.parts if p not in ("", "."))
    if not parts:
        raise WorkspaceSnapshotError(f"Path {raw_path!r} resolves to workspace root.")
    return "/".join(parts), parts


def _wsnap_open_root_fd(root: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    canonical = root.expanduser().resolve(strict=True)
    flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, flags)
    try:
        for part in canonical.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _wsnap_read_confined_bytes(
    root: Path,
    rel_parts: tuple[str, ...],
    *,
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_FILE_BYTES,
) -> tuple[bytes, os.stat_result]:
    """Read regular file bytes under ``root`` via directory-anchored ``O_NOFOLLOW`` descriptors."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    current_fd = _wsnap_open_root_fd(root)
    file_fd: int | None = None
    try:
        for part in rel_parts[:-1]:
            next_fd = os.open(part, dir_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(rel_parts[-1], file_flags, dir_fd=current_fd)
        os.close(current_fd)
        current_fd = -1
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise IsADirectoryError("/".join(rel_parts))
        if info.st_size > max_bytes:
            raise WorkspaceSnapshotError(
                f"Workspace file {'/'.join(rel_parts)!r} ({info.st_size} bytes) exceeds snapshot limit ({max_bytes} bytes)."
            )
        with os.fdopen(file_fd, "rb") as fh:
            file_fd = None
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise WorkspaceSnapshotError(
                f"Workspace file {'/'.join(rel_parts)!r} grew beyond snapshot limit ({max_bytes} bytes)."
            )
        return data, info
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if current_fd >= 0:
            os.close(current_fd)


def _wsnap_hash_confined_file_in_place(root: Path, rel_parts: tuple[str, ...]) -> tuple[str, os.stat_result] | None:
    """Compute streaming SHA-256 of a regular file in-place without persisting its bytes anywhere.

    Returns ``None`` if the path does not exist or is a symlink/non-regular entry.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    current_fd = _wsnap_open_root_fd(root)
    file_fd: int | None = None
    try:
        for part in rel_parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=current_fd)
            except OSError:
                return None
            os.close(current_fd)
            current_fd = next_fd
        try:
            file_fd = os.open(rel_parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0), dir_fd=current_fd)
        except OSError:
            return None
        os.close(current_fd)
        current_fd = -1
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        hasher = hashlib.sha256()
        with os.fdopen(file_fd, "rb") as fh:
            file_fd = None
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest(), info
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if current_fd >= 0:
            os.close(current_fd)


def atomic_write_confined_bytes(
    root: Path,
    raw_rel_path: str,
    data: bytes,
    *,
    mode: int | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> str:
    """Atomically write ``data`` to a confined workspace path via anchored ``O_NOFOLLOW`` temp file + fsync."""
    rel_posix, rel_parts = _wsnap_normalize_rel_path(root, raw_rel_path)
    _wsnap_preflight_disk_space(root, needed_bytes=len(data), min_free_bytes=min_free_bytes, operation="workspace_write")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    parent_fd = _wsnap_open_root_fd(root)
    try:
        for part in rel_parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o777, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    if is_disk_full_os_error(exc):
                        raise SnapshotDiskFullError(
                            f"Disk full creating parent directory for {rel_posix!r}: {exc}",
                            operation="workspace_mkdir",
                            target_path=rel_posix,
                            errno_code=int(exc.errno or errno.ENOSPC),
                        ) from exc
                    raise
                next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd

        leaf = rel_parts[-1]
        try:
            existing_st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(existing_st.st_mode):
                raise SnapshotConflictError(f"Refusing to overwrite symlink at {rel_posix!r}.")
            if stat.S_ISDIR(existing_st.st_mode):
                raise SnapshotConflictError(f"Refusing to overwrite directory at {rel_posix!r} with a file.")
        except FileNotFoundError:
            pass

        tmp_name = f".m5-wsnap-{uuid.uuid4().hex}.tmp"
        tmp_fd: int | None = None
        try:
            open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)
            tmp_fd = os.open(tmp_name, open_flags, mode if mode is not None else 0o666, dir_fd=parent_fd)
            if mode is not None:
                os.fchmod(tmp_fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(tmp_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, f"Short write on {rel_posix!r}")
                view = view[written:]
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = None
            os.rename(tmp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        except OSError as exc:
            if is_disk_full_os_error(exc):
                raise SnapshotDiskFullError(
                    f"Disk full while writing workspace file {rel_posix!r}: {exc}",
                    operation="workspace_write",
                    target_path=rel_posix,
                    errno_code=int(exc.errno or errno.ENOSPC),
                ) from exc
            raise
        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(parent_fd)


def _wsnap_unlink_confined_file(root: Path, raw_rel_path: str) -> bool:
    """Unlink a single regular file under ``root`` via anchored ``O_NOFOLLOW`` parent fd."""
    rel_posix, rel_parts = _wsnap_normalize_rel_path(root, raw_rel_path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    parent_fd = _wsnap_open_root_fd(root)
    try:
        for part in rel_parts[:-1]:
            try:
                next_fd = os.open(part, dir_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return False
            os.close(parent_fd)
            parent_fd = next_fd
        leaf = rel_parts[-1]
        try:
            st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode):
            raise SnapshotConflictError(f"Refusing to unlink non-regular entry at {rel_posix!r}.")
        os.unlink(leaf, dir_fd=parent_fd)
        return True
    finally:
        os.close(parent_fd)


def _wsnap_query_free_bytes(target_dir: Path) -> int:
    probe = target_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return int(usage.free)


def _wsnap_preflight_disk_space(
    target_dir: Path,
    *,
    needed_bytes: int,
    min_free_bytes: int,
    operation: str,
) -> None:
    if min_free_bytes <= 0 and needed_bytes <= 0:
        return
    try:
        free_bytes = _wsnap_query_free_bytes(target_dir)
    except OSError as exc:
        if is_disk_full_os_error(exc):
            raise SnapshotDiskFullError(
                f"Disk full probing free space for {operation}: {exc}",
                operation=operation,
                target_path=str(target_dir),
                errno_code=int(exc.errno or errno.ENOSPC),
            ) from exc
        raise
    required = max(0, needed_bytes) + max(0, min_free_bytes)
    if free_bytes < required:
        raise SnapshotDiskFullError(
            f"Insufficient free disk space for {operation}: {free_bytes} bytes free < {required} bytes required.",
            operation=operation,
            target_path=str(target_dir),
            errno_code=errno.ENOSPC,
        )


def _wsnap_atomic_write_private_bytes(
    target_path: Path,
    payload: bytes,
    *,
    min_free_bytes: int,
    operation: str,
) -> None:
    """Atomically write ``0600`` bytes to ``target_path`` with temp-file + fsync + os.replace."""
    parent = target_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        if is_disk_full_os_error(exc):
            raise SnapshotDiskFullError(
                f"Disk full creating snapshot directory {parent}: {exc}",
                operation=operation,
                target_path=str(target_path),
                errno_code=int(exc.errno or errno.ENOSPC),
            ) from exc
        raise
    _wsnap_preflight_disk_space(parent, needed_bytes=len(payload), min_free_bytes=min_free_bytes, operation=operation)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=str(parent))
        tmp_path = Path(raw_tmp)
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, f"Short write on {target_path}")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp_path, target_path)
        tmp_path = None
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except SnapshotDiskFullError:
        raise
    except OSError as exc:
        if is_disk_full_os_error(exc):
            raise SnapshotDiskFullError(
                f"Disk full during {operation} ({target_path}): {exc}",
                operation=operation,
                target_path=str(target_path),
                errno_code=int(exc.errno or errno.ENOSPC),
            ) from exc
        raise WorkspaceSnapshotError(f"Failed writing {target_path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


class ContentAddressedBlobStore:
    """Owner-only (`0600`) SHA-256 content-addressed blob store with read-back verification."""

    def __init__(self, cas_root: Path, *, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES) -> None:
        self.cas_root = cas_root
        self.min_free_bytes = min_free_bytes

    def _blob_path(self, sha256_hex: str) -> Path:
        digest = (sha256_hex or "").strip().lower()
        if not _WSNAP_HEX64_RE.match(digest):
            raise SnapshotIntegrityError(f"Invalid CAS SHA-256 digest: {sha256_hex!r}")
        return self.cas_root / digest[:2] / f"{digest}.blob"

    def has_blob(self, sha256_hex: str) -> bool:
        try:
            path = self._blob_path(sha256_hex)
        except SnapshotIntegrityError:
            return False
        if not path.is_file() or path.is_symlink():
            return False
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        return actual == sha256_hex.strip().lower()

    def put_bytes(self, data: bytes) -> tuple[str, bool]:
        """Store ``data`` under its SHA-256 digest; returns ``(sha256_hex, newly_written)``."""
        digest = hashlib.sha256(data).hexdigest()
        blob_path = self._blob_path(digest)
        if self.has_blob(digest):
            return digest, False
        _wsnap_atomic_write_private_bytes(
            blob_path,
            data,
            min_free_bytes=self.min_free_bytes,
            operation="cas_put_blob",
        )
        return digest, True

    def get_bytes(self, sha256_hex: str) -> bytes:
        """Read and verify a blob from CAS; raises ``SnapshotIntegrityError`` on corruption or absence."""
        digest = (sha256_hex or "").strip().lower()
        blob_path = self._blob_path(digest)
        if not blob_path.is_file() or blob_path.is_symlink():
            raise SnapshotIntegrityError(f"Missing CAS blob {digest}.")
        try:
            payload = blob_path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityError(f"Unreadable CAS blob {digest}: {exc}") from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != digest:
            raise SnapshotIntegrityError(f"CAS blob {digest} failed SHA-256 verification (got {actual}).")
        return payload


@dataclass(frozen=True, slots=True)
class GitBaselineInfo:
    is_git: bool
    git_toplevel: str = ""
    workspace_rel_prefix: str = "."
    has_head: bool = False
    head_commit: str = ""
    head_tree: str = ""
    branch: str = ""
    clean_at_capture: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_git": self.is_git,
            "git_toplevel": self.git_toplevel,
            "workspace_rel_prefix": self.workspace_rel_prefix,
            "has_head": self.has_head,
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "branch": self.branch,
            "clean_at_capture": self.clean_at_capture,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GitBaselineInfo:
        return cls(
            is_git=bool(raw.get("is_git", False)),
            git_toplevel=str(raw.get("git_toplevel") or ""),
            workspace_rel_prefix=str(raw.get("workspace_rel_prefix") or "."),
            has_head=bool(raw.get("has_head", False)),
            head_commit=str(raw.get("head_commit") or ""),
            head_tree=str(raw.get("head_tree") or ""),
            branch=str(raw.get("branch") or ""),
            clean_at_capture=bool(raw.get("clean_at_capture", True)),
        )


@dataclass(frozen=True, slots=True)
class StagedFileEntry:
    rel_path: str
    status: str  # "A", "M", "D", "T"
    git_mode: str = ""
    git_blob_oid: str = ""
    cas_sha256: str | None = None
    size_bytes: int = 0
    head_git_mode: str = ""
    head_git_oid: str = ""
    head_cas_sha256: str | None = None
    sensitive_excluded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "status": self.status,
            "git_mode": self.git_mode,
            "git_blob_oid": self.git_blob_oid,
            "cas_sha256": self.cas_sha256,
            "size_bytes": self.size_bytes,
            "head_git_mode": self.head_git_mode,
            "head_git_oid": self.head_git_oid,
            "head_cas_sha256": self.head_cas_sha256,
            "sensitive_excluded": self.sensitive_excluded,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> StagedFileEntry:
        return cls(
            rel_path=str(raw["rel_path"]),
            status=str(raw.get("status") or "M"),
            git_mode=str(raw.get("git_mode") or ""),
            git_blob_oid=str(raw.get("git_blob_oid") or ""),
            cas_sha256=str(raw["cas_sha256"]) if raw.get("cas_sha256") else None,
            size_bytes=int(raw.get("size_bytes") or 0),
            head_git_mode=str(raw.get("head_git_mode") or ""),
            head_git_oid=str(raw.get("head_git_oid") or ""),
            head_cas_sha256=str(raw["head_cas_sha256"]) if raw.get("head_cas_sha256") else None,
            sensitive_excluded=bool(raw.get("sensitive_excluded", False)),
        )


@dataclass(frozen=True, slots=True)
class WorktreeFileEntry:
    rel_path: str
    category: str  # "unstaged_modified", "unstaged_deleted", "untracked", "non_git_user_file"
    existed: bool
    cas_sha256: str | None = None
    size_bytes: int = 0
    mode: int | None = None
    index_git_mode: str = ""
    index_git_oid: str = ""
    index_cas_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "category": self.category,
            "existed": self.existed,
            "cas_sha256": self.cas_sha256,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "index_git_mode": self.index_git_mode,
            "index_git_oid": self.index_git_oid,
            "index_cas_sha256": self.index_cas_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> WorktreeFileEntry:
        return cls(
            rel_path=str(raw["rel_path"]),
            category=str(raw["category"]),
            existed=bool(raw["existed"]),
            cas_sha256=str(raw["cas_sha256"]) if raw.get("cas_sha256") else None,
            size_bytes=int(raw.get("size_bytes") or 0),
            mode=int(raw["mode"]) if raw.get("mode") is not None else None,
            index_git_mode=str(raw.get("index_git_mode") or ""),
            index_git_oid=str(raw.get("index_git_oid") or ""),
            index_cas_sha256=str(raw["index_cas_sha256"]) if raw.get("index_cas_sha256") else None,
        )


@dataclass(frozen=True, slots=True)
class PreservedFileEntry:
    """Metadata-only record for ignored or sensitive files (bytes are NEVER stored in CAS)."""

    rel_path: str
    reason: str  # "ignored_sensitive", "sensitive_unignored", "git_ignored"
    size_bytes: int
    mode: int
    in_place_sha256: str
    stored_in_cas: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "in_place_sha256": self.in_place_sha256,
            "stored_in_cas": False,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PreservedFileEntry:
        return cls(
            rel_path=str(raw["rel_path"]),
            reason=str(raw.get("reason") or "ignored_sensitive"),
            size_bytes=int(raw.get("size_bytes") or 0),
            mode=int(raw.get("mode") or 0o600),
            in_place_sha256=str(raw.get("in_place_sha256") or ""),
            stored_in_cas=False,
        )


@dataclass(frozen=True, slots=True)
class PreEditTargetEntry:
    """Lazy pre-edit capture of a target path immediately before its first managed write/edit."""

    rel_path: str
    existed_before: bool
    cas_sha256: str | None = None
    size_bytes: int = 0
    mode: int | None = None
    captured_at: float = 0.0
    last_written_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "existed_before": self.existed_before,
            "cas_sha256": self.cas_sha256,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "captured_at": self.captured_at,
            "last_written_sha256": self.last_written_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PreEditTargetEntry:
        return cls(
            rel_path=str(raw["rel_path"]),
            existed_before=bool(raw["existed_before"]),
            cas_sha256=str(raw["cas_sha256"]) if raw.get("cas_sha256") else None,
            size_bytes=int(raw.get("size_bytes") or 0),
            mode=int(raw["mode"]) if raw.get("mode") is not None else None,
            captured_at=float(raw.get("captured_at") or 0.0),
            last_written_sha256=str(raw["last_written_sha256"]) if raw.get("last_written_sha256") else None,
        )


@dataclass(slots=True)
class WorkspaceSnapshotManifest:
    schema_version: int
    snapshot_id: str
    session_id: str
    label: str
    workspace_root: str
    created_at: float
    updated_at: float
    git: GitBaselineInfo
    staged_changes: list[StagedFileEntry] = field(default_factory=list)
    unstaged_changes: list[WorktreeFileEntry] = field(default_factory=list)
    untracked_files: list[WorktreeFileEntry] = field(default_factory=list)
    preserved_files: list[PreservedFileEntry] = field(default_factory=list)
    pre_edit_targets: dict[str, PreEditTargetEntry] = field(default_factory=dict)
    cas_blobs_written: int = 0
    cas_bytes_written: int = 0
    manifest_sha256: str = ""

    def body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "label": self.label,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "git": self.git.to_dict(),
            "staged_changes": [item.to_dict() for item in self.staged_changes],
            "unstaged_changes": [item.to_dict() for item in self.unstaged_changes],
            "untracked_files": [item.to_dict() for item in self.untracked_files],
            "preserved_files": [item.to_dict() for item in self.preserved_files],
            "pre_edit_targets": {
                key: self.pre_edit_targets[key].to_dict()
                for key in sorted(self.pre_edit_targets)
            },
            "cas_blobs_written": self.cas_blobs_written,
            "cas_bytes_written": self.cas_bytes_written,
        }

    def compute_digest(self) -> str:
        canonical = json.dumps(self.body_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        out = self.body_dict()
        out["manifest_sha256"] = self.manifest_sha256 or self.compute_digest()
        return out

    def summary_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "label": self.label,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "git": self.git.to_dict(),
            "counts": {
                "staged_changes": len(self.staged_changes),
                "unstaged_changes": len(self.unstaged_changes),
                "untracked_files": len(self.untracked_files),
                "preserved_files": len(self.preserved_files),
                "preserved_sensitive_files": sum(
                    1 for p in self.preserved_files if "sensitive" in p.reason
                ),
                "pre_edit_targets": len(self.pre_edit_targets),
                "cas_blobs_written": self.cas_blobs_written,
                "cas_bytes_written": self.cas_bytes_written,
            },
            "manifest_sha256": self.manifest_sha256 or self.compute_digest(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> WorkspaceSnapshotManifest:
        pre_targets_raw = raw.get("pre_edit_targets") or {}
        if not isinstance(pre_targets_raw, Mapping):
            raise SnapshotIntegrityError("Invalid pre_edit_targets map in snapshot manifest.")
        inst = cls(
            schema_version=int(raw.get("schema_version") or SNAPSHOT_SCHEMA_VERSION),
            snapshot_id=str(raw["snapshot_id"]),
            session_id=str(raw["session_id"]),
            label=str(raw.get("label") or "baseline"),
            workspace_root=str(raw["workspace_root"]),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            git=GitBaselineInfo.from_dict(raw.get("git") or {}),
            staged_changes=[StagedFileEntry.from_dict(x) for x in (raw.get("staged_changes") or [])],
            unstaged_changes=[WorktreeFileEntry.from_dict(x) for x in (raw.get("unstaged_changes") or [])],
            untracked_files=[WorktreeFileEntry.from_dict(x) for x in (raw.get("untracked_files") or [])],
            preserved_files=[PreservedFileEntry.from_dict(x) for x in (raw.get("preserved_files") or [])],
            pre_edit_targets={
                str(k): PreEditTargetEntry.from_dict(v)
                for k, v in pre_targets_raw.items()
            },
            cas_blobs_written=int(raw.get("cas_blobs_written") or 0),
            cas_bytes_written=int(raw.get("cas_bytes_written") or 0),
            manifest_sha256=str(raw.get("manifest_sha256") or ""),
        )
        expected = inst.compute_digest()
        if not inst.manifest_sha256 or inst.manifest_sha256 != expected:
            raise SnapshotIntegrityError(
                f"Snapshot manifest {inst.snapshot_id!r} failed SHA-256 verification."
            )
        return inst


@dataclass(frozen=True, slots=True)
class SnapshotRestoreReport:
    snapshot_id: str
    session_id: str
    workspace_root: str
    verified: bool
    restored_files: tuple[str, ...]
    removed_created_files: tuple[str, ...]
    restored_deleted_files: tuple[str, ...]
    staged_reconciled: tuple[str, ...]
    preserved_verified: tuple[str, ...]
    head_commit: str
    head_tree: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "verified": self.verified,
            "restored_files": list(self.restored_files),
            "removed_created_files": list(self.removed_created_files),
            "restored_deleted_files": list(self.restored_deleted_files),
            "staged_reconciled": list(self.staged_reconciled),
            "preserved_verified": list(self.preserved_verified),
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
        }


def _wsnap_run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    input_bytes: bytes | None = None,
    timeout: float = 8.0,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    argv = ["git", *args]
    assert_non_destructive_git_argv(argv)
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr_txt = proc.stderr.decode("utf-8", errors="replace").strip()[:240]
        raise WorkspaceSnapshotError(f"Git command {' '.join(argv[:4])} failed ({proc.returncode}): {stderr_txt}")
    return proc


def _wsnap_parse_diff_z(raw: bytes) -> list[tuple[str, str, str, str, str, str]]:
    """Parse ``git diff-index -z --no-renames`` / ``git diff-files -z --no-renames`` output.

    Returns tuples of ``(src_mode, dst_mode, src_oid, dst_oid, status_letter, rel_path)``.
    """
    if not raw:
        return []
    parts = raw.split(b"\x00")
    out: list[tuple[str, str, str, str, str, str]] = []
    idx = 0
    while idx < len(parts):
        meta = parts[idx]
        if not meta:
            idx += 1
            continue
        if not meta.startswith(b":") or idx + 1 >= len(parts):
            idx += 1
            continue
        header = meta[1:].decode("utf-8", errors="replace").split()
        rel_path = parts[idx + 1].decode("utf-8", errors="surrogateescape")
        idx += 2
        if len(header) < 5 or not rel_path:
            continue
        src_mode, dst_mode, src_oid, dst_oid, status_code = (
            header[0],
            header[1],
            header[2],
            header[3],
            header[4][:1],
        )
        out.append((src_mode, dst_mode, src_oid, dst_oid, status_code, rel_path))
    return out


def _wsnap_parse_ls_files_stage_z(raw: bytes) -> dict[str, tuple[str, str, str]]:
    """Parse ``git ls-files --stage -z`` into ``{rel_path: (mode, oid, stage)}`` (stage 0 preferred)."""
    entries: dict[str, tuple[str, str, str]] = {}
    if not raw:
        return entries
    for item in raw.split(b"\x00"):
        if not item or b"\t" not in item:
            continue
        meta_b, path_b = item.split(b"\t", 1)
        meta_parts = meta_b.decode("utf-8", errors="replace").split()
        if len(meta_parts) < 3:
            continue
        mode_s, oid_s, stage_s = meta_parts[0], meta_parts[1], meta_parts[2]
        rel_s = path_b.decode("utf-8", errors="surrogateescape")
        if rel_s not in entries or stage_s == "0":
            entries[rel_s] = (mode_s, oid_s, stage_s)
    return entries


def _wsnap_has_skipped_dir_component(rel_path: str) -> bool:
    parts = [p for p in rel_path.replace("\\", "/").strip("/").split("/") if p and p != "."]
    # Check directory components AND direct match for top-level skip directories
    return any(part in HEAVY_SKIP_DIRS for part in parts)


class WorkspaceSnapshotStore:
    """Durable workspace baseline, selective CAS snapshot, and non-destructive restore engine."""

    def __init__(
        self,
        harness_store_dir: Path,
        *,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_file_bytes: int = DEFAULT_MAX_SNAPSHOT_FILE_BYTES,
        max_scan_entries: int = DEFAULT_MAX_SCAN_ENTRIES,
    ) -> None:
        self.harness_store_dir = harness_store_dir
        self.snapshots_root = harness_store_dir / "snapshots"
        self.sessions_root = self.snapshots_root / "sessions"
        self.min_free_bytes = min_free_bytes
        self.max_file_bytes = max_file_bytes
        self.max_scan_entries = max_scan_entries
        self.cas = ContentAddressedBlobStore(
            self.snapshots_root / "cas",
            min_free_bytes=min_free_bytes,
        )

    def _session_dir(self, session_id: str) -> Path:
        clean_sid = _wsnap_validate_identifier(session_id, "session_id")
        return self.sessions_root / clean_sid

    def _manifest_path(self, session_id: str, snapshot_id: str) -> Path:
        clean_snap = _wsnap_validate_identifier(snapshot_id, "snapshot_id")
        return self._session_dir(session_id) / f"{clean_snap}.json"

    def _pointer_path(self, session_id: str, pointer_name: str, workspace_root: Path | None = None) -> Path:
        if workspace_root is None:
            return self._session_dir(session_id) / f"_{pointer_name}.json"
        ws_str = str(self._validate_workspace_root(workspace_root))
        ws_key = hashlib.sha256(ws_str.encode("utf-8")).hexdigest()[:12]
        return self._session_dir(session_id) / f"_{pointer_name}_{ws_key}.json"

    def _validate_workspace_root(self, workspace_root: Path) -> Path:
        cand = workspace_root.expanduser()
        if cand.is_symlink() or not cand.is_dir():
            raise WorkspaceSnapshotError(f"Workspace root must be a real directory: {workspace_root}")
        resolved = cand.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise WorkspaceSnapshotError("Refusing filesystem root as workspace.")
        return resolved

    def _save_manifest(
        self,
        manifest: WorkspaceSnapshotManifest,
        *,
        set_baseline_pointer: bool = False,
        set_latest_pointer: bool = True,
    ) -> WorkspaceSnapshotManifest:
        manifest.updated_at = time.time()
        manifest.manifest_sha256 = manifest.compute_digest()
        encoded = (json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        target = self._manifest_path(manifest.session_id, manifest.snapshot_id)
        _wsnap_atomic_write_private_bytes(
            target,
            encoded,
            min_free_bytes=self.min_free_bytes,
            operation="save_snapshot_manifest",
        )
        ptr_payload = (
            json.dumps(
                {
                    "snapshot_id": manifest.snapshot_id,
                    "manifest_sha256": manifest.manifest_sha256,
                    "workspace_root": manifest.workspace_root,
                    "updated_at": manifest.updated_at,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        ws_resolved = Path(manifest.workspace_root)
        if set_baseline_pointer:
            _wsnap_atomic_write_private_bytes(
                self._pointer_path(manifest.session_id, "baseline", workspace_root=ws_resolved),
                ptr_payload,
                min_free_bytes=self.min_free_bytes,
                operation="save_baseline_pointer",
            )
            default_base_ptr = self._pointer_path(manifest.session_id, "baseline")
            if not default_base_ptr.exists():
                _wsnap_atomic_write_private_bytes(
                    default_base_ptr,
                    ptr_payload,
                    min_free_bytes=self.min_free_bytes,
                    operation="save_baseline_pointer",
                )
        if set_latest_pointer:
            _wsnap_atomic_write_private_bytes(
                self._pointer_path(manifest.session_id, "latest", workspace_root=ws_resolved),
                ptr_payload,
                min_free_bytes=self.min_free_bytes,
                operation="save_latest_pointer",
            )
            _wsnap_atomic_write_private_bytes(
                self._pointer_path(manifest.session_id, "latest"),
                ptr_payload,
                min_free_bytes=self.min_free_bytes,
                operation="save_latest_pointer",
            )
        return manifest

    def _resolve_pointer_id(self, session_id: str, pointer_name: str, workspace_root: Path | None = None) -> str | None:
        candidates = (
            [self._pointer_path(session_id, pointer_name, workspace_root=workspace_root)]
            if workspace_root is not None
            else [self._pointer_path(session_id, pointer_name)]
        )
        for ptr in candidates:
            if not ptr.is_file() or ptr.is_symlink():
                continue
            try:
                data = json.loads(ptr.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SnapshotIntegrityError(f"Corrupt snapshot pointer {ptr.name}: {exc}") from exc
            if not isinstance(data, dict) or not data.get("snapshot_id"):
                raise SnapshotIntegrityError(f"Malformed snapshot pointer {ptr.name}.")
            if workspace_root is not None:
                ws_str = str(self._validate_workspace_root(workspace_root))
                if str(data.get("workspace_root") or "") != ws_str:
                    continue
            return str(data["snapshot_id"])
        return None

    def load_manifest(
        self,
        session_id: str,
        snapshot_id: str = "baseline",
        *,
        workspace_root: Path | None = None,
    ) -> WorkspaceSnapshotManifest:
        """Load and SHA-256-verify a snapshot manifest (`'baseline'`, `'latest'`, or explicit id)."""
        clean_sid = _wsnap_validate_identifier(session_id, "session_id")
        actual_id = snapshot_id
        if snapshot_id in {"baseline", "latest"}:
            resolved = self._resolve_pointer_id(clean_sid, snapshot_id, workspace_root=workspace_root)
            if resolved is None:
                raise KeyError(f"No {snapshot_id} snapshot recorded for session {clean_sid!r}.")
            actual_id = resolved
        mpath = self._manifest_path(clean_sid, actual_id)
        if not mpath.is_file() or mpath.is_symlink():
            raise KeyError(f"Snapshot {actual_id!r} not found for session {clean_sid!r}.")
        try:
            raw = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError(f"Corrupt snapshot manifest {actual_id!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SnapshotIntegrityError(f"Snapshot manifest {actual_id!r} root must be a JSON object.")
        return WorkspaceSnapshotManifest.from_dict(raw)

    def list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        """Return chronological snapshot summaries for ``session_id``."""
        sdir = self._session_dir(session_id)
        if not sdir.is_dir():
            return []
        baseline_id = self._resolve_pointer_id(session_id, "baseline")
        latest_id = self._resolve_pointer_id(session_id, "latest")
        items: list[WorkspaceSnapshotManifest] = []
        for entry in sorted(sdir.iterdir()):
            if entry.name.startswith("_") or entry.suffix != ".json" or not entry.is_file():
                continue
            items.append(self.load_manifest(session_id, entry.stem))
        items.sort(key=lambda m: (m.created_at, m.snapshot_id))
        summaries: list[dict[str, Any]] = []
        for m in items:
            summary = m.summary_dict()
            summary["is_baseline"] = m.snapshot_id == baseline_id
            summary["is_latest"] = m.snapshot_id == latest_id
            summaries.append(summary)
        return summaries

    def delete_session_snapshots(self, session_id: str) -> None:
        """Remove per-session manifest pointers/files without touching workspace files."""
        sdir = self._session_dir(session_id)
        if sdir.is_dir() and not sdir.is_symlink():
            shutil.rmtree(sdir, ignore_errors=True)

    def inspect_git_baseline(self, workspace_root: Path) -> GitBaselineInfo:
        """Inspect Git HEAD, tree, and symbolic branch without mutating `.git`."""
        ws = self._validate_workspace_root(workspace_root)
        inside = _wsnap_run_git(["rev-parse", "--is-inside-work-tree"], cwd=ws, check=False)
        if inside.returncode != 0 or inside.stdout.decode("utf-8", errors="replace").strip() != "true":
            return GitBaselineInfo(is_git=False)
        top_proc = _wsnap_run_git(["rev-parse", "--show-toplevel"], cwd=ws, check=False)
        if top_proc.returncode != 0:
            return GitBaselineInfo(is_git=False)
        toplevel = Path(top_proc.stdout.decode("utf-8", errors="surrogateescape").strip()).resolve()
        try:
            rel_prefix_path = ws.relative_to(toplevel)
            rel_prefix = "." if str(rel_prefix_path) == "." else rel_prefix_path.as_posix()
        except ValueError:
            rel_prefix = "."

        head_proc = _wsnap_run_git(["rev-parse", "--verify", "HEAD"], cwd=ws, check=False)
        has_head = head_proc.returncode == 0
        head_commit = head_proc.stdout.decode("utf-8", errors="replace").strip() if has_head else ""
        head_tree = ""
        if has_head:
            tree_proc = _wsnap_run_git(["rev-parse", "--verify", "HEAD^{tree}"], cwd=ws, check=False)
            if tree_proc.returncode == 0:
                head_tree = tree_proc.stdout.decode("utf-8", errors="replace").strip()

        sym_proc = _wsnap_run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=ws, check=False)
        if sym_proc.returncode == 0:
            branch = sym_proc.stdout.decode("utf-8", errors="replace").strip()
        elif has_head:
            branch = "DETACHED"
        else:
            branch = ""
        return GitBaselineInfo(
            is_git=True,
            git_toplevel=str(toplevel),
            workspace_rel_prefix=rel_prefix,
            has_head=has_head,
            head_commit=head_commit,
            head_tree=head_tree,
            branch=branch,
            clean_at_capture=True,
        )

    def _record_preserved_in_place(
        self,
        ws: Path,
        rel_posix: str,
        rel_parts: tuple[str, ...],
        *,
        reason: str,
        preserved_by_path: dict[str, PreservedFileEntry],
    ) -> None:
        if rel_posix in preserved_by_path:
            return
        hashed = _wsnap_hash_confined_file_in_place(ws, rel_parts)
        if hashed is None:
            return
        digest, info = hashed
        preserved_by_path[rel_posix] = PreservedFileEntry(
            rel_path=rel_posix,
            reason=reason,
            size_bytes=int(info.st_size),
            mode=stat.S_IMODE(info.st_mode),
            in_place_sha256=digest,
            stored_in_cas=False,
        )

    def _walk_bounded_ignored_dir(
        self,
        ws: Path,
        rel_dir_posix: str,
        *,
        preserved_by_path: dict[str, PreservedFileEntry],
        budget_remaining: list[int],
    ) -> None:
        """Scan small non-cache ignored directories (e.g. ``secrets/``, ``config/``) for sensitive/user files."""
        if _wsnap_has_skipped_dir_component(rel_dir_posix) or budget_remaining[0] <= 0:
            return
        try:
            _, dir_parts = _wsnap_normalize_rel_path(ws, rel_dir_posix)
        except WorkspaceSnapshotError:
            return
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        root_fd = _wsnap_open_root_fd(ws)
        try:
            cur_fd = root_fd
            root_fd = -1
            for part in dir_parts:
                try:
                    nxt = os.open(part, dir_flags, dir_fd=cur_fd)
                except OSError:
                    os.close(cur_fd)
                    return
                os.close(cur_fd)
                cur_fd = nxt

            def _scan_fd(dfd: int, prefix: tuple[str, ...]) -> None:
                if budget_remaining[0] <= 0:
                    return
                try:
                    names = sorted(os.listdir(dfd))
                except OSError:
                    return
                for name in names:
                    if budget_remaining[0] <= 0:
                        return
                    budget_remaining[0] -= 1
                    if name in HEAVY_SKIP_DIRS:
                        continue
                    try:
                        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        continue
                    child_parts = (*prefix, name)
                    child_posix = "/".join(child_parts)
                    if stat.S_ISREG(st.st_mode):
                        reason = "ignored_sensitive" if is_sensitive_rel_path(child_posix) else "git_ignored"
                        self._record_preserved_in_place(
                            ws,
                            child_posix,
                            child_parts,
                            reason=reason,
                            preserved_by_path=preserved_by_path,
                        )
                    elif stat.S_ISDIR(st.st_mode):
                        sub_fd: int | None = None
                        try:
                            sub_fd = os.open(name, dir_flags, dir_fd=dfd)
                            _scan_fd(sub_fd, child_parts)
                        except OSError:
                            continue
                        finally:
                            if sub_fd is not None:
                                os.close(sub_fd)

            try:
                _scan_fd(cur_fd, dir_parts)
            finally:
                os.close(cur_fd)
        finally:
            if root_fd >= 0:
                os.close(root_fd)

    def capture_baseline(
        self,
        session_id: str,
        workspace_root: Path,
        *,
        label: str = "pre_edit_baseline",
        snapshot_id: str | None = None,
        set_as_baseline_if_missing: bool = True,
    ) -> WorkspaceSnapshotManifest:
        """Capture Git HEAD/tree, staged/unstaged/untracked changes into CAS, and preserve ignored secrets."""
        clean_sid = _wsnap_validate_identifier(session_id, "session_id")
        ws = self._validate_workspace_root(workspace_root)
        snap_id = (
            _wsnap_validate_identifier(snapshot_id, "snapshot_id")
            if snapshot_id
            else f"wsnap-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        )
        _wsnap_preflight_disk_space(
            self.snapshots_root,
            needed_bytes=4096,
            min_free_bytes=self.min_free_bytes,
            operation="capture_baseline",
        )

        git_info = self.inspect_git_baseline(ws)
        staged_changes: list[StagedFileEntry] = []
        unstaged_changes: list[WorktreeFileEntry] = []
        untracked_files: list[WorktreeFileEntry] = []
        preserved_by_path: dict[str, PreservedFileEntry] = {}
        cas_blobs_written = 0
        cas_bytes_written = 0

        def _put_cas(payload: bytes) -> str:
            nonlocal cas_blobs_written, cas_bytes_written
            digest, written = self.cas.put_bytes(payload)
            if written:
                cas_blobs_written += 1
                cas_bytes_written += len(payload)
            return digest

        if git_info.is_git:
            stage_proc = _wsnap_run_git(["ls-files", "--stage", "-z", "--", "."], cwd=ws)
            index_stage = _wsnap_parse_ls_files_stage_z(stage_proc.stdout)

            # 1. Staged index deltas vs HEAD (or all staged entries when HEAD is unborn)
            if git_info.has_head:
                cached_proc = _wsnap_run_git(
                    ["diff-index", "--cached", "--relative", "--no-renames", "-z", "HEAD", "--", "."],
                    cwd=ws,
                )
                cached_deltas = _wsnap_parse_diff_z(cached_proc.stdout)
            else:
                cached_deltas = [
                    ("000000", mode_s, "0" * 40, oid_s, "A", rel_s)
                    for rel_s, (mode_s, oid_s, _stg) in sorted(index_stage.items())
                ]

            for src_mode, dst_mode, src_oid, dst_oid, status_code, raw_rel in cached_deltas:
                if _wsnap_has_skipped_dir_component(raw_rel):
                    continue
                try:
                    rel_posix, rel_parts = _wsnap_normalize_rel_path(ws, raw_rel)
                except WorkspaceSnapshotError:
                    continue
                if is_sensitive_rel_path(rel_posix):
                    self._record_preserved_in_place(
                        ws,
                        rel_posix,
                        rel_parts,
                        reason="sensitive_unignored",
                        preserved_by_path=preserved_by_path,
                    )
                    staged_changes.append(
                        StagedFileEntry(
                            rel_path=rel_posix,
                            status=status_code,
                            git_mode=dst_mode if status_code != "D" else "",
                            git_blob_oid=dst_oid if status_code != "D" else "",
                            cas_sha256=None,
                            size_bytes=0,
                            head_git_mode=src_mode if src_mode != "000000" else "",
                            head_git_oid=src_oid if set(src_oid) != {"0"} else "",
                            head_cas_sha256=None,
                            sensitive_excluded=True,
                        )
                    )
                    continue

                head_cas_digest: str | None = None
                if src_mode not in ("", "000000", "120000", "160000") and set(src_oid) != {"0"}:
                    cat_head = _wsnap_run_git(["cat-file", "blob", src_oid], cwd=ws, check=False)
                    if cat_head.returncode == 0 and len(cat_head.stdout) <= self.max_file_bytes:
                        head_cas_digest = _put_cas(cat_head.stdout)

                if status_code == "D":
                    staged_changes.append(
                        StagedFileEntry(
                            rel_path=rel_posix,
                            status="D",
                            git_mode="",
                            git_blob_oid="",
                            cas_sha256=None,
                            size_bytes=0,
                            head_git_mode=src_mode,
                            head_git_oid=src_oid,
                            head_cas_sha256=head_cas_digest,
                        )
                    )
                elif dst_mode in ("120000", "160000"):
                    staged_changes.append(
                        StagedFileEntry(
                            rel_path=rel_posix,
                            status=status_code,
                            git_mode=dst_mode,
                            git_blob_oid=dst_oid,
                            cas_sha256=None,
                            size_bytes=0,
                            head_git_mode=src_mode if src_mode != "000000" else "",
                            head_git_oid=src_oid if set(src_oid) != {"0"} else "",
                            head_cas_sha256=head_cas_digest,
                        )
                    )
                else:
                    cat_idx = _wsnap_run_git(["cat-file", "blob", dst_oid], cwd=ws)
                    staged_bytes = cat_idx.stdout
                    if len(staged_bytes) > self.max_file_bytes:
                        raise WorkspaceSnapshotError(
                            f"Staged file {rel_posix!r} ({len(staged_bytes)} bytes) exceeds snapshot limit."
                        )
                    cas_digest = _put_cas(staged_bytes)
                    staged_changes.append(
                        StagedFileEntry(
                            rel_path=rel_posix,
                            status=status_code,
                            git_mode=dst_mode,
                            git_blob_oid=dst_oid,
                            cas_sha256=cas_digest,
                            size_bytes=len(staged_bytes),
                            head_git_mode=src_mode if src_mode != "000000" else "",
                            head_git_oid=src_oid if set(src_oid) != {"0"} else "",
                            head_cas_sha256=head_cas_digest,
                        )
                    )

            # 2. Unstaged working-tree deltas vs index
            wt_proc = _wsnap_run_git(["diff-files", "--relative", "--no-renames", "-z", "--", "."], cwd=ws)
            for idx_mode, _wt_mode, idx_oid, _wt_oid, status_code, raw_rel in _wsnap_parse_diff_z(wt_proc.stdout):
                if _wsnap_has_skipped_dir_component(raw_rel):
                    continue
                try:
                    rel_posix, rel_parts = _wsnap_normalize_rel_path(ws, raw_rel)
                except WorkspaceSnapshotError:
                    continue
                if is_sensitive_rel_path(rel_posix):
                    self._record_preserved_in_place(
                        ws,
                        rel_posix,
                        rel_parts,
                        reason="sensitive_unignored",
                        preserved_by_path=preserved_by_path,
                    )
                    continue

                idx_cas_digest: str | None = None
                if idx_mode not in ("", "000000", "120000", "160000") and set(idx_oid) != {"0"}:
                    cat_i = _wsnap_run_git(["cat-file", "blob", idx_oid], cwd=ws, check=False)
                    if cat_i.returncode == 0 and len(cat_i.stdout) <= self.max_file_bytes:
                        idx_cas_digest = _put_cas(cat_i.stdout)

                if status_code == "D":
                    unstaged_changes.append(
                        WorktreeFileEntry(
                            rel_path=rel_posix,
                            category="unstaged_deleted",
                            existed=False,
                            cas_sha256=None,
                            size_bytes=0,
                            mode=None,
                            index_git_mode=idx_mode,
                            index_git_oid=idx_oid,
                            index_cas_sha256=idx_cas_digest,
                        )
                    )
                    continue

                try:
                    wt_bytes, wt_st = _wsnap_read_confined_bytes(ws, rel_parts, max_bytes=self.max_file_bytes)
                except FileNotFoundError:
                    unstaged_changes.append(
                        WorktreeFileEntry(
                            rel_path=rel_posix,
                            category="unstaged_deleted",
                            existed=False,
                            cas_sha256=None,
                            size_bytes=0,
                            mode=None,
                            index_git_mode=idx_mode,
                            index_git_oid=idx_oid,
                            index_cas_sha256=idx_cas_digest,
                        )
                    )
                    continue
                except (IsADirectoryError, OSError):
                    continue

                wt_digest = _put_cas(wt_bytes)
                unstaged_changes.append(
                    WorktreeFileEntry(
                        rel_path=rel_posix,
                        category="unstaged_modified",
                        existed=True,
                        cas_sha256=wt_digest,
                        size_bytes=len(wt_bytes),
                        mode=stat.S_IMODE(wt_st.st_mode),
                        index_git_mode=idx_mode,
                        index_git_oid=idx_oid,
                        index_cas_sha256=idx_cas_digest,
                    )
                )

            # 3. Relevant untracked user files (excluding ignored & heavy cache directories)
            untracked_proc = _wsnap_run_git(
                ["ls-files", "--others", "--exclude-standard", "-z", "--", "."],
                cwd=ws,
            )
            untracked_raw = [
                tok.decode("utf-8", errors="surrogateescape")
                for tok in untracked_proc.stdout.split(b"\x00")
                if tok
            ]
            if len(untracked_raw) > self.max_scan_entries:
                raise WorkspaceSnapshotError(
                    f"Untracked file count ({len(untracked_raw)}) exceeds scan budget ({self.max_scan_entries})."
                )
            for raw_rel in sorted(untracked_raw):
                if _wsnap_has_skipped_dir_component(raw_rel):
                    continue
                try:
                    rel_posix, rel_parts = _wsnap_normalize_rel_path(ws, raw_rel)
                except WorkspaceSnapshotError:
                    continue
                if is_sensitive_rel_path(rel_posix):
                    self._record_preserved_in_place(
                        ws,
                        rel_posix,
                        rel_parts,
                        reason="sensitive_unignored",
                        preserved_by_path=preserved_by_path,
                    )
                    continue
                try:
                    u_bytes, u_st = _wsnap_read_confined_bytes(ws, rel_parts, max_bytes=self.max_file_bytes)
                except (FileNotFoundError, IsADirectoryError, OSError):
                    continue
                u_digest = _put_cas(u_bytes)
                untracked_files.append(
                    WorktreeFileEntry(
                        rel_path=rel_posix,
                        category="untracked",
                        existed=True,
                        cas_sha256=u_digest,
                        size_bytes=len(u_bytes),
                        mode=stat.S_IMODE(u_st.st_mode),
                    )
                )

            # 4. Ignored sensitive & local files (--directory avoids walking node_modules / .venv)
            ignored_proc = _wsnap_run_git(
                ["ls-files", "--others", "-i", "--exclude-standard", "--directory", "-z", "--", "."],
                cwd=ws,
                check=False,
            )
            scan_budget = [min(self.max_scan_entries, 2_000)]
            if ignored_proc.returncode == 0:
                for tok in ignored_proc.stdout.split(b"\x00"):
                    if not tok or scan_budget[0] <= 0:
                        continue
                    raw_ign = tok.decode("utf-8", errors="surrogateescape")
                    is_dir_entry = raw_ign.endswith("/")
                    trimmed = raw_ign.rstrip("/")
                    if not trimmed or _wsnap_has_skipped_dir_component(trimmed):
                        continue
                    if is_dir_entry:
                        self._walk_bounded_ignored_dir(
                            ws,
                            trimmed,
                            preserved_by_path=preserved_by_path,
                            budget_remaining=scan_budget,
                        )
                    else:
                        scan_budget[0] -= 1
                        try:
                            rel_posix, rel_parts = _wsnap_normalize_rel_path(ws, trimmed)
                        except WorkspaceSnapshotError:
                            continue
                        reason = "ignored_sensitive" if is_sensitive_rel_path(rel_posix) else "git_ignored"
                        self._record_preserved_in_place(
                            ws,
                            rel_posix,
                            rel_parts,
                            reason=reason,
                            preserved_by_path=preserved_by_path,
                        )

            git_info = GitBaselineInfo(
                is_git=True,
                git_toplevel=git_info.git_toplevel,
                workspace_rel_prefix=git_info.workspace_rel_prefix,
                has_head=git_info.has_head,
                head_commit=git_info.head_commit,
                head_tree=git_info.head_tree,
                branch=git_info.branch,
                clean_at_capture=not (staged_changes or unstaged_changes or untracked_files),
            )
        else:
            # Non-Git workspace: bounded O_NOFOLLOW walk snapshotting non-secret user files
            self._capture_non_git_workspace(
                ws,
                untracked_files=untracked_files,
                preserved_by_path=preserved_by_path,
                put_cas=_put_cas,
            )

        now = time.time()
        manifest = WorkspaceSnapshotManifest(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=snap_id,
            session_id=clean_sid,
            label=label,
            workspace_root=str(ws),
            created_at=now,
            updated_at=now,
            git=git_info,
            staged_changes=sorted(staged_changes, key=lambda e: e.rel_path),
            unstaged_changes=sorted(unstaged_changes, key=lambda e: e.rel_path),
            untracked_files=sorted(untracked_files, key=lambda e: e.rel_path),
            preserved_files=[preserved_by_path[k] for k in sorted(preserved_by_path)],
            pre_edit_targets={},
            cas_blobs_written=cas_blobs_written,
            cas_bytes_written=cas_bytes_written,
        )
        has_existing_baseline = self._resolve_pointer_id(clean_sid, "baseline", workspace_root=ws) is not None
        return self._save_manifest(
            manifest,
            set_baseline_pointer=(set_as_baseline_if_missing and not has_existing_baseline),
            set_latest_pointer=True,
        )

    def _capture_non_git_workspace(
        self,
        ws: Path,
        *,
        untracked_files: list[WorktreeFileEntry],
        preserved_by_path: dict[str, PreservedFileEntry],
        put_cas: Any,
    ) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        dir_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        scanned = 0
        root_fd = _wsnap_open_root_fd(ws)

        def _walk(dfd: int, prefix: tuple[str, ...]) -> None:
            nonlocal scanned
            names = sorted(os.listdir(dfd))
            for name in names:
                scanned += 1
                if scanned > self.max_scan_entries:
                    raise WorkspaceSnapshotError(
                        f"Non-Git workspace entry count exceeds scan budget ({self.max_scan_entries})."
                    )
                if name in HEAVY_SKIP_DIRS:
                    continue
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    continue
                rel_parts = (*prefix, name)
                rel_posix = "/".join(rel_parts)
                if stat.S_ISREG(st.st_mode):
                    if is_sensitive_rel_path(rel_posix):
                        self._record_preserved_in_place(
                            ws,
                            rel_posix,
                            rel_parts,
                            reason="ignored_sensitive",
                            preserved_by_path=preserved_by_path,
                        )
                    else:
                        f_bytes, f_st = _wsnap_read_confined_bytes(ws, rel_parts, max_bytes=self.max_file_bytes)
                        digest = put_cas(f_bytes)
                        untracked_files.append(
                            WorktreeFileEntry(
                                rel_path=rel_posix,
                                category="non_git_user_file",
                                existed=True,
                                cas_sha256=digest,
                                size_bytes=len(f_bytes),
                                mode=stat.S_IMODE(f_st.st_mode),
                            )
                        )
                elif stat.S_ISDIR(st.st_mode):
                    if name.lower() in SENSITIVE_DIR_NAMES:
                        self._walk_bounded_ignored_dir(
                            ws,
                            rel_posix,
                            preserved_by_path=preserved_by_path,
                            budget_remaining=[200],
                        )
                        continue
                    sub_fd: int | None = None
                    try:
                        sub_fd = os.open(name, dir_flags, dir_fd=dfd)
                        _walk(sub_fd, rel_parts)
                    finally:
                        if sub_fd is not None:
                            os.close(sub_fd)

        try:
            _walk(root_fd, ())
        finally:
            os.close(root_fd)

    def ensure_baseline(
        self,
        session_id: str,
        workspace_root: Path,
        *,
        label: str = "pre_edit_baseline",
    ) -> WorkspaceSnapshotManifest:
        """Return the existing baseline manifest for ``(session_id, workspace_root)`` or capture one."""
        ws = self._validate_workspace_root(workspace_root)
        existing_id = self._resolve_pointer_id(session_id, "baseline", workspace_root=ws)
        if existing_id is not None:
            return self.load_manifest(session_id, existing_id)
        return self.capture_baseline(session_id, ws, label=label, set_as_baseline_if_missing=True)

    def capture_target_before_edit(
        self,
        session_id: str,
        workspace_root: Path,
        raw_rel_path: str,
    ) -> PreEditTargetEntry:
        """Lazily snapshot a target file's pre-edit bytes into CAS before first mutation in a session."""
        ws = self._validate_workspace_root(workspace_root)
        rel_posix, rel_parts = _wsnap_normalize_rel_path(ws, raw_rel_path)
        if is_sensitive_rel_path(rel_posix):
            raise SnapshotConflictError(
                f"Refusing managed edit on sensitive path {rel_posix!r}; sensitive files are preserved in place."
            )
        baseline = self.ensure_baseline(session_id, ws)
        latest_id = self._resolve_pointer_id(session_id, "latest", workspace_root=ws) or baseline.snapshot_id
        latest = baseline if latest_id == baseline.snapshot_id else self.load_manifest(session_id, latest_id)

        need_baseline = rel_posix not in baseline.pre_edit_targets
        need_latest = latest.snapshot_id != baseline.snapshot_id and rel_posix not in latest.pre_edit_targets
        if not need_baseline and not need_latest:
            return baseline.pre_edit_targets[rel_posix]

        now = time.time()
        try:
            prior_bytes, prior_st = _wsnap_read_confined_bytes(ws, rel_parts, max_bytes=self.max_file_bytes)
        except FileNotFoundError:
            current_entry = PreEditTargetEntry(
                rel_path=rel_posix,
                existed_before=False,
                cas_sha256=None,
                size_bytes=0,
                mode=None,
                captured_at=now,
            )
            written = False
        else:
            digest, written = self.cas.put_bytes(prior_bytes)
            current_entry = PreEditTargetEntry(
                rel_path=rel_posix,
                existed_before=True,
                cas_sha256=digest,
                size_bytes=len(prior_bytes),
                mode=stat.S_IMODE(prior_st.st_mode),
                captured_at=now,
            )

        if need_baseline:
            if written and current_entry.existed_before:
                baseline.cas_blobs_written += 1
                baseline.cas_bytes_written += current_entry.size_bytes
            baseline.pre_edit_targets[rel_posix] = current_entry
            self._save_manifest(
                baseline,
                set_baseline_pointer=False,
                set_latest_pointer=(latest.snapshot_id == baseline.snapshot_id),
            )
        if need_latest:
            if written and current_entry.existed_before and not need_baseline:
                latest.cas_blobs_written += 1
                latest.cas_bytes_written += current_entry.size_bytes
            latest.pre_edit_targets[rel_posix] = current_entry
            self._save_manifest(latest, set_baseline_pointer=False, set_latest_pointer=True)
        return baseline.pre_edit_targets[rel_posix]

    def record_target_after_edit(
        self,
        session_id: str,
        workspace_root: Path,
        raw_rel_path: str,
        written_sha256: str,
    ) -> None:
        """Record the SHA-256 written by a managed edit so restore detects concurrent user modifications."""
        ws = self._validate_workspace_root(workspace_root)
        rel_posix, _ = _wsnap_normalize_rel_path(ws, raw_rel_path)
        baseline_id = self._resolve_pointer_id(session_id, "baseline", workspace_root=ws)
        if baseline_id is None:
            return
        latest_id = self._resolve_pointer_id(session_id, "latest", workspace_root=ws)
        seen: set[str] = set()
        for idx_snap in (baseline_id, latest_id):
            if not idx_snap or idx_snap in seen:
                continue
            seen.add(idx_snap)
            manifest = self.load_manifest(session_id, idx_snap)
            prior = manifest.pre_edit_targets.get(rel_posix)
            if prior is None:
                continue
            manifest.pre_edit_targets[rel_posix] = PreEditTargetEntry(
                rel_path=prior.rel_path,
                existed_before=prior.existed_before,
                cas_sha256=prior.cas_sha256,
                size_bytes=prior.size_bytes,
                mode=prior.mode,
                captured_at=prior.captured_at,
                last_written_sha256=written_sha256,
            )
            self._save_manifest(
                manifest,
                set_baseline_pointer=False,
                set_latest_pointer=(idx_snap == latest_id),
            )

    def _repo_rel_for_ws_rel(self, git_info: GitBaselineInfo, ws_rel_posix: str) -> str:
        prefix = git_info.workspace_rel_prefix
        if not prefix or prefix == ".":
            return ws_rel_posix
        return f"{prefix.strip('/')}/{ws_rel_posix.lstrip('/')}"

    def restore_snapshot(
        self,
        session_id: str,
        snapshot_id: str = "baseline",
        *,
        target_root: Path | None = None,
        allow_head_drift: bool = False,
        force_overwrite_concurrent: bool = False,
    ) -> SnapshotRestoreReport:
        """Non-destructively restore baseline working-tree, staged Git index, and untracked files.

        Never invokes ``git reset --hard``, ``git clean``, or ``git checkout --force``.
        Preserves ignored sensitive files in place and verifies the restored state.
        """
        manifest = self.load_manifest(session_id, snapshot_id, workspace_root=target_root)
        ws = self._validate_workspace_root(target_root or Path(manifest.workspace_root))
        if str(ws) != str(Path(manifest.workspace_root).resolve()):
            raise SnapshotConflictError(
                f"Target workspace {ws} does not match manifest workspace {manifest.workspace_root}."
            )

        preserved_map = {p.rel_path: p for p in manifest.preserved_files}
        staged_map = {s.rel_path: s for s in manifest.staged_changes}
        unstaged_map = {u.rel_path: u for u in manifest.unstaged_changes}
        untracked_map = {u.rel_path: u for u in manifest.untracked_files}

        # 1. Verify Git HEAD safety before touching any file
        live_git = self.inspect_git_baseline(ws)
        if manifest.git.is_git:
            if not live_git.is_git:
                raise SnapshotConflictError("Workspace is no longer a Git repository; refusing restore.")
            if not allow_head_drift and manifest.git.has_head != live_git.has_head:
                raise SnapshotConflictError("Git HEAD state changed since snapshot capture; refusing destructive restore.")
            if not allow_head_drift and manifest.git.has_head and live_git.head_commit != manifest.git.head_commit:
                raise SnapshotConflictError(
                    f"Git HEAD moved ({manifest.git.head_commit[:12]} -> {live_git.head_commit[:12]}); refusing restore."
                )

        # 2. Build deterministic working-tree restore plan:
        #    desired_wt[path] = (cas_sha256_or_None_if_absent, mode_or_None, origin_tag)
        desired_wt: dict[str, tuple[str | None, int | None, str]] = {}

        for rel_p, pre in manifest.pre_edit_targets.items():
            if rel_p in preserved_map or is_sensitive_rel_path(rel_p):
                raise SnapshotConflictError(f"Refusing to restore over sensitive/preserved file {rel_p!r}.")
            if pre.existed_before:
                desired_wt[rel_p] = (pre.cas_sha256, pre.mode, "pre_edit_target")
            else:
                desired_wt[rel_p] = (None, None, "agent_created")

        for rel_p, st_entry in staged_map.items():
            if st_entry.sensitive_excluded or rel_p in preserved_map:
                continue
            if st_entry.status == "D":
                desired_wt[rel_p] = (None, None, "staged_deleted")
            elif st_entry.cas_sha256 is not None:
                mode_int = 0o755 if st_entry.git_mode == "100755" else 0o644
                desired_wt[rel_p] = (st_entry.cas_sha256, mode_int, "staged")

        for rel_p, u_entry in unstaged_map.items():
            if rel_p in preserved_map:
                continue
            if not u_entry.existed or u_entry.category == "unstaged_deleted":
                desired_wt[rel_p] = (None, None, "unstaged_deleted")
            else:
                desired_wt[rel_p] = (u_entry.cas_sha256, u_entry.mode, "unstaged_modified")

        for rel_p, ut_entry in untracked_map.items():
            if rel_p in preserved_map:
                continue
            desired_wt[rel_p] = (ut_entry.cas_sha256, ut_entry.mode, "untracked")

        # 3. Pre-verify EVERY required CAS blob and concurrent-modification safety BEFORE mutating workspace
        blob_cache: dict[str, bytes] = {}
        total_write_bytes = 0
        for rel_p, (cas_digest, _mode, origin_tag) in sorted(desired_wt.items()):
            _, rel_parts = _wsnap_normalize_rel_path(ws, rel_p)
            if cas_digest is not None:
                if cas_digest not in blob_cache:
                    blob_cache[cas_digest] = self.cas.get_bytes(cas_digest)
                total_write_bytes += len(blob_cache[cas_digest])
            elif origin_tag == "agent_created" and not force_overwrite_concurrent:
                pre_t = manifest.pre_edit_targets.get(rel_p)
                if pre_t is not None and pre_t.last_written_sha256:
                    live_hash = _wsnap_hash_confined_file_in_place(ws, rel_parts)
                    if live_hash is not None and live_hash[0] != pre_t.last_written_sha256:
                        raise SnapshotConflictError(
                            f"Refusing to remove {rel_p!r}: file was modified outside recorded agent edit."
                        )

        for st_entry in manifest.staged_changes:
            if st_entry.cas_sha256 and st_entry.cas_sha256 not in blob_cache:
                blob_cache[st_entry.cas_sha256] = self.cas.get_bytes(st_entry.cas_sha256)

        _wsnap_preflight_disk_space(
            ws,
            needed_bytes=total_write_bytes,
            min_free_bytes=self.min_free_bytes,
            operation="restore_snapshot",
        )

        # 4. Apply working-tree restores atomically (never touching preserved/ignored or unrecorded files)
        restored_files: list[str] = []
        removed_created_files: list[str] = []
        restored_deleted_files: list[str] = []

        for rel_p, (cas_digest, target_mode, origin_tag) in sorted(desired_wt.items()):
            _, rel_parts = _wsnap_normalize_rel_path(ws, rel_p)
            if cas_digest is None:
                removed = _wsnap_unlink_confined_file(ws, rel_p)
                if removed:
                    if origin_tag == "agent_created":
                        removed_created_files.append(rel_p)
                    else:
                        restored_deleted_files.append(rel_p)
            else:
                target_bytes = blob_cache[cas_digest]
                live_hash = _wsnap_hash_confined_file_in_place(ws, rel_parts)
                live_same = (
                    live_hash is not None
                    and live_hash[0] == cas_digest
                    and (target_mode is None or stat.S_IMODE(live_hash[1].st_mode) == target_mode)
                )
                if not live_same:
                    atomic_write_confined_bytes(
                        ws,
                        rel_p,
                        target_bytes,
                        mode=target_mode,
                        min_free_bytes=self.min_free_bytes,
                    )
                    restored_files.append(rel_p)

        # 5. Reconcile `.git/index` staged entries via plumbing (`git hash-object -w` + `git update-index`)
        staged_reconciled: list[str] = []
        if manifest.git.is_git:
            staged_reconciled = self._reconcile_git_index(ws, manifest, blob_cache)

        # 6. Verify restored working tree, Git HEAD/index, and preserved sensitive files
        preserved_verified = self.verify_snapshot_state(manifest, target_root=ws)
        return SnapshotRestoreReport(
            snapshot_id=manifest.snapshot_id,
            session_id=manifest.session_id,
            workspace_root=str(ws),
            verified=True,
            restored_files=tuple(restored_files),
            removed_created_files=tuple(removed_created_files),
            restored_deleted_files=tuple(restored_deleted_files),
            staged_reconciled=tuple(staged_reconciled),
            preserved_verified=preserved_verified,
            head_commit=live_git.head_commit,
            head_tree=live_git.head_tree,
        )

    def _reconcile_git_index(
        self,
        ws: Path,
        manifest: WorkspaceSnapshotManifest,
        blob_cache: Mapping[str, bytes],
    ) -> list[str]:
        """Reconcile `.git/index` under ``ws`` to match ``manifest.staged_changes`` without touching working tree."""
        toplevel = Path(manifest.git.git_toplevel).resolve() if manifest.git.git_toplevel else ws
        staged_map = {s.rel_path: s for s in manifest.staged_changes}
        reconciled: list[str] = []

        if manifest.git.has_head:
            cur_cached = _wsnap_run_git(
                ["diff-index", "--cached", "--relative", "--no-renames", "-z", "HEAD", "--", "."],
                cwd=ws,
            )
            current_staged_deltas = {
                rel_p: (src_m, dst_m, src_oid, dst_oid, st_code)
                for src_m, dst_m, src_oid, dst_oid, st_code, rel_p in _wsnap_parse_diff_z(cur_cached.stdout)
            }
        else:
            stage_proc = _wsnap_run_git(["ls-files", "--stage", "-z", "--", "."], cwd=ws)
            current_staged_deltas = {
                rel_p: ("000000", mode_s, "0" * 40, oid_s, "A")
                for rel_p, (mode_s, oid_s, _st) in _wsnap_parse_ls_files_stage_z(stage_proc.stdout).items()
            }

        # Re-apply any baseline staged change that drifted
        for rel_p, st_entry in sorted(staged_map.items()):
            if st_entry.sensitive_excluded:
                continue
            repo_rel = self._repo_rel_for_ws_rel(manifest.git, rel_p)
            cur = current_staged_deltas.get(rel_p)
            if st_entry.status == "D":
                if cur is None or cur[4] != "D":
                    _wsnap_run_git(["update-index", "--force-remove", "--", repo_rel], cwd=toplevel)
                    reconciled.append(rel_p)
            elif st_entry.cas_sha256 is not None:
                need_update = (
                    cur is None
                    or cur[1] != st_entry.git_mode
                    or cur[3] != st_entry.git_blob_oid
                )
                if need_update:
                    oid_proc = _wsnap_run_git(
                        ["hash-object", "-w", "--stdin"],
                        cwd=toplevel,
                        input_bytes=blob_cache[st_entry.cas_sha256],
                    )
                    written_oid = oid_proc.stdout.decode("utf-8", errors="replace").strip()
                    mode_use = st_entry.git_mode or "100644"
                    _wsnap_run_git(
                        ["update-index", "--add", "--cacheinfo", f"{mode_use},{written_oid},{repo_rel}"],
                        cwd=toplevel,
                    )
                    reconciled.append(rel_p)

        # Unstage any path staged AFTER baseline capture (restoring HEAD index entry or removing newly added index entry)
        for rel_p, (src_m, _dst_m, src_oid, _dst_oid, _st_code) in sorted(current_staged_deltas.items()):
            if rel_p in staged_map or _wsnap_has_skipped_dir_component(rel_p) or is_sensitive_rel_path(rel_p):
                continue
            repo_rel = self._repo_rel_for_ws_rel(manifest.git, rel_p)
            if manifest.git.has_head and src_m not in ("", "000000") and set(src_oid) != {"0"}:
                _wsnap_run_git(
                    ["update-index", "--add", "--cacheinfo", f"{src_m},{src_oid},{repo_rel}"],
                    cwd=toplevel,
                )
                reconciled.append(rel_p)
            else:
                _wsnap_run_git(["update-index", "--force-remove", "--", repo_rel], cwd=toplevel)
                reconciled.append(rel_p)

        return reconciled

    def verify_snapshot_state(
        self,
        manifest: WorkspaceSnapshotManifest,
        *,
        target_root: Path | None = None,
    ) -> tuple[str, ...]:
        """Verify Git HEAD/index, user working-tree files, and in-place preserved sensitive files."""
        ws = self._validate_workspace_root(target_root or Path(manifest.workspace_root))
        if manifest.git.is_git:
            live_git = self.inspect_git_baseline(ws)
            if not live_git.is_git:
                raise SnapshotIntegrityError("Verification failed: workspace is not a Git repository.")
            if manifest.git.has_head and (
                live_git.head_commit != manifest.git.head_commit
                or live_git.head_tree != manifest.git.head_tree
            ):
                raise SnapshotIntegrityError(
                    f"Verification failed: Git HEAD/tree drifted ({live_git.head_commit} vs {manifest.git.head_commit})."
                )
            stage_proc = _wsnap_run_git(["ls-files", "--stage", "-z", "--", "."], cwd=ws)
            live_stage = _wsnap_parse_ls_files_stage_z(stage_proc.stdout)
            for st_entry in manifest.staged_changes:
                if st_entry.sensitive_excluded:
                    continue
                if st_entry.status == "D":
                    if st_entry.rel_path in live_stage:
                        raise SnapshotIntegrityError(
                            f"Verification failed: staged deletion {st_entry.rel_path!r} still present in index."
                        )
                elif st_entry.git_blob_oid:
                    got = live_stage.get(st_entry.rel_path)
                    if got is None or got[1] != st_entry.git_blob_oid or got[0] != st_entry.git_mode:
                        raise SnapshotIntegrityError(
                            f"Verification failed: staged index entry {st_entry.rel_path!r} does not match baseline."
                        )

        expected_wt: dict[str, str | None] = {}
        for rel_p, pre in manifest.pre_edit_targets.items():
            expected_wt[rel_p] = pre.cas_sha256 if pre.existed_before else None
        for st_entry in manifest.staged_changes:
            if not st_entry.sensitive_excluded:
                expected_wt[st_entry.rel_path] = None if st_entry.status == "D" else st_entry.cas_sha256
        for u_entry in manifest.unstaged_changes:
            expected_wt[u_entry.rel_path] = u_entry.cas_sha256 if u_entry.existed else None
        for ut_entry in manifest.untracked_files:
            expected_wt[ut_entry.rel_path] = ut_entry.cas_sha256

        for rel_p, exp_sha in sorted(expected_wt.items()):
            _, rel_parts = _wsnap_normalize_rel_path(ws, rel_p)
            live_hash = _wsnap_hash_confined_file_in_place(ws, rel_parts)
            if exp_sha is None:
                if live_hash is not None:
                    raise SnapshotIntegrityError(
                        f"Verification failed: {rel_p!r} should be absent after baseline restore."
                    )
            else:
                if live_hash is None or live_hash[0] != exp_sha:
                    raise SnapshotIntegrityError(
                        f"Verification failed: {rel_p!r} digest mismatch (expected {exp_sha}, got {live_hash[0] if live_hash else 'missing'})."
                    )

        verified_preserved: list[str] = []
        for p_entry in manifest.preserved_files:
            _, rel_parts = _wsnap_normalize_rel_path(ws, p_entry.rel_path)
            live_hash = _wsnap_hash_confined_file_in_place(ws, rel_parts)
            if live_hash is None:
                raise SnapshotIntegrityError(
                    f"Verification failed: preserved file {p_entry.rel_path!r} was missing."
                )
            if live_hash[0] != p_entry.in_place_sha256:
                raise SnapshotIntegrityError(
                    f"Verification failed: preserved file {p_entry.rel_path!r} was modified."
                )
            verified_preserved.append(p_entry.rel_path)

        return tuple(verified_preserved)
