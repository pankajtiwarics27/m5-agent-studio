from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import execution_budget
import runtime_persistence
from execution_budget import (
    BROWSER_WORKER,
    CONTAINED_EXECUTION,
    ISOLATION_STEPS,
    NATIVE_LIFECYCLE,
    BudgetError,
    BudgetExceeded,
    BudgetLimits,
    ExecutionBudget,
    ExecutionCancelled,
    IsolationAcceptance,
    IsolationEvidence,
    TaskBudgetSpec,
    TaskBudgetState,
    isolation_refusal,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# AP-007-F2.  Clearly fictional values: no isolated runtime is qualified in this
# repository and no acceptance is recorded outside tests.
_F2_DIGEST = "python@sha256:" + "ab" * 32
_F2_NOT_BEFORE = 1_800_000_000.0
_F2_ACCEPTANCE = IsolationAcceptance(
    decision_ref="test-fixture-acceptance-not-an-owner-decision",
    runtime="docker",
    runtime_version="0.0.0-test-fixture",
    image_digest=_F2_DIGEST,
    qualified_not_before=_F2_NOT_BEFORE,
    activated_steps=frozenset({CONTAINED_EXECUTION}),
)
_F2_PROVEN = IsolationEvidence(
    execution_mode="sandbox",
    admitted=True,
    qualified=True,
    backend_id="fixture-backend",
    runtime="docker",
    runtime_version="0.0.0-test-fixture",
    image_digest=_F2_DIGEST,
    qualification_id="fixture-qualification",
    qualified_at=_F2_NOT_BEFORE,
)


def test_token_cost_tool_and_retry_budgets_are_enforced() -> None:
    budget = ExecutionBudget(BudgetLimits(max_seconds=10, max_tokens=10, max_cost=1.0, max_tools=2, max_retries=1))
    budget.consume(tokens=5, cost=0.5, tools=1)
    assert budget.allow_retry() is True
    assert budget.allow_retry() is False
    with pytest.raises(BudgetExceeded, match="token"):
        budget.consume(tokens=6)

    fresh = ExecutionBudget(BudgetLimits(10, 10, 1.0, 2))
    fresh.consume(tools=2)
    with pytest.raises(BudgetExceeded, match="tool"):
        fresh.consume(tools=1)
    with pytest.raises(BudgetExceeded, match="cost"):
        fresh.consume(cost=1.1)


def test_time_budget_uses_monotonic_clock_and_cancellation_wins() -> None:
    now = [100.0]
    budget = ExecutionBudget(BudgetLimits(5, 10, 1.0, 1), clock=lambda: now[0])
    now[0] = 106.0
    with pytest.raises(BudgetExceeded, match="time"):
        budget.check()

    budget.cancel()
    with pytest.raises(ExecutionCancelled, match="cancelled"):
        budget.check()


def test_invalid_limits_and_usage_fail_closed() -> None:
    with pytest.raises(BudgetError, match="max_seconds"):
        BudgetLimits(0, 1, 1.0, 1)
    budget = ExecutionBudget(BudgetLimits(1, 1, 1.0, 1))
    with pytest.raises(BudgetError, match="increments"):
        budget.consume(tokens=-1)
    with pytest.raises(BudgetError, match="increments"):
        budget.consume(cost=-1)


# ---------------------------------------------------------------------------
# AP-007: bounded-stop limits, measurement gaps and the isolation requirement
# ---------------------------------------------------------------------------

# The persisted form of a default budget before AP-007, byte for byte.
_PRE_AP007_DEFAULT_BUDGET = (
    '{"cancel_requested":false,"elapsed_s":0.0,"exhausted_reason":"","pause_requested":false,'
    '"recoveries_used":0,"spec":{"max_elapsed_s":1800.0,"max_recoveries":3,"max_steps":25,'
    '"max_tokens":500000,"max_tool_calls":120},"steps_used":0,"tokens_used":0,"tool_calls_used":0}'
)


def test_ap007_undeclared_limits_leave_stored_budgets_and_their_identity_unchanged() -> None:
    """A budget that declares none of the new limits is stored exactly as before."""
    assert TaskBudgetState(spec=TaskBudgetSpec()).to_json() == _PRE_AP007_DEFAULT_BUDGET
    reread = TaskBudgetState.from_json(_PRE_AP007_DEFAULT_BUDGET)
    assert reread.to_json() == _PRE_AP007_DEFAULT_BUDGET
    assert reread.spec.declares_bounded_limits is False
    assert reread.spec.isolation_required is False
    assert reread.bounded_stop(now=1e12) is None

    # Counters at zero are omitted too, so writing one back does not change the document.
    assert set(json.loads(TaskBudgetState(spec=TaskBudgetSpec()).to_json())) == set(
        json.loads(_PRE_AP007_DEFAULT_BUDGET)
    )
    # Declaring a limit is an authority change: the captured budget identity moves.
    plain = runtime_persistence.compute_task_budget_identity(_PRE_AP007_DEFAULT_BUDGET)
    for declared in (
        TaskBudgetSpec(max_no_progress=3),
        TaskBudgetSpec(max_repair_attempts=0),
        TaskBudgetSpec(deadline_at=2_000_000_000.0),
        TaskBudgetSpec(max_memory_bytes=1),
        TaskBudgetSpec(max_storage_bytes=1),
        TaskBudgetSpec(isolation_required=True),
    ):
        document = TaskBudgetState(spec=declared).to_json()
        assert runtime_persistence.compute_task_budget_identity(document) != plain
        assert TaskBudgetState.from_json(document).spec == declared
    # Usage and measurements are not authority: they leave the identity alone.
    measured = dataclasses.replace(
        TaskBudgetState(spec=TaskBudgetSpec()), no_progress_count=2, memory_bytes_peak=9, unmeasured=("storage",)
    )
    assert runtime_persistence.compute_task_budget_identity(measured.to_json()) == plain
    assert TaskBudgetState.from_json(measured.to_json()) == measured


def test_ap007_bounded_limit_fields_name_exactly_the_new_spec_fields() -> None:
    """The HTTP boundary refuses these keys, so the list must not drift from the dataclass."""
    legacy = {"max_steps", "max_tool_calls", "max_tokens", "max_elapsed_s", "max_recoveries"}
    declared = {field.name for field in dataclasses.fields(TaskBudgetSpec)}
    assert set(execution_budget.BOUNDED_LIMIT_FIELDS) == declared - legacy
    assert len(set(execution_budget.BOUNDED_LIMIT_FIELDS)) == len(execution_budget.BOUNDED_LIMIT_FIELDS)
    full = TaskBudgetSpec(
        max_no_progress=1,
        max_repair_attempts=1,
        deadline_at=10.0,
        max_memory_bytes=1,
        max_storage_bytes=1,
        isolation_required=True,
    )
    assert set(full.to_dict()) == declared


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("max_no_progress", -1),
        ("max_no_progress", True),
        ("max_no_progress", "3"),
        ("max_no_progress", 1.5),
        ("max_repair_attempts", -1),
        ("max_repair_attempts", False),
        ("deadline_at", 0),
        ("deadline_at", -5.0),
        ("deadline_at", float("nan")),
        ("deadline_at", float("inf")),
        ("deadline_at", "tomorrow"),
        ("deadline_at", True),
        ("max_memory_bytes", 0),
        ("max_memory_bytes", "1GB"),
        ("max_storage_bytes", 0),
        ("max_storage_bytes", 2.5),
        ("isolation_required", 1),
        ("isolation_required", "true"),
        ("isolation_required", None),
    ],
)
def test_ap007_malformed_limits_fail_closed(field: str, bad: Any) -> None:
    """A limit that cannot be read is an error, never a silently absent limit."""
    with pytest.raises(BudgetError, match=field):
        TaskBudgetSpec(**{field: bad})
    with pytest.raises(BudgetError, match=field):
        TaskBudgetSpec.from_dict({field: bad})
    # The same value in a stored document makes the whole budget unreadable.
    with pytest.raises(BudgetError, match=field):
        TaskBudgetState.from_json(json.dumps({"spec": {"max_steps": 5, field: bad}}))


