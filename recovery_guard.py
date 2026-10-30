"""Durable failure memory, stagnation detection and bounded recovery (M5-P2-05).

The runtime already bounds *total* work per task (steps, tool calls, tokens,
elapsed time, recovery attempts) and already refuses to complete a coding task
without authoritative evidence. What it did not do is remember **why** earlier
attempts failed. Without that memory a loop can retry the same strategy forever
within its step budget, or oscillate between two patch states, and every retry
looks like fresh work.

This module is the memory and the bound:

* every verification / recovery / rollback attempt is appended to a durable
  per-task document with an :class:`EvidenceSnapshot` — the source revision, the
  transcript hash, the failure fingerprint and the per-file digests;
* :func:`detect_stagnation` classifies a new attempt as ``first``, ``progress``,
  ``stagnant`` (identical evidence to the previous failure) or ``oscillating``
  (a previously abandoned state is revisited);
* retry / replan / rollback counters live in the same document as the history,
  so "consume a budget" and "remember the attempt" cannot diverge across a
  crash, and the counters survive restart with the failures they describe;
* :meth:`RecoveryGuard.require_progress` refuses a recovery action whose evidence
  has not changed, and an escalated verdict requests a safe pause instead of
  another attempt.

Standard library only: this module is inlined into the standalone ``studio.py``
bundle, so it must not import anything outside the standard library and must not
define a top-level name another inlined module already uses.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

RECOVERY_SCHEMA_VERSION = 1

#: Bounded history: the newest attempts are kept, the rest are pruned.
MAX_ATTEMPTS = 100
#: Bounded document: a corrupt or hostile file larger than this is refused.
MAX_DOCUMENT_BYTES = 1 << 20
#: Bound on any stored free-text field.
MAX_TEXT = 300

ATTEMPT_KINDS: tuple[str, ...] = ("verification", "recovery", "rollback", "replan")
ATTEMPT_OUTCOMES: tuple[str, ...] = ("failed", "verified", "refused")
BUDGET_KINDS: tuple[str, ...] = ("retry", "replan", "rollback")

STAGNATION_FIRST = "first"
STAGNATION_PROGRESS = "progress"
STAGNATION_STAGNANT = "stagnant"
STAGNATION_OSCILLATING = "oscillating"

_ERG_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_ERG_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RecoveryGuardError(ValueError):
    """Raised when recovery memory or a recovery budget cannot be honoured."""


class RecoveryRejected(RecoveryGuardError):
    """Raised when an action is refused because it would repeat known evidence."""


def _erg_safe_task_id(task_id: str) -> str:
    cleaned = str(task_id or "").strip()
    if not _ERG_SAFE_ID.match(cleaned):
        raise RecoveryGuardError("task_id must be 1-120 characters of [A-Za-z0-9._-].")
    return cleaned


def _erg_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _erg_bounded(text: Any, limit: int = MAX_TEXT) -> str:
    if text is None:
        return ""
    raw = str(text)
    if "\x00" in raw:
        raise RecoveryGuardError("stored text must not contain NUL.")
    return raw[:limit]


def _erg_non_neg_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        else:
            raise RecoveryGuardError(f"{name} must be a non-negative integer.")
    if value < 0:
        raise RecoveryGuardError(f"{name} must be a non-negative integer.")
    return int(value)


@dataclass(frozen=True, slots=True)
class RecoveryLimits:
    """Bounds on repeated recovery work for one task."""

    max_retries: int = 3
    max_replans: int = 3
    max_rollbacks: int = 3
    #: Identical failure evidence tolerated before a safe pause is requested.
    max_identical_attempts: int = 2

    def __post_init__(self) -> None:
        for name in ("max_retries", "max_replans", "max_rollbacks", "max_identical_attempts"):
            _erg_non_neg_int(getattr(self, name), name)
        if self.max_identical_attempts < 2:
            raise RecoveryGuardError("max_identical_attempts must be at least 2.")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_retries": self.max_retries,
            "max_replans": self.max_replans,
            "max_rollbacks": self.max_rollbacks,
            "max_identical_attempts": self.max_identical_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RecoveryLimits:
        raw = data if isinstance(data, dict) else {}
        return cls(
            max_retries=_erg_non_neg_int(raw.get("max_retries", 3), "max_retries"),
            max_replans=_erg_non_neg_int(raw.get("max_replans", 3), "max_replans"),
            max_rollbacks=_erg_non_neg_int(raw.get("max_rollbacks", 3), "max_rollbacks"),
            max_identical_attempts=_erg_non_neg_int(
                raw.get("max_identical_attempts", 2), "max_identical_attempts"
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """What one attempt could observe: the conditions it ran under and what failed."""

    revision_digest: str = ""
    transcript_sha256: str = ""
    failure_fingerprint: str = ""
    failure_code: str = ""
    failure_ids: tuple[str, ...] = ()
    environment_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("revision_digest", "transcript_sha256", "failure_fingerprint", "environment_digest"):
            value = getattr(self, name)
            if value and not _ERG_HEX64.match(str(value)):
                raise RecoveryGuardError(f"{name} must be a 64-character lowercase hex digest.")
        object.__setattr__(self, "failure_code", _erg_bounded(self.failure_code))
        ids = tuple(_erg_bounded(item) for item in (self.failure_ids or ()) if str(item or "").strip())
        object.__setattr__(self, "failure_ids", ids[:50])

    @property
    def conditions_digest(self) -> str:
        """Digest of the observable conditions, independent of what failed."""
        payload = json.dumps(
            {
                "revision": self.revision_digest,
                "environment": self.environment_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _erg_digest(payload)

    @property
    def evidence_key(self) -> str:
        """Digest of the whole observation; equal keys mean nothing changed."""
        payload = json.dumps(
            {
                "conditions": self.conditions_digest,
                "transcript": self.transcript_sha256,
                "fingerprint": self.failure_fingerprint,
                "code": self.failure_code,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return _erg_digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_digest": self.revision_digest,
            "transcript_sha256": self.transcript_sha256,
            "failure_fingerprint": self.failure_fingerprint,
            "failure_code": self.failure_code,
            "failure_ids": list(self.failure_ids),
            "environment_digest": self.environment_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvidenceSnapshot:
        raw = data if isinstance(data, dict) else {}
        ids = raw.get("failure_ids") or ()
        if isinstance(ids, str) or not isinstance(ids, (list, tuple)):
            ids = ()
        return cls(
            revision_digest=str(raw.get("revision_digest", "")),
            transcript_sha256=str(raw.get("transcript_sha256", "")),
            failure_fingerprint=str(raw.get("failure_fingerprint", "")),
            failure_code=str(raw.get("failure_code", "")),
            failure_ids=tuple(str(item) for item in ids),
            environment_digest=str(raw.get("environment_digest", "")),
        )


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One durable recovery-memory entry."""

    sequence: int
    kind: str
    outcome: str
    attempted_at: float
    evidence: EvidenceSnapshot

    def __post_init__(self) -> None:
        if self.kind not in ATTEMPT_KINDS:
            raise RecoveryGuardError(f"unknown attempt kind: {self.kind!r}")
        if self.outcome not in ATTEMPT_OUTCOMES:
            raise RecoveryGuardError(f"unknown attempt outcome: {self.outcome!r}")
        _erg_non_neg_int(self.sequence, "sequence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "outcome": self.outcome,
            "attempted_at": round(float(self.attempted_at), 6),
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AttemptRecord:
        raw = data if isinstance(data, dict) else {}
        return cls(
            sequence=_erg_non_neg_int(raw.get("sequence", 0), "sequence"),
            kind=str(raw.get("kind", "")),
            outcome=str(raw.get("outcome", "")),
            attempted_at=float(raw.get("attempted_at", 0.0) or 0.0),
            evidence=EvidenceSnapshot.from_dict(raw.get("evidence") if isinstance(raw.get("evidence"), dict) else None),
        )


@dataclass(frozen=True, slots=True)
class StagnationVerdict:
    """Classification of one attempt against the recorded failure memory."""

    state: str
    repeat_count: int = 0
    escalate: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "repeat_count": self.repeat_count,
            "escalate": self.escalate,
            "reason": self.reason,
        }


