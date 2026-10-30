"""Authoritative execution-spine admission gate, action classes, and anti-bypass scope (M5-P0-06 / M5-P1-02).

This stdlib-only module defines the authoritative admission boundary between the
runtime and every consequential tool, skill script, smart block, and MCP
operation:

- ``ActionClass`` / ``classify_action`` -- deterministic separation between
  managed reads (``read_only``) and consequential effects (``managed_edit``,
  ``consequential``, ``external``);
- ``TaskAdmissionSnapshot`` / ``TaskContextResolver`` -- authoritative task
  lifecycle state and lease-epoch fencing checks;
- ``Policy`` -- deny-by-default authorization decision for one invocation;
- ``ActionLedger`` -- durable intent (start) before execution and durable
  outcome (finish) after;
- ``ActiveAdmissionScope`` / ``require_active_admission`` -- unforgeable
  ``ContextVar`` capability token minted exclusively inside
  ``DurableActionBoundary.admit_and_execute`` so direct inner-executor bypass
  fails closed at runtime;
- ``DurableActionBoundary`` -- classify -> check grant provenance -> check task
  state & epoch -> authorize policy -> durably record intent -> execute inside
  scoped admission token -> durably record outcome.

This module is inlined into the standalone artifact by
``scripts/build_studio.py`` (``INLINED_MODULES``); keep its imports strictly to
the Python standard library and free of references to ``app``.
"""
from __future__ import annotations

import contextvars
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ActionClass = Literal["read_only", "managed_edit", "consequential", "external"]

ACTION_CLASSES: frozenset[str] = frozenset({"read_only", "managed_edit", "consequential", "external"})
CONSEQUENTIAL_ACTION_CLASSES: frozenset[str] = frozenset({"managed_edit", "consequential", "external"})

MANAGED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_files",
        "grep_search",
        "read_file",
        "codegraph_architecture",
        "codegraph_context",
        "codegraph_impact",
        "codegraph_path",
        "codegraph_quality",
        "oc_lint",
        "ctx_search",
        "oc_search",
        "oc_read",
        "mcp_bridge__get_current_session_data",
        "mcp_bridge__get_harness_diff",
        "mcp_bridge__oc_search",
        "mcp_bridge__oc_lint",
        "mcp_bridge__codegraph_query",
    }
)

MANAGED_READ_BLOCKS: frozenset[str] = frozenset({"json-peek", "csv-peek", "shortcut-debt"})
CONSEQUENTIAL_BLOCKS: frozenset[str] = frozenset({"test-summary", "first-error"})
ALL_REUSABLE_BLOCKS: frozenset[str] = MANAGED_READ_BLOCKS | CONSEQUENTIAL_BLOCKS

MANAGED_EDIT_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "oc_save_doc"})

CONSEQUENTIAL_TOOLS: frozenset[str] = frozenset(
    {
        "run_bash",
        "ctx_execute_python",
        "delegate_subagent",
        "skill_execute_script",
        "mcp_bridge__start_council",
        "mcp_bridge__send_response",
        "mcp_bridge__summon_omlx_agent",
        "mcp_bridge__close_council",
        "mcp_bridge__create_harness_worktree",
        "mcp_bridge__oc_save_doc",
    }
)

GRANT_SOURCES: frozenset[str] = frozenset(
    {
        "unspecified",
        "none",
        "policy",
        "operator_approval",
        "operator_one_call",
        "skill_opt_in",
        "model_output",
    }
)

# Grant sources permitted to carry an explicit one-call or human approval (`approved=True`).
# `"unspecified"` preserves compatibility for narrow contract callers that supply
# `ActionRequest(..., approved=True)` directly; `"model_output"`, `"none"`, and `"policy"`
# can never confer an explicit approval grant.
EXPLICIT_APPROVAL_GRANT_SOURCES: frozenset[str] = frozenset(
    {"unspecified", "operator_approval", "operator_one_call", "skill_opt_in"}
)

# Keys inside model-emitted tool arguments that are untrusted data and are never
# consulted by admission, grant, policy, task-state, or epoch checks.
MODEL_UNTRUSTED_AUTHORITY_KEYS: frozenset[str] = frozenset(
    {
        "approved",
        "user_approved",
        "auto_approve",
        "grant",
        "grant_source",
        "approval_id",
        "policy",
        "harness_policy",
        "action_class",
        "task_id",
        "task_state",
        "epoch",
        "current_epoch",
        "worker_id",
        "bypass_admission",
    }
)

ADMISSIBLE_CONSEQUENTIAL_TASK_STATES: frozenset[str] = frozenset({"running"})
ADMISSIBLE_MANAGED_READ_TASK_STATES: frozenset[str] = frozenset(
    {"running", "waiting_approval", "awaiting_approval"}
)


