#!/usr/bin/env python3
"""Generate a source-linked inventory of registered APIs, SQLite tables, and I/O boundaries."""
from __future__ import annotations

import ast
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "app.py"
RUNTIME_PERSISTENCE_SOURCE = ROOT / "runtime_persistence.py"
OUT = ROOT / "docs" / "subsystem-dependency-inventory.md"
sys.path.insert(0, str(ROOT))
import app  # noqa: E402


class InventoryError(RuntimeError):
    """Raised when source code contains an inventory item that is not documented."""


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    handler: str
    line: int
    subsystem: str
    authorization: str


# Exact inventory: additions/removals must be reviewed alongside schema changes.
TABLES: dict[str, str] = {
    "runs": "Saved run summaries and payload JSON.",
    "perf_samples": "Per-run/model performance measurements and token counters.",
    "hall_sessions": "Council Hall session metadata and conclusions.",
    "hall_messages": "Council Hall messages, reasoning, and usage records.",
    "cloud_providers": "Provider aliases, endpoint settings, model lists, and keyring references.",
    "model_combos": "Ordered model routing/fallback targets.",
    "harness_sessions": "Harness workspaces, policy, selected models, status, and step counts.",
    "mcp_servers": "MCP transport/configuration, tool inventory/allowlist, and keyring references.",
    "opencontext_docs": "Persistent local OpenContext documents and pinned state.",
    "opencontext_fts": "SQLite FTS5 index for OpenContext document search.",
    "ctx_vault_chunks": "Session-scoped compacted tool-output chunks.",
    "ctx_vault_fts": "SQLite FTS5 index for session context-vault search.",
    "ctx_session_metrics": "Raw/sent byte and compaction counters per session.",
    "schema_migrations": "Applied SQLite schema version history.",
    "tool_audit_events": "Tool execution metadata, approval state, and argument/output hashes; no raw argument/output fields.",
    "runtime_tasks": "Durable runtime task state, monotonic epoch counter, budget snapshot, and terminal outcome.",
    "runtime_task_epochs": "Append-only runtime task execution epochs and stale-writer fencing metadata.",
    "runtime_task_transitions": "Append-only durable state transitions per runtime task and epoch.",
    "runtime_actions": "Durable action/effect records with idempotency keys, request/result digests, and execution status.",
    "runtime_task_checkpoints": "Hash-verified durable task checkpoints and typed evidence references per task and sequence.",
}

