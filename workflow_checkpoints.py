"""Durable workflow checkpoints, evidence references, and safe resume validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import runtime_persistence

CheckpointStatus = Literal["active", "interrupted", "resumed", "completed"]
_STATUSES = frozenset({"active", "interrupted", "resumed", "completed"})

TaskCheckpointStatus = Literal["pending_commit", "committed", "interrupted", "resumed", "superseded"]


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
    return hmac.compare_digest(left, right)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Typed durable evidence pointer attached to a task checkpoint or transition."""

    kind: str
    ref_id: str
    sha256: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip() or "\x00" in self.kind:
            raise CheckpointError("EvidenceRef.kind must be a non-empty string.")
        if not isinstance(self.ref_id, str) or not self.ref_id.strip() or "\x00" in self.ref_id:
            raise CheckpointError("EvidenceRef.ref_id must be a non-empty string.")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.strip()[:64],
            "ref_id": self.ref_id.strip()[:256],
            "sha256": str(self.sha256 or "").strip()[:128],
            "summary": str(self.summary or "").strip()[:240],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceRef:
        if not isinstance(data, dict):
            raise CheckpointError("EvidenceRef entry must be a JSON object.")
        return cls(
            kind=str(data.get("kind", "")),
            ref_id=str(data.get("ref_id", "")),
            sha256=str(data.get("sha256", "")),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True, slots=True)
