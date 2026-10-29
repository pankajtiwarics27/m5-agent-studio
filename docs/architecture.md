# Architecture

A single-user, local-first FastAPI application: one process, one SQLite file, one
bearer token. This page is the map — what runs, how a request moves, where state
lives, and which parts are generated rather than hand-written.

Related: [`subsystem-dependency-inventory.md`](subsystem-dependency-inventory.md)
(generated endpoint/table/boundary list), [`route-security-matrix.md`](route-security-matrix.md)
(generated per-route auth), [`../SECURITY.md`](../SECURITY.md) (threat posture),
[`../AGENTS.md`](../AGENTS.md) (working rules).

## Components

```text
app.py                     canonical FastAPI backend: routes, middleware,
                           policy, persistence, provider streaming
<contract>.py  (20+)       extracted typed boundaries, each mypy-gated:
                           workflow_orchestration, mcp_client, context_store,
                           history_privacy, onboarding_preflight,
                           release_manager, routing_policy, sandbox_boundary,
                           session_trust, model_*, hardware_*, …
static/index.html          canonical UI: one file, five panes, no build step
studio.py                  GENERATED standalone (app.py + inlined modules + UI)
scripts/                   build, generate, validate, smoke, maintenance
tests/                     unit, contract, adversarial, opt-in live sandbox
product/…roadmap_v2.json   source of truth for story status
docs/                      this set, plus generated artifacts and ADRs
```

The default shape is a **modular monolith** (ADR-0001): boundaries are modules
with frozen dataclasses and injected dependencies, not services. That keeps one
deployment unit while still letting a boundary be unit-tested without the server.

## Request path

Every request crosses the same middleware before it reaches a handler
(`app.py`, `security_boundary`). The order is normative:

```text
Host allowlist -> Origin/CSRF check (state-changing) -> bearer auth on /api/* & /v1/*
  -> per-client rate limit (expensive POSTs) -> body-size cap
  -> route handler
```

Consequences worth knowing:

- `GET /healthz` and `GET /` are the only unauthenticated routes that matter;
  FastAPI docs/OpenAPI are **disabled** unless explicitly enabled.
- If the process has no token, protected routes return `503` rather than becoming
  anonymous — fail closed, including when ASGI lifespan startup is skipped.
- Tool execution passes a second, separate boundary: central deny-by-default
  authorization plus an approval/replay check, then a hash-only audit record
  written *before* the tool runs. A failed audit write blocks execution.
- MCP and provider calls go through URL validation, DNS pinning, redirect
  rejection, and proxy bypass before any socket is opened.

## Storage

One SQLite database (default `./studio_history.db`), WAL mode, FTS5 indexes for full-text
search, schema version tracked in `PRAGMA user_version` and the
`schema_migrations` table:

| Area | Tables |
|---|---|
| Runs and performance | `runs`, `perf_samples` |
| Multi-model sessions | `hall_sessions`, `hall_messages` |
| Routing configuration | `cloud_providers`, `model_combos` |
| Agent work | `harness_sessions`, `tool_audit_events` |
| MCP | `mcp_servers` |
| Memory | `opencontext_docs` + `opencontext_fts`, `ctx_vault_chunks` + `ctx_vault_fts`, `ctx_session_metrics` |
| Schema bookkeeping | `schema_migrations` |

Credential columns hold **references** (`keyring:v1:…`), never plaintext; values
live in the OS keychain and are migrated there at startup. The rest of the
database is not encrypted — see [`privacy.md`](privacy.md).

Harness events and message snapshots also exist as JSONL/message files under
`.harnesses/`, which can contain raw prompts and tool output and are covered by
retention rather than by the SQLite export.

## Generated artifacts

Four files are generated and CI fails if they drift from their sources:

| Artifact | Generator |
|---|---|
| `studio.py` | `scripts/build_studio.py` (inlines `INLINED_MODULES` + the UI) |
| `docs/route-security-matrix.md` | `scripts/generate_route_matrix.py` |
| `docs/subsystem-dependency-inventory.md` | `scripts/generate_subsystem_inventory.py` |
| `docs/product-roadmap-status.md` | `scripts/generate_roadmap_status.py` |

Edit the source, regenerate, and commit both. Never hand-edit a generated file:
the next CI run will fail, which is the point.

## Extension seams

| You want to… | Change | Contract to keep |
|---|---|---|
| Add or change a route | `app.py` | Generated route matrix and subsystem inventory must regenerate cleanly |
| Add a typed boundary | new `<module>.py` | Add to `pyproject.toml` mypy `files` **and** to `INLINED_MODULES` in `build_studio.py`, or the standalone breaks off-checkout |
| Add a UI feature | `static/index.html` | Rebuild `studio.py`; `scripts/check_frontend_syntax.py` parses the inline JS |
| Add a provider | provider config + adapter path | URL validation, pinning, redirect rejection, keyring storage |
| Add a tool | central policy registry | Deny by default, audit before execution, approval scope |
| Change the schema | `init_db` + additive migration map | Bump `SCHEMA_VERSION`, keep migrations additive and transactional |

## Limits

- **Single user, no tenancy.** One bearer token is the entire authorization
  boundary; there are no roles, tenants, or per-route permissions. Do not expose
  this to untrusted users or the public internet.
- **One process.** Rate limits are in-memory and per-process, not distributed.
- **No hot reload of state.** Upgrading the release means restarting against the
  new artifact; the schema migrates additively at startup.
- **Prompt-injection defenses are model-dependent.** Tool output is JSON-escaped
  and labelled untrusted, but that is a mitigation, not an isolation boundary.
- **Independent security review is deferred** to the final end-to-end pass; see
  [`security-review-signoff.md`](security-review-signoff.md).

## Evidence

`tests/test_subsystem_inventory.py`, `tests/test_configuration_contracts.py`, and
`tests/test_remote_protections.py` pin this structure; CI regenerates every
generated artifact and fails on drift. The middleware order described above is
asserted in `tests/test_remote_protections.py`, not merely documented here.