def failure_fingerprint(failure_code: str, failure_ids: Sequence[str]) -> str:
    """Canonical digest of *what* failed, independent of ordering."""
    cleaned = sorted({_erg_bounded(item) for item in (failure_ids or ()) if str(item or "").strip()})
    payload = json.dumps(
        {"code": _erg_bounded(failure_code), "failures": cleaned},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _erg_digest(payload)


def evidence_delta(previous: EvidenceSnapshot, candidate: EvidenceSnapshot) -> dict[str, Any]:
    """Describe what changed between two observations (never a bare boolean)."""
    before = set(previous.failure_ids)
    after = set(candidate.failure_ids)
    return {
        "revision_changed": previous.revision_digest != candidate.revision_digest,
        "transcript_changed": previous.transcript_sha256 != candidate.transcript_sha256,
        "conditions_changed": previous.conditions_digest != candidate.conditions_digest,
        "failure_code_changed": previous.failure_code != candidate.failure_code,
        "failures_added": sorted(after - before),
        "failures_removed": sorted(before - after),
    }


def detect_stagnation(
    history: Sequence[AttemptRecord],
    candidate: EvidenceSnapshot,
    *,
    limits: RecoveryLimits | None = None,
    compare: str = "evidence",
) -> StagnationVerdict:
    """Classify ``candidate`` against the failed attempts already recorded.

    Only failed attempts are memory: a verified attempt is progress by
    definition. An attempt whose key repeats the most recent failure is
    ``stagnant``; one whose key matches an *older* abandoned state is
    ``oscillating``; anything else is ``progress``.

    ``compare="evidence"`` (the default) uses the whole observation, which is
    what a fresh verification attempt is judged by. ``compare="conditions"``
    ignores the transcript and compares only the conditions a *recovery* action
    starts from, because a recovery attempt has not produced a transcript yet —
    it is allowed when the workspace or environment changed.
    """
    if compare not in {"evidence", "conditions"}:
        raise RecoveryGuardError("compare must be 'evidence' or 'conditions'.")
    bounds = limits or RecoveryLimits()
    failed = [record for record in history if record.outcome == "failed"]
    if not failed:
        return StagnationVerdict(state=STAGNATION_FIRST, repeat_count=0, reason="no recorded failure")
    key = candidate.conditions_digest if compare == "conditions" else candidate.evidence_key
    keys = [
        record.evidence.conditions_digest if compare == "conditions" else record.evidence.evidence_key
        for record in failed
    ]
    trailing = 0
    for recorded in reversed(keys):
        if recorded != key:
            break
        trailing += 1
    if trailing:
        repeat_count = trailing + 1
        if compare == "conditions":
            reason = f"identical conditions observed {repeat_count} time(s) with no new evidence"
        else:
            reason = (
                f"identical failure evidence observed {repeat_count} time(s) "
                f"(fingerprint {candidate.failure_fingerprint[:12] or 'n/a'})"
            )
        return StagnationVerdict(
            state=STAGNATION_STAGNANT,
            repeat_count=repeat_count,
            escalate=repeat_count >= bounds.max_identical_attempts,
            reason=reason,
        )
    occurrences = keys.count(key)
    if occurrences:
        return StagnationVerdict(
            state=STAGNATION_OSCILLATING,
            repeat_count=occurrences + 1,
            escalate=True,
            reason=(
                "a previously abandoned state was revisited (patch oscillation; "
                f"{occurrences} earlier occurrence(s))"
            ),
        )
    return StagnationVerdict(state=STAGNATION_PROGRESS, repeat_count=0, reason="evidence changed")


@dataclass(frozen=True, slots=True)
class RecoveryBudgetState:
    """Persisted retry / replan / rollback counters."""

    limits: RecoveryLimits
    retries_used: int = 0
    replans_used: int = 0
    rollbacks_used: int = 0
    pause_requested: bool = False
    pause_reason: str = ""

    def used(self, kind: str) -> int:
        if kind == "retry":
            return self.retries_used
        if kind == "replan":
            return self.replans_used
        if kind == "rollback":
            return self.rollbacks_used
        raise RecoveryGuardError(f"unknown budget kind: {kind!r}")

    def limit(self, kind: str) -> int:
        if kind == "retry":
            return self.limits.max_retries
        if kind == "replan":
            return self.limits.max_replans
        if kind == "rollback":
            return self.limits.max_rollbacks
        raise RecoveryGuardError(f"unknown budget kind: {kind!r}")

    def violation_reason(self, kind: str | None = None) -> str:
        kinds = (kind,) if kind else BUDGET_KINDS
        for candidate in kinds:
            used, limit = self.used(candidate), self.limit(candidate)
            if used >= limit:
                return f"{candidate} budget exhausted ({used}/{limit})"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": self.limits.to_dict(),
            "retries_used": self.retries_used,
            "replans_used": self.replans_used,
            "rollbacks_used": self.rollbacks_used,
            "pause_requested": self.pause_requested,
            "pause_reason": self.pause_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RecoveryBudgetState:
        raw = data if isinstance(data, dict) else {}
        return cls(
            limits=RecoveryLimits.from_dict(raw.get("limits") if isinstance(raw.get("limits"), dict) else None),
            retries_used=_erg_non_neg_int(raw.get("retries_used", 0), "retries_used"),
            replans_used=_erg_non_neg_int(raw.get("replans_used", 0), "replans_used"),
            rollbacks_used=_erg_non_neg_int(raw.get("rollbacks_used", 0), "rollbacks_used"),
            pause_requested=bool(raw.get("pause_requested", False)),
            pause_reason=_erg_bounded(raw.get("pause_reason", "")),
        )


class RecoveryGuard:
    """Durable per-task failure memory and recovery budgets.

    One atomically replaced JSON document per task at
    ``<store>/recovery/<task_id>.json`` (directory ``0700``, file ``0600``).
    Attempts are appended and counters incremented in the same write, so a crash
    cannot leave a consumed budget without the attempt that spent it (or the
    reverse), and a fresh process reads the same memory back.
    """

    def __init__(
        self,
        store_root: Path | str,
        *,
        limits: RecoveryLimits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(store_root)
        self._default_limits = limits or RecoveryLimits()
        self._clock = clock

    # -- paths -------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._root / "recovery"

    def _path(self, task_id: str) -> Path:
        return self.directory / f"{_erg_safe_task_id(task_id)}.json"

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RecoveryGuardError(f"recovery store {self.directory} is not a real directory.")
        with_os = getattr(os, "chmod", None)
        if with_os is not None:
            with_os(self.directory, 0o700)

    # -- documents ---------------------------------------------------------

    def _read_document(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return self._empty_document(task_id)
        if stat.S_ISLNK(info.st_mode):
            raise RecoveryGuardError(f"recovery record {path} must not be a symlink.")
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryGuardError(f"recovery record {path} is not a regular file.")
        if info.st_size > MAX_DOCUMENT_BYTES:
            raise RecoveryGuardError(f"recovery record {path} exceeds {MAX_DOCUMENT_BYTES} bytes.")
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryGuardError(f"recovery record {path} is unreadable: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RecoveryGuardError(f"recovery record {path} must be a JSON object.")
        if int(parsed.get("schema_version", 0)) != RECOVERY_SCHEMA_VERSION:
            raise RecoveryGuardError(f"recovery record {path} has an unsupported schema version.")
        if str(parsed.get("task_id", task_id)) != _erg_safe_task_id(task_id):
            raise RecoveryGuardError(f"recovery record {path} belongs to another task.")
        raw_attempts = parsed.get("attempts") or []
        if not isinstance(raw_attempts, list):
            raise RecoveryGuardError(f"recovery record {path} has a malformed attempts list.")
        document = self._empty_document(task_id)
        document["budget"] = RecoveryBudgetState.from_dict(
            parsed.get("budget") if isinstance(parsed.get("budget"), dict) else None
        ).to_dict()
        document["attempts"] = [
            AttemptRecord.from_dict(entry).to_dict() for entry in raw_attempts if isinstance(entry, dict)
        ]
        document["updated_at"] = float(parsed.get("updated_at", 0.0) or 0.0)
        return document

    def _empty_document(self, task_id: str) -> dict[str, Any]:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "task_id": _erg_safe_task_id(task_id),
            "budget": RecoveryBudgetState(limits=self._default_limits).to_dict(),
            "attempts": [],
            "updated_at": 0.0,
        }

    def _write_document(self, task_id: str, document: dict[str, Any]) -> None:
        self._ensure_directory()
        path = self._path(task_id)
        document["schema_version"] = RECOVERY_SCHEMA_VERSION
        document["task_id"] = _erg_safe_task_id(task_id)
        document["updated_at"] = round(float(self._clock()), 6)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)

    # -- read API ----------------------------------------------------------

    def history(self, task_id: str) -> tuple[AttemptRecord, ...]:
        return tuple(AttemptRecord.from_dict(entry) for entry in self._read_document(task_id)["attempts"])

    def budget(self, task_id: str) -> RecoveryBudgetState:
        return RecoveryBudgetState.from_dict(self._read_document(task_id)["budget"])

    def summary(self, task_id: str, *, limit: int = 10) -> dict[str, Any]:
        """Read-only view for the task-state API: counters, pause flag, recent attempts."""
        document = self._read_document(task_id)
        attempts = [AttemptRecord.from_dict(entry) for entry in document["attempts"]]
        budget = RecoveryBudgetState.from_dict(document["budget"])
        recent = attempts[-max(1, min(int(limit), MAX_ATTEMPTS)) :]
        failures = [record for record in attempts if record.outcome == "failed"]
        verdict = (
            detect_stagnation(failures[:-1], failures[-1].evidence, limits=budget.limits)
            if failures
            else StagnationVerdict(state=STAGNATION_FIRST, reason="no recorded failure")
        )
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "task_id": _erg_safe_task_id(task_id),
            "budget": budget.to_dict(),
            "pause_requested": budget.pause_requested,
            "pause_reason": budget.pause_reason,
            "attempt_count": len(attempts),
            "failure_count": len(failures),
            "latest_verdict": verdict.to_dict(),
            "recent_attempts": [record.to_dict() for record in recent],
            "updated_at": document["updated_at"],
        }

    # -- write API ---------------------------------------------------------

    def _load(self, task_id: str) -> tuple[dict[str, Any], list[AttemptRecord], RecoveryBudgetState]:
        document = self._read_document(task_id)
        attempts = [AttemptRecord.from_dict(entry) for entry in document["attempts"]]
        budget = RecoveryBudgetState.from_dict(document["budget"])
        return document, attempts, budget

    def _save(
        self,
        task_id: str,
        document: dict[str, Any],
        attempts: Sequence[AttemptRecord],
        budget: RecoveryBudgetState,
    ) -> None:
        document["attempts"] = [record.to_dict() for record in attempts[-MAX_ATTEMPTS:]]
        document["budget"] = budget.to_dict()
        self._write_document(task_id, document)

    def record_attempt(
        self,
        task_id: str,
        *,
        kind: str,
        outcome: str,
        evidence: EvidenceSnapshot,
        now: float | None = None,
    ) -> tuple[AttemptRecord, StagnationVerdict]:
        """Append one attempt durably and return its verdict against prior failures."""
        document, attempts, budget = self._load(task_id)
        verdict = detect_stagnation(attempts, evidence, limits=budget.limits)
        sequence = (attempts[-1].sequence + 1) if attempts else 1
        record = AttemptRecord(
            sequence=sequence,
            kind=kind,
            outcome=outcome,
            attempted_at=float(self._clock() if now is None else now),
            evidence=evidence,
        )
        if outcome == "failed" and verdict.escalate and not budget.pause_requested:
            budget = RecoveryBudgetState(
                limits=budget.limits,
                retries_used=budget.retries_used,
                replans_used=budget.replans_used,
                rollbacks_used=budget.rollbacks_used,
                pause_requested=True,
                pause_reason=verdict.reason,
            )
        self._save(task_id, document, list(attempts) + [record], budget)
        return record, verdict

    def consume(self, task_id: str, kind: str, *, now: float | None = None) -> RecoveryBudgetState:
        """Atomically consume one retry/replan/rollback; persist exhaustion before raising."""
        if kind not in BUDGET_KINDS:
            raise RecoveryGuardError(f"unknown budget kind: {kind!r}")
        document, attempts, budget = self._load(task_id)
        used, limit = budget.used(kind), budget.limit(kind)
        if used >= limit:
            exhausted = RecoveryBudgetState(
                limits=budget.limits,
                retries_used=budget.retries_used,
                replans_used=budget.replans_used,
                rollbacks_used=budget.rollbacks_used,
                pause_requested=True,
                pause_reason=budget.pause_reason or f"{kind} budget exhausted ({used}/{limit})",
            )
            self._save(task_id, document, attempts, exhausted)
            raise RecoveryRejected(f"{kind} budget exhausted ({used}/{limit}); task paused for review.")
        updated = RecoveryBudgetState(
            limits=budget.limits,
            retries_used=budget.retries_used + (1 if kind == "retry" else 0),
            replans_used=budget.replans_used + (1 if kind == "replan" else 0),
            rollbacks_used=budget.rollbacks_used + (1 if kind == "rollback" else 0),
            pause_requested=budget.pause_requested,
            pause_reason=budget.pause_reason,
        )
        self._save(task_id, document, attempts, updated)
        return updated

    def require_progress(
        self,
        task_id: str,
        evidence: EvidenceSnapshot,
        *,
        action: str = "recovery",
        compare: str = "evidence",
    ) -> StagnationVerdict:
        """Refuse ``action`` unless its evidence differs from the recorded failure.

        This is the acceptance-criterion gate: a recovery action needs changed
        evidence or changed conditions. Identical or revisited evidence raises
        :class:`RecoveryRejected` and requests a safe pause; nothing is mutated on
        behalf of the caller.
        """
        document, attempts, budget = self._load(task_id)
        verdict = detect_stagnation(attempts, evidence, limits=budget.limits, compare=compare)
        if verdict.state in (STAGNATION_STAGNANT, STAGNATION_OSCILLATING):
            paused = RecoveryBudgetState(
                limits=budget.limits,
                retries_used=budget.retries_used,
                replans_used=budget.replans_used,
                rollbacks_used=budget.rollbacks_used,
                pause_requested=True,
                pause_reason=budget.pause_reason or verdict.reason,
            )
            self._save(task_id, document, attempts, paused)
            raise RecoveryRejected(
                f"{action} refused: {verdict.reason}. Changed evidence or conditions are required "
                "before another attempt; the task is paused for review."
            )
        return verdict

    def clear(self, task_id: str) -> None:
        """Remove one task's memory (task deletion / explicit reset)."""
        self._path(task_id).unlink(missing_ok=True)
