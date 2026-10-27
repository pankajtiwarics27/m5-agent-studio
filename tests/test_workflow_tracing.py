from __future__ import annotations

import json

import pytest

from workflow_tracing import TraceError, WorkflowTrace


def test_trace_correlates_events_and_exports_actionable_failure() -> None:
    now = [10.0]
    trace = WorkflowTrace("workflow-1", trace_id="trace-1", clock=lambda: now[0])
    start = trace.record("workflow.started", "running", attributes={"model": "fixture", "step": 1})
    now[0] = 11.0
    diagnostic = trace.failure("provider unavailable", "select an approved local alternative", retryable=True)

    assert start.trace_id == diagnostic.trace_id == "trace-1"
    assert len({event.span_id for event in trace.events()}) == 2
    assert trace.events()[-1].status == "failed"
    exported = json.loads(json.dumps(trace.export()))
    assert exported["trace_id"] == "trace-1"
    assert exported["events"][-1]["attributes"]["retryable"] == "True"
    assert diagnostic.to_mapping()["action"].startswith("select")


def test_terminal_trace_rejects_later_events() -> None:
    trace = WorkflowTrace("workflow-1")
    trace.record("workflow.cancelled", "cancelled")
    with pytest.raises(TraceError, match="closed"):
        trace.record("late.event", "running")


def test_invalid_trace_inputs_fail_closed() -> None:
    with pytest.raises(TraceError, match="workflow_id"):
        WorkflowTrace("")
    trace = WorkflowTrace("workflow")
    with pytest.raises(TraceError, match="unknown trace status"):
        trace.record("event", "unknown")  # type: ignore[arg-type]
    with pytest.raises(TraceError, match="reason"):
        trace.failure("", "retry", retryable=False)
