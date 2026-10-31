"""Unified execution profiles, verified capability descriptors, resource footprints, and expansion reconciliation (M5-P3-02).

This stdlib-only module enforces one consistent permission and concurrency model
across managed in-process tools, sandboxed/host subprocess tools, and external MCP
operations:

1. **Verified capability descriptors** (:class:`CapabilityDescriptor`,
   :func:`resolve_capability_descriptor`):
   - Server-derived descriptors specifying action class, execution surface,
     isolation posture, honest ``verified_isolation`` truth (never claiming
     container isolation is verified unless live qualification succeeded),
     filesystem/network/process capabilities, forbidden secret/VCS-hook access,
     and side-effect certainty.
   - Untrusted model fields inside tool arguments are never consulted.
2. **Unified managed & subprocess policy** (:class:`PolicyViolation`,
   :func:`evaluate_unified_policy`):
   - Applies the same workspace-confinement, symlink-rejection, secret-file,
     protected-VCS-hook/config, destructive-command, and ``plan`` read-only
     invariants to ``run_bash``, ``ctx_execute_python``, ``ctx_run_block``, and
     ``skill_execute_script`` as to managed ``read_file`` / ``write_file`` /
     ``edit_file`` tools — closing shell quoting, globbing, hex-escape, path
     traversal, and inline-Python bypasses.
3. **Conservative footprints & barrier admission** (:class:`ResourceFootprint`,
   :func:`derive_execution_profile`, :class:`UnifiedExecutionCoordinator`):
   - Exact tools reserve narrow ``shared`` (read) or ``exclusive`` (write) path
     barriers plus a ``shared`` workspace barrier so disjoint file operations can
     proceed while same-path read/write and write/write conflicts are denied.
   - Operations with unknown side effects (arbitrary shell commands, Python
     snippets, skill scripts, multi-file rollbacks, external MCP tools, and
     unregistered tools) reserve a conservative ``exclusive`` workspace barrier
     and subprocess/mutation capacity slots via :mod:`resource_admission`.
4. **Post-execution expansion observation & reconciliation**
   (:func:`capture_footprint_state`, :func:`observe_and_reconcile`,
   :class:`FootprintReconciliationLedger`):
   - Snapshots workspace metadata and protected-file SHA-256 digests before
     execution, diffs post-execution mutations, reconciles valid conservative
     expansions into a durable owner-only JSONL ledger, quarantines newly
     planted forbidden artifacts (secrets, active ``.git/hooks``, escaping
     symlinks), and fails closed (``ok=False``, ``uncertain=True`` when
     existing protected or undeclared exact-scope state changed).

Inlined into ``studio.py`` by ``scripts/build_studio.py``; keep imports strictly
to the Python standard library plus earlier ``INLINED_MODULES`` siblings.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import execution_spine
import process_supervision
import resource_admission
import sandbox_boundary
import side_effect_ledger
import workspace_snapshots

EP_SCHEMA_VERSION = 1

ExecutionSurface = Literal["managed", "subprocess", "external"]
IsolationPosture = Literal[
    "in_process_confined",
    "container_qualified",
    "container_hardened_unverified",
    "host_opt_in_unsandboxed",
    "disabled",
    "external_mcp",
]
FilesystemCapability = Literal[
    "none",
    "workspace_read",
    "workspace_targeted_write",
    "workspace_broad_write",
]
NetworkCapability = Literal[
    "none",
    "configured_provider",
    "host_unrestricted",
    "external_mcp",
]
ProcessCapability = Literal["none", "bounded_subprocess"]
SideEffectCertainty = Literal["exact", "bounded_read", "unknown_conservative"]
FootprintScope = Literal[
    "exact_paths",
    "bounded_workspace_read",
    "conservative_workspace",
    "external_and_workspace",
    "vault",
    "session_state",
]
ReconciliationStatus = Literal[
    "clean",
    "reconciled_expansion",
    "scope_violation",
    "policy_violation",
]

BarrierMode = Literal["shared", "exclusive"]
DEFAULT_ACTION_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024 * 1024  # 8 GiB concurrent tool pool
DEFAULT_READ_FOOTPRINT_BYTES = 16 * 1024 * 1024  # 16 MiB
DEFAULT_MANAGED_WRITE_FOOTPRINT_BYTES = 32 * 1024 * 1024  # 32 MiB
DEFAULT_CONSERVATIVE_FOOTPRINT_BYTES = 128 * 1024 * 1024  # 128 MiB non-subprocess conservative
DEFAULT_SUBPROCESS_FOOTPRINT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB (matches container --memory=2g)
DEFAULT_ACTION_SLOTS = 32
DEFAULT_SUBPROCESS_SLOTS = 4
DEFAULT_MUTATION_SLOTS = 16
DEFAULT_MAX_FOOTPRINT_SCAN_ENTRIES = 25_000
DEFAULT_MAX_FOOTPRINT_LEDGER_BYTES = 4 * 1024 * 1024
DEFAULT_RETAIN_FOOTPRINT_RECORDS = 1_000

_EP_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EP_CMD_SUBST_DOLLAR_RE = re.compile(r"\$\(([^()]{1,4000})\)")
_EP_CMD_SUBST_BACKTICK_RE = re.compile(r"`([^`]{1,4000})`")
_EP_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_EP_REDIR_PREFIX_RE = re.compile(r"^(?:\d*>>?|\d*<<?|\d*<>|&>>?|>&)\s*(.*)$")
_EP_ANSI_C_QUOTE_RE = re.compile(r"\$'((?:\\.|[^'])*)'")
_EP_HEX_ESCAPE_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_EP_OCT_ESCAPE_RE = re.compile(r"\\([0-7]{1,3})")
_EP_GLOB_CHARS_RE = re.compile(r"[*?\[\]]")
_EP_BRACE_SIMPLE_RE = re.compile(r"^(.*)\{([^{}]+)\}(.*)$")

_EP_EXEMPT_SECRET_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        ".env.test.example",
    }
)
_EP_SECRET_DIR_NAMES: frozenset[str] = frozenset({".ssh", ".gnupg", ".aws", ".secrets"})
_EP_SECRET_EXACT_NAMES: frozenset[str] = frozenset(
    set(sandbox_boundary.SECRET_NAMES)
    | {
        ".env",
        ".secrets",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".dockerconfigjson",
        "credentials",
        "application_default_credentials.json",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    }
)
_EP_SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")

_EP_CANONICAL_SECRET_PROBES: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".secrets",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".dockerconfigjson",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "server.pem",
    "private.key",
    "bundle.p12",
    "bundle.pfx",
)
_EP_BENIGN_FILENAME_PROBES: tuple[str, ...] = (
    "app.py",
    "README.md",
    "test_env.py",
    "test_secrets.py",
    "test_key.py",
    "notes.txt",
    "package.json",
)

_EP_ALLOWED_DEV_PATHS: frozenset[str] = frozenset(
    {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/stdin",
        "/dev/zero",
    }
)
_EP_SENSITIVE_ABS_PREFIXES: tuple[str, ...] = (
    "/etc/",
    "/private/etc/",
    "/proc/",
    "/sys/",
    "/root/",
    "/var/",
    "/private/var/",
    "/tmp/",
    "/private/tmp/",
    "/Users/",
    "/home/",
    "/run/",
    "/opt/",
    "/usr/",
)
_EP_WRAPPER_BINARIES: frozenset[str] = frozenset({"env", "nice", "nohup", "time", "command", "exec"})
# Shells whose `-c "<command>"` payload is a *second* command line: it is parsed
# and inspected exactly like the outer one, so `sh -c 'cat .env'` cannot reach a
# secret that `cat .env` is refused.
_EP_SHELL_INTERPRETERS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish", "busybox"}
)
# Interpreters that execute inline program text which no inspector here can
# parse. Their payload is not executed by this policy; instead every path-like
# substring of it is checked against the same confinement and secret rules.
_EP_INLINE_CODE_INTERPRETERS: frozenset[str] = frozenset({"perl", "ruby", "node", "nodejs", "php", "lua"})
_EP_MAX_NESTING = 3
_EP_INLINE_PATH_RE = re.compile(
    r"(?:~|\$HOME|\$\{HOME\})(?:/[\w.\-]+)+"  # ~/... and $HOME/... escapes
    r"|/[\w.\-]+(?:/[\w.\-]+)*"  # absolute paths
    r"|(?:[\.]{1,2}/|[\w.\-]+/)+[\w.\-]*"  # relative paths with a separator
)
_EP_FORBIDDEN_PRIV_BINARIES: frozenset[str] = frozenset(
    {"sudo", "su", "doas", "chroot", "nsenter", "unshare", "docker", "podman", " nerdctl".strip()}
)
_EP_DESTRUCTIVE_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rm -rf /", re.compile(r"(^|\s|;|&|\|)rm\s+(-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)\s+/(?:\s|$|;|&|\|)")),
    ("rm -rf ~", re.compile(r"(^|\s|;|&|\|)rm\s+(-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)\s+~")),
    ("rm -rf .git", re.compile(r"(^|\s|;|&|\|)rm\s+-[A-Za-z]*r[A-Za-z]*\s+(?:\./)?\.git(?:/|\s|$|;|\"|')")),
    ("mkfs", re.compile(r"(^|\s|;|&|\|)mkfs\.")),
    ("dd raw", re.compile(r"(^|\s|;|&|\|)dd\s+if=")),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;:")),
    ("chmod 777 /", re.compile(r"chmod\s+-R\s+777\s+/(?:\s|$)")),
)
_EP_DANGEROUS_GIT_CONFIG_RE = re.compile(
    r"\b(?:fsmonitor|sshcommand|hookspath|core\.gitproxy|credential\.helper|filter\.[A-Za-z0-9_.-]+\.(?:clean|smudge))\b",
    re.IGNORECASE,
)
_EP_PY_FS_CALL_NAMES: frozenset[str] = frozenset(
    {
        "open",
        "Path",
        "PurePath",
        "PosixPath",
        "PurePosixPath",
        "os.open",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "os.symlink",
        "os.link",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.chdir",
        "os.stat",
        "os.lstat",
        "os.chmod",
        "os.makedirs",
        "os.mkdir",
        "os.rmdir",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "glob.glob",
        "glob.iglob",
    }
)
_EP_PY_SHELL_CALL_NAMES: frozenset[str] = frozenset(
    {
        "os.system",
        "os.popen",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_EP_INTERPRETER_SKIP_DIRS: frozenset[str] = frozenset({".venv", "venv", "node_modules", ".git"})

_EP_LEDGER_LOCK = threading.Lock()


def _ep_now() -> float:
    return round(time.time(), 6)


def _ep_restrict_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ep_ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _ep_safe_session_filename(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if _EP_SAFE_SESSION_RE.match(sid):
        return sid
    digest = hashlib.sha256(sid.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"session-{digest}"


# ---------------------------------------------------------------------------
# 1. Protected secret & VCS path predicates
# ---------------------------------------------------------------------------


def is_protected_secret_component(name: str) -> bool:
    """Return True when a single path component names a secret file or directory."""
    low = (name or "").strip().lower()
    if not low or low in _EP_EXEMPT_SECRET_NAMES:
        return False
    if low in _EP_SECRET_DIR_NAMES or low in _EP_SECRET_EXACT_NAMES:
        return True
    if low.startswith(".env."):
        return True
    if low.startswith("credentials."):
        return True
    if low.endswith(_EP_SECRET_SUFFIXES):
        return True
    return False


def is_protected_secret_rel_path(raw_path: str) -> bool:
    """Return True if any path segment targets a protected secret file or directory."""
    norm = (raw_path or "").strip().replace("\\", "/")
    if not norm:
        return False
    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if not parts:
        return False
    for part in parts[:-1]:
        if part.lower() in _EP_SECRET_DIR_NAMES or is_protected_secret_component(part):
            return True
    last = parts[-1]
    if last.lower() in _EP_EXEMPT_SECRET_NAMES:
        return False
    return is_protected_secret_component(last)


def is_protected_vcs_rel_path(raw_path: str) -> bool:
    """Return True if ``raw_path`` directly targets protected Git hooks/config control paths."""
    norm = (raw_path or "").strip().replace("\\", "/")
    if not norm:
        return False
    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if not parts:
        return False
    # Check any `.git` segment inside the path (covering both root `.git/...` and nested `.git/...`).
    for idx, part in enumerate(parts):
        if part.lower() != ".git":
            continue
        tail = [seg.lower() for seg in parts[idx + 1 :]]
        if not tail:
            return True
        head = tail[0]
        if head in {"config", "config.worktree"}:
            return True
        if head == "hooks":
            if len(tail) == 1:
                return True
            hook_name = tail[-1]
            if not hook_name.endswith(".sample"):
                return True
        if head == "info" and len(tail) >= 2 and tail[1] == "attributes":
            return True
    return False


def _ep_is_targeted_secret_glob(token: str) -> str | None:
    """Detect shell/glob patterns that target secret or VCS-hook names without matching normal code files."""
    norm = (token or "").strip().replace("\\", "/")
    if not norm or not _EP_GLOB_CHARS_RE.search(norm):
        return None
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        return None
    # Check `.git/hook*` or `.git/conf*` globbing.
    for idx, part in enumerate(parts):
        if fnmatch.fnmatchcase(".git", part.lower()) and not fnmatch.fnmatchcase("src", part.lower()):
            if idx + 1 < len(parts):
                nxt = parts[idx + 1].lower()
                for vcs_probe in ("hooks", "config", "config.worktree"):
                    if fnmatch.fnmatchcase(vcs_probe, nxt) and not fnmatch.fnmatchcase("objects", nxt):
                        return f".git/{vcs_probe}"
            elif part != ".git":
                return ".git"
    leaf = parts[-1].lower()
    if any(fnmatch.fnmatchcase(benign.lower(), leaf) for benign in _EP_BENIGN_FILENAME_PROBES):
        return None
    for secret_probe in _EP_CANONICAL_SECRET_PROBES:
        if fnmatch.fnmatchcase(secret_probe.lower(), leaf):
            return secret_probe
    return None


# ---------------------------------------------------------------------------
# 2. Verified capability descriptors & resource footprints
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Server-verified capability profile for one tool or operation (Task 1)."""

    tool_name: str
    action_class: execution_spine.ActionClass
    execution_surface: ExecutionSurface
    isolation_posture: IsolationPosture
    verified_isolation: bool
    fs_capability: FilesystemCapability
    network_capability: NetworkCapability
    process_capability: ProcessCapability
    secret_access: Literal["forbidden"] = "forbidden"
    vcs_hook_access: Literal["forbidden"] = "forbidden"
    side_effect_certainty: SideEffectCertainty = "unknown_conservative"
    registered: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": EP_SCHEMA_VERSION,
            "tool_name": self.tool_name,
            "action_class": self.action_class,
            "execution_surface": self.execution_surface,
            "isolation_posture": self.isolation_posture,
            "verified_isolation": self.verified_isolation,
            "fs_capability": self.fs_capability,
            "network_capability": self.network_capability,
            "process_capability": self.process_capability,
            "secret_access": self.secret_access,
            "vcs_hook_access": self.vcs_hook_access,
            "side_effect_certainty": self.side_effect_certainty,
            "registered": self.registered,
        }


