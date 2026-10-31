---
id: ADR-0015
title: mcp-idempotency-and-external-effect-reconciliation
status: accepted
date: 2026-10-31
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0015: MCP connector idempotency semantics, external-effect reconciliation, and uncertain-write safe pause

## Context

`M5-P3-05` requires preventing blind replay of non-idempotent remote writes across MCP
servers (`http` Streamable HTTP `2025-03-26` / stateless `2026-07-28`, and `stdio`
JSON-RPC) while preserving `ADR-0003` SQLite schema discipline (`SCHEMA_VERSION = 3`) and
`ADR-0002` / `ADR-0014` central action admission:

1. **Over-optimistic idempotency assumptions on HTTP `tools/call`:** Previously,
   `mcp_client.py` attached an `Idempotency-Key` HTTP header carrying `action_id` on HTTP
   `tools/call`, yet generic MCP servers and downstream SaaS connectors rarely deduplicate
   on HTTP `Idempotency-Key` unless explicitly implemented, and `stdio` MCP servers have
   no HTTP transport headers at all. Treating every MCP tool as implicitly idempotency-safe
   masks connectors where replay creates duplicate issues, deployments, charges, or chat
   posts.
2. **Lack of structured JSON-RPC action/idempotency binding:** JSON-RPC `tools/call`
   payloads carried `name` and `arguments` (plus stateless protocol metadata on `2026`
   transports) without structured `_meta` action identifiers, explicit idempotency status
   markers, or optional argument-field injection for connectors that accept a client
   idempotency token inside `arguments`.
3. **No MCP queryable-outcome reconciliation on ambiguous failures or lease takeover:**
   `DurableActionLedger` (`M5-P1-03`) halted ambiguous `mcp__*` writes as `uncertain`, and
   `WorkerLeaseCoordinator` reconciled workspace file mutations via content hashes, but no
   mechanism existed to run a read-only connector lookup tool (such as fetching a ticket by
   `client_request_id` or deployment tag) through the central execution gate to prove
   whether an interrupted write committed or never applied before deciding whether to
   short-circuit or safely re-execute.
4. **Silent turn continuation after uncertain non-idempotent writes:** When a non-idempotent
   MCP write failed after transport dispatch (read timeout, peer reset, malformed body, or
   `stdio` subprocess EOF), `execute_mcp_tool_call` recorded `uncertain` on that single
   `action_id` and returned a tool error, allowing an autonomous single-agent or GSD Wave 2
   turn loop to immediately issue a second MCP write under a fresh `action_id` and create
   duplicate external side effects.
5. **Transport-level bypass risk:** Direct callers of `_mcp_http_rpc` or `_mcp_stdio_rpc`
   with `method == "tools/call"` were not rejected at the wire helper if invoked outside an
   active `DurableActionBoundary` admission context, and MCP prompt/discovery settings
   needed explicit connector-policy sanitization and central gate alignment.

## Decision

Extend [`mcp_client.py`](../../mcp_client.py), [`side_effect_ledger.py`](../../side_effect_ledger.py),
[`execution_spine.py`](../../execution_spine.py), [`execution_profiles.py`](../../execution_profiles.py),
[`process_supervision.py`](../../process_supervision.py), and [`app.py`](../../app.py) with
explicit MCP connector semantics, wire-level idempotency binding, strict queryable-outcome
reconciliation, uncertain-write session pause, and transport-level admission enforcement:

1. **Fail-closed MCP tool semantics (`McpToolSemantics`, `classify_mcp_tool_semantics`):**
   - Verified `read_only=True` tools (`annotations.readOnlyHint == True` or explicit policy
     override) are classified `effect_kind="read_only"`,
     `idempotency_status="inherently_idempotent"`, `replay_policy="safe_retry"`.
   - Every non-read-only MCP tool defaults fail-closed to `idempotency_supported=False`,
     `idempotency_status="unsupported"`, `idempotency_mechanism="unsupported"`, and
     `replay_policy="non_idempotent"` (or `"reconcilable"` when a valid
     `McpReconciliationSpec` is configured on that tool).
   - Operators or server descriptors may opt a tool into `idempotency_supported=True` via
     `toolPolicies.{tool_name}` (`idempotencySupported`, `idempotencyMechanism`,
     `idempotencyArgField`, `destructive`, `requiresApproval`, `reconciliation`) or MCP
     `annotations` (`idempotentHint`, `x-idempotencySupported`). Unsupported idempotency is
     surfaced explicitly on server summaries, `/api/mcp/tools`, tool execution payloads,
     and durable external-effect records.
