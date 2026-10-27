"""Correlated structured traces and actionable workflow diagnostics."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

TraceStatus = Literal["running", "completed", "failed", "cancelled"]


class TraceError(ValueError):
    """Raised when trace data is malformed."""


@dataclass(frozen=True, slots=True)
class TraceEvent:
    trace_id: str
    span_id: str
    event: str
    status: TraceStatus
    timestamp: float
    detail: str = ""
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class FailureDiagnostic:
    trace_id: str
    reason: str
    action: str
    retryable: bool

    def to_mapping(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "reason": self.reason, "action": self.action, "retryable": self.retryable}


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise TraceError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


class WorkflowTrace:
    """In-memory correlated trace; callers can persist/export its immutable events."""

    def __init__(self, workflow_id: str, *, trace_id: str | None = None, clock: callable = time.time) -> None:
        self.workflow_id = _text(workflow_id, "workflow_id")
        self.trace_id = _text(trace_id or secrets.token_urlsafe(12), "trace_id")
        self._clock = clock
        self._events: list[TraceEvent] = []
        self._closed = False

    def record(
        self,
        event: str,
        status: TraceStatus,
        *,
        detail: str = "",
        attributes: dict[str, object] | None = None,
        span_id: str | None = None,
    ) -> TraceEvent:
        if self._closed:
            raise TraceError("trace is already closed.")
        if status not in {"running", "completed", "failed", "cancelled"}:
            raise TraceError(f"unknown trace status: {status}")
        normalized_attributes = tuple(sorted((str(key), str(value)) for key, value in (attributes or {}).items()))
        recorded = TraceEvent(self.trace_id, _text(span_id or secrets.token_urlsafe(8), "span_id"), _text(event, "event"), status, self._clock(), detail, normalized_attributes)
        self._events.append(recorded)
        if status in {"completed", "failed", "cancelled"}:
            self._closed = True
        return recorded

    def failure(self, reason: str, action: str, *, retryable: bool) -> FailureDiagnostic:
        diagnostic = FailureDiagnostic(self.trace_id, _text(reason, "reason"), _text(action, "action"), retryable)
        self.record("workflow.failure", "failed", detail=diagnostic.reason, attributes={"action": diagnostic.action, "retryable": retryable})
        return diagnostic

    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def export(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "trace_id": self.trace_id,
            "events": [
                {
                    "span_id": event.span_id,
                    "event": event.event,
                    "status": event.status,
                    "timestamp": event.timestamp,
                    "detail": event.detail,
                    "attributes": dict(event.attributes),
                }
                for event in self._events
            ],
        }
