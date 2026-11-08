"""LC-08 warm native executor. No acquisition, transport or producer is installed.

A synchronous trusted planner supplies an ALREADY-admitted request and scoped
native/fit evidence. This module never creates authority, cold-loads, settles
memory or treats native ACK/cancel as physical release. Cold orchestration is a
separate consumer of the existing coordinator/bridge contracts.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import lmstudio_protocol as lp
import ollama_protocol as op
from lmstudio_lifecycle import LmActivity, LmPermit, LmStudioLifecycleBridge
from model_lifecycle_coordinator import RequestLease
from ollama_lifecycle import OllamaActivity, OllamaLifecycleBridge
from ollama_lifecycle import OllamaPermit, _profile_digest
from request_fit import (
    FitProfile,
    OutputBudget,
    RequestFitDenied,
    decode,
    PreparedRequest,
    number,
    prepare_request,
    reference,
)
from request_fit_admission import (
    CounterQualification,
    CountReceipt,
    FitObservation,
    FitPermit,
    RequestFitAdmission,
)
from runtime_dispatch_boundary import RuntimeDispatchCall, RuntimeDispatchDenied
from runtime_persistence import assert_no_active_write_transaction
from shared_memory_admission import MemoryLease, MemorySample


class NativeFitDenied(RuntimeDispatchDenied):
    """Only LC-07's fixed codes/numeric diagnostics cross the application boundary."""

    def __init__(self, error: RequestFitDenied) -> None:
        super().__init__("coordinated_execution_unavailable")
        self.fit_error = error
        self.args = (error.code,)

    def to_mapping(self) -> dict[str, Any]:
        return self.fit_error.to_mapping()


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeFitInputs:
    service: RequestFitAdmission
    profile: FitProfile
    permit: FitPermit
    memory: MemoryLease
    qualification: CounterQualification | None
    counter: (
        Callable[[PreparedRequest, CounterQualification], Awaitable[CountReceipt]]
        | None
    )
    observe: Callable[[], Awaitable[FitObservation]]
    sample: Callable[[], Awaitable[MemorySample]]


@dataclass(frozen=True, slots=True, kw_only=True)
class OllamaExecution:
    bridge: OllamaLifecycleBridge
    permit: OllamaPermit
    activity: OllamaActivity
    terminal: Callable[[], Awaitable[OllamaActivity]]
    prepared: op.PreparedOllamaChat
    fit: NativeFitInputs


@dataclass(frozen=True, slots=True, kw_only=True)
class LmExecution:
    bridge: LmStudioLifecycleBridge
    permit: LmPermit
    activity: LmActivity
    terminal: Callable[[], Awaitable[LmActivity]]
    prepared: lp.LmPreparedInference
    fit: NativeFitInputs


Execution = OllamaExecution | LmExecution


def application_input(call: RuntimeDispatchCall) -> dict[str, Any]:
    if not isinstance(call, RuntimeDispatchCall) or call.action != "infer":
        raise RequestFitDenied("invalid_request")
    value = decode(call.payload)
    if set(value) != {
        "messages",
        "tools",
        "temperature",
        "max_tokens",
        "reasoning_mode",
    }:
        raise RequestFitDenied("invalid_request")
    number(value["max_tokens"], positive=True)
    if (
        not isinstance(value["messages"], list)
        or not value["messages"]
        or value["tools"] is not None
        and not isinstance(value["tools"], list)
        or type(value["temperature"]) not in (int, float)
        or not math.isfinite(value["temperature"])
        or not isinstance(value["reasoning_mode"], str)
    ):
        raise RequestFitDenied("invalid_request")
    return value


