"""Narrow execution-spine seams: policy, durable action ledger, admission (M5-P0-06).

The M5 2.0 execution spine needs auditable boundaries between the runtime and
its persistence, policy, and action-admission machinery, without a monolith
rewrite. This module defines those narrow interfaces (Protocols) plus the
default composition used by the served application:

- ``Policy`` -- authorization decision for one tool invocation;
- ``ActionLedger`` -- durable intent (start) before execution and durable
  outcome (finish) after;
- ``DurableActionBoundary`` -- authorize -> record intent -> execute ->
  record outcome, with the fail-closed guarantees pinned by
  ``tests/test_execution_spine.py``.

``app.py`` wires its existing inline policy (``authorize_tool_invocation``)
and audit tables (``_audit_tool_start`` / ``_audit_tool_finish``) behind these
interfaces at the first critical entry point (``_run_authorized_tool``, which
gates harness tool calls and MCP tool calls alike). Coverage is deliberately
partial: the maintained inventory of entry points that do NOT yet cross these
boundaries lives in ``docs/m5-2-0-bypass-inventory.md``. This extraction is
not intended to change behavior.

This module is stdlib-only and is inlined into the standalone artifact by
``scripts/build_studio.py`` (``INLINED_MODULES``); keep its imports to the
standard library and free of references to ``app``.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of a policy check for one tool invocation."""

    allowed: bool
    reason: str = ""
    requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One consequential action submitted to the admission boundary."""

    tool_name: str
    args: dict[str, Any]
    policy: str
    approved: bool
    session_id: str
    additional_denial: str | None = None
    denial_as_exception: bool = False


class Policy(Protocol):
    """Authorization seam: decide whether an action may execute."""

    def effective_policy(self, requested: str) -> str:
        """Resolve the requested policy name to a known effective policy."""
        ...

    def authorize(
        self, tool_name: str, args: dict[str, Any], policy: str, *, approved: bool
    ) -> AdmissionDecision:
        """Return a deny-by-default decision for one invocation."""
        ...


class ActionLedger(Protocol):
    """Persistence seam: durable intent before execution, durable outcome after."""

    def start(
        self, session_id: str, tool_name: str, policy: str, explicit_approval: bool, args: dict[str, Any]
    ) -> int:
        """Durably record the action attempt; returns the ledger event id."""
        ...

    def finish(
        self, event_id: int, result: dict[str, Any], elapsed_ms: int, *, status: str = "completed"
    ) -> None:
        """Durably record the outcome for a previously started action."""
        ...


Executor = Callable[[], Awaitable[dict[str, Any]]]


class DurableActionBoundary:
    """Authorize -> durably record intent -> execute -> durably record outcome.

    Guarantees pinned by ``tests/test_execution_spine.py``:

    1. Policy-evaluation errors deny (fail closed); they never authorize.
    2. An action whose intent record cannot be persisted is never executed.
    3. Denials and failures are recorded durably when the ledger allows; a
       ledger failure while finalizing surfaces as an ``audit_warning`` on the
       result instead of silently dropping the record, and the durable
       ``started`` row still proves the attempt.
    4. Executor exceptions re-raise only after the durable attempt record
       exists (a failed finalization is swallowed for the failure path only).
    5. No permission is inferred from caller or model text: approval is an
       explicit boolean resolved against the policy decision.
    6. Optional ``pre_execute_guard`` (e.g. asserting no active SQLite write
       transaction spans tool dispatch, M5-P1-01) fails closed before
       ``executor`` runs, finalizing the attempt as ``failed`` and re-raising.
    """

    def __init__(
        self,
        policy: Policy,
        ledger: ActionLedger,
        *,
        pre_execute_guard: Callable[[], None] | None = None,
    ) -> None:
        self._policy = policy
        self._ledger = ledger
        self._pre_execute_guard = pre_execute_guard

    async def admit_and_execute(self, request: ActionRequest, executor: Executor) -> dict[str, Any]:
        effective = self._policy.effective_policy(request.policy)
        try:
            decision = self._policy.authorize(
                request.tool_name, request.args, effective, approved=request.approved
            )
        except Exception:
            # Policy evaluation errors are not permission to execute.
            decision = AdmissionDecision(False, "Authorization policy evaluation failed; invocation denied.")
        if request.additional_denial:
            decision = AdmissionDecision(False, request.additional_denial)
        explicit_approval = bool(request.approved and decision.requires_approval)
        event_id = self._ledger.start(
            request.session_id, request.tool_name, effective, explicit_approval, request.args
        )
        started = time.perf_counter()
        if not decision.allowed:
            result: dict[str, Any] = {
                "ok": False,
                "output": f"Denied: {decision.reason}",
                "diff": "",
                "elapsed_ms": 1,
            }
            try:
                self._ledger.finish(event_id, result, 1, status="denied")
            except Exception:
                result["audit_warning"] = "Denial could not be finalized in the local audit log."
            if request.denial_as_exception:
                raise PermissionError(result["output"])
            return result
        try:
            if self._pre_execute_guard is not None:
                self._pre_execute_guard()
            result = await executor()
        except Exception as exc:
            failure: dict[str, Any] = {
                "ok": False,
                "output": f"Tool `{request.tool_name}` failed: {exc}",
                "diff": "",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
            try:
                self._ledger.finish(event_id, failure, failure["elapsed_ms"], status="failed")
            except Exception:
                pass  # The durable started row still proves that execution was attempted.
            raise
        elapsed = int(result.get("elapsed_ms") or round((time.perf_counter() - started) * 1000))
        try:
            self._ledger.finish(event_id, result, elapsed)
        except Exception:
            # The durable `started` event still records the action attempt; don't erase its outcome in memory.
            result = {**result, "audit_warning": "Tool result could not be finalized in the local audit log."}
        return result
