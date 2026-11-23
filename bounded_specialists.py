"""Bounded specialists and versioned handoffs (AP-017).

Enable useful collaboration without a second coordinator or uncontrolled fan-out.

This module is a **composition contract**, not a coordinator, scheduler or
executor. ``child_delegation`` already owns spawn-time authority and budget
derivation, durable delegation state, join records and cancel propagation;
``artifact_lineage`` already owns versioned artifact identity; ``execution_budget``
already owns the budget arithmetic; ``challenger_evaluation`` already owns
comparable-budget reporting. Restating any of those here would create the second
control plane the story forbids, so this module *requires their values* and
refuses to proceed when they disagree with what it was told.

What it adds on top:

1. A **typed specialist task** whose handoff input and output are versioned
   artifact identities, and whose authority and declared budget must be strict
   parent subsets — checked again at dispatch, not only at spawn (AC1).
2. **Scoped-write claims**: two workers cannot hold overlapping write scopes,
   and an overlapping claim is refused rather than serialised silently (AC2).
3. A **durable, bounded ledger** of dispatch, settle and join so a cancel,
   failure or restart is reconciled from records instead of hope (AC3).
4. A **reviewer brief** that carries evidence and explicitly refuses to ask for
   endorsement or a verdict (AC4).
5. An explicit statement that logical parallelism is not concurrent local
   inference, with a refusal when a caller claims otherwise (AC5).
6. **Topology retention** decided only by measured, comparable evidence, which
   defaults to single agent when no measurement exists (AC6).

It runs no model, starts no runtime, calls no provider and contacts no network.
Nothing here is wired to a production caller. ``runtime_activation_authorized``
remains false.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import child_delegation
import execution_budget
import scoped_grants
from challenger_evaluation import (
    ChallengerEvaluationError,
    EvaluationDesign,
    StrategyResult,
    Variability,
    build_report,
)

BOUNDED_SPECIALIST_SCHEMA_VERSION = 1

SpecialistRole = Literal["critic", "worker", "verifier", "reviewer"]
SPECIALIST_ROLES: tuple[SpecialistRole, ...] = (
    "critic",
    "worker",
    "verifier",
    "reviewer",
)

#: Roles whose output is an opinion about someone else's work. A reviewer or
#: critic never holds a write scope: it receives a brief and returns evidence.
ADVISORY_ROLES: frozenset[str] = frozenset({"critic", "reviewer"})

#: Roles that may hold a scoped write claim.
WRITING_ROLES: frozenset[str] = frozenset({"worker", "verifier"})

DispatchState = Literal[
    "dispatched",
    "completed",
    "failed",
    "cancelled",
    "joined",
    "joined_discarded",
]
DISPATCH_STATES: tuple[DispatchState, ...] = (
    "dispatched",
    "completed",
    "failed",
    "cancelled",
    "joined",
    "joined_discarded",
)

#: The only states from which no further transition is possible.
TERMINAL_STATES: frozenset[str] = frozenset({"joined", "joined_discarded"})

TOPOLOGIES: tuple[str, ...] = ("single_agent", "specialists")

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: Hard ceiling on recorded entries in one reconciliation ledger. A join that
#: needs more bookkeeping than this is not bounded, so the ledger refuses rather
#: than growing without limit (AC3).
MAX_LEDGER_ENTRIES = 256


class BoundedSpecialistError(RuntimeError):
    """Base error for bounded specialist work."""


class SpecialistRejectedError(BoundedSpecialistError):
    """Raised when a handoff, claim or join is refused. Carries a stable code."""

    def __init__(
        self, code: str, reason: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = str(code)
        self.reason = str(reason)
        self.details: dict[str, Any] = dict(details) if details is not None else {}

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "reason": self.reason}
        if self.details:
            out["details"] = dict(self.details)
        return out


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _clean_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise SpecialistRejectedError(
            "invalid_specialist_id",
            f"{label} must be a bounded identifier matching {_ID_RE.pattern}: {value!r}.",
        )
    return text


def _digest(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _DIGEST_RE.match(text):
        raise SpecialistRejectedError(
            "invalid_digest",
            f"{label} must be a sha256 hex digest: {value!r}.",
        )
    return text


def _non_empty(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SpecialistRejectedError("empty_field", f"{label} must not be empty.")
    return text


# ---------------------------------------------------------------------------
# AC1 — typed specialist task with strict parent-subset authority and budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParentBounds:
    """The parent's authority and remaining budget, supplied by the caller.

    This module does not derive these values — ``child_delegation`` and
    ``execution_budget`` already do — it only refuses a child that exceeds them.
    """

    parent_policy: str
    parent_tools: tuple[str, ...]
    parent_write_scopes: tuple[str, ...]
    remaining_steps: int
    remaining_tool_calls: int
    remaining_tokens: int
    remaining_elapsed_s: float

    def __post_init__(self) -> None:
        _non_empty(self.parent_policy, "parent_policy")
        object.__setattr__(
            self, "parent_tools", tuple(str(t) for t in self.parent_tools)
        )
        object.__setattr__(
            self, "parent_write_scopes", tuple(str(s) for s in self.parent_write_scopes)
        )
        for name in ("remaining_steps", "remaining_tool_calls", "remaining_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SpecialistRejectedError(
                    "invalid_parent_bounds",
                    f"parent {name} must be a non-negative integer.",
                )
        if not isinstance(self.remaining_elapsed_s, (int, float)) or isinstance(
            self.remaining_elapsed_s, bool
        ):
            raise SpecialistRejectedError(
                "invalid_parent_bounds", "parent remaining_elapsed_s must be a number."
            )
        if float(self.remaining_elapsed_s) < 0.0:
            raise SpecialistRejectedError(
                "invalid_parent_bounds",
                "parent remaining_elapsed_s must not be negative.",
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "parent_policy": self.parent_policy,
            "parent_tools": list(self.parent_tools),
            "parent_write_scopes": list(self.parent_write_scopes),
            "remaining_steps": self.remaining_steps,
            "remaining_tool_calls": self.remaining_tool_calls,
            "remaining_tokens": self.remaining_tokens,
            "remaining_elapsed_s": round(float(self.remaining_elapsed_s), 6),
        }


@dataclass(frozen=True, slots=True)
class HandoffArtifact:
    """A versioned artifact identity used as a handoff input or output (AC2).

    The version is the artifact's identity digest, so "the worker read the brief
    v2" and "the brief is now v3" are different facts, and a join across
    versions is detectable rather than assumed.
    """

    artifact_id: str
    version_sha256: str
    produced_by: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", _clean_id(self.artifact_id, "artifact_id")
        )
        object.__setattr__(
            self, "version_sha256", _digest(self.version_sha256, "version_sha256")
        )
        if self.produced_by:
            object.__setattr__(
                self, "produced_by", _clean_id(self.produced_by, "produced_by")
            )
        object.__setattr__(self, "label", str(self.label or "").strip())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "version_sha256": self.version_sha256,
            "produced_by": self.produced_by,
            "label": self.label,
        }


def bind_artifact_identity(
    *,
    artifact_id: str,
    identity_sha256: str,
    produced_by: str = "",
    label: str = "",
) -> HandoffArtifact:
    """Adapter over ``artifact_lineage``'s versioned identity (AP-006).

    Accepts the ``identity_sha256`` that ``artifact_lineage.artifact_identity_sha256``
    produces so callers do not invent a competing version token.
    """
    return HandoffArtifact(
        artifact_id=artifact_id,
        version_sha256=identity_sha256,
        produced_by=produced_by,
        label=label,
    )


@dataclass(frozen=True, slots=True)
class SpecialistTask:
    """One typed specialist assignment with versioned input and output (AC1, AC2)."""

    task_id: str
    role: SpecialistRole
    objective: str
    inputs: tuple[HandoffArtifact, ...]
    expected_output: str
    policy: str
    allowed_tools: tuple[str, ...]
    write_scopes: tuple[str, ...]
    budget: execution_budget.TaskBudgetSpec
    parent: ParentBounds

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _clean_id(self.task_id, "task_id"))
        if self.role not in SPECIALIST_ROLES:
            raise SpecialistRejectedError(
                "unknown_specialist_role", f"Unknown specialist role: {self.role!r}."
            )
        object.__setattr__(self, "objective", _non_empty(self.objective, "objective"))
        object.__setattr__(
            self, "expected_output", _non_empty(self.expected_output, "expected_output")
        )
        inputs = tuple(self.inputs)
        if not inputs:
            raise SpecialistRejectedError(
                "handoff_without_versioned_input",
                "a specialist handoff must name at least one versioned input artifact (AC2).",
            )
        if any(not isinstance(i, HandoffArtifact) for i in inputs):
            raise TypeError("inputs must hold HandoffArtifact values.")
        seen: set[str] = set()
        for item in inputs:
            if item.artifact_id in seen:
                raise SpecialistRejectedError(
                    "duplicate_handoff_input",
                    f"handoff input {item.artifact_id!r} appears twice.",
                )
            seen.add(item.artifact_id)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(
            self, "allowed_tools", tuple(str(t) for t in self.allowed_tools)
        )
        object.__setattr__(
            self, "write_scopes", tuple(str(s) for s in self.write_scopes)
        )
        if not isinstance(self.budget, execution_budget.TaskBudgetSpec):
            raise TypeError("budget must be an execution_budget.TaskBudgetSpec.")
        if not isinstance(self.parent, ParentBounds):
            raise TypeError("parent must be ParentBounds.")
        self._check_strict_subset()

    # -- AC1: authority and remaining budget are strict parent subsets ---------

    def _check_strict_subset(self) -> None:
        bounds = self.parent

        # A specialist may narrow the parent's policy but never widen it.
        parent_rank = _policy_rank(bounds.parent_policy)
        child_rank = _policy_rank(self.policy)
        if child_rank > parent_rank:
            raise SpecialistRejectedError(
                "specialist_policy_exceeds_parent",
                f"specialist policy {self.policy!r} exceeds parent policy {bounds.parent_policy!r} (AC1).",
            )

        # Every tool a specialist holds must be one the parent holds.
        extra = sorted(set(self.allowed_tools) - set(bounds.parent_tools))
        if extra:
            raise SpecialistRejectedError(
                "specialist_tool_exceeds_parent",
                f"specialist requested tools the parent does not hold: {extra} (AC1).",
                details={"tools": extra},
            )

        # Write scopes must be inside the parent's, never merely overlapping.
        for scope in self.write_scopes:
            if not any(
                _scope_within(scope, parent_scope)
                for parent_scope in bounds.parent_write_scopes
            ):
                raise SpecialistRejectedError(
                    "specialist_scope_exceeds_parent",
                    f"specialist write scope {scope!r} is not within any parent scope (AC1).",
                    details={"scope": scope},
                )

        # A caller cannot grant more than the parent has left. Equality is not a
        # subset: a child claiming the parent's whole remaining budget leaves the
        # parent nothing to join with, so remaining must be strictly greater.
        for name, child_value, parent_value in (
            ("max_steps", self.budget.max_steps, bounds.remaining_steps),
            ("max_tool_calls", self.budget.max_tool_calls, bounds.remaining_tool_calls),
            ("max_tokens", self.budget.max_tokens, bounds.remaining_tokens),
        ):
            if child_value >= parent_value:
                raise SpecialistRejectedError(
                    "specialist_budget_not_strict_subset",
                    f"{name}={child_value} is not a strict subset of the parent's remaining "
                    f"{parent_value} (AC1).",
                    details={"dimension": name},
                )
        if float(self.budget.max_elapsed_s) >= float(bounds.remaining_elapsed_s):
            raise SpecialistRejectedError(
                "specialist_budget_not_strict_subset",
                f"max_elapsed_s={self.budget.max_elapsed_s} is not a strict subset of the parent's "
                f"remaining {bounds.remaining_elapsed_s} (AC1).",
                details={"dimension": "max_elapsed_s"},
            )

        # An advisory role cannot hold write authority at all (AC4): a reviewer
        # that can edit what it reviews is not a reviewer.
        if self.role in ADVISORY_ROLES and self.write_scopes:
            raise SpecialistRejectedError(
                "advisory_role_cannot_write",
                f"role {self.role!r} is advisory and cannot hold a write scope (AC4).",
                details={"write_scopes": list(self.write_scopes)},
            )
        if self.role in ADVISORY_ROLES and (
            set(self.allowed_tools) & _mutating_tools()
        ):
            raise SpecialistRejectedError(
                "advisory_role_cannot_mutate",
                f"role {self.role!r} is advisory and cannot hold mutating tools (AC4).",
            )

    @property
    def handoff_digest(self) -> str:
        return _canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": BOUNDED_SPECIALIST_SCHEMA_VERSION,
            "task_id": self.task_id,
            "role": self.role,
            "objective": self.objective,
            "inputs": [
                i.to_mapping() for i in sorted(self.inputs, key=lambda i: i.artifact_id)
            ],
            "expected_output": self.expected_output,
            "policy": self.policy,
            "allowed_tools": list(self.allowed_tools),
            "write_scopes": list(self.write_scopes),
            "budget": self.budget.to_dict(),
            "parent": self.parent.to_mapping(),
        }


def _mutating_tools() -> frozenset[str]:
    """The mutating tools the delegation contract already classifies.

    Reused rather than re-listed so a tool added to ``child_delegation`` cannot
    silently become available to an advisory reviewer here.
    """
    return frozenset(
        set(child_delegation.CD_MUTATING_FILE_TOOLS)
        | set(child_delegation.CD_CODE_EXEC_TOOLS)
    )


_SCOPE_WORKSPACE = "m5"

_POLICY_RANKS: dict[str, int] = {"plan": 0, "standard": 1, "autonomous": 2}


def _policy_rank(policy: str) -> int:
    text = str(policy or "").strip()
    if text not in _POLICY_RANKS:
        raise SpecialistRejectedError("unknown_policy", f"Unknown policy: {policy!r}.")
    return _POLICY_RANKS[text]


def _scope_within(child_scope: str, parent_scope: str) -> bool:
    """True when ``parent_scope`` subsumes ``child_scope``.

    Delegated to ``scoped_grants.scope_subsumes`` so a specialist cannot be
    granted a scope the repository's own grant matcher does not already consider
    covered. Both bare relative paths and the ``path:``/``dir:``/``workspace:``
    grant forms the repository uses are accepted.
    """
    child = str(child_scope).strip()
    parent = str(parent_scope).strip()
    if not child or not parent:
        return False
    if scoped_grants.scope_subsumes(parent, child):
        return True
    # Bare relative paths are the ergonomic form; ``scoped_grants`` expects
    # ``dir:<ws>:<rel>`` / ``path:<ws>:<rel>``, so map onto one workspace token
    # rather than growing a second path grammar.
    child_rel = child.removeprefix("path:").removeprefix("dir:").strip("/")
    parent_rel = parent.removeprefix("path:").removeprefix("dir:").strip("/")
    if not child_rel or not parent_rel:
        return False
    if ":" in parent_rel:
        parent_rel = parent_rel.rsplit(":", 1)[-1].strip("/")
    if ":" in child_rel:
        child_rel = child_rel.rsplit(":", 1)[-1].strip("/")
    return scoped_grants.scope_subsumes(
        f"dir:{_SCOPE_WORKSPACE}:{parent_rel}",
        f"path:{_SCOPE_WORKSPACE}:{child_rel}",
    )


# ---------------------------------------------------------------------------
# AC2 — scoped-write ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteClaim:
    """One worker's exclusive claim over workspace paths, inside a parent scope."""

    claim_id: str
    task_id: str
    scopes: tuple[str, ...]
    parent_scope: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _clean_id(self.claim_id, "claim_id"))
        object.__setattr__(self, "task_id", _clean_id(self.task_id, "task_id"))
        scopes = tuple(str(s).strip() for s in self.scopes)
        if not scopes:
            raise SpecialistRejectedError(
                "empty_write_claim", "a write claim must name at least one scope."
            )
        for scope in scopes:
            if not _scope_within(scope, self.parent_scope):
                raise SpecialistRejectedError(
                    "write_claim_escapes_parent",
                    f"claim scope {scope!r} is outside the parent scope {self.parent_scope!r} (AC2).",
                )
        object.__setattr__(self, "scopes", scopes)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "task_id": self.task_id,
            "scopes": list(self.scopes),
            "parent_scope": self.parent_scope,
        }


