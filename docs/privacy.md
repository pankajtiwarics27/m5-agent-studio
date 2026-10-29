# Privacy

Studio is local-first: it stores everything on your machine and contacts nothing
until you ask it to. This page states exactly what is kept, what can leave, and
where the honest limits are.

Deeper references: [`history-privacy.md`](history-privacy.md) (export, retention,
redaction), [`../SECURITY.md`](../SECURITY.md) (data handling and secret storage),
[`threat-model.md`](threat-model.md) (actors and abuse cases).

## What stays local

Everything Studio writes lives under your user account and is never uploaded by
Studio itself:

| Data | Location | Notes |
|---|---|---|
| Conversations, runs, telemetry samples | SQLite (`studio_history.db` next to the app source by default; override with `STUDIO_DB_PATH`) | WAL mode; sensitive prompts and code included |
| Multi-model sessions | same database (`hall_*`) | Full prompts and model output |
| Agent work and audit trail | `harness_sessions`, `tool_audit_events` | Audit rows store **hashes** of tool arguments/output, never the content |
| Harness events and message snapshots | `.harnesses/…` JSONL / message files | Can contain raw prompts, arguments, and tool output; covered by retention |
| Memory / context vault | `opencontext_*`, `ctx_vault_*` tables | Session-scoped chunks plus FTS indexes |
| Workspace files | Your workspace directory | Studio reads and writes only inside the configured workspace |
| API token | `~/.config/m5-agent-studio/api-token` (honours `XDG_CONFIG_HOME`) | File `0600`, directory `0700`, owner-checked |
| Provider/MCP credentials | **OS keychain** | Databases hold `keyring:v1:…` references only |

There is **no telemetry, no analytics, no crash reporting, and no startup-time
network request.** Nothing is collected in the background.

## What leaves the machine

Only caller-triggered traffic leaves the host, and only to endpoints you
configure:

| Trigger | Destination | What is sent |
|---|---|---|
| Chat/agent/compare workflows against a cloud provider | The provider's API | Your prompts, context files, and tool output needed for that turn |
| `Fetch models` / model listing | The provider's `/models` endpoint | No credentials beyond its own auth header |
| MCP tool calls | The MCP server you configured | The tool arguments, scoped by the approval policy |
| Skills installation | GitHub over HTTPS | A shallow clone request |
| Release/update checks | Nothing — there is no updater | — |

All of this passes URL validation (public addresses only by default), DNS
pinning to the validated IP, redirect rejection, and ambient-proxy bypass. A
loopback or private destination needs an explicit opt-in
(`STUDIO_PRIVATE_URL_HOSTS`, or the broader `STUDIO_ALLOW_PRIVATE_URLS=1`).

Choosing a cloud provider for a prompt *is* the act of sharing it. Studio cannot
make that local, and does not pretend to.

## Secrets

- Provider API keys, MCP auth tokens, and credential-like MCP header/environment
  values are stored as references in SQLite with the value in the OS keychain.
- Legacy plaintext values are migrated at startup. **If the keychain is
  unavailable, startup fails** rather than continuing with plaintext on disk.
- Masked values only are returned by API responses; a blank field in the UI keeps
  the existing key instead of clearing it.
- The preflight (`/api/onboarding/preflight`) reduces providers to
  alias/enabled/has-credential/model-count and never returns key material.
- Keys are never written to logs, the audit table, or history exports.

## Retention and export

- **Export** (`GET /api/history/export`, or `scripts/db_maintenance.py export`)
  produces a redacted JSON export that excludes credentials and the tool-audit
  table.
- **Retention** (`STUDIO_HISTORY_RETENTION_DAYS`, `POST /api/history/retention/run`,
  or `scripts/db_maintenance.py retention`) deletes aged conversation,
  telemetry, and context-vault rows — including their FTS copies — plus inactive
  harness sessions and their transcript files. OpenContext documents, the audit
  trail, and credentials are preserved.
- **Backups** (`scripts/db_maintenance.py backup`) are created with the online
  backup API and mode `0600`; plaintext-credential databases are refused.

## Deletion

To remove local state: stop Studio, delete the database, the `.harnesses/`
directory, the workspace you pointed it at, and `~/.config/m5-agent-studio/`.
Delete the keychain entries for `m5-agent-studio` too — they are not covered by
deleting the database. Backups you made are separate files; remove them
explicitly.

## Limits

- **The database is not encrypted at rest.** Prompts, code, and tool output are
  stored in plaintext SQLite. Use full-disk encryption and account permissions.
- **Redaction is best-effort pattern matching**, not a guarantee: it lowers the
  chance of leaking an obvious credential into an export, and it can miss
  secrets embedded in ordinary prose. See `history-privacy.md` for the exact
  patterns.
- **Harness transcripts are the weak point.** Message snapshots and JSONL events
  can hold raw content by design; retention is the control, not redaction.
- **The audit log is an integrity aid, not tamper-evident.** Same-account
  in-place modification of files is outside the boundary.
- **Prompt content sent to a provider is governed by that provider**, not by
  Studio. Review their retention terms before sending sensitive material.
- Independent review of these controls is deferred to the final end-to-end
  verification; see [`security-review-signoff.md`](security-review-signoff.md).
