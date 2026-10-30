---
id: ADR-0003
title: sqlite-schema-v3-and-persistence-discipline
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0003: SQLite Schema v3 Migration and Fail-Closed Persistence Discipline

## Context

Story `M5-P1-01` on the M5 2.0 roadmap requires durably representing autonomous
tasks, state transitions, worker epochs, and action attempts in local SQLite
while preserving all existing user data (`runs`, `hall_sessions`,
`hall_messages`, `cloud_providers`, `model_combos`, `harness_sessions`,
`mcp_servers`, `opencontext_docs` / `opencontext_fts`, `ctx_vault_chunks` /
`ctx_vault_fts`, `ctx_session_metrics`, and `tool_audit_events`). Prior to this
change, `app.py` and `context_store.py` opened SQLite connections directly at
roughly forty call sites (including three raw `sqlite3.connect(DB_PATH)` sites
bypassing `STORAGE.connect`), held write locks across OS keyring calls in
`upsert_cloud_provider` and `_migrate_legacy_secrets`, lacked bounded busy-retry
policy or typed capacity/corruption classification, and had no runtime guard
preventing a SQLite write transaction from spanning tool execution, subprocess
spawning, or model inference.

## Decision

Introduce a stdlib-only module `runtime_persistence.py` (inlined into standalone
`studio.py` via `scripts/build_studio.py`) and advance `SCHEMA_VERSION` from `2`
to `3`:

1. **Additive Schema v3 tables**: add `runtime_tasks`, `runtime_task_epochs`,
   `runtime_task_transitions`, and `runtime_actions` with `FOREIGN KEY`
   integrity (`ON DELETE RESTRICT`), state/policy `CHECK` constraints, and
   secondary lookup indexes while keeping Schema v1 legacy column backfills and
   Schema v2 `tool_audit_events` additive and idempotent. Exclude the four new
   spine tables from user history export in `history_privacy.py` alongside
   `tool_audit_events` and `schema_migrations`.
2. **Pre-migration backup and rollback protection**: `upgrade_database`
   validates SQLite file headers, `PRAGMA quick_check`, and `PRAGMA user_version`
   before mutating any bytes (failing closed on corrupted files or future schema
   versions). When upgrading an existing populated database (`user_version`
   below `3`), it creates an owner-only (`0600`) online SQLite backup snapshot,
   applies base DDL plus versioned migrations inside a single `BEGIN IMMEDIATE`
   transaction, rolls back on error, automatically restores the verified
   snapshot if live integrity or schema state drifted during an interrupted
   upgrade, and unlinks ephemeral snapshots after verified completion unless the
   caller requested an explicit `backup_path`.
3. **Short write transactions and boundary guards**: route all SQLite reads and
   writes in `app.py` and `context_store.py` through `run_read` and
   `run_write_transaction`. Perform OS keyring reads and writes outside
   `BEGIN IMMEDIATE` transactions. Reject async callbacks, awaitable return
   values, and nested write transactions inside `run_write_transaction`, track
   active write depth in a `ContextVar`, and enforce
   `assert_no_active_write_transaction` at `DurableActionBoundary`
   (`pre_execute_guard`), `_start_code_process`, and `stream_any_model`.
4. **Bounded busy retries and fail-closed error hierarchy**: retry transient
   `SQLITE_BUSY` / `SQLITE_LOCKED` contention within bounded attempts and wall
   time (`BusyRetryPolicy`), then fail closed with `DatabaseBusyError`. Never
   retry disk-full / read-only capacity errors (`DatabaseCapacityError`) or
   malformed / non-database corruption (`DatabaseCorruptionError`); roll back
   and fail closed immediately.

## Alternatives

- **Adopt SQLAlchemy and Alembic**: rejected — adds third-party runtime/ORM
  dependencies that break the stdlib-only standalone `studio.py` bundle and
  violate the M5 2.0 small-footprint non-bloat constraint.
- **Store autonomous task, epoch, and action state only in `.harnesses/*.jsonl`
  flat files**: rejected — flat JSONL files cannot enforce atomic cross-record
  epoch fencing, optimistic `state_version` compare-and-swap transitions, or
  foreign-key integrity with durable rollback on partial disk writes.
- **Leave existing `with STORAGE.connect(DB_PATH)` call sites in `app.py` and
  wrap only the new `RuntimePersistenceStore`**: rejected — leaves raw
  `sqlite3.connect` bypasses, unguarded connection lifetimes, keyring I/O inside
  write transactions, and inconsistent busy/capacity/corruption handling across
  the application.
- **Keep permanent automatic backup copies next to `studio_history.db` after
  every successful startup upgrade**: rejected — `docs/storage-migrations.md`
  explicitly warns that backups hold unredacted local history and must not
  accumulate silently without operator awareness; ephemeral `0600` upgrade
  snapshots protect against interrupted migrations without leaving unrequested
  copies behind.

## Tradeoffs

- **Benefits**: legacy `v0`, `v1`, and `v2` databases upgrade to `v3` without
  data or FTS5 loss; interrupted upgrades roll back or restore cleanly; write
  locks stay short and cannot span tool execution, subprocesses, model
  inference, or keyring I/O; busy, capacity, and corruption failures surface
  deterministic fail-closed exception types; `RuntimePersistenceStore` provides
  the durable task/epoch/transition/action foundation for `M5-P1-02` through
  `M5-P1-05`.
- **Costs/risks**: opening and closing short per-operation connections adds
  minor local connection-setup overhead compared to a long-lived connection,
  accepted because local SQLite file opens take microseconds whereas leaked
  transactions stall every writer; older Studio binaries (`SCHEMA_VERSION` 2)
  refuse to open a `v3` database by design unless restored from a pre-upgrade
  backup; harness `.jsonl` transcript appends remain non-transactional sidecar
  files until unified into the state machine in `M5-P1-02`.
- **Verified facts**: verified by `tests/test_runtime_persistence.py` (15 tests
  covering `v0`/`v2` upgrades, FTS5 survival, interrupted migration rollback and
  drift restore, epoch fencing, write-tx boundary guards, AST connect-bypass
  check, transient vs sustained `SQLITE_BUSY`, `PRAGMA max_page_count`
  disk-full rollback without retry, and corrupt-file rejection without retry)
  plus the existing storage, maintenance, privacy, and standalone smoke suites.

## Review Status

Not reviewed. Record review status separately from decision status. When
reviewed, identify the reviewer or review role and date in both the metadata
and this section.