class DirectExecutorBypassError(PermissionError):
    """Raised when an inner consequential executor is called outside DurableActionBoundary."""


def normalize_reusable_block_name(raw_block: Any) -> str:
    """Normalize a smart-block selector so case/whitespace never bypasses policy."""
    return str(raw_block or "").strip().lower()


def classify_action(tool_name: str, args: Mapping[str, Any] | None = None) -> ActionClass:
    """Deterministically classify an action into managed read vs consequential effect.

    Unknown or malformed tool names always classify as ``"consequential"`` (never
    ``"read_only"``) so unregistered operations fail closed under consequential
    scrutiny. Model-supplied metadata inside ``args`` (such as ``args["action_class"]``)
    is ignored; only structural dispatch selectors are inspected.
    """
    clean = str(tool_name or "").strip()
    if not clean:
        return "consequential"
    if clean == "ctx_run_block":
        block = normalize_reusable_block_name(args.get("block") if isinstance(args, Mapping) else "")
        if block in MANAGED_READ_BLOCKS:
            return "read_only"
        return "consequential"
    if clean in MANAGED_READ_TOOLS:
        return "read_only"
    if clean in MANAGED_EDIT_TOOLS:
        return "managed_edit"
    if clean.startswith("mcp__"):
        return "external"
    if clean in CONSEQUENTIAL_TOOLS:
        return "consequential"
    return "consequential"


def is_consequential_action_class(action_class: str) -> bool:
    """Return True for every non-read-only (or unrecognized) action class."""
    if action_class not in ACTION_CLASSES:
        return True
    return action_class != "read_only"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of a policy check for one tool invocation."""

    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    action_class: ActionClass = "consequential"

    @property
    def is_consequential(self) -> bool:
        return is_consequential_action_class(self.action_class)


@dataclass(frozen=True, slots=True)
class TaskAdmissionSnapshot:
    """Authoritative server-side snapshot of task lifecycle state and lease epoch."""

    task_id: str
    state: str
    current_epoch: int = 0
    epoch_status: str | None = None
    epoch_worker_id: str | None = None
    lease_expired: bool = False


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One action submitted to the authoritative admission boundary."""

    tool_name: str
    args: dict[str, Any]
    policy: str
    approved: bool
    session_id: str
    additional_denial: str | None = None
    denial_as_exception: bool = False
    action_class: ActionClass | None = None
    grant_source: str = "unspecified"
    approval_id: str | None = None
    task_id: str | None = None
    task_state: str | None = None
    epoch: int | None = None
    worker_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveAdmissionScope:
    """Unforgeable capability scope active only while an admitted executor runs."""

    tool_name: str
    action_class: ActionClass
    policy: str
    session_id: str
    event_id: int
    grant_source: str
    task_id: str | None = None
    epoch: int | None = None


_ACTIVE_ADMISSION_SCOPE: contextvars.ContextVar[ActiveAdmissionScope | None] = contextvars.ContextVar(
    "m5_active_admission_scope",
    default=None,
)


def current_admission_scope() -> ActiveAdmissionScope | None:
    """Return the active admission scope for the current async/call context, if any."""
    return _ACTIVE_ADMISSION_SCOPE.get()


def require_active_admission(
    *,
    expected_tool: str | None = None,
    expected_action_classes: frozenset[str] | set[str] | None = None,
) -> ActiveAdmissionScope:
    """Assert that execution is inside an admitted ``DurableActionBoundary`` scope.

    Raises ``DirectExecutorBypassError`` (a ``PermissionError`` subclass) when
    an inner consequential executor is called directly without crossing the
    admission gate, or when an admitted lower-privilege scope attempts to
    invoke an executor of another action class or tool identity.
    """
    scope = _ACTIVE_ADMISSION_SCOPE.get()
    if scope is None:
        target = f" for `{expected_tool}`" if expected_tool else ""
        raise DirectExecutorBypassError(
            f"Direct executor bypass blocked{target}: consequential execution must cross DurableActionBoundary."
        )
    if expected_tool is not None and scope.tool_name != expected_tool:
        raise DirectExecutorBypassError(
            f"Direct executor bypass blocked: active admission for `{scope.tool_name}` cannot execute `{expected_tool}`."
        )
    if expected_action_classes is not None and scope.action_class not in expected_action_classes:
        allowed_str = ", ".join(sorted(expected_action_classes))
        raise DirectExecutorBypassError(
            f"Direct executor bypass blocked: active admission class `{scope.action_class}` "
            f"for `{scope.tool_name}` is not permitted for `{allowed_str}` executor."
        )
    return scope


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


class TaskContextResolver(Protocol):
    """Authoritative resolver for task lifecycle state and lease-epoch metadata."""

    def resolve_task_context(self, request: ActionRequest) -> TaskAdmissionSnapshot | None:
        """Return the authoritative snapshot for ``request``, or ``None`` if unbound/missing."""
        ...