def test_ap007_malformed_counters_and_markers_fail_closed() -> None:
    for document in (
        {"no_progress_count": -1},
        {"repair_attempts_used": "2"},
        {"memory_bytes_peak": True},
        {"storage_bytes_peak": 1.5},
        {"unmeasured": "memory"},
        {"unmeasured": ["cpu"]},
        {"unmeasured": {"memory": True}},
    ):
        with pytest.raises(BudgetError):
            TaskBudgetState.from_json(json.dumps({"spec": {"max_steps": 5}, **document}))
    assert TaskBudgetState.from_json(
        json.dumps({"spec": {"max_steps": 5}, "unmeasured": ["storage", "memory", "storage"]})
    ).unmeasured == ("memory", "storage")


def test_ap007_bounded_stop_boundaries_are_exact() -> None:
    """At the limit work continues; one past it the stop is required, with a stable reason."""
    spec = TaskBudgetSpec(
        max_no_progress=2,
        max_repair_attempts=1,
        deadline_at=1_000.0,
        max_memory_bytes=100,
        max_storage_bytes=200,
    )
    at_limit = TaskBudgetState(
        spec=spec, no_progress_count=2, repair_attempts_used=1, memory_bytes_peak=100, storage_bytes_peak=200
    )
    assert at_limit.bounded_stop(now=999.99) is None
    assert at_limit.violation_reason(now=999.99) == ""

    def _code(**changed: Any) -> str:
        found = dataclasses.replace(at_limit, **changed).bounded_stop(now=999.99)
        assert found is not None
        return found[0]

    assert _code(no_progress_count=3) == "no_progress"
    assert _code(repair_attempts_used=2) == "repair_attempts"
    assert _code(memory_bytes_peak=101) == "memory"
    assert _code(storage_bytes_peak=201) == "storage"
    assert _code(unmeasured=("memory",)) == "measurement_unavailable"
    assert _code(unmeasured=("storage",)) == "measurement_unavailable"
    assert {_code(no_progress_count=3), _code(unmeasured=("memory",))} <= set(execution_budget.BOUNDED_STOP_CODES)

    # The deadline is reached at the instant itself, and its reason does not depend on "now".
    on_time = at_limit.bounded_stop(now=1_000.0)
    much_later = at_limit.bounded_stop(now=9_999.0)
    assert on_time == much_later == ("deadline", "deadline exceeded (deadline_at=1000.00)")
    assert at_limit.violation_reason(now=1_000.0) == "deadline exceeded (deadline_at=1000.00)"
    # With no time given the wall clock is used, so a forgetful caller still enforces it.
    assert at_limit.bounded_stop() is not None

    # A zero limit allows none: the first occurrence stops.
    strict = TaskBudgetState(spec=TaskBudgetSpec(max_no_progress=0, max_repair_attempts=0))
    assert strict.bounded_stop(now=1.0) is None
    assert dataclasses.replace(strict, no_progress_count=1).bounded_stop(now=1.0) is not None
    assert dataclasses.replace(strict, repair_attempts_used=1).bounded_stop(now=1.0) is not None

    # A marker for a ceiling that is not declared is not a gap.
    undeclared = TaskBudgetState(spec=TaskBudgetSpec(max_memory_bytes=100), unmeasured=("storage",))
    assert undeclared.measurement_gaps == ()
    assert undeclared.bounded_stop(now=1.0) is None


