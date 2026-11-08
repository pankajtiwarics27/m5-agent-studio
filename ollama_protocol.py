"""LC-05 pure, strict native-chat translation; not an activated provider path.

No I/O, counting, truncation, fallback, XML execution frames or synthetic tool IDs.
Native tool calls are complete objects, NOT OpenAI argument/name deltas.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from shared_memory_admission import ModelMemoryProfile

SOURCE_PIN = "d63b702609c4300a2ca07be2bdb0adb9834725e5"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_TOOLS = 16


class OllamaDenied(RuntimeError):
    """Bounded reason codes only; never include prompts or native error bodies."""


def _deny(_: object = None) -> Any:
    raise OllamaDenied("invalid_native_json")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def _float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        _deny()
    return value


def parse_json(raw: str | bytes) -> Any:
    try:
        value = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_deny, parse_float=_float
        )
        _json_value(value)
        return value
    except (ValueError, RecursionError, UnicodeError):
        raise OllamaDenied("invalid_native_json") from None


def _json_value(value: object, depth: int = 0) -> None:
    if depth > 64:
        _deny()
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _deny()
            _json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
    elif value is not None and type(value) not in (str, bool, int, float):
        _deny()


def encode(value: object) -> bytes:
    _json_value(value)
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise OllamaDenied("invalid_native_json") from None
    if len(raw) > MAX_BODY_BYTES:
        raise OllamaDenied("native_body_too_large")
    return raw


def text(value: object, *, label: bool = False) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or (label and (not value or len(value) > 256))
    ):
        raise OllamaDenied("invalid_native_text")
    return value


def integer(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < int(positive):
        raise OllamaDenied("invalid_native_integer")
    return value


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise OllamaDenied("invalid_native_object")
    return value


def _keys(value: dict[str, Any], allowed: set[str]) -> None:
    if set(value) - allowed:
        raise OllamaDenied("unsupported_native_field")


def local_reference(name: str) -> str:
    # Only explicit, unambiguous local names. Do not normalize aliases/cloud tags.
    text(name, label=True)
    tag = name.rsplit("/", 1)[-1].split(":")
    if (
        len(tag) != 2
        or not all(tag)
        or any(c.isspace() for c in name)
        or tag[-1].lower() in ("cloud", "local")
        or tag[-1].lower().endswith("-cloud")
    ):
        raise OllamaDenied("explicit_local_tag_required")
    return name + ":local"


@dataclass(frozen=True, slots=True)
class OllamaProfile:
    """Trusted approved native options + LC-03 bound; never inferred from /show.

    The target fingerprint must attest ALL these settings, native defaults,
    runner/build and the memory upper bound. A record alone grants no authority.
    """

    memory: ModelMemoryProfile
    runner: str
    options_json: str
    think: bool | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory, ModelMemoryProfile):
            raise OllamaDenied("invalid_native_profile")
        local_reference(self.memory.target.artifact_id)
        text(self.runner, label=True)
        options = obj(parse_json(self.options_json))
        int_options = {
            "num_ctx",
            "num_batch",
            "num_gpu",
            "main_gpu",
            "num_thread",
            "draft_num_predict",
            "num_keep",
            "seed",
            "num_predict",
            "top_k",
            "repeat_last_n",
        }
        float_options = {
            "top_p",
            "min_p",
            "typical_p",
            "temperature",
            "repeat_penalty",
            "presence_penalty",
            "frequency_penalty",
        }
        _keys(options, int_options | float_options | {"use_mmap", "stop"})
        for key, value in options.items():
            if key in int_options and (
                type(value) is not int or not -(2**63) <= value < 2**63
            ):
                raise OllamaDenied("invalid_native_option")
            if key in float_options and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or abs(value) > 3.4028234663852886e38
            ):
                raise OllamaDenied("invalid_native_option")
            if key == "use_mmap" and type(value) is not bool:
                raise OllamaDenied("invalid_native_option")
            if key == "stop" and (
                not isinstance(value, list)
                or any(not isinstance(x, str) for x in value)
            ):
                raise OllamaDenied("invalid_native_option")
        if (
            integer(options.get("num_ctx"), positive=True) != self.memory.context_tokens
            or integer(options.get("num_predict"), positive=True)
            > self.memory.context_tokens
        ):
            raise OllamaDenied("native_profile_mismatch")
        encode(options)
        # Known max -> high normalization in the pinned server is not identity.
        if (
            self.think is not None
            and type(self.think) is not bool
            and self.think not in ("low", "medium", "high")
        ):
            raise OllamaDenied("unsupported_thinking_level")

    def body(self, *, unload: bool = False) -> dict[str, Any]:
        body = dict(
            model=local_reference(self.memory.target.artifact_id),
            runner=self.runner,
            options=parse_json(self.options_json),
            keep_alive=0 if unload else -1,
            stream=False,
            messages=[],
            truncate=False,
            shift=False,
        )
        if self.think is not None:
            body["think"] = self.think
        return body


# Reject schema keywords that this source's typed native structs would discard.
# This is deliberately narrower than arbitrary OpenAI JSON Schema/strict mode.
def _schema(schema: object, *, root: bool = False, depth: int = 0) -> None:
    if depth > 20:
        raise OllamaDenied("tool_schema_too_deep")
    value = obj(schema)
    allowed = {"type", "properties", "required", "items"}
    if not root:
        allowed |= {"description", "enum", "anyOf"}
    _keys(value, allowed)
    kind = value.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if kind is not None and (
        not kinds
        or any(
            x
            not in ("object", "array", "string", "number", "integer", "boolean", "null")
            for x in kinds
        )
    ):
        raise OllamaDenied("unsupported_tool_schema")
    if root and kind != "object":
        raise OllamaDenied("unsupported_tool_schema")
    if "description" in value:
        text(value["description"])
    if "enum" in value and not isinstance(value["enum"], list):
        raise OllamaDenied("unsupported_tool_schema")
    properties = obj(value.get("properties", {}))
    for name, child in properties.items():
        text(name, label=True)
        _schema(child, depth=depth + 1)
    if "required" in value and (
        not isinstance(value["required"], list)
        or any(not isinstance(x, str) or x not in properties for x in value["required"])
    ):
        raise OllamaDenied("unsupported_tool_schema")
    if "items" in value:
        _schema(value["items"], depth=depth + 1)
    if "anyOf" in value:
        if not isinstance(value["anyOf"], list) or not value["anyOf"]:
            raise OllamaDenied("unsupported_tool_schema")
        for child in value["anyOf"]:
            _schema(child, depth=depth + 1)


def _tools(tools: list[dict[str, Any]]) -> set[str]:
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise OllamaDenied("invalid_tool_collection")
    names: set[str] = set()
    for tool in tools:
        _keys(obj(tool), {"type", "function"})
        if tool.get("type") != "function":
            raise OllamaDenied("unsupported_native_tool")
        function = obj(tool.get("function"))
        _keys(function, {"name", "description", "parameters"})
        name = text(function.get("name"), label=True)
        if name in names:
            raise OllamaDenied("duplicate_tool_name")
        names.add(name)
        if "description" in function:
            text(function["description"])
        _schema(function.get("parameters"), root=True)
    return names


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise OllamaDenied("empty_inference_messages")
    result = []
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        _keys(
            obj(message),
            {
                "role",
                "content",
                "reasoning_content",
                "tool_calls",
                "tool_call_id",
                "name",
            },
        )
        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            raise OllamaDenied("unsupported_message_role")
        # No dropping/flattening images, names, reasoning or unsupported parts.
        native: dict[str, Any] = {
            "role": role,
            "content": text(message.get("content", "")),
        }
        if role == "tool":
            if set(message) - {"role", "content", "tool_call_id", "name"}:
                raise OllamaDenied("unsupported_tool_result_field")
            call_id = text(message.get("tool_call_id"), label=True)
            name = pending.pop(call_id, None)
            if name is None or ("name" in message and message["name"] != name):
                raise OllamaDenied("tool_result_identity_mismatch")
            native.update(tool_call_id=call_id, tool_name=name)
        else:
            if pending:
                raise OllamaDenied("incomplete_tool_exchange")
            if "name" in message or "tool_call_id" in message:
                raise OllamaDenied("unsupported_message_identity")
            if role != "assistant" and (
                "reasoning_content" in message or "tool_calls" in message
            ):
                raise OllamaDenied("unsupported_message_field")
            if "reasoning_content" in message:
                native["thinking"] = text(message["reasoning_content"])
            if "tool_calls" in message:
                calls = message["tool_calls"]
                if not isinstance(calls, list) or not 0 < len(calls) <= MAX_TOOLS:
                    raise OllamaDenied("invalid_tool_collection")
                native["tool_calls"] = []
                for call in calls:
                    _keys(obj(call), {"id", "type", "function"})
                    if call.get("type") != "function":
                        raise OllamaDenied("unsupported_native_tool")
                    call_id = text(call.get("id"), label=True)
                    if call_id in seen:
                        raise OllamaDenied("duplicate_tool_id")
                    seen.add(call_id)
                    fn = obj(call.get("function"))
                    _keys(fn, {"name", "arguments"})
                    name = text(fn.get("name"), label=True)
                    arguments = obj(parse_json(text(fn.get("arguments"))))
                    pending[call_id] = name
                    native["tool_calls"].append(
                        {
                            "id": call_id,
                            "function": {"name": name, "arguments": arguments},
                        }
                    )
        result.append(native)
    if pending:
        raise OllamaDenied("incomplete_tool_exchange")
    return result


@dataclass(frozen=True, slots=True)
class PreparedOllamaChat:
    profile: OllamaProfile
    body: bytes
    digest: str
    tool_names: frozenset[str]


def prepare_chat(
    profile: OllamaProfile,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> PreparedOllamaChat:
    # Serialize immediately: caller mutations cannot change what is admitted.
    body = profile.body()
    body.update(messages=_messages(messages), stream=True)
    if tools is not None:
        body["tools"] = tools
    names = _tools(tools if tools is not None else [])
    raw = encode(body)
    return PreparedOllamaChat(
        profile, raw, hashlib.sha256(raw).hexdigest(), frozenset(names)
    )


def validate_prepared(prepared: PreparedOllamaChat, profile: OllamaProfile) -> None:
    if not isinstance(prepared, PreparedOllamaChat) or prepared.profile != profile:
        raise OllamaDenied("native_profile_mismatch")
    body = obj(parse_json(prepared.body))
    # Reconstruct from canonical messages to reject forged dataclass instances,
    # option overrides, stripped flags, unsupported fields and stale tool scopes.
    expected = profile.body()
    expected.update(stream=True, messages=body.get("messages"))
    if "tools" in body:
        expected["tools"] = body["tools"]
    if (
        body != expected
        or not isinstance(body.get("messages"), list)
        or not body["messages"]
    ):
        raise OllamaDenied("native_preparation_mismatch")
    canonical = []
    for item in body["messages"]:
        message = dict(obj(item))
        if "thinking" in message:
            message["reasoning_content"] = message.pop("thinking")
        if "tool_name" in message:
            message["name"] = message.pop("tool_name")
        if "tool_calls" in message:
            if not isinstance(message["tool_calls"], list):
                raise OllamaDenied("invalid_tool_collection")
            calls = []
            for call in message["tool_calls"]:
                fn = obj(obj(call).get("function"))
                calls.append(
                    dict(
                        id=call.get("id"),
                        type="function",
                        function=dict(
                            name=fn.get("name"),
                            arguments=encode(fn.get("arguments")).decode(),
                        ),
                    )
                )
            message["tool_calls"] = calls
        canonical.append(message)
    rebuilt = prepare_chat(profile, canonical, tools=body.get("tools"))
    if rebuilt != prepared:
        raise OllamaDenied("native_preparation_mismatch")


class NativeChatDecoder:
    """Bounded NDJSON parser. Terminal/tool events withheld until validated EOF.

    EOF/done are protocol facts, NOT runner terminality/physical-release proof.
    The bridge additionally verifies terminal activity before publishing tools.
    """

    def __init__(self, prepared: PreparedOllamaChat) -> None:
        self.prepared = prepared
        self.buffer = bytearray()
        self.total = 0
        self.done: dict[str, Any] | None = None
        self.finished = False
        self.calls: list[dict[str, Any]] = []
        self.ids: set[str] = set()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        if self.finished:
            raise OllamaDenied("native_stream_already_finished")
        if not isinstance(data, bytes):
            raise OllamaDenied("invalid_native_chunk")
        self.total += len(data)
        if self.total > MAX_STREAM_BYTES:
            raise OllamaDenied("native_stream_too_large")
        self.buffer.extend(data)
        events = []
        while b"\n" in self.buffer:
            index = self.buffer.index(b"\n")
            if index > MAX_LINE_BYTES:
                raise OllamaDenied("native_line_too_large")
            raw = bytes(self.buffer[:index])
            del self.buffer[: index + 1]
            events.extend(self._record(raw))
        if len(self.buffer) > MAX_LINE_BYTES:
            raise OllamaDenied("native_line_too_large")
        return events

    def _record(self, raw: bytes) -> list[dict[str, Any]]:
        if not raw.strip():
            return []
        if self.done is not None:
            raise OllamaDenied("native_data_after_done")
        value = obj(parse_json(raw))
        if (
            "error" in value
            or value.get("remote_host")
            or value.get("remote_model")
            or value.get("model")
            != local_reference(self.prepared.profile.memory.target.artifact_id)
        ):
            raise OllamaDenied("native_stream_identity_or_error")
        if type(value.get("done")) is not bool:
            raise OllamaDenied("invalid_native_done")
        message = obj(value.get("message"))
        _keys(message, {"role", "content", "thinking", "tool_calls"})
        if message.get("role") != "assistant":
            raise OllamaDenied("invalid_native_role")
        if self.prepared.profile.think is False and message.get("thinking"):
            raise OllamaDenied("native_thinking_profile_mismatch")
        events = []
        for key, kind in (("thinking", "reasoning"), ("content", "content")):
            piece = text(message.get(key, ""))
            if piece:
                events.append({"type": kind, "text": piece})
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or len(calls) + len(self.calls) > MAX_TOOLS:
            raise OllamaDenied("invalid_tool_collection")
        for call in calls:
            _keys(obj(call), {"id", "function"})
            call_id = text(call.get("id"), label=True)
            if call_id in self.ids:
                raise OllamaDenied("duplicate_tool_id")
            self.ids.add(call_id)
            fn = obj(call.get("function"))
            _keys(fn, {"index", "name", "arguments"})
            if "index" in fn and integer(fn["index"]) >= MAX_TOOLS:
                raise OllamaDenied("invalid_native_tool_index")
            name = text(fn.get("name"), label=True)
            if name not in self.prepared.tool_names:
                raise OllamaDenied("unadvertised_native_tool")
            event = dict(
                type="tool_call",
                id=call_id,
                name=name,
                arguments=obj(fn.get("arguments")),
            )
            if "index" in fn:
                event["index"] = fn["index"]
            self.calls.append(event)
        if value["done"]:
            if value.get("done_reason") not in ("stop", "length"):
                raise OllamaDenied("unsupported_native_finish_reason")
            if self.calls and value["done_reason"] == "length":
                raise OllamaDenied("incomplete_tool_generation")
            integer(value.get("prompt_eval_count"))
            integer(value.get("eval_count"))
            for field in (
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
                "prompt_eval_cached_count",
            ):
                if field in value:
                    integer(value[field])
            self.done = value
        elif value.get("done_reason"):
            raise OllamaDenied("premature_native_finish_reason")
        return events

    def finish(self) -> list[dict[str, Any]]:
        if self.finished:
            raise OllamaDenied("native_stream_already_finished")
        self.finished = True
        events = self._record(bytes(self.buffer)) if self.buffer else []
        self.buffer.clear()
        if self.done is None:
            raise OllamaDenied("native_stream_incomplete")
        packet = self.done

        def rate(count: int, duration: object) -> float | None:
            return (
                count * 1_000_000_000 / duration
                if type(duration) is int and duration > 0
                else None
            )

        events.extend(self.calls)
        events.append(
            dict(
                type="usage",
                prompt_tokens=packet["prompt_eval_count"],
                completion_tokens=packet["eval_count"],
                reasoning_tokens=None,
                cached_tokens=packet.get("prompt_eval_cached_count"),
                prefill_tps=rate(
                    packet["prompt_eval_count"], packet.get("prompt_eval_duration")
                ),
                decode_tps=rate(packet["eval_count"], packet.get("eval_duration")),
                ttft=None,
                elapsed_s=packet["total_duration"] / 1_000_000_000
                if "total_duration" in packet
                else None,
                finish_reason="tool_calls" if self.calls else packet["done_reason"],
                native_finish_reason=packet["done_reason"],
            )
        )
        return events