class TaskCheckpointRecord:
    """Authoritative checkpoint row in ``runtime_task_checkpoints`` (M5-P1-05)."""

    checkpoint_id: str
    task_id: str
    seq: int
    step: int
    epoch: int
    milestone_id: str
    status: str
    payload: dict[str, Any]
    payload_hash: str
    evidence_refs: tuple[EvidenceRef, ...]
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "seq": self.seq,
            "step": self.step,
            "epoch": self.epoch,
            "milestone_id": self.milestone_id,
            "status": self.status,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def compute_task_checkpoint_digest(payload_json: str, evidence_refs_json: str) -> str:
    """Compute canonical SHA-256 digest covering both payload and evidence references."""
    material = f"{payload_json}\n{evidence_refs_json}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _encode_checkpoint_bundle(
    payload: dict[str, Any],
    evidence_refs: Sequence[EvidenceRef | dict[str, Any]],
) -> tuple[str, str, tuple[EvidenceRef, ...], str]:
    if not isinstance(payload, dict):
        raise CheckpointError("checkpoint payload must be a dict.")
    normalized_refs: list[EvidenceRef] = []
    for item in evidence_refs:
        if isinstance(item, EvidenceRef):
            normalized_refs.append(item)
        elif isinstance(item, dict):
            normalized_refs.append(EvidenceRef.from_dict(item))
        else:
            to_dict_fn = getattr(item, "to_dict", None)
            if callable(to_dict_fn):
                normalized_refs.append(EvidenceRef.from_dict(to_dict_fn()))
            else:
                raise CheckpointError("evidence_refs items must be EvidenceRef or dict.")
    try:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        refs_json = json.dumps(
            [r.to_dict() for r in normalized_refs],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise CheckpointError("checkpoint payload and evidence_refs must be JSON serializable.") from exc
    digest = compute_task_checkpoint_digest(payload_json, refs_json)
    return payload_json, refs_json, tuple(normalized_refs), digest


def _decode_and_verify_checkpoint_row(
    row: sqlite3.Row,
    *,
    verify_integrity: bool = True,
) -> TaskCheckpointRecord:
    raw_payload = str(row["payload_json"])
    raw_refs = str(row["evidence_refs_json"])
    stored_hash = str(row["payload_hash"])
    try:
        payload_obj = json.loads(raw_payload)
        refs_obj = json.loads(raw_refs)
    except (TypeError, ValueError) as exc:
        raise ResumeRejected("checkpoint JSON is malformed or truncated.") from exc
    if not isinstance(payload_obj, dict):
        raise ResumeRejected("checkpoint payload_json is not a JSON object.")
    if not isinstance(refs_obj, list):
        raise ResumeRejected("checkpoint evidence_refs_json is not a JSON array.")
    try:
        refs = tuple(EvidenceRef.from_dict(item) for item in refs_obj)
    except CheckpointError as exc:
        raise ResumeRejected(f"checkpoint evidence reference invalid: {exc}") from exc
    status = str(row["status"])
    if status not in runtime_persistence.CHECKPOINT_STATUSES:
        raise ResumeRejected(f"unknown checkpoint status {status!r}.")
    if verify_integrity:
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        canonical_refs = json.dumps(
            [r.to_dict() for r in refs],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected_hash = compute_task_checkpoint_digest(canonical_payload, canonical_refs)
        if not hmac_compare(expected_hash, stored_hash):
            raise ResumeRejected("checkpoint integrity hash does not match payload or evidence refs.")
    return TaskCheckpointRecord(
        checkpoint_id=str(row["checkpoint_id"]),
        task_id=str(row["task_id"]),
        seq=int(row["seq"]),
        step=int(row["step"]),
        epoch=int(row["epoch"]),
        milestone_id=str(row["milestone_id"]),
        status=status,
        payload=payload_obj,
        payload_hash=stored_hash,
        evidence_refs=refs,
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class DurableTaskCheckpointStore:
    """Authoritative SQLite store for ``runtime_task_checkpoints`` + ``runtime_tasks.checkpoint_ref``."""

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

    def stage_checkpoint(
        self,
        task_id: str,
        *,
        step: int,
        payload: dict[str, Any],
        evidence_refs: Sequence[EvidenceRef | dict[str, Any]] = (),
        epoch: int = 0,
        milestone_id: str = "",
        now: float | None = None,
    ) -> TaskCheckpointRecord:
        """Write a ``pending_commit`` checkpoint row without advancing ``runtime_tasks.checkpoint_ref``."""
        tid = _workflow_id(task_id)
        pos = _step(step)
        payload_json, refs_json, _, digest = _encode_checkpoint_bundle(payload, evidence_refs)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskCheckpointRecord:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT current_epoch FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_checkpoints WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            ckpt_id = f"ckpt-{tid[:48]}-s{next_seq}-{digest[:10]}"
            conn.execute(
                """
                INSERT INTO runtime_task_checkpoints (
                    checkpoint_id, task_id, seq, step, epoch, milestone_id, status,
                    payload_json, payload_hash, evidence_refs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_commit', ?, ?, ?, ?, ?)
                """,
                (ckpt_id, tid, next_seq, pos, int(epoch), str(milestone_id)[:128], payload_json, digest, refs_json, ts, ts),
            )
            saved = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (ckpt_id,),
            ).fetchone()
            assert saved is not None
            return _decode_and_verify_checkpoint_row(saved, verify_integrity=True)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def save_checkpoint(
        self,
        task_id: str,
        *,
        step: int,
        payload: dict[str, Any],
        evidence_refs: Sequence[EvidenceRef | dict[str, Any]] = (),
        epoch: int | None = None,
        milestone_id: str = "",
        status: Literal["committed", "interrupted"] = "committed",
        require_active_epoch: bool = False,
        now: float | None = None,
    ) -> TaskCheckpointRecord:
        """Atomically persist checkpoint + evidence refs and advance ``runtime_tasks.checkpoint_ref``."""
        tid = _workflow_id(task_id)
        pos = _step(step)
        if status not in {"committed", "interrupted"}:
            raise CheckpointError(f"Unsupported save_checkpoint status: {status!r}")
        payload_json, refs_json, _, digest = _encode_checkpoint_bundle(payload, evidence_refs)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskCheckpointRecord:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT current_epoch FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current_epoch = int(task_row["current_epoch"])
            if epoch is not None and int(epoch) != current_epoch:
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot checkpoint task {tid!r} at current_epoch={current_epoch}."
                )
            effective_epoch = current_epoch if epoch is None else int(epoch)
            if require_active_epoch and effective_epoch > 0:
                ep_row = conn.execute(
                    "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, effective_epoch),
                ).fetchone()
                if ep_row is None or str(ep_row["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Inactive epoch {effective_epoch} cannot checkpoint task {tid!r}."
                    )
                if float(ep_row["lease_expires_at"]) <= ts:
                    raise runtime_persistence.LeaseExpiredError(
                        f"Expired epoch {effective_epoch} cannot checkpoint task {tid!r}."
                    )
            conn.execute(
                """
                UPDATE runtime_task_checkpoints
                SET status = 'superseded', updated_at = ?
                WHERE task_id = ? AND status IN ('committed', 'interrupted', 'resumed', 'pending_commit')
                """,
                (ts, tid),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_checkpoints WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            ckpt_id = f"ckpt-{tid[:48]}-s{next_seq}-{digest[:10]}"
            conn.execute(
                """
                INSERT INTO runtime_task_checkpoints (
                    checkpoint_id, task_id, seq, step, epoch, milestone_id, status,
                    payload_json, payload_hash, evidence_refs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ckpt_id,
                    tid,
                    next_seq,
                    pos,
                    effective_epoch,
                    str(milestone_id)[:128],
                    status,
                    payload_json,
                    digest,
                    refs_json,
                    ts,
                    ts,
                ),
            )
            conn.execute(
                "UPDATE runtime_tasks SET checkpoint_ref = ?, updated_at = ? WHERE task_id = ?",
                (ckpt_id, ts, tid),
            )
            saved = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (ckpt_id,),
            ).fetchone()
            assert saved is not None
            return _decode_and_verify_checkpoint_row(saved, verify_integrity=True)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def get_checkpoint(
        self,
        checkpoint_id: str,
        *,
        verify_integrity: bool = True,
    ) -> TaskCheckpointRecord | None:
        cid = _workflow_id(checkpoint_id)

        def _read(conn: sqlite3.Connection) -> TaskCheckpointRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                return None
            return _decode_and_verify_checkpoint_row(row, verify_integrity=verify_integrity)

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def load_latest_checkpoint(
        self,
        task_id: str,
        *,
        verify_integrity: bool = True,
    ) -> TaskCheckpointRecord | None:
        tid = _workflow_id(task_id)

        def _read(conn: sqlite3.Connection) -> TaskCheckpointRecord | None:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT checkpoint_ref FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                return None
            ckpt_ref = str(task_row["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                ref_row = conn.execute(
                    "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                    (ckpt_ref, tid),
                ).fetchone()
                if ref_row is None:
                    raise ResumeRejected(
                        f"Task {tid!r} references missing checkpoint {ckpt_ref!r}."
                    )
                return _decode_and_verify_checkpoint_row(ref_row, verify_integrity=verify_integrity)
            latest_row = conn.execute(
                """
                SELECT * FROM runtime_task_checkpoints
                WHERE task_id = ? AND status IN ('committed', 'interrupted', 'resumed')
                ORDER BY seq DESC LIMIT 1
                """,
                (tid,),
            ).fetchone()
            if latest_row is None:
                return None
            return _decode_and_verify_checkpoint_row(latest_row, verify_integrity=verify_integrity)

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def list_checkpoints(
        self,
        task_id: str,
        *,
        verify_integrity: bool = False,
    ) -> list[TaskCheckpointRecord]:
        tid = _workflow_id(task_id)

        def _read(conn: sqlite3.Connection) -> list[TaskCheckpointRecord]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE task_id = ? ORDER BY seq ASC",
                (tid,),
            ).fetchall()
            return [_decode_and_verify_checkpoint_row(r, verify_integrity=verify_integrity) for r in rows]

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def mark_latest_interrupted(
        self,
        task_id: str,
        *,
        now: float | None = None,
    ) -> TaskCheckpointRecord | None:
        tid = _workflow_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskCheckpointRecord | None:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT checkpoint_ref FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                return None
            ckpt_ref = str(task_row["checkpoint_ref"] or "").strip()
            row = None
            if ckpt_ref:
                row = conn.execute(
                    "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                    (ckpt_ref, tid),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM runtime_task_checkpoints
                    WHERE task_id = ? AND status IN ('committed', 'interrupted', 'resumed')
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (tid,),
                ).fetchone()
            if row is None:
                return None
            verified = _decode_and_verify_checkpoint_row(row, verify_integrity=True)
            conn.execute(
                "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                (ts, verified.checkpoint_id),
            )
            if ckpt_ref != verified.checkpoint_id:
                conn.execute(
                    "UPDATE runtime_tasks SET checkpoint_ref = ?, updated_at = ? WHERE task_id = ?",
                    (verified.checkpoint_id, ts, tid),
                )
            refreshed = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (verified.checkpoint_id,),
            ).fetchone()
            assert refreshed is not None
            return _decode_and_verify_checkpoint_row(refreshed, verify_integrity=True)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def validate_and_mark_resumed(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        now: float | None = None,
    ) -> TaskCheckpointRecord | None:
        """Verify latest checkpoint integrity and mark ``resumed`` inside ``BEGIN IMMEDIATE``."""
        tid = _workflow_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskCheckpointRecord | None:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT checkpoint_ref FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                raise ResumeRejected(f"Runtime task {tid!r} does not exist.")
            ckpt_ref = str(task_row["checkpoint_ref"] or "").strip()
            row = None
            if ckpt_ref:
                row = conn.execute(
                    "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                    (ckpt_ref, tid),
                ).fetchone()
                if row is None:
                    raise ResumeRejected(
                        f"Task {tid!r} references missing checkpoint {ckpt_ref!r}."
                    )
            else:
                row = conn.execute(
                    """
                    SELECT * FROM runtime_task_checkpoints
                    WHERE task_id = ? AND status IN ('interrupted', 'committed', 'resumed')
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (tid,),
                ).fetchone()
            if row is None:
                return None
            verified = _decode_and_verify_checkpoint_row(row, verify_integrity=True)
            next_epoch = verified.epoch if epoch is None else int(epoch)
            conn.execute(
                """
                UPDATE runtime_task_checkpoints
                SET status = 'resumed', epoch = ?, updated_at = ?
                WHERE checkpoint_id = ?
                """,
                (next_epoch, ts, verified.checkpoint_id),
            )
            conn.execute(
                "UPDATE runtime_tasks SET checkpoint_ref = ?, updated_at = ? WHERE task_id = ?",
                (verified.checkpoint_id, ts, tid),
            )
            refreshed = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (verified.checkpoint_id,),
            ).fetchone()
            assert refreshed is not None
            return _decode_and_verify_checkpoint_row(refreshed, verify_integrity=True)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