@dataclass(frozen=True, slots=True)
class ResourceBarrier:
    """Shared or exclusive resource barrier acquired atomically with admission (Task 3)."""

    target: str
    mode: BarrierMode = "exclusive"

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target.strip():
            raise resource_admission.AdmissionError("barrier.target must be a non-empty string.")
        if self.mode not in ("shared", "exclusive"):
            raise resource_admission.AdmissionError(
                f"barrier.mode must be 'shared' or 'exclusive', got {self.mode!r}."
            )


@dataclass(frozen=True, slots=True)
class BarrierAdmissionDecision:
    """Outcome of atomic capacity + resource-barrier reservation."""

    allowed: bool
    lease_id: str | None = None
    conflicting_barriers: tuple[str, ...] = ()
    exhausted_resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceFootprint:
    """Declared resource counters and read/write barriers for admission (Task 2 & 3)."""

    scope: FootprintScope
    memory_bytes: int
    action_slots: int
    subprocess_slots: int
    mutation_slots: int
    barriers: tuple[ResourceBarrier, ...]
    declared_read_paths: tuple[str, ...] = ()
    declared_write_paths: tuple[str, ...] = ()
    conservative: bool = False
    workspace_key: str = ""

    def to_resource_request(self) -> resource_admission.ResourceRequest:
        return resource_admission.ResourceRequest(
            memory_bytes=self.memory_bytes,
            slots=self.action_slots,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "conservative": self.conservative,
            "workspace_key": self.workspace_key,
            "memory_bytes": self.memory_bytes,
            "resources": {
                "action_slots": self.action_slots,
                "subprocess_slots": self.subprocess_slots,
                "mutation_slots": self.mutation_slots,
            },
            "barriers": [{"target": b.target, "mode": b.mode} for b in self.barriers],
            "declared_read_paths": list(self.declared_read_paths),
            "declared_write_paths": list(self.declared_write_paths),
        }


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Paired verified capability descriptor and resource footprint."""

    descriptor: CapabilityDescriptor
    footprint: ResourceFootprint

    def to_mapping(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor.to_mapping(),
            "footprint": self.footprint.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class PolicyViolation:
    """Structured unified-policy refusal shared by managed and subprocess surfaces."""

    error_code: str
    rule: str
    message: str
    target: str = ""


_EP_EXACT_READ_TOOLS: frozenset[str] = frozenset({"read_file"})
_EP_BOUNDED_WS_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_files",
        "grep_search",
        "codegraph_architecture",
        "codegraph_context",
        "codegraph_impact",
        "codegraph_path",
        "codegraph_quality",
        "mcp_bridge__codegraph_query",
        "mcp_bridge__get_harness_diff",
    }
)
_EP_VAULT_READ_TOOLS: frozenset[str] = frozenset(
    {"oc_read", "oc_search", "oc_lint", "mcp_bridge__oc_search", "mcp_bridge__oc_lint"}
)
_EP_VAULT_WRITE_TOOLS: frozenset[str] = frozenset({"oc_save_doc", "mcp_bridge__oc_save_doc"})
_EP_SESSION_READ_TOOLS: frozenset[str] = frozenset(
    {
        "ctx_search",
        "list_edit_batches",
        "reconcile_task_actions",
        "mcp_bridge__get_current_session_data",
    }
)
_EP_EXACT_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})
_EP_SUBPROCESS_TOOLS: frozenset[str] = frozenset(
    {"run_bash", "ctx_execute_python", "skill_execute_script"}
)
_EP_COUNCIL_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "mcp_bridge__start_council",
        "mcp_bridge__send_response",
        "mcp_bridge__summon_omlx_agent",
        "mcp_bridge__close_council",
    }
)
_EP_KNOWN_TOOLS: frozenset[str] = (
    _EP_EXACT_READ_TOOLS
    | _EP_BOUNDED_WS_READ_TOOLS
    | _EP_VAULT_READ_TOOLS
    | _EP_VAULT_WRITE_TOOLS
    | _EP_SESSION_READ_TOOLS
    | _EP_EXACT_WRITE_TOOLS
    | _EP_SUBPROCESS_TOOLS
    | _EP_COUNCIL_MUTATION_TOOLS
    | frozenset({"ctx_run_block", "rollback_edits", "delegate_subagent", "mcp_bridge__create_harness_worktree"})
)


def _ep_subprocess_posture(
    execution_mode: str,
    *,
    container_qualified: bool,
) -> tuple[IsolationPosture, bool, NetworkCapability]:
    mode = (execution_mode or "disabled").strip().lower()
    if mode == "sandbox":
        if container_qualified:
            return ("container_qualified", True, "none")
        return ("container_hardened_unverified", False, "none")
    if mode == "host":
        return ("host_opt_in_unsandboxed", False, "host_unrestricted")
    return ("disabled", False, "none")


def resolve_capability_descriptor(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    execution_mode: str = "disabled",
    container_qualified: bool = False,
) -> CapabilityDescriptor:
    """Derive a verified server-side capability descriptor; untrusted ``args`` hints are ignored."""
    clean = str(tool_name or "").strip()
    safe_args = args if isinstance(args, Mapping) else {}
    action_class = execution_spine.classify_action(clean, safe_args)

    if clean == "ctx_run_block":
        block = execution_spine.normalize_reusable_block_name(safe_args.get("block"))
        if block in {"json-peek", "csv-peek"}:
            return CapabilityDescriptor(
                tool_name=clean,
                action_class="read_only",
                execution_surface="managed",
                isolation_posture="in_process_confined",
                verified_isolation=True,
                fs_capability="workspace_read",
                network_capability="none",
                process_capability="none",
                side_effect_certainty="exact",
                registered=True,
            )
        if block == "shortcut-debt":
            return CapabilityDescriptor(
                tool_name=clean,
                action_class="read_only",
                execution_surface="managed",
                isolation_posture="in_process_confined",
                verified_isolation=True,
                fs_capability="workspace_read",
                network_capability="none",
                process_capability="none",
                side_effect_certainty="bounded_read",
                registered=True,
            )
        posture, verified, net_cap = _ep_subprocess_posture(
            execution_mode, container_qualified=container_qualified
        )
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="consequential",
            execution_surface="subprocess",
            isolation_posture=posture,
            verified_isolation=verified,
            fs_capability="workspace_broad_write",
            network_capability=net_cap,
            process_capability="bounded_subprocess",
            side_effect_certainty="unknown_conservative",
            registered=(block in execution_spine.CONSEQUENTIAL_BLOCKS),
        )

    if clean in _EP_EXACT_READ_TOOLS:
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="read_only",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_read",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="exact",
        )

    if clean in _EP_BOUNDED_WS_READ_TOOLS:
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="read_only",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_read",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="bounded_read",
        )

    if clean in _EP_VAULT_READ_TOOLS or clean in _EP_SESSION_READ_TOOLS:
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="read_only",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="none",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="bounded_read",
        )

    if clean in _EP_EXACT_WRITE_TOOLS:
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="managed_edit",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_targeted_write",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="exact",
        )

    if clean == "rollback_edits":
        dry_run = bool(safe_args.get("dry_run", False))
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="managed_edit",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_read" if dry_run else "workspace_broad_write",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="bounded_read" if dry_run else "unknown_conservative",
        )

    if clean in _EP_VAULT_WRITE_TOOLS:
        return CapabilityDescriptor(
            tool_name=clean,
            action_class=action_class,
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="none",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="exact",
        )

    if clean == "delegate_subagent":
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="consequential",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_read",
            network_capability="configured_provider",
            process_capability="none",
            side_effect_certainty="bounded_read",
        )

    if clean in _EP_COUNCIL_MUTATION_TOOLS:
        net: NetworkCapability = (
            "configured_provider" if clean == "mcp_bridge__summon_omlx_agent" else "none"
        )
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="consequential",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="none",
            network_capability=net,
            process_capability="none",
            side_effect_certainty="unknown_conservative",
        )

    if clean == "mcp_bridge__create_harness_worktree":
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="consequential",
            execution_surface="managed",
            isolation_posture="in_process_confined",
            verified_isolation=True,
            fs_capability="workspace_broad_write",
            network_capability="none",
            process_capability="none",
            side_effect_certainty="unknown_conservative",
        )

    if clean in _EP_SUBPROCESS_TOOLS:
        posture, verified, net_cap = _ep_subprocess_posture(
            execution_mode, container_qualified=container_qualified
        )
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="consequential",
            execution_surface="subprocess",
            isolation_posture=posture,
            verified_isolation=verified,
            fs_capability="workspace_broad_write",
            network_capability=net_cap,
            process_capability="bounded_subprocess",
            side_effect_certainty="unknown_conservative",
        )

    if clean.startswith("mcp__"):
        parts = clean.split("__", 2)
        valid_mcp = len(parts) == 3 and bool(parts[1].strip()) and bool(parts[2].strip())
        return CapabilityDescriptor(
            tool_name=clean,
            action_class="external",
            execution_surface="external",
            isolation_posture="external_mcp",
            verified_isolation=False,
            fs_capability="workspace_broad_write",
            network_capability="external_mcp",
            process_capability="none",
            side_effect_certainty="unknown_conservative",
            registered=valid_mcp,
        )

    # Unregistered / unknown operation: fail closed with conservative capabilities.
    return CapabilityDescriptor(
        tool_name=clean or "<empty>",
        action_class=action_class,
        execution_surface="external",
        isolation_posture="disabled",
        verified_isolation=False,
        fs_capability="workspace_broad_write",
        network_capability="external_mcp",
        process_capability="bounded_subprocess",
        side_effect_certainty="unknown_conservative",
        registered=False,
    )


def normalize_workspace_rel_path(
    workspace_root: Path | None,
    raw_path: str,
) -> tuple[str | None, PolicyViolation | None]:
    """Normalize ``raw_path`` into a canonical workspace-relative POSIX path or return a violation."""
    text = str(raw_path or "").strip()
    if not text:
        return None, None
    if "\x00" in text:
        return None, PolicyViolation(
            error_code="unauthorized_path",
            rule="nul_byte",
            message="Refused: target path contains NUL characters.",
        )

    resolved_ws: Path | None = None
    if workspace_root is not None:
        try:
            resolved_ws = workspace_root.expanduser().resolve()
        except OSError:
            resolved_ws = None

    norm_slashes = text.replace("\\", "/")
    if norm_slashes.startswith("~") or "$HOME" in norm_slashes or "${HOME}" in norm_slashes:
        return None, PolicyViolation(
            error_code="unauthorized_path",
            rule="workspace_escape",
            message="Refused: home-directory expansion escapes the workspace boundary.",
        )

    candidate = Path(norm_slashes)
    rel_parts: list[str] = []
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:/", norm_slashes):
        if resolved_ws is None:
            if is_protected_secret_rel_path(norm_slashes):
                base = norm_slashes.rstrip("/").rsplit("/", 1)[-1]
                return None, PolicyViolation(
                    error_code="unauthorized_path",
                    rule="secret_path",
                    message=(
                        f"Refused: access to protected secret path `{base}` is forbidden by unified workspace policy "
                        "(templates like `.env.example` remain allowed)."
                    ),
                    target=base,
                )
            if is_protected_vcs_rel_path(norm_slashes):
                return None, PolicyViolation(
                    error_code="unauthorized_path",
                    rule="vcs_protected_path",
                    message=f"Refused: access to protected Git control path `{norm_slashes}` is forbidden by unified policy.",
                    target=norm_slashes,
                )
            if norm_slashes.startswith(("/etc/", "/private/etc/", "/proc/", "/sys/", "/root/")):
                return None, PolicyViolation(
                    error_code="unauthorized_path",
                    rule="workspace_escape",
                    message="Refused: absolute system path escapes the workspace boundary.",
                )
            return None, None
        try:
            # Inspect lexical relative segments without following a final symlink first.
            abs_parent = candidate.parent.resolve()
            abs_candidate = abs_parent / candidate.name
            rel_from_ws = abs_candidate.relative_to(resolved_ws)
        except (OSError, ValueError):
            return None, PolicyViolation(
                error_code="unauthorized_path",
                rule="workspace_escape",
                message="Refused: target path escapes the workspace boundary.",
            )
        rel_parts = [p for p in rel_from_ws.parts if p not in ("", ".")]
    else:
        for seg in norm_slashes.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not rel_parts:
                    return None, PolicyViolation(
                        error_code="unauthorized_path",
                        rule="workspace_escape",
                        message="Refused: parent traversal (`..`) escapes the workspace boundary.",
                    )
                rel_parts.pop()
            else:
                rel_parts.append(seg)

    rel_posix = "/".join(rel_parts) if rel_parts else "."

    if resolved_ws is not None and rel_parts:
        cur = resolved_ws
        for seg in rel_parts:
            cur = cur / seg
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                break
            except OSError:
                return None, PolicyViolation(
                    error_code="unsafe_path",
                    rule="unstatable_path",
                    message="Refused: cannot inspect workspace path metadata safely.",
                )
            if stat.S_ISLNK(st.st_mode):
                return None, PolicyViolation(
                    error_code="unsafe_path",
                    rule="symlink_target",
                    message="Refused: target path traverses a symlink; managed and sandboxed policy forbids symlink targets.",
                    target=rel_posix,
                )

    if rel_posix != ".":
        if is_protected_secret_rel_path(rel_posix):
            base = rel_parts[-1] if rel_parts else rel_posix
            return None, PolicyViolation(
                error_code="unauthorized_path",
                rule="secret_path",
                message=(
                    f"Refused: access to protected secret path `{base}` is forbidden by unified workspace policy "
                    "(templates like `.env.example` remain allowed)."
                ),
                target=base,
            )
        if is_protected_vcs_rel_path(rel_posix):
            return None, PolicyViolation(
                error_code="unauthorized_path",
                rule="vcs_protected_path",
                message=f"Refused: access to protected Git control path `{rel_posix}` is forbidden by unified policy.",
                target=rel_posix,
            )

    return rel_posix, None


def derive_execution_profile(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    workspace_root: Path | None = None,
    session_id: str = "default",
    execution_mode: str = "disabled",
    container_qualified: bool = False,
) -> ExecutionProfile:
    """Derive the verified capability descriptor and resource/barrier footprint for ``tool_name``."""
    safe_args = args if isinstance(args, Mapping) else {}
    descriptor = resolve_capability_descriptor(
        tool_name,
        safe_args,
        execution_mode=execution_mode,
        container_qualified=container_qualified,
    )
    ws_key = "unbound"
    if workspace_root is not None:
        try:
            ws_key = str(workspace_root.expanduser().resolve())
        except OSError:
            ws_key = str(workspace_root)

    clean = descriptor.tool_name

    # 1. Exact single-file reads (`read_file`, `ctx_run_block` `json-peek`/`csv-peek`).
    if clean == "read_file" or (
        clean == "ctx_run_block"
        and execution_spine.normalize_reusable_block_name(safe_args.get("block")) in {"json-peek", "csv-peek"}
    ):
        raw_target = str(safe_args.get("path") if clean == "read_file" else safe_args.get("target") or "")
        norm_rel, _viol = normalize_workspace_rel_path(workspace_root, raw_target)
        read_paths = (norm_rel,) if norm_rel and norm_rel != "." else ()
        barriers: list[ResourceBarrier] = [
            ResourceBarrier(f"workspace:{ws_key}", "shared")
        ]
        for rp in read_paths:
            barriers.append(ResourceBarrier(f"path:{ws_key}:{rp}", "shared"))
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="exact_paths",
                memory_bytes=DEFAULT_READ_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=0,
                barriers=tuple(barriers),
                declared_read_paths=read_paths,
                declared_write_paths=(),
                conservative=False,
                workspace_key=ws_key,
            ),
        )

    # 2. Exact single-file managed writes (`write_file`, `edit_file`).
    if clean in _EP_EXACT_WRITE_TOOLS:
        raw_target = str(safe_args.get("path") or "")
        norm_rel, _viol = normalize_workspace_rel_path(workspace_root, raw_target)
        write_paths = (norm_rel,) if norm_rel and norm_rel != "." else ()
        if not write_paths:
            # Malformed/empty target falls back to conservative exclusive workspace barrier.
            return ExecutionProfile(
                descriptor=descriptor,
                footprint=ResourceFootprint(
                    scope="conservative_workspace",
                    memory_bytes=DEFAULT_MANAGED_WRITE_FOOTPRINT_BYTES,
                    action_slots=1,
                    subprocess_slots=0,
                    mutation_slots=1,
                    barriers=(ResourceBarrier(f"workspace:{ws_key}", "exclusive"),),
                    conservative=True,
                    workspace_key=ws_key,
                ),
            )
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="exact_paths",
                memory_bytes=DEFAULT_MANAGED_WRITE_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=1,
                barriers=(
                    ResourceBarrier(f"workspace:{ws_key}", "shared"),
                    ResourceBarrier(f"path:{ws_key}:{write_paths[0]}", "exclusive"),
                ),
                declared_read_paths=(),
                declared_write_paths=write_paths,
                conservative=False,
                workspace_key=ws_key,
            ),
        )

    # 3. Bounded workspace reads (`list_files`, `grep_search`, `codegraph_*`, `shortcut-debt`, `delegate_subagent`, `rollback_edits(dry_run=True)`).
    if (
        clean in _EP_BOUNDED_WS_READ_TOOLS
        or clean == "delegate_subagent"
        or (clean == "ctx_run_block" and execution_spine.normalize_reusable_block_name(safe_args.get("block")) == "shortcut-debt")
        or (clean == "rollback_edits" and bool(safe_args.get("dry_run", False)))
    ):
        read_barriers: list[ResourceBarrier] = [
            ResourceBarrier(f"workspace:{ws_key}", "shared")
        ]
        declared_reads: list[str] = []
        if clean == "delegate_subagent":
            ctx_files = safe_args.get("context_files")
            if isinstance(ctx_files, Sequence) and not isinstance(ctx_files, (str, bytes)):
                for cf in list(ctx_files)[:6]:
                    nr, nv = normalize_workspace_rel_path(workspace_root, str(cf))
                    if nv is None and nr and nr != "." and nr not in declared_reads:
                        declared_reads.append(nr)
                        read_barriers.append(ResourceBarrier(f"path:{ws_key}:{nr}", "shared"))
        elif clean in {"list_files", "grep_search"}:
            nr, nv = normalize_workspace_rel_path(workspace_root, str(safe_args.get("path") or "."))
            if nv is None and nr and nr != ".":
                declared_reads.append(nr)
                read_barriers.append(ResourceBarrier(f"path:{ws_key}:{nr}", "shared"))
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="bounded_workspace_read",
                memory_bytes=DEFAULT_READ_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=0,
                barriers=tuple(read_barriers),
                declared_read_paths=tuple(declared_reads),
                conservative=False,
                workspace_key=ws_key,
            ),
        )

    # 4. Vault operations (`oc_read`/`oc_search`/`oc_lint` vs `oc_save_doc`).
    if clean in _EP_VAULT_READ_TOOLS:
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="vault",
                memory_bytes=DEFAULT_READ_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=0,
                barriers=(ResourceBarrier("opencontext:vault", "shared"),),
                conservative=False,
            ),
        )
    if clean in _EP_VAULT_WRITE_TOOLS:
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="vault",
                memory_bytes=DEFAULT_MANAGED_WRITE_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=1,
                barriers=(ResourceBarrier("opencontext:vault", "exclusive"),),
                conservative=False,
            ),
        )

    # 5. Session-local read-only queries (`ctx_search`, `list_edit_batches`, `reconcile_task_actions`, council read).
    if clean in _EP_SESSION_READ_TOOLS:
        sid = str(session_id or "default")
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="session_state",
                memory_bytes=DEFAULT_READ_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=0,
                barriers=(ResourceBarrier(f"session:{sid}", "shared"),),
                conservative=False,
            ),
        )

    # 6. Council session mutators.
    if clean in _EP_COUNCIL_MUTATION_TOOLS:
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="session_state",
                memory_bytes=DEFAULT_CONSERVATIVE_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=1,
                barriers=(ResourceBarrier("council:session", "exclusive"),),
                conservative=True,
            ),
        )

    # 7. External MCP tools: reserve both remote-server barrier and conservative workspace barrier.
    if clean.startswith("mcp__"):
        parts = clean.split("__", 2)
        alias = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else "unknown"
        ext_barriers: list[ResourceBarrier] = [
            ResourceBarrier(f"mcp:{alias}", "exclusive"),
            ResourceBarrier(f"workspace:{ws_key}", "exclusive"),
        ]
        return ExecutionProfile(
            descriptor=descriptor,
            footprint=ResourceFootprint(
                scope="external_and_workspace",
                memory_bytes=DEFAULT_CONSERVATIVE_FOOTPRINT_BYTES,
                action_slots=1,
                subprocess_slots=0,
                mutation_slots=1,
                barriers=tuple(ext_barriers),
                conservative=True,
                workspace_key=ws_key,
            ),
        )

    # 8. Arbitrary shell commands, Python snippets, skill scripts, multi-file rollbacks,
    # worktree creation, and any unknown tool: reserve conservative exclusive workspace scope.
    is_subproc = descriptor.process_capability != "none"
    return ExecutionProfile(
        descriptor=descriptor,
        footprint=ResourceFootprint(
            scope="conservative_workspace",
            memory_bytes=(
                DEFAULT_SUBPROCESS_FOOTPRINT_BYTES if is_subproc else DEFAULT_CONSERVATIVE_FOOTPRINT_BYTES
            ),
            action_slots=1,
            subprocess_slots=1 if is_subproc else 0,
            mutation_slots=1,
            barriers=(ResourceBarrier(f"workspace:{ws_key}", "exclusive"),),
            declared_read_paths=(),
            declared_write_paths=(),
            conservative=True,
            workspace_key=ws_key,
        ),
    )


# ---------------------------------------------------------------------------
# 3. Unified policy evaluation (closing shell / Python / script bypasses)
# ---------------------------------------------------------------------------


def _ep_decode_ansi_c_escapes(text: str) -> str:
    """Decode ``$'\\x2e\\x65\\x6e\\x76'`` hex and octal ANSI-C quotes so escaped names cannot hide."""

    def _replace_quote(match: re.Match[str]) -> str:
        inner = match.group(1)
        inner = _EP_HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), inner)
        inner = _EP_OCT_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8) & 0xFF), inner)
        return inner

    return _EP_ANSI_C_QUOTE_RE.sub(_replace_quote, text)


def _ep_expand_simple_braces(token: str) -> list[str]:
    """Expand single-level ``{.env,README.md}`` shell braces into concrete candidate tokens."""
    m = _EP_BRACE_SIMPLE_RE.match(token)
    if not m or "," not in m.group(2):
        return [token]
    prefix, body, suffix = m.group(1), m.group(2), m.group(3)
    items = [part for part in body.split(",") if part]
    if not items or len(items) > 16:
        return [token]
    return [f"{prefix}{item}{suffix}" for item in items]


def _ep_is_allowed_container_path(norm_path: str) -> tuple[bool, str]:
    """Check ``/workspace/...`` container mount paths for traversal and return relative tail."""
    if norm_path == "/workspace":
        return True, "."
    if not norm_path.startswith("/workspace/"):
        return False, ""
    rel_parts: list[str] = []
    for seg in norm_path[len("/workspace/") :].split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not rel_parts:
                return False, ""
            rel_parts.pop()
        else:
            rel_parts.append(seg)
    return True, "/".join(rel_parts) if rel_parts else "."


def _ep_check_candidate_path_token(
    raw_token: str,
    *,
    workspace_root: Path | None,
    is_executable_position: bool = False,
    strict_abs_check: bool = True,
) -> PolicyViolation | None:
    """Apply unified secret, VCS-control, symlink, and workspace-confinement checks to one path token."""
    tok = (raw_token or "").strip()
    if not tok:
        return None
    if "\x00" in tok:
        return PolicyViolation("unauthorized_path", "nul_byte", "Refused: NUL byte in command or path.")

    norm = tok.replace("\\", "/")

    # Skip URLs (`http://`, `https://`, `git://`, `ssh://`) — network confinement is governed separately.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", norm):
        return None

    # 1. Home-directory expansion (`~`, `~/...`, `~user/...`, `$HOME`, `${HOME}`).
    if norm == "~" or norm.startswith("~/") or re.match(r"^~[A-Za-z_][A-Za-z0-9_-]*(?:/|$)", norm):
        return PolicyViolation(
            "unauthorized_path",
            "workspace_escape",
            "Refused: home-directory path (`~`) escapes the workspace boundary.",
            target=tok,
        )
    if "$HOME" in norm or "${HOME}" in norm:
        return PolicyViolation(
            "unauthorized_path",
            "workspace_escape",
            "Refused: `$HOME` expansion escapes the workspace boundary.",
            target="$HOME",
        )

    # 2. Targeted secret or `.git` globs (e.g. `.en*`, `.e?v`, `.[e]nv`, `*.pem`, `.git/hook*`).
    glob_hit = _ep_is_targeted_secret_glob(norm)
    if glob_hit is not None:
        return PolicyViolation(
            "unauthorized_path",
            "secret_path",
            f"Refused: glob pattern targeting protected path `{glob_hit}` is forbidden by unified policy.",
            target=glob_hit,
        )

    # 3. Protected secret or VCS path anywhere in the token.
    if is_protected_secret_rel_path(norm):
        base = norm.rstrip("/").rsplit("/", 1)[-1]
        return PolicyViolation(
            "unauthorized_path",
            "secret_path",
            f"Refused: access to protected secret path `{base}` is forbidden by unified policy.",
            target=base,
        )
    if is_protected_vcs_rel_path(norm):
        return PolicyViolation(
            "unauthorized_path",
            "vcs_protected_path",
            f"Refused: access to protected Git control path `{norm}` is forbidden by unified policy.",
            target=norm,
        )

    # 4. Harmless standard pseudo-devices (`/dev/null`, `/dev/fd/*`, etc.).
    if norm in _EP_ALLOWED_DEV_PATHS or norm.startswith("/dev/fd/"):
        return None

    # 5. Container mount `/workspace` paths.
    if norm == "/workspace" or norm.startswith("/workspace/"):
        ok_ws, ws_rel = _ep_is_allowed_container_path(norm)
        if not ok_ws:
            return PolicyViolation(
                "unauthorized_path",
                "workspace_escape",
                "Refused: container `/workspace` path escapes the workspace mount.",
            )
        if ws_rel != ".":
            if is_protected_secret_rel_path(ws_rel):
                base = ws_rel.rsplit("/", 1)[-1]
                return PolicyViolation(
                    "unauthorized_path",
                    "secret_path",
                    f"Refused: access to protected secret path `{base}` is forbidden by unified policy.",
                    target=base,
                )
            if is_protected_vcs_rel_path(ws_rel):
                return PolicyViolation(
                    "unauthorized_path",
                    "vcs_protected_path",
                    f"Refused: access to protected Git control path `{ws_rel}` is forbidden by unified policy.",
                    target=ws_rel,
                )
        return None

    resolved_ws: Path | None = None
    if workspace_root is not None:
        try:
            resolved_ws = workspace_root.expanduser().resolve()
        except OSError:
            resolved_ws = None

    # 6. Absolute paths.
    if norm.startswith("/") or re.match(r"^[A-Za-z]:/", norm):
        if is_executable_position:
            # Allow standard interpreter/tool binaries in argv[0] (e.g. `sys.executable -m pytest`),
            # provided they are not secret or VCS-hook paths (already checked above).
            return None
        if resolved_ws is not None:
            try:
                cand = Path(norm)
                abs_parent = cand.parent.resolve()
                abs_cand = abs_parent / cand.name
                rel_from_ws = abs_cand.relative_to(resolved_ws)
                rel_parts = [p for p in rel_from_ws.parts if p not in ("", ".")]
                norm = "/".join(rel_parts) if rel_parts else "."
            except (OSError, ValueError):
                if strict_abs_check:
                    return PolicyViolation(
                        "unauthorized_path",
                        "workspace_escape",
                        "Refused: absolute path escapes the workspace boundary.",
                    )
                if not strict_abs_check and norm == "/":
                    return None
                # For general Python string literals (not explicit FS calls), block if host path exists
                # or starts with a sensitive/host filesystem prefix.
                cand_obj = Path(norm)
                host_like = norm.startswith(_EP_SENSITIVE_ABS_PREFIXES)
                if not host_like:
                    try:
                        host_like = cand_obj.exists() or (
                            str(cand_obj.parent) not in ("", "/") and cand_obj.parent.exists()
                        )
                    except OSError:
                        host_like = True
                if host_like:
                    return PolicyViolation(
                        "unauthorized_path",
                        "workspace_escape",
                        "Refused: absolute host path escapes the workspace boundary.",
                    )
                return None
        else:
            if norm.startswith(_EP_SENSITIVE_ABS_PREFIXES):
                return PolicyViolation(
                    "unauthorized_path",
                    "workspace_escape",
                    "Refused: absolute system path escapes the workspace boundary.",
                )
            return None

    # 7. Relative traversal (`..`, `../outside`, `a/../../outside`) & workspace symlink checks.
    if norm == ".." or norm.startswith("../") or "/../" in norm or norm.endswith("/.."):
        rel_stack: list[str] = []
        for seg in norm.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not rel_stack:
                    return PolicyViolation(
                        "unauthorized_path",
                        "workspace_escape",
                        "Refused: parent traversal (`..`) escapes the workspace boundary.",
                    )
                rel_stack.pop()
            else:
                rel_stack.append(seg)
        norm = "/".join(rel_stack) if rel_stack else "."
        if norm != "." and (is_protected_secret_rel_path(norm) or is_protected_vcs_rel_path(norm)):
            return PolicyViolation(
                "unauthorized_path",
                "secret_path",
                "Refused: normalized path resolves to a protected workspace file.",
            )

    if resolved_ws is not None and norm not in ("", ".") and not norm.startswith("-"):
        segs = [s for s in norm.split("/") if s not in ("", ".")]
        if segs and not (is_executable_position and segs[0] in _EP_INTERPRETER_SKIP_DIRS):
            cur = resolved_ws
            for seg in segs:
                if _EP_GLOB_CHARS_RE.search(seg):
                    break
                cur = cur / seg
                try:
                    st = os.lstat(cur)
                except OSError:
                    break
                if stat.S_ISLNK(st.st_mode):
                    return PolicyViolation(
                        "unsafe_path",
                        "symlink_target",
                        "Refused: command or tool path traverses a workspace symlink.",
                        target="/".join(segs),
                    )
    return None


def _ep_eval_const_str(node: ast.AST) -> str | None:
    """Fold simple constant Python string expressions (concatenation, ``Path / 'x'``, ``os.path.join``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
            else:
                parts.append("*")
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        left = _ep_eval_const_str(node.left)
        right = _ep_eval_const_str(node.right)
        if left is not None and right is not None:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Div):
                return f"{left.rstrip('/')}/{right.lstrip('/')}"
    if isinstance(node, ast.Call):
        cname = _ep_call_name(node.func)
        if cname in {"Path", "PurePath", "PosixPath", "PurePosixPath", "os.path.join"} and node.args:
            folded = [_ep_eval_const_str(a) for a in node.args]
            if all(item is not None for item in folded):
                strs = [item for item in folded if item is not None]
                return "/".join(s.strip("/") if idx > 0 else s.rstrip("/") for idx, s in enumerate(strs) if s)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
            sep = _ep_eval_const_str(node.func.value)
            seq_arg = node.args[0]
            if sep is not None and isinstance(seq_arg, (ast.List, ast.Tuple)):
                elems = [_ep_eval_const_str(e) for e in seq_arg.elts]
                if all(item is not None for item in elems):
                    return sep.join(item for item in elems if item is not None)
    return None


