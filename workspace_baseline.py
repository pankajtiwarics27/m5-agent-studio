"""Pre-edit Git baseline and selective user-work snapshots (M5-P2-01).

The runtime edits files the user also edits. Before any managed edit lands, the
runtime must be able to answer one question with evidence: *what did this
working tree look like immediately before we touched it?* This module captures
that state and makes it restorable, without ever running a destructive Git
command and without copying files indiscriminately.

What is captured
----------------
1. **Git identity** -- `HEAD` commit, current ref, whether the workspace is a
   repo at all. Read-only plumbing only (see `ALLOWED_GIT_SUBCOMMANDS`).
2. **Index state** -- `git ls-files --stage -z`, so staged content (index blobs)
   is distinguishable from unstaged worktree content.
3. **Worktree changes** -- `git status --porcelain=v1 -z`, classified into
   staged / unstaged / deleted / untracked / conflicted.
4. **Bytes** -- every captured file's content goes into a content-addressed store
   (`cas/<sha256[:2]>/<sha256>`), plus the index or `HEAD` blob for deletions, so
   both staged and unstaged work can be reconstructed.
5. **Explicit focus paths** -- the file(s) an edit is about to touch, even when
   they are untracked or outside Git's view.

What is deliberately *not* captured
-----------------------------------
* Ignored files are **not** enumerated or copied in bulk: `git status` is called
  without `--ignored`, and ignore status is probed per path with
  `git check-ignore`. A file the runtime is about to edit is still snapshotted;
  everything else ignored stays where it is.
* Secret-named files (`.env`, `*.pem`, `*.key`, `id_rsa`, `.netrc`, ...) are
  recorded as metadata only -- path, digest, size -- never copied. An operator
  can opt a specific path in via `protected_ignored`, which still stores bytes
  owner-only inside a directory that is excluded from Git.
* Files above `max_file_bytes` are recorded as metadata only and reported as
  unrestorable. They are never silently treated as preserved.

Safety properties
-----------------
* **No destructive Git.** Commands are built from an allowlist; a deny-list of
  `reset`, `clean`, `checkout`, `stash`, `restore`, `rm`, `switch`, `rebase`,
  `merge`, `commit`, `add`, `apply`, `prune`, `gc` is refused even if a caller
  tries. `GIT_OPTIONAL_LOCKS=0` stops `git status` from refreshing or locking the
  index.
* **No writes outside the store.** Restores never target the workspace: they
  materialize into a caller-supplied scratch directory, or verify in place.
* **Fail closed on storage exhaustion.** Blob and manifest writes are atomic and
  fsynced; `ENOSPC`/`EDQUOT` and an insufficient-headroom pre-check raise
  `BaselineStorageExhausted` so the caller can pause instead of editing over
  unprotected user work.
* **Tamper-evident manifests.** Each manifest embeds a SHA-256 over its own
  canonical content; loading re-verifies it and raises `BaselineCorruptError`
  rather than trusting a damaged record.

Standard-library only; no network, no shell (`shell=False`), explicit timeouts.
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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

BASELINE_SCHEMA_VERSION = 1

#: Read-only plumbing this module may run. Everything else is refused.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "ls-files",
        "status",
        "check-ignore",
        "cat-file",
        "show",
        "ls-tree",
        "diff",
        "hash-object",
    }
)

#: Refused regardless of context: these can destroy or rewrite user work.
FORBIDDEN_GIT_SUBCOMMANDS = frozenset(
    {
        "reset",
        "clean",
        "checkout",
        "stash",
        "restore",
        "rm",
        "switch",
        "rebase",
        "merge",
        "apply",
        "commit",
        "add",
        "prune",
        "gc",
        "reflog",
        "update-ref",
        "write-tree",
        "read-tree",
        "branch",
        "push",
        "pull",
        "fetch",
        "filter-branch",
    }
)

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
#: Free-space headroom kept beyond the bytes a capture is expected to write.
DEFAULT_HEADROOM_BYTES = 8 * 1024 * 1024
DEFAULT_GIT_TIMEOUT_S = 20.0

_SECRET_NAME_PATTERNS = (
    re.compile(r"^\.env(\..*)?$", re.IGNORECASE),
    re.compile(r"^\.netrc$", re.IGNORECASE),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.IGNORECASE),
    re.compile(r".*\.(pem|key|p12|pfx|keystore|jks)$", re.IGNORECASE),
    re.compile(r"^\.?secrets?(\..*)?$", re.IGNORECASE),
    re.compile(r"^credentials(\..*)?$", re.IGNORECASE),
    re.compile(r"^.*(api[-_]?token|api[-_]?key)[^/]*$", re.IGNORECASE),
)

_CAPTURE_MODE_CAS = "cas"
_CAPTURE_MODE_METADATA = "metadata_only"


class BaselineError(Exception):
    """Base error for baseline capture, load, and restore operations."""


class BaselineStorageExhausted(BaselineError):
    """The snapshot store cannot accept more bytes; the caller must pause."""


class BaselineNotFoundError(BaselineError):
    """No baseline with that id exists for the workspace."""


class BaselineCorruptError(BaselineError):
    """A manifest or blob failed its integrity check."""


class BaselineUnsafePathError(BaselineError):
    """A path is outside the workspace, is a symlink, or is otherwise unsafe."""


class GitCommandRefused(BaselineError):
    """A Git command outside the read-only allowlist was requested."""


def is_secret_filename(name: str) -> bool:
    """True when a file name alone marks the file as credential-like."""
    return any(pat.match(name) for pat in _SECRET_NAME_PATTERNS)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    """UTC timestamp with microsecond precision.

    Sub-second precision is load-bearing: baseline ids are random, so
    `captured_at` is the only durable ordering key and two captures of the same
    workspace routinely happen inside one second.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class CapturedFile:
    """One path observed before an edit, with the evidence needed to restore it."""

    path: str
    change: str
    digest: str | None
    size: int
    mode: int
    captured: bool
    capture_reason: str
    sensitive: bool = False
    ignored: bool = False
    staged_blob: str | None = None
    blob_source: str = ""

    @property
    def restorable(self) -> bool:
        return self.captured and self.digest is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change": self.change,
            "digest": self.digest,
            "size": self.size,
            "mode": self.mode,
            "captured": self.captured,
            "capture_reason": self.capture_reason,
            "sensitive": self.sensitive,
            "ignored": self.ignored,
            "staged_blob": self.staged_blob,
            "blob_source": self.blob_source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapturedFile":
        return cls(
            path=str(raw["path"]),
            change=str(raw["change"]),
            digest=raw.get("digest"),
            size=int(raw.get("size") or 0),
            mode=int(raw.get("mode") or 0),
            captured=bool(raw.get("captured")),
            capture_reason=str(raw.get("capture_reason") or ""),
            sensitive=bool(raw.get("sensitive")),
            ignored=bool(raw.get("ignored")),
            staged_blob=raw.get("staged_blob"),
            blob_source=str(raw.get("blob_source") or ""),
        )


