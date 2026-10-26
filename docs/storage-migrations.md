# Storage and schema migration contract

## Storage boundary

Application database connections go through the `Storage` protocol and `STORAGE` instance in `app.py`. `SQLiteStorage` is the current implementation; it opens the configured `DB_PATH` with the same standard-library `sqlite3` driver and preserves the existing SQLite/FTS5 SQL. Tests can inject a connection provider without changing the application path contract. This is a narrow connection/storage seam, not a claim that current SQL or FTS5 behavior is portable to another database engine.

`DB_PATH` remains the compatibility setting. No ORM, migration framework, external database, or new dependency is introduced. The standalone `studio.py` is generated from `app.py` and embeds the same SQLite storage implementation.

## Migration invariants

- The current schema remains `SCHEMA_VERSION = 2`; this story changes no tables or columns and does not invent a new version.
- Existing upgrades remain additive and idempotent. `PRAGMA user_version` and `schema_migrations` record the current schema state.
- Migrations acquire `BEGIN IMMEDIATE`, apply DDL and version records transactionally, and roll back on failure. A future schema version is rejected before mutation.
- Legacy rows and credential references are preserved. Credential values continue to be migrated through the OS keyring; backups containing plaintext provider or MCP credentials are rejected.

## Backup and failed-migration recovery

Studio does not silently create long-lived pre-migration copies. History databases can contain prompts, code, tool output, and credentials from older schemas; hidden backups would create additional sensitive files with unclear retention. Back up explicitly before upgrading:

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
python -m pytest -q tests/test_db_maintenance.py tests/test_security_and_quality.py
```
