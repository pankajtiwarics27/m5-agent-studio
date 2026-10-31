"""Durable intent/result ledger and replay classification for workflow side effects (M5-P1-03).

Provides both:
1. The standalone ``SideEffectLedger`` contract (retained for narrow contract tests).
2. The authoritative M5 2.0 ``DurableActionLedger`` integrated into the served
   runtime over Schema v3 ``runtime_actions`` (``runtime_persistence``):
   - Stable ``action_id`` and monotonic ``attempt`` identities
     (``derive_action_id``, ``format_attempt_id``);
   - Explicit separation of ``never_started``, ``in_flight``, ``committed``, and
     ``uncertain`` (plus ``denied`` and confirmed ``failed``);
   - Missing results on crash/interruption are reconciled to ``uncertain``
     (never ``failed``);
   - Action-type replay classification (``idempotent``, ``reconcilable``,
     ``non_idempotent``) ensuring non-idempotent uncertain writes are never
     retried blindly.

Stdlib-only (plus ``runtime_persistence``); inlined into standalone ``studio.py``
by ``scripts/build_studio.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import runtime_persistence

EffectStatus = Literal["claimed", "completed", "failed"]
_STATUSES = frozenset({"claimed", "completed", "failed"})

ReplayPolicy = Literal["idempotent", "reconcilable", "non_idempotent"]
LedgerActionStatus = Literal["never_started", "in_flight", "committed", "failed", "denied", "uncertain"]
ReconciliationVerdict = Literal["verified_committed", "verified_not_applied", "unresolved"]

_LEDGER_READ_ONLY_TOOLS: frozenset[str] = frozenset(
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
_LEDGER_READ_ONLY_BLOCKS: frozenset[str] = frozenset({"json-peek", "csv-peek", "shortcut-debt"})

_AMBIGUOUS_EXC_NAMES: frozenset[str] = frozenset(
    {
        "McpAmbiguousOutcomeError",
        "AmbiguousSideEffectError",
        "CancelledError",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
    }
)


class SideEffectError(ValueError):
    """Raised when a side-effect ledger request is malformed."""


class DuplicateSideEffect(RuntimeError):
    """Raised when an operation key already represents a non-retryable effect."""


class AmbiguousSideEffectError(RuntimeError):
    """Raised when a side effect was dispatched and its outcome could not be confirmed."""


class ActionIdentityConflictError(DuplicateSideEffect, PermissionError):
    """Raised when an action_id is reused for a different tool or argument digest."""


class UncertainActionReplayError(DuplicateSideEffect, PermissionError):
    """Raised when a non-idempotent or unverified uncertain action is replayed blindly."""


class CommittedActionReplayError(DuplicateSideEffect, PermissionError):
    """Raised when an already-committed consequential action is re-submitted."""


class UnresolvedEffectsHoldError(DuplicateSideEffect, PermissionError):
    """Raised when lease takeover or consequential admission is held by unresolved effects (M5-P1-04)."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        epoch: int = 0,
        unresolved_action_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.epoch = epoch
        self.unresolved_action_ids = unresolved_action_ids


@dataclass(frozen=True, slots=True)
class EffectRecord:
    operation_key: str
    effect_name: str
    request_hash: str
    status: EffectStatus
    result_hash: str | None
    attempts: int
    updated_at: str


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SideEffectError(f"{field} must be a non-empty string without NUL characters.")
    return value.strip()


def _status(value: str) -> EffectStatus:
    if value not in _STATUSES:
        raise SideEffectError(f"unknown side-effect status: {value}")
    return value  # type: ignore[return-value]