def _f2_refusal(evidence: Any) -> str:
    """Judge ``evidence`` against the fixture acceptance, for the contained-execution step."""
    return isolation_refusal(evidence, acceptance=_F2_ACCEPTANCE)


def _f2_admission(**backend: Any) -> SimpleNamespace:
    """A stand-in for an allowed, qualified sandbox admission decision on the pinned backend."""
    fields = {
        "backend_id": "fixture-backend",
        "runtime": "docker",
        "runtime_version": "0.0.0-test-fixture",
        "image_digest": _F2_DIGEST,
        **backend,
    }
    return SimpleNamespace(allowed=True, container_qualified=True, active_backend=SimpleNamespace(**fields))


def _f2_qualification(**fields: Any) -> SimpleNamespace:
    """A stand-in for a ``BackendQualificationRecord``: when it was recorded and when its run happened."""
    record = {
        "qualification_id": "fixture-qualification",
        "backend_id": "fixture-backend",
        "verdict": "qualified",
        "qualified_at": _F2_NOT_BEFORE,
        "report_generated_at": _F2_NOT_BEFORE,
        **fields,
    }
    return SimpleNamespace(**record)


def test_ap007_isolation_needs_literal_proof_and_has_no_host_fallback() -> None:
    proven = _F2_PROVEN
    assert _f2_refusal(proven) == ""

    refusals = {
        "nothing presented": _f2_refusal(None),
        "a mapping instead of evidence": _f2_refusal({"qualified": True}),
        "host mode": _f2_refusal(dataclasses.replace(proven, execution_mode="host")),
        "disabled mode": _f2_refusal(dataclasses.replace(proven, execution_mode="disabled")),
        "no mode": _f2_refusal(dataclasses.replace(proven, execution_mode="")),
        "admission refused": _f2_refusal(dataclasses.replace(proven, admitted=False, error_code="incident")),
        "not qualified": _f2_refusal(dataclasses.replace(proven, qualified=False)),
        "no backend named": _f2_refusal(dataclasses.replace(proven, backend_id="  ")),
        "truthy but not True": _f2_refusal(
            dataclasses.replace(proven, admitted=1, qualified="yes")  # type: ignore[arg-type]
        ),
    }
    for case, refusal in refusals.items():
        assert refusal.startswith("runtime isolation is required"), case
    assert "no isolation evidence was presented" in refusals["nothing presented"]
    assert "`host`" in refusals["host mode"]
    assert "incident" in refusals["admission refused"]

    # Admission output is adapted strictly: "allowed" alone never proves isolation.
    allowed_without_backend = SimpleNamespace(allowed=True, container_qualified=False, active_backend=None)
    adapted = IsolationEvidence.from_admission(allowed_without_backend, execution_mode="SANDBOX")
    assert (adapted.execution_mode, adapted.admitted, adapted.qualified, adapted.backend_id) == (
        "sandbox",
        True,
        False,
        "",
    )
    assert _f2_refusal(adapted)
    truthy = SimpleNamespace(allowed=1, container_qualified="yes", active_backend=SimpleNamespace(backend_id="b1"))
    assert _f2_refusal(IsolationEvidence.from_admission(truthy, execution_mode="sandbox"))
    assert _f2_refusal(IsolationEvidence.from_admission(object(), execution_mode="sandbox"))
    qualified = _f2_admission()
    record = _f2_qualification()
    proven_by_admission = IsolationEvidence.from_admission(qualified, execution_mode="sandbox", qualification=record)
    assert _f2_refusal(proven_by_admission) == ""
    assert _f2_refusal(IsolationEvidence.from_admission(qualified, execution_mode="host", qualification=record))
    # AP-007-F2: an allowed, qualified admission is not enough without its qualification record.
    without_record = IsolationEvidence.from_admission(qualified, execution_mode="sandbox")
    assert "names no qualification" in _f2_refusal(without_record)
    # The evidence record names no secret and no reason text when it is serialised.
    assert set(adapted.to_dict()) == {
        "execution_mode",
        "admitted",
        "qualified",
        "backend_id",
        "error_code",
        "runtime",
        "runtime_version",
        "image_digest",
        "qualification_id",
        "qualified_at",
        "step",
    }


