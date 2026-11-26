# Release rollback runbook

For an operator who has to undo a Studio release or restore a damaged history database. Every command
below was executed against this revision before the page was written; the numbers and exit codes quoted
are the ones it returned. `release_drill_witness.py` exercises the same machinery in a throwaway
directory (see [ap032-drill-witness.md](ap032-drill-witness.md)), and
`tests/test_db_maintenance.py` pins the guarantees this page leans on: a hot
backup keeps rows still in the WAL, a restore refuses live WAL/SHM sidecars, and
a refused restore leaves the destination untouched.

**Scope.** The packaged release and the local history database. Not a cloud failover plan, and not
workspace recovery — Studio never deletes workspace files.

## 0. Decide which failure you are in

| Symptom | Go to |
|---|---|
| The app starts but behaves wrongly after an update | §2 roll back the release |
| The history database is corrupt, or a restore left it unreadable | §3 restore a database |
| The schema version is not what the running build expects | §4 |
| History must be recovered after retention pruned it | §1 first — retention is irreversible without a backup |

## 1. Take a backup before anything else

```bash
python scripts/db_maintenance.py backup --database ~/studio_history.db
# Backup created and validated: ~/studio_history-20261126T072446Z.backup.sqlite3
```

The name is `<stem>-<UTC stamp>.backup.sqlite3`, written next to the source unless `--output` says
otherwise, mode `0600`, validated by re-opening the copy after writing.

It refuses — exit `2`, nothing written — in each of these cases, all of which I triggered:

| Input | Message |
|---|---|
| Path missing | `Database does not exist or is not a regular file` |
| Not SQLite | `Not a readable SQLite database: file is not a database` |
| `user_version` above 4 | `Database schema version 99 is newer than supported version 4` |
| `--output` already exists | `Refusing to overwrite an existing backup` |
| Plaintext credential in `cloud_providers.api_key` | `Database still contains a plaintext provider credential` |

Three things that table does **not** say, and the earlier draft of this page wrongly implied:

- The credential check looks at `cloud_providers.api_key` and at the `auth_token`/`headers_json`/
  `env_json` columns of `mcp_servers`, and only there. A key pasted into a *message body* is not
  detected — I put a provider-shaped token in `messages.body` and the backup completed normally. The
  scan is about the credential tables, not a content scan of your history.
- A value prefixed with the keyring reference (`keyring:v1:`) passes, which is the point of the check.
- A running Studio is **not** a reason to skip the backup. Backing up a database with live WAL sidecars
  succeeded and captured the un-checkpointed row. Backup is safe hot; restore is not (see §3).

## 2. Roll back the release

Use the lifecycle commands in `scripts/release.py`; do not hand-edit the install root.

```bash
python scripts/release.py status --root ~/.local/share/m5-agent-studio
python scripts/release.py rollback --root ~/.local/share/m5-agent-studio
python scripts/release.py verify  --root ~/.local/share/m5-agent-studio    # exit 1 when unhealthy
```

Measured behaviour:

- `rollback` **swaps** current and previous; it is repeatable and returns to where you started after an
  even number of calls. Run 9.0.2 → rollback → 9.0.1 → rollback → 9.0.2, `verify` exit `0`, `healthy: true`.
- `update` refuses to go backwards: `Refusing to downgrade from 9.0.1 to 9.0.0; use rollback, or pass
  allow_downgrade=True deliberately` — exit `1`. Do not reach for `--allow-downgrade` to undo an update;
  `rollback` is the operation that preserves the previous release for you.
- `verify` checks the digests of the active **and** previous releases, so a corrupt rollback target is
  reported before you need it.

Note the flags, because `packaging-rollback.md` used to print them in a shape this CLI rejects.
`scripts/check_documented_commands.py` now checks every documented flag against the script that
accepts it, so this class of page cannot drift silently again:

| Command | Takes `--root`? | Where the artifact comes from |
|---|---|---|
| `package --version X --artifact studio.py --output <dir>` | no | `--output` staging dir (default `dist/releases/`) |
| `install --version X --root <root> --from-dir <dir>` | yes | `--from-dir`, else the default staging dir |
| `update --version X --root <root> --from-dir <dir>` | yes | same |
| `rollback` / `verify` / `status` | yes | the install root |
| `prune --keep N` | yes | the install root |

