"""Durable workflow checkpoints and safe resume validation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CheckpointStatus = Literal["active", "interrupted", "resumed", "completed"]
_STATUSES = frozenset({"active", "interrupted", "resumed", "completed"})


class CheckpointError(ValueError):
    """Raised when checkpoint data is malformed or not resumable."""


class ResumeRejected(RuntimeError):
    """Raised when a checkpoint cannot be safely resumed."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    workflow_id: str
    step: int
    state: CheckpointStatus
    payload: dict[str, object]
    payload_hash: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AuditEntry:
    workflow_id: str
    step: int
    event: str
    detail: str
    created_at: str


def _workflow_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CheckpointError("workflow_id must be a non-empty string without NUL characters.")
    return value.strip()


def _step(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointError("step must be a non-negative integer.")
    return value


def _status(value: str) -> CheckpointStatus:
    if value not in _STATUSES:
        raise CheckpointError(f"unknown checkpoint state: {value}")
    return value  # type: ignore[return-value]


def _encoded_payload(payload: dict[str, object]) -> tuple[str, str]:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint payload must be JSON serializable.") from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WorkflowCheckpointStore:
    """SQLite checkpoint store that only resumes intact interrupted steps."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    workflow_id TEXT PRIMARY KEY,
                    step INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS workflow_checkpoint_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _checkpoint(row: sqlite3.Row | None) -> Checkpoint | None:
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ResumeRejected("checkpoint payload is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ResumeRejected("checkpoint payload is not an object.")
        _status(row["state"])
        return Checkpoint(row["workflow_id"], row["step"], row["state"], payload, row["payload_hash"], row["updated_at"])

    def save(self, workflow_id: str, step: int, payload: dict[str, object], *, state: CheckpointStatus = "active") -> Checkpoint:
        identifier = _workflow_id(workflow_id)
        position = _step(step)
        normalized_state = _status(state)
        encoded, payload_hash = _encoded_payload(payload)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_checkpoints(workflow_id, step, state, payload_json, payload_hash)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    step=excluded.step, state=excluded.state,
                    payload_json=excluded.payload_json, payload_hash=excluded.payload_hash,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (identifier, position, normalized_state, encoded, payload_hash),
            )
            connection.execute(
                "INSERT INTO workflow_checkpoint_audit(workflow_id, step, event, detail) VALUES (?, ?, ?, ?)",
                (identifier, position, "checkpoint_saved", normalized_state),
            )
            row = connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id = ?", (identifier,)).fetchone()
        result = self._checkpoint(row)
        assert result is not None
        return result

    def mark_interrupted(self, workflow_id: str) -> Checkpoint:
        identifier = _workflow_id(workflow_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id = ?", (identifier,)).fetchone()
            checkpoint = self._checkpoint(row)
            if checkpoint is None:
                raise CheckpointError("checkpoint does not exist.")
            connection.execute("UPDATE workflow_checkpoints SET state = 'interrupted', updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ?", (identifier,))
            connection.execute(
                "INSERT INTO workflow_checkpoint_audit(workflow_id, step, event, detail) VALUES (?, ?, ?, ?)",
                (identifier, checkpoint.step, "workflow_interrupted", "safe resume available"),
            )
            updated = connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id = ?", (identifier,)).fetchone()
        result = self._checkpoint(updated)
        assert result is not None
        return result

    def resume(self, workflow_id: str) -> Checkpoint:
        identifier = _workflow_id(workflow_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id = ?", (identifier,)).fetchone()
            checkpoint = self._checkpoint(row)
            if checkpoint is None:
                raise ResumeRejected("checkpoint does not exist.")
            if checkpoint.state != "interrupted":
                raise ResumeRejected("only interrupted workflows can be resumed.")
            encoded, payload_hash = _encoded_payload(checkpoint.payload)
            if not hmac_compare(payload_hash, checkpoint.payload_hash):
                raise ResumeRejected("checkpoint integrity hash does not match payload.")
            connection.execute(
                "UPDATE workflow_checkpoints SET state = 'resumed', payload_json = ?, payload_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ?",
                (encoded, payload_hash, identifier),
            )
            connection.execute(
                "INSERT INTO workflow_checkpoint_audit(workflow_id, step, event, detail) VALUES (?, ?, ?, ?)",
                (identifier, checkpoint.step, "workflow_resumed", "integrity verified"),
            )
            updated = connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id = ?", (identifier,)).fetchone()
        result = self._checkpoint(updated)
        assert result is not None
        return result

    def audit(self, workflow_id: str) -> tuple[AuditEntry, ...]:
        identifier = _workflow_id(workflow_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id, step, event, detail, created_at FROM workflow_checkpoint_audit WHERE workflow_id = ? ORDER BY id",
                (identifier,),
            ).fetchall()
        return tuple(AuditEntry(row["workflow_id"], row["step"], row["event"], row["detail"], row["created_at"]) for row in rows)


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time comparison for checkpoint integrity digests."""
    import hmac

    return hmac.compare_digest(left, right)
