# Release rollback runbook

For an operator who has to undo a Studio release or restore a damaged history database. Every command
here is the one `scripts/db_maintenance.py` actually provides, and each step was exercised by
`release_drill_witness.py` (see [ap032-drill-witness.md](ap032-drill-witness.md)).

**Scope.** This covers the local history database and the packaged release. It is not a cloud failover
plan, and it does not restore a workspace you deleted — Studio never deletes workspace files.

## 0. Decide which failure you are in

| Symptom | Go to |
|---|---|
| The app starts but behaves wrongly after an upgrade | §2 roll back the release |
| The history database is corrupt or the app refuses to start after a restore | §3 restore a database |
| A migration half-applied or left the schema at the wrong version | §3, then §4 |
| You need last week's history after pruning it | §1 first — retention is irreversible without a backup |

## 1. Take a backup before anything else

```bash
python scripts/db_maintenance.py backup --database ~/studio_history.db
# Backup created and validated: /home/.../studio_history-<UTC stamp>.backup.sqlite3
```

The command refuses rather than half-succeeding if the source is not a readable database, if
`PRAGMA integrity_check` fails, if the schema version is newer than the tool supports, or if a
plaintext provider credential is still in the file. It writes `0600`, validates the copy after writing,
and refuses to overwrite an existing backup.

**Do this even when the database looks fine.** A rollback without a backup is a rollback you cannot
reverse.

## 2. Roll back the release

Studio is installed from a versioned artifact; roll back by reinstalling the previous version, not by
editing the database.

1. Stop Studio cleanly, and confirm no process holds the database (no `-wal`/`-shm` sidecars left
   behind).
2. Reinstall the previous release artifact through the same channel you installed from
   (`docs/packaging-rollback.md` covers install, update and artifact integrity).
3. Start Studio, which runs the schema check. **A database at a newer schema version than the
   reinstalled binary supports is refused, not silently downgraded** — that refusal is the guard, not
   a bug. Restore a pre-upgrade backup (§3) instead.
4. Record what you rolled back and why. If a migration ran forward, note its version.

The drills verified this direction only in the sense that matters for recoverability: a migrated
database rolled back to its pre-migration snapshot, then migrated forward again, ending at the same
version with the same rows. They did **not** verify an install-level rollback of the packaged app; that
is `packaging-rollback.md`'s territory.

## 3. Restore a history database

```bash
# Studio must be stopped. WAL/SHM sidecars present -> the restore refuses.
python scripts/db_maintenance.py restore --database ~/studio_history.db --input <backup>.sqlite3 --force
# Database restored and validated: ...
# Pre-restore safety backup: ...pre-restore-<UTC stamp>.bak
```

What the tool guarantees, each of which was exercised:

- refuses if the destination still has `-wal`/`-shm` sidecars (Studio was not stopped cleanly);
- validates the *input* before touching the destination;
- with `--force`, takes a **pre-restore safety backup** of the current database first, so a wrong
  restore is itself reversible;
- writes through a temporary file and `os.replace`, so an interrupted restore cannot leave a partial
  file in place;
- re-validates the restored file, then sets `0600`.

### Verifying the restore is correct

Do **not** compare file hashes. A restored SQLite database is logically identical but not
byte-identical — page layout, freelist state and header fields legitimately differ. Measured on this
repository's own round-trip:

| comparison | result |
|---|---|
| `sha256` of the file bytes | differs |
| logical digest (ordered tables, ordered rows, `user_version`) | identical |

To check logically, use the witness's own digest:

```bash
python -c "from release_drill_witness import logical_digest; from pathlib import Path; \
print(logical_digest(Path('<backup>.sqlite3'))); print(logical_digest(Path('$HOME/studio_history.db')))"
```

Two identical digests mean the rows and the schema version match. Two different ones mean the restore
did not reproduce the backup — investigate before starting Studio.

## 4. If the schema version is wrong

The runtime applies migrations transactionally under `BEGIN IMMEDIATE` and rolls back on failure, so a
crash mid-migration should leave the previous version intact and the database readable. Check:

```bash
python -c "import sqlite3,sys; c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True); \
print('user_version', c.execute('PRAGMA user_version').fetchone()[0]); \
print(c.execute('PRAGMA integrity_check').fetchone()[0])" ~/studio_history.db
```

- `integrity_check` not `ok` → restore a backup (§3).
- version newer than the binary supports → you are running an older release against a newer database;
  either reinstate the newer release or restore the pre-upgrade backup.
- version older than expected → start Studio once to apply the migrations; the drill confirmed v1→v4
  keeps every original column value while adding columns.

## 5. What to record afterwards

For the release ledger and for whoever reads this next:

- the exact backup path you restored from and the logical digest before and after;
- `user_version` before and after;
- what the failure looked like, what you changed, and whether the pre-restore safety backup was needed;
- anything you could not verify.

Recording only the successful steps is how a runbook becomes fiction. If step 3 refused because of WAL
sidecars, that belongs in the record.

## What this runbook does not cover

- **Workspace files.** Never deleted, so never restored here.
- **Retention.** `retention --days N` is irreversible; a backup is your only recourse.
- **Keyring-held credentials.** The backup refuses a database holding plaintext credentials and does
  not carry the OS keyring. After restoring onto a new machine, provider keys must be re-entered.
- **A production-window proof.** The drills used a fixture holding two small tables. Nothing here
  measures how long a multi-gigabyte history restore takes on your hardware, and the drill does not
  assert a duration.
