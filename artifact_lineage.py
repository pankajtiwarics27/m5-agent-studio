"""Artifact lifecycle: content identity, provenance, selective invalidation, retention.

`M5-P5-02` plus the AP-006 extension (versioned artifact identity). Seven questions this
module answers, and one it deliberately does not:

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
4. **Which version of the artifact is this?** (AP-006.) A content digest alone cannot say
   *who produced it, under which configuration, from which input versions*. A provenance
   entry therefore also carries an optional producer, a producer-configuration digest,
   named input versions and the checks it declares as required. `identity_sha256` is the
   verifiable identity over exactly those fields, so a config or input change produces a
   different identity even when the bytes happen to match.
5. **May a partially-produced artifact be treated as finished?** (AP-006.) No. An artifact
   is `complete` only when it declares required checks and the newest recorded outcome of
   every one of them is `passed`. `artifact_status` reports `partial` (missing or failed
   checks), `invalidated`, or `unknown`, and `require_complete` raises rather than letting
   a caller treat an incomplete artifact as accepted.
6. **Is a stored file reference safe to preview or export?** (AP-006.) `confine_reference`
   resolves one reference inside a caller-owned root component by component with
   `lstat`, refusing `..`, absolute escapes, symlinked components and non-regular files;
   `check_preview` additionally refuses unknown, invalidated, untrusted, secret and
   digest-mismatched artifacts. Preview is a read of untrusted bytes, not an execution.
7. **May this artifact's bytes be removed, and what survives?** (AP-006.) `export_artifact`
   gates on confidentiality and the deny-wins operation verdict and records the export in
   history; `delete_artifact` refuses pins, valid evidence and live descendants, and
   leaves a tombstone that keeps the identity and the scoped reference history (source
   kinds, task ids, checks) while dropping the content and the free-form source reference.

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
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, cast

# Schema v2 adds producer/configuration/input identities, declared checks, and the
# export/deletion history (AP-006). A v1 store is read as-is — every v1 record already
# has every field, because the additions are optional — and is rewritten as v2 by the
# next atomic store write. A store written by a *newer* schema is refused instead of
# being silently downgraded.
AL_SCHEMA_VERSION = 2
AL_SUPPORTED_SCHEMA_VERSIONS = (1, AL_SCHEMA_VERSION)
AL_DIRNAME = "artifacts"
AL_STATE_NAME = "state.json"
AL_TEMP_PREFIX = ".al-state-"

# Ordered weakest-last: an artifact's effective trust is the weakest entry it has.
TRUST_LEVELS = ("trusted", "unverified", "untrusted")
# Ordered most-public-first: an artifact's effective confidentiality is the strictest.
CONFIDENTIALITY_LEVELS = ("public", "internal", "secret")

EVIDENCE_VALID = "valid"
EVIDENCE_STALE = "stale"

CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_OUTCOMES = (CHECK_PASSED, CHECK_FAILED)

ARTIFACT_COMPLETE = "complete"
ARTIFACT_PARTIAL = "partial"
ARTIFACT_INVALIDATED = "invalidated"
ARTIFACT_UNKNOWN = "unknown"

MAX_CASCADE_DEPTH = 8
MAX_INPUTS = 32
MAX_CHECKS = 32


class ArtifactLineageError(RuntimeError):
    """Base error for artifact lineage failures."""


class ArtifactLineageIntegrityError(ArtifactLineageError):
    """The lineage store is unreadable, unsafe, or of an unsupported schema version."""


class UnknownArtifactError(ArtifactLineageError):
    """Asked about content that was never recorded."""


class ArtifactIncompleteError(ArtifactLineageError):
    """An artifact was asked to count as accepted or complete while it is not."""


class ArtifactReferenceError(ArtifactLineageError):
    """A stored reference is unsafe: traversal, escape, symlink or digest mismatch."""


class ArtifactExportError(ArtifactLineageError):
    """An export was refused because the artifact may not leave its recorded scope."""


class ArtifactDeletionError(ArtifactLineageError):
    """A deletion was refused because something durable still depends on the artifact."""


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


def _al_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _al_is_digest(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _al_digest_or_empty(value: Any, field_name: str) -> str:
    text = _al_text(value, 64).lower()
    if text and not _al_is_digest(text):
        raise ArtifactLineageError(f"{field_name} must be a lowercase SHA-256 hex string when set.")
    return text


def _al_inputs(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Named input versions: what this artifact was produced from.

    Values are opaque version tokens (a digest, a revision, a task version), bounded and
    stored verbatim. They are identities to compare, never instructions to follow.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ArtifactLineageError("inputs must map an input name to the version used.")
    inputs: dict[str, str] = {}
    for key, value in raw.items():
        name = _al_text(key, 120)
        version = _al_text(value, 120)
        if not name:
            raise ArtifactLineageError("An input needs a name.")
        if not version:
            raise ArtifactLineageError(f"Input {name!r} needs the version that was used.")
        inputs[name] = version
        if len(inputs) > MAX_INPUTS:
            raise ArtifactLineageError(f"At most {MAX_INPUTS} inputs may be recorded per artifact.")
    return inputs


def _al_checks(raw: Iterable[Any]) -> tuple[str, ...]:
    """Declared required checks: names a caller must verify before this artifact is done."""
    checks: list[str] = []
    for item in raw:
        name = _al_text(item, 60)
        if not name:
            raise ArtifactLineageError("A declared check needs a name.")
        if name not in checks:
            checks.append(name)
        if len(checks) > MAX_CHECKS:
            raise ArtifactLineageError(f"At most {MAX_CHECKS} required checks may be declared.")
    return tuple(checks)


def artifact_identity_sha256(
    content_sha256: str,
    *,
    producer: str = "",
    producer_version: str = "",
    config_sha256: str = "",
    inputs: Mapping[str, Any] | None = None,
) -> str:
    """The verifiable identity of one artifact *version*.

    Content identity answers "what are the bytes"; this answers "which version of which
    thing": the bytes plus the producer, its configuration and the input versions it was
    produced from. Two runs of the same producer over the same inputs on the same content
    share an identity; changing any one of them produces a different one.
    """
    sha = _al_text(content_sha256, 64).lower()
    if not _al_is_digest(sha):
        raise ArtifactLineageError(f"Content digest must be a lowercase SHA-256 hex string: {content_sha256!r}")
    canonical = {
        "content_sha256": sha,
        "producer": _al_text(producer, 120),
        "producer_version": _al_text(producer_version, 60),
        "config_sha256": _al_digest_or_empty(config_sha256, "Producer configuration digest"),
        "inputs": dict(sorted(_al_inputs(inputs).items())),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfinedReference:
    """A file reference that was proven to sit inside an approved root."""

    root: str
    relative_path: str
    content_sha256: str
    size: int
    modified_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "relative_path": self.relative_path,
            "content_sha256": self.content_sha256,
            "size": self.size,
            "modified_at": self.modified_at,
        }


def confine_reference(root: Path | str, reference: Any, *, expected_sha256: str = "") -> ConfinedReference:
    """Resolve one stored artifact reference inside ``root``, refusing every escape.

    The walk is component by component with ``lstat``: a symlink anywhere in the path is
    refused rather than followed, so a link planted inside the root cannot point the
    caller at something outside it. Relative and absolute references are both accepted,
    as long as the result is inside the root, and ``expected_sha256`` — when supplied —
    must match the bytes on disk exactly.
    """
    if not isinstance(reference, str) or not reference or "\x00" in reference or "\\" in reference:
        raise ArtifactReferenceError("An artifact reference must be a plain non-empty path.")
    base_raw = Path(root).expanduser()
    try:
        base = base_raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactReferenceError(f"The artifact root is unavailable: {base_raw}") from exc
    if not base.is_dir():
        raise ArtifactReferenceError(f"The artifact root is not a directory: {base}")

    candidate = Path(reference)
    if candidate.is_absolute():
        resolved_parent = Path(os.path.normpath(str(candidate)))
        try:
            relative = resolved_parent.relative_to(base)
        except ValueError as exc:
            raise ArtifactReferenceError("The reference is outside the approved artifact root.") from exc
    else:
        relative = candidate
    parts = relative.parts
    if not parts or any(part in ("..", ".", "") for part in parts):
        raise ArtifactReferenceError("Parent traversal and empty path segments are not allowed.")

    current = base
    info: os.stat_result | None = None
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise ArtifactReferenceError(f"The reference is unavailable: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactReferenceError(
                f"The reference crosses a symlink at {part!r}; symlink escape is refused."
            )
    assert info is not None
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactReferenceError("Only regular files can be recorded as artifact references.")

    digest = file_content_digest(current)
    expected = _al_text(expected_sha256, 64).lower()
    if expected and expected != digest:
        raise ArtifactReferenceError(
            f"The reference digest does not match the recorded identity ({expected[:12]} != {digest[:12]})."
        )
    return ConfinedReference(
        root=str(base),
        relative_path=str(PurePosixPath(*parts)),
        content_sha256=digest,
        size=int(info.st_size),
        modified_at=float(info.st_mtime),
    )


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
    producer: str = ""
    producer_version: str = ""
    config_sha256: str = ""
    inputs: Mapping[str, str] = field(default_factory=dict)
    required_checks: tuple[str, ...] = ()

    @property
    def invalidated(self) -> bool:
        return self.invalidated_at is not None

    @property
    def identity_sha256(self) -> str:
        """The verifiable identity of this artifact version (AP-006)."""
        return artifact_identity_sha256(
            self.content_sha256,
            producer=self.producer,
            producer_version=self.producer_version,
            config_sha256=self.config_sha256,
            inputs=self.inputs,
        )

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
            "producer": self.producer,
            "producer_version": self.producer_version,
            "config_sha256": self.config_sha256,
            "inputs": dict(sorted(self.inputs.items())),
            "required_checks": list(self.required_checks),
            "identity_sha256": self.identity_sha256,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProvenanceEntry:
        generation = raw.get("security_generation")
        invalidated_at = raw.get("invalidated_at")
        entries_raw = raw.get("inputs")
        inputs = {
            str(name): str(version)
            for name, version in (entries_raw.items() if isinstance(entries_raw, Mapping) else ())
        }
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
            producer=str(raw.get("producer") or ""),
            producer_version=str(raw.get("producer_version") or ""),
            config_sha256=str(raw.get("config_sha256") or ""),
            inputs=inputs,
            required_checks=tuple(str(x) for x in raw.get("required_checks") or ()),
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


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    """One recorded outcome of one declared check, kept in history."""

    content_sha256: str
    name: str
    outcome: str
    detail: str
    recorded_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "name": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ArtifactCheck:
        return cls(
            content_sha256=str(raw.get("content_sha256") or ""),
            name=str(raw.get("name") or ""),
            outcome=str(raw.get("outcome") or ""),
            detail=str(raw.get("detail") or ""),
            recorded_at=float(raw.get("recorded_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class ArtifactStatus:
    """Whether an artifact is a finished, checked deliverable, and why not."""

    content_sha256: str
    identity_sha256: str
    state: str
    complete: bool
    code: str
    reason: str
    required_checks: tuple[str, ...] = ()
    missing_checks: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "identity_sha256": self.identity_sha256,
            "state": self.state,
            "complete": self.complete,
            "code": self.code,
            "reason": self.reason,
            "required_checks": list(self.required_checks),
            "missing_checks": list(self.missing_checks),
            "failed_checks": list(self.failed_checks),
        }


@dataclass(frozen=True, slots=True)
class PreviewVerdict:
    """Whether a confined preview may be shown, and what may be shown."""

    allowed: bool
    code: str
    reason: str
    reference: ConfinedReference | None
    complete: bool
    identity_sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "reference": self.reference.to_mapping() if self.reference else None,
            "complete": self.complete,
            "identity_sha256": self.identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class InputInvalidationResult:
    """Which artifacts an input-version change invalidated, and which it left alone."""

    changed_inputs: Mapping[str, str]
    invalidated: tuple[str, ...]
    reason: str
    invalidated_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "changed_inputs": dict(sorted(self.changed_inputs.items())),
            "invalidated": list(self.invalidated),
            "reason": self.reason,
            "invalidated_at": self.invalidated_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactExport:
    """A scoped export envelope: identity plus the history a reader needs to judge it."""

    export_id: str
    content_sha256: str
    identity_sha256: str
    trust: str
    confidentiality: str
    state: str
    complete: bool
    missing_checks: tuple[str, ...]
    entries: tuple[Mapping[str, Any], ...]
    exported_at: float
    requested_by: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "content_sha256": self.content_sha256,
            "identity_sha256": self.identity_sha256,
            "trust": self.trust,
            "confidentiality": self.confidentiality,
            "state": self.state,
            "complete": self.complete,
            "missing_checks": list(self.missing_checks),
            "entries": [dict(entry) for entry in self.entries],
            "exported_at": self.exported_at,
            "requested_by": self.requested_by,
        }


@dataclass(frozen=True, slots=True)
class ArtifactDeletion:
    """What survived a deletion: the identity and the scoped reference history."""

    content_sha256: str
    identity_sha256: str
    reason: str
    deleted_at: float
    preserved_references: tuple[Mapping[str, Any], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "identity_sha256": self.identity_sha256,
            "reason": self.reason,
            "deleted_at": self.deleted_at,
            "preserved_references": [dict(item) for item in self.preserved_references],
        }


def _al_empty_state() -> dict[str, Any]:
    return {
        "schema_version": AL_SCHEMA_VERSION,
        "revision": 0,
        "blobs": {},
        "evidence": {},
        "pins": {},
        "checks": {},
        "exports": [],
        "tombstones": {},
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
        if not isinstance(version, int) or version not in AL_SUPPORTED_SCHEMA_VERSIONS:
            raise ArtifactLineageIntegrityError(
                f"Unsupported artifact lineage schema_version {version!r}; supported: "
                f"{', '.join(str(item) for item in AL_SUPPORTED_SCHEMA_VERSIONS)}."
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
        checks = raw.get("checks")
        if isinstance(checks, Mapping):
            state["checks"] = {
                str(k): [dict(item) for item in v if isinstance(item, Mapping)]
                for k, v in checks.items()
                if isinstance(v, list)
            }
        exports = raw.get("exports")
        if isinstance(exports, list):
            state["exports"] = [dict(item) for item in exports if isinstance(item, Mapping)]
        tombstones = raw.get("tombstones")
        if isinstance(tombstones, Mapping):
            state["tombstones"] = {str(k): dict(v) for k, v in tombstones.items() if isinstance(v, Mapping)}
        # A v1 store needs no field rewriting: every v2 addition is optional, so the
        # existing records already carry their full meaning (M5-P5-02 semantics). The
        # next atomic write stamps schema_version 2 and keeps every record.
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
        producer: str = "",
        producer_version: str = "",
        config_sha256: str = "",
        inputs: Mapping[str, Any] | None = None,
        required_checks: Iterable[str] = (),
    ) -> ProvenanceEntry:
        """Record one sighting of one piece of content.

        The content is deduplicated; the *provenance is not*. Recording the same bytes
        again appends an entry instead of overwriting one, so a trusted sighting cannot
        launder an earlier untrusted one.

        ``producer``/``producer_version``/``config_sha256``/``inputs`` describe the version
        that produced this sighting and feed ``identity_sha256``; ``required_checks``
        declares the checks that must pass before the artifact counts as complete.
        """
        sha = str(content_sha256 or "").strip().lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ArtifactLineageError(f"Content digest must be a lowercase SHA-256 hex string: {content_sha256!r}")
        producer = _al_text(producer, 120)
        producer_version = _al_text(producer_version, 60)
        config_sha256 = _al_digest_or_empty(config_sha256, "Producer configuration digest")
        input_versions = _al_inputs(inputs)
        declared_checks = _al_checks(required_checks)
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
                producer=producer,
                producer_version=producer_version,
                config_sha256=config_sha256,
                inputs=input_versions,
                required_checks=declared_checks,
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

    # ---------------------------------------------- artifact versions and checks

    def record_file_artifact(
        self,
        root: Path | str,
        reference: Any,
        *,
        expected_sha256: str = "",
        source_kind: str = "workspace_file",
        source_ref: str = "",
        trust: str = "unverified",
        confidentiality: str = "internal",
        allowed_operations: Sequence[str] = (),
        denied_operations: Sequence[str] = (),
        task_id: str = "",
        security_generation: int | None = None,
        derived_from: Sequence[str] = (),
        producer: str = "",
        producer_version: str = "",
        config_sha256: str = "",
        inputs: Mapping[str, Any] | None = None,
        required_checks: Iterable[str] = (),
    ) -> ProvenanceEntry:
        """Record a file artifact whose bytes were confined and re-hashed from disk.

        The caller supplies the root it already owns. The digest is computed from the
        confined file, never accepted from the caller, and the recorded ``source_ref`` is
        the relative path inside that root rather than an arbitrary string.
        """
        confined = confine_reference(root, reference, expected_sha256=expected_sha256)
        return self.record_artifact(
            confined.content_sha256,
            source_kind=source_kind,
            source_ref=_al_text(source_ref, 240) or confined.relative_path,
            trust=trust,
            confidentiality=confidentiality,
            allowed_operations=allowed_operations,
            denied_operations=denied_operations,
            task_id=task_id,
            security_generation=security_generation,
            derived_from=derived_from,
            size=confined.size,
            producer=producer,
            producer_version=producer_version,
            config_sha256=config_sha256,
            inputs=inputs,
            required_checks=required_checks,
        )

    def record_check(self, content_sha256: str, name: Any, *, outcome: str, detail: str = "") -> ArtifactCheck:
        """Record one outcome of one check. History is append-only; the newest wins.

        A failing check does not erase an earlier pass and a later pass does not erase an
        earlier failure — both stay in the record, and the newest outcome is the one
        `artifact_status` judges. Re-verifying after a fix therefore legitimately clears
        the failure, while the failure itself remains auditable.
        """
        sha = _al_text(content_sha256, 64).lower()
        check_name = _al_text(name, 60)
        if not check_name:
            raise ArtifactLineageError("A check needs a name.")
        result = _al_text(outcome, 20).lower()
        if result not in CHECK_OUTCOMES:
            raise ArtifactLineageError(f"Check outcome must be one of {', '.join(CHECK_OUTCOMES)}.")
        record = ArtifactCheck(
            content_sha256=sha,
            name=check_name,
            outcome=result,
            detail=_al_text(detail, 400),
            recorded_at=_al_now(),
        )
        with self._lock:
            state = self._read_state_unlocked()
            if not isinstance(state["blobs"].get(sha), Mapping):
                raise UnknownArtifactError(
                    f"No provenance is recorded for {sha or 'this content'}; record the artifact before its checks."
                )
            state["checks"].setdefault(sha, []).append(record.to_mapping())
            self._write_state_unlocked(state)
        return record

    def checks_for(self, content_sha256: str) -> tuple[ArtifactCheck, ...]:
        sha = _al_text(content_sha256, 64).lower()
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["checks"].get(sha) or []
        return tuple(ArtifactCheck.from_mapping(item) for item in raw if isinstance(item, Mapping))

    @staticmethod
    def _latest_outcomes(checks: Sequence[ArtifactCheck]) -> dict[str, str]:
        latest: dict[str, str] = {}
        for check in sorted(checks, key=lambda item: item.recorded_at):
            latest[check.name] = check.outcome
        return latest

    def artifact_status(self, content_sha256: str) -> ArtifactStatus:
        """Is this a finished, checked artifact? Partial and invalidated never count."""
        sha = _al_text(content_sha256, 64).lower()
        entries = self.entries_for(sha)
        if not entries:
            return ArtifactStatus(
                content_sha256=sha,
                identity_sha256="",
                state=ARTIFACT_UNKNOWN,
                complete=False,
                code="unknown_artifact",
                reason="No provenance is recorded for this content.",
            )
        declared: list[str] = []
        for entry in entries:
            for check_name in entry.required_checks:
                if check_name not in declared:
                    declared.append(check_name)
        required = tuple(declared)
        identity = entries[-1].identity_sha256
        invalid = [entry for entry in entries if entry.invalidated]
        if invalid:
            return ArtifactStatus(
                content_sha256=sha,
                identity_sha256=identity,
                state=ARTIFACT_INVALIDATED,
                complete=False,
                code="invalidated_artifact",
                reason=invalid[0].invalidation_reason or "invalidated with no reason recorded",
                required_checks=required,
            )
        latest = self._latest_outcomes(self.checks_for(sha))
        missing = tuple(name for name in required if name not in latest)
        failed = tuple(name for name in required if latest.get(name) == CHECK_FAILED)
        if not required:
            return ArtifactStatus(
                content_sha256=sha,
                identity_sha256=identity,
                state=ARTIFACT_PARTIAL,
                complete=False,
                code="checks_not_declared",
                reason="This artifact declares no required checks, so it cannot count as complete.",
                required_checks=required,
            )
        if missing or failed:
            parts = []
            if missing:
                parts.append(f"missing checks: {', '.join(missing)}")
            if failed:
                parts.append(f"failed checks: {', '.join(failed)}")
            return ArtifactStatus(
                content_sha256=sha,
                identity_sha256=identity,
                state=ARTIFACT_PARTIAL,
                complete=False,
                code="checks_failed" if failed else "checks_missing",
                reason="; ".join(parts),
                required_checks=required,
                missing_checks=missing,
                failed_checks=failed,
            )
        return ArtifactStatus(
            content_sha256=sha,
            identity_sha256=identity,
            state=ARTIFACT_COMPLETE,
            complete=True,
            code="complete",
            reason=f"Every declared check passed ({', '.join(required)}).",
            required_checks=required,
        )

    def require_complete(self, content_sha256: str) -> ArtifactStatus:
        """Return the status, or raise when the artifact is not a finished deliverable."""
        status = self.artifact_status(content_sha256)
        if not status.complete:
            label = status.content_sha256[:12] or "?"
            raise ArtifactIncompleteError(f"Artifact {label} is {status.state}: {status.reason}")
        return status

    def check_preview(
        self,
        content_sha256: str,
        root: Path | str,
        reference: Any,
        *,
        expected_sha256: str = "",
        allow_secret: bool = False,
        operation: str = "preview",
    ) -> PreviewVerdict:
        """Decide whether a stored file may be shown as this artifact's preview.

        A preview is a read of bytes someone else produced. It is refused for unknown,
        invalidated, untrusted or secret content, and for any reference that leaves the
        approved root, crosses a symlink, or no longer hashes to the recorded identity.
        ``complete`` is reported so a UI can label a partial preview instead of hiding it.
        """
        sha = _al_text(content_sha256, 64).lower()
        status = self.artifact_status(sha)
        if status.state == ARTIFACT_UNKNOWN:
            return PreviewVerdict(False, "unknown_artifact", status.reason, None, False, "")
        if status.state == ARTIFACT_INVALIDATED:
            return PreviewVerdict(False, "invalidated_artifact", status.reason, None, False, status.identity_sha256)
        if not allow_secret and self.effective_confidentiality(sha) == "secret":
            return PreviewVerdict(
                False,
                "confidential_content",
                "Secret content needs an explicit preview allowance.",
                None,
                status.complete,
                status.identity_sha256,
            )
        if self.effective_trust(sha) == "untrusted":
            return PreviewVerdict(
                False,
                "untrusted_content",
                "Content from an untrusted source is never rendered as an active preview.",
                None,
                status.complete,
                status.identity_sha256,
            )
        verdict = self.check_operation(sha, operation)
        if not verdict.allowed:
            return PreviewVerdict(False, verdict.code, verdict.reason, None, status.complete, status.identity_sha256)
        try:
            confined = confine_reference(root, reference, expected_sha256=_al_text(expected_sha256, 64).lower() or sha)
        except ArtifactReferenceError as exc:
            return PreviewVerdict(False, "unsafe_reference", str(exc), None, status.complete, status.identity_sha256)
        return PreviewVerdict(
            True,
            "allowed",
            "The reference is confined to the approved root and matches the recorded identity.",
            confined,
            status.complete,
            status.identity_sha256,
        )

    # ------------------------------------------------- selective input invalidation

    def invalidate_for_inputs(
        self, changed_inputs: Mapping[str, Any], *, reason: str
    ) -> InputInvalidationResult:
        """Invalidate exactly the artifacts produced from inputs whose version moved.

        Only entries whose recorded input version differs from the supplied one are
        affected, plus their derivatives by the existing cascade. Artifacts that never
        consumed a changed input — and artifacts whose recorded version already matches —
        are untouched, so unrelated work keeps working.
        """
        changes = _al_inputs(changed_inputs)
        if not changes:
            raise ArtifactLineageError("An input invalidation needs at least one changed input version.")
        note = _al_text(reason, 240) or "input version changed"
        now = _al_now()
        with self._lock:
            state = self._read_state_unlocked()
            seeds: list[str] = []
            for sha, blob in sorted(state["blobs"].items()):
                if not isinstance(blob, Mapping):
                    continue
                for raw in (blob.get("entries") or {}).values():
                    if not isinstance(raw, Mapping):
                        continue
                    recorded = raw.get("inputs")
                    if not isinstance(recorded, Mapping):
                        continue
                    if any(
                        name in recorded and str(recorded[name]) != version
                        for name, version in changes.items()
                    ):
                        seeds.append(str(sha))
                        break
            invalidated: list[str] = []
            for sha in seeds:
                for affected in self._invalidate_unlocked(state, sha, note, now):
                    if affected not in invalidated:
                        invalidated.append(affected)
            if invalidated:
                self._write_state_unlocked(state)
        return InputInvalidationResult(
            changed_inputs=changes,
            invalidated=tuple(invalidated),
            reason=note,
            invalidated_at=now,
        )

    # -------------------------------------------------------- export and deletion

    def export_artifact(
        self, content_sha256: str, *, allow_secret: bool = False, requested_by: str = ""
    ) -> ArtifactExport:
        """Assemble a scoped export envelope and record that the export happened.

        Confidential content needs an explicit allowance and the deny-wins operation
        verdict must permit ``export``. An invalidated artifact cannot be exported at
        all; a partial one is exported with ``complete=False`` and its missing checks, so
        it cannot be mistaken for a finished deliverable.
        """
        sha = _al_text(content_sha256, 64).lower()
        status = self.artifact_status(sha)
        if status.state == ARTIFACT_UNKNOWN:
            raise UnknownArtifactError(f"No provenance is recorded for {sha or 'this content'}.")
        if status.state == ARTIFACT_INVALIDATED:
            raise ArtifactExportError(f"Artifact {sha[:12]} is invalidated: {status.reason}")
        if not allow_secret and self.effective_confidentiality(sha) == "secret":
            raise ArtifactExportError("Confidential content needs an explicit export allowance.")
        verdict = self.check_operation(sha, "export")
        if not verdict.allowed:
            raise ArtifactExportError(verdict.reason)
        export = ArtifactExport(
            export_id=_al_id("ex"),
            content_sha256=sha,
            identity_sha256=status.identity_sha256,
            trust=self.effective_trust(sha),
            confidentiality=self.effective_confidentiality(sha),
            state=status.state,
            complete=status.complete,
            missing_checks=status.missing_checks,
            entries=tuple(entry.to_mapping() for entry in self.entries_for(sha)),
            exported_at=_al_now(),
            requested_by=_al_text(requested_by, 120),
        )
        with self._lock:
            state = self._read_state_unlocked()
            state["exports"].append(export.to_mapping())
            self._write_state_unlocked(state)
        return export

    def exports(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            state = self._read_state_unlocked()
            return tuple(dict(item) for item in state["exports"])

    def delete_artifact(
        self,
        content_sha256: str,
        *,
        reason: str,
        confirmed: bool = False,
        allow_secret: bool = False,
    ) -> ArtifactDeletion:
        """Remove one artifact's recorded bytes-identity, keeping its reference history.

        Refused when pinned, when valid evidence still rests on it, or when a current
        artifact is derived from it. What remains is a tombstone: the identity, the
        reason, and the scoped reference history (source kinds, task ids, trust,
        confidentiality and declared checks) — never the content and never the free-form
        source reference, which can carry a path or a URL.
        """
        sha = _al_text(content_sha256, 64).lower()
        note = _al_text(reason, 240)
        if confirmed is not True:
            raise ArtifactDeletionError("Deleting an artifact requires explicit confirmation.")
        if not note:
            raise ArtifactDeletionError("A deletion needs a recorded reason.")
        entries = self.entries_for(sha)
        if not entries:
            raise UnknownArtifactError(f"No provenance is recorded for {sha or 'this content'}.")
        if not allow_secret and self.effective_confidentiality(sha) == "secret":
            raise ArtifactDeletionError("Confidential content needs an explicit deletion allowance.")
        with self._lock:
            state = self._read_state_unlocked()
            pinned = {
                str(pin.get("ref") or ""): str(pin_id)
                for pin_id, pin in state["pins"].items()
                if isinstance(pin, Mapping)
            }
            if sha in pinned:
                raise ArtifactDeletionError(
                    f"Artifact {sha[:12]} is pinned by {pinned[sha]}; release the pin first."
                )
            for raw in state["evidence"].values():
                if (
                    isinstance(raw, Mapping)
                    and str(raw.get("content_sha256") or "") == sha
                    and raw.get("status") == EVIDENCE_VALID
                ):
                    raise ArtifactDeletionError(
                        f"Evidence {raw.get('evidence_id')} still rests on artifact {sha[:12]}."
                    )
            for other_sha, blob in state["blobs"].items():
                if str(other_sha) == sha or not isinstance(blob, Mapping):
                    continue
                for raw in (blob.get("entries") or {}).values():
                    if (
                        isinstance(raw, Mapping)
                        and sha in (raw.get("derived_from") or ())
                        and raw.get("invalidated_at") is None
                    ):
                        raise ArtifactDeletionError(
                            f"Artifact {str(other_sha)[:12]} is derived from {sha[:12]} and is still current."
                        )
            preserved = tuple(
                {
                    "entry_id": entry.entry_id,
                    "source_kind": entry.source_kind,
                    "task_id": entry.task_id,
                    "trust": entry.trust,
                    "confidentiality": entry.confidentiality,
                    "recorded_at": entry.recorded_at,
                    "invalidated_at": entry.invalidated_at,
                    "required_checks": list(entry.required_checks),
                }
                for entry in entries
            )
            deletion = ArtifactDeletion(
                content_sha256=sha,
                identity_sha256=entries[-1].identity_sha256,
                reason=note,
                deleted_at=_al_now(),
                preserved_references=preserved,
            )
            del state["blobs"][sha]
            for raw in state["evidence"].values():
                if (
                    isinstance(raw, Mapping)
                    and str(raw.get("content_sha256") or "") == sha
                    and raw.get("status") == EVIDENCE_VALID
                ):
                    stored = cast("dict[str, Any]", raw)
                    stored["status"] = EVIDENCE_STALE
                    stored["stale_reason"] = f"artifact {sha[:12]} deleted: {note}"
            state["tombstones"][sha] = deletion.to_mapping()
            self._write_state_unlocked(state)
        return deletion

    def tombstones(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            state = self._read_state_unlocked()
            return {sha: dict(raw) for sha, raw in sorted(state["tombstones"].items())}

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
                "check_record_count": sum(len(items) for items in state["checks"].values()),
                "artifacts_without_required_checks": sum(
                    1
                    for blob in blobs.values()
                    if isinstance(blob, Mapping)
                    and blob.get("entries")
                    and not any(
                        (raw.get("required_checks") or [])
                        for raw in (blob.get("entries") or {}).values()
                        if isinstance(raw, Mapping)
                    )
                ),
                "export_count": len(state["exports"]),
                "tombstone_count": len(state["tombstones"]),
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
    "AL_SUPPORTED_SCHEMA_VERSIONS",
    "ARTIFACT_COMPLETE",
    "ARTIFACT_INVALIDATED",
    "ARTIFACT_PARTIAL",
    "ARTIFACT_UNKNOWN",
    "CHECK_FAILED",
    "CHECK_OUTCOMES",
    "CHECK_PASSED",
    "CONFIDENTIALITY_LEVELS",
    "EVIDENCE_STALE",
    "EVIDENCE_VALID",
    "TRUST_LEVELS",
    "ArtifactCheck",
    "ArtifactDeletion",
    "ArtifactDeletionError",
    "ArtifactExport",
    "ArtifactExportError",
    "ArtifactIncompleteError",
    "ArtifactLineageError",
    "ArtifactLineageIntegrityError",
    "ArtifactReferenceError",
    "ArtifactRegistry",
    "ArtifactStatus",
    "ConfinedReference",
    "EvidenceRecord",
    "EvidenceVerdict",
    "InputInvalidationResult",
    "OperationVerdict",
    "PreviewVerdict",
    "ProvenanceEntry",
    "RetentionPlan",
    "RetentionResult",
    "UnknownArtifactError",
    "artifact_identity_sha256",
    "confine_reference",
    "content_digest",
    "file_content_digest",
]