def _ep_call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parent = _ep_call_name(func.value)
        return f"{parent}.{func.attr}" if parent else func.attr
    return ""


def _ep_inspect_python_code(
    code: str,
    *,
    workspace_root: Path | None,
) -> PolicyViolation | None:
    """Inspect Python source via AST constant folding for policy bypasses."""
    if not code or not code.strip():
        return None
    if "\x00" in code:
        return PolicyViolation("unauthorized_path", "nul_byte", "Refused: NUL byte in Python snippet.")
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Malformed Python may still contain literal secret/escape paths; fall back to token scan.
        for tok in re.findall(r"""(?:"([^"]+)"|'([^']+)')""", code):
            lit = tok[0] or tok[1]
            viol = _ep_check_candidate_path_token(
                lit, workspace_root=workspace_root, strict_abs_check=False
            )
            if viol is not None:
                return viol
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            cname = _ep_call_name(node.func)
            is_fs_call = cname in _EP_PY_FS_CALL_NAMES or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "read_text",
                    "write_text",
                    "read_bytes",
                    "write_bytes",
                    "open",
                    "glob",
                    "rglob",
                    "unlink",
                    "symlink_to",
                    "hardlink_to",
                }
            )
            if cname in _EP_PY_SHELL_CALL_NAMES and node.args:
                first = node.args[0]
                folded_cmd = _ep_eval_const_str(first)
                if folded_cmd is not None:
                    sub_viol = inspect_subprocess_policy(
                        folded_cmd, python_code=False, workspace_root=workspace_root
                    )
                    if sub_viol is not None:
                        return sub_viol
                elif isinstance(first, (ast.List, ast.Tuple)):
                    argv_strs = [_ep_eval_const_str(el) for el in first.elts]
                    for idx, item in enumerate(argv_strs):
                        if item is not None:
                            viol = _ep_check_candidate_path_token(
                                item,
                                workspace_root=workspace_root,
                                is_executable_position=(idx == 0),
                                strict_abs_check=True,
                            )
                            if viol is not None:
                                return viol
            for arg_node in [*node.args, *(kw.value for kw in node.keywords if kw.arg is not None)]:
                folded = _ep_eval_const_str(arg_node)
                if folded is not None:
                    viol = _ep_check_candidate_path_token(
                        folded,
                        workspace_root=workspace_root,
                        strict_abs_check=is_fs_call,
                    )
                    if viol is not None:
                        return viol
        elif isinstance(node, (ast.BinOp, ast.JoinedStr)):
            folded = _ep_eval_const_str(node)
            if folded is not None:
                viol = _ep_check_candidate_path_token(
                    folded, workspace_root=workspace_root, strict_abs_check=False
                )
                if viol is not None:
                    return viol
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for sub_part in re.split(r"[\s'\"()]+", node.value):
                if not sub_part:
                    continue
                viol = _ep_check_candidate_path_token(
                    sub_part, workspace_root=workspace_root, strict_abs_check=False
                )
                if viol is not None:
                    return viol
    return None


