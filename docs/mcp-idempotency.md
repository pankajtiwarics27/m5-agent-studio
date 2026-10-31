# MCP Idempotency and External-Effect Reconciliation

`M5-P3-05` ([`ADR-0015`](adr/0015-mcp-idempotency-and-external-effect-reconciliation.md))
prevents blind replay of non-idempotent remote writes across MCP servers (`http`
Streamable HTTP `2025-03-26`, stateless `2026-07-28`, and `stdio` JSON-RPC) by combining
fail-closed connector semantics, wire-level action and idempotency binding, strict
read-only outcome reconciliation, uncertain-write session pauses, and central admission
enforcement.

## Connector semantics and explicit unsupported marking

[`mcp_client.py`](../mcp_client.py) classifies every discovered or configured MCP tool into
an immutable `McpToolSemantics` descriptor via `classify_mcp_tool_semantics`:

- **Read-only tools (`read_only=True`):** Tools with `annotations.readOnlyHint == True` or
  explicit `toolPolicies.{tool}.readOnly == True` (when not marked destructive) report
  `effect_kind="read_only"`, `idempotency_supported=True`,
  `idempotency_status="inherently_idempotent"`, and `replay_policy="safe_retry"`.
- **Mutating tools default fail-closed:** Every non-read-only MCP tool defaults to
  `idempotency_supported=False`, `idempotency_status="unsupported"`,
  `idempotency_mechanism="unsupported"`, and `replay_policy="non_idempotent"` (or
  `replay_policy="reconcilable"` when a valid `McpReconciliationSpec` is configured).
- **The hint is the connector's claim, not Studio's judgement.** `annotations` come
  from the remote server, so a server that marks a destructive tool `readOnlyHint: true`
  (and omits `destructiveHint`) is classified read-only, and a failed call to it is
  retried without the uncertain-write pause. Connectors are operator-configured and
  reviewable, and an operator `destructive: true` in the connector policy overrides the
  annotation — but the fail-closed default protects against *unclassified* tools, not
  against a server that mislabels itself.
- **Explicit connector opt-in:** Operators or tool descriptors can configure
  `toolPolicies.{tool}` on `/api/mcp/servers` (`idempotencySupported`,
  `idempotencyMechanism` in `http_header | jsonrpc_meta | argument_field | meta_and_arg`,
  `idempotencyArgField`, `destructive`, `requiresApproval`, and `reconciliation`).
  Unsupported idempotency is exposed explicitly on `/api/mcp/servers`, `/api/mcp/tools`,
  tool execution responses (`mcp_semantics`), and durable external-effect records.

## Action-ID and idempotency-key binding

Before dispatching `tools/call`, `bind_mcp_call_idempotency` binds deterministic correlation
and idempotency metadata without mutating undeclared business fields:

- **JSON-RPC `params._meta`:** Always attaches `m5.studio/actionId` and
  `m5.studio/idempotencyStatus`, while preserving `io.modelcontextprotocol/protocolVersion`
  on stateless `2026-07-28` transports. `tools/list` never carries `idempotencyKey` or
  `m5.studio/actionId`.
- **Supported idempotency keys:** When `idempotency_supported=True`, the key is the
  action id itself — one action, one key — and is emitted as `_meta.idempotencyKey`,
  `_meta["io.modelcontextprotocol/idempotencyKey"]`, and (when `idempotency_mechanism`
  is `argument_field` or `meta_and_arg`) `arguments[idempotency_arg_field]`. The
  canonical argument digest (`args_sha256`) is recorded separately on the durable
  record; it is deliberately *not* folded into the key, because two distinct actions
  with identical arguments must not share a key, and a re-serialised argument would
  otherwise look like a new write.
- **HTTP transport headers:** HTTP `tools/call` emits `X-Studio-Action-Id`,
  `Idempotency-Key` (the derived key when supported, or `action_id` transport fallback), and
  `X-MCP-Idempotency-Supported: true|false`.

## Durable external-effect store and strict reconciliation

`McpExternalEffectStore` persists connector policies and secret-redacted external-effect
records in `$HARNESS_STORE_DIR/mcp_effects/state.json` (mode `0o600`, temp-file write +
`os.fsync(fd)` + atomic `os.replace` + directory `os.fsync(dir_fd)`), keeping SQLite at
`SCHEMA_VERSION = 3`:

