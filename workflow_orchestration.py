"""Provider-neutral orchestration primitives for streamed Studio workflows.

The orchestration layer coordinates stages, but it does not perform I/O itself.
Model streaming, persistence/observability, local-model cleanup, and request
disconnect checks are injected by the application. This keeps provider and
storage boundaries explicit and makes the workflow behavior testable offline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol


Message = dict[str, str]
ModelEvent = Mapping[str, Any]
StageId = int | str


class ModelStreamer(Protocol):
    """Stream normalized provider events for one model invocation."""

    def __call__(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        reasoning_mode: str,
    ) -> AsyncIterator[ModelEvent]: ...


class UsageRecorder(Protocol):
    """Record one normalized usage event without knowing the storage backend."""

    def __call__(self, workflow: str, model: str, usage: Mapping[str, Any]) -> None: ...


class DisconnectChecker(Protocol):
    def __call__(self) -> Awaitable[bool]: ...


class LocalModelPredicate(Protocol):
    def __call__(self, model: str) -> bool: ...


class ModelUnloader(Protocol):
    def __call__(self, model: str) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    """A provider-independent stage in a sequential workflow."""

    stage_id: StageId
    key: str
    title: str
    model: str
    system: str
    prompt: Callable[[Mapping[str, str]], str]
    max_tokens: int
    reasoning_mode: str = "off"
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class StageResult:
    stage_id: StageId
    key: str
    model: str
    content: str
    reasoning: str
    usage: dict[str, Any]


WorkflowEventKind = Literal["stage_start", "token", "stage_done", "unloading", "disconnected"]


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    """Internal event with a stable public payload and optional typed result."""

    kind: WorkflowEventKind
    data: dict[str, Any]
    result: StageResult | None = None


class WorkflowOrchestrator:
    """Run sequential model stages while preserving application-owned boundaries."""

    def __init__(
        self,
        *,
        stream_model: ModelStreamer,
        record_usage: UsageRecorder,
        is_local_model: LocalModelPredicate,
        unload_model: ModelUnloader,
        unload_target: Callable[[str], str] | None = None,
    ) -> None:
        self._stream_model = stream_model
        self._record_usage = record_usage
        self._is_local_model = is_local_model
        self._unload_model = unload_model
        self._unload_target = unload_target or (lambda model: model)

    async def run_stages(
        self,
        stages: Sequence[WorkflowStage],
        *,
        workflow: str,
        auto_unload: bool,
        is_disconnected: DisconnectChecker | None = None,
    ) -> AsyncIterator[WorkflowEvent]:
        """Run stages in order and yield normalized orchestration events.

        A provider exception is intentionally allowed to propagate. The caller
        owns its existing error-to-SSE/API contract and can decide whether a
        partially completed workflow is persisted. No stage is started after a
        disconnect has been observed.
        """
        context: dict[str, str] = {}
        for index, stage in enumerate(stages):
            if is_disconnected is not None and await is_disconnected():
                yield WorkflowEvent("disconnected", {"stage": stage.stage_id})
                return

            yield WorkflowEvent(
                "stage_start",
                {
                    "stage": stage.stage_id,
                    "key": stage.key,
                    "title": stage.title,
                    "model": stage.model,
                },
            )
            messages: list[Message] = [
                {"role": "system", "content": stage.system},
                {"role": "user", "content": stage.prompt(context)},
            ]
            result: StageResult | None = None
            async for event in self._run_stage(
                stage, messages, workflow=workflow, is_disconnected=is_disconnected
            ):
                yield event
                if event.kind == "disconnected":
                    return
                if event.kind == "stage_done":
                    result = event.result
            if result is None:
                return
            context[stage.key] = result.content or result.reasoning

            next_model = stages[index + 1].model if index + 1 < len(stages) else None
            if auto_unload and self._is_local_model(stage.model) and next_model != stage.model:
                yield WorkflowEvent("unloading", {"stage": stage.stage_id, "model": stage.model})
                await self._unload_model(self._unload_target(stage.model))

    async def _run_stage(
        self,
        stage: WorkflowStage,
        messages: list[Message],
        *,
        workflow: str,
        is_disconnected: DisconnectChecker | None,
    ) -> AsyncIterator[WorkflowEvent]:
        content: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, Any] = {}
        disconnected = False

        async def stream_once(
            reasoning_mode: str,
            max_tokens: int,
            *,
            record_usage: bool,
        ) -> AsyncIterator[WorkflowEvent]:
            nonlocal disconnected, usage
            async for item in self._stream_model(
                stage.model,
                messages,
                stage.temperature,
                max_tokens,
                reasoning_mode,
            ):
                if is_disconnected is not None and await is_disconnected():
                    disconnected = True
                    yield WorkflowEvent("disconnected", {"stage": stage.stage_id})
                    return
                event_type = item.get("type")
                if event_type == "content":
                    text = str(item.get("text") or "")
                    content.append(text)
                    yield WorkflowEvent("token", {"stage": stage.stage_id, "kind": "content", "text": text})
                elif event_type == "reasoning":
                    text = str(item.get("text") or "")
                    reasoning.append(text)
                    yield WorkflowEvent("token", {"stage": stage.stage_id, "kind": "reasoning", "text": text})
                elif event_type == "usage":
                    usage = dict(item)
                    if record_usage:
                        self._record_usage(workflow, stage.model, usage)

        async for event in stream_once(stage.reasoning_mode, stage.max_tokens, record_usage=True):
            yield event
        if disconnected:
            return

        # Preserve the existing local-model recovery path: a reasoning-only
        # response gets one content retry with thinking disabled.
        if not "".join(content).strip() and reasoning and self._is_local_model(stage.model):
            content.clear()
            async for event in stream_once("off", 4096, record_usage=False):
                yield event
                if disconnected:
                    return

        result = StageResult(stage.stage_id, stage.key, stage.model, "".join(content), "".join(reasoning), usage)
        yield WorkflowEvent(
            "stage_done",
            {"stage": stage.stage_id, "model": stage.model, "usage": usage},
            result,
        )