def _ep_split_shell_segments_quote_aware(command: str) -> tuple[list[str], bool]:
    """Split a shell command on unquoted ``;``, ``|``, ``&&``, ``||``, and newlines."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in (";", "\n", "\r"):
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf.clear()
            i += 1
            continue
        if ch == "|":
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf.clear()
            i += 2 if (i + 1 < n and command[i + 1] == "|") else 1
            continue
        if ch == "&" and i + 1 < n and command[i + 1] == "&":
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf.clear()
            i += 2
            continue
        buf.append(ch)
        i += 1
    if quote is not None:
        return [], False
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return segments, True


def _ep_space_unquoted_redirections(segment: str) -> str:
    """Insert whitespace around unquoted shell redirections without breaking quoted code or ``2>&1``."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(segment[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(segment[i + 1])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        # Preserve fd duplications like `2>&1`, `1>&2`, `>&2`.
        dup_m = re.match(r"^\d*>&\d+", segment[i:])
        if dup_m is not None:
            out.append(" ")
            out.append(dup_m.group(0))
            out.append(" ")
            i += len(dup_m.group(0))
            continue
        redir_m = re.match(r"^(?:\d*>>?|\d*<<?|\d*<>|&>>?)", segment[i:])
        if redir_m is not None:
            out.append(" ")
            out.append(redir_m.group(0))
            out.append(" ")
            i += len(redir_m.group(0))
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ep_inline_shell_payload(tokens: Sequence[str], cmd_idx: int) -> str | None:
    """The inline command of ``shell -c "<cmd>"`` (also ``-lc``/``-ec``/``busybox sh -c``).

    Returns ``None`` when the tokens hold no inline command (a script *path* is a
    separate, uninspected case: this policy reads command lines, not files).
    """
    exe = tokens[cmd_idx].rsplit("/", 1)[-1].lower()
    idx = cmd_idx + 1
    if exe == "busybox":
        while idx < len(tokens) and tokens[idx].startswith("-"):
            idx += 1
        if idx >= len(tokens):
            return None
        exe = tokens[idx].rsplit("/", 1)[-1].lower()
        idx += 1
    if exe not in _EP_SHELL_INTERPRETERS:
        return None
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--command":
            return tokens[idx + 1] if idx + 1 < len(tokens) else None
        if not tok.startswith("-"):
            return None  # a script path: nothing inline to inspect
        if not tok.startswith("--") and tok.endswith("c"):
            return tokens[idx + 1] if idx + 1 < len(tokens) else None
        idx += 1
    return None


def _ep_inline_code_payload(tokens: Sequence[str], cmd_idx: int) -> str | None:
    """Inline program text for interpreters that execute it without a file."""
    exe = tokens[cmd_idx].rsplit("/", 1)[-1].lower()
    if exe not in _EP_INLINE_CODE_INTERPRETERS:
        return None
    for idx in range(cmd_idx + 1, len(tokens)):
        tok = tokens[idx]
        if tok.startswith("--eval="):
            return tok.split("=", 1)[1] or None
        if tok in ("-e", "-r", "--eval", "--print") and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if not tok.startswith("-"):
            return None
    return None


