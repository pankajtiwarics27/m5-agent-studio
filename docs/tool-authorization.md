# Central tool authorization and audit

AS-010 centralizes authorization and local audit for tool execution without changing the SQLite schema or adding dependencies. The trust boundary is the tool-invocation boundary—not the HTTP route boundary—so an authenticated request can still be denied by tool policy.

## Execution paths covered

- Built-in harness and Direct Chat tools pass through `_execute_harness_tool`.
- Direct MCP playground calls, automated MCP research, and harness-dispatched MCP tools use the policy-checked MCP entry point or execute under the already-audited harness boundary.
- The bundled `--mcp` stdio server routes inbound `tools/call` requests through the same authorization/audit service using a distinct `mcp_bridge__` policy identity.
- Harness approval rejections, unknown tool names, execution-mode denials, and the SDLC backlog guard are audited as denials rather than returned outside the policy boundary.

Each entry path requires a registered built-in name, a valid inbound bridge name, or a correctly parsed MCP name whose server is enabled and whose remote tool is in its enabled-tool allowlist. Unknown or malformed names deny by default.

## Policy compatibility

| Policy | Existing behavior retained by the central decision |
|---|---|
| `standard` | Registered built-ins remain available; shell/Python/test execution requires approval and an enabled execution mode. MCP calls follow each server's `auto`, `always_ask`, or `inherit` setting. |
| `autonomous` | Registered built-ins remain available subject to execution-mode and sandbox controls. MCP servers marked `always_ask` still require approval. |
| `plan` | Read-only behavior: no MCP, file writes/edits, shell/Python/test execution, or OpenContext saves. |
| Invalid policy | Normalizes to `standard`, matching the existing safe fallback. |

The manual MCP playground is an authenticated, explicit one-call action. The bundled inbound MCP bridge treats each `tools/call` request as one-call intent because that opted-in local process interface has no interactive approval channel. Both remain subject to explicit registries and server/tool allowlists. MCP stdio process launch remains disabled unless separately opted in.

## Audit and failure behavior

A durable `started` audit row is committed before dispatch. Completion updates the same row to `completed`, `denied`, or `failed`; rows include session/tool/policy, explicit-approval state, success, elapsed time, and SHA-256 hashes of arguments and output. Raw arguments and output are not persisted in the audit table.

If policy evaluation fails, the call is denied. If the audit start cannot be written, dispatch does not happen. If execution raises, the attempt is finalized as `failed` before the error is returned/propagated. If finalization fails after execution, the durable `started` row remains and the in-memory result carries an `audit_warning`; SQLite audit is local evidence, not tamper-proof logging.

## Remote approval scoping and replay protection (AS-027)

Sensitive remote actions — tool invocations that the central policy marks as
requiring approval — are decided through a session-scoped, exactly-once,
replay-protected approval flow:

- **Session scoping.** A pending approval carries the id of the harness
  session that created it. The authenticated `POST /api/harness/approve`
  endpoint requires the submission to carry the same `session_id`; a
  cross-session decision is rejected with a distinct `403` while the pending
  request stays intact for its originating session. An absent or unknown
  `session_id` is rejected.
- **Replay protection.** Every resolved approval id (approved, rejected,
  aborted, or timed out) is retained in a bounded, time-to-expire replay
  tombstone. Re-submitting a resolved id fails closed with a `409`
  ("already resolved") instead of being re-approved or returning a generic
  `404`. The tombstone map is bounded and pruned on access, so it cannot grow
  unboundedly.
- **Exactly-once resolution.** The pending slot is consumed atomically when a
  decision is recorded; a timed-out approval is mirrored to the recorded final
  decision (never silently turned into a reject by the wait side).
- **Durable trail.** Approval requests and decisions are appended to the
  per-session harness event log; the central tool audit table records the
  underlying tool attempt. Raw arguments and outputs are hashed, not stored.

The UI's `approval_required` events now include the originating `session_id`
so a client can submit the scoped decision. No new dependencies, HTTP routes,
or public API fields were added; the existing `POST /api/harness/approve`
contract gains an optional `session_id` field and two distinct rejection
statuses (`403` scope mismatch, `409` replay).

## Evidence

Focused contract coverage is in `tests/test_audit_log.py` and `tests/test_security_and_quality.py`: allowed and denied policy decisions, approval rejection, unknown tools, direct and inbound MCP auditing, hashed-only audit privacy, audit-start fail-closed behavior, execution failure finalization, session-scoped approval decisions, cross-session rejection, replay rejection, timeout tombstoning, and durable event-log records. No live provider, cloud MCP endpoint, or external service is contacted by these tests.
