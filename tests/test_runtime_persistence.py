"""Tests for M5-P1-01: SQLite schema v3 migration and persistence discipline.

Covers:
1. Legacy (v0, v1, v2) database upgrades preserving existing relational + FTS5 data.
2. Interrupted migration rollback and automatic 0600 backup-snapshot recovery.
3. Durable task, epoch, transition, and action invariants in RuntimePersistenceStore.
4. Short write-transaction enforcement: no write transaction spans tool execution,
   model inference, subprocess launch, async coroutines, nested writes, or OS keyring I/O.
5. Fail-closed handling of SQLITE_BUSY, SQLITE_FULL/capacity exhaustion, and
   SQLITE_CORRUPT/malformed files with bounded retries only for lock contention.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import app as studio
import context_store
import execution_spine
import history_privacy
import runtime_persistence as rp
from scripts.db_maintenance import _validate_database, backup_database, restore_database


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, item: str, value: str) -> None:
        self.values[(service, item)] = value

    def get_password(self, service: str, item: str) -> str | None:
        return self.values.get((service, item))

    def delete_password(self, service: str, item: str) -> None:
        self.values.pop((service, item), None)


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> _MemoryKeyring:
    backend = _MemoryKeyring()
    monkeypatch.setattr(studio.keyring, "set_password", backend.set_password)
    monkeypatch.setattr(studio.keyring, "get_password", backend.get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", backend.delete_password)
    return backend


class _LocalStorage:
    def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
        return sqlite3.connect(database, timeout=timeout)


# =============================================================================
# 1. Existing Data Survives Migration (v0 / v1 / v2 -> v3 + FTS5 + Privacy)
# =============================================================================


def test_v0_legacy_database_survives_upgrade_to_v3_with_fts5_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: _MemoryKeyring,
) -> None:
    db_path = tmp_path / "legacy-v0.db"
    monkeypatch.setattr(studio, "DB_PATH", db_path)

    # Simulate an early v0 database missing newer columns across multiple tables
    # while already holding user runs, providers, sessions, and FTS5 content.
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL)")
        conn.execute("INSERT INTO runs(title) VALUES ('legacy-v0-run')")
        conn.execute(
            "CREATE TABLE cloud_providers("
            "alias TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cloud_providers(alias, name, base_url, updated_at) "
            "VALUES ('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', 12.5)"
        )
        conn.execute(
            "CREATE TABLE harness_sessions("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace_dir TEXT NOT NULL, "
            "worktree_dir TEXT NOT NULL, lead_model TEXT NOT NULL, critic_model TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO harness_sessions("
            "id, title, workspace_dir, worktree_dir, lead_model, critic_model, created_at, updated_at) "
            "VALUES ('hns-legacy', 'Legacy Harness', '/tmp/ws', '/tmp/ws', 'lead-a', 'critic-b', 1.0, 2.0)"
        )
        conn.execute(
            "CREATE TABLE opencontext_docs("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE opencontext_fts "
            "USING fts5(doc_id UNINDEXED, folder, title, description, content, tokenize='porter unicode61')"
        )
        conn.execute(
            "INSERT INTO opencontext_docs(id, title, content, updated_at) "
            "VALUES ('oc-legacy', 'Thermal Headroom Note', 'Keep unified memory safe under load', 5.0)"
        )
        conn.execute(
            "INSERT INTO opencontext_fts(doc_id, folder, title, description, content) "
            "VALUES ('oc-legacy', 'decisions', 'Thermal Headroom Note', 'Legacy note', "
            "'Keep unified memory safe under load')"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    studio.init_db()
    # Second call verifies idempotency on an already-upgraded v3 database.
    studio.init_db()

    # Ephemeral pre-migration backup snapshot must be cleaned up after verified commit.
    assert list(tmp_path.glob("*.pre-v3-*")) == []
    if os.name == "posix":
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == studio.SCHEMA_VERSION == 3
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
        ]
        run_row = conn.execute("SELECT title, mode, payload_json FROM runs WHERE id = 1").fetchone()
        assert run_row == ("legacy-v0-run", "", "{}")
        prov_row = conn.execute(
            "SELECT name, wire, enabled FROM cloud_providers WHERE alias = 'openrouter'"
        ).fetchone()
        assert prov_row == ("OpenRouter", "openai", 1)
        sess_row = conn.execute(
            "SELECT title, policy, status, step_count FROM harness_sessions WHERE id = 'hns-legacy'"
        ).fetchone()
        assert sess_row == ("Legacy Harness", "standard", "idle", 0)
        for spine_table in (
            "runtime_tasks",
            "runtime_task_epochs",
            "runtime_task_transitions",
            "runtime_actions",
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (spine_table,),
            ).fetchone()
            assert exists is not None, f"Missing spine table {spine_table}"

    hits = context_store.search_docs(studio.STORAGE, db_path, "unified memory")
    assert any(doc["id"] == "oc-legacy" for doc in hits)


def test_v2_database_upgrades_to_v3_preserving_audit_history_and_backup_contracts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "production-v2.db"
    storage = _LocalStorage()

    # Construct a realistic Schema v2 database with user data and audit trail.
    with sqlite3.connect(db_path) as conn:
        studio._ensure_base_tables(conn)
        rp.apply_schema_migrations(conn, target_version=2)
        conn.execute(
            "INSERT INTO runs(created_at, mode, title, payload_json) VALUES (100.0, 'chat', 'v2-chat', '{\"ok\":true}')"
        )
        conn.execute(
            "INSERT INTO hall_sessions(id, title, request_text, created_by, status, context_files_json, created_at, updated_at) "
            "VALUES ('ch-v2', 'Architecture Review', 'Check WAL bounds', 'Operator', 'active', '[]', 100.0, 101.0)"
        )
        conn.execute(
            "INSERT INTO hall_messages(session_id, author, role_key, model, content, reasoning, usage_json, created_at) "
            "VALUES ('ch-v2', 'Operator', 'human', NULL, 'Check WAL bounds', '', '{}', 100.0)"
        )
        conn.execute(
            "INSERT INTO tool_audit_events(created_at, session_id, tool_name, policy, explicit_approval, args_sha256, status, ok, elapsed_ms, output_sha256) "
            "VALUES (102.0, 'hns-v2', 'read_file', 'standard', 1, 'abc123', 'completed', 1, 5, 'def456')"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

    final_version = rp.upgrade_database(
        storage,
        db_path,
        base_ddl=studio._ensure_base_tables,
        target_version=3,
    )
    assert final_version == 3

    store = rp.RuntimePersistenceStore(storage, db_path)
    task = store.create_task(
        "tsk-after-v2-upgrade",
        session_id="hns-v2",
        title="Verify upgraded spine",
        budget_json=json.dumps({"max_steps": 8}),
    )
    assert task.state == "pending"
    epoch_rec = store.claim_epoch("tsk-after-v2-upgrade", "worker-1")
    assert epoch_rec.epoch == 1

    # Verify history privacy export keeps v2 user data while excluding runtime_* spine tables.
    with sqlite3.connect(db_path) as conn:
        exported = history_privacy.export_history(conn)
    assert len(exported["tables"]["runs"]) == 1
    assert exported["tables"]["runs"][0]["title"] == "v2-chat"
    assert len(exported["tables"]["hall_sessions"]) == 1
    for spine_table in (
        "runtime_tasks",
        "runtime_task_epochs",
        "runtime_task_transitions",
        "runtime_actions",
    ):
        assert spine_table not in exported["tables"]
        assert spine_table in exported["excluded_tables"]["security_and_integrity_trails"]

    # Verify offline db_maintenance backup/verify/restore supports Schema v3.
    backup_file = tmp_path / "v3-backup.sqlite3"
    backup_database(db_path, backup_file)
    assert _validate_database(backup_file) == 3
    restored_target = tmp_path / "restored-v3.db"
    restore_database(backup_file, restored_target)
    restored_store = rp.RuntimePersistenceStore(storage, restored_target)
    restored_task = restored_store.get_task("tsk-after-v2-upgrade")
    assert restored_task is not None and restored_task.title == "Verify upgraded spine"
    restored_epoch = restored_store.get_epoch("tsk-after-v2-upgrade", 1)
    assert restored_epoch is not None and restored_epoch.worker_id == "worker-1"


# =============================================================================
# 2. Interrupted Migration Rollback & Pre-Migration Backup Protection
# =============================================================================


def test_interrupted_v3_migration_rolls_back_leaving_v2_database_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "interrupted-v2.db"
    storage = _LocalStorage()

    with sqlite3.connect(db_path) as conn:
        studio._ensure_base_tables(conn)
        rp.apply_schema_migrations(conn, target_version=2)
        conn.execute(
            "INSERT INTO runs(created_at, mode, title, payload_json) VALUES (55.0, 'debate', 'must-survive', '{}')"
        )
        conn.commit()

    def _inject_v3_fault(step_version: int, _conn: sqlite3.Connection) -> None:
        if step_version == 3:
            raise sqlite3.IntegrityError("simulated mid-v3 power cut")

    with pytest.raises(sqlite3.IntegrityError, match="simulated mid-v3 power cut"):
        rp.upgrade_database(
            storage,
            db_path,
            base_ddl=studio._ensure_base_tables,
            target_version=3,
            fault_hook=_inject_v3_fault,
        )

    assert list(tmp_path.glob("*.pre-v3-*")) == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
        ]
        assert conn.execute("SELECT title FROM runs").fetchall() == [("must-survive",)]
        # Transactional DDL rollback must leave zero partial v3 tables behind.
        v3_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'runtime_%'"
        ).fetchall()
        assert v3_tables == []


def test_drifted_interrupted_upgrade_auto_restores_verified_pre_migration_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "drift-recover.db"
    explicit_backup = tmp_path / "kept-snapshot.sqlite3"
    storage = _LocalStorage()

    with sqlite3.connect(db_path) as conn:
        studio._ensure_base_tables(conn)
        rp.apply_schema_migrations(conn, target_version=2)
        conn.execute(
            "INSERT INTO runs(created_at, mode, title, payload_json) VALUES (88.0, 'chat', 'snapshot-row', '{}')"
        )
        conn.commit()

    def _corrupting_migration_step(
        conn: sqlite3.Connection,
        *,
        current_version: int,
        target_version: int,
        fault_hook: Any = None,
    ) -> int:
        # Simulate an interrupted migration where raw bytes on disk were damaged mid-flight.
        conn.rollback()
        db_path.write_bytes(b"corrupted-mid-migration-bytes" * 200)
        raise rp.SchemaMigrationError("mid-flight disk write interrupted")

    monkeypatch.setattr(rp, "_apply_migrations_locked", _corrupting_migration_step)

    with pytest.raises(rp.SchemaMigrationError, match="mid-flight disk write interrupted"):
        rp.upgrade_database(
            storage,
            db_path,
            base_ddl=studio._ensure_base_tables,
            target_version=3,
            backup_path=explicit_backup,
        )

    assert explicit_backup.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(explicit_backup.stat().st_mode) == 0o600

    # Live database was automatically restored from the verified 0600 snapshot!
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT title FROM runs").fetchall() == [("snapshot-row",)]


def test_future_schema_version_fails_closed_before_backup_or_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "future-v99.db"
    backup_path = tmp_path / "should-not-exist.sqlite3"
    storage = _LocalStorage()

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE future_only(id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO future_only(id) VALUES (42)")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()

    with pytest.raises(rp.SchemaMigrationError, match="newer than supported"):
        rp.upgrade_database(storage, db_path, target_version=3, backup_path=backup_path)

    assert not backup_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert conn.execute("SELECT id FROM future_only").fetchall() == [(42,)]


# =============================================================================
# 3. Durable Task, Epoch, Transition, and Action Persistence Invariants
# =============================================================================


def test_runtime_persistence_store_enforces_epoch_fencing_and_action_durability(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "spine-store.db"
    storage = _LocalStorage()
    rp.upgrade_database(storage, db_path, base_ddl=studio._ensure_base_tables, target_version=3)
    store = rp.RuntimePersistenceStore(storage, db_path)

    created = store.create_task(
        "task-01",
        session_id="sess-01",
        title="SQLite migration task",
        state="pending",
        budget_json=json.dumps({"max_tokens": 4096}),
        now=1000.0,
    )
    assert created.state == "pending"
    assert created.current_epoch == 0
    assert created.state_version == 0

    ep1 = store.claim_epoch("task-01", "worker-a", lease_ttl_s=30.0, now=1000.5)
    assert ep1.epoch == 1
    assert ep1.status == "active"

    running, tr1 = store.record_transition(
        "task-01",
        to_state="running",
        expected_state_version=0,
        epoch=1,
        reason="admitted",
        now=1001.0,
    )
    assert running.state == "running"
    assert running.state_version == 1
    assert tr1.seq == 1 and tr1.from_state == "pending" and tr1.to_state == "running"

    # Mismatched expected_state_version fails closed without advancing transition seq.
    with pytest.raises(ValueError, match="state_version mismatch"):
        store.record_transition(
            "task-01",
            to_state="paused",
            expected_state_version=0,
            epoch=1,
            now=1002.0,
        )

    # Record durable action intent + duplicate action_id uniqueness check.
    intent = store.record_action_intent(
        "act-01",
        task_id="task-01",
        session_id="sess-01",
        epoch=1,
        action_class="read_only",
        replay_policy="idempotent",
        tool_name="read_file",
        policy="standard",
        explicit_approval=False,
        args_sha256="a" * 64,
        now=1003.0,
    )
    assert intent.status == "in_flight"
    assert intent.args_sha256 == "a" * 64

    with pytest.raises(sqlite3.IntegrityError):
        store.record_action_intent(
            "act-01",
            task_id="task-01",
            session_id="sess-01",
            epoch=1,
            tool_name="read_file",
            policy="standard",
            args_sha256="a" * 64,
            now=1003.5,
        )

    completed_action = store.finalize_action(
        "act-01",
        status="committed",
        ok=True,
        elapsed_ms=14,
        output_sha256="b" * 64,
        now=1004.0,
    )
    assert completed_action.status == "committed"
    assert completed_action.ok is True
    assert completed_action.elapsed_ms == 14

    # Claim new epoch on recovery and fence out stale epoch-1 writers!
    ep2 = store.claim_epoch(
        "task-01",
        "worker-b",
        fence_reason="crash_recovery_takeover",
        now=1005.0,
    )
    assert ep2.epoch == 2
    fenced_ep1 = store.get_epoch("task-01", 1)
    assert fenced_ep1 is not None
    assert fenced_ep1.status == "fenced"
    assert fenced_ep1.fenced_at == 1005.0
    assert fenced_ep1.fence_reason == "crash_recovery_takeover"

    with pytest.raises(PermissionError, match="Stale task epoch 1"):
        store.record_transition(
            "task-01",
            to_state="completed",
            epoch=1,
            now=1006.0,
        )

    with pytest.raises(PermissionError, match="Stale epoch 1"):
        store.record_action_intent(
            "act-stale",
            task_id="task-01",
            epoch=1,
            tool_name="run_bash",
            policy="standard",
            args_sha256="c" * 64,
            now=1006.0,
        )

    finished, tr2 = store.record_transition(
        "task-01",
        to_state="completed",
        expected_state_version=1,
        epoch=2,
        reason="verified",
        checkpoint_ref="ckpt-final",
        now=1007.0,
    )
    assert finished.state == "completed"
    assert finished.state_version == 2
    assert finished.checkpoint_ref == "ckpt-final"
    assert tr2.seq == 2

    transitions = store.list_transitions("task-01")
    assert [(t.seq, t.epoch, t.from_state, t.to_state) for t in transitions] == [
        (1, 1, "pending", "running"),
        (2, 2, "running", "completed"),
    ]
    fetched_action = store.get_action("act-01")
    assert fetched_action is not None and fetched_action.status == "committed"


# =============================================================================
# 4. No Write Transaction Spans Tool Execution, Model Inference, or Keyring I/O
# =============================================================================


@pytest.mark.anyio
async def test_no_write_transaction_may_span_tool_execution_or_subprocess_or_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: _MemoryKeyring,
) -> None:
    db_path = tmp_path / "tx-boundary.db"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    studio.init_db()

    # 1. Normal tool execution observes ZERO active SQLite write transaction during executor().
    observed_depths: list[int] = []

    async def _clean_tool_executor() -> dict[str, Any]:
        observed_depths.append(rp.active_write_transaction_depth())
        return {"ok": True, "output": "clean", "diff": "", "elapsed_ms": 2}

    res = await studio._run_authorized_tool(
        "read_file",
        {"path": "README.md"},
        "standard",
        approved=True,
        session_id="sess-clean",
        executor=_clean_tool_executor,
    )
    assert res["ok"] is True
    assert observed_depths == [0]

    # 2. Async/coroutine callbacks or awaitable return values inside run_write_transaction are rejected.
    async def _illegal_async_tx(conn: sqlite3.Connection) -> None:
        conn.execute("SELECT 1")

    with pytest.raises(rp.TransactionDisciplineError, match="synchronous"):
        rp.run_write_transaction(studio.STORAGE, db_path, _illegal_async_tx)  # type: ignore[arg-type]

    def _returns_coroutine(conn: sqlite3.Connection) -> Any:
        conn.execute(
            "INSERT INTO runs(created_at, mode, title, payload_json) VALUES (1.0, 'chat', 'rolled-back', '{}')"
        )
        return _clean_tool_executor()

    with pytest.raises(rp.TransactionDisciplineError, match="awaitable"):
        rp.run_write_transaction(studio.STORAGE, db_path, _returns_coroutine)

    # Confirm the write performed before returning the awaitable was rolled back!
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE title = 'rolled-back'").fetchone()[0] == 0

    # 3. Nested write transactions are rejected so locks are never held across composite boundaries.
    with pytest.raises(rp.TransactionDisciplineError, match="Nested SQLite write"):
        rp.run_write_transaction(
            studio.STORAGE,
            db_path,
            lambda _c: rp.run_write_transaction(studio.STORAGE, db_path, lambda _c2: None),
        )

    # 4. If a caller holds an active write-transaction context, tool execution, model inference,
    #    and subprocess spawning ALL fail closed before any external I/O occurs!
    side_effect_ran = False

    async def _should_never_run() -> dict[str, Any]:
        nonlocal side_effect_ran
        side_effect_ran = True
        return {"ok": True, "output": "boom"}

    token = rp._ACTIVE_WRITE_TX_DEPTH.set(1)
    try:
        # Both layers fail closed: _run_authorized_tool blocks at nested write, and
        # DurableActionBoundary.pre_execute_guard blocks even with a non-SQLite ledger!
        with pytest.raises(rp.TransactionDisciplineError):
            await studio._run_authorized_tool(
                "read_file",
                {"path": "README.md"},
                "standard",
                approved=True,
                session_id="sess-blocked",
                executor=_should_never_run,
            )
        assert side_effect_ran is False

        class _NoOpLedger:
            def start(self, session_id: str, tool_name: str, policy: str, explicit_approval: bool, args: dict[str, Any]) -> int:
                return 1

            def finish(self, event_id: int, result: dict[str, Any], elapsed_ms: int, *, status: str = "completed") -> None:
                return None

        direct_boundary = execution_spine.DurableActionBoundary(
            studio._AppToolPolicy(),
            _NoOpLedger(),
            pre_execute_guard=lambda: rp.assert_no_active_write_transaction("tool_execution"),
        )
        with pytest.raises(rp.TransactionDisciplineError, match="tool_execution"):
            await direct_boundary.admit_and_execute(
                execution_spine.ActionRequest(
                    tool_name="read_file",
                    args={"path": "README.md"},
                    policy="standard",
                    approved=True,
                    session_id="sess-blocked",
                ),
                _should_never_run,
            )
        assert side_effect_ran is False

        with pytest.raises(rp.TransactionDisciplineError, match="code_subprocess"):
            await studio._start_code_process(tmp_path, "echo forbidden", python_code=False)

        async with httpx.AsyncClient() as client:
            with pytest.raises(rp.TransactionDisciplineError, match="model_inference"):
                async for _ in studio.stream_any_model(
                    client,
                    "local-model",
                    [{"role": "user", "content": "hi"}],
                ):
                    pass
    finally:
        rp._ACTIVE_WRITE_TX_DEPTH.reset(token)


def test_keyring_io_never_runs_inside_active_sqlite_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "keyring-outside-tx.db"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    tx_depths_during_keyring: list[tuple[str, int]] = []
    store_data: dict[tuple[str, str], str] = {}

    def _checked_set_password(service: str, account: str, secret: str) -> None:
        tx_depths_during_keyring.append(("set", rp.active_write_transaction_depth()))
        store_data[(service, account)] = secret

    def _checked_get_password(service: str, account: str) -> str | None:
        tx_depths_during_keyring.append(("get", rp.active_write_transaction_depth()))
        return store_data.get((service, account))

    def _checked_delete_password(service: str, account: str) -> None:
        tx_depths_during_keyring.append(("delete", rp.active_write_transaction_depth()))
        store_data.pop((service, account), None)

    monkeypatch.setattr(studio.keyring, "set_password", _checked_set_password)
    monkeypatch.setattr(studio.keyring, "get_password", _checked_get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", _checked_delete_password)

    studio.init_db()
    studio.upsert_cloud_provider(
        alias="openai",
        name="OpenAI",
        wire="openai",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-secret-value-123456",
        models=["gpt-4.1"],
    )
    studio.delete_cloud_provider("openai")

    assert tx_depths_during_keyring, "Expected keyring calls during upsert/delete"
    assert all(depth == 0 for _op, depth in tx_depths_during_keyring)


def test_app_and_context_store_never_bypass_runtime_persistence_connect_discipline() -> None:
    """Static AST guard: only SQLiteStorage.connect may call sqlite3.connect directly."""
    for module_name in ("app.py", "context_store.py"):
        tree = ast.parse(Path(module_name).read_text(encoding="utf-8"), filename=module_name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        if child.func.attr == "connect":
                            assert module_name == "app.py" and node.name == "connect", (
                                f"{module_name}:{node.name} calls .connect() directly instead of runtime_persistence"
                            )


# =============================================================================
# 5. Busy, Disk-Full, and Corruption Paths Fail Closed
# =============================================================================


def test_transient_busy_lock_recovers_within_bounded_retry_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "transient-busy.db"
    storage = _LocalStorage()
    rp.upgrade_database(storage, db_path, base_ddl=studio._ensure_base_tables, target_version=3)

    locked = threading.Event()
    released = threading.Event()

    def _hold_lock_briefly() -> None:
        locker = sqlite3.connect(db_path, timeout=0.01, isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(0.04)
            locker.commit()
        finally:
            locker.close()
            released.set()

    t = threading.Thread(target=_hold_lock_briefly, daemon=True)
    t.start()
    try:
        assert locked.wait(timeout=2.0)
        policy = rp.BusyRetryPolicy(
            max_attempts=5,
            busy_timeout_ms=20,
            initial_backoff_s=0.015,
            max_backoff_s=0.03,
            max_total_wait_s=0.5,
        )
        store = rp.RuntimePersistenceStore(storage, db_path, retry_policy=policy)
        task = store.create_task("tsk-contended", session_id="s1", title="Contended write")
        assert task.task_id == "tsk-contended"
    finally:
        t.join(timeout=2.0)
        assert released.is_set()


def test_sustained_busy_lock_exhausts_bounded_retries_and_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "sustained-busy.db"
    storage = _LocalStorage()
    rp.upgrade_database(storage, db_path, base_ddl=studio._ensure_base_tables, target_version=3)

    locker = sqlite3.connect(db_path, timeout=0.01, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        policy = rp.BusyRetryPolicy(
            max_attempts=3,
            busy_timeout_ms=15,
            initial_backoff_s=0.005,
            max_backoff_s=0.01,
            max_total_wait_s=0.2,
        )
        t0 = time.perf_counter()
        with pytest.raises(rp.DatabaseBusyError, match=r"SQLite write busy after 3 attempt\(s\)"):
            rp.run_write_transaction(
                storage,
                db_path,
                lambda conn: conn.execute(
                    "INSERT INTO runs(created_at, mode, title, payload_json) VALUES (1.0, 'chat', 'blocked', '{}')"
                ),
                retry_policy=policy,
            )
        elapsed = time.perf_counter() - t0
        assert elapsed < 1.5
    finally:
        locker.rollback()
        locker.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE title = 'blocked'").fetchone()[0] == 0


def test_disk_full_capacity_exhaustion_rolls_back_and_fails_closed_without_retry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "capacity-capped.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO items(payload) VALUES ('baseline-committed')")
        current_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        conn.execute(f"PRAGMA max_page_count = {current_pages + 1}")
        conn.commit()

    attempts = 0

    class _CountingStorage:
        def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
            nonlocal attempts
            attempts += 1
            conn = sqlite3.connect(database, timeout=timeout)
            conn.execute(f"PRAGMA max_page_count = {current_pages + 1}")
            return conn

    def _overflow_write(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO items(payload) VALUES ('partial-row-must-rollback')")
        conn.execute("INSERT INTO items(payload) VALUES (?)", ("X" * 32_768,))

    with pytest.raises(rp.DatabaseCapacityError):
        rp.run_write_transaction(
            _CountingStorage(),
            db_path,
            _overflow_write,
            retry_policy=rp.BusyRetryPolicy(max_attempts=4, busy_timeout_ms=25, initial_backoff_s=0.05),
        )

    # Disk-full is non-transient: must fail closed on attempt 1 with ZERO retries and full rollback!
    assert attempts == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT payload FROM items").fetchall()
        assert rows == [("baseline-committed",)]


@pytest.mark.parametrize(
    "bad_bytes",
    [
        b"not-a-valid-sqlite-header",
        b"X" * 4096,
    ],
)
def test_corrupt_database_files_fail_closed_immediately_without_retry(
    tmp_path: Path,
    bad_bytes: bytes,
) -> None:
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(bad_bytes)
    attempts = 0

    class _CountingStorage:
        def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
            nonlocal attempts
            attempts += 1
            return sqlite3.connect(database, timeout=timeout)

    storage = _CountingStorage()
    with pytest.raises(rp.DatabaseCorruptionError):
        rp.upgrade_database(storage, db_path, target_version=3)
    assert attempts == 1

    attempts = 0
    with pytest.raises(rp.DatabaseCorruptionError):
        rp.run_read(storage, db_path, lambda conn: conn.execute("SELECT 1").fetchall())
    assert attempts == 1

    attempts = 0
    with pytest.raises(rp.DatabaseCorruptionError):
        rp.run_write_transaction(storage, db_path, lambda conn: conn.execute("CREATE TABLE t(x INT)"))
    assert attempts == 1


@pytest.mark.anyio
async def test_authorized_tool_boundary_fails_closed_on_busy_full_or_corrupt_audit_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for err_msg, expected_cls in (
        ("database is locked", rp.DatabaseBusyError),
        ("database or disk is full", rp.DatabaseCapacityError),
        ("database disk image is malformed", rp.DatabaseCorruptionError),
    ):
        executed = False

        async def _probe_executor() -> dict[str, Any]:
            nonlocal executed
            executed = True
            return {"ok": True, "output": "ran"}

        class _BrokenStorage:
            def connect(self, database: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
                raise sqlite3.OperationalError(err_msg)

        monkeypatch.setattr(studio, "DB_PATH", tmp_path / "broken.db")
        monkeypatch.setattr(studio, "STORAGE", _BrokenStorage())

        with pytest.raises(expected_cls):
            await studio._run_authorized_tool(
                "read_file",
                {"path": "README.md"},
                "standard",
                approved=True,
                session_id="sess-fail-closed",
                executor=_probe_executor,
            )
        assert executed is False