def _ep_inspect_inline_code_paths(payload: str, *, workspace_root: Path | None) -> PolicyViolation | None:
    """Check the path-like substrings of uninspectable inline code.

    This is a token scan, not an execution model: it catches the honest form
    (``perl -e 'open(F,">>.git/config")'``) and cannot see a path assembled at
    runtime. The boundary for that is the sandbox (M5-P3-01), which is why this
    is described as defence in depth in docs/execution-profiles.md.
    """
    for match in _EP_INLINE_PATH_RE.finditer(payload or ""):
        viol = _ep_check_candidate_path_token(
            match.group(0), workspace_root=workspace_root, strict_abs_check=True
        )
        if viol is not None:
            return viol
    return None


def inspect_subprocess_policy(
    command_or_code: str,
    *,
    python_code: bool = False,
    workspace_root: Path | None = None,
    extra_args: Sequence[str] = (),
    _depth: int = 0,
) -> PolicyViolation | None:
    """Enforce unified managed policy on shell commands, Python snippets, and script arguments."""
    text = str(command_or_code or "").strip()
    if not text and not extra_args:
        return None
    if "\x00" in text:
        return PolicyViolation("unauthorized_path", "nul_byte", "Refused: NUL byte in subprocess input.")

    for extra in extra_args:
        viol = _ep_check_candidate_path_token(
            str(extra), workspace_root=workspace_root, is_executable_position=False, strict_abs_check=True
        )
        if viol is not None:
            return viol

    if python_code:
        return _ep_inspect_python_code(text, workspace_root=workspace_root)

    decoded = _ep_decode_ansi_c_escapes(text)

    git_block = workspace_snapshots.detect_destructive_git_command(decoded)
    if git_block:
        return PolicyViolation(
            error_code="destructive_command",
            rule="destructive_git",
            message=f"Refused: {git_block}",
        )

    for label, rx in _EP_DESTRUCTIVE_SHELL_PATTERNS:
        if rx.search(decoded):
            return PolicyViolation(
                error_code="destructive_command",
                rule="destructive_shell",
                message=f"Refused: blocked destructive shell pattern (`{label}`).",
            )

    if _EP_DANGEROUS_GIT_CONFIG_RE.search(decoded) and "git" in decoded.lower():
        return PolicyViolation(
            error_code="unauthorized_path",
            rule="vcs_protected_path",
            message="Refused: modifying executable Git hook/filter/transport configuration is forbidden.",
        )

    if _depth < 3:
        for subst_rx in (_EP_CMD_SUBST_DOLLAR_RE, _EP_CMD_SUBST_BACKTICK_RE):
            for m in subst_rx.finditer(decoded):
                nested = inspect_subprocess_policy(
                    m.group(1),
                    python_code=False,
                    workspace_root=workspace_root,
                    _depth=_depth + 1,
                )
                if nested is not None:
                    return nested

    segments, balanced = _ep_split_shell_segments_quote_aware(decoded)
    if not balanced:
        return PolicyViolation(
            error_code="policy_violation",
            rule="malformed_shell",
            message="Refused: shell command contains unbalanced quotes or malformed syntax.",
        )

    for raw_seg in segments:
        seg = raw_seg.strip()
        if not seg:
            continue
        spaced_seg = _ep_space_unquoted_redirections(seg)
        try:
            tokens = shlex.split(spaced_seg, posix=True)
        except ValueError:
            return PolicyViolation(
                error_code="policy_violation",
                rule="malformed_shell",
                message="Refused: shell command contains unbalanced quotes or malformed syntax.",
            )
        if not tokens:
            continue

        # Locate executable token (`cmd_idx`), skipping leading `KEY=VALUE` assignments and wrappers.
        cmd_idx = 0
        while cmd_idx < len(tokens):
            tok = tokens[cmd_idx]
            env_m = _EP_ENV_ASSIGN_RE.match(tok)
            if env_m is not None:
                rhs = env_m.group(2)
                for expanded in _ep_expand_simple_braces(rhs):
                    viol = _ep_check_candidate_path_token(
                        expanded, workspace_root=workspace_root, strict_abs_check=True
                    )
                    if viol is not None:
                        return viol
                cmd_idx += 1
                continue
            base_cmd = tok.rsplit("/", 1)[-1].lower()
            if base_cmd in _EP_WRAPPER_BINARIES:
                cmd_idx += 1
                while cmd_idx < len(tokens) and tokens[cmd_idx].startswith("-"):
                    cmd_idx += 1
                continue
            break

        if cmd_idx < len(tokens):
            exe_tok = tokens[cmd_idx]
            exe_base = exe_tok.rsplit("/", 1)[-1].lower()
            if exe_base in _EP_FORBIDDEN_PRIV_BINARIES:
                return PolicyViolation(
                    error_code="policy_violation",
                    rule="privilege_escalation",
                    message=f"Refused: invoking `{exe_base}` inside tool commands is forbidden by unified policy.",
                )
            # Inspect `python -c <code>` / `python3 -c <code>` payloads with the Python AST inspector.
            if exe_base.startswith("python"):
                for idx in range(cmd_idx + 1, len(tokens)):
                    if tokens[idx] == "-c" and idx + 1 < len(tokens):
                        py_viol = _ep_inspect_python_code(
                            tokens[idx + 1], workspace_root=workspace_root
                        )
                        if py_viol is not None:
                            return py_viol
                        break

            # `sh -c "<command>"` is a second command line: inspect it as one.
            # Without this the payload is a single quoted token that the path
            # checks below cannot parse, and `sh -c 'cat .env'` would be allowed
            # where `cat .env` is refused.
            shell_payload = _ep_inline_shell_payload(tokens, cmd_idx)
            if shell_payload is not None and _depth < _EP_MAX_NESTING:
                shell_viol = inspect_subprocess_policy(
                    shell_payload,
                    python_code=False,
                    workspace_root=workspace_root,
                    _depth=_depth + 1,
                )
                if shell_viol is not None:
                    return shell_viol

            # A shell with no `-c` payload and no script argument reads its
            # command from stdin (`curl ... | sh`, `sh <<EOF`), which no
            # command-line inspection can see. Fail closed.
            if (
                exe_base in _EP_SHELL_INTERPRETERS
                and shell_payload is None
                and not any(
                    not tok.startswith("-")
                    for tok in tokens[cmd_idx + 1 :]
                )
            ):
                return PolicyViolation(
                    error_code="policy_violation",
                    rule="uninspectable_shell_source",
                    message=(
                        f"Refused: `{exe_base}` with no inline command or script reads its command "
                        "from stdin, which cannot be policy-checked; pass `-c <command>` or a script path."
                    ),
                )

            # Inline program text for interpreters there is no parser for: check
            # every path-like substring against the same rules.
            code_payload = _ep_inline_code_payload(tokens, cmd_idx)
            if code_payload is not None:
                code_viol = _ep_inspect_inline_code_paths(code_payload, workspace_root=workspace_root)
                if code_viol is not None:
                    return code_viol

        skip_redir_ops = {">", ">>", "<", "<<", "<>", "&>", "&>>", "1>", "1>>", "2>", "2>>", ">&2", "2>&1", "1>&2"}
        py_inline_idx: int | None = None
        if cmd_idx < len(tokens) and tokens[cmd_idx].rsplit("/", 1)[-1].lower().startswith("python"):
            for idx in range(cmd_idx + 1, len(tokens)):
                if tokens[idx] == "-c" and idx + 1 < len(tokens):
                    py_inline_idx = idx + 1
                    break

        for idx in range(cmd_idx, len(tokens)):
            if idx == py_inline_idx:
                continue
            tok = tokens[idx]
            if tok in skip_redir_ops or re.fullmatch(r"\d*>&\d+", tok):
                continue
            redir_m = _EP_REDIR_PREFIX_RE.match(tok)
            if redir_m is not None:
                tok = redir_m.group(1).strip()
                if not tok:
                    continue
            is_exe = idx == cmd_idx
            # Check `--flag=value` options for embedded paths (e.g. `--env-file=.env`, `-f=.git/config`).
            candidates: list[tuple[str, bool]] = [(tok, is_exe)]
            if tok.startswith("-") and "=" in tok:
                _, opt_val = tok.split("=", 1)
                if opt_val:
                    candidates.append((opt_val, False))
            for cand_str, cand_is_exe in candidates:
                for expanded in _ep_expand_simple_braces(cand_str):
                    viol = _ep_check_candidate_path_token(
                        expanded,
                        workspace_root=workspace_root,
                        is_executable_position=cand_is_exe,
                        strict_abs_check=True,
                    )
                    if viol is not None:
                        return viol

    return None


def evaluate_unified_policy(
    descriptor: CapabilityDescriptor,
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    effective_policy: str,
    workspace_root: Path | None = None,
) -> PolicyViolation | None:
    """Evaluate unified managed & subprocess policy before action admission."""
    safe_args = args if isinstance(args, Mapping) else {}
    clean = descriptor.tool_name

    # 1. Read-only `plan` policy parity across all capability descriptors.
    if effective_policy == "plan":
        if (
            descriptor.action_class != "read_only"
            or descriptor.process_capability != "none"
            or descriptor.fs_capability not in {"none", "workspace_read"}
        ):
            return PolicyViolation(
                error_code="policy_violation",
                rule="plan_read_only",
                message=f"Tool `{clean}` is not permitted by read-only plan policy.",
            )

    # 2. Managed workspace path confinement & secret/VCS parity.
    if clean in {"read_file", "write_file", "edit_file", "list_files", "grep_search"}:
        raw_p = str(safe_args.get("path") or ("." if clean in {"list_files", "grep_search"} else ""))
        if raw_p:
            _, viol = normalize_workspace_rel_path(workspace_root, raw_p)
            if viol is not None:
                return viol
            glob_hit = _ep_is_targeted_secret_glob(raw_p)
            if glob_hit is not None:
                return PolicyViolation(
                    error_code="unauthorized_path",
                    rule="secret_path",
                    message=f"Refused: access to protected secret path `{glob_hit}` is forbidden.",
                    target=glob_hit,
                )
        if clean == "list_files":
            pat_hit = _ep_is_targeted_secret_glob(str(safe_args.get("pattern") or ""))
            if pat_hit is not None:
                return PolicyViolation(
                    error_code="unauthorized_path",
                    rule="secret_path",
                    message=f"Refused: listing pattern targeting protected secret `{pat_hit}` is forbidden.",
                    target=pat_hit,
                )

    if clean == "ctx_run_block":
        block = execution_spine.normalize_reusable_block_name(safe_args.get("block"))
        target = str(safe_args.get("target") or "")
        if block in {"json-peek", "csv-peek", "shortcut-debt"}:
            if target:
                _, viol = normalize_workspace_rel_path(workspace_root, target)
                if viol is not None:
                    return viol
        elif block in execution_spine.CONSEQUENTIAL_BLOCKS:
            cmd = target.strip() or "python3 -m unittest discover"
            viol = inspect_subprocess_policy(
                cmd, python_code=False, workspace_root=workspace_root
            )
            if viol is not None:
                return viol

    # 3. Subprocess command / Python / skill-script parity with managed policy.
    if clean == "run_bash":
        cmd = str(safe_args.get("command") or "")
        return inspect_subprocess_policy(cmd, python_code=False, workspace_root=workspace_root)

    if clean == "ctx_execute_python":
        code = str(safe_args.get("code") or "")
        return inspect_subprocess_policy(code, python_code=True, workspace_root=workspace_root)

    if clean == "skill_execute_script":
        script_raw = str(safe_args.get("script") or "")
        raw_extra = safe_args.get("args")
        extra_seq: list[str] = (
            [str(x) for x in raw_extra]
            if isinstance(raw_extra, Sequence) and not isinstance(raw_extra, (str, bytes))
            else []
        )
        if script_raw:
            _, s_viol = normalize_workspace_rel_path(workspace_root, script_raw)
            if s_viol is not None:
                return s_viol
        return inspect_subprocess_policy(
            "",
            python_code=False,
            workspace_root=workspace_root,
            extra_args=extra_seq,
        )

    return None