# Function references are resolved from app.py on every generation, so line anchors stay current.
EXTERNAL_BOUNDARIES: tuple[tuple[str, str, str, str], ...] = (
    ("_PinnedAsyncHTTPTransport", "Outbound HTTP transport", "Configured provider, local runtime, and MCP HTTP requests use URL validation/IP pinning; redirects and ambient proxies are disabled.", "Requests occur only when the relevant API/workflow is invoked."),
    ("_stream_cloud_openai", "Configured OpenAI-compatible provider", "Provider URL and credentials come from provider configuration/keyring; remote requests occur during caller-triggered chat/model workflows. `/api/providers/fetch-models` separately queries the configured `/models` endpoint.", "No provider traffic from startup or route inventory."),
    ("_stream_cloud_anthropic", "Configured Anthropic-compatible provider", "Provider URL and credentials come from provider configuration/keyring; remote request is explicit in a chat/model workflow.", "No provider traffic from startup or route inventory."),
    ("_stream_omlx_completion", "Local model runtime (oMLX-compatible HTTP)", "Uses configured OMLX_BASE_URL (loopback default); local model list/stream/admin operations are caller-triggered.", "No model call from startup or route inventory."),
    ("_mcp_http_rpc", "Configured MCP HTTP server", "Per-server endpoint; URL validation, pinned transport, and redirect rejection apply. Tool execution also checks enabled server and enabled-tool allowlist.", "Only MCP sync/call or a workflow using configured MCP tools."),
    ("_mcp_stdio_rpc", "Configured local MCP stdio process", "Launches a configured executable with filtered environment; disabled unless STUDIO_ALLOW_MCP_STDIO=1. Runs with host-user privileges, not the code sandbox.", "Only when explicitly enabled and an MCP operation requires stdio."),
    ("install_skills_native_git", "GitHub skill repository download", "Shallow Git clone from validated GitHub HTTPS source; selected skill files are copied. Downloaded search code is not run unless STUDIO_ALLOW_SKILL_CODE=1.", "Triggered by /api/skills/install, not app startup."),
    ("_run_skill_script", "Local skill script subprocess", "Routes repository/documented skill scripts (`skill_execute_script`) through `_run_authorized_tool` (`DurableActionBoundary`) with server-only `skill_opt_in` provenance, requiring `STUDIO_ALLOW_SKILL_CODE=1`, non-`plan` policy, and a live execution mode: sandbox routes through the no-network container boundary, host requires `STUDIO_ALLOW_HOST_EXECUTION=1`, disabled never executes. No silent host fallback.", "Triggered only when a workflow loads skills and the skill provides scripts/search.py."),
    ("_fetch_online_inspiration", "Online inspiration/search", "Calls GitHub repository search and DuckDuckGo; may also use enabled search MCP servers and, only with STUDIO_ALLOW_SKILL_CODE=1 and a live execution mode, a locally installed skill script run inside the execution boundary. No separate remote-search opt-in is visible in this path.", "Triggered by SDLC feedback workflow; remote search is part of that route's behavior."),
    ("_start_code_process", "Shell/Python tool subprocess", "Execution is disabled by default; sandbox mode uses a pre-pulled container with networking disabled; host mode requires STUDIO_ALLOW_HOST_EXECUTION=1 and runs unsandboxed.", "Triggered only by approved workflow tool calls under the configured execution policy."),
    ("get_docker_telemetry", "Local container-runtime telemetry", "Invokes the local `docker` CLI when present; does not contact a remote provider through Studio's HTTP client.", "Triggered by telemetry requests; cached briefly."),
    ("_sysctl_int", "Local host telemetry", "Reads OS system values using `/usr/sbin/sysctl`; unsupported/failing calls fall back to unavailable/zero telemetry.", "Triggered by telemetry collection."),
    ("_orbstack_host_rss_bytes", "Optional local runtime telemetry", "On the macOS path, reads local process RSS using `/bin/ps` and filters for OrbStack; no network request.", "Triggered by telemetry collection; otherwise returns unavailable."),
    ("get_mac_unified_memory", "Local memory telemetry", "On the macOS path, reads `/usr/bin/vm_stat`; when unavailable, the existing implementation falls back to 24 GiB total memory and 16 KiB pages.", "Triggered by telemetry collection."),
    ("_create_harness_worktree", "Local Git worktree operations", "Uses local `git worktree` and branch commands for workspace isolation/removal; does not fetch or push remotes.", "Triggered by harness session creation/removal when worktree mode is requested."),
    ("_keyring_store", "Operating-system credential store", "Stores provider/MCP secrets outside SQLite; credential migration/storage fails closed if the keyring cannot accept writes.", "Used when saving, migrating, reading, or deleting credentials."),
)

