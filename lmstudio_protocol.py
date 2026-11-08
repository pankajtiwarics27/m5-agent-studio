"""Dormant LC-06 wire subset. No I/O, inference execution, SDK or token counter."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from shared_memory_admission import ModelMemoryProfile

SOURCE_PIN = "8c7159be3ffe364af10aa8ae6f5a7570e9402089"
MAX_BYTES = 4 * 1024 * 1024
CONFIG_KEYS = {
    "context_length",
    "eval_batch_size",
    "flash_attention",
    "num_experts",
    "offload_kv_cache_to_gpu",
    "parallel",
}


class LmStudioDenied(RuntimeError):
    """Bounded codes, never native bodies, prompts, paths or credentials."""


def label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isprintable()
        or value.strip() != value
    ):
        raise LmStudioDenied("invalid_label")
    return value


def integer(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value < 2**63:
        raise LmStudioDenied("invalid_integer")
    return value


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise LmStudioDenied("invalid_object")
    return value


def _json_value(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise LmStudioDenied("json_depth_exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LmStudioDenied("invalid_json_key")
            _json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
    elif type(value) is float:
        if not math.isfinite(value):
            raise LmStudioDenied("nonfinite_json")
    elif value is not None and type(value) not in (bool, int, str):
        raise LmStudioDenied("invalid_json_value")


def encode(value: object) -> bytes:
    _json_value(value)
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LmStudioDenied("invalid_json") from None
    if len(raw) > MAX_BYTES:
        raise LmStudioDenied("json_size_exceeded")
    return raw


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise LmStudioDenied("duplicate_json_key")
        result[key] = value
    return result


def decode(raw: bytes | str) -> Any:
    if len(raw) > MAX_BYTES:
        raise LmStudioDenied("json_size_exceeded")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        _json_value(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LmStudioDenied("invalid_json") from None


def config(value: object) -> dict[str, Any]:
    result = obj(value)
    if set(result) - CONFIG_KEYS:
        raise LmStudioDenied("unsupported_config")
    integer(result.get("context_length"), positive=True)
    for key, val in result.items():
        if key in ("flash_attention", "offload_kv_cache_to_gpu"):
            if type(val) is not bool:
                raise LmStudioDenied("invalid_config")
        else:
            integer(val, positive=True)
    return result


@dataclass(frozen=True, slots=True)
class LmStudioProfile:
    memory: ModelMemoryProfile
    format: str
    config_json: str
    selected_variant: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory, ModelMemoryProfile) or self.format not in (
            "gguf",
            "mlx",
        ):
            raise LmStudioDenied("invalid_profile")
        if self.selected_variant is not None:
            label(self.selected_variant)
        final = config(decode(self.config_json))
        if self.format == "mlx" and set(final) - {"context_length", "parallel"}:
            raise LmStudioDenied("backend_config_unsupported")
        if final["context_length"] != self.memory.context_tokens:
            raise LmStudioDenied("context_profile_mismatch")
        encode(final)

    @property
    def digest(self) -> str:
        return hashlib.sha256(encode(asdict(self))).hexdigest()

    def load_config(self) -> dict[str, Any]:
        # `parallel` is reported by list, NOT documented as a load argument or
        # echo field. Never silently set an unsupported knob to force a fit.
        return {
            k: v for k, v in config(decode(self.config_json)).items() if k != "parallel"
        }

    def load_body(self) -> bytes:
        return encode(
            dict(
                model=self.memory.target.artifact_id,
                echo_load_config=True,
                **self.load_config(),
            )
        )


@dataclass(frozen=True, slots=True)
class LmInstance:
    instance_id: str
    artifact_key: str
    model_type: str
    format: str | None
    config_json: str


@dataclass(frozen=True, slots=True)
class LmArtifact:
    key: str
    model_type: str
    format: str | None
    selected_variant: str | None


@dataclass(frozen=True, slots=True)
class LmCatalog:
    keys: tuple[str, ...]
    instances: tuple[LmInstance, ...]
    artifacts: tuple[LmArtifact, ...]


def parse_catalog(payload: object) -> LmCatalog:
    models = obj(payload).get("models")
    if not isinstance(models, list) or len(models) > 4096:
        raise LmStudioDenied("invalid_catalog")
    keys: list[str] = []
    instances: list[LmInstance] = []
    artifacts: list[LmArtifact] = []
    ids: set[str] = set()
    for model in models:
        row = obj(model)
        key = label(row.get("key"))
        if (
            key in keys
            or row.get("type") not in ("llm", "embedding", "decision")
            or row.get("format") not in ("gguf", "mlx", None)
        ):
            raise LmStudioDenied("catalog_identity_conflict")
        keys.append(key)
        variant = row.get("selected_variant")
        if "variants" in row or variant is not None:
            variants = row.get("variants")
            if (
                not isinstance(variants, list)
                or not variants
                or any(not isinstance(x, str) for x in variants)
                or variant not in variants
            ):
                raise LmStudioDenied("variant_unknown")
            label(variant)
        artifacts.append(LmArtifact(key, row["type"], row.get("format"), variant))
        integer(row.get("size_bytes"))  # artifact size, NOT resident memory
        loaded = row.get("loaded_instances")
        if not isinstance(loaded, list) or len(instances) + len(loaded) > 4096:
            raise LmStudioDenied("loaded_instances_unknown")
        for item in loaded:
            item = obj(item)
            instance_id = label(item.get("id"))
            if instance_id in ids:
                raise LmStudioDenied("duplicate_instance")
            ids.add(instance_id)
            instances.append(
                LmInstance(
                    instance_id,
                    key,
                    row["type"],
                    row.get("format"),
                    encode(config(item.get("config"))).decode(),
                )
            )
    return LmCatalog(tuple(keys), tuple(instances), tuple(artifacts))


@dataclass(frozen=True, slots=True)
class LmPreparedInference:
    instance_id: str
    profile_digest: str
    body: bytes
    digest: str


def prepare_inference(
    instance_id: str, profile: LmStudioProfile, request: dict[str, Any]
) -> LmPreparedInference:
    """Own unchanged compatible-wire JSON; no native-v1-chat substitution.

    Full template/tool/reasoning/context-fit equivalence is an installed gate,
    not implied by these structural checks. The final consumer is LC-08 work.
    """
    label(instance_id)
    raw = encode(obj(request))
    body = obj(decode(raw))
    allowed = {
        "model",
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "repeat_penalty",
        "seed",
        "tools",
        "tool_choice",
    }
    if (
        set(body) - allowed
        or body.get("model") != instance_id
        or body.get("stream") is not True
        or not isinstance(body.get("messages"), list)
        or not body["messages"]
        or integer(body.get("max_tokens"), positive=True)
        > profile.memory.context_tokens
    ):
        raise LmStudioDenied("inference_request_unsupported")
    if "stream_options" in body and body["stream_options"] != {"include_usage": True}:
        raise LmStudioDenied("inference_request_unsupported")
    for message in body["messages"]:
        message = obj(message)
        if message.get("role") not in ("system", "user", "assistant", "tool"):
            raise LmStudioDenied("message_role_unsupported")
        # Deliberately text-only. Do not flatten media or strip reasoning/tool
        # fields: the qualified driver must preserve this entire owned payload.
        content = message.get("content")
        if not isinstance(content, str) and not (
            content is None
            and message.get("role") == "assistant"
            and message.get("tool_calls")
        ):
            raise LmStudioDenied("message_content_unsupported")
        if set(message) - {
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
            "tool_call_id",
            "name",
        }:
            raise LmStudioDenied("message_field_unsupported")
    if "tools" in body and not isinstance(body["tools"], list):
        raise LmStudioDenied("tools_unsupported")
    return LmPreparedInference(
        instance_id, profile.digest, raw, hashlib.sha256(raw).hexdigest()
    )


class LmSseDecoder:
    """Validate bounded SSE, retain JSON data bytes; never execute tool deltas.

    Once a tool delta occurs, preserve that and all following packets in order
    until independent terminal confirmation. This is not a canonical executor.
    """

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.buffer = bytearray()
        self.data: list[bytes] = []
        self.total = 0
        self.done = False
        self.finished_choice = False
        self.finished = False
        self.pending: list[bytes] = []
        self.tool_seen = False

    def _frame(self) -> list[bytes]:
        if not self.data:
            return []
        raw = b"\n".join(self.data)
        self.data.clear()
        if self.done:
            raise LmStudioDenied("data_after_done")
        if raw == b"[DONE]":
            if not self.finished_choice:
                raise LmStudioDenied("choice_incomplete")
            self.done = True
            return []
        packet = obj(decode(raw))
        if "error" in packet or packet.get("model") != self.instance_id:
            raise LmStudioDenied("response_identity_or_error")
        choices = packet.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise LmStudioDenied("choices_unsupported")
        for choice in choices:
            choice = obj(choice)
            if (
                choice.get("index") != 0
                or type(choice.get("index")) is not int
                or self.finished_choice
            ):
                raise LmStudioDenied("choice_identity_or_order")
            delta = obj(choice.get("delta"))
            if "function_call" in delta:
                raise LmStudioDenied("legacy_function_call_unsupported")
            if delta.get("tool_calls"):
                self.tool_seen = True
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason not in ("stop", "tool_calls", "length") or (
                    self.tool_seen and reason == "length"
                ):
                    raise LmStudioDenied("finish_reason_unsupported")
                self.finished_choice = True
        if not choices and not isinstance(packet.get("usage"), dict):
            raise LmStudioDenied("empty_response_packet")
        if self.pending or self.tool_seen or self.finished_choice or "usage" in packet:
            self.pending.append(raw)
            return []
        return [raw]

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.finished or not isinstance(chunk, bytes):
            raise LmStudioDenied("invalid_stream_chunk")
        self.total += len(chunk)
        if self.total > 16 * 1024 * 1024:
            raise LmStudioDenied("stream_size_exceeded")
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            index = self.buffer.index(b"\n")
            if index > 1024 * 1024:
                raise LmStudioDenied("sse_line_size_exceeded")
            line = bytes(self.buffer[:index]).removesuffix(b"\r")
            del self.buffer[: index + 1]
            if not line:
                result.extend(self._frame())
            elif line.startswith(b"data:"):
                self.data.append(line[5:].removeprefix(b" "))
                if sum(map(len, self.data)) > MAX_BYTES:
                    raise LmStudioDenied("sse_frame_size_exceeded")
            elif not line.startswith(b":"):
                raise LmStudioDenied("sse_field_unsupported")
        if len(self.buffer) > 1024 * 1024:
            raise LmStudioDenied("sse_line_size_exceeded")
        return result

    def finish(self) -> tuple[bytes, ...]:
        if self.finished or self.buffer.strip() or self.data or not self.done:
            raise LmStudioDenied("stream_incomplete")
        self.finished = True
        return tuple(self.pending)