Executor = Callable[[], Awaitable[dict[str, Any]]]


def _validate_grant_and_action_class(
    request: ActionRequest,
) -> tuple[ActionClass, bool, str | None]:
    """Validate action class and grant provenance; return ``(action_class, effective_approved, denial_reason)``."""
    inferred_class = classify_action(request.tool_name, request.args)
    resolved_class: ActionClass = inferred_class
    if request.action_class is not None:
        if request.action_class not in ACTION_CLASSES:
            return (
                "consequential",
                False,
                f"Unrecognized action class `{request.action_class}`; invocation denied.",
            )
        if is_consequential_action_class(inferred_class) and request.action_class == "read_only":
            return (
                inferred_class,
                False,
                f"Action class downgrade from `{inferred_class}` to `read_only` for `{request.tool_name}` is forbidden.",
            )
        resolved_class = request.action_class

    grant_source = str(request.grant_source or "").strip() or "unspecified"
    if grant_source not in GRANT_SOURCES:
        return (
            resolved_class,
            False,
            f"Unrecognized grant provenance `{grant_source}`; invocation denied.",
        )
    if grant_source == "model_output":
        if request.approved:
            return (
                resolved_class,
                False,
                "Permission or approval cannot be inferred from model output.",
            )
        effective_approved = False
    elif request.approved and grant_source not in EXPLICIT_APPROVAL_GRANT_SOURCES:
        return (
            resolved_class,
            False,
            f"Explicit approval cannot originate from grant source `{grant_source}`.",
        )
    else:
        effective_approved = bool(request.approved)

    if request.tool_name == "skill_execute_script" and grant_source != "skill_opt_in":
        return (
            resolved_class,
            False,
            "Skill script execution requires server `skill_opt_in` provenance and cannot be invoked by model tool calls.",
        )

    return resolved_class, effective_approved, None


def _validate_task_state_and_epoch(
    request: ActionRequest,
    action_class: ActionClass,
    snapshot: TaskAdmissionSnapshot | None,
) -> str | None:
    """Validate task lifecycle state and durable epoch fencing; return denial reason or ``None``."""
    if request.task_id is not None and snapshot is None:
        return f"Authoritative task `{request.task_id}` does not exist."

    if snapshot is None:
        if request.epoch is not None:
            return "Task epoch supplied without an authoritative task context."
        if request.task_state is not None:
            snapshot = TaskAdmissionSnapshot(
                task_id=request.session_id or "transient",
                state=request.task_state,
                current_epoch=0,
            )
        else:
            return None

    normalized_state = str(snapshot.state or "").strip().lower()
    if not normalized_state:
        return f"Task `{snapshot.task_id}` has an empty or invalid lifecycle state."

    if is_consequential_action_class(action_class):
        if normalized_state not in ADMISSIBLE_CONSEQUENTIAL_TASK_STATES:
            return (
                f"Consequential `{action_class}` action `{request.tool_name}` requires task state "
                f"`running` (current state: `{normalized_state}`)."
            )
    else:
        if normalized_state not in ADMISSIBLE_MANAGED_READ_TASK_STATES:
            return (
                f"Managed read `{request.tool_name}` is not permitted when task `{snapshot.task_id}` "
                f"is in state `{normalized_state}`."
            )

    if request.epoch is not None and request.epoch <= 0:
        return f"Invalid non-positive task epoch `{request.epoch}`."
    if snapshot.current_epoch < 0:
        return f"Task `{snapshot.task_id}` has invalid negative epoch `{snapshot.current_epoch}`."

    if snapshot.current_epoch > 0:
        if request.epoch is None:
            return (
                f"Missing required task epoch for task `{snapshot.task_id}` "
                f"(current epoch: {snapshot.current_epoch})."
            )
        if request.epoch != snapshot.current_epoch:
            return (
                f"Stale or mismatched task epoch {request.epoch} for task `{snapshot.task_id}` "
                f"(current epoch: {snapshot.current_epoch})."
            )
        if snapshot.epoch_status != "active":
            return (
                f"Task `{snapshot.task_id}` epoch {request.epoch} is not active "
                f"(status: `{snapshot.epoch_status or 'missing'}`)."
            )
        if snapshot.lease_expired:
            return f"Task `{snapshot.task_id}` epoch {request.epoch} lease has expired."
        if (
            request.worker_id is not None
            and snapshot.epoch_worker_id is not None
            and request.worker_id != snapshot.epoch_worker_id
        ):
            return (
                f"Worker `{request.worker_id}` does not own task `{snapshot.task_id}` epoch "
                f"{request.epoch} (owned by `{snapshot.epoch_worker_id}`)."
            )
    elif request.epoch is not None:
        return (
            f"Unexpected task epoch {request.epoch} supplied for task `{snapshot.task_id}` "
            "with no active epoch."
        )

    return None


