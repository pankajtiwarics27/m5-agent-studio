# Migration guide

How to move an existing Studio installation forward — and back — without losing
data. Covers three different kinds of "upgrade", because they fail differently:

1. the **release** (which `studio.py` you run),
2. the **database schema** (additive migrations at startup),
3. **credentials** (plaintext → OS keychain).

Related: [`storage-migrations.md`](storage-migrations.md) (storage contract),
[`packaging-rollback.md`](packaging-rollback.md) (versioned releases),
[`configuration-contracts.md`](configuration-contracts.md) (settings and precedence).

## Before you upgrade

1. **Back up first.** Backups are cheap; a lost conversation is not.

   ```bash
   python3 scripts/db_maintenance.py backup --database "${STUDIO_DB_PATH:-./studio_history.db}"
   ```

   This uses SQLite's online backup API (so committed WAL content is included),
   passes an integrity check, and writes mode `0600`. A database with plaintext
   credentials is refused — fix the keychain first.

2. **Note your current version.** `python3 -c "import studio; print(studio.STUDIO_VERSION)"`,
   or `scripts/release.py status` if you installed a versioned release.

3. **Stop Studio.** Schema migration runs at startup and assumes it owns the
   database. Do not upgrade under a running server.

4. **Copy `.harnesses/`** if you care about agent transcripts; it is not part of
   the SQLite backup.

## Upgrading the release

**Versioned install (recommended).** Releases are immutable directories with a
manifest, and activation is an atomic pointer swap:

```bash
python3 scripts/release.py package --version 9.0.2
python3 scripts/release.py update  --version 9.0.2     # refuses to downgrade
python3 scripts/release.py verify                      # digests of active + previous
python3 scripts/release.py status                      # current / previous / installed
```

Restart Studio against `<root>/versions/9.0.2/studio.py`.

**In-place checkout.** Rebuild the artifact from the new sources and restart:

```bash
git pull
python scripts/build_studio.py
```

`studio.py` is generated; never hand-edit it. If you changed a *source* module,
CI regenerates the artifact and fails on drift, so a stale local build shows up
as a test failure rather than a subtle runtime difference.

**Database compatibility direction.** Newer code upgrades an older schema
automatically. Older code **refuses** a newer schema
(`Database schema version N is newer than supported version M`) instead of
half-reading it — so downgrading the release generally requires restoring a
backup taken before the upgrade. Plan that before you upgrade.

## Database schema

- Version lives in `PRAGMA user_version`; applied versions are recorded in
  `schema_migrations`. Current `SCHEMA_VERSION` is `3` (`runtime_persistence.py`,
  [ADR-0003](adr/0003-sqlite-schema-v3-and-persistence-discipline.md)).
- Migrations are **additive and transactional**: `ALTER TABLE … ADD COLUMN` and
  Schema v2/v3 table definitions run inside `BEGIN IMMEDIATE` after an ephemeral
  owner-only (`0600`) pre-migration snapshot, with rollback and snapshot
  verification on failure. No destructive rewrites, and no rows are dropped.
- New columns get defaults, so old rows keep working.
- WAL mode and FTS5 indexes are recreated idempotently; FTS rows are cleaned up
  when their source rows are deleted.

If startup fails mid-migration, the transaction rolls back and the database is
left at its previous version. Restore your backup, then re-run.

**Restore (Studio stopped):**

```bash
python3 scripts/db_maintenance.py restore \
  --database "${STUDIO_DB_PATH:-./studio_history.db}" \
  --input /path/to/backup.sqlite3 --force
```

Restore makes a pre-restore copy of the current database, so a mistaken restore
is itself reversible.

## Credential migration

At startup Studio migrates plaintext provider/MCP credentials into the OS
keychain and rewrites the columns as `keyring:v1:...` references.

- The migration is **idempotent**: already-migrated references are skipped.
- It **fails closed**: if the keychain rejects a write, startup aborts. Studio
  will not silently continue with plaintext secrets on disk.
- Missing keychain on a headless box (SSH, container) is the usual cause. Unlock
  the keychain or run in a desktop session — not as a reason to hand-edit the DB.

**Moving machines:** copy the database, but re-enter credentials. Keychain
entries are per-machine and per-account; they do not travel with the file. That
is intentional.

## Rolling back

| Situation | Do this |
|---|---|
| New release misbehaves, schema unchanged | `python3 scripts/release.py rollback` — swaps active/previous, repeatable |
| Rollback target is corrupt | Refused by design (`verify` shows why); reinstall the known-good version instead |
| Schema was upgraded by the newer release | Restore the pre-upgrade backup, then run the older release |
| Single file `studio.py` was overwritten | Re-run `package`/`install` for the previous version, or `git checkout` the prior artifact |

`rollback` verifies the target's digest before activating it, so a damaged older
release cannot be swapped in during an incident.

## Configuration changes

Environment settings are read once at startup; a change requires a restart.
Contract details are in [`configuration-contracts.md`](configuration-contracts.md).

| Setting | If you had… | Now |
|---|---|---|
| `STUDIO_EXECUTION_MODE` | Only `STUDIO_ALLOW_HOST_EXECUTION=1` | Still honoured as deprecated shorthand for host mode; prefer the explicit `STUDIO_EXECUTION_MODE=sandbox` |
| `STUDIO_ALLOW_PRIVATE_URLS` | Set for a private endpoint | Still works, but prefer per-host `STUDIO_PRIVATE_URL_HOSTS` to keep the exception narrow |
| `STUDIO_API_TOKEN` | Empty on loopback | Auto-generated token file remains the default; a non-loopback bind still requires an explicit ≥32-character token |
| `STUDIO_ALLOWED_HOSTS` | Unset | Defaults to loopback names; behind a proxy you must list the public host and set `STUDIO_ALLOWED_ORIGINS` |

Invalid combinations (for example `STUDIO_AUTH_DISABLED=1` on a non-loopback
host) fail at startup with the setting name in the message, rather than being
silently normalised.

## Evidence

- `tests/test_security_and_quality.py` — versioned schema upgrade on legacy
  tables, refusal of a future schema without mutation, transaction rollback,
  backup/recovery, and credential-migration failure behaviour.
- `tests/test_db_maintenance.py` — backup, restore, pre-restore copy, and refusal
  of plaintext-credential databases.
- `tests/test_release_manager.py` (39 tests) — install/update/rollback including
  downgrade refusal, corrupt rollback target, and CLI end-to-end.
- `scripts/release.py verify` and `scripts/db_maintenance.py` were exercised by
  hand against a real artifact and database for this page (Linux / Python 3.11.2).

Not claimed: a live upgrade on a macOS host, cross-machine keychain continuity
(explicitly unsupported), and automated rollback of a *schema* upgrade (restore a
backup instead).