@dataclass(frozen=True)
class BaselineRecord:
    """A tamper-evident pre-edit snapshot of one workspace."""

    baseline_id: str
    session_id: str
    workspace: str
    reason: str
    captured_at: str
    git_repo: bool
    head_commit: str
    head_ref: str
    files: tuple[CapturedFile, ...] = ()
    skipped: tuple[str, ...] = ()
    staged_entries: int = 0
    total_bytes: int = 0
    schema_version: int = BASELINE_SCHEMA_VERSION

    def file(self, path: str) -> CapturedFile | None:
        for entry in self.files:
            if entry.path == path:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "baseline_id": self.baseline_id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "reason": self.reason,
            "captured_at": self.captured_at,
            "git_repo": self.git_repo,
            "head_commit": self.head_commit,
            "head_ref": self.head_ref,
            "files": [f.to_dict() for f in self.files],
            "skipped": list(self.skipped),
            "staged_entries": self.staged_entries,
            "total_bytes": self.total_bytes,
            "restorable_paths": [f.path for f in self.files if f.restorable],
            "unrestorable_paths": [f.path for f in self.files if not f.restorable],
        }

    def content_sha256(self) -> str:
        payload = {k: v for k, v in self.to_dict().items() if k != "manifest_sha256"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return _sha256_hex(canonical.encode("utf-8"))

    def to_manifest(self) -> dict[str, Any]:
        data = self.to_dict()
        data["manifest_sha256"] = self.content_sha256()
        return data

    @classmethod
    def from_manifest(cls, raw: dict[str, Any]) -> "BaselineRecord":
        record = cls(
            baseline_id=str(raw["baseline_id"]),
            session_id=str(raw.get("session_id") or ""),
            workspace=str(raw.get("workspace") or ""),
            reason=str(raw.get("reason") or ""),
            captured_at=str(raw.get("captured_at") or ""),
            git_repo=bool(raw.get("git_repo")),
            head_commit=str(raw.get("head_commit") or ""),
            head_ref=str(raw.get("head_ref") or ""),
            files=tuple(CapturedFile.from_dict(f) for f in raw.get("files") or ()),
            skipped=tuple(str(s) for s in raw.get("skipped") or ()),
            staged_entries=int(raw.get("staged_entries") or 0),
            total_bytes=int(raw.get("total_bytes") or 0),
            schema_version=int(raw.get("schema_version") or BASELINE_SCHEMA_VERSION),
        )
        recorded = str(raw.get("manifest_sha256") or "")
        if recorded and recorded != record.content_sha256():
            raise BaselineCorruptError(
                f"Baseline {record.baseline_id} manifest digest mismatch; refusing to trust it."
            )
        return record


@dataclass(frozen=True)
class RestoreVerification:
    """Result of proving that a baseline can be reconstructed."""

    baseline_id: str
    ok: bool
    verified_paths: tuple[str, ...] = ()
    missing_blobs: tuple[str, ...] = ()
    digest_mismatches: tuple[str, ...] = ()
    unrestorable_paths: tuple[str, ...] = ()
    restored_paths: tuple[str, ...] = ()
    scratch_dir: str = ""
    checked_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "ok": self.ok,
            "verified_paths": list(self.verified_paths),
            "missing_blobs": list(self.missing_blobs),
            "digest_mismatches": list(self.digest_mismatches),
            "unrestorable_paths": list(self.unrestorable_paths),
            "restored_paths": list(self.restored_paths),
            "scratch_dir": self.scratch_dir,
            "checked_bytes": self.checked_bytes,
        }


