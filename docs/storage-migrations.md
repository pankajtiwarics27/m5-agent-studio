# Storage and schema migration contract

## Storage boundary

Application database connections go through the `Storage` protocol and `STORAGE` instance in `app.py`, executed exclusively via `runtime_persistence.py` (`run_read`, `run_write_transaction`, `upgrade_database`, and `apply_schema_migrations`). `SQLiteStorage` is the current implementation; it opens the configured `DB_PATH` with the standard-library `sqlite3` driver and preserves the SQLite WAL and FTS5 SQL. Tests can inject a connection provider without changing the application path contract. This is a narrow connection/storage seam, not a claim that current SQL or FTS5 behavior is portable to another database engine.

`DB_PATH` remains the compatibility setting. No ORM, migration framework, external database, or new dependency is introduced. The standalone `studio.py` is generated from `app.py` and inlines `runtime_persistence.py` alongside the same SQLite storage implementation.

## Durable JSON sidecar stores

Seven subsystems keep their own state file under `$HARNESS_STORE_DIR` rather than in
SQLite, because each must survive a crash *before* the database is usable and each is
written from a failure path where a transaction is not available:

| Store | Owner module | Path |
|---|---|---|
| Containment (fences, holds, in-flight processes) | `process_supervision.py` | `containment/state.json` |
| Scoped grants, revocations, policy revisions | `scoped_grants.py` | `grants/state.json` |
| MCP external effects and connector policies | `mcp_client.py` | `mcp_effects/state.json` |
| Security incidents, configuration generations, and backend qualifications | `sandbox_boundary.py` | `security_incidents/state.json` |
| Artifact content identity, provenance, evidence freshness, and pins | `artifact_lineage.py` | `artifacts/state.json` |
| Portable model handoffs and per-session switch retry budgets | `routing_policy.py` | `handoffs/state.json` |
| Bounded child delegations, disjoint write leases, and benchmark summaries | `child_delegation.py` | `delegations/state.json` |

They share one contract, and `tests/test_durable_state_contract.py` asserts every
clause of it against **every** store:

- the state file is `0600` and its directory `0700`;
- writes go to an `O_EXCL` temp file in the same directory, which is `fsync`ed, then
  `os.replace`d, then the containing **directory** is `fsync`ed — syncing only the
  file leaves the rename in the page cache, so a crash can roll the manifest back
  behind a record the caller was told was written;
- a symlinked state file *or* a symlinked state directory is refused;
- corrupt JSON and a future `schema_version` fail closed rather than starting empty or
  downgrading a newer manifest;
- no temporary files are left behind.

The contract is checked per store because the implementations are hand-written
and can drift — the directory `fsync` was missing from exactly one of them, and a
per-store unit test would not have noticed. A store that is not in the suite fails
`test_every_module_with_a_durable_state_file_is_covered`, which discovers the stores
from source — that is how the two stores added after the original three were folded in.

## Migration invariants and persistence discipline

- The current schema is `SCHEMA_VERSION = 3` (mirrored by `RUNTIME_SCHEMA_VERSION = 3` in `runtime_persistence.py`), recorded in `PRAGMA user_version` and `schema_migrations`:
  - **Version 1**: additive columns on legacy Studio tables (`runs`, `perf_samples`, `hall_sessions`, `hall_messages`, `cloud_providers`, `model_combos`, `harness_sessions`, `mcp_servers`, `opencontext_docs`, `ctx_vault_chunks`, `ctx_session_metrics`).
  - **Version 2**: `tool_audit_events` privacy-preserving tool attempt log.
  - **Version 3** (`M5-P1-01`, [ADR-0003](adr/0003-sqlite-schema-v3-and-persistence-discipline.md)): durable execution-spine tables `runtime_tasks`, `runtime_task_epochs`, `runtime_task_transitions`, and `runtime_actions` with `FOREIGN KEY` constraints (`ON DELETE RESTRICT`) and state/policy `CHECK` invariants.
- Upgrades are additive and idempotent. `upgrade_database` validates file headers, `PRAGMA quick_check`, and `PRAGMA user_version` before mutating any bytes; corrupted files (`DatabaseCorruptionError`) and future schema versions (`SchemaMigrationError`) fail closed immediately.
- Write transactions (`run_write_transaction`) acquire `BEGIN IMMEDIATE` synchronously, apply bounded `SQLITE_BUSY` / `SQLITE_LOCKED` retries (`BusyRetryPolicy`), reject async/awaitable or nested write callbacks (`TransactionDisciplineError`), perform OS keyring I/O outside the SQLite write lock, and fail closed without retry on disk-full/quota (`DatabaseCapacityError`) or corruption (`DatabaseCorruptionError`).
- `assert_no_active_write_transaction` prevents any active SQLite write transaction from spanning tool execution (`DurableActionBoundary` `pre_execute_guard`), subprocess launch (`_start_code_process`), or model inference (`stream_any_model`).
- Legacy rows, FTS5 indexes, and credential references are preserved. Credential values continue to be migrated through the OS keyring; backups containing plaintext provider or MCP credentials are rejected.

## Backup and failed-migration recovery

Studio does not silently retain long-lived pre-migration copies after a successful startup. History databases can contain prompts, code, tool output, and credentials from older schemas; permanent hidden backups would create additional sensitive files with unclear retention. During an upgrade of an existing populated database (`user_version < 3`), `upgrade_database` creates an ephemeral owner-only (`0600`) online SQLite snapshot before entering `BEGIN IMMEDIATE`, automatically restores from that verified snapshot if an interrupted upgrade disturbs live integrity or schema state, and unlinks the ephemeral snapshot once post-upgrade integrity succeeds (unless an explicit `backup_path` is passed). Operators should still back up explicitly before upgrading releases:

```bash
python3 scripts/db_maintenance.py backup --database "${STUDIO_DB_PATH:-./studio_history.db}" --output /secure/path/studio-pre-upgrade.sqlite3
```

The utility uses SQLite's online backup API, validates integrity and schema, refuses overwrite and plaintext credentials, and creates owner-only files. If a migration fails, the transactional migration rolls back; the startup fails closed. After stopping Studio, restore the known-good backup if recovery is needed:

```bash
python3 scripts/db_maintenance.py restore \
  --database "${STUDIO_DB_PATH:-./studio_history.db}" \
  --input /secure/path/studio-pre-upgrade.sqlite3 \
  --force
```

`--force` is explicit and restore creates a validated pre-restore safety backup. Backups refer to OS-keyring entries on the original machine; moving one to another machine requires re-entering credentials. Backups still contain local history and must be protected accordingly.

## Evidence

- `test_storage_connection_seam_is_injectable` verifies application connection calls use the injected storage seam and current `DB_PATH`; `test_storage_failure_is_not_bypassed` verifies the app does not bypass a failed backend.
- `test_schema_upgrade_preserves_rows_and_is_idempotent` and `test_schema_failure_rolls_back_partial_ddl` verify upgrade, idempotence, and transactional rollback behavior.
- `test_verified_backup_restores_after_failed_migration` exercises the explicit backup → failed migration → validated restore path and confirms both data and schema version are preserved.
- Backup integrity, permissions, future-version rejection, force protection, and safety-backup behavior remain tested in `tests/test_db_maintenance.py`.

Run the local storage/migration tests with:

```bash
python -m pytest -q tests/test_runtime_persistence.py tests/test_storage_contracts.py tests/test_db_maintenance.py tests/test_security_and_quality.py
```
