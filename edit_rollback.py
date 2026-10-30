"""Multi-file rollback and action reconciliation for managed edits (M5-P2-03).

`M5-P2-02` records every applied edit as a durable, version-checked transaction.
That answers "what did this one edit do". It does not answer the question a
half-finished multi-file change raises: *the runtime wrote three of five files
and then stopped — how do we get back to a consistent state without `git reset
--hard`?*

This module answers it with three pieces:

1. **A durable batch journal.** A batch groups the managed edits applied to one
   workspace under one session/task run. Each step records the path, the tool,
   the digest *and bytes* of the file before the edit (absent for a create), the
   digest and bytes it wrote, the linked `M5-P2-02` transaction id, and an
   explicit state. Steps are appended and the batch document is replaced
   atomically, so an interruption leaves a readable record of what had been
   applied.

2. **An inverse patch with version checks.** Rolling a step back restores the
   bytes the edit replaced — never a blanket checkout. If the file still holds
   exactly what the edit wrote, the restore is a compare-and-swap
   (``expected_sha256`` = the digest this batch wrote). If the user has changed
   the file since, the inverse of ``post -> pre`` is applied only where the
   regions this batch wrote are still intact, so unrelated user edits survive.
   When a written region was also changed by the user, the step is a **conflict**
   and nothing is written.

3. **Reconciliation before rollback.** A batch is only rolled back when the
   caller has established that no managed-edit action is in an ``uncertain`` or
   ``in_flight`` state for the same task: an unknown outcome means we cannot know
   which bytes the runtime may still write. Anything ambiguous leaves the batch
   in an explicit ``unresolved`` state with per-path reasons, which an operator or
   a later turn can act on. Recovery never guesses and never silently drops work.

The module is standard-library only, does no I/O outside the store directory and
workspace it is given, and never performs a destructive Git operation.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

import workspace_snapshots

ROLLBACK_SCHEMA_VERSION = 1

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100

BATCH_STATES = frozenset({"open", "closed", "rolled_back", "partially_rolled_back", "unresolved"})
STEP_STATES = frozenset({"applied", "rolled_back", "unresolved", "skipped"})

#: Per-step rollback decisions produced by the planner.
DECISION_REVERT = "revert"
DECISION_REVERT_MERGED = "revert_merged"
DECISION_DELETE = "delete_created"
DECISION_ALREADY_REVERTED = "already_reverted"
DECISION_CONFLICT = "conflict"

_ERB_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EditRollbackError(RuntimeError):
    """Raised when a batch cannot be addressed, recorded, or rolled back safely."""


class EditBatchNotFound(EditRollbackError):
    """Raised when no batch with that id exists."""


def _erb_now() -> float:
    return round(time.time(), 6)


def _safe_id(value: str, label: str) -> str:
    text = (value or "").strip()
    if not _SAFE_ID_RE.match(text):
        raise EditRollbackError(f"Unsafe {label}: {value!r}")
    return text


def _sha(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not _ERB_SHA256_RE.match(text):
        raise EditRollbackError(f"{label} must be a 64-character lowercase SHA-256 digest: {value!r}")
    return text


@dataclass(frozen=True, slots=True)
class EditStep:
    """One managed edit inside a batch, with the bytes needed to undo it."""

    rel_path: str
    tool: str
    pre_sha256: str | None
    post_sha256: str
    pre_blob: str | None
    post_blob: str | None = None
    pre_mode: int | None = None
    edit_id: str = ""
    state: str = "applied"
    recorded_at: float = 0.0
    note: str = ""

    @property
    def created(self) -> bool:
        return self.pre_sha256 is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "tool": self.tool,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "pre_blob": self.pre_blob,
            "post_blob": self.post_blob,
            "pre_mode": self.pre_mode,
            "edit_id": self.edit_id,
            "state": self.state,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EditStep":
        return cls(
            rel_path=str(raw["rel_path"]),
            tool=str(raw.get("tool") or ""),
            pre_sha256=str(raw["pre_sha256"]) if raw.get("pre_sha256") else None,
            post_sha256=str(raw["post_sha256"]),
            pre_blob=str(raw["pre_blob"]) if raw.get("pre_blob") else None,
            post_blob=str(raw["post_blob"]) if raw.get("post_blob") else None,
            pre_mode=int(raw["pre_mode"]) if raw.get("pre_mode") is not None else None,
            edit_id=str(raw.get("edit_id") or ""),
            state=str(raw.get("state") or "applied"),
            recorded_at=float(raw.get("recorded_at") or 0.0),
            note=str(raw.get("note") or ""),
        )


@dataclass(slots=True)
class EditBatch:
    """A durable, ordered set of managed edits that can be rolled back together."""

    batch_id: str
    session_id: str
    workspace_root: str
    task_id: str = ""
    label: str = ""
    state: str = "open"
    created_at: float = 0.0
    updated_at: float = 0.0
    steps: list[EditStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def applied_steps(self) -> list[EditStep]:
        return [step for step in self.steps if step.state == "applied"]

    def step(self, rel_path: str) -> EditStep | None:
        for step in reversed(self.steps):
            if step.rel_path == rel_path:
                return step
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ROLLBACK_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "task_id": self.task_id,
            "label": self.label,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [step.to_dict() for step in self.steps],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EditBatch":
        state = str(raw.get("state") or "open")
        if state not in BATCH_STATES:
            raise EditRollbackError(f"Unknown batch state in stored record: {state!r}")
        steps = [EditStep.from_dict(item) for item in raw.get("steps") or ()]
        for step in steps:
            if step.state not in STEP_STATES:
                raise EditRollbackError(f"Unknown step state in stored record: {step.state!r}")
        return cls(
            batch_id=str(raw["batch_id"]),
            session_id=str(raw["session_id"]),
            workspace_root=str(raw.get("workspace_root") or ""),
            task_id=str(raw.get("task_id") or ""),
            label=str(raw.get("label") or ""),
            state=state,
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            steps=steps,
            notes=[str(note) for note in raw.get("notes") or ()],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "label": self.label,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": len(self.steps),
            "applied": len(self.applied_steps),
            "paths": [step.rel_path for step in self.steps],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class StepDecision:
    """What rolling one step back would do, and why."""

    rel_path: str
    decision: str
    reason: str = ""
    current_sha256: str | None = None
    expected_pre_sha256: str | None = None
    expected_post_sha256: str | None = None
    merged_text: str | None = None

    @property
    def safe(self) -> bool:
        return self.decision != DECISION_CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "decision": self.decision,
            "reason": self.reason,
            "current_sha256": self.current_sha256,
            "expected_pre_sha256": self.expected_pre_sha256,
            "expected_post_sha256": self.expected_post_sha256,
        }


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """The ordered plan for undoing a batch, newest edit first."""

    batch_id: str
    decisions: tuple[StepDecision, ...]
    blocked_reasons: tuple[str, ...] = ()

    @property
    def conflicts(self) -> tuple[StepDecision, ...]:
        return tuple(decision for decision in self.decisions if not decision.safe)

    @property
    def safe(self) -> bool:
        return not self.conflicts and not self.blocked_reasons

    @property
    def needs_work(self) -> tuple[StepDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.decision != DECISION_ALREADY_REVERTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "safe": self.safe,
            "steps": [decision.to_dict() for decision in self.decisions],
            "conflicts": [decision.to_dict() for decision in self.conflicts],
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """Outcome of a rollback attempt, always with an explicit state."""

    batch_id: str
    state: str
    dry_run: bool
    plan: RollbackPlan
    applied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state in {"rolled_back", "closed"} or (self.dry_run and self.plan.safe)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "state": self.state,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "applied": list(self.applied),
            "failed": list(self.failed),
            "reasons": list(self.reasons),
            "plan": self.plan.to_dict(),
        }


class EditBatchJournal:
    """Durable store of edit batches: one atomically replaced JSON document each."""

    def __init__(self, store_root: Path) -> None:
        self.store_root = Path(store_root)

    # -- paths -----------------------------------------------------------

    def batches_dir(self) -> Path:
        return self.store_root / "edits" / "batches"

    def batch_path(self, batch_id: str) -> Path:
        return self.batches_dir() / f"{_safe_id(batch_id, 'batch id')}.json"

    def open_pointer_path(self, session_id: str) -> Path:
        return self.store_root / "edits" / "open" / f"{_safe_id(session_id, 'session id')}.json"

    # -- persistence -----------------------------------------------------

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            os.write(fd, (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def save(self, batch: EditBatch) -> EditBatch:
        batch.updated_at = _erb_now()
        self._write_json(self.batch_path(batch.batch_id), batch.to_dict())
        return batch

    def load(self, batch_id: str) -> EditBatch:
        path = self.batch_path(batch_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EditBatchNotFound(f"No edit batch {batch_id!r} in {path.parent}.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EditRollbackError(f"Edit batch {batch_id!r} is unreadable: {exc}") from exc
        try:
            return EditBatch.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise EditRollbackError(f"Edit batch {batch_id!r} is malformed: {exc}") from exc

    def list_batches(self, session_id: str, *, limit: int = DEFAULT_LIST_LIMIT) -> list[EditBatch]:
        directory = self.batches_dir()
        if not directory.is_dir():
            return []
        batches: list[EditBatch] = []
        for path in directory.glob("*.json"):
            try:
                batch = self.load(path.stem)
            except EditRollbackError:
                continue
            if batch.session_id == session_id:
                batches.append(batch)
        batches.sort(key=lambda batch: (batch.created_at, batch.batch_id), reverse=True)
        return batches[: max(1, min(int(limit), MAX_LIST_LIMIT))]

    # -- open batch pointer ---------------------------------------------

    def current_batch(self, session_id: str) -> EditBatch | None:
        """The session's open batch, if the pointer still resolves to one."""
        try:
            raw = json.loads(self.open_pointer_path(session_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        batch_id = str(raw.get("batch_id") or "")
        if not batch_id:
            return None
        try:
            batch = self.load(batch_id)
        except EditRollbackError:
            return None
        return batch if batch.state == "open" else None

    def _set_pointer(self, session_id: str, batch_id: str | None) -> None:
        path = self.open_pointer_path(session_id)
        if batch_id is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        self._write_json(path, {"batch_id": batch_id, "updated_at": _erb_now()})

    def open_batch(
        self,
        session_id: str,
        workspace_root: Path,
        *,
        task_id: str = "",
        label: str = "",
    ) -> EditBatch:
        """Return the session's open batch, starting a new one when there is none."""
        existing = self.current_batch(session_id)
        if existing is not None:
            return existing
        batch = EditBatch(
            batch_id=f"eb-{uuid.uuid4().hex[:16]}",
            session_id=_safe_id(session_id, "session id"),
            workspace_root=str(Path(workspace_root).resolve()),
            task_id=str(task_id or "")[:128],
            label=str(label or "")[:120],
            state="open",
            created_at=_erb_now(),
            updated_at=_erb_now(),
        )
        self.save(batch)
        self._set_pointer(batch.session_id, batch.batch_id)
        return batch

    def close_batch(self, batch_id: str, *, state: str = "closed", note: str = "") -> EditBatch:
        if state not in BATCH_STATES:
            raise EditRollbackError(f"Unknown batch state: {state!r}")
        batch = self.load(batch_id)
        batch.state = state
        if note:
            batch.notes.append(note)
        self.save(batch)
        if batch.state != "open":
            self._set_pointer(batch.session_id, None)
        return batch

    def record_step(
        self,
        batch_id: str,
        *,
        rel_path: str,
        tool: str,
        pre_sha256: str | None,
        post_sha256: str,
        pre_blob: str | None,
        post_blob: str | None = None,
        pre_mode: int | None = None,
        edit_id: str = "",
    ) -> tuple[EditBatch, EditStep]:
        """Append (or refresh) one file's progress in the batch, durably."""
        batch = self.load(batch_id)
        if batch.state != "open":
            raise EditRollbackError(f"Edit batch {batch_id!r} is {batch.state!r} and cannot record new steps.")
        pre = _sha(pre_sha256, "pre_sha256")
        post = _sha(post_sha256, "post_sha256")
        if post is None:
            raise EditRollbackError("A batch step requires the digest of the bytes that were written.")
        pre_digest = _sha(pre_blob, "pre_blob")
        post_digest = _sha(post_blob, "post_blob")
        existing = batch.step(rel_path)
        if existing is not None and existing.state == "applied":
            # The same path was edited again inside this batch. A rollback must
            # restore the *first* pre-image, so keep it and advance the written side.
            updated = EditStep(
                rel_path=existing.rel_path,
                tool=str(tool or existing.tool)[:40],
                pre_sha256=existing.pre_sha256,
                post_sha256=post,
                pre_blob=existing.pre_blob,
                post_blob=post_digest or existing.post_blob,
                pre_mode=existing.pre_mode,
                edit_id=str(edit_id or existing.edit_id)[:64],
                state="applied",
                recorded_at=existing.recorded_at,
                note="edited again inside the same batch",
            )
            batch.steps = [updated if item.rel_path == rel_path and item.state == "applied" else item for item in batch.steps]
            self.save(batch)
            return batch, updated
        step = EditStep(
            rel_path=rel_path,
            tool=str(tool or "")[:40],
            pre_sha256=pre,
            post_sha256=post,
            pre_blob=pre_digest,
            post_blob=post_digest,
            pre_mode=int(pre_mode) if pre_mode is not None else None,
            edit_id=str(edit_id or "")[:64],
            state="applied",
            recorded_at=_erb_now(),
        )
        batch.steps.append(step)
        self.save(batch)
        return batch, step


    def mark_step(self, batch_id: str, rel_path: str, *, state: str, note: str = "") -> EditBatch:
        """Set the state of the batch's most recent step for ``rel_path``, durably."""
        if state not in STEP_STATES:
            raise EditRollbackError(f"Unknown step state: {state!r}")
        batch = self.load(batch_id)
        target: EditStep | None = None
        for step in reversed(batch.steps):
            if step.rel_path == rel_path:
                target = step
                break
        if target is None:
            raise EditRollbackError(f"Edit batch {batch_id!r} has no step for {rel_path!r}.")
        batch.steps = [replace(step, state=state, note=note) if step is target else step for step in batch.steps]
        return self.save(batch)


def _line_changes(base: list[str], other: list[str]) -> list[tuple[int, int, list[str]]]:
    """Line hunks turning ``base`` into ``other``: ``(base_start, base_end, lines)``."""
    changes: list[tuple[int, int, list[str]]] = []
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changes.append((i1, i2, other[j1:j2]))
    return changes


def inverse_merge(current_text: str, pre_text: str, post_text: str) -> tuple[str | None, str]:
    """Three-way merge of the rollback (``post -> pre``) with the user's later edits.

    ``post`` is the common base: the hunks that turn it back into ``pre`` are applied
    onto ``current`` alongside whatever the user changed since. Hunks that touch
    disjoint lines both apply, so unrelated user edits survive a rollback. When a
    hunk and a user edit touch the same base lines and disagree, the merge is refused
    (``(None, reason)``) instead of guessing — the caller must not overwrite.
    """
    post = post_text.splitlines(keepends=True)
    pre = pre_text.splitlines(keepends=True)
    current = current_text.splitlines(keepends=True)
    user_changes = _line_changes(post, current)
    revert_changes = _line_changes(post, pre)
    merged: list[str] = []
    pos = 0
    ui = ti = 0
    while ui < len(user_changes) or ti < len(revert_changes):
        user = user_changes[ui] if ui < len(user_changes) else None
        revert = revert_changes[ti] if ti < len(revert_changes) else None
        if revert is not None and user is not None:
            overlaps = user[0] < revert[1] and revert[0] < user[1]
            if overlaps:
                if user[2] == revert[2]:
                    start, stop = min(user[0], revert[0]), max(user[1], revert[1])
                    merged.extend(post[pos:start])
                    merged.extend(revert[2])
                    pos = stop
                    ui += 1
                    ti += 1
                    continue
                return None, "a region this batch wrote has changed since the edit, so reverting it would discard that change"
        take_user = revert is None or (user is not None and user[0] <= revert[0])
        change = user if take_user else revert
        assert change is not None  # narrowed by the branch above
        start, stop, lines = change
        if start < pos:  # pragma: no cover - opcodes are ordered; guard against surprises
            return None, "overlapping edits could not be ordered for a safe merge"
        merged.extend(post[pos:start])
        merged.extend(lines)
        pos = stop
        if take_user:
            ui += 1
        else:
            ti += 1
    merged.extend(post[pos:])
    return "".join(merged), ""


class EditRollbackEngine:
    """Plan and apply the inverse of an edit batch, refusing anything unsafe."""

    def __init__(
        self,
        journal: EditBatchJournal,
        cas: workspace_snapshots.ContentAddressedBlobStore,
        *,
        min_free_bytes: int = workspace_snapshots.DEFAULT_MIN_FREE_BYTES,
    ) -> None:
        self.journal = journal
        self.cas = cas
        self.min_free_bytes = min_free_bytes

    # -- planning --------------------------------------------------------

    def plan(self, batch_id: str, workspace: Path, *, blocked_reasons: Sequence[str] = ()) -> RollbackPlan:
        """Decide, per path, whether the batch can be undone without losing user work."""
        batch = self.journal.load(batch_id)
        decisions: list[StepDecision] = []
        for step in reversed(batch.steps):
            if step.state != "applied":
                continue
            decisions.append(self._decide(step, workspace))
        return RollbackPlan(batch_id=batch.batch_id, decisions=tuple(decisions), blocked_reasons=tuple(blocked_reasons))

    def _decide(self, step: EditStep, workspace: Path) -> StepDecision:
        try:
            current = workspace_snapshots.hash_target_in_place(workspace, step.rel_path)
        except (workspace_snapshots.WorkspaceSnapshotError, OSError) as exc:
            return StepDecision(
                rel_path=step.rel_path,
                decision=DECISION_CONFLICT,
                reason=f"the path cannot be inspected safely: {exc}",
                expected_pre_sha256=step.pre_sha256,
                expected_post_sha256=step.post_sha256,
            )
        current_sha = current[0] if current is not None else None
        base = {
            "rel_path": step.rel_path,
            "current_sha256": current_sha,
            "expected_pre_sha256": step.pre_sha256,
            "expected_post_sha256": step.post_sha256,
        }
        if current_sha == step.pre_sha256:
            return StepDecision(
                decision=DECISION_ALREADY_REVERTED,
                reason="the file already holds the bytes this batch replaced",
                **base,
            )
        if current_sha == step.post_sha256:
            if step.created:
                return StepDecision(
                    decision=DECISION_DELETE,
                    reason="the file still holds exactly what this batch created",
                    **base,
                )
            return StepDecision(
                decision=DECISION_REVERT,
                reason="the file still holds exactly what this batch wrote",
                **base,
            )
        if step.created:
            return StepDecision(
                decision=DECISION_CONFLICT,
                reason="this batch created the file and it has been modified since",
                **base,
            )
        if current_sha is None:
            return StepDecision(
                decision=DECISION_CONFLICT,
                reason="the file was deleted after this batch wrote it",
                **base,
            )
        current_text = self._read_text(workspace, step.rel_path)
        pre_text = self._blob_text(step.pre_blob)
        post_text = self._blob_text(step.post_blob)
        if current_text is None or pre_text is None or post_text is None:
            return StepDecision(
                decision=DECISION_CONFLICT,
                reason="the pre- or post-edit bytes are unavailable for a three-way merge",
                **base,
            )
        merged, reason = inverse_merge(current_text, pre_text, post_text)
        if merged is None:
            return StepDecision(decision=DECISION_CONFLICT, reason=reason, **base)
        return StepDecision(
            decision=DECISION_REVERT_MERGED,
            reason="the inverse patch applies around the user's later edits",
            merged_text=merged,
            **base,
        )

    # -- applying --------------------------------------------------------

    def execute(
        self,
        batch_id: str,
        workspace: Path,
        *,
        dry_run: bool = False,
        blocked_reasons: Sequence[str] = (),
    ) -> RollbackResult:
        """Undo a batch, or explain precisely why that is unsafe.

        A conflicting or blocked plan performs **no writes** and leaves the batch
        ``unresolved`` with per-path reasons. Applying is per step and durable, so
        an interruption mid-rollback is resumable.
        """
        workspace = Path(workspace).resolve(strict=True)
        plan = self.plan(batch_id, workspace, blocked_reasons=blocked_reasons)
        batch = self.journal.load(batch_id)
        if dry_run:
            return RollbackResult(batch_id=batch.batch_id, state=batch.state, dry_run=True, plan=plan)

        if plan.blocked_reasons or plan.conflicts:
            reasons = list(plan.blocked_reasons) + [f"{d.rel_path}: {d.reason}" for d in plan.conflicts]
            if not plan.decisions and not plan.blocked_reasons:
                reasons = ["the batch has no applied edits to roll back"]
            batch.state = "unresolved"
            batch.notes.extend(f"rollback refused: {reason}" for reason in reasons)
            self.journal.save(batch)
            return RollbackResult(
                batch_id=batch.batch_id,
                state=batch.state,
                dry_run=False,
                plan=plan,
                reasons=tuple(reasons),
            )

        applied: list[str] = []
        steps_by_path = {step.rel_path: step for step in batch.steps if step.state == "applied"}
        for decision in plan.decisions:
            if decision.decision == DECISION_ALREADY_REVERTED:
                self._mark_step(batch, decision, "rolled_back", "already at the pre-edit bytes")
                applied.append(decision.rel_path)
                continue
            step = steps_by_path.get(decision.rel_path)
            try:
                if step is None:
                    raise EditRollbackError(f"no applied batch step for {decision.rel_path!r}")
                self._apply_decision(decision, workspace, step)
            except (workspace_snapshots.SnapshotConflictError, workspace_snapshots.WorkspaceSnapshotError, OSError) as exc:
                self._mark_step(batch, decision, "unresolved", f"rollback failed: {exc}")
                batch.state = "partially_rolled_back"
                batch.notes.append(f"rollback stopped at {decision.rel_path}: {exc}")
                self.journal.save(batch)
                return RollbackResult(
                    batch_id=batch.batch_id,
                    state=batch.state,
                    dry_run=False,
                    plan=plan,
                    applied=tuple(applied),
                    failed=(decision.rel_path,),
                    reasons=(str(exc),),
                )
            self._mark_step(batch, decision, "rolled_back", decision.reason)
            applied.append(decision.rel_path)

        batch.state = "rolled_back"
        self.journal.save(batch)
        self.journal._set_pointer(batch.session_id, None)
        return RollbackResult(
            batch_id=batch.batch_id,
            state=batch.state,
            dry_run=False,
            plan=plan,
            applied=tuple(applied),
        )

    def _mark_step(self, batch: EditBatch, decision: StepDecision, state: str, note: str) -> None:
        batch.steps = [
            EditStep(
                rel_path=step.rel_path,
                tool=step.tool,
                pre_sha256=step.pre_sha256,
                post_sha256=step.post_sha256,
                pre_blob=step.pre_blob,
                post_blob=step.post_blob,
                pre_mode=step.pre_mode,
                edit_id=step.edit_id,
                state=state if step.rel_path == decision.rel_path and step.state == "applied" else step.state,
                recorded_at=step.recorded_at,
                note=note if step.rel_path == decision.rel_path else step.note,
            )
            for step in batch.steps
        ]
        self.journal.save(batch)

    def _apply_decision(self, decision: StepDecision, workspace: Path, step: EditStep) -> None:
        current_sha = decision.current_sha256
        if decision.decision == DECISION_DELETE:
            # Only remove a file this batch created, and only if it is byte-identical
            # to what the batch wrote.
            removed = self._delete_created(workspace, decision.rel_path, expected_sha256=decision.expected_post_sha256)
            if not removed:
                raise workspace_snapshots.SnapshotConflictError(
                    f"{decision.rel_path!r} changed before it could be removed; nothing was deleted."
                )
            return
        if decision.decision == DECISION_REVERT:
            data = self._blob_bytes(decision.rel_path, step.pre_blob or decision.expected_pre_sha256)
            workspace_snapshots.atomic_write_confined_bytes(
                workspace,
                decision.rel_path,
                data,
                mode=step.pre_mode,
                min_free_bytes=self.min_free_bytes,
                expected_sha256=decision.expected_post_sha256,
            )
            return
        if decision.decision == DECISION_REVERT_MERGED:
            if decision.merged_text is None:
                raise workspace_snapshots.SnapshotConflictError("no merged content was planned for this step")
            workspace_snapshots.atomic_write_confined_bytes(
                workspace,
                decision.rel_path,
                decision.merged_text.encode("utf-8", errors="surrogateescape"),
                min_free_bytes=self.min_free_bytes,
                expected_sha256=current_sha,
            )
            return
        raise workspace_snapshots.SnapshotConflictError(f"unsupported rollback decision: {decision.decision!r}")

    def _delete_created(self, workspace: Path, rel_path: str, *, expected_sha256: str | None) -> bool:
        current = workspace_snapshots.hash_target_in_place(workspace, rel_path)
        if current is None or current[0] != expected_sha256:
            return False
        return workspace_snapshots.remove_confined_target(workspace, rel_path)

    def _blob_bytes(self, rel_path: str, digest: str | None) -> bytes:
        if not digest:
            raise workspace_snapshots.SnapshotConflictError(
                f"no stored pre-edit bytes for {rel_path!r}; refusing a destructive rollback"
            )
        try:
            return self.cas.get_bytes(digest)
        except (workspace_snapshots.WorkspaceSnapshotError, OSError) as exc:
            raise workspace_snapshots.SnapshotConflictError(
                f"stored pre-edit bytes for {rel_path!r} are unavailable: {exc}"
            ) from exc

    def _blob_text(self, digest: str | None) -> str | None:
        if not digest:
            return None
        try:
            data = self.cas.get_bytes(digest)
        except (workspace_snapshots.WorkspaceSnapshotError, OSError):
            return None
        return data.decode("utf-8", errors="surrogateescape")

    @staticmethod
    def _read_text(workspace: Path, rel_path: str) -> str | None:
        try:
            data = workspace_snapshots.read_confined_target(workspace, rel_path)
        except (workspace_snapshots.WorkspaceSnapshotError, OSError):
            return None
        if data is None:
            return None
        return data.decode("utf-8", errors="surrogateescape")
