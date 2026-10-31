"""Bounded child subagent delegation, authority subsets, mutation locks, and durable parent joins (M5-P4-03).

Stdlib-only contract module (inlined into standalone ``studio.py``). Every private
module-level symbol uses the ``_cd_`` / ``CD_`` prefix to guarantee zero AST symbol
collisions in the single-file bundle.

Core invariants:
1. Child authority is a strict subset of parent authority (policy lattice, tool allowlist,
   capabilities, workspace-confined readable/writable path scopes, non-escalating grants,
   and maximum delegation depth = 1 with ``delegate_subagent`` forbidden on children).
2. Child task budgets are strict subsets of the parent's unreserved remaining budget, and
   actual child consumption is debited back into the parent's cumulative budget on join.
3. Disjoint workspace mutation reservations prevent overlapping writes across sibling
   children AND between an unjoined child and its parent; base SHA-256 file snapshots
   guard against concurrent external drift and support clean rollback on rejected joins.
4. Parent pause, cancel, epoch fence, and crash recovery propagate to all active children,
   and parent ``complete_task`` refuses completion while any child remains unjoined or
   holds unresolved consequential actions.
5. Delegation is gated on justified benefit over single-agent execution and evaluated via
   reproducible single-agent vs bounded-delegation benchmark runs.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import threading
import time
from typing import Any, Literal

import execution_budget

CD_SCHEMA_VERSION = 1
CD_POLICY_RANK: dict[str, int] = {"plan": 0, "standard": 1, "autonomous": 2}

CD_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_files",
        "grep_search",
        "list_edit_batches",
        "reconcile_task_actions",
        "codegraph_architecture",
        "codegraph_context",
        "codegraph_impact",
        "codegraph_path",
        "codegraph_quality",
        "ctx_search",
        "oc_search",
        "oc_read",
        "oc_lint",
    }
)
CD_MUTATING_FILE_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "rollback_edits"}
)
CD_CODE_EXEC_TOOLS: frozenset[str] = frozenset(
    {"run_bash", "ctx_execute_python", "ctx_run_block", "skill_execute_script"}
)
CD_CONTEXT_WRITE_TOOLS: frozenset[str] = frozenset({"oc_save_doc"})
CD_FORBIDDEN_CHILD_TOOLS: frozenset[str] = frozenset({"delegate_subagent"})
CD_DEFAULT_CRITIC_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_files",
    "grep_search",
    "codegraph_context",
    "codegraph_quality",
)

_CD_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CD_SECRET_BASENAME_RE = re.compile(
    r"^(\.env(\..+)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|key|p12|pfx|keystore)|credentials\.json|\.netrc|\.pypirc)$",
    re.IGNORECASE,
)

ChildRole = Literal["critic", "worker", "verifier"]
ChildDelegationStatus = Literal[
    "running",
    "completed_unjoined",
    "failed_unjoined",
    "cancelled_unjoined",
    "joined",
    "joined_discarded",
]
ChildJoinDisposition = Literal[
    "accepted",
    "discarded",
    "audit_only",
    "cancelled_reconciled",
]


class ChildDelegationError(RuntimeError):
    """Base error for bounded child delegation."""


class DelegationRejectedError(ChildDelegationError):
    """Raised when spawning, executing, or joining a child delegation fails closed."""

    def __init__(self, code: str, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = str(code)
        self.reason = str(reason)
        self.details: dict[str, Any] = dict(details) if details is not None else {}

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "reason": self.reason}
        if self.details:
            out["details"] = dict(self.details)
        return out


class DelegationAuthorityViolationError(DelegationRejectedError):
    """Raised when a child requests or exercises authority exceeding its parent (AC 1)."""


class DelegationStoreIntegrityError(ChildDelegationError):
    """Raised when ``delegations/state.json`` is symlinked, corrupt, or has an unsupported schema."""


def _cd_canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cd_clean_id(value: object, label: str, *, max_len: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise DelegationRejectedError("invalid_identifier", f"{label} must be a non-empty string.")
    cleaned = value.strip()
    if len(cleaned) > max_len or not _CD_OPAQUE_ID_RE.fullmatch(cleaned):
        raise DelegationRejectedError("invalid_identifier", f"{label} {cleaned!r} is malformed.")
    return cleaned


def child_subtask_id(parent_task_id: str, sub_key: str) -> str:
    """Build deterministic ``runtime_tasks.task_id`` for a delegated child subagent."""
    parent = _cd_clean_id(parent_task_id, "parent_task_id")
    key = _cd_clean_id(sub_key, "sub_key")
    candidate = f"{parent}::sub:{key}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{parent[:96]}::sub:{digest}"


def is_child_subtask_id(task_id: str) -> bool:
    """Return True when ``task_id`` names a delegated child subagent row (not a milestone)."""
    return isinstance(task_id, str) and "::sub:" in task_id


def _cd_is_forbidden_rel_path(norm_rel: str) -> tuple[bool, str]:
    parts = [pt for pt in norm_rel.split("/") if pt]
    if not parts:
        return True, "empty_path"
    if parts[0] == ".git" or ".git" in parts:
        return True, "git_metadata_forbidden"
    for part in parts:
        if _CD_SECRET_BASENAME_RE.fullmatch(part):
            return True, "secret_path_forbidden"
    return False, ""


def normalize_child_scope_path(
    workspace_root: Path,
    raw_path: str,
    *,
    allow_wildcard: bool = False,
) -> str:
    """Validate and normalize a child-scoped workspace relative path or directory prefix.

    Rejects absolute paths, ``..`` segments, NUL bytes, ``.git`` metadata, secret/credential
    filenames, and any symlink component along existing path prefixes inside ``workspace_root``.
    """
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise DelegationAuthorityViolationError(
            "child_invalid_scope_path",
            f"Child scope path must be a non-empty relative path: {raw_path!r}",
        )
    cleaned = raw_path.strip().replace("\\", "/")
    if cleaned == "*":
        if allow_wildcard:
            return "*"
        raise DelegationAuthorityViolationError(
            "child_unbounded_write_scope_forbidden",
            "Wildcard '*' scope is forbidden for child write authority; enumerate explicit paths.",
        )
    if cleaned.startswith("/"):
        raise DelegationAuthorityViolationError(
            "child_scope_escapes_workspace",
            f"Absolute path {raw_path!r} is forbidden in child scope.",
        )
    is_dir_hint = cleaned.endswith("/")
    raw_parts = [pt for pt in cleaned.split("/") if pt not in ("", ".")]
    if not raw_parts or any(pt == ".." for pt in raw_parts):
        raise DelegationAuthorityViolationError(
            "child_scope_escapes_workspace",
            f"Child scope path {raw_path!r} must stay strictly within workspace_root.",
        )
    norm = "/".join(raw_parts)
    bad, bad_reason = _cd_is_forbidden_rel_path(norm)
    if bad:
        raise DelegationAuthorityViolationError(
            "child_forbidden_sensitive_path",
            f"Child scope {norm!r} targets protected path ({bad_reason}).",
        )

    ws_resolved = workspace_root.resolve()
    cursor = ws_resolved
    for part in raw_parts:
        cursor = cursor / part
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(st.st_mode):
            raise DelegationAuthorityViolationError(
                "child_symlink_scope_forbidden",
                f"Symlink component {cursor.name!r} in child scope {norm!r} is forbidden.",
            )
    resolved_target = (ws_resolved / norm).resolve()
    if resolved_target != ws_resolved and ws_resolved not in resolved_target.parents:
        raise DelegationAuthorityViolationError(
            "child_scope_escapes_workspace",
            f"Child scope {norm!r} resolves outside workspace_root.",
        )
    if is_dir_hint or (resolved_target.exists() and resolved_target.is_dir()):
        return f"{norm}/"
    return norm


def child_scopes_overlap(scope_a: str, scope_b: str) -> bool:
    """Return True when two normalized path scopes overlap (file, directory prefix, or '*')."""
    a = scope_a.strip()
    b = scope_b.strip()
    if not a or not b:
        return False
    if a == "*" or b == "*":
        return True
    a_clean = a.rstrip("/")
    b_clean = b.rstrip("/")
    if a_clean == b_clean:
        return True
    if a.endswith("/") and b_clean.startswith(a_clean + "/"):
        return True
    if b.endswith("/") and a_clean.startswith(b_clean + "/"):
        return True
    return False


def _cd_scope_is_covered_by(child_scope: str, parent_scopes: Collection[str]) -> bool:
    """Return True iff ``child_scope`` is a strict or equal subset of at least one ``parent_scopes`` entry."""
    for p_raw in parent_scopes:
        p = str(p_raw).strip()
        if p in {"*", "global:*"} or p.startswith("workspace:"):
            return True
        if p.startswith("path:"):
            # scoped_grants uses either `path:<rel>` or `path:<ws>:<rel>`
            tail = p.removeprefix("path:")
            if not child_scope.endswith("/") and (tail == child_scope or tail.endswith(":" + child_scope)):
                return True
        elif p.startswith("dir:"):
            tail = p.removeprefix("dir:").rstrip("/")
            c_clean = child_scope.rstrip("/")
            for candidate in (tail, tail.split(":", 1)[-1] if ":" in tail else tail):
                if c_clean == candidate or c_clean.startswith(candidate + "/"):
                    return True
        else:
            p_clean = p.rstrip("/")
            c_clean = child_scope.rstrip("/")
            if p == "*":
                return True
            if p.endswith("/") and (c_clean == p_clean or c_clean.startswith(p_clean + "/")):
                return True
            if not child_scope.endswith("/") and p_clean == c_clean:
                return True
    return False


def snapshot_scope_file_versions(
    workspace_root: Path,
    scopes: Sequence[str],
) -> dict[str, str | None]:
    """Capture SHA-256 digests of existing regular files covered by ``scopes``."""
    ws = workspace_root.resolve()
    versions: dict[str, str | None] = {}
    for sc in scopes:
        if sc == "*":
            continue
        if sc.endswith("/"):
            dir_target = ws / sc.rstrip("/")
            if dir_target.exists() and dir_target.is_dir():
                for root_s, dirs, files in os.walk(dir_target, followlinks=False):
                    dirs[:] = [d for d in sorted(dirs) if d != ".git" and not (Path(root_s) / d).is_symlink()]
                    for fname in sorted(files):
                        fp = Path(root_s) / fname
                        if fp.is_symlink() or not fp.is_file():
                            continue
                        rel = fp.relative_to(ws).as_posix()
                        bad, _ = _cd_is_forbidden_rel_path(rel)
                        if not bad:
                            versions[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        else:
            fp = ws / sc
            if fp.is_symlink():
                raise DelegationAuthorityViolationError(
                    "child_symlink_scope_forbidden",
                    f"Scoped file {sc!r} is a symlink.",
                )
            if fp.exists():
                if not fp.is_file():
                    raise DelegationAuthorityViolationError(
                        "child_invalid_scope_path",
                        f"Scoped file {sc!r} is not a regular file.",
                    )
                versions[sc] = hashlib.sha256(fp.read_bytes()).hexdigest()
            else:
                versions[sc] = None
    return versions


def _cd_infer_tool_capabilities(tool_name: str) -> tuple[str, ...]:
    if tool_name in CD_READ_ONLY_TOOLS:
        return ("workspace_read",)
    if tool_name in CD_MUTATING_FILE_TOOLS:
        return ("workspace_write", "workspace_read")
    if tool_name in CD_CODE_EXEC_TOOLS:
        return ("code_execution", "workspace_write", "workspace_read")
    if tool_name in CD_CONTEXT_WRITE_TOOLS or tool_name == "mcp_bridge__oc_save_doc":
        return ("context_write",)
    if tool_name.startswith("mcp__") or tool_name.startswith("mcp_bridge__"):
        return ("external_mcp",)
    return ("unknown",)


@dataclass(frozen=True, slots=True)
class ChildAuthoritySubset:
    """Immutable, hash-addressed subset of parent permissions granted to a child subagent (AC 1)."""

    parent_policy: str
    policy: str
    allowed_tools: tuple[str, ...]
    capabilities: tuple[str, ...]
    readable_paths: tuple[str, ...]
    writable_paths: tuple[str, ...]
    delegated_grant_ids: tuple[str, ...] = ()
    max_depth: int = 1
    authority_sha256: str = ""

    def __post_init__(self) -> None:
        if self.parent_policy not in CD_POLICY_RANK or self.policy not in CD_POLICY_RANK:
            raise DelegationAuthorityViolationError(
                "child_invalid_policy",
                f"Unknown parent/child policy ({self.parent_policy!r} -> {self.policy!r}).",
            )
        if CD_POLICY_RANK[self.policy] > CD_POLICY_RANK[self.parent_policy]:
            raise DelegationAuthorityViolationError(
                "child_policy_exceeds_parent",
                f"Child policy `{self.policy}` exceeds parent policy `{self.parent_policy}`.",
            )
        if not self.allowed_tools:
            raise DelegationAuthorityViolationError(
                "child_empty_tool_subset",
                "Child authority must declare at least one allowed tool or audit capability.",
            )
        if any(t in CD_FORBIDDEN_CHILD_TOOLS for t in self.allowed_tools):
            raise DelegationAuthorityViolationError(
                "child_recursive_delegation_forbidden",
                "Child subagents cannot hold `delegate_subagent` authority (max_depth=1).",
            )
        if self.max_depth != 1:
            raise DelegationAuthorityViolationError(
                "child_recursive_delegation_forbidden",
                f"Unsupported delegation depth {self.max_depth}; max_depth must be 1.",
            )
        if not self.authority_sha256:
            digest = _cd_canonical_sha256(
                {
                    "parent_policy": self.parent_policy,
                    "policy": self.policy,
                    "allowed_tools": list(self.allowed_tools),
                    "capabilities": list(self.capabilities),
                    "readable_paths": list(self.readable_paths),
                    "writable_paths": list(self.writable_paths),
                    "delegated_grant_ids": list(self.delegated_grant_ids),
                    "max_depth": self.max_depth,
                }
            )
            object.__setattr__(self, "authority_sha256", digest)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "parent_policy": self.parent_policy,
            "policy": self.policy,
            "allowed_tools": list(self.allowed_tools),
            "capabilities": list(self.capabilities),
            "readable_paths": list(self.readable_paths),
            "writable_paths": list(self.writable_paths),
            "delegated_grant_ids": list(self.delegated_grant_ids),
            "max_depth": self.max_depth,
            "authority_sha256": self.authority_sha256,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ChildAuthoritySubset:
        obj = cls(
            parent_policy=str(data["parent_policy"]),
            policy=str(data["policy"]),
            allowed_tools=tuple(str(x) for x in data.get("allowed_tools", ())),
            capabilities=tuple(str(x) for x in data.get("capabilities", ())),
            readable_paths=tuple(str(x) for x in data.get("readable_paths", ())),
            writable_paths=tuple(str(x) for x in data.get("writable_paths", ())),
            delegated_grant_ids=tuple(str(x) for x in data.get("delegated_grant_ids", ())),
            max_depth=int(data.get("max_depth", 1)),
        )
        expected_sha = str(data.get("authority_sha256") or "")
        if expected_sha and expected_sha != obj.authority_sha256:
            raise DelegationStoreIntegrityError("ChildAuthoritySubset digest mismatch.")
        return obj


def derive_child_authority(
    *,
    workspace_root: Path,
    parent_policy: str,
    parent_revoked_capabilities: Collection[str] = (),
    parent_allowed_scopes: Collection[str] | None = None,
    requested_policy: str | None = None,
    role: str = "critic",
    requested_tools: Sequence[str] | None = None,
    readable_paths: Sequence[str] | None = None,
    writable_paths: Sequence[str] | None = None,
    delegated_grant_ids: Sequence[str] = (),
) -> ChildAuthoritySubset:
    """Derive and validate a strict child authority subset from parent permissions (Task 1, AC 1)."""
    if parent_policy not in CD_POLICY_RANK:
        raise DelegationAuthorityViolationError(
            "child_invalid_policy",
            f"Unknown parent policy `{parent_policy}`.",
        )
    default_pol = "plan" if role == "critic" else parent_policy
    eff_policy = str(requested_policy or default_pol).strip()
    if eff_policy not in CD_POLICY_RANK:
        raise DelegationAuthorityViolationError(
            "child_invalid_policy",
            f"Unknown requested child policy `{eff_policy}`.",
        )
    if CD_POLICY_RANK[eff_policy] > CD_POLICY_RANK[parent_policy]:
        raise DelegationAuthorityViolationError(
            "child_policy_exceeds_parent",
            f"Requested child policy `{eff_policy}` exceeds parent policy `{parent_policy}`.",
        )

    raw_tools = (
        tuple(str(t).strip() for t in requested_tools if str(t).strip())
        if requested_tools is not None
        else CD_DEFAULT_CRITIC_TOOLS
    )
    if not raw_tools:
        raise DelegationAuthorityViolationError(
            "child_empty_tool_subset",
            "Child `allowed_tools` must not be empty.",
        )
    dedup_tools: list[str] = []
    seen_t: set[str] = set()
    for t in raw_tools:
        if t in CD_FORBIDDEN_CHILD_TOOLS:
            raise DelegationAuthorityViolationError(
                "child_recursive_delegation_forbidden",
                "Child subagents cannot receive `delegate_subagent` permission.",
            )
        if t not in seen_t:
            seen_t.add(t)
            dedup_tools.append(t)

    revoked_set = {str(c) for c in parent_revoked_capabilities}
    caps: set[str] = set()
    for t in dedup_tools:
        t_caps = _cd_infer_tool_capabilities(t)
        if "unknown" in t_caps:
            raise DelegationAuthorityViolationError(
                "child_unknown_tool_forbidden",
                f"Tool `{t}` is unknown and cannot be delegated to a child subagent.",
            )
        if parent_policy == "plan" and t not in CD_READ_ONLY_TOOLS:
            raise DelegationAuthorityViolationError(
                "child_tool_exceeds_parent",
                f"Parent in `plan` policy cannot delegate non-read-only tool `{t}`.",
            )
        if eff_policy == "plan" and t not in CD_READ_ONLY_TOOLS:
            raise DelegationAuthorityViolationError(
                "child_tool_exceeds_child_policy",
                f"Child in `plan` policy cannot hold non-read-only tool `{t}`.",
            )
        overlap_rev = revoked_set.intersection(t_caps)
        if overlap_rev:
            raise DelegationAuthorityViolationError(
                "child_capability_revoked_on_parent",
                f"Cannot delegate tool `{t}`: capability {sorted(overlap_rev)} is revoked on parent.",
            )
        caps.update(t_caps)

    norm_write: list[str] = []
    if writable_paths:
        for wp in writable_paths:
            nw = normalize_child_scope_path(workspace_root, wp, allow_wildcard=False)
            if parent_allowed_scopes is not None and not _cd_scope_is_covered_by(nw, parent_allowed_scopes):
                raise DelegationAuthorityViolationError(
                    "child_scope_exceeds_parent",
                    f"Child writable path `{nw}` exceeds parent's allowed resource scopes.",
                )
            if nw not in norm_write:
                norm_write.append(nw)

    needs_write = any(t in CD_MUTATING_FILE_TOOLS or t in CD_CODE_EXEC_TOOLS for t in dedup_tools)
    if needs_write and not norm_write:
        raise DelegationAuthorityViolationError(
            "child_unbounded_write_scope_forbidden",
            "Mutating or code-executing child subagents must declare explicit `writable_paths`.",
        )
    if not needs_write and norm_write:
        raise DelegationAuthorityViolationError(
            "child_write_scope_without_write_tool",
            "Read-only child subagent cannot claim `writable_paths`.",
        )

    norm_read: list[str] = []
    if readable_paths:
        for rp_item in readable_paths:
            nr = normalize_child_scope_path(workspace_root, rp_item, allow_wildcard=True)
            if (
                nr != "*"
                and parent_allowed_scopes is not None
                and not _cd_scope_is_covered_by(nr, parent_allowed_scopes)
            ):
                raise DelegationAuthorityViolationError(
                    "child_scope_exceeds_parent",
                    f"Child readable path `{nr}` exceeds parent's allowed resource scopes.",
                )
            if nr not in norm_read:
                norm_read.append(nr)
    else:
        norm_read = ["*"] if parent_allowed_scopes is None else [
            normalize_child_scope_path(workspace_root, wp, allow_wildcard=False) for wp in norm_write
        ] or ["*"]

    return ChildAuthoritySubset(
        parent_policy=parent_policy,
        policy=eff_policy,
        allowed_tools=tuple(dedup_tools),
        capabilities=tuple(sorted(caps)),
        readable_paths=tuple(norm_read),
        writable_paths=tuple(norm_write),
        delegated_grant_ids=tuple(str(g) for g in delegated_grant_ids if str(g).strip()),
        max_depth=1,
    )


def enforce_child_tool_authority(
    authority: ChildAuthoritySubset,
    tool_name: str,
    args: Mapping[str, Any],
    *,
    workspace_root: Path,
    live_parent_policy: str,
    live_parent_revoked_capabilities: Collection[str] = (),
) -> tuple[bool, str, str]:
    """Enforce child authority subset AND live parent permission ceiling at tool execution time."""
    if live_parent_policy not in CD_POLICY_RANK:
        return False, "child_invalid_parent_policy", f"Unknown live parent policy `{live_parent_policy}`."
    if CD_POLICY_RANK[authority.policy] > CD_POLICY_RANK[live_parent_policy]:
        return (
            False,
            "child_parent_policy_downgraded",
            f"Parent policy downgraded to `{live_parent_policy}`; child `{authority.policy}` authority revoked.",
        )
    if live_parent_policy == "plan" and tool_name not in CD_READ_ONLY_TOOLS:
        return (
            False,
            "child_parent_policy_downgraded",
            f"Parent is now `plan`; child cannot execute `{tool_name}`.",
        )
    if tool_name in CD_FORBIDDEN_CHILD_TOOLS:
        return (
            False,
            "child_recursive_delegation_forbidden",
            "Child subagent is forbidden from invoking `delegate_subagent`.",
        )
    if tool_name not in authority.allowed_tools:
        return (
            False,
            "child_tool_not_in_subset",
            f"Tool `{tool_name}` is outside child's delegated `allowed_tools` {list(authority.allowed_tools)}.",
        )
    req_caps = _cd_infer_tool_capabilities(tool_name)
    revoked_hit = set(live_parent_revoked_capabilities).intersection(req_caps)
    if revoked_hit:
        return (
            False,
            "child_capability_revoked_on_parent",
            f"Capability {sorted(revoked_hit)} was revoked on parent/session.",
        )
    if not set(req_caps).issubset(set(authority.capabilities)):
        return (
            False,
            "child_capability_not_in_subset",
            f"Tool `{tool_name}` requires {list(req_caps)} outside child capabilities {list(authority.capabilities)}.",
        )

    if tool_name in CD_MUTATING_FILE_TOOLS:
        raw_p = str(args.get("path") or "").strip()
        if not raw_p and tool_name == "rollback_edits":
            return True, "", ""
        try:
            norm_p = normalize_child_scope_path(workspace_root, raw_p, allow_wildcard=False)
        except DelegationAuthorityViolationError as exc:
            return False, exc.code, exc.reason
        if not any(child_scopes_overlap(norm_p, w_sc) and _cd_scope_is_covered_by(norm_p, (w_sc,)) for w_sc in authority.writable_paths):
            return (
                False,
                "child_write_scope_exceeded",
                f"Child cannot mutate `{norm_p}`; permitted `writable_paths` = {list(authority.writable_paths)}.",
            )

    if tool_name in {"read_file", "list_files", "grep_search"} and "*" not in authority.readable_paths:
        raw_rp = str(args.get("path") or ".").strip()
        if raw_rp in {"", "."}:
            return (
                False,
                "child_read_scope_exceeded",
                f"Whole-workspace read `{raw_rp}` exceeds restricted child `readable_paths` {list(authority.readable_paths)}.",
            )
        try:
            norm_rp = normalize_child_scope_path(workspace_root, raw_rp, allow_wildcard=False)
        except DelegationAuthorityViolationError as exc:
            return False, exc.code, exc.reason
        combined_read = tuple(authority.readable_paths) + tuple(authority.writable_paths)
        if not _cd_scope_is_covered_by(norm_rp, combined_read):
            return (
                False,
                "child_read_scope_exceeded",
                f"Child cannot read `{norm_rp}`; permitted `readable_paths` = {list(combined_read)}.",
            )

    return True, "", ""


@dataclass(frozen=True, slots=True)
class ChildBudgetAllocation:
    """Bounded child task budget carved strictly within parent's remaining unreserved budget (Task 1)."""

    spec: execution_budget.TaskBudgetSpec
    parent_remaining_steps_at_spawn: int
    parent_remaining_tool_calls_at_spawn: int
    parent_remaining_tokens_at_spawn: int
    parent_remaining_elapsed_s_at_spawn: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "parent_remaining_steps_at_spawn": self.parent_remaining_steps_at_spawn,
            "parent_remaining_tool_calls_at_spawn": self.parent_remaining_tool_calls_at_spawn,
            "parent_remaining_tokens_at_spawn": self.parent_remaining_tokens_at_spawn,
            "parent_remaining_elapsed_s_at_spawn": self.parent_remaining_elapsed_s_at_spawn,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ChildBudgetAllocation:
        return cls(
            spec=execution_budget.TaskBudgetSpec.from_dict(data["spec"]),
            parent_remaining_steps_at_spawn=int(data["parent_remaining_steps_at_spawn"]),
            parent_remaining_tool_calls_at_spawn=int(data["parent_remaining_tool_calls_at_spawn"]),
            parent_remaining_tokens_at_spawn=int(data["parent_remaining_tokens_at_spawn"]),
            parent_remaining_elapsed_s_at_spawn=float(data["parent_remaining_elapsed_s_at_spawn"]),
        )


