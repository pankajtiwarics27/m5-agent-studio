"""Contract tests for the execution-spine boundaries (M5-P0-06).

These tests pin the semantics of ``execution_spine.DurableActionBoundary`` —
the authorize -> record-intent -> execute -> record-outcome seam extracted from
``app._run_authorized_tool``. They use in-memory fakes only: no SQLite, no
subprocess, no network. The full application suite exercises the same boundary
end-to-end through ``_run_authorized_tool`` (behavior stability evidence).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import execution_spine as spine


class FakePolicy:
    """Scriptable Policy double."""

    def __init__(self, decision: spine.AdmissionDecision | None = None, error: Exception | None = None):
        self.decision = decision if decision is not None else spine.AdmissionDecision(True, "", False)
        self.error = error
        self.calls: list[tuple[str, str, bool]] = []

    def effective_policy(self, requested: str) -> str:
        return requested if requested in {"plan", "standard", "elevated"} else "standard"

    def authorize(self, tool_name, args, policy, *, approved):
        self.calls.append((tool_name, policy, approved))
        if self.error is not None:
            raise self.error
        return self.decision


class FakeLedger:
    """Scriptable ActionLedger double that records the call order."""

    def __init__(self, start_error: Exception | None = None, finish_error: Exception | None = None):
        self.start_error = start_error
        self.finish_error = finish_error
        self.events: list[tuple[str, dict[str, Any]]] = []

    def start(self, session_id, tool_name, policy, explicit_approval, args):
        if self.start_error is not None:
            raise self.start_error
        self.events.append(("start", {"session_id": session_id, "tool_name": tool_name, "policy": policy,
                                     "explicit_approval": explicit_approval, "args": args}))
        return 101

    def finish(self, event_id, result, elapsed_ms, *, status="completed"):
        if self.finish_error is not None:
            raise self.finish_error
        self.events.append(("finish", {"event_id": event_id, "status": status, "elapsed_ms": elapsed_ms}))


async def _ok_executor() -> dict[str, Any]:
    return {"ok": True, "output": "done", "diff": "", "elapsed_ms": 5}


async def _boom_executor() -> dict[str, Any]:
    raise RuntimeError("executor exploded")


def _request(**overrides: Any) -> spine.ActionRequest:
    base = dict(tool_name="run_bash", args={"command": "echo hi"}, policy="standard",
                approved=False, session_id="s-1")
    base.update(overrides)
    return spine.ActionRequest(**base)


def test_allowed_action_records_intent_then_completed_outcome() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", False))
    ledger = FakeLedger()

    result = asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), _ok_executor))

    assert result == {"ok": True, "output": "done", "diff": "", "elapsed_ms": 5}
    assert [kind for kind, _ in ledger.events] == ["start", "finish"]
    assert ledger.events[1][1] == {"event_id": 101, "status": "completed", "elapsed_ms": 5}


def test_policy_denial_blocks_execution_and_records_denied() -> None:
    policy = FakePolicy(spine.AdmissionDecision(False, "not allowed", False))
    ledger = FakeLedger()
    called = []

    async def executor():
        called.append(1)
        return await _ok_executor()

    result = asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), executor))

    assert called == []
    assert result["ok"] is False
    assert result["output"] == "Denied: not allowed"
    assert ledger.events[1][1]["status"] == "denied"


def test_policy_evaluation_error_denies_fail_closed() -> None:
    policy = FakePolicy(error=RuntimeError("policy backend down"))
    ledger = FakeLedger()
    called = []

    async def executor():
        called.append(1)
        return await _ok_executor()

    result = asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), executor))

    assert called == []
    assert result["output"] == "Denied: Authorization policy evaluation failed; invocation denied."


def test_additional_denial_overrides_an_allow() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", True))
    ledger = FakeLedger()
    called = []

    async def executor():
        called.append(1)
        return await _ok_executor()

    result = asyncio.run(
        spine.DurableActionBoundary(policy, ledger).admit_and_execute(
            _request(approved=True, additional_denial="session policy denied"), executor
        )
    )

    assert called == []
    assert result["output"] == "Denied: session policy denied"
    # Original semantics: a hard additional denial is recorded without approval.
    assert ledger.events[0][1]["explicit_approval"] is False


def test_intent_record_failure_blocks_dispatch() -> None:
    policy = FakePolicy()
    ledger = FakeLedger(start_error=RuntimeError("disk full"))
    called = []

    async def executor():
        called.append(1)
        return await _ok_executor()

    with pytest.raises(RuntimeError, match="disk full"):
        asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), executor))

    assert called == []  # durable intent before execution: no record, no action


def test_executor_failure_is_recorded_and_reraised() -> None:
    policy = FakePolicy()
    ledger = FakeLedger()

    with pytest.raises(RuntimeError, match="executor exploded"):
        asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), _boom_executor))

    assert [kind for kind, _ in ledger.events] == ["start", "finish"]
    assert ledger.events[1][1]["status"] == "failed"


def test_outcome_finalization_failure_surfaces_as_audit_warning() -> None:
    policy = FakePolicy()
    ledger = FakeLedger(finish_error=RuntimeError("ledger locked"))

    result = asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), _ok_executor))

    assert result["ok"] is True
    assert result["audit_warning"] == "Tool result could not be finalized in the local audit log."


def test_denial_finalization_failure_still_reports_denial() -> None:
    policy = FakePolicy(spine.AdmissionDecision(False, "no", False))
    ledger = FakeLedger(finish_error=RuntimeError("ledger locked"))

    result = asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), _ok_executor))

    assert result["ok"] is False
    assert result["audit_warning"] == "Denial could not be finalized in the local audit log."


def test_denial_as_exception_raises_permission_error() -> None:
    policy = FakePolicy(spine.AdmissionDecision(False, "no", False))
    ledger = FakeLedger()

    with pytest.raises(PermissionError, match="Denied: no"):
        asyncio.run(
            spine.DurableActionBoundary(policy, ledger).admit_and_execute(
                _request(denial_as_exception=True), _ok_executor
            )
        )

    assert ledger.events[1][1]["status"] == "denied"


def test_explicit_approval_requires_both_flag_and_decision() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", True))
    ledger = FakeLedger()

    asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(approved=True), _ok_executor))
    asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(approved=False), _ok_executor))

    assert ledger.events[0][1]["explicit_approval"] is True
    assert ledger.events[2][1]["explicit_approval"] is False


def test_effective_policy_resolved_once_and_fed_to_policy_and_ledger() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", False))
    ledger = FakeLedger()

    asyncio.run(
        spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(policy="bogus"), _ok_executor)
    )

    assert policy.calls == [("run_bash", "standard", False)]
    assert ledger.events[0][1]["policy"] == "standard"


def test_task_context_resolver_exception_fails_closed() -> None:
    class BrokenResolver:
        def resolve_task_context(self, request: spine.ActionRequest) -> spine.TaskAdmissionSnapshot | None:
            raise RuntimeError("sqlite busy")

    policy = FakePolicy(spine.AdmissionDecision(True, "", False))
    ledger = FakeLedger()
    called: list[int] = []

    async def tracked_executor():
        called.append(1)
        return await _ok_executor()

    result = asyncio.run(
        spine.DurableActionBoundary(
            policy, ledger, task_context_resolver=BrokenResolver()
        ).admit_and_execute(_request(task_id="t1"), tracked_executor)
    )
    assert called == []
    assert result["ok"] is False
    assert "Task lifecycle context resolution failed" in result["output"]
    assert ledger.events[1][1]["status"] == "denied"


def test_active_admission_scope_is_minted_only_during_execution_and_reset_on_error() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", False))
    ledger = FakeLedger()

    assert spine.current_admission_scope() is None
    with pytest.raises(spine.DirectExecutorBypassError):
        spine.require_active_admission(expected_tool="run_bash")

    async def scoped_executor():
        scope = spine.require_active_admission(
            expected_tool="run_bash", expected_action_classes=frozenset({"consequential"})
        )
        assert scope.session_id == "s-1"
        raise RuntimeError("boom inside scope")

    with pytest.raises(RuntimeError, match="boom inside scope"):
        asyncio.run(spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(), scoped_executor))

    assert spine.current_admission_scope() is None


def test_epoch_without_task_context_is_denied() -> None:
    policy = FakePolicy(spine.AdmissionDecision(True, "", False))
    ledger = FakeLedger()
    result = asyncio.run(
        spine.DurableActionBoundary(policy, ledger).admit_and_execute(_request(epoch=2), _ok_executor)
    )
    assert result["ok"] is False
    assert "without an authoritative task context" in result["output"]


def test_classify_action_separates_managed_reads_from_consequential_effects() -> None:
    assert spine.classify_action("read_file", {"path": "a.py"}) == "read_only"
    assert spine.classify_action("ctx_run_block", {"block": " JSON-PEEK "}) == "read_only"
    assert spine.classify_action("ctx_run_block", {"block": "Test-Summary"}) == "consequential"
    assert spine.classify_action("write_file", {"path": "a.py"}) == "managed_edit"
    assert spine.classify_action("skill_execute_script", {"script": "s.py"}) == "consequential"
    assert spine.classify_action("mcp__srv__call", {}) == "external"
    assert spine.classify_action("unknown_tool", {"action_class": "read_only"}) == "consequential"