# ---------------------------------------------------------------------------
# 4. Pre/post workspace observation & expansion reconciliation (Task 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedEntryState:
    """Lightweight pre/post snapshot entry for one workspace or protected VCS path."""

    rel_path: str
    kind: Literal["file", "symlink"]
    size: int
    mtime_ns: int
    mode: int
    symlink_target: str = ""
    symlink_escapes: bool = False
    is_secret: bool = False
    is_protected_vcs: bool = False
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceFootprintState:
    """Bounded pre- or post-execution workspace state snapshot."""

    workspace_root: Path
    entries: dict[str, ObservedEntryState]
    truncated: bool = False
    ignored_paths: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class FootprintReconciliation:
    """Result of observing and reconciling an action's actual filesystem side effects."""

    status: ReconciliationStatus
    declared_scope: FootprintScope
    declared_write_paths: tuple[str, ...]
    created_paths: tuple[str, ...] = ()
    modified_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    touched_paths: tuple[str, ...] = ()
    undeclared_paths: tuple[str, ...] = ()
    reconciled_paths: tuple[str, ...] = ()
    quarantined_paths: tuple[str, ...] = ()
    scope_violations: tuple[str, ...] = ()
    policy_violations: tuple[dict[str, str], ...] = ()
    expanded: bool = False
    uncertain_required: bool = False
    scan_truncated: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": EP_SCHEMA_VERSION,
            "status": self.status,
            "declared_scope": self.declared_scope,
            "declared_write_paths": list(self.declared_write_paths),
            "created_paths": list(self.created_paths),
            "modified_paths": list(self.modified_paths),
            "deleted_paths": list(self.deleted_paths),
            "touched_paths": list(self.touched_paths),
            "undeclared_paths": list(self.undeclared_paths),
            "reconciled_paths": list(self.reconciled_paths),
            "quarantined_paths": list(self.quarantined_paths),
            "scope_violations": list(self.scope_violations),
            "policy_violations": [dict(v) for v in self.policy_violations],
            "expanded": self.expanded,
            "uncertain_required": self.uncertain_required,
            "scan_truncated": self.scan_truncated,
        }