def _scopes_overlap(a: str, b: str) -> bool:
    """True when two write scopes intersect, using the repository's grant matcher.

    ``child_delegation.child_scopes_overlap`` is the seam the delegation path
    already enforces; reusing it keeps one definition of "conflicting scope".
    """
    left = str(a).strip()
    right = str(b).strip()
    if not left or not right:
        return False
    return bool(child_delegation.child_scopes_overlap(left, right)) or bool(
        child_delegation.child_scopes_overlap(
            left if left.endswith("/") else left + "/",
            right if right.endswith("/") else right + "/",
        )
    )


def assert_scopes_disjoint(
    claims: Iterable[WriteClaim],
    *,
    seed: Sequence[WriteClaim] = (),
) -> tuple[WriteClaim, ...]:
    """Refuse overlapping write scopes across the team, and against existing claims.

    Two workers writing the same file is not "parallelism with a merge"; it is an
    uncontrolled overwrite. The refusal names both claims so the conflict is
    actionable rather than mysterious (AC2).
    """
    accepted: list[WriteClaim] = []
    for prior in list(seed) + list(claims):
        if not isinstance(prior, WriteClaim):
            raise TypeError("claims must hold WriteClaim values.")
        for existing in accepted:
            if existing.task_id == prior.task_id:
                continue
            for a in existing.scopes:
                for b in prior.scopes:
                    if _scopes_overlap(a, b):
                        raise SpecialistRejectedError(
                            "overlapping_write_scope",
                            f"task {prior.task_id!r} ({b}) overlaps task "
                            f"{existing.task_id!r} ({a}); conflicting scopes may not be held "
                            "concurrently (AC2).",
                            details={
                                "left_task": existing.task_id,
                                "right_task": prior.task_id,
                                "left_scope": a,
                                "right_scope": b,
                            },
                        )
        accepted.append(prior)
    return tuple(accepted)


