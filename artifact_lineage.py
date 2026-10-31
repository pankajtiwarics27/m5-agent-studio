"""Artifact lifecycle: content identity, provenance, selective invalidation, retention.

`M5-P5-02`. Three questions this module answers, and one it deliberately does not:

1. **What is this content?** A SHA-256 over the bytes. Content identity is deduplicated:
   the same bytes are recorded once.
2. **Where did it come from, and how much do we trust *that*?** A separate provenance
   entry per sighting. Deduplication must never merge trust histories: bytes first seen
   in a trusted baseline and later reported by an untrusted source keep *both* records,
   and every answer about the artifact is the most restrictive answer across them
   (weakest trust, highest confidentiality, deny-wins on operations).
3. **Is the evidence built on it still current?** Evidence records the artifact digests
   and the security configuration generation it was computed against. When either moves,
   the evidence is **stale**, and `accept_evidence` refuses it instead of serving it.

It is *not* a proof engine: there is no general theorem of trust, no transitive
soundness argument, and no attempt to decide whether an arbitrary program is safe. It
records lineage that a caller can check, and refuses to promote what it cannot justify.

Store: `$HARNESS_STORE_DIR/artifacts/state.json`, following the durable JSON sidecar
contract shared with `process_supervision`, `scoped_grants`, `mcp_client` and
`sandbox_boundary` (owner-only permissions, `O_EXCL` temp + `fsync` + `os.replace` +
directory `fsync`, symlink refusal, corrupt/future-version refusal). That contract is
asserted against this store by `tests/test_durable_state_contract.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

AL_SCHEMA_VERSION = 1
AL_DIRNAME = "artifacts"
AL_STATE_NAME = "state.json"
AL_TEMP_PREFIX = ".al-state-"

# Ordered weakest-last: an artifact's effective trust is the weakest entry it has.
TRUST_LEVELS = ("trusted", "unverified", "untrusted")
# Ordered most-public-first: an artifact's effective confidentiality is the strictest.
CONFIDENTIALITY_LEVELS = ("public", "internal", "secret")

EVIDENCE_VALID = "valid"
EVIDENCE_STALE = "stale"

MAX_CASCADE_DEPTH = 8


class ArtifactLineageError(RuntimeError):
    """Base error for artifact lineage failures."""


class ArtifactLineageIntegrityError(ArtifactLineageError):
    """The lineage store is unreadable, unsafe, or of an unsupported schema version."""


class UnknownArtifactError(ArtifactLineageError):
    """Asked about content that was never recorded."""


def _al_now() -> float:
    return time.time()


def _al_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _al_rank(value: str, order: Sequence[str], *, field: str, default: str) -> int:
    text = str(value or "").strip().lower()
    if not text:
        return order.index(default)
    if text not in order:
        raise ArtifactLineageError(f"Unknown {field} {value!r}; expected one of {', '.join(order)}.")
    return order.index(text)


def _al_bounded(items: Iterable[str], limit: int = 32) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in items:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text[:120])
        if len(seen) >= limit:
            break
    return tuple(seen)


def content_digest(data: bytes) -> str:
    """Content identity: what the bytes are, independent of where they came from."""
    return hashlib.sha256(data).hexdigest()


def file_content_digest(path: Path | str) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One sighting of one piece of content, from one source, with its own trust history."""

    entry_id: str
    content_sha256: str
    source_kind: str
    source_ref: str
    trust: str
    confidentiality: str
    allowed_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    task_id: str
    security_generation: int | None
    derived_from: tuple[str, ...]
    recorded_at: float
    invalidated_at: float | None = None
    invalidation_reason: str = ""

    @property
    def invalidated(self) -> bool:
        return self.invalidated_at is not None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "content_sha256": self.content_sha256,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "trust": self.trust,
            "confidentiality": self.confidentiality,
            "allowed_operations": list(self.allowed_operations),
            "denied_operations": list(self.denied_operations),
            "task_id": self.task_id,
            "security_generation": self.security_generation,
            "derived_from": list(self.derived_from),
            "recorded_at": self.recorded_at,
            "invalidated_at": self.invalidated_at,
            "invalidation_reason": self.invalidation_reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProvenanceEntry:
        generation = raw.get("security_generation")
        invalidated_at = raw.get("invalidated_at")
        return cls(
            entry_id=str(raw.get("entry_id") or ""),
            content_sha256=str(raw.get("content_sha256") or ""),
            source_kind=str(raw.get("source_kind") or ""),
            source_ref=str(raw.get("source_ref") or ""),
            trust=str(raw.get("trust") or "unverified"),
            confidentiality=str(raw.get("confidentiality") or "internal"),
            allowed_operations=tuple(str(x) for x in raw.get("allowed_operations") or ()),
            denied_operations=tuple(str(x) for x in raw.get("denied_operations") or ()),
            task_id=str(raw.get("task_id") or ""),
            security_generation=int(generation) if isinstance(generation, int) else None,
            derived_from=tuple(str(x) for x in raw.get("derived_from") or ()),
            recorded_at=float(raw.get("recorded_at") or 0.0),
            invalidated_at=float(invalidated_at) if isinstance(invalidated_at, (int, float)) else None,
            invalidation_reason=str(raw.get("invalidation_reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class OperationVerdict:
    """Whether an operation is allowed on an artifact, and why not when it is not."""

    allowed: bool
    code: str
    reason: str

    def to_mapping(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Evidence computed against specific artifact digests and a security generation."""

    evidence_id: str
    kind: str
    subject: str
    task_id: str
    content_sha256: str
    source_files: Mapping[str, str]
    security_generation: int | None
    recorded_at: float
    status: str = EVIDENCE_VALID
    stale_reason: str = ""

    @property
    def stale(self) -> bool:
        return self.status == EVIDENCE_STALE

    def to_mapping(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "subject": self.subject,
            "task_id": self.task_id,
            "content_sha256": self.content_sha256,
            "source_files": dict(sorted(self.source_files.items())),
            "security_generation": self.security_generation,
            "recorded_at": self.recorded_at,
            "status": self.status,
            "stale_reason": self.stale_reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> EvidenceRecord:
        generation = raw.get("security_generation")
        files_raw = raw.get("source_files")
        files = {
            str(k): str(v)
            for k, v in (files_raw.items() if isinstance(files_raw, Mapping) else ())
        }
        return cls(
            evidence_id=str(raw.get("evidence_id") or ""),
            kind=str(raw.get("kind") or ""),
            subject=str(raw.get("subject") or ""),
            task_id=str(raw.get("task_id") or ""),
            content_sha256=str(raw.get("content_sha256") or ""),
            source_files=files,
            security_generation=int(generation) if isinstance(generation, int) else None,
            recorded_at=float(raw.get("recorded_at") or 0.0),
            status=str(raw.get("status") or EVIDENCE_VALID),
            stale_reason=str(raw.get("stale_reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class EvidenceVerdict:
    """The answer to "may this evidence be used?", with the reason either way."""

    accepted: bool
    code: str
    reason: str
    evidence: EvidenceRecord

    def to_mapping(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "evidence": self.evidence.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """Which artifacts may be collected, and why each survivor is kept."""

    evictable: tuple[str, ...]
    protected: Mapping[str, str]
    retain_seconds: float
    planned_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "evictable": list(self.evictable),
            "protected": dict(sorted(self.protected.items())),
            "retain_seconds": self.retain_seconds,
            "planned_at": self.planned_at,
        }


@dataclass(frozen=True, slots=True)
class RetentionResult:
    evicted: tuple[str, ...]
    kept: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"evicted": list(self.evicted), "kept": list(self.kept)}


def _al_empty_state() -> dict[str, Any]:
    return {
        "schema_version": AL_SCHEMA_VERSION,
        "revision": 0,
        "blobs": {},
        "evidence": {},
        "pins": {},
    }


class ArtifactRegistry:
    """Durable artifact lineage: content identity separated from provenance and confidentiality."""

    def __init__(self, store_root: Path | str) -> None:
        self._root = Path(store_root).expanduser()
        self._dir = self._root / AL_DIRNAME
        self._state_path = self._dir / AL_STATE_NAME
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    # ------------------------------------------------------------------ store

    def _ensure_dir_unlocked(self) -> None:
        if self._dir.is_symlink():
            raise ArtifactLineageIntegrityError(f"Refusing symlinked lineage directory: {self._dir}")
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._dir, 0o700)
        except OSError:  # pragma: no cover - platform without chmod semantics
            pass

    def _read_state_unlocked(self) -> dict[str, Any]:
        path = self._state_path
        if path.is_symlink():
            raise ArtifactLineageIntegrityError(f"Refusing symlinked lineage state file: {path}")
        if not path.exists():
            return _al_empty_state()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArtifactLineageIntegrityError(f"Artifact lineage state is not valid JSON: {path}") from exc
        except OSError as exc:  # pragma: no cover - unreadable file
            raise ArtifactLineageIntegrityError(f"Artifact lineage state is unreadable: {path}") from exc
        if not isinstance(raw, Mapping):
            raise ArtifactLineageIntegrityError(f"Artifact lineage state is not an object: {path}")
        version = raw.get("schema_version")
        if not isinstance(version, int) or version != AL_SCHEMA_VERSION:
            raise ArtifactLineageIntegrityError(
                f"Unsupported artifact lineage schema_version {version!r}; expected {AL_SCHEMA_VERSION}."
            )
        state = _al_empty_state()
        state["revision"] = int(raw.get("revision") or 0)
        blobs = raw.get("blobs")
        if isinstance(blobs, Mapping):
            state["blobs"] = {str(k): dict(v) for k, v in blobs.items() if isinstance(v, Mapping)}
        evidence = raw.get("evidence")
        if isinstance(evidence, Mapping):
            state["evidence"] = {str(k): dict(v) for k, v in evidence.items() if isinstance(v, Mapping)}
        pins = raw.get("pins")
        if isinstance(pins, Mapping):
            state["pins"] = {str(k): dict(v) for k, v in pins.items() if isinstance(v, Mapping)}
        return state

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self._ensure_dir_unlocked()
        path = self._state_path
        if path.is_symlink():
            raise ArtifactLineageIntegrityError(f"Refusing symlinked lineage state file: {path}")
        state["revision"] = int(state.get("revision") or 0) + 1
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        tmp_path = self._dir / f"{AL_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:10]}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_path, flags, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, path)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(self._dir, flags)
        except OSError:  # pragma: no cover - platform without directory fds
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    # ----------------------------------------------------------------- record

    def record_artifact(
        self,
        content_sha256: str,
        *,
        source_kind: str,
        source_ref: str,
        trust: str = "unverified",
        confidentiality: str = "internal",
        allowed_operations: Sequence[str] = (),
        denied_operations: Sequence[str] = (),
        task_id: str = "",
        security_generation: int | None = None,
        derived_from: Sequence[str] = (),
        size: int | None = None,
    ) -> ProvenanceEntry:
        """Record one sighting of one piece of content.

        The content is deduplicated; the *provenance is not*. Recording the same bytes
        again appends an entry instead of overwriting one, so a trusted sighting cannot
        launder an earlier untrusted one.
        """
        sha = str(content_sha256 or "").strip().lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ArtifactLineageError(f"Content digest must be a lowercase SHA-256 hex string: {content_sha256!r}")
        trust = str(trust or "unverified").strip().lower()
        _al_rank(trust, TRUST_LEVELS, field="trust", default="unverified")
        confidentiality = str(confidentiality or "internal").strip().lower()
        _al_rank(confidentiality, CONFIDENTIALITY_LEVELS, field="confidentiality", default="internal")
        parents = []
        for parent in derived_from:
            clean = str(parent or "").strip().lower()
            if clean and clean not in parents:
                parents.append(clean)

        with self._lock:
            state = self._read_state_unlocked()
            blob = state["blobs"].get(sha)
            if not isinstance(blob, dict):
                blob = {"size": 0, "first_seen": _al_now(), "entries": {}}
            if size is not None:
                blob["size"] = max(int(blob.get("size") or 0), int(size))
            for parent in parents:
                if parent not in state["blobs"]:
                    # A parent we have never seen is recorded as a stub rather than
                    # assumed trustworthy: lineage must not invent a trusted origin.
                    state["blobs"][parent] = {
                        "size": 0,
                        "first_seen": _al_now(),
                        "entries": {},
                        "stub": True,
                    }
            entry = ProvenanceEntry(
                entry_id=_al_id("pe"),
                content_sha256=sha,
                source_kind=str(source_kind or "unknown")[:60],
                source_ref=str(source_ref or "")[:240],
                trust=trust,
                confidentiality=confidentiality,
                allowed_operations=_al_bounded(allowed_operations),
                denied_operations=_al_bounded(denied_operations),
                task_id=str(task_id or "")[:120],
                security_generation=int(security_generation) if isinstance(security_generation, int) else None,
                derived_from=tuple(parents),
                recorded_at=_al_now(),
            )
            entries = blob.setdefault("entries", {})
            entries[entry.entry_id] = entry.to_mapping()
            state["blobs"][sha] = blob
            self._write_state_unlocked(state)
            return entry

    # ----------------------------------------------------------------- queries

    def entries_for(self, content_sha256: str) -> tuple[ProvenanceEntry, ...]:
        """Every provenance entry for this content, oldest first. Dedup, not merge."""
        sha = str(content_sha256 or "").strip().lower()
        with self._lock:
            state = self._read_state_unlocked()
            blob = state["blobs"].get(sha)
            if not isinstance(blob, dict):
                return ()
            entries = [
                ProvenanceEntry.from_mapping(raw)
                for raw in (blob.get("entries") or {}).values()
                if isinstance(raw, Mapping)
            ]
        return tuple(sorted(entries, key=lambda item: item.recorded_at))

    def has_artifact(self, content_sha256: str) -> bool:
        return bool(self.entries_for(content_sha256))

    def effective_trust(self, content_sha256: str) -> str:
        """The weakest trust among every sighting; unknown content is not trusted at all."""
        entries = self.entries_for(content_sha256)
        if not entries:
            return "untrusted"
        return max(entries, key=lambda e: TRUST_LEVELS.index(e.trust)).trust

    def effective_confidentiality(self, content_sha256: str) -> str:
        entries = self.entries_for(content_sha256)
        if not entries:
            return "secret"
        return max(entries, key=lambda e: CONFIDENTIALITY_LEVELS.index(e.confidentiality)).confidentiality

    def task_artifact_confidentialities(self, task_id: str) -> tuple[tuple[str, str], ...]:
        """Return ``(content_sha256, effective_confidentiality)`` pairs linked to ``task_id``."""
        clean_tid = str(task_id or "").strip()
        if not clean_tid:
            return ()
        with self._lock:
            state = self._read_state_unlocked()
            matched_shas: list[str] = []
            for sha, blob in sorted(state["blobs"].items()):
                if not isinstance(blob, Mapping):
                    continue
                for entry in (blob.get("entries") or {}).values():
                    if isinstance(entry, Mapping) and str(entry.get("task_id") or "") == clean_tid:
                        matched_shas.append(str(sha))
                        break
        return tuple((sha, self.effective_confidentiality(sha)) for sha in matched_shas)

    def check_operation(self, content_sha256: str, operation: str) -> OperationVerdict:
        """Deny wins: allowed only if *every* entry allows it and none denies it."""
        entries = self.entries_for(content_sha256)
        if not entries:
            return OperationVerdict(
                False, "unknown_artifact", "No provenance is recorded for this content, so nothing is permitted."
            )
        invalid = [entry for entry in entries if entry.invalidated]
        if invalid:
            return OperationVerdict(
                False,
                "invalidated_artifact",
                f"{len(invalid)} provenance entr{'y is' if len(invalid) == 1 else 'ies are'} invalidated "
                f"({invalid[0].invalidation_reason or 'no reason recorded'}).",
            )
        op = str(operation or "").strip()
        for entry in entries:
            if op and op in entry.denied_operations:
                return OperationVerdict(
                    False,
                    "operation_denied",
                    f"Provenance {entry.entry_id} from {entry.source_kind}:{entry.source_ref or '-'} denies `{op}`.",
                )
        if any(entry.allowed_operations for entry in entries):
            for entry in entries:
                if not entry.allowed_operations:
                    return OperationVerdict(
                        False,
                        "operation_not_allowed",
                        f"Provenance {entry.entry_id} from {entry.source_kind}:{entry.source_ref or '-'} "
                        f"does not allow `{op}`.",
                    )
                if op not in entry.allowed_operations:
                    return OperationVerdict(
                        False,
                        "operation_not_allowed",
                        f"Provenance {entry.entry_id} from {entry.source_kind}:{entry.source_ref or '-'} "
                        f"allows only {', '.join(entry.allowed_operations)}.",
                    )
        return OperationVerdict(True, "allowed", f"Every provenance entry permits `{op}`.")

    def summary(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_state_unlocked()
            blobs = state["blobs"]
            entry_count = sum(len(v.get("entries") or {}) for v in blobs.values() if isinstance(v, Mapping))
            invalidated = sum(
                1
                for blob in blobs.values()
                if isinstance(blob, Mapping)
                for entry in (blob.get("entries") or {}).values()
                if isinstance(entry, Mapping) and entry.get("invalidated_at") is not None
            )
            return {
                "schema_version": AL_SCHEMA_VERSION,
                "revision": int(state["revision"]),
                "artifact_count": len(blobs),
                "provenance_entry_count": entry_count,
                "invalidated_entry_count": invalidated,
                "evidence_count": len(state["evidence"]),
                "stale_evidence_count": sum(
                    1
                    for raw in state["evidence"].values()
                    if isinstance(raw, Mapping) and raw.get("status") == EVIDENCE_STALE
                ),
                "pins": {k: dict(v) for k, v in sorted(state["pins"].items())},
            }

    # ------------------------------------------------------------- invalidation

    def invalidate(self, content_sha256: str, *, reason: str, cause_sha256: str = "") -> tuple[str, ...]:
        """Mark every provenance entry for this content invalid, and cascade to derivatives.

        Invalidation records *why* and never deletes: the content and its history stay
        available for a human, they simply stop being usable as if they were current.
        """
        sha = str(content_sha256 or "").strip().lower()
        note = str(reason or "invalidated")[:240]
        with self._lock:
            state = self._read_state_unlocked()
            now = _al_now()
            affected = self._invalidate_unlocked(state, sha, note, now)
            if affected:
                self._write_state_unlocked(state)
        return tuple(affected)

    def _invalidate_unlocked(self, state: dict[str, Any], sha: str, note: str, now: float) -> list[str]:
        affected: list[str] = []
        frontier = [(sha, 0)]
        seen: set[str] = set()
        while frontier:
            current, depth = frontier.pop(0)
            if current in seen or depth > MAX_CASCADE_DEPTH:
                continue
            seen.add(current)
            blob = state["blobs"].get(current)
            if not isinstance(blob, dict):
                continue
            changed = False
            for raw in (blob.get("entries") or {}).values():
                if isinstance(raw, dict) and raw.get("invalidated_at") is None:
                    raw["invalidated_at"] = now
                    raw["invalidation_reason"] = note if depth == 0 else f"{note} (via {sha[:12]})"
                    changed = True
            if changed:
                affected.append(current)
            for other_sha, other in state["blobs"].items():
                if not isinstance(other, Mapping):
                    continue
                for raw in (other.get("entries") or {}).values():
                    if isinstance(raw, Mapping) and current in (raw.get("derived_from") or ()):
                        frontier.append((str(other_sha), depth + 1))
        # Evidence computed over an invalidated artifact can no longer be accepted.
        for raw in state["evidence"].values():
            if not isinstance(raw, dict):
                continue
            if raw.get("content_sha256") in seen and raw.get("status") == EVIDENCE_VALID:
                raw["status"] = EVIDENCE_STALE
                raw["stale_reason"] = f"artifact {raw.get('content_sha256', '')[:12]} invalidated: {note}"
        return affected

    # ----------------------------------------------------------------- evidence

    def record_evidence(
        self,
        *,
        kind: str,
        subject: str,
        content_sha256: str = "",
        source_files: Mapping[str, str] | None = None,
        task_id: str = "",
        security_generation: int | None = None,
        evidence_id: str = "",
    ) -> EvidenceRecord:
        """Record evidence together with the inputs it is only valid against."""
        files = {str(k)[:240]: str(v).lower() for k, v in (source_files or {}).items()}
        for rel_path, digest in files.items():
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ArtifactLineageError(f"Source digest for {rel_path!r} is not a SHA-256 hex string.")
        record = EvidenceRecord(
            evidence_id=evidence_id or _al_id("ev"),
            kind=str(kind or "unknown")[:60],
            subject=str(subject or "")[:240],
            task_id=str(task_id or "")[:120],
            content_sha256=str(content_sha256 or "").strip().lower(),
            source_files=files,
            security_generation=int(security_generation) if isinstance(security_generation, int) else None,
            recorded_at=_al_now(),
        )
        with self._lock:
            state = self._read_state_unlocked()
            existing = state["evidence"].get(record.evidence_id)
            if isinstance(existing, Mapping):
                raise ArtifactLineageError(f"Evidence {record.evidence_id!r} is already recorded.")
            state["evidence"][record.evidence_id] = record.to_mapping()
            self._write_state_unlocked(state)
        return record

    def evidence(self, evidence_id: str) -> EvidenceRecord:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["evidence"].get(str(evidence_id))
        if not isinstance(raw, Mapping):
            raise ArtifactLineageError(f"Evidence {evidence_id!r} is not recorded.")
        return EvidenceRecord.from_mapping(raw)

    def list_evidence(self, *, task_id: str = "", status: str = "") -> tuple[EvidenceRecord, ...]:
        with self._lock:
            state = self._read_state_unlocked()
            records = [
                EvidenceRecord.from_mapping(raw)
                for raw in state["evidence"].values()
                if isinstance(raw, Mapping)
            ]
        if task_id:
            records = [r for r in records if r.task_id == task_id]
        if status:
            records = [r for r in records if r.status == status]
        return tuple(sorted(records, key=lambda r: r.recorded_at))

    def accept_evidence(
        self,
        evidence_id: str,
        *,
        current_files: Mapping[str, str] | None = None,
        security_generation: int | None = None,
    ) -> EvidenceVerdict:
        """Decide whether recorded evidence is still current, and record the refusal.

        Stale evidence is *not* deleted and not silently served: the record moves to
        `stale` with the reason, so the answer "this was true once, and stopped being
        true for this reason" survives.
        """
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["evidence"].get(str(evidence_id))
            if not isinstance(raw, Mapping):
                raise ArtifactLineageError(f"Evidence {evidence_id!r} is not recorded.")
            record = EvidenceRecord.from_mapping(raw)
            code, reason = "", ""
            if record.status == EVIDENCE_STALE:
                code, reason = "already_stale", record.stale_reason or "previously invalidated"
            elif current_files is not None:
                for rel_path, recorded_digest in record.source_files.items():
                    current = str(current_files.get(rel_path, "")).lower()
                    if not current:
                        code = "source_missing"
                        reason = f"{rel_path} is no longer present; the evidence cannot be tied to it."
                        break
                    if current != recorded_digest:
                        code = "source_changed"
                        reason = (
                            f"{rel_path} changed after the evidence was recorded "
                            f"({recorded_digest[:12]} -> {current[:12]})."
                        )
                        break
            if not code and security_generation is not None and record.security_generation is not None:
                if int(security_generation) != record.security_generation:
                    code = "security_generation_changed"
                    reason = (
                        "the security configuration generation changed after the evidence was recorded "
                        f"(generation {record.security_generation} -> {security_generation})."
                    )
            if not code and record.content_sha256:
                entries = [
                    ProvenanceEntry.from_mapping(item)
                    for item in (state["blobs"].get(record.content_sha256, {}).get("entries") or {}).values()
                    if isinstance(item, Mapping)
                ]
                if any(entry.invalidated for entry in entries):
                    code = "subject_invalidated"
                    reason = f"artifact {record.content_sha256[:12]} is invalidated."
            if code:
                stored = cast("dict[str, Any]", raw)
                if stored.get("status") != EVIDENCE_STALE:
                    stored["status"] = EVIDENCE_STALE
                    stored["stale_reason"] = reason
                    self._write_state_unlocked(state)
                record = replace(record, status=EVIDENCE_STALE, stale_reason=reason)
                return EvidenceVerdict(False, code, reason, record)
            return EvidenceVerdict(
                True, "accepted", "Evidence matches every input it was recorded against.", record
            )

    # --------------------------------------------------------------------- pins

    def pin(self, pin_id: str, *, kind: str, ref: str = "", reason: str = "") -> None:
        """Protect pinned lineage from retention (recovery artifacts, active tasks)."""
        pid = str(pin_id or "").strip()
        if not pid:
            raise ArtifactLineageError("A pin needs an id.")
        with self._lock:
            state = self._read_state_unlocked()
            state["pins"][pid] = {
                "kind": str(kind or "generic")[:40],
                "ref": str(ref or "")[:240],
                "reason": str(reason or "")[:240],
                "created_at": _al_now(),
            }
            self._write_state_unlocked(state)

    def unpin(self, pin_id: str) -> bool:
        with self._lock:
            state = self._read_state_unlocked()
            if str(pin_id) not in state["pins"]:
                return False
            del state["pins"][str(pin_id)]
            self._write_state_unlocked(state)
            return True

    def pins(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            state = self._read_state_unlocked()
            return {k: dict(v) for k, v in sorted(state["pins"].items())}

    # ---------------------------------------------------------------- retention

    def plan_retention(
        self,
        *,
        retain_seconds: float,
        active_task_ids: Sequence[str] = (),
        protected_refs: Mapping[str, str] | None = None,
        extra_protected: Mapping[str, str] | None = None,
        now: float | None = None,
        allow_secret_eviction: bool = False,
    ) -> RetentionPlan:
        """Decide what may be collected, protecting whatever something still depends on.

        A survivor is kept for a named reason, never by accident: an active task, a
        caller-supplied protection (the recovery path's own bytes, via
        `workspace_quarantine.recovery_protection`), a recorded pin, valid evidence, or
        the grace window. Everything else is a candidate.
        """
        stamp = _al_now() if now is None else float(now)
        active = {str(t) for t in active_task_ids if str(t)}
        protected: dict[str, str] = {str(k): str(v) for k, v in (extra_protected or {}).items()}

        for pin_id, pin in self.pins().items():
            ref = str(pin.get("ref") or "")
            if ref:
                protected.setdefault(ref, f"pinned by {pin_id} ({pin.get('kind') or 'generic'})")

        for ref, reason in (protected_refs or {}).items():
            if str(ref):
                protected.setdefault(str(ref), str(reason) or "protected by the caller")

        for record in self.list_evidence():
            if record.status == EVIDENCE_VALID and record.content_sha256:
                protected.setdefault(
                    record.content_sha256,
                    f"referenced by valid evidence {record.evidence_id} ({record.kind})",
                )

        with self._lock:
            state = self._read_state_unlocked()
            blobs = state["blobs"]
            evictable: list[str] = []
            kept: dict[str, str] = dict(protected)
            for sha, blob in sorted(blobs.items()):
                if not isinstance(blob, Mapping):
                    continue
                entries = [
                    ProvenanceEntry.from_mapping(raw)
                    for raw in (blob.get("entries") or {}).values()
                    if isinstance(raw, Mapping)
                ]
                if sha in kept:
                    continue
                if not entries:
                    # A stub parent we never actually saw: nothing to protect, nothing to keep.
                    evictable.append(sha)
                    continue
                task_hit = next((e for e in entries if e.task_id and e.task_id in active), None)
                if task_hit is not None:
                    kept[sha] = f"active task {task_hit.task_id} still references it"
                    continue
                if not allow_secret_eviction and any(e.confidentiality == "secret" for e in entries):
                    kept[sha] = "confidential content is kept by default"
                    continue
                first_seen = float(blob.get("first_seen") or 0.0)
                if stamp - first_seen <= float(retain_seconds):
                    kept[sha] = f"inside the {float(retain_seconds):g}s retention window"
                    continue
                evictable.append(sha)
        return RetentionPlan(
            evictable=tuple(sorted(evictable)),
            protected=kept,
            retain_seconds=float(retain_seconds),
            planned_at=stamp,
        )

    def apply_retention(self, plan: RetentionPlan) -> RetentionResult:
        """Collect exactly the artifacts the plan judged evictable — nothing else."""
        evict = {str(sha) for sha in plan.evictable}
        with self._lock:
            state = self._read_state_unlocked()
            removed = [sha for sha in sorted(evict) if sha in state["blobs"]]
            for sha in removed:
                del state["blobs"][sha]
            if removed:
                self._write_state_unlocked(state)
            kept = tuple(sorted(set(state["blobs"]) - evict))
        return RetentionResult(evicted=tuple(removed), kept=kept)


__all__ = [
    "AL_SCHEMA_VERSION",
    "CONFIDENTIALITY_LEVELS",
    "EVIDENCE_STALE",
    "EVIDENCE_VALID",
    "TRUST_LEVELS",
    "ArtifactLineageError",
    "ArtifactLineageIntegrityError",
    "ArtifactRegistry",
    "EvidenceRecord",
    "EvidenceVerdict",
    "OperationVerdict",
    "ProvenanceEntry",
    "RetentionPlan",
    "RetentionResult",
    "UnknownArtifactError",
    "content_digest",
    "file_content_digest",
]