def _ep_hash_file_small(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    """Compute a streaming SHA-256 of ``path`` without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    h = hashlib.sha256()
    remaining = max_bytes
    try:
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    except OSError:
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return h.hexdigest()


def _ep_record_entry(
    resolved_ws: Path,
    full_path: Path,
    rel_posix: str,
    st: os.stat_result,
) -> ObservedEntryState | None:
    is_sec = is_protected_secret_rel_path(rel_posix)
    is_vcs = is_protected_vcs_rel_path(rel_posix)
    if stat.S_ISLNK(st.st_mode):
        try:
            link_target = os.readlink(full_path)
        except OSError:
            link_target = ""
        escapes = False
        try:
            resolved_target = full_path.resolve()
            resolved_target.relative_to(resolved_ws)
        except (OSError, ValueError):
            escapes = True
        return ObservedEntryState(
            rel_path=rel_posix,
            kind="symlink",
            size=int(st.st_size),
            mtime_ns=int(st.st_mtime_ns),
            mode=stat.S_IMODE(st.st_mode),
            symlink_target=link_target,
            symlink_escapes=escapes,
            is_secret=is_sec,
            is_protected_vcs=is_vcs,
        )
    if stat.S_ISREG(st.st_mode):
        digest = _ep_hash_file_small(full_path) if (is_sec or is_vcs) else ""
        return ObservedEntryState(
            rel_path=rel_posix,
            kind="file",
            size=int(st.st_size),
            mtime_ns=int(st.st_mtime_ns),
            mode=stat.S_IMODE(st.st_mode),
            is_secret=is_sec,
            is_protected_vcs=is_vcs,
            sha256=digest,
        )
    return None


def capture_footprint_state(
    workspace_root: Path | None,
    *,
    max_entries: int = DEFAULT_MAX_FOOTPRINT_SCAN_ENTRIES,
    ignore_paths: Sequence[Path] = (),
) -> WorkspaceFootprintState | None:
    """Capture a bounded, non-secret metadata snapshot of ``workspace_root``."""
    if workspace_root is None:
        return None
    try:
        if workspace_root.is_symlink() or not workspace_root.is_dir():
            return None
        resolved_ws = workspace_root.resolve()
    except OSError:
        return None

    ignored_resolved: set[Path] = set()
    for ig in ignore_paths:
        try:
            ignored_resolved.add(Path(ig).expanduser().resolve())
        except OSError:
            continue

    entries: dict[str, ObservedEntryState] = {}
    truncated = False
    scanned = 0
    stack: list[tuple[Path, str]] = [(resolved_ws, "")]

    while stack:
        cur_dir, prefix = stack.pop()
        try:
            with os.scandir(cur_dir) as it:
                dir_items = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for dent in dir_items:
            full_entry = cur_dir / dent.name
            if ignored_resolved and full_entry in ignored_resolved:
                continue
            name = dent.name
            rel_posix = f"{prefix}/{name}" if prefix else name
            scanned += 1
            if scanned > max_entries:
                truncated = True
                stack.clear()
                break
            try:
                st = dent.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                if name == ".git" and not prefix:
                    _ep_scan_protected_git_control(resolved_ws, cur_dir / ".git", entries)
                    continue
                if name in workspace_snapshots.HEAVY_SKIP_DIRS:
                    continue
                stack.append((cur_dir / name, rel_posix))
                continue
            rec = _ep_record_entry(resolved_ws, cur_dir / name, rel_posix, st)
            if rec is not None:
                entries[rel_posix] = rec

    return WorkspaceFootprintState(
        workspace_root=resolved_ws,
        entries=entries,
        truncated=truncated,
        ignored_paths=tuple(sorted(ignored_resolved)),
    )


def _ep_scan_protected_git_control(
    resolved_ws: Path,
    git_dir: Path,
    entries: dict[str, ObservedEntryState],
) -> None:
    """Inspect only security-relevant Git control files (never ``.git/objects`` or ``.git/index`` stat cache)."""
    for fixed_rel in ("config", "config.worktree", "info/attributes"):
        full = git_dir / fixed_rel
        try:
            st = os.lstat(full)
        except OSError:
            continue
        rec = _ep_record_entry(resolved_ws, full, f".git/{fixed_rel}", st)
        if rec is not None:
            entries[f".git/{fixed_rel}"] = rec
    hooks_dir = git_dir / "hooks"
    try:
        with os.scandir(hooks_dir) as it:
            for dent in it:
                if dent.name.endswith(".sample"):
                    continue
                try:
                    st = dent.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel_p = f".git/hooks/{dent.name}"
                rec = _ep_record_entry(resolved_ws, hooks_dir / dent.name, rel_p, st)
                if rec is not None:
                    entries[rel_p] = rec
    except OSError:
        return


def _ep_entry_changed(before: ObservedEntryState, after: ObservedEntryState) -> bool:
    if before.kind != after.kind:
        return True
    if before.kind == "symlink":
        return before.symlink_target != after.symlink_target or before.symlink_escapes != after.symlink_escapes
    if before.sha256 and after.sha256:
        return before.sha256 != after.sha256
    return (
        before.size != after.size
        or before.mtime_ns != after.mtime_ns
        or before.mode != after.mode
    )


def _ep_quarantine_created_artifact(resolved_ws: Path, rel_posix: str) -> bool:
    """Unlink a newly planted forbidden regular file or symlink without following symlinks."""
    parts = [p for p in rel_posix.split("/") if p not in ("", ".")]
    if not parts:
        return False
    cur = resolved_ws
    for seg in parts[:-1]:
        cur = cur / seg
        try:
            st_dir = os.lstat(cur)
        except OSError:
            return False
        if not stat.S_ISDIR(st_dir.st_mode) or stat.S_ISLNK(st_dir.st_mode):
            return False
    leaf = cur / parts[-1]
    try:
        st_leaf = os.lstat(leaf)
        if stat.S_ISREG(st_leaf.st_mode) or stat.S_ISLNK(st_leaf.st_mode):
            os.unlink(leaf)
            return True
    except OSError:
        return False
    return False


_EP_DANGEROUS_GIT_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "core.fsmonitor",
        "core.sshcommand",
        "core.hookspath",
        "core.gitproxy",
        "credential.helper",
    }
)


def _ep_git_config_directives(text: str) -> list[str]:
    """Dotted ``section[.sub].key`` names in a Git config file, in file order.

    Git writes the *sectioned* form (``[credential]`` then ``helper = ...``), never
    the dotted form, so a regex over the raw text recognises only what a human
    would type at the command line. This parses sections, subsections and keys
    so both spellings resolve to the same name.
    """
    names: list[str] = []
    section = ""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            end = line.find("]")
            inner = line[1:end] if end != -1 else line[1:]
            if end == -1:
                continue  # malformed header: nothing to attribute keys to
            head, _, rest = inner.partition(" ")
            sub = rest.strip().strip('"')
            section = head.strip().lower() + (f".{sub.lower()}" if sub else "")
            continue
        key, sep, _value = line.partition("=")
        key = key.strip().lower()
        if not key or not sep:
            if key:
                names.append(f"{section}.{key}" if section else key)
            continue
        names.append(f"{section}.{key}" if section else key)
    return names


def _ep_git_config_dangerous_directives(text: str) -> list[str]:
    """The directives that make a config file an execution or secret-exfiltration vector."""
    found: list[str] = []
    for name in _ep_git_config_directives(text):
        if name in _EP_DANGEROUS_GIT_CONFIG_KEYS:
            found.append(name)
        elif name.startswith("filter.") and name.rsplit(".", 1)[-1] in {"clean", "smudge"}:
            found.append(name)
        elif name.startswith("include.") or (
            name.startswith("includeif.") and name.endswith(".path")
        ):
            # An include pulls in configuration this policy never inspected,
            # including any directive listed above.
            found.append(name)
    return found


def _ep_git_config_has_dangerous_directives(git_config_path: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(git_config_path, flags)
    except OSError:
        return True
    try:
        raw = os.read(fd, 65536).decode("utf-8", errors="replace")
    except OSError:
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return bool(_ep_git_config_dangerous_directives(raw))


def observe_and_reconcile(
    profile: ExecutionProfile,
    pre_state: WorkspaceFootprintState | None,
    *,
    max_entries: int = DEFAULT_MAX_FOOTPRINT_SCAN_ENTRIES,
) -> FootprintReconciliation:
    """Observe post-execution filesystem mutations, quarantine planted violations, and reconcile expansion."""
    declared_writes = profile.footprint.declared_write_paths
    declared_set = set(declared_writes)

    if pre_state is None:
        return FootprintReconciliation(
            status="clean",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
        )

    post_state = capture_footprint_state(
        pre_state.workspace_root,
        max_entries=max_entries,
        ignore_paths=pre_state.ignored_paths,
    )
    if post_state is None:
        return FootprintReconciliation(
            status="scope_violation",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
            scope_violations=("workspace_disappeared",),
            uncertain_required=True,
        )

    before_map = pre_state.entries
    after_map = post_state.entries

    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for rel_p in sorted(set(before_map) | set(after_map)):
        b_ent = before_map.get(rel_p)
        a_ent = after_map.get(rel_p)
        if b_ent is None and a_ent is not None:
            # Allow benign `git init` `.git/config` creation only if it carries no hook/filter/ssh directives.
            if rel_p == ".git/config" and not _ep_git_config_has_dangerous_directives(
                pre_state.workspace_root / ".git" / "config"
            ):
                continue
            created.append(rel_p)
        elif b_ent is not None and a_ent is None:
            deleted.append(rel_p)
        elif b_ent is not None and a_ent is not None and _ep_entry_changed(b_ent, a_ent):
            modified.append(rel_p)

    touched = sorted(set(created) | set(modified) | set(deleted))
    undeclared = [p for p in touched if p not in declared_set]
    expanded = bool(undeclared)

    policy_violations: list[dict[str, str]] = []
    quarantined: list[str] = []
    uncertain_required = False

    for rel_p in created:
        a_ent = after_map[rel_p]
        if a_ent.is_secret:
            if _ep_quarantine_created_artifact(pre_state.workspace_root, rel_p):
                quarantined.append(rel_p)
            else:
                uncertain_required = True
            policy_violations.append(
                {"path": rel_p.rsplit("/", 1)[-1], "reason": "protected_secret_created"}
            )
        elif a_ent.is_protected_vcs:
            if rel_p.startswith(".git/hooks/") and _ep_quarantine_created_artifact(
                pre_state.workspace_root, rel_p
            ):
                quarantined.append(rel_p)
            else:
                uncertain_required = True
            policy_violations.append({"path": rel_p, "reason": "protected_vcs_created"})
        elif a_ent.kind == "symlink" and a_ent.symlink_escapes:
            if _ep_quarantine_created_artifact(pre_state.workspace_root, rel_p):
                quarantined.append(rel_p)
            else:
                uncertain_required = True
            policy_violations.append({"path": rel_p, "reason": "escaping_symlink_created"})

    for rel_p in modified:
        a_ent = after_map[rel_p]
        b_ent = before_map[rel_p]
        if a_ent.is_secret or b_ent.is_secret:
            uncertain_required = True
            policy_violations.append(
                {"path": rel_p.rsplit("/", 1)[-1], "reason": "protected_secret_modified"}
            )
        elif a_ent.is_protected_vcs or b_ent.is_protected_vcs:
            uncertain_required = True
            policy_violations.append({"path": rel_p, "reason": "protected_vcs_modified"})
        elif a_ent.kind == "symlink" and a_ent.symlink_escapes:
            uncertain_required = True
            policy_violations.append({"path": rel_p, "reason": "escaping_symlink_modified"})

    for rel_p in deleted:
        b_ent = before_map[rel_p]
        if b_ent.is_secret:
            uncertain_required = True
            policy_violations.append(
                {"path": rel_p.rsplit("/", 1)[-1], "reason": "protected_secret_deleted"}
            )
        elif b_ent.is_protected_vcs:
            uncertain_required = True
            policy_violations.append({"path": rel_p, "reason": "protected_vcs_deleted"})

    scan_truncated = bool(pre_state.truncated or post_state.truncated)

    if policy_violations:
        return FootprintReconciliation(
            status="policy_violation",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
            created_paths=tuple(created),
            modified_paths=tuple(modified),
            deleted_paths=tuple(deleted),
            touched_paths=tuple(touched),
            undeclared_paths=tuple(undeclared),
            reconciled_paths=(),
            quarantined_paths=tuple(quarantined),
            scope_violations=(),
            policy_violations=tuple(policy_violations),
            expanded=expanded,
            uncertain_required=uncertain_required,
            scan_truncated=scan_truncated,
        )

    descriptor = profile.descriptor
    if descriptor.action_class == "read_only" and touched:
        return FootprintReconciliation(
            status="scope_violation",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
            created_paths=tuple(created),
            modified_paths=tuple(modified),
            deleted_paths=tuple(deleted),
            touched_paths=tuple(touched),
            undeclared_paths=tuple(undeclared),
            scope_violations=tuple(touched),
            expanded=True,
            uncertain_required=True,
            scan_truncated=scan_truncated,
        )

    if descriptor.side_effect_certainty == "exact" and undeclared:
        return FootprintReconciliation(
            status="scope_violation",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
            created_paths=tuple(created),
            modified_paths=tuple(modified),
            deleted_paths=tuple(deleted),
            touched_paths=tuple(touched),
            undeclared_paths=tuple(undeclared),
            scope_violations=tuple(undeclared),
            expanded=True,
            uncertain_required=True,
            scan_truncated=scan_truncated,
        )

    if expanded:
        return FootprintReconciliation(
            status="reconciled_expansion",
            declared_scope=profile.footprint.scope,
            declared_write_paths=declared_writes,
            created_paths=tuple(created),
            modified_paths=tuple(modified),
            deleted_paths=tuple(deleted),
            touched_paths=tuple(touched),
            undeclared_paths=tuple(undeclared),
            reconciled_paths=tuple(undeclared),
            expanded=True,
            uncertain_required=False,
            scan_truncated=scan_truncated,
        )

    return FootprintReconciliation(
        status="clean",
        declared_scope=profile.footprint.scope,
        declared_write_paths=declared_writes,
        created_paths=tuple(created),
        modified_paths=tuple(modified),
        deleted_paths=tuple(deleted),
        touched_paths=tuple(touched),
        undeclared_paths=(),
        reconciled_paths=(),
        expanded=False,
        uncertain_required=False,
        scan_truncated=scan_truncated,
    )


class FootprintReconciliationLedger:
    """Owner-only JSONL ledger recording reconciled expansions and violations per session."""

    def __init__(
        self,
        store_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_FOOTPRINT_LEDGER_BYTES,
        retain_records: int = DEFAULT_RETAIN_FOOTPRINT_RECORDS,
    ) -> None:
        self._root = Path(store_dir) / "footprints"
        self._max_bytes = max(65536, int(max_bytes))
        self._retain_records = max(10, int(retain_records))

    def _ledger_path(self, session_id: str) -> Path:
        _ep_ensure_private_dir(self._root)
        return self._root / f"{_ep_safe_session_filename(session_id)}.jsonl"

    def append(
        self,
        session_id: str,
        *,
        tool_name: str,
        action_id: str,
        profile: ExecutionProfile,
        reconciliation: FootprintReconciliation,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": EP_SCHEMA_VERSION,
            "record_id": f"fpr-{uuid.uuid4().hex[:16]}",
            "recorded_at": _ep_now(),
            "session_id": str(session_id or ""),
            "action_id": str(action_id or ""),
            "tool_name": tool_name,
            "profile": profile.to_mapping(),
            "reconciliation": reconciliation.to_mapping(),
        }
        line = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        path = self._ledger_path(session_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        with _EP_LEDGER_LOCK:
            fd = os.open(path, flags, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            _ep_restrict_file(path)
            try:
                if path.stat().st_size > self._max_bytes:
                    self._rotate_locked(path)
            except OSError:
                pass
        return payload

    def _rotate_locked(self, path: Path) -> None:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = raw_lines[-self._retain_records :]
        tmp = path.with_suffix(".jsonl.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ("\n".join(kept) + ("\n" if kept else "")).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        _ep_restrict_file(path)

    def list_records(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        path = self._ledger_path(session_id)
        if not path.is_file():
            return []
        with _EP_LEDGER_LOCK:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        parsed: list[dict[str, Any]] = []
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                parsed.append(obj)
                if len(parsed) >= max(1, limit):
                    break
        return parsed

    def purge_session(self, session_id: str) -> bool:
        """Delete persisted footprint reconciliation records for ``session_id``."""
        path = self._ledger_path(session_id)
        removed = False
        with _EP_LEDGER_LOCK:
            for candidate in (path, path.with_suffix(".jsonl.prev"), path.with_suffix(".jsonl.tmp")):
                try:
                    st = os.lstat(candidate)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                    candidate.unlink()
                    removed = True
        return removed


# ---------------------------------------------------------------------------
# 5. Admission-integrated barrier coordinator (Task 3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _EPActiveHandle:
    lease_id: str
    session_id: str
    tool_name: str
    profile: ExecutionProfile
    pre_state: WorkspaceFootprintState | None
    workspace_root: Path | None
    store_dir: Path | None
    task_id: str | None = None
    epoch: int | None = None
    worker_id: str | None = None
    on_reconciled: Callable[[str, ExecutionProfile, FootprintReconciliation], None] | None = None
    released: bool = False


@dataclass(frozen=True, slots=True)
class _EPReservedLease:
    lease_id: str
    action_lease: resource_admission.AdmissionLease
    subprocess_lease: resource_admission.AdmissionLease | None
    mutation_lease: resource_admission.AdmissionLease | None
    barriers: tuple[ResourceBarrier, ...]


class UnifiedExecutionController:
    """Shared in-process resource and barrier controller backed by ``ResourceAdmissionScheduler``."""

    def __init__(
        self,
        *,
        memory_budget_bytes: int = DEFAULT_ACTION_MEMORY_BUDGET_BYTES,
        action_slots: int = DEFAULT_ACTION_SLOTS,
        subprocess_slots: int = DEFAULT_SUBPROCESS_SLOTS,
        mutation_slots: int = DEFAULT_MUTATION_SLOTS,
    ) -> None:
        self._lock = threading.Lock()
        self._action_scheduler = resource_admission.ResourceAdmissionScheduler(
            resource_admission.ResourceCapacity(memory_bytes=memory_budget_bytes, slots=action_slots)
        )
        self._subprocess_scheduler = resource_admission.ResourceAdmissionScheduler(
            resource_admission.ResourceCapacity(memory_bytes=memory_budget_bytes, slots=subprocess_slots)
        )
        self._mutation_scheduler = resource_admission.ResourceAdmissionScheduler(
            resource_admission.ResourceCapacity(memory_bytes=memory_budget_bytes, slots=mutation_slots)
        )
        self._barrier_holders: dict[str, dict[str, BarrierMode]] = {}
        self._active_leases: dict[str, _EPReservedLease] = {}

    @property
    def action_scheduler(self) -> resource_admission.ResourceAdmissionScheduler:
        return self._action_scheduler

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            act_snap = self._action_scheduler.snapshot()
            sub_snap = self._subprocess_scheduler.snapshot()
            mut_snap = self._mutation_scheduler.snapshot()
            active_barriers = {
                target: {
                    "exclusive": sum(1 for m in holders.values() if m == "exclusive"),
                    "shared": sum(1 for m in holders.values() if m == "shared"),
                }
                for target, holders in sorted(self._barrier_holders.items())
                if holders
            }
            return {
                "memory_budget_bytes": act_snap.capacity.memory_bytes,
                "used_memory_bytes": act_snap.used_memory_bytes,
                "available_memory_bytes": act_snap.available_memory_bytes,
                "action_slots": {
                    "capacity": act_snap.capacity.slots,
                    "used": act_snap.used_slots,
                    "available": act_snap.available_slots,
                },
                "subprocess_slots": {
                    "capacity": sub_snap.capacity.slots,
                    "used": sub_snap.used_slots,
                    "available": sub_snap.available_slots,
                },
                "mutation_slots": {
                    "capacity": mut_snap.capacity.slots,
                    "used": mut_snap.used_slots,
                    "available": mut_snap.available_slots,
                },
                "active_leases": len(self._active_leases),
                "active_barriers": active_barriers,
            }

    def admit_footprint(self, footprint: ResourceFootprint) -> BarrierAdmissionDecision:
        """Atomically acquire resource capacity and shared/exclusive barriers or deny cleanly."""
        with self._lock:
            conflicts: list[str] = []
            seen_targets: dict[str, BarrierMode] = {}
            for barrier in footprint.barriers:
                existing = self._barrier_holders.get(barrier.target)
                if existing:
                    if barrier.mode == "exclusive":
                        conflicts.append(barrier.target)
                    elif any(mode == "exclusive" for mode in existing.values()):
                        conflicts.append(barrier.target)
                prior_req = seen_targets.get(barrier.target)
                if prior_req != "exclusive":
                    seen_targets[barrier.target] = barrier.mode

            if conflicts:
                return BarrierAdmissionDecision(
                    allowed=False,
                    conflicting_barriers=tuple(dict.fromkeys(conflicts)),
                )

            exhausted: list[str] = []
            act_snap = self._action_scheduler.snapshot()
            if act_snap.available_memory_bytes < footprint.memory_bytes:
                exhausted.append("memory_bytes")
            if act_snap.available_slots < footprint.action_slots:
                exhausted.append("action_slots")
            if footprint.subprocess_slots > 0:
                sub_snap = self._subprocess_scheduler.snapshot()
                if sub_snap.available_slots < footprint.subprocess_slots:
                    exhausted.append("subprocess_slots")
            if footprint.mutation_slots > 0:
                mut_snap = self._mutation_scheduler.snapshot()
                if mut_snap.available_slots < footprint.mutation_slots:
                    exhausted.append("mutation_slots")
            if exhausted:
                return BarrierAdmissionDecision(
                    allowed=False,
                    exhausted_resources=tuple(exhausted),
                )

            act_lease = self._action_scheduler.try_admit(footprint.to_resource_request())
            if act_lease is None:
                return BarrierAdmissionDecision(
                    allowed=False,
                    exhausted_resources=("action_slots",),
                )
            sub_lease: resource_admission.AdmissionLease | None = None
            if footprint.subprocess_slots > 0:
                sub_lease = self._subprocess_scheduler.try_admit(
                    resource_admission.ResourceRequest(memory_bytes=1, slots=footprint.subprocess_slots)
                )
                if sub_lease is None:
                    self._action_scheduler.release(act_lease)
                    return BarrierAdmissionDecision(
                        allowed=False,
                        exhausted_resources=("subprocess_slots",),
                    )
            mut_lease: resource_admission.AdmissionLease | None = None
            if footprint.mutation_slots > 0:
                mut_lease = self._mutation_scheduler.try_admit(
                    resource_admission.ResourceRequest(memory_bytes=1, slots=footprint.mutation_slots)
                )
                if mut_lease is None:
                    if sub_lease is not None:
                        self._subprocess_scheduler.release(sub_lease)
                    self._action_scheduler.release(act_lease)
                    return BarrierAdmissionDecision(
                        allowed=False,
                        exhausted_resources=("mutation_slots",),
                    )

            dedup_barriers = tuple(
                ResourceBarrier(target=t, mode=m) for t, m in seen_targets.items()
            )
            lease_id = act_lease.lease_id
            for barrier in dedup_barriers:
                self._barrier_holders.setdefault(barrier.target, {})[lease_id] = barrier.mode
            self._active_leases[lease_id] = _EPReservedLease(
                lease_id=lease_id,
                action_lease=act_lease,
                subprocess_lease=sub_lease,
                mutation_lease=mut_lease,
                barriers=dedup_barriers,
            )
            return BarrierAdmissionDecision(allowed=True, lease_id=lease_id)

    def release(self, lease_id: str) -> bool:
        """Atomically release barriers and capacity reservations for ``lease_id``."""
        with self._lock:
            reserved = self._active_leases.pop(lease_id, None)
            if reserved is None:
                return False
            for barrier in reserved.barriers:
                holders = self._barrier_holders.get(barrier.target)
                if holders is not None:
                    holders.pop(lease_id, None)
                    if not holders:
                        self._barrier_holders.pop(barrier.target, None)
            if reserved.mutation_lease is not None:
                self._mutation_scheduler.release(reserved.mutation_lease)
            if reserved.subprocess_lease is not None:
                self._subprocess_scheduler.release(reserved.subprocess_lease)
            self._action_scheduler.release(reserved.action_lease)
            return True

    def bind(
        self,
        *,
        workspace_root: Path | None = None,
        store_dir: Path | None = None,
        ignore_paths: Sequence[Path] = (),
        execution_mode_fn: Callable[[], str] | None = None,
        container_qualified: bool = False,
        on_reconciled: Callable[[str, ExecutionProfile, FootprintReconciliation], None] | None = None,
        containment_store: process_supervision.ContainmentStore | None = None,
        current_boot_id: str | None = None,
        sql_task_fencing: bool = False,
    ) -> UnifiedExecutionCoordinator:
        return UnifiedExecutionCoordinator(
            self,
            workspace_root=workspace_root,
            store_dir=store_dir,
            ignore_paths=ignore_paths,
            execution_mode_fn=execution_mode_fn,
            container_qualified=container_qualified,
            on_reconciled=on_reconciled,
            containment_store=containment_store,
            current_boot_id=current_boot_id,
            sql_task_fencing=sql_task_fencing,
        )


class UnifiedExecutionCoordinator:
    """Per-call adapter implementing ``execution_spine.ExecutionProfileCoordinator``."""

    def __init__(
        self,
        controller: UnifiedExecutionController,
        *,
        workspace_root: Path | None = None,
        store_dir: Path | None = None,
        ignore_paths: Sequence[Path] = (),
        execution_mode_fn: Callable[[], str] | None = None,
        container_qualified: bool = False,
        on_reconciled: Callable[[str, ExecutionProfile, FootprintReconciliation], None] | None = None,
        containment_store: process_supervision.ContainmentStore | None = None,
        current_boot_id: str | None = None,
        sql_task_fencing: bool = False,
    ) -> None:
        self._controller = controller
        self._workspace_root = workspace_root
        self._store_dir = store_dir
        self._ignore_paths = tuple(ignore_paths)
        self._execution_mode_fn = execution_mode_fn
        self._container_qualified = container_qualified
        self._on_reconciled = on_reconciled
        self._containment_store = containment_store or (
            process_supervision.ContainmentStore(store_dir) if store_dir is not None else None
        )
        self._current_boot_id = current_boot_id
        self._sql_task_fencing = bool(sql_task_fencing)

    def _current_execution_mode(self) -> str:
        if self._execution_mode_fn is None:
            return "disabled"
        try:
            return str(self._execution_mode_fn() or "disabled")
        except Exception:
            return "disabled"

    def _persist_barrier_hold(
        self,
        handle: _EPActiveHandle,
        *,
        action_id: str,
        reason: str,
    ) -> None:
        if self._containment_store is None:
            return
        if not execution_spine.is_consequential_action_class(handle.profile.descriptor.action_class):
            return
        if handle.profile.descriptor.execution_surface == "managed" and reason not in {
            "footprint_policy_violation",
            "undeclared_footprint_expansion",
        }:
            return
        barrier_tuples = tuple(
            (b.target, b.mode)
            for b in handle.profile.footprint.barriers
            if b.mode == "exclusive" or handle.profile.descriptor.execution_surface != "managed"
        )
        if not barrier_tuples:
            return
        ws_str = ""
        if handle.workspace_root is not None:
            try:
                ws_str = str(handle.workspace_root.expanduser().resolve())
            except OSError:
                ws_str = str(handle.workspace_root)
        hold_key = action_id or f"{handle.session_id}:{handle.lease_id}"
        hold_id = f"hold-{hashlib.sha256(hold_key.encode('utf-8')).hexdigest()[:16]}"
        try:
            self._containment_store.persist_barrier_hold(
                process_supervision.PersistedBarrierHold(
                    hold_id=hold_id,
                    session_id=handle.session_id,
                    task_id=handle.task_id,
                    epoch=handle.epoch,
                    worker_id=handle.worker_id,
                    action_id=action_id,
                    tool_name=handle.tool_name,
                    footprint_scope=handle.profile.footprint.scope,
                    workspace_root=ws_str,
                    barriers=barrier_tuples,
                    reason=reason,
                )
            )
        except Exception:
            pass

    def admit_profile(
        self,
        request: execution_spine.ActionRequest,
        *,
        effective_policy: str,
        action_class: execution_spine.ActionClass,
    ) -> execution_spine.ProfileAdmissionOutcome:
        mode = self._current_execution_mode()
        profile = derive_execution_profile(
            request.tool_name,
            request.args,
            workspace_root=self._workspace_root,
            session_id=request.session_id,
            execution_mode=mode,
            container_qualified=self._container_qualified,
        )
        violation = evaluate_unified_policy(
            profile.descriptor,
            request.tool_name,
            request.args,
            effective_policy=effective_policy,
            workspace_root=self._workspace_root,
        )
        if violation is not None:
            return execution_spine.ProfileAdmissionOutcome(
                allowed=False,
                reason=violation.message,
                error_code=violation.error_code,
                footprint_scope=profile.footprint.scope,
                profile=profile.to_mapping(),
            )

        barrier_tuples = tuple((b.target, b.mode) for b in profile.footprint.barriers)
        if self._containment_store is not None:
            active_sup_id = process_supervision.current_supervision_id()
            use_sql_fencing = bool(self._sql_task_fencing and request.task_id is not None)
            wid_for_fence = (
                request.worker_id
                if (not use_sql_fencing or str(request.worker_id or "").startswith("wrk-"))
                else None
            )
            is_fenced, fenced_reason = self._containment_store.is_origin_fenced(
                worker_id=(wid_for_fence if not use_sql_fencing else None),
                task_id=(None if use_sql_fencing else request.task_id),
                epoch=(None if use_sql_fencing else request.epoch),
                session_id=request.session_id,
                supervision_id=active_sup_id,
                current_boot_id=self._current_boot_id,
            )
            if not is_fenced and use_sql_fencing and self._current_boot_id and request.worker_id:
                is_fenced, fenced_reason = self._containment_store.is_origin_fenced(
                    worker_id=(
                        request.worker_id if str(request.worker_id).startswith("wrk-") else None
                    ),
                    current_boot_id=self._current_boot_id,
                )
            if is_fenced:
                return execution_spine.ProfileAdmissionOutcome(
                    allowed=False,
                    reason=(
                        f"Stale or contained execution origin (`{fenced_reason}`) "
                        f"cannot admit `{request.tool_name}`."
                    ),
                    error_code="stale_origin_fenced",
                    footprint_scope=profile.footprint.scope,
                    profile=profile.to_mapping(),
                )

            candidate_action_id = str(request.action_id or "")
            if not candidate_action_id and (
                request.task_id is not None
                or request.step_id is not None
                or request.call_index is not None
            ):
                candidate_action_id = side_effect_ledger.derive_action_id(
                    str(request.task_id or request.session_id or "default"),
                    request.tool_name,
                    request.args,
                    step_id=request.step_id,
                    call_index=request.call_index,
                )
            allow_ack_rollback = (
                profile.descriptor.tool_name == "rollback_edits"
                and isinstance(request.args, Mapping)
                and not bool(request.args.get("dry_run", False))
                and bool(request.args.get("acknowledge_unresolved", False))
            )
            holds, conflict_targets_list = self._containment_store.find_conflicting_holds(
                barrier_tuples,
                action_class=action_class,
                candidate_action_id=candidate_action_id,
                allow_acknowledged_rollback=allow_ack_rollback,
            )
            if holds:
                conflict_targets = tuple(conflict_targets_list)
                live_pids = tuple(
                    dict.fromkeys(pid for h in holds for pid, _fp in h.lingering_pids)
                )
                hold_labels = tuple(
                    dict.fromkeys(h.action_id or h.hold_id for h in holds)
                )
                if live_pids:
                    err_code = "lingering_process_containment"
                    reason_msg = (
                        f"Conflicting action `{request.tool_name}` blocked by active containment barrier "
                        f"({', '.join(conflict_targets)}): lingering child process(es) "
                        f"{list(live_pids)} have not exited yet."
                    )
                else:
                    err_code = "unresolved_containment_barrier"
                    reason_msg = (
                        f"Conflicting action `{request.tool_name}` blocked by persisted containment barrier "
                        f"({', '.join(conflict_targets)}): unresolved side effects "
                        f"({', '.join(hold_labels[:3])}) must be reconciled before conflicting execution."
                    )
                return execution_spine.ProfileAdmissionOutcome(
                    allowed=False,
                    reason=reason_msg,
                    error_code=err_code,
                    footprint_scope=profile.footprint.scope,
                    conflict_denied=True,
                    conflicting_barriers=conflict_targets,
                    profile=profile.to_mapping(),
                )

        decision = self._controller.admit_footprint(profile.footprint)
        if not decision.allowed or decision.lease_id is None:
            barriers_txt = ", ".join(decision.conflicting_barriers)
            exhausted_txt = ", ".join(decision.exhausted_resources)
            details = barriers_txt or exhausted_txt or "resource contention"
            return execution_spine.ProfileAdmissionOutcome(
                allowed=False,
                reason=(
                    f"Conflicting action admission prevented for `{request.tool_name}` "
                    f"(footprint `{profile.footprint.scope}` blocked by: {details})."
                ),
                error_code="conflicting_action",
                footprint_scope=profile.footprint.scope,
                conflict_denied=True,
                conflicting_barriers=decision.conflicting_barriers,
                exhausted_resources=decision.exhausted_resources,
                profile=profile.to_mapping(),
            )

        # Capture pre-execution workspace state under the acquired barrier so no
        # concurrent writer can race with our baseline snapshot.
        should_observe = self._workspace_root is not None and (
            execution_spine.is_consequential_action_class(action_class)
            or profile.footprint.scope in {"exact_paths", "bounded_workspace_read"}
        )
        try:
            pre_state = (
                capture_footprint_state(self._workspace_root, ignore_paths=self._ignore_paths)
                if should_observe
                else None
            )
        except Exception:
            self._controller.release(decision.lease_id)
            raise

        handle = _EPActiveHandle(
            lease_id=decision.lease_id,
            session_id=request.session_id,
            tool_name=request.tool_name,
            profile=profile,
            pre_state=pre_state,
            workspace_root=self._workspace_root,
            store_dir=self._store_dir,
            task_id=request.task_id,
            epoch=request.epoch,
            worker_id=request.worker_id,
            on_reconciled=self._on_reconciled,
        )
        return execution_spine.ProfileAdmissionOutcome(
            allowed=True,
            lease_id=decision.lease_id,
            footprint_scope=profile.footprint.scope,
            barriers=barrier_tuples,
            profile=profile.to_mapping(),
            handle=handle,
        )

    def release_only(self, handle: Any) -> None:
        if isinstance(handle, _EPActiveHandle) and not handle.released:
            handle.released = True
            self._controller.release(handle.lease_id)

    def abort_on_interrupt(
        self,
        handle: Any,
        *,
        action_id: str = "",
        reason: str = "interrupted_in_flight",
    ) -> None:
        if not isinstance(handle, _EPActiveHandle):
            return
        try:
            self._persist_barrier_hold(handle, action_id=action_id, reason=reason)
        finally:
            self.release_only(handle)

    def reconcile_and_release(
        self,
        handle: Any,
        result: dict[str, Any] | None,
        *,
        action_id: str = "",
        exc: BaseException | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(handle, _EPActiveHandle):
            return result
        reconciliation: FootprintReconciliation | None = None
        updated: dict[str, Any] | None = None
        try:
            reconciliation = observe_and_reconcile(handle.profile, handle.pre_state)
            if handle.store_dir is not None and (
                reconciliation.expanded or reconciliation.status != "clean"
            ):
                try:
                    FootprintReconciliationLedger(handle.store_dir).append(
                        handle.session_id,
                        tool_name=handle.tool_name,
                        action_id=action_id,
                        profile=handle.profile,
                        reconciliation=reconciliation,
                    )
                except OSError:
                    pass
            if handle.on_reconciled is not None and (
                reconciliation.expanded or reconciliation.status != "clean"
            ):
                try:
                    handle.on_reconciled(handle.session_id, handle.profile, reconciliation)
                except Exception:
                    pass

            if result is not None:
                updated = {
                    **result,
                    "execution_profile": handle.profile.to_mapping(),
                    "footprint_reconciliation": reconciliation.to_mapping(),
                }
                if reconciliation.status == "policy_violation":
                    reasons = ", ".join(
                        f"{v.get('path')}:{v.get('reason')}" for v in reconciliation.policy_violations
                    )
                    prior_out = str(updated.get("output") or "")
                    updated["ok"] = False
                    updated["policy_violation"] = True
                    updated["error_code"] = "footprint_policy_violation"
                    if reconciliation.uncertain_required:
                        updated["uncertain"] = True
                    updated["output"] = (
                        f"Denied post-execution: `{handle.tool_name}` violated unified workspace policy ({reasons})."
                        + (f"\n{prior_out}" if prior_out else "")
                    )
                elif reconciliation.status == "scope_violation":
                    bad_paths = ", ".join(reconciliation.scope_violations[:5])
                    prior_out = str(updated.get("output") or "")
                    updated["ok"] = False
                    updated["scope_violation"] = True
                    updated["error_code"] = "undeclared_footprint_expansion"
                    updated["uncertain"] = True
                    updated["output"] = (
                        f"Denied post-execution: `{handle.tool_name}` mutated undeclared workspace path(s) ({bad_paths})."
                        + (f"\n{prior_out}" if prior_out else "")
                    )

            exc_uncertain = exc is not None and side_effect_ledger.is_ambiguous_exception(exc)
            res_uncertain = updated is not None and bool(updated.get("uncertain"))
            if exc_uncertain or res_uncertain:
                hold_reason = (
                    str(updated.get("error_code") or "uncertain_outcome")
                    if updated is not None
                    else type(exc).__name__
                )
                self._persist_barrier_hold(handle, action_id=action_id, reason=hold_reason)
            elif (
                self._containment_store is not None
                and action_id
                and updated is not None
                and bool(updated.get("ok"))
            ):
                try:
                    self._containment_store.release_holds_for_action(action_id)
                except Exception:
                    pass
        finally:
            self.release_only(handle)

        return updated