# ---------------------------------------------------------------------------
# AC3 — durable, bounded dispatch/join ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchEntry:
    """One recorded state transition for a specialist task (AC3)."""

    task_id: str
    state: DispatchState
    at: float
    reason: str = ""
    output: HandoffArtifact | None = None
    consumed_steps: int = 0
    consumed_tool_calls: int = 0
    consumed_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _clean_id(self.task_id, "task_id"))
        if self.state not in DISPATCH_STATES:
            raise SpecialistRejectedError(
                "unknown_dispatch_state", f"Unknown dispatch state: {self.state!r}."
            )
        if not isinstance(self.at, (int, float)) or isinstance(self.at, bool):
            raise SpecialistRejectedError(
                "invalid_dispatch_time", "at must be a number."
            )
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        if self.state in {"failed", "cancelled"} and not self.reason:
            raise SpecialistRejectedError(
                "dispatch_outcome_without_reason",
                f"a {self.state} entry must state why; a silent failure is not a join (AC3).",
            )
        for name in ("consumed_steps", "consumed_tool_calls", "consumed_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SpecialistRejectedError(
                    "invalid_dispatch_consumption",
                    f"{name} must be a non-negative integer.",
                )
        if self.output is not None and not isinstance(self.output, HandoffArtifact):
            raise TypeError("output must be a HandoffArtifact.")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "at": round(float(self.at), 6),
            "reason": self.reason,
            "output": self.output.to_mapping() if self.output is not None else None,
            "consumed_steps": self.consumed_steps,
            "consumed_tool_calls": self.consumed_tool_calls,
            "consumed_tokens": self.consumed_tokens,
        }


