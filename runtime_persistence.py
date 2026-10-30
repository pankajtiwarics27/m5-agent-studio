"""SQLite schema v3 migrations and fail-closed persistence discipline (M5-P1-01).

Provides the authoritative local persistence discipline for the M5 2.0
single-agent execution spine without introducing an ORM or external migration
dependency:

1. **Additive versioned schema (`RUNTIME_SCHEMA_VERSION = 3`)**:
   - Version 1: additive columns on legacy Studio tables and ``schema_migrations``.
   - Version 2: ``tool_audit_events`` privacy-preserving tool attempt log.
   - Version 3: durable spine tables for autonomous ``runtime_tasks``,
     ``runtime_task_epochs`` (worker leases and monotonic fencing epochs),
     ``runtime_task_transitions`` (append-only state transitions), and
     ``runtime_actions`` (durable action intents, outcomes, and replay policy).
2. **Pre-migration backup and rollback protection**:
   - Pre-flight checks reject corrupted databases and future ``user_version``
     databases before mutating any bytes.
   - Upgrading an existing populated database takes an owner-only (``0600``)
     SQLite online backup first, applies DDL and ``user_version`` atomically
     under ``BEGIN IMMEDIATE``, rolls back on error, automatically restores from
     the verified snapshot if the live file's pre-migration state or integrity
     was disturbed, and unlinks ephemeral snapshots after verified completion so
     unredacted history is not silently duplicated on disk.
3. **Short write transactions and bounded busy retries**:
   - ``run_write_transaction`` enforces synchronous-only callbacks, acquires
     ``BEGIN IMMEDIATE`` with bounded busy retries and wall-clock ceilings,
     tracks active write depth via a ``ContextVar``, and always closes
     connections on exit.
   - ``assert_no_active_write_transaction`` fails closed if tool execution or
     model inference is entered while a write transaction is open.
4. **Fail-closed error classification**:
   - Busy/locked exhaustion -> ``DatabaseBusyError``
   - Disk-full / quota / read-only storage -> ``DatabaseCapacityError`` (no retry)
   - Corrupt / non-database / failed ``quick_check`` -> ``DatabaseCorruptionError`` (no retry)

Stdlib-only; inlined into standalone ``studio.py`` by ``scripts/build_studio.py``.
"""
from __future__ import annotations

import contextlib
import contextvars
import errno
import inspect
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

RUNTIME_SCHEMA_VERSION = 3

T = TypeVar("T")

_ACTIVE_WRITE_TX_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "m5_active_sqlite_write_tx_depth",
    default=0,
)

TASK_STATES: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "paused",
        "waiting_approval",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    }
)

EPOCH_STATUSES: frozenset[str] = frozenset({"active", "released", "expired", "fenced"})

ACTION_CLASSES: frozenset[str] = frozenset({"read_only", "managed_edit", "consequential", "external"})

REPLAY_POLICIES: frozenset[str] = frozenset({"idempotent", "reconcilable", "non_idempotent"})

ACTION_STATUSES: frozenset[str] = frozenset(
    {"never_started", "in_flight", "committed", "failed", "denied", "uncertain"}
)


class StorageProvider(Protocol):
    """Minimal storage connection seam compatible with ``app.SQLiteStorage``."""

    def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection: ...


class PersistenceError(sqlite3.DatabaseError):
    """Base fail-closed persistence error for authoritative SQLite state."""


class PersistenceOperationalError(PersistenceError, sqlite3.OperationalError):
    """Operational SQLite failure that is not busy, capacity, or corruption."""


class DatabaseBusyError(PersistenceError, sqlite3.OperationalError):
    """SQLite remained locked past the bounded busy-retry budget."""


class DatabaseCapacityError(PersistenceError, sqlite3.OperationalError):
    """SQLite write failed due to disk-full, quota, or read-only storage."""


class DatabaseCorruptionError(PersistenceError):
    """SQLite database failed integrity checks or is malformed/not a database."""


class SchemaMigrationError(PersistenceError, RuntimeError):
    """Schema version incompatibility or unrecoverable migration invariant error."""


class TransactionDisciplineError(PersistenceError, RuntimeError):
    """Write-transaction boundary violation (async body or spanning tool/inference)."""


@dataclass(frozen=True, slots=True)
class BusyRetryPolicy:
    """Bounded retry and lock-wait parameters for SQLite contention."""

    max_attempts: int = 4
    initial_backoff_s: float = 0.01
    max_backoff_s: float = 0.08
    max_total_wait_s: float = 0.25
    busy_timeout_ms: int = 250

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_attempts > 20:
            raise ValueError("max_attempts must be between 1 and 20.")
        if self.initial_backoff_s < 0 or self.max_backoff_s < self.initial_backoff_s:
            raise ValueError("backoff bounds must satisfy 0 <= initial_backoff_s <= max_backoff_s.")
        if self.max_total_wait_s < 0 or self.max_total_wait_s > 10.0:
            raise ValueError("max_total_wait_s must be between 0 and 10 seconds.")
        if self.busy_timeout_ms < 0 or self.busy_timeout_ms > 10_000:
            raise ValueError("busy_timeout_ms must be between 0 and 10000 ms.")


DEFAULT_BUSY_RETRY_POLICY = BusyRetryPolicy()


def _sqlite_code(exc: BaseException) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return int(code) if isinstance(code, int) else None