2. **Action-ID and idempotency-key binding (`bind_mcp_call_idempotency`):**
   - Every `tools/call` binds `m5.studio/actionId` and `m5.studio/idempotencyStatus` into
     JSON-RPC `params._meta`, preserving stateless `2026-07-28`
     `io.modelcontextprotocol/protocolVersion` metadata while leaving `tools/list` free of
     action or idempotency keys.
   - HTTP `tools/call` emits `X-Studio-Action-Id: {action_id}` and `Idempotency-Key: {key}`
     (deterministic SHA-256 over server alias, tool name, action ID, and canonical
     arguments when supported, or `{action_id}` fallback for transport compatibility) plus
     `X-MCP-Idempotency-Supported: true|false`.
   - JSON-RPC `_meta.idempotencyKey` and `_meta["io.modelcontextprotocol/idempotencyKey"]`
     are emitted only when `idempotency_supported=True`. When
     `idempotency_mechanism="argument_field"` (or `"meta_and_arg"`), the deterministic key
     is injected into `arguments[idempotency_arg_field]` unless already caller-supplied,
     never mutating arbitrary undeclared business fields.
3. **File-backed external-effect ledger (`McpExternalEffectStore`):**
   - Persist connector policies and secret-redacted external-effect records (`pending`,
     `committed`, `failed_pre_send`, `failed_remote`, `uncertain`, `verified_committed`,
     `verified_not_applied`, `operator_resolved_committed`, `operator_resolved_aborted`) in
     `$HARNESS_STORE_DIR/mcp_effects/state.json` using mode `0o600` temp-file writes,
     `os.fsync(fd)`, atomic `os.replace`, and parent-directory `os.fsync(dir_fd)`.
   - Keep `SCHEMA_VERSION = 3` untouched (`ADR-0003`) and preserve single-file `studio.py`
     inlining without adding a new sibling module.
4. **Strict queryable-outcome reconciliation (`McpReconciliationSpec`, `reconcile_mcp_effects_async`):**
   - A write tool's `McpReconciliationSpec` names a verified `read_only=True` allowlisted
     `query_tool` on the same MCP server, builds deterministic lookup arguments from
     `action_id`, `idempotency_key`, or recorded `arg:{field}` parameters, and executes the
     lookup through the central `execute_mcp_tool_call` admission gate.
   - Positive structural matches (`result_path`, `match_field` against `action_id`,
     `idempotency_key`, or `arg:{field}`, and optional `committed_field` /
     `committed_equals`) transition the effect to `verified_committed` and resolve
     `DurableActionLedger` via `reason="pre_takeover_verified_committed"`, causing replay
     or worker-lease takeover to short-circuit (`reconciled=True`, `already_committed=True`)
     with zero duplicate remote calls.
   - Negative lookups transition to `verified_not_applied` only when
     `authoritative_not_found=True` and `not_found_Statuses` / empty collection rules
     match; transport errors, tool errors, or unverified empty results remain `uncertain`.
   - `WorkerLeaseCoordinator.reconcile_task_effects` invokes the registered MCP
     reconciler even when callers omit `known_action_args`, sourcing redacted metadata from
     `McpExternalEffectStore`.