@dataclass(frozen=True, slots=True)
class TaskJoin:
    """The reconciled fate of one specialist task, rebuilt from its entries."""

    task_id: str
    state: DispatchState
    joined: bool
    discarded: bool
    reason: str = ""
    output: HandoffArtifact | None = None
    consumed_steps: int = 0
    consumed_tool_calls: int = 0
    consumed_tokens: int = 0
    reconciled_after_restart: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state,
            "joined": self.joined,
            "discarded": self.discarded,
            "reason": self.reason,
            "output": self.output.to_mapping() if self.output is not None else None,
            "consumed_steps": self.consumed_steps,
            "consumed_tool_calls": self.consumed_tool_calls,
            "consumed_tokens": self.consumed_tokens,
            "reconciled_after_restart": self.reconciled_after_restart,
        }


@dataclass(frozen=True, slots=True)
class JoinLedger:
    """Append-only, bounded record of dispatch and join transitions (AC3).

    The ledger is deliberately in-memory and value-typed: durability belongs to
    ``child_delegation.ChildDelegationStore``, which already persists delegation
    state atomically. This type exists so a restart can be *reconciled* from a
    loaded entry list and so an unbounded ledger is refused rather than grown.
    """

    entries: tuple[DispatchEntry, ...] = ()

    def __post_init__(self) -> None:
        items = tuple(self.entries)
        if len(items) > MAX_LEDGER_ENTRIES:
            raise SpecialistRejectedError(
                "join_ledger_exceeds_bound",
                f"a ledger of {len(items)} entries exceeds the bound of {MAX_LEDGER_ENTRIES}; "
                "an unbounded join is not a bounded join (AC3).",
            )
        for item in items:
            if not isinstance(item, DispatchEntry):
                raise TypeError("entries must hold DispatchEntry values.")
        object.__setattr__(self, "entries", items)

    def record(self, entry: DispatchEntry) -> JoinLedger:
        """Append one transition, refusing an illegal one for the current state."""
        if not isinstance(entry, DispatchEntry):
            raise TypeError("entry must be a DispatchEntry.")
        current = self.state_of(entry.task_id)
        if current in TERMINAL_STATES:
            raise SpecialistRejectedError(
                "transition_from_terminal_state",
                f"task {entry.task_id!r} is already {current}; a joined task cannot be "
                "re-dispatched or re-joined (AC3).",
            )
        if (
            entry.state == "dispatched"
            and current is not None
            and current == "dispatched"
        ):
            raise SpecialistRejectedError(
                "duplicate_dispatch",
                f"task {entry.task_id!r} is already dispatched; one task has one dispatch (AC3).",
            )
        if (
            entry.state in {"completed", "failed", "cancelled"}
            and current != "dispatched"
        ):
            raise SpecialistRejectedError(
                "transition_without_dispatch",
                f"task {entry.task_id!r} cannot become {entry.state!r} without a dispatch (AC3).",
            )
        if entry.state in TERMINAL_STATES and current not in {
            "completed",
            "failed",
            "cancelled",
        }:
            raise SpecialistRejectedError(
                "join_without_settled_outcome",
                f"task {entry.task_id!r} cannot be joined from state {current!r}; an unfinished "
                "task is not joinable (AC3).",
            )
        return JoinLedger(entries=self.entries + (entry,))

    def state_of(self, task_id: str) -> DispatchState | None:
        for entry in reversed(self.entries):
            if entry.task_id == task_id:
                return entry.state
        return None

    def open_task_ids(self) -> tuple[str, ...]:
        """Tasks still dispatched with no settled outcome — the restart worklist."""
        order: list[str] = []
        latest: dict[str, DispatchState] = {}
        for entry in self.entries:
            if entry.task_id not in latest:
                order.append(entry.task_id)
            latest[entry.task_id] = entry.state
        return tuple(t for t in order if latest[t] == "dispatched")

    def reconcile(self) -> tuple[TaskJoin, ...]:
        """Rebuild every task's join from entries alone, for use after a restart."""
        order: list[str] = []
        latest: dict[str, DispatchEntry] = {}
        consumed: dict[str, list[int]] = {}
        outputs: dict[str, HandoffArtifact] = {}
        reconciled: set[str] = set()
        for entry in self.entries:
            if entry.task_id not in latest:
                order.append(entry.task_id)
                consumed[entry.task_id] = [0, 0, 0]
            latest[entry.task_id] = entry
            bucket = consumed[entry.task_id]
            bucket[0] = max(bucket[0], entry.consumed_steps)
            bucket[1] = max(bucket[1], entry.consumed_tool_calls)
            bucket[2] = max(bucket[2], entry.consumed_tokens)
            # The output is produced by the settled outcome; a terminal join entry
            # carries the disposition, not a second copy of the artifact. Keeping
            # only the latest entry's output would discard the very thing being
            # joined, so the most recent non-empty output is carried forward.
            if entry.output is not None:
                outputs[entry.task_id] = entry.output
            if entry.reason == "restart_reconciliation":
                reconciled.add(entry.task_id)
        joins: list[TaskJoin] = []
        for task_id in order:
            entry = latest[task_id]
            steps, tools, tokens = consumed[task_id]
            joins.append(
                TaskJoin(
                    task_id=task_id,
                    state=entry.state,
                    joined=entry.state in TERMINAL_STATES,
                    discarded=entry.state == "joined_discarded",
                    reason=entry.reason,
                    output=outputs.get(task_id),
                    consumed_steps=steps,
                    consumed_tool_calls=tools,
                    consumed_tokens=tokens,
                    reconciled_after_restart=task_id in reconciled,
                )
            )
        return tuple(joins)

    def after_restart(self, *, now: float) -> JoinLedger:
        """Fail open tasks closed: an unjoinable dispatch becomes a recorded cancel.

        A process that died with a task in flight must not leave that task looking
        alive. Each still-dispatched task is cancelled with a stated reason, so the
        join is durable and bounded instead of inferred (AC3).
        """
        ledger = self
        for task_id in self.open_task_ids():
            ledger = ledger.record(
                DispatchEntry(
                    task_id=task_id,
                    state="cancelled",
                    at=now,
                    reason="restart_reconciliation",
                )
            )
        return ledger

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": BOUNDED_SPECIALIST_SCHEMA_VERSION,
            "entry_count": len(self.entries),
            "max_entries": MAX_LEDGER_ENTRIES,
            "entries": [e.to_mapping() for e in self.entries],
            "joins": [j.to_mapping() for j in self.reconcile()],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> JoinLedger:
        entries: list[DispatchEntry] = []
        for item in raw.get("entries", ()):
            output_raw = item.get("output")
            entries.append(
                DispatchEntry(
                    task_id=str(item["task_id"]),
                    state=str(item["state"]),  # type: ignore[arg-type]
                    at=float(item["at"]),
                    reason=str(item.get("reason") or ""),
                    output=(
                        HandoffArtifact(
                            artifact_id=str(output_raw["artifact_id"]),
                            version_sha256=str(output_raw["version_sha256"]),
                            produced_by=str(output_raw.get("produced_by") or ""),
                            label=str(output_raw.get("label") or ""),
                        )
                        if isinstance(output_raw, Mapping)
                        else None
                    ),
                    consumed_steps=int(item.get("consumed_steps", 0)),
                    consumed_tool_calls=int(item.get("consumed_tool_calls", 0)),
                    consumed_tokens=int(item.get("consumed_tokens", 0)),
                )
            )
        return cls(entries=tuple(entries))

    def digest(self) -> str:
        return _canonical_sha256(self.to_mapping())