def is_busy_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents transient SQLite lock contention."""
    if isinstance(exc, DatabaseBusyError):
        return True
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = _sqlite_code(exc)
    busy_codes = {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }
    if code is not None and (code & 0xFF) in busy_codes:
        return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "database schema is locked" in msg


def is_capacity_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents disk-full, quota, or read-only failure."""
    if isinstance(exc, DatabaseCapacityError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC), errno.EROFS}:
            return True
    if isinstance(exc, sqlite3.DatabaseError):
        code = _sqlite_code(exc)
        full_codes = {
            getattr(sqlite3, "SQLITE_FULL", 13),
            getattr(sqlite3, "SQLITE_READONLY", 8),
            getattr(sqlite3, "SQLITE_IOERR", 10),
        }
        msg = str(exc).lower()
        if "database or disk is full" in msg or "disk i/o error" in msg or "readonly database" in msg:
            return True
        if code is not None and (code & 0xFF) in full_codes and ("locked" not in msg):
            return True
    return False


def is_corruption_error(exc: BaseException) -> bool:
    """Return True iff ``exc`` represents SQLite file corruption or invalid header."""
    if isinstance(exc, DatabaseCorruptionError):
        return True
    if isinstance(exc, sqlite3.DatabaseError):
        code = _sqlite_code(exc)
        corrupt_codes = {
            getattr(sqlite3, "SQLITE_CORRUPT", 11),
            getattr(sqlite3, "SQLITE_NOTADB", 26),
        }
        if code is not None and (code & 0xFF) in corrupt_codes:
            return True
        msg = str(exc).lower()
        if (
            "database disk image is malformed" in msg
            or "file is not a database" in msg
            or "file is encrypted or is not a database" in msg
            or "malformed database schema" in msg
        ):
            return True
    return False


def classify_persistence_error(exc: BaseException) -> BaseException:
    """Translate raw SQLite/OS storage faults into typed fail-closed errors."""
    if isinstance(exc, PersistenceError):
        return exc
    if isinstance(exc, sqlite3.IntegrityError):
        return exc
    if is_corruption_error(exc):
        return DatabaseCorruptionError(f"SQLite corruption detected (fail closed): {exc}")
    if is_capacity_error(exc):
        return DatabaseCapacityError(f"SQLite storage capacity/write failure (fail closed): {exc}")
    if is_busy_error(exc):
        return DatabaseBusyError(f"SQLite remained busy/locked (fail closed): {exc}")
    if isinstance(exc, sqlite3.OperationalError):
        return PersistenceOperationalError(str(exc))
    if isinstance(exc, sqlite3.DatabaseError):
        return PersistenceError(str(exc))
    return exc


def active_write_transaction_depth() -> int:
    """Return how many tracked SQLite write transactions are active in this context."""
    return _ACTIVE_WRITE_TX_DEPTH.get()


def assert_no_active_write_transaction(boundary_name: str) -> None:
    """Fail closed if a caller attempts tool execution or inference inside a write transaction."""
    depth = _ACTIVE_WRITE_TX_DEPTH.get()
    if depth > 0:
        raise TransactionDisciplineError(
            f"Write transaction must not span {boundary_name} (active_write_tx_depth={depth}); failing closed."
        )


@contextlib.contextmanager
def _write_transaction_scope() -> Iterator[None]:
    token = _ACTIVE_WRITE_TX_DEPTH.set(_ACTIVE_WRITE_TX_DEPTH.get() + 1)
    try:
        yield
    finally:
        _ACTIVE_WRITE_TX_DEPTH.reset(token)


_SQLITE_FILE_HEADER = b"SQLite format 3\x00"


def _verify_sqlite_file_header(db_path: Path) -> None:
    try:
        if not db_path.is_file():
            return
        size = db_path.stat().st_size
        if size == 0:
            return
        with db_path.open("rb") as handle:
            prefix = handle.read(16)
    except OSError:
        return
    if prefix != _SQLITE_FILE_HEADER:
        raise DatabaseCorruptionError(
            f"SQLite corruption detected (fail closed): invalid file header in {db_path}"
        )


def _configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")


def _reject_async_callable(fn: Any, kind: str) -> None:
    if inspect.iscoroutinefunction(fn):
        raise TransactionDisciplineError(
            f"{kind} callback must be synchronous; async callables cannot hold SQLite transactions."
        )


def _reject_awaitable_result(value: Any, conn: sqlite3.Connection, kind: str) -> None:
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        with contextlib.suppress(sqlite3.Error):
            if conn.in_transaction:
                conn.rollback()
        raise TransactionDisciplineError(
            f"{kind} callback returned an awaitable; SQLite transactions cannot span async operations."
        )


def run_read(
    storage: StorageProvider,
    db_path: Path,
    fn: Callable[[sqlite3.Connection], T],
    *,
    retry_policy: BusyRetryPolicy = DEFAULT_BUSY_RETRY_POLICY,
) -> T:
    """Execute a bounded read callback and always close the SQLite connection."""
    _reject_async_callable(fn, "Read")
    started = time.monotonic()
    backoff = retry_policy.initial_backoff_s
    last_busy: BaseException | None = None

    for attempt in range(1, retry_policy.max_attempts + 1):
        conn: sqlite3.Connection | None = None
        try:
            conn = storage.connect(db_path)
            _verify_sqlite_file_header(db_path)
            _configure_connection(conn, busy_timeout_ms=retry_policy.busy_timeout_ms)
            result = fn(conn)
            _reject_awaitable_result(result, conn, "Read")
            if conn.in_transaction:
                conn.rollback()
            return result
        except BaseException as exc:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    if conn.in_transaction:
                        conn.rollback()
            if is_corruption_error(exc) or is_capacity_error(exc):
                raise classify_persistence_error(exc) from exc
            if is_busy_error(exc):
                last_busy = exc
                remaining = retry_policy.max_total_wait_s - (time.monotonic() - started)
                if attempt < retry_policy.max_attempts and remaining > 0:
                    sleep_s = min(backoff, remaining)
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    backoff = min(retry_policy.max_backoff_s, max(backoff * 2, 0.005))
                    continue
                raise DatabaseBusyError(
                    f"SQLite read busy after {attempt} attempt(s) ({round((time.monotonic() - started) * 1000)} ms): {exc}"
                ) from exc
            mapped = classify_persistence_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    assert last_busy is not None
    raise DatabaseBusyError(f"SQLite read busy: {last_busy}") from last_busy


