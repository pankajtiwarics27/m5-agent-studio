"""Version-aware managed edits: durable edit transactions and observed versions (M5-P2-02).

A managed edit is a compare-and-swap on file *content*, not on a path. Two facts
have to be durable for that to be true:

1. **What the agent saw.** ``read_file`` hands the agent a version of a file. If
   the user edits the file afterwards, the agent's reasoning is about bytes that
   no longer exist, and applying "replace ``old_text`` with ``new_text``" would
   silently revert the user's change. ``ObservedVersionIndex`` records the SHA-256
   of the bytes handed to the agent per session and path, durably (it survives a
   restart, because the transcript does too).
2. **What the edit actually did.** ``EditTransactionLedger`` appends one durable
   record per applied edit: pre-edit digest (or "the file did not exist"),
   post-edit digest, the expected/observed digests the decision was based on, the
   tool, mode, size, session/task/snapshot ids and a monotonic ``seq``. A later
   reader — recovery, reconciliation, an operator audit — can therefore explain
   every byte the runtime wrote, without trusting an in-memory claim.

Both stores live under the harness store directory (``edits/`` beside
``sessions/``), are owner-only (``0700`` directories, ``0600`` files), append with
``O_APPEND`` + ``fsync`` (ledger) or replace atomically (observed index), and keep
a bounded history so a long session cannot grow them without limit.

The module is standard-library only, does no I/O outside the store directory it
is given, never invents a record, and never rewrites a durable record after it is
written.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

EDIT_LEDGER_VERSION = 1

#: Default number of transactions returned by a history read.
DEFAULT_READ_LIMIT = 50
MAX_READ_LIMIT = 500
#: Rotate a session ledger beyond this size, keeping the newest records.
DEFAULT_MAX_LEDGER_BYTES = 4 * 1024 * 1024
DEFAULT_RETAIN_TRANSACTIONS = 2000
#: How many observed versions are remembered per session before the oldest go.
MAX_OBSERVED_ENTRIES = 2048
MAX_REL_PATH_LENGTH = 1024

_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_LEDGER_LOCK = threading.Lock()
_OBSERVED_LOCK = threading.Lock()
_SEQ_CACHE: dict[str, int] = {}


class EditVersionError(ValueError):
    """Raised for a malformed expected hash, session id, or workspace path."""


def parse_expected_sha256(raw: Any) -> str | None:
    """Validate a caller-supplied expected content hash and normalize it to lowercase hex.

    Accepts ``None``/``""`` (no expectation), a bare 64-character hex digest, or a
    ``sha256:``-prefixed digest, case-insensitively. Anything else — a truncated
    hash, a git blob id from another algorithm, a path, a dict — raises instead of
    being ignored: a silently dropped expectation would be a promise the runtime
    did not keep.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise EditVersionError("expected_sha256 must be a string containing a SHA-256 digest.")
    text = raw.strip()
    if not text:
        return None
    match = _SHA256_RE.match(text)
    if match is None:
        raise EditVersionError(
            "expected_sha256 must be 64 hexadecimal characters (optionally prefixed with 'sha256:'), "
            f"got {text[:80]!r}."
        )
    return match.group(1).lower()


def _safe_session_id(session_id: str) -> str:
    """Return a store-safe session id or raise (never sanitize into a different session)."""
    sid = (session_id or "").strip()
    if not sid:
        raise EditVersionError("Edit ledger session id must not be empty.")
    if not _SAFE_SESSION_RE.match(sid):
        raise EditVersionError(f"Unsafe edit ledger session id: {session_id!r}")
    return sid


def _safe_rel_path(rel_path: str) -> str:
    """Validate a workspace-relative path for recording; never normalize it into another file."""
    raw = (rel_path or "").strip().replace("\\", "/")
    if not raw:
        raise EditVersionError("Edit ledger path must not be empty.")
    if len(raw) > MAX_REL_PATH_LENGTH:
        raise EditVersionError("Edit ledger path is too long to record.")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise EditVersionError(f"Edit ledger path must be workspace-relative, got {rel_path!r}.")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise EditVersionError(f"Edit ledger path must stay inside the workspace, got {rel_path!r}.")
    return "/".join(parts)