# ---------------------------------------------------------------------------
# AC4 — reviewer receives a brief and evidence, never an endorsement request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewerBrief:
    """What a reviewer is given: the work, its evidence, and open questions (AC4).

    The brief carries no verdict field and no requested conclusion, so "approve
    this" is not expressible. ``assert_no_endorsement_request`` refuses text that
    tries to smuggle the demand in prose instead.
    """

    brief_id: str
    reviewer_task_id: str
    builder_task_id: str
    summary: str
    evidence: tuple[str, ...]
    open_questions: tuple[str, ...]
    artifacts: tuple[HandoffArtifact, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "brief_id", _clean_id(self.brief_id, "brief_id"))
        object.__setattr__(
            self,
            "reviewer_task_id",
            _clean_id(self.reviewer_task_id, "reviewer_task_id"),
        )
        object.__setattr__(
            self, "builder_task_id", _clean_id(self.builder_task_id, "builder_task_id")
        )
        if self.reviewer_task_id == self.builder_task_id:
            raise SpecialistRejectedError(
                "reviewer_may_not_review_itself",
                "a reviewer cannot be the builder it reviews (AC4).",
            )
        object.__setattr__(self, "summary", _non_empty(self.summary, "summary"))
        evidence = tuple(str(e).strip() for e in self.evidence if str(e).strip())
        if not evidence:
            raise SpecialistRejectedError(
                "reviewer_brief_without_evidence",
                "a reviewer brief must carry evidence; a bare instruction is not a brief (AC4).",
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(
            self, "open_questions", tuple(str(q).strip() for q in self.open_questions)
        )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        assert_no_endorsement_request(self.summary, field="summary")
        for item in evidence:
            assert_no_endorsement_request(item, field="evidence")
        for question in self.open_questions:
            assert_no_endorsement_request(question, field="open_questions")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "reviewer_task_id": self.reviewer_task_id,
            "builder_task_id": self.builder_task_id,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "open_questions": list(self.open_questions),
            "artifacts": [a.to_mapping() for a in self.artifacts],
            "requests_endorsement": False,
            "verdict_is_agent_decided": False,
        }


#: Phrases that turn a brief into an instruction to endorse. Matching is
#: case-insensitive and deliberately blunt: a false refusal costs a rephrase,
#: while a missed one silently converts review into rubber-stamping (AC4).
_ENDORSEMENT_PATTERNS: tuple[str, ...] = (
    "approve this",
    "approve it",
    "sign off",
    "sign-off",
    "just confirm",
    "confirm the work is correct",
    "no need to review",
    "rubber stamp",
    "agree with the builder",
    "endorse",
    "mark it approved",
    "say it passes",
)


