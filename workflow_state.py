"""Persisted workflow and durable root-task/milestone state machine (M5-P1-05)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

import child_delegation
import execution_budget
import execution_spine
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


class MissionGraphError(WorkflowStateError):
    """Raised when a versioned mission graph is malformed or conflicts with durable state."""


class MissionDependencyRejected(WorkflowTransitionRejected):
    """Raised when a mission node is not dependency-ready or its required outputs are absent."""


class MissionHaltUnsettled(MissionGraphError):
    """Raised when new work is planned while an earlier stopped plan is not settled.

    A stopped plan is settled once every consequential action it left unresolved has
    been reconciled and a supervised stop has verified that its owned processes ended.
    """


class MissionIsolationRejected(WorkflowTransitionRejected):
    """Raised when a mission needs runtime isolation that is absent or unqualified (AP-007)."""


class MissionBindingRejected(WorkflowTransitionRejected):
    """Raised when a captured plan/input/authority/budget identity is stale."""


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


def _capture_security_generation(provider: Callable[[], int | None] | None) -> int | None:
    """Read the current security configuration generation, or ``None`` when unknown.

    ``None`` means "not checked", never "unchanged": a caller that cannot produce a
    generation gets no staleness claim, and the report says nothing about it rather
    than implying stability.
    """
    if provider is None:
        return None
    try:
        value = provider()
    except Exception:  # noqa: BLE001 - an unreadable security store must not crash verification
        return None
    return int(value) if isinstance(value, int) else None


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


@dataclass(frozen=True, slots=True)
class MissionTaskSpec:
    """One immutable node in a versioned mission plan."""

    task_key: str
    title: str = ""
    required_outputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionDependencySpec:
    """Directed edge: ``task_key`` waits for ``depends_on_task_key``."""

    task_key: str
    depends_on_task_key: str
    required_outputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionOutputRef:
    """Verified-by-caller artifact identity persisted at mission-node completion."""

    output_key: str
    artifact_ref: str
    artifact_sha256: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MissionPlanRecord:
    mission_id: str
    plan_version: int
    root_task_id: str
    plan_digest: str
    input_digest: str
    session_policy_revision: int
    global_policy_revision: int
    budget_digest: str
    status: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class MissionExecutionBinding:
    """Server-captured immutable plan identity; never client/model authority."""

    mission_id: str
    plan_version: int
    root_task_id: str
    session_id: str
    plan_digest: str
    input_digest: str
    session_policy_revision: int
    global_policy_revision: int
    budget_digest: str

    def checkpoint_binding(self) -> workflow_checkpoints.MissionCheckpointBinding:
        return workflow_checkpoints.MissionCheckpointBinding(
            mission_id=self.mission_id,
            plan_version=self.plan_version,
            plan_digest=self.plan_digest,
            input_digest=self.input_digest,
            session_policy_revision=self.session_policy_revision,
            global_policy_revision=self.global_policy_revision,
            budget_digest=self.budget_digest,
        )


@dataclass(frozen=True, slots=True)
class MissionTaskReadiness:
    mission_id: str
    plan_version: int
    task_key: str
    task: runtime_persistence.TaskRecord
    ready: bool
    blocked_by_task_keys: tuple[str, ...] = ()
    failed_dependency_keys: tuple[str, ...] = ()
    missing_required_outputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionTaskLease:
    task: runtime_persistence.TaskRecord
    epoch: runtime_persistence.TaskEpochRecord


@dataclass(frozen=True, slots=True)
class MissionJoinTaskResult:
    task_key: str
    task_id: str
    state: str
    outcome: str
    required_outputs: tuple[str, ...]
    present_outputs: tuple[str, ...]
    missing_outputs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "task_id": self.task_id,
            "state": self.state,
            "outcome": self.outcome,
            "required_outputs": list(self.required_outputs),
            "present_outputs": list(self.present_outputs),
            "missing_outputs": list(self.missing_outputs),
        }


@dataclass(frozen=True, slots=True)
class MissionJoinReport:
    mission_id: str
    plan_version: int
    status: Literal["pending", "partial", "failed", "succeeded"]
    tasks: tuple[MissionJoinTaskResult, ...]
    evaluated_at: float
    report_hash: str

    @property
    def failed_task_keys(self) -> tuple[str, ...]:
        return tuple(item.task_key for item in self.tasks if item.outcome in {"failed", "cancelled"})

    @property
    def incomplete_task_keys(self) -> tuple[str, ...]:
        return tuple(item.task_key for item in self.tasks if item.outcome == "incomplete")

    @property
    def missing_output_task_keys(self) -> tuple[str, ...]:
        return tuple(item.task_key for item in self.tasks if item.outcome == "missing_required_outputs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "plan_version": self.plan_version,
            "status": self.status,
            "tasks": [item.to_dict() for item in self.tasks],
            "failed_task_keys": list(self.failed_task_keys),
            "incomplete_task_keys": list(self.incomplete_task_keys),
            "missing_output_task_keys": list(self.missing_output_task_keys),
            "evaluated_at": self.evaluated_at,
            "report_hash": self.report_hash,
        }


@dataclass(frozen=True, slots=True)
class MissionResumePlan:
    task: runtime_persistence.TaskRecord
    epoch: runtime_persistence.TaskEpochRecord
    checkpoint: workflow_checkpoints.TaskCheckpointRecord
    next_step: int


#: Ways a mission plan can be halted from outside its workers (AP-007).
MISSION_HALT_KINDS: tuple[str, ...] = ("cancelled", "bounded_stop")
_MISSION_RECOVERY_KIND = "mission_recovery"

#: What the reader can do about each limitation.  A report never ends at "it failed".
_MISSION_NEXT_ACTIONS: dict[str, str] = {
    "mission_cancelled": (
        "Review the retained outputs. The cancelled plan cannot be resumed; continuing needs a new plan version, "
        "or a new task if the root task itself was cancelled."
    ),
    "no_progress": (
        "Change the inputs or the approach before retrying. Continuing needs an owner-approved budget change "
        "and a new plan version."
    ),
    "repair_attempts": (
        "Stop repairing the same failure and inspect the recorded attempts. Continuing needs an owner-approved "
        "budget change and a new plan version."
    ),
    "deadline": (
        "The deadline has passed. Continuing needs a later deadline through an owner-approved budget change "
        "and a new plan version."
    ),
    "memory": (
        "The recorded peak stays on record, so retrying alone does not clear it. Continuing needs a higher "
        "memory ceiling through an owner-approved budget change and a new plan version."
    ),
    "storage": (
        "The recorded peak stays on record, so freeing space alone does not clear it. Continuing needs a higher "
        "storage ceiling through an owner-approved budget change and a new plan version."
    ),
    "measurement_unavailable": (
        "Record a real reading for the resource on the root task, or remove its ceiling through an "
        "owner-approved budget change, then create a new plan version. Work does not continue against a "
        "ceiling that cannot be measured."
    ),
    "budget_unreadable": (
        "A budget document could not be read, so no limit can be evaluated. Replace it through an "
        "owner-approved budget change before any work continues."
    ),
    "stop_record_missing": "Inspect the task transitions of this plan before continuing; its stop record is missing.",
    "isolation_unavailable": (
        "Qualify and enable an isolated runtime before this mission can run. It is not executed on the host."
    ),
    "uncertain_effects": (
        "Verify each listed action and reconcile it as committed or not applied before relying on this work. "
        "After a stop, no new plan is accepted for this task until every one is reconciled."
    ),
    "owned_work_lingering": (
        "Some owned processes could not be verified as stopped. Inspect the host, then run the stop again "
        "with a process supervisor. No new plan is accepted for this task until the stop is verified."
    ),
    "halt_required": (
        "Halt this plan with a process supervisor. Until then its workers are not fenced and its owned "
        "processes are not terminated."
    ),
    "owned_work_not_terminated": (
        "Run the stop again with a process supervisor. Until then, do not assume that owned processes have "
        "ended, and no new plan is accepted for this task."
    ),
    "tasks_incomplete": "The listed tasks are not complete, so the mission is not complete.",
    "missing_required_outputs": (
        "The listed tasks reported completion without their declared outputs. Do not treat them as delivered."
    ),
    "artifact_incomplete": (
        "The listed outputs are retained but are not finished, checked artifacts. Do not deliver them as complete."
    ),
    "artifacts_not_verified": (
        "Evaluate this report again with the artifact registry. Until then the retained outputs are unverified."
    ),
    "plan_superseded": "Read the report of the active plan version instead.",
}


@dataclass(frozen=True, slots=True)
class MissionRecoveryTask:
    """One mission node in a recovery report: what it reached and what it left behind."""

    task_key: str
    task_id: str
    state: str
    outcome: str
    retained_outputs: tuple[dict[str, Any], ...]
    missing_outputs: tuple[str, ...]
    unresolved_action_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "task_id": self.task_id,
            "state": self.state,
            "outcome": self.outcome,
            "retained_outputs": [dict(item) for item in self.retained_outputs],
            "missing_outputs": list(self.missing_outputs),
            "unresolved_action_ids": list(self.unresolved_action_ids),
        }


@dataclass(frozen=True, slots=True)
class MissionRecoveryReport:
    """Truthful parent report for a mission that may have stopped early (AP-007).

    ``claims_success`` is true only when the plan is live, the durable join
    succeeded, no consequential effect is unresolved, every retained output was
    checked as a complete artifact and no limitation remains.  Every other outcome
    names what was retained and what the reader can do next.
    """

    mission_id: str
    plan_version: int
    plan_status: str
    outcome: str
    claims_success: bool
    join_status: str
    tasks: tuple[MissionRecoveryTask, ...]
    stop: dict[str, Any] | None
    limitations: tuple[dict[str, str], ...]
    evaluated_at: float
    report_hash: str

    @property
    def retained_outputs(self) -> tuple[dict[str, Any], ...]:
        return tuple(output for task in self.tasks for output in task.retained_outputs)

    @property
    def unresolved_action_ids(self) -> tuple[str, ...]:
        return tuple(action for task in self.tasks for action in task.unresolved_action_ids)

    @property
    def limitation_codes(self) -> tuple[str, ...]:
        return tuple(item["code"] for item in self.limitations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_kind": _MISSION_RECOVERY_KIND,
            "mission_id": self.mission_id,
            "plan_version": self.plan_version,
            "plan_status": self.plan_status,
            "outcome": self.outcome,
            "claims_success": self.claims_success,
            "join_status": self.join_status,
            "tasks": [task.to_dict() for task in self.tasks],
            "stop": dict(self.stop) if self.stop is not None else None,
            "limitations": [dict(item) for item in self.limitations],
            "evaluated_at": self.evaluated_at,
            "report_hash": self.report_hash,
        }


@dataclass(frozen=True, slots=True)
class _MissionHaltTicket:
    """What the durable stop transaction hands to process containment."""

    mission_id: str
    plan_version: int
    session_id: str
    stop: dict[str, Any]
    # One entry per task the plan owns (nodes and their descendants):
    # (task_id, ((epoch, worker_id), ...)) where the epochs are the leases this stop fenced.
    scopes: tuple[tuple[str, tuple[tuple[int, str], ...]], ...]
    newly_halted: bool
    needs_containment: bool


def _mission_require_isolation(
    root_budget: execution_budget.TaskBudgetState,
    isolation: execution_budget.IsolationEvidence | None,
) -> None:
    """Refuse dispatch when the mission needs isolation that is not proven."""
    if not root_budget.spec.isolation_required:
        return
    refusal = execution_budget.isolation_refusal(isolation)
    if refusal:
        raise MissionIsolationRejected(
            f"{refusal}; dependent execution is blocked and the host shell is not used."
        )


def _mission_limitation(code: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "message": message[:600],
        "next_action": _MISSION_NEXT_ACTIONS.get(code, "Inspect this report before continuing."),
    }


def _mission_artifact_verdict(artifact_status: Callable[[str], Any], sha256: str) -> tuple[str, bool, str]:
    """Ask the AP-006 artifact owner whether one retained output is a finished artifact."""
    try:
        verdict = artifact_status(sha256)
    except Exception as exc:  # a checker that cannot answer is never read as "complete"
        return "unknown", False, f"artifact check could not run ({type(exc).__name__})"
    state = str(getattr(verdict, "state", "") or "unknown")[:40]
    complete = getattr(verdict, "complete", None) is True and state == "complete"
    return state, complete, str(getattr(verdict, "reason", "") or "")[:240]


def _mission_clean_name(value: object, label: str, *, max_len: int = 128) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MissionGraphError(f"{label} must be a non-empty NUL-free string.")
    cleaned = value.strip()
    if len(cleaned) > max_len or any(not (ch.isalnum() or ch in "._:-") for ch in cleaned):
        raise MissionGraphError(
            f"{label} must be at most {max_len} characters using letters, digits, '.', '_', ':', or '-'."
        )
    return cleaned


def _mission_sha256(value: object, label: str) -> str:
    cleaned = str(value or "").strip().lower()
    if len(cleaned) != 64 or any(ch not in "0123456789abcdef" for ch in cleaned):
        raise MissionGraphError(f"{label} must be a 64-character lowercase SHA-256 digest.")
    return cleaned


def mission_input_digest(inputs: Mapping[str, Any]) -> str:
    """Compute a canonical digest without persisting potentially sensitive input bytes."""
    if not isinstance(inputs, Mapping):
        raise MissionGraphError("mission inputs must be a mapping.")
    try:
        canonical = json.dumps(
            dict(inputs),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise MissionGraphError("mission inputs must be JSON serializable.") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mission_task_id(root_task_id: str, plan_version: int, task_key: str) -> str:
    """Build a deterministic runtime-task identity scoped to one immutable plan version."""
    root = _validate_identifier(root_task_id, "root_task_id")
    key = _mission_clean_name(task_key, "task_key")
    if isinstance(plan_version, bool) or not isinstance(plan_version, int) or plan_version < 1:
        raise MissionGraphError("plan_version must be an integer >= 1.")
    candidate = f"{root}::plan:{plan_version}:{key}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(f"{plan_version}:{key}".encode("utf-8")).hexdigest()[:20]
    return f"{root[:88]}::plan:{plan_version}:{digest}"[:128]


def _mission_plan_from_row(row: sqlite3.Row) -> MissionPlanRecord:
    return MissionPlanRecord(
        mission_id=str(row["mission_id"]),
        plan_version=int(row["plan_version"]),
        root_task_id=str(row["root_task_id"]),
        plan_digest=str(row["plan_digest"]),
        input_digest=str(row["input_digest"]),
        session_policy_revision=int(row["session_policy_revision"]),
        global_policy_revision=int(row["global_policy_revision"]),
        budget_digest=str(row["budget_digest"]),
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _mission_normalize_graph(
    root_task_id: str,
    mission_id: str,
    plan_version: int,
    tasks: Sequence[MissionTaskSpec],
    dependencies: Sequence[MissionDependencySpec],
) -> tuple[tuple[MissionTaskSpec, ...], tuple[MissionDependencySpec, ...], str]:
    mid = _mission_clean_name(mission_id, "mission_id")
    root = _validate_identifier(root_task_id, "root_task_id")
    if isinstance(plan_version, bool) or not isinstance(plan_version, int) or plan_version < 1:
        raise MissionGraphError("plan_version must be an integer >= 1.")
    if not tasks:
        raise MissionGraphError("a mission plan requires at least one task.")
    if len(tasks) > 512:
        raise MissionGraphError("a mission plan may contain at most 512 tasks.")

    normalized_tasks: list[MissionTaskSpec] = []
    task_outputs: dict[str, tuple[str, ...]] = {}
    for task_raw in tasks:
        if not isinstance(task_raw, MissionTaskSpec):
            raise MissionGraphError("tasks must contain MissionTaskSpec values.")
        key = _mission_clean_name(task_raw.task_key, "task_key")
        if key in task_outputs:
            raise MissionGraphError(f"duplicate mission task key {key!r}.")
        outputs = tuple(
            dict.fromkeys(
                _mission_clean_name(item, "required_output")
                for item in task_raw.required_outputs
            )
        )
        title = str(task_raw.title or key).strip()[:240]
        normalized = MissionTaskSpec(key, title, outputs)
        normalized_tasks.append(normalized)
        task_outputs[key] = outputs

    normalized_deps: list[MissionDependencySpec] = []
    seen_edges: set[tuple[str, str]] = set()
    adjacency: dict[str, list[str]] = {key: [] for key in task_outputs}
    indegree: dict[str, int] = {key: 0 for key in task_outputs}
    for dependency_raw in dependencies:
        if not isinstance(dependency_raw, MissionDependencySpec):
            raise MissionGraphError("dependencies must contain MissionDependencySpec values.")
        task_key = _mission_clean_name(dependency_raw.task_key, "dependency.task_key")
        prerequisite = _mission_clean_name(
            dependency_raw.depends_on_task_key,
            "dependency.depends_on_task_key",
        )
        if task_key not in task_outputs or prerequisite not in task_outputs:
            raise MissionGraphError(
                f"dependency {task_key!r} <- {prerequisite!r} references an unknown mission task."
            )
        if task_key == prerequisite:
            raise MissionGraphError(f"mission task {task_key!r} cannot depend on itself.")
        edge = (task_key, prerequisite)
        if edge in seen_edges:
            raise MissionGraphError(f"duplicate mission dependency {task_key!r} <- {prerequisite!r}.")
        seen_edges.add(edge)
        required = tuple(
            dict.fromkeys(
                _mission_clean_name(item, "dependency.required_output")
                for item in dependency_raw.required_outputs
            )
        )
        undeclared = sorted(set(required) - set(task_outputs[prerequisite]))
        if undeclared:
            raise MissionGraphError(
                f"dependency on {prerequisite!r} requires undeclared output(s): {', '.join(undeclared)}."
            )
        normalized_deps.append(MissionDependencySpec(task_key, prerequisite, required))
        adjacency[prerequisite].append(task_key)
        indegree[task_key] += 1

    queue = [item.task_key for item in normalized_tasks if indegree[item.task_key] == 0]
    visited = 0
    while queue:
        current = queue.pop(0)
        visited += 1
        for successor in adjacency[current]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if visited != len(normalized_tasks):
        cyclic = sorted(key for key, count in indegree.items() if count > 0)
        raise MissionGraphError(f"mission dependency cycle detected involving: {', '.join(cyclic)}.")

    graph_payload = {
        "mission_id": mid,
        "plan_version": plan_version,
        "root_task_id": root,
        "tasks": [
            {
                "task_key": item.task_key,
                "task_id": mission_task_id(root, plan_version, item.task_key),
                "title": item.title,
                "required_outputs": list(item.required_outputs),
            }
            for item in normalized_tasks
        ],
        "dependencies": [
            {
                "task_key": item.task_key,
                "depends_on_task_key": item.depends_on_task_key,
                "required_outputs": list(item.required_outputs),
            }
            for item in sorted(
                normalized_deps,
                key=lambda dep: (dep.task_key, dep.depends_on_task_key),
            )
        ],
    }
    canonical = json.dumps(graph_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return tuple(normalized_tasks), tuple(normalized_deps), hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mission_join_report_locked(
    conn: sqlite3.Connection,
    mission_id: str,
    plan_version: int,
    *,
    now: float,
) -> MissionJoinReport:
    plan = conn.execute(
        "SELECT 1 FROM runtime_mission_plans WHERE mission_id = ? AND plan_version = ?",
        (mission_id, plan_version),
    ).fetchone()
    if plan is None:
        raise MissionGraphError(f"Mission plan {mission_id!r} v{plan_version} does not exist.")
    rows = conn.execute(
        """
        SELECT mt.task_key, mt.task_id, mt.required_outputs_json, rt.state
        FROM runtime_mission_tasks AS mt
        JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
        WHERE mt.mission_id = ? AND mt.plan_version = ?
        ORDER BY mt.position ASC, mt.task_key ASC
        """,
        (mission_id, plan_version),
    ).fetchall()
    results: list[MissionJoinTaskResult] = []
    completed_count = 0
    has_failure = False
    has_incomplete = False
    for row in rows:
        try:
            raw_required = json.loads(str(row["required_outputs_json"]))
        except (TypeError, ValueError) as exc:
            raise MissionGraphError("persisted mission output contract is malformed.") from exc
        if not isinstance(raw_required, list):
            raise MissionGraphError("persisted mission output contract must be a list.")
        required = tuple(str(item) for item in raw_required)
        output_rows = conn.execute(
            """
            SELECT output_key FROM runtime_mission_outputs
            WHERE mission_id = ? AND plan_version = ? AND task_id = ?
            ORDER BY output_key ASC
            """,
            (mission_id, plan_version, str(row["task_id"])),
        ).fetchall()
        present = tuple(str(item["output_key"]) for item in output_rows)
        missing = tuple(sorted(set(required) - set(present)))
        state = str(row["state"])
        if state == "completed" and not missing:
            outcome = "completed"
            completed_count += 1
        elif state == "completed":
            outcome = "missing_required_outputs"
            has_failure = True
        elif state in {"failed", "cancelled"}:
            outcome = state
            has_failure = True
        else:
            outcome = "incomplete"
            has_incomplete = True
        results.append(
            MissionJoinTaskResult(
                task_key=str(row["task_key"]),
                task_id=str(row["task_id"]),
                state=state,
                outcome=outcome,
                required_outputs=required,
                present_outputs=present,
                missing_outputs=missing,
            )
        )
    if results and completed_count == len(results):
        status: Literal["pending", "partial", "failed", "succeeded"] = "succeeded"
    elif completed_count > 0:
        status = "partial"
    elif has_failure:
        status = "failed"
    elif has_incomplete:
        status = "pending"
    else:
        status = "failed"
    core = {
        "mission_id": mission_id,
        "plan_version": plan_version,
        "status": status,
        "tasks": [item.to_dict() for item in results],
    }
    report_hash = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return MissionJoinReport(mission_id, plan_version, status, tuple(results), now, report_hash)


def _mission_persist_join_locked(conn: sqlite3.Connection, report: MissionJoinReport) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO runtime_mission_joins (
            mission_id, plan_version, status, report_json, report_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            report.mission_id,
            report.plan_version,
            report.status,
            json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            report.report_hash,
            report.evaluated_at,
        ),
    )


def _mission_latest_stop_locked(
    conn: sqlite3.Connection,
    mission_id: str,
    plan_version: int,
) -> dict[str, Any] | None:
    """Return the newest persisted stop record, so a measured stop survives a restart."""
    rows = conn.execute(
        """
        SELECT report_json FROM runtime_mission_joins
        WHERE mission_id = ? AND plan_version = ?
          AND report_json LIKE '%"report_kind":"mission_recovery"%'
        ORDER BY id DESC LIMIT 5
        """,
        (mission_id, plan_version),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(str(row["report_json"]))
        except (TypeError, ValueError):
            continue
        if (
            isinstance(data, dict)
            and data.get("report_kind") == _MISSION_RECOVERY_KIND
            and isinstance(data.get("stop"), dict)
        ):
            return dict(data["stop"])
    return None


def _mission_owned_locked(conn: sqlite3.Connection, mission_id: str, plan_version: int) -> list[sqlite3.Row]:
    """Every durable task one plan version owns: its nodes and all of their descendants.

    A node may have delegated children of its own.  They are owned work too, so a
    stop that covered only the nodes would leave those workers live.
    """
    rows = conn.execute(
        """
        WITH RECURSIVE owned(task_id, node_task_id, task_key, position) AS (
            SELECT mt.task_id, mt.task_id, mt.task_key, mt.position
            FROM runtime_mission_tasks AS mt
            WHERE mt.mission_id = ? AND mt.plan_version = ?
            UNION
            SELECT rt.task_id, owned.node_task_id, owned.task_key, owned.position
            FROM runtime_tasks AS rt
            JOIN owned ON rt.parent_task_id = owned.task_id
        )
        SELECT owned.task_key, owned.node_task_id, rt.task_id, rt.state, rt.current_epoch,
               rt.checkpoint_ref, rt.budget_json
        FROM owned
        JOIN runtime_tasks AS rt ON rt.task_id = owned.task_id
        ORDER BY owned.position ASC, owned.task_key ASC,
                 (rt.task_id != owned.node_task_id) ASC, rt.task_id ASC
        """,
        (mission_id, plan_version),
    ).fetchall()
    seen: set[str] = set()
    unique: list[sqlite3.Row] = []
    for row in rows:
        task_id = str(row["task_id"])
        if task_id not in seen:
            seen.add(task_id)
            unique.append(row)
    return unique


def _mission_unresolved_locked(conn: sqlite3.Connection, task_ids: Sequence[str]) -> list[str]:
    """Consequential actions of these tasks whose effect is still unknown, oldest first."""
    found: list[tuple[float, str]] = []
    for task_id in task_ids:
        for row in conn.execute(
            """
            SELECT action_id, created_at FROM runtime_actions
            WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
            """,
            (task_id,),
        ).fetchall():
            found.append((float(row["created_at"] or 0.0), str(row["action_id"])))
    return [action_id for _, action_id in sorted(found)]


def _mission_halt_scopes_locked(
    conn: sqlite3.Connection,
    task_ids: Sequence[str],
) -> tuple[tuple[str, tuple[tuple[int, str], ...]], ...]:
    """The worker leases a stop fenced, per owned task.

    Only these are fenced again at the process layer.  Fencing every historical
    epoch would flood the bounded origin table and could push out unrelated fences.
    """
    scopes: list[tuple[str, tuple[tuple[int, str], ...]]] = []
    for task_id in task_ids:
        rows = conn.execute(
            """
            SELECT epoch, worker_id FROM runtime_task_epochs
            WHERE task_id = ? AND status = 'fenced'
              AND (
                fence_reason IN ('mission_cancelled', 'task_cancelled')
                OR fence_reason LIKE 'mission_bounded_stop:%'
              )
            ORDER BY epoch ASC
            """,
            (task_id,),
        ).fetchall()
        scopes.append((task_id, tuple((int(row["epoch"]), str(row["worker_id"] or "")) for row in rows)))
    return tuple(scopes)


def _mission_root_stop(
    root_budget: execution_budget.TaskBudgetState | None,
    *,
    now: float,
) -> tuple[str, str] | None:
    """A stop the root budget requires.  A budget that cannot be read is itself a stop."""
    if root_budget is None:
        return "budget_unreadable", "the mission root budget document is unreadable"
    return root_budget.bounded_stop(now=now)


def _mission_task_stops_locked(
    conn: sqlite3.Connection,
    mission_id: str,
    plan_version: int,
    *,
    now: float,
) -> list[tuple[str, str, str]]:
    """Stops required by the budgets of unfinished nodes: ``(task_key, code, reason)``."""
    rows = conn.execute(
        """
        SELECT mt.task_key, rt.budget_json FROM runtime_mission_tasks AS mt
        JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
        WHERE mt.mission_id = ? AND mt.plan_version = ? AND rt.state != 'completed'
        ORDER BY mt.position ASC, mt.task_key ASC
        """,
        (mission_id, plan_version),
    ).fetchall()
    stops: list[tuple[str, str, str]] = []
    for row in rows:
        key = str(row["task_key"])
        try:
            node_budget = execution_budget.TaskBudgetState.from_json(str(row["budget_json"]))
        except execution_budget.BudgetError:
            stops.append((key, "budget_unreadable", f"the budget document of task {key} is unreadable"))
            continue
        found = node_budget.bounded_stop(now=now)
        if found is not None:
            stops.append((key, found[0], found[1]))
    return stops


def _mission_recovery_locked(
    conn: sqlite3.Connection,
    mission_id: str,
    plan_version: int,
    *,
    now: float,
    artifact_status: Callable[[str], Any] | None = None,
    isolation: execution_budget.IsolationEvidence | None = None,
    stop: dict[str, Any] | None = None,
) -> MissionRecoveryReport:
    """Build the truthful mission report from durable state only."""
    join = _mission_join_report_locked(conn, mission_id, plan_version, now=now)
    plan = conn.execute(
        """
        SELECT mp.status, rt.state AS root_state, rt.budget_json AS root_budget_json
        FROM runtime_mission_plans AS mp
        JOIN runtime_tasks AS rt ON rt.task_id = mp.root_task_id
        WHERE mp.mission_id = ? AND mp.plan_version = ?
        """,
        (mission_id, plan_version),
    ).fetchone()
    assert plan is not None
    plan_status = str(plan["status"])
    root_budget: execution_budget.TaskBudgetState | None
    try:
        root_budget = execution_budget.TaskBudgetState.from_json(str(plan["root_budget_json"]))
    except execution_budget.BudgetError:
        root_budget = None
    if stop is None:
        stop = _mission_latest_stop_locked(conn, mission_id, plan_version)

    owned_ids: dict[str, list[str]] = {}
    for owned_row in _mission_owned_locked(conn, mission_id, plan_version):
        owned_ids.setdefault(str(owned_row["node_task_id"]), []).append(str(owned_row["task_id"]))

    tasks: list[MissionRecoveryTask] = []
    unfinished_artifacts: list[str] = []
    unresolved: list[str] = []
    retained_count = 0
    for item in join.tasks:
        retained: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT output_key, artifact_ref, artifact_sha256, producer_epoch
            FROM runtime_mission_outputs
            WHERE mission_id = ? AND plan_version = ? AND task_id = ?
            ORDER BY output_key ASC
            """,
            (mission_id, plan_version, item.task_id),
        ).fetchall():
            entry: dict[str, Any] = {
                "task_key": item.task_key,
                "output_key": str(row["output_key"]),
                "artifact_ref": str(row["artifact_ref"]),
                "artifact_sha256": str(row["artifact_sha256"]),
                "producer_epoch": int(row["producer_epoch"]),
                "artifact_state": "unverified",
                "counts_as_complete": False,
            }
            if artifact_status is not None:
                state, complete, detail = _mission_artifact_verdict(artifact_status, entry["artifact_sha256"])
                entry["artifact_state"] = state
                entry["counts_as_complete"] = complete
                entry["artifact_reason"] = detail
                if not complete:
                    unfinished_artifacts.append(f"{item.task_key}:{entry['output_key']} ({state})")
            retained.append(entry)
        retained_count += len(retained)
        # A node answers for the unresolved effects of its delegated children too.
        action_ids = tuple(_mission_unresolved_locked(conn, owned_ids.get(item.task_id, [item.task_id])))
        unresolved.extend(action_ids)
        tasks.append(
            MissionRecoveryTask(
                task_key=item.task_key,
                task_id=item.task_id,
                state=item.state,
                outcome=item.outcome,
                retained_outputs=tuple(retained),
                missing_outputs=item.missing_outputs,
                unresolved_action_ids=action_ids,
            )
        )

    halted = plan_status in {"cancelled", "failed"}
    work_owed = join.status != "succeeded"
    live = plan_status == "active"
    root_cancelled = str(plan["root_state"]) == "cancelled" or (
        root_budget is not None and root_budget.cancel_requested
    )
    # Budget-required stops and the isolation gate only matter while work is still owed.
    root_stop = _mission_root_stop(root_budget, now=now) if (live and work_owed) else None
    task_stops = (
        _mission_task_stops_locked(conn, mission_id, plan_version, now=now)
        if (live and work_owed and root_stop is None)
        else []
    )
    isolation_refused = ""
    if live and work_owed and root_budget is not None and root_budget.spec.isolation_required:
        isolation_refused = execution_budget.isolation_refusal(isolation)
    incomplete = any(task.outcome == "incomplete" for task in tasks)

    if plan_status == "superseded":
        outcome = "superseded"
    elif not work_owed and not halted:
        if unresolved or unfinished_artifacts:
            outcome = "partial"
        elif (retained_count and artifact_status is None) or root_budget is None:
            outcome = "unverified"
        else:
            outcome = "succeeded"
    elif plan_status == "cancelled" or (live and root_cancelled):
        outcome = "cancelled"
    elif plan_status == "failed" or root_stop is not None:
        outcome = "stopped"
    elif incomplete:
        outcome = "blocked" if isolation_refused else "in_progress"
    else:
        outcome = join.status

    limitations: list[dict[str, str]] = []
    stop_kind = str((stop or {}).get("kind") or "")
    if outcome == "cancelled":
        detail = str((stop or {}).get("reason") or "") if stop_kind == "cancelled" else ""
        limitations.append(
            _mission_limitation(
                "mission_cancelled",
                "The mission was cancelled before it completed" + (f" ({detail})." if detail else "."),
            )
        )
    elif outcome == "stopped":
        if stop_kind == "bounded_stop" and stop is not None:
            where = str(stop.get("scope") or "")
            limitations.append(
                _mission_limitation(
                    str(stop.get("code") or "stop_record_missing"),
                    f"The mission stopped at a declared limit: {stop.get('reason') or 'no reason recorded'}"
                    + (f" [{where}]." if where else "."),
                )
            )
        elif root_stop is not None:
            limitations.append(
                _mission_limitation(root_stop[0], f"The mission stopped at a declared limit: {root_stop[1]} [root].")
            )
        else:
            limitations.append(
                _mission_limitation("stop_record_missing", "The plan is marked failed but carries no stop record.")
            )
    by_code: dict[str, list[tuple[str, str]]] = {}
    for key, code, reason in task_stops:
        by_code.setdefault(code, []).append((key, reason))
    for code, items in by_code.items():
        keys = ", ".join(key for key, _ in items[:20])
        limitations.append(
            _mission_limitation(code, f"{len(items)} task(s) stopped at a declared limit ({keys}): {items[0][1]}.")
        )
    if root_budget is None and not any(item["code"] == "budget_unreadable" for item in limitations):
        limitations.append(
            _mission_limitation("budget_unreadable", "The mission root budget document is unreadable.")
        )
    if isolation_refused:
        limitations.append(
            _mission_limitation(
                "isolation_unavailable",
                f"{isolation_refused}; dependent execution is blocked and the host shell is not used.",
            )
        )
    if unresolved:
        limitations.append(
            _mission_limitation(
                "uncertain_effects",
                f"{len(unresolved)} consequential action(s) may or may not have taken effect: "
                f"{', '.join(unresolved[:20])}.",
            )
        )
    if outcome in {"cancelled", "stopped"}:
        owned = (stop or {}).get("owned_work")
        if not halted:
            # The root task or its budget refuses further work, but nobody has halted
            # this plan: its leases are still live and nothing was terminated.
            limitations.append(
                _mission_limitation(
                    "halt_required",
                    "A stop is required, but this plan has not been halted. Its worker leases are not fenced "
                    "and its owned processes are not terminated.",
                )
            )
        elif not isinstance(owned, dict) or owned.get("termination_attempted") is not True:
            limitations.append(
                _mission_limitation(
                    "owned_work_not_terminated",
                    "Dispatch is fenced durably, but no supervised stop is recorded for this plan, "
                    "so termination of its owned processes is not attested.",
                )
            )
        elif owned.get("cleanly_stopped") is not True:
            limitations.append(
                _mission_limitation(
                    "owned_work_lingering",
                    f"{owned.get('lingering', 0)} owned process(es) were not verified as stopped "
                    f"({owned.get('errors', 0)} containment error(s)).",
                )
            )
    unfinished = [f"{task.task_key} ({task.outcome})" for task in tasks if task.outcome != "completed"]
    if unfinished:
        limitations.append(
            _mission_limitation(
                "tasks_incomplete",
                f"{len(unfinished)} of {len(tasks)} task(s) are not complete: {', '.join(unfinished[:20])}.",
            )
        )
    missing_output_keys = [task.task_key for task in tasks if task.outcome == "missing_required_outputs"]
    if missing_output_keys:
        limitations.append(
            _mission_limitation(
                "missing_required_outputs",
                f"Declared outputs are missing for: {', '.join(missing_output_keys[:20])}.",
            )
        )
    if unfinished_artifacts:
        limitations.append(
            _mission_limitation(
                "artifact_incomplete",
                f"Retained but not complete: {', '.join(unfinished_artifacts[:20])}.",
            )
        )
    elif artifact_status is None and retained_count:
        limitations.append(
            _mission_limitation(
                "artifacts_not_verified",
                f"{retained_count} retained output(s) were not checked against the artifact registry.",
            )
        )
    if plan_status == "superseded":
        # The headline comes first: this report is no longer the one to act on.
        limitations.insert(0, _mission_limitation("plan_superseded", "A newer plan version replaced this one."))

    core = {
        "report_kind": _MISSION_RECOVERY_KIND,
        "mission_id": mission_id,
        "plan_version": plan_version,
        "plan_status": plan_status,
        "outcome": outcome,
        "join_status": join.status,
        "tasks": [task.to_dict() for task in tasks],
        "stop": stop,
        "limitations": limitations,
    }
    report_hash = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return MissionRecoveryReport(
        mission_id=mission_id,
        plan_version=plan_version,
        plan_status=plan_status,
        outcome=outcome,
        # Success needs a clean bill, not merely the absence of a recorded failure.
        claims_success=outcome == "succeeded" and not limitations,
        join_status=join.status,
        tasks=tuple(tasks),
        stop=dict(stop) if stop is not None else None,
        limitations=tuple(limitations),
        evaluated_at=now,
        report_hash=report_hash,
    )


