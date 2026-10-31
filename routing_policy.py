"""Explicit privacy, network, billing, portable handoff, and conservative model switching (AS-025, M5-P4-02).

This module serves two tightly coupled responsibilities:
1. Privacy-, network-, and billing-aware fallback selection (:func:`evaluate_fallback`),
   where both network and billable model transitions default to deny unless policy and
   explicit per-request consent allow them.
2. Crash-durable portable model handoffs and conservative sequential switching
   (:class:`PortableHandoffCore`, :class:`HarnessPrivateExtensions`,
   :class:`HandoffRecord`, :class:`PortableHandoffStore`, and selective semantic
   confirmation helpers) for ``M5-P4-02``:
   - Strictly forbids KV-cache, prompt-cache, speculative-decoding, or provider-opaque
     state portability assumptions across models/providers (``AC 1``).
   - Separates model-agnostic ``PortableHandoffCore`` from Studio-authoritative
     ``HarnessPrivateExtensions`` (``Task 1``).
   - Validates workspace-confined file references, SHA-256 source versions, checkpoint
     hashes, routing consent, and scoped grant/permission validity while preserving
     task budgets and active session/task grants (``Task 2``, ``AC 2``).
   - Applies selective semantic confirmation on high-risk transitions (``Task 3``).
   - Tracks bounded switch retry budgets and persists ``failed_paused`` records so
     failed handoffs leave tasks in a safe paused state after worker fencing
     (``Task 4``, ``AC 3``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RP_HANDOFF_SCHEMA_VERSION = 1
RP_DEFAULT_MAX_SWITCH_ATTEMPTS = 3
RP_MAX_STORED_HANDOFFS = 256

HandoffStatus = Literal["staged", "committed", "failed_paused", "superseded"]
SwitchRiskLevel = Literal["low", "high"]

RP_ALLOWED_MESSAGE_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool"})

# Non-portable KV-cache, prompt-cache, speculative-draft, tokenizer, and provider-opaque
# fields that MUST NEVER be assumed portable across model or provider switches (AC 1).
RP_FORBIDDEN_NON_PORTABLE_KEYS: frozenset[str] = frozenset(
    {
        "kv_cache",
        "kv_cache_id",
        "kv_cache_path",
        "kv_cache_digest",
        "kv_state",
        "ssd_cache_ref",
        "prompt_cache_id",
        "prompt_cache_key",
        "cache_control",
        "cached_prefix_tokens",
        "previous_response_id",
        "thinking_signature",
        "thought_signature",
        "provider_opaque_state",
        "draft_model_state",
        "mtp_hidden_state",
        "token_ids",
        "logits_state",
    }
)


class RoutingPolicyError(ValueError):
    """Raised when a policy, consent, or handoff specification is malformed."""


class FallbackDenied(RuntimeError):
    """Raised by callers that require a selected fallback and received none."""


class HandoffStoreIntegrityError(RuntimeError):
    """Raised when the durable handoff sidecar store is corrupt, symlinked, or unsupported."""


class HandoffPortabilityViolationError(RoutingPolicyError):
    """Raised when a caller requests KV-cache or opaque-state portability across a switch."""


class HandoffRejectedError(RuntimeError):
    """Raised when conservative model switching fails closed into a safe paused/blocked state."""

    def __init__(
        self,
        code: str,
        reason: str,
        *,
        handoff: HandoffRecord | None = None,
        task_state: str = "paused",
    ) -> None:
        self.code = _non_empty(code, "code")
        self.reason = _non_empty(reason, "reason")
        self.handoff = handoff
        self.task_state = task_state if task_state in {"paused", "blocked"} else "paused"
        super().__init__(f"{self.code}: {self.reason}")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "task_state": self.task_state,
            "handoff": self.handoff.to_mapping() if self.handoff is not None else None,
        }


def _non_empty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RoutingPolicyError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


def _rp_clean_optional_text(value: object, field_name: str, *, max_len: int = 200_000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RoutingPolicyError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise RoutingPolicyError(f"{field_name} must not contain NUL characters.")
    return value[:max_len]


def _rp_canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# =============================================================================
# AS-025 Privacy, Network, and Billing Fallback Policy
# =============================================================================


@dataclass(frozen=True, slots=True)
class FallbackCandidate:
    """A candidate's externally observable network and billing requirements."""

    model_id: str
    provider: str
    network_required: bool = False
    billable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _non_empty(self.model_id, "model_id"))
        object.__setattr__(self, "provider", _non_empty(self.provider, "provider"))
        if not isinstance(self.network_required, bool) or not isinstance(self.billable, bool):
            raise RoutingPolicyError("network_required and billable must be booleans.")


@dataclass(frozen=True, slots=True)
class FallbackPolicy:
    """Operator policy; both network and billing defaults are deny."""

    allow_network_fallback: bool = False
    allow_billable_fallback: bool = False
    permitted_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allow_network_fallback, bool) or not isinstance(self.allow_billable_fallback, bool):
            raise RoutingPolicyError("fallback policy permissions must be booleans.")
        providers = tuple(sorted({_non_empty(provider, "permitted_providers") for provider in self.permitted_providers}))
        object.__setattr__(self, "permitted_providers", providers)


@dataclass(frozen=True, slots=True)
class FallbackConsent:
    """Per-request explicit consent, identified for audit and replay review."""

    consent_id: str | None = None
    network_approved: bool = False
    billable_approved: bool = False

    def __post_init__(self) -> None:
        if self.consent_id is not None:
            object.__setattr__(self, "consent_id", _non_empty(self.consent_id, "consent_id"))
        if not isinstance(self.network_approved, bool) or not isinstance(self.billable_approved, bool):
            raise RoutingPolicyError("consent approvals must be booleans.")
        if (self.network_approved or self.billable_approved) and self.consent_id is None:
            raise RoutingPolicyError("approved fallback consent requires consent_id.")


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """Policy result with eligible candidates and explicit exclusion reasons."""

    selected: FallbackCandidate | None
    eligible: tuple[FallbackCandidate, ...]
    exclusions: tuple[str, ...]

    def require_selection(self) -> FallbackCandidate:
        if self.selected is None:
            raise FallbackDenied("no fallback candidate satisfies policy and consent.")
        return self.selected


def _exclusion(candidate: FallbackCandidate, policy: FallbackPolicy, consent: FallbackConsent) -> str | None:
    if policy.permitted_providers and candidate.provider not in policy.permitted_providers:
        return f"{candidate.model_id}: provider is not permitted by policy"
    if candidate.network_required:
        if not policy.allow_network_fallback:
            return f"{candidate.model_id}: network fallback disabled by policy"
        if not consent.network_approved or consent.consent_id is None:
            return f"{candidate.model_id}: network fallback requires explicit consent"
    if candidate.billable:
        if not policy.allow_billable_fallback:
            return f"{candidate.model_id}: billable fallback disabled by policy"
        if not consent.billable_approved or consent.consent_id is None:
            return f"{candidate.model_id}: billable fallback requires explicit consent"
    return None


def evaluate_fallback(
    candidates: Sequence[FallbackCandidate],
    policy: FallbackPolicy | None = None,
    consent: FallbackConsent | None = None,
) -> FallbackDecision:
    """Select the first eligible candidate without performing any fallback."""
    active_policy = policy or FallbackPolicy()
    active_consent = consent or FallbackConsent()
    eligible: list[FallbackCandidate] = []
    exclusions: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, FallbackCandidate):
            raise TypeError("candidates must contain FallbackCandidate values.")
        reason = _exclusion(candidate, active_policy, active_consent)
        if reason is None:
            eligible.append(candidate)
        else:
            exclusions.append(reason)
    return FallbackDecision(eligible[0] if eligible else None, tuple(eligible), tuple(exclusions))