def assert_no_endorsement_request(text: str, *, field: str) -> None:
    lowered = str(text).lower()
    for pattern in _ENDORSEMENT_PATTERNS:
        if pattern in lowered:
            raise SpecialistRejectedError(
                "reviewer_brief_requests_endorsement",
                f"{field} asks the reviewer to endorse the builder ({pattern!r}); a reviewer "
                "receives a brief and evidence, not an instruction to agree (AC4).",
                details={"field": field, "matched": pattern},
            )


def build_reviewer_brief(
    *,
    brief_id: str,
    reviewer_task_id: str,
    builder: SpecialistTask,
    builder_output: HandoffArtifact,
    evidence: Sequence[str],
    open_questions: Sequence[str] = (),
    summary: str | None = None,
) -> ReviewerBrief:
    """Assemble a reviewer brief from a finished builder task and its evidence."""
    if not isinstance(builder, SpecialistTask):
        raise TypeError("builder must be a SpecialistTask.")
    if not isinstance(builder_output, HandoffArtifact):
        raise TypeError("builder_output must be a HandoffArtifact.")
    return ReviewerBrief(
        brief_id=brief_id,
        reviewer_task_id=reviewer_task_id,
        builder_task_id=builder.task_id,
        summary=summary or f"Review the work for: {builder.objective}",
        evidence=tuple(evidence),
        open_questions=tuple(open_questions),
        artifacts=tuple(builder.inputs) + (builder_output,),
    )


# ---------------------------------------------------------------------------
# AC5 — logical parallelism is not concurrent local inference
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParallelismPlan:
    """Declared logical parallelism, with an explicit claim about local inference.

    Specialist tasks may be *planned* in parallel, queued, resumed or joined
    independently. That says nothing about how many models may decode at once,
    which is owned by the runtime's scheduler and admission contracts — not by
    this module and not by the number of open tasks (AC5).
    """

    logical_parallelism: int
    concurrent_local_inference: int = 1
    local_inference_claimed: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.logical_parallelism, bool) or not isinstance(
            self.logical_parallelism, int
        ):
            raise SpecialistRejectedError(
                "invalid_parallelism_plan", "logical_parallelism must be an integer."
            )
        if self.logical_parallelism < 1:
            raise SpecialistRejectedError(
                "invalid_parallelism_plan", "logical_parallelism must be at least 1."
            )
        if isinstance(self.concurrent_local_inference, bool) or not isinstance(
            self.concurrent_local_inference, int
        ):
            raise SpecialistRejectedError(
                "invalid_parallelism_plan",
                "concurrent_local_inference must be an integer.",
            )
        if self.concurrent_local_inference < 1:
            raise SpecialistRejectedError(
                "invalid_parallelism_plan",
                "concurrent_local_inference must be at least 1 (a plan that decodes nothing "
                "cannot run).",
            )
        if not isinstance(self.local_inference_claimed, bool):
            raise SpecialistRejectedError(
                "invalid_parallelism_plan", "local_inference_claimed must be a boolean."
            )
        if self.local_inference_claimed and self.concurrent_local_inference > 1:
            raise SpecialistRejectedError(
                "concurrent_local_inference_claimed",
                "logical parallelism does not imply concurrent local inference; a single local "
                "runtime decodes one request at a time and the scheduler owns concurrency (AC5).",
                details={"logical_parallelism": self.logical_parallelism},
            )

    @property
    def decodes_concurrently(self) -> bool:
        return self.concurrent_local_inference > 1

    def to_mapping(self) -> dict[str, Any]:
        return {
            "logical_parallelism": self.logical_parallelism,
            "concurrent_local_inference": self.concurrent_local_inference,
            "logical_parallelism_implies_concurrent_inference": False,
            "local_inference_claimed": self.local_inference_claimed,
            "decodes_concurrently": self.decodes_concurrently,
            "scheduler_ownership": "runtime admission and scheduler contracts",
        }


# ---------------------------------------------------------------------------
# AC6 — retain a team only where measurement says it wins
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TopologyDecision:
    """Which topology is in force, and the measurement that decided it.

    ``single_agent`` needs no measurement: it is the default and the baseline, so
    it is always in force unless a measured comparison replaces it. A
    ``specialists`` decision cannot be constructed without measurement.
    """

    topology: str
    retained: bool
    reason: str
    measured: bool
    quality_delta: float | None = None
    cost_delta: float | None = None
    latency_delta: float | None = None
    interventions_delta: float | None = None

    def __post_init__(self) -> None:
        if self.topology not in TOPOLOGIES:
            raise SpecialistRejectedError(
                "unknown_topology", f"Unknown topology: {self.topology!r}."
            )
        object.__setattr__(self, "reason", _non_empty(self.reason, "reason"))
        if not isinstance(self.retained, bool) or not isinstance(self.measured, bool):
            raise SpecialistRejectedError(
                "invalid_topology_decision",
                "retained and measured must be booleans (a string 'yes' is not a decision).",
            )
        if self.topology != "single_agent" and not self.measured:
            raise SpecialistRejectedError(
                "retention_without_measurement",
                f"topology {self.topology!r} may only be put in force on measured evidence; an "
                "unmeasured team is kept by assumption, not by trade-off (AC6).",
            )
        if self.topology == "single_agent" and not self.retained:
            raise SpecialistRejectedError(
                "cannot_discard_the_baseline",
                "single_agent is the baseline; there is nothing simpler to fall back to.",
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "retained": self.retained,
            "reason": self.reason,
            "measured": self.measured,
            "quality_delta": self.quality_delta,
            "cost_delta": self.cost_delta,
            "latency_delta": self.latency_delta,
            "interventions_delta": self.interventions_delta,
        }


def _delta(specialists: Variability | None, single: Variability | None) -> float | None:
    if specialists is None or single is None:
        return None
    return round(float(specialists.median) - float(single.median), 6)


