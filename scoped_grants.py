"""Auditable operation/resource/task scoped grants, expiry, revocation, and subprocess env hygiene (M5-P3-04).

This stdlib-only module provides:
1. Canonical capability & resource scope mapping across all Studio execution
   surfaces (`resolve_tool_capabilities`, `resolve_tool_resource_scopes`) so
   grant coverage checks require subsumption while revocation checks block any
   overlapping tool/surface (`AC 3`: revocation cannot be bypassed through
   another tool).
2. Durable, atomic `ScopedGrantStore` (`$HARNESS_STORE_DIR/grants/state.json`,
   `0700` dir / `0600` file, `O_NOFOLLOW`, `fsync`) persisting grant scopes,
   monotonic session & global policy revisions, mandatory finite `expires_at`
   timestamps with **zero automatic renewal** (`AC 1` & `AC 2`), bounded use
   counts, active revocation rules, and an auditable event trail.
3. Subprocess environment sanitization (`sanitize_subprocess_env`,
   `is_secret_env_key`) keeping API keys, bearer tokens, keyring secrets, and
   dynamic-linker/startup injection variables out of arbitrary subprocesses.

This module is inlined into standalone `studio.py` by `scripts/build_studio.py`
(`INLINED_MODULES`); keep all imports strictly to the Python standard library
and all private module-level helpers prefixed with `_sg_` / `_SG_`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import stat
import tempfile
import threading
import time
from typing import Any, Literal
import uuid

SG_SCHEMA_VERSION = 1
SG_DEFAULT_GRANT_TTL_SECONDS = 600.0
SG_MAX_GRANT_TTL_SECONDS = 86_400.0
SG_MAX_AUDIT_EVENTS = 500

GrantStatus = Literal["active", "consumed", "revoked", "expired"]

SG_CAPABILITY_TAXONOMY: frozenset[str] = frozenset(
    {
        "workspace_read",
        "workspace_write",
        "code_execution",
        "context_write",
        "subagent_delegation",
        "council_mutation",
        "external_mcp",
    }
)

_SG_READ_BLOCKS: frozenset[str] = frozenset({"json-peek", "csv-peek", "shortcut-debt"})
_SG_EXEC_BLOCKS: frozenset[str] = frozenset({"test-summary", "first-error"})

_SG_WORKSPACE_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "list_files",
        "grep_search",
        "read_file",
        "codegraph_architecture",
        "codegraph_context",
        "codegraph_impact",
        "codegraph_path",
        "codegraph_quality",
        "oc_lint",
        "ctx_search",
        "oc_search",
        "oc_read",
        "list_edit_batches",
        "reconcile_task_actions",
        "mcp_bridge__get_current_session_data",
        "mcp_bridge__get_harness_diff",
        "mcp_bridge__oc_search",
        "mcp_bridge__oc_lint",
        "mcp_bridge__codegraph_query",
    }
)

_SG_MANAGED_WRITE_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "rollback_edits"}
)

_SG_CODE_EXEC_TOOLS: frozenset[str] = frozenset(
    {"run_bash", "ctx_execute_python", "skill_execute_script"}
)

_SG_CONTEXT_WRITE_TOOLS: frozenset[str] = frozenset(
    {"oc_save_doc", "mcp_bridge__oc_save_doc"}
)

_SG_COUNCIL_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "mcp_bridge__start_council",
        "mcp_bridge__send_response",
        "mcp_bridge__summon_omlx_agent",
        "mcp_bridge__close_council",
        "mcp_bridge__create_harness_worktree",
    }
)

SG_SAFE_BASE_ENV_KEYS: frozenset[str] = frozenset(
    {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TERM"}
)

SG_FORBIDDEN_INJECTION_ENV_KEYS: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "LD_DEBUG_OUTPUT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "IFS",
        "PROMPT_COMMAND",
        "NODE_OPTIONS",
        "RUBYOPT",
        "PERL5OPT",
        "PERL5LIB",
        "JAVA_TOOL_OPTIONS",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "STUDIO_API_TOKEN",
    }
)

_SG_SECRET_ENV_KEY_PREFIXES: tuple[str, ...] = (
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_API_",
    "GROQ_",
    "MISTRAL_",
    "COHERE_",
    "XAI_",
    "DEEPSEEK_",
    "OPENROUTER_",
    "TOGETHER_",
    "FIREWORKS_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "HUGGING_FACE_",
    "HF_",
    "GITHUB_",
    "GH_",
    "GITLAB_",
    "NPM_",
    "PYPI_",
)

_SG_SECRET_ENV_KEY_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|"
    r"PRIVATE_?KEY|ACCESS_?KEY|SESSION_?KEY|CLIENT_?SECRET|AUTH|BEARER|COOKIE|WEBHOOK)(?:$|_)",
    re.IGNORECASE,
)


class ScopedGrantError(RuntimeError):
    """Base error for scoped grant persistence and validation failures."""


class GrantStoreIntegrityError(ScopedGrantError):
    """Raised when the persisted grant store is corrupted, tampered, or symlinked."""


class InvalidGrantSpecError(ValueError):
    """Raised when grant issuance or revocation parameters violate safety invariants."""


def _sg_clean_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if value is None:
        if allow_empty:
            return ""
        raise InvalidGrantSpecError(f"{field} must be a non-empty string.")
    text = str(value).strip()
    if "\x00" in text:
        raise InvalidGrantSpecError(f"{field} must not contain NUL bytes.")
    if not text and not allow_empty:
        raise InvalidGrantSpecError(f"{field} must be a non-empty string.")
    return text


def _sg_canonical_args_sha256(args: Mapping[str, Any] | None) -> str:
    payload = dict(args) if isinstance(args, Mapping) else {}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _sg_norm_rel_path(raw: str) -> str:
    cleaned = raw.replace("\\", "/").strip()
    if not cleaned:
        return ""
    norm = posixpath.normpath(cleaned)
    if norm in {".", "/"}:
        return ""
    return norm.lstrip("/")


def _sg_canonical_ws_rel_path(ws_str: str, raw: str) -> str:
    """Resolve `raw` inside `ws_str` (collapsing `..`, `.`, `//`, and internal symlinks)."""
    if not ws_str:
        return _sg_norm_rel_path(raw)
    cleaned = str(raw or "").replace("\\", "/").strip()
    if not cleaned:
        return ""
    try:
        ws_path = Path(ws_str).resolve()
        cand = Path(cleaned) if cleaned.startswith("/") else (ws_path / cleaned)
        resolved = cand.resolve()
        rel = resolved.relative_to(ws_path).as_posix()
        return "" if rel == "." else rel
    except (OSError, ValueError):
        return ""


def _sg_normalize_workspace_str(workspace_root: Path | str | None) -> str:
    if workspace_root is None:
        return ""
    raw = str(workspace_root).strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:
        return raw.rstrip("/")


def normalize_resource_scope(
    raw_scope: str,
    *,
    workspace_root: Path | str | None = None,
) -> str:
    """Normalize a resource scope string into canonical `kind:...` form."""
    text = _sg_clean_text(raw_scope, "resource_scope")
    ws_str = _sg_normalize_workspace_str(workspace_root)
    if text == "*" or text == "global:*":
        return "global:*"
    if text.startswith("workspace:"):
        rest = text.removeprefix("workspace:").strip()
        if rest in {"", ".", "*"} and ws_str:
            return f"workspace:{ws_str}"
        return f"workspace:{_sg_normalize_workspace_str(rest) or rest}"
    if text.startswith("path:"):
        rest = text.removeprefix("path:").strip()
        if ws_str and not rest.startswith("/"):
            rel = _sg_canonical_ws_rel_path(ws_str, rest) or _sg_norm_rel_path(rest)
            return f"path:{ws_str}:{rel}" if rel else f"workspace:{ws_str}"
        if ":" in rest and rest.startswith("/"):
            prefix, rel = rest.rsplit(":", 1)
            if prefix.startswith("/") and not rel.startswith("/"):
                norm_ws = _sg_normalize_workspace_str(prefix)
                rel_canon = _sg_canonical_ws_rel_path(norm_ws, rel) or _sg_norm_rel_path(rel)
                return f"path:{norm_ws}:{rel_canon}" if rel_canon else f"workspace:{norm_ws}"
        if ws_str and rest.startswith(ws_str + "/"):
            rel = _sg_canonical_ws_rel_path(ws_str, rest) or _sg_norm_rel_path(
                rest[len(ws_str) + 1 :]
            )
            return f"path:{ws_str}:{rel}" if rel else f"workspace:{ws_str}"
        return f"path:{rest}"
    if text.startswith("dir:"):
        rest = text.removeprefix("dir:").strip()
        if ws_str and not rest.startswith("/"):
            rel = _sg_canonical_ws_rel_path(ws_str, rest) or _sg_norm_rel_path(rest)
            return f"dir:{ws_str}:{rel}" if rel else f"workspace:{ws_str}"
        if ":" in rest and rest.startswith("/"):
            prefix, rel = rest.rsplit(":", 1)
            norm_ws = _sg_normalize_workspace_str(prefix)
            rel_norm = _sg_canonical_ws_rel_path(norm_ws, rel) or _sg_norm_rel_path(rel)
            return f"dir:{norm_ws}:{rel_norm}" if rel_norm else f"workspace:{norm_ws}"
        return f"dir:{rest.rstrip('/')}"
    if text.startswith("mcp:"):
        parts = [p.strip() for p in text.split(":") if p.strip()]
        if len(parts) == 2:
            return f"mcp:{parts[1].lower()}"
        if len(parts) >= 3:
            return f"mcp:{parts[1].lower()}:{parts[2]}"
        raise InvalidGrantSpecError(f"Malformed MCP resource scope: {raw_scope!r}")
    if text.startswith("context:"):
        folder = text.removeprefix("context:").strip() or "*"
        return f"context:{folder}"
    # Bare relative file path when workspace_root is known:
    if ws_str and not text.startswith("/"):
        rel = _sg_norm_rel_path(text)
        return f"path:{ws_str}:{rel}" if rel else f"workspace:{ws_str}"
    raise InvalidGrantSpecError(
        f"Unsupported resource_scope {raw_scope!r}; expected workspace:..., path:..., dir:..., mcp:..., context:..., or global:*."
    )


def resolve_tool_capabilities(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Deterministically map a tool invocation to the canonical capabilities it exercises."""
    clean = str(tool_name or "").strip()
    safe_args = args if isinstance(args, Mapping) else {}
    if not clean:
        return ("code_execution", "workspace_read", "workspace_write", "external_mcp")
    if clean == "ctx_run_block":
        block = str(safe_args.get("block") or "").strip().lower()
        if block in _SG_READ_BLOCKS:
            return ("workspace_read",)
        if block in _SG_EXEC_BLOCKS:
            return ("code_execution", "workspace_read", "workspace_write")
        return ("code_execution", "workspace_read", "workspace_write")
    if clean in _SG_CODE_EXEC_TOOLS:
        return ("code_execution", "workspace_read", "workspace_write")
    if clean in _SG_MANAGED_WRITE_TOOLS:
        return ("workspace_write", "workspace_read")
    if clean in _SG_WORKSPACE_READ_ONLY_TOOLS:
        return ("workspace_read",)
    if clean in _SG_CONTEXT_WRITE_TOOLS:
        return ("context_write",)
    if clean == "delegate_subagent":
        return ("subagent_delegation",)
    if clean in _SG_COUNCIL_MUTATION_TOOLS:
        return ("council_mutation",)
    if clean.startswith("mcp__"):
        return ("external_mcp",)
    return ("code_execution", "workspace_read", "workspace_write", "external_mcp")


def resolve_tool_resource_scopes(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    workspace_root: Path | str | None = None,
    candidate_rel_paths: Sequence[str] = (),
    barriers: Sequence[tuple[str, str]] = (),
) -> tuple[str, ...]:
    """Derive canonical resource scopes accessed or reserved by a tool invocation."""
    clean = str(tool_name or "").strip()
    safe_args = args if isinstance(args, Mapping) else {}
    ws_str = _sg_normalize_workspace_str(workspace_root)
    scopes: list[str] = []

    def _add(item: str) -> None:
        if item and item not in scopes:
            scopes.append(item)

    if clean.startswith("mcp__"):
        parts = clean.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            alias = parts[1].strip().lower()
            rtool = parts[2].strip()
            _add(f"mcp:{alias}:{rtool}")
        else:
            _add("mcp:*")
        return tuple(scopes)

    if clean in _SG_CONTEXT_WRITE_TOOLS:
        folder = str(safe_args.get("folder") or "decisions").strip() or "decisions"
        _add(f"context:{folder}")
        return tuple(scopes)

    if clean in {"write_file", "edit_file", "read_file"} and ws_str:
        raw_p = str(safe_args.get("path") or safe_args.get("file_path") or "").strip()
        if raw_p:
            rel = _sg_canonical_ws_rel_path(ws_str, raw_p)
            if rel:
                _add(f"path:{ws_str}:{rel}")
        if not scopes:
            _add(f"workspace:{ws_str}")
        return tuple(scopes)

    if clean == "rollback_edits" and ws_str:
        raw_paths = safe_args.get("paths")
        if isinstance(raw_paths, Sequence) and not isinstance(raw_paths, (str, bytes)):
            for rp in raw_paths:
                rel = _sg_canonical_ws_rel_path(ws_str, str(rp or ""))
                if rel:
                    _add(f"path:{ws_str}:{rel}")
        if not scopes:
            _add(f"workspace:{ws_str}")
        return tuple(scopes)

    # Subprocess, grep/list/codegraph, and conservative barriers:
    for target, _mode in barriers:
        if target.startswith("path:") or target.startswith("workspace:"):
            _add(normalize_resource_scope(target, workspace_root=ws_str))
    for rel_cand in candidate_rel_paths:
        rel_norm = _sg_canonical_ws_rel_path(ws_str, rel_cand)
        if ws_str and rel_norm:
            _add(f"path:{ws_str}:{rel_norm}")
    if clean == "ctx_run_block" and ws_str:
        raw_bp = str(safe_args.get("path") or "").strip()
        if raw_bp:
            rel_bp = _sg_canonical_ws_rel_path(ws_str, raw_bp)
            if rel_bp:
                _add(f"path:{ws_str}:{rel_bp}")
    if ws_str and not any(s.startswith("workspace:") for s in scopes):
        if clean in _SG_CODE_EXEC_TOOLS or clean in {"grep_search", "list_files"} or not scopes:
            _add(f"workspace:{ws_str}")
    if not scopes:
        _add("global:*")
    return tuple(scopes)


def _sg_parse_ws_scope(scope: str) -> tuple[str, str, str] | None:
    """Return `(kind, ws, rel)` for `workspace:{ws}`, `dir:{ws}:{rel}`, or `path:{ws}:{rel}`."""
    if scope.startswith("workspace:"):
        return ("workspace", scope.removeprefix("workspace:"), "")
    if scope.startswith("dir:"):
        rest = scope.removeprefix("dir:")
        if ":" in rest:
            ws, rel = rest.rsplit(":", 1)
            return ("dir", ws, _sg_norm_rel_path(rel))
    if scope.startswith("path:"):
        rest = scope.removeprefix("path:")
        if ":" in rest:
            ws, rel = rest.rsplit(":", 1)
            return ("path", ws, _sg_norm_rel_path(rel))
    return None


def scope_subsumes(grant_scope: str, action_scope: str) -> bool:
    """Return True iff `grant_scope` is at least as broad as `action_scope`."""
    if grant_scope == "global:*" or grant_scope == action_scope:
        return True
    if grant_scope.startswith("mcp:") and action_scope.startswith("mcp:"):
        g_parts = grant_scope.split(":")
        a_parts = action_scope.split(":")
        if len(g_parts) == 2 and g_parts[1] == "*":
            return True
        if len(g_parts) == 2 and len(a_parts) >= 2 and g_parts[1] == a_parts[1]:
            return True
        if (
            len(g_parts) == 3
            and len(a_parts) == 3
            and g_parts[1] == a_parts[1]
            and (g_parts[2] == "*" or g_parts[2] == a_parts[2])
        ):
            return True
        return False
    if grant_scope.startswith("context:") and action_scope.startswith("context:"):
        g_folder = grant_scope.removeprefix("context:")
        return g_folder == "*" or grant_scope == action_scope

    g_ws = _sg_parse_ws_scope(grant_scope)
    a_ws = _sg_parse_ws_scope(action_scope)
    if g_ws is not None and a_ws is not None:
        g_kind, g_root, g_rel = g_ws
        a_kind, a_root, a_rel = a_ws
        if g_root != a_root:
            return False
        if g_kind == "workspace":
            return True
        if g_kind == "dir":
            if a_kind == "workspace":
                return False
            return a_rel == g_rel or a_rel.startswith(g_rel + "/")
        if g_kind == "path":
            return a_kind == "path" and a_rel == g_rel
    return False


def scopes_overlap(scope_a: str, scope_b: str) -> bool:
    """Symmetric non-empty intersection check used by revocation enforcement (`AC 3`)."""
    return scope_subsumes(scope_a, scope_b) or scope_subsumes(scope_b, scope_a)