class SideEffectLedger:
    """Claim operation keys atomically before execution and finalize exactly once."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS side_effects (
                    operation_key TEXT PRIMARY KEY,
                    effect_name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_hash TEXT,
                    attempts INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (status IN ('claimed', 'completed', 'failed')),
                    CHECK (attempts > 0)
                )
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> EffectRecord | None:
        if row is None:
            return None
        _status(row["status"])
        return EffectRecord(
            row["operation_key"],
            row["effect_name"],
            row["request_hash"],
            row["status"],
            row["result_hash"],
            row["attempts"],
            row["updated_at"],
        )

    def get(self, operation_key: str) -> EffectRecord | None:
        key = _text(operation_key, "operation_key")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return self._record(
                connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
            )

    def claim(self, operation_key: str, effect_name: str, request_hash: str) -> EffectRecord:
        key = _text(operation_key, "operation_key")
        name = _text(effect_name, "effect_name")
        digest = _text(request_hash, "request_hash")
        with sqlite3.connect(self.database, timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
            existing = self._record(row)
            if existing is not None:
                if existing.request_hash != digest or existing.effect_name != name:
                    raise DuplicateSideEffect("operation key conflicts with a different request.")
                if existing.status != "failed":
                    raise DuplicateSideEffect("operation key already has a claimed or completed side effect.")
                connection.execute(
                    "UPDATE side_effects SET status = 'claimed', result_hash = NULL, attempts = attempts + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE operation_key = ?",
                    (key,),
                )
            else:
                connection.execute(
                    "INSERT INTO side_effects(operation_key, effect_name, request_hash, status, attempts) "
                    "VALUES (?, ?, ?, 'claimed', 1)",
                    (key, name, digest),
                )
            updated = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result

    def complete(self, operation_key: str, result_hash: str) -> EffectRecord:
        return self._finish(operation_key, "completed", result_hash)

    def fail(self, operation_key: str, result_hash: str = "failure") -> EffectRecord:
        return self._finish(operation_key, "failed", result_hash)

    def _finish(self, operation_key: str, status: EffectStatus, result_hash: str) -> EffectRecord:
        key = _text(operation_key, "operation_key")
        digest = _text(result_hash, "result_hash")
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
            existing = self._record(row)
            if existing is None or existing.status != "claimed":
                raise DuplicateSideEffect("only a claimed side effect can be finalized.")
            connection.execute(
                "UPDATE side_effects SET status = ?, result_hash = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE operation_key = ?",
                (status, digest, key),
            )
            updated = connection.execute("SELECT * FROM side_effects WHERE operation_key = ?", (key,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result


# ---------------------------------------------------------------------------
# Authoritative M5 2.0 Durable Intent / Result Ledger (`runtime_actions`)
# ---------------------------------------------------------------------------


def canonical_args_sha256(args: Mapping[str, Any] | None) -> str:
    """Return a deterministic SHA-256 hex digest for structured tool arguments."""
    payload = dict(args) if isinstance(args, Mapping) else {}
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()


def canonical_result_sha256(result: Mapping[str, Any] | None) -> str:
    """Return a deterministic SHA-256 hex digest for tool output + diff material."""
    payload = result if isinstance(result, Mapping) else {}
    material = str(payload.get("output") or "") + "\n" + str(payload.get("diff") or "")
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def derive_action_id(
    scope_id: str,
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    step_id: str | int | None = None,
    call_index: int | None = None,
) -> str:
    """Derive a deterministic stable ``action_id`` for a logical execution slot.

    Re-entering the same ``(scope_id, step_id, call_index, tool_name, args_sha256)``
    slot across retries or crash recovery produces the exact same ``action_id``.
    """
    clean_scope = _text(scope_id or "default", "scope_id")
    clean_tool = _text(tool_name, "tool_name")
    args_digest = canonical_args_sha256(args)
    slot_material = json.dumps(
        {
            "scope": clean_scope,
            "step": "" if step_id is None else str(step_id),
            "index": 0 if call_index is None else int(call_index),
            "tool": clean_tool,
            "args_sha256": args_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(slot_material.encode("utf-8")).hexdigest()[:32]
    return f"act-{digest}"


def mint_unique_action_id(
    scope_id: str,
    tool_name: str,
    args: Mapping[str, Any] | None,
) -> str:
    """Mint a unique ``action_id`` when a caller invokes an ad-hoc unslotted action."""
    clean_scope = str(scope_id or "adhoc").strip() or "adhoc"
    clean_tool = _text(tool_name, "tool_name")
    nonce = uuid.uuid4().hex
    return derive_action_id(clean_scope, clean_tool, args, step_id=f"adhoc-{nonce}", call_index=0)


def format_attempt_id(action_id: str, attempt: int) -> str:
    """Format the canonical per-attempt identifier for a stable ``action_id``."""
    clean_aid = _text(action_id, "action_id")
    if int(attempt) < 1:
        raise SideEffectError("attempt must be >= 1.")
    return f"{clean_aid}#attempt-{int(attempt)}"


def classify_replay_policy(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    *,
    action_class: str | None = None,
) -> ReplayPolicy:
    """Classify replay safety by action type (`idempotent`, `reconcilable`, `non_idempotent`).

    - ``idempotent``: managed reads (`read_only` tools and read-only smart blocks)
      whose re-execution cannot duplicate or corrupt state.
    - ``reconcilable``: deterministic target overwrites (`write_file`, and
      `oc_save_doc` / `mcp_bridge__oc_save_doc` when an explicit ``doc_id`` is
      supplied) whose target state can be verified before deciding whether to
      mark committed or retry; still never retried blindly without verification.
    - ``non_idempotent``: shell/Python execution, skill scripts, relative
      substring edits (`edit_file`), `oc_save_doc` without `doc_id` (creates a
      new document UUID per call), subagent delegation, outbound MCP `tools/call`,
      consequential inbound bridge calls, and any unknown tool.
    """
    clean = str(tool_name or "").strip()
    safe_args = args if isinstance(args, Mapping) else {}
    if action_class == "read_only":
        return "idempotent"
    if clean in _LEDGER_READ_ONLY_TOOLS:
        return "idempotent"
    if clean == "ctx_run_block":
        block = str(safe_args.get("block") or "").strip().lower()
        if block in _LEDGER_READ_ONLY_BLOCKS:
            return "idempotent"
        return "non_idempotent"
    if clean == "write_file":
        return "reconcilable"
    if clean in ("oc_save_doc", "mcp_bridge__oc_save_doc"):
        doc_id = str(safe_args.get("doc_id") or "").strip()
        return "reconcilable" if doc_id else "non_idempotent"
    return "non_idempotent"


def is_ambiguous_exception(exc: BaseException) -> bool:
    """Return True when ``exc`` indicates an ambiguous post-dispatch outcome (`uncertain`)."""
    if isinstance(
        exc,
        (
            AmbiguousSideEffectError,
            asyncio.CancelledError,
            TimeoutError,
            ConnectionResetError,
            BrokenPipeError,
        ),
    ):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _AMBIGUOUS_EXC_NAMES:
            return True
    return False


class ActionReconciler(Protocol):
    """Callback probing target state for ``reconcilable`` uncertain actions before replay."""

    def __call__(
        self,
        record: runtime_persistence.ActionRecord,
        tool_name: str,
        args: Mapping[str, Any],
    ) -> ReconciliationVerdict:
        ...


@dataclass(frozen=True, slots=True)
class LedgerBeginOutcome:
    """Decision returned when registering or re-entering an action attempt."""

    proceed: bool
    action_id: str
    attempt: int
    attempt_id: str
    status: LedgerActionStatus
    replay_policy: ReplayPolicy
    record: runtime_persistence.ActionRecord | None = None
    replay_blocked: bool = False
    already_committed: bool = False
    reconciled: bool = False
    reason: str = ""
    error: Exception | None = None


def _sel_row_to_action(row: sqlite3.Row) -> runtime_persistence.ActionRecord:
    return runtime_persistence.ActionRecord(
        action_id=str(row["action_id"]),
        task_id=(str(row["task_id"]) if row["task_id"] is not None else None),
        session_id=str(row["session_id"]),
        epoch=int(row["epoch"]),
        attempt=int(row["attempt"]),
        action_class=str(row["action_class"]),
        replay_policy=str(row["replay_policy"]),
        tool_name=str(row["tool_name"]),
        policy=str(row["policy"]),
        explicit_approval=bool(row["explicit_approval"]),
        args_sha256=str(row["args_sha256"]),
        status=str(row["status"]),
        ok=(bool(row["ok"]) if row["ok"] is not None else None),
        elapsed_ms=(int(row["elapsed_ms"]) if row["elapsed_ms"] is not None else None),
        output_sha256=str(row["output_sha256"]),
        error_code=str(row["error_code"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class DurableActionLedger:
    """Authoritative SQLite ``runtime_actions`` intent, outcome, and replay ledger."""

    def __init__(
        self,
        storage: runtime_persistence.StorageProvider,
        db_path: Path,
        *,
        retry_policy: runtime_persistence.BusyRetryPolicy | None = None,
        reconciler: ActionReconciler | None = None,
        on_action_resolved: Callable[[str], None] | None = None,
    ) -> None:
        self._storage = storage
        self._db_path = Path(db_path)
        self._retry_policy: runtime_persistence.BusyRetryPolicy = (
            retry_policy or runtime_persistence.DEFAULT_BUSY_RETRY_POLICY
        )
        self._reconciler = reconciler
        self._on_action_resolved = on_action_resolved

    @property
    def reconciler(self) -> ActionReconciler | None:
        return self._reconciler

    def get_action(self, action_id: str) -> runtime_persistence.ActionRecord | None:
        aid = _text(action_id, "action_id")

        def _read(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            return _sel_row_to_action(row) if row is not None else None

        return runtime_persistence.run_read(
            self._storage, self._db_path, _read, retry_policy=self._retry_policy
        )

    def stage_never_started(
        self,
        action_id: str,
        *,
        tool_name: str,
        args: Mapping[str, Any] | None,
        policy: str,
        action_class: str = "consequential",
        replay_policy: ReplayPolicy | None = None,
        task_id: str | None = None,
        session_id: str = "",
        epoch: int = 0,
        explicit_approval: bool = False,
        now: float | None = None,
    ) -> runtime_persistence.ActionRecord:
        """Persist an explicit ``never_started`` intent record prior to dispatch."""
        aid = _text(action_id, "action_id")[:128]
        tname = _text(tool_name, "tool_name")[:200]
        pol = _text(policy, "policy")[:40]
        digest = canonical_args_sha256(args)
        eff_class = action_class if action_class in runtime_persistence.ACTION_CLASSES else "consequential"
        eff_replay = replay_policy or classify_replay_policy(tname, args, action_class=eff_class)
        if eff_replay not in runtime_persistence.REPLAY_POLICIES:
            raise SideEffectError(f"Unsupported replay_policy: {eff_replay!r}")
        tid = _text(task_id, "task_id")[:128] if task_id else None
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord:
            conn.row_factory = sqlite3.Row
            existing = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if existing is not None:
                rec = _sel_row_to_action(existing)
                if rec.tool_name != tname or rec.args_sha256 != digest:
                    raise ActionIdentityConflictError(
                        f"Action `{aid}` conflicts with existing `{rec.tool_name}` intent digest."
                    )
                return rec
            conn.execute(
                """
                INSERT INTO runtime_actions (
                    action_id, task_id, session_id, epoch, attempt, action_class,
                    replay_policy, tool_name, policy, explicit_approval, args_sha256,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'never_started', ?, ?)
                """,
                (
                    aid,
                    tid,
                    str(session_id)[:128],
                    max(0, int(epoch)),
                    eff_class,
                    eff_replay,
                    tname,
                    pol,
                    int(bool(explicit_approval)),
                    digest,
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            assert row is not None
            return _sel_row_to_action(row)

        return runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )

    def begin(
        self,
        *,
        action_id: str | None,
        step_id: str | int | None = None,
        call_index: int | None = None,
        tool_name: str,
        args: Mapping[str, Any] | None,
        policy: str,
        action_class: str,
        replay_policy: str | None = None,
        allowed: bool,
        explicit_approval: bool,
        session_id: str,
        task_id: str | None = None,
        epoch: int | None = None,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> LedgerBeginOutcome:
        """Atomically evaluate replay safety and commit durable ``in_flight`` (or ``denied``) intent."""
        tname = _text(tool_name, "tool_name")[:200]
        pol = _text(policy or "standard", "policy")[:40]
        safe_args = args if isinstance(args, Mapping) else {}
        digest = canonical_args_sha256(safe_args)
        eff_class = action_class if action_class in runtime_persistence.ACTION_CLASSES else "consequential"
        resolved_replay: ReplayPolicy = (
            replay_policy  # type: ignore[assignment]
            if replay_policy in runtime_persistence.REPLAY_POLICIES
            else classify_replay_policy(tname, safe_args, action_class=eff_class)
        )
        scope_key = str(task_id or session_id or "default")
        if action_id is not None and str(action_id).strip():
            aid = _text(str(action_id), "action_id")[:128]
        elif step_id is not None or call_index is not None:
            aid = derive_action_id(scope_key, tname, safe_args, step_id=step_id, call_index=call_index)
        else:
            aid = mint_unique_action_id(scope_key, tname, safe_args)

        clean_tid = _text(task_id, "task_id")[:128] if task_id else None
        clean_epoch = max(0, int(epoch or 0))
        clean_wid = str(worker_id).strip()[:128] if worker_id and str(worker_id).strip() else None
        ts = time.time() if now is None else float(now)

        # Optional pre-transaction reconciliation probe for `reconcilable` actions in `uncertain` state
        # (performed strictly outside `BEGIN IMMEDIATE` so filesystem/read checks hold no write lock).
        reconcile_verdict: ReconciliationVerdict = "unresolved"
        if allowed and resolved_replay == "reconcilable" and self._reconciler is not None:
            prior = self.get_action(aid)
            if prior is not None and prior.tool_name == tname and prior.args_sha256 == digest:
                if prior.status in ("uncertain", "in_flight") or (
                    prior.status == "committed"
                    and prior.error_code in ("pre_takeover_verified_committed", "mcp_query_verified_committed")
                ):
                    try:
                        reconcile_verdict = self._reconciler(prior, tname, safe_args)
                    except Exception:
                        reconcile_verdict = "unresolved"

        def _tx(conn: sqlite3.Connection) -> LedgerBeginOutcome:
            conn.row_factory = sqlite3.Row
            existing_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            existing = _sel_row_to_action(existing_row) if existing_row is not None else None

            if existing is not None and (existing.tool_name != tname or existing.args_sha256 != digest):
                err = ActionIdentityConflictError(
                    f"Action `{aid}` already bound to `{existing.tool_name}` with a different argument digest."
                )
                return LedgerBeginOutcome(
                    proceed=False,
                    action_id=aid,
                    attempt=existing.attempt,
                    attempt_id=format_attempt_id(aid, existing.attempt),
                    status=existing.status,  # type: ignore[arg-type]
                    replay_policy=resolved_replay,
                    record=existing,
                    replay_blocked=True,
                    reason=str(err),
                    error=err,
                )

            effective_tid = clean_tid
            missing_tid_code = ""
            effective_allowed = allowed
            tx_fence_reason = ""
            tx_fence_error: Exception | None = None
            if effective_tid is not None:
                t_row = conn.execute(
                    "SELECT state, current_epoch FROM runtime_tasks WHERE task_id = ?",
                    (effective_tid,),
                ).fetchone()
                if t_row is None:
                    if not effective_allowed:
                        effective_tid = None
                        missing_tid_code = "unknown_task_id"
                    else:
                        raise KeyError(f"Runtime task {effective_tid!r} does not exist.")
                elif effective_allowed:
                    cur_ep = int(t_row["current_epoch"])
                    if cur_ep > 0:
                        ep_row = conn.execute(
                            "SELECT worker_id, status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                            (effective_tid, clean_epoch),
                        ).fetchone()
                        if clean_epoch != cur_ep or ep_row is None:
                            effective_allowed = False
                            missing_tid_code = "stale_epoch_fenced"
                            tx_fence_reason = (
                                f"Denied: Stale or mismatched task epoch {clean_epoch} for task `{effective_tid}` "
                                f"(current epoch: {cur_ep})."
                            )
                            tx_fence_error = runtime_persistence.StaleEpochError(tx_fence_reason)
                        elif str(ep_row["status"]) != "active":
                            effective_allowed = False
                            missing_tid_code = "inactive_epoch_fenced"
                            tx_fence_reason = (
                                f"Denied: Task `{effective_tid}` epoch {clean_epoch} is not active "
                                f"(status: `{ep_row['status']}`)."
                            )
                            tx_fence_error = runtime_persistence.StaleEpochError(tx_fence_reason)
                        elif float(ep_row["lease_expires_at"]) <= ts:
                            conn.execute(
                                """
                                UPDATE runtime_task_epochs
                                SET status = 'expired', fenced_at = ?, fence_reason = 'lease_expired_at_admission'
                                WHERE task_id = ? AND epoch = ? AND status = 'active'
                                """,
                                (ts, effective_tid, clean_epoch),
                            )
                            effective_allowed = False
                            missing_tid_code = "lease_expired_fenced"
                            tx_fence_reason = f"Denied: Task `{effective_tid}` epoch {clean_epoch} lease has expired."
                            tx_fence_error = runtime_persistence.LeaseExpiredError(tx_fence_reason)
                        elif clean_wid is None or clean_wid != str(ep_row["worker_id"]):
                            effective_allowed = False
                            missing_tid_code = "worker_mismatch_fenced"
                            tx_fence_reason = (
                                f"Denied: Worker `{clean_wid or ''}` does not own task `{effective_tid}` "
                                f"epoch {clean_epoch} (owned by `{ep_row['worker_id']}`)."
                            )
                            tx_fence_error = runtime_persistence.LeaseOwnershipConflictError(tx_fence_reason)
                    if effective_allowed and eff_class != "read_only":
                        unresolved_rows = conn.execute(
                            """
                            SELECT action_id FROM runtime_actions
                            WHERE task_id = ? AND action_id != ? AND action_class != 'read_only'
                              AND (status = 'uncertain' OR (status = 'in_flight' AND epoch < ?))
                            ORDER BY created_at ASC LIMIT 3
                            """,
                            (effective_tid, aid, clean_epoch),
                        ).fetchall()
                        if unresolved_rows:
                            unres_ids = tuple(str(r["action_id"]) for r in unresolved_rows)
                            effective_allowed = False
                            missing_tid_code = "unresolved_effects_hold"
                            tx_fence_reason = (
                                f"Denied: Consequential `{eff_class}` action `{tname}` blocked because "
                                f"task `{effective_tid}` has unresolved side effects ({', '.join(unres_ids)})."
                            )
                            tx_fence_error = UnresolvedEffectsHoldError(
                                tx_fence_reason,
                                task_id=effective_tid,
                                epoch=clean_epoch,
                                unresolved_action_ids=unres_ids,
                            )

            if not effective_allowed:
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO runtime_actions (
                            action_id, task_id, session_id, epoch, attempt, action_class,
                            replay_policy, tool_name, policy, explicit_approval, args_sha256,
                            status, ok, elapsed_ms, error_code, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'denied', 0, 1, ?, ?, ?)
                        """,
                        (
                            aid,
                            effective_tid,
                            str(session_id)[:128],
                            clean_epoch,
                            eff_class,
                            resolved_replay,
                            tname,
                            pol,
                            int(bool(explicit_approval)),
                            digest,
                            missing_tid_code or "admission_denied",
                            ts,
                            ts,
                        ),
                    )
                    denied_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert denied_row is not None
                    denied_rec = _sel_row_to_action(denied_row)
                    return LedgerBeginOutcome(
                        proceed=False,
                        action_id=aid,
                        attempt=denied_rec.attempt,
                        attempt_id=format_attempt_id(aid, denied_rec.attempt),
                        status="denied",
                        replay_policy=resolved_replay,
                        record=denied_rec,
                        replay_blocked=(tx_fence_error is not None),
                        reason=tx_fence_reason,
                        error=tx_fence_error,
                    )
                if existing.status in ("never_started", "denied", "failed"):
                    next_att = existing.attempt + 1
                    conn.execute(
                        """
                        UPDATE runtime_actions
                        SET attempt = ?, epoch = ?, policy = ?, explicit_approval = ?,
                            status = 'denied', ok = 0, elapsed_ms = 1,
                            error_code = ?, updated_at = ?
                        WHERE action_id = ?
                        """,
                        (
                            next_att,
                            clean_epoch,
                            pol,
                            int(bool(explicit_approval)),
                            missing_tid_code or "admission_denied",
                            ts,
                            aid,
                        ),
                    )
                    denied_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert denied_row is not None
                    denied_rec = _sel_row_to_action(denied_row)
                    return LedgerBeginOutcome(
                        proceed=False,
                        action_id=aid,
                        attempt=denied_rec.attempt,
                        attempt_id=format_attempt_id(aid, denied_rec.attempt),
                        status="denied",
                        replay_policy=resolved_replay,
                        record=denied_rec,
                        replay_blocked=(tx_fence_error is not None),
                        reason=tx_fence_reason,
                        error=tx_fence_error,
                    )
                # Preserve existing `committed`, `in_flight`, or `uncertain` record without clobbering it.
                return LedgerBeginOutcome(
                    proceed=False,
                    action_id=aid,
                    attempt=existing.attempt,
                    attempt_id=format_attempt_id(aid, existing.attempt),
                    status=existing.status,  # type: ignore[arg-type]
                    replay_policy=resolved_replay,
                    record=existing,
                    replay_blocked=(tx_fence_error is not None),
                    reason=tx_fence_reason,
                    error=tx_fence_error,
                )

            # `allowed` is True: commit durable `in_flight` intent or enforce replay classification!
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO runtime_actions (
                        action_id, task_id, session_id, epoch, attempt, action_class,
                        replay_policy, tool_name, policy, explicit_approval, args_sha256,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'in_flight', ?, ?)
                    """,
                    (
                        aid,
                        effective_tid,
                        str(session_id)[:128],
                        clean_epoch,
                        eff_class,
                        resolved_replay,
                        tname,
                        pol,
                        int(bool(explicit_approval)),
                        digest,
                        ts,
                        ts,
                    ),
                )
                ins_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                assert ins_row is not None
                ins_rec = _sel_row_to_action(ins_row)
                return LedgerBeginOutcome(
                    proceed=True,
                    action_id=aid,
                    attempt=1,
                    attempt_id=format_attempt_id(aid, 1),
                    status="in_flight",
                    replay_policy=resolved_replay,
                    record=ins_rec,
                )

            # Existing record found on replay of `action_id`:
            if existing.status == "committed":
                if resolved_replay == "idempotent":
                    next_att = existing.attempt + 1
                    conn.execute(
                        """
                        UPDATE runtime_actions
                        SET attempt = ?, epoch = ?, policy = ?, explicit_approval = ?,
                            status = 'in_flight', ok = NULL, elapsed_ms = NULL,
                            output_sha256 = '', error_code = '', updated_at = ?
                        WHERE action_id = ?
                        """,
                        (next_att, clean_epoch, pol, int(bool(explicit_approval)), ts, aid),
                    )
                    upd = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert upd is not None
                    return LedgerBeginOutcome(
                        proceed=True,
                        action_id=aid,
                        attempt=next_att,
                        attempt_id=format_attempt_id(aid, next_att),
                        status="in_flight",
                        replay_policy=resolved_replay,
                        record=_sel_row_to_action(upd),
                    )
                if (
                    resolved_replay == "reconcilable"
                    and existing.error_code in ("pre_takeover_verified_committed", "mcp_query_verified_committed")
                    and reconcile_verdict == "verified_committed"
                ):
                    conn.execute(
                        """
                        UPDATE runtime_actions
                        SET epoch = ?, error_code = '', updated_at = ?
                        WHERE action_id = ?
                        """,
                        (clean_epoch, ts, aid),
                    )
                    rec_upd = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert rec_upd is not None
                    return LedgerBeginOutcome(
                        proceed=False,
                        action_id=aid,
                        attempt=existing.attempt,
                        attempt_id=format_attempt_id(aid, existing.attempt),
                        status="committed",
                        replay_policy=resolved_replay,
                        record=_sel_row_to_action(rec_upd),
                        already_committed=True,
                        reconciled=True,
                        reason=(
                            f"Reconciled `{tname}` (`{aid}`): target state already matches intent; "
                            "skipping duplicate side effect."
                        ),
                    )
                err_c = CommittedActionReplayError(
                    f"Replay blocked: consequential action `{aid}` (`{tname}`) is already `committed` "
                    f"(attempt {existing.attempt}) and will not be executed again."
                )
                return LedgerBeginOutcome(
                    proceed=False,
                    action_id=aid,
                    attempt=existing.attempt,
                    attempt_id=format_attempt_id(aid, existing.attempt),
                    status="committed",
                    replay_policy=resolved_replay,
                    record=existing,
                    replay_blocked=True,
                    already_committed=True,
                    reason=str(err_c),
                    error=err_c,
                )

            if existing.status in ("never_started", "denied", "failed"):
                next_att = existing.attempt + 1
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET attempt = ?, task_id = COALESCE(?, task_id), epoch = ?, policy = ?,
                        explicit_approval = ?, status = 'in_flight', ok = NULL,
                        elapsed_ms = NULL, output_sha256 = '', error_code = '', updated_at = ?
                    WHERE action_id = ?
                    """,
                    (next_att, effective_tid, clean_epoch, pol, int(bool(explicit_approval)), ts, aid),
                )
                upd = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                assert upd is not None
                return LedgerBeginOutcome(
                    proceed=True,
                    action_id=aid,
                    attempt=next_att,
                    attempt_id=format_attempt_id(aid, next_att),
                    status="in_flight",
                    replay_policy=resolved_replay,
                    record=_sel_row_to_action(upd),
                )

            # `existing.status` is `'in_flight'` (missing result!) or `'uncertain'`:
            # Missing result on an `'in_flight'` record is NEVER treated as `'failed'` —
            # transition orphaned `'in_flight'` to `'uncertain'` before applying replay rules.
            cur_record = existing
            if existing.status == "in_flight":
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', ok = NULL,
                        error_code = CASE WHEN error_code = '' THEN 'missing_result' ELSE error_code END,
                        updated_at = ?
                    WHERE action_id = ? AND status = 'in_flight'
                    """,
                    (ts, aid),
                )
                re_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                assert re_row is not None
                cur_record = _sel_row_to_action(re_row)

            # Now `cur_record.status == 'uncertain'`: apply action-type replay classification!
            if resolved_replay == "idempotent":
                next_att = cur_record.attempt + 1
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET attempt = ?, epoch = ?, policy = ?, explicit_approval = ?,
                        status = 'in_flight', ok = NULL, elapsed_ms = NULL,
                        output_sha256 = '', error_code = '', updated_at = ?
                    WHERE action_id = ?
                    """,
                    (next_att, clean_epoch, pol, int(bool(explicit_approval)), ts, aid),
                )
                upd = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                assert upd is not None
                return LedgerBeginOutcome(
                    proceed=True,
                    action_id=aid,
                    attempt=next_att,
                    attempt_id=format_attempt_id(aid, next_att),
                    status="in_flight",
                    replay_policy=resolved_replay,
                    record=_sel_row_to_action(upd),
                )

            if resolved_replay == "reconcilable":
                if reconcile_verdict == "verified_committed":
                    conn.execute(
                        """
                        UPDATE runtime_actions
                        SET status = 'committed', ok = 1, error_code = 'reconciled_committed', updated_at = ?
                        WHERE action_id = ? AND status = 'uncertain'
                        """,
                        (ts, aid),
                    )
                    rec_row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert rec_row is not None
                    rec_obj = _sel_row_to_action(rec_row)
                    return LedgerBeginOutcome(
                        proceed=False,
                        action_id=aid,
                        attempt=rec_obj.attempt,
                        attempt_id=format_attempt_id(aid, rec_obj.attempt),
                        status="committed",
                        replay_policy=resolved_replay,
                        record=rec_obj,
                        already_committed=True,
                        reconciled=True,
                        reason=f"Reconciled `{tname}` (`{aid}`): target state already reflects the committed effect.",
                    )
                if reconcile_verdict == "verified_not_applied":
                    next_att = cur_record.attempt + 1
                    conn.execute(
                        """
                        UPDATE runtime_actions
                        SET attempt = ?, epoch = ?, policy = ?, explicit_approval = ?,
                            status = 'in_flight', ok = NULL, elapsed_ms = NULL,
                            output_sha256 = '', error_code = '', updated_at = ?
                        WHERE action_id = ?
                        """,
                        (next_att, clean_epoch, pol, int(bool(explicit_approval)), ts, aid),
                    )
                    upd = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
                    assert upd is not None
                    return LedgerBeginOutcome(
                        proceed=True,
                        action_id=aid,
                        attempt=next_att,
                        attempt_id=format_attempt_id(aid, next_att),
                        status="in_flight",
                        replay_policy=resolved_replay,
                        record=_sel_row_to_action(upd),
                        reconciled=True,
                    )

            err_u = UncertainActionReplayError(
                f"Replay blocked: `{resolved_replay}` action `{aid}` (`{tname}`, attempt {cur_record.attempt}) "
                f"is in `uncertain` state ({cur_record.error_code or 'unreconciled'}) and cannot be retried blindly."
            )
            return LedgerBeginOutcome(
                proceed=False,
                action_id=aid,
                attempt=cur_record.attempt,
                attempt_id=format_attempt_id(aid, cur_record.attempt),
                status="uncertain",
                replay_policy=resolved_replay,
                record=cur_record,
                replay_blocked=True,
                reason=str(err_u),
                error=err_u,
            )

        outcome = runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )
        if outcome.reconciled and self._on_action_resolved is not None:
            try:
                self._on_action_resolved(outcome.action_id)
            except Exception:
                pass
        return outcome

    def mark_never_started(
        self,
        action_id: str,
        *,
        error_code: str = "pre_execute_guard_blocked",
        now: float | None = None,
    ) -> runtime_persistence.ActionRecord:
        """Transition a pre-execution blocked attempt back to ``never_started``."""
        aid = _text(action_id, "action_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = 'never_started', ok = NULL, elapsed_ms = 0,
                    output_sha256 = '', error_code = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (str(error_code)[:120], ts, aid),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime action {aid!r} does not exist.")
            return _sel_row_to_action(row)

        return runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )

    def complete(
        self,
        action_id: str,
        result: Mapping[str, Any],
        elapsed_ms: int,
        *,
        now: float | None = None,
    ) -> runtime_persistence.ActionRecord:
        """Durably finalize a completed, deterministically failed, or uncertain tool result."""
        aid = _text(action_id, "action_id")
        out_digest = canonical_result_sha256(result)
        clean_ms = max(0, int(elapsed_ms))
        ts = time.time() if now is None else float(now)

        if bool(result.get("uncertain")):
            final_status: LedgerActionStatus = "uncertain"
            ok_val: int | None = None
            err_code = str(result.get("error_code") or "ambiguous_outcome")[:120]
        elif bool(result.get("ok")):
            final_status = "committed"
            ok_val = 1
            err_code = ""
        else:
            final_status = "failed"
            ok_val = 0
            err_code = str(result.get("error_code") or "tool_returned_false")[:120]

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = ?, ok = ?, elapsed_ms = ?, output_sha256 = ?, error_code = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (final_status, ok_val, clean_ms, out_digest, err_code, ts, aid),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime action {aid!r} does not exist.")
            return _sel_row_to_action(row)

        return runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )

    def record_exception(
        self,
        action_id: str,
        exc: BaseException,
        elapsed_ms: int,
        *,
        now: float | None = None,
    ) -> runtime_persistence.ActionRecord:
        """Finalize an exception raised during execution as ``uncertain`` (if ambiguous) or ``failed``."""
        aid = _text(action_id, "action_id")
        ambiguous = is_ambiguous_exception(exc)
        final_status: LedgerActionStatus = "uncertain" if ambiguous else "failed"
        ok_val: int | None = None if ambiguous else 0
        err_code = type(exc).__name__[:120]
        out_digest = canonical_result_sha256({"output": str(exc), "diff": ""})
        clean_ms = max(0, int(elapsed_ms))
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = ?, ok = ?, elapsed_ms = ?, output_sha256 = ?, error_code = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (final_status, ok_val, clean_ms, out_digest, err_code, ts, aid),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if row is None:
                raise KeyError(f"Runtime action {aid!r} does not exist.")
            return _sel_row_to_action(row)

        return runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )

    def reconcile_missing_results(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        error_code: str = "missing_result",
        now: float | None = None,
    ) -> list[runtime_persistence.ActionRecord]:
        """Transition orphaned ``in_flight`` actions with missing results to ``uncertain`` (never ``failed``)."""
        ts = time.time() if now is None else float(now)
        code = str(error_code or "missing_result")[:120]

        def _tx(conn: sqlite3.Connection) -> list[runtime_persistence.ActionRecord]:
            conn.row_factory = sqlite3.Row
            clauses = ["status = 'in_flight'"]
            params: list[Any] = []
            if task_id is not None:
                clauses.append("task_id = ?")
                params.append(_text(task_id, "task_id"))
            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(_text(session_id, "session_id"))
            where_sql = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT action_id FROM runtime_actions WHERE {where_sql} ORDER BY created_at ASC",
                tuple(params),
            ).fetchall()
            if not rows:
                return []
            ids = [str(r["action_id"]) for r in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE runtime_actions
                SET status = 'uncertain', ok = NULL, error_code = ?, updated_at = ?
                WHERE action_id IN ({placeholders}) AND status = 'in_flight'
                """,
                (code, ts, *ids),
            )
            updated_rows = conn.execute(
                f"SELECT * FROM runtime_actions WHERE action_id IN ({placeholders}) ORDER BY created_at ASC",
                tuple(ids),
            ).fetchall()
            return [_sel_row_to_action(r) for r in updated_rows]

        return runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )

    def resolve_uncertain_action(
        self,
        action_id: str,
        *,
        resolution: Literal["verified_committed", "verified_not_applied"],
        reason: str = "",
        now: float | None = None,
    ) -> runtime_persistence.ActionRecord:
        """Explicitly reconcile an ``uncertain`` action after verification."""
        aid = _text(action_id, "action_id")
        if resolution not in ("verified_committed", "verified_not_applied"):
            raise SideEffectError(f"Unsupported uncertain resolution: {resolution!r}")
        ts = time.time() if now is None else float(now)
        target_status = "committed" if resolution == "verified_committed" else "never_started"
        ok_val: int | None = 1 if resolution == "verified_committed" else None
        code = str(reason or resolution)[:120]

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.ActionRecord:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT status FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            if cur is None:
                raise KeyError(f"Runtime action {aid!r} does not exist.")
            if str(cur["status"]) not in ("uncertain", "in_flight"):
                raise SideEffectError(
                    f"Only `uncertain` or orphaned `in_flight` actions can be reconciled (got `{cur['status']}`)."
                )
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = ?, ok = ?, error_code = ?, updated_at = ?
                WHERE action_id = ?
                """,
                (target_status, ok_val, code, ts, aid),
            )
            row = conn.execute("SELECT * FROM runtime_actions WHERE action_id = ?", (aid,)).fetchone()
            assert row is not None
            return _sel_row_to_action(row)

        rec_resolved = runtime_persistence.run_write_transaction(
            self._storage, self._db_path, _tx, retry_policy=self._retry_policy
        )
        if self._on_action_resolved is not None:
            try:
                self._on_action_resolved(aid)
            except Exception:
                pass
        return rec_resolved

    def list_task_actions(
        self,
        task_id: str,
        *,
        statuses: Iterable[str] | None = None,
    ) -> list[runtime_persistence.ActionRecord]:
        """Return durable ``runtime_actions`` rows recorded against ``task_id``."""
        tid = _text(task_id, "task_id")
        status_list = [str(s) for s in statuses] if statuses is not None else []

        def _read(conn: sqlite3.Connection) -> list[runtime_persistence.ActionRecord]:
            conn.row_factory = sqlite3.Row
            if status_list:
                placeholders = ",".join("?" for _ in status_list)
                rows = conn.execute(
                    f"""
                    SELECT * FROM runtime_actions
                    WHERE task_id = ? AND status IN ({placeholders})
                    ORDER BY created_at ASC, action_id ASC
                    """,
                    (tid, *status_list),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runtime_actions WHERE task_id = ? ORDER BY created_at ASC, action_id ASC",
                    (tid,),
                ).fetchall()
            return [_sel_row_to_action(r) for r in rows]

        return runtime_persistence.run_read(
            self._storage, self._db_path, _read, retry_policy=self._retry_policy
        )

    def list_unresolved_consequential_actions(
        self,
        task_id: str,
        *,
        before_epoch: int | None = None,
    ) -> list[runtime_persistence.ActionRecord]:
        """Return consequential actions on ``task_id`` that are ``uncertain`` or orphaned ``in_flight``."""
        tid = _text(task_id, "task_id")

        def _read(conn: sqlite3.Connection) -> list[runtime_persistence.ActionRecord]:
            conn.row_factory = sqlite3.Row
            if before_epoch is None:
                rows = conn.execute(
                    """
                    SELECT * FROM runtime_actions
                    WHERE task_id = ? AND action_class != 'read_only'
                      AND status IN ('uncertain', 'in_flight')
                    ORDER BY created_at ASC, action_id ASC
                    """,
                    (tid,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM runtime_actions
                    WHERE task_id = ? AND action_class != 'read_only'
                      AND (status = 'uncertain' OR (status = 'in_flight' AND epoch < ?))
                    ORDER BY created_at ASC, action_id ASC
                    """,
                    (tid, int(before_epoch)),
                ).fetchall()
            return [_sel_row_to_action(r) for r in rows]

        return runtime_persistence.run_read(
            self._storage, self._db_path, _read, retry_policy=self._retry_policy
        )


@dataclass(frozen=True, slots=True)
class PreTakeoverReconciliationReport:
    """Summary of pre-takeover reconciliation for orphaned in-flight & uncertain effects (M5-P1-04)."""

    task_id: str
    swept_in_flight_ids: tuple[str, ...] = ()
    reconciled_committed_ids: tuple[str, ...] = ()
    reconciled_not_applied_ids: tuple[str, ...] = ()
    unresolved_action_ids: tuple[str, ...] = ()

    @property
    def has_unresolved_effects(self) -> bool:
        return bool(self.unresolved_action_ids)


@dataclass(frozen=True, slots=True)
class LeaseClaimOutcome:
    """Result of single-owner worker lease claim + pre-takeover reconciliation (M5-P1-04)."""

    task: runtime_persistence.TaskRecord
    epoch_record: runtime_persistence.TaskEpochRecord
    superseded_epochs: tuple[runtime_persistence.TaskEpochRecord, ...]
    reconciliation: PreTakeoverReconciliationReport


class WorkerLeaseCoordinator:
    """Coordinates single-owner worker leases, durable epoch fencing, and safe takeover (M5-P1-04).

    Guarantees:
    1. Only one unexpired active worker lease can own a task at a time; concurrent
       claimants fail with :class:`runtime_persistence.LeaseOwnershipConflictError`.
    2. Lease expiry marks the expired epoch ``expired`` without releasing or
       clearing unresolved ``in_flight`` / ``uncertain`` actions.
    3. Before replacement execution enters ``running`` in a new monotonic epoch,
       all orphaned ``in_flight`` actions on ``task_id`` are swept to ``uncertain``,
       any ``reconcilable`` actions with supplied candidate args are probed outside
       SQLite write locks, and if any consequential action remains ``uncertain``,
       the task is held in ``blocked`` and :class:`UnresolvedEffectsHoldError` is raised.
    """

    def __init__(
        self,
        store: runtime_persistence.RuntimePersistenceStore,
        ledger: DurableActionLedger,
        *,
        on_epochs_fenced: Callable[[str, Sequence[runtime_persistence.TaskEpochRecord], str], None] | None = None,
        resumption_blocker: Callable[[str, str], Sequence[str]] | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._on_epochs_fenced = on_epochs_fenced
        self._resumption_blocker = resumption_blocker

    @property
    def store(self) -> runtime_persistence.RuntimePersistenceStore:
        return self._store

    @property
    def ledger(self) -> DurableActionLedger:
        return self._ledger

    def reconcile_task_effects(
        self,
        task_id: str,
        *,
        known_action_args: Mapping[str, Mapping[str, Any]] | None = None,
        error_code: str = "lease_takeover_in_flight",
        now: float | None = None,
    ) -> PreTakeoverReconciliationReport:
        """Sweep orphaned ``in_flight`` rows to ``uncertain`` and probe reconcilable effects."""
        tid = _text(task_id, "task_id")
        ts = time.time() if now is None else float(now)

        swept = self._ledger.reconcile_missing_results(
            task_id=tid,
            error_code=error_code,
            now=ts,
        )
        swept_ids = tuple(r.action_id for r in swept)

        reconciled_committed: list[str] = []
        reconciled_not_applied: list[str] = []
        reconciler = self._ledger.reconciler
        if reconciler is not None:
            uncertain_rows = self._ledger.list_task_actions(tid, statuses=("uncertain",))
            for rec in uncertain_rows:
                if rec.replay_policy != "reconcilable":
                    continue
                cand_args = known_action_args.get(rec.action_id) if known_action_args else None
                if cand_args is not None:
                    if canonical_args_sha256(cand_args) != rec.args_sha256:
                        continue
                    probe_args: Mapping[str, Any] = cand_args
                elif rec.tool_name.startswith("mcp__"):
                    probe_args = {}
                else:
                    continue
                try:
                    verdict = reconciler(rec, rec.tool_name, probe_args)
                except Exception:
                    verdict = "unresolved"
                if verdict == "verified_committed":
                    self._ledger.resolve_uncertain_action(
                        rec.action_id,
                        resolution="verified_committed",
                        reason="pre_takeover_verified_committed",
                        now=ts,
                    )
                    reconciled_committed.append(rec.action_id)
                elif verdict == "verified_not_applied":
                    self._ledger.resolve_uncertain_action(
                        rec.action_id,
                        resolution="verified_not_applied",
                        reason="pre_takeover_verified_not_applied",
                        now=ts,
                    )
                    reconciled_not_applied.append(rec.action_id)

        unresolved = self._ledger.list_unresolved_consequential_actions(tid)
        return PreTakeoverReconciliationReport(
            task_id=tid,
            swept_in_flight_ids=swept_ids,
            reconciled_committed_ids=tuple(reconciled_committed),
            reconciled_not_applied_ids=tuple(reconciled_not_applied),
            unresolved_action_ids=tuple(r.action_id for r in unresolved),
        )

    def acquire_or_takeover(
        self,
        task_id: str,
        worker_id: str,
        *,
        session_id: str = "",
        title: str = "",
        workspace_dir: str = "",
        policy: str = "standard",
        lease_ttl_s: float = 30.0,
        force_takeover: bool = False,
        fence_reason: str = "lease_takeover",
        known_action_args: Mapping[str, Mapping[str, Any]] | None = None,
        now: float | None = None,
    ) -> LeaseClaimOutcome:
        """Atomically claim a worker lease, fence prior epochs, and reconcile before ``running``."""
        tid = _text(task_id, "task_id")
        wid = _text(worker_id, "worker_id")
        ts = time.time() if now is None else float(now)

        self._store.ensure_task(
            tid,
            session_id=session_id or tid,
            title=title,
            workspace_dir=workspace_dir,
            policy=policy,
            now=ts,
        )

        # Claim the monotonic epoch FIRST so:
        # 1) live unexpired owners reject concurrent callers before touching their in-flight actions;
        # 2) expired/superseded owners are fenced before pre-takeover reconciliation begins.
        epoch_rec, superseded = self._store.acquire_worker_lease(
            tid,
            wid,
            lease_ttl_s=lease_ttl_s,
            force_takeover=force_takeover,
            fence_reason=fence_reason,
            now=ts,
        )
        if superseded and self._on_epochs_fenced is not None:
            try:
                self._on_epochs_fenced(tid, superseded, fence_reason)
            except Exception:
                pass

        report = self.reconcile_task_effects(
            tid,
            known_action_args=known_action_args,
            now=ts,
        )
        extra_blockers: tuple[str, ...] = ()
        if self._resumption_blocker is not None:
            try:
                extra_blockers = tuple(str(x) for x in self._resumption_blocker(tid, workspace_dir) if str(x))
            except Exception:
                extra_blockers = ()
        if report.has_unresolved_effects or extra_blockers:
            combined_unresolved = tuple(dict.fromkeys((*report.unresolved_action_ids, *extra_blockers)))
            self._store.release_epoch(
                tid,
                epoch_rec.epoch,
                wid,
                next_state="blocked",
                reason="unresolved_effects_hold",
                now=ts,
            )
            sample = ", ".join(combined_unresolved[:3])
            raise UnresolvedEffectsHoldError(
                f"Replacement execution on task `{tid}` held (`blocked`) at epoch {epoch_rec.epoch}: "
                f"unresolved side effects ({sample}) must be reconciled before resuming.",
                task_id=tid,
                epoch=epoch_rec.epoch,
                unresolved_action_ids=combined_unresolved,
            )

        transition_reason = (
            "lease_takeover_reconciled" if superseded else "lease_acquired"
        )
        updated_task, _ = self._store.record_transition(
            tid,
            to_state="running",
            epoch=epoch_rec.epoch,
            reason=transition_reason,
            now=ts,
        )
        return LeaseClaimOutcome(
            task=updated_task,
            epoch_record=epoch_rec,
            superseded_epochs=tuple(superseded),
            reconciliation=report,
        )

    def heartbeat(
        self,
        task_id: str,
        epoch: int,
        worker_id: str,
        *,
        lease_ttl_s: float = 30.0,
        now: float | None = None,
    ) -> runtime_persistence.TaskEpochRecord:
        return self._store.heartbeat_epoch(
            task_id, epoch, worker_id, lease_ttl_s=lease_ttl_s, now=now
        )

    def release(
        self,
        task_id: str,
        epoch: int,
        worker_id: str,
        *,
        next_state: str | None = "paused",
        reason: str = "turn_completed",
        now: float | None = None,
    ) -> bool:
        return self._store.release_epoch(
            task_id,
            epoch,
            worker_id,
            next_state=next_state,
            reason=reason,
            now=now,
        )

    def fence_and_halt(
        self,
        task_id: str,
        *,
        target_state: str = "paused",
        reason: str = "fenced_by_operator",
        sweep_in_flight: bool = False,
        now: float | None = None,
    ) -> list[runtime_persistence.TaskEpochRecord]:
        """Fence active lease epochs on ``task_id`` and record a halted lifecycle state."""
        tid = _text(task_id, "task_id")
        ts = time.time() if now is None else float(now)
        task = self._store.get_task(tid)
        if task is None:
            return []
        fenced = self._store.fence_active_epoch(tid, reason=reason, now=ts)
        if fenced and self._on_epochs_fenced is not None:
            try:
                self._on_epochs_fenced(tid, fenced, reason)
            except Exception:
                pass
        if sweep_in_flight:
            self._ledger.reconcile_missing_results(task_id=tid, error_code=reason, now=ts)
        effective_state = target_state
        if target_state != "cancelled":
            unresolved = self._ledger.list_unresolved_consequential_actions(tid)
            if unresolved:
                effective_state = "blocked"
        if task.state != effective_state:
            self._store.record_transition(
                tid,
                to_state=effective_state,
                reason=reason,
                allow_inactive_epoch=True,
                now=ts,
            )
        return fenced