def decide_topology(
    *,
    design: EvaluationDesign,
    results: Sequence[StrategyResult],
    metrics_by_strategy: Mapping[str, Mapping[str, Variability]] | None = None,
    observed_source_hashes: Mapping[str, str] | None = None,
    budget_tolerance: float | None = None,
) -> TopologyDecision:
    """Decide whether a team beats the simpler baseline, using measured evidence.

    Defaults to ``single_agent``. A team is retained only when the comparison is
    trustworthy *and* the specialists measurably improve verified quality without
    a worse cost, latency or intervention median. Anything unmeasured,
    unjustified or incomparable keeps the simpler execution (AC6).

    ``metrics_by_strategy`` is the caller's measured operational evidence, keyed
    by strategy and then by ``cost`` / ``latency_ms`` / ``interventions``. Build
    it with ``challenger_evaluation.measure_metrics``; this module does not invent
    numbers to fill a gap.
    """
    if not isinstance(design, EvaluationDesign):
        raise TypeError("design must be an EvaluationDesign value.")
    try:
        report = build_report(
            design=design,
            results=results,
            observed_source_hashes=observed_source_hashes,
            budget_tolerance=budget_tolerance,
        )
    except ChallengerEvaluationError as exc:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=False,
            reason=f"comparison is not usable ({exc}); the simpler execution is retained (AC6).",
        )

    by_strategy = {r.strategy: r for r in results}
    specialists = by_strategy.get("specialists")
    single = by_strategy.get("single_agent")
    if specialists is None or single is None:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=False,
            reason=(
                "no matched single_agent and specialists results; nothing measured a trade-off (AC6)."
            ),
        )

    if not report.is_trustworthy:
        blockers = "; ".join(report.blockers) or "unspecified"
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=False,
            reason=(
                f"the comparison is not trustworthy ({blockers}); a team is not retained on "
                "evidence that does not hold up (AC6)."
            ),
        )

    q_delta = _delta(specialists.quality, single.quality)
    if q_delta is None:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=False,
            reason=(
                "verified quality is not measured for both strategies; no trade-off exists yet (AC6)."
            ),
        )

    measured_metrics = metrics_by_strategy or {}
    op_deltas: dict[str, float | None] = {}
    for name in ("cost", "latency_ms", "interventions"):
        op_deltas[name] = _delta(
            measured_metrics.get("specialists", {}).get(name),
            measured_metrics.get("single_agent", {}).get(name),
        )
    cost_delta = op_deltas["cost"]
    latency_delta = op_deltas["latency_ms"]
    interventions_delta = op_deltas["interventions"]

    if q_delta <= 0.0:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=True,
            reason=(
                f"specialists do not improve verified quality (median delta {q_delta}); "
                "the simpler execution is retained (AC6)."
            ),
            quality_delta=q_delta,
            cost_delta=cost_delta,
            latency_delta=latency_delta,
            interventions_delta=interventions_delta,
        )

    unmeasured = sorted(name for name, value in op_deltas.items() if value is None)
    if unmeasured:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=True,
            reason=(
                f"specialists improve verified quality (median delta {q_delta}), but "
                + ", ".join(unmeasured)
                + " were not measured, so no better trade-off is shown; the simpler execution is "
                "retained (AC6)."
            ),
            quality_delta=q_delta,
            cost_delta=cost_delta,
            latency_delta=latency_delta,
            interventions_delta=interventions_delta,
        )

    worsened = {
        name: value
        for name, value in op_deltas.items()
        if value is not None and value > 0.0
    }
    if worsened:
        return TopologyDecision(
            topology="single_agent",
            retained=True,
            measured=True,
            reason=(
                "specialists improve quality but cost more in "
                + ", ".join(sorted(worsened))
                + "; the trade-off is not demonstrably better, so the simpler execution is "
                "retained (AC6)."
            ),
            quality_delta=q_delta,
            cost_delta=cost_delta,
            latency_delta=latency_delta,
            interventions_delta=interventions_delta,
        )

    return TopologyDecision(
        topology="specialists",
        retained=True,
        measured=True,
        reason=(
            f"specialists improve verified quality (median delta {q_delta}) with no worse cost, "
            "latency or intervention median; the team is retained on this measurement (AC6)."
        ),
        quality_delta=q_delta,
        cost_delta=cost_delta,
        latency_delta=latency_delta,
        interventions_delta=interventions_delta,
    )