def derive_child_budget_allocation(
    parent_budget: execution_budget.TaskBudgetState,
    *,
    reserved_by_active_children: Mapping[str, float] | None = None,
    requested: Mapping[str, Any] | None = None,
) -> ChildBudgetAllocation:
    """Derive a child ``TaskBudgetSpec`` strictly bounded by parent's unreserved remaining budget."""
    viol = parent_budget.violation_reason(check_recovery=False)
    if viol:
        raise DelegationRejectedError(
            "parent_budget_exhausted",
            f"Parent task budget is already exhausted ({viol}); cannot delegate child subagent.",
        )
    res = reserved_by_active_children or {}
    avail_steps = parent_budget.spec.max_steps - parent_budget.steps_used - int(res.get("steps", 0))
    avail_tools = parent_budget.spec.max_tool_calls - parent_budget.tool_calls_used - int(res.get("tool_calls", 0))
    avail_tokens = parent_budget.spec.max_tokens - parent_budget.tokens_used - int(res.get("tokens", 0))
    avail_elapsed = round(
        parent_budget.spec.max_elapsed_s - parent_budget.elapsed_s - float(res.get("elapsed_s", 0.0)),
        6,
    )
    avail_recov = max(
        0,
        parent_budget.spec.max_recoveries - parent_budget.recoveries_used - int(res.get("recoveries", 0)),
    )

    if avail_steps < 1 or avail_tools < 1 or avail_tokens < 1 or avail_elapsed <= 0.0:
        raise DelegationRejectedError(
            "child_budget_exceeds_parent",
            "Insufficient unreserved parent budget remaining to spawn a child subagent "
            f"(steps={avail_steps}, tool_calls={avail_tools}, tokens={avail_tokens}, elapsed_s={avail_elapsed}).",
        )

    req = requested or {}

    def _pick_int(key: str, default_cap: int, avail: int) -> int:
        if key in req and req[key] is not None:
            val = int(req[key])
            if val < 1:
                raise DelegationRejectedError("invalid_child_budget", f"Child `{key}` must be >= 1.")
            if val > avail:
                raise DelegationRejectedError(
                    "child_budget_exceeds_parent",
                    f"Requested child `{key}={val}` exceeds parent's unreserved remaining `{avail}`.",
                )
            return val
        return max(1, min(default_cap, avail))

    max_steps = _pick_int("max_steps", 4, avail_steps)
    max_tools = _pick_int("max_tool_calls", 12, avail_tools)
    max_tokens = _pick_int("max_tokens", 24_000, avail_tokens)

    if "max_elapsed_s" in req and req["max_elapsed_s"] is not None:
        max_elapsed = float(req["max_elapsed_s"])
        if not math.isfinite(max_elapsed) or max_elapsed <= 0.0:
            raise DelegationRejectedError("invalid_child_budget", "Child `max_elapsed_s` must be > 0.")
        if max_elapsed > avail_elapsed + 1e-6:
            raise DelegationRejectedError(
                "child_budget_exceeds_parent",
                f"Requested child `max_elapsed_s={max_elapsed}` exceeds parent's remaining `{avail_elapsed}`.",
            )
        max_elapsed = min(max_elapsed, avail_elapsed)
    else:
        max_elapsed = min(120.0, avail_elapsed)

    if "max_recoveries" in req and req["max_recoveries"] is not None:
        max_recov = int(req["max_recoveries"])
        if max_recov < 0 or max_recov > avail_recov:
            raise DelegationRejectedError(
                "child_budget_exceeds_parent",
                f"Requested child `max_recoveries={max_recov}` exceeds parent's remaining `{avail_recov}`.",
            )
    else:
        max_recov = 0

    return ChildBudgetAllocation(
        spec=execution_budget.TaskBudgetSpec(
            max_steps=max_steps,
            max_tool_calls=max_tools,
            max_tokens=max_tokens,
            max_elapsed_s=max_elapsed,
            max_recoveries=max_recov,
        ),
        parent_remaining_steps_at_spawn=avail_steps,
        parent_remaining_tool_calls_at_spawn=avail_tools,
        parent_remaining_tokens_at_spawn=avail_tokens,
        parent_remaining_elapsed_s_at_spawn=avail_elapsed,
    )