def test_ap007_f2_nothing_is_accepted_without_a_recorded_acceptance() -> None:
    """Proof alone is never enough: what is accepted has to be recorded first."""
    for refusal in (isolation_refusal(_F2_PROVEN), isolation_refusal(_F2_PROVEN, acceptance=None)):
        assert refusal == "runtime isolation is required but no accepted isolation backend is recorded"
    # A look-alike is not an acceptance record.
    look_alikes: tuple[Any, ...] = (
        _F2_ACCEPTANCE.to_dict(),
        SimpleNamespace(**_F2_ACCEPTANCE.to_dict()),
        {"runtime": "docker"},
        True,
        "accepted",
    )
    for look_alike in look_alikes:
        assert "no accepted isolation backend is recorded" in isolation_refusal(_F2_PROVEN, acceptance=look_alike)
    # The record itself is what the proof is judged against.
    assert isolation_refusal(_F2_PROVEN, acceptance=_F2_ACCEPTANCE) == ""


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("runtime", "podman", "not the accepted runtime and image"),
        ("runtime", "", "not the accepted runtime and image"),
        ("runtime_version", "0.0.1-test-fixture", "not the accepted runtime and image"),
        ("runtime_version", "", "not the accepted runtime and image"),
        ("image_digest", "python@sha256:" + "cd" * 32, "not the accepted runtime and image"),
        # The same content named without its repository is a different string: no guessing.
        ("image_digest", "sha256:" + "ab" * 32, "not the accepted runtime and image"),
        ("image_digest", "python@sha256:" + "ab" * 6, "not the accepted runtime and image"),
        ("image_digest", "", "not the accepted runtime and image"),
        ("qualification_id", "", "names no qualification"),
        ("qualification_id", "   ", "names no qualification"),
        ("qualified_at", _F2_NOT_BEFORE - 0.001, "predates the recorded acceptance"),
        ("qualified_at", 0.0, "predates the recorded acceptance"),
        ("qualified_at", -1.0, "predates the recorded acceptance"),
        ("qualified_at", float("nan"), "predates the recorded acceptance"),
        ("qualified_at", float("inf"), "predates the recorded acceptance"),
        ("qualified_at", True, "predates the recorded acceptance"),
        ("qualified_at", str(_F2_NOT_BEFORE + 10), "predates the recorded acceptance"),
        ("qualified_at", None, "predates the recorded acceptance"),
        ("step", BROWSER_WORKER, "proves the `browser_worker` step, not `contained_execution`"),
        ("step", NATIVE_LIFECYCLE, "proves the `native_lifecycle` step, not `contained_execution`"),
        ("step", "", "proves the `unknown` step"),
        ("step", None, "proves the `unknown` step"),
    ],
)
def test_ap007_f2_proof_must_match_the_recorded_pin_and_a_fresh_qualification(
    field: str, value: Any, fragment: str
) -> None:
    """Answers 1 and 2: the pinned container runtime and image, and a qualification that is not older."""
    assert _f2_refusal(_F2_PROVEN) == ""
    refusal = _f2_refusal(dataclasses.replace(_F2_PROVEN, **{field: value}))
    assert refusal.startswith("runtime isolation is required")
    assert fragment in refusal


