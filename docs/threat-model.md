# Threat model and security inventory

## Scope and security objectives

This inventory covers the local-first Studio process, its authenticated HTTP API,
browser client, configured model/provider and MCP connections, local SQLite and
credential storage, optional tool execution, skill installation, and the
generated standalone artifact. It describes the current v9.0.1 controls and
residual risks; it is not a penetration-test report or a guarantee of isolation
from a compromised host, kernel, dependency, provider, or container runtime.

The security objectives are:

1. Keep unauthenticated network callers away from state-changing and inference
   routes by default.
2. Prevent untrusted URLs, redirects, model output, MCP content, and workspace
   files from silently becoming authorization or execution instructions.
3. Keep provider and MCP credentials out of the SQLite history and audit event
   payloads.
4. Keep code execution, local MCP stdio, and installed skill code disabled until
   the operator explicitly opts in.
5. Preserve local data and fail closed when storage, credential, migration, or
   authorization prerequisites cannot be satisfied.

## Threat actors

| ID | Actor | Capability or motivation | Trust status |
|---|---|---|---|
| TA-1 | Remote unauthenticated caller | Sends HTTP requests to a non-loopback bind or exposed forwarded port; seeks data access, inference, or state changes. | Untrusted |
| TA-2 | Authenticated operator or compromised browser | Holds the bearer token or controls the local browser; can invoke the single-operator API and configure integrations. | Trusted for API access, not for every input |
| TA-3 | Malicious or compromised provider/MCP endpoint | Returns hostile content, redirects, tool descriptions, or instructions; may attempt credential or network pivoting. | Untrusted external system |
| TA-4 | Prompt-injection author or hostile workspace content | Places instructions in model output, repository files, search results, or MCP responses to influence tool selection. | Untrusted data |
| TA-5 | Local process with user-account access | Reads local files, SQLite history, configuration, or process state; may tamper with the operator's environment. | Outside application control |
| TA-6 | Compromised dependency, container runtime, or build input | Exploits the Python/Node/toolchain supply chain or weakens an optional execution boundary. | Outside application control |

The application has one operator trust domain. A bearer token is an API gate,
not a user identity, role system, or tenant boundary. This assumption is
source-linked in the [subsystem inventory](subsystem-dependency-inventory.md).

## Trust boundaries and assets

| Boundary | Assets at risk | Crossing data or action | Primary controls and source evidence |
|---|---|---|---|
| B1: network/browser to Studio HTTP API | Conversation history, provider/MCP configuration, workspace files, model execution, tool execution | HTTP requests, bearer token, browser Origin and Host headers | Loopback/default bind and startup token checks [`app.py#L2193`](../app.py#L2193); shared bearer/Host/Origin gate [`app.py#L2268`](../app.py#L2268); route map [`docs/subsystem-dependency-inventory.md#registered-http-endpoints`](subsystem-dependency-inventory.md#registered-http-endpoints) |
| B2: Studio to provider, local model, or MCP HTTP endpoint | Prompt contents, model output, API credentials, network reachability | HTTPS/HTTP requests and streamed responses | URL syntax and DNS/IP validation [`app.py#L650`](../app.py#L650), private-address policy [`app.py#L677`](../app.py#L677), pinned transport and redirect rejection [`app.py#L706`](../app.py#L706); credentials use the OS keyring [`app.py#L553`](../app.py#L553) |
| B3: untrusted content/model output to tool authorization | Filesystem, subprocess, MCP, and OpenContext actions | Model plans, workspace/MCP/search text, tool names and arguments | Untrusted-data instruction boundary [`app.py#L6043`](../app.py#L6043); deny-by-default policy [`app.py#L5977`](../app.py#L5977); centralized execution/audit [`app.py#L7053`](../app.py#L7053) |
| B4: Studio to workspace and optional execution runtime | Source files, credentials in mounted paths, host privileges, network | File paths, shell/Python commands, container mounts and environment | Confined path resolution [`app.py#L6369`](../app.py#L6369); secret/workspace preflight [`app.py#L372`](../app.py#L372); disabled/sandbox/host mode checks [`app.py#L353`](../app.py#L353); constrained command construction [`app.py#L403`](../app.py#L403) |
| B5: Studio to local persistence and credential store | History, prompts, tool metadata, provider/MCP secrets, schema integrity | SQLite rows, migrations, keyring reads/writes, audit events | Transactional migrations [`app.py#L975`](../app.py#L975); non-secret keyring references [`app.py#L553`](../app.py#L553); audit start before dispatch [`app.py#L6945`](../app.py#L6945) |
| B6: repository/build inputs to runtime artifacts | Source integrity, standalone behavior, deployment artifact | Python source, static UI, generated `studio.py`, installed skill files | Deterministic standalone build [`scripts/build_studio.py`](../scripts/build_studio.py); GitHub-only skill source and disabled skill code execution [`app.py#L1810`](../app.py#L1810) |

## Prioritized abuse-case inventory

Priority reflects the combination of plausible exposure, impact, and the
importance of preserving the local-first execution boundary. `P0` items must
remain fail-closed; `P1` items require explicit operator review or recovery;
`P2` items are residual risks requiring deployment or supply-chain controls.

| ID | Priority | Abuse case and impact | Mitigations and evidence | Residual risk |
|---|---|---|---|---|
| AC-001 | P0 | TA-1 reaches a remote bind without authentication and reads or changes local state. | Non-loopback startup requires a high-entropy token; shared API middleware checks bearer, Host, Origin, request limits, and selected rate limits. Evidence: `test_auth_cannot_be_disabled_on_remote_bind`, `test_explicit_api_token_precedes_local_token_file`. | A bearer token remains a single-operator deployment gate; there is no RBAC or tenant isolation. |
| AC-002 | P0 | TA-2 or TA-3 supplies a private, credentialed, or redirected URL to reach metadata, internal services, or an unintended destination. | URL syntax rejects embedded credentials; DNS results are checked immediately before I/O; private destinations require explicit opt-in; redirects and ambient proxy bypasses are rejected. Evidence: `test_outbound_url_blocks_private_literal_addresses_by_default`, `test_per_host_private_url_exception_is_narrow`, `tests/test_redirect_safety.py`. | Explicit private-URL opt-ins and trusted integrations remain operator risk. |
| AC-003 | P0 | A provider, MCP server, model, or workspace injects instructions that cause an unauthorized tool call or approval bypass. | External content is marked untrusted; registered tools, workflow policy, allowlists, and approval checks are enforced centrally; denials are audited. Evidence: `test_central_tool_authorization_is_deny_by_default`, `test_mcp_calls_enforce_enabled_and_registered_tool_allowlist`, `test_plan_and_unapproved_standard_policy_fail_closed`. | Prompt-injection resistance is not a formal model guarantee; an authenticated operator can intentionally approve a dangerous action. |
| AC-004 | P0 | Tool arguments or outputs leak credentials through SQLite audit history or provider configuration. | Secrets are stored in the OS keyring with non-secret SQLite references; the audit table stores hashes and metadata rather than raw arguments/output; keyring failures block persistence. Evidence: `test_keyring_reference_never_contains_secret`, `test_inbound_mcp_bridge_calls_share_central_policy_and_audit`. | Harness JSONL events and message snapshots may contain raw prompts, tool arguments, and outputs; they rely on local file permissions and configured retention. The local keyring, process memory, and non-secret history database still depend on host-account protection. |
| AC-005 | P0 | A workflow executes arbitrary shell/Python with host privileges or mounts a secret-bearing workspace. | Execution is disabled by default; sandbox mode requests no network, read-only root, dropped capabilities, resource limits, and no-new-privileges; host mode needs two explicit settings; preflight rejects known secret files and broad roots. Evidence: `test_execution_mode_requires_explicit_host_opt_in`, `test_sandbox_preflight_refuses_secret_files_and_overbroad_mounts`, `test_sandbox_command_enforces_container_isolation_flags`. | Host execution and container isolation remain privileged opt-ins and depend on the local runtime/kernel. |
| AC-006 | P0 | A path traversal or symlink escapes the workspace and exposes or modifies another local file. | Confined paths and discovered scanner candidates are resolved against the workspace root; secret-name checks apply to resolved scanner targets; worktree cleanup requires an exact registered worktree and preserves it on Git errors. Evidence: `test_resolve_confined_path_rejects_traversal_and_accepts_child`, `test_grep_and_workspace_scanners_reject_secret_and_external_symlinks`, `test_worktree_cleanup_failure_preserves_data_and_nested_paths`. | Filesystem permissions, concurrent symlink replacement races, and host behavior remain part of the deployment boundary. |
| AC-007 | P0 | Enabling MCP stdio launches an unexpected local executable or passes ambient credentials to it. | Stdio is disabled by default; configured command, args, and filtered environment are required; inbound bridge calls still pass central authorization and audit. Evidence: `test_inbound_mcp_bridge_calls_share_central_policy_and_audit`, `test_child_process_environment_excludes_credential_variables`. | Explicit stdio opt-in runs with host-user privileges and is not inside the code sandbox. |
| AC-008 | P1 | A schema migration partially applies, loses history, or leaves a failed upgrade unrecoverable. | Migrations are transactional, additive, versioned, and idempotent; verified SQLite backup/restore supports recovery. Evidence: `test_schema_migration_failure_rolls_back_partial_ddl`, `test_failed_migration_can_be_recovered_from_verified_backup`. | Backups are not encrypted by the application and must be protected by the operator. |
| AC-009 | P1 | A skill download places executable search code in the workspace and it runs without review. | Skill sources are restricted to GitHub HTTPS; selected files are copied; installed `search.py` execution requires an explicit opt-in. Evidence: `test_skill_installer_only_accepts_github_sources`. | A trusted GitHub revision can still contain malicious prompt or code content; review and pinning remain operator responsibilities. |
| AC-010 | P1 | Large requests, subprocess output, or expensive workflows exhaust local resources. | Request bodies, workflow limits, subprocess timeouts, output bytes, and selected per-client rate limits are bounded. Evidence: `test_subprocess_output_is_drained_but_memory_bounded`; route and subsystem inventory. | In-memory limits are not distributed across multiple workers and do not provide full availability protection. |
| AC-011 | P2 | TA-5 reads or tampers with local history, keyring entries, generated files, or process state. | Restrictive local file permissions, OS credential storage, bearer gating, and documented single-operator deployment assumptions reduce exposure. | SQLite history is not encrypted at rest; a local account or host compromise is outside the application trust boundary. |
| AC-012 | P2 | TA-6 compromises a dependency, build tool, container runtime, or generated artifact. | Versioned project constraints, deterministic standalone regeneration, no automatic sandbox image pull, and generated-artifact checks provide review points. | No external security audit, dependency provenance attestation, kernel isolation guarantee, or formal compliance certification is provided. |

## Prioritized mitigation plan

| Priority | Current requirement | Owner/checkpoint |
|---|---|---|
| P0 | Keep authentication, URL validation, central tool authorization/audit, path confinement, and execution opt-ins fail-closed. | Changes to these controls require focused negative tests and review of this inventory. |
| P1 | Keep migration recovery, resource bounds, and skill-source restrictions tested and documented. | Run focused local tests before release; protect backups and review installed skills. |
| P2 | Reduce host and supply-chain residual risk with filesystem encryption, least-privilege accounts, pinned dependencies/artifacts, trusted container runtimes, and egress controls. | Deployment/operator controls; not silently enabled by Studio. |

## Review status and evidence limits

This inventory was created from the source-linked AS-003 subsystem map and the
current security tests. It documents implemented controls and residual risks;
it does not claim that optional providers, MCP servers, Docker/Podman, or cloud
services are available or safe. The local test suite does not perform a
penetration test, execute live cloud calls, validate every supported operating
system, or prove prompt-injection resistance. Re-review this document whenever
an HTTP route, outbound destination, credential path, execution mode, tool
policy, migration, or generated-artifact boundary changes.

Focused evidence files include `tests/test_security_and_quality.py`,
`tests/test_redirect_safety.py`, `tests/test_pinned_transport.py`, and the
source-linked `docs/subsystem-dependency-inventory.md`.
