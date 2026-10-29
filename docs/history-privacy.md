# History privacy: export, retention, and redaction (AS-030)

Local history is your data. Studio keeps conversations, Council Hall sessions,
harness runs, telemetry samples, context-vault chunks, and OpenContext
documents in the local SQLite database (`STUDIO_DB_PATH`); nothing is sent
anywhere unless you invoke a feature that explicitly does so. This document
describes the controls for getting data out, deleting it, and limiting how
long it is kept. Harness transcript files (`.harnesses/sessions/*.jsonl` and
`*.messages.json`) can contain raw prompts, tool arguments, and outputs; they
are local user data too and are included in the retention boundary, but not in
the SQLite export bundle.

## Export

- **UI/API:** `GET /api/history/export` (bearer-authenticated, behind the
  standard host/Origin boundary) downloads a JSON attachment,
  `studio-history-export.json`.
- **CLI (Studio stopped or running):**
  `python3 scripts/db_maintenance.py export --database "$STUDIO_DB_PATH" --output /path/history.json`
  writes the same bundle owner-only (0600).

The bundle contains the history tables (`runs`, `perf_samples`,
`hall_sessions`, `hall_messages`, `harness_sessions`, `ctx_vault_chunks`,
`ctx_session_metrics`, `opencontext_docs`) with timestamps converted to
ISO-8601 UTC. **It never contains** credential/configuration tables
(`cloud_providers`, `model_combos`, `mcp_servers` — secrets live in the OS
keyring as references anyway), the tool audit trail (`tool_audit_events`), or
schema bookkeeping; the exclusion list is embedded in every export.

### Redaction (best-effort)

Exports are scrubbed with a pattern-based redactor covering common credential
shapes: provider API keys (`sk-…`, `sk-ant-…`, `AIza…`, `AKIA…`, `ghp_…`,
`xox…`), `Bearer` tokens, and `token/secret/password/api_key/…` key-value
assignments and JSON fields. Replacements are counted and reported in the
export (`redaction.replacements`).

This is defense-in-depth, **not a guarantee**: secrets that do not match a
known shape in free text will not be detected. Treat exports as sensitive and
keep raw databases protected regardless.

## Retention

Set `STUDIO_HISTORY_RETENTION_DAYS` (integer, 1–3650; default `0` = disabled)
to prune conversation, telemetry, context-vault, and harness-session artifacts
older than the configured age. Harness-session age is based on the newest of
the database `updated_at` timestamp and associated transcript-file mtimes, so
an actively updated session is not removed just because it was created long ago:

- On startup, `init_db` applies the policy and reports deletion counts to
  stderr. Database errors leave SQL rows intact and are reported; transcript
  file-unlink failures are counted separately, leave the affected file in
  place, and are retried on a later retention pass.
- On demand: `POST /api/history/retention/run` (authenticated) applies the
  policy immediately and returns per-table deletion counts; it returns `400`
  while retention is disabled.
- From the CLI: `python3 scripts/db_maintenance.py retention --database "$STUDIO_DB_PATH" --days 30`.

**Never pruned by retention:** OpenContext documents (a persistent knowledge
vault — delete individual documents through the OpenContext API when you want
them gone), the tool audit trail (a security record), credential/configuration
tables, and schema bookkeeping. Council Hall messages and context-vault chunks
are pruned with their owning sessions; matching FTS5 rows are removed in the
same retention pass. Expired harness-session rows and their `.jsonl` event logs
and `.messages.json` snapshots are also removed. Each transcript file must
independently be older than the cutoff before it is unlinked; an old orphan
file does not make a newer paired file eligible for deletion. Retention reports
separate counts for database rows, FTS rows, successfully removed transcript
files, and transcript-file unlink failures (`harness_session_files_failed`). This is logical deletion, not forensic erasure: SQLite pages/WAL may retain deleted bytes until SQLite reuses or vacuums them, and filesystem unlink does not guarantee secure overwrite.

## Deletion

- Retention (above) is time-based bulk deletion.
- The SQLite export bundle covers the documented database tables, not the
  separate harness JSONL/message files or the internal FTS5 indexes; it is not a
  complete inventory of every on-disk artifact. Use retention's deletion counts
  and search checks when verifying expiry.
- Whole-database removal: stop Studio and delete `studio_history.db`
  (or restore from before the data existed with
  `python3 scripts/db_maintenance.py restore`).
- Per-item deletion exists where it is meaningful today: harness sessions
  (`DELETE /api/harness/sessions/{id}`) and OpenContext documents
  (`DELETE /api/opencontext/docs/{id}`).

## Tests and evidence

`tests/test_history_privacy.py` covers the credential shapes and their
false-positive behavior, the export boundary (excluded tables, redaction
counts, ISO timestamps), retention pruning semantics (old vs. recent,
children and FTS rows pruned with owners, harness rows/files removed while
recent activity is preserved, OpenContext/audit untouched, argument
validation), the authenticated API paths (401 without token, fail-closed when
a token is missing, 400 while disabled, redacted payload, cross-origin
rejection), and the operator CLI including owner-only export files.