def test_ap007_f2_the_freshness_boundary_and_the_pin_comparison_are_exact() -> None:
    # A qualification taken exactly at the recorded time, or later, is fresh.
    assert _f2_refusal(dataclasses.replace(_F2_PROVEN, qualified_at=_F2_NOT_BEFORE)) == ""
    assert _f2_refusal(dataclasses.replace(_F2_PROVEN, qualified_at=_F2_NOT_BEFORE + 86_400)) == ""
    assert _f2_refusal(dataclasses.replace(_F2_PROVEN, qualified_at=int(_F2_NOT_BEFORE) + 1)) == ""
    # Only the runtime name is case-insensitive; surrounding whitespace is not a difference.
    padded = dataclasses.replace(_F2_PROVEN, runtime=" Docker ", runtime_version=" 0.0.0-test-fixture ")
    assert _f2_refusal(padded) == ""
    assert _f2_refusal(dataclasses.replace(_F2_PROVEN, runtime_version="0.0.0-TEST-FIXTURE"))
    assert _f2_refusal(dataclasses.replace(_F2_PROVEN, image_digest=_F2_DIGEST.upper()))
    # Moving the recorded time forward turns the same proof stale.
    later = dataclasses.replace(_F2_ACCEPTANCE, qualified_not_before=_F2_NOT_BEFORE + 1)
    assert "predates the recorded acceptance" in isolation_refusal(_F2_PROVEN, acceptance=later)