def run_write_transaction(
    storage: StorageProvider,
    db_path: Path,
    fn: Callable[[sqlite3.Connection], T],
    *,
    retry_policy: BusyRetryPolicy = DEFAULT_BUSY_RETRY_POLICY,
) -> T:
    """Execute a short synchronous ``BEGIN IMMEDIATE`` write transaction with bounded retries."""
    _reject_async_callable(fn, "Write-transaction")
    if _ACTIVE_WRITE_TX_DEPTH.get() > 0:
        raise TransactionDisciplineError(
            "Nested SQLite write transactions are forbidden; keep write transactions short and non-reentrant."
        )
    started = time.monotonic()
    backoff = retry_policy.initial_backoff_s
    last_busy: BaseException | None = None

    for attempt in range(1, retry_policy.max_attempts + 1):
        conn: sqlite3.Connection | None = None
        try:
            conn = storage.connect(db_path)
            _verify_sqlite_file_header(db_path)
            _configure_connection(conn, busy_timeout_ms=retry_policy.busy_timeout_ms)
            with _write_transaction_scope():
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                result = fn(conn)
                _reject_awaitable_result(result, conn, "Write-transaction")
                conn.commit()
            return result
        except BaseException as exc:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    if conn.in_transaction:
                        conn.rollback()
            if isinstance(exc, TransactionDisciplineError):
                raise
            if is_corruption_error(exc) or is_capacity_error(exc):
                raise classify_persistence_error(exc) from exc
            if is_busy_error(exc):
                last_busy = exc
                remaining = retry_policy.max_total_wait_s - (time.monotonic() - started)
                if attempt < retry_policy.max_attempts and remaining > 0:
                    sleep_s = min(backoff, remaining)
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    backoff = min(retry_policy.max_backoff_s, max(backoff * 2, 0.005))
                    continue
                raise DatabaseBusyError(
                    f"SQLite write busy after {attempt} attempt(s) ({round((time.monotonic() - started) * 1000)} ms): {exc}"
                ) from exc
            mapped = classify_persistence_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()

    assert last_busy is not None
    raise DatabaseBusyError(f"SQLite write busy: {last_busy}") from last_busy


# ---------------------------------------------------------------------------
# Additive versioned schema catalog (v1 -> v2 -> v3)
# ---------------------------------------------------------------------------