def is_secret_env_key(key: str) -> bool:
    """Return True if an environment variable name is secret-bearing or injection-sensitive."""
    upper = str(key or "").strip().upper()
    if not upper:
        return False
    if upper in {"M5_SUPERVISION_ID", "PATH", "HOME"} | SG_SAFE_BASE_ENV_KEYS:
        return False
    if upper in SG_FORBIDDEN_INJECTION_ENV_KEYS or upper.startswith(
        ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
    ):
        return True
    if any(upper.startswith(prefix) for prefix in _SG_SECRET_ENV_KEY_PREFIXES):
        return True
    return bool(_SG_SECRET_ENV_KEY_RE.search(upper))


def sanitize_subprocess_env(
    host_env: Mapping[str, str],
    *,
    interpreter_dir: str,
    extra: Mapping[str, Any] | None = None,
    allow_scoped_mcp_env: bool = False,
    known_secret_values: Iterable[str] = (),
    extra_allowed_host_keys: Collection[str] = (),
    include_git_non_interactive: bool = False,
) -> dict[str, str]:
    """Construct a minimal, secret-scrubbed environment for child subprocesses.

    - Arbitrary subprocesses (`allow_scoped_mcp_env=False`: `run_bash`,
      `ctx_execute_python`, `ctx_run_block`, `skill_execute_script`, `git`,
      `docker`) NEVER inherit secret-named keys or values matching registered
      Studio/provider/MCP credentials, and NEVER inherit loader/startup
      injection variables (`LD_PRELOAD`, `DYLD_*`, `PYTHONPATH`, `BASH_ENV`, etc.).
    - Scoped stdio MCP servers (`allow_scoped_mcp_env=True`) receive ONLY their
      explicitly configured `extra` mapping plus safe base keys, while still
      blocking `SG_FORBIDDEN_INJECTION_ENV_KEYS`, `STUDIO_API_TOKEN`, and any
      unrelated Studio/provider secrets passed in `known_secret_values`.
    """
    secrets_to_scrub = tuple(
        sorted(
            {str(s) for s in known_secret_values if isinstance(s, str) and len(s) >= 6},
            key=len,
            reverse=True,
        )
    )

    def _contains_known_secret(val: str) -> bool:
        return any(sec in val for sec in secrets_to_scrub)

    allowed_host = SG_SAFE_BASE_ENV_KEYS | {str(k).upper() for k in extra_allowed_host_keys}
    env: dict[str, str] = {}
    for raw_k, raw_v in host_env.items():
        k_str = str(raw_k)
        k_up = k_str.upper()
        if "\x00" in k_str or k_up in SG_FORBIDDEN_INJECTION_ENV_KEYS:
            continue
        if k_up not in allowed_host:
            continue
        if k_up not in extra_allowed_host_keys and is_secret_env_key(k_up):
            continue
        v_str = str(raw_v).replace("\x00", "")
        if _contains_known_secret(v_str):
            continue
        env[k_str] = v_str

    clean_interp = str(interpreter_dir or "").replace("\x00", "").strip()
    path_parts = [p for p in (clean_interp, os.defpath) if p and not _contains_known_secret(p)]
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts)) or os.defpath

    if include_git_non_interactive:
        env["GIT_TERMINAL_PROMPT"] = "0"

    if extra:
        for raw_ek, raw_ev in extra.items():
            ek = str(raw_ek).strip()
            if not ek or "\x00" in ek:
                continue
            ek_up = ek.upper()
            if (
                ek_up in SG_FORBIDDEN_INJECTION_ENV_KEYS
                or ek_up.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
                or ek_up == "PATH"
            ):
                continue
            ev = str(raw_ev).replace("\x00", "")
            if _contains_known_secret(ev):
                continue
            if not allow_scoped_mcp_env and is_secret_env_key(ek_up):
                continue
            env[ek] = ev

    return env