AUTHORIZATION_PATHS: tuple[tuple[str, str, str], ...] = (
    ("lifespan", "Startup/bind authorization", "Requires a configured high-entropy token for non-loopback binds and permits auth-disabled mode only on loopback."),
    ("security_boundary", "Shared HTTP gate", "Checks Host and Origin, bearer token, selected rate limits, and request byte limits before routing."),
    ("authorize_tool_invocation", "Central tool authorization policy", "Deny-by-default allowlist, action-class classification (`read_only` vs `managed_edit`, `consequential`, `external`), and workflow-policy decision for registered built-in, skill, smart-block, and MCP tools; preserves approval and execution-mode guards."),
    ("_run_authorized_tool", "Central tool execution and audit boundary", "Enforces action-class separation, grant provenance, task-state and lease-epoch validation, durable `runtime_actions` intent-before-effect and replay classification (`side_effect_ledger.DurableActionLedger`), privacy-preserving `tool_audit_events` start/finish, write-transaction discipline, and scoped anti-bypass admission tokens via `DurableActionBoundary`."),
    ("tool_requires_approval", "Harness approval decision", "Determines whether a human approval is required; centralized authorization independently enforces the decision at execution."),
    ("_execute_harness_tool", "Harness execution boundary", "Routes built-in tool attempts through central authorization, durable audit, and the executor."),

    ("_create_pending_tool_approval", "Approval request lifecycle", "Creates an in-process pending approval entry and records its request event."),
    ("api_approve_harness_tool", "Approval resolution API", "Authenticated operator resolves a pending approval by ID; there is no separate identity/role model."),
    ("execute_mcp_tool_call", "MCP tool execution", "Uses central policy and audit; requires an enabled server and enabled-tool allowlist entry, with explicit approval per server/workflow policy; stdio launch is separately opt-in."),
    ("_execute_mcp_stdio_tool", "Inbound MCP bridge execution", "Routes each bundled stdio `tools/call` through the central registry, one-call authorization, and privacy-preserving audit."),
    ("mcp_tool_needs_approval", "MCP approval requirement", "Applies the configured MCP approval policy; the central execution boundary enforces it for harness, direct, automated, and inbound bridge calls."),
    ("api_call_mcp_server_tool", "MCP playground route", "Authenticated explicit playground action is treated as one-call user approval, then centrally authorized and audited against server/tool allowlists."),
)