def test_ap007_f2_each_step_is_refused_until_it_is_recorded_and_has_evidence() -> None:
    """Answer 4: contained execution, then the browser worker, then the native lifecycle."""
    assert ISOLATION_STEPS == (CONTAINED_EXECUTION, BROWSER_WORKER, NATIVE_LIFECYCLE)

    nothing_activated = dataclasses.replace(_F2_ACCEPTANCE, activated_steps=frozenset())
    assert "`contained_execution` step is not activated" in isolation_refusal(_F2_PROVEN, acceptance=nothing_activated)

    # With contained execution recorded, the later steps are still refused.
    for step in (BROWSER_WORKER, NATIVE_LIFECYCLE):
        for evidence in (_F2_PROVEN, dataclasses.replace(_F2_PROVEN, step=step), None):
            refusal = isolation_refusal(evidence, acceptance=_F2_ACCEPTANCE, step=step)
            assert f"`{step}` step is not activated" in refusal

    # Recording a later step does not let contained-execution proof stand in for it,
    # and this build defines no evidence for the later steps at all.
    everything = dataclasses.replace(_F2_ACCEPTANCE, activated_steps=frozenset(ISOLATION_STEPS))
    assert isolation_refusal(_F2_PROVEN, acceptance=everything) == ""
    for step in (BROWSER_WORKER, NATIVE_LIFECYCLE):
        for evidence in (_F2_PROVEN, dataclasses.replace(_F2_PROVEN, step=step), None):
            refusal = isolation_refusal(evidence, acceptance=everything, step=step)
            assert refusal == f"runtime isolation is required but no evidence is defined for the `{step}` step"

    # A dispatch that names no known step is refused before anything else is read.
    unknown_steps: tuple[Any, ...] = ("", "host", "sandbox", "CONTAINED_EXECUTION", None, 1, True)
    for unknown in unknown_steps:
        assert "names no known activation step" in isolation_refusal(_F2_PROVEN, acceptance=everything, step=unknown)
        assert "names no known activation step" in isolation_refusal(_F2_PROVEN, step=unknown)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("decision_ref", ""),
        ("decision_ref", "   "),
        ("decision_ref", None),
        ("decision_ref", 7),
        ("decision_ref", "x" * 201),
        ("decision_ref", "two\nlines"),
        ("runtime", ""),
        ("runtime", "docker desktop"),
        ("runtime", "../docker"),
        ("runtime", None),
        ("runtime_version", ""),
        ("runtime_version", None),
        ("runtime_version", 29),
        ("image_digest", ""),
        ("image_digest", None),
        ("image_digest", "python:3.12-slim"),
        ("image_digest", "sha256:aa943ce41142"),
        ("image_digest", "python@sha256:" + "AB" * 32),
        ("image_digest", "python@md5:" + "ab" * 32),
        ("qualified_not_before", 0),
        ("qualified_not_before", -1.0),
        ("qualified_not_before", float("nan")),
        ("qualified_not_before", float("inf")),
        ("qualified_not_before", True),
        ("qualified_not_before", "1800000000"),
        ("qualified_not_before", None),
        ("activated_steps", frozenset({BROWSER_WORKER})),
        ("activated_steps", frozenset({NATIVE_LIFECYCLE})),
        ("activated_steps", frozenset({CONTAINED_EXECUTION, NATIVE_LIFECYCLE})),
        ("activated_steps", frozenset({BROWSER_WORKER, NATIVE_LIFECYCLE})),
        ("activated_steps", frozenset({"host"})),
        ("activated_steps", frozenset({CONTAINED_EXECUTION, "host"})),
        ("activated_steps", CONTAINED_EXECUTION),
        ("activated_steps", None),
        ("activated_steps", [1]),
        ("activated_steps", {CONTAINED_EXECUTION: True}),
    ],
)
def test_ap007_f2_a_malformed_acceptance_record_cannot_be_built(field: str, bad: Any) -> None:
    with pytest.raises(BudgetError):
        dataclasses.replace(_F2_ACCEPTANCE, **{field: bad})


def test_ap007_f2_an_acceptance_record_is_normalised_and_keeps_the_activation_order() -> None:
    record = IsolationAcceptance(
        decision_ref="  fixture-ref  ",
        runtime="Docker",
        runtime_version=" 0.0.0-test-fixture ",
        image_digest="sha256:" + "ab" * 32,
        qualified_not_before=1_800_000_000,
        activated_steps=[BROWSER_WORKER, CONTAINED_EXECUTION],  # type: ignore[arg-type]
    )
    assert (record.decision_ref, record.runtime, record.runtime_version) == (
        "fixture-ref",
        "docker",
        "0.0.0-test-fixture",
    )
    assert record.activated_steps == frozenset({CONTAINED_EXECUTION, BROWSER_WORKER})
    assert isinstance(record.qualified_not_before, float)
    assert record.to_dict() == {
        "decision_ref": "fixture-ref",
        "runtime": "docker",
        "runtime_version": "0.0.0-test-fixture",
        "image_digest": "sha256:" + "ab" * 32,
        "qualified_not_before": 1_800_000_000.0,
        "activated_steps": [CONTAINED_EXECUTION, BROWSER_WORKER],
    }
    # Each prefix of the order is a valid record; nothing recorded is one of them.
    for count in range(len(ISOLATION_STEPS) + 1):
        steps = frozenset(ISOLATION_STEPS[:count])
        assert dataclasses.replace(_F2_ACCEPTANCE, activated_steps=steps).activated_steps == steps
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.runtime = "podman"  # type: ignore[misc]


