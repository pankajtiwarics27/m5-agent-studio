"""Persisted workflow and durable root-task/milestone state machine (M5-P1-05)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Sequence

import child_delegation
import execution_budget
import process_supervision
import recovery_guard
import runtime_persistence
import verification_evidence
import workflow_checkpoints

WorkflowStatus = Literal["pending", "running", "paused", "completed", "failed", "cancelled"]
_STATUSES = frozenset({"pending", "running", "paused", "completed", "failed", "cancelled"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset({"running", "cancelled"}),
    "cancelled": frozenset(),
}

# Authoritative M5-P1-05 task/milestone transition matrix.
# Note: 'paused', 'blocked', 'failed', and 'waiting_approval' CANNOT transition
# directly to 'completed' — only an active 'running' task with zero pending
# pause/cancel requests, zero incomplete milestones, and zero unresolved side
# effects may enter 'completed'.
TASK_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "paused", "blocked", "cancelled"}),
    "running": frozenset({"running", "paused", "waiting_approval", "blocked", "completed", "failed", "cancelled"}),
    "waiting_approval": frozenset({"running", "paused", "blocked", "failed", "cancelled"}),
    "paused": frozenset({"running", "blocked", "cancelled"}),
    "blocked": frozenset({"running", "paused", "failed", "cancelled"}),
    "failed": frozenset({"running", "paused", "blocked", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}


class WorkflowStateError(ValueError):
    """Raised when workflow state or transition input is invalid."""


class WorkflowTransitionRejected(RuntimeError):
    """Raised when a requested transition is not legal for current state."""


class FalseCompletionRejected(WorkflowTransitionRejected):
    """Raised when a task claims completion without verified patch/test/ledger evidence."""


_SAFE_VERIFY_ENV_KEYS: frozenset[str] = frozenset(
    {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH", "VIRTUAL_ENV", "SYSTEMROOT"}
)


def _normalize_workspace_rel_path(raw_path: str, field_name: str = "path") -> str:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise WorkflowStateError(f"{field_name} must be a non-empty relative path without NUL characters.")
    cleaned = raw_path.strip().replace("\\", "/")
    p = Path(cleaned)
    if p.is_absolute():
        raise WorkflowStateError(f"{field_name} must be relative to workspace_dir: {raw_path!r}")
    parts = [pt for pt in p.parts if pt not in ("", ".")]
    if not parts or any(pt == ".." for pt in parts):
        raise WorkflowStateError(f"{field_name} must stay within workspace_dir without '..': {raw_path!r}")
    return "/".join(parts)


def _resolve_safe_workspace_target(workspace_dir: Path, rel_path: str) -> Path:
    ws_root = workspace_dir.resolve()
    norm = _normalize_workspace_rel_path(rel_path)
    cursor = ws_root
    for part in Path(norm).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise WorkflowStateError(f"Symlink escape rejected in verification path: {rel_path!r}")
    resolved = cursor.resolve()
    if resolved != ws_root and ws_root not in resolved.parents:
        raise WorkflowStateError(f"Verification path escapes workspace_dir: {rel_path!r}")
    return resolved


def compute_workspace_file_sha256(workspace_dir: Path, rel_path: str) -> str | None:
    """Return SHA-256 hex digest of ``workspace_dir / rel_path`` or ``None`` if absent."""
    target = _resolve_safe_workspace_target(workspace_dir, rel_path)
    if not target.exists():
        return None
    if not target.is_file():
        raise WorkflowStateError(f"Verification target is not a regular file: {rel_path!r}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskVerificationSpec:
    """Authoritative patch, protected-file, ledger, and test verification contract (M5-P1-06)."""

    require_managed_edit: bool = True
    require_git_diff: bool = False
    required_modified_files: tuple[str, ...] = ()
    required_files: tuple[str, ...] = ()
    protected_files: dict[str, str] = field(default_factory=dict)
    baseline_file_digests: dict[str, str | None] = field(default_factory=dict)
    require_agent_test_action: bool = False
    test_command: tuple[str, ...] = ()
    expected_test_output_contains: tuple[str, ...] = ()
    timeout_s: float = 30.0
    #: Test ids known to fail before this task ran (M5-P2-04). A failure that
    #: matches one of these is reported as pre-existing rather than introduced.
    pre_existing_failures: tuple[str, ...] = ()
    #: When true, a run whose only failures are pre-existing may still verify.
    #: Default false: pre-existing failures are classified honestly but completion
    #: still needs a clean run.
    allow_pre_existing_failures: bool = False
    #: Require the workspace revision to be identical before and after the
    #: authoritative test command, so the result is tied to one revision.
    require_revision_stability: bool = True
    #: Cheapest check that must pass before the broader command runs (M5-P2-04
    #: staged verification). A failing first stage stops the broader run.
    targeted_test_command: tuple[str, ...] = ()
    #: ``(criterion, test_ids)`` pairs declared by the caller. Coverage is
    #: recorded from the observed transcript, and an uncovered criterion fails
    #: completion instead of being assumed covered.
    acceptance_coverage: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        norm_mod = tuple(_normalize_workspace_rel_path(p, "required_modified_files") for p in self.required_modified_files)
        norm_req = tuple(_normalize_workspace_rel_path(p, "required_files") for p in self.required_files)
        norm_prot: dict[str, str] = {}
        for k, v in self.protected_files.items():
            nk = _normalize_workspace_rel_path(k, "protected_files")
            if not isinstance(v, str) or len(v.strip()) != 64:
                raise WorkflowStateError(f"protected_files[{nk!r}] must be a 64-char SHA-256 hex digest.")
            norm_prot[nk] = v.strip().lower()
        norm_base: dict[str, str | None] = {}
        for k, bval in self.baseline_file_digests.items():
            nk = _normalize_workspace_rel_path(k, "baseline_file_digests")
            if bval is not None:
                if not isinstance(bval, str) or len(bval.strip()) != 64:
                    raise WorkflowStateError(f"baseline_file_digests[{nk!r}] must be None or a 64-char SHA-256 hex digest.")
                norm_base[nk] = bval.strip().lower()
            else:
                norm_base[nk] = None
        cmd_tuple = tuple(str(tok) for tok in self.test_command)
        if any((not tok or "\x00" in tok) for tok in cmd_tuple):
            raise WorkflowStateError("test_command tokens must be non-empty strings without NUL.")
        exp_sub = tuple(str(s) for s in self.expected_test_output_contains if str(s))
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) or self.timeout_s <= 0 or self.timeout_s > 120.0:
            raise WorkflowStateError("timeout_s must be in (0, 120].")
        norm_pre: list[str] = []
        for raw_fail in self.pre_existing_failures:
            cleaned = str(raw_fail or "").strip()
            if not cleaned:
                continue
            if "\x00" in cleaned:
                raise WorkflowStateError("pre_existing_failures entries must be NUL-free strings.")
            if cleaned not in norm_pre:
                norm_pre.append(cleaned[:300])
        targeted_cmd = tuple(str(tok) for tok in self.targeted_test_command)
        if any((not tok or "\x00" in tok) for tok in targeted_cmd):
            raise WorkflowStateError("targeted_test_command tokens must be non-empty strings without NUL.")
        norm_cov: list[tuple[str, tuple[str, ...]]] = []
        seen_criteria: set[str] = set()
        for entry in self.acceptance_coverage:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise WorkflowStateError("acceptance_coverage entries must be (criterion, test_ids) pairs.")
            criterion = str(entry[0] or "").strip()
            raw_ids = entry[1] if entry[1] is not None else ()
            if not criterion or "\x00" in criterion:
                raise WorkflowStateError("acceptance_coverage criteria must be non-empty, NUL-free strings.")
            if isinstance(raw_ids, str) or not isinstance(raw_ids, (list, tuple)):
                raise WorkflowStateError("acceptance_coverage test ids must be a list/tuple of strings.")
            ids = tuple(dict.fromkeys(str(t).strip()[:300] for t in raw_ids if str(t).strip()))
            if not ids:
                raise WorkflowStateError(f"acceptance_coverage[{criterion!r}] needs at least one test id.")
            if criterion[:300] in seen_criteria:
                continue
            seen_criteria.add(criterion[:300])
            norm_cov.append((criterion[:300], ids))
        object.__setattr__(self, "targeted_test_command", targeted_cmd)
        object.__setattr__(self, "acceptance_coverage", tuple(norm_cov[:100]))
        object.__setattr__(self, "pre_existing_failures", tuple(norm_pre[:200]))
        object.__setattr__(self, "required_modified_files", norm_mod)
        object.__setattr__(self, "required_files", norm_req)
        object.__setattr__(self, "protected_files", norm_prot)
        object.__setattr__(self, "baseline_file_digests", norm_base)
        object.__setattr__(self, "test_command", cmd_tuple)
        object.__setattr__(self, "expected_test_output_contains", exp_sub)
        object.__setattr__(self, "timeout_s", float(self.timeout_s))

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_managed_edit": self.require_managed_edit,
            "require_git_diff": self.require_git_diff,
            "required_modified_files": list(self.required_modified_files),
            "required_files": list(self.required_files),
            "protected_files": dict(sorted(self.protected_files.items())),
            "baseline_file_digests": dict(sorted(self.baseline_file_digests.items())),
            "require_agent_test_action": self.require_agent_test_action,
            "test_command": list(self.test_command),
            "expected_test_output_contains": list(self.expected_test_output_contains),
            "timeout_s": round(self.timeout_s, 4),
            "pre_existing_failures": list(self.pre_existing_failures),
            "allow_pre_existing_failures": self.allow_pre_existing_failures,
            "require_revision_stability": self.require_revision_stability,
            "targeted_test_command": list(self.targeted_test_command),
            "acceptance_coverage": [
                {"criterion": criterion, "test_ids": list(ids)}
                for criterion, ids in self.acceptance_coverage
            ],
        }

    def sha256(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskVerificationSpec:
        if not isinstance(data, dict):
            raise WorkflowStateError("TaskVerificationSpec data must be a dict.")
        raw_mod = data.get("required_modified_files") or ()
        raw_req = data.get("required_files") or ()
        raw_prot = data.get("protected_files") or {}
        raw_base = data.get("baseline_file_digests") or {}
        raw_cmd = data.get("test_command") or ()
        raw_exp = data.get("expected_test_output_contains") or ()
        if not isinstance(raw_mod, (list, tuple)) or not isinstance(raw_req, (list, tuple)):
            raise WorkflowStateError("required_modified_files and required_files must be sequences.")
        if not isinstance(raw_prot, dict) or not isinstance(raw_base, dict):
            raise WorkflowStateError("protected_files and baseline_file_digests must be dicts.")
        if isinstance(raw_cmd, str) or not isinstance(raw_cmd, (list, tuple)):
            raise WorkflowStateError("test_command must be an argv list/tuple of strings.")
        if isinstance(raw_exp, str) or not isinstance(raw_exp, (list, tuple)):
            raise WorkflowStateError("expected_test_output_contains must be a list/tuple of strings.")
        return cls(
            require_managed_edit=bool(data.get("require_managed_edit", True)),
            require_git_diff=bool(data.get("require_git_diff", False)),
            required_modified_files=tuple(str(x) for x in raw_mod),
            required_files=tuple(str(x) for x in raw_req),
            protected_files={str(k): str(v) for k, v in raw_prot.items()},
            baseline_file_digests={str(k): (None if v is None else str(v)) for k, v in raw_base.items()},
            require_agent_test_action=bool(data.get("require_agent_test_action", False)),
            test_command=tuple(str(x) for x in raw_cmd),
            expected_test_output_contains=tuple(str(x) for x in raw_exp),
            timeout_s=float(data.get("timeout_s", 30.0)),
            pre_existing_failures=tuple(str(x) for x in (data.get("pre_existing_failures") or ())),
            allow_pre_existing_failures=bool(data.get("allow_pre_existing_failures", False)),
            require_revision_stability=bool(data.get("require_revision_stability", True)),
            targeted_test_command=tuple(str(x) for x in (data.get("targeted_test_command") or ())),
            acceptance_coverage=tuple(
                (str(entry.get("criterion", "")), tuple(str(t) for t in (entry.get("test_ids") or ())))
                for entry in (data.get("acceptance_coverage") or ())
                if isinstance(entry, dict)
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskVerificationReport:
    """Auditable outcome of authoritative patch + test + ledger completion verification."""

    verified: bool
    spec_sha256: str = ""
    failure_code: str = ""
    failure_detail: str = ""
    modified_files: tuple[str, ...] = ()
    patch_sha256: str = ""
    test_exit_code: int | None = None
    test_output_excerpt: str = ""
    test_sha256: str = ""
    committed_edit_action_ids: tuple[str, ...] = ()
    committed_test_action_ids: tuple[str, ...] = ()
    evidence_refs: tuple[workflow_checkpoints.EvidenceRef, ...] = ()
    #: Digest of the workspace revision the result is tied to (M5-P2-04).
    source_revision: str = ""
    revision: dict[str, Any] = field(default_factory=dict)
    test_evidence: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    staged_test_evidence: dict[str, Any] = field(default_factory=dict)
    acceptance_coverage: tuple[dict[str, Any], ...] = ()
    failure_classification: str = ""
    denied: bool = False
    #: Recovery-memory verdict for this attempt (M5-P2-05): state, repeat count,
    #: escalation, and whether a safe pause was requested.
    stagnation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "spec_sha256": self.spec_sha256,
            "failure_code": self.failure_code,
            "failure_detail": self.failure_detail,
            "modified_files": list(self.modified_files),
            "patch_sha256": self.patch_sha256,
            "test_exit_code": self.test_exit_code,
            "test_output_excerpt": self.test_output_excerpt,
            "test_sha256": self.test_sha256,
            "committed_edit_action_ids": list(self.committed_edit_action_ids),
            "committed_test_action_ids": list(self.committed_test_action_ids),
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "source_revision": self.source_revision,
            "revision": dict(self.revision),
            "test_evidence": dict(self.test_evidence),
            "environment": dict(self.environment),
            "staged_test_evidence": dict(self.staged_test_evidence),
            "acceptance_coverage": [dict(entry) for entry in self.acceptance_coverage],
            "failure_classification": self.failure_classification,
            "denied": self.denied,
            "stagnation": dict(self.stagnation),
        }


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    workflow_id: str
    state: WorkflowStatus
    version: int
    updated_at: str


class PersistedWorkflowState:
    """SQLite state store whose transitions are validated inside transactions."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        if "\x00" in str(self.database):
            raise WorkflowStateError("database path must not contain NUL characters.")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_state (
                    workflow_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (state IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled')),
                    CHECK (version >= 0)
                )
                """
            )

    @staticmethod
    def _workflow_id(workflow_id: str) -> str:
        if not isinstance(workflow_id, str) or not workflow_id.strip() or "\x00" in workflow_id:
            raise WorkflowStateError("workflow_id must be a non-empty string without NUL characters.")
        return workflow_id.strip()

    @staticmethod
    def _status(status: str) -> WorkflowStatus:
        if status not in _STATUSES:
            raise WorkflowStateError(f"unknown workflow state: {status}")
        return status  # type: ignore[return-value]

    @staticmethod
    def _record(row: sqlite3.Row | None) -> WorkflowRecord | None:
        if row is None:
            return None
        return WorkflowRecord(row["workflow_id"], row["state"], row["version"], row["updated_at"])

    def create(self, workflow_id: str, *, state: WorkflowStatus = "pending") -> WorkflowRecord:
        identifier = self._workflow_id(workflow_id)
        normalized = self._status(state)
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO workflow_state(workflow_id, state, version) VALUES (?, ?, 0)",
                    (identifier, normalized),
                )
                row = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise WorkflowStateError("workflow already exists.") from exc
        record = self._record(row)
        assert record is not None
        return record

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        identifier = self._workflow_id(workflow_id)
        with self._connect() as connection:
            return self._record(connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone())

    def transition(self, workflow_id: str, target: WorkflowStatus, *, expected_version: int | None = None) -> WorkflowRecord:
        identifier = self._workflow_id(workflow_id)
        next_state = self._status(target)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
            current = self._record(row)
            if current is None:
                raise WorkflowStateError("workflow does not exist.")
            if expected_version is not None and current.version != expected_version:
                raise WorkflowTransitionRejected("workflow version changed; refresh before transitioning.")
            if next_state not in _TRANSITIONS[current.state]:
                raise WorkflowTransitionRejected(f"transition {current.state} -> {next_state} is not allowed.")
            connection.execute(
                "UPDATE workflow_state SET state = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE workflow_id = ?",
                (next_state, identifier),
            )
            updated = connection.execute("SELECT * FROM workflow_state WHERE workflow_id = ?", (identifier,)).fetchone()
        result = self._record(updated)
        assert result is not None
        return result


@dataclass(frozen=True, slots=True)
class TaskControlResult:
    """Outcome of a safe-boundary pause or cancel command."""

    task: runtime_persistence.TaskRecord
    budget: execution_budget.TaskBudgetState
    deferred_to_safe_boundary: bool
    unresolved_action_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumedTaskPlan:
    """Validated recovery plan produced before resuming an interrupted task."""

    task: runtime_persistence.TaskRecord
    budget: execution_budget.TaskBudgetState
    checkpoint: workflow_checkpoints.TaskCheckpointRecord | None
    milestones: tuple[runtime_persistence.TaskRecord, ...]
    next_step: int
    completed_milestone_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoverySweepReport:
    """Summary of incomplete transitions and staged checkpoints reconciled on recovery."""

    recovered_task_ids: tuple[str, ...]
    blocked_task_ids: tuple[str, ...]
    cancelled_task_ids: tuple[str, ...]
    reconciled_checkpoint_ids: tuple[str, ...]
    reconciled_milestone_ids: tuple[str, ...]


def _validate_identifier(value: str, label: str = "task_id") -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise WorkflowStateError(f"{label} must be a non-empty string without NUL characters.")
    cleaned = value.strip()
    if len(cleaned) > 128:
        raise WorkflowStateError(f"{label} exceeds maximum length 128.")
    return cleaned


def milestone_task_id(root_task_id: str, milestone_key: str) -> str:
    """Build deterministic child ``runtime_tasks.task_id`` for a milestone."""
    root = _validate_identifier(root_task_id, "root_task_id")
    key = _validate_identifier(milestone_key, "milestone_key")
    candidate = f"{root}::ms:{key}"
    if len(candidate) <= 128:
        return candidate
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{root[:96]}::ms:{digest}"


def _encode_evidence_json(
    *,
    checkpoint_id: str = "",
    evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    refs_out: list[dict[str, str]] = []
    for item in evidence_refs:
        if isinstance(item, workflow_checkpoints.EvidenceRef):
            refs_out.append(item.to_dict())
        elif isinstance(item, dict):
            refs_out.append(workflow_checkpoints.EvidenceRef.from_dict(item).to_dict())
        else:
            to_dict_fn = getattr(item, "to_dict", None)
            if callable(to_dict_fn):
                refs_out.append(workflow_checkpoints.EvidenceRef.from_dict(to_dict_fn()).to_dict())
    payload: dict[str, Any] = {}
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    if refs_out:
        payload["evidence_refs"] = refs_out
    if extra:
        payload.update(extra)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DurableTaskStateMachine:
    """Authoritative SQLite state machine for root tasks, child milestones, and checkpoints."""

    def __init__(
        self,
        storage: runtime_persistence.StorageProvider,
        db_path: Path,
        *,
        retry_policy: runtime_persistence.BusyRetryPolicy = runtime_persistence.DEFAULT_BUSY_RETRY_POLICY,
        recovery_memory: recovery_guard.RecoveryGuard | None = None,
        containment_store: process_supervision.ContainmentStore | None = None,
    ) -> None:
        self._storage = storage
        self._db_path = db_path
        self._retry_policy = retry_policy
        self._containment_store = containment_store
        self.store = runtime_persistence.RuntimePersistenceStore(storage, db_path, retry_policy=retry_policy)
        self.budgets = execution_budget.DurableTaskBudgetManager(storage, db_path, retry_policy=retry_policy)
        self.checkpoints = workflow_checkpoints.DurableTaskCheckpointStore(
            storage,
            db_path,
            retry_policy=retry_policy,
        )
        self.recovery = recovery_memory if recovery_memory is not None else recovery_guard.RecoveryGuard(
            db_path.parent / "recovery"
        )

    def ensure_root_task(
        self,
        task_id: str,
        *,
        session_id: str = "",
        title: str = "",
        workspace_dir: str = "",
        policy: str = "standard",
        budget_spec: execution_budget.TaskBudgetSpec | None = None,
        override_budget_spec: bool = False,
        now: float | None = None,
    ) -> tuple[runtime_persistence.TaskRecord, execution_budget.TaskBudgetState]:
        tid = _validate_identifier(task_id, "task_id")
        self.store.ensure_task(
            tid,
            session_id=session_id or tid,
            title=title,
            state="pending",
            workspace_dir=workspace_dir,
            policy=policy,
            now=now,
        )
        budget = self.budgets.ensure_budget(
            tid,
            spec=budget_spec,
            override_spec=override_budget_spec,
            now=now,
        )
        refreshed = self.store.get_task(tid)
        assert refreshed is not None
        return refreshed, budget

    def ensure_milestone(
        self,
        root_task_id: str,
        milestone_key: str,
        *,
        title: str = "",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        root_id = _validate_identifier(root_task_id, "root_task_id")
        ms_id = milestone_task_id(root_id, milestone_key)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            parent_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (root_id,)).fetchone()
            if parent_row is None:
                raise WorkflowStateError(f"Root task {root_id!r} does not exist.")
            existing = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            if existing is not None:
                return runtime_persistence._row_to_task(existing)
            conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, parent_task_id, session_id, title, state,
                    current_epoch, state_version, workspace_dir, policy,
                    budget_json, checkpoint_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?, '{}', '', ?, ?)
                """,
                (
                    ms_id,
                    root_id,
                    str(parent_row["session_id"]),
                    str(title or milestone_key)[:240],
                    str(parent_row["workspace_dir"]),
                    str(parent_row["policy"]),
                    ts,
                    ts,
                ),
            )
            created = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            assert created is not None
            return runtime_persistence._row_to_task(created)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def list_milestones(self, root_task_id: str) -> list[runtime_persistence.TaskRecord]:
        root_id = _validate_identifier(root_task_id, "root_task_id")
        return [
            t for t in self.store.list_child_tasks(root_id)
            if not child_delegation.is_child_subtask_id(t.task_id)
        ]

    def list_child_subtasks(self, root_task_id: str) -> list[runtime_persistence.TaskRecord]:
        """Return delegated child subagent tasks (`::sub:`) under ``root_task_id`` (M5-P4-03)."""
        root_id = _validate_identifier(root_task_id, "root_task_id")
        return [
            t for t in self.store.list_child_tasks(root_id)
            if child_delegation.is_child_subtask_id(t.task_id)
        ]

    def spawn_child_subtask(
        self,
        parent_task_id: str,
        sub_key: str,
        *,
        title: str = "",
        policy: str = "plan",
        budget_spec: execution_budget.TaskBudgetSpec | None = None,
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Create a bounded child ``runtime_tasks`` row under ``parent_task_id`` (M5-P4-03)."""
        parent_id = _validate_identifier(parent_task_id, "parent_task_id")
        child_id = child_delegation.child_subtask_id(parent_id, sub_key)
        if policy not in child_delegation.CD_POLICY_RANK:
            raise WorkflowStateError(f"Unknown child policy {policy!r}.")
        ts = time.time() if now is None else float(now)
        init_budget = execution_budget.TaskBudgetState(
            spec=(budget_spec if budget_spec is not None else execution_budget.TaskBudgetSpec())
        )

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            parent_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (parent_id,)).fetchone()
            if parent_row is None:
                raise WorkflowStateError(f"Parent task {parent_id!r} does not exist.")
            parent_pol = str(parent_row["policy"])
            if child_delegation.CD_POLICY_RANK[policy] > child_delegation.CD_POLICY_RANK.get(parent_pol, -1):
                raise WorkflowTransitionRejected(
                    f"Child policy {policy!r} exceeds parent task {parent_id!r} policy {parent_pol!r}."
                )
            p_budget = execution_budget.TaskBudgetState.from_json(str(parent_row["budget_json"]))
            if p_budget.cancel_requested or str(parent_row["state"]) == "cancelled":
                raise WorkflowTransitionRejected(
                    f"Cannot spawn child subtask under cancelled parent {parent_id!r}."
                )
            existing = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (child_id,)).fetchone()
            if existing is not None:
                return runtime_persistence._row_to_task(existing)
            conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, parent_task_id, session_id, title, state,
                    current_epoch, state_version, workspace_dir, policy,
                    budget_json, checkpoint_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?, '', ?, ?)
                """,
                (
                    child_id,
                    parent_id,
                    str(parent_row["session_id"]),
                    str(title or sub_key)[:240],
                    str(parent_row["workspace_dir"]),
                    policy,
                    init_budget.to_json(),
                    ts,
                    ts,
                ),
            )
            created = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (child_id,)).fetchone()
            assert created is not None
            return runtime_persistence._row_to_task(created)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def mark_child_subtask_joined(
        self,
        child_task_id: str,
        *,
        delegation_id: str,
        disposition: str,
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Atomically verify terminal/zero-orphan child state and stamp ``joined:`` marker (M5-P4-03)."""
        cid = _validate_identifier(child_task_id, "child_task_id")
        did = _validate_identifier(delegation_id, "delegation_id")
        ts = time.time() if now is None else float(now)
        marker = f"joined:{did}:{disposition}"[:256]

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (cid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Child subtask {cid!r} does not exist.")
            c_state = str(row["state"])
            if c_state not in {"completed", "failed", "cancelled"}:
                raise WorkflowTransitionRejected(
                    f"Child subtask {cid!r} in non-terminal state {c_state!r} cannot be joined."
                )
            active_ep = conn.execute(
                "SELECT epoch FROM runtime_task_epochs WHERE task_id = ? AND status = 'active'",
                (cid,),
            ).fetchone()
            if active_ep is not None:
                raise WorkflowTransitionRejected(
                    f"Child subtask {cid!r} still has active epoch {active_ep['epoch']}; release or fence before joining."
                )
            unres = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                """,
                (cid,),
            ).fetchall()
            if unres:
                ids = ", ".join(str(r["action_id"]) for r in unres)
                raise WorkflowTransitionRejected(
                    f"Child subtask {cid!r} cannot be joined with unresolved consequential action(s): {ids}."
                )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET checkpoint_ref = ?, state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (marker, ts, cid),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (cid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def transition_milestone(
        self,
        root_task_id: str,
        milestone_key: str,
        to_state: str,
        *,
        epoch: int | None = None,
        expected_state_version: int | None = None,
        reason: str = "",
        checkpoint_id: str = "",
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        root_id = _validate_identifier(root_task_id, "root_task_id")
        ms_id = milestone_task_id(root_id, milestone_key)
        if to_state not in runtime_persistence.TASK_STATES:
            raise WorkflowStateError(f"unknown milestone state: {to_state}")
        evidence_json = _encode_evidence_json(checkpoint_id=checkpoint_id, evidence_refs=evidence_refs)
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            parent_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (root_id,)).fetchone()
            if parent_row is None:
                raise WorkflowStateError(f"Root task {root_id!r} does not exist.")
            if epoch is not None and int(parent_row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot transition milestone {ms_id!r} (root epoch={parent_row['current_epoch']})."
                )
            parent_budget = execution_budget.TaskBudgetState.from_json(str(parent_row["budget_json"]))
            if to_state == "completed" and (
                parent_budget.pause_requested
                or parent_budget.cancel_requested
                or str(parent_row["state"]) in {"paused", "cancelled"}
            ):
                raise WorkflowTransitionRejected(
                    f"Milestone {ms_id!r} cannot claim completion while root task pause/cancel is active."
                )
            ms_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            if ms_row is None:
                raise WorkflowStateError(f"Milestone {ms_id!r} does not exist.")
            from_state = str(ms_row["state"])
            if expected_state_version is not None and int(ms_row["state_version"]) != expected_state_version:
                raise WorkflowTransitionRejected("milestone state_version changed; refresh before transitioning.")
            if from_state == to_state and to_state in {"running", "paused", "cancelled"}:
                return runtime_persistence._row_to_task(ms_row)
            allowed = TASK_STATE_TRANSITIONS.get(from_state, frozenset())
            if to_state not in allowed:
                raise WorkflowTransitionRejected(
                    f"milestone transition {from_state} -> {to_state} is not allowed."
                )
            eff_epoch = int(parent_row["current_epoch"]) if epoch is None else int(epoch)
            new_ckpt = checkpoint_id if checkpoint_id else str(ms_row["checkpoint_ref"])
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (ms_id,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, current_epoch = ?, state_version = state_version + 1,
                    checkpoint_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (to_state, eff_epoch, new_ckpt[:256], ts, ms_id),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ms_id, next_seq, from_state, to_state, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (ms_id,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def transition_task(
        self,
        task_id: str,
        to_state: str,
        *,
        expected_state_version: int | None = None,
        epoch: int | None = None,
        allow_new_turn_from_completed: bool = False,
        allow_inactive_epoch: bool = False,
        reason: str = "",
        checkpoint_id: str | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        extra_evidence: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        tid = _validate_identifier(task_id, "task_id")
        if to_state not in runtime_persistence.TASK_STATES:
            raise WorkflowStateError(f"unknown task state: {to_state}")
        if to_state == "completed":
            raise WorkflowTransitionRejected(
                "Use complete_task() to transition a root task to 'completed' with milestone and boundary verification."
            )
        evidence_json = _encode_evidence_json(
            checkpoint_id=checkpoint_id or "",
            evidence_refs=evidence_refs,
            extra=extra_evidence,
        )
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            from_state = str(row["state"])
            if expected_state_version is not None and int(row["state_version"]) != expected_state_version:
                raise WorkflowTransitionRejected("task state_version changed; refresh before transitioning.")
            current_epoch = int(row["current_epoch"])
            if epoch is not None and current_epoch != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale task epoch {epoch} cannot transition task {tid!r} at current_epoch={current_epoch}."
                )
            if epoch is not None and int(epoch) > 0 and not allow_inactive_epoch:
                ep_row = conn.execute(
                    "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, int(epoch)),
                ).fetchone()
                if ep_row is None or str(ep_row["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Inactive task epoch {epoch} cannot transition task {tid!r}."
                    )
                if float(ep_row["lease_expires_at"]) <= ts:
                    raise runtime_persistence.LeaseExpiredError(
                        f"Expired task epoch {epoch} cannot transition task {tid!r}."
                    )
            allowed = TASK_STATE_TRANSITIONS.get(from_state, frozenset())
            if from_state == "completed" and to_state == "running" and allow_new_turn_from_completed:
                pass
            elif to_state not in allowed:
                raise WorkflowTransitionRejected(
                    f"task transition {from_state} -> {to_state} is not allowed."
                )
            eff_epoch = current_epoch if epoch is None else int(epoch)
            new_ckpt = str(row["checkpoint_ref"]) if checkpoint_ref_is_none(checkpoint_id) else str(checkpoint_id)[:256]
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = ?, state_version = state_version + 1, checkpoint_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (to_state, new_ckpt, ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, to_state, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def check_safe_boundary(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        abort_requested: bool = False,
    ) -> Literal["continue", "pause", "cancel"]:
        tid = _validate_identifier(task_id, "task_id")

        def _read(conn: sqlite3.Connection) -> Literal["continue", "pause", "cancel"]:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state, current_epoch, budget_json FROM runtime_tasks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if state == "cancelled" or budget.cancel_requested:
                return "cancel"
            if epoch is not None and int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Epoch {epoch} was superseded on task {tid!r} (current_epoch={row['current_epoch']})."
                )
            if state == "paused" or budget.pause_requested or abort_requested:
                return "pause"
            return "continue"

        return runtime_persistence.run_read(self._storage, self._db_path, _read, retry_policy=self._retry_policy)

    def request_pause(
        self,
        task_id: str,
        *,
        reason: str = "operator_pause_requested",
        now: float | None = None,
    ) -> TaskControlResult:
        """Pause an idle/orphaned task immediately or flag ``pause_requested`` for safe-boundary stop."""
        tid = _validate_identifier(task_id, "task_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskControlResult:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if state in {"completed", "cancelled"} or budget.cancel_requested:
                raise WorkflowTransitionRejected(
                    f"Cannot pause task {tid!r} in state {state!r}."
                )
            if state == "paused":
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(row),
                    budget=budget,
                    deferred_to_safe_boundary=False,
                )
            current_epoch = int(row["current_epoch"])
            has_live_lease = False
            if current_epoch > 0:
                ep_row = conn.execute(
                    "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, current_epoch),
                ).fetchone()
                if ep_row is not None and str(ep_row["status"]) == "active" and float(ep_row["lease_expires_at"]) > ts:
                    has_live_lease = True
            if has_live_lease and state in {"running", "waiting_approval"}:
                deferred_budget = execution_budget.TaskBudgetState(
                    spec=budget.spec,
                    steps_used=budget.steps_used,
                    tool_calls_used=budget.tool_calls_used,
                    tokens_used=budget.tokens_used,
                    elapsed_s=budget.elapsed_s,
                    recoveries_used=budget.recoveries_used,
                    pause_requested=True,
                    cancel_requested=budget.cancel_requested,
                    exhausted_reason=budget.exhausted_reason,
                    verification_contract=budget.verification_contract,
                )
                conn.execute(
                    "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                    (deferred_budget.to_json(), ts, tid),
                )
                child_run_rows = conn.execute(
                    "SELECT task_id, budget_json FROM runtime_tasks WHERE parent_task_id = ? AND state IN ('running', 'waiting_approval')",
                    (tid,),
                ).fetchall()
                for c_row in child_run_rows:
                    try:
                        cb = execution_budget.TaskBudgetState.from_json(str(c_row["budget_json"]))
                        cb_def = replace(cb, pause_requested=True)
                        conn.execute(
                            "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                            (cb_def.to_json(), ts, str(c_row["task_id"])),
                        )
                    except execution_budget.BudgetError:
                        pass
                refreshed = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
                assert refreshed is not None
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(refreshed),
                    budget=deferred_budget,
                    deferred_to_safe_boundary=True,
                )

            unresolved_rows = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                ORDER BY created_at ASC
                """,
                (tid, tid),
            ).fetchall()
            if unresolved_rows:
                raise WorkflowTransitionRejected(
                    f"Cannot pause task {tid!r} directly while consequential actions remain unresolved."
                )

            cleared_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=False,
                exhausted_reason=budget.exhausted_reason,
                verification_contract=budget.verification_contract,
            )
            ckpt_ref = str(row["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ckpt_ref),
                )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cleared_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                """,
                (
                    tid,
                    next_seq,
                    state,
                    current_epoch,
                    str(reason)[:240],
                    _encode_evidence_json(checkpoint_id=ckpt_ref),
                    ts,
                ),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, updated_at = ?
                WHERE parent_task_id = ? AND state IN ('running', 'waiting_approval')
                """,
                (ts, tid),
            )
            paused_row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert paused_row is not None
            return TaskControlResult(
                task=runtime_persistence._row_to_task(paused_row),
                budget=cleared_budget,
                deferred_to_safe_boundary=False,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def pause_at_safe_boundary(
        self,
        task_id: str,
        *,
        worker_id: str,
        epoch: int,
        step: int,
        checkpoint_payload: dict[str, Any] | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        milestone_id: str = "",
        reason: str = "paused_at_safe_boundary",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Persist an ``interrupted`` checkpoint, pause running milestones, and release lease into ``paused``."""
        tid = _validate_identifier(task_id, "task_id")
        wid = _validate_identifier(worker_id, "worker_id")
        ckpt: workflow_checkpoints.TaskCheckpointRecord | None = None
        if checkpoint_payload is not None:
            ckpt = self.checkpoints.save_checkpoint(
                tid,
                step=step,
                payload=checkpoint_payload,
                evidence_refs=evidence_refs,
                epoch=epoch,
                milestone_id=milestone_id,
                status="interrupted",
                require_active_epoch=True,
                now=now,
            )
        else:
            ckpt = self.checkpoints.mark_latest_interrupted(tid, now=now)

        ts = time.time() if now is None else float(now)
        evidence_json = _encode_evidence_json(
            checkpoint_id=ckpt.checkpoint_id if ckpt else "",
            evidence_refs=evidence_refs,
        )

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            if int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot pause task {tid!r} at current_epoch={row['current_epoch']}."
                )
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if str(row["state"]) == "cancelled" or budget.cancel_requested:
                raise WorkflowTransitionRejected(f"Task {tid!r} was cancelled before pause boundary completed.")
            ep_row = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (tid, int(epoch)),
            ).fetchone()
            if ep_row is None or str(ep_row["worker_id"]) != wid or str(ep_row["status"]) != "active":
                raise runtime_persistence.StaleEpochError(
                    f"Epoch {epoch} on task {tid!r} is no longer actively owned by {wid!r}."
                )
            conn.execute(
                "UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ? WHERE task_id = ? AND epoch = ?",
                (ts, tid, int(epoch)),
            )
            cleared_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=False,
                exhausted_reason=budget.exhausted_reason,
                verification_contract=budget.verification_contract,
            )
            from_state = str(row["state"])
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cleared_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, int(epoch), str(reason)[:240], evidence_json, ts),
            )
            ms_rows = conn.execute(
                "SELECT task_id, state FROM runtime_tasks WHERE parent_task_id = ? AND state IN ('running', 'waiting_approval')",
                (tid,),
            ).fetchall()
            for ms_row in ms_rows:
                ms_tid = str(ms_row["task_id"])
                ms_from = str(ms_row["state"])
                ms_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (ms_tid,),
                ).fetchone()
                ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                conn.execute(
                    "UPDATE runtime_tasks SET state = 'paused', state_version = state_version + 1, updated_at = ? WHERE task_id = ?",
                    (ts, ms_tid),
                )
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, 'paused', ?, ?, ?, ?)
                    """,
                    (ms_tid, ms_seq, ms_from, int(epoch), str(reason)[:240], evidence_json, ts),
                )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = "operator_cancelled",
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        now: float | None = None,
    ) -> TaskControlResult:
        """Fence active epochs, preserve unresolved side-effect records, and transition to ``cancelled``."""
        tid = _validate_identifier(task_id, "task_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> TaskControlResult:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            from_state = str(row["state"])
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if from_state == "completed":
                raise WorkflowTransitionRejected(f"Cannot cancel completed task {tid!r}.")
            if from_state == "cancelled":
                return TaskControlResult(
                    task=runtime_persistence._row_to_task(row),
                    budget=budget,
                    deferred_to_safe_boundary=False,
                )
            current_epoch = int(row["current_epoch"])
            conn.execute(
                """
                UPDATE runtime_task_epochs
                SET status = 'fenced', fenced_at = ?, fence_reason = 'task_cancelled'
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status = 'active'
                """,
                (ts, tid, tid),
            )
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = 'uncertain', error_code = 'cancelled_in_flight', updated_at = ?
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status = 'in_flight' AND action_class != 'read_only'
                """,
                (ts, tid, tid),
            )
            conn.execute(
                """
                UPDATE runtime_actions
                SET status = 'failed', ok = 0, error_code = 'cancelled_in_flight', updated_at = ?
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status = 'in_flight' AND action_class = 'read_only'
                """,
                (ts, tid, tid),
            )
            unresolved_rows = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                ORDER BY created_at ASC
                """,
                (tid, tid),
            ).fetchall()
            unresolved_ids = tuple(str(r["action_id"]) for r in unresolved_rows)

            ckpt_ref = str(row["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ckpt_ref),
                )
            cancelled_budget = execution_budget.TaskBudgetState(
                spec=budget.spec,
                steps_used=budget.steps_used,
                tool_calls_used=budget.tool_calls_used,
                tokens_used=budget.tokens_used,
                elapsed_s=budget.elapsed_s,
                recoveries_used=budget.recoveries_used,
                pause_requested=False,
                cancel_requested=True,
                exhausted_reason=budget.exhausted_reason,
                verification_contract=budget.verification_contract,
            )
            evidence_json = _encode_evidence_json(
                checkpoint_id=ckpt_ref,
                evidence_refs=evidence_refs,
                extra={"unresolved_action_ids": list(unresolved_ids)} if unresolved_ids else None,
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'cancelled', state_version = state_version + 1, budget_json = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (cancelled_budget.to_json(), ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'cancelled', ?, ?, ?, ?)
                """,
                (tid, next_seq, from_state, current_epoch, str(reason)[:240], evidence_json, ts),
            )
            ms_rows = conn.execute(
                """
                SELECT task_id, state FROM runtime_tasks
                WHERE parent_task_id = ? AND state NOT IN ('completed', 'cancelled')
                """,
                (tid,),
            ).fetchall()
            for ms_row in ms_rows:
                ms_tid = str(ms_row["task_id"])
                ms_from = str(ms_row["state"])
                ms_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (ms_tid,),
                ).fetchone()
                ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                conn.execute(
                    "UPDATE runtime_tasks SET state = 'cancelled', state_version = state_version + 1, updated_at = ? WHERE task_id = ?",
                    (ts, ms_tid),
                )
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, 'cancelled', ?, ?, ?, ?)
                    """,
                    (ms_tid, ms_seq, ms_from, current_epoch, str(reason)[:240], evidence_json, ts),
                )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return TaskControlResult(
                task=runtime_persistence._row_to_task(updated),
                budget=cancelled_budget,
                deferred_to_safe_boundary=False,
                unresolved_action_ids=unresolved_ids,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def _recovery_conditions(self, task: Any) -> recovery_guard.EvidenceSnapshot:
        """Observable conditions a recovery attempt would start from (M5-P2-05)."""
        workspace = str(getattr(task, "workspace_dir", "") or "").strip()
        if not workspace:
            return recovery_guard.EvidenceSnapshot()
        root = Path(workspace)
        files: tuple[str, ...] = ()
        try:
            contract = self.budgets.get_budget(task.task_id).verification_contract or {}
            raw_files = contract.get("required_modified_files") or ()
            if isinstance(raw_files, (list, tuple)):
                files = tuple(str(item) for item in raw_files)
        except (KeyError, execution_budget.BudgetError, runtime_persistence.PersistenceError):
            files = ()
        try:
            revision = verification_evidence.capture_source_revision(root, files)
            environment = verification_evidence.environment_snapshot(root)
        except (verification_evidence.VerificationEvidenceError, OSError):
            return recovery_guard.EvidenceSnapshot()
        return recovery_guard.EvidenceSnapshot(
            revision_digest=revision.digest,
            environment_digest=hashlib.sha256(
                json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )

    def prepare_resume(
        self,
        task_id: str,
        *,
        consume_recovery_budget: bool = True,
        now: float | None = None,
    ) -> ResumedTaskPlan:
        """Validate resumability, checkpoint integrity, unresolved side effects, and recovery budget."""
        tid = _validate_identifier(task_id, "task_id")
        task = self.store.get_task(tid)
        if task is None:
            raise WorkflowStateError(f"Task {tid!r} does not exist.")
        if task.state == "cancelled":
            raise WorkflowTransitionRejected(f"Cancelled task {tid!r} cannot be resumed.")
        if task.state == "completed":
            raise WorkflowTransitionRejected(f"Completed task {tid!r} does not require resume.")
        if task.state == "blocked":
            raise workflow_checkpoints.ResumeRejected(
                f"Blocked task {tid!r} has unresolved side effects or integrity violations and cannot resume automatically."
            )
        if task.state not in {"paused", "failed"}:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} in state {task.state!r} must be paused or failed before resume."
            )
        unresolved = [
            act.action_id
            for act in self.store.list_task_actions(tid, statuses=("in_flight", "uncertain"))
            if act.action_class != "read_only"
        ]
        if unresolved:
            raise workflow_checkpoints.ResumeRejected(
                f"Task {tid!r} has unresolved consequential action(s): {', '.join(unresolved)}."
            )
        if self._containment_store is not None:
            holds = self._containment_store.find_resumption_holds(
                task_id=tid,
                workspace_root=task.workspace_dir,
            )
            if holds:
                hold_labels = [h.action_id or h.hold_id for h in holds]
                raise workflow_checkpoints.ResumeRejected(
                    f"Task {tid!r} has conflicting containment hold(s) blocking resumption: "
                    f"{', '.join(hold_labels)}."
                )
        # Verify checkpoint integrity BEFORE consuming recovery budget.
        ckpt = self.checkpoints.load_latest_checkpoint(tid, verify_integrity=True)
        existing_budget = self.budgets.get_budget(tid)
        if existing_budget.cancel_requested:
            raise WorkflowTransitionRejected(f"Task {tid!r} has cancellation requested and cannot resume.")
        violation = existing_budget.violation_reason(check_recovery=False)
        if violation:
            raise execution_budget.BudgetExceeded(violation)

        # A recovery action needs changed evidence or changed conditions: refusing
        # here is what stops an unchanged workspace from being retried forever.
        conditions = self._recovery_conditions(task)
        if conditions.revision_digest or conditions.environment_digest:
            try:
                self.recovery.require_progress(tid, conditions, action="resume", compare="conditions")
            except recovery_guard.RecoveryRejected as exc:
                with contextlib.suppress(
                    KeyError, runtime_persistence.PersistenceError, execution_budget.BudgetError
                ):
                    self.budgets.set_control_flags(tid, pause_requested=True, now=now)
                if task.state != "paused":
                    with contextlib.suppress(WorkflowTransitionRejected):
                        self.transition_task(
                            tid,
                            "paused",
                            allow_inactive_epoch=True,
                            reason="recovery_requires_new_evidence",
                            checkpoint_id=ckpt.checkpoint_id if ckpt else None,
                            extra_evidence={"error": str(exc)},
                            now=now,
                        )
                raise WorkflowTransitionRejected(str(exc)) from exc
            except (recovery_guard.RecoveryGuardError, OSError):
                # Unreadable memory cannot prove progress, but it also cannot prove
                # repetition; the recovery budget remains the bound.
                pass

        if consume_recovery_budget:
            try:
                budget = self.budgets.consume_recovery(tid, now=now)
            except execution_budget.BudgetExceeded as exc:
                self.transition_task(
                    tid,
                    "blocked",
                    allow_inactive_epoch=True,
                    reason="recovery_budget_exceeded",
                    checkpoint_id=ckpt.checkpoint_id if ckpt else None,
                    extra_evidence={"error": str(exc)},
                    now=now,
                )
                raise
        else:
            budget = self.budgets.set_control_flags(tid, pause_requested=False, now=now)

        resumed_ckpt = self.checkpoints.validate_and_mark_resumed(
            tid,
            epoch=task.current_epoch,
            now=now,
        )
        milestones = tuple(self.list_milestones(tid))
        completed_ms = tuple(ms.task_id for ms in milestones if ms.state == "completed")
        refreshed_task = self.store.get_task(tid)
        assert refreshed_task is not None
        next_step = (resumed_ckpt.step + 1) if resumed_ckpt is not None else 0
        return ResumedTaskPlan(
            task=refreshed_task,
            budget=budget,
            checkpoint=resumed_ckpt,
            milestones=milestones,
            next_step=next_step,
            completed_milestone_ids=completed_ms,
        )

    def set_verification_spec(
        self,
        task_id: str,
        spec: TaskVerificationSpec | None,
        *,
        now: float | None = None,
    ) -> None:
        """Durably bind or clear a ``TaskVerificationSpec`` for ``task_id``."""
        tid = _validate_identifier(task_id, "task_id")
        self.budgets.set_verification_contract(
            tid,
            spec.to_dict() if spec is not None else None,
            now=now,
        )

    def get_verification_spec(self, task_id: str) -> TaskVerificationSpec | None:
        """Return the persisted ``TaskVerificationSpec`` for ``task_id``, if configured."""
        tid = _validate_identifier(task_id, "task_id")
        budget = self.budgets.get_budget(tid)
        if budget.verification_contract is None:
            return None
        return TaskVerificationSpec.from_dict(budget.verification_contract)

    def verify_task_completion(
        self,
        task_id: str,
        *,
        workspace_dir: Path,
        spec: TaskVerificationSpec | None = None,
    ) -> TaskVerificationReport:
        """Verify completion, then record the attempt in durable failure memory.

        The report is unchanged when the attempt made progress. When the same
        failure evidence repeats (or an abandoned state is revisited), the verdict
        is attached and a safe pause is requested instead of letting the loop try
        the same strategy again (M5-P2-05).
        """
        report = self._verify_task_completion_impl(task_id, workspace_dir=workspace_dir, spec=spec)
        return self._record_verification_attempt(_validate_identifier(task_id, "task_id"), report)

    def _record_verification_attempt(self, task_id: str, report: TaskVerificationReport) -> TaskVerificationReport:
        """Persist this attempt's evidence and attach the stagnation verdict."""
        failure_ids = tuple(
            str(record.get("test_id", ""))
            for record in (report.test_evidence.get("failures") or ())
            if isinstance(record, dict)
        )
        environment_digest = ""
        if report.environment:
            environment_digest = hashlib.sha256(
                json.dumps(report.environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        evidence = recovery_guard.EvidenceSnapshot(
            revision_digest=report.source_revision if len(report.source_revision) == 64 else "",
            transcript_sha256=report.test_sha256 if len(report.test_sha256) == 64 else "",
            failure_fingerprint=recovery_guard.failure_fingerprint(report.failure_code, failure_ids),
            failure_code=report.failure_code,
            failure_ids=failure_ids,
            environment_digest=environment_digest,
        )
        outcome = "verified" if report.verified else "failed"
        recovery_key = task_id.replace(":", "-").strip("-._")[:120] or "subtask"
        try:
            record, verdict = self.recovery.record_attempt(
                recovery_key, kind="verification", outcome=outcome, evidence=evidence
            )
        except (recovery_guard.RecoveryGuardError, OSError) as exc:
            if report.verified:
                return report
            # Fail closed: without memory there is no stagnation bound, so stop.
            return replace(
                report,
                failure_detail=f"{report.failure_detail} [recovery memory unavailable: {exc}]".strip()[:600],
                stagnation={"state": "unavailable", "escalate": True, "reason": str(exc)[:240], "pause_requested": True},
            )
        stagnation = dict(verdict.to_dict())
        stagnation["sequence"] = record.sequence
        stagnation["pause_requested"] = False
        if not report.verified and verdict.escalate:
            try:
                self.budgets.set_control_flags(task_id, pause_requested=True)
                stagnation["pause_requested"] = True
            except (KeyError, runtime_persistence.PersistenceError, execution_budget.BudgetError):
                stagnation["pause_requested"] = self.recovery.budget(task_id).pause_requested
        return replace(report, stagnation=stagnation)

    def _verify_task_completion_impl(
        self,
        task_id: str,
        *,
        workspace_dir: Path,
        spec: TaskVerificationSpec | None = None,
    ) -> TaskVerificationReport:
        """Authoritatively verify ledger, patch, protected-file, and test evidence outside write locks."""
        runtime_persistence.assert_no_active_write_transaction("task_verification")
        tid = _validate_identifier(task_id, "task_id")
        ws_root = Path(workspace_dir).resolve()
        effective_spec = spec if spec is not None else self.get_verification_spec(tid)
        spec_digest = effective_spec.sha256() if effective_spec is not None else ""

        own_actions = list(self.store.list_task_actions(tid))
        child_unresolved_actions: list[runtime_persistence.ActionRecord] = []
        accepted_child_actions: list[runtime_persistence.ActionRecord] = []
        for child_sub in self.list_child_subtasks(tid):
            c_acts = self.store.list_task_actions(child_sub.task_id)
            for ca in c_acts:
                if ca.status in ("in_flight", "uncertain") and ca.action_class != "read_only":
                    child_unresolved_actions.append(ca)
                elif str(child_sub.checkpoint_ref).endswith(":accepted"):
                    accepted_child_actions.append(ca)

        actions = sorted(
            [*own_actions, *accepted_child_actions],
            key=lambda a: (a.updated_at, a.created_at),
        )
        unresolved = [
            a.action_id
            for a in [*actions, *child_unresolved_actions]
            if a.status in ("in_flight", "uncertain") and a.action_class != "read_only"
        ]
        if unresolved:
            return TaskVerificationReport(
                verified=False,
                spec_sha256=spec_digest,
                failure_code="unresolved_consequential_actions",
                failure_detail=f"Unresolved consequential actions prevent completion: {', '.join(unresolved)}.",
            )

        committed_edits = [
            a for a in actions if a.action_class == "managed_edit" and a.status == "committed" and a.ok == 1
        ]
        shell_actions = [
            a for a in actions if a.tool_name == "run_bash" and a.status in ("committed", "failed")
        ]
        committed_tests = [a for a in shell_actions if a.status == "committed" and a.ok == 1]

        # Universal false-completion guard: if the latest shell/test action failed (`ok == 0`) with no
        # subsequent passing shell/test run, reject false completion even without an explicit contract.
        if shell_actions and shell_actions[-1].status == "failed":
            last_failed = shell_actions[-1]
            return TaskVerificationReport(
                verified=False,
                spec_sha256=spec_digest,
                failure_code="latest_shell_or_test_failed",
                failure_detail=(
                    f"Latest `run_bash` action `{last_failed.action_id}` failed (`ok=0`); "
                    "cannot claim completion without a subsequent passing verification run."
                ),
                committed_edit_action_ids=tuple(a.action_id for a in committed_edits),
                committed_test_action_ids=tuple(a.action_id for a in committed_tests),
            )

        if effective_spec is None:
            return TaskVerificationReport(
                verified=True,
                spec_sha256="",
                committed_edit_action_ids=tuple(a.action_id for a in committed_edits),
                committed_test_action_ids=tuple(a.action_id for a in committed_tests),
            )

        # 1. Protected user files must remain untouched (`sha256` identical to baseline).
        for prot_rel, expected_sha in sorted(effective_spec.protected_files.items()):
            try:
                actual_sha = compute_workspace_file_sha256(ws_root, prot_rel)
            except WorkflowStateError as exc:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="protected_file_invalid",
                    failure_detail=str(exc),
                )
            if actual_sha is None:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="protected_file_missing",
                    failure_detail=f"Protected user file `{prot_rel}` was deleted during task execution.",
                )
            if actual_sha != expected_sha:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="protected_file_modified",
                    failure_detail=(
                        f"Protected user file `{prot_rel}` was modified "
                        f"(expected sha256={expected_sha[:12]}..., got {actual_sha[:12]}...)."
                    ),
                )

        # 2. Managed-edit provenance in durable `runtime_actions`.
        if effective_spec.require_managed_edit and not committed_edits:
            return TaskVerificationReport(
                verified=False,
                spec_sha256=spec_digest,
                failure_code="missing_managed_edit",
                failure_detail="Completion rejected: zero committed `managed_edit` actions exist in `runtime_actions`.",
            )

        # 3. Required modified files must exist and differ from pre-task baseline digest.
        patch_entries: list[dict[str, str]] = []
        modified_paths: list[str] = []
        for mod_rel in effective_spec.required_modified_files:
            try:
                cur_sha = compute_workspace_file_sha256(ws_root, mod_rel)
            except WorkflowStateError as exc:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="invalid_modified_file_path",
                    failure_detail=str(exc),
                )
            if cur_sha is None:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="missing_required_file",
                    failure_detail=f"Required modified file `{mod_rel}` does not exist in workspace.",
                )
            if mod_rel in effective_spec.baseline_file_digests:
                base_sha = effective_spec.baseline_file_digests[mod_rel]
                if base_sha is not None and cur_sha == base_sha:
                    return TaskVerificationReport(
                        verified=False,
                        spec_sha256=spec_digest,
                        failure_code="unmodified_required_file",
                        failure_detail=f"Required target file `{mod_rel}` is unchanged from its pre-task digest.",
                    )
            modified_paths.append(mod_rel)
            patch_entries.append({"path": mod_rel, "sha256": cur_sha})

        for req_rel in effective_spec.required_files:
            try:
                target_fp = _resolve_safe_workspace_target(ws_root, req_rel)
            except WorkflowStateError as exc:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="invalid_required_file_path",
                    failure_detail=str(exc),
                )
            if not target_fp.is_file() or target_fp.stat().st_size == 0:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="missing_required_file",
                    failure_detail=f"Required artifact file `{req_rel}` is missing or empty.",
                )
            req_sha = hashlib.sha256(target_fp.read_bytes()).hexdigest()
            if req_rel not in modified_paths:
                modified_paths.append(req_rel)
                patch_entries.append({"path": req_rel, "sha256": req_sha})

        # 4. Optional Git diff verification.
        git_diff_text = ""
        if effective_spec.require_git_diff:
            if not (ws_root / ".git").exists():
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="missing_git_repository",
                    failure_detail="require_git_diff=True but workspace is not a Git repository.",
                )
            git_env = {k: v for k, v in os.environ.items() if k in _SAFE_VERIFY_ENV_KEYS}
            git_env["GIT_TERMINAL_PROMPT"] = "0"
            st_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ws_root,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
                env=git_env,
            )
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=ws_root,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
                env=git_env,
            )
            git_status_out = (st_proc.stdout or "").strip()
            git_diff_text = (diff_proc.stdout or "").strip()
            if st_proc.returncode != 0 or (not git_status_out and not git_diff_text):
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="missing_git_diff",
                    failure_detail="Completion rejected: Git working tree shows zero diff against HEAD.",
                )

        # 5. Post-edit agent test action ordering in `runtime_actions`.
        if effective_spec.require_agent_test_action:
            if not committed_tests:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="missing_agent_test_execution",
                    failure_detail="Completion rejected: agent never committed a passing `run_bash` test execution.",
                )
            if committed_edits and committed_tests[-1].updated_at < committed_edits[-1].updated_at:
                return TaskVerificationReport(
                    verified=False,
                    spec_sha256=spec_digest,
                    failure_code="stale_test_before_latest_edit",
                    failure_detail=(
                        "Completion rejected: managed file edit occurred AFTER the last passing test action; "
                        "tests were not re-run on the final workspace state."
                    ),
                )

        patch_payload = json.dumps(
            {
                "files": patch_entries,
                "git_diff_sha256": hashlib.sha256(git_diff_text.encode("utf-8")).hexdigest() if git_diff_text else "",
                "edit_actions": [a.action_id for a in committed_edits],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        patch_sha = hashlib.sha256(patch_payload.encode("utf-8")).hexdigest()
        refs: list[workflow_checkpoints.EvidenceRef] = [
            workflow_checkpoints.EvidenceRef(
                kind="patch_verification",
                ref_id=",".join(modified_paths)[:120] or "workspace_patch",
                sha256=patch_sha,
                summary=f"verified {len(modified_paths)} file(s), {len(committed_edits)} edit action(s)"[:240],
            )
        ]

        # 6. Authoritative independent test command execution, tied to one revision.
        try:
            base_revision = verification_evidence.capture_source_revision(ws_root, tuple(modified_paths))
        except (verification_evidence.VerificationEvidenceError, OSError) as exc:
            return TaskVerificationReport(
                verified=False,
                spec_sha256=spec_digest,
                failure_code="revision_capture_failed",
                failure_detail=f"Cannot tie the verification result to a source revision: {exc}",
            )
        revision_files = tuple(modified_paths)
        environment = verification_evidence.environment_snapshot(ws_root)
        test_exit: int | None = None
        test_excerpt = ""
        test_sha = ""
        evidence: verification_evidence.TestEvidence | None = None
        evidence_kwargs: dict[str, Any] = {
            "spec_sha256": spec_digest,
            "modified_files": tuple(modified_paths),
            "patch_sha256": patch_sha,
            "committed_edit_action_ids": tuple(a.action_id for a in committed_edits),
            "committed_test_action_ids": tuple(a.action_id for a in committed_tests),
            "source_revision": base_revision.digest,
            "revision": base_revision.to_dict(),
            "environment": dict(environment),
        }
        if effective_spec.targeted_test_command:
            # Stage 1: the cheapest check that proves the change forwards.
            try:
                targeted = verification_evidence.run_verification_command(
                    effective_spec.targeted_test_command,
                    workspace=ws_root,
                    timeout_s=effective_spec.timeout_s,
                    files=revision_files,
                    pre_existing=effective_spec.pre_existing_failures,
                )
            except (verification_evidence.VerificationEvidenceError, OSError) as exc:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_targeted_test_exec_error",
                    failure_detail=f"Targeted verification command could not be recorded: {exc}",
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            evidence_kwargs["staged_test_evidence"] = {"targeted": targeted.to_dict()}
            evidence_kwargs["failure_classification"] = targeted.classification
            evidence_kwargs["denied"] = bool(targeted.denied)
            targeted_label = " ".join(effective_spec.targeted_test_command)[:100]
            refs.append(
                workflow_checkpoints.EvidenceRef(
                    kind="targeted_test_verification",
                    ref_id=targeted_label or "targeted_test_command",
                    sha256=targeted.output_sha256,
                    summary=(
                        f"exit_code={targeted.exit_code}; classification={targeted.classification}; "
                        f"revision={targeted.revision_before.digest[:12] if targeted.revision_before else '?'}"
                    )[:240],
                )
            )
            if targeted.denied:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_test_denied",
                    failure_detail=(
                        f"Targeted verification command was refused, not run (`{targeted.denied}`); "
                        "a denied run is never a passing test result."
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            if targeted.exit_code is None:
                target_detail = targeted.failures[0].detail if targeted.failures else "no exit code"
                return TaskVerificationReport(
                    verified=False,
                    failure_code=(
                        "verification_test_timeout"
                        if "timed out" in target_detail
                        else "verification_targeted_test_exec_error"
                    ),
                    failure_detail=f"Targeted verification command did not complete: {target_detail}",
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            if effective_spec.require_revision_stability and targeted.revision_changed:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="workspace_changed_during_verification",
                    failure_detail=(
                        "The workspace revision changed while the targeted verification command ran; "
                        "the result cannot be tied to the revision it claims to verify."
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            targeted_tolerated = (
                effective_spec.allow_pre_existing_failures
                and targeted.classification == verification_evidence.RUN_PRE_EXISTING_ONLY
            )
            if targeted.exit_code != 0 and not targeted_tolerated:
                targeted_failing = ", ".join(
                    record.test_id or (record.detail[:60] or "<unparsed>") for record in targeted.failures[:5]
                )
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_targeted_test_failed",
                    failure_detail=(
                        f"Targeted verification command exited with code {targeted.exit_code} "
                        f"(classification={targeted.classification}; failures: {targeted_failing or 'unparsed'}); "
                        "the broader verification stage was not run."
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )

        if effective_spec.test_command:
            try:
                evidence = verification_evidence.run_verification_command(
                    effective_spec.test_command,
                    workspace=ws_root,
                    timeout_s=effective_spec.timeout_s,
                    files=revision_files,
                    pre_existing=effective_spec.pre_existing_failures,
                )
            except (verification_evidence.VerificationEvidenceError, OSError) as exc:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_test_exec_error",
                    failure_detail=f"Authoritative verification command could not be recorded: {exc}",
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            test_exit = evidence.exit_code
            test_excerpt = evidence.output_excerpt
            test_sha = evidence.output_sha256
            evidence_kwargs.update(
                {
                    "test_exit_code": evidence.exit_code,
                    "test_output_excerpt": test_excerpt,
                    "test_sha256": test_sha,
                    "source_revision": evidence.revision_before.digest if evidence.revision_before else base_revision.digest,
                    "revision": evidence.revision_before.to_dict() if evidence.revision_before else base_revision.to_dict(),
                    "test_evidence": evidence.to_dict(),
                    "failure_classification": evidence.classification,
                    "denied": bool(evidence.denied),
                }
            )

            if evidence.denied:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_test_denied",
                    failure_detail=(
                        "Authoritative verification command was refused, not run "
                        f"(`{evidence.denied}`); a denied run is never a passing test result."
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            if evidence.exit_code is None:
                first_detail = evidence.failures[0].detail if evidence.failures else "no exit code"
                timed_out = "timed out" in first_detail
                return TaskVerificationReport(
                    verified=False,
                    failure_code="verification_test_timeout" if timed_out else "verification_test_exec_error",
                    failure_detail=(
                        f"Authoritative verification command timed out after {effective_spec.timeout_s}s."
                        if timed_out
                        else f"Authoritative verification command failed to start: {first_detail}"
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            if effective_spec.require_revision_stability and evidence.revision_changed:
                return TaskVerificationReport(
                    verified=False,
                    failure_code="workspace_changed_during_verification",
                    failure_detail=(
                        "The workspace revision changed while the verification command ran "
                        f"({evidence.revision_before.digest[:12] if evidence.revision_before else '?'} -> "
                        f"{evidence.revision_after.digest[:12] if evidence.revision_after else '?'}); "
                        "the result cannot be tied to the revision it claims to verify."
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )

            tolerates_pre_existing = (
                effective_spec.allow_pre_existing_failures
                and evidence.classification == verification_evidence.RUN_PRE_EXISTING_ONLY
            )
            if evidence.exit_code != 0 and not tolerates_pre_existing:
                if evidence.classification == verification_evidence.RUN_ENVIRONMENT_BLOCKED:
                    failure_code = "verification_test_environment_blocked"
                elif evidence.classification == verification_evidence.RUN_PRE_EXISTING_ONLY:
                    failure_code = "verification_test_failed_pre_existing"
                elif evidence.classification == verification_evidence.RUN_INTRODUCED:
                    failure_code = "verification_test_failed_introduced"
                else:
                    failure_code = "verification_test_failed"
                failing = ", ".join(
                    record.test_id or (record.detail[:60] or "<unparsed>") for record in evidence.failures[:5]
                )
                return TaskVerificationReport(
                    verified=False,
                    failure_code=failure_code,
                    failure_detail=(
                        f"Authoritative test command exited with code {evidence.exit_code} "
                        f"(classification={evidence.classification}; failures: {failing or 'unparsed'}): "
                        f"{test_excerpt[:200]}"
                    ),
                    evidence_refs=tuple(refs),
                    **evidence_kwargs,
                )
            coverage = verification_evidence.evaluate_acceptance_coverage(
                effective_spec.acceptance_coverage,
                output=evidence.output_text,
                failures=evidence.failures,
            )
            if coverage:
                evidence_kwargs["acceptance_coverage"] = coverage
                uncovered = [entry for entry in coverage if not entry["covered"]]
                if uncovered:
                    missing_detail = "; ".join(
                        f"{entry['criterion'][:80]} (missing: {', '.join(entry['missing_test_ids'])[:120]})"
                        for entry in uncovered[:5]
                    )
                    return TaskVerificationReport(
                        verified=False,
                        failure_code="verification_acceptance_coverage_missing",
                        failure_detail=(
                            "Declared acceptance criteria were not demonstrated by the authoritative run: "
                            f"{missing_detail}"
                        ),
                        **evidence_kwargs,
                    )
            for needle in effective_spec.expected_test_output_contains:
                if needle not in (test_excerpt if evidence.exit_code == 0 else ""):
                    return TaskVerificationReport(
                        verified=False,
                        failure_code="verification_test_output_mismatch",
                        failure_detail=f"Authoritative test output did not contain required marker {needle!r}.",
                        **evidence_kwargs,
                    )
            cmd_label = " ".join(effective_spec.test_command)[:100]
            refs.append(
                workflow_checkpoints.EvidenceRef(
                    kind="test_verification",
                    ref_id=cmd_label or "test_command",
                    sha256=test_sha,
                    summary=(
                        f"exit_code={evidence.exit_code}; classification={evidence.classification}; "
                        f"revision={evidence_kwargs['source_revision'][:12]}"
                    )[:240],
                )
            )

        return TaskVerificationReport(
            verified=True,
            spec_sha256=spec_digest,
            modified_files=tuple(modified_paths),
            patch_sha256=patch_sha,
            test_exit_code=test_exit,
            test_output_excerpt=test_excerpt,
            test_sha256=test_sha,
            committed_edit_action_ids=tuple(a.action_id for a in committed_edits),
            committed_test_action_ids=tuple(a.action_id for a in committed_tests),
            evidence_refs=tuple(refs),
            source_revision=evidence_kwargs["source_revision"],
            revision=evidence_kwargs["revision"],
            test_evidence=evidence_kwargs.get("test_evidence", {}),
            staged_test_evidence=evidence_kwargs.get("staged_test_evidence", {}),
            acceptance_coverage=tuple(evidence_kwargs.get("acceptance_coverage", ())),
            failure_classification=evidence_kwargs.get("failure_classification", ""),
            denied=bool(evidence_kwargs.get("denied", False)),
        )

    def complete_task(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        epoch: int | None = None,
        step: int = 0,
        checkpoint_payload: dict[str, Any] | None = None,
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        verification_report: TaskVerificationReport | Any | None = None,
        abort_requested: bool = False,
        reason: str = "task_completed",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Complete a running root task only when all milestones, boundaries, actions, and verifications pass."""
        tid = _validate_identifier(task_id, "task_id")
        if abort_requested:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot claim completion when abort/pause was requested."
            )
        ts = time.time() if now is None else float(now)

        # Pre-check state, control flags, milestones, unresolved actions, and verification contract.
        current_task = self.store.get_task(tid)
        if current_task is None:
            raise WorkflowStateError(f"Task {tid!r} does not exist.")
        if current_task.state != "running":
            raise WorkflowTransitionRejected(
                f"Task {tid!r} in state {current_task.state!r} cannot transition to 'completed'."
            )
        current_budget = self.budgets.get_budget(tid)
        if current_budget.pause_requested or current_budget.cancel_requested:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot claim completion while pause_requested={current_budget.pause_requested} "
                f"or cancel_requested={current_budget.cancel_requested}."
            )
        milestones = self.list_milestones(tid)
        incomplete_ms = [ms.task_id for ms in milestones if ms.state != "completed"]
        if incomplete_ms:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot complete while milestone(s) remain incomplete: {', '.join(incomplete_ms)}."
            )
        child_subtasks = self.list_child_subtasks(tid)
        unjoined_subs = [
            sub.task_id
            for sub in child_subtasks
            if sub.state not in {"completed", "failed", "cancelled"}
            or not str(sub.checkpoint_ref or "").startswith("joined:")
        ]
        if unjoined_subs:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot complete while delegated child subtask(s) remain active or unjoined: "
                f"{', '.join(unjoined_subs)}."
            )
        all_actions = sorted(
            self.store.list_task_actions(tid),
            key=lambda a: (a.updated_at, a.created_at),
        )
        child_actions = [
            act
            for sub in child_subtasks
            for act in self.store.list_task_actions(sub.task_id)
        ]
        unresolved = [
            act.action_id
            for act in [*all_actions, *child_actions]
            if act.status in ("in_flight", "uncertain") and act.action_class != "read_only"
        ]
        if unresolved:
            raise WorkflowTransitionRejected(
                f"Task {tid!r} cannot complete with unresolved consequential action(s): {', '.join(unresolved)}."
            )
        shell_actions = [
            act for act in all_actions if act.tool_name == "run_bash" and act.status in ("committed", "failed")
        ]
        if shell_actions and shell_actions[-1].status == "failed":
            raise FalseCompletionRejected(
                f"Task {tid!r} cannot claim completion: latest `run_bash` action "
                f"`{shell_actions[-1].action_id}` failed (`ok=0`)."
            )

        persisted_spec = (
            TaskVerificationSpec.from_dict(current_budget.verification_contract)
            if current_budget.verification_contract is not None
            else None
        )
        merged_refs: list[workflow_checkpoints.EvidenceRef | dict[str, Any]] = list(evidence_refs)
        verification_extra: dict[str, Any] | None = None
        if persisted_spec is not None:
            if verification_report is None:
                raise FalseCompletionRejected(
                    f"Task {tid!r} has an active verification contract and cannot complete without a verification_report."
                )
            if not bool(getattr(verification_report, "verified", False)):
                detail = str(getattr(verification_report, "failure_detail", "") or "verification failed")
                raise FalseCompletionRejected(f"Task {tid!r} false completion rejected: {detail}")
            rep_spec_sha = str(getattr(verification_report, "spec_sha256", ""))
            if rep_spec_sha != persisted_spec.sha256():
                raise FalseCompletionRejected(
                    f"Task {tid!r} verification_report spec_sha256 mismatch against persisted contract."
                )
            for vref in getattr(verification_report, "evidence_refs", ()):
                merged_refs.append(vref)
            if hasattr(verification_report, "to_dict"):
                verification_extra = {"verification": verification_report.to_dict()}
        elif verification_report is not None:
            if not bool(getattr(verification_report, "verified", False)):
                detail = str(getattr(verification_report, "failure_detail", "") or "verification failed")
                raise FalseCompletionRejected(f"Task {tid!r} false completion rejected: {detail}")
            for vref in getattr(verification_report, "evidence_refs", ()):
                merged_refs.append(vref)

        ckpt: workflow_checkpoints.TaskCheckpointRecord | None = None
        if checkpoint_payload is not None:
            ckpt = self.checkpoints.save_checkpoint(
                tid,
                step=step,
                payload=checkpoint_payload,
                evidence_refs=merged_refs,
                epoch=epoch,
                status="committed",
                require_active_epoch=epoch is not None and epoch > 0,
                now=ts,
            )

        evidence_json = _encode_evidence_json(
            checkpoint_id=ckpt.checkpoint_id if ckpt else current_task.checkpoint_ref,
            evidence_refs=merged_refs,
            extra=verification_extra,
        )

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            if row is None:
                raise WorkflowStateError(f"Task {tid!r} does not exist.")
            if str(row["state"]) != "running":
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} left 'running' state ({row['state']!r}) before completion committed."
                )
            budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
            if budget.pause_requested or budget.cancel_requested:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} received pause/cancel before completion committed."
                )
            if budget.verification_contract is not None:
                tx_spec = TaskVerificationSpec.from_dict(budget.verification_contract)
                if verification_report is None or not bool(getattr(verification_report, "verified", False)):
                    raise FalseCompletionRejected(
                        f"Task {tid!r} cannot commit completion without a verified report."
                    )
                if str(getattr(verification_report, "spec_sha256", "")) != tx_spec.sha256():
                    raise FalseCompletionRejected(
                        f"Task {tid!r} verification contract changed before completion committed."
                    )
            current_epoch = int(row["current_epoch"])
            if epoch is not None and current_epoch != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot complete task {tid!r} at current_epoch={current_epoch}."
                )
            if epoch is not None and int(epoch) > 0:
                ep_row = conn.execute(
                    "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (tid, int(epoch)),
                ).fetchone()
                if ep_row is None or str(ep_row["status"]) != "active":
                    raise runtime_persistence.StaleEpochError(
                        f"Inactive epoch {epoch} cannot complete task {tid!r}."
                    )
                if worker_id is not None and str(ep_row["worker_id"]) != worker_id:
                    raise runtime_persistence.StaleEpochError(
                        f"Epoch {epoch} on task {tid!r} is not owned by {worker_id!r}."
                    )
                if float(ep_row["lease_expires_at"]) <= ts:
                    raise runtime_persistence.LeaseExpiredError(
                        f"Expired epoch {epoch} cannot complete task {tid!r}."
                    )
                conn.execute(
                    "UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ? WHERE task_id = ? AND epoch = ?",
                    (ts, tid, int(epoch)),
                )
            child_rows = conn.execute(
                "SELECT task_id, state, checkpoint_ref FROM runtime_tasks WHERE parent_task_id = ?",
                (tid,),
            ).fetchall()
            bad_ms = [
                str(r["task_id"])
                for r in child_rows
                if not child_delegation.is_child_subtask_id(str(r["task_id"])) and str(r["state"]) != "completed"
            ]
            if bad_ms:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} has incomplete milestone(s) inside completion commit."
                )
            bad_subs = [
                str(r["task_id"])
                for r in child_rows
                if child_delegation.is_child_subtask_id(str(r["task_id"]))
                and (
                    str(r["state"]) not in {"completed", "failed", "cancelled"}
                    or not str(r["checkpoint_ref"] or "").startswith("joined:")
                )
            ]
            if bad_subs:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} has active or unjoined child subtask(s) inside completion commit: {', '.join(bad_subs)}."
                )
            bad_actions = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                  AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                """,
                (tid, tid),
            ).fetchall()
            if bad_actions:
                raise WorkflowTransitionRejected(
                    f"Task {tid!r} has unresolved action(s) inside completion commit."
                )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (tid,),
            ).fetchone()
            next_seq = int(seq_row[0]) if seq_row else 1
            eff_epoch = current_epoch if epoch is None else int(epoch)
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'completed', state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (ts, tid),
            )
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, 'running', 'completed', ?, ?, ?, ?)
                """,
                (tid, next_seq, eff_epoch, str(reason)[:240], evidence_json, ts),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (tid,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def recover_incomplete_transitions(
        self,
        *,
        now: float | None = None,
    ) -> RecoverySweepReport:
        """Reconcile staged checkpoints, interrupted root transitions, and milestone skew on startup/recovery."""
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> RecoverySweepReport:
            conn.row_factory = sqlite3.Row
            reconciled_ckpts: list[str] = []
            recovered_tasks: list[str] = []
            blocked_tasks: list[str] = []
            cancelled_tasks: list[str] = []
            reconciled_milestones: list[str] = []

            # 1. Reconcile staged 'pending_commit' checkpoints left by mid-transition crashes.
            pending_ckpts = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE status = 'pending_commit' ORDER BY created_at ASC, seq ASC"
            ).fetchall()
            for ck_row in pending_ckpts:
                ck_id = str(ck_row["checkpoint_id"])
                ck_tid = str(ck_row["task_id"])
                try:
                    verified = workflow_checkpoints._decode_and_verify_checkpoint_row(ck_row, verify_integrity=True)
                except workflow_checkpoints.ResumeRejected:
                    conn.execute(
                        "UPDATE runtime_task_checkpoints SET status = 'superseded', updated_at = ? WHERE checkpoint_id = ?",
                        (ts, ck_id),
                    )
                    reconciled_ckpts.append(ck_id)
                    continue
                conn.execute(
                    """
                    UPDATE runtime_task_checkpoints
                    SET status = 'superseded', updated_at = ?
                    WHERE task_id = ? AND seq < ? AND status IN ('committed', 'interrupted', 'resumed')
                    """,
                    (ts, ck_tid, verified.seq),
                )
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ck_id),
                )
                conn.execute(
                    "UPDATE runtime_tasks SET checkpoint_ref = ?, updated_at = ? WHERE task_id = ?",
                    (ck_id, ts, ck_tid),
                )
                reconciled_ckpts.append(ck_id)

            # 2. Reconcile orphaned non-terminal root tasks without an active unexpired lease.
            root_rows = conn.execute(
                """
                SELECT * FROM runtime_tasks
                WHERE parent_task_id IS NULL
                  AND state IN ('running', 'waiting_approval', 'pending', 'paused', 'blocked', 'failed', 'cancelled')
                ORDER BY created_at ASC
                """
            ).fetchall()
            for t_row in root_rows:
                tid = str(t_row["task_id"])
                state = str(t_row["state"])
                current_epoch = int(t_row["current_epoch"])
                has_live_lease = False
                if current_epoch > 0:
                    ep_row = conn.execute(
                        "SELECT status, lease_expires_at FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                        (tid, current_epoch),
                    ).fetchone()
                    if ep_row is not None and str(ep_row["status"]) == "active":
                        if float(ep_row["lease_expires_at"]) > ts:
                            has_live_lease = True
                        else:
                            conn.execute(
                                """
                                UPDATE runtime_task_epochs
                                SET status = 'expired', fenced_at = ?, fence_reason = 'lease_expired_on_recovery'
                                WHERE task_id = ? AND epoch = ? AND status = 'active'
                                """,
                                (ts, tid, current_epoch),
                            )

                if has_live_lease:
                    continue

                # Sweep any orphaned in-flight actions on inactive epochs (parent + children) before deciding state.
                conn.execute(
                    """
                    UPDATE runtime_task_epochs
                    SET status = 'expired', fenced_at = ?, fence_reason = 'parent_recovery_sweep'
                    WHERE task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?)
                      AND status = 'active'
                    """,
                    (ts, tid),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                      AND status = 'in_flight' AND action_class != 'read_only'
                    """,
                    (ts, tid, tid),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'failed', ok = 0, error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                      AND status = 'in_flight' AND action_class = 'read_only'
                    """,
                    (ts, tid, tid),
                )

                try:
                    budget = execution_budget.TaskBudgetState.from_json(str(t_row["budget_json"]))
                except execution_budget.BudgetError:
                    budget = execution_budget.TaskBudgetState(spec=execution_budget.TaskBudgetSpec())

                if state in {"running", "waiting_approval"} or (
                    budget.cancel_requested and state not in {"cancelled", "completed"}
                ):
                    unresolved_rows = conn.execute(
                        """
                        SELECT action_id FROM runtime_actions
                        WHERE (task_id = ? OR task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?))
                          AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                        ORDER BY created_at ASC
                        """,
                        (tid, tid),
                    ).fetchall()
                    unresolved_ids = [str(r["action_id"]) for r in unresolved_rows]
                    ckpt_ref = str(t_row["checkpoint_ref"] or "").strip()
                    ckpt_corrupt = False
                    if ckpt_ref:
                        ck_check = conn.execute(
                            "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                            (ckpt_ref, tid),
                        ).fetchone()
                        if ck_check is None:
                            ckpt_corrupt = True
                        else:
                            try:
                                workflow_checkpoints._decode_and_verify_checkpoint_row(ck_check, verify_integrity=True)
                                conn.execute(
                                    """
                                    UPDATE runtime_task_checkpoints
                                    SET status = 'interrupted', updated_at = ?
                                    WHERE checkpoint_id = ? AND status IN ('committed', 'resumed')
                                    """,
                                    (ts, ckpt_ref),
                                )
                            except workflow_checkpoints.ResumeRejected:
                                ckpt_corrupt = True

                    if unresolved_ids or ckpt_corrupt:
                        target_state = "blocked"
                        reason = (
                            "corrupt_checkpoint_on_recovery"
                            if ckpt_corrupt
                            else "recovered_incomplete_with_unresolved_effects"
                        )
                        blocked_tasks.append(tid)
                    elif budget.cancel_requested:
                        target_state = "cancelled"
                        reason = "recovered_incomplete_cancel"
                        cancelled_tasks.append(tid)
                    else:
                        target_state = "paused"
                        reason = "recovered_incomplete_transition"
                        recovered_tasks.append(tid)

                    reconciled_budget = execution_budget.TaskBudgetState(
                        spec=budget.spec,
                        steps_used=budget.steps_used,
                        tool_calls_used=budget.tool_calls_used,
                        tokens_used=budget.tokens_used,
                        elapsed_s=budget.elapsed_s,
                        recoveries_used=budget.recoveries_used,
                        pause_requested=False,
                        cancel_requested=(target_state == "cancelled"),
                        exhausted_reason=budget.exhausted_reason,
                        verification_contract=budget.verification_contract,
                    )
                    seq_row = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                        (tid,),
                    ).fetchone()
                    next_seq = int(seq_row[0]) if seq_row else 1
                    conn.execute(
                        """
                        UPDATE runtime_tasks
                        SET state = ?, state_version = state_version + 1, budget_json = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (target_state, reconciled_budget.to_json(), ts, tid),
                    )
                    conn.execute(
                        """
                        INSERT INTO runtime_task_transitions (
                            task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tid,
                            next_seq,
                            state,
                            target_state,
                            current_epoch,
                            reason,
                            _encode_evidence_json(
                                checkpoint_id=ckpt_ref,
                                extra={"unresolved_action_ids": unresolved_ids} if unresolved_ids else None,
                            ),
                            ts,
                        ),
                    )
                    state = target_state

                # 3. Reconcile child milestone skew when parent has no live worker lease.
                if state in {"paused", "blocked", "failed", "cancelled"}:
                    ms_target = "cancelled" if state == "cancelled" else ("blocked" if state == "blocked" else "paused")
                    ms_filter = (
                        "state NOT IN ('completed', 'cancelled')"
                        if state == "cancelled"
                        else "state IN ('running', 'waiting_approval')"
                    )
                    skewed_ms = conn.execute(
                        f"SELECT task_id, state FROM runtime_tasks WHERE parent_task_id = ? AND {ms_filter}",
                        (tid,),
                    ).fetchall()
                    for ms_row in skewed_ms:
                        ms_tid = str(ms_row["task_id"])
                        ms_from = str(ms_row["state"])
                        ms_seq_row = conn.execute(
                            "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                            (ms_tid,),
                        ).fetchone()
                        ms_seq = int(ms_seq_row[0]) if ms_seq_row else 1
                        conn.execute(
                            """
                            UPDATE runtime_tasks
                            SET state = ?, state_version = state_version + 1, updated_at = ?
                            WHERE task_id = ?
                            """,
                            (ms_target, ts, ms_tid),
                        )
                        conn.execute(
                            """
                            INSERT INTO runtime_task_transitions (
                                task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, 'recovered_milestone_skew', '{}', ?)
                            """,
                            (ms_tid, ms_seq, ms_from, ms_target, current_epoch, ts),
                        )
                        reconciled_milestones.append(ms_tid)

            return RecoverySweepReport(
                recovered_task_ids=tuple(recovered_tasks),
                blocked_task_ids=tuple(blocked_tasks),
                cancelled_task_ids=tuple(cancelled_tasks),
                reconciled_checkpoint_ids=tuple(reconciled_ckpts),
                reconciled_milestone_ids=tuple(reconciled_milestones),
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )


def checkpoint_ref_is_none(value: str | None) -> bool:
    return value is None