def _mission_persist_recovery_locked(conn: sqlite3.Connection, report: MissionRecoveryReport) -> None:
    """Append the hash-addressed report beside the join snapshots it extends."""
    conn.execute(
        """
        INSERT OR IGNORE INTO runtime_mission_joins (
            mission_id, plan_version, status, report_json, report_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            report.mission_id,
            report.plan_version,
            report.join_status,
            json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            report.report_hash,
            report.evaluated_at,
        ),
    )


#: Upper bound on the unstoppable processes one stop record lists by identity.
_MISSION_LINGERING_LISTED = 256


def _mission_prior_lingering(stop: Mapping[str, Any]) -> tuple[list[tuple[int, str]], int]:
    """Processes an earlier stop could not end: ``(listed identities, count it could not list)``."""
    owned = stop.get("owned_work")
    if not isinstance(owned, dict):
        return [], 0
    listed: list[tuple[int, str]] = []
    raw = owned.get("lingering_pids")
    for item in raw if isinstance(raw, list) else []:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and not isinstance(item[0], bool)
            and isinstance(item[1], str)
        ):
            listed.append((item[0], item[1]))
    count = owned.get("lingering")
    total = count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0
    # Anything counted but not listed by identity can never be re-checked, so it
    # stays counted: absence of evidence is not evidence that a process ended.
    return listed, max(0, total - len(listed))


def _mission_owned_work(
    outcomes: Sequence[process_supervision.TerminationOutcome],
    *,
    origins_fenced: int,
    errors: int,
    started: float,
    fenced: float,
    done: float,
    retry: bool,
    attempted_at: float,
    carried: Sequence[tuple[int, str]] = (),
    carried_unlisted: int = 0,
) -> dict[str, Any]:
    """Summarise measured containment.  Durations are milliseconds from the stop request.

    ``carried`` are processes an earlier attempt could not end and that are still
    running now.  A stop is clean only when nothing lingers and nothing errored; an
    attempt that merely found no registered process does not clear earlier survivors.
    """
    lingering: list[tuple[int, str]] = []
    for pid, fingerprint in (*carried, *(pair for item in outcomes for pair in item.lingering_pids)):
        if (pid, fingerprint) not in lingering:
            lingering.append((int(pid), str(fingerprint)))
    unlisted = max(0, int(carried_unlisted)) + max(0, len(lingering) - _MISSION_LINGERING_LISTED)
    listed = lingering[:_MISSION_LINGERING_LISTED]
    payload: dict[str, Any] = {
        "termination_attempted": True,
        "attempted_at": attempted_at,
        "origins_fenced": origins_fenced,
        "processes": len(outcomes),
        "escalated": sum(1 for item in outcomes if item.escalated_kill),
        "lingering": len(listed) + unlisted,
        "lingering_pids": [[pid, fingerprint] for pid, fingerprint in listed],
        "errors": errors,
        "cleanly_stopped": errors == 0 and not listed and unlisted == 0,
        "dispatch_fenced_ms": round(max(0.0, fenced - started) * 1000, 3),
        "cleanup_latency_ms": round(max(0.0, done - started) * 1000, 3),
        "slowest_process_ms": max((int(item.elapsed_ms) for item in outcomes), default=0),
    }
    if retry:
        # A repeated stop measures from its own request, not from the original one.
        payload["retry"] = True
    return payload


def _mission_halt_unsettled_locked(conn: sqlite3.Connection, root_task_id: str) -> str:
    """Why no new plan may be created for this root yet, or ``""`` when nothing blocks it.

    Every stopped plan of the root must be settled first.  The check is scoped to the
    root task, so naming a new mission does not step around an unsettled stop.
    """
    halted = conn.execute(
        """
        SELECT mission_id, plan_version FROM runtime_mission_plans
        WHERE root_task_id = ? AND status IN ('cancelled', 'failed')
        ORDER BY mission_id ASC, plan_version ASC
        """,
        (root_task_id,),
    ).fetchall()
    for row in halted:
        mission_id, plan_version = str(row["mission_id"]), int(row["plan_version"])
        label = f"stopped mission plan {mission_id!r} v{plan_version}"
        owned_ids = [str(item["task_id"]) for item in _mission_owned_locked(conn, mission_id, plan_version)]
        unresolved = _mission_unresolved_locked(conn, owned_ids)
        if unresolved:
            return (
                f"{len(unresolved)} consequential action(s) of {label} are unresolved "
                f"({', '.join(unresolved[:5])}); reconcile them before planning new work."
            )
        stop = _mission_latest_stop_locked(conn, mission_id, plan_version)
        owned = stop.get("owned_work") if stop is not None else None
        if not isinstance(owned, dict) or owned.get("termination_attempted") is not True:
            return (
                f"termination of the owned work of {label} is not attested; "
                "run the stop with a process supervisor before planning new work."
            )
        if owned.get("cleanly_stopped") is not True:
            return (
                f"owned processes of {label} were not verified as stopped; "
                "run the stop again with a process supervisor before planning new work."
            )
    return ""


def _mission_stop_marker(stop: Mapping[str, Any]) -> str:
    """Short machine-readable reason shared by epoch fences, transitions and containment."""
    if stop.get("kind") == "cancelled":
        return "mission_cancelled"
    return f"mission_bounded_stop:{stop.get('code') or 'unknown'}"[:160]


def _mission_plan_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MissionGraphError("plan_version must be an integer >= 1.")
    return value


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

    def _mission_validate_binding_locked(
        self,
        conn: sqlite3.Connection,
        *,
        mission_id: str,
        plan_version: int,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        expected_task_id: str,
    ) -> sqlite3.Row:
        """Revalidate every captured identity against current durable/server authority."""
        mid = _mission_clean_name(mission_id, "mission_id")
        if not isinstance(binding, MissionExecutionBinding):
            raise MissionBindingRejected("A server-captured MissionExecutionBinding is required.")
        plan = conn.execute(
            """
            SELECT mp.*, rt.session_id AS root_session_id, rt.state AS root_state,
                   rt.budget_json AS root_budget_json
            FROM runtime_mission_plans AS mp
            JOIN runtime_tasks AS rt ON rt.task_id = mp.root_task_id
            WHERE mp.mission_id = ? AND mp.plan_version = ?
            """,
            (mid, int(plan_version)),
        ).fetchone()
        if plan is None:
            raise MissionBindingRejected(f"Mission plan {mid!r} v{plan_version} does not exist.")
        if str(plan["status"]) != "active":
            raise MissionBindingRejected(
                f"Mission plan {mid!r} v{plan_version} is {plan['status']!r}, not active."
            )
        expected_binding = (
            mid,
            int(plan_version),
            str(plan["root_task_id"]),
            str(plan["root_session_id"]),
            str(plan["plan_digest"]),
            str(plan["input_digest"]),
            int(plan["session_policy_revision"]),
            int(plan["global_policy_revision"]),
            str(plan["budget_digest"]),
        )
        presented_binding = (
            binding.mission_id,
            int(binding.plan_version),
            binding.root_task_id,
            binding.session_id,
            binding.plan_digest,
            binding.input_digest,
            int(binding.session_policy_revision),
            int(binding.global_policy_revision),
            binding.budget_digest,
        )
        if presented_binding != expected_binding:
            raise MissionBindingRejected("Captured mission plan/version binding is stale or mismatched.")

        current_input_digest = mission_input_digest(current_inputs)
        if current_input_digest != str(plan["input_digest"]):
            raise MissionBindingRejected("Mission input digest changed after the plan was captured.")
        if not isinstance(current_authority, execution_spine.MissionAuthorityEnvelope):
            raise MissionBindingRejected("Current server mission authority envelope is required.")
        if (
            current_authority.session_id != str(plan["root_session_id"])
            or current_authority.root_task_id != str(plan["root_task_id"])
            or current_authority.task_id != expected_task_id
        ):
            raise MissionBindingRejected("Current mission session/root/task authority identity does not match.")
        if current_authority.session_policy_revision != int(plan["session_policy_revision"]):
            raise MissionBindingRejected("Mission session policy/grant generation is stale.")
        if current_authority.global_policy_revision != int(plan["global_policy_revision"]):
            raise MissionBindingRejected("Mission global policy generation is stale.")
        try:
            current_budget_digest = runtime_persistence.compute_task_budget_identity(
                str(plan["root_budget_json"])
            )
        except ValueError as exc:
            raise MissionBindingRejected(f"Mission budget identity is unreadable: {exc}") from exc
        if current_budget_digest != str(plan["budget_digest"]):
            raise MissionBindingRejected("Mission budget identity changed after the plan was captured.")
        member = conn.execute(
            """
            SELECT 1 FROM runtime_mission_tasks
            WHERE mission_id = ? AND plan_version = ? AND task_id = ?
            """,
            (mid, int(plan_version), expected_task_id),
        ).fetchone()
        if member is None:
            raise MissionBindingRejected("Current task is not a member of the active mission plan version.")
        return plan

    @staticmethod
    def _mission_readiness_locked(
        conn: sqlite3.Connection,
        *,
        mission_id: str,
        plan_version: int,
        task_row: sqlite3.Row,
    ) -> MissionTaskReadiness:
        blocked: list[str] = []
        failed: list[str] = []
        missing: list[str] = []
        deps = conn.execute(
            """
            SELECT dep.task_id, dep.depends_on_task_id, dep.required_outputs_json,
                   prereq.task_key AS prerequisite_key, rt.state AS prerequisite_state
            FROM runtime_mission_dependencies AS dep
            JOIN runtime_mission_tasks AS prereq
              ON prereq.mission_id = dep.mission_id
             AND prereq.plan_version = dep.plan_version
             AND prereq.task_id = dep.depends_on_task_id
            JOIN runtime_tasks AS rt ON rt.task_id = dep.depends_on_task_id
            WHERE dep.mission_id = ? AND dep.plan_version = ? AND dep.task_id = ?
            ORDER BY prereq.position ASC, prereq.task_key ASC
            """,
            (mission_id, plan_version, str(task_row["task_id"])),
        ).fetchall()
        for dep in deps:
            dep_key = str(dep["prerequisite_key"])
            dep_state = str(dep["prerequisite_state"])
            if dep_state != "completed":
                blocked.append(dep_key)
                if dep_state in {"failed", "cancelled"}:
                    failed.append(dep_key)
                continue
            try:
                required = json.loads(str(dep["required_outputs_json"]))
            except (TypeError, ValueError) as exc:
                raise MissionGraphError("persisted mission dependency output contract is malformed.") from exc
            if not isinstance(required, list):
                raise MissionGraphError("persisted mission dependency output contract must be a list.")
            present_rows = conn.execute(
                """
                SELECT output_key FROM runtime_mission_outputs
                WHERE mission_id = ? AND plan_version = ? AND task_id = ?
                """,
                (mission_id, plan_version, str(dep["depends_on_task_id"])),
            ).fetchall()
            present = {str(row["output_key"]) for row in present_rows}
            for output_key in required:
                if str(output_key) not in present:
                    blocked.append(dep_key)
                    missing.append(f"{dep_key}:{output_key}")
        task = runtime_persistence._row_to_task(task_row)
        ready_state = task.state in {"pending", "blocked", "failed"}
        return MissionTaskReadiness(
            mission_id=mission_id,
            plan_version=plan_version,
            task_key=str(task_row["task_key"]),
            task=task,
            ready=ready_state and not blocked and not failed and not missing,
            blocked_by_task_keys=tuple(dict.fromkeys(blocked)),
            failed_dependency_keys=tuple(dict.fromkeys(failed)),
            missing_required_outputs=tuple(dict.fromkeys(missing)),
        )

    def create_mission_plan(
        self,
        root_task_id: str,
        mission_id: str,
        plan_version: int,
        tasks: Sequence[MissionTaskSpec],
        dependencies: Sequence[MissionDependencySpec] = (),
        *,
        inputs: Mapping[str, Any],
        authority: execution_spine.MissionAuthorityEnvelope,
        now: float | None = None,
    ) -> MissionPlanRecord:
        """Persist one immutable DAG version and its runtime-task decomposition atomically."""
        root_id = _validate_identifier(root_task_id, "root_task_id")
        mid = _mission_clean_name(mission_id, "mission_id")
        normalized_tasks, normalized_deps, plan_digest = _mission_normalize_graph(
            root_id,
            mid,
            plan_version,
            tasks,
            dependencies,
        )
        input_digest = mission_input_digest(inputs)
        if not isinstance(authority, execution_spine.MissionAuthorityEnvelope):
            raise MissionBindingRejected("A server-created mission authority envelope is required.")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> MissionPlanRecord:
            conn.row_factory = sqlite3.Row
            root = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (root_id,)).fetchone()
            if root is None:
                raise MissionGraphError(f"Root task {root_id!r} does not exist.")
            if root["parent_task_id"] is not None:
                raise MissionGraphError(f"Mission root {root_id!r} must be a root runtime task.")
            if str(root["state"]) in {"completed", "cancelled"}:
                raise MissionGraphError(f"Terminal root task {root_id!r} cannot receive a mission plan.")
            if (
                authority.session_id != str(root["session_id"])
                or authority.root_task_id != root_id
                or authority.task_id != root_id
            ):
                raise MissionBindingRejected("Mission-plan authority does not match the root task/session.")
            if min(authority.session_policy_revision, authority.global_policy_revision) < 0:
                raise MissionBindingRejected("Mission policy generations must be non-negative.")
            try:
                budget_digest = runtime_persistence.compute_task_budget_identity(str(root["budget_json"]))
                root_budget = execution_budget.TaskBudgetState.from_json(str(root["budget_json"]))
            except (ValueError, execution_budget.BudgetError) as exc:
                raise MissionGraphError(f"Root task budget is unreadable: {exc}") from exc

            existing = conn.execute(
                "SELECT * FROM runtime_mission_plans WHERE mission_id = ? AND plan_version = ?",
                (mid, plan_version),
            ).fetchone()
            if existing is not None:
                same = (
                    str(existing["root_task_id"]) == root_id
                    and str(existing["plan_digest"]) == plan_digest
                    and str(existing["input_digest"]) == input_digest
                    and int(existing["session_policy_revision"]) == authority.session_policy_revision
                    and int(existing["global_policy_revision"]) == authority.global_policy_revision
                    and str(existing["budget_digest"]) == budget_digest
                )
                if not same:
                    raise MissionGraphError(
                        f"Mission plan {mid!r} v{plan_version} already exists with different immutable content."
                    )
                return _mission_plan_from_row(existing)
            max_row = conn.execute(
                "SELECT COALESCE(MAX(plan_version), 0) FROM runtime_mission_plans WHERE mission_id = ?",
                (mid,),
            ).fetchone()
            expected_version = int(max_row[0]) + 1 if max_row else 1
            if plan_version != expected_version:
                raise MissionGraphError(
                    f"Mission plan versions must be contiguous; expected v{expected_version}, got v{plan_version}."
                )

            # AP-007: a stopped plan is settled before new work is planned on the same
            # root.  Otherwise a new version could repeat an effect whose outcome is
            # still unknown, or start beside processes that were never verified as ended.
            unsettled = _mission_halt_unsettled_locked(conn, root_id)
            if unsettled:
                raise MissionHaltUnsettled(f"Root task {root_id!r} cannot receive a new mission plan: {unsettled}")

            previous = conn.execute(
                "SELECT plan_version FROM runtime_mission_plans WHERE mission_id = ? AND status = 'active'",
                (mid,),
            ).fetchall()
            for previous_row in previous:
                previous_version = int(previous_row["plan_version"])
                old_nodes = conn.execute(
                    """
                    SELECT task_id FROM runtime_mission_tasks
                    WHERE mission_id = ? AND plan_version = ?
                    """,
                    (mid, previous_version),
                ).fetchall()
                for old_node in old_nodes:
                    conn.execute(
                        """
                        UPDATE runtime_task_epochs
                        SET status = 'fenced', fenced_at = ?, fence_reason = 'mission_plan_superseded'
                        WHERE task_id = ? AND status = 'active'
                        """,
                        (ts, str(old_node["task_id"])),
                    )
                conn.execute(
                    """
                    UPDATE runtime_mission_plans
                    SET status = 'superseded', updated_at = ?
                    WHERE mission_id = ? AND plan_version = ? AND status = 'active'
                    """,
                    (ts, mid, previous_version),
                )

            conn.execute(
                """
                INSERT INTO runtime_mission_plans (
                    mission_id, plan_version, root_task_id, plan_digest, input_digest,
                    session_policy_revision, global_policy_revision, budget_digest,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    mid,
                    plan_version,
                    root_id,
                    plan_digest,
                    input_digest,
                    authority.session_policy_revision,
                    authority.global_policy_revision,
                    budget_digest,
                    ts,
                    ts,
                ),
            )
            child_budget = execution_budget.TaskBudgetState(spec=root_budget.spec).to_json()
            task_ids: dict[str, str] = {}
            for position, task_spec in enumerate(normalized_tasks):
                task_id = mission_task_id(root_id, plan_version, task_spec.task_key)
                task_ids[task_spec.task_key] = task_id
                conn.execute(
                    """
                    INSERT INTO runtime_tasks (
                        task_id, parent_task_id, session_id, title, state,
                        current_epoch, state_version, workspace_dir, policy,
                        budget_json, checkpoint_ref, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, 0, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        task_id,
                        root_id,
                        str(root["session_id"]),
                        task_spec.title,
                        str(root["workspace_dir"]),
                        str(root["policy"]),
                        child_budget,
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO runtime_mission_tasks (
                        mission_id, plan_version, task_id, task_key, position,
                        required_outputs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid,
                        plan_version,
                        task_id,
                        task_spec.task_key,
                        position,
                        json.dumps(list(task_spec.required_outputs), separators=(",", ":")),
                        ts,
                    ),
                )
            for dependency in normalized_deps:
                conn.execute(
                    """
                    INSERT INTO runtime_mission_dependencies (
                        mission_id, plan_version, task_id, depends_on_task_id,
                        required_outputs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid,
                        plan_version,
                        task_ids[dependency.task_key],
                        task_ids[dependency.depends_on_task_key],
                        json.dumps(list(dependency.required_outputs), separators=(",", ":")),
                        ts,
                    ),
                )
            created = conn.execute(
                "SELECT * FROM runtime_mission_plans WHERE mission_id = ? AND plan_version = ?",
                (mid, plan_version),
            ).fetchone()
            assert created is not None
            return _mission_plan_from_row(created)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def get_mission_plan(self, mission_id: str, plan_version: int) -> MissionPlanRecord | None:
        mid = _mission_clean_name(mission_id, "mission_id")

        def _read(conn: sqlite3.Connection) -> MissionPlanRecord | None:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runtime_mission_plans WHERE mission_id = ? AND plan_version = ?",
                (mid, int(plan_version)),
            ).fetchone()
            return _mission_plan_from_row(row) if row is not None else None

        return runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read,
            retry_policy=self._retry_policy,
        )

    def capture_mission_binding(self, mission_id: str, plan_version: int) -> MissionExecutionBinding:
        """Capture the persisted immutable identity to carry across queue/wait/restart boundaries."""
        mid = _mission_clean_name(mission_id, "mission_id")

        def _read(conn: sqlite3.Connection) -> MissionExecutionBinding:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT mp.*, rt.session_id AS root_session_id
                FROM runtime_mission_plans AS mp
                JOIN runtime_tasks AS rt ON rt.task_id = mp.root_task_id
                WHERE mp.mission_id = ? AND mp.plan_version = ?
                """,
                (mid, int(plan_version)),
            ).fetchone()
            if row is None:
                raise MissionGraphError(f"Mission plan {mid!r} v{plan_version} does not exist.")
            return MissionExecutionBinding(
                mission_id=mid,
                plan_version=int(row["plan_version"]),
                root_task_id=str(row["root_task_id"]),
                session_id=str(row["root_session_id"]),
                plan_digest=str(row["plan_digest"]),
                input_digest=str(row["input_digest"]),
                session_policy_revision=int(row["session_policy_revision"]),
                global_policy_revision=int(row["global_policy_revision"]),
                budget_digest=str(row["budget_digest"]),
            )

        return runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read,
            retry_policy=self._retry_policy,
        )

    def list_mission_tasks(self, mission_id: str, plan_version: int) -> tuple[runtime_persistence.TaskRecord, ...]:
        mid = _mission_clean_name(mission_id, "mission_id")

        def _read(conn: sqlite3.Connection) -> tuple[runtime_persistence.TaskRecord, ...]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT rt.* FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ?
                ORDER BY mt.position ASC, mt.task_key ASC
                """,
                (mid, int(plan_version)),
            ).fetchall()
            return tuple(runtime_persistence._row_to_task(row) for row in rows)

        return runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read,
            retry_policy=self._retry_policy,
        )

    def list_mission_readiness(self, mission_id: str, plan_version: int) -> tuple[MissionTaskReadiness, ...]:
        """Return deterministic dependency-aware scheduling state without dispatching work."""
        mid = _mission_clean_name(mission_id, "mission_id")

        def _read(conn: sqlite3.Connection) -> tuple[MissionTaskReadiness, ...]:
            conn.row_factory = sqlite3.Row
            plan = conn.execute(
                "SELECT status FROM runtime_mission_plans WHERE mission_id = ? AND plan_version = ?",
                (mid, int(plan_version)),
            ).fetchone()
            if plan is None:
                raise MissionGraphError(f"Mission plan {mid!r} v{plan_version} does not exist.")
            rows = conn.execute(
                """
                SELECT rt.*, mt.task_key FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ?
                ORDER BY mt.position ASC, mt.task_key ASC
                """,
                (mid, int(plan_version)),
            ).fetchall()
            readiness = tuple(
                self._mission_readiness_locked(
                    conn,
                    mission_id=mid,
                    plan_version=int(plan_version),
                    task_row=row,
                )
                for row in rows
            )
            if str(plan["status"]) != "active":
                # AP-007: a cancelled, stopped or superseded plan dispatches nothing,
                # so its scheduling view must not advertise claimable work.
                return tuple(replace(item, ready=False) for item in readiness)
            return readiness

        return runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read,
            retry_policy=self._retry_policy,
        )

    def claim_ready_mission_task(
        self,
        mission_id: str,
        plan_version: int,
        task_key: str,
        *,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        worker_id: str,
        expected_state_version: int | None = None,
        lease_ttl_s: float = 30.0,
        now: float | None = None,
        isolation: execution_budget.IsolationEvidence | None = None,
    ) -> MissionTaskLease:
        """Atomically revalidate bindings/dependencies, then mint a fenced worker epoch.

        AP-007: a root budget that declares ``isolation_required`` refuses the claim
        unless ``isolation`` proves a qualified sandbox.  No epoch is minted, so the
        node never reaches ``running`` and no action can be admitted for it.
        """
        mid = _mission_clean_name(mission_id, "mission_id")
        key = _mission_clean_name(task_key, "task_key")
        wid = _validate_identifier(worker_id, "worker_id")
        if not isinstance(lease_ttl_s, (int, float)) or isinstance(lease_ttl_s, bool) or not 0 < float(lease_ttl_s) <= 86_400:
            raise MissionGraphError("lease_ttl_s must be in (0, 86400].")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> MissionTaskLease:
            conn.row_factory = sqlite3.Row
            node = conn.execute(
                """
                SELECT rt.*, mt.task_key FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ? AND mt.task_key = ?
                """,
                (mid, int(plan_version), key),
            ).fetchone()
            if node is None:
                raise MissionGraphError(f"Mission task {key!r} does not exist in {mid!r} v{plan_version}.")
            plan = self._mission_validate_binding_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                binding=binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                expected_task_id=str(node["task_id"]),
            )
            if str(plan["root_state"]) in {"completed", "failed", "cancelled"}:
                raise MissionDependencyRejected(
                    f"Mission root is {plan['root_state']!r}; no new node can be scheduled."
                )
            try:
                root_budget = execution_budget.TaskBudgetState.from_json(str(plan["root_budget_json"]))
            except execution_budget.BudgetError as exc:
                raise MissionBindingRejected(f"Mission root budget is unreadable: {exc}") from exc
            violation = root_budget.violation_reason(check_recovery=False, now=ts)
            if root_budget.cancel_requested or violation:
                raise MissionBindingRejected(violation or "Mission root cancellation is active.")
            _mission_require_isolation(root_budget, isolation)
            if expected_state_version is not None and int(node["state_version"]) != expected_state_version:
                raise MissionBindingRejected("Mission task state_version changed before scheduling.")
            readiness = self._mission_readiness_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                task_row=node,
            )
            if not readiness.ready:
                details = [
                    *(f"blocked:{item}" for item in readiness.blocked_by_task_keys),
                    *(f"failed:{item}" for item in readiness.failed_dependency_keys),
                    *(f"missing:{item}" for item in readiness.missing_required_outputs),
                ]
                raise MissionDependencyRejected(
                    f"Mission task {key!r} is not dependency-ready ({', '.join(details) or readiness.task.state})."
                )
            active_rows = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND status = 'active'",
                (str(node["task_id"]),),
            ).fetchall()
            for active in active_rows:
                if float(active["lease_expires_at"]) > ts:
                    raise runtime_persistence.LeaseOwnershipConflictError(
                        f"Mission task {key!r} already has active worker {active['worker_id']!r}."
                    )
                conn.execute(
                    """
                    UPDATE runtime_task_epochs
                    SET status = 'expired', fenced_at = ?, fence_reason = 'mission_lease_expired'
                    WHERE task_id = ? AND epoch = ? AND status = 'active'
                    """,
                    (ts, str(node["task_id"]), int(active["epoch"])),
                )
            next_epoch = int(node["current_epoch"]) + 1
            next_state_version = int(node["state_version"]) + 1
            conn.execute(
                """
                INSERT INTO runtime_task_epochs (
                    task_id, epoch, worker_id, status, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (str(node["task_id"]), next_epoch, wid, ts, ts, ts + float(lease_ttl_s)),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'running', current_epoch = ?, state_version = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (next_epoch, next_state_version, ts, str(node["task_id"])),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (str(node["task_id"]),),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'running', ?, 'mission_dependency_ready', ?, ?)
                """,
                (
                    str(node["task_id"]),
                    int(seq_row[0]) if seq_row else 1,
                    str(node["state"]),
                    next_epoch,
                    json.dumps(
                        {"mission_id": mid, "plan_version": int(plan_version), "task_key": key},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    ts,
                ),
            )
            task_after = conn.execute(
                "SELECT * FROM runtime_tasks WHERE task_id = ?",
                (str(node["task_id"]),),
            ).fetchone()
            epoch_after = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (str(node["task_id"]), next_epoch),
            ).fetchone()
            assert task_after is not None and epoch_after is not None
            return MissionTaskLease(
                runtime_persistence._row_to_task(task_after),
                runtime_persistence._row_to_epoch(epoch_after),
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def checkpoint_mission_task(
        self,
        mission_id: str,
        plan_version: int,
        task_key: str,
        *,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        worker_id: str,
        epoch: int,
        expected_state_version: int,
        step: int,
        payload: dict[str, Any],
        evidence_refs: Sequence[workflow_checkpoints.EvidenceRef | dict[str, Any]] = (),
        interrupt: bool = False,
        now: float | None = None,
    ) -> workflow_checkpoints.TaskCheckpointRecord:
        """Commit a hash-verified checkpoint bound to current plan/input/authority/budget."""
        mid = _mission_clean_name(mission_id, "mission_id")
        key = _mission_clean_name(task_key, "task_key")
        wid = _validate_identifier(worker_id, "worker_id")
        ts = time.time() if now is None else float(now)

        def _precheck(conn: sqlite3.Connection) -> str:
            conn.row_factory = sqlite3.Row
            node = conn.execute(
                """
                SELECT rt.* FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ? AND mt.task_key = ?
                """,
                (mid, int(plan_version), key),
            ).fetchone()
            if node is None:
                raise MissionGraphError(f"Mission task {key!r} does not exist.")
            task_id = str(node["task_id"])
            self._mission_validate_binding_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                binding=binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                expected_task_id=task_id,
            )
            if str(node["state"]) != "running":
                raise MissionBindingRejected(f"Mission task {key!r} is not running.")
            if int(node["state_version"]) != int(expected_state_version):
                raise MissionBindingRejected("Mission task state_version changed before checkpointing.")
            if int(node["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot checkpoint mission task {key!r}."
                )
            lease = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (task_id, int(epoch)),
            ).fetchone()
            if lease is None or str(lease["status"]) != "active" or str(lease["worker_id"]) != wid:
                raise runtime_persistence.StaleEpochError(
                    f"Mission task {key!r} epoch {epoch} is not actively owned by {wid!r}."
                )
            if float(lease["lease_expires_at"]) <= ts:
                raise runtime_persistence.LeaseExpiredError(
                    f"Mission task {key!r} epoch {epoch} expired before checkpointing."
                )
            return task_id

        task_id = runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _precheck,
            retry_policy=self._retry_policy,
        )
        checkpoint = self.checkpoints.save_checkpoint(
            task_id,
            step=step,
            payload=payload,
            evidence_refs=evidence_refs,
            epoch=int(epoch),
            status="interrupted" if interrupt else "committed",
            require_active_epoch=True,
            mission_binding=binding.checkpoint_binding(),
            now=ts,
        )
        if not interrupt:
            return checkpoint

        def _pause(conn: sqlite3.Connection) -> None:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise MissionGraphError(f"Mission task {key!r} disappeared after checkpoint commit.")
            if str(row["checkpoint_ref"]) != checkpoint.checkpoint_id:
                raise MissionBindingRejected("A successor checkpoint replaced the interrupt checkpoint.")
            if int(row["state_version"]) != int(expected_state_version):
                raise MissionBindingRejected("Mission task state_version changed before interrupt committed.")
            if str(row["state"]) != "running" or int(row["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError("Mission worker lost ownership before interrupt committed.")
            lease = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (task_id, int(epoch)),
            ).fetchone()
            if lease is None or str(lease["status"]) != "active" or str(lease["worker_id"]) != wid:
                raise runtime_persistence.StaleEpochError("Mission worker lease is no longer active at interrupt.")
            conn.execute(
                """
                UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ?
                WHERE task_id = ? AND epoch = ? AND status = 'active'
                """,
                (ts, task_id, int(epoch)),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'paused', state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (ts, task_id),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, 'running', 'paused', ?, 'mission_checkpoint_interrupted', ?, ?)
                """,
                (
                    task_id,
                    int(seq_row[0]) if seq_row else 1,
                    int(epoch),
                    _encode_evidence_json(checkpoint_id=checkpoint.checkpoint_id),
                    ts,
                ),
            )

        runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _pause,
            retry_policy=self._retry_policy,
        )
        return checkpoint

    def complete_mission_task(
        self,
        mission_id: str,
        plan_version: int,
        task_key: str,
        *,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        worker_id: str,
        epoch: int,
        expected_state_version: int,
        outputs: Sequence[MissionOutputRef],
        reason: str = "mission_task_completed",
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Atomically persist required outputs and complete only the owning live epoch."""
        mid = _mission_clean_name(mission_id, "mission_id")
        key = _mission_clean_name(task_key, "task_key")
        wid = _validate_identifier(worker_id, "worker_id")
        normalized_outputs: dict[str, MissionOutputRef] = {}
        for raw in outputs:
            if not isinstance(raw, MissionOutputRef):
                raise MissionGraphError("outputs must contain MissionOutputRef values.")
            output_key = _mission_clean_name(raw.output_key, "output_key")
            if output_key in normalized_outputs:
                raise MissionGraphError(f"duplicate mission output key {output_key!r}.")
            artifact_ref = str(raw.artifact_ref or "").strip()
            if not artifact_ref or "\x00" in artifact_ref or len(artifact_ref) > 512:
                raise MissionGraphError("artifact_ref must be a non-empty NUL-free string of at most 512 characters.")
            artifact_sha = _mission_sha256(raw.artifact_sha256, "artifact_sha256")
            try:
                json.dumps(raw.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise MissionGraphError("mission output evidence must be JSON serializable.") from exc
            normalized_outputs[output_key] = MissionOutputRef(
                output_key,
                artifact_ref,
                artifact_sha,
                dict(raw.evidence),
            )
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            node = conn.execute(
                """
                SELECT rt.*, mt.task_key, mt.required_outputs_json
                FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ? AND mt.task_key = ?
                """,
                (mid, int(plan_version), key),
            ).fetchone()
            if node is None:
                raise MissionGraphError(f"Mission task {key!r} does not exist.")
            task_id = str(node["task_id"])
            plan = self._mission_validate_binding_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                binding=binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                expected_task_id=task_id,
            )
            if str(node["state"]) != "running":
                raise MissionDependencyRejected(
                    f"Mission task {key!r} in state {node['state']!r} cannot complete."
                )
            if int(node["state_version"]) != int(expected_state_version):
                raise MissionBindingRejected("Mission task state_version changed before completion.")
            if int(node["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError(
                    f"Stale epoch {epoch} cannot complete mission task {key!r}."
                )
            lease = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (task_id, int(epoch)),
            ).fetchone()
            if lease is None or str(lease["status"]) != "active" or str(lease["worker_id"]) != wid:
                raise runtime_persistence.StaleEpochError(
                    f"Mission task {key!r} epoch {epoch} is not actively owned by {wid!r}."
                )
            if float(lease["lease_expires_at"]) <= ts:
                raise runtime_persistence.LeaseExpiredError(
                    f"Mission task {key!r} epoch {epoch} expired before completion."
                )
            readiness = self._mission_readiness_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                task_row=node,
            )
            if readiness.blocked_by_task_keys or readiness.failed_dependency_keys or readiness.missing_required_outputs:
                raise MissionDependencyRejected(
                    f"Mission task {key!r} dependencies changed before completion committed."
                )
            try:
                required_raw = json.loads(str(node["required_outputs_json"]))
            except (TypeError, ValueError) as exc:
                raise MissionGraphError("persisted mission output contract is malformed.") from exc
            if not isinstance(required_raw, list):
                raise MissionGraphError("persisted mission output contract must be a list.")
            required = {str(item) for item in required_raw}
            presented = set(normalized_outputs)
            missing = sorted(required - presented)
            unexpected = sorted(presented - required)
            if missing or unexpected:
                parts = []
                if missing:
                    parts.append(f"missing required output(s): {', '.join(missing)}")
                if unexpected:
                    parts.append(f"undeclared output(s): {', '.join(unexpected)}")
                raise FalseCompletionRejected(
                    f"Mission task {key!r} cannot complete; {'; '.join(parts)}."
                )
            unresolved = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                """,
                (task_id,),
            ).fetchall()
            if unresolved:
                raise FalseCompletionRejected(
                    f"Mission task {key!r} has unresolved consequential action(s)."
                )
            existing_outputs = conn.execute(
                """
                SELECT output_key FROM runtime_mission_outputs
                WHERE mission_id = ? AND plan_version = ? AND task_id = ?
                """,
                (mid, int(plan_version), task_id),
            ).fetchall()
            if existing_outputs:
                raise FalseCompletionRejected(
                    f"Mission task {key!r} already has durable outputs; refusing overwrite."
                )
            for output in normalized_outputs.values():
                conn.execute(
                    """
                    INSERT INTO runtime_mission_outputs (
                        mission_id, plan_version, task_id, output_key, artifact_ref,
                        artifact_sha256, evidence_json, producer_epoch, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mid,
                        int(plan_version),
                        task_id,
                        output.output_key,
                        output.artifact_ref,
                        output.artifact_sha256,
                        json.dumps(output.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                        int(epoch),
                        ts,
                    ),
                )
            conn.execute(
                """
                UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ?
                WHERE task_id = ? AND epoch = ? AND status = 'active'
                """,
                (ts, task_id, int(epoch)),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'completed', state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (ts, task_id),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, 'running', 'completed', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    int(seq_row[0]) if seq_row else 1,
                    int(epoch),
                    str(reason)[:240],
                    json.dumps(
                        {
                            "mission_id": mid,
                            "plan_version": int(plan_version),
                            "outputs": [
                                {
                                    "output_key": item.output_key,
                                    "artifact_ref": item.artifact_ref,
                                    "artifact_sha256": item.artifact_sha256,
                                }
                                for item in normalized_outputs.values()
                            ],
                            "plan_digest": str(plan["plan_digest"]),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    ts,
                ),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def fail_mission_task(
        self,
        mission_id: str,
        plan_version: int,
        task_key: str,
        *,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        worker_id: str,
        epoch: int,
        expected_state_version: int,
        reason: str,
        now: float | None = None,
    ) -> runtime_persistence.TaskRecord:
        """Record an explicit terminal node failure under the same worker/version fences."""
        mid = _mission_clean_name(mission_id, "mission_id")
        key = _mission_clean_name(task_key, "task_key")
        wid = _validate_identifier(worker_id, "worker_id")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> runtime_persistence.TaskRecord:
            conn.row_factory = sqlite3.Row
            node = conn.execute(
                """
                SELECT rt.* FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ? AND mt.task_key = ?
                """,
                (mid, int(plan_version), key),
            ).fetchone()
            if node is None:
                raise MissionGraphError(f"Mission task {key!r} does not exist.")
            task_id = str(node["task_id"])
            self._mission_validate_binding_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                binding=binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                expected_task_id=task_id,
            )
            if str(node["state"]) != "running" or int(node["state_version"]) != int(expected_state_version):
                raise MissionBindingRejected("Mission task state/version changed before failure commit.")
            if int(node["current_epoch"]) != int(epoch):
                raise runtime_persistence.StaleEpochError("Stale mission worker cannot fail its successor.")
            lease = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (task_id, int(epoch)),
            ).fetchone()
            if lease is None or str(lease["status"]) != "active" or str(lease["worker_id"]) != wid:
                raise runtime_persistence.StaleEpochError("Mission failure worker lease is not active/owned.")
            conn.execute(
                "UPDATE runtime_task_epochs SET status = 'released', heartbeat_at = ? WHERE task_id = ? AND epoch = ?",
                (ts, task_id, int(epoch)),
            )
            ckpt_ref = str(node["checkpoint_ref"] or "").strip()
            if ckpt_ref:
                conn.execute(
                    "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?",
                    (ts, ckpt_ref),
                )
            conn.execute(
                "UPDATE runtime_tasks SET state = 'failed', state_version = state_version + 1, updated_at = ? WHERE task_id = ?",
                (ts, task_id),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, 'running', 'failed', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    int(seq_row[0]) if seq_row else 1,
                    int(epoch),
                    str(reason)[:240],
                    json.dumps({"mission_id": mid, "plan_version": int(plan_version)}, separators=(",", ":")),
                    ts,
                ),
            )
            updated = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)).fetchone()
            assert updated is not None
            return runtime_persistence._row_to_task(updated)

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def evaluate_mission_join(
        self,
        mission_id: str,
        plan_version: int,
        *,
        persist: bool = True,
        now: float | None = None,
    ) -> MissionJoinReport:
        """Evaluate and optionally persist explicit success/partial/failure/output state."""
        mid = _mission_clean_name(mission_id, "mission_id")
        ts = time.time() if now is None else float(now)

        def _evaluate(conn: sqlite3.Connection) -> MissionJoinReport:
            conn.row_factory = sqlite3.Row
            report = _mission_join_report_locked(conn, mid, int(plan_version), now=ts)
            if persist:
                _mission_persist_join_locked(conn, report)
            return report

        runner = runtime_persistence.run_write_transaction if persist else runtime_persistence.run_read
        return runner(
            self._storage,
            self._db_path,
            _evaluate,
            retry_policy=self._retry_policy,
        )

    def mission_recovery_report(
        self,
        mission_id: str,
        plan_version: int,
        *,
        artifact_status: Callable[[str], Any] | None = None,
        isolation: execution_budget.IsolationEvidence | None = None,
        persist: bool = True,
        now: float | None = None,
    ) -> MissionRecoveryReport:
        """Report what a mission reached, what it retained and what limits it (AP-007).

        ``artifact_status`` is the AP-006 lookup (``ArtifactRegistry.artifact_status``).
        Without it every retained output is listed as ``unverified`` and success is
        not claimed.  ``isolation`` is server-observed evidence for the isolation gate.
        The lookup runs under a read snapshot; only the finished report is written.
        """
        mid = _mission_clean_name(mission_id, "mission_id")
        version = _mission_plan_version(plan_version)
        ts = time.time() if now is None else float(now)
        return self._mission_report(
            mid,
            version,
            ts=ts,
            artifact_status=artifact_status,
            isolation=isolation,
            stop=None,
            persist=persist,
        )

    def _mission_report(
        self,
        mission_id: str,
        plan_version: int,
        *,
        ts: float,
        artifact_status: Callable[[str], Any] | None,
        isolation: execution_budget.IsolationEvidence | None,
        stop: dict[str, Any] | None,
        persist: bool,
    ) -> MissionRecoveryReport:
        def _evaluate(conn: sqlite3.Connection) -> MissionRecoveryReport:
            conn.row_factory = sqlite3.Row
            return _mission_recovery_locked(
                conn,
                mission_id,
                plan_version,
                now=ts,
                artifact_status=artifact_status,
                isolation=isolation,
                stop=stop,
            )

        report = runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _evaluate,
            retry_policy=self._retry_policy,
        )
        if persist:
            runtime_persistence.run_write_transaction(
                self._storage,
                self._db_path,
                lambda conn: _mission_persist_recovery_locked(conn, report),
                retry_policy=self._retry_policy,
            )
        return report

    def _mission_halt_begin(
        self,
        mission_id: str,
        plan_version: int,
        *,
        kind: str,
        authority: execution_spine.MissionAuthorityEnvelope,
        note: str,
        ts: float,
    ) -> _MissionHaltTicket:
        """Durably stop dispatch for one plan version in a single transaction."""

        def _tx(conn: sqlite3.Connection) -> _MissionHaltTicket:
            conn.row_factory = sqlite3.Row
            plan = conn.execute(
                """
                SELECT mp.status, mp.root_task_id, rt.session_id AS root_session_id,
                       rt.budget_json AS root_budget_json
                FROM runtime_mission_plans AS mp
                JOIN runtime_tasks AS rt ON rt.task_id = mp.root_task_id
                WHERE mp.mission_id = ? AND mp.plan_version = ?
                """,
                (mission_id, plan_version),
            ).fetchone()
            if plan is None:
                raise MissionGraphError(f"Mission plan {mission_id!r} v{plan_version} does not exist.")
            root_id = str(plan["root_task_id"])
            session_id = str(plan["root_session_id"] or "")
            # Identity only.  Policy and budget generations are deliberately not
            # compared: stale authority may always stop work, it may never start it.
            if (
                authority.session_id != session_id
                or authority.root_task_id != root_id
                or authority.task_id != root_id
            ):
                raise MissionBindingRejected("Mission stop authority does not match the root task/session.")
            status = str(plan["status"])
            if status not in {"active", "cancelled", "failed"}:
                raise WorkflowTransitionRejected(
                    f"Mission plan {mission_id!r} v{plan_version} is {status!r} and cannot be halted."
                )
            owned = _mission_owned_locked(conn, mission_id, plan_version)
            owned_task_ids = [str(row["task_id"]) for row in owned]

            if status != "active":
                # Already halted: the first stop stands.  Nothing durable changes; the
                # caller may still retry containment that was never attested as clean.
                prior = _mission_latest_stop_locked(conn, mission_id, plan_version) or {
                    "kind": "cancelled" if status == "cancelled" else "bounded_stop",
                    "code": "mission_cancelled" if status == "cancelled" else "stop_record_missing",
                    "reason": "",
                    "requested_at": ts,
                }
                prior_owned = prior.get("owned_work")
                clean = isinstance(prior_owned, dict) and prior_owned.get("cleanly_stopped") is True
                return _MissionHaltTicket(
                    mission_id=mission_id,
                    plan_version=plan_version,
                    session_id=session_id,
                    stop=prior,
                    scopes=_mission_halt_scopes_locked(conn, owned_task_ids),
                    newly_halted=False,
                    needs_containment=not clean,
                )

            try:
                already_succeeded = (
                    _mission_join_report_locked(conn, mission_id, plan_version, now=ts).status == "succeeded"
                )
            except MissionGraphError:
                already_succeeded = False  # an unreadable output contract never blocks a stop
            if already_succeeded:
                raise WorkflowTransitionRejected(
                    f"Mission plan {mission_id!r} v{plan_version} already succeeded; there is nothing to halt."
                )
            if kind == "bounded_stop":
                try:
                    root_budget: execution_budget.TaskBudgetState | None = execution_budget.TaskBudgetState.from_json(
                        str(plan["root_budget_json"])
                    )
                except execution_budget.BudgetError:
                    root_budget = None
                root_stop = _mission_root_stop(root_budget, now=ts)
                if root_stop is not None:
                    code, reason, scope = root_stop[0], root_stop[1], "root"
                else:
                    task_stops = _mission_task_stops_locked(conn, mission_id, plan_version, now=ts)
                    if not task_stops:
                        # The stop is derived from durable budgets, never from a caller's claim.
                        raise WorkflowTransitionRejected(
                            f"No declared limit requires a bounded stop of mission plan {mission_id!r} "
                            f"v{plan_version}; use kind='cancelled' for an operator stop."
                        )
                    code, reason, scope = task_stops[0][1], task_stops[0][2], f"task:{task_stops[0][0]}"
                action_code = "bounded_stop_in_flight"
            else:
                code, reason, scope = "mission_cancelled", note or "operator_cancelled", ""
                action_code = "cancelled_in_flight"
            marker = _mission_stop_marker({"kind": kind, "code": code})

            stopped_keys: list[str] = []
            stopped_descendants = 0
            uncertain_ids: list[str] = []
            fenced_epochs = 0
            for task in owned:
                task_id = str(task["task_id"])
                from_state = str(task["state"])
                is_node = task_id == str(task["node_task_id"])
                if from_state == "completed":
                    continue  # finished work and its retained outputs are left exactly as they are
                fenced_epochs += conn.execute(
                    """
                    UPDATE runtime_task_epochs
                    SET status = 'fenced', fenced_at = ?, fence_reason = ?
                    WHERE task_id = ? AND status = 'active'
                    """,
                    (ts, marker, task_id),
                ).rowcount
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', ok = NULL, error_code = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class != 'read_only'
                    """,
                    (action_code, ts, task_id),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'failed', ok = 0, error_code = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class = 'read_only'
                    """,
                    (action_code, ts, task_id),
                )
                task_unresolved = _mission_unresolved_locked(conn, [task_id])
                uncertain_ids.extend(task_unresolved)
                # The budget refuses every later charge or observation on its own,
                # independent of the epoch fence.  An unreadable budget never blocks a stop.
                budget_json = str(task["budget_json"])
                with contextlib.suppress(execution_budget.BudgetError):
                    budget_json = replace(
                        execution_budget.TaskBudgetState.from_json(budget_json),
                        pause_requested=False,
                        cancel_requested=True,
                    ).to_json()
                if from_state in {"cancelled", "failed"}:
                    conn.execute(
                        "UPDATE runtime_tasks SET budget_json = ?, updated_at = ? WHERE task_id = ?",
                        (budget_json, ts, task_id),
                    )
                    continue
                ckpt_ref = str(task["checkpoint_ref"] or "").strip()
                if ckpt_ref:
                    conn.execute(
                        "UPDATE runtime_task_checkpoints SET status = 'interrupted', updated_at = ? "
                        "WHERE checkpoint_id = ?",
                        (ts, ckpt_ref),
                    )
                conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET state = 'cancelled', state_version = state_version + 1, budget_json = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (budget_json, ts, task_id),
                )
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                evidence: dict[str, Any] = {
                    "mission_id": mission_id,
                    "plan_version": plan_version,
                    "mission_task_key": str(task["task_key"]),
                    "halt_kind": kind,
                    "stop_code": code,
                }
                if task_unresolved:
                    evidence["unresolved_action_ids"] = task_unresolved
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, 'cancelled', ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        int(seq_row[0]) if seq_row else 1,
                        from_state,
                        int(task["current_epoch"]),
                        marker,
                        _encode_evidence_json(checkpoint_id=ckpt_ref, extra=evidence),
                        ts,
                    ),
                )
                if is_node:
                    stopped_keys.append(str(task["task_key"]))
                else:
                    stopped_descendants += 1

            conn.execute(
                """
                UPDATE runtime_mission_plans SET status = ?, updated_at = ?
                WHERE mission_id = ? AND plan_version = ? AND status = 'active'
                """,
                ("cancelled" if kind == "cancelled" else "failed", ts, mission_id, plan_version),
            )
            stop: dict[str, Any] = {
                "kind": kind,
                "code": code,
                "reason": reason,
                "requested_at": ts,
                "stopped_task_keys": stopped_keys,
                "stopped_descendant_tasks": stopped_descendants,
                "fenced_epochs": fenced_epochs,
                "uncertain_action_ids": uncertain_ids,
                # Recorded with the stop itself, so a crash before containment still
                # leaves an honest "termination not attested" record.
                "owned_work": {"termination_attempted": False},
            }
            if scope:
                stop["scope"] = scope
            if note and kind == "bounded_stop":
                stop["note"] = note
            # Stop first, report second: a plan whose own records cannot be read is
            # still stopped, and the caller learns about the unreadable record afterwards.
            with contextlib.suppress(MissionGraphError):
                _mission_persist_recovery_locked(
                    conn,
                    _mission_recovery_locked(conn, mission_id, plan_version, now=ts, stop=stop),
                )
            return _MissionHaltTicket(
                mission_id=mission_id,
                plan_version=plan_version,
                session_id=session_id,
                stop=stop,
                scopes=_mission_halt_scopes_locked(conn, owned_task_ids),
                newly_halted=True,
                needs_containment=True,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
        )

    def _mission_halt_fence_origins(
        self,
        ticket: _MissionHaltTicket,
        supervisor: process_supervision.ProcessSupervisor | None,
    ) -> int:
        """Refuse later spawns from every lease this stop fenced, at the process layer.

        The worker identity is fenced with its lease, as the session cancel path
        does.  A worker pool must therefore use one identity per claim: an identity
        shared across missions is refused everywhere once one of them is halted.
        """
        store = supervisor.store if supervisor is not None else self._containment_store
        if store is None:
            return 0
        fenced = 0
        for task_id, epochs in ticket.scopes:
            for epoch, worker_id in epochs:
                store.fence_origin(
                    reason=_mission_stop_marker(ticket.stop),
                    session_id=ticket.session_id,
                    task_id=task_id,
                    epoch=epoch,
                    worker_id=worker_id or None,
                )
                fenced += 1
        return fenced

    def _mission_halt_finish(
        self,
        ticket: _MissionHaltTicket,
        owned_work: dict[str, Any] | None,
        *,
        ts: float,
        artifact_status: Callable[[str], Any] | None,
    ) -> MissionRecoveryReport:
        stop = dict(ticket.stop)
        if owned_work is not None:
            stop["owned_work"] = owned_work
        return self._mission_report(
            ticket.mission_id,
            ticket.plan_version,
            ts=ts,
            artifact_status=artifact_status,
            isolation=None,
            stop=stop,
            persist=True,
        )

    def halt_mission(
        self,
        mission_id: str,
        plan_version: int,
        *,
        authority: execution_spine.MissionAuthorityEnvelope,
        kind: str = "cancelled",
        reason: str = "",
        supervisor: process_supervision.ProcessSupervisor | None = None,
        artifact_status: Callable[[str], Any] | None = None,
        now: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> MissionRecoveryReport:
        """Stop one mission plan: fence dispatch, end owned work, report truthfully (AP-007).

        Order matters.

        1. One transaction marks the plan ``cancelled`` (operator stop) or ``failed``
           (bounded stop).  For every unfinished task the plan owns, which means its
           nodes and all of their delegated descendants, it fences the live worker
           lease, turns in-flight consequential actions ``uncertain``, sets the budget
           cancellation flag and moves the task to ``cancelled``.  Nothing can be
           admitted for the plan afterwards.
        2. The process layer refuses later spawns from the fenced leases.
        3. With a ``supervisor``, every process registered for those tasks is
           terminated.  The time from this request to the last verified exit is
           recorded as ``cleanup_latency_ms``.
        4. The report is stored with the measured stop record.

        ``kind="bounded_stop"`` is accepted only when a durable budget requires it; the
        code and reason come from that budget, never from the caller.  ``authority`` is
        checked for identity only, so a stale policy generation cannot block a stop.
        Completed tasks and their outputs are retained.  The root task is not cancelled.

        A second call changes nothing durable and the first stop kind stands.  With a
        ``supervisor`` it repeats containment when no clean stop is on record, and it
        re-checks processes an earlier attempt could not end instead of assuming they
        are gone.  Without a ``supervisor`` termination is recorded as not attested.

        New work can be planned on the root only after the stop is settled: see
        :class:`MissionHaltUnsettled`.
        """
        ticket, ts, started = self._mission_halt_prepare(
            mission_id, plan_version, authority=authority, kind=kind, reason=reason, now=now, clock=clock
        )
        settled = self._mission_halt_without_supervisor(ticket, supervisor, ts=ts, artifact_status=artifact_status)
        if settled is not None or supervisor is None:
            assert settled is not None
            return settled
        origins = self._mission_halt_fence_origins(ticket, supervisor)
        fenced = clock()
        prior_listed, prior_unlisted = _mission_prior_lingering(ticket.stop)
        outcomes: list[process_supervision.TerminationOutcome] = []
        errors = 0
        for task_id, _epochs in ticket.scopes:
            try:
                outcomes.extend(
                    supervisor.contain_by_scope_sync(
                        reason=_mission_stop_marker(ticket.stop),
                        session_id=ticket.session_id,
                        task_id=task_id,
                    )
                )
            except Exception:
                errors += 1  # reported as "not verified as stopped"; the durable stop stands
        owned = _mission_owned_work(
            outcomes,
            origins_fenced=origins,
            errors=errors,
            started=started,
            fenced=fenced,
            done=clock(),
            retry=not ticket.newly_halted,
            attempted_at=ts,
            # Survivors of an earlier attempt are re-checked, never assumed gone.
            carried=supervisor.still_running(prior_listed),
            carried_unlisted=prior_unlisted,
        )
        return self._mission_halt_finish(ticket, owned, ts=ts, artifact_status=artifact_status)

    async def halt_mission_async(
        self,
        mission_id: str,
        plan_version: int,
        *,
        authority: execution_spine.MissionAuthorityEnvelope,
        kind: str = "cancelled",
        reason: str = "",
        supervisor: process_supervision.ProcessSupervisor | None = None,
        artifact_status: Callable[[str], Any] | None = None,
        now: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> MissionRecoveryReport:
        """Event-loop form of :meth:`halt_mission`; containment awaits the supervisor."""
        ticket, ts, started = self._mission_halt_prepare(
            mission_id, plan_version, authority=authority, kind=kind, reason=reason, now=now, clock=clock
        )
        settled = self._mission_halt_without_supervisor(ticket, supervisor, ts=ts, artifact_status=artifact_status)
        if settled is not None or supervisor is None:
            assert settled is not None
            return settled
        origins = self._mission_halt_fence_origins(ticket, supervisor)
        fenced = clock()
        prior_listed, prior_unlisted = _mission_prior_lingering(ticket.stop)
        outcomes: list[process_supervision.TerminationOutcome] = []
        errors = 0
        for task_id, _epochs in ticket.scopes:
            try:
                outcomes.extend(
                    await supervisor.contain_by_scope_async(
                        reason=_mission_stop_marker(ticket.stop),
                        session_id=ticket.session_id,
                        task_id=task_id,
                    )
                )
            except Exception:
                errors += 1  # reported as "not verified as stopped"; the durable stop stands
        owned = _mission_owned_work(
            outcomes,
            origins_fenced=origins,
            errors=errors,
            started=started,
            fenced=fenced,
            done=clock(),
            retry=not ticket.newly_halted,
            attempted_at=ts,
            # Survivors of an earlier attempt are re-checked, never assumed gone.
            carried=supervisor.still_running(prior_listed),
            carried_unlisted=prior_unlisted,
        )
        return self._mission_halt_finish(ticket, owned, ts=ts, artifact_status=artifact_status)

    def _mission_halt_without_supervisor(
        self,
        ticket: _MissionHaltTicket,
        supervisor: process_supervision.ProcessSupervisor | None,
        *,
        ts: float,
        artifact_status: Callable[[str], Any] | None,
    ) -> MissionRecoveryReport | None:
        """Finish a stop that runs no process containment; ``None`` means containment follows."""
        if not ticket.needs_containment:
            return self._mission_halt_finish(ticket, None, ts=ts, artifact_status=artifact_status)
        if supervisor is not None:
            return None
        origins = self._mission_halt_fence_origins(ticket, None)
        # A repeated unsupervised stop must not overwrite an earlier supervised record.
        owned: dict[str, Any] | None = (
            {"termination_attempted": False, "origins_fenced": origins} if ticket.newly_halted else None
        )
        return self._mission_halt_finish(ticket, owned, ts=ts, artifact_status=artifact_status)

    def _mission_halt_prepare(
        self,
        mission_id: str,
        plan_version: int,
        *,
        authority: execution_spine.MissionAuthorityEnvelope,
        kind: str,
        reason: str,
        now: float | None,
        clock: Callable[[], float],
    ) -> tuple[_MissionHaltTicket, float, float]:
        mid = _mission_clean_name(mission_id, "mission_id")
        version = _mission_plan_version(plan_version)
        if kind not in MISSION_HALT_KINDS:
            raise WorkflowStateError(f"Unsupported mission halt kind: {kind!r}.")
        if not isinstance(authority, execution_spine.MissionAuthorityEnvelope):
            raise MissionBindingRejected("A server-created mission authority envelope is required to stop a mission.")
        note = " ".join(str(reason or "").replace("\x00", " ").split())[:240]
        ts = time.time() if now is None else float(now)
        started = clock()
        ticket = self._mission_halt_begin(mid, version, kind=kind, authority=authority, note=note, ts=ts)
        return ticket, ts, started

    def prepare_mission_task_resume(
        self,
        mission_id: str,
        plan_version: int,
        task_key: str,
        *,
        binding: MissionExecutionBinding,
        current_inputs: Mapping[str, Any],
        current_authority: execution_spine.MissionAuthorityEnvelope,
        worker_id: str,
        expected_state_version: int,
        lease_ttl_s: float = 30.0,
        now: float | None = None,
        isolation: execution_budget.IsolationEvidence | None = None,
    ) -> MissionResumePlan:
        """Atomically validate checkpoint bindings and mint the sole successor epoch.

        AP-007: the isolation requirement and the deadline are re-checked here, so a
        resume cannot reach work that a fresh claim would have refused.
        """
        mid = _mission_clean_name(mission_id, "mission_id")
        key = _mission_clean_name(task_key, "task_key")
        wid = _validate_identifier(worker_id, "worker_id")
        if not isinstance(lease_ttl_s, (int, float)) or isinstance(lease_ttl_s, bool) or not 0 < float(lease_ttl_s) <= 86_400:
            raise MissionGraphError("lease_ttl_s must be in (0, 86400].")
        ts = time.time() if now is None else float(now)

        def _tx(conn: sqlite3.Connection) -> MissionResumePlan:
            conn.row_factory = sqlite3.Row
            node = conn.execute(
                """
                SELECT rt.* FROM runtime_mission_tasks AS mt
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mt.mission_id = ? AND mt.plan_version = ? AND mt.task_key = ?
                """,
                (mid, int(plan_version), key),
            ).fetchone()
            if node is None:
                raise workflow_checkpoints.ResumeRejected(f"Mission task {key!r} does not exist.")
            task_id = str(node["task_id"])
            plan = self._mission_validate_binding_locked(
                conn,
                mission_id=mid,
                plan_version=int(plan_version),
                binding=binding,
                current_inputs=current_inputs,
                current_authority=current_authority,
                expected_task_id=task_id,
            )
            if str(node["state"]) not in {"paused", "failed"}:
                raise workflow_checkpoints.ResumeRejected(
                    f"Mission task {key!r} in state {node['state']!r} is not resumable."
                )
            if int(node["state_version"]) != int(expected_state_version):
                raise workflow_checkpoints.ResumeRejected("Mission task state_version changed before resume.")
            ckpt_id = str(node["checkpoint_ref"] or "").strip()
            if not ckpt_id:
                raise workflow_checkpoints.ResumeRejected("Mission task has no recoverable checkpoint.")
            ckpt_row = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                (ckpt_id, task_id),
            ).fetchone()
            if ckpt_row is None:
                raise workflow_checkpoints.ResumeRejected("Mission task references a missing checkpoint.")
            checkpoint = workflow_checkpoints._decode_and_verify_checkpoint_row(
                ckpt_row,
                verify_integrity=True,
            )
            if checkpoint.status != "interrupted":
                raise workflow_checkpoints.ResumeRejected(
                    f"Mission checkpoint must be interrupted before resume, not {checkpoint.status!r}."
                )
            bound = conn.execute(
                "SELECT * FROM runtime_mission_checkpoint_bindings WHERE checkpoint_id = ?",
                (ckpt_id,),
            ).fetchone()
            if bound is None:
                raise workflow_checkpoints.ResumeRejected("Checkpoint is missing its mission resume binding.")
            bound_identity = (
                str(bound["mission_id"]),
                int(bound["plan_version"]),
                str(bound["task_id"]),
                str(bound["plan_digest"]),
                str(bound["input_digest"]),
                int(bound["session_policy_revision"]),
                int(bound["global_policy_revision"]),
                str(bound["budget_digest"]),
            )
            current_identity = (
                mid,
                int(plan_version),
                task_id,
                str(plan["plan_digest"]),
                str(plan["input_digest"]),
                int(plan["session_policy_revision"]),
                int(plan["global_policy_revision"]),
                str(plan["budget_digest"]),
            )
            if bound_identity != current_identity:
                raise workflow_checkpoints.ResumeRejected("Checkpoint mission binding is stale.")
            checkpoint_epoch = int(bound["checkpoint_epoch"])
            if checkpoint.epoch != checkpoint_epoch or int(node["current_epoch"]) != checkpoint_epoch:
                raise workflow_checkpoints.ResumeRejected(
                    "Checkpoint worker epoch was superseded; stale work cannot resume."
                )
            if int(node["state_version"]) != int(bound["task_state_version"]) + 1:
                raise workflow_checkpoints.ResumeRejected(
                    "Mission task changed beyond the single checkpoint interruption transition."
                )
            unresolved = conn.execute(
                """
                SELECT action_id FROM runtime_actions
                WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                """,
                (task_id,),
            ).fetchall()
            if unresolved:
                raise workflow_checkpoints.ResumeRejected(
                    "Mission task has unresolved consequential actions and cannot resume."
                )
            try:
                root_budget = execution_budget.TaskBudgetState.from_json(str(plan["root_budget_json"]))
            except execution_budget.BudgetError as exc:
                raise workflow_checkpoints.ResumeRejected(f"Mission root budget is unreadable: {exc}") from exc
            violation = root_budget.violation_reason(check_recovery=False, now=ts)
            if root_budget.cancel_requested or violation:
                raise workflow_checkpoints.ResumeRejected(violation or "Mission cancellation is active.")
            _mission_require_isolation(root_budget, isolation)
            active_rows = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND status = 'active'",
                (task_id,),
            ).fetchall()
            for active in active_rows:
                if float(active["lease_expires_at"]) > ts:
                    raise workflow_checkpoints.ResumeRejected(
                        f"Mission task already has active successor worker {active['worker_id']!r}."
                    )
                conn.execute(
                    """
                    UPDATE runtime_task_epochs
                    SET status = 'expired', fenced_at = ?, fence_reason = 'mission_resume_expired'
                    WHERE task_id = ? AND epoch = ? AND status = 'active'
                    """,
                    (ts, task_id, int(active["epoch"])),
                )
            next_epoch = int(node["current_epoch"]) + 1
            conn.execute(
                """
                INSERT INTO runtime_task_epochs (
                    task_id, epoch, worker_id, status, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (task_id, next_epoch, wid, ts, ts, ts + float(lease_ttl_s)),
            )
            conn.execute(
                """
                UPDATE runtime_task_checkpoints
                SET status = 'resumed', epoch = ?, updated_at = ? WHERE checkpoint_id = ?
                """,
                (next_epoch, ts, ckpt_id),
            )
            conn.execute(
                """
                UPDATE runtime_mission_checkpoint_bindings
                SET resumed_epoch = ? WHERE checkpoint_id = ?
                """,
                (next_epoch, ckpt_id),
            )
            conn.execute(
                """
                UPDATE runtime_tasks
                SET state = 'running', current_epoch = ?, state_version = state_version + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (next_epoch, ts, task_id),
            )
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO runtime_task_transitions (
                    task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                ) VALUES (?, ?, ?, 'running', ?, 'mission_checkpoint_resumed', ?, ?)
                """,
                (
                    task_id,
                    int(seq_row[0]) if seq_row else 1,
                    str(node["state"]),
                    next_epoch,
                    _encode_evidence_json(checkpoint_id=ckpt_id),
                    ts,
                ),
            )
            updated_task = conn.execute("SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)).fetchone()
            updated_epoch = conn.execute(
                "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                (task_id, next_epoch),
            ).fetchone()
            updated_ckpt = conn.execute(
                "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ?",
                (ckpt_id,),
            ).fetchone()
            assert updated_task is not None and updated_epoch is not None and updated_ckpt is not None
            return MissionResumePlan(
                task=runtime_persistence._row_to_task(updated_task),
                epoch=runtime_persistence._row_to_epoch(updated_epoch),
                checkpoint=workflow_checkpoints._decode_and_verify_checkpoint_row(
                    updated_ckpt,
                    verify_integrity=True,
                ),
                next_step=checkpoint.step + 1,
            )

        return runtime_persistence.run_write_transaction(
            self._storage,
            self._db_path,
            _tx,
            retry_policy=self._retry_policy,
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

        def _read(conn: sqlite3.Connection) -> list[runtime_persistence.TaskRecord]:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT rt.* FROM runtime_tasks AS rt
                WHERE rt.parent_task_id = ?
                  AND (
                    NOT EXISTS (
                        SELECT 1 FROM runtime_mission_tasks AS mt WHERE mt.task_id = rt.task_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM runtime_mission_tasks AS mt
                        JOIN runtime_mission_plans AS mp
                          ON mp.mission_id = mt.mission_id
                         AND mp.plan_version = mt.plan_version
                        WHERE mt.task_id = rt.task_id AND mp.status IN ('active', 'completed')
                    )
                  )
                ORDER BY rt.created_at ASC, rt.task_id ASC
                """,
                (root_id,),
            ).fetchall()
            return [
                runtime_persistence._row_to_task(row)
                for row in rows
                if not child_delegation.is_child_subtask_id(str(row["task_id"]))
            ]

        return runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read,
            retry_policy=self._retry_policy,
        )

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
                deferred_budget = replace(
                    budget,
                    pause_requested=True,
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

            cleared_budget = replace(
                budget,
                pause_requested=False,
                cancel_requested=False,
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
            cleared_budget = replace(
                budget,
                pause_requested=False,
                cancel_requested=False,
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
            cancelled_budget = replace(
                budget,
                pause_requested=False,
                cancel_requested=True,
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
        security_generation: Callable[[], int | None] | None = None,
    ) -> TaskVerificationReport:
        """Verify completion, then record the attempt in durable failure memory.

        The report is unchanged when the attempt made progress. When the same
        failure evidence repeats (or an abandoned state is revisited), the verdict
        is attached and a safe pause is requested instead of letting the loop try
        the same strategy again (M5-P2-05).
        """
        report = self._verify_task_completion_impl(
            task_id,
            workspace_dir=workspace_dir,
            spec=spec,
            security_generation=security_generation,
        )
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
        security_generation: Callable[[], int | None] | None = None,
    ) -> TaskVerificationReport:
        """Authoritatively verify ledger, patch, protected-file, and test evidence outside write locks."""
        runtime_persistence.assert_no_active_write_transaction("task_verification")
        tid = _validate_identifier(task_id, "task_id")
        ws_root = Path(workspace_dir).resolve()
        effective_spec = spec if spec is not None else self.get_verification_spec(tid)
        spec_digest = effective_spec.sha256() if effective_spec is not None else ""
        # Captured before any evidence is gathered: a security configuration change
        # between here and the final verdict makes the evidence stale (M5-P5-02).
        generation_before = _capture_security_generation(security_generation)

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

        generation_after = _capture_security_generation(security_generation)
        if (
            generation_before is not None
            and generation_after is not None
            and generation_after != generation_before
        ):
            # The evidence was gathered under one security configuration and is being
            # accepted under another: the guarantee it demonstrates may no longer hold.
            return TaskVerificationReport(
                verified=False,
                spec_sha256=spec_digest,
                failure_code="security_configuration_changed_during_verification",
                failure_detail=(
                    "The security configuration generation changed while the verification ran "
                    f"(generation {generation_before} -> {generation_after}); the evidence cannot be "
                    "tied to the guarantees it claims to demonstrate."
                ),
                committed_edit_action_ids=tuple(a.action_id for a in committed_edits),
                committed_test_action_ids=tuple(a.action_id for a in committed_tests),
                source_revision=evidence_kwargs["source_revision"],
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

        def _read_active_join(conn: sqlite3.Connection) -> MissionJoinReport | None:
            conn.row_factory = sqlite3.Row
            active = conn.execute(
                """
                SELECT mission_id, plan_version FROM runtime_mission_plans
                WHERE root_task_id = ? AND status = 'active'
                """,
                (tid,),
            ).fetchone()
            if active is None:
                return None
            return _mission_join_report_locked(
                conn,
                str(active["mission_id"]),
                int(active["plan_version"]),
                now=ts,
            )

        mission_join = runtime_persistence.run_read(
            self._storage,
            self._db_path,
            _read_active_join,
            retry_policy=self._retry_policy,
        )
        if mission_join is not None and mission_join.status != "succeeded":
            raise FalseCompletionRejected(
                f"Task {tid!r} mission join is {mission_join.status!r}; "
                f"failed={list(mission_join.failed_task_keys)}, "
                f"incomplete={list(mission_join.incomplete_task_keys)}, "
                f"missing_outputs={list(mission_join.missing_output_task_keys)}."
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
                """
                SELECT rt.task_id, rt.state, rt.checkpoint_ref
                FROM runtime_tasks AS rt
                WHERE rt.parent_task_id = ?
                  AND (
                    NOT EXISTS (
                        SELECT 1 FROM runtime_mission_tasks AS mt WHERE mt.task_id = rt.task_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM runtime_mission_tasks AS mt
                        JOIN runtime_mission_plans AS mp
                          ON mp.mission_id = mt.mission_id
                         AND mp.plan_version = mt.plan_version
                        WHERE mt.task_id = rt.task_id AND mp.status = 'active'
                    )
                  )
                """,
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
            active_plan = conn.execute(
                """
                SELECT mission_id, plan_version FROM runtime_mission_plans
                WHERE root_task_id = ? AND status = 'active'
                """,
                (tid,),
            ).fetchone()
            committed_join: MissionJoinReport | None = None
            if active_plan is not None:
                committed_join = _mission_join_report_locked(
                    conn,
                    str(active_plan["mission_id"]),
                    int(active_plan["plan_version"]),
                    now=ts,
                )
                if committed_join.status != "succeeded":
                    raise FalseCompletionRejected(
                        f"Task {tid!r} mission join changed to {committed_join.status!r} before completion committed."
                    )
                _mission_persist_join_locked(conn, committed_join)
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
            if committed_join is not None:
                conn.execute(
                    """
                    UPDATE runtime_mission_plans
                    SET status = 'completed', updated_at = ?
                    WHERE mission_id = ? AND plan_version = ? AND status = 'active'
                    """,
                    (ts, committed_join.mission_id, committed_join.plan_version),
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
                      AND task_id NOT IN (
                          SELECT mt.task_id FROM runtime_mission_tasks AS mt
                          JOIN runtime_mission_plans AS mp
                            ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                          WHERE mp.status = 'active'
                      )
                      AND status = 'active'
                    """,
                    (ts, tid),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE (
                        task_id = ? OR (
                            task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?)
                            AND task_id NOT IN (
                                SELECT mt.task_id FROM runtime_mission_tasks AS mt
                                JOIN runtime_mission_plans AS mp
                                  ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                                WHERE mp.status = 'active'
                            )
                        )
                    )
                      AND status = 'in_flight' AND action_class != 'read_only'
                    """,
                    (ts, tid, tid),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'failed', ok = 0, error_code = 'orphaned_in_flight_on_recovery', updated_at = ?
                    WHERE (
                        task_id = ? OR (
                            task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?)
                            AND task_id NOT IN (
                                SELECT mt.task_id FROM runtime_mission_tasks AS mt
                                JOIN runtime_mission_plans AS mp
                                  ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                                WHERE mp.status = 'active'
                            )
                        )
                    )
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
                        WHERE (
                            task_id = ? OR (
                                task_id IN (SELECT task_id FROM runtime_tasks WHERE parent_task_id = ?)
                                AND task_id NOT IN (
                                    SELECT mt.task_id FROM runtime_mission_tasks AS mt
                                    JOIN runtime_mission_plans AS mp
                                      ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                                    WHERE mp.status = 'active'
                                )
                            )
                        )
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

                    reconciled_budget = replace(
                        budget,
                        pause_requested=False,
                        cancel_requested=(target_state == "cancelled"),
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
                        f"""
                        SELECT task_id, state FROM runtime_tasks
                        WHERE parent_task_id = ? AND {ms_filter}
                          AND task_id NOT IN (
                              SELECT mt.task_id FROM runtime_mission_tasks AS mt
                              JOIN runtime_mission_plans AS mp
                                ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                              WHERE mp.status = 'active'
                          )
                        """,
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

            # 4. Recover active mission nodes independently from the root's legacy
            # milestone lease. Mission nodes own their own epochs and resume bindings.
            mission_rows = conn.execute(
                """
                SELECT rt.*, mt.mission_id, mt.plan_version, mt.task_key,
                       mp.plan_digest, mp.input_digest, mp.session_policy_revision,
                       mp.global_policy_revision, mp.budget_digest
                FROM runtime_mission_tasks AS mt
                JOIN runtime_mission_plans AS mp
                  ON mp.mission_id = mt.mission_id AND mp.plan_version = mt.plan_version
                JOIN runtime_tasks AS rt ON rt.task_id = mt.task_id
                WHERE mp.status = 'active' AND rt.state IN ('running', 'waiting_approval')
                ORDER BY rt.created_at ASC, mt.position ASC
                """
            ).fetchall()
            for mission_row in mission_rows:
                task_id = str(mission_row["task_id"])
                current_epoch = int(mission_row["current_epoch"])
                active = conn.execute(
                    "SELECT * FROM runtime_task_epochs WHERE task_id = ? AND epoch = ?",
                    (task_id, current_epoch),
                ).fetchone()
                if (
                    active is not None
                    and str(active["status"]) == "active"
                    and float(active["lease_expires_at"]) > ts
                ):
                    continue
                if active is not None and str(active["status"]) == "active":
                    conn.execute(
                        """
                        UPDATE runtime_task_epochs
                        SET status = 'expired', fenced_at = ?, fence_reason = 'mission_lease_expired_on_recovery'
                        WHERE task_id = ? AND epoch = ? AND status = 'active'
                        """,
                        (ts, task_id, current_epoch),
                    )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'uncertain', error_code = 'orphaned_mission_action_on_recovery', updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class != 'read_only'
                    """,
                    (ts, task_id),
                )
                conn.execute(
                    """
                    UPDATE runtime_actions
                    SET status = 'failed', ok = 0, error_code = 'orphaned_mission_action_on_recovery', updated_at = ?
                    WHERE task_id = ? AND status = 'in_flight' AND action_class = 'read_only'
                    """,
                    (ts, task_id),
                )
                unresolved_rows = conn.execute(
                    """
                    SELECT action_id FROM runtime_actions
                    WHERE task_id = ? AND status IN ('in_flight', 'uncertain') AND action_class != 'read_only'
                    ORDER BY created_at ASC
                    """,
                    (task_id,),
                ).fetchall()
                unresolved_ids = [str(row["action_id"]) for row in unresolved_rows]
                checkpoint_id = str(mission_row["checkpoint_ref"] or "").strip()
                checkpoint_invalid = not checkpoint_id
                if checkpoint_id:
                    checkpoint_row = conn.execute(
                        "SELECT * FROM runtime_task_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
                        (checkpoint_id, task_id),
                    ).fetchone()
                    binding_row = conn.execute(
                        "SELECT * FROM runtime_mission_checkpoint_bindings WHERE checkpoint_id = ?",
                        (checkpoint_id,),
                    ).fetchone()
                    if checkpoint_row is None or binding_row is None:
                        checkpoint_invalid = True
                    else:
                        try:
                            checkpoint = workflow_checkpoints._decode_and_verify_checkpoint_row(
                                checkpoint_row,
                                verify_integrity=True,
                            )
                            expected_binding = (
                                str(mission_row["mission_id"]),
                                int(mission_row["plan_version"]),
                                task_id,
                                str(mission_row["plan_digest"]),
                                str(mission_row["input_digest"]),
                                int(mission_row["session_policy_revision"]),
                                int(mission_row["global_policy_revision"]),
                                str(mission_row["budget_digest"]),
                                current_epoch,
                            )
                            stored_binding = (
                                str(binding_row["mission_id"]),
                                int(binding_row["plan_version"]),
                                str(binding_row["task_id"]),
                                str(binding_row["plan_digest"]),
                                str(binding_row["input_digest"]),
                                int(binding_row["session_policy_revision"]),
                                int(binding_row["global_policy_revision"]),
                                str(binding_row["budget_digest"]),
                                int(binding_row["checkpoint_epoch"]),
                            )
                            checkpoint_invalid = (
                                expected_binding != stored_binding
                                or checkpoint.epoch != current_epoch
                            )
                        except workflow_checkpoints.ResumeRejected:
                            checkpoint_invalid = True
                    if not checkpoint_invalid:
                        conn.execute(
                            """
                            UPDATE runtime_task_checkpoints
                            SET status = 'interrupted', updated_at = ? WHERE checkpoint_id = ?
                            """,
                            (ts, checkpoint_id),
                        )
                if unresolved_ids or checkpoint_invalid:
                    target = "blocked"
                    recovery_reason = (
                        "mission_checkpoint_invalid_on_recovery"
                        if checkpoint_invalid
                        else "mission_unresolved_effect_on_recovery"
                    )
                    blocked_tasks.append(task_id)
                else:
                    target = "paused"
                    recovery_reason = "mission_checkpoint_recovered"
                    recovered_tasks.append(task_id)
                conn.execute(
                    """
                    UPDATE runtime_tasks
                    SET state = ?, state_version = state_version + 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (target, ts, task_id),
                )
                seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM runtime_task_transitions WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO runtime_task_transitions (
                        task_id, seq, from_state, to_state, epoch, reason, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        int(seq_row[0]) if seq_row else 1,
                        str(mission_row["state"]),
                        target,
                        current_epoch,
                        recovery_reason,
                        _encode_evidence_json(
                            checkpoint_id=checkpoint_id,
                            extra={"unresolved_action_ids": unresolved_ids} if unresolved_ids else None,
                        ),
                        ts,
                    ),
                )
                reconciled_milestones.append(task_id)

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