LEGACY_COLUMN_ADDITIONS: dict[str, dict[str, str]] = {
    "runs": {
        "created_at": "REAL NOT NULL DEFAULT 0",
        "mode": "TEXT NOT NULL DEFAULT ''",
        "title": "TEXT NOT NULL DEFAULT ''",
        "payload_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "perf_samples": {
        "created_at": "REAL NOT NULL DEFAULT 0",
        "mode": "TEXT NOT NULL DEFAULT ''",
        "model": "TEXT NOT NULL DEFAULT ''",
        "decode_tps": "REAL",
        "prefill_tps": "REAL",
        "ttft": "REAL",
        "completion_tokens": "INTEGER",
        "cached_tokens": "INTEGER",
    },
    "hall_sessions": {
        "title": "TEXT NOT NULL DEFAULT ''",
        "request_text": "TEXT NOT NULL DEFAULT ''",
        "created_by": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "context_files_json": "TEXT NOT NULL DEFAULT '[]'",
        "conclusion_author": "TEXT",
        "conclusion_text": "TEXT",
        "conclusion_at": "REAL",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "hall_messages": {
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "author": "TEXT NOT NULL DEFAULT ''",
        "role_key": "TEXT NOT NULL DEFAULT ''",
        "model": "TEXT",
        "content": "TEXT NOT NULL DEFAULT ''",
        "reasoning": "TEXT DEFAULT ''",
        "usage_json": "TEXT DEFAULT '{}'",
        "created_at": "REAL NOT NULL DEFAULT 0",
    },
    "cloud_providers": {
        "name": "TEXT NOT NULL DEFAULT ''",
        "wire": "TEXT NOT NULL DEFAULT 'openai'",
        "base_url": "TEXT NOT NULL DEFAULT ''",
        "api_key": "TEXT NOT NULL DEFAULT ''",
        "models_json": "TEXT NOT NULL DEFAULT '[]'",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "model_combos": {
        "name": "TEXT NOT NULL DEFAULT ''",
        "targets_json": "TEXT NOT NULL DEFAULT '[]'",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "harness_sessions": {
        "title": "TEXT NOT NULL DEFAULT ''",
        "workspace_dir": "TEXT NOT NULL DEFAULT ''",
        "worktree_dir": "TEXT NOT NULL DEFAULT ''",
        "branch_name": "TEXT NOT NULL DEFAULT ''",
        "use_worktree": "INTEGER NOT NULL DEFAULT 0",
        "policy": "TEXT NOT NULL DEFAULT 'standard'",
        "lead_model": "TEXT NOT NULL DEFAULT ''",
        "critic_model": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'idle'",
        "step_count": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "REAL NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "mcp_servers": {
        "name": "TEXT NOT NULL DEFAULT ''",
        "category": "TEXT NOT NULL DEFAULT 'cloud'",
        "transport": "TEXT NOT NULL DEFAULT 'http'",
        "url": "TEXT NOT NULL DEFAULT ''",
        "auth_header": "TEXT NOT NULL DEFAULT 'Authorization'",
        "auth_token": "TEXT NOT NULL DEFAULT ''",
        "headers_json": "TEXT NOT NULL DEFAULT '{}'",
        "command": "TEXT NOT NULL DEFAULT ''",
        "args_json": "TEXT NOT NULL DEFAULT '[]'",
        "env_json": "TEXT NOT NULL DEFAULT '{}'",
        "approval_policy": "TEXT NOT NULL DEFAULT 'inherit'",
        "tools_json": "TEXT NOT NULL DEFAULT '[]'",
        "enabled_tools_json": "TEXT NOT NULL DEFAULT '[]'",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "last_sync_at": "REAL DEFAULT 0",
        "last_error": "TEXT DEFAULT ''",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "opencontext_docs": {
        "folder": "TEXT NOT NULL DEFAULT 'decisions'",
        "title": "TEXT NOT NULL DEFAULT ''",
        "description": "TEXT NOT NULL DEFAULT ''",
        "content": "TEXT NOT NULL DEFAULT ''",
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
    "ctx_vault_chunks": {
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "doc_id": "TEXT NOT NULL DEFAULT ''",
        "source_tool": "TEXT NOT NULL DEFAULT ''",
        "chunk_idx": "INTEGER NOT NULL DEFAULT 0",
        "content": "TEXT NOT NULL DEFAULT ''",
        "created_at": "REAL NOT NULL DEFAULT 0",
    },
    "ctx_session_metrics": {
        "raw_bytes": "INTEGER NOT NULL DEFAULT 0",
        "sent_bytes": "INTEGER NOT NULL DEFAULT 0",
        "compactions": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "REAL NOT NULL DEFAULT 0",
    },
}


def _apply_v1_legacy_columns(conn: sqlite3.Connection) -> None:
    for table, columns in LEGACY_COLUMN_ADDITIONS.items():
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if not existing:
            continue
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def _apply_v2_tool_audit_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            policy TEXT NOT NULL,
            explicit_approval INTEGER NOT NULL,
            args_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            ok INTEGER,
            elapsed_ms INTEGER,
            output_sha256 TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _apply_v3_execution_spine_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_tasks (
            task_id TEXT PRIMARY KEY CHECK (length(trim(task_id)) > 0),
            parent_task_id TEXT REFERENCES runtime_tasks(task_id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'paused', 'waiting_approval', 'blocked', 'completed', 'failed', 'cancelled')),
            current_epoch INTEGER NOT NULL DEFAULT 0 CHECK (current_epoch >= 0),
            state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
            workspace_dir TEXT NOT NULL DEFAULT '',
            policy TEXT NOT NULL DEFAULT 'standard',
            budget_json TEXT NOT NULL DEFAULT '{}',
            checkpoint_ref TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_task_epochs (
            task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id) ON DELETE RESTRICT,
            epoch INTEGER NOT NULL CHECK (epoch >= 1),
            worker_id TEXT NOT NULL CHECK (length(trim(worker_id)) > 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'released', 'expired', 'fenced')),
            claimed_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL,
            lease_expires_at REAL NOT NULL,
            fenced_at REAL,
            fence_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (task_id, epoch)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_task_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES runtime_tasks(task_id) ON DELETE RESTRICT,
            seq INTEGER NOT NULL CHECK (seq >= 1),
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL
                CHECK (to_state IN ('pending', 'running', 'paused', 'waiting_approval', 'blocked', 'completed', 'failed', 'cancelled')),
            epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
            reason TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            UNIQUE (task_id, seq)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_actions (
            action_id TEXT PRIMARY KEY CHECK (length(trim(action_id)) > 0),
            task_id TEXT REFERENCES runtime_tasks(task_id) ON DELETE RESTRICT,
            session_id TEXT NOT NULL DEFAULT '',
            epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            action_class TEXT NOT NULL DEFAULT 'consequential'
                CHECK (action_class IN ('read_only', 'managed_edit', 'consequential', 'external')),
            replay_policy TEXT NOT NULL DEFAULT 'non_idempotent'
                CHECK (replay_policy IN ('idempotent', 'reconcilable', 'non_idempotent')),
            tool_name TEXT NOT NULL CHECK (length(trim(tool_name)) > 0),
            policy TEXT NOT NULL,
            explicit_approval INTEGER NOT NULL DEFAULT 0 CHECK (explicit_approval IN (0, 1)),
            args_sha256 TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('never_started', 'in_flight', 'committed', 'failed', 'denied', 'uncertain')),
            ok INTEGER CHECK (ok IS NULL OR ok IN (0, 1)),
            elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
            output_sha256 TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_tasks_session_state ON runtime_tasks(session_id, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_tasks_parent ON runtime_tasks(parent_task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_task_epochs_active ON runtime_task_epochs(task_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_task_transitions_task ON runtime_task_transitions(task_id, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_actions_task_status ON runtime_actions(task_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_actions_session ON runtime_actions(session_id, created_at)"
    )


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """One additive, idempotent SQLite schema upgrade step."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATION_STEPS: tuple[MigrationStep, ...] = (
    MigrationStep(1, "v1_legacy_additive_columns", _apply_v1_legacy_columns),
    MigrationStep(2, "v2_tool_audit_events", _apply_v2_tool_audit_events),
    MigrationStep(3, "v3_runtime_execution_spine", _apply_v3_execution_spine_tables),
)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _apply_migrations_locked(
    conn: sqlite3.Connection,
    *,
    current_version: int,
    target_version: int = RUNTIME_SCHEMA_VERSION,
    fault_hook: Callable[[int, sqlite3.Connection], None] | None = None,
) -> int:
    """Run all additive migration steps up to ``target_version`` inside an active transaction."""
    if current_version > target_version:
        raise SchemaMigrationError(
            f"Database schema version {current_version} is newer than supported version {target_version}."
        )
    # Always reconcile additive legacy columns first so partial pre-v1 tables upgrade cleanly.
    _apply_v1_legacy_columns(conn)
    _ensure_migrations_table(conn)
    for step in MIGRATION_STEPS:
        if step.version > target_version:
            continue
        step.apply(conn)
        if fault_hook is not None:
            fault_hook(step.version, conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (step.version,))
    if current_version < target_version:
        conn.execute(f"PRAGMA user_version = {int(target_version)}")
    return target_version


def apply_schema_migrations(
    conn: sqlite3.Connection,
    *,
    target_version: int = RUNTIME_SCHEMA_VERSION,
    fault_hook: Callable[[int, sqlite3.Connection], None] | None = None,
) -> int:
    """Upgrade ``conn`` transactionally under ``BEGIN IMMEDIATE``; roll back on any failure."""
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        current = int(row[0]) if row else 0
    except BaseException as exc:
        mapped = classify_persistence_error(exc)
        if mapped is exc:
            raise
        raise mapped from exc

    if current > target_version:
        raise SchemaMigrationError(
            f"Database schema version {current} is newer than supported version {target_version}."
        )

    try:
        with _write_transaction_scope():
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            _apply_migrations_locked(
                conn,
                current_version=current,
                target_version=target_version,
                fault_hook=fault_hook,
            )
            conn.commit()
        return target_version
    except BaseException as exc:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        mapped = classify_persistence_error(exc)
        if mapped is exc:
            raise
        raise mapped from exc


def _verify_sqlite_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA quick_check").fetchone()
    if not row or str(row[0]).lower() != "ok":
        detail = str(row[0]) if row else "empty quick_check result"
        raise DatabaseCorruptionError(f"SQLite quick_check failed (fail closed): {detail}")


def _set_owner_only(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists():
            path.chmod(0o600)


def _create_online_snapshot(source_conn: sqlite3.Connection, directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_name = tempfile.mkstemp(prefix=prefix, suffix=".sqlite3.tmp", dir=directory)
    os.close(fd)
    snapshot = Path(raw_name)
    try:
        snapshot.chmod(0o600)
        with contextlib.closing(sqlite3.connect(snapshot, timeout=10.0)) as dst:
            source_conn.backup(dst, pages=256, sleep=0.01)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
            _verify_sqlite_integrity(dst)
        with snapshot.open("rb") as handle:
            os.fsync(handle.fileno())
        return snapshot
    except BaseException as exc:
        snapshot.unlink(missing_ok=True)
        mapped = classify_persistence_error(exc)
        if mapped is exc:
            raise
        raise mapped from exc


def _restore_snapshot_to_live(snapshot: Path, live_db: Path) -> None:
    fd, raw_stage = tempfile.mkstemp(prefix=f".{live_db.name}.restore-", suffix=".tmp", dir=live_db.parent)
    os.close(fd)
    staged = Path(raw_stage)
    try:
        staged.chmod(0o600)
        with (
            contextlib.closing(sqlite3.connect(snapshot, timeout=10.0)) as src,
            contextlib.closing(sqlite3.connect(staged, timeout=10.0)) as dst,
        ):
            src.backup(dst, pages=256, sleep=0.01)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
            _verify_sqlite_integrity(dst)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        for suffix in ("-wal", "-shm"):
            Path(f"{live_db}{suffix}").unlink(missing_ok=True)
        os.replace(staged, live_db)
        _set_owner_only(live_db)
    finally:
        staged.unlink(missing_ok=True)


def upgrade_database(
    storage: StorageProvider,
    db_path: Path,
    *,
    base_ddl: Callable[[sqlite3.Connection], None] | None = None,
    target_version: int = RUNTIME_SCHEMA_VERSION,
    backup_path: Path | None = None,
    retry_policy: BusyRetryPolicy = DEFAULT_BUSY_RETRY_POLICY,
    fault_hook: Callable[[int, sqlite3.Connection], None] | None = None,
) -> int:
    """Initialize or upgrade ``db_path`` with pre-flight checks, ``0600`` backup, and rollback/restore."""
    if backup_path is not None and backup_path.expanduser().resolve() == db_path.expanduser().resolve():
        raise SchemaMigrationError("Pre-migration backup_path must differ from db_path.")

    file_existed = False
    try:
        file_existed = db_path.is_file() and db_path.stat().st_size > 0
    except OSError:
        file_existed = False

    pre_version = 0
    pre_tables: set[str] = set()
    snapshot: Path | None = None

    if file_existed:
        conn_check: sqlite3.Connection | None = None
        try:
            conn_check = storage.connect(db_path)
            _configure_connection(conn_check, busy_timeout_ms=retry_policy.busy_timeout_ms)
            _verify_sqlite_integrity(conn_check)
            v_row = conn_check.execute("PRAGMA user_version").fetchone()
            pre_version = int(v_row[0]) if v_row else 0
            if pre_version > target_version:
                raise SchemaMigrationError(
                    f"Database schema version {pre_version} is newer than supported version {target_version}."
                )
            pre_tables = {
                str(r[0])
                for r in conn_check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            needs_snapshot = (bool(pre_tables) and pre_version < target_version) or (backup_path is not None)
            if needs_snapshot:
                snap_dir = backup_path.parent if backup_path is not None else db_path.parent
                snapshot = _create_online_snapshot(
                    conn_check,
                    snap_dir,
                    prefix=f".{db_path.name}.pre-v{target_version}-",
                )
        except BaseException as exc:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)
            mapped = classify_persistence_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            if conn_check is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn_check.close()

    def _upgrade_tx(conn: sqlite3.Connection) -> int:
        _verify_sqlite_integrity(conn)
        v_row = conn.execute("PRAGMA user_version").fetchone()
        current = int(v_row[0]) if v_row else 0
        if current > target_version:
            raise SchemaMigrationError(
                f"Database schema version {current} is newer than supported version {target_version}."
            )
        if base_ddl is not None:
            base_ddl(conn)
        return _apply_migrations_locked(
            conn,
            current_version=current,
            target_version=target_version,
            fault_hook=fault_hook,
        )

    try:
        # Configure WAL mode outside BEGIN IMMEDIATE once before entering the atomic DDL transaction.
        wal_conn: sqlite3.Connection | None = None
        try:
            wal_conn = storage.connect(db_path)
            _configure_connection(wal_conn, busy_timeout_ms=retry_policy.busy_timeout_ms)
            _verify_sqlite_integrity(wal_conn)
            v_row = wal_conn.execute("PRAGMA user_version").fetchone()
            current_wal = int(v_row[0]) if v_row else 0
            if current_wal > target_version:
                raise SchemaMigrationError(
                    f"Database schema version {current_wal} is newer than supported version {target_version}."
                )
            wal_conn.execute("PRAGMA journal_mode = WAL")
            wal_conn.execute("PRAGMA synchronous = NORMAL")
        except BaseException as exc:
            mapped = classify_persistence_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
        finally:
            if wal_conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    wal_conn.close()

        applied_version = run_write_transaction(
            storage,
            db_path,
            _upgrade_tx,
            retry_policy=retry_policy,
        )
    except BaseException:
        if snapshot is not None:
            live_intact = False
            if db_path.is_file():
                try:
                    with contextlib.closing(sqlite3.connect(db_path, timeout=5.0)) as verify_conn:
                        _verify_sqlite_integrity(verify_conn)
                        after_v = int(verify_conn.execute("PRAGMA user_version").fetchone()[0])
                        after_tables = {
                            str(r[0])
                            for r in verify_conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            ).fetchall()
                        }
                        live_intact = (after_v == pre_version) and (after_tables == pre_tables)
                except sqlite3.Error:
                    live_intact = False
            if not live_intact and db_path.parent.is_dir():
                with contextlib.suppress(OSError, sqlite3.Error):
                    _restore_snapshot_to_live(snapshot, db_path)
                    live_intact = True
            if backup_path is not None:
                with contextlib.suppress(OSError):
                    os.replace(snapshot, backup_path)
                    _set_owner_only(backup_path)
            elif live_intact:
                snapshot.unlink(missing_ok=True)
        raise

    for protected in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        _set_owner_only(protected)

    if snapshot is not None:
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(snapshot, backup_path)
            _set_owner_only(backup_path)
        else:
            snapshot.unlink(missing_ok=True)

    return applied_version


# ---------------------------------------------------------------------------
# Typed durable spine records and short-transaction store (M5-P1-01)
# ---------------------------------------------------------------------------


def _non_empty_text(value: str, field: str, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string without NUL characters.")
    cleaned = value.strip()
    if len(cleaned) > max_len:
        raise ValueError(f"{field} exceeds maximum length ({max_len}).")
    return cleaned


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Durable autonomous root/child task record."""

    task_id: str
    parent_task_id: str | None
    session_id: str
    title: str
    state: str
    current_epoch: int
    state_version: int
    workspace_dir: str
    policy: str
    budget_json: str
    checkpoint_ref: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class TaskEpochRecord:
    """Durable worker lease and monotonic fencing epoch record."""

    task_id: str
    epoch: int
    worker_id: str
    status: str
    claimed_at: float
    heartbeat_at: float
    lease_expires_at: float
    fenced_at: float | None
    fence_reason: str


@dataclass(frozen=True, slots=True)
class TaskTransitionRecord:
    """Append-only durable state transition entry for a task."""

    id: int
    task_id: str
    seq: int
    from_state: str
    to_state: str
    epoch: int
    reason: str
    evidence_json: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ActionRecord:
    """Durable consequential action intent and outcome record."""

    action_id: str
    task_id: str | None
    session_id: str
    epoch: int
    attempt: int
    action_class: str
    replay_policy: str
    tool_name: str
    policy: str
    explicit_approval: bool
    args_sha256: str
    status: str
    ok: bool | None
    elapsed_ms: int | None
    output_sha256: str
    error_code: str
    created_at: float
    updated_at: float


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        parent_task_id=str(row["parent_task_id"]) if row["parent_task_id"] is not None else None,
        session_id=str(row["session_id"]),
        title=str(row["title"]),
        state=str(row["state"]),
        current_epoch=int(row["current_epoch"]),
        state_version=int(row["state_version"]),
        workspace_dir=str(row["workspace_dir"]),
        policy=str(row["policy"]),
        budget_json=str(row["budget_json"]),
        checkpoint_ref=str(row["checkpoint_ref"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _row_to_epoch(row: sqlite3.Row) -> TaskEpochRecord:
    return TaskEpochRecord(
        task_id=str(row["task_id"]),
        epoch=int(row["epoch"]),
        worker_id=str(row["worker_id"]),
        status=str(row["status"]),
        claimed_at=float(row["claimed_at"]),
        heartbeat_at=float(row["heartbeat_at"]),
        lease_expires_at=float(row["lease_expires_at"]),
        fenced_at=float(row["fenced_at"]) if row["fenced_at"] is not None else None,
        fence_reason=str(row["fence_reason"]),
    )


def _row_to_transition(row: sqlite3.Row) -> TaskTransitionRecord:
    return TaskTransitionRecord(
        id=int(row["id"]),
        task_id=str(row["task_id"]),
        seq=int(row["seq"]),
        from_state=str(row["from_state"]),
        to_state=str(row["to_state"]),
        epoch=int(row["epoch"]),
        reason=str(row["reason"]),
        evidence_json=str(row["evidence_json"]),
        created_at=float(row["created_at"]),
    )


def _row_to_action(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        action_id=str(row["action_id"]),
        task_id=str(row["task_id"]) if row["task_id"] is not None else None,
        session_id=str(row["session_id"]),
        epoch=int(row["epoch"]),
        attempt=int(row["attempt"]),
        action_class=str(row["action_class"]),
        replay_policy=str(row["replay_policy"]),
        tool_name=str(row["tool_name"]),
        policy=str(row["policy"]),
        explicit_approval=bool(row["explicit_approval"]),
        args_sha256=str(row["args_sha256"]),
        status=str(row["status"]),
        ok=bool(row["ok"]) if row["ok"] is not None else None,
        elapsed_ms=int(row["elapsed_ms"]) if row["elapsed_ms"] is not None else None,
        output_sha256=str(row["output_sha256"]),
        error_code=str(row["error_code"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class RuntimePersistenceStore:
    """Short-transaction SQLite repository for tasks, transitions, actions, and epochs."""

    def __init__(
        self,
        storage: StorageProvider,
        db_path: Path,
        *,
        retry_policy: BusyRetryPolicy = DEFAULT_BUSY_RETRY_POLICY,
    ) -> None:
        self._storage = storage
        self._db_path = db_path
        self._retry_policy = retry_policy

    def create_task(
        self,
        task_id: str,
        *,
        session_id: str = "",
        parent_task_id: str | None = None,
        title: str = "",
        state: str = "pending",
        workspace_dir: str = "",
        policy: str = "standard",
        budget_json: str = "{}",
        checkpoint_ref: str = "",
        now: float | None = None,
    ) -> TaskRecord:
        tid = _non_empty_text(task_id, "task_id", max_len=128)
        parent = _non_empty_text(parent_task_id, "parent_task_id", max_len=128) if parent_task_id else None
        if state not in TASK_STATES:
            raise ValueError(f"Unsupported task state: {state!r}")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskRecord:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, parent_task_id, session_id, title, state,
                    current_epoch, state_version, workspace_dir, policy,
                    budget_json, checkpoint_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    parent,
                    str(session_id)[:128],
                    str(title)[:240],
                    state,
                    str(workspace_dir),
                    str(policy)[:40],
                    str(budget_json or "{}"),
                    str(checkpoint_ref)[:256],
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert row is not None
            return _row_to_task(row)

        return run_write_transaction(self._storage, self._db_path, _tx, retry_policy=self._retry_policy)

    def get_task(self, task_id: str) -> TaskRecord | None:
        tid = _non_empty_text(task_id, "task_id", max_len=128)

        def _read(conn: sqlite3.Connection) -> TaskRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            return _row_to_task(row) if row is not None else None

        return run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def claim_epoch(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_ttl_s: float = 30.0,
        fence_reason: str = "superseded_by_new_epoch",
        now: float | None = None,
    ) -> TaskEpochRecord:
        tid = _non_empty_text(task_id, "task_id", max_len=128)
        wid = _non_empty_text(worker_id, "worker_id", max_len=128)
        if lease_ttl_s <= 0:
            raise ValueError("lease_ttl_s must be positive.")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskEpochRecord:
            conn.row_factory = sqlite3.Row
            task_row = conn.execute("SELECT current_epoch FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if task_row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            next_epoch = int(task_row["current_epoch"]) + 1
            conn.execute(
                """
                UPDATE runtime_task_epochs
                SET status = 'fenced', fenced_at = ?, fence_reason = ?
                WHERE task_id = ? AND status = 'active'
                """,
                (ts, str(fence_reason)[:160], tid),
            )
            conn.execute(
                "UPDATE runtime_tasks SET current_epoch = ?, updated_at = ? WHERE task_id = ?",
                (next_epoch, ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_epochs (
                    task_id, epoch, worker_id, status, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (tid, next_epoch, wid, ts, ts, ts + lease_ttl_s),
            )
            row = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (tid, next_epoch),
            ).fetchone()
            assert row is not None
            return _row_to_epoch(row)

        return run_write_transaction(self._storage, self._db_path, _tx, retry_policy=self._retry_policy)

    def get_epoch(self, task_id: str, epoch: int) -> TaskEpochRecord | None:
        tid = _non_empty_text(task_id, "task_id", max_len=128)

        def _read(conn: sqlite3.Connection) -> TaskEpochRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (tid, int(epoch)),
            ).fetchone()
            return _row_to_epoch(row) if row is not None else None

        return run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def record_transition(
        self,
        task_id: str,
        *,
        to_state: str,
        expected_state_version: int | None = None,
        epoch: int | None = None,
        reason: str = "",
        evidence_json: str = "{}",
        checkpoint_ref: str | None = None,
        now: float | None = None,
    ) -> tuple[TaskRecord, TaskTransitionRecord]:
        tid = _non_empty_text(task_id, "task_id", max_len=128)
        if to_state not in TASK_STATES:
            raise ValueError(f"Unsupported target task state: {to_state!r}")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> tuple[TaskRecord, TaskTransitionRecord]:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current_version = int(row["state_version"])
            if expected_state_version is not None and current_version != expected_state_version:
                raise ValueError(
                    f"Task {tid!r} state_version mismatch (expected {expected_state_version}, actual {current_version})."
                )
            current_epoch = int(row["current_epoch"])
            if epoch is not None and epoch != current_epoch:
                raise PermissionError(
                    f"Stale task epoch {epoch} cannot transition task {tid!r} at current_epoch={current_epoch}."
                )
            effective_epoch = current_epoch if epoch is None else int(epoch)
            from_state = str(row["state"])
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            new_checkpoint = str(row["checkpoint_ref"]) if checkpoint_ref is None else str(checkpoint_ref)[:256]
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, state_version = state_version + 1, checkpoint_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (to_state, new_checkpoint, ts, tid),
            )
            cur = conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, to_state, effective_epoch, str(reason)[:240], str(evidence_json or "{}"), ts),
            )
            updated_task = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            tr_row = conn.execute(
                "SELECT * FROM runtime_task_transitions WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            assert updated_task is not None and tr_row is not None
            return _row_to_task(updated_task), _row_to_transition(tr_row)

        return run_write_transaction(self._storage, self._db_path, _tx, retry_policy=self._retry_policy)

    def list_transitions(self, task_id: str) -> list[TaskTransitionRecord]:
        tid = _non_empty_text(task_id, "task_id", max_len=128)

        def _read(conn: sqlite3.Connection) -> list[TaskTransitionRecord]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM runtime_task_transitions WHERE task_id = ? ORDER BY seq ASC",
                (tid,),
            ).fetchall()
            return [_row_to_transition(r) for r in rows]

        return run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def record_action_intent(
        self,
        action_id: str,
        *,
        tool_name: str,
        policy: str,
        args_sha256: str,
        task_id: str | None = None,
        session_id: str = "",
        epoch: int = 0,
        attempt: int = 1,
        action_class: str = "consequential",
        replay_policy: str = "non_idempotent",
        explicit_approval: bool = False,
        status: str = "in_flight",
        now: float | None = None,
    ) -> ActionRecord:
        aid = _non_empty_text(action_id, "action_id", max_len=128)
        tid = _non_empty_text(task_id, "task_id", max_len=128) if task_id else None
        tname = _non_empty_text(tool_name, "tool_name", max_len=200)
        pol = _non_empty_text(policy, "policy", max_len=40)
        digest = _non_empty_text(args_sha256, "args_sha256", max_len=128)
        if action_class not in ACTION_CLASSES:
            raise ValueError(f"Unsupported action_class: {action_class!r}")
        if replay_policy not in REPLAY_POLICIES:
            raise ValueError(f"Unsupported replay_policy: {replay_policy!r}")
        if status not in {"never_started", "in_flight", "denied"}:
            raise ValueError(f"Invalid initial action status: {status!r}")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> ActionRecord:
            conn.row_factory = sqlite3.Row
            if tid is not None:
                task_row = conn.execute(
                    "SELECT current_epoch FROM runtime_tasks WHERE task_id = ?",
                    (tid,),
                ).fetchone()
                if task_row is None:
                    raise KeyError(f"Runtime task {tid!r} does not exist.")
                if epoch > 0 and int(task_row["current_epoch"]) != int(epoch):
                    raise PermissionError(
                        f"Stale epoch {epoch} cannot record action intent for task {tid!r} (current_epoch={task_row['current_epoch']})."
                    )
            conn.execute(
                """
                INSERT INTO runtime_actions (
                    action_id, task_id, session_id, epoch, attempt, action_class,
                    replay_policy, tool_name, policy, explicit_approval, args_sha256,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    tid,
                    str(session_id)[:128],
                    int(epoch),
                    int(attempt),
                    action_class,
                    replay_policy,
                    tname,
                    pol,
                    int(bool(explicit_approval)),
                    digest,
                    status,
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            assert row is not None
            return _row_to_action(row)

        return run_write_transaction(self._storage, self._db_path, _tx, retry_policy=self._retry_policy)

    def finalize_action(
        self,
        action_id: str,
        *,
        status: str,
        ok: bool | None,
        elapsed_ms: int | None,
        output_sha256: str = "",
        error_code: str = "",
        now: float | None = None,
    ) -> ActionRecord:
        aid = _non_empty_text(action_id, "action_id", max_len=128)
        if status not in {"committed", "failed", "denied", "uncertain"}:
            raise ValueError(f"Unsupported terminal/reconciled action status: {status!r}")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> ActionRecord:
            conn.row_factory = sqlite3.Row
            existing = conn.execute("SELECT status FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if existing is None:
                raise KeyError(f"Runtime action {aid!r} does not exist.")
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = ?, ok = ?, elapsed_ms = ?, output_sha256 = ?, error_code = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (
                    status,
                    (int(bool(ok)) if ok is not None else None),
                    (max(0, int(elapsed_ms)) if elapsed_ms is not None else None),
                    str(output_sha256)[:128],
                    str(error_code)[:120],
                    ts,
                    aid,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            assert row is not None
            return _row_to_action(row)

        return run_write_transaction(self._storage, self._db_path, _tx, retry_policy=self._retry_policy)

    def get_action(self, action_id: str) -> ActionRecord | None:
        aid = _non_empty_text(action_id, "action_id", max_len=128)

        def _read(conn: sqlite3.Connection) -> ActionRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            return _row_to_action(row) if row is not None else None

        return run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)
