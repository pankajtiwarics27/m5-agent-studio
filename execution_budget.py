"""Time, token, cost, tool, cancellation, and durable recovery execution budgets."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import runtime_persistence


class BudgetError(ValueError):
    """Raised when a budget or usage value is invalid."""


class BudgetExceeded(RuntimeError):
    """Raised when an operation would exceed an execution or recovery budget."""


class ExecutionCancelled(RuntimeError):
    """Raised when cancellation has been requested."""


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_seconds: float
    max_tokens: int
    max_cost: float
    max_tools: int
    max_retries: int = 0

    def __post_init__(self) -> None:
        if self.max_seconds <= 0 or self.max_tokens < 0 or self.max_cost < 0 or self.max_tools < 0 or self.max_retries < 0:
            raise BudgetError("budget limits must be non-negative, with max_seconds positive.")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    elapsed_seconds: float
    tokens: int
    cost: float
    tools: int
    retries: int


class ExecutionBudget:
    """Thread-safe budget guard with monotonic time and explicit cancellation."""

    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._tokens = 0
        self._cost = 0.0
        self._tools = 0
        self._retries = 0
        self._cancelled = False
        self._lock = threading.RLock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def _usage(self) -> BudgetUsage:
        return BudgetUsage(self._clock() - self._started, self._tokens, self._cost, self._tools, self._retries)

    def usage(self) -> BudgetUsage:
        with self._lock:
            return self._usage()

    def check(self) -> BudgetUsage:
        with self._lock:
            usage = self._usage()
            if self._cancelled:
                raise ExecutionCancelled("execution was cancelled.")
            if usage.elapsed_seconds > self.limits.max_seconds:
                raise BudgetExceeded("time budget exceeded.")
            if usage.tokens > self.limits.max_tokens:
                raise BudgetExceeded("token budget exceeded.")
            if usage.cost > self.limits.max_cost:
                raise BudgetExceeded("cost budget exceeded.")
            if usage.tools > self.limits.max_tools:
                raise BudgetExceeded("tool budget exceeded.")
            if usage.retries > self.limits.max_retries:
                raise BudgetExceeded("retry budget exceeded.")
            return usage

    def consume(self, *, tokens: int = 0, cost: float = 0.0, tools: int = 0) -> BudgetUsage:
        if isinstance(tokens, bool) or tokens < 0 or isinstance(tools, bool) or tools < 0 or cost < 0:
            raise BudgetError("usage increments must be non-negative.")
        with self._lock:
            self._tokens += tokens
            self._cost += cost
            self._tools += tools
            return self.check()

    def retry(self) -> BudgetUsage:
        with self._lock:
            self._retries += 1
            return self.check()

    def allow_retry(self) -> bool:
        try:
            self.retry()
        except BudgetExceeded:
            return False
        return True


def _non_neg_int(value: Any, name: str, *, min_value: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < min_value:
        raise BudgetError(f"{name} must be an integer >= {min_value}.")
    return int(value)


def _non_neg_float(value: Any, name: str, *, strictly_positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError(f"{name} must be numeric.")
    val = float(value)
    if strictly_positive and val <= 0:
        raise BudgetError(f"{name} must be positive.")
    if not strictly_positive and val < 0:
        raise BudgetError(f"{name} must be non-negative.")
    return val


@dataclass(frozen=True, slots=True)
class TaskBudgetSpec:
    """Durable root-task and crash-recovery budget bounds (M5-P1-05)."""

    max_steps: int = 25
    max_tool_calls: int = 120
    max_tokens: int = 500_000
    max_elapsed_s: float = 1_800.0
    max_recoveries: int = 3

    def __post_init__(self) -> None:
        _non_neg_int(self.max_steps, "max_steps", min_value=1)
        _non_neg_int(self.max_tool_calls, "max_tool_calls", min_value=0)
        _non_neg_int(self.max_tokens, "max_tokens", min_value=0)
        _non_neg_float(self.max_elapsed_s, "max_elapsed_s", strictly_positive=True)
        _non_neg_int(self.max_recoveries, "max_recoveries", min_value=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "max_elapsed_s": round(self.max_elapsed_s, 4),
            "max_recoveries": self.max_recoveries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskBudgetSpec:
        raw = data or {}
        return cls(
            max_steps=int(raw.get("max_steps", 25)),
            max_tool_calls=int(raw.get("max_tool_calls", 120)),
            max_tokens=int(raw.get("max_tokens", 500_000)),
            max_elapsed_s=float(raw.get("max_elapsed_s", 1_800.0)),
            max_recoveries=int(raw.get("max_recoveries", 3)),
        )


@dataclass(frozen=True, slots=True)
class TaskBudgetState:
    """Persisted task + recovery budget counters and safe-boundary flags."""

    spec: TaskBudgetSpec
    steps_used: int = 0
    tool_calls_used: int = 0
    tokens_used: int = 0
    elapsed_s: float = 0.0
    recoveries_used: int = 0
    pause_requested: bool = False
    cancel_requested: bool = False
    exhausted_reason: str = ""
    verification_contract: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "steps_used": self.steps_used,
            "tool_calls_used": self.tool_calls_used,
            "tokens_used": self.tokens_used,
            "elapsed_s": round(self.elapsed_s, 4),
            "recoveries_used": self.recoveries_used,
            "pause_requested": self.pause_requested,
            "cancel_requested": self.cancel_requested,
            "exhausted_reason": self.exhausted_reason,
        }

    def to_json(self) -> str:
        payload = self.to_dict()
        if self.verification_contract is not None:
            payload["verification_contract"] = self.verification_contract
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(
        cls,
        raw: str | None,
        *,
        default_spec: TaskBudgetSpec | None = None,
    ) -> TaskBudgetState:
        fallback = default_spec or TaskBudgetSpec()
        text = (raw or "").strip()
        if not text or text == "{}":
            return cls(spec=fallback)
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise BudgetError("persisted task budget JSON is malformed.") from exc
        if not isinstance(parsed, dict):
            raise BudgetError("persisted task budget JSON must be an object.")
        spec_raw = parsed.get("spec")
        if isinstance(spec_raw, dict):
            spec = TaskBudgetSpec.from_dict(spec_raw)
        else:
            spec = TaskBudgetSpec(
                max_steps=int(parsed.get("max_steps", fallback.max_steps)),
                max_tool_calls=int(parsed.get("max_tool_calls", fallback.max_tool_calls)),
                max_tokens=int(parsed.get("max_tokens", fallback.max_tokens)),
                max_elapsed_s=float(parsed.get("max_elapsed_s", fallback.max_elapsed_s)),
                max_recoveries=int(parsed.get("max_recoveries", fallback.max_recoveries)),
            )
        vc_raw = parsed.get("verification_contract")
        vc_dict = dict(vc_raw) if isinstance(vc_raw, dict) else None
        return cls(
            spec=spec,
            steps_used=_non_neg_int(int(parsed.get("steps_used", 0)), "steps_used"),
            tool_calls_used=_non_neg_int(int(parsed.get("tool_calls_used", 0)), "tool_calls_used"),
            tokens_used=_non_neg_int(int(parsed.get("tokens_used", 0)), "tokens_used"),
            elapsed_s=_non_neg_float(float(parsed.get("elapsed_s", 0.0)), "elapsed_s"),
            recoveries_used=_non_neg_int(int(parsed.get("recoveries_used", 0)), "recoveries_used"),
            pause_requested=bool(parsed.get("pause_requested", False)),
            cancel_requested=bool(parsed.get("cancel_requested", False)),
            exhausted_reason=str(parsed.get("exhausted_reason", "")),
            verification_contract=vc_dict,
        )

    def violation_reason(self, *, check_recovery: bool = False) -> str:
        if self.steps_used > self.spec.max_steps:
            return f"step budget exceeded ({self.steps_used}/{self.spec.max_steps})"
        if self.tool_calls_used > self.spec.max_tool_calls:
            return f"tool budget exceeded ({self.tool_calls_used}/{self.spec.max_tool_calls})"
        if self.tokens_used > self.spec.max_tokens:
            return f"token budget exceeded ({self.tokens_used}/{self.spec.max_tokens})"
        if self.elapsed_s > self.spec.max_elapsed_s:
            return f"time budget exceeded ({self.elapsed_s:.2f}s/{self.spec.max_elapsed_s:.2f}s)"
        if check_recovery and self.recoveries_used > self.spec.max_recoveries:
            return f"recovery budget exceeded ({self.recoveries_used}/{self.spec.max_recoveries})"
        return ""


class DurableTaskBudgetManager:
    """SQLite-backed task and recovery budget enforcement over ``runtime_tasks.budget_json``."""

    def __init__(
        self,
        storage: runtime_persistence.StorageProvider,
        db_path: Path,
        *,
        retry_policy: runtime_persistence.BusyRetryPolicy = runtime_persistence.DEFAULT_BUSY_RETRY_POLICY,
    ) -> None:
        self._storage = storage
        self._db_path = db_path
        self._retry_policy = retry_policy

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        if not isinstance(task_id, str) or not task_id.strip() or "\x00" in task_id:
            raise BudgetError("task_id must be a non-empty string without NUL characters.")
        return task_id.strip()

    def get_budget(
        self,
        task_id: str,
        *,
        default_spec: TaskBudgetSpec | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)

        def _read(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            return TaskBudgetState.from_json(str(row["budget_json"]), default_spec=default_spec)

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def ensure_budget(
        self,
        task_id: str,
        *,
        spec: TaskBudgetSpec | None = None,
        override_spec: bool = False,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            raw = str(row["budget_json"] or "{}").strip()
            current = TaskBudgetState.from_json(raw, default_spec=spec)
            effective_spec = spec if (override_spec and spec is not None) else current.spec
            updated = TaskBudgetState(
                spec=effective_spec,
                steps_used=current.steps_used,
                tool_calls_used=current.tool_calls_used,
                tokens_used=current.tokens_used,
                elapsed_s=current.elapsed_s,
                recoveries_used=current.recoveries_used,
                pause_requested=current.pause_requested,
                cancel_requested=current.cancel_requested,
                exhausted_reason=current.exhausted_reason,
                verification_contract=current.verification_contract,
            )
            encoded = updated.to_json()
            if encoded != raw:
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (encoded, ts, tid),
                )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def set_control_flags(
        self,
        task_id: str,
        *,
        pause_requested: bool | None = None,
        cancel_requested: bool | None = None,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            updated = TaskBudgetState(
                spec=current.spec,
                steps_used=current.steps_used,
                tool_calls_used=current.tool_calls_used,
                tokens_used=current.tokens_used,
                elapsed_s=current.elapsed_s,
                recoveries_used=current.recoveries_used,
                pause_requested=current.pause_requested if pause_requested is None else bool(pause_requested),
                cancel_requested=current.cancel_requested if cancel_requested is None else bool(cancel_requested),
                exhausted_reason=current.exhausted_reason,
                verification_contract=current.verification_contract,
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (updated.to_json(), ts, tid),
            )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def record_usage(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        steps_delta: int = 0,
        tool_calls_delta: int = 0,
        tokens_delta: int = 0,
        elapsed_delta_s: float = 0.0,
        enforce: bool = True,
        now: float | None = None,
    ) -> TaskBudgetState:
        tid = self._validate_task_id(task_id)
        _non_neg_int(steps_delta, "steps_delta")
        _non_neg_int(tool_calls_delta, "tool_calls_delta")
        _non_neg_int(tokens_delta, "tokens_delta")
        _non_neg_float(elapsed_delta_s, "elapsed_delta_s")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> tuple[TaskBudgetState, str, bool]:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT current_epoch, budget_json FROM runtime_tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            if epoch is not None and int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot mutate budget for task {tid!r} at current_epoch={row['current_epoch']}."
                )
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            candidate = TaskBudgetState(
                spec=current.spec,
                steps_used=current.steps_used + steps_delta,
                tool_calls_used=current.tool_calls_used + tool_calls_delta,
                tokens_used=current.tokens_used + tokens_delta,
                elapsed_s=round(current.elapsed_s + elapsed_delta_s, 6),
                recoveries_used=current.recoveries_used,
                pause_requested=current.pause_requested,
                cancel_requested=current.cancel_requested,
                exhausted_reason=current.exhausted_reason,
                verification_contract=current.verification_contract,
            )
            violation = candidate.violation_reason(check_recovery=False)
            final_state = (
                TaskBudgetState(
                    spec=candidate.spec,
                    steps_used=candidate.steps_used,
                    tool_calls_used=candidate.tool_calls_used,
                    tokens_used=candidate.tokens_used,
                    elapsed_s=candidate.elapsed_s,
                    recoveries_used=candidate.recoveries_used,
                    pause_requested=candidate.pause_requested,
                    cancel_requested=candidate.cancel_requested,
                    exhausted_reason=violation or candidate.exhausted_reason,
                    verification_contract=candidate.verification_contract,
                )
                if violation
                else candidate
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (final_state.to_json(), ts, tid),
            )
            return final_state, violation, final_state.cancel_requested

        state, violation, cancelled = runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
        if enforce and cancelled:
            raise ExecutionCancelled("task cancellation requested.")
        if enforce and violation:
            raise BudgetExceeded(violation)
        return state

    def consume_recovery(
        self,
        task_id: str,
        *,
        now: float | None = None,
    ) -> TaskBudgetState:
        """Atomically consume 1 durable recovery attempt; commit exhaustion before raising."""
        tid = self._validate_task_id(task_id)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> tuple[TaskBudgetState, str]:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            next_recoveries = current.recoveries_used + 1
            violation = ""
            if next_recoveries > current.spec.max_recoveries:
                violation = f"recovery budget exceeded ({next_recoveries}/{current.spec.max_recoveries})"
            persisted = TaskBudgetState(
                spec=current.spec,
                steps_used=current.steps_used,
                tool_calls_used=current.tool_calls_used,
                tokens_used=current.tokens_used,
                elapsed_s=current.elapsed_s,
                recoveries_used=current.recoveries_used if violation else next_recoveries,
                pause_requested=False if not violation else current.pause_requested,
                cancel_requested=current.cancel_requested,
                exhausted_reason=violation or current.exhausted_reason,
                verification_contract=current.verification_contract,
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (persisted.to_json(), ts, tid),
            )
            return persisted, violation

        state, violation = runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
        if violation:
            raise BudgetExceeded(violation)
        return state

    def set_verification_contract(
        self,
        task_id: str,
        contract: dict[str, Any] | None,
        *,
        now: float | None = None,
    ) -> TaskBudgetState:
        """Persist or clear the authoritative task verification contract inside ``budget_json``."""
        tid = self._validate_task_id(task_id)
        if contract is not None and not isinstance(contract, dict):
            raise BudgetError("verification_contract must be a dict or None.")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskBudgetState:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT budget_json FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime task {tid!r} does not exist.")
            current = TaskBudgetState.from_json(str(row["budget_json"]))
            updated = TaskBudgetState(
                spec=current.spec,
                steps_used=current.steps_used,
                tool_calls_used=current.tool_calls_used,
                tokens_used=current.tokens_used,
                elapsed_s=current.elapsed_s,
                recoveries_used=current.recoveries_used,
                pause_requested=current.pause_requested,
                cancel_requested=current.cancel_requested,
                exhausted_reason=current.exhausted_reason,
                verification_contract=dict(contract) if contract is not None else None,
            )
            conn.execute(
                "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                (updated.to_json(), ts, tid),
            )
            return updated

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )
