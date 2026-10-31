# Unified Execution Profiles and Resource Footprints (`M5-P3-02`)

`execution_profiles.py` (stdlib-only, inlined into `studio.py`) unifies permission rules,
resource footprints, admission barriers, and post-execution side-effect reconciliation
across managed in-process tools, subprocess execution (`run_bash`, `ctx_execute_python`,
`ctx_run_block`, `_run_skill_script`), and external MCP invocations (`ADR-0012`).

---

## 1. Verified Capability Descriptors

Every tool invocation is mapped by `resolve_capability_descriptor(tool_name, args, ...)`
to an immutable `CapabilityDescriptor` derived **exclusively from runtime-owned tool
metadata and active execution configuration** — untrusted model-supplied hints inside
`args` are ignored:

| Field | Values | Contract |
|---|---|---|
| `action_class` | `read_only`, `managed_edit`, `consequential` | Aligned with `execution_spine.classify_action`. |
| `execution_surface` | `managed`, `subprocess`, `external` | Distinguishes confined Python logic from child processes and MCP calls. |
| `isolation_posture` | `in_process_confined`, `container_qualified`, `container_hardened_unverified`, `host_opt_in_unsandboxed`, `disabled` | Never claims `container_qualified` unless container isolation has been verified (`ADR-0010`). |
| `verified_isolation` | `bool` | `True` only for `in_process_confined` and `container_qualified`. |
| `fs_capability` | `none`, `workspace_confined_read`, `workspace_targeted_write`, `workspace_broad_write`, `vault_read`, `vault_write` | Declared filesystem reach of the surface. |
| `network_capability` | `none`, `host_unrestricted`, `external_mcp` | Network reach (`none` for managed tools and `--network=none` container mode). |
| `process_capability` | `none`, `bounded_subprocess` | Whether child processes may be spawned. |
| `secret_access` / `vcs_hook_access` | `forbidden` | Secret files and `.git/hooks/*` / `.git/config` mutations are forbidden across every surface. |
| `side_effect_certainty` | `none`, `exact`, `bounded_read`, `unknown_conservative` | Determines whether narrow path barriers or conservative workspace barriers are required. |

---

## 2. Policy Parity Across Managed and Subprocess Surfaces

`evaluate_unified_policy(...)` runs inside both `app.authorize_tool_invocation` and
`UnifiedExecutionCoordinator.admit_profile` before any subprocess is spawned or any
managed file tool executes:

