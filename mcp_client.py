"""MCP wire-protocol boundary, connector idempotency semantics, and external-effect reconciliation.

AS-012 / M5-P3-05 boundary module. Owns:

1. Wire protocol negotiation (``adaptive_call``):
   - Fast-path: Stateless ``2026-07-28`` (``MCP-Protocol-Version`` + ``_meta`` +
     ``Mcp-Method`` headers) or cached ``2025`` ``Mcp-Session-Id``.
   - Automatic fallback: ``2025-06-18`` ``initialize`` + ``Mcp-Session-Id`` +
     ``notifications/initialized`` handshake (plus ``/sse`` -> ``/mcp`` URL
     retry on HTTP 405).
2. Connector idempotency & replay semantics (``classify_mcp_tool_semantics``,
   ``bind_tool_call_params``):
   - Explicitly classifies every MCP tool as ``supported``, ``unsupported``, or
     ``not_applicable_read_only`` (fail-closed ``unsupported`` default for any
     non-read-only tool without explicit connector/tool idempotency metadata).
   - Binds deterministic ``action_id`` and idempotency keys across HTTP headers,
     JSON-RPC ``_meta``, and opted-in argument fields where supported.
3. Queryable-outcome reconciliation & durable external-effect journal
   (``McpReconciliationSpec``, ``build_reconciliation_query_args``,
   ``evaluate_reconciliation_response``, ``McpExternalEffectStore``):
   - Persists secret-redacted outbound MCP attempt records in
     ``$HARNESS_STORE_DIR/mcp_effects/state.json`` (`0700` dir / `0600` file,
     ``O_NOFOLLOW`` symlink refusal, atomic ``fsync`` replacement).
   - Evaluates read-only reconciliation query responses strictly (requiring
     explicit ``authoritative_not_found`` before treating negative lookups as
     ``verified_not_applied``).

URL validation, redirect rejection, central admission, and network/process I/O
stay in the application: this module never opens a network connection.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Literal

CLIENT_NAME = "m5-studio"
STATELESS_PROTOCOL_VERSION = "2026-07-28"
STATEFUL_PROTOCOL_VERSION = "2025-06-18"
MCP_EFFECT_SCHEMA_VERSION = 1
_MCPC_MAX_STORED_EFFECTS = 500

McpIdempotencyStatus = Literal["supported", "unsupported", "not_applicable_read_only"]
McpIdempotencyMechanism = Literal[
    "http_header",
    "jsonrpc_meta",
    "argument_field",
    "intrinsic",
    "read_only",
    "unsupported",
]
McpReplayPolicy = Literal["idempotent", "reconcilable", "non_idempotent"]
McpReconciliationMode = Literal["action_receipt", "resource_lookup"]
McpReconciliationVerdict = Literal["verified_committed", "verified_not_applied", "unresolved"]
McpEffectStatus = Literal[
    "in_flight",
    "committed",
    "failed",
    "uncertain",
    "reconciled_committed",
    "reconciled_not_applied",
]

_MCPC_SECRET_ARG_KEY_RE = re.compile(
    r"(?:^|_|-)(?:api_?key|secret|token|password|passwd|credential|credentials|"
    r"private_?key|access_?key|client_?secret|auth|bearer|cookie|webhook)(?:$|_|-)",
    re.IGNORECASE,
)


class McpTransportError(RuntimeError):
    """Raised for MCP protocol/transport failures (subset of RuntimeError for compatibility)."""


class McpRpcError(McpTransportError):
    """Raised when an MCP server returns an explicit JSON-RPC ``error`` object."""


class McpAmbiguousOutcomeError(McpTransportError):
    """Raised when an outbound ``tools/call`` outcome is ambiguous (HTTP 5xx or garbled 200)."""


class McpEffectStoreIntegrityError(RuntimeError):
    """Raised when the persisted MCP external-effect store is corrupted or symlinked."""


@dataclass(frozen=True)
class TransportResponse:
    """Transport-agnostic response view; the application adapts its HTTP client to this."""

    status_code: int
    text: str
    content_type: str
    headers: Mapping[str, str]

    @classmethod
    def from_httpx(cls, resp: Any) -> "TransportResponse":
        return cls(
            status_code=int(resp.status_code),
            text=resp.text,
            content_type=(resp.headers.get("content-type") or "").lower(),
            headers={str(k): str(v) for k, v in resp.headers.items()},
        )


Post = Callable[[str, Mapping[str, str], dict[str, Any], float], Awaitable[TransportResponse]]


class SessionStore:
    """Explicit per-alias session-id cache; replaces the previous module global."""

    def __init__(self, backing: dict[str, str] | None = None) -> None:
        self._sessions: dict[str, str] = backing if backing is not None else {}

    def get(self, alias: str) -> str:
        return self._sessions.get(alias, "")

    def set(self, alias: str, session_id: str) -> None:
        self._sessions[alias] = session_id

    def clear(self, alias: str) -> None:
        self._sessions.pop(alias, None)


# ---------------------------------------------------------------------------
# Connector Semantics, Idempotency Classification & Reconciliation Specs
# ---------------------------------------------------------------------------


def _mcpc_canonical_args_sha256(args: Mapping[str, Any] | None) -> str:
    payload = dict(args) if isinstance(args, Mapping) else {}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class McpReconciliationSpec:
    """Declarative read-only query contract for reconciling an uncertain MCP write."""

    query_tool: str
    mode: McpReconciliationMode = "action_receipt"
    query_arg_map: tuple[tuple[str, str], ...] = ()
    static_args: tuple[tuple[str, Any], ...] = ()
    match_fields: tuple[str, ...] = ()
    result_field: str = ""
    authoritative_not_found: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "query_tool": self.query_tool,
            "mode": self.mode,
            "query_arg_map": {k: v for k, v in self.query_arg_map},
            "static_args": {k: v for k, v in self.static_args},
            "match_fields": list(self.match_fields),
            "result_field": self.result_field,
            "authoritative_not_found": self.authoritative_not_found,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "McpReconciliationSpec | None":
        if not isinstance(raw, Mapping):
            return None
        qtool = str(raw.get("query_tool") or raw.get("queryTool") or "").strip()
        if not qtool:
            return None
        raw_mode = str(raw.get("mode") or "").strip().lower()
        raw_map = raw.get("query_arg_map") or raw.get("queryArgMap")
        arg_pairs: list[tuple[str, str]] = []
        if isinstance(raw_map, Mapping):
            for k, v in raw_map.items():
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs:
                    arg_pairs.append((ks, vs))
        if raw_mode in ("action_receipt", "resource_lookup"):
            mode: McpReconciliationMode = raw_mode  # type: ignore[assignment]
        else:
            mode = "resource_lookup" if arg_pairs else "action_receipt"
        raw_static = raw.get("static_args") or raw.get("staticArgs")
        static_pairs: list[tuple[str, Any]] = []
        if isinstance(raw_static, Mapping):
            for k, v in raw_static.items():
                ks = str(k).strip()
                if ks:
                    static_pairs.append((ks, v))
        raw_match = raw.get("match_fields") or raw.get("matchFields") or ()
        match_fields = tuple(
            str(f).strip()
            for f in (raw_match if isinstance(raw_match, Sequence) and not isinstance(raw_match, (str, bytes)) else ())
            if str(f).strip()
        )
        return cls(
            query_tool=qtool,
            mode=mode,
            query_arg_map=tuple(arg_pairs),
            static_args=tuple(static_pairs),
            match_fields=match_fields,
            result_field=str(raw.get("result_field") or raw.get("resultField") or "").strip(),
            authoritative_not_found=bool(
                raw.get("authoritative_not_found", raw.get("authoritativeNotFound", False))
            ),
        )


@dataclass(frozen=True, slots=True)
class McpToolSemantics:
    """Evaluated connector + tool semantics for an outbound MCP tool (`M5-P3-05`)."""

    alias: str
    tool_name: str
    transport: str
    read_only: bool
    destructive: bool
    idempotency_supported: bool
    idempotency_status: McpIdempotencyStatus
    idempotency_mechanism: McpIdempotencyMechanism
    idempotency_arg_name: str = ""
    allow_keyed_replay: bool = False
    requires_approval_override: bool = False
    reconciliation: McpReconciliationSpec | None = None

    @property
    def replay_policy(self) -> McpReplayPolicy:
        if self.read_only:
            return "idempotent"
        if self.reconciliation is not None:
            return "reconcilable"
        if self.idempotency_supported and self.allow_keyed_replay:
            return "idempotent"
        return "non_idempotent"

    @property
    def reconciliation_supported(self) -> bool:
        return self.reconciliation is not None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "tool_name": self.tool_name,
            "transport": self.transport,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "idempotency_supported": self.idempotency_supported,
            "idempotency_status": self.idempotency_status,
            "idempotency_mechanism": self.idempotency_mechanism,
            "idempotency_arg_name": self.idempotency_arg_name,
            "allow_keyed_replay": self.allow_keyed_replay,
            "requires_approval_override": self.requires_approval_override,
            "replay_policy": self.replay_policy,
            "reconciliation_supported": self.reconciliation_supported,
            "reconciliation": self.reconciliation.to_mapping() if self.reconciliation else None,
        }


def classify_mcp_tool_semantics(
    server: Mapping[str, Any],
    tool_spec: Mapping[str, Any] | None = None,
    *,
    remote_tool: str = "",
    connector_policy: Mapping[str, Any] | None = None,
) -> McpToolSemantics:
    """Classify MCP tool idempotency, replay policy, and reconciliation contract.

    Fail-closed rules (`AC 2`):
    - ``read_only`` is True ONLY when explicitly declared via MCP
      ``annotations.readOnlyHint == True`` (and ``destructiveHint != True``) or
      explicit ``read_only: True`` tool/connector metadata.
    - ``idempotency_supported`` is True ONLY when explicitly declared via
      ``annotations.idempotentHint == True``, tool ``idempotency`` /
      ``x-idempotency`` metadata, ``inputSchema`` ``x-idempotency-key`` property
      opt-in, or server ``connector_policy``. Otherwise it is explicitly marked
      ``idempotency_supported = False``, ``idempotency_status = 'unsupported'``.
    """
    alias = str(server.get("alias") or "").strip().lower()
    transport = str(server.get("transport") or "http").strip().lower() or "http"
    spec = tool_spec if isinstance(tool_spec, Mapping) else {}
    tname = str(remote_tool or spec.get("name") or "").strip()

    conn_pol = connector_policy if isinstance(connector_policy, Mapping) else {}
    srv_inline_pol = server.get("connector_semantics")
    if not conn_pol and isinstance(srv_inline_pol, Mapping):
        conn_pol = srv_inline_pol
    tool_overrides = conn_pol.get("tool_overrides")
    override: Mapping[str, Any] = (
        tool_overrides.get(tname, {})
        if isinstance(tool_overrides, Mapping) and isinstance(tool_overrides.get(tname), Mapping)
        else {}
    )

    annotations_raw = spec.get("annotations")
    annotations: Mapping[str, Any] = (
        annotations_raw if isinstance(annotations_raw, Mapping) else {}
    )
    idem_meta_raw = (
        override.get("idempotency")
        or spec.get("idempotency")
        or spec.get("x-idempotency")
        or {}
    )
    idem_meta: Mapping[str, Any] = idem_meta_raw if isinstance(idem_meta_raw, Mapping) else {}

    destructive = bool(
        override.get(
            "destructive",
            annotations.get("destructiveHint", idem_meta.get("destructive", False)),
        )
    )
    explicit_read_only = bool(
        override.get(
            "read_only",
            spec.get(
                "read_only",
                annotations.get("readOnlyHint", idem_meta.get("mode") == "read_only"),
            ),
        )
    )
    read_only = bool(explicit_read_only and not destructive)
    requires_approval_override = bool(
        override.get(
            "requires_approval",
            annotations.get("requiresApproval", idem_meta.get("requires_approval", False)),
        )
    )

    reconcile_raw = (
        override.get("reconciliation")
        or spec.get("reconciliation")
        or spec.get("x-reconciliation")
        or conn_pol.get("reconciliation")
    )
    reconciliation = (
        None
        if read_only
        else McpReconciliationSpec.from_mapping(
            reconcile_raw if isinstance(reconcile_raw, Mapping) else None
        )
    )

    if read_only:
        return McpToolSemantics(
            alias=alias,
            tool_name=tname,
            transport=transport,
            read_only=True,
            destructive=False,
            idempotency_supported=True,
            idempotency_status="not_applicable_read_only",
            idempotency_mechanism="read_only",
            idempotency_arg_name="",
            allow_keyed_replay=True,
            requires_approval_override=requires_approval_override,
            reconciliation=None,
        )

    # Check explicit argument-level idempotency schema opt-in (`x-idempotency-key: true`)
    schema_arg_name = ""
    input_schema = spec.get("inputSchema")
    if isinstance(input_schema, Mapping):
        props = input_schema.get("properties")
        if isinstance(props, Mapping):
            for prop_name, prop_def in props.items():
                if isinstance(prop_def, Mapping) and bool(prop_def.get("x-idempotency-key")):
                    schema_arg_name = str(prop_name).strip()
                    break

    idempotent_hint = bool(annotations.get("idempotentHint", False))
    srv_supports_key = bool(conn_pol.get("supports_idempotency_key", False))
    tool_supports_raw = override.get(
        "idempotency_supported",
        idem_meta.get("supported", None),
    )

    if tool_supports_raw is False:
        supported = False
    elif tool_supports_raw is True or bool(schema_arg_name) or idempotent_hint or srv_supports_key:
        supported = True
    else:
        supported = False

    if not supported:
        return McpToolSemantics(
            alias=alias,
            tool_name=tname,
            transport=transport,
            read_only=False,
            destructive=destructive,
            idempotency_supported=False,
            idempotency_status="unsupported",
            idempotency_mechanism="unsupported",
            idempotency_arg_name="",
            allow_keyed_replay=False,
            requires_approval_override=requires_approval_override,
            reconciliation=reconciliation,
        )

    raw_mech = str(
        override.get("idempotency_mechanism")
        or idem_meta.get("mechanism")
        or conn_pol.get("idempotency_mechanism")
        or ""
    ).strip().lower()
    arg_name = str(
        override.get("idempotency_arg_name")
        or idem_meta.get("arg_name")
        or conn_pol.get("idempotency_arg_name")
        or schema_arg_name
        or ""
    ).strip()

    if raw_mech in ("http_header", "jsonrpc_meta", "argument_field", "intrinsic"):
        mechanism: McpIdempotencyMechanism = raw_mech  # type: ignore[assignment]
        if mechanism == "http_header" and transport != "http":
            mechanism = "jsonrpc_meta"
        if mechanism == "argument_field" and not arg_name:
            mechanism = "jsonrpc_meta" if transport != "http" else "http_header"
    elif arg_name:
        mechanism = "argument_field"
    elif idempotent_hint and not srv_supports_key and tool_supports_raw is None:
        mechanism = "intrinsic"
    elif transport == "http":
        mechanism = "http_header"
    else:
        mechanism = "jsonrpc_meta"

    allow_keyed_replay = bool(
        override.get(
            "allow_keyed_replay",
            idem_meta.get(
                "allow_keyed_replay",
                conn_pol.get("allow_keyed_replay", idempotent_hint),
            ),
        )
    )
    return McpToolSemantics(
        alias=alias,
        tool_name=tname,
        transport=transport,
        read_only=False,
        destructive=destructive,
        idempotency_supported=True,
        idempotency_status="supported",
        idempotency_mechanism=mechanism,
        idempotency_arg_name=(arg_name if mechanism == "argument_field" else ""),
        allow_keyed_replay=allow_keyed_replay,
        requires_approval_override=requires_approval_override,
        reconciliation=reconciliation,
    )


def bind_tool_call_params(
    params: Mapping[str, Any] | None,
    *,
    action_id: str,
    semantics: McpToolSemantics | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Bind deterministic ``action_id`` and supported idempotency keys into ``tools/call`` params.

    Returns ``(wire_params, bound_idempotency_key)`` without mutating ``params``.
    When idempotency is ``unsupported``, ``_meta`` explicitly marks
    ``m5.studio/idempotencyStatus = 'unsupported'`` and never fabricates an
    ``idempotencyKey`` claim (`AC 2`).
    """
    base: dict[str, Any] = dict(params) if isinstance(params, Mapping) else {}
    clean_aid = str(action_id or "").strip()
    if not clean_aid:
        return base, None

    raw_args = base.get("arguments")
    args_copy: dict[str, Any] = dict(raw_args) if isinstance(raw_args, Mapping) else {}
    raw_meta = base.get("_meta")
    meta_copy: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    meta_copy["m5.studio/actionId"] = clean_aid

    bound_key: str | None = None
    if semantics is None:
        meta_copy["idempotencyKey"] = clean_aid
        meta_copy["io.modelcontextprotocol/idempotencyKey"] = clean_aid
        bound_key = clean_aid
    else:
        meta_copy["m5.studio/idempotencyStatus"] = semantics.idempotency_status
        if semantics.idempotency_supported and not semantics.read_only:
            meta_copy["idempotencyKey"] = clean_aid
            meta_copy["io.modelcontextprotocol/idempotencyKey"] = clean_aid
            if semantics.idempotency_mechanism == "argument_field" and semantics.idempotency_arg_name:
                existing_val = args_copy.get(semantics.idempotency_arg_name)
                if existing_val is None or str(existing_val).strip() == "":
                    args_copy[semantics.idempotency_arg_name] = clean_aid
                bound_key = str(args_copy[semantics.idempotency_arg_name])
            else:
                bound_key = clean_aid

    base["arguments"] = args_copy
    base["_meta"] = meta_copy
    return base, bound_key


def build_reconciliation_query_args(
    spec: McpReconciliationSpec,
    *,
    action_id: str,
    remote_tool: str,
    write_args: Mapping[str, Any] | None,
    query_tool_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build deterministic arguments for a read-only reconciliation query tool.

    Returns ``None`` if a required mapped field is missing from ``write_args``
    so callers fail closed (`unresolved`).
    """
    clean_aid = str(action_id or "").strip()
    safe_write = write_args if isinstance(write_args, Mapping) else {}
    out: dict[str, Any] = {k: v for k, v in spec.static_args}

    if spec.mode == "resource_lookup" or spec.query_arg_map:
        for query_param, source_ref in spec.query_arg_map:
            if source_ref == "$action_id":
                if not clean_aid:
                    return None
                out[query_param] = clean_aid
            elif source_ref == "$tool":
                out[query_param] = remote_tool
            else:
                if (
                    source_ref not in safe_write
                    or safe_write[source_ref] is None
                    or safe_write[source_ref] == "[REDACTED]"
                ):
                    return None
                out[query_param] = safe_write[source_ref]
        return out

    # Default `action_receipt` mode:
    default_receipt_args: dict[str, Any] = {
        "action_id": clean_aid,
        "idempotency_key": clean_aid,
        "tool": remote_tool,
    }
    if isinstance(query_tool_schema, Mapping) and query_tool_schema.get("additionalProperties") is False:
        allowed_props = query_tool_schema.get("properties")
        if isinstance(allowed_props, Mapping) and allowed_props:
            filtered = {k: v for k, v in default_receipt_args.items() if k in allowed_props}
            if not filtered:
                return None
            out.update(filtered)
            return out
    out.update(default_receipt_args)
    return out


def _mcpc_extract_structured_payload(raw_rpc_result: Mapping[str, Any]) -> dict[str, Any] | None:
    structured = raw_rpc_result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = raw_rpc_result.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for blk in content:
            if isinstance(blk, Mapping) and blk.get("type") == "text" and isinstance(blk.get("text"), str):
                txt = blk["text"].strip()
                if txt.startswith("{") and txt.endswith("}"):
                    try:
                        parsed = json.loads(txt)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, Mapping):
                        return dict(parsed)
    return None


def _mcpc_fields_match(
    candidate: Mapping[str, Any],
    match_fields: Sequence[str],
    *,
    action_id: str,
    write_args: Mapping[str, Any],
) -> bool:
    for field in match_fields:
        if field == "$action_id":
            actual_aid = str(candidate.get("action_id") or candidate.get("idempotency_key") or "")
            if actual_aid != action_id:
                return False
            continue
        if field not in candidate or field not in write_args:
            return False
        if candidate[field] != write_args[field]:
            return False
    return True


def evaluate_reconciliation_response(
    spec: McpReconciliationSpec,
    raw_rpc_result: Mapping[str, Any] | None,
    *,
    action_id: str,
    write_args: Mapping[str, Any] | None = None,
) -> tuple[McpReconciliationVerdict, str]:
    """Strictly evaluate a read-only query tool response into a reconciliation verdict.

    Fail-closed guarantees (`Task 3`, `AC 1`):
    - Any RPC error, ``isError=True``, or unparseable/non-JSON response yields
      ``('unresolved', '')``.
    - Negative responses (``not_found`` / ``found=False`` / empty list) yield
      ``'verified_not_applied'`` ONLY when ``spec.authoritative_not_found`` is
      True; otherwise they yield ``'unresolved'`` so eventual consistency never
      causes a duplicate external write.
    """
    if not isinstance(raw_rpc_result, Mapping) or bool(raw_rpc_result.get("isError", False)):
        return ("unresolved", "")
    payload = _mcpc_extract_structured_payload(raw_rpc_result)
    if payload is None:
        return ("unresolved", "")

    clean_aid = str(action_id or "").strip()
    safe_write = write_args if isinstance(write_args, Mapping) else {}

    echoed_aid = payload.get("action_id") or payload.get("idempotency_key")
    if echoed_aid is not None and str(echoed_aid).strip() and str(echoed_aid).strip() != clean_aid:
        return ("unresolved", "")

    target_obj: Any = payload.get(spec.result_field) if spec.result_field else payload

    # 1. Explicit status / boolean receipt checks:
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in {"committed", "succeeded", "completed", "applied", "exists"}:
        cand_map = target_obj if isinstance(target_obj, Mapping) else payload
        if spec.match_fields and not _mcpc_fields_match(
            cand_map, spec.match_fields, action_id=clean_aid, write_args=safe_write
        ):
            return ("unresolved", "")
        return ("verified_committed", json.dumps(payload, sort_keys=True, ensure_ascii=False))

    if raw_status in {"not_found", "never_applied", "absent", "not_started"}:
        if spec.authoritative_not_found:
            return ("verified_not_applied", "")
        return ("unresolved", "")

    if payload.get("committed") is True or payload.get("applied") is True:
        cand_map = target_obj if isinstance(target_obj, Mapping) else payload
        if spec.match_fields and not _mcpc_fields_match(
            cand_map, spec.match_fields, action_id=clean_aid, write_args=safe_write
        ):
            return ("unresolved", "")
        return ("verified_committed", json.dumps(payload, sort_keys=True, ensure_ascii=False))

    # 2. Resource lookup / `found` / `exists` / `items` checks:
    if payload.get("found") is False or payload.get("exists") is False:
        return ("verified_not_applied", "") if spec.authoritative_not_found else ("unresolved", "")

    if isinstance(target_obj, Sequence) and not isinstance(target_obj, (str, bytes, Mapping)):
        matches = [
            item
            for item in target_obj
            if isinstance(item, Mapping)
            and (
                not spec.match_fields
                or _mcpc_fields_match(
                    item, spec.match_fields, action_id=clean_aid, write_args=safe_write
                )
            )
        ]
        if len(matches) == 1:
            return ("verified_committed", json.dumps(dict(matches[0]), sort_keys=True, ensure_ascii=False))
        if len(target_obj) == 0 and spec.authoritative_not_found:
            return ("verified_not_applied", "")
        return ("unresolved", "")

    if target_obj is None and spec.result_field:
        return ("verified_not_applied", "") if spec.authoritative_not_found else ("unresolved", "")

    if isinstance(target_obj, Mapping):
        if payload.get("found") is True or payload.get("exists") is True:
            if not spec.match_fields or _mcpc_fields_match(
                target_obj, spec.match_fields, action_id=clean_aid, write_args=safe_write
            ):
                return ("verified_committed", json.dumps(dict(target_obj), sort_keys=True, ensure_ascii=False))
            return ("unresolved", "")
        if spec.match_fields and _mcpc_fields_match(
            target_obj, spec.match_fields, action_id=clean_aid, write_args=safe_write
        ):
            return ("verified_committed", json.dumps(dict(target_obj), sort_keys=True, ensure_ascii=False))

    return ("unresolved", "")


# ---------------------------------------------------------------------------
# Durable MCP External-Effect & Connector-Semantics Store (`$HARNESS_STORE_DIR/mcp_effects/state.json`)
# ---------------------------------------------------------------------------


def _mcpc_redact_value(
    value: Any,
    *,
    secrets: tuple[str, ...],
    parent_key: str = "",
) -> tuple[Any, bool]:
    if parent_key and _MCPC_SECRET_ARG_KEY_RE.search(parent_key):
        return ("[REDACTED]", True)
    if isinstance(value, str):
        if any(sec in value for sec in secrets):
            return ("[REDACTED]", True)
        return (value, False)
    if isinstance(value, Mapping):
        out_dict: dict[str, Any] = {}
        changed = False
        for k, v in value.items():
            ks = str(k)
            rv, rc = _mcpc_redact_value(v, secrets=secrets, parent_key=ks)
            out_dict[ks] = rv
            changed = changed or rc
        return (out_dict, changed)
    if isinstance(value, list):
        out_list: list[Any] = []
        changed = False
        for item in value:
            rv, rc = _mcpc_redact_value(item, secrets=secrets)
            out_list.append(rv)
            changed = changed or rc
        return (out_list, changed)
    return (value, False)


@dataclass(frozen=True, slots=True)
class McpExternalEffectRecord:
    """Durable record of an outbound MCP `tools/call` attempt and reconciliation state."""

    action_id: str
    session_id: str
    task_id: str | None
    epoch: int
    alias: str
    remote_tool: str
    transport: str
    redacted_args: dict[str, Any]
    args_redacted: bool
    args_sha256: str
    idempotency_supported: bool
    idempotency_status: McpIdempotencyStatus
    idempotency_mechanism: McpIdempotencyMechanism
    bound_idempotency_key: str | None
    replay_policy: McpReplayPolicy
    reconciliation_supported: bool
    query_tool: str
    status: McpEffectStatus
    attempts: int
    remote_dispatches: int
    reconciled_by: str
    reconciled_output: str
    last_error: str
    created_at: float
    updated_at: float

    @property
    def tool_name(self) -> str:
        return f"mcp__{self.alias}__{self.remote_tool}"

    @property
    def reconciliation_verdict(self) -> McpReconciliationVerdict:
        if self.status == "reconciled_committed":
            return "verified_committed"
        if self.status == "reconciled_not_applied":
            return "verified_not_applied"
        return "unresolved"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "epoch": self.epoch,
            "alias": self.alias,
            "remote_tool": self.remote_tool,
            "tool_name": self.tool_name,
            "reconciliation_verdict": self.reconciliation_verdict,
            "transport": self.transport,
            "redacted_args": dict(self.redacted_args),
            "args_redacted": self.args_redacted,
            "args_sha256": self.args_sha256,
            "idempotency_supported": self.idempotency_supported,
            "idempotency_status": self.idempotency_status,
            "idempotency_mechanism": self.idempotency_mechanism,
            "bound_idempotency_key": self.bound_idempotency_key,
            "replay_policy": self.replay_policy,
            "reconciliation_supported": self.reconciliation_supported,
            "query_tool": self.query_tool,
            "status": self.status,
            "attempts": self.attempts,
            "remote_dispatches": self.remote_dispatches,
            "reconciled_by": self.reconciled_by,
            "reconciled_output": self.reconciled_output,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "McpExternalEffectRecord":
        raw_args = raw.get("redacted_args")
        return cls(
            action_id=str(raw["action_id"]),
            session_id=str(raw.get("session_id") or ""),
            task_id=(str(raw["task_id"]) if raw.get("task_id") is not None else None),
            epoch=int(raw.get("epoch") or 0),
            alias=str(raw.get("alias") or ""),
            remote_tool=str(raw.get("remote_tool") or ""),
            transport=str(raw.get("transport") or "http"),
            redacted_args=(dict(raw_args) if isinstance(raw_args, Mapping) else {}),
            args_redacted=bool(raw.get("args_redacted", False)),
            args_sha256=str(raw.get("args_sha256") or ""),
            idempotency_supported=bool(raw.get("idempotency_supported", False)),
            idempotency_status=str(raw.get("idempotency_status") or "unsupported"),  # type: ignore[arg-type]
            idempotency_mechanism=str(raw.get("idempotency_mechanism") or "unsupported"),  # type: ignore[arg-type]
            bound_idempotency_key=(
                str(raw["bound_idempotency_key"])
                if raw.get("bound_idempotency_key") is not None
                else None
            ),
            replay_policy=str(raw.get("replay_policy") or "non_idempotent"),  # type: ignore[arg-type]
            reconciliation_supported=bool(raw.get("reconciliation_supported", False)),
            query_tool=str(raw.get("query_tool") or ""),
            status=str(raw.get("status") or "uncertain"),  # type: ignore[arg-type]
            attempts=int(raw.get("attempts") or 1),
            remote_dispatches=int(raw.get("remote_dispatches") or 0),
            reconciled_by=str(raw.get("reconciled_by") or ""),
            reconciled_output=str(raw.get("reconciled_output") or ""),
            last_error=str(raw.get("last_error") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
        )


def _mcpc_check_no_symlink_chain(base_dir: Path, target_dir: Path) -> None:
    for candidate in (base_dir, target_dir):
        if candidate.exists() and candidate.is_symlink():
            raise McpEffectStoreIntegrityError(
                f"Refusing symlinked MCP external-effect directory: {candidate}"
            )


class McpExternalEffectStore:
    """Durable store for MCP connector semantics and outbound external-effect reconciliation."""

    def __init__(
        self,
        harness_store_dir: Path | str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._base_dir = Path(harness_store_dir)
        self._dir = self._base_dir / "mcp_effects"
        self._state_path = self._dir / "state.json"
        self._clock = clock
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": MCP_EFFECT_SCHEMA_VERSION,
            "connector_policies": {},
            "effects": {},
        }

    def _ensure_dir_locked(self) -> None:
        _mcpc_check_no_symlink_chain(self._base_dir, self._dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _mcpc_check_no_symlink_chain(self._base_dir, self._dir)
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass

    def _load_locked(self) -> dict[str, Any]:
        _mcpc_check_no_symlink_chain(self._base_dir, self._dir)
        if not self._state_path.exists():
            if self._state_path.is_symlink():
                raise McpEffectStoreIntegrityError(
                    f"Refusing dangling symlinked MCP effect state file: {self._state_path}"
                )
            return self._empty_state()
        try:
            st = os.lstat(self._state_path)
        except OSError as exc:
            raise McpEffectStoreIntegrityError(f"Cannot stat MCP effect store: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise McpEffectStoreIntegrityError(
                f"Refusing non-regular or symlinked MCP effect state file: {self._state_path}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self._state_path), flags)
            try:
                raw_bytes = os.read(fd, 16 * 1024 * 1024)
            finally:
                os.close(fd)
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise McpEffectStoreIntegrityError(
                f"Corrupted MCP external-effect state at {self._state_path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict) or parsed.get("schema_version") != MCP_EFFECT_SCHEMA_VERSION:
            raise McpEffectStoreIntegrityError(
                f"Unsupported MCP external-effect schema at {self._state_path}"
            )
        conn_pols = parsed.get("connector_policies")
        effects = parsed.get("effects")
        if not isinstance(conn_pols, dict) or not isinstance(effects, dict):
            raise McpEffectStoreIntegrityError(
                f"Malformed MCP external-effect collections at {self._state_path}"
            )
        return {
            "schema_version": MCP_EFFECT_SCHEMA_VERSION,
            "connector_policies": dict(conn_pols),
            "effects": dict(effects),
        }

    def _save_locked(self, state: Mapping[str, Any]) -> None:
        self._ensure_dir_locked()
        if self._state_path.is_symlink():
            raise McpEffectStoreIntegrityError(
                f"Refusing symlinked MCP effect state target: {self._state_path}"
            )
        effects_map = dict(state.get("effects") or {})
        if len(effects_map) > _MCPC_MAX_STORED_EFFECTS:
            # Prune oldest terminal (`committed` / `failed`) records first; never prune `uncertain` / `in_flight`!
            removable = sorted(
                (
                    (float(v.get("updated_at") or 0.0), k)
                    for k, v in effects_map.items()
                    if isinstance(v, Mapping) and v.get("status") in ("committed", "failed")
                ),
                key=lambda item: item[0],
            )
            excess = len(effects_map) - _MCPC_MAX_STORED_EFFECTS
            for _ts, rk in removable[:excess]:
                effects_map.pop(rk, None)

        payload_bytes = (
            json.dumps(
                {
                    "schema_version": MCP_EFFECT_SCHEMA_VERSION,
                    "connector_policies": dict(state.get("connector_policies") or {}),
                    "effects": effects_map,
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        ).encode("utf-8")

        tmp_path = self._dir / f".state.{os.getpid()}.{threading.get_ident()}.tmp"
        if tmp_path.is_symlink():
            raise McpEffectStoreIntegrityError(f"Refusing symlinked temp path: {tmp_path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp_path), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, self._state_path)
        try:
            dir_fd = os.open(str(self._dir), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

    def get_connector_policy(self, alias: str) -> dict[str, Any]:
        clean = str(alias or "").strip().lower()
        if not clean:
            return {}
        with self._lock:
            state = self._load_locked()
            raw = state["connector_policies"].get(clean)
            return dict(raw) if isinstance(raw, Mapping) else {}

    def set_connector_policy(self, alias: str, policy: Mapping[str, Any]) -> dict[str, Any]:
        clean = str(alias or "").strip().lower()
        if not clean:
            raise ValueError("MCP alias is required.")
        normalized: dict[str, Any] = {
            "supports_idempotency_key": bool(policy.get("supports_idempotency_key", False)),
            "idempotency_mechanism": str(policy.get("idempotency_mechanism") or "").strip().lower(),
            "idempotency_arg_name": str(policy.get("idempotency_arg_name") or "").strip(),
            "allow_keyed_replay": bool(policy.get("allow_keyed_replay", False)),
        }
        rec_raw = policy.get("reconciliation")
        rec_spec = McpReconciliationSpec.from_mapping(rec_raw if isinstance(rec_raw, Mapping) else None)
        if rec_spec is not None:
            normalized["reconciliation"] = rec_spec.to_mapping()
        overrides_raw = policy.get("tool_overrides")
        if isinstance(overrides_raw, Mapping):
            normalized["tool_overrides"] = {
                str(k): dict(v) for k, v in overrides_raw.items() if isinstance(v, Mapping)
            }
        with self._lock:
            state = self._load_locked()
            state["connector_policies"][clean] = normalized
            self._save_locked(state)
        return normalized

    def delete_connector_policy(self, alias: str) -> None:
        clean = str(alias or "").strip().lower()
        if not clean:
            return
        with self._lock:
            state = self._load_locked()
            if clean in state["connector_policies"]:
                state["connector_policies"].pop(clean, None)
                self._save_locked(state)

    def stage_dispatch(
        self,
        *,
        action_id: str,
        session_id: str,
        task_id: str | None,
        epoch: int | None,
        semantics: McpToolSemantics,
        arguments: Mapping[str, Any] | None,
        bound_idempotency_key: str | None,
        known_secret_values: Iterable[str] = (),
    ) -> McpExternalEffectRecord:
        """Persist pre-dispatch MCP effect metadata before contacting the connector."""
        aid = str(action_id or "").strip()
        if not aid:
            raise ValueError("action_id is required when staging an MCP external effect.")
        safe_args = dict(arguments) if isinstance(arguments, Mapping) else {}
        digest = _mcpc_canonical_args_sha256(safe_args)
        secrets = tuple(
            sorted(
                {str(s) for s in known_secret_values if isinstance(s, str) and len(s) >= 6},
                key=len,
                reverse=True,
            )
        )
        redacted_obj, was_redacted = _mcpc_redact_value(safe_args, secrets=secrets)
        now = self._clock()
        with self._lock:
            state = self._load_locked()
            prev_raw = state["effects"].get(aid)
            prev = McpExternalEffectRecord.from_mapping(prev_raw) if isinstance(prev_raw, Mapping) else None
            rec = McpExternalEffectRecord(
                action_id=aid,
                session_id=str(session_id or ""),
                task_id=(str(task_id) if task_id else None),
                epoch=max(0, int(epoch or 0)),
                alias=semantics.alias,
                remote_tool=semantics.tool_name,
                transport=semantics.transport,
                redacted_args=redacted_obj if isinstance(redacted_obj, dict) else {},
                args_redacted=was_redacted,
                args_sha256=digest,
                idempotency_supported=semantics.idempotency_supported,
                idempotency_status=semantics.idempotency_status,
                idempotency_mechanism=semantics.idempotency_mechanism,
                bound_idempotency_key=bound_idempotency_key,
                replay_policy=semantics.replay_policy,
                reconciliation_supported=semantics.reconciliation_supported,
                query_tool=(semantics.reconciliation.query_tool if semantics.reconciliation else ""),
                status="in_flight",
                attempts=(prev.attempts + 1 if prev is not None else 1),
                remote_dispatches=(prev.remote_dispatches + 1 if prev is not None else 1),
                reconciled_by="",
                reconciled_output="",
                last_error="",
                created_at=(prev.created_at if prev is not None else now),
                updated_at=now,
            )
            state["effects"][aid] = rec.to_mapping()
            self._save_locked(state)
            return rec

    def record_outcome(
        self,
        action_id: str,
        *,
        status: McpEffectStatus,
        output: str = "",
        error: str = "",
    ) -> McpExternalEffectRecord | None:
        aid = str(action_id or "").strip()
        if not aid:
            return None
        now = self._clock()
        with self._lock:
            state = self._load_locked()
            raw = state["effects"].get(aid)
            if not isinstance(raw, Mapping):
                return None
            prev = McpExternalEffectRecord.from_mapping(raw)
            updated = McpExternalEffectRecord(
                action_id=prev.action_id,
                session_id=prev.session_id,
                task_id=prev.task_id,
                epoch=prev.epoch,
                alias=prev.alias,
                remote_tool=prev.remote_tool,
                transport=prev.transport,
                redacted_args=prev.redacted_args,
                args_redacted=prev.args_redacted,
                args_sha256=prev.args_sha256,
                idempotency_supported=prev.idempotency_supported,
                idempotency_status=prev.idempotency_status,
                idempotency_mechanism=prev.idempotency_mechanism,
                bound_idempotency_key=prev.bound_idempotency_key,
                replay_policy=prev.replay_policy,
                reconciliation_supported=prev.reconciliation_supported,
                query_tool=prev.query_tool,
                status=status,
                attempts=prev.attempts,
                remote_dispatches=prev.remote_dispatches,
                reconciled_by=prev.reconciled_by,
                reconciled_output=(output[:12_000] if output else prev.reconciled_output),
                last_error=(error[:500] if error else prev.last_error),
                created_at=prev.created_at,
                updated_at=now,
            )
            state["effects"][aid] = updated.to_mapping()
            self._save_locked(state)
            return updated

    def record_reconciliation(
        self,
        action_id: str,
        *,
        verdict: McpReconciliationVerdict,
        reconciled_by: str,
        reconciled_output: str = "",
    ) -> McpExternalEffectRecord | None:
        aid = str(action_id or "").strip()
        if not aid:
            return None
        now = self._clock()
        with self._lock:
            state = self._load_locked()
            raw = state["effects"].get(aid)
            if not isinstance(raw, Mapping):
                return None
            prev = McpExternalEffectRecord.from_mapping(raw)
            if verdict == "verified_committed":
                target_status: McpEffectStatus = "reconciled_committed"
            elif verdict == "verified_not_applied":
                target_status = "reconciled_not_applied"
            else:
                target_status = prev.status
            updated = McpExternalEffectRecord(
                action_id=prev.action_id,
                session_id=prev.session_id,
                task_id=prev.task_id,
                epoch=prev.epoch,
                alias=prev.alias,
                remote_tool=prev.remote_tool,
                transport=prev.transport,
                redacted_args=prev.redacted_args,
                args_redacted=prev.args_redacted,
                args_sha256=prev.args_sha256,
                idempotency_supported=prev.idempotency_supported,
                idempotency_status=prev.idempotency_status,
                idempotency_mechanism=prev.idempotency_mechanism,
                bound_idempotency_key=prev.bound_idempotency_key,
                replay_policy=prev.replay_policy,
                reconciliation_supported=prev.reconciliation_supported,
                query_tool=prev.query_tool,
                status=target_status,
                attempts=prev.attempts,
                remote_dispatches=prev.remote_dispatches,
                reconciled_by=(
                    str(reconciled_by or "")[:120]
                    if verdict != "unresolved"
                    else prev.reconciled_by
                ),
                reconciled_output=(
                    reconciled_output[:12_000]
                    if verdict != "unresolved"
                    else prev.reconciled_output
                ),
                last_error=(
                    str(reconciled_by or prev.last_error)[:500]
                    if verdict == "unresolved"
                    else prev.last_error
                ),
                created_at=prev.created_at,
                updated_at=now,
            )
            state["effects"][aid] = updated.to_mapping()
            self._save_locked(state)
            return updated

    def resolve_operator(
        self,
        action_id: str,
        *,
        resolution: Literal["committed", "not_applied"],
        reason: str = "",
    ) -> McpExternalEffectRecord:
        verdict: McpReconciliationVerdict = (
            "verified_committed" if resolution == "committed" else "verified_not_applied"
        )
        tag = f"operator:{reason}" if reason else f"operator:{resolution}"
        rec = self.record_reconciliation(
            action_id,
            verdict=verdict,
            reconciled_by=tag,
        )
        if rec is None:
            raise KeyError(f"MCP external effect {action_id!r} not found.")
        return rec

    def summary(self, *, session_id: str | None = None) -> dict[str, Any]:
        rows = self.list_effects(session_id=session_id)
        counts: dict[str, int] = {
            "in_flight": 0,
            "committed": 0,
            "failed": 0,
            "uncertain": 0,
            "reconciled_committed": 0,
            "reconciled_not_applied": 0,
        }
        unsupported_count = 0
        blocking_ids: list[str] = []
        for r in rows:
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.idempotency_status == "unsupported":
                unsupported_count += 1
            if r.status == "uncertain" and r.idempotency_status != "not_applicable_read_only":
                blocking_ids.append(r.action_id)
        return {
            "total": len(rows),
            **counts,
            "unsupported_idempotency_count": unsupported_count,
            "blocking_action_ids": blocking_ids,
        }

    def sweep_orphaned_in_flight(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        active_action_ids: Collection[str] = (),
    ) -> tuple[McpExternalEffectRecord, ...]:
        """Transition orphaned `in_flight` MCP writes to `uncertain`."""
        active_set = set(active_action_ids)
        now = self._clock()
        swept: list[McpExternalEffectRecord] = []
        with self._lock:
            state = self._load_locked()
            for aid, raw in list(state["effects"].items()):
                if aid in active_set or not isinstance(raw, Mapping):
                    continue
                rec = McpExternalEffectRecord.from_mapping(raw)
                if rec.status != "in_flight":
                    continue
                if session_id is not None and rec.session_id != session_id:
                    continue
                if task_id is not None and rec.task_id != task_id:
                    continue
                next_status: McpEffectStatus = (
                    "failed" if rec.replay_policy == "idempotent" and rec.idempotency_status == "not_applicable_read_only" else "uncertain"
                )
                upd = McpExternalEffectRecord(
                    action_id=rec.action_id,
                    session_id=rec.session_id,
                    task_id=rec.task_id,
                    epoch=rec.epoch,
                    alias=rec.alias,
                    remote_tool=rec.remote_tool,
                    transport=rec.transport,
                    redacted_args=rec.redacted_args,
                    args_redacted=rec.args_redacted,
                    args_sha256=rec.args_sha256,
                    idempotency_supported=rec.idempotency_supported,
                    idempotency_status=rec.idempotency_status,
                    idempotency_mechanism=rec.idempotency_mechanism,
                    bound_idempotency_key=rec.bound_idempotency_key,
                    replay_policy=rec.replay_policy,
                    reconciliation_supported=rec.reconciliation_supported,
                    query_tool=rec.query_tool,
                    status=next_status,
                    attempts=rec.attempts,
                    remote_dispatches=rec.remote_dispatches,
                    reconciled_by=rec.reconciled_by,
                    reconciled_output=rec.reconciled_output,
                    last_error=rec.last_error or "orphaned_in_flight",
                    created_at=rec.created_at,
                    updated_at=now,
                )
                state["effects"][aid] = upd.to_mapping()
                swept.append(upd)
            if swept:
                self._save_locked(state)
        return tuple(swept)

    def get_effect(self, action_id: str) -> McpExternalEffectRecord | None:
        aid = str(action_id or "").strip()
        if not aid:
            return None
        with self._lock:
            state = self._load_locked()
            raw = state["effects"].get(aid)
            return McpExternalEffectRecord.from_mapping(raw) if isinstance(raw, Mapping) else None

    def list_effects(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        alias: str | None = None,
        statuses: Collection[str] | None = None,
    ) -> tuple[McpExternalEffectRecord, ...]:
        status_set = set(statuses) if statuses is not None else None
        clean_alias = str(alias).strip().lower() if alias else None
        with self._lock:
            state = self._load_locked()
            out: list[McpExternalEffectRecord] = []
            for raw in state["effects"].values():
                if not isinstance(raw, Mapping):
                    continue
                rec = McpExternalEffectRecord.from_mapping(raw)
                if session_id is not None and rec.session_id != session_id:
                    continue
                if task_id is not None and rec.task_id != task_id:
                    continue
                if clean_alias is not None and rec.alias != clean_alias:
                    continue
                if status_set is not None and rec.status not in status_set:
                    continue
                out.append(rec)
            out.sort(key=lambda r: r.created_at)
            return tuple(out)

    def find_blocking_uncertain_writes(
        self,
        *,
        session_id: str,
        alias: str | None = None,
        exclude_action_id: str | None = None,
    ) -> tuple[McpExternalEffectRecord, ...]:
        """Return unreconciled `uncertain` non-read-only MCP writes blocking further writes in `session_id`."""
        sid = str(session_id or "").strip()
        if not sid:
            return ()
        clean_alias = str(alias).strip().lower() if alias else None
        with self._lock:
            state = self._load_locked()
            blockers: list[McpExternalEffectRecord] = []
            for raw in state["effects"].values():
                if not isinstance(raw, Mapping):
                    continue
                rec = McpExternalEffectRecord.from_mapping(raw)
                if rec.session_id != sid:
                    continue
                if exclude_action_id and rec.action_id == exclude_action_id:
                    continue
                if clean_alias is not None and rec.alias != clean_alias:
                    continue
                if rec.idempotency_status == "not_applicable_read_only":
                    continue
                if rec.status == "uncertain":
                    blockers.append(rec)
            blockers.sort(key=lambda r: r.created_at)
            return tuple(blockers)


# ---------------------------------------------------------------------------
# Pure protocol pieces
# ---------------------------------------------------------------------------


def parse_mcp_payload(text: str, content_type: str, status_code: int, expected_id: int) -> dict[str, Any]:
    """Parse a JSON or SSE JSON-RPC response body and return its ``result`` object."""
    ctype = (content_type or "").lower()
    raw_text = (text or "").strip()
    if "text/event-stream" in ctype or raw_text.startswith("event:") or raw_text.startswith("data:"):
        chosen: dict[str, Any] | None = None
        for line in raw_text.splitlines():
            s = line.strip()
            if not s.startswith("data:"):
                continue
            d_str = s[5:].strip()
            if not d_str or d_str == "[DONE]":
                continue
            try:
                pkt = json.loads(d_str)
            except json.JSONDecodeError:
                continue
            if isinstance(pkt, dict):
                if pkt.get("id") == expected_id:
                    chosen = pkt
                    break
                if "result" in pkt or "error" in pkt:
                    chosen = pkt
        if chosen is None:
            raise McpTransportError(f"MCP SSE stream contained no JSON-RPC result: {raw_text[:240]}")
        obj = chosen
    else:
        try:
            obj = json.loads(raw_text)
        except Exception as exc:
            raise McpTransportError(f"Invalid MCP JSON response (HTTP {status_code}): {raw_text[:240]}") from exc

    if not isinstance(obj, dict):
        raise McpTransportError(f"Invalid MCP JSON-RPC envelope (HTTP {status_code}): {raw_text[:240]}")
    if obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else ""
        raise McpRpcError(f"MCP RPC error ({code}): {msg}")
    if "result" not in obj:
        raise McpTransportError(f"MCP JSON-RPC envelope missing result (HTTP {status_code}): {raw_text[:240]}")
    return obj.get("result") or {}


def build_headers(
    server: Mapping[str, Any],
    method: str,
    tool_name: str = "",
    client_version: str = "",
    action_id: str = "",
    *,
    idempotency_status: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Method": method,
    }
    if tool_name:
        headers["Mcp-Name"] = tool_name
    if action_id and method == "tools/call":
        headers["Idempotency-Key"] = action_id
        headers["X-M5-Action-Id"] = action_id
        if idempotency_status:
            headers["X-M5-Idempotency-Status"] = idempotency_status
    for k, v in (server.get("headers") or {}).items():
        headers[str(k)] = str(v)
    tok = (server.get("effective_token") or "").strip()
    ah = (server.get("auth_header") or "Authorization").strip()
    if tok and ah.lower() != "none":
        if ah.lower() == "authorization" and not tok.lower().startswith(("bearer ", "basic ", "token ")):
            headers["Authorization"] = f"Bearer {tok}"
        else:
            headers[ah] = tok
    return headers


def build_stateless_body(method: str, params: Mapping[str, Any] | None, request_id: int, client_version: str) -> dict[str, Any]:
    base_params = dict(params) if params else {}
    existing_meta = base_params.get("_meta")
    merged_meta = {
        **(dict(existing_meta) if isinstance(existing_meta, Mapping) else {}),
        "io.modelcontextprotocol/protocolVersion": STATELESS_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": CLIENT_NAME, "version": client_version},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    meta_params = {
        **base_params,
        "_meta": merged_meta,
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": meta_params}


def build_initialize_body(client_version: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": STATEFUL_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": client_version},
        },
    }


def extract_session_id(headers: Mapping[str, str]) -> str:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    return lowered.get("mcp-session-id", "")


def retry_url_after_405(url: str) -> str | None:
    """Legacy ``/sse`` endpoints answer 405; retry once against ``/mcp``."""
    if url.rstrip("/").endswith("/sse"):
        return re.sub(r"/sse/?$", "/mcp", url)
    return None


# ---------------------------------------------------------------------------
# Adaptive negotiation state machine
# ---------------------------------------------------------------------------


def _raise_for_auth(resp: TransportResponse, url: str) -> None:
    if resp.status_code in (401, 403):
        raise McpTransportError(f"MCP HTTP {resp.status_code} Unauthorized at `{url}` — check API token / Authorization header.")


def _looks_like_session_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(k in lowered for k in ("init", "session", "protocol", "version", "-32600", "-32602"))


async def adaptive_call(
    post: Post,
    *,
    url: str,
    server: Mapping[str, Any],
    method: str,
    params: Mapping[str, Any] | None = None,
    timeout: float = 25.0,
    sessions: SessionStore | None = None,
    client_version: str = "",
    action_id: str = "",
    semantics: McpToolSemantics | None = None,
) -> dict[str, Any]:
    """Execute one MCP RPC through the adaptive stateless/stateful transport.

    ``post`` must apply the application's outbound URL validation and redirect
    rejection for every request (including retries issued from here).
    For ``method == 'tools/call'``, HTTP 5xx or malformed HTTP 200 responses raise
    :class:`McpAmbiguousOutcomeError` immediately rather than re-dispatching a
    second ``tools/call`` after handshake.
    """
    sessions = sessions or SessionStore()
    alias = str(server.get("alias") or "")
    is_tool_call = method == "tools/call"
    effective_params: Mapping[str, Any] | None = params
    idem_status = semantics.idempotency_status if semantics is not None else ""
    if is_tool_call and action_id:
        effective_params, _bound_key = bind_tool_call_params(
            params, action_id=action_id, semantics=semantics
        )
    tool_name = str((effective_params or {}).get("name") or "") if is_tool_call else ""
    base_headers = build_headers(
        server,
        method,
        tool_name=tool_name,
        client_version=client_version,
        action_id=action_id,
        idempotency_status=idem_status,
    )
    cached_sid = sessions.get(alias)

    req_id = 2
    if cached_sid:
        h1 = {**base_headers, "Mcp-Session-Id": cached_sid}
        body1: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": dict(effective_params) if effective_params else {},
        }
    else:
        h1 = {**base_headers, "MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION}
        body1 = build_stateless_body(method, effective_params, req_id, client_version)

    resp1 = await post(url, h1, body1, timeout)
    retry_target = retry_url_after_405(url) if resp1.status_code == 405 else None
    if retry_target:
        url = retry_target
        resp1 = await post(url, h1, body1, timeout)

    if resp1.status_code == 200:
        try:
            return parse_mcp_payload(resp1.text, resp1.content_type, resp1.status_code, req_id)
        except McpRpcError as rpc_err:
            if not _looks_like_session_error(str(rpc_err)):
                raise
        except McpTransportError as parse_err:
            if is_tool_call:
                raise McpAmbiguousOutcomeError(str(parse_err)) from parse_err
            raise
    _raise_for_auth(resp1, url)
    if is_tool_call and resp1.status_code >= 500:
        raise McpAmbiguousOutcomeError(
            f"MCP HTTP {resp1.status_code} on `{method}` (`{tool_name}`): remote outcome is uncertain ({resp1.text[:200]})"
        )

    # Fallback to the 2025 stateful `initialize` + `Mcp-Session-Id` handshake.
    sessions.clear(alias)
    init_headers = build_headers(server, "initialize", client_version=client_version)
    init_resp = await post(url, init_headers, build_initialize_body(client_version), timeout)
    if init_resp.status_code >= 400:
        raise McpTransportError(f"MCP HTTP {init_resp.status_code} from `{url}`: {init_resp.text[:260]}")
    parse_mcp_payload(init_resp.text, init_resp.content_type, init_resp.status_code, 1)

    sid = extract_session_id(init_resp.headers)
    req_headers = build_headers(
        server,
        method,
        tool_name=tool_name,
        client_version=client_version,
        action_id=action_id,
        idempotency_status=idem_status,
    )
    notif_headers = build_headers(server, "notifications/initialized", client_version=client_version)
    if sid:
        sessions.set(alias, sid)
        req_headers["Mcp-Session-Id"] = sid
        notif_headers["Mcp-Session-Id"] = sid

    try:
        await post(url, notif_headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, 10.0)
    except Exception:
        pass

    final_resp = await post(
        url,
        req_headers,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": method,
            "params": dict(effective_params) if effective_params else {},
        },
        timeout,
    )
    if final_resp.status_code >= 400:
        if is_tool_call and final_resp.status_code >= 500:
            raise McpAmbiguousOutcomeError(
                f"MCP HTTP {final_resp.status_code} on `{method}` (`{tool_name}`): remote outcome is uncertain ({final_resp.text[:200]})"
            )
        raise McpTransportError(f"MCP HTTP {final_resp.status_code} on `{method}`: {final_resp.text[:260]}")
    try:
        return parse_mcp_payload(final_resp.text, final_resp.content_type, final_resp.status_code, 3)
    except McpRpcError:
        raise
    except McpTransportError as parse_err:
        if is_tool_call:
            raise McpAmbiguousOutcomeError(str(parse_err)) from parse_err
        raise