# =============================================================================
# M5-P4-02 Portable Handoff Core, Harness Extensions, and Conservative Switching
# =============================================================================


def assert_no_kv_cache_portability_request(options: Mapping[str, Any] | None) -> None:
    """Fail closed if a switch/handoff request asks to reuse KV cache or opaque model state."""
    if not options:
        return
    truthy_flags = (
        "assume_kv_cache_portable",
        "kv_cache_portable",
        "reuse_kv_cache",
        "transfer_kv_cache",
        "preserve_kv_cache",
        "reuse_prompt_cache",
    )
    for flag_name in truthy_flags:
        if bool(options.get(flag_name)):
            raise HandoffPortabilityViolationError(
                f"KV cache portability assumption (`{flag_name}=True`) is forbidden across model switches."
            )
    for forbidden_key in sorted(RP_FORBIDDEN_NON_PORTABLE_KEYS):
        val = options.get(forbidden_key)
        if val is not None and val != "" and val is not False and val != [] and val != {}:
            raise HandoffPortabilityViolationError(
                f"Non-portable model state field `{forbidden_key}` cannot be transferred across a model switch."
            )


def _rp_normalize_rel_path(raw_path: str, field_name: str = "path") -> str:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise HandoffRejectedError(
            "handoff_invalid_reference",
            f"{field_name} must be a non-empty relative path without NUL characters.",
        )
    cleaned = raw_path.strip().replace("\\", "/")
    p = Path(cleaned)
    if p.is_absolute():
        raise HandoffRejectedError(
            "handoff_invalid_reference",
            f"{field_name} must be relative to workspace_root, got {raw_path!r}.",
        )
    parts = [pt for pt in p.parts if pt not in ("", ".")]
    if not parts or any(pt == ".." for pt in parts):
        raise HandoffRejectedError(
            "handoff_invalid_reference",
            f"{field_name} must stay confined within workspace_root without '..': {raw_path!r}.",
        )
    return "/".join(parts)


def _rp_resolve_confined_file(workspace_root: Path, rel_path: str) -> tuple[str, Path]:
    if workspace_root.is_symlink():
        raise HandoffRejectedError(
            "handoff_invalid_reference",
            f"Workspace root {workspace_root} must not be a symlink.",
        )
    ws_resolved = workspace_root.resolve()
    norm = _rp_normalize_rel_path(rel_path)
    cursor = ws_resolved
    for part in Path(norm).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HandoffRejectedError(
                "handoff_invalid_reference",
                f"Symlink escape rejected in handoff file reference: {rel_path!r}.",
            )
    resolved = cursor.resolve()
    if resolved != ws_resolved and ws_resolved not in resolved.parents:
        raise HandoffRejectedError(
            "handoff_invalid_reference",
            f"Handoff file reference escapes workspace_root: {rel_path!r}.",
        )
    return norm, resolved


@dataclass(frozen=True, slots=True)
class PortableMessage:
    """Model-agnostic plain-text conversation turn with zero KV/provider-opaque state."""

    role: str
    content: str
    name: str = ""
    tool_call_id: str = ""

    def __post_init__(self) -> None:
        role_clean = _non_empty(self.role, "PortableMessage.role").lower()
        if role_clean not in RP_ALLOWED_MESSAGE_ROLES:
            raise RoutingPolicyError(
                f"PortableMessage.role must be one of {sorted(RP_ALLOWED_MESSAGE_ROLES)}, got {self.role!r}."
            )
        object.__setattr__(self, "role", role_clean)
        object.__setattr__(self, "content", _rp_clean_optional_text(self.content, "PortableMessage.content"))
        object.__setattr__(self, "name", _rp_clean_optional_text(self.name, "PortableMessage.name", max_len=200))
        object.__setattr__(
            self,
            "tool_call_id",
            _rp_clean_optional_text(self.tool_call_id, "PortableMessage.tool_call_id", max_len=200),
        )

    def approx_tokens(self) -> int:
        # Conservative token estimate (~1 token per 4 chars or whitespace word, whichever is larger)
        char_est = max(1, (len(self.content) + 3) // 4)
        word_est = max(1, len(self.content.split()))
        return max(char_est, word_est) + 4

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        return out

    def to_wire_message(self) -> dict[str, str]:
        out: dict[str, str] = {"role": self.role, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        return out

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PortableMessage:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("PortableMessage must be a JSON object.")
        for bad_key in RP_FORBIDDEN_NON_PORTABLE_KEYS:
            if bad_key in raw:
                raise HandoffStoreIntegrityError(
                    f"Persisted PortableMessage contains forbidden non-portable key `{bad_key}`."
                )
        return cls(
            role=str(raw.get("role") or ""),
            content=str(raw.get("content") or ""),
            name=str(raw.get("name") or ""),
            tool_call_id=str(raw.get("tool_call_id") or ""),
        )


def sanitize_messages_for_wire(raw_messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Strip all KV-cache, prompt-cache, and provider-opaque fields before sending on wire."""
    cleaned: list[dict[str, str]] = []
    for msg in raw_messages:
        if not isinstance(msg, Mapping):
            continue
        role = str(msg.get("role") or "user").strip() or "user"
        raw_content = msg.get("content")
        if isinstance(raw_content, list):
            parts: list[str] = []
            for block in raw_content:
                if isinstance(block, Mapping):
                    btype = str(block.get("type") or "")
                    if btype in {"thinking", "redacted_thinking"}:
                        continue
                    txt = block.get("text") or block.get("content")
                    if isinstance(txt, str):
                        parts.append(txt)
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
        else:
            content = str(raw_content or "")
        entry: dict[str, str] = {"role": role, "content": content.replace("\x00", "")}
        if isinstance(msg.get("name"), str) and msg["name"].strip():
            entry["name"] = msg["name"].strip()[:200]
        if isinstance(msg.get("tool_call_id"), str) and msg["tool_call_id"].strip():
            entry["tool_call_id"] = msg["tool_call_id"].strip()[:200]
        cleaned.append(entry)
    return cleaned


def sanitize_portable_messages(
    raw_messages: Sequence[Mapping[str, Any]],
    *,
    max_context_tokens: int | None = None,
) -> tuple[tuple[PortableMessage, ...], tuple[str, ...], int]:
    """Convert raw transcript turns into plain ``PortableMessage`` items with zero KV assumptions.

    Drops ``role == "system"`` framing (which Studio regenerates fresh for the target model),
    strips and records every ``RP_FORBIDDEN_NON_PORTABLE_KEYS`` attribute (including nested
    ``cache_control`` or ``thinking_signature`` blocks), and applies conservative newest-first
    context-window fitting while preserving the initial user goal turn when ``max_context_tokens``
    is supplied.
    """
    if max_context_tokens is not None:
        if isinstance(max_context_tokens, bool) or not isinstance(max_context_tokens, int) or max_context_tokens <= 0:
            raise RoutingPolicyError("max_context_tokens must be a positive integer when provided.")

    stripped_keys: set[str] = set()
    portable: list[PortableMessage] = []

    for msg in raw_messages:
        if not isinstance(msg, Mapping):
            raise TypeError("raw_messages must contain mapping items.")
        for key in msg.keys():
            key_str = str(key)
            if key_str in RP_FORBIDDEN_NON_PORTABLE_KEYS:
                stripped_keys.add(key_str)
        role_raw = str(msg.get("role") or "").strip().lower()
        if role_raw == "system":
            continue
        if role_raw not in RP_ALLOWED_MESSAGE_ROLES:
            stripped_keys.add(f"unsupported_role:{role_raw or 'empty'}")
            continue

        raw_content = msg.get("content")
        if isinstance(raw_content, list):
            text_chunks: list[str] = []
            for block in raw_content:
                if isinstance(block, Mapping):
                    for bkey in block.keys():
                        bkey_s = str(bkey)
                        if bkey_s in RP_FORBIDDEN_NON_PORTABLE_KEYS:
                            stripped_keys.add(bkey_s)
                    btype = str(block.get("type") or "")
                    if btype in {"thinking", "redacted_thinking"}:
                        stripped_keys.add(f"content_block:{btype}")
                        continue
                    btext = block.get("text") or block.get("content")
                    if isinstance(btext, str):
                        text_chunks.append(btext)
                elif isinstance(block, str):
                    text_chunks.append(block)
            content_str = "\n".join(text_chunks)
        else:
            content_str = str(raw_content or "")

        portable.append(
            PortableMessage(
                role=role_raw,
                content=content_str.replace("\x00", ""),
                name=str(msg.get("name") or "") if isinstance(msg.get("name"), str) else "",
                tool_call_id=str(msg.get("tool_call_id") or "") if isinstance(msg.get("tool_call_id"), str) else "",
            )
        )

    if max_context_tokens is None or not portable:
        return tuple(portable), tuple(sorted(stripped_keys)), 0

    anchor: PortableMessage | None = portable[0] if portable[0].role == "user" else None
    remaining_budget = max_context_tokens
    if anchor is not None:
        anchor_cost = anchor.approx_tokens()
        if anchor_cost > max_context_tokens:
            raise HandoffRejectedError(
                "handoff_context_budget_too_small",
                f"Target model context budget ({max_context_tokens} tokens) cannot fit anchored task objective "
                f"({anchor_cost} estimated tokens).",
            )
        remaining_budget -= anchor_cost
        tail_candidates = portable[1:]
    else:
        tail_candidates = portable

    kept_tail: list[PortableMessage] = []
    for item in reversed(tail_candidates):
        cost = item.approx_tokens()
        if cost > remaining_budget:
            break
        kept_tail.append(item)
        remaining_budget -= cost
    kept_tail.reverse()

    final_msgs = ([anchor] if anchor is not None else []) + kept_tail
    truncated = len(portable) - len(final_msgs)
    return tuple(final_msgs), tuple(sorted(stripped_keys)), truncated


@dataclass(frozen=True, slots=True)
class HandoffFileReference:
    """Workspace-confined file reference pinned to its observed SHA-256 content digest."""

    path: str
    sha256: str | None
    size_bytes: int = 0
    exists: bool = True

    def __post_init__(self) -> None:
        norm = _rp_normalize_rel_path(self.path, "HandoffFileReference.path")
        object.__setattr__(self, "path", norm)
        if not isinstance(self.exists, bool):
            raise RoutingPolicyError("HandoffFileReference.exists must be a boolean.")
        if self.exists:
            if not isinstance(self.sha256, str) or len(self.sha256.strip()) != 64:
                raise RoutingPolicyError(f"Existing HandoffFileReference {norm!r} requires a 64-char SHA-256 hex digest.")
            object.__setattr__(self, "sha256", self.sha256.strip().lower())
            if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
                raise RoutingPolicyError("HandoffFileReference.size_bytes must be a non-negative integer.")
        else:
            object.__setattr__(self, "sha256", None)
            object.__setattr__(self, "size_bytes", 0)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "exists": self.exists,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HandoffFileReference:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("HandoffFileReference must be a JSON object.")
        sha_raw = raw.get("sha256")
        return cls(
            path=str(raw.get("path") or ""),
            sha256=(str(sha_raw) if sha_raw is not None else None),
            size_bytes=int(raw.get("size_bytes") or 0),
            exists=bool(raw.get("exists", True)),
        )


def collect_and_validate_file_references(
    workspace_root: Path,
    referenced_paths: Sequence[str],
    *,
    expected_versions: Mapping[str, str | None] | None = None,
) -> tuple[HandoffFileReference, ...]:
    """Validate workspace confinement and SHA-256 source versions for handoff file references."""
    norm_expected: dict[str, str | None] = {}
    if expected_versions:
        for raw_k, exp_sha in expected_versions.items():
            nk = _rp_normalize_rel_path(str(raw_k), "expected_file_versions")
            if exp_sha is not None:
                clean_sha = str(exp_sha).strip().lower()
                if len(clean_sha) != 64 or any(ch not in "0123456789abcdef" for ch in clean_sha):
                    raise HandoffRejectedError(
                        "handoff_invalid_reference",
                        f"expected_file_versions[{nk!r}] must be None or a 64-char hex SHA-256 digest.",
                    )
                norm_expected[nk] = clean_sha
            else:
                norm_expected[nk] = None

    ordered_paths: list[str] = []
    seen: set[str] = set()
    for raw_p in referenced_paths:
        np = _rp_normalize_rel_path(str(raw_p), "referenced_files")
        if np not in seen:
            seen.add(np)
            ordered_paths.append(np)
    for nk in norm_expected:
        if nk not in seen:
            seen.add(nk)
            ordered_paths.append(nk)

    refs: list[HandoffFileReference] = []
    for rel_p in ordered_paths:
        norm_p, target = _rp_resolve_confined_file(workspace_root, rel_p)
        if not target.exists():
            if norm_p in norm_expected and norm_expected[norm_p] is not None:
                raise HandoffRejectedError(
                    "handoff_source_version_mismatch",
                    f"Referenced file `{norm_p}` is missing on disk (expected sha256={norm_expected[norm_p]!r}).",
                )
            if norm_p not in norm_expected:
                raise HandoffRejectedError(
                    "handoff_invalid_reference",
                    f"Referenced file `{norm_p}` does not exist in workspace.",
                )
            refs.append(HandoffFileReference(path=norm_p, sha256=None, size_bytes=0, exists=False))
            continue

        if not target.is_file():
            raise HandoffRejectedError(
                "handoff_invalid_reference",
                f"Referenced workspace path `{norm_p}` is not a regular file.",
            )
        data = target.read_bytes()
        live_sha = hashlib.sha256(data).hexdigest()
        if norm_p in norm_expected:
            exp = norm_expected[norm_p]
            if exp is None:
                raise HandoffRejectedError(
                    "handoff_source_version_mismatch",
                    f"Referenced file `{norm_p}` was expected to be absent but exists with sha256={live_sha}.",
                )
            if live_sha != exp:
                raise HandoffRejectedError(
                    "handoff_source_version_mismatch",
                    f"Source version drift on `{norm_p}`: expected sha256={exp}, observed sha256={live_sha}.",
                )
        refs.append(
            HandoffFileReference(
                path=norm_p,
                sha256=live_sha,
                size_bytes=len(data),
                exists=True,
            )
        )
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class HandoffGrantSnapshot:
    """Point-in-time snapshot of an active scoped grant preserved across a model switch."""

    grant_id: str
    capabilities: tuple[str, ...]
    resource_scopes: tuple[str, ...]
    expires_at: float
    max_uses: int | None
    uses_consumed: int
    session_policy_revision: int
    global_policy_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _non_empty(self.grant_id, "grant_id"))
        object.__setattr__(self, "capabilities", tuple(str(c) for c in self.capabilities))
        object.__setattr__(self, "resource_scopes", tuple(str(s) for s in self.resource_scopes))
        if not math.isfinite(self.expires_at):
            raise RoutingPolicyError("HandoffGrantSnapshot.expires_at must be finite.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "capabilities": list(self.capabilities),
            "resource_scopes": list(self.resource_scopes),
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "uses_consumed": self.uses_consumed,
            "session_policy_revision": self.session_policy_revision,
            "global_policy_revision": self.global_policy_revision,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HandoffGrantSnapshot:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("HandoffGrantSnapshot must be a JSON object.")
        max_u = raw.get("max_uses")
        return cls(
            grant_id=str(raw["grant_id"]),
            capabilities=tuple(str(c) for c in (raw.get("capabilities") or ())),
            resource_scopes=tuple(str(s) for s in (raw.get("resource_scopes") or ())),
            expires_at=float(raw["expires_at"]),
            max_uses=(int(max_u) if max_u is not None else None),
            uses_consumed=int(raw.get("uses_consumed") or 0),
            session_policy_revision=int(raw.get("session_policy_revision") or 1),
            global_policy_revision=int(raw.get("global_policy_revision") or 1),
        )


@dataclass(frozen=True, slots=True)
class PortableHandoffCore:
    """Model-agnostic portable handoff payload (Task 1, AC 1).

    Contains strictly plain UTF-8 text, structured milestone IDs, and SHA-256 content
    digests. ``kv_cache_portable`` is invariantly ``False``: KV caches, prompt-cache
    handles, speculative draft states, and provider-opaque signatures are never assumed
    portable across models or providers.
    """

    handoff_id: str
    session_id: str
    task_id: str
    source_lead_model: str
    target_lead_model: str
    source_critic_model: str
    target_critic_model: str
    kv_cache_portable: bool = False
    kv_cache_handling: str = "discarded_recomputed_from_text"
    stripped_non_portable_fields: tuple[str, ...] = ()
    objective: str = ""
    next_step: int = 1
    completed_milestone_ids: tuple[str, ...] = ()
    pending_milestone_ids: tuple[str, ...] = ()
    portable_messages: tuple[PortableMessage, ...] = ()
    truncated_messages: int = 0
    file_references: tuple[HandoffFileReference, ...] = ()
    checkpoint_id: str = ""
    checkpoint_payload_hash: str = ""
    evidence_refs: tuple[dict[str, Any], ...] = ()
    open_notes: str = ""
    core_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "handoff_id", _non_empty(self.handoff_id, "handoff_id"))
        object.__setattr__(self, "session_id", _non_empty(self.session_id, "session_id"))
        object.__setattr__(self, "task_id", _non_empty(self.task_id, "task_id"))
        object.__setattr__(self, "source_lead_model", _non_empty(self.source_lead_model, "source_lead_model"))
        object.__setattr__(self, "target_lead_model", _non_empty(self.target_lead_model, "target_lead_model"))
        object.__setattr__(self, "source_critic_model", _non_empty(self.source_critic_model, "source_critic_model"))
        object.__setattr__(self, "target_critic_model", _non_empty(self.target_critic_model, "target_critic_model"))
        if self.kv_cache_portable is not False:
            raise HandoffPortabilityViolationError(
                "PortableHandoffCore.kv_cache_portable must be False; KV cache portability is forbidden."
            )
        if isinstance(self.next_step, bool) or not isinstance(self.next_step, int) or self.next_step < 1:
            raise RoutingPolicyError("PortableHandoffCore.next_step must be an integer >= 1.")
        if isinstance(self.truncated_messages, bool) or not isinstance(self.truncated_messages, int) or self.truncated_messages < 0:
            raise RoutingPolicyError("PortableHandoffCore.truncated_messages must be >= 0.")
        object.__setattr__(self, "objective", _rp_clean_optional_text(self.objective, "objective", max_len=20_000))
        object.__setattr__(self, "open_notes", _rp_clean_optional_text(self.open_notes, "open_notes", max_len=20_000))
        object.__setattr__(
            self,
            "stripped_non_portable_fields",
            tuple(sorted({str(x) for x in self.stripped_non_portable_fields if str(x)})),
        )
        object.__setattr__(
            self,
            "completed_milestone_ids",
            tuple(str(x) for x in self.completed_milestone_ids if str(x)),
        )
        object.__setattr__(
            self,
            "pending_milestone_ids",
            tuple(str(x) for x in self.pending_milestone_ids if str(x)),
        )
        computed = _rp_canonical_sha256(self._canonical_body())
        if self.core_sha256:
            if self.core_sha256 != computed:
                raise HandoffStoreIntegrityError(
                    f"PortableHandoffCore hash mismatch on {self.handoff_id}: "
                    f"stored={self.core_sha256}, computed={computed}."
                )
        else:
            object.__setattr__(self, "core_sha256", computed)

    def _canonical_body(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "source_lead_model": self.source_lead_model,
            "target_lead_model": self.target_lead_model,
            "source_critic_model": self.source_critic_model,
            "target_critic_model": self.target_critic_model,
            "kv_cache_portable": False,
            "kv_cache_handling": self.kv_cache_handling,
            "stripped_non_portable_fields": list(self.stripped_non_portable_fields),
            "objective": self.objective,
            "next_step": self.next_step,
            "completed_milestone_ids": list(self.completed_milestone_ids),
            "pending_milestone_ids": list(self.pending_milestone_ids),
            "portable_messages": [m.to_mapping() for m in self.portable_messages],
            "truncated_messages": self.truncated_messages,
            "file_references": [r.to_mapping() for r in self.file_references],
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_payload_hash": self.checkpoint_payload_hash,
            "evidence_refs": [dict(e) for e in self.evidence_refs],
            "open_notes": self.open_notes,
        }

    def to_mapping(self) -> dict[str, Any]:
        body = self._canonical_body()
        body["core_sha256"] = self.core_sha256
        return body

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PortableHandoffCore:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("PortableHandoffCore must be a JSON object.")
        if raw.get("kv_cache_portable") is not False:
            raise HandoffStoreIntegrityError(
                "Persisted PortableHandoffCore violates AC 1 (`kv_cache_portable` must be False)."
            )
        raw_msgs = raw.get("portable_messages")
        if not isinstance(raw_msgs, list):
            raise HandoffStoreIntegrityError("PortableHandoffCore.portable_messages must be a list.")
        raw_refs = raw.get("file_references")
        if not isinstance(raw_refs, list):
            raise HandoffStoreIntegrityError("PortableHandoffCore.file_references must be a list.")
        raw_ev = raw.get("evidence_refs") or []
        if not isinstance(raw_ev, list):
            raise HandoffStoreIntegrityError("PortableHandoffCore.evidence_refs must be a list.")
        return cls(
            handoff_id=str(raw["handoff_id"]),
            session_id=str(raw["session_id"]),
            task_id=str(raw["task_id"]),
            source_lead_model=str(raw["source_lead_model"]),
            target_lead_model=str(raw["target_lead_model"]),
            source_critic_model=str(raw["source_critic_model"]),
            target_critic_model=str(raw["target_critic_model"]),
            kv_cache_portable=False,
            kv_cache_handling=str(raw.get("kv_cache_handling") or "discarded_recomputed_from_text"),
            stripped_non_portable_fields=tuple(str(x) for x in (raw.get("stripped_non_portable_fields") or ())),
            objective=str(raw.get("objective") or ""),
            next_step=int(raw.get("next_step") or 1),
            completed_milestone_ids=tuple(str(x) for x in (raw.get("completed_milestone_ids") or ())),
            pending_milestone_ids=tuple(str(x) for x in (raw.get("pending_milestone_ids") or ())),
            portable_messages=tuple(PortableMessage.from_mapping(m) for m in raw_msgs),
            truncated_messages=int(raw.get("truncated_messages") or 0),
            file_references=tuple(HandoffFileReference.from_mapping(r) for r in raw_refs),
            checkpoint_id=str(raw.get("checkpoint_id") or ""),
            checkpoint_payload_hash=str(raw.get("checkpoint_payload_hash") or ""),
            evidence_refs=tuple(dict(e) for e in raw_ev if isinstance(e, Mapping)),
            open_notes=str(raw.get("open_notes") or ""),
            core_sha256=str(raw.get("core_sha256") or ""),
        )


@dataclass(frozen=True, slots=True)
class HarnessPrivateExtensions:
    """Studio-authoritative private extensions persisted alongside PortableHandoffCore (Task 1).

    Holds lease fencing provenance, policy revisions, validated grant snapshots, task/switch
    budgets, routing consent, local heavy-model admission transition metadata, risk classification,
    and semantic-confirmation verification records. Models never author or override these fields.
    """

    fenced_epoch: int
    fenced_worker_ids: tuple[str, ...]
    policy: str
    session_policy_revision: int
    global_policy_revision: int
    validated_grants: tuple[HandoffGrantSnapshot, ...]
    revoked_epoch_grant_ids: tuple[str, ...]
    budget_snapshot: dict[str, Any]
    switch_attempts_used: int
    max_switch_attempts: int
    routing_decision: dict[str, Any] = field(default_factory=dict)
    admission_transition: dict[str, Any] = field(default_factory=dict)
    risk_level: SwitchRiskLevel = "low"
    risk_reasons: tuple[str, ...] = ()
    semantic_confirmation: dict[str, Any] = field(default_factory=dict)
    extensions_sha256: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.fenced_epoch, bool) or not isinstance(self.fenced_epoch, int) or self.fenced_epoch < 0:
            raise RoutingPolicyError("fenced_epoch must be a non-negative integer.")
        object.__setattr__(self, "policy", _non_empty(self.policy, "policy"))
        if self.risk_level not in {"low", "high"}:
            raise RoutingPolicyError(f"Invalid risk_level: {self.risk_level!r}")
        if self.switch_attempts_used < 0 or self.max_switch_attempts < 1:
            raise RoutingPolicyError("Invalid switch retry budget bounds in HarnessPrivateExtensions.")
        object.__setattr__(self, "fenced_worker_ids", tuple(str(w) for w in self.fenced_worker_ids if str(w)))
        object.__setattr__(
            self,
            "revoked_epoch_grant_ids",
            tuple(str(g) for g in self.revoked_epoch_grant_ids if str(g)),
        )
        object.__setattr__(self, "risk_reasons", tuple(str(r) for r in self.risk_reasons if str(r)))
        computed = _rp_canonical_sha256(self._canonical_body())
        if self.extensions_sha256:
            if self.extensions_sha256 != computed:
                raise HandoffStoreIntegrityError(
                    f"HarnessPrivateExtensions hash mismatch: stored={self.extensions_sha256}, computed={computed}."
                )
        else:
            object.__setattr__(self, "extensions_sha256", computed)

    def _canonical_body(self) -> dict[str, Any]:
        return {
            "fenced_epoch": self.fenced_epoch,
            "fenced_worker_ids": list(self.fenced_worker_ids),
            "policy": self.policy,
            "session_policy_revision": self.session_policy_revision,
            "global_policy_revision": self.global_policy_revision,
            "validated_grants": [g.to_mapping() for g in self.validated_grants],
            "revoked_epoch_grant_ids": list(self.revoked_epoch_grant_ids),
            "budget_snapshot": dict(self.budget_snapshot),
            "switch_attempts_used": self.switch_attempts_used,
            "max_switch_attempts": self.max_switch_attempts,
            "routing_decision": dict(self.routing_decision),
            "admission_transition": dict(self.admission_transition),
            "risk_level": self.risk_level,
            "risk_reasons": list(self.risk_reasons),
            "semantic_confirmation": dict(self.semantic_confirmation),
        }

    def to_mapping(self) -> dict[str, Any]:
        body = self._canonical_body()
        body["extensions_sha256"] = self.extensions_sha256
        return body

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HarnessPrivateExtensions:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("HarnessPrivateExtensions must be a JSON object.")
        raw_grants = raw.get("validated_grants") or []
        if not isinstance(raw_grants, list):
            raise HandoffStoreIntegrityError("HarnessPrivateExtensions.validated_grants must be a list.")
        risk_lv = str(raw.get("risk_level") or "low")
        if risk_lv not in {"low", "high"}:
            raise HandoffStoreIntegrityError(f"Unsupported risk_level in store: {risk_lv!r}")
        return cls(
            fenced_epoch=int(raw.get("fenced_epoch") or 0),
            fenced_worker_ids=tuple(str(w) for w in (raw.get("fenced_worker_ids") or ())),
            policy=str(raw.get("policy") or "standard"),
            session_policy_revision=int(raw.get("session_policy_revision") or 1),
            global_policy_revision=int(raw.get("global_policy_revision") or 1),
            validated_grants=tuple(HandoffGrantSnapshot.from_mapping(g) for g in raw_grants),
            revoked_epoch_grant_ids=tuple(str(g) for g in (raw.get("revoked_epoch_grant_ids") or ())),
            budget_snapshot=dict(raw.get("budget_snapshot") or {}),
            switch_attempts_used=int(raw.get("switch_attempts_used") or 0),
            max_switch_attempts=int(raw.get("max_switch_attempts") or RP_DEFAULT_MAX_SWITCH_ATTEMPTS),
            routing_decision=dict(raw.get("routing_decision") or {}),
            admission_transition=dict(raw.get("admission_transition") or {}),
            risk_level=risk_lv,  # type: ignore[arg-type]
            risk_reasons=tuple(str(r) for r in (raw.get("risk_reasons") or ())),
            semantic_confirmation=dict(raw.get("semantic_confirmation") or {}),
            extensions_sha256=str(raw.get("extensions_sha256") or ""),
        )


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """Complete crash-durable handoff envelope pairing PortableHandoffCore + HarnessPrivateExtensions."""

    handoff_id: str
    session_id: str
    task_id: str
    status: HandoffStatus
    portable_core: PortableHandoffCore
    harness_extensions: HarnessPrivateExtensions
    failure_code: str = ""
    failure_reason: str = ""
    resulting_task_state: str = "paused"
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in {"staged", "committed", "failed_paused", "superseded"}:
            raise RoutingPolicyError(f"Unsupported HandoffRecord.status: {self.status!r}")
        if self.portable_core.handoff_id != self.handoff_id:
            raise RoutingPolicyError("HandoffRecord.handoff_id must match portable_core.handoff_id.")
        if self.resulting_task_state not in {"pending", "paused", "blocked"}:
            raise RoutingPolicyError(
                f"HandoffRecord.resulting_task_state must be a safe non-running state, got {self.resulting_task_state!r}."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "status": self.status,
            "portable_core": self.portable_core.to_mapping(),
            "harness_extensions": self.harness_extensions.to_mapping(),
            "failure_code": self.failure_code,
            "failure_reason": self.failure_reason,
            "resulting_task_state": self.resulting_task_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HandoffRecord:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("HandoffRecord must be a JSON object.")
        st = str(raw.get("status") or "")
        if st not in {"staged", "committed", "failed_paused", "superseded"}:
            raise HandoffStoreIntegrityError(f"Invalid HandoffRecord status in store: {st!r}")
        core_raw = raw.get("portable_core")
        ext_raw = raw.get("harness_extensions")
        if not isinstance(core_raw, Mapping) or not isinstance(ext_raw, Mapping):
            raise HandoffStoreIntegrityError("HandoffRecord requires portable_core and harness_extensions objects.")
        return cls(
            handoff_id=str(raw["handoff_id"]),
            session_id=str(raw["session_id"]),
            task_id=str(raw["task_id"]),
            status=st,  # type: ignore[arg-type]
            portable_core=PortableHandoffCore.from_mapping(core_raw),
            harness_extensions=HarnessPrivateExtensions.from_mapping(ext_raw),
            failure_code=str(raw.get("failure_code") or ""),
            failure_reason=str(raw.get("failure_reason") or ""),
            resulting_task_state=str(raw.get("resulting_task_state") or "paused"),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class SessionSwitchBudget:
    """Durable per-session switch retry tracker (Task 4)."""

    session_id: str
    switch_attempts_used: int
    consecutive_failures: int
    max_switch_attempts: int = RP_DEFAULT_MAX_SWITCH_ATTEMPTS
    last_handoff_id: str = ""
    updated_at: float = 0.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "switch_attempts_used": self.switch_attempts_used,
            "consecutive_failures": self.consecutive_failures,
            "max_switch_attempts": self.max_switch_attempts,
            "last_handoff_id": self.last_handoff_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SessionSwitchBudget:
        if not isinstance(raw, Mapping):
            raise HandoffStoreIntegrityError("SessionSwitchBudget must be a JSON object.")
        used = int(raw.get("switch_attempts_used") or 0)
        fails = int(raw.get("consecutive_failures") or 0)
        cap = int(raw.get("max_switch_attempts") or RP_DEFAULT_MAX_SWITCH_ATTEMPTS)
        if used < 0 or fails < 0 or cap < 1:
            raise HandoffStoreIntegrityError("SessionSwitchBudget bounds in store are invalid.")
        return cls(
            session_id=_non_empty(str(raw["session_id"]), "session_id"),
            switch_attempts_used=used,
            consecutive_failures=fails,
            max_switch_attempts=cap,
            last_handoff_id=str(raw.get("last_handoff_id") or ""),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


def classify_switch_risk(
    *,
    source_provider_kind: str,
    target_provider_kind: str,
    steps_used: int,
    committed_consequential_count: int,
    file_reference_count: int,
    truncated_messages: int,
    recoveries_used: int,
    consecutive_switch_failures: int,
    explicit_high_risk: bool = False,
) -> tuple[SwitchRiskLevel, tuple[str, ...]]:
    """Deterministically classify whether a model transition requires semantic confirmation (Task 3)."""
    reasons: list[str] = []
    if explicit_high_risk:
        reasons.append("explicit_high_risk_flag")
    if source_provider_kind != target_provider_kind:
        reasons.append(f"provider_boundary_transition:{source_provider_kind}->{target_provider_kind}")
    if steps_used > 0:
        reasons.append(f"in_progress_task_steps:{steps_used}")
    if committed_consequential_count > 0:
        reasons.append(f"committed_consequential_actions:{committed_consequential_count}")
    if file_reference_count > 0:
        reasons.append(f"referenced_workspace_files:{file_reference_count}")
    if truncated_messages > 0:
        reasons.append(f"context_messages_truncated:{truncated_messages}")
    if recoveries_used > 0:
        reasons.append(f"prior_task_recoveries:{recoveries_used}")
    if consecutive_switch_failures > 0:
        reasons.append(f"prior_failed_switches:{consecutive_switch_failures}")

    if reasons:
        return "high", tuple(reasons)
    return "low", ()


def build_semantic_confirmation_prompt(
    core: PortableHandoffCore,
    extensions: HarnessPrivateExtensions,
) -> list[dict[str, str]]:
    """Build a zero-tool, plain-text semantic confirmation prompt for a high-risk transition."""
    expected_paths = sorted(ref.path for ref in core.file_references)
    handoff_summary = {
        "handoff_id": core.handoff_id,
        "core_sha256": core.core_sha256,
        "objective": core.objective,
        "next_step": core.next_step,
        "policy": extensions.policy,
        "referenced_paths": expected_paths,
        "completed_milestone_ids": list(core.completed_milestone_ids),
        "pending_milestone_ids": list(core.pending_milestone_ids),
        "kv_cache_portable": False,
    }
    sys_msg = (
        "You are receiving a portable M5 2.0 task handoff after a conservative model switch. "
        "Do NOT assume any KV cache, prompt cache, or hidden state from the prior model. "
        "Respond ONLY with a single valid JSON object containing exact keys: "
        '{"core_sha256": "...", "policy": "...", "next_step": <int>, '
        '"referenced_paths": [...], "ack_no_kv_cache": true, "restated_objective": "..."}.'
    )
    user_msg = (
        "Confirm authority, scope, and plain-text handoff invariants for the following bundle:\n"
        + json.dumps(handoff_summary, indent=2, sort_keys=True)
    )
    return [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": user_msg},
    ]


def _rp_extract_json_object(raw_text: str) -> Mapping[str, Any]:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise HandoffRejectedError(
                "handoff_semantic_confirmation_failed",
                "Target model semantic confirmation did not return a JSON object.",
            ) from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise HandoffRejectedError(
                "handoff_semantic_confirmation_failed",
                f"Target model semantic confirmation JSON is malformed: {exc}.",
            ) from exc
    if not isinstance(parsed, Mapping):
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            "Target model semantic confirmation payload must be a JSON object.",
        )
    return parsed


def verify_semantic_confirmation_response(
    raw_response: str | Mapping[str, Any],
    *,
    core: PortableHandoffCore,
    extensions: HarnessPrivateExtensions,
) -> dict[str, Any]:
    """Validate target-model or verifier semantic confirmation against authoritative handoff state."""
    payload = _rp_extract_json_object(raw_response) if isinstance(raw_response, str) else raw_response
    if not isinstance(payload, Mapping):
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            "Semantic confirmation payload must be a mapping or JSON string.",
        )

    for bad_key in RP_FORBIDDEN_NON_PORTABLE_KEYS:
        if bad_key in payload and payload.get(bad_key) not in (None, False, "", [], {}):
            raise HandoffRejectedError(
                "handoff_semantic_confirmation_failed",
                f"Semantic confirmation attempted to assert forbidden KV/non-portable field `{bad_key}`.",
            )

    ack_no_kv = payload.get("ack_no_kv_cache")
    if ack_no_kv is not True:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            "Semantic confirmation must explicitly set `ack_no_kv_cache: true`.",
        )

    conf_hash = str(payload.get("core_sha256") or payload.get("confirmed_core_sha256") or "").strip()
    if conf_hash != core.core_sha256:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            f"Semantic confirmation core_sha256 mismatch: expected {core.core_sha256}, got {conf_hash!r}.",
        )

    conf_pol = str(payload.get("policy") or payload.get("confirmed_policy") or "").strip()
    if conf_pol != extensions.policy:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            f"Semantic confirmation policy mismatch: expected {extensions.policy!r}, got {conf_pol!r}.",
        )

    raw_step = payload.get("next_step", payload.get("confirmed_next_step"))
    if isinstance(raw_step, bool) or not isinstance(raw_step, int) or raw_step != core.next_step:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            f"Semantic confirmation next_step mismatch: expected {core.next_step}, got {raw_step!r}.",
        )

    raw_paths = payload.get("referenced_paths", payload.get("confirmed_paths"))
    if not isinstance(raw_paths, (list, tuple)) or any(not isinstance(p, str) for p in raw_paths):
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            "Semantic confirmation must include `referenced_paths` as a list of relative path strings.",
        )
    confirmed_paths = sorted({_rp_normalize_rel_path(p, "referenced_paths") for p in raw_paths})
    expected_paths = sorted(ref.path for ref in core.file_references)
    if confirmed_paths != expected_paths:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            f"Semantic confirmation referenced_paths mismatch: expected {expected_paths}, got {confirmed_paths}.",
        )

    restated = str(payload.get("restated_objective") or payload.get("confirmed_objective") or "").strip()
    if core.objective and not restated:
        raise HandoffRejectedError(
            "handoff_semantic_confirmation_failed",
            "Semantic confirmation must include a non-empty `restated_objective` when task objective is set.",
        )

    return {
        "required": True,
        "status": "verified",
        "confirmed_core_sha256": conf_hash,
        "confirmed_policy": conf_pol,
        "confirmed_next_step": raw_step,
        "confirmed_paths": confirmed_paths,
        "ack_no_kv_cache": True,
        "restated_objective": restated[:500],
    }


def render_portable_handoff_banner(record: HandoffRecord) -> str:
    """Render a plain-text model-agnostic handoff header for the incoming model's fresh prompt."""
    core = record.portable_core
    ext = record.harness_extensions
    ref_lines = (
        ", ".join(f"{r.path}@{r.sha256[:12]}" if r.sha256 else f"{r.path}(absent)" for r in core.file_references)
        or "none"
    )
    return (
        "\n\n[PORTABLE HANDOFF — KV CACHE RECOMPUTED FROM PLAIN TEXT]\n"
        f"- handoff_id: {core.handoff_id} (core_sha256={core.core_sha256[:16]})\n"
        f"- transition: lead `{core.source_lead_model}` -> `{core.target_lead_model}`, "
        f"critic `{core.source_critic_model}` -> `{core.target_critic_model}`\n"
        f"- kv_cache_portable: false ({core.kv_cache_handling})\n"
        f"- policy: {ext.policy} (session_policy_revision={ext.session_policy_revision})\n"
        f"- next_step: {core.next_step} | completed_milestones: {list(core.completed_milestone_ids)}\n"
        f"- verified_file_references: {ref_lines}\n"
    )


class PortableHandoffStore:
    """Crash-durable JSON sidecar store for portable handoffs and switch retry budgets.

    Persists atomically in ``<store_dir>/handoffs/state.json`` with ``0700`` directory
    permissions, ``0600`` file permissions, ``O_EXCL | O_NOFOLLOW`` temporary file writes,
    temp-file ``os.fsync``, ``os.replace``, and parent-directory ``os.fsync``. Symlinked
    files/directories, corrupt JSON, and unsupported ``schema_version`` values fail closed
    with :class:`HandoffStoreIntegrityError`.
    """

    def __init__(self, store_dir: Path | str) -> None:
        base = Path(store_dir).expanduser()
        self._base_dir = base
        self._dir = base / "handoffs"
        self._state_path = self._dir / "state.json"
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _ensure_dir_unlocked(self) -> None:
        if self._base_dir.is_symlink():
            raise HandoffStoreIntegrityError(
                f"Handoff store root {self._base_dir} must not be a symlink."
            )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if self._dir.is_symlink():
            raise HandoffStoreIntegrityError(
                f"Handoff store directory {self._dir} must not be a symlink."
            )
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self._dir, 0o700)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": RP_HANDOFF_SCHEMA_VERSION,
            "handoffs": {},
            "session_budgets": {},
        }

    def _read_state_unlocked(self) -> dict[str, Any]:
        if self._base_dir.is_symlink() or self._dir.is_symlink():
            raise HandoffStoreIntegrityError(
                f"Handoff store directory {self._dir} must not be a symlink."
            )
        if os.path.lexists(self._state_path):
            try:
                st = os.lstat(self._state_path)
            except OSError as exc:
                raise HandoffStoreIntegrityError(
                    f"Cannot stat handoff state {self._state_path}: {exc}"
                ) from exc
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise HandoffStoreIntegrityError(
                    f"Handoff state {self._state_path} must be a regular non-symlink file."
                )
        else:
            return self._empty_state()

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self._state_path), flags)
            try:
                raw_bytes = os.read(fd, 16 * 1024 * 1024)
            finally:
                os.close(fd)
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HandoffStoreIntegrityError(
                f"Corrupt or unreadable handoff state {self._state_path}: {exc}"
            ) from exc

        if not isinstance(parsed, dict) or parsed.get("schema_version") != RP_HANDOFF_SCHEMA_VERSION:
            raise HandoffStoreIntegrityError(
                f"Unsupported or tampered handoff schema in {self._state_path}."
            )
        if not isinstance(parsed.get("handoffs"), dict) or not isinstance(parsed.get("session_budgets"), dict):
            raise HandoffStoreIntegrityError("Malformed handoffs or session_budgets map in handoff state.")

        for h_raw in parsed["handoffs"].values():
            HandoffRecord.from_mapping(h_raw)
        for b_raw in parsed["session_budgets"].values():
            SessionSwitchBudget.from_mapping(b_raw)
        return parsed

    def _write_state_unlocked(self, state: dict[str, Any]) -> None:
        self._ensure_dir_unlocked()
        if len(state["handoffs"]) > RP_MAX_STORED_HANDOFFS:
            prunable = sorted(
                (
                    (float(rec.get("updated_at") or 0.0), hid)
                    for hid, rec in state["handoffs"].items()
                    if isinstance(rec, Mapping) and rec.get("status") in {"superseded", "failed_paused"}
                ),
                key=lambda item: item[0],
            )
            while len(state["handoffs"]) > RP_MAX_STORED_HANDOFFS and prunable:
                _, drop_id = prunable.pop(0)
                state["handoffs"].pop(drop_id, None)

        payload = (
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        tmp_path = self._dir / f".state.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:12]}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.close(fd)
        os.replace(tmp_path, self._state_path)
        with contextlib.suppress(OSError):
            os.chmod(self._state_path, 0o600)
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(str(self._dir), dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def get_switch_budget(
        self,
        session_id: str,
        *,
        default_max_attempts: int = RP_DEFAULT_MAX_SWITCH_ATTEMPTS,
    ) -> SessionSwitchBudget:
        sid = _non_empty(session_id, "session_id")
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["session_budgets"].get(sid)
            if raw is None:
                return SessionSwitchBudget(
                    session_id=sid,
                    switch_attempts_used=0,
                    consecutive_failures=0,
                    max_switch_attempts=max(1, int(default_max_attempts)),
                )
            return SessionSwitchBudget.from_mapping(raw)

    def reserve_switch_attempt(
        self,
        session_id: str,
        *,
        max_attempts: int | None = None,
        now: float | None = None,
    ) -> SessionSwitchBudget:
        """Atomically enforce and increment the bounded per-session switch retry counter (Task 4)."""
        sid = _non_empty(session_id, "session_id")
        ts = time.time() if now is None else float(now)
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["session_budgets"].get(sid)
            cur = (
                SessionSwitchBudget.from_mapping(raw)
                if raw is not None
                else SessionSwitchBudget(
                    session_id=sid,
                    switch_attempts_used=0,
                    consecutive_failures=0,
                    max_switch_attempts=max(1, int(max_attempts or RP_DEFAULT_MAX_SWITCH_ATTEMPTS)),
                )
            )
            effective_cap = cur.max_switch_attempts
            if max_attempts is not None:
                req_cap = int(max_attempts)
                if req_cap < 1:
                    raise RoutingPolicyError("max_switch_attempts must be >= 1.")
                # Never allow an implicit request override to raise an already-persisted tighter cap;
                # only `reset_switch_budget` may reset or raise an exhausted switch cap.
                effective_cap = min(cur.max_switch_attempts, req_cap) if raw is not None else req_cap

            if cur.switch_attempts_used >= effective_cap:
                raise HandoffRejectedError(
                    "handoff_retry_budget_exceeded",
                    f"Model switch retry budget exhausted for session `{sid}` "
                    f"({cur.switch_attempts_used}/{effective_cap} attempts used).",
                )

            updated = SessionSwitchBudget(
                session_id=sid,
                switch_attempts_used=cur.switch_attempts_used + 1,
                consecutive_failures=cur.consecutive_failures,
                max_switch_attempts=effective_cap,
                last_handoff_id=cur.last_handoff_id,
                updated_at=ts,
            )
            state["session_budgets"][sid] = updated.to_mapping()
            self._write_state_unlocked(state)
            return updated

    def reset_switch_budget(
        self,
        session_id: str,
        *,
        max_attempts: int = RP_DEFAULT_MAX_SWITCH_ATTEMPTS,
        now: float | None = None,
    ) -> SessionSwitchBudget:
        sid = _non_empty(session_id, "session_id")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise RoutingPolicyError("max_attempts must be an integer >= 1.")
        ts = time.time() if now is None else float(now)
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["session_budgets"].get(sid)
            last_hid = str(raw.get("last_handoff_id") or "") if isinstance(raw, Mapping) else ""
            updated = SessionSwitchBudget(
                session_id=sid,
                switch_attempts_used=0,
                consecutive_failures=0,
                max_switch_attempts=max_attempts,
                last_handoff_id=last_hid,
                updated_at=ts,
            )
            state["session_budgets"][sid] = updated.to_mapping()
            self._write_state_unlocked(state)
            return updated

    def save_handoff(self, record: HandoffRecord, *, now: float | None = None) -> HandoffRecord:
        ts = time.time() if now is None else float(now)
        with self._lock:
            state = self._read_state_unlocked()
            if record.status == "committed":
                for hid, existing_raw in list(state["handoffs"].items()):
                    if hid == record.handoff_id:
                        continue
                    existing = HandoffRecord.from_mapping(existing_raw)
                    if existing.session_id == record.session_id and existing.status in {"committed", "staged"}:
                        superseded = HandoffRecord(
                            handoff_id=existing.handoff_id,
                            session_id=existing.session_id,
                            task_id=existing.task_id,
                            status="superseded",
                            portable_core=existing.portable_core,
                            harness_extensions=existing.harness_extensions,
                            failure_code=existing.failure_code,
                            failure_reason=existing.failure_reason,
                            resulting_task_state=existing.resulting_task_state,
                            created_at=existing.created_at,
                            updated_at=ts,
                        )
                        state["handoffs"][hid] = superseded.to_mapping()

            persisted = HandoffRecord(
                handoff_id=record.handoff_id,
                session_id=record.session_id,
                task_id=record.task_id,
                status=record.status,
                portable_core=record.portable_core,
                harness_extensions=record.harness_extensions,
                failure_code=record.failure_code,
                failure_reason=record.failure_reason,
                resulting_task_state=record.resulting_task_state,
                created_at=record.created_at or ts,
                updated_at=ts,
            )
            state["handoffs"][persisted.handoff_id] = persisted.to_mapping()

            raw_b = state["session_budgets"].get(persisted.session_id)
            cur_b = (
                SessionSwitchBudget.from_mapping(raw_b)
                if raw_b is not None
                else SessionSwitchBudget(
                    session_id=persisted.session_id,
                    switch_attempts_used=persisted.harness_extensions.switch_attempts_used,
                    consecutive_failures=0,
                    max_switch_attempts=persisted.harness_extensions.max_switch_attempts,
                )
            )
            if persisted.status == "committed":
                next_failures = 0
            elif persisted.status == "failed_paused":
                next_failures = cur_b.consecutive_failures + 1
            else:
                next_failures = cur_b.consecutive_failures
            upd_b = SessionSwitchBudget(
                session_id=persisted.session_id,
                switch_attempts_used=max(
                    cur_b.switch_attempts_used,
                    persisted.harness_extensions.switch_attempts_used,
                ),
                consecutive_failures=next_failures,
                max_switch_attempts=cur_b.max_switch_attempts,
                last_handoff_id=persisted.handoff_id,
                updated_at=ts,
            )
            state["session_budgets"][persisted.session_id] = upd_b.to_mapping()
            self._write_state_unlocked(state)
            return persisted

    def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
        hid = _non_empty(handoff_id, "handoff_id")
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["handoffs"].get(hid)
            return HandoffRecord.from_mapping(raw) if raw is not None else None

    def get_latest_handoff(
        self,
        session_id: str,
        *,
        statuses: Sequence[str] | None = None,
    ) -> HandoffRecord | None:
        sid = _non_empty(session_id, "session_id")
        allowed = set(statuses) if statuses is not None else None
        with self._lock:
            state = self._read_state_unlocked()
            matches: list[HandoffRecord] = []
            for raw in state["handoffs"].values():
                rec = HandoffRecord.from_mapping(raw)
                if rec.session_id != sid:
                    continue
                if allowed is not None and rec.status not in allowed:
                    continue
                matches.append(rec)
            if not matches:
                return None
            matches.sort(key=lambda r: (r.updated_at, r.created_at))
            return matches[-1]

    def list_handoffs(self, session_id: str | None = None) -> tuple[HandoffRecord, ...]:
        with self._lock:
            state = self._read_state_unlocked()
            out: list[HandoffRecord] = []
            for raw in state["handoffs"].values():
                rec = HandoffRecord.from_mapping(raw)
                if session_id is not None and rec.session_id != session_id:
                    continue
                out.append(rec)
            out.sort(key=lambda r: (r.created_at, r.updated_at))
            return tuple(out)

    def purge_session(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        with self._lock:
            state = self._read_state_unlocked()
            changed = False
            for hid, raw in list(state["handoffs"].items()):
                if isinstance(raw, Mapping) and raw.get("session_id") == sid:
                    state["handoffs"].pop(hid, None)
                    changed = True
            if sid in state["session_budgets"]:
                state["session_budgets"].pop(sid, None)
                changed = True
            if changed:
                self._write_state_unlocked(state)

    def validate_handoff_for_activation(
        self,
        record: HandoffRecord,
        *,
        workspace_root: Path,
        current_lead_model: str,
        current_critic_model: str,
        current_policy: str,
        latest_checkpoint_id: str = "",
        latest_checkpoint_hash: str = "",
    ) -> None:
        """Re-verify persisted handoff references, source file versions, checkpoint, and policy before execution."""
        core = record.portable_core
        if core.kv_cache_portable is not False:
            raise HandoffRejectedError(
                "kv_cache_portability_forbidden",
                f"Handoff `{record.handoff_id}` violates KV-cache non-portability invariant.",
            )
        if core.target_lead_model != current_lead_model or core.target_critic_model != current_critic_model:
            raise HandoffRejectedError(
                "handoff_model_mismatch",
                f"Active session models ({current_lead_model!r}, {current_critic_model!r}) do not match "
                f"committed handoff `{record.handoff_id}` targets "
                f"({core.target_lead_model!r}, {core.target_critic_model!r}).",
            )
        if current_policy != record.harness_extensions.policy:
            # Tightening policy after a handoff is allowed (grants already bumped), but silent loosening is not.
            pol_rank = {"plan": 0, "standard": 1, "autonomous": 2}
            if pol_rank.get(current_policy, 99) > pol_rank.get(record.harness_extensions.policy, -1):
                raise HandoffRejectedError(
                    "handoff_permission_escalation_forbidden",
                    f"Session policy escalated from handoff policy `{record.harness_extensions.policy}` "
                    f"to `{current_policy}` without re-validating handoff authority.",
                )
        expected_map = {ref.path: ref.sha256 for ref in core.file_references}
        if expected_map:
            collect_and_validate_file_references(
                workspace_root,
                [ref.path for ref in core.file_references],
                expected_versions=expected_map,
            )
        if core.checkpoint_id and latest_checkpoint_id == core.checkpoint_id:
            if core.checkpoint_payload_hash and latest_checkpoint_hash != core.checkpoint_payload_hash:
                raise HandoffRejectedError(
                    "handoff_checkpoint_corrupt",
                    f"Checkpoint `{core.checkpoint_id}` hash drifted after handoff `{record.handoff_id}`.",
                )
