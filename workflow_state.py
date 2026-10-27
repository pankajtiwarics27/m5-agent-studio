"""Persisted workflow state machine with validated atomic transitions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