_TABLE_RE = re.compile(r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _source_functions(source: str) -> dict[str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise InventoryError(f"Cannot parse app.py: {exc}") from exc
    return {
        node.name: node.lineno
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _table_lines(source: str, persistence_source: str | None = None) -> dict[str, tuple[str, int]]:
    persistence_text = (
        RUNTIME_PERSISTENCE_SOURCE.read_text(encoding="utf-8")
        if persistence_source is None
        else persistence_source
    )
    found: dict[str, tuple[str, int]] = {}
    for filename, text in (("app.py", source), ("runtime_persistence.py", persistence_text)):
        for match in _TABLE_RE.finditer(text):
            name = match.group(1)
            found.setdefault(name, (filename, text.count("\n", 0, match.start()) + 1))
    missing = sorted(TABLES.keys() - found.keys())
    undocumented = sorted(found.keys() - TABLES.keys())
    if missing or undocumented:
        raise InventoryError(f"SQLite table inventory mismatch; missing={missing}, undocumented={undocumented}")
    return found


def _subsystem(path: str) -> str:
    if path == "/":
        return "Browser UI"
    if path == "/healthz":
        return "Liveness"
    prefixes = (
        ("/v1/", "OpenAI-compatible gateway"),
        ("/api/providers", "Provider configuration"),
        ("/api/onboarding/", "Guided onboarding preflight"),
        ("/api/combos", "Model routing configuration"),
        ("/api/chat/", "Inference and agent workflows"),
        ("/api/council/", "Council workflow"),
        ("/api/debate/", "Debate workflow"),
        ("/api/skills", "Skills"),
        ("/api/workspace/", "Workspace browsing"),
        ("/api/hall/", "Council Hall"),
        ("/api/telemetry", "Runtime telemetry"),
        ("/api/models/", "Local model controls"),
        ("/api/cache/", "Local runtime cache control"),
        ("/api/history/", "History privacy controls"),
        ("/api/mcp/", "MCP configuration and tools"),
        ("/api/opencontext/", "OpenContext memory"),
        ("/api/codegraph/", "Workspace code analysis"),
        ("/api/harness/", "Harness, GSD, and SDLC workflows"),
        ("/api/security/", "Security incident containment and qualification"),
    )
    for prefix, label in prefixes:
        if path.startswith(prefix):
            return label
    raise InventoryError(f"No subsystem mapping for registered endpoint {path!r}")


def _authorization(path: str) -> str:
    if path in {"/", "/healthz"}:
        return "Public; no bearer token."
    if path in {"/docs", "/openapi.json", "/redoc"}:
        return "Public only when API docs are explicitly enabled."
    if path.startswith("/api/harness/"):
        if path in {"/api/harness/run", "/api/harness/gsd-wave", "/api/harness/sdlc/run", "/api/harness/sdlc/feedback"}:
            return "Shared API bearer/host/Origin boundary; workflow policy and centralized tool authorization/approval apply to tool execution."
        if path == "/api/harness/approve":
            return "Shared API bearer/host/Origin boundary; resolves a pending approval ID within the single-operator trust domain."
        if path.endswith("/apply"):
            return "Shared API bearer/host/Origin boundary; applies the selected harness worktree when this operation is called; no separate user/role identity exists."
        return "Shared API bearer/host/Origin boundary; no user/tenant role check is implemented."
    if path == "/api/mcp/servers/{alias}/call":
        return "Shared API bearer/host/Origin boundary; explicit playground request is one-call user approval and passes through central authorization/audit; MCP server/tool allowlists apply, and stdio still requires explicit opt-in."
    if path == "/api/skills/install":
        return "Shared API bearer/host/Origin boundary; GitHub HTTPS source validation; installing files is operator-triggered; downloaded code is not executed by default."
    if path.startswith(("/api/models/", "/api/cache/")):
        return "Shared API bearer/host/Origin boundary; this bearer-authenticated request directly invokes local runtime controls, with no separate role/approval layer."
    if path == "/api/history/export":
        return "Shared API bearer/host/Origin boundary; returns the local history bundle with best-effort secret redaction applied; credential/configuration and audit tables are excluded by construction; no separate user/tenant role check is implemented."
    if path == "/api/history/retention/run":
        return "Shared API bearer/host/Origin boundary; applies configured retention to aged conversation/telemetry/context-vault rows and FTS copies plus inactive harness sessions/transcripts; never touches credentials, the tool audit trail, or OpenContext documents; no separate user/tenant role check is implemented."
    if path.startswith("/api/security/"):
        return "Shared API bearer/host/Origin boundary; single-operator security incident containment, disjoint-dependency fallback qualification, and post-incident requalification/restoration."
    if path.startswith(("/api/", "/v1/")):
        return "Shared API bearer/host/Origin boundary; no user/tenant role check is implemented."
    raise InventoryError(f"No authorization mapping for registered endpoint {path!r}")


def endpoints(routes: Iterable[Any] | None = None, source: str | None = None) -> list[Endpoint]:
    source_text = APP_SOURCE.read_text(encoding="utf-8") if source is None else source
    function_lines = _source_functions(source_text)
    route_objects = app.app.routes if routes is None else routes
    found: list[Endpoint] = []
    for route in route_objects:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        handler = getattr(route.endpoint, "__name__", "")
        if handler not in function_lines:
            raise InventoryError(f"No app.py source reference for endpoint {path!r} handler {handler!r}")
        subsystem = _subsystem(path)
        authorization = _authorization(path)
        for method in sorted(method for method in methods if method not in {"HEAD", "OPTIONS"}):
            found.append(Endpoint(method, path, handler, function_lines[handler], subsystem, authorization))
    return sorted(set(found), key=lambda item: (item.path, item.method))


def _ref(symbol: str, functions: dict[str, int]) -> str:
    try:
        line = functions[symbol]
    except KeyError as exc:
        raise InventoryError(f"Required source symbol {symbol!r} is missing from app.py") from exc
    return f"[`app.py:L{line}`](../app.py#L{line})"


def _source_line(source: str, pattern: str, label: str) -> int:
    match = re.search(pattern, source, re.MULTILINE)
    if match is None:
        raise InventoryError(f"Required source reference for {label!r} was not found")
    return source.count("\n", 0, match.start()) + 1


def render(source: str | None = None, routes: Iterable[Any] | None = None) -> str:
    source_text = APP_SOURCE.read_text(encoding="utf-8") if source is None else source
    functions = _source_functions(source_text)
    tables = _table_lines(source_text)
    route_list = endpoints(routes, source_text)
    app_creation_line = _source_line(source_text, r"^\s*app\s*=\s*FastAPI\(", "FastAPI app constructor")
    static_mount_line = _source_line(source_text, r"^\s*app\.mount\(\"/static\"", "static assets mount")
    required = [symbol for symbol, *_ in EXTERNAL_BOUNDARIES]
    required.extend(symbol for symbol, *_ in AUTHORIZATION_PATHS)
    missing_symbols = sorted(set(required) - functions.keys())
    if missing_symbols:
        raise InventoryError(f"External-boundary source references are missing: {missing_symbols}")

    lines = [
        "# API and subsystem dependency inventory",
        "",
        "> Generated by `python scripts/generate_subsystem_inventory.py`; do not edit by hand.",
        "> This is a source inventory, not a claim that any configured provider/MCP/runtime is live or available.",
        "",
        "## Runtime and authorization overview",
        "",
        f"- FastAPI app and startup boundary: {_ref('lifespan', functions)}. Startup validates bind/auth/execution settings, initializes local SQLite, and creates the shared HTTP client; it does not issue model/provider requests.",
        f"- Shared HTTP request policy: {_ref('security_boundary', functions)}. All `/api/*` and `/v1/*` routes require bearer auth unless explicitly disabled on loopback; host allowlisting, state-changing Origin checks, request limits, and selected POST rate limits are also applied here.",
        f"- Route inventory: {len(route_list)} method/path registrations, plus the separately listed static mount. Optional FastAPI `/docs` and `/openapi.json` are disabled by default in the app constructor at [`app.py:L{app_creation_line}`](../app.py#L{app_creation_line}); when enabled, those routes are public. `/redoc` is not registered.",
        "- There is one operator trust domain: no user identity, tenant separation, or route-level RBAC is implemented. A valid bearer token is the primary API authorization boundary.",
        "- Route authentication and tool authorization are separate boundaries: built-in, outbound MCP, and bundled inbound stdio MCP tool attempts pass through one deny-by-default policy and privacy-preserving audit boundary, including denials and approval rejections. The authenticated `/api/harness/approve` resolves pending harness approvals; explicit playground and inbound `tools/call` requests count as one-call intent but remain subject to registered-tool/server allowlists.",
        "",
        "## Registered HTTP endpoints",
        "",
        "| Method | Path | Handler source | Subsystem | Authorization path |",
        "|---|---|---|---|---|",
    ]
    for endpoint in route_list:
        source_ref = f"[`{endpoint.handler}`](../app.py#L{endpoint.line})"
        lines.append(f"| `{endpoint.method}` | `{endpoint.path}` | {source_ref} | {endpoint.subsystem} | {endpoint.authorization} |")

    lines.extend([
        "",
        "## Authorization path detail",
        "",
        "| Layer | Source | Decision / scope |",
        "|---|---|---|",
    ])
    for symbol, label, description in AUTHORIZATION_PATHS:
        lines.append(f"| {label} | {_ref(symbol, functions)} | {description} |")

    lines.extend([
        "",
        "## Mounted static assets",
        "",
        f"The public `/static/*` ASGI mount is registered when the static directory exists. It is listed separately because Starlette mounts do not expose a method set for the ordinary route enumerator. Source: [`app.py:L{static_mount_line}`](../app.py#L{static_mount_line}).",
        "",
        "## SQLite tables and schema contracts",
        "",
        f"`DB_PATH` is local SQLite; initialization and additive schema upgrades are implemented by `init_db` and `_apply_schema_migrations` ({_ref('init_db', functions)}, {_ref('_apply_schema_migrations', functions)}) delegating to `runtime_persistence.py`. Current `SCHEMA_VERSION` is {app.SCHEMA_VERSION}. Credential values are migrated to the OS keyring; history/context remains local SQLite data.",
        "",
        "| Table | Kind | Responsibility | Declaration source |",
        "|---|---|---|---|",
    ])
    virtual_tables = {"opencontext_fts", "ctx_vault_fts"}
    for name in sorted(TABLES):
        kind = "FTS5 virtual table" if name in virtual_tables else "table"
        filename, line = tables[name]
        lines.append(f"| `{name}` | {kind} | {TABLES[name]} | [`{filename}:L{line}`](../{filename}#L{line}) |")

    lines.extend([
        "",
        "## External and local process boundaries",
        "",
        "| Boundary | Source | Dependencies and authorization/trigger |",
        "|---|---|---|",
    ])
    for symbol, label, controls, trigger in EXTERNAL_BOUNDARIES:
        lines.append(f"| {label} | {_ref(symbol, functions)} | {controls} {trigger} |")

    lines.extend([
        "",
        "## Subsystem dependency map",
        "",
        "| Subsystem | Local state | Outbound/process dependencies | Entry points / controls |",
        "|---|---|---|---|",
        "| Providers and model routing | `runs`, `cloud_providers`, `model_combos`; OS keyring for secrets | Configured remote provider APIs or local OMLX-compatible HTTP runtime | `/api/providers/*`, `/api/combos*`, `/api/chat/stream`, `/api/council/stream`, `/api/debate/stream`, `/v1/*`; outbound URL policy and redirects disabled |",
        "| MCP | `mcp_servers`; OS keyring for token/credential values | Configured MCP HTTP server or local stdio command | `/api/mcp/*` and bundled `--mcp` bridge; centralized deny-by-default authorization/audit and enabled-server/tool allowlists; per-server approval applies to outbound calls, while inbound bridge calls use its registered-tool policy; `STUDIO_ALLOW_MCP_STDIO=1` remains required for outbound stdio servers |",
        "| Skills | Skill files below configured local roots; no SQLite table | GitHub shallow clone on install; optional local `search.py` process | `/api/skills*`; HTTPS GitHub source validation; downloaded code execution off unless `STUDIO_ALLOW_SKILL_CODE=1` |",
        "| Council Hall | `hall_sessions`, `hall_messages` | Configured model/provider routing | `/api/hall/*`; shared API bearer boundary |",
        "| Harness / GSD / SDLC | `harness_sessions`, `tool_audit_events`, local JSONL/message files and Git worktrees | Configured model/provider and MCP calls; local Git; shell/Python via disabled/sandbox/explicit host mode | `/api/harness/*`; shared bearer plus workflow/tool policy and approval checks; worktree apply is a separate authenticated route action |",
        "| Security incident containment and qualification | `$HARNESS_STORE_DIR/security_incidents/state.json` and `$HARNESS_STORE_DIR/containment/state.json` (`0600` owner-only fsynced state; no SQLite table) | Local process-tree signal delivery (`ProcessSupervisor`) on containment; no outbound network calls | `/api/security/*`; shared bearer boundary; fail-closed suspension, shared-dependency fallback checks, and post-incident requalification |",
        "| OpenContext and context vault | `opencontext_docs`, `opencontext_fts`, `ctx_vault_chunks`, `ctx_vault_fts`, `ctx_session_metrics` | Local SQLite/FTS5 and workspace files | `/api/opencontext/*`, context tools; shared API bearer boundary |",
        "| Telemetry and performance | `perf_samples` and cached in-process telemetry | Local system/container CLI probes and local OMLX admin HTTP endpoints | `/api/telemetry`; shared bearer boundary; telemetry may fall back to demo data when local backend is unavailable |",
        "| Guided onboarding preflight | Read-only view of settings, `cloud_providers`/`model_combos` rows, workspace permissions, and container-CLI presence | None: deliberately no outbound calls, keyring reads, or DNS resolution | `/api/onboarding/preflight`; shared bearer boundary, local-only snapshot, no secrets in the response |",
        "| CodeGraph and workspace view | Workspace files; no dedicated relational table | Local filesystem analysis only | `/api/codegraph/*`, `/api/workspace/files`; shared API bearer boundary and workspace/path checks |",
        "",
        "## Evidence and limitations",
        "",
        "- API routes are enumerated from FastAPI's live registered route objects; endpoint source anchors are resolved from `app.py` on each generation.",
        "- SQLite declarations are scanned from `app.py` and `runtime_persistence.py`. Generation fails on an undocumented or missing table and on an endpoint without subsystem/auth/source mapping.",
        "- The HTTP auth column records shared boundary coverage plus notable route-specific policy; it does not assert per-user authorization, which the code does not implement.",
        "- External destinations and process launches are documented as possible code paths. This generator does not contact them, and integration tests use local/mocked fixtures.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    try:
        content = render()
    except (InventoryError, OSError) as exc:
        print(f"Subsystem inventory generation failed: {exc}", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(endpoints())} endpoint entries, {len(TABLES)} table entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