- **Redaction of the record:** argument names are matched after folding to
  `snake_case` and splitting camelCase, so `Authorization`, `X-Api-Key`,
  `accessToken`, `refreshToken`, `sessionKey`, `authentication` and `oauth` are all
  treated as credentials rather than as ordinary fields. Free text — `last_error`,
  `reconciled_output`, `reconciled_by` — is scrubbed before it is written: any
  registered secret value, plus the recognisable `<scheme> <credential>` shape
  (`Bearer`, `Basic`, `Token`), because those strings are remote-controlled and used
  to reach disk verbatim. This is a scrubber for shapes that can be recognised, not a
  guarantee for secrets Studio never held.
- **Pre-send vs post-send outcomes:** Validation, policy, or pre-connect errors record
  `failed_pre_send`; explicit JSON-RPC or `isError=True` tool responses record
  `failed_remote`; post-dispatch timeouts, resets, malformed payloads, or `stdio` EOF
  record `uncertain` and raise `McpAmbiguousOutcomeError`.
- **Strict queryable-outcome reconciliation (`McpReconciliationSpec`):** A write tool may
  declare a `read_only=True` allowlisted `query_tool` on the same MCP server with
  `query_args_template` (`$action_id`, `$idempotency_key`, or `$arg:{field}`),
  `result_path`, `match_field`, `match_source`, `committed_field` / `committed_equals`, and
  `authoritative_not_found`.
  - When the lookup proves the remote effect committed, the record transitions to
    `verified_committed` and resolves `DurableActionLedger` with
    `reason="pre_takeover_verified_committed"`. Subsequent replay or
    `WorkerLeaseCoordinator.acquire_or_takeover` returns `reconciled=True` and
    `already_committed=True` without dispatching a second remote write.
  - When `authoritative_not_found=True` and the lookup authoritatively confirms absence, the
    record transitions to `verified_not_applied` so recovery can safely re-execute.
  - Unverified empty lookups, tool errors, or transport failures keep the effect `uncertain`.

## Uncertain non-idempotent write safe pause and operator resolution

When a non-idempotent MCP write ends `uncertain` inside a harness session:

1. Studio transitions the session to `status="paused"` with
   `pause_reason="uncertain_external_effect"` and halts single-agent and GSD Wave 2 tool
   loops immediately so queued sibling calls in the same model turn cannot execute.
2. Subsequent mutating tool calls in that session are denied at admission with
   `error_code="uncertain_mcp_write_paused"`, while verified read-only MCP inspection and
   reconciliation probes remain permitted when no lingering subprocesses exist
   (`allow_readonly_inspection=True` on `ContainmentStore`).
3. `POST /api/harness/sessions/{session_id}/resume` returns `HTTP 409` while unresolved
   uncertain MCP writes remain on the session.
4. Operators inspect, reconcile, or resolve uncertain effects through authenticated session
   endpoints:
   - `GET /api/harness/sessions/{session_id}/mcp-effects`
   - `POST /api/harness/sessions/{session_id}/mcp-effects/reconcile`
   - `POST /api/harness/sessions/{session_id}/mcp-effects/{action_id}/resolve` (`resolution`
     set to `"committed"` or `"aborted"`). Replaying an operator-committed `non_idempotent`
     action still raises `CommittedActionReplayError` so no duplicate external side effect is
     created.

## Central gate anti-bypass

Every MCP `tools/call` — including agent invocations, direct `execute_mcp_tool_call`, and
reconciliation `query_tool` probes — passes through `authorize_tool_invocation`,
`DurableActionBoundary`, `UnifiedExecutionController`, and `ScopedGrantStore`:

- `_mcp_http_rpc` and `_mcp_stdio_rpc` call `execution_spine.require_active_admission` when
  `method == "tools/call"`, rejecting direct transport calls outside an admitted execution
  boundary.
- Disabled servers, non-allowlisted tools, `STUDIO_ALLOW_MCP_STDIO=False`, per-tool
  `requiresApproval`, and active capability revocations (`M5-P3-04`) fail closed before any
  outbound JSON-RPC write or `in_flight` ledger mutation occurs.

## Evidence

[`tests/test_mcp_idempotency_reconciliation.py`](../tests/test_mcp_idempotency_reconciliation.py)
verifies connector semantics classification and explicit unsupported marking, HTTP/`stdio`
action-ID and idempotency-key binding, `verified_committed` and `verified_not_applied`
reconciliation without duplicate external writes, uncertain non-idempotent write session
pauses and operator API resolution, `/mcp-effects/reconcile` plus `WorkerLeaseCoordinator`
lease-takeover reconciliation, and transport-level central admission gate enforcement.