# ---------------------------------------------------------------------------
# Team assembly — the one place the ACs meet
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecialistTeam:
    """A validated team: bounded tasks, disjoint write claims, bounded ledger.

    Construction is the join point of AC1, AC2 and AC5: every member's authority
    and budget is a strict parent subset, write scopes are pairwise disjoint, and
    the declared parallel plan never claims concurrent local inference.
    """

    team_id: str
    parent_task_id: str
    tasks: tuple[SpecialistTask, ...]
    claims: tuple[WriteClaim, ...]
    plan: ParallelismPlan

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _clean_id(self.team_id, "team_id"))
        object.__setattr__(
            self, "parent_task_id", _clean_id(self.parent_task_id, "parent_task_id")
        )
        tasks = tuple(self.tasks)
        if not tasks:
            raise SpecialistRejectedError(
                "empty_specialist_team",
                "a team must contain at least one specialist task.",
            )
        seen: set[str] = set()
        for task in tasks:
            if not isinstance(task, SpecialistTask):
                raise TypeError("tasks must hold SpecialistTask values.")
            if task.task_id in seen:
                raise SpecialistRejectedError(
                    "duplicate_specialist_task", f"task {task.task_id!r} appears twice."
                )
            seen.add(task.task_id)
        object.__setattr__(self, "tasks", tasks)
        claims = tuple(self.claims)
        assert_scopes_disjoint(claims)
        writers = {c.task_id for c in claims}
        for claim in claims:
            if claim.task_id not in seen:
                raise SpecialistRejectedError(
                    "claim_for_unknown_task",
                    f"write claim {claim.claim_id!r} names task {claim.task_id!r}, which is not in "
                    "the team (AC2).",
                )
        for task in tasks:
            if task.role in ADVISORY_ROLES and task.task_id in writers:
                raise SpecialistRejectedError(
                    "advisory_role_cannot_write",
                    f"advisory task {task.task_id!r} holds a write claim (AC4).",
                )
            if (
                task.role in WRITING_ROLES
                and task.write_scopes
                and task.task_id not in writers
            ):
                raise SpecialistRejectedError(
                    "write_scope_without_claim",
                    f"task {task.task_id!r} declares write scopes but holds no claim; an "
                    "unclaimed scope can be overwritten by anyone (AC2).",
                )
        object.__setattr__(self, "claims", claims)
        if not isinstance(self.plan, ParallelismPlan):
            raise TypeError("plan must be a ParallelismPlan.")
        if self.plan.logical_parallelism < len(tasks):
            raise SpecialistRejectedError(
                "parallel_plan_understates_the_team",
                f"plan declares {self.plan.logical_parallelism} slots for {len(tasks)} tasks; "
                "the plan must describe the team it dispatches (AC5).",
            )

    @property
    def advisory_tasks(self) -> tuple[SpecialistTask, ...]:
        return tuple(t for t in self.tasks if t.role in ADVISORY_ROLES)

    @property
    def writing_tasks(self) -> tuple[SpecialistTask, ...]:
        return tuple(t for t in self.tasks if t.role in WRITING_ROLES)

    def total_reserved_tokens(self) -> int:
        return sum(int(t.budget.max_tokens) for t in self.tasks)

    def assert_reservations_fit_parent(self) -> None:
        """The team's combined reservations must leave the parent able to join.

        Each member is individually a strict subset, which is not enough: three
        children each taking half the parent would still over-subscribe it, and
        the join would have nothing left to run under (AC1).
        """
        if not self.tasks:
            return
        parent = self.tasks[0].parent
        totals = {
            "max_steps": sum(int(t.budget.max_steps) for t in self.tasks),
            "max_tool_calls": sum(int(t.budget.max_tool_calls) for t in self.tasks),
            "max_tokens": sum(int(t.budget.max_tokens) for t in self.tasks),
        }
        available = {
            "max_steps": parent.remaining_steps,
            "max_tool_calls": parent.remaining_tool_calls,
            "max_tokens": parent.remaining_tokens,
        }
        for name in totals:
            if totals[name] >= available[name]:
                raise SpecialistRejectedError(
                    "team_budget_oversubscribes_parent",
                    f"team reserves {totals[name]} of the parent's remaining {available[name]} "
                    f"in {name}; the join would have nothing left to run under (AC1).",
                    details={"dimension": name},
                )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": BOUNDED_SPECIALIST_SCHEMA_VERSION,
            "team_id": self.team_id,
            "parent_task_id": self.parent_task_id,
            "tasks": [
                t.to_mapping() for t in sorted(self.tasks, key=lambda t: t.task_id)
            ],
            "claims": [
                c.to_mapping() for c in sorted(self.claims, key=lambda c: c.claim_id)
            ],
            "plan": self.plan.to_mapping(),
            "reserved_tokens": self.total_reserved_tokens(),
        }

    def digest(self) -> str:
        return _canonical_sha256(self.to_mapping())


def assemble_team(
    *,
    team_id: str,
    parent_task_id: str,
    tasks: Sequence[SpecialistTask],
    claims: Sequence[WriteClaim] = (),
    plan: ParallelismPlan | None = None,
) -> SpecialistTeam:
    """Validate and return a team, checking the parent-wide budget reservation."""
    task_list = tuple(tasks)
    team = SpecialistTeam(
        team_id=team_id,
        parent_task_id=parent_task_id,
        tasks=task_list,
        claims=tuple(claims),
        plan=plan or ParallelismPlan(logical_parallelism=max(1, len(task_list))),
    )
    team.assert_reservations_fit_parent()
    return team


# ---------------------------------------------------------------------------
# Reviewability
# ---------------------------------------------------------------------------


def render_team(team: SpecialistTeam, *, ledger: JoinLedger | None = None) -> str:
    """Deterministic sorted-key JSON describing a team and its join state."""
    if not isinstance(team, SpecialistTeam):
        raise TypeError("team must be a SpecialistTeam.")
    payload: dict[str, Any] = {
        "schema_version": BOUNDED_SPECIALIST_SCHEMA_VERSION,
        "team": team.to_mapping(),
        "specialist_rules": {
            "authority_is_strict_parent_subset": True,
            "budget_is_strict_parent_subset": True,
            "handoffs_are_versioned": True,
            "overlapping_write_scopes_permitted": False,
            "unbounded_join_permitted": False,
            "reviewer_may_be_instructed_to_endorse": False,
            "logical_parallelism_implies_concurrent_inference": False,
            "topology_retained_without_measurement": False,
        },
        "does_not_prove": [
            "no model, runtime or provider was executed; every specialist output is a value the caller supplied",
            "a validated team is not evidence that the team is faster, cheaper or better than one agent",
            "joins are reconciled from records, not from a live process",
        ],
    }
    if ledger is not None:
        payload["joins"] = [j.to_mapping() for j in ledger.reconcile()]
    payload["team_sha256"] = team.digest()
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "BOUNDED_SPECIALIST_SCHEMA_VERSION",
    "MAX_LEDGER_ENTRIES",
    "ADVISORY_ROLES",
    "DISPATCH_STATES",
    "SPECIALIST_ROLES",
    "TOPOLOGIES",
    "WRITING_ROLES",
    "BoundedSpecialistError",
    "SpecialistRejectedError",
    "ParentBounds",
    "HandoffArtifact",
    "SpecialistTask",
    "WriteClaim",
    "DispatchEntry",
    "TaskJoin",
    "JoinLedger",
    "ReviewerBrief",
    "ParallelismPlan",
    "TopologyDecision",
    "SpecialistTeam",
    "assemble_team",
    "assert_no_endorsement_request",
    "assert_scopes_disjoint",
    "bind_artifact_identity",
    "build_reviewer_brief",
    "decide_topology",
    "render_team",
]