5. **Uncertain non-idempotent write safe pause and operator resolution:**
   - When a non-idempotent MCP write ends `uncertain` inside a harness session, Studio
     transitions the session to `status="paused"` (`pause_reason="uncertain_external_effect"`),
     halts single-agent and GSD Wave 2 loops before any subsequent tool call in the model
     turn can run, blocks subsequent mutating tool admissions in that session with
     `error_code="uncertain_mcp_write_paused"`, and refuses `/resume` (`HTTP 409`) until
     reconciled or operator-resolved.
   - `ContainmentStore.find_conflicting_holds` permits `allow_readonly_inspection=True` on
     `mcp:{alias}` holds when `lingering_pids == ()`, allowing read-only MCP reconciliation
     and inspection queries while keeping all MCP writes blocked.
   - Operators inspect and resolve uncertain effects via
     `GET /api/harness/sessions/{session_id}/mcp-effects`,
     `POST /api/harness/sessions/{session_id}/mcp-effects/reconcile`, and
     `POST /api/harness/sessions/{session_id}/mcp-effects/{action_id}/resolve`
     (`committed` or `aborted`), with re-execution of an operator-committed
     `non_idempotent` action still rejected by `CommittedActionReplayError`.
6. **Wire-level central gate anti-bypass:**
   - `_mcp_http_rpc` and `_mcp_stdio_rpc` call `execution_spine.require_active_admission`
     whenever `method == "tools/call"`, blocking direct helper calls that attempt to bypass
     `DurableActionBoundary` and `ScopedGrantStore`.
   - `authorize_tool_invocation` checks `STUDIO_ALLOW_MCP_STDIO` and per-tool
     `requiresApproval` before durable `in_flight` intent creation so denied calls never
     dirty the ledger.

## Alternatives

- **Treat HTTP `Idempotency-Key` headers as proof of connector idempotency.**
  Rejected: most remote MCP servers ignore HTTP `Idempotency-Key` or proxy to APIs without
  deduplication, and `stdio` MCP servers have no HTTP headers. Explicit opt-in with
  fail-closed `idempotency_status="unsupported"` prevents false safety claims.
- **Auto-retry uncertain writes when a lookup query returns empty without `authoritative_not_found`.**
  Rejected: eventual consistency or partial search indexes can return empty immediately
  after a write succeeds. Requiring `authoritative_not_found=True` for
  `verified_not_applied` keeps reconciliation sound.
- **Add new SQLite tables and bump `SCHEMA_VERSION` to `4` for MCP effect state.**
  Rejected: `ADR-0003` freezes `SCHEMA_VERSION = 3` unless strictly unavoidable; durable
  `0o600` fsynced JSON storage in `$HARNESS_STORE_DIR/mcp_effects/state.json` paired with
  the existing SQLite `durable_action_intents` ledger satisfies durability without schema
  churn.
- **Allow reconciliation queries to call `_mcp_http_rpc` / `_mcp_stdio_rpc` directly.**
  Rejected: bypassing the central admission gate would circumvent disabled-server checks,
  tool allowlists, `STUDIO_ALLOW_MCP_STDIO`, and scoped-grant revocations (`M5-P3-04`).

## Tradeoffs

- Conservative default classification (`idempotency_supported=False`,
  `replay_policy="non_idempotent"`) pauses autonomous runs after transport timeouts on
  mutating MCP tools until either a configured read-only reconciliation spec proves the
  outcome or an operator resolves the effect. This trades unattended continuation during
  network faults for zero silent duplicate external side effects.
- File-backed `McpExternalEffectStore` serializes mutations through an in-process lock and
  atomic file replacement (`os.fsync` on file and parent directory), matching Studio's
  single-process runtime architecture (`ADR-0005` through `ADR-0014`) rather than
  multi-node distributed consensus.
- Secret redaction scrubs sensitive argument keys (`token`, `secret`, `password`,
  `api_key`, `authorization`, `cookie`, `credential`, `private_key`) before persisting
  `arguments_redacted` in `McpExternalEffectStore`, which means reconciliation `arg:{field}`
  templates can reference non-secret business identifiers (such as `client_request_id` or
  `slug`) rather than redacted credential fields.

## Review Status

Not reviewed. Accepted as the architectural record for `M5-P3-05`; independent human
review has not yet been performed. See [`docs/mcp-idempotency.md`](../mcp-idempotency.md)
and [`tests/test_mcp_idempotency_reconciliation.py`](../../tests/test_mcp_idempotency_reconciliation.py).