def _now() -> float:
    return round(time.time(), 6)


def _restrict_file(path: Path) -> None:
    """Best-effort owner-only permissions on a file we just created."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@dataclass(frozen=True, slots=True)
class EditTransaction:
    """One applied managed edit, with the hashes that justify it."""

    edit_id: str
    seq: int
    session_id: str
    rel_path: str
    tool: str
    pre_sha256: str | None
    post_sha256: str
    pre_existed: bool
    expected_sha256: str | None
    observed_sha256: str | None
    mode: int | None
    bytes_written: int
    recorded_at: float
    task_id: str = ""
    snapshot_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EDIT_LEDGER_VERSION,
            "edit_id": self.edit_id,
            "seq": self.seq,
            "session_id": self.session_id,
            "rel_path": self.rel_path,
            "tool": self.tool,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "pre_existed": self.pre_existed,
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "mode": self.mode,
            "bytes_written": self.bytes_written,
            "recorded_at": self.recorded_at,
            "task_id": self.task_id,
            "snapshot_id": self.snapshot_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EditTransaction":
        return cls(
            edit_id=str(raw["edit_id"]),
            seq=int(raw["seq"]),
            session_id=str(raw["session_id"]),
            rel_path=str(raw["rel_path"]),
            tool=str(raw.get("tool") or ""),
            pre_sha256=str(raw["pre_sha256"]) if raw.get("pre_sha256") else None,
            post_sha256=str(raw["post_sha256"]),
            pre_existed=bool(raw.get("pre_existed")),
            expected_sha256=str(raw["expected_sha256"]) if raw.get("expected_sha256") else None,
            observed_sha256=str(raw["observed_sha256"]) if raw.get("observed_sha256") else None,
            mode=int(raw["mode"]) if raw.get("mode") is not None else None,
            bytes_written=int(raw.get("bytes_written") or 0),
            recorded_at=float(raw.get("recorded_at") or 0.0),
            task_id=str(raw.get("task_id") or ""),
            snapshot_id=str(raw.get("snapshot_id") or ""),
        )


class EditTransactionLedger:
    """Append-only, durable record of the edits this runtime applied."""

    def __init__(
        self,
        store_root: Path,
        *,
        max_ledger_bytes: int = DEFAULT_MAX_LEDGER_BYTES,
        retain_transactions: int = DEFAULT_RETAIN_TRANSACTIONS,
    ) -> None:
        self.store_root = Path(store_root)
        self.max_ledger_bytes = max(0, int(max_ledger_bytes))
        self.retain_transactions = max(1, int(retain_transactions))

    # -- paths -----------------------------------------------------------

    def ledger_dir(self) -> Path:
        return self.store_root / "edits"

    def ledger_path(self, session_id: str) -> Path:
        return self.ledger_dir() / f"{_safe_session_id(session_id)}.jsonl"

    # -- writing ---------------------------------------------------------

    def record(
        self,
        *,
        session_id: str,
        rel_path: str,
        tool: str,
        post_sha256: str,
        pre_sha256: str | None = None,
        expected_sha256: str | None = None,
        observed_sha256: str | None = None,
        mode: int | None = None,
        bytes_written: int = 0,
        task_id: str = "",
        snapshot_id: str = "",
    ) -> EditTransaction:
        """Append one transaction and return it as recorded (durable before return)."""
        sid = _safe_session_id(session_id)
        rel = _safe_rel_path(rel_path)
        post = parse_expected_sha256(post_sha256)
        if post is None:
            raise EditVersionError("An edit transaction requires the SHA-256 of the bytes written.")
        pre = parse_expected_sha256(pre_sha256)
        expected = parse_expected_sha256(expected_sha256)
        observed = parse_expected_sha256(observed_sha256)
        path = self.ledger_path(sid)
        _ensure_private_dir(path.parent)
        with _LEDGER_LOCK:
            seq = _next_seq(path)
            transaction = EditTransaction(
                edit_id=f"ed-{uuid.uuid4().hex[:16]}",
                seq=seq,
                session_id=sid,
                rel_path=rel,
                tool=str(tool or "")[:40],
                pre_sha256=pre,
                post_sha256=post,
                pre_existed=pre is not None,
                expected_sha256=expected,
                observed_sha256=observed,
                mode=int(mode) if mode is not None else None,
                bytes_written=max(0, int(bytes_written)),
                recorded_at=_now(),
                task_id=str(task_id or "")[:128],
                snapshot_id=str(snapshot_id or "")[:128],
            )
            payload = json.dumps(transaction.to_dict(), sort_keys=True, ensure_ascii=False)
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0), 0o600)
            try:
                _restrict_file(path)
                os.write(fd, (payload + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            _rotate_if_needed(path, self.max_ledger_bytes, self.retain_transactions)
        return transaction

    # -- reading ---------------------------------------------------------

    def _read_records(self, path: Path) -> tuple[list[EditTransaction], int]:
        """Read valid transactions plus the count of unreadable (torn) lines."""
        records: list[EditTransaction] = []
        skipped = 0
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return records, skipped
        except OSError as exc:
            raise EditVersionError(f"Cannot read the edit ledger at {path}: {exc}") from exc
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                records.append(EditTransaction.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # A torn append is reported, never repaired into a plausible record.
                skipped += 1
        return records, skipped

    def list_transactions(self, session_id: str, *, limit: int = DEFAULT_READ_LIMIT) -> list[EditTransaction]:
        """Newest-first transactions for a session."""
        path = self.ledger_path(session_id)
        records, _skipped = self._read_records(path)
        records.sort(key=lambda record: record.seq, reverse=True)
        return records[: max(1, min(int(limit), MAX_READ_LIMIT))]

    def latest_for_path(self, session_id: str, rel_path: str) -> EditTransaction | None:
        """Most recent transaction for one workspace path, if any."""
        target = _safe_rel_path(rel_path)
        for record in self.list_transactions(session_id, limit=MAX_READ_LIMIT):
            if record.rel_path == target:
                return record
        return None

    def count_unreadable_lines(self, session_id: str) -> int:
        """Number of lines that could not be parsed (torn appends), for diagnostics."""
        _records, skipped = self._read_records(self.ledger_path(session_id))
        return skipped


def _next_seq(path: Path) -> int:
    """Next sequence number for a ledger file, seeded from its last record."""
    key = str(path)
    cached = _SEQ_CACHE.get(key)
    if cached is not None:
        _SEQ_CACHE[key] = cached + 1
        return cached + 1
    last = 0
    try:
        with path.open("rb") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = max(last, int(json.loads(line).get("seq") or 0))
                except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                    continue
    except FileNotFoundError:
        last = 0
    _SEQ_CACHE[key] = last + 1
    return last + 1


def _rotate_if_needed(path: Path, max_bytes: int, retain: int) -> None:
    """Keep the ledger bounded by rewriting the newest records atomically."""
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size <= max_bytes:
            return
    except OSError:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    kept = [line for line in raw.splitlines() if line.strip()][-retain:]
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(fd, ("\n".join(kept) + "\n").encode("utf-8") if kept else b"")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _SEQ_CACHE.pop(str(path), None)


class ObservedVersionIndex:
    """Durable record of the content version handed to the agent for a path."""

    def __init__(self, store_root: Path, *, max_entries: int = MAX_OBSERVED_ENTRIES) -> None:
        self.store_root = Path(store_root)
        self.max_entries = max(1, int(max_entries))

    def index_dir(self) -> Path:
        return self.store_root / "edits"

    def index_path(self, session_id: str) -> Path:
        return self.index_dir() / f"{_safe_session_id(session_id)}.observed.json"

    def _load(self, session_id: str) -> dict[str, dict[str, Any]]:
        path = self.index_path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            # A corrupt index must not be trusted as "the agent has no version":
            # callers treat a failed lookup as unknown and fall back to their own read.
            raise EditVersionError(f"Observed-version index at {path} is unreadable.")
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raise EditVersionError(f"Observed-version index at {path} is malformed.")
        cleaned: dict[str, dict[str, Any]] = {}
        for key, value in entries.items():
            if isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("sha256"), str):
                cleaned[key] = {"sha256": value["sha256"], "recorded_at": float(value.get("recorded_at") or 0.0)}
        return cleaned

    def _save(self, session_id: str, entries: dict[str, dict[str, Any]]) -> None:
        path = self.index_path(session_id)
        _ensure_private_dir(path.parent)
        payload = json.dumps(
            {"schema_version": EDIT_LEDGER_VERSION, "session_id": _safe_session_id(session_id), "entries": entries},
            sort_keys=True,
            ensure_ascii=False,
        )
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _restrict_file(path)

    def mark_observed(self, session_id: str, rel_path: str, sha256_hex: str) -> None:
        """Record that the agent currently holds the bytes with this digest."""
        rel = _safe_rel_path(rel_path)
        digest = parse_expected_sha256(sha256_hex)
        if digest is None:
            raise EditVersionError("Observed version requires a SHA-256 digest.")
        with _OBSERVED_LOCK:
            entries = self._load(session_id)
            entries[rel] = {"sha256": digest, "recorded_at": _now()}
            if len(entries) > self.max_entries:
                ordered = sorted(entries.items(), key=lambda item: item[1]["recorded_at"], reverse=True)
                entries = dict(ordered[: self.max_entries])
            self._save(session_id, entries)

    def expected_for(self, session_id: str, rel_path: str) -> str | None:
        """Digest the agent last saw for a path, or None when nothing was observed."""
        rel = _safe_rel_path(rel_path)
        with _OBSERVED_LOCK:
            entry = self._load(session_id).get(rel)
        if entry is None:
            return None
        return parse_expected_sha256(entry["sha256"])

    def forget(self, session_id: str, rel_path: str) -> None:
        """Drop the recorded version for a path (used when the runtime deletes it)."""
        rel = _safe_rel_path(rel_path)
        with _OBSERVED_LOCK:
            entries = self._load(session_id)
            if entries.pop(rel, None) is not None:
                self._save(session_id, entries)

    def entries(self, session_id: str) -> dict[str, str]:
        """All recorded path → digest pairs for a session."""
        with _OBSERVED_LOCK:
            entries = self._load(session_id)
        return {path: entry["sha256"] for path, entry in entries.items()}

    def clear(self, session_id: str) -> None:
        """Forget every recorded version for a session."""
        with _OBSERVED_LOCK:
            try:
                self.index_path(session_id).unlink()
            except FileNotFoundError:
                pass


def describe_history(transactions: Iterable[EditTransaction], *, limit: int = 20) -> list[dict[str, Any]]:
    """Compact newest-first history rows for display or tests."""
    rows: list[dict[str, Any]] = []
    for record in list(transactions)[:limit]:
        rows.append(
            {
                "edit_id": record.edit_id,
                "seq": record.seq,
                "path": record.rel_path,
                "tool": record.tool,
                "pre_sha256": record.pre_sha256,
                "post_sha256": record.post_sha256,
                "recorded_at": record.recorded_at,
            }
        )
    return rows


__all__ = [
    "DEFAULT_READ_LIMIT",
    "EDIT_LEDGER_VERSION",
    "MAX_OBSERVED_ENTRIES",
    "MAX_READ_LIMIT",
    "EditTransaction",
    "EditTransactionLedger",
    "EditVersionError",
    "ObservedVersionIndex",
    "describe_history",
    "parse_expected_sha256",
]