def test_ap007_f2_evidence_takes_a_qualification_only_for_the_active_backend() -> None:
    """A sandbox admission is adapted with the backend it names and the qualification of that backend."""
    decision = _f2_admission(runtime="Docker", runtime_version=" 0.0.0-test-fixture ")
    adapted = IsolationEvidence.from_admission(
        decision,
        execution_mode="sandbox",
        qualification=_f2_qualification(qualified_at=_F2_NOT_BEFORE + 900, report_generated_at=_F2_NOT_BEFORE + 0.5),
    )
    assert (adapted.runtime, adapted.runtime_version, adapted.image_digest) == (
        "docker",
        "0.0.0-test-fixture",
        _F2_DIGEST,
    )
    assert (adapted.qualification_id, adapted.qualified_at, adapted.step) == (
        "fixture-qualification",
        _F2_NOT_BEFORE + 0.5,
        CONTAINED_EXECUTION,
    )
    assert _f2_refusal(adapted) == ""

    unusable: dict[str, Any] = {
        "no record": None,
        "another backend's record": _f2_qualification(backend_id="other-backend"),
        "a record that names no backend": _f2_qualification(backend_id=""),
        "a failed verdict": _f2_qualification(verdict="failed"),
        "a truthy verdict": _f2_qualification(verdict=True),
        "no qualification id": _f2_qualification(qualification_id=""),
        "no record time": _f2_qualification(qualified_at=None),
        "no run time": _f2_qualification(report_generated_at=None),
        "a run time of zero": _f2_qualification(report_generated_at=0.0),
        "a time that is not a number": _f2_qualification(qualified_at=float("nan")),
        "a run time that is not a number": _f2_qualification(report_generated_at=float("inf")),
        "a boolean time": _f2_qualification(qualified_at=True),
        "an older qualification": _f2_qualification(
            qualified_at=_F2_NOT_BEFORE - 1, report_generated_at=_F2_NOT_BEFORE - 1
        ),
        # Answer 2: an old run recorded again today is still an old run.
        "an old run recorded later": _f2_qualification(
            qualified_at=_F2_NOT_BEFORE + 86_400, report_generated_at=_F2_NOT_BEFORE - 86_400
        ),
        # A record written before the run it claims to rest on is judged by the earlier time.
        "a record older than its run": _f2_qualification(
            qualified_at=_F2_NOT_BEFORE - 5, report_generated_at=_F2_NOT_BEFORE + 5
        ),
        "an unrelated object": object(),
    }
    for case, record in unusable.items():
        evidence = IsolationEvidence.from_admission(decision, execution_mode="sandbox", qualification=record)
        assert _f2_refusal(evidence).startswith("runtime isolation is required"), case

    stale = IsolationEvidence.from_admission(
        decision, execution_mode="sandbox", qualification=unusable["an old run recorded later"]
    )
    assert stale.qualified_at == _F2_NOT_BEFORE - 86_400
    assert "predates the recorded acceptance" in _f2_refusal(stale)

    # Without an active backend a qualification record proves nothing.
    no_backend = SimpleNamespace(allowed=True, container_qualified=True, active_backend=None)
    orphan = IsolationEvidence.from_admission(no_backend, execution_mode="sandbox", qualification=_f2_qualification())
    assert (orphan.backend_id, orphan.qualification_id, orphan.qualified_at) == ("", "", 0.0)
    assert _f2_refusal(orphan)
    # A backend other than the pinned one is adapted faithfully and then refused.
    other = IsolationEvidence.from_admission(
        _f2_admission(image_digest="python@sha256:" + "cd" * 32),
        execution_mode="sandbox",
        qualification=_f2_qualification(),
    )
    assert "not the accepted runtime and image" in _f2_refusal(other)


def test_ap007_budget_documents_are_never_rebuilt_field_by_field() -> None:
    """Every update copies the stored budget, so a counter added later cannot be dropped.

    A fresh budget may be built from a spec alone.  Rebuilding an existing one by
    listing its fields is the mistake this guards: it silently resets whatever the
    list forgot, including the counters that decide a bounded stop.
    """
    offenders: list[str] = []
    for name in ("execution_budget.py", "workflow_state.py", "app.py", "child_delegation.py"):
        tree = ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))
        parser_calls: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TaskBudgetState":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "from_json":
                        parser_calls = {id(call) for call in ast.walk(item) if isinstance(call, ast.Call)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in parser_calls:
                continue
            target = node.func
            called = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if called != "TaskBudgetState":
                continue
            if node.args or {keyword.arg for keyword in node.keywords} - {"spec"}:
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == []
