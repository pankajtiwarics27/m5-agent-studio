"""LC-07 immutable full-wire request/fit arithmetic; no I/O or token estimation.

This is not a summarizer, template renderer, native counter or activation path.
Caller assembly remains visible: unseen/omitted upstream inputs cannot be proved.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

from model_lifecycle_coordinator import RequestLease
from shared_memory_admission import ModelMemoryProfile

Wire = Literal["omlx_openai", "ollama_native", "lmstudio_openai"]
Confidence = Literal["exact", "qualified_upper_bound"]
KINDS = frozenset(
    {
        "system_security",
        "history",
        "latest_user",
        "tool_definitions",
        "tool_exchange",
        "skills_memory",
        "handoff",
        "objective",
        "template",
        "media",
        "request_settings",
    }
)
ERRORS = frozenset(
    {
        "invalid_request",
        "unsupported_media",
        "incomplete_tool_exchange",
        "output_budget_unknown",
        "profile_mismatch",
        "context_exceeded",
        "counter_unqualified",
        "authority_denied",
        "lease_stale",
        "binding_unverified",
        "counter_already_dispatched",
        "count_unverified",
        "count_uncertain",
        "approval_stale",
        "admission_unavailable",
        "memory_unavailable",
    }
)
MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FitDiagnostic:
    approved_context_tokens: int
    effective_context_tokens: int | None
    input_tokens: int | None
    output_limit_tokens: int
    reasoning_reserve_tokens: int
    generation_reserve_tokens: int
    confidence: Confidence | None
    component_tokens: tuple[tuple[str, int | None], ...]

    def to_mapping(self) -> dict[str, Any]:
        return dict(
            approved_context_tokens=self.approved_context_tokens,
            effective_context_tokens=self.effective_context_tokens,
            input_tokens=self.input_tokens,
            output_limit_tokens=self.output_limit_tokens,
            reasoning_reserve_tokens=self.reasoning_reserve_tokens,
            generation_reserve_tokens=self.generation_reserve_tokens,
            confidence=self.confidence,
            component_tokens=dict(self.component_tokens),
        )


class RequestFitDenied(RuntimeError):
    def __init__(self, code: str, diagnostic: FitDiagnostic | None = None) -> None:
        # Never echo caller strings or arbitrary upstream/native exception text.
        self.code = code if code in ERRORS else "invalid_request"
        self.diagnostic = diagnostic
        super().__init__(self.code)

    def to_mapping(self) -> dict[str, Any]:
        return dict(
            code=self.code,
            retryable=False,
            fit=None if self.diagnostic is None else self.diagnostic.to_mapping(),
        )


def number(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value < 2**63:
        raise RequestFitDenied("invalid_request")
    return value


def reference(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.strip() != value
        or not value.isprintable()
    ):
        raise RequestFitDenied("invalid_request")
    return value


def digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(x not in "0123456789abcdef" for x in value)
    ):
        raise RequestFitDenied("invalid_request")
    return value


def _json(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise RequestFitDenied("invalid_request")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RequestFitDenied("invalid_request")
            _json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json(item, depth + 1)
    elif type(value) is float:
        if not math.isfinite(value):
            raise RequestFitDenied("invalid_request")
    elif value is not None and type(value) not in (str, int, bool):
        raise RequestFitDenied("invalid_request")


def encode(value: object) -> bytes:
    _json(value)
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RequestFitDenied("invalid_request") from None
    if len(raw) > MAX_BODY_BYTES:
        raise RequestFitDenied("invalid_request")
    return raw


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestFitDenied("invalid_request")
        result[key] = value
    return result


def decode(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BODY_BYTES:
        raise RequestFitDenied("invalid_request")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        _json(value)
    except (ValueError, UnicodeError, RecursionError):
        raise RequestFitDenied("invalid_request") from None
    if not isinstance(value, dict):
        raise RequestFitDenied("invalid_request")
    return value


@dataclass(frozen=True, slots=True)
class OutputBudget:
    output_tokens: int
    reasoning_tokens: int = 0
    accounting: Literal["included", "additional"] = "included"

    def __post_init__(self) -> None:
        number(self.output_tokens, positive=True)
        number(self.reasoning_tokens)
        if self.accounting not in ("included", "additional") or (
            self.accounting == "included" and self.reasoning_tokens > self.output_tokens
        ):
            raise RequestFitDenied("output_budget_unknown")
        number(self.reserve, positive=True)

    @property
    def reserve(self) -> int:
        # Included = total generated tokens already cover thinking. Never add
        # that subset twice. Additional requires separate qualified wire limits.
        return self.output_tokens + (
            self.reasoning_tokens if self.accounting == "additional" else 0
        )


@dataclass(frozen=True, slots=True)
class FitProfile:
    memory: ModelMemoryProfile
    wire: Wire
    native_profile_digest: str
    tokenizer_revision: str | None = None
    renderer_revision: str | None = None
    output_accounting: Literal["included", "additional"] = "included"

    def __post_init__(self) -> None:
        if not isinstance(self.memory, ModelMemoryProfile) or self.wire not in (
            "omlx_openai",
            "ollama_native",
            "lmstudio_openai",
        ):
            raise RequestFitDenied("profile_mismatch")
        digest(self.native_profile_digest)
        for rev in (self.tokenizer_revision, self.renderer_revision):
            if rev is not None:
                reference(rev)
        if self.output_accounting not in ("included", "additional") or (
            self.wire != "omlx_openai" and self.output_accounting != "included"
        ):
            raise RequestFitDenied("output_budget_unknown")


@dataclass(frozen=True, slots=True)
class AssemblyNote:
    """Trusted caller provenance; not proof of inputs the caller never supplied."""

    kind: Literal["skills_memory", "handoff", "objective"]
    message_index: int

    def __post_init__(self) -> None:
        if self.kind not in ("skills_memory", "handoff", "objective"):
            raise RequestFitDenied("invalid_request")
        number(self.message_index)


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    lease: RequestLease
    profile: FitProfile
    budget: OutputBudget
    origin: str
    body: bytes = field(repr=False)
    body_digest: str
    features: frozenset[str]
    components: frozenset[str]
    notes: tuple[AssemblyNote, ...]


def _media_or_text(content: object) -> bool:
    if isinstance(content, str) or content is None:
        return False
    if not isinstance(content, list) or not content:
        raise RequestFitDenied("unsupported_media")
    media = False
    for part in content:
        if not isinstance(part, dict):
            raise RequestFitDenied("unsupported_media")
        if (
            part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and set(part) == {"type", "text"}
        ):
            continue
        # Do not resolve URLs, paths or attachments, decode images, or fetch
        # anything. Only already-owned inline image data is representable here.
        image = part.get("image_url")
        if (
            part.get("type") != "image_url"
            or set(part) != {"type", "image_url"}
            or not isinstance(image, dict)
            or set(image) - {"url", "detail"}
            or not isinstance(image.get("url"), str)
            or not image["url"].startswith("data:image/")
            or ";base64," not in image["url"]
        ):
            raise RequestFitDenied("unsupported_media")
        media = True
    return media


def _conversation(messages: object, wire: Wire) -> tuple[set[str], set[str]]:
    if not isinstance(messages, list) or not messages:
        raise RequestFitDenied("invalid_request")
    features = {"messages", "template", "request_settings"}
    components = {"history", "template", "request_settings"}
    pending: dict[str, str] = {}
    seen: set[str] = set()
    users = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (
            "system",
            "user",
            "assistant",
            "tool",
        ):
            raise RequestFitDenied("invalid_request")
        role = message["role"]
        thinking = "thinking" if wire == "ollama_native" else "reasoning_content"
        if set(message) & {"images", "audio", "video", "input_audio", "attachments"}:
            raise RequestFitDenied("unsupported_media")
        if set(message) - {
            "role",
            "content",
            "tool_calls",
            "tool_call_id",
            "tool_name" if wire == "ollama_native" else "name",
            thinking,
        }:
            raise RequestFitDenied("invalid_request")
        if wire == "ollama_native" and isinstance(message.get("content"), list):
            raise RequestFitDenied("unsupported_media")
        if "content" not in message or (
            message["content"] is None
            and not (role == "assistant" and message.get("tool_calls"))
        ):
            raise RequestFitDenied("invalid_request")
        if _media_or_text(message.get("content")):
            features.add("media")
            components.add("media")
        if role == "system":
            components.add("system_security")
        if role == "user":
            users += 1
            components.add("latest_user")
        if thinking in message:
            if not isinstance(message[thinking], str):
                raise RequestFitDenied("invalid_request")
            features.add("reasoning")
        if role == "tool":
            call_id = reference(message.get("tool_call_id"))
            name = pending.pop(call_id, None)
            given_name = message.get("tool_name" if wire == "ollama_native" else "name")
            if name is None or (given_name is not None and given_name != name):
                raise RequestFitDenied("incomplete_tool_exchange")
            components.add("tool_exchange")
            features.add("tool_history")
            continue
        if pending:
            raise RequestFitDenied("incomplete_tool_exchange")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or (calls and role != "assistant"):
            raise RequestFitDenied("incomplete_tool_exchange")
        for call in calls:
            if (
                not isinstance(call, dict)
                or set(call) - {"id", "type", "function"}
                # Native Ollama's exact prepared call has id/function, not
                # OpenAI's required type tag. Missing type is accepted ONLY for
                # this wire; a present non-function tag still refuses.
                or call.get("type", "function" if wire == "ollama_native" else None)
                != "function"
                or not isinstance(call.get("function"), dict)
                or set(call["function"]) - {"name", "arguments"}
            ):
                raise RequestFitDenied("incomplete_tool_exchange")
            call_id = reference(call.get("id"))
            if call_id in seen:
                raise RequestFitDenied("incomplete_tool_exchange")
            seen.add(call_id)
            name = reference(call["function"].get("name"))
            arguments = call["function"].get("arguments")
            if wire == "ollama_native":
                if not isinstance(arguments, dict):
                    raise RequestFitDenied("incomplete_tool_exchange")
            elif not isinstance(arguments, str) or not isinstance(
                decode(arguments.encode()), dict
            ):
                raise RequestFitDenied("incomplete_tool_exchange")
            pending[call_id] = name
    if pending or not users:
        raise RequestFitDenied("incomplete_tool_exchange")
    return features, components


def request_components(
    value: dict[str, Any], wire: Wire
) -> tuple[frozenset[str], frozenset[str]]:
    """Pure feature inventory, NOT final-body validation, counting or authority.

    Also used before a cold request has an exact native instance selector. Final
    prepare_request still validates the complete immutable provider wire/body.
    """
    features, components = _conversation(value.get("messages"), wire)
    if "tools" in value:
        if not isinstance(value["tools"], list):
            raise RequestFitDenied("invalid_request")
        names: set[str] = set()
        for tool in value["tools"]:
            if (
                not isinstance(tool, dict)
                or set(tool) != {"type", "function"}
                or tool.get("type") != "function"
                or not isinstance(tool.get("function"), dict)
                or set(tool["function"]) - {"name", "description", "parameters"}
            ):
                raise RequestFitDenied("invalid_request")
            fn = tool["function"]
            name = reference(fn.get("name"))
            if (
                name in names
                or not isinstance(fn.get("parameters", {}), dict)
                or ("description" in fn and not isinstance(fn["description"], str))
            ):
                raise RequestFitDenied("invalid_request")
            names.add(name)
        if value["tools"]:
            features.add("tools")
            components.add("tool_definitions")
    if any(
        k in value
        for k in (
            "think",
            "thinking_budget",
            "reasoning_effort",
            "chat_template_kwargs",
        )
    ):
        features.add("reasoning")
    return frozenset(features), frozenset(components)


def prepare_request(
    lease: RequestLease,
    profile: FitProfile,
    budget: OutputBudget,
    body: bytes,
    *,
    origin: str,
    notes: tuple[AssemblyNote, ...] = (),
) -> PreparedRequest:
    """Accept the FINAL provider request, preserving every byte and input anchor.

    Upstream assembly/sanitization must finish before this call; any subsequent
    tool/template/settings/media/history change needs a new request/fit approval.
    """
    if (
        not isinstance(lease, RequestLease)
        or not isinstance(profile, FitProfile)
        or lease.target != profile.memory.target
    ):
        raise RequestFitDenied("profile_mismatch")
    reference(origin)
    if (
        not isinstance(budget, OutputBudget)
        or budget.accounting != profile.output_accounting
    ):
        raise RequestFitDenied("output_budget_unknown")
    value = decode(body)
    allowed = {"model", "messages", "tools", "stream"}
    if profile.wire == "ollama_native":
        allowed |= {
            "runner",
            "options",
            "keep_alive",
            "truncate",
            "shift",
            "think",
            "format",
        }
    else:
        allowed |= {
            "max_tokens",
            "stream_options",
            "temperature",
            "top_p",
            "top_k",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "logit_bias",
            "repeat_penalty",
            "tool_choice",
            "response_format",
            "chat_template_kwargs",
            "thinking_budget",
            "reasoning_effort",
            "truncate",
            "shift",
        }
    if set(value) - allowed or any(
        k in value and value[k] is not False for k in ("truncate", "shift")
    ):
        raise RequestFitDenied("invalid_request")
    if "thinking_budget" in value and (
        type(value["thinking_budget"]) is not int
        or value["thinking_budget"] != budget.reasoning_tokens
    ):
        raise RequestFitDenied("output_budget_unknown")
    selector = (
        lease.instance_id
        if profile.wire == "lmstudio_openai"
        else lease.target.artifact_id + ":local"
        if profile.wire == "ollama_native"
        else lease.target.artifact_id
    )
    if value.get("model") != selector or value.get("stream") is not True:
        raise RequestFitDenied("profile_mismatch")
    if profile.wire == "ollama_native":
        options = value.get("options")
        if (
            not isinstance(options, dict)
            or options.get("num_ctx") != profile.memory.context_tokens
            or type(options.get("num_ctx")) is not int
            or value.get("truncate") is not False
            or value.get("shift") is not False
        ):
            raise RequestFitDenied("profile_mismatch")
        output = options.get("num_predict")
    else:
        output = value.get("max_tokens")
    if (
        type(output) is not int
        or output != budget.output_tokens
        or (
            budget.accounting == "additional"
            and value.get("thinking_budget") != budget.reasoning_tokens
        )
    ):
        raise RequestFitDenied("output_budget_unknown")
    feature_set, component_set = request_components(value, profile.wire)
    features, components = set(feature_set), set(component_set)
    if not isinstance(notes, tuple) or any(
        not isinstance(n, AssemblyNote) or n.message_index >= len(value["messages"])
        for n in notes
    ):
        raise RequestFitDenied("invalid_request")
    components.update(n.kind for n in notes)
    return PreparedRequest(
        lease,
        profile,
        budget,
        origin,
        body,
        hashlib.sha256(body).hexdigest(),
        frozenset(features),
        frozenset(components),
        notes,
    )


def verify_prepared(prepared: PreparedRequest) -> None:
    if (
        not isinstance(prepared, PreparedRequest)
        or prepare_request(
            prepared.lease,
            prepared.profile,
            prepared.budget,
            prepared.body,
            origin=prepared.origin,
            notes=prepared.notes,
        )
        != prepared
    ):
        raise RequestFitDenied("invalid_request")


def assess_fit(
    prepared: PreparedRequest,
    *,
    effective_context: int,
    input_tokens: int,
    confidence: Confidence,
    component_tokens: tuple[tuple[str, int], ...] = (),
) -> FitDiagnostic:
    verify_prepared(prepared)
    number(effective_context, positive=True)
    number(input_tokens)
    if effective_context != prepared.profile.memory.context_tokens:
        raise RequestFitDenied("profile_mismatch")
    if confidence not in ("exact", "qualified_upper_bound") or not isinstance(
        component_tokens, tuple
    ):
        raise RequestFitDenied("count_unverified")
    counts: dict[str, int] = {}
    for entry in component_tokens:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise RequestFitDenied("count_unverified")
        key, count = entry
        if key not in prepared.components or key in counts:
            raise RequestFitDenied("count_unverified")
        counts[key] = number(count)
    if sum(counts.values()) > input_tokens:
        raise RequestFitDenied("count_unverified")
    result = FitDiagnostic(
        prepared.profile.memory.context_tokens,
        effective_context,
        input_tokens,
        prepared.budget.output_tokens,
        prepared.budget.reasoning_tokens,
        prepared.budget.reserve,
        confidence,
        tuple((k, counts.get(k)) for k in sorted(prepared.components)),
    )
    # The full-wire input total already contains template/tools/media expansion.
    # Component counts are informational, never another additive budget.
    if input_tokens + prepared.budget.reserve > effective_context:
        raise RequestFitDenied("context_exceeded", result)
    return result