def prepare_ollama_input(
    call: RuntimeDispatchCall, profile: op.OllamaProfile
) -> op.PreparedOllamaChat:
    value = application_input(call)
    if call.runtime != "ollama" or call.model != profile.memory.target.artifact_id:
        raise RequestFitDenied("profile_mismatch")
    options = op.parse_json(profile.options_json)
    # Approved profile is immutable. Never insert an option/default or silently
    # turn requested xhigh into high/off, change temperature or cap output.
    requested_think: object = {
        "off": False,
        "on": True,
        "low": "low",
        "medium": "medium",
        "high": "high",
    }.get(value["reasoning_mode"])
    if (
        requested_think is None
        or type(requested_think) is not type(profile.think)
        or requested_think != profile.think
    ):
        raise RequestFitDenied("profile_mismatch")
    if (
        options.get("num_predict") != value["max_tokens"]
        or options.get("temperature") != value["temperature"]
    ):
        raise RequestFitDenied("output_budget_unknown")
    return op.prepare_chat(profile, value["messages"], tools=value["tools"])


def prepare_lm_input(
    call: RuntimeDispatchCall, lease: RequestLease, profile: lp.LmStudioProfile
) -> lp.LmPreparedInference:
    value = application_input(call)
    if (
        call.runtime != "lmstudio"
        or call.model != lease.target.artifact_id
        or lease.target != profile.memory.target
    ):
        raise RequestFitDenied("profile_mismatch")
    # No REST reasoning-level knob has been established here. A separately
    # installed-scoped `reasoning_mode_off` capability is required by the executor
    # for off; all other levels explicitly refuse rather than losing the option.
    if value["reasoning_mode"] != "off":
        raise RequestFitDenied("profile_mismatch")
    body = dict(
        model=lease.instance_id,
        messages=value["messages"],
        temperature=value["temperature"],
        max_tokens=value["max_tokens"],
        stream=True,
        stream_options={"include_usage": True},
    )
    if value["tools"] is not None:
        body["tools"] = value["tools"]
    return lp.prepare_inference(lease.instance_id, profile, body)


class CanonicalNativePackets:
    """Native structural calls are never inferred from text or reasoning.

    Tool/usage events are withheld until the source native bridge is exhausted
    (including its independent native terminal check). Never manufacture usage,
    throughput, call IDs or partial-JSON tool arguments.
    """

    def __init__(self, tools: object) -> None:
        self.names: set[str] = set()
        if tools is not None:
            if not isinstance(tools, list):
                raise RequestFitDenied("invalid_request")
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(
                    tool.get("function"), dict
                ):
                    raise RequestFitDenied("invalid_request")
                self.names.add(reference(tool["function"].get("name")))
        self.calls: dict[int, dict[str, str]] = {}
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.held: list[dict[str, Any]] = []

    def ollama(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind in ("content", "reasoning") and isinstance(event.get("text"), str):
            return [event]
        if kind == "tool_call":
            if event.get("name") not in self.names or not isinstance(
                event.get("arguments"), dict
            ):
                raise RequestFitDenied("invalid_request")
            reference(event.get("id"))
            self.held.append(event)
        elif kind == "usage":
            if self.usage is not None:
                raise RequestFitDenied("invalid_request")
            self.usage = event
        else:
            raise RequestFitDenied("invalid_request")
        return []

    def lm(self, raw: bytes) -> list[dict[str, Any]]:
        packet = decode(raw)
        result = []
        for choice in packet.get("choices", []):
            delta = choice.get("delta", {})
            if set(delta) - {"role", "content", "reasoning_content", "tool_calls"}:
                raise RequestFitDenied("invalid_request")
            for key, kind in (
                ("content", "content"),
                ("reasoning_content", "reasoning"),
            ):
                text = delta.get(key)
                if text is not None:
                    if not isinstance(text, str):
                        raise RequestFitDenied("invalid_request")
                    if text:
                        result.append(dict(type=kind, text=text))
            calls = delta.get("tool_calls", [])
            if not isinstance(calls, list):
                raise RequestFitDenied("invalid_request")
            for call in calls:
                if not isinstance(call, dict) or set(call) - {
                    "index",
                    "id",
                    "type",
                    "function",
                }:
                    raise RequestFitDenied("invalid_request")
                index = number(call.get("index"))
                if index >= 16 or call.get("type", "function") != "function":
                    raise RequestFitDenied("invalid_request")
                part = self.calls.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if "id" in call:
                    if part["id"]:
                        raise RequestFitDenied("invalid_request")
                    part["id"] = reference(call["id"])
                fn = call.get("function", {})
                if not isinstance(fn, dict) or set(fn) - {"name", "arguments"}:
                    raise RequestFitDenied("invalid_request")
                for key in ("name", "arguments"):
                    piece = fn.get(key, "")
                    if (
                        not isinstance(piece, str)
                        or len(part[key]) + len(piece) > 65536
                    ):
                        raise RequestFitDenied("invalid_request")
                    part[key] += piece
            if choice.get("finish_reason") is not None:
                if self.finish_reason is not None:
                    raise RequestFitDenied("invalid_request")
                self.finish_reason = choice["finish_reason"]
        if packet.get("usage") is not None:
            if self.usage is not None or not isinstance(packet["usage"], dict):
                raise RequestFitDenied("invalid_request")
            usage = packet["usage"]
            counts = {}
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                counts[field] = number(usage[field]) if field in usage else None
            self.usage = dict(
                type="usage",
                **counts,
                reasoning_tokens=None,
                cached_tokens=None,
                decode_tps=None,
                prefill_tps=None,
                ttft=None,
                elapsed_s=None,
            )
        return result

    def finish(self, runtime: str) -> list[dict[str, Any]]:
        if runtime == "lmstudio":
            if self.finish_reason not in ("stop", "length", "tool_calls") or bool(
                self.calls
            ) != (self.finish_reason == "tool_calls"):
                raise RequestFitDenied("invalid_request")
            ids: set[str] = set()
            for index, part in sorted(self.calls.items()):
                identifier = reference(part["id"])
                if identifier in ids or part["name"] not in self.names:
                    raise RequestFitDenied("invalid_request")
                ids.add(identifier)
                arguments = decode(part["arguments"].encode())
                self.held.append(
                    dict(
                        type="tool_call",
                        index=index,
                        id=identifier,
                        name=part["name"],
                        arguments=arguments,
                        arguments_json=part["arguments"],
                    )
                )
            if self.usage is None:
                self.usage = dict(
                    type="usage",
                    prompt_tokens=None,
                    completion_tokens=None,
                    total_tokens=None,
                    reasoning_tokens=None,
                    cached_tokens=None,
                    decode_tps=None,
                    prefill_tps=None,
                    ttft=None,
                    elapsed_s=None,
                )
            self.usage["finish_reason"] = self.finish_reason
        if self.usage is None:
            raise RequestFitDenied("invalid_request")
        return [*self.held, self.usage]


class NativeDispatchExecutor:
    """Concrete warm execution; no default planner, cold loads or controls.

    The synchronous planner may not perform I/O. It supplies native permits and
    already-owned residency, never an arbitrary transport/stream callback. Every
    count and inference still passes the existing concrete service/bridge gates.
    """

    def __init__(
        self, plan: Callable[[RuntimeDispatchCall], Execution] | None = None
    ) -> None:
        self.plan = plan

    async def command(self, call: RuntimeDispatchCall) -> dict[str, Any]:
        raise RuntimeDispatchDenied("coordinated_backend_unavailable")

    async def stream(self, call: RuntimeDispatchCall) -> AsyncIterator[dict[str, Any]]:
        assert_no_active_write_transaction("coordinated native plan")
        if self.plan is None:
            raise RuntimeDispatchDenied("coordinated_backend_unavailable")
        job = self.plan(call)
        if not isinstance(job, (OllamaExecution, LmExecution)):
            raise RuntimeDispatchDenied("coordinated_backend_unavailable")
        source: AsyncGenerator[Any, None] | None = None
        native: op.PreparedOllamaChat | lp.LmPreparedInference
        try:
            if not isinstance(job.fit, NativeFitInputs) or not isinstance(
                job.fit.service, RequestFitAdmission
            ):
                raise RequestFitDenied("authority_denied")
            lease = job.permit.intent
            fit = job.fit
            if not isinstance(lease, RequestLease) or fit.permit.lease != lease:
                raise RequestFitDenied("lease_stale")
            if isinstance(job, OllamaExecution):
                if (
                    not isinstance(job.bridge, OllamaLifecycleBridge)
                    or call.runtime != "ollama"
                    or job.bridge.coordinator is not fit.service.coord
                    or job.bridge.memory is not fit.service.memory
                    or fit.profile.native_profile_digest
                    != _profile_digest(job.permit.qualification.profile)
                ):
                    raise RequestFitDenied("profile_mismatch")
                native = prepare_ollama_input(call, job.permit.qualification.profile)
            else:
                if (
                    not isinstance(job.bridge, LmStudioLifecycleBridge)
                    or call.runtime != "lmstudio"
                    or job.bridge.coord is not fit.service.coord
                    or job.bridge.memory is not fit.service.memory
                    or fit.profile.native_profile_digest
                    != job.permit.qualification.profile.digest
                ):
                    raise RequestFitDenied("profile_mismatch")
                q = job.permit.qualification
                if not any(
                    c.name == "reasoning_mode_off"
                    and c.state_for_installed_scope(
                        runtime_id=q.runtime_id,
                        runtime_version=q.version,
                        profile_fingerprint=lease.target.profile_fingerprint,
                    )
                    == "supported"
                    for c in q.capabilities
                ):
                    raise RequestFitDenied("profile_mismatch")
                native = prepare_lm_input(call, lease, q.profile)
            if native != job.prepared or job.permit.request_digest != native.digest:
                raise RequestFitDenied("profile_mismatch")
            if job.bridge.admit_request != fit.service.authorizes:
                raise RequestFitDenied("authority_denied")
            inputs = application_input(call)
            prepared = prepare_request(
                lease,
                fit.profile,
                OutputBudget(inputs["max_tokens"]),
                native.body,
                origin="coordinated_dispatch",
            )
            await fit.service.evaluate(
                fit.permit,
                prepared,
                lease=fit.memory,
                qualification=fit.qualification,
                counter=fit.counter,
                observe=fit.observe,
            )
            sample = await fit.sample()
            packets = CanonicalNativePackets(inputs["tools"])
            if isinstance(job, OllamaExecution):
                source = cast(
                    AsyncGenerator[Any, None],
                    job.bridge.infer(
                        job.permit,
                        job.prepared,
                        job.activity,
                        lease=fit.memory,
                        sample=sample,
                        produce_terminal=job.terminal,
                    ),
                )
                async for event in source:
                    for item in packets.ollama(event):
                        yield item
            else:
                source = cast(
                    AsyncGenerator[Any, None],
                    job.bridge.infer(
                        job.permit,
                        job.prepared,
                        job.activity,
                        lease=fit.memory,
                        sample=sample,
                        produce_terminal=job.terminal,
                    ),
                )
                async for raw in source:
                    for item in packets.lm(raw):
                        yield item
            for item in packets.finish(call.runtime):
                yield item
        except RequestFitDenied as error:
            raise NativeFitDenied(error) from None
        except RuntimeDispatchDenied:
            raise
        except Exception:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None
        finally:
            try:
                if source is not None:
                    await source.aclose()
            finally:
                if (
                    isinstance(job.fit, NativeFitInputs)
                    and isinstance(job.fit.service, RequestFitAdmission)
                    and isinstance(job.fit.permit, FitPermit)
                ):
                    job.fit.service.revoke(job.fit.permit.lease)