@dataclass(frozen=True, slots=True)
class PolicyRevisionRecord:
    session_id: str
    revision: int
    policy: str
    updated_at: float
    reason: str = "initial"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "revision": self.revision,
            "policy": self.policy,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PolicyRevisionRecord:
        rev = int(raw["revision"])
        if rev < 1:
            raise GrantStoreIntegrityError("PolicyRevisionRecord.revision must be >= 1.")
        return cls(
            session_id=_sg_clean_text(raw["session_id"], "session_id"),
            revision=rev,
            policy=_sg_clean_text(raw.get("policy") or "standard", "policy"),
            updated_at=float(raw["updated_at"]),
            reason=str(raw.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class ScopedGrantRecord:
    """Immutable, finite-lifetime scoped permission grant (`AC 1`, `AC 2`)."""

    grant_id: str
    session_id: str
    task_id: str | None
    epoch: int | None
    capabilities: tuple[str, ...]
    tool_names: tuple[str, ...]
    resource_scopes: tuple[str, ...]
    args_sha256: str
    session_policy_revision: int
    global_policy_revision: int
    issued_at: float
    expires_at: float
    max_uses: int | None
    uses_consumed: int
    status: GrantStatus
    issued_by: str = "operator"
    note: str = ""
    revoked_at: float | None = None
    revoked_reason: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "epoch": self.epoch,
            "capabilities": list(self.capabilities),
            "tool_names": list(self.tool_names),
            "resource_scopes": list(self.resource_scopes),
            "args_sha256": self.args_sha256,
            "session_policy_revision": self.session_policy_revision,
            "global_policy_revision": self.global_policy_revision,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "uses_consumed": self.uses_consumed,
            "status": self.status,
            "issued_by": self.issued_by,
            "note": self.note,
            "revoked_at": self.revoked_at,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ScopedGrantRecord:
        status = str(raw["status"])
        if status not in {"active", "consumed", "revoked", "expired"}:
            raise GrantStoreIntegrityError(f"Invalid grant status in store: {status!r}")
        issued_at = float(raw["issued_at"])
        expires_at = float(raw["expires_at"])
        if not (math.isfinite(issued_at) and math.isfinite(expires_at)) or expires_at <= issued_at:
            raise GrantStoreIntegrityError("Grant timestamps in store are non-finite or non-positive.")
        caps = tuple(str(c) for c in (raw.get("capabilities") or ()))
        if not caps or any(c not in SG_CAPABILITY_TAXONOMY for c in caps):
            raise GrantStoreIntegrityError(f"Invalid grant capabilities in store: {caps!r}")
        max_uses_raw = raw.get("max_uses")
        max_uses = int(max_uses_raw) if max_uses_raw is not None else None
        if max_uses is not None and max_uses < 1:
            raise GrantStoreIntegrityError("Grant max_uses in store must be >= 1.")
        epoch_raw = raw.get("epoch")
        return cls(
            grant_id=_sg_clean_text(raw["grant_id"], "grant_id"),
            session_id=_sg_clean_text(raw["session_id"], "session_id"),
            task_id=(str(raw["task_id"]).strip() if raw.get("task_id") else None),
            epoch=(int(epoch_raw) if epoch_raw is not None else None),
            capabilities=caps,
            tool_names=tuple(str(t) for t in (raw.get("tool_names") or ()) if str(t).strip()),
            resource_scopes=tuple(
                str(s) for s in (raw.get("resource_scopes") or ()) if str(s).strip()
            ),
            args_sha256=str(raw.get("args_sha256") or ""),
            session_policy_revision=int(raw["session_policy_revision"]),
            global_policy_revision=int(raw["global_policy_revision"]),
            issued_at=issued_at,
            expires_at=expires_at,
            max_uses=max_uses,
            uses_consumed=int(raw.get("uses_consumed", 0)),
            status=status,  # type: ignore[arg-type]
            issued_by=str(raw.get("issued_by") or "operator"),
            note=str(raw.get("note") or ""),
            revoked_at=(float(raw["revoked_at"]) if raw.get("revoked_at") is not None else None),
            revoked_reason=str(raw.get("revoked_reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class RevocationRuleRecord:
    """Active capability/resource/tool revocation rule enforced across all tools (`AC 3`)."""

    revocation_id: str
    session_id: str
    task_id: str | None
    capabilities: tuple[str, ...]
    resource_scopes: tuple[str, ...]
    tool_names: tuple[str, ...]
    source_grant_id: str
    reason: str
    created_at: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "revocation_id": self.revocation_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "capabilities": list(self.capabilities),
            "resource_scopes": list(self.resource_scopes),
            "tool_names": list(self.tool_names),
            "source_grant_id": self.source_grant_id,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RevocationRuleRecord:
        caps = tuple(str(c) for c in (raw.get("capabilities") or ()) if str(c).strip())
        if any(c not in SG_CAPABILITY_TAXONOMY for c in caps):
            raise GrantStoreIntegrityError(f"Invalid revocation capabilities: {caps!r}")
        return cls(
            revocation_id=_sg_clean_text(raw["revocation_id"], "revocation_id"),
            session_id=_sg_clean_text(raw["session_id"], "session_id"),
            task_id=(str(raw["task_id"]).strip() if raw.get("task_id") else None),
            capabilities=caps,
            resource_scopes=tuple(
                str(s) for s in (raw.get("resource_scopes") or ()) if str(s).strip()
            ),
            tool_names=tuple(str(t) for t in (raw.get("tool_names") or ()) if str(t).strip()),
            source_grant_id=str(raw.get("source_grant_id") or ""),
            reason=str(raw.get("reason") or "revoked"),
            created_at=float(raw["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class GrantValidationOutcome:
    allowed: bool
    grant_id: str | None = None
    error_code: str | None = None
    reason: str = ""
    grant: ScopedGrantRecord | None = None


class ScopedGrantStore:
    """Durable, atomic store for scoped grants, policy revisions, and revocations."""

    def __init__(
        self,
        store_root: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(store_root)
        self._dir = self._root / "grants"
        self._state_path = self._dir / "state.json"
        self._clock = clock
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _ensure_dir_secure(self) -> None:
        if self._root.exists() and self._root.is_symlink():
            raise GrantStoreIntegrityError(
                f"Grant store root {self._root} must not be a symlink."
            )
        self._root.mkdir(parents=True, exist_ok=True)
        if self._dir.exists() and self._dir.is_symlink():
            raise GrantStoreIntegrityError(
                f"Grant store directory {self._dir} must not be a symlink."
            )
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": SG_SCHEMA_VERSION,
            "global_policy_revision": 1,
            "session_policy_revisions": {},
            "grants": {},
            "revocations": {},
            "rejected_approvals": {},
            "audit_events": [],
        }

    def _load_locked(self) -> dict[str, Any]:
        if self._dir.exists() and self._dir.is_symlink():
            raise GrantStoreIntegrityError(
                f"Grant store directory {self._dir} is a symlink; refusing access."
            )
        try:
            st = os.lstat(self._state_path)
        except FileNotFoundError:
            return self._empty_state()
        except OSError as exc:
            raise GrantStoreIntegrityError(f"Cannot stat grant store: {exc}") from exc

        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise GrantStoreIntegrityError(
                f"Grant state file {self._state_path} must be a regular non-symlink file."
            )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._state_path, flags)
            try:
                raw_bytes = os.read(fd, 16 * 1024 * 1024)
            finally:
                os.close(fd)
        except OSError as exc:
            raise GrantStoreIntegrityError(f"Cannot read grant state file: {exc}") from exc

        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GrantStoreIntegrityError(f"Corrupted grant store JSON: {exc}") from exc

        if not isinstance(parsed, dict) or parsed.get("schema_version") != SG_SCHEMA_VERSION:
            raise GrantStoreIntegrityError(
                f"Unsupported grant store schema_version: {parsed.get('schema_version') if isinstance(parsed, dict) else type(parsed).__name__}"
            )
        global_rev = parsed.get("global_policy_revision")
        if not isinstance(global_rev, int) or global_rev < 1:
            raise GrantStoreIntegrityError("Invalid global_policy_revision in grant store.")
        for key in ("session_policy_revisions", "grants", "revocations", "rejected_approvals"):
            if not isinstance(parsed.get(key), dict):
                raise GrantStoreIntegrityError(f"Invalid `{key}` mapping in grant store.")
        if not isinstance(parsed.get("audit_events"), list):
            raise GrantStoreIntegrityError("Invalid `audit_events` list in grant store.")

        try:
            for k, v in parsed["session_policy_revisions"].items():
                if not isinstance(v, Mapping) or v.get("session_id") != k:
                    raise GrantStoreIntegrityError(f"Mismatched session_policy_revisions entry {k!r}.")
                PolicyRevisionRecord.from_mapping(v)
            for k, v in parsed["grants"].items():
                if not isinstance(v, Mapping) or v.get("grant_id") != k:
                    raise GrantStoreIntegrityError(f"Mismatched grant entry {k!r}.")
                ScopedGrantRecord.from_mapping(v)
            for k, v in parsed["revocations"].items():
                if not isinstance(v, Mapping) or v.get("revocation_id") != k:
                    raise GrantStoreIntegrityError(f"Mismatched revocation entry {k!r}.")
                RevocationRuleRecord.from_mapping(v)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, GrantStoreIntegrityError):
                raise
            raise GrantStoreIntegrityError(f"Malformed record in grant store: {exc}") from exc
        return parsed

    def _save_locked(self, state: dict[str, Any]) -> None:
        self._ensure_dir_secure()
        if self._state_path.exists() and self._state_path.is_symlink():
            raise GrantStoreIntegrityError(
                f"Refusing to overwrite symlinked grant store {self._state_path}."
            )
        if len(state["audit_events"]) > SG_MAX_AUDIT_EVENTS:
            state["audit_events"] = state["audit_events"][-SG_MAX_AUDIT_EVENTS:]
        encoded = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".grants-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, encoded)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp_name, self._state_path)
            try:
                dir_fd = os.open(self._dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _append_audit_locked(
        self,
        state: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        now: float,
    ) -> None:
        state["audit_events"].append(
            {
                "event_id": f"gev-{uuid.uuid4().hex[:14]}",
                "type": event_type,
                "ts": now,
                **payload,
            }
        )

    def _expire_due_locked(self, state: dict[str, Any], *, now: float) -> list[ScopedGrantRecord]:
        expired: list[ScopedGrantRecord] = []
        for gid, raw in list(state["grants"].items()):
            rec = ScopedGrantRecord.from_mapping(raw)
            if rec.status == "active" and now >= rec.expires_at:
                updated = ScopedGrantRecord(
                    grant_id=rec.grant_id,
                    session_id=rec.session_id,
                    task_id=rec.task_id,
                    epoch=rec.epoch,
                    capabilities=rec.capabilities,
                    tool_names=rec.tool_names,
                    resource_scopes=rec.resource_scopes,
                    args_sha256=rec.args_sha256,
                    session_policy_revision=rec.session_policy_revision,
                    global_policy_revision=rec.global_policy_revision,
                    issued_at=rec.issued_at,
                    expires_at=rec.expires_at,
                    max_uses=rec.max_uses,
                    uses_consumed=rec.uses_consumed,
                    status="expired",
                    issued_by=rec.issued_by,
                    note=rec.note,
                    revoked_at=rec.revoked_at,
                    revoked_reason="ttl_expired",
                )
                state["grants"][gid] = updated.to_mapping()
                expired.append(updated)
                self._append_audit_locked(
                    state,
                    "grant_expired",
                    {
                        "grant_id": gid,
                        "session_id": rec.session_id,
                        "expires_at": rec.expires_at,
                    },
                    now=now,
                )
        return expired

    def sweep_expired_grants(self) -> tuple[ScopedGrantRecord, ...]:
        """Mark any past-`expires_at` active grants as `expired` on disk (`init_db` / maintenance)."""
        with self._lock:
            state = self._load_locked()
            if not state["grants"]:
                return ()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if expired:
                self._save_locked(state)
            return tuple(expired)

    def get_revisions(
        self,
        session_id: str,
        *,
        current_policy: str = "standard",
    ) -> tuple[int, int]:
        """Return `(session_policy_revision, global_policy_revision)` without mutating disk if default."""
        sid = _sg_clean_text(session_id, "session_id")
        with self._lock:
            state = self._load_locked()
            raw = state["session_policy_revisions"].get(sid)
            if raw is None:
                return (1, int(state["global_policy_revision"]))
            rec = PolicyRevisionRecord.from_mapping(raw)
            _ = current_policy
            return (rec.revision, int(state["global_policy_revision"]))

    def ensure_session_policy(
        self,
        session_id: str,
        policy: str,
        *,
        reason: str = "session_initialized",
    ) -> PolicyRevisionRecord:
        sid = _sg_clean_text(session_id, "session_id")
        pol = _sg_clean_text(policy or "standard", "policy")
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            existing_raw = state["session_policy_revisions"].get(sid)
            if existing_raw is None:
                rec = PolicyRevisionRecord(
                    session_id=sid,
                    revision=1,
                    policy=pol,
                    updated_at=now,
                    reason=reason,
                )
                state["session_policy_revisions"][sid] = rec.to_mapping()
                self._save_locked(state)
                return rec
            existing = PolicyRevisionRecord.from_mapping(existing_raw)
            if existing.policy == pol:
                return existing
            rev_num, _ = self.bump_session_policy_revision(
                sid, new_policy=pol, reason=reason
            )
            return PolicyRevisionRecord(
                session_id=sid,
                revision=rev_num,
                policy=pol,
                updated_at=now,
                reason=reason,
            )

    def bump_session_policy_revision(
        self,
        session_id: str,
        *,
        new_policy: str,
        reason: str = "policy_updated",
    ) -> tuple[int, tuple[ScopedGrantRecord, ...]]:
        """Atomically bump a session's `policy_revision` and revoke all prior-revision active grants."""
        sid = _sg_clean_text(session_id, "session_id")
        pol = _sg_clean_text(new_policy or "standard", "policy")
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            existing_raw = state["session_policy_revisions"].get(sid)
            prev_rev = (
                PolicyRevisionRecord.from_mapping(existing_raw).revision
                if existing_raw is not None
                else 1
            )
            next_rev = prev_rev + 1
            rec = PolicyRevisionRecord(
                session_id=sid,
                revision=next_rev,
                policy=pol,
                updated_at=now,
                reason=reason,
            )
            state["session_policy_revisions"][sid] = rec.to_mapping()
            revoked: list[ScopedGrantRecord] = []
            for gid, g_raw in list(state["grants"].items()):
                g = ScopedGrantRecord.from_mapping(g_raw)
                if g.session_id == sid and g.status == "active":
                    upd = ScopedGrantRecord(
                        grant_id=g.grant_id,
                        session_id=g.session_id,
                        task_id=g.task_id,
                        epoch=g.epoch,
                        capabilities=g.capabilities,
                        tool_names=g.tool_names,
                        resource_scopes=g.resource_scopes,
                        args_sha256=g.args_sha256,
                        session_policy_revision=g.session_policy_revision,
                        global_policy_revision=g.global_policy_revision,
                        issued_at=g.issued_at,
                        expires_at=g.expires_at,
                        max_uses=g.max_uses,
                        uses_consumed=g.uses_consumed,
                        status="revoked",
                        issued_by=g.issued_by,
                        note=g.note,
                        revoked_at=now,
                        revoked_reason=f"policy_revision_changed:{reason}",
                    )
                    state["grants"][gid] = upd.to_mapping()
                    revoked.append(upd)
            self._append_audit_locked(
                state,
                "policy_revision_bumped",
                {
                    "session_id": sid,
                    "new_revision": next_rev,
                    "new_policy": pol,
                    "reason": reason,
                    "revoked_grant_ids": [r.grant_id for r in revoked],
                },
                now=now,
            )
            self._save_locked(state)
            return (next_rev, tuple(revoked))

    def bump_global_policy_revision(
        self,
        *,
        reason: str = "global_policy_updated",
        mcp_alias: str | None = None,
        mcp_server_disabled_or_deleted: bool = False,
        removed_mcp_tools: Collection[str] = (),
    ) -> tuple[int, tuple[ScopedGrantRecord, ...]]:
        """Increment `global_policy_revision` and revoke affected MCP grants/capabilities."""
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            next_global = int(state["global_policy_revision"]) + 1
            state["global_policy_revision"] = next_global

            clean_alias = str(mcp_alias or "").strip().lower()
            removed_set = {str(t).strip() for t in removed_mcp_tools if str(t).strip()}
            revoked: list[ScopedGrantRecord] = []

            for gid, g_raw in list(state["grants"].items()):
                g = ScopedGrantRecord.from_mapping(g_raw)
                if g.status != "active":
                    continue
                should_revoke = False
                if clean_alias:
                    alias_prefix = f"mcp:{clean_alias}"
                    touches_alias = any(
                        sc == "mcp:*" or sc == alias_prefix or sc.startswith(alias_prefix + ":")
                        for sc in g.resource_scopes
                    ) or any(
                        tn.startswith(f"mcp__{clean_alias}__") for tn in g.tool_names
                    )
                    if touches_alias and mcp_server_disabled_or_deleted:
                        should_revoke = True
                    elif touches_alias and removed_set:
                        if any(
                            sc == alias_prefix
                            or sc in {f"mcp:{clean_alias}:{rt}" for rt in removed_set}
                            for sc in g.resource_scopes
                        ):
                            should_revoke = True
                else:
                    should_revoke = True

                if should_revoke:
                    upd = ScopedGrantRecord(
                        grant_id=g.grant_id,
                        session_id=g.session_id,
                        task_id=g.task_id,
                        epoch=g.epoch,
                        capabilities=g.capabilities,
                        tool_names=g.tool_names,
                        resource_scopes=g.resource_scopes,
                        args_sha256=g.args_sha256,
                        session_policy_revision=g.session_policy_revision,
                        global_policy_revision=g.global_policy_revision,
                        issued_at=g.issued_at,
                        expires_at=g.expires_at,
                        max_uses=g.max_uses,
                        uses_consumed=g.uses_consumed,
                        status="revoked",
                        issued_by=g.issued_by,
                        note=g.note,
                        revoked_at=now,
                        revoked_reason=reason,
                    )
                    state["grants"][gid] = upd.to_mapping()
                    revoked.append(upd)

            self._append_audit_locked(
                state,
                "global_policy_revision_bumped",
                {
                    "new_global_revision": next_global,
                    "mcp_alias": clean_alias,
                    "reason": reason,
                    "revoked_grant_ids": [r.grant_id for r in revoked],
                },
                now=now,
            )
            self._save_locked(state)
            return (next_global, tuple(revoked))

    def record_rejected_approval(
        self,
        approval_id: str,
        *,
        session_id: str,
        reason: str = "operator_rejected",
    ) -> None:
        aid = _sg_clean_text(approval_id, "approval_id")
        sid = _sg_clean_text(session_id or "unknown", "session_id")
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            state["rejected_approvals"][aid] = {
                "approval_id": aid,
                "session_id": sid,
                "reason": reason,
                "ts": now,
            }
            self._append_audit_locked(
                state,
                "approval_rejected",
                {"approval_id": aid, "session_id": sid, "reason": reason},
                now=now,
            )
            self._save_locked(state)

    def has_grant_or_tombstone(self, grant_id: str) -> bool:
        gid = str(grant_id or "").strip()
        if not gid:
            return False
        with self._lock:
            state = self._load_locked()
            return gid in state["grants"] or gid in state["rejected_approvals"]

    def get_grant(self, grant_id: str) -> ScopedGrantRecord | None:
        gid = str(grant_id or "").strip()
        if not gid:
            return None
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if expired:
                self._save_locked(state)
            raw = state["grants"].get(gid)
            return ScopedGrantRecord.from_mapping(raw) if raw is not None else None

    def list_grants(
        self,
        *,
        session_id: str | None = None,
        include_inactive: bool = True,
    ) -> tuple[ScopedGrantRecord, ...]:
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if expired:
                self._save_locked(state)
            out: list[ScopedGrantRecord] = []
            for raw in state["grants"].values():
                rec = ScopedGrantRecord.from_mapping(raw)
                if session_id is not None and rec.session_id != session_id:
                    continue
                if not include_inactive and rec.status != "active":
                    continue
                out.append(rec)
            out.sort(key=lambda r: r.issued_at)
            return tuple(out)

    def list_revocations(
        self,
        *,
        session_id: str | None = None,
    ) -> tuple[RevocationRuleRecord, ...]:
        with self._lock:
            state = self._load_locked()
            out: list[RevocationRuleRecord] = []
            for raw in state["revocations"].values():
                rec = RevocationRuleRecord.from_mapping(raw)
                if session_id is not None and rec.session_id not in {session_id, "*"}:
                    continue
                out.append(rec)
            out.sort(key=lambda r: r.created_at)
            return tuple(out)

    def issue_grant(
        self,
        *,
        session_id: str,
        capabilities: Sequence[str],
        resource_scopes: Sequence[str],
        ttl_seconds: float = SG_DEFAULT_GRANT_TTL_SECONDS,
        grant_id: str | None = None,
        task_id: str | None = None,
        epoch: int | None = None,
        tool_names: Sequence[str] = (),
        exact_args: Mapping[str, Any] | None = None,
        current_policy: str = "standard",
        max_uses: int | None = None,
        issued_by: str = "operator",
        note: str = "",
        workspace_root: Path | str | None = None,
    ) -> ScopedGrantRecord:
        """Persist a new scoped, finite-expiry grant bound to the current policy revision."""
        sid = _sg_clean_text(session_id, "session_id")
        if (
            not isinstance(ttl_seconds, (int, float))
            or isinstance(ttl_seconds, bool)
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) <= 0.0
            or float(ttl_seconds) > SG_MAX_GRANT_TTL_SECONDS
        ):
            raise InvalidGrantSpecError(
                f"ttl_seconds must be finite and within (0, {SG_MAX_GRANT_TTL_SECONDS}]."
            )
        caps = tuple(dict.fromkeys(_sg_clean_text(c, "capability") for c in capabilities))
        if not caps or any(c not in SG_CAPABILITY_TAXONOMY for c in caps):
            raise InvalidGrantSpecError(
                f"capabilities must be a non-empty subset of {sorted(SG_CAPABILITY_TAXONOMY)}."
            )
        norm_scopes = tuple(
            dict.fromkeys(
                normalize_resource_scope(s, workspace_root=workspace_root)
                for s in resource_scopes
            )
        )
        if not norm_scopes:
            raise InvalidGrantSpecError("resource_scopes must be non-empty.")

        clean_tools = tuple(
            dict.fromkeys(_sg_clean_text(t, "tool_name") for t in tool_names if str(t).strip())
        )
        if max_uses is not None:
            if isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1:
                raise InvalidGrantSpecError("max_uses must be a positive integer or None.")

        gid = (
            _sg_clean_text(grant_id, "grant_id")
            if grant_id is not None
            else f"grant-{uuid.uuid4().hex[:16]}"
        )
        args_digest = _sg_canonical_args_sha256(exact_args) if exact_args is not None else ""

        with self._lock:
            state = self._load_locked()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            if gid in state["grants"] or gid in state["rejected_approvals"]:
                raise InvalidGrantSpecError(f"Grant ID {gid!r} already exists and cannot be reused.")

            pol_raw = state["session_policy_revisions"].get(sid)
            if pol_raw is None:
                pol_rec = PolicyRevisionRecord(
                    session_id=sid,
                    revision=1,
                    policy=_sg_clean_text(current_policy or "standard", "policy"),
                    updated_at=now,
                    reason="grant_issuance_init",
                )
                state["session_policy_revisions"][sid] = pol_rec.to_mapping()
            else:
                pol_rec = PolicyRevisionRecord.from_mapping(pol_raw)

            rec = ScopedGrantRecord(
                grant_id=gid,
                session_id=sid,
                task_id=(str(task_id).strip() if task_id else None),
                epoch=(int(epoch) if epoch is not None else None),
                capabilities=caps,
                tool_names=clean_tools,
                resource_scopes=norm_scopes,
                args_sha256=args_digest,
                session_policy_revision=pol_rec.revision,
                global_policy_revision=int(state["global_policy_revision"]),
                issued_at=now,
                expires_at=now + float(ttl_seconds),
                max_uses=max_uses,
                uses_consumed=0,
                status="active",
                issued_by=_sg_clean_text(issued_by or "operator", "issued_by"),
                note=str(note or ""),
            )
            state["grants"][gid] = rec.to_mapping()

            # Issuing an explicit fresh grant lifts only exact-matching prior revocation rules
            # ONLY when explicitly re-granted? NO — revocations stay authoritative unless cleared;
            # never silently clear security revocations!
            self._append_audit_locked(
                state,
                "grant_issued",
                {
                    "grant_id": gid,
                    "session_id": sid,
                    "task_id": rec.task_id,
                    "epoch": rec.epoch,
                    "capabilities": list(caps),
                    "tool_names": list(clean_tools),
                    "resource_scopes": list(norm_scopes),
                    "session_policy_revision": rec.session_policy_revision,
                    "global_policy_revision": rec.global_policy_revision,
                    "expires_at": rec.expires_at,
                    "max_uses": max_uses,
                    "issued_by": rec.issued_by,
                },
                now=now,
            )
            self._save_locked(state)
            return rec

    def _grant_matches_action_locked(
        self,
        rec: ScopedGrantRecord,
        *,
        session_id: str,
        task_id: str | None,
        epoch: int | None,
        tool_name: str,
        args: Mapping[str, Any] | None,
        action_capabilities: Sequence[str],
        action_scopes: Sequence[str],
        current_session_rev: int,
        current_global_rev: int,
        now: float,
    ) -> tuple[bool, str, str]:
        if rec.status == "revoked":
            return (
                False,
                "grant_revoked",
                f"Scoped grant `{rec.grant_id}` was revoked ({rec.revoked_reason or 'revoked'}).",
            )
        if rec.status == "expired" or now >= rec.expires_at:
            return (
                False,
                "grant_expired",
                f"Scoped grant `{rec.grant_id}` expired at {rec.expires_at:.3f} (no automatic renewal).",
            )
        if rec.status == "consumed" or (
            rec.max_uses is not None and rec.uses_consumed >= rec.max_uses
        ):
            return (
                False,
                "grant_consumed",
                f"Scoped grant `{rec.grant_id}` exhausted its allowed uses ({rec.uses_consumed}/{rec.max_uses}); no automatic renewal.",
            )
        if rec.session_id != session_id:
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` belongs to session `{rec.session_id}`, not `{session_id}`.",
            )
        if rec.task_id is not None and task_id != rec.task_id:
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` is bound to task `{rec.task_id}`, not `{task_id}`.",
            )
        if rec.epoch is not None and epoch != rec.epoch:
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` is bound to epoch {rec.epoch}, not {epoch}.",
            )
        if rec.session_policy_revision != current_session_rev:
            return (
                False,
                "grant_policy_stale",
                f"Scoped grant `{rec.grant_id}` was issued at session policy revision {rec.session_policy_revision} (current {current_session_rev}).",
            )
        if rec.global_policy_revision != current_global_rev:
            return (
                False,
                "grant_policy_stale",
                f"Scoped grant `{rec.grant_id}` was issued at global policy revision {rec.global_policy_revision} (current {current_global_rev}).",
            )
        if rec.tool_names and tool_name not in rec.tool_names:
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` permits tools {list(rec.tool_names)}, not `{tool_name}`.",
            )
        if rec.args_sha256 and _sg_canonical_args_sha256(args) != rec.args_sha256:
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` is bound to exact approved tool arguments.",
            )
        if not set(action_capabilities).issubset(set(rec.capabilities)):
            missing = sorted(set(action_capabilities) - set(rec.capabilities))
            return (
                False,
                "grant_scope_mismatch",
                f"Scoped grant `{rec.grant_id}` does not grant required capability {missing} for `{tool_name}`.",
            )
        for act_sc in action_scopes:
            if not any(scope_subsumes(g_sc, act_sc) for g_sc in rec.resource_scopes):
                return (
                    False,
                    "grant_scope_mismatch",
                    f"Scoped grant `{rec.grant_id}` resource scopes {list(rec.resource_scopes)} do not cover `{act_sc}`.",
                )
        return (True, "", "")

    def check_revocation_denials(
        self,
        *,
        session_id: str,
        task_id: str | None,
        tool_name: str,
        action_capabilities: Sequence[str],
        action_scopes: Sequence[str],
    ) -> GrantValidationOutcome | None:
        """Check whether ANY active revocation rule blocks this tool/capability/resource (`AC 3`)."""
        sid = str(session_id or "").strip()
        with self._lock:
            state = self._load_locked()
            if not state["revocations"]:
                return None
            act_caps = set(action_capabilities)
            for raw in state["revocations"].values():
                rule = RevocationRuleRecord.from_mapping(raw)
                if rule.session_id not in {sid, "*"}:
                    continue
                if rule.task_id is not None and task_id is not None and rule.task_id != task_id:
                    continue
                # Tool-name match OR capability overlap:
                cap_hit = (not rule.capabilities) or bool(act_caps & set(rule.capabilities))
                tool_hit = bool(rule.tool_names) and (tool_name in rule.tool_names)
                if rule.capabilities and not cap_hit and not tool_hit:
                    continue
                if rule.tool_names and not rule.capabilities and not tool_hit:
                    continue
                # Resource scope overlap (symmetric! conservative subprocess `workspace:{ws}`
                # overlaps a revoked `path:{ws}:{rel}` so shell cannot bypass file revocation!):
                scope_hit = (not rule.resource_scopes) or any(
                    scopes_overlap(r_sc, a_sc)
                    for r_sc in rule.resource_scopes
                    for a_sc in action_scopes
                )
                if not scope_hit:
                    continue
                return GrantValidationOutcome(
                    allowed=False,
                    grant_id=(rule.source_grant_id or None),
                    error_code="capability_revoked",
                    reason=(
                        f"Capability/resource revoked ({rule.revocation_id}: {rule.reason}); "
                        f"`{tool_name}` cannot execute or bypass revocation."
                    ),
                )
            return None

    def find_matching_grant(
        self,
        *,
        session_id: str,
        task_id: str | None,
        epoch: int | None,
        tool_name: str,
        args: Mapping[str, Any] | None,
        workspace_root: Path | str | None = None,
        candidate_rel_paths: Sequence[str] = (),
        barriers: Sequence[tuple[str, str]] = (),
    ) -> ScopedGrantRecord | None:
        """Read-only lookup of a currently valid active grant (never extends `expires_at` or consumes)."""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        act_caps = resolve_tool_capabilities(tool_name, args)
        act_scopes = resolve_tool_resource_scopes(
            tool_name,
            args,
            workspace_root=workspace_root,
            candidate_rel_paths=candidate_rel_paths,
            barriers=barriers,
        )
        if self.check_revocation_denials(
            session_id=sid,
            task_id=task_id,
            tool_name=tool_name,
            action_capabilities=act_caps,
            action_scopes=act_scopes,
        ) is not None:
            return None

        with self._lock:
            state = self._load_locked()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if expired:
                self._save_locked(state)
            pol_raw = state["session_policy_revisions"].get(sid)
            sess_rev = PolicyRevisionRecord.from_mapping(pol_raw).revision if pol_raw else 1
            glob_rev = int(state["global_policy_revision"])

            for raw in state["grants"].values():
                rec = ScopedGrantRecord.from_mapping(raw)
                if rec.status != "active":
                    continue
                ok, _, _ = self._grant_matches_action_locked(
                    rec,
                    session_id=sid,
                    task_id=task_id,
                    epoch=epoch,
                    tool_name=tool_name,
                    args=args,
                    action_capabilities=act_caps,
                    action_scopes=act_scopes,
                    current_session_rev=sess_rev,
                    current_global_rev=glob_rev,
                    now=now,
                )
                if ok:
                    return rec
            return None

    def validate_and_consume(
        self,
        *,
        grant_id: str,
        session_id: str,
        task_id: str | None,
        epoch: int | None,
        tool_name: str,
        args: Mapping[str, Any] | None,
        action_capabilities: Sequence[str],
        action_scopes: Sequence[str],
    ) -> GrantValidationOutcome:
        """Atomically validate `grant_id` at admission time and consume 1 use (`AC 1`, `AC 2`)."""
        gid = str(grant_id or "").strip()
        sid = str(session_id or "").strip()
        if not gid:
            return GrantValidationOutcome(
                allowed=False,
                error_code="grant_not_found",
                reason="Missing grant_id for scoped grant admission.",
            )

        rev_denial = self.check_revocation_denials(
            session_id=sid,
            task_id=task_id,
            tool_name=tool_name,
            action_capabilities=action_capabilities,
            action_scopes=action_scopes,
        )
        if rev_denial is not None:
            return rev_denial

        with self._lock:
            state = self._load_locked()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if gid in state["rejected_approvals"]:
                if expired:
                    self._save_locked(state)
                rej = state["rejected_approvals"][gid]
                return GrantValidationOutcome(
                    allowed=False,
                    grant_id=gid,
                    error_code="grant_revoked",
                    reason=f"Approval `{gid}` was rejected or cancelled ({rej.get('reason') or 'rejected'}).",
                )
            raw = state["grants"].get(gid)
            if raw is None:
                if expired:
                    self._save_locked(state)
                return GrantValidationOutcome(
                    allowed=False,
                    grant_id=gid,
                    error_code="grant_not_found",
                    reason=f"Scoped grant `{gid}` does not exist.",
                )

            rec = ScopedGrantRecord.from_mapping(raw)
            pol_raw = state["session_policy_revisions"].get(sid)
            sess_rev = PolicyRevisionRecord.from_mapping(pol_raw).revision if pol_raw else 1
            glob_rev = int(state["global_policy_revision"])

            ok, err_code, err_reason = self._grant_matches_action_locked(
                rec,
                session_id=sid,
                task_id=task_id,
                epoch=epoch,
                tool_name=tool_name,
                args=args,
                action_capabilities=action_capabilities,
                action_scopes=action_scopes,
                current_session_rev=sess_rev,
                current_global_rev=glob_rev,
                now=now,
            )
            if not ok:
                self._append_audit_locked(
                    state,
                    "grant_denied",
                    {
                        "grant_id": gid,
                        "session_id": sid,
                        "tool_name": tool_name,
                        "error_code": err_code,
                        "reason": err_reason,
                    },
                    now=now,
                )
                self._save_locked(state)
                return GrantValidationOutcome(
                    allowed=False,
                    grant_id=gid,
                    error_code=err_code,
                    reason=err_reason,
                    grant=rec,
                )

            # Consume 1 use WITHOUT touching `expires_at` (strictly NO automatic renewal!).
            new_uses = rec.uses_consumed + 1
            new_status: GrantStatus = (
                "consumed" if (rec.max_uses is not None and new_uses >= rec.max_uses) else "active"
            )
            consumed_rec = ScopedGrantRecord(
                grant_id=rec.grant_id,
                session_id=rec.session_id,
                task_id=rec.task_id,
                epoch=rec.epoch,
                capabilities=rec.capabilities,
                tool_names=rec.tool_names,
                resource_scopes=rec.resource_scopes,
                args_sha256=rec.args_sha256,
                session_policy_revision=rec.session_policy_revision,
                global_policy_revision=rec.global_policy_revision,
                issued_at=rec.issued_at,
                expires_at=rec.expires_at,
                max_uses=rec.max_uses,
                uses_consumed=new_uses,
                status=new_status,
                issued_by=rec.issued_by,
                note=rec.note,
                revoked_at=rec.revoked_at,
                revoked_reason=rec.revoked_reason,
            )
            state["grants"][gid] = consumed_rec.to_mapping()
            self._append_audit_locked(
                state,
                "grant_consumed",
                {
                    "grant_id": gid,
                    "session_id": sid,
                    "tool_name": tool_name,
                    "uses_consumed": new_uses,
                    "max_uses": rec.max_uses,
                    "expires_at": rec.expires_at,
                    "status": new_status,
                },
                now=now,
            )
            self._save_locked(state)
            return GrantValidationOutcome(
                allowed=True,
                grant_id=gid,
                grant=consumed_rec,
            )

    def revoke_grant(
        self,
        grant_id: str,
        *,
        session_id: str | None = None,
        reason: str = "operator_revoked",
        block_scope: bool = True,
    ) -> tuple[ScopedGrantRecord | None, RevocationRuleRecord | None]:
        """Revoke `grant_id` and (when `block_scope=True`) persist a scope revocation rule
        preventing bypass through any other tool (`AC 3`)."""
        gid = _sg_clean_text(grant_id, "grant_id")
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            raw = state["grants"].get(gid)
            if raw is None:
                return (None, None)
            rec = ScopedGrantRecord.from_mapping(raw)
            if session_id is not None and rec.session_id != session_id:
                raise InvalidGrantSpecError(
                    f"Grant `{gid}` belongs to session `{rec.session_id}`, not `{session_id}`."
                )
            updated = ScopedGrantRecord(
                grant_id=rec.grant_id,
                session_id=rec.session_id,
                task_id=rec.task_id,
                epoch=rec.epoch,
                capabilities=rec.capabilities,
                tool_names=rec.tool_names,
                resource_scopes=rec.resource_scopes,
                args_sha256=rec.args_sha256,
                session_policy_revision=rec.session_policy_revision,
                global_policy_revision=rec.global_policy_revision,
                issued_at=rec.issued_at,
                expires_at=rec.expires_at,
                max_uses=rec.max_uses,
                uses_consumed=rec.uses_consumed,
                status="revoked",
                issued_by=rec.issued_by,
                note=rec.note,
                revoked_at=now,
                revoked_reason=reason,
            )
            state["grants"][gid] = updated.to_mapping()
            rule: RevocationRuleRecord | None = None
            if block_scope:
                rev_id = f"rev-{hashlib.sha256(f'{gid}:{now}'.encode('utf-8')).hexdigest()[:14]}"
                conseq_caps = tuple(c for c in rec.capabilities if c != "workspace_read")
                rule_caps = conseq_caps if conseq_caps else rec.capabilities
                rule = RevocationRuleRecord(
                    revocation_id=rev_id,
                    session_id=rec.session_id,
                    task_id=rec.task_id,
                    capabilities=rule_caps,
                    resource_scopes=rec.resource_scopes,
                    tool_names=(),  # Enforce at capability + resource level so no other tool can bypass!
                    source_grant_id=gid,
                    reason=reason,
                    created_at=now,
                )
                state["revocations"][rev_id] = rule.to_mapping()

            self._append_audit_locked(
                state,
                "grant_revoked",
                {
                    "grant_id": gid,
                    "session_id": rec.session_id,
                    "reason": reason,
                    "block_scope": block_scope,
                    "revocation_id": (rule.revocation_id if rule else None),
                },
                now=now,
            )
            self._save_locked(state)
            return (updated, rule)

    def revoke_scope(
        self,
        *,
        session_id: str,
        capabilities: Sequence[str] = (),
        resource_scopes: Sequence[str] = (),
        tool_names: Sequence[str] = (),
        task_id: str | None = None,
        reason: str = "operator_scope_revoked",
        workspace_root: Path | str | None = None,
    ) -> tuple[RevocationRuleRecord, tuple[ScopedGrantRecord, ...]]:
        """Persist a capability/resource/tool revocation rule and revoke overlapping grants."""
        sid = _sg_clean_text(session_id, "session_id")
        caps = tuple(dict.fromkeys(_sg_clean_text(c, "capability") for c in capabilities if str(c).strip()))
        if any(c not in SG_CAPABILITY_TAXONOMY for c in caps):
            raise InvalidGrantSpecError(
                f"Invalid capability in revocation: {caps!r}; expected {sorted(SG_CAPABILITY_TAXONOMY)}."
            )
        norm_scopes = tuple(
            dict.fromkeys(
                normalize_resource_scope(s, workspace_root=workspace_root)
                for s in resource_scopes
                if str(s).strip()
            )
        )
        clean_tools = tuple(
            dict.fromkeys(_sg_clean_text(t, "tool_name") for t in tool_names if str(t).strip())
        )
        if not caps and not norm_scopes and not clean_tools:
            raise InvalidGrantSpecError(
                "revoke_scope requires at least one of capabilities, resource_scopes, or tool_names."
            )
        # If only `tool_names` was specified without explicit `capabilities`, derive capabilities
        # from those tools so no sibling tool with the same capability can bypass the revocation!
        effective_caps = caps
        if not effective_caps and clean_tools:
            derived: list[str] = []
            for tn in clean_tools:
                for c in resolve_tool_capabilities(tn):
                    if c not in derived:
                        derived.append(c)
            effective_caps = tuple(derived)

        with self._lock:
            state = self._load_locked()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            rev_id = f"rev-{uuid.uuid4().hex[:14]}"
            rule = RevocationRuleRecord(
                revocation_id=rev_id,
                session_id=sid,
                task_id=(str(task_id).strip() if task_id else None),
                capabilities=effective_caps,
                resource_scopes=norm_scopes,
                tool_names=clean_tools,
                source_grant_id="",
                reason=reason,
                created_at=now,
            )
            state["revocations"][rev_id] = rule.to_mapping()

            revoked_grants: list[ScopedGrantRecord] = []
            for gid, g_raw in list(state["grants"].items()):
                g = ScopedGrantRecord.from_mapping(g_raw)
                if g.status != "active":
                    continue
                if sid != "*" and g.session_id != sid:
                    continue
                if rule.task_id is not None and g.task_id is not None and g.task_id != rule.task_id:
                    continue
                cap_overlap = (not effective_caps) or bool(set(g.capabilities) & set(effective_caps))
                scope_overlap = (not norm_scopes) or any(
                    scopes_overlap(r_sc, g_sc)
                    for r_sc in norm_scopes
                    for g_sc in g.resource_scopes
                )
                if cap_overlap and scope_overlap:
                    upd = ScopedGrantRecord(
                        grant_id=g.grant_id,
                        session_id=g.session_id,
                        task_id=g.task_id,
                        epoch=g.epoch,
                        capabilities=g.capabilities,
                        tool_names=g.tool_names,
                        resource_scopes=g.resource_scopes,
                        args_sha256=g.args_sha256,
                        session_policy_revision=g.session_policy_revision,
                        global_policy_revision=g.global_policy_revision,
                        issued_at=g.issued_at,
                        expires_at=g.expires_at,
                        max_uses=g.max_uses,
                        uses_consumed=g.uses_consumed,
                        status="revoked",
                        issued_by=g.issued_by,
                        note=g.note,
                        revoked_at=now,
                        revoked_reason=reason,
                    )
                    state["grants"][gid] = upd.to_mapping()
                    revoked_grants.append(upd)

            self._append_audit_locked(
                state,
                "scope_revoked",
                {
                    "revocation_id": rev_id,
                    "session_id": sid,
                    "capabilities": list(effective_caps),
                    "resource_scopes": list(norm_scopes),
                    "tool_names": list(clean_tools),
                    "reason": reason,
                    "revoked_grant_ids": [r.grant_id for r in revoked_grants],
                },
                now=now,
            )
            self._save_locked(state)
            return (rule, tuple(revoked_grants))

    def revoke_by_lifecycle_event(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        epochs: Sequence[int] = (),
        reason: str = "lifecycle_revoked",
    ) -> tuple[ScopedGrantRecord, ...]:
        """Event-driven revocation for lease epoch fencing, task cancellation, abort, or incident."""
        epoch_set = {int(e) for e in epochs}
        with self._lock:
            state = self._load_locked()
            if not state["grants"]:
                return ()
            now = self._clock()
            self._expire_due_locked(state, now=now)
            revoked: list[ScopedGrantRecord] = []
            for gid, g_raw in list(state["grants"].items()):
                g = ScopedGrantRecord.from_mapping(g_raw)
                if g.status != "active":
                    continue
                if session_id is not None and g.session_id != session_id:
                    continue
                if task_id is not None and g.task_id != task_id and g.session_id != task_id:
                    continue
                if epoch_set and (g.epoch is None or g.epoch not in epoch_set):
                    continue
                upd = ScopedGrantRecord(
                    grant_id=g.grant_id,
                    session_id=g.session_id,
                    task_id=g.task_id,
                    epoch=g.epoch,
                    capabilities=g.capabilities,
                    tool_names=g.tool_names,
                    resource_scopes=g.resource_scopes,
                    args_sha256=g.args_sha256,
                    session_policy_revision=g.session_policy_revision,
                    global_policy_revision=g.global_policy_revision,
                    issued_at=g.issued_at,
                    expires_at=g.expires_at,
                    max_uses=g.max_uses,
                    uses_consumed=g.uses_consumed,
                    status="revoked",
                    issued_by=g.issued_by,
                    note=g.note,
                    revoked_at=now,
                    revoked_reason=reason,
                )
                state["grants"][gid] = upd.to_mapping()
                revoked.append(upd)
            if revoked:
                self._append_audit_locked(
                    state,
                    "lifecycle_grants_revoked",
                    {
                        "session_id": session_id,
                        "task_id": task_id,
                        "epochs": sorted(epoch_set),
                        "reason": reason,
                        "revoked_grant_ids": [r.grant_id for r in revoked],
                    },
                    now=now,
                )
                self._save_locked(state)
            return tuple(revoked)

    def purge_session(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            state = self._load_locked()
            changed = False
            if sid in state["session_policy_revisions"]:
                state["session_policy_revisions"].pop(sid, None)
                changed = True
            for gid in [
                k for k, v in state["grants"].items() if v.get("session_id") == sid
            ]:
                state["grants"].pop(gid, None)
                changed = True
            for rid in [
                k for k, v in state["revocations"].items() if v.get("session_id") == sid
            ]:
                state["revocations"].pop(rid, None)
                changed = True
            for aid in [
                k for k, v in state["rejected_approvals"].items() if v.get("session_id") == sid
            ]:
                state["rejected_approvals"].pop(aid, None)
                changed = True
            if changed:
                self._save_locked(state)

    def summary(
        self,
        *,
        session_id: str,
        current_policy: str = "standard",
    ) -> dict[str, Any]:
        sid = _sg_clean_text(session_id, "session_id")
        with self._lock:
            state = self._load_locked()
            now = self._clock()
            expired = self._expire_due_locked(state, now=now)
            if expired:
                self._save_locked(state)
            pol_raw = state["session_policy_revisions"].get(sid)
            sess_rev = PolicyRevisionRecord.from_mapping(pol_raw).revision if pol_raw else 1
            pol_name = PolicyRevisionRecord.from_mapping(pol_raw).policy if pol_raw else current_policy
            grants = [
                ScopedGrantRecord.from_mapping(v).to_mapping()
                for v in state["grants"].values()
                if v.get("session_id") == sid
            ]
            revocations = [
                RevocationRuleRecord.from_mapping(v).to_mapping()
                for v in state["revocations"].values()
                if v.get("session_id") in {sid, "*"}
            ]
            recent_events = [
                dict(ev)
                for ev in state["audit_events"]
                if ev.get("session_id") in {sid, None, "*"}
            ][-25:]
            return {
                "schema_version": SG_SCHEMA_VERSION,
                "session_id": sid,
                "policy": pol_name,
                "session_policy_revision": sess_rev,
                "global_policy_revision": int(state["global_policy_revision"]),
                "active_grants": [g for g in grants if g["status"] == "active"],
                "grants": grants,
                "revocations": revocations,
                "recent_events": recent_events,
            }