- **Protected secret paths (`error_code="unauthorized_path"`):** `.env` and `.env.*`
  variants (while allowing `.env.example`, `.env.sample`, `.env.template`, `.env.dist`),
  SSH/PGP/cloud credential stores (`.ssh/*`, `id_rsa`, `id_ed25519`, `id_ecdsa`,
  `id_dsa`, `.gnupg/*`, `.aws/*`, `.netrc`, `.npmrc`, `.pypirc`), private keys and
  keystores (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.keystore`, `*.jks`), and
  `credentials.json` / `secrets.json` / `secrets.yaml`.
- **Protected VCS hooks and configuration (`error_code="unauthorized_path"`):**
  `.git/hooks/*`, `.git/config`, `.git/config.worktree`, `.git/info/attributes`, and
  dangerous `git config` execution directives (`core.fsmonitor`, `core.sshCommand`,
  `core.hooksPath`, `core.pager`, `core.editor`, `alias.*`, `filter.*`, `credential.*`).
- **Workspace confinement and symlink refusal (`unauthorized_path` / `unsafe_path`):**
  parent traversal (`..`, `../...`), home/environment escapes (`~`, `~/...`, `$HOME`),
  outside-workspace absolute paths, and intra-workspace or escaping symlinks.
- **Subprocess Bypass Resistance (`inspect_subprocess_policy`):**
  - Quote-aware shell segment and redirection tokenization (`cat ".env"`, `head <.env`,
    `grep --file=.env`),
  - Targeted wildcard/character-class glob detection (`.en*`, `.e?v`, `.[e]nv`,
    `secrets/*.key`),
  - Brace-expansion (`{.env,README.md}`), ANSI-C hex/octal escapes (`$'\x2e\x65\x6e\x76'`),
    and command-substitution (`$(...)`, backticks) inspection,
  - Recursive inspection of inline shell payloads — `sh -c`, `bash -lc`, `zsh -c`,
    `dash -c`, `ksh -c`, `busybox sh -c`, also behind `env`/`nice`/`nohup` — up to three
    nesting levels, so `sh -c 'cat .env'` is refused exactly like `cat .env`,
  - Refusal of a shell that *reads its command from stdin* (`curl … | sh`,
    `cat payload.txt | bash`, `sh <<EOF`): no command-line inspection can see it,
  - Inspection of inline program text for interpreters there is no parser for
    (`perl -e`, `ruby -e`, `node -e`, `php -r`, `lua -e`): every path-like substring
    is checked against the same secret, VCS-control and confinement rules,
  - Python AST constant folding (`open`, `Path(...) / ...`, `os.path.join`, string `+`,
    `str.join`, `os.system`, and `subprocess.*`) for `ctx_execute_python`.

### What command inspection cannot see

Command inspection is one layer, not the boundary. These are known and are why
the sandbox (M5-P3-01) is the real containment, and why `unqualified` isolation
keeps host execution unavailable:

- **script file contents** — `sh build.sh` is inspected as a command line, not read;
  the run's *effects* are covered by post-execution footprint reconciliation
  (§4), which quarantines planted secrets, hook scripts and escaping symlinks;
- **paths assembled at runtime** — `perl -e '$p = ".e"."nv"; open(F,$p)'` builds the
  name after the scan; encoded payloads (`… | base64 -d | sh`) are the same class;
- **the `sh -c` nesting bound** — beyond three levels the innermost payload is not
  re-parsed;
- **everything outside the command line** — a compiled binary, a downloaded script,
  or a child process started by a tool that this policy does not front.

These limits are properties of inspecting text before execution. They do not
weaken the refusals above; they bound what the refusals can promise.

---

## 3. Resource Footprints and Admission Barriers

`derive_execution_profile(...)` pairs every `CapabilityDescriptor` with a
`ResourceFootprint`, and `UnifiedExecutionController` acquires shared/exclusive
`ResourceBarrier` locks atomically alongside `resource_admission.ResourceAdmissionScheduler`
capacity slots (`memory_bytes`, `action_slots`, `subprocess_slots`, `mutation_slots`):

| Footprint Scope | Representative Tools | `conservative` | Acquired Barriers |
|---|---|---|---|
| `exact_paths` (read) | `read_file`, `ctx_run_block(json-peek\|csv-peek)` | `False` | `workspace:{ws}` (`shared`) + `path:{ws}:{rel}` (`shared`) |
| `exact_paths` (write) | `write_file`, `edit_file` | `False` | `workspace:{ws}` (`shared`) + `path:{ws}:{rel}` (`exclusive`) |
| `bounded_workspace_read` | `list_files`, `grep_search`, `codegraph_*`, `shortcut-debt` | `False` | `workspace:{ws}` (`shared`) (+ `path:{ws}:{rel}` `shared` if scoped) |
| `vault` | `oc_read`/`oc_search`/`oc_lint` (`shared`), `oc_save_doc` (`exclusive`) | `False` | `opencontext:vault` (`shared` or `exclusive`) |
| `session_state` | `ctx_search`, `list_edit_batches`, `reconcile_task_actions` | `False` | `session:{sid}` (`shared`) |
| `conservative_workspace` | `run_bash`, `ctx_execute_python`, `ctx_run_block(test-summary\|first-error)`, `skill_execute_script`, `rollback_edits(dry_run=False)`, unknown tools | `True` | `workspace:{ws}` (`exclusive`) + conservative memory/subprocess/mutation slots |
| `external_and_workspace` | `mcp__<alias>__<tool>` | `True` | `mcp:{alias}` (`exclusive`) + `workspace:{ws}` (`exclusive`) |

### Conflict Semantics

- Concurrent `read_file` calls on the same path, or concurrent `write_file` calls on
  **disjoint** paths (`src/a.py` vs `src/b.py`), share `workspace:{ws}` (`shared`) and
  succeed concurrently.
- Concurrent `write_file` + `write_file` or `read_file` + `write_file` on the **same**
  normalized path fail closed immediately (`ok=False`, `conflict_denied=True`,
  `error_code="conflicting_action"`).
- Any in-flight conservative action (`run_bash`, `ctx_execute_python`, `mcp__*`, etc.)
  holds `workspace:{ws}` (`exclusive`), preventing concurrent reads, edits, or second
  shell commands in that workspace until its barrier is released.
- Barrier and scheduler leases are always released in `DurableActionBoundary`'s `finally`
  block across exceptions, task cancellations, and injected crash faults.

---

## 4. Post-Execution Observation and Reconciliation

Under the held barrier, `UnifiedExecutionCoordinator` snapshots non-secret workspace
metadata (`capture_footprint_state`) before execution and reconciles actual post-execution
mutations (`observe_and_reconcile`):

1. **Valid conservative expansion (`status="reconciled_expansion"`):**
   Files legitimately created, modified, or deleted by `run_bash` / `ctx_execute_python`
   are recorded in `$HARNESS_STORE_DIR/footprints/{session_id}.jsonl` (`0600` permissions)
   via `FootprintReconciliationLedger` and surfaced on `result["footprint_reconciliation"]`.
2. **Newly planted policy violations (`status="policy_violation"`):**
   If a subprocess dynamically computes and creates a forbidden secret file, `.git/hooks/*`
   script, or workspace-escaping symlink at runtime, `observe_and_reconcile` unlinks the
   newly planted artifact via `O_NOFOLLOW` (`quarantined_paths`) without touching outside
   targets, marks `ok=False` (`error_code="footprint_policy_violation"`), and appends an
   `execution/footprint_violation` harness event.
3. **Pre-existing protected file tampering or undeclared exact-scope expansion:**
   Pre-existing user files are **never unlinked**. Instead, unauthorized modification of
   an existing protected file or undeclared collateral mutation by an `exact_paths` tool
   fails closed with `ok=False`, `uncertain=True` (`status="uncertain"` in
   `runtime_actions`), causing `IntentLedger` to block subsequent consequential actions on
   that task until operator reconciliation (`M5-P1-02` / `M5-P1-03`).

---

## 5. Canonical Container Invocation (`sandbox_boundary.build_container_invocation`)

`sandbox_boundary.build_container_invocation(...)` is the single implementation of
hardened container CLI construction (`--pull=never`, `--network=none`, `--read-only`,
`--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--ipc=private`, `--pids-limit`,
`--memory`, `--cpus`, `--ulimit nofile=1024:1024`, `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m`,
single `/workspace` bind mount, and POSIX `--user uid:gid`), delegated to by both
`sandbox_boundary.sandbox_command` and `app._sandbox_command` and verified against
`scripts/qualify_environment.py` (`p3.product.argv_parity`).