def evaluate_delegation_justification(
    *,
    role: str,
    objective: str,
    parent_budget: execution_budget.TaskBudgetState,
    writable_paths: Sequence[str] = (),
    context_files: Sequence[str] = (),
    parent_uncommitted_paths: Collection[str] = (),
    benchmark_prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate whether subagent overhead is justified over single-agent execution (Objective, Task 4)."""
    clean_obj = " ".join(str(objective or "").split())
    if len(clean_obj) < 8:
        raise DelegationRejectedError(
            "delegation_overhead_unjustified",
            "Delegated task objective is too trivial (< 8 chars) to justify subagent overhead.",
        )
    rem_steps = parent_budget.spec.max_steps - parent_budget.steps_used
    rem_tokens = parent_budget.spec.max_tokens - parent_budget.tokens_used
    if rem_steps < 2 or rem_tokens < 256:
        raise DelegationRejectedError(
            "delegation_overhead_unjustified",
            f"Parent remaining budget (steps={rem_steps}, tokens={rem_tokens}) is too small for delegation + join.",
        )
    for wp in writable_paths:
        for p_unc in parent_uncommitted_paths:
            if child_scopes_overlap(wp, str(p_unc)):
                raise DelegationRejectedError(
                    "child_conflicts_with_parent_edits",
                    f"Child writable scope `{wp}` overlaps parent's active edit `{p_unc}`; keep single-agent.",
                )

    reasons: list[str] = []
    if benchmark_prior is not None:
        deltas = benchmark_prior.get("multi_agent_minus_single_model")
        if isinstance(deltas, Mapping):
            sr_delta = float(deltas.get("success_rate", 0.0))
            lat_delta = float(deltas.get("latency_ms", 0.0))
            cost_delta = float(deltas.get("cost", 0.0))
            if sr_delta < 0.0:
                raise DelegationRejectedError(
                    "delegation_degrades_verified_outcomes",
                    f"Measured benchmark shows delegation degrades verified success rate ({sr_delta:+.3f}); "
                    "refusing subagent spawn.",
                )
            pareto_improves_tie = (
                sr_delta == 0.0
                and cost_delta <= 0.0
                and lat_delta <= 0.0
                and (cost_delta < 0.0 or lat_delta < 0.0)
            )
            if (
                role != "critic"
                and sr_delta == 0.0
                and (
                    benchmark_prior.get("delegation_overhead_justified") is False
                    or not pareto_improves_tie
                )
            ):
                raise DelegationRejectedError(
                    "delegation_overhead_unjustified",
                    f"Measured benchmark shows zero verified accuracy gain without Pareto improvement "
                    f"(latency {lat_delta:+.1f}ms, cost {cost_delta:+.2f}); execute inline in single-agent mode.",
                )
            reasons.append(f"measured_benchmark_success_delta:{sr_delta:+.3f}")

    if role == "critic":
        reasons.append(f"fresh_context_audit(files={len(context_files)})")
    elif role == "verifier":
        reasons.append("isolated_verification_pass")
    else:
        reasons.append(f"bounded_scoped_mutation(paths={len(writable_paths)})")

    return {
        "justified": True,
        "role": role,
        "reasons": reasons,
    }


@dataclass(frozen=True, slots=True)
class ChildJoinOutcome:
    """Durable parent-join record capturing child consumption, action reconciliation, and mutation fate."""

    joined_at: float
    join_disposition: ChildJoinDisposition
    verified: bool
    verification_summary: str
    consumed_steps: int
    consumed_tool_calls: int
    consumed_tokens: int
    consumed_elapsed_s: float
    committed_action_ids: tuple[str, ...] = ()
    unresolved_action_ids: tuple[str, ...] = ()
    modified_paths: tuple[str, ...] = ()
    rolled_back_paths: tuple[str, ...] = ()
    parent_budget_debited: bool = True
    output_sha256: str = ""

    def __post_init__(self) -> None:
        if self.unresolved_action_ids:
            raise DelegationRejectedError(
                "child_unresolved_consequential_actions",
                f"Cannot join child with unresolved consequential actions: {list(self.unresolved_action_ids)}.",
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "joined_at": self.joined_at,
            "join_disposition": self.join_disposition,
            "verified": self.verified,
            "verification_summary": self.verification_summary,
            "consumed_steps": self.consumed_steps,
            "consumed_tool_calls": self.consumed_tool_calls,
            "consumed_tokens": self.consumed_tokens,
            "consumed_elapsed_s": self.consumed_elapsed_s,
            "committed_action_ids": list(self.committed_action_ids),
            "unresolved_action_ids": list(self.unresolved_action_ids),
            "modified_paths": list(self.modified_paths),
            "rolled_back_paths": list(self.rolled_back_paths),
            "parent_budget_debited": self.parent_budget_debited,
            "output_sha256": self.output_sha256,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ChildJoinOutcome:
        return cls(
            joined_at=float(data["joined_at"]),
            join_disposition=data["join_disposition"],
            verified=bool(data["verified"]),
            verification_summary=str(data.get("verification_summary", "")),
            consumed_steps=int(data.get("consumed_steps", 0)),
            consumed_tool_calls=int(data.get("consumed_tool_calls", 0)),
            consumed_tokens=int(data.get("consumed_tokens", 0)),
            consumed_elapsed_s=float(data.get("consumed_elapsed_s", 0.0)),
            committed_action_ids=tuple(str(x) for x in data.get("committed_action_ids", ())),
            unresolved_action_ids=tuple(str(x) for x in data.get("unresolved_action_ids", ())),
            modified_paths=tuple(str(x) for x in data.get("modified_paths", ())),
            rolled_back_paths=tuple(str(x) for x in data.get("rolled_back_paths", ())),
            parent_budget_debited=bool(data.get("parent_budget_debited", True)),
            output_sha256=str(data.get("output_sha256", "")),
        )


@dataclass(frozen=True, slots=True)
class ChildDelegationRecord:
    """Full crash-durable record of one delegated child subagent and its join lifecycle."""

    delegation_id: str
    child_task_id: str
    parent_task_id: str
    session_id: str
    parent_epoch_at_spawn: int
    child_epoch: int
    child_worker_id: str
    role: ChildRole
    objective: str
    model_spec: str
    status: ChildDelegationStatus
    authority: ChildAuthoritySubset
    budget_allocation: ChildBudgetAllocation
    justification: dict[str, Any]
    base_file_versions: dict[str, str | None]
    post_file_versions: dict[str, str | None]
    edit_batch_ids: tuple[str, ...] = ()
    output_excerpt: str = ""
    failure_reason: str = ""
    join_outcome: ChildJoinOutcome | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_joined(self) -> bool:
        return self.status in {"joined", "joined_discarded"}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "child_task_id": self.child_task_id,
            "parent_task_id": self.parent_task_id,
            "session_id": self.session_id,
            "parent_epoch_at_spawn": self.parent_epoch_at_spawn,
            "child_epoch": self.child_epoch,
            "child_worker_id": self.child_worker_id,
            "role": self.role,
            "objective": self.objective,
            "model_spec": self.model_spec,
            "status": self.status,
            "authority": self.authority.to_mapping(),
            "budget_allocation": self.budget_allocation.to_mapping(),
            "justification": dict(self.justification),
            "base_file_versions": dict(self.base_file_versions),
            "post_file_versions": dict(self.post_file_versions),
            "edit_batch_ids": list(self.edit_batch_ids),
            "output_excerpt": self.output_excerpt,
            "failure_reason": self.failure_reason,
            "join_outcome": self.join_outcome.to_mapping() if self.join_outcome is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ChildDelegationRecord:
        jo_raw = data.get("join_outcome")
        return cls(
            delegation_id=str(data["delegation_id"]),
            child_task_id=str(data["child_task_id"]),
            parent_task_id=str(data["parent_task_id"]),
            session_id=str(data["session_id"]),
            parent_epoch_at_spawn=int(data.get("parent_epoch_at_spawn", 0)),
            child_epoch=int(data.get("child_epoch", 0)),
            child_worker_id=str(data.get("child_worker_id", "")),
            role=data["role"],
            objective=str(data.get("objective", "")),
            model_spec=str(data.get("model_spec", "")),
            status=data["status"],
            authority=ChildAuthoritySubset.from_mapping(data["authority"]),
            budget_allocation=ChildBudgetAllocation.from_mapping(data["budget_allocation"]),
            justification=dict(data.get("justification", {})),
            base_file_versions={str(k): (str(v) if v is not None else None) for k, v in dict(data.get("base_file_versions", {})).items()},
            post_file_versions={str(k): (str(v) if v is not None else None) for k, v in dict(data.get("post_file_versions", {})).items()},
            edit_batch_ids=tuple(str(x) for x in data.get("edit_batch_ids", ())),
            output_excerpt=str(data.get("output_excerpt", "")),
            failure_reason=str(data.get("failure_reason", "")),
            join_outcome=ChildJoinOutcome.from_mapping(jo_raw) if isinstance(jo_raw, Mapping) else None,
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
        )


def _cd_reject_if_symlink(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DelegationStoreIntegrityError(f"Cannot stat {label} `{path}`: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise DelegationStoreIntegrityError(f"Refusing symlinked {label}: `{path}`.")


class ChildDelegationStore:
    """Crash-durable JSON sidecar store at ``<store_dir>/delegations/state.json``."""

    def __init__(self, store_root: Path) -> None:
        self._root = Path(store_root)
        self._dir = self._root / "delegations"
        self._state_path = self._dir / "state.json"
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _ensure_dir_unlocked(self) -> None:
        _cd_reject_if_symlink(self._root, "store root")
        self._root.mkdir(parents=True, exist_ok=True)
        _cd_reject_if_symlink(self._dir, "delegations state directory")
        if not self._dir.exists():
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._dir.is_dir():
            raise DelegationStoreIntegrityError(f"Delegation state directory `{self._dir}` is not a directory.")
        os.chmod(self._dir, 0o700)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": CD_SCHEMA_VERSION,
            "delegations": {},
            "benchmarks": {},
        }

    def _read_state_unlocked(self) -> dict[str, Any]:
        _cd_reject_if_symlink(self._root, "store root")
        _cd_reject_if_symlink(self._dir, "delegations state directory")
        _cd_reject_if_symlink(self._state_path, "delegations state file")
        if not self._state_path.exists():
            return self._empty_state()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._state_path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise DelegationStoreIntegrityError(f"Refusing symlinked `{self._state_path}`.") from exc
            raise DelegationStoreIntegrityError(f"Cannot open `{self._state_path}`: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise DelegationStoreIntegrityError(f"`{self._state_path}` is not a regular file.")
            raw = os.read(fd, st.st_size + 1)
        finally:
            os.close(fd)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise DelegationStoreIntegrityError(f"Corrupt JSON in `{self._state_path}`: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DelegationStoreIntegrityError(f"`{self._state_path}` root must be a JSON object.")
        ver = parsed.get("schema_version")
        if not isinstance(ver, int) or isinstance(ver, bool) or ver != CD_SCHEMA_VERSION:
            raise DelegationStoreIntegrityError(
                f"Unsupported delegations schema_version {ver!r} (expected {CD_SCHEMA_VERSION})."
            )
        delegations = parsed.get("delegations")
        benchmarks = parsed.get("benchmarks")
        if not isinstance(delegations, dict) or not isinstance(benchmarks, dict):
            raise DelegationStoreIntegrityError(f"Malformed collections in `{self._state_path}`.")
        return {
            "schema_version": CD_SCHEMA_VERSION,
            "delegations": dict(delegations),
            "benchmarks": dict(benchmarks),
        }

    def _write_state_unlocked(self, state: Mapping[str, Any]) -> None:
        self._ensure_dir_unlocked()
        _cd_reject_if_symlink(self._state_path, "delegations state file")
        encoded = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        nonce = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
        tmp_path = self._dir / f".state.json.tmp.{nonce}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        os.close(fd)
        try:
            os.replace(tmp_path, self._state_path)
            os.chmod(self._state_path, 0o600)
            dir_fd = os.open(self._dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def reserved_budget_for_parent(self, parent_task_id: str) -> dict[str, float]:
        """Return summed budget reservations across currently active (`running`) children."""
        with self._lock:
            state = self._read_state_unlocked()
            totals = {"steps": 0.0, "tool_calls": 0.0, "tokens": 0.0, "elapsed_s": 0.0, "recoveries": 0.0}
            for raw in state["delegations"].values():
                rec = ChildDelegationRecord.from_mapping(raw)
                if rec.parent_task_id == parent_task_id and rec.status == "running":
                    totals["steps"] += rec.budget_allocation.spec.max_steps
                    totals["tool_calls"] += rec.budget_allocation.spec.max_tool_calls
                    totals["tokens"] += rec.budget_allocation.spec.max_tokens
                    totals["elapsed_s"] += rec.budget_allocation.spec.max_elapsed_s
                    totals["recoveries"] += rec.budget_allocation.spec.max_recoveries
            return totals

    def register_spawn(self, record: ChildDelegationRecord) -> ChildDelegationRecord:
        """Atomically verify disjoint write scopes against unjoined siblings and persist spawn."""
        with self._lock:
            state = self._read_state_unlocked()
            new_writes = record.authority.writable_paths
            new_exec = "code_execution" in record.authority.capabilities
            if new_writes or new_exec:
                for raw in state["delegations"].values():
                    existing = ChildDelegationRecord.from_mapping(raw)
                    if existing.session_id != record.session_id or existing.is_joined:
                        continue
                    ex_writes = existing.authority.writable_paths
                    ex_exec = "code_execution" in existing.authority.capabilities
                    if (new_exec and (ex_exec or ex_writes)) or (ex_exec and new_writes):
                        raise DelegationRejectedError(
                            "child_conflicting_write_scope",
                            f"Active unjoined child `{existing.delegation_id}` holds code-execution/write lock "
                            "in the same workspace.",
                        )
                    for nw in new_writes:
                        for ew in ex_writes:
                            if child_scopes_overlap(nw, ew):
                                raise DelegationRejectedError(
                                    "child_conflicting_write_scope",
                                    f"Child writable scope `{nw}` conflicts with unjoined sibling "
                                    f"`{existing.delegation_id}` scope `{ew}`.",
                                )
            now_ts = record.created_at or time.time()
            stored = replace(record, created_at=now_ts, updated_at=now_ts)
            state["delegations"][stored.delegation_id] = stored.to_mapping()
            self._write_state_unlocked(state)
            return stored

    def check_parent_mutation_conflict(
        self,
        session_id: str,
        rel_path: str,
    ) -> ChildDelegationRecord | None:
        """Return an unjoined child record if ``rel_path`` overlaps its leased writable scope."""
        clean_rel = rel_path.strip().replace("\\", "/").lstrip("/")
        with self._lock:
            state = self._read_state_unlocked()
            for raw in state["delegations"].values():
                rec = ChildDelegationRecord.from_mapping(raw)
                if rec.session_id != session_id or rec.is_joined:
                    continue
                if "code_execution" in rec.authority.capabilities:
                    return rec
                for wp in rec.authority.writable_paths:
                    if child_scopes_overlap(clean_rel, wp):
                        return rec
        return None

    def get_delegation(self, delegation_id: str) -> ChildDelegationRecord | None:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["delegations"].get(delegation_id)
            return ChildDelegationRecord.from_mapping(raw) if isinstance(raw, Mapping) else None

    def get_by_child_task_id(self, child_task_id: str) -> ChildDelegationRecord | None:
        with self._lock:
            state = self._read_state_unlocked()
            for raw in state["delegations"].values():
                rec = ChildDelegationRecord.from_mapping(raw)
                if rec.child_task_id == child_task_id:
                    return rec
            return None

    def list_delegations(
        self,
        *,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        unjoined_only: bool = False,
    ) -> tuple[ChildDelegationRecord, ...]:
        with self._lock:
            state = self._read_state_unlocked()
            items: list[ChildDelegationRecord] = []
            for raw in state["delegations"].values():
                rec = ChildDelegationRecord.from_mapping(raw)
                if session_id is not None and rec.session_id != session_id:
                    continue
                if parent_task_id is not None and rec.parent_task_id != parent_task_id:
                    continue
                if unjoined_only and rec.is_joined:
                    continue
                items.append(rec)
            items.sort(key=lambda r: (r.created_at, r.delegation_id))
            return tuple(items)

    def update_child_outcome(
        self,
        delegation_id: str,
        *,
        status: ChildDelegationStatus,
        post_file_versions: Mapping[str, str | None] | None = None,
        edit_batch_ids: Sequence[str] | None = None,
        output_excerpt: str | None = None,
        failure_reason: str | None = None,
        now: float | None = None,
    ) -> ChildDelegationRecord:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["delegations"].get(delegation_id)
            if not isinstance(raw, Mapping):
                raise DelegationRejectedError("delegation_not_found", f"Delegation `{delegation_id}` does not exist.")
            cur = ChildDelegationRecord.from_mapping(raw)
            updated = replace(
                cur,
                status=status,
                post_file_versions=(
                    {str(k): (str(v) if v is not None else None) for k, v in post_file_versions.items()}
                    if post_file_versions is not None
                    else cur.post_file_versions
                ),
                edit_batch_ids=(tuple(str(x) for x in edit_batch_ids) if edit_batch_ids is not None else cur.edit_batch_ids),
                output_excerpt=(str(output_excerpt)[:4000] if output_excerpt is not None else cur.output_excerpt),
                failure_reason=(str(failure_reason)[:1000] if failure_reason is not None else cur.failure_reason),
                updated_at=float(now if now is not None else time.time()),
            )
            state["delegations"][delegation_id] = updated.to_mapping()
            self._write_state_unlocked(state)
            return updated

    def mark_joined(
        self,
        delegation_id: str,
        join_outcome: ChildJoinOutcome,
        *,
        discarded: bool = False,
        now: float | None = None,
    ) -> ChildDelegationRecord:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["delegations"].get(delegation_id)
            if not isinstance(raw, Mapping):
                raise DelegationRejectedError("delegation_not_found", f"Delegation `{delegation_id}` does not exist.")
            cur = ChildDelegationRecord.from_mapping(raw)
            if cur.is_joined:
                return cur
            updated = replace(
                cur,
                status=("joined_discarded" if discarded else "joined"),
                join_outcome=join_outcome,
                updated_at=float(now if now is not None else time.time()),
            )
            state["delegations"][delegation_id] = updated.to_mapping()
            self._write_state_unlocked(state)
            return updated

    def mark_cancelled_by_parent(
        self,
        parent_task_id: str,
        *,
        reason: str = "parent_cancelled",
        now: float | None = None,
    ) -> tuple[ChildDelegationRecord, ...]:
        """Propagate parent cancel/fence to all running children in durable delegation state."""
        ts = float(now if now is not None else time.time())
        affected: list[ChildDelegationRecord] = []
        with self._lock:
            state = self._read_state_unlocked()
            changed = False
            for did, raw in list(state["delegations"].items()):
                rec = ChildDelegationRecord.from_mapping(raw)
                if rec.parent_task_id == parent_task_id and rec.status == "running":
                    upd = replace(
                        rec,
                        status="cancelled_unjoined",
                        failure_reason=str(reason)[:1000],
                        updated_at=ts,
                    )
                    state["delegations"][did] = upd.to_mapping()
                    affected.append(upd)
                    changed = True
            if changed:
                self._write_state_unlocked(state)
        return tuple(affected)

    def save_benchmark_summary(self, category: str, summary: Mapping[str, Any]) -> None:
        cat = _cd_clean_id(category, "benchmark_category")
        with self._lock:
            state = self._read_state_unlocked()
            state["benchmarks"][cat] = dict(summary)
            self._write_state_unlocked(state)

    def get_benchmark_summary(self, category: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["benchmarks"].get(category)
            return dict(raw) if isinstance(raw, Mapping) else None

    def purge_session(self, session_id: str) -> int:
        with self._lock:
            state = self._read_state_unlocked()
            to_del = [
                did
                for did, raw in state["delegations"].items()
                if isinstance(raw, Mapping) and str(raw.get("session_id")) == session_id
            ]
            if not to_del:
                return 0
            for did in to_del:
                del state["delegations"][did]
            self._write_state_unlocked(state)
            return len(to_del)


@dataclass(frozen=True, slots=True)
class DelegationEvalRun:
    """One measured single-agent or bounded-delegation run for deterministic comparison (Task 4)."""

    run_id: str
    strategy: Literal["single_model", "multi_agent"]
    scenario_id: str
    task_digest: str
    config_digest: str
    hardware_digest: str
    outcome: Literal["completed", "failed", "cancelled"]
    verified_success: bool
    latency_ms: float
    token_cost: float
    tool_calls: int
    peak_memory_bytes: int

    def __post_init__(self) -> None:
        if self.strategy not in {"single_model", "multi_agent"}:
            raise ChildDelegationError(f"Invalid strategy {self.strategy!r}.")
        if self.outcome != "completed" and self.verified_success:
            raise ChildDelegationError("Failed or cancelled run cannot claim verified_success=True.")
        if self.latency_ms < 0 or self.token_cost < 0 or self.tool_calls < 0 or self.peak_memory_bytes < 0:
            raise ChildDelegationError("Benchmark metrics must be non-negative.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "scenario_id": self.scenario_id,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "hardware_digest": self.hardware_digest,
            "outcome": self.outcome,
            "verified_success": self.verified_success,
            "latency_ms": round(float(self.latency_ms), 6),
            "token_cost": round(float(self.token_cost), 6),
            "tool_calls": int(self.tool_calls),
            "peak_memory_bytes": int(self.peak_memory_bytes),
        }


def _cd_metric_stats(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "median": round(float(statistics.median(values)), 6),
        "spread": round(float(max(values) - min(values)), 6),
        "minimum": round(float(min(values)), 6),
        "maximum": round(float(max(values)), 6),
        "count": len(values),
    }


def _cd_summarize_strategy(
    strategy: Literal["single_model", "multi_agent"],
    runs: Sequence[DelegationEvalRun],
) -> dict[str, Any]:
    succ = sum(1 for r in runs if r.verified_success)
    return {
        "strategy": strategy,
        "run_count": len(runs),
        "successful_runs": succ,
        "success_rate": round(succ / len(runs), 6),
        "latency_ms": _cd_metric_stats([r.latency_ms for r in runs]),
        "cost": _cd_metric_stats([r.token_cost for r in runs]),
        "tool_calls": _cd_metric_stats([float(r.tool_calls) for r in runs]),
        "peak_memory_bytes": _cd_metric_stats([float(r.peak_memory_bytes) for r in runs]),
    }


def compare_delegation_runs(
    scenario_id: str,
    runs: Sequence[DelegationEvalRun],
) -> dict[str, Any]:
    """Compare matched ``single_model`` vs ``multi_agent`` runs and classify whether overhead is justified."""
    clean_scen = _cd_clean_id(scenario_id, "scenario_id")
    if not runs:
        raise ChildDelegationError("Benchmark runs must be non-empty.")
    seen_ids: set[str] = set()
    task_digests: set[str] = set()
    cfg_digests: set[str] = set()
    hw_digests: set[str] = set()
    for r in runs:
        if r.run_id in seen_ids:
            raise ChildDelegationError(f"Duplicate benchmark run_id {r.run_id!r}.")
        seen_ids.add(r.run_id)
        if r.scenario_id != clean_scen:
            raise ChildDelegationError(f"Run {r.run_id!r} scenario mismatch ({r.scenario_id!r} != {clean_scen!r}).")
        task_digests.add(r.task_digest)
        cfg_digests.add(r.config_digest)
        hw_digests.add(r.hardware_digest)
    if len(task_digests) != 1 or len(cfg_digests) != 1 or len(hw_digests) != 1:
        raise ChildDelegationError("Comparable runs must share identical task, config, and hardware digests.")

    single_runs = [r for r in runs if r.strategy == "single_model"]
    multi_runs = [r for r in runs if r.strategy == "multi_agent"]
    if not single_runs or not multi_runs:
        raise ChildDelegationError("Comparison requires at least one single_model and one multi_agent run.")

    s_sum = _cd_summarize_strategy("single_model", single_runs)
    m_sum = _cd_summarize_strategy("multi_agent", multi_runs)
    sr_delta = round(float(m_sum["success_rate"]) - float(s_sum["success_rate"]), 6)
    lat_delta = round(float(m_sum["latency_ms"]["median"]) - float(s_sum["latency_ms"]["median"]), 6)
    cost_delta = round(float(m_sum["cost"]["median"]) - float(s_sum["cost"]["median"]), 6)
    mem_delta = round(
        float(m_sum["peak_memory_bytes"]["median"]) - float(s_sum["peak_memory_bytes"]["median"]),
        6,
    )
    deltas = {
        "success_rate": sr_delta,
        "latency_ms": lat_delta,
        "cost": cost_delta,
        "peak_memory_bytes": mem_delta,
    }
    preserves_or_improves = sr_delta >= 0.0
    overhead_justified = bool(
        sr_delta > 0.0
        or (
            sr_delta == 0.0
            and cost_delta <= 0.0
            and lat_delta <= 0.0
            and (cost_delta < 0.0 or lat_delta < 0.0)
        )
    )
    payload: dict[str, Any] = {
        "schema_version": CD_SCHEMA_VERSION,
        "scenario_id": clean_scen,
        "task_digest": next(iter(task_digests)),
        "config_digest": next(iter(cfg_digests)),
        "hardware_digest": next(iter(hw_digests)),
        "single_model": s_sum,
        "multi_agent": m_sum,
        "multi_agent_minus_single_model": deltas,
        "preserves_or_improves_verified_outcomes": preserves_or_improves,
        "delegation_overhead_justified": overhead_justified,
        "runs": [r.to_mapping() for r in runs],
    }
    payload["report_sha256"] = _cd_canonical_sha256(payload)
    return payload