class DurableActionBoundary:
    """Authorize -> durably record intent -> execute -> durably record outcome.

    Guarantees pinned by ``tests/test_execution_spine.py`` and
    ``tests/test_action_admission_gate.py``:

    1. Action classification deterministically separates managed reads
       (``read_only``) from consequential effects (``managed_edit``,
       ``consequential``, ``external``) and forbids class downgrades.
    2. Grant provenance, task lifecycle state, durable epoch fencing, and policy
       errors all fail closed; no permission is ever inferred from model text
       or untrusted tool arguments.
    3. An action whose intent record cannot be persisted is never executed.
    4. Denials and failures are recorded durably when the ledger allows; a
       ledger failure while finalizing surfaces as an ``audit_warning`` on the
       result instead of silently dropping the record, and the durable
       ``started`` row still proves the attempt.
    5. Executor exceptions re-raise only after the durable attempt record
       exists (a failed finalization is swallowed for the failure path only).
    6. Execution runs inside a ``ContextVar`` ``ActiveAdmissionScope`` so inner
       executors enforce ``require_active_admission`` and reject direct bypass.
    7. Optional ``pre_execute_guard`` (e.g. asserting no active SQLite write
       transaction spans tool dispatch, M5-P1-01) fails closed before
       ``executor`` runs, finalizing the attempt as ``failed`` and re-raising.
    """

    def __init__(
        self,
        policy: Policy,
        ledger: ActionLedger,
        *,
        pre_execute_guard: Callable[[], None] | None = None,
        task_context_resolver: TaskContextResolver | None = None,
    ) -> None:
        self._policy = policy
        self._ledger = ledger
        self._pre_execute_guard = pre_execute_guard
        self._task_context_resolver = task_context_resolver

    async def admit_and_execute(self, request: ActionRequest, executor: Executor) -> dict[str, Any]:
        effective = self._policy.effective_policy(request.policy)
        action_class, effective_approved, pre_denial = _validate_grant_and_action_class(request)

        task_denial: str | None = None
        if pre_denial is None:
            snapshot: TaskAdmissionSnapshot | None = None
            if self._task_context_resolver is not None:
                try:
                    snapshot = self._task_context_resolver.resolve_task_context(request)
                except Exception:
                    task_denial = "Task lifecycle context resolution failed; invocation denied."
            elif request.task_id is not None and request.task_state is not None:
                snapshot = TaskAdmissionSnapshot(
                    task_id=request.task_id,
                    state=request.task_state,
                    current_epoch=request.epoch or 0,
                    epoch_status="active" if request.epoch else None,
                    epoch_worker_id=request.worker_id,
                    lease_expired=False,
                )
            if task_denial is None:
                task_denial = _validate_task_state_and_epoch(request, action_class, snapshot)

        if pre_denial is not None:
            decision = AdmissionDecision(False, pre_denial, False, action_class)
        elif task_denial is not None:
            decision = AdmissionDecision(False, task_denial, False, action_class)
        else:
            try:
                raw_decision = self._policy.authorize(
                    request.tool_name, request.args, effective, approved=effective_approved
                )
                decision = AdmissionDecision(
                    bool(raw_decision.allowed),
                    str(raw_decision.reason or ""),
                    bool(raw_decision.requires_approval),
                    action_class,
                )
            except Exception:
                # Policy evaluation errors are not permission to execute.
                decision = AdmissionDecision(
                    False,
                    "Authorization policy evaluation failed; invocation denied.",
                    False,
                    action_class,
                )

        if request.additional_denial:
            decision = AdmissionDecision(
                False,
                request.additional_denial,
                False,
                action_class,
            )

        explicit_approval = bool(effective_approved and decision.requires_approval)
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
                "action_class": action_class,
            }
            try:
                self._ledger.finish(event_id, result, 1, status="denied")
            except Exception:
                result["audit_warning"] = "Denial could not be finalized in the local audit log."
            if request.denial_as_exception:
                raise PermissionError(result["output"])
            return result

        scope = ActiveAdmissionScope(
            tool_name=request.tool_name,
            action_class=action_class,
            policy=effective,
            session_id=request.session_id,
            event_id=event_id,
            grant_source=str(request.grant_source or "unspecified"),
            task_id=request.task_id,
            epoch=request.epoch,
        )
        try:
            if self._pre_execute_guard is not None:
                self._pre_execute_guard()
            scope_token = _ACTIVE_ADMISSION_SCOPE.set(scope)
            try:
                result = await executor()
            finally:
                _ACTIVE_ADMISSION_SCOPE.reset(scope_token)
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
