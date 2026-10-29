# Security model and safe deployment

## Scope

M5 Agent Studio can route prompts to local/cloud LLMs, store conversation and code context, connect to MCP servers, install skill content, and (when explicitly enabled) execute tools. Treat it as a privileged developer application. It is not a multi-tenant service and is not a hardened remote execution platform.

## Safe defaults in v9.0.1

The generated HTTP inventory is in [`docs/route-security-matrix.md`](docs/route-security-matrix.md); it documents shared middleware coverage, not route-specific roles or tenant authorization.

- Loopback bind by default; non-loopback bind requires `STUDIO_API_TOKEN` with at least 32 characters. FastAPI docs/OpenAPI endpoints are disabled by default.
- Bearer auth is required on `/api/*` and `/v1/*` unless `STUDIO_AUTH_DISABLED=1` is explicitly enabled on loopback. If the token is unexpectedly unavailable (for example, the ASGI lifespan did not initialize), protected routes fail closed with `503`; they never silently become anonymous. Same-origin checks cover browser state-changing requests. Host and proxy-origin allowlists are configurable. Request bodies are capped and expensive workflows use a per-client-IP in-memory sliding-window limit; these limits are not distributed across multiple workers.
- Provider API keys, MCP auth tokens, and credential-like MCP headers/environment values are stored as references in SQLite and values in the OS credential store. Existing plaintext database entries are migrated on startup. Keep a backup before upgrading; if keyring migration fails, startup intentionally fails rather than serving the old plaintext values.
- Private/HTTP outbound URLs are blocked unless a trusted operator opts in. `STUDIO_PRIVATE_URL_HOSTS` permits narrower explicit per-host exceptions; built-in local endpoints are individually recognized. The broader `STUDIO_ALLOW_PRIVATE_URLS=1` compatibility switch remains available. The custom transport resolves and validates immediately before I/O, connects to the exact validated IP while preserving the original Host/TLS SNI, and rejects redirects. Ambient proxy variables are bypassed to prevent proxy-based pinning bypass; proxy-required deployments need an explicit reviewed integration.
- Code execution, MCP stdio launch, and execution of installed skill search scripts are disabled by default. Code execution can run through an opt-in Docker/Podman runner with no network, read-only container root, dropped Linux capabilities, resource limits, and only the session workspace mounted. Child processes receive a small environment instead of the full Studio process environment.
- Harness tool attempts are durably recorded before execution in a hash/metadata audit table; that table does not store raw tool arguments or output and is exposed by the authenticated `/api/harness/audit` route. Separately, harness event JSONL and message snapshot files can contain raw prompts, arguments, and outputs. They are local user data, are not included in the SQLite history export, and are removed by configured history retention with their expired session. The audit table is not tamper-proof.
- Workspace scanners resolve discovered file paths and reject symlinks that escape the session root; secret-name checks are applied to resolved targets as well as requested paths. This is filename/path-based defense-in-depth, not detection of arbitrary secrets embedded in ordinary files. Tool results are passed to models as JSON-escaped untrusted data, with system guidance not to follow instructions inside workspace/MCP/search content. Prompt-injection defenses remain model-dependent and are not a hard isolation boundary.
- Requested Git worktree creation fails closed; the service never silently falls back to editing the primary working directory.
- SQLite and harness event/message files use restrictive Unix permissions where supported.

## Recommended local setup

For local UI/API use, keep `STUDIO_HOST=127.0.0.1` and do not forward port 3000 publicly. A random token is generated on first startup and stored under `~/.config/m5-agent-studio/api-token`; the browser prompts for it. For managed deployments, set `STUDIO_API_TOKEN` explicitly. `STUDIO_AUTH_DISABLED=1` is available only as a deliberate convenience for trusted loopback-only development; do not use it on shared machines.

For non-loopback deployments, use a trusted TLS reverse proxy, set a random `STUDIO_API_TOKEN`, configure `STUDIO_ALLOWED_HOSTS` and `STUDIO_ALLOWED_ORIGINS`, and restrict firewall access. Do not put the bearer token into a URL or commit it. Avoid shared machines and multi-user deployment; there is no user/tenant isolation model.

## Explicitly unsafe opt-ins

- `STUDIO_EXECUTION_MODE=host` together with `STUDIO_ALLOW_HOST_EXECUTION=1`: runs shell/Python code as the OS user that launched Studio. This is unsandboxed and does not isolate filesystem, network, credentials, child processes, or system resources. The deprecated compatibility setting `STUDIO_ALLOW_HOST_EXECUTION=1` without an explicit mode selects this unsafe host mode.
- `STUDIO_ALLOW_MCP_STDIO=1`: launches configured local MCP commands with explicit configured environment values. Review each command, args, executable path, and env before enabling.
- `STUDIO_ALLOW_SKILL_CODE=1`: executes a selected skill's search script. Review the repo and pin a trusted revision before enabling.
- `STUDIO_ALLOW_PRIVATE_URLS=1`: allows private-address and HTTP outbound destinations and weakens SSRF protection. Use only for trusted integrations on a controlled network.

The Docker/Podman runner is an opt-in defense-in-depth boundary, not a claim of perfect isolation from container-runtime/kernel vulnerabilities. It uses a read-only base filesystem, mounts only the session workspace read-write, disables networking, drops capabilities, applies CPU/memory/PID limits, and does not pass host credentials into the container. Pre-pull and pin a trusted image; the runner uses `--pull=never`. A workspace preflight rejects the home/root directory, known secret filenames, credentialed Git remote URLs, inaccessible trees, and scans exceeding 100,000 entries; it is filename-based and cannot detect every secret embedded in arbitrary files. For higher assurance, use a dedicated VM/runtime host and egress controls. MCP stdio and explicitly enabled host execution remain outside this sandbox.

## Data handling

The SQLite history database, OpenContext documents, harness JSONL events, message snapshots, workspace files, and generated plans can contain sensitive prompts, source code, and tool output. Credential fields are moved to the OS credential store, but **the rest of the history database is not encrypted at rest**. Use filesystem encryption, restrictive account permissions, retention/cleanup controls, and protected backups. Be mindful that sending a prompt to a cloud provider or MCP endpoint shares that prompt with the configured remote service.

## Known limitations to resolve before enterprise deployment

1. No multi-user identity, role model, or audit-grade authentication/session management; bearer token is a deployment gate only.
2. Optional Docker/Podman sandbox is implemented for harness shell/Python/test commands, but has not been runtime-tested across supported platforms; containers share the host kernel. Explicit host mode and MCP stdio remain privileged when opted in.
3. DNS validation is pinned to the TCP connection and redirects are rejected, but the custom transport uses httpcore internals and has not been validated across supported platforms or unusual TLS/proxy environments. Keep egress controls in place and run the local provider/MCP contract tests on target platforms.
4. SQLite conversation/history data is not encrypted at rest and there is no retention policy UI yet.
5. SDLC acceptance now fails closed on verification and critic verdict, but evidence quality still relies partly on model review; do not equate it with a formal test attestation.
6. No security audit, external penetration test, or formal compliance certification has been performed.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to a repository maintainer through a private contact channel. Do not open a public issue or discussion, or publish exploit details, live credentials, private prompts, or personal data. This repository does not publish a dedicated security email address; if you do not have a private maintainer contact route, ask a maintainer to arrange one without disclosing sensitive details publicly. Do not assume GitHub private vulnerability reporting is enabled. Share only the minimum information needed for initial triage.

Please state the affected version or commit, impact, and safe reproduction details when possible. Maintainers will coordinate any fix and disclosure timing with the reporter; no response-time guarantee is currently published.