Passing `--root` to `package` fails with `unrecognized arguments`, which is how I found this.

### Release rollback and database rollback are separate

Neither command touches the other. Rolling back the *release* does not downgrade the *database*, and
restoring an old database does not change which release is active. A schema newer than the running
build is refused at startup — the refusal is the guard, not a fault. If you rolled a release back
across a schema migration, restore the pre-update backup in §3.

## 3. Restore a history database

**Stop Studio first.** Unlike backup, restore refuses a live database:

```bash
python scripts/db_maintenance.py restore --database ~/studio_history.db --input <backup>.sqlite3 --force
# Database restored and validated: ...
# Pre-restore safety backup: ~/studio_history.db.pre-restore-20261126T072500Z.bak
```

What it does, each measured:

- refuses with `SQLite WAL/SHM sidecars exist. Stop Studio cleanly before restoring.` while a `-wal` or
  `-shm` file is present — this is how it detects that Studio is still running;
- refuses without `--force` when the destination exists: `Destination already exists; pass --force only
  after stopping Studio` (exit `2`);
- validates the input **before** touching the destination: a 4 KB of `/dev/urandom` "backup" was refused
  with `Not a readable SQLite database`, exit `2`, and the destination still held all three of its rows;
- with `--force`, writes a pre-restore safety backup of the current database first (`0600`), so a wrong
  restore is itself reversible;
- replaces atomically via a temporary file and `os.replace`, then re-validates and sets `0600` — the
  destination was `0644` before the restore and `0600` after.

### Verify the restore logically, never by file hash

A restored SQLite database is logically identical but not byte-identical. Measured here: the logical
digests of the backup and the restored file matched exactly while the file bytes differ.

```bash
python -c "from release_drill_witness import logical_digest; from pathlib import Path; \
print(logical_digest(Path('<backup>.sqlite3'))); print(logical_digest(Path('$HOME/studio_history.db')))"
```

Equal digests mean the same tables, the same rows and the same `user_version`. Unequal means the
restore did not reproduce the backup — investigate before starting Studio. Comparing `sha256` of the
two files is not a weaker check, it is the wrong check: it fails on a correct restore.

## 4. If the schema version is wrong

Migrations run transactionally, so an interrupted migration should leave the previous version readable.
Check without a writable handle:

```bash
python -c "import sqlite3,sys; c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro',uri=True); \
print('user_version', c.execute('PRAGMA user_version').fetchone()[0]); \
print(c.execute('PRAGMA integrity_check').fetchone()[0])" ~/studio_history.db
# user_version 4
# ok
```

- `integrity_check` not `ok` → restore a backup (§3).
- `user_version` newer than the running build supports → you are on an older release against a newer
  database. Reinstate the newer release, or restore the pre-update backup.
- `user_version` older than expected → start Studio once to migrate. The drill observed v1→v4 keeping
  every original column value while adding columns.

## 5. What to record afterwards

- The backup path you restored from, and the logical digest before and after.
- `user_version` before and after; the release version before and after a rollback.
- What the failure looked like, what you changed, whether the safety backup was needed.
- Anything you could not verify.

Recording only the steps that worked is how a runbook becomes fiction. A refusal in §1 or §3 is a
finding about the incident, not a detour around it.

## What this runbook does not cover

- **Workspace files.** Never deleted, so never restored here.
- **Retention.** `retention --days N` is irreversible; a backup is the only recourse. On a database with
  nothing old enough it reports `deleted 0 item(s) - nothing to prune` and exits `0`.
- **Credentials.** The backup does not carry the OS keyring, only the `keyring:v1:` reference. After
  restoring onto another machine, provider keys must be re-entered. `export` omits the credential and
  configuration tables entirely and records the redaction count in the file.
- **Duration.** The drills and this page used small fixture databases. Nothing here measures how long a
  multi-gigabyte restore takes on your hardware, and no duration is asserted.
- **A signed or notarized artifact.** `release.py rollback` restores bytes it verified; the signing hook
  is still a hook ([`release-provenance.md`](release-provenance.md)).