class _Git:
    """Read-only Git plumbing bound to one workspace directory."""

    def __init__(self, workspace: Path, *, timeout_s: float = DEFAULT_GIT_TIMEOUT_S) -> None:
        self.workspace = workspace
        self.timeout_s = timeout_s
        self._git = shutil.which("git")

    @property
    def available(self) -> bool:
        return self._git is not None

    def run(self, *args: str) -> tuple[int, bytes]:
        if not args:
            raise GitCommandRefused("No Git subcommand supplied.")
        sub = args[0]
        if sub in FORBIDDEN_GIT_SUBCOMMANDS or sub not in ALLOWED_GIT_SUBCOMMANDS:
            raise GitCommandRefused(
                f"Git subcommand {sub!r} is outside the read-only allowlist for baselines."
            )
        if not self.available:
            return 127, b""
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": os.environ.get("HOME", str(self.workspace)),
            #Never prompt, never take the index lock, never page output.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }
        try:
            proc = subprocess.run(
                [str(self._git), *args],
                cwd=str(self.workspace),
                capture_output=True,
                timeout=self.timeout_s,
                env=env,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 127, b""
        return proc.returncode, proc.stdout

    def text(self, *args: str) -> str:
        code, out = self.run(*args)
        if code != 0:
            return ""
        return out.decode("utf-8", errors="replace")


def _safe_join(root: Path, rel_posix: str) -> Path:
    """Join a workspace-relative POSIX path, refusing escapes and absolute paths."""
    if not rel_posix or rel_posix.startswith("/") or "\\" in rel_posix or "\x00" in rel_posix:
        raise BaselineUnsafePathError(f"Unsafe workspace-relative path: {rel_posix!r}")
    parts: list[str] = []
    for part in rel_posix.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise BaselineUnsafePathError(f"Path escapes the workspace: {rel_posix!r}")
        parts.append(part)
    if not parts:
        raise BaselineUnsafePathError(f"Empty workspace-relative path: {rel_posix!r}")
    return root.joinpath(*parts)


def _default_read_bytes(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    """Read one regular file without following a symlink; bounded by `limit`."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BaselineUnsafePathError(f"Not a regular file: {path}")
        if info.st_size > limit:
            raise ValueError("file exceeds the snapshot size limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


class WorkspaceBaselineStore:
    """Content-addressed, tamper-evident store of pre-edit workspace baselines."""

    def __init__(
        self,
        store_root: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
        read_bytes: Callable[[Path, int], tuple[bytes, os.stat_result]] | None = None,
        git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S,
    ) -> None:
        self.store_root = Path(store_root)
        self.max_file_bytes = int(max_file_bytes)
        self.headroom_bytes = int(headroom_bytes)
        self._read_bytes = read_bytes or _default_read_bytes
        self.git_timeout_s = git_timeout_s

    # -- layout -----------------------------------------------------------

    def _workspace_key(self, workspace: Path) -> str:
        return _sha256_hex(str(Path(workspace).resolve()).encode("utf-8"))[:16]

    def baseline_dir(self, workspace: Path, baseline_id: str) -> Path:
        if not re.fullmatch(r"bl-[0-9a-f]{12}", baseline_id or ""):
            raise BaselineError(f"Invalid baseline id: {baseline_id!r}")
        return self.store_root / self._workspace_key(workspace) / baseline_id

    def _blob_path(self, workspace: Path, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
            raise BaselineError(f"Invalid blob digest: {digest!r}")
        return self.store_root / self._workspace_key(workspace) / "cas" / digest[:2] / digest

    # -- storage primitives ----------------------------------------------

    @staticmethod
    def _raise_for_storage_error(exc: OSError, what: str) -> None:
        if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            raise BaselineStorageExhausted(
                f"No space left while writing the {what}; pause instead of editing unprotected work."
            ) from exc
        raise BaselineError(f"Could not write the {what}: {exc}") from exc

    def _atomic_write(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
        fd: int | None = None
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp, path)
        except OSError as exc:
            self._raise_for_storage_error(exc, "baseline store")
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _free_bytes(self) -> int | None:
        try:
            return int(shutil.disk_usage(self.store_root).free)
        except OSError:
            try:
                self.store_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                return int(shutil.disk_usage(self.store_root).free)
            except OSError:
                return None

    def _store_blob(self, workspace: Path, data: bytes) -> str:
        digest = _sha256_hex(data)
        path = self._blob_path(workspace, digest)
        if path.is_file():
            return digest
        self._atomic_write(path, data)
        return digest

    def read_blob(self, workspace: Path, digest: str) -> bytes:
        path = self._blob_path(workspace, digest)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BaselineCorruptError(f"Baseline blob {digest} is missing: {exc}") from exc
        if _sha256_hex(data) != digest:
            raise BaselineCorruptError(f"Baseline blob {digest} failed its content check.")
        return data

    # -- git inspection ---------------------------------------------------

    def _git(self, workspace: Path) -> _Git:
        return _Git(workspace, timeout_s=self.git_timeout_s)

    def is_git_repo(self, workspace: Path) -> bool:
        git = self._git(workspace)
        code, out = git.run("rev-parse", "--is-inside-work-tree")
        return code == 0 and out.strip() == b"true"

    def is_ignored(self, workspace: Path, rel_path: str) -> bool:
        git = self._git(workspace)
        if not self.is_git_repo(workspace):
            return False
        code, _out = git.run("check-ignore", "-q", "--", rel_path)
        return code == 0

    def _changed_paths(self, workspace: Path) -> dict[str, str]:
        """Return ``{rel_path: change_class}`` from a read-only status call."""
        git = self._git(workspace)
        if not self.is_git_repo(workspace):
            return {}
        code, out = git.run("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
        if code != 0:
            return {}
        entries = out.split(b"\x00")
        changes: dict[str, str] = {}
        for raw in entries:
            if len(raw) < 4:
                continue
            x = chr(raw[0])
            y = chr(raw[1])
            raw_path = raw[3:]
            try:
                rel = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if x == "?" and y == "?":
                changes[rel] = "untracked"
                continue
            if x == "!" and y == "!":
                # Defensive: --ignored is never requested, but never snapshot it blindly.
                changes[rel] = "ignored"
                continue
            if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
                changes[rel] = "conflicted"
            elif y == "D":
                changes[rel] = "deleted"
            elif "R" in (x, y) or "C" in (x, y):
                changes[rel] = "renamed"
            elif x in "MADTRC":
                changes[rel] = "staged_modified"
            elif y in "MADT":
                changes[rel] = "modified"
            else:
                changes[rel] = "modified"
        return changes

    def _staged_entries(self, workspace: Path) -> dict[str, str]:
        """Return ``{rel_path: index_blob_sha}`` for the current index."""
        git = self._git(workspace)
        if not self.is_git_repo(workspace):
            return {}
        code, out = git.run("ls-files", "--stage", "-z")
        if code != 0:
            return {}
        entries: dict[str, str] = {}
        for raw in out.split(b"\x00"):
            if not raw.strip():
                continue
            meta, _sep, raw_path = raw.partition(b"\t")
            fields = meta.split()
            if len(fields) < 3:
                continue
            try:
                rel = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                continue
            entries[rel] = fields[1].decode("ascii", errors="replace")
        return entries

    def _head_blob(self, workspace: Path, rel_path: str) -> bytes | None:
        git = self._git(workspace)
        code, out = git.run("cat-file", "-p", f"HEAD:{rel_path}")
        if code != 0:
            return None
        return out

    def _read_index_blob(self, workspace: Path, blob_sha: str) -> bytes | None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", blob_sha or ""):
            return None
        git = self._git(workspace)
        code, out = git.run("cat-file", "-p", blob_sha)
        return out if code == 0 else None

    # -- capture ----------------------------------------------------------

    def capture(
        self,
        workspace: Path,
        *,
        session_id: str,
        reason: str = "pre_edit",
        focus_paths: Sequence[str] = (),
        protected_ignored: Sequence[str] = (),
    ) -> BaselineRecord:
        """Snapshot the working tree (and focus paths) before an edit.

        Raises `BaselineStorageExhausted` when the store cannot accept the bytes,
        and `BaselineUnsafePathError` when a focus path escapes the workspace.
        """
        workspace = Path(workspace).resolve(strict=True)
        # Validate every caller-supplied path first: an absolute or escaping path
        # is refused rather than silently rewritten into a different file.
        protected = {p.strip() for p in protected_ignored if p and p.strip()}
        focus = [p.strip() for p in focus_paths if p and p.strip()]
        for rel in [*focus, *sorted(protected)]:
            _safe_join(workspace, rel)

        git_repo = self.is_git_repo(workspace)
        git = self._git(workspace)
        head_commit = git.text("rev-parse", "HEAD").strip() if git_repo else ""
        head_ref = git.text("rev-parse", "--abbrev-ref", "HEAD").strip() if git_repo else ""
        staged = self._staged_entries(workspace) if git_repo else {}
        changes = self._changed_paths(workspace)
        for rel in focus:
            changes.setdefault(rel, "focus")
        # Explicit operator opt-ins are captured even though ignored files are
        # never enumerated in bulk.
        for rel in sorted(protected):
            changes.setdefault(rel, "protected")

        candidates = sorted(changes)
        expected_bytes = 0
        for rel in candidates:
            try:
                info = os.lstat(_safe_join(workspace, rel))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_size <= self.max_file_bytes:
                expected_bytes += int(info.st_size)
        free = self._free_bytes()
        if free is not None and expected_bytes + self.headroom_bytes > free:
            raise BaselineStorageExhausted(
                "Not enough free disk space to snapshot the workspace before editing "
                f"({expected_bytes} B needed plus {self.headroom_bytes} B headroom, {free} B free)."
            )

        captured_files: list[CapturedFile] = []
        skipped: list[str] = []
        total_bytes = 0
        for rel in candidates:
            change = changes[rel]
            if change == "ignored" and rel not in protected:
                captured_files.append(
                    CapturedFile(
                        path=rel,
                        change=change,
                        digest=None,
                        size=0,
                        mode=0,
                        captured=False,
                        capture_reason="ignored_not_copied",
                        ignored=True,
                    )
                )
                skipped.append(rel)
                continue
            entry, written = self._capture_one(workspace, rel, change, staged, focus, protected)
            captured_files.append(entry)
            total_bytes += written
            if not entry.captured:
                skipped.append(rel)

        baseline_id = f"bl-{uuid.uuid4().hex[:12]}"
        record = BaselineRecord(
            baseline_id=baseline_id,
            session_id=session_id,
            workspace=str(workspace),
            reason=reason,
            captured_at=_now_iso(),
            git_repo=git_repo,
            head_commit=head_commit,
            head_ref=head_ref,
            files=tuple(captured_files),
            skipped=tuple(skipped),
            staged_entries=len(staged),
            total_bytes=total_bytes,
        )
        manifest = json.dumps(record.to_manifest(), indent=2, sort_keys=True, ensure_ascii=False)
        self._atomic_write(self.baseline_dir(workspace, baseline_id) / "baseline.json", manifest.encode("utf-8"))
        return record

    def _capture_one(
        self,
        workspace: Path,
        rel: str,
        change: str,
        staged: dict[str, str],
        focus: Iterable[str],
        protected: set[str],
    ) -> tuple[CapturedFile, int]:
        path = _safe_join(workspace, rel)
        sensitive = is_secret_filename(Path(rel).name)
        try:
            ignored = self.is_ignored(workspace, rel)
        except BaselineError:
            ignored = False
        staged_blob = staged.get(rel)

        info: os.stat_result | None = None
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                return (
                    CapturedFile(
                        path=rel,
                        change=change,
                        digest=None,
                        size=0,
                        mode=0,
                        captured=False,
                        capture_reason="symlink_not_followed",
                        sensitive=sensitive,
                        ignored=ignored,
                    ),
                    0,
                )
        except OSError:
            info = None

        metadata_only = sensitive or (ignored and rel not in protected)
        mode = stat.S_IMODE(info.st_mode) if info is not None else 0

        if metadata_only:
            digest = None
            size = 0
            if info is not None and stat.S_ISREG(info.st_mode) and info.st_size <= self.max_file_bytes:
                try:
                    data, _info = self._read_bytes(path, self.max_file_bytes)
                    digest = _sha256_hex(data)
                    size = len(data)
                except (OSError, ValueError):
                    digest = None
            return (
                CapturedFile(
                    path=rel,
                    change=change,
                    digest=digest,
                    size=size,
                    mode=mode,
                    captured=False,
                    capture_reason="sensitive_not_copied" if sensitive else "ignored_not_copied",
                    sensitive=sensitive,
                    ignored=ignored,
                    staged_blob=staged_blob,
                    blob_source="metadata_only",
                ),
                0,
            )

        if info is None or change in ("deleted", "staged_deleted"):
            # The worktree copy is gone: preserve the index or HEAD content so the
            # deletion is reversible, without re-creating anything in the tree.
            blob = self._read_index_blob(workspace, staged_blob) if staged_blob else None
            source = "index"
            if blob is None:
                blob = self._head_blob(workspace, rel)
                source = "head"
            if blob is None:
                return (
                    CapturedFile(
                        path=rel,
                        change=change,
                        digest=None,
                        size=0,
                        mode=mode,
                        captured=False,
                        capture_reason="deleted_without_object",
                        sensitive=sensitive,
                        ignored=ignored,
                        staged_blob=staged_blob,
                    ),
                    0,
                )
            digest = self._store_blob(workspace, blob)
            return (
                CapturedFile(
                    path=rel,
                    change=change,
                    digest=digest,
                    size=len(blob),
                    mode=mode or 0o644,
                    captured=True,
                    capture_reason=f"captured_from_{source}",
                    sensitive=sensitive,
                    ignored=ignored,
                    staged_blob=staged_blob,
                    blob_source=source,
                ),
                len(blob),
            )

        if not stat.S_ISREG(info.st_mode):
            return (
                CapturedFile(
                    path=rel,
                    change=change,
                    digest=None,
                    size=0,
                    mode=mode,
                    captured=False,
                    capture_reason="not_a_regular_file",
                    sensitive=sensitive,
                    ignored=ignored,
                ),
                0,
            )
        if info.st_size > self.max_file_bytes:
            return (
                CapturedFile(
                    path=rel,
                    change=change,
                    digest=None,
                    size=int(info.st_size),
                    mode=mode,
                    captured=False,
                    capture_reason="exceeds_snapshot_size_limit",
                    sensitive=sensitive,
                    ignored=ignored,
                    staged_blob=staged_blob,
                ),
                0,
            )
        data, _info = self._read_bytes(path, self.max_file_bytes)
        digest = self._store_blob(workspace, data)
        return (
            CapturedFile(
                path=rel,
                change=change,
                digest=digest,
                size=len(data),
                mode=mode,
                captured=True,
                capture_reason="captured_worktree",
                sensitive=sensitive,
                ignored=ignored,
                staged_blob=staged_blob,
                blob_source="worktree",
            ),
            len(data),
        )

    # -- loading ----------------------------------------------------------

    def _workspace_dir(self, workspace: Path) -> Path:
        return self.store_root / self._workspace_key(workspace)

    def list_baselines(self, workspace: Path, *, limit: int = 20) -> list[BaselineRecord]:
        base = self._workspace_dir(Path(workspace).resolve())
        if not base.is_dir():
            return []
        records: list[BaselineRecord] = []
        for path in base.iterdir():
            if not (path.is_dir() and path.name.startswith("bl-")):
                continue
            try:
                records.append(self.load(workspace, path.name))
            except BaselineError:
                continue
        # Ids are random, so directory order says nothing about age: order by the
        # tamper-evident capture timestamp, with the (also random) id only as a
        # deterministic tie-break.
        records.sort(key=lambda record: (record.captured_at, record.baseline_id), reverse=True)
        return records[: max(1, int(limit))]

    def load(self, workspace: Path, baseline_id: str) -> BaselineRecord:
        manifest_path = self.baseline_dir(Path(workspace).resolve(), baseline_id) / "baseline.json"
        if not manifest_path.is_file():
            raise BaselineNotFoundError(
                f"Baseline {baseline_id} does not exist for workspace {str(workspace)!r}."
            )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineCorruptError(f"Cannot read baseline {baseline_id}: {exc}") from exc
        return BaselineRecord.from_manifest(raw)

    def latest(self, workspace: Path, *, session_id: str | None = None) -> BaselineRecord | None:
        for record in self.list_baselines(workspace, limit=50):
            if session_id is None or record.session_id == session_id:
                return record
        return None

    # -- verification / restore -------------------------------------------

    def verify(self, workspace: Path, baseline_id: str, *, scratch_dir: Path | None = None) -> RestoreVerification:
        """Prove the snapshot is complete and restorable; never touches the workspace."""
        workspace = Path(workspace).resolve()
        record = self.load(workspace, baseline_id)
        verified: list[str] = []
        missing: list[str] = []
        mismatched: list[str] = []
        unrestorable: list[str] = []
        restored: list[str] = []
        checked = 0
        target_root = Path(scratch_dir) if scratch_dir is not None else None
        if target_root is not None:
            target_root.mkdir(parents=True, exist_ok=True)
        for entry in record.files:
            if not entry.restorable or entry.digest is None:
                unrestorable.append(entry.path)
                continue
            try:
                data = self.read_blob(workspace, entry.digest)
            except BaselineCorruptError:
                path = self._blob_path(workspace, entry.digest)
                (mismatched if path.exists() else missing).append(entry.path)
                continue
            if _sha256_hex(data) != entry.digest or len(data) != entry.size:
                mismatched.append(entry.path)
                continue
            verified.append(entry.path)
            checked += len(data)
            if target_root is not None:
                dest = _safe_join(target_root, entry.path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                if entry.mode:
                    try:
                        os.chmod(dest, entry.mode)
                    except OSError:
                        pass
                if _sha256_hex(dest.read_bytes()) != entry.digest:
                    mismatched.append(entry.path)
                    verified.pop()
                    continue
                restored.append(entry.path)
        ok = not missing and not mismatched
        return RestoreVerification(
            baseline_id=baseline_id,
            ok=ok,
            verified_paths=tuple(verified),
            missing_blobs=tuple(missing),
            digest_mismatches=tuple(mismatched),
            unrestorable_paths=tuple(unrestorable),
            restored_paths=tuple(restored),
            scratch_dir=str(target_root or ""),
            checked_bytes=checked,
        )

    def describe(self, workspace: Path, baseline_id: str) -> dict[str, Any]:
        record = self.load(workspace, baseline_id)
        data = record.to_dict()
        data["manifest_sha256"] = record.content_sha256()
        return data


__all__ = [
    "ALLOWED_GIT_SUBCOMMANDS",
    "BASELINE_SCHEMA_VERSION",
    "BaselineCorruptError",
    "BaselineError",
    "BaselineNotFoundError",
    "BaselineRecord",
    "BaselineStorageExhausted",
    "BaselineUnsafePathError",
    "CapturedFile",
    "DEFAULT_MAX_FILE_BYTES",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "GitCommandRefused",
    "RestoreVerification",
    "WorkspaceBaselineStore",
    "is_secret_filename",
]
