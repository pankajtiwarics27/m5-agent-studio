"""Persisted workflow and durable root-task/milestone state machine (M5-P1-05)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import execution_budget
import runtime_persistence
import workflow_checkpoints

WorkflowStatus = Literal["pending", "running", "paused", "completed", "failed", "cancelled"]
_STATUSES = frozenset({"pending", "running", "paused", "completed", "failed", "cancelled"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset({"running", "cancelled"}),
    "cancelled": frozenset(),
}

# Authoritative M5-P1-05 task/milestone transition matrix.
# Note: 'paused', 'blocked', 'failed', and 'waiting_approval' CANNOT transition
# directly to 'completed' — only an active 'running' task with zero pending
# pause/cancel requests, zero incomplete milestones, and zero unresolved side
# effects may enter 'completed'.
TASK_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "paused", "blocked", "cancelled"}),
    "running": frozenset({"running", "paused", "waiting_approval", "blocked", "completed", "failed", "cancelled"}),
    "waiting_approval": frozenset({"running", "paused", "blocked", "failed", "cancelled"}),
    "paused": frozenset({"running", "blocked", "cancelled"}),
    "blocked": frozenset({"running", "paused", "failed", "cancelled"}),
    "failed": frozenset({"running", "paused", "blocked", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class WorkflowStateError(ValueError):
    """Raised when workflow state or transition input is invalid."""


class WorkflowTransitionRejected(RuntimeError):
    """Raised when a requested transition is not legal for current state."""


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    workflow_id: str
    state: WorkflowStatus
    version: int
    updated_at: str


class PersistedWorkflowState:
    """SQLite state store whose transitions are validated inside transactions."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        if "\x00" in str(self.database):
            raise WorkflowStateError("database path must not contain NUL characters.")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_state (
                    workflow_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (state IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                    CHECK (version >= 0)
                )
                """
            )

    @staticmethod
    def _workflow_id(workflow_id: str) -> str:
        if not isinstance(workflow_id, str) or not workflow_id.strip() or "\x00" in workflow_id:
            raise WorkflowStateError("workflow_id must be a non-empty string without NUL characters.")
        return workflow_id.strip()

    @staticmethod
    def _status(status: str) -> WorkflowStatus:
        if status not in _STATUSES:
            raise WorkflowStateError(f"unknown workflow state: {status}")
        return status  # type: ignore[return-value]

    @staticmethod
    def _record(row: sqlite3.Row | None) -> WorkflowRecord | None:
        if row is None:
            return None
        return WorkflowRecord(row["workflow_id"], row["state"], row["version"], row["updated_at"])

    def create(self, workflow_id: str, *, state: WorkflowStatus = "pending") -> WorkflowRecord:
        identifier = self._workflow_id(workflow_id)
        normalized = self._status(state)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO workflow_state(workflow_id, state, version) VALUES (?, ?, 0)",
                    (identifier, normalized),
                )
                row = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise WorkflowStateError("workflow already exists.") from exc
        record = self._record(row)
        assert record is not None
        return record

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        identifier = self._workflow_id(workflow_id)
        with self._connect() as connection:
            return self._record(connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone())

    def transition(self, workflow_id: str, target: WorkflowStatus, *, expected_version: int | None = None) -> WorkflowRecord:
        identifier = self._workflow_id(workflow_id)
        next_state = self._status(target)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
            current = self._record(row)
            if current is None:
                raise WorkflowStateError("workflow does not exist.")
            if expected_version is not None and current.version != expected_version:
                raise WorkflowTransitionRejected("workflow version changed; refresh before transitioning.")
            if next_state not in _TRANSITIONS[current.state]:
                raise WorkflowTransitionRejected(f"transition {current.state} -> {next_state} is not allowed.")
            connection.execute(
                "UPDATE workflow_state SET state = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ?",
                (next_state, identifier),
            )
            updated = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result


@dataclass(frozen=True, slots=True)
class TaskControlResult:
    """Outcome of a safe-boundary pause or cancel command."""

    task: runtime_persistence.TaskRecord
    budget: execution_budget.TaskBudgetState
    deferred_to_safe_boundary: bool
    unresolved_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumedTaskPlan:
    """Validated recovery plan produced before resuming an interrupted task."""

    task: runtime_persistence.TaskRecord
    budget: execution_budget.TaskBudgetState
    checkpoint: workflow_checkpoints.TaskCheckpointRecord | None
    milestones: tuple[runtime_persistence.TaskRecord, ...]
    next_step: int
    completed_milestone_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoverySweepReport:
    """Summary of incomplete transitions and staged checkpoints reconciled on recovery."""

    recovered_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    cancelled_task_ids: tuple[str, ...]
    reconciled_checkpoint_ids: tuple[str, ...]
    reconciled_milestone_ids: tuple[str, ...]


def _validate_identifier(value: str, label: str = "task_id") -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise WorkflowStateError(f"{label} must be a non-empty string without NUL characters.")
    cleaned = value.strip()
    if len(cleaned) > 128:
        raise WorkflowStateError(f"{label} exceeds maximum length 128.")
    return cleaned


def milestone_task_id(root_task_id: str, milestone_key: str) -> str:
    """Build deterministic child ``runtime_tasks.task_id`` for a milestone."""
    root = _validate_identifier(root_task_id, "root_task_id")
    key = _validate_identifier(milestone_key, "milestone_key")
    candidate = f"{root}::ms:{key}"
    if len(candidate) <= 128:
        return candidate
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{root[:96]}::ms:{digest}"


def _encode_evidence_json(
    *,
    checkpoint_id: str = "",
    evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    refs_out: list[dict[str, str]] = []
    for item in evidence_refs:
        if isinstance(item, workflow_checkpoints.EvidenceRef):
            refs_out.append(item.to_dict())
        elif isinstance(item, dict):
            refs_out.append(workflow_checkpoints.EvidenceRef.from_dict(item).to_dict())
        else:
            to_dict_fn = getattr(item, "to_dict", None)
            if callable(to_dict_fn):
                refs_out.append(workflow_checkpoints.EvidenceRef.from_dict(to_dict_fn()).to_dict())
    payload: dict[str, Any] = {}
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    if refs_out:
        payload["evidence_refs"] = refs_out
    if extra:
        payload.update(extra)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DurableTaskStateMachine:
    """Authoritative SQLite state machine for root tasks, child milestones, and checkpoints."""

    def __init__(
        self,
        storage: runtime_persistence.StorageProvider,
        db_path: Path,
        *,
        retry_policy: runtime_persistence.BusyRetryPolicy = runtime_persistence.DEFAULT_BUSY_RETRY_POLICY,
    ) -> None:
        self._storage = storage
        self._db_path = db_path
        self._retry_policy = retry_policy
        self.store = runtime_persistence.RuntimePersistenceStore(storage, db_path, retry_policy=retry_policy)
        self.budgets = execution_budget.DurableTaskBudgetManager(storage, db_path, retry_policy=retry_policy)
        self.checkpoints = workflow_checkpoints.DurableTaskCheckpointStore(
            storage,
            db_path,
            retry_policy=retry_policy,
        )

    def ensure_root_task(
        self,
        task_id: str,
        *,
        session_id: str = "",
        title: str = "",
        workspace_dir: str = "",
        policy: str = "standard",
        budget_spec: execution_budget.TaskBudgetSpec | None = None,
        override_budget_spec: bool = False,
        now: float | None = None,
    ) -> tuple[runtime_persistence.TaskRecord, execution_budget.TaskBudgetState]:
        tid = _validate_identifier(task_id, "task_id")
        self.store.ensure_task(
            tid,
            session_id=session_id or tid,
            title=title,
            state="pending",
            workspace_dir=workspace_dir,
            policy=policy,
            now=now,
        )
        budget = self.budgets.ensure_budget(
            tid,
            spec=budget_spec,
            override_spec=override_budget_spec,
            now=now,
        )
        refreshed = self.store.get_task(tid)
        assert refreshed is not None
        return refreshed, budget

    def ensure_milestone(
        self,
        root_task_id: str,
        milestone_key: str,
        *,
        title: str = "",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        root_id = _validate_identifier(root_task_id, "root_task_id")
        ms_id = milestone_task_id(root_id, milestone_key)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            parent_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (root_id,)).fetchone()
            if parent_row is None:
                raise WorkflowStateError(f"Root task {root_id!r} does not exist.")
            existing = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            if existing is not None:
                return runtime_persistence._row_to_task(existing)
            conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, parent_task_id, session_id, title, state,
                    current_epoch, state_version, workspace_dir, policy,
                    budget_json, checkpoint_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?, '{}', '', ?, ?)
                """,
                (
                    ms_id,
                    root_id,
                    str(parent_row["session_id"]),
                    str(title or milestone_key)[:240],
                    str(parent_row["workspace_dir"]),
                    str(parent_row["policy"]),
                    ts,
                    ts,
                ),
            )
            created = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            assert created is not None
            return runtime_persistence._row_to_task(created)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def list_milestones(self, root_task_id: str) -> list[runtime_persistence.TaskRecord]:
        return self.store.list_child_tasks(_validate_identifier(root_task_id, "root_task_id"))

    def transition_milestone(
        self,
        root_task_id: str,
        milestone_key: str,
        to_state: str,
        *,
        epoch: int | None = None,
        expected_state_version: int | None = None,
        reason: str = "",
        checkpoint_id: str = "",
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        root_id = _validate_identifier(root_task_id, "root_task_id")
        ms_id = milestone_task_id(root_id, milestone_key)
        if to_state not in runtime_persistence.TASK_STATES:
            raise WorkflowStateError(f"unknown milestone state: {to_state}")
        evidence_json = _encode_evidence_json(checkpoint_id=checkpoint_id, evidence_refs=evidence_refs)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            parent_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (root_id,)).fetchone()
            if parent_row is None:
                raise WorkflowStateError(f"Root task {root_id!r} does not exist.")
            if epoch is not None and int(parent_row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot transition milestone {ms_id!r} (root epoch={parent_row['current_epoch']})."
                )
            parent_budget = execution_budget.TaskBudgetState.from_json(str(parent_row["budget_json"]))
            if to_state == "completed" and (
                parent_budget.pause_requested
                or parent_budget.cancel_requested
                or str(parent_row["state"]) in {"paused", "cancelled"}
            ):
                raise WorkflowTransitionRejected(
                    f"Milestone {ms_id!r} cannot claim completion while root task pause/cancel is active."
                )
            ms_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            if ms_row is None:
                raise WorkflowStateError(f"Milestone {ms_id!r} does not exist.")
            from_state = str(ms_row["state"])
            if expected_state_version is not None and int(ms_row["state_version"]) != expected_state_version:
                raise WorkflowTransitionRejected("milestone state_version changed; refresh before transitioning.")
            if from_state == to_state and to_state in {"running", "paused", "cancelled"}:
                return runtime_persistence._row_to_task(ms_row)
            allowed = TASK_STATE_TRANSITIONS.get(from_state, frozenset())
            if to_state not in allowed:
                raise WorkflowTransitionRejected(
                    f"milestone transition {from_state} -> {to_state} is not allowed."
                )
            eff_epoch = int(parent_row["current_epoch"]) if epoch is None else int(epoch)
            new_ckpt = checkpoint_id if checkpoint_id else str(ms_row["checkpoint_ref"])
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (ms_id,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, current_epoch = ?, state_version = state_version + 1,
                    checkpoint_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (to_state, eff_epoch, new_ckpt[:256], ts, ms_id),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ms_id, next_seq, from_state, to_state, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def transition_task(
        self,
        task_id: str,
        to_state: str,
        *,
        expected_state_version: int | None = None,
        epoch: int | None = None,
        allow_new_turn_from_completed: bool = False,
        allow_inactive_epoch: bool = False,
        reason: str = "",
        checkpoint_id: str | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        extra_evidence: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        tid = _validate_identifier(task_id, "task_id")
        if to_state not in runtime_persistence.TASK_STATES:
            raise WorkflowStateError(f"unknown task state: {to_state}")
        if to_state == "completed":
            raise WorkflowTransitionRejected(
                "Use complete_task() to transition a root task to 'completed' with milestone and boundary verification."
            )
        evidence_json = _encode_evidence_json(
            checkpoint_id=checkpoint_id or "",
            evidence_refs=evidence_refs,
            extra=extra_evidence,
        )
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            from_state = str(row["state"])
            if expected_state_version is not None and int(row["state_version"]) != expected_state_version:
                raise WorkflowTransitionRejected("task state_version changed; refresh before transitioning.")
            current_epoch = int(row["current_epoch"])
            if epoch is not None and current_epoch != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale task epoch {epoch} cannot transition task {tid!r} at current_epoch={current_epoch}."
                )
            if epoch is not None and int(epoch) > 0 and not allow_inactive_epoch:
                ep_row = conn.execute(
                    "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, int(epoch)),
                ).fetchone()
                if ep_row is None or str(ep_row["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Inactive task epoch {epoch} cannot transition task {tid!r}."
                    )
                if float(ep_row["lease_expires_at"]) <= ts:
                    raise runtime_persistence.LeaseExpiredError(
                        f"Expired task epoch {epoch} cannot transition task {tid!r}."
                    )
            allowed = TASK_STATE_TRANSITIONS.get(from_state, frozenset())
            if from_state == "completed" and to_state == "running" and allow_new_turn_from_completed:
                pass
            elif to_state not in allowed:
                raise WorkflowTransitionRejected(
                    f"task transition {from_state} -> {to_state} is not allowed."
                )
            eff_epoch = current_epoch if epoch is None else int(epoch)
            new_ckpt = str(row["checkpoint_ref"]) if checkpoint_ref_is_none(checkpoint_id) else str(checkpoint_id)[:256]
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, state_version = state_version + 1, checkpoint_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (to_state, new_ckpt, ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, to_state, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def check_safe_boundary(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        abort_requested: bool = False,
    ) -> Literal["continue", "pause", "cancel"]:
        tid = _validate_identifier(task_id, "task_id")

        def _read(conn: sqlite3.Connection) -> Literal["continue", "pause", "cancel"]:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state, current_epoch, budget_json FROM runtime_tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if state == "cancelled" or budget.cancel_requested:
                return "cancel"
            if epoch is not None and int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Epoch {epoch} was superseded on task {tid!r} (current_epoch={row['current_epoch']})."
                )
            if state == "paused" or budget.pause_requested or abort_requested:
                return "pause"
            return "continue"

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def request_pause(
        self,
        task_id: str,
        *,
        reason: str = "operator_pause_requested",
        now: float | None = None,
    ) -> TaskControlResult:
        """Pause an idle/orphaned task immediately or flag ``pause_requested`` for safe-boundary stop."""
        tid = _validate_identifier(task_id, "task_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskControlResult:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if state in {"completed", "cancelled"} or budget.cancel_requested:
                raise WorkflowTransitionRejected(
                    f"Cannot pause task {tid!r} in state {state!r}."
                )
            if state == "paused":
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(row),
                    budget=budget,
                    deferred_to_safe_boundary=False,
                )
            current_epoch = int(row["current_epoch"])
            has_live_lease = False
            if current_epoch > 0:
                ep_row = conn.execute(
                    "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, current_epoch),
                ).fetchone()
                if ep_row is not None and str(ep_row["status"]) == "active" and float(ep_row["lease_expires_at"]) > ts:
                    has_live_lease = True
            if has_live_lease and state in {"running", "waiting_approval"}:
                deferred_budget = execution_budget.TaskBudgetState(
                    spec=budget.spec,
                    steps_used=budget.steps_used,
                    tool_calls_used=budget.tool_calls_used,
                    tokens_used=budget.tokens_used,
                    elapsed_s=budget.elapsed_s,
                    recoveries_used=budget.recoveries_used,
                    pause_requested=True,
                    cancel_requested=budget.cancel_requested,
                    exhausted_reason=budget.exhausted_reason,
                )
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (deferred_budget.to_json(), ts, tid),
                )
                refreshed = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
                assert refreshed is not None
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(refreshed),
                    budget=deferred_budget,
                    deferred_to_safe_boundary=True,
                )

            unresolved_rows = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                ORDER BY created_at ASC
                """,
                (tid,),
            ).fetchall()
            if unresolved_rows:
                raise WorkflowTransitionRejected(
                    f"Cannot pause task {tid!r} directly while consequential actions remain unresolved."
                )

            cleared_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=False,
                exhausted_reason=budget.exhausted_reason,
            )
            ckpt_ref = str(row["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ckpt_ref),
                )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cleared_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                """,
                (
                    tid,
                    next_seq,
                    state,
                    current_epoch,
                    str(reason)[:240],
                    _encode_evidence_json(checkpoint_id=ckpt_ref),
                    ts,
                ),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, updated_at = ?
                WHERE parent_task_id = ? AND state IN ('running', 'waiting_approval')
                """,
                (ts, tid),
            )
            paused_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert paused_row is not None
            return TaskControlResult(
                task=runtime_persistence._row_to_task(paused_row),
                budget=cleared_budget,
                deferred_to_safe_boundary=False,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def pause_at_safe_boundary(
        self,
        task_id: str,
        *,
        worker_id: str,
        epoch: int,
        step: int,
        checkpoint_payload: dict[str, Any] | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        milestone_id: str = "",
        reason: str = "paused_at_safe_boundary",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Persist an ``interrupted`` checkpoint, pause running milestones, and release lease into ``paused``."""
        tid = _validate_identifier(task_id, "task_id")
        wid = _validate_identifier(worker_id, "worker_id")
        ckpt: workflow_checkpoints.TaskCheckpointRecord | None = None
        if checkpoint_payload is not None:
            ckpt = self.checkpoints.save_checkpoint(
                tid,
                step=step,
                payload=checkpoint_payload,
                evidence_refs=evidence_refs,
                epoch=epoch,
                milestone_id=milestone_id,
                status="interrupted",
                require_active_epoch=True,
                now=now,
            )
        else:
            ckpt = self.checkpoints.mark_latest_interrupted(tid, now=now)

        ts = time.time() if now is None else float(now)
        evidence_json = _encode_evidence_json(
            checkpoint_id=ckpt.checkpoint_id if ckpt else "",
            evidence_refs=evidence_refs,
        )

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            if int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot pause task {tid!r} at current_epoch={row['current_epoch']}."
                )
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if str(row["state"]) == "cancelled" or budget.cancel_requested:
                raise WorkflowTransitionRejected(f"Task {tid!r} was cancelled before pause boundary completed.")
            ep_row = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (tid, int(epoch)),
            ).fetchone()
            if ep_row is None or str(ep_row["worker_id"]) != wid or str(ep_row["status"]) != "active":
                raise runtime_persistence.StaleEpochError(
                    f"Epoch {epoch} on task {tid!r} is no longer actively owned by {wid!r}."
                )
            conn.execute(
                "UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ? WHERE task_id = ? AND epoch = ?",
                (ts, tid, int(epoch)),
            )
            cleared_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=False,
                exhausted_reason=budget.exhausted_reason,
            )
            from_state = str(row["state"])
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cleared_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, int(epoch), str(reason)[:240], evidence_json, ts),
            )
            ms_rows = conn.execute(
                "SELECT task_id, state FROM runtime_tasks WHERE parent_task_id = ? AND state IN ('running', 'waiting_approval')",
                (tid,),
            ).fetchall()
            for ms_row in ms_rows:
                ms_tid = str(ms_row["task_id"])
                ms_from = str(ms_row["state"])
                ms_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (ms_tid,),
                ).fetchone()
                ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                conn.execute(
                    "UPDATE runtime_tasks SET state = 'paused', state_version = state_version + 1, updated_at = ? WHERE task_id = ?",
                    (ts, ms_tid),
                )
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                    """,
                    (ms_tid, ms_seq, ms_from, int(epoch), str(reason)[:240], evidence_json, ts),
                )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = "operator_cancelled",
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        now: float | None = None,
    ) -> TaskControlResult:
        """Fence active epochs, preserve unresolved side-effect records, and transition to ``cancelled``."""
        tid = _validate_identifier(task_id, "task_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskControlResult:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            from_state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if from_state == "completed":
                raise WorkflowTransitionRejected(f"Cannot cancel completed task {tid!r}.")
            if from_state == "cancelled":
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(row),
                    budget=budget,
                    deferred_to_safe_boundary=False,
                )
            current_epoch = int(row["current_epoch"])
            conn.execute(
                """
                UPDATE runtime_task_epochs
                SET status = 'fenced', fenced_at = ?, fence_reason = 'task_cancelled'
                WHERE task_id = ? AND status = 'active'
                """,
                (ts, tid),
            )
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = 'uncertain', error_code = 'cancelled_in_flight', updated_at = ?
                WHERE task_id = ? AND status = 'in_flight' AND action_class != 'read_only'
                """,
                (ts, tid),
            )
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = 'failed', ok = 0, error_code = 'cancelled_in_flight', updated_at = ?
                WHERE task_id = ? AND status = 'in_flight' AND action_class = 'read_only'
                """,
                (ts, tid),
            )
            unresolved_rows = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                ORDER BY created_at ASC
                """,
                (tid,),
            ).fetchall()
            unresolved_ids = tuple(str(r["action_id"]) for r in unresolved_rows)

            ckpt_ref = str(row["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ckpt_ref),
                )
            cancelled_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=True,
                exhausted_reason=budget.exhausted_reason,
            )
            evidence_json = _encode_evidence_json(
                checkpoint_id=ckpt_ref,
                evidence_refs=evidence_refs,
                extra={"unresolved_action_ids": list(unresolved_ids)} if unresolved_ids else None,
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'cancelled', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cancelled_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'cancelled', ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, current_epoch, str(reason)[:240], evidence_json, ts),
            )
            ms_rows = conn.execute(
                """
                SELECT task_id, state FROM runtime_tasks
                WHERE parent_task_id = ? AND state NOT IN ('completed', 'cancelled')
                """,
                (tid,),
            ).fetchall()
            for ms_row in ms_rows:
                ms_tid = str(ms_row["task_id"])
                ms_from = str(ms_row["state"])
                ms_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (ms_tid,),
                ).fetchone()
                ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                conn.execute(
                    "UPDATE runtime_tasks SET state = 'cancelled', state_version = state_version + 1, updated_at = ? WHERE task_id = ?",
                    (ts, ms_tid),
                )
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, 'cancelled', ?, ?, ?, ?)
                    """,
                    (ms_tid, ms_seq, ms_from, current_epoch, str(reason)[:240], evidence_json, ts),
                )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return TaskControlResult(
                task=runtime_persistence._row_to_task(updated),
                budget=cancelled_budget,
                deferred_to_safe_boundary=False,
                unresolved_action_ids=unresolved_ids,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def prepare_resume(
        self,
        task_id: str,
        *,
        consume_recovery_budget: bool = True,
        now: float | None = None,
    ) -> ResumedTaskPlan:
        """Validate resumability, checkpoint integrity, unresolved side effects, and recovery budget."""
        tid = _validate_identifier(task_id, "task_id")
        task = self.store.get_task(tid)
        if task is None:
            raise WorkflowStateError(f"Task {tid!r} does not exist.")
        if task.state == "cancelled":
            raise WorkflowTransitionRejected(f"Cancelled task {tid!r} cannot be resumed.")
        if task.state == "completed":
            raise WorkflowTransitionRejected(f"Completed task {tid!r} does not require resume.")
        if task.state == "blocked":
            raise workflow_checkpoints.ResumeRejected(
                f"Blocked task {tid!r} has unresolved side effects or integrity violations and cannot resume automatically."
            )
        if task.state not in {"paused", "failed"}:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} in state {task.state!r} must be paused or failed before resume."
            )
        unresolved = [
            act.action_id
            for act in self.store.list_task_actions(tid, statuses=("in_flight", "uncertain"))
            if act.action_class != "read_only"
        ]
        if unresolved:
            raise workflow_checkpoints.ResumeRejected(
                f"Task {tid!r} has unresolved consequential action(s): {', '.join(unresolved)}."
            )
        # Verify checkpoint integrity BEFORE consuming recovery budget.
        ckpt = self.checkpoints.load_latest_checkpoint(tid, verify_integrity=True)
        existing_budget = self.budgets.get_budget(tid)
        if existing_budget.cancel_requested:
            raise WorkflowTransitionRejected(f"Task {tid!r} has cancellation requested and cannot resume.")
        violation = existing_budget.violation_reason(check_recovery=False)
        if violation:
            raise execution_budget.BudgetExceeded(violation)

        if consume_recovery_budget:
            try:
                budget = self.budgets.consume_recovery(tid, now=now)
            except execution_budget.BudgetExceeded as exc:
                self.transition_task(
                    tid,
                    "blocked",
                    allow_inactive_epoch=True,
                    reason="recovery_budget_exceeded",
                    checkpoint_id=ckpt.checkpoint_id if ckpt else None,
                    extra_evidence={"error": str(exc)},
                    now=now,
                )
                raise
        else:
            budget = self.budgets.set_control_flags(tid, pause_requested=False, now=now)

        resumed_ckpt = self.checkpoints.validate_and_mark_resumed(
            tid,
            epoch=task.current_epoch,
            now=now,
        )
        milestones = tuple(self.list_milestones(tid))
        completed_ms = tuple(ms.task_id for ms in milestones if ms.state == "completed")
        refreshed_task = self.store.get_task(tid)
        assert refreshed_task is not None
        next_step = (resumed_ckpt.step + 1) if resumed_ckpt is not None else 0
        return ResumedTaskPlan(
            task=refreshed_task,
            budget=budget,
            checkpoint=resumed_ckpt,
            milestones=milestones,
            next_step=next_step,
            completed_milestone_ids=completed_ms,
        )

    def complete_task(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        epoch: int | None = None,
        step: int = 0,
        checkpoint_payload: dict[str, Any] | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        abort_requested: bool = False,
        reason: str = "task_completed",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Complete a running root task only when all milestones, boundaries, and actions are clean."""
        tid = _validate_identifier(task_id, "task_id")
        if abort_requested:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot claim completion when abort/pause was requested."
            )
        ts = time.time() if now is None else float(now)

        # Pre-check state, control flags, milestones, and unresolved actions before writing final checkpoint.
        current_task = self.store.get_task(tid)
        if current_task is None:
            raise WorkflowStateError(f"Task {tid!r} does not exist.")
        if current_task.state != "running":
            raise WorkflowTransitionRejected(
                f"Task {tid!r} in state {current_task.state!r} cannot transition to 'completed'."
            )
        current_budget = self.budgets.get_budget(tid)
        if current_budget.pause_requested or current_budget.cancel_requested:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot claim completion while pause_requested={current_budget.pause_requested} "
                f"or cancel_requested={current_budget.cancel_requested}."
            )
        milestones = self.list_milestones(tid)
        incomplete_ms = [ms.task_id for ms in milestones if ms.state != "completed"]
        if incomplete_ms:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot complete while milestone(s) remain incomplete: {', '.join(incomplete_ms)}."
            )
        unresolved = [
            act.action_id
            for act in self.store.list_task_actions(tid, statuses=("in_flight", "uncertain"))
            if act.action_class != "read_only"
        ]
        if unresolved:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot complete with unresolved consequential action(s): {', '.join(unresolved)}."
            )

        ckpt: workflow_checkpoints.TaskCheckpointRecord | None = None
        if checkpoint_payload is not None:
            ckpt = self.checkpoints.save_checkpoint(
                tid,
                step=step,
                payload=checkpoint_payload,
                evidence_refs=evidence_refs,
                epoch=epoch,
                status="committed",
                require_active_epoch=epoch is not None and epoch > 0,
                now=ts,
            )

        evidence_json = _encode_evidence_json(
            checkpoint_id=ckpt.checkpoint_id if ckpt else current_task.checkpoint_ref,
            evidence_refs=evidence_refs,
        )

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            if str(row["state"]) != "running":
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} left 'running' state ({row['state']!r}) before completion committed."
                )
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if budget.pause_requested or budget.cancel_requested:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} received pause/cancel before completion committed."
                )
            current_epoch = int(row["current_epoch"])
            if epoch is not None and current_epoch != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot complete task {tid!r} at current_epoch={current_epoch}."
                )
            if epoch is not None and int(epoch) > 0:
                ep_row = conn.execute(
                    "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, int(epoch)),
                ).fetchone()
                if ep_row is None or str(ep_row["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Inactive epoch {epoch} cannot complete task {tid!r}."
                    )
                if worker_id is not None and str(ep_row["worker_id"]) != worker_id:
                    raise runtime_persistence.StaleEpochError(
                        f"Epoch {epoch} on task {tid!r} is not owned by {worker_id!r}."
                    )
                if float(ep_row["lease_expires_at"]) <= ts:
                    raise runtime_persistence.LeaseExpiredError(
                        f"Expired epoch {epoch} cannot complete task {tid!r}."
                    )
                conn.execute(
                    "UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ? WHERE task_id = ? AND epoch = ?",
                    (ts, tid, int(epoch)),
                )
            bad_ms = conn.execute(
                "SELECT task_id FROM runtime_tasks WHERE parent_task_id = ? AND state != 'completed'",
                (tid,),
            ).fetchall()
            if bad_ms:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} has incomplete milestone(s) inside completion commit."
                )
            bad_actions = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                """,
                (tid,),
            ).fetchall()
            if bad_actions:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} has unresolved action(s) inside completion commit."
                )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            eff_epoch = current_epoch if epoch is None else int(epoch)
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'completed', state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, 'running', 'completed', ?, ?, ?, ?)
                """,
                (tid, next_seq, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def recover_incomplete_transitions(
        self,
        *,
        now: float | None = None,
    ) -> RecoverySweepReport:
        """Reconcile staged checkpoints, interrupted root transitions, and milestone skew on startup/recovery."""
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> RecoverySweepReport:
            conn.row_factory = sqlite3.Row
            reconciled_ckpts: list[str] = []
            recovered_tasks: list[str] = []
            blocked_tasks: list[str] = []
            cancelled_tasks: list[str] = []
            reconciled_milestones: list[str] = []

            # 1. Reconcile staged 'pending_commit' checkpoints left by mid-transition crashes.
            pending_ckpts = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE status = 'pending_commit' ORDER BY created_at ASC, seq ASC"
            ).fetchall()
            for ck_row in pending_ckpts:
                ck_id = str(ck_row["checkpoint_id"])
                ck_tid = str(ck_row["task_id"])
                try:
                    verified = workflow_checkpoints._decode_and_verify_checkpoint_row(ck_row, verify_integrity=True)
                except workflow_checkpoints.ResumeRejected:
                    conn.execute(
                        "UPDATE runtime_task_checkpoints SET status = 'superseded', updated_at = ? WHERE checkpoint_id = ?",
                        (ts, ck_id),
                    )
                    reconciled_ckpts.append(ck_id)
                    continue
                conn.execute(
                    """
                    UPDATE runtime_task_checkpoints
                    SET status = 'superseded', updated_at = ?
                    WHERE task_id = ? AND seq < ? AND status IN ('committed', 'interrupted', 'resumed')
                    """,
                    (ts, ck_tid, verified.seq),
                )
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ck_id),
                )
                conn.execute(
                    "UPDATE runtime_tasks SET checkpoint_ref = ?, updated_at = ? WHERE task_id = ?",
                    (ck_id, ts, ck_tid),
                )
                reconciled_ckpts.append(ck_id)

            # 2. Reconcile orphaned non-terminal root tasks without an active unexpired lease.
            root_rows = conn.execute(
                """
                SELECT * FROM runtime_tasks
                WHERE parent_task_id IS NULL
                  AND state IN ('running', 'waiting_approval', 'pending', 'paused', 'blocked', 'failed', 'cancelled')
                ORDER BY created_at ASC
                """
            ).fetchall()
            for t_row in root_rows:
                tid = str(t_row["task_id"])
                state = str(t_row["state"])
                current_epoch = int(t_row["current_epoch"])
                has_live_lease = False
                if current_epoch > 0:
                    ep_row = conn.execute(
                        "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                        (tid, current_epoch),
                    ).fetchone()
                    if ep_row is not None and str(ep_row["status"]) == "active":
                        if float(ep_row["lease_expires_at"]) > ts:
                            has_live_lease = True
                        else:
                            conn.execute(
                                """
                                UPDATE runtime_task_epochs
                                SET status = 'expired', fenced_at = ?, fence_reason = 'lease_expired_on_recovery'
                                WHERE task_id = ? AND epoch = ? AND status = 'active'
                                """,
                                (ts, tid, current_epoch),
                            )

                if has_live_lease:
                    continue

                # Sweep any orphaned in-flight actions on inactive epochs before deciding task state.
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class != 'read_only'
                    """,
                    (ts, tid),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'failed', ok = 0, error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class = 'read_only'
                    """,
                    (ts, tid),
                )

                try:
                    budget = execution_budget.TaskBudgetState.from_json(str(t_row["budget_json"]))
                except execution_budget.BudgetError:
                    budget = execution_budget.TaskBudgetState(spec=execution_budget.TaskBudgetSpec())

                if state in {"running", "waiting_approval"} or (
                    budget.cancel_requested and state not in {"cancelled", "completed"}
                ):
                    unresolved_rows = conn.execute(
                        """
                        SELECT action_id FROM runtime_actions
                        WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                        ORDER BY created_at ASC
                        """,
                        (tid,),
                    ).fetchall()
                    unresolved_ids = [str(r["action_id"]) for r in unresolved_rows]
                    ckpt_ref = str(t_row["checkpoint_ref"] or "").strip()
                    ckpt_corrupt = False
                    if ckpt_ref:
                        ck_check = conn.execute(
                            "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                            (ckpt_ref, tid),
                        ).fetchone()
                        if ck_check is None:
                            ckpt_corrupt = True
                        else:
                            try:
                                workflow_checkpoints._decode_and_verify_checkpoint_row(ck_check, verify_integrity=True)
                                conn.execute(
                                    """
                                    UPDATE runtime_task_checkpoints
                                    SET status = 'interrupted', updated_at = ?
                                    WHERE checkpoint_id = ? AND status IN ('committed', 'resumed')
                                    """,
                                    (ts, ckpt_ref),
                                )
                            except workflow_checkpoints.ResumeRejected:
                                ckpt_corrupt = True

                    if unresolved_ids or ckpt_corrupt:
                        target_state = "blocked"
                        reason = (
                            "corrupt_checkpoint_on_recovery"
                            if ckpt_corrupt
                            else "recovered_incomplete_with_unresolved_effects"
                        )
                        blocked_tasks.append(tid)
                    elif budget.cancel_requested:
                        target_state = "cancelled"
                        reason = "recovered_incomplete_cancel"
                        cancelled_tasks.append(tid)
                    else:
                        target_state = "paused"
                        reason = "recovered_incomplete_transition"
                        recovered_tasks.append(tid)

                    reconciled_budget = execution_budget.TaskBudgetState(
                        spec=budget.spec,
                        steps_used=budget.steps_used,
                        tool_calls_used=budget.tool_calls_used,
                        tokens_used=budget.tokens_used,
                        elapsed_s=budget.elapsed_s,
                        recoveries_used=budget.recoveries_used,
                        pause_requested=False,
                        cancel_requested=(target_state == "cancelled"),
                        exhausted_reason=budget.exhausted_reason,
                    )
                    seq_row = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                        (tid,),
                    ).fetchone()
                    next_seq = int(seq_row[0]) if seq_row else 1
                    conn.execute(
                        """
                        UPDATE runtime_tasks
                        SET state = ?, state_version = state_version + 1, budget_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (target_state, reconciled_budget.to_json(), ts, tid),
                    )
                    conn.execute(
                        """
                        INSERT INTO runtime_task_transitions (
                            task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tid,
                            next_seq,
                            state,
                            target_state,
                            current_epoch,
                            reason,
                            _encode_evidence_json(
                                checkpoint_id=ckpt_ref,
                                extra={"unresolved_action_ids": unresolved_ids} if unresolved_ids else None,
                            ),
                            ts,
                        ),
                    )
                    state = target_state

                # 3. Reconcile child milestone skew when parent has no live worker lease.
                if state in {"paused", "blocked", "failed", "cancelled"}:
                    ms_target = "cancelled" if state == "cancelled" else ("blocked" if state == "blocked" else "paused")
                    ms_filter = (
                        "state NOT IN ('completed', 'cancelled')"
                        if state == "cancelled"
                        else "state IN ('running', 'waiting_approval')"
                    )
                    skewed_ms = conn.execute(
                        f"SELECT task_id, state FROM runtime_tasks WHERE parent_task_id = ? AND {ms_filter}",
                        (tid,),
                    ).fetchall()
                    for ms_row in skewed_ms:
                        ms_tid = str(ms_row["task_id"])
                        ms_from = str(ms_row["state"])
                        ms_seq_row = conn.execute(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                            (ms_tid,),
                        ).fetchone()
                        ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                        conn.execute(
                            """
                            UPDATE runtime_tasks
                            SET state = ?, state_version = state_version + 1, updated_at = ?
                            WHERE task_id = ?
                            """,
                            (ms_target, ts, ms_tid),
                        )
                        conn.execute(
                            """
                            INSERT INTO runtime_task_transitions (
                                task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, 'recovered_milestone_skew', '{}', ?)
                            """,
                            (ms_tid, ms_seq, ms_from, ms_target, current_epoch, ts),
                        )
                        reconciled_milestones.append(ms_tid)

            return RecoverySweepReport(
                recovered_task_ids=tuple(recovered_tasks),
                blocked_task_ids=tuple(blocked_tasks),
                cancelled_task_ids=tuple(cancelled_tasks),
                reconciled_checkpoint_ids=tuple(reconciled_ckpts),
                reconciled_milestone_ids=tuple(reconciled_milestones),
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )


def checkpoint_ref_is_none(value: str | None) -> bool:
    return value is None
