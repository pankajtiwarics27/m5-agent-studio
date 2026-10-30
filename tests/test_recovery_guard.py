"""M5-P2-05 tests: bounded recovery and stagnation detection.

The behaviours pinned here are the story's acceptance criteria:

* **no infinite retry loop** — identical failure evidence is detected, classified
  as stagnation, escalates to a durable pause, and retry/replan/rollback budgets
  refuse further attempts once spent;
* **failure memory and budgets survive a crash** — every attempt and counter lives
  in one atomically replaced document, so a new process reads the same memory back
  and cannot spend a budget without recording the attempt that spent it;
* **recovery requires changed evidence or conditions** — a recovery action whose
  evidence repeats a recorded failure is refused, and revisiting an abandoned
  state is classified as oscillation rather than progress.
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
import recovery_guard as rg
import workflow_state

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _evidence(
    *,
    revision: str = HEX_A,
    transcript: str = HEX_B,
    failure_code: str = "verification_test_failed_introduced",
    failure_ids: tuple[str, ...] = ("tests/test_calc.py::test_add",),
    environment: str = "",
) -> rg.EvidenceSnapshot:
    return rg.EvidenceSnapshot(
        revision_digest=revision,
        transcript_sha256=transcript,
        failure_fingerprint=rg.failure_fingerprint(failure_code, failure_ids),
        failure_code=failure_code,
        failure_ids=failure_ids,
        environment_digest=environment,
    )


@pytest.fixture()
def guard(tmp_path: Path) -> rg.RecoveryGuard:
    return rg.RecoveryGuard(tmp_path / "store")


# ---------------------------------------------------------------------------
# 1. Fingerprints and evidence deltas
# ---------------------------------------------------------------------------


def test_failure_fingerprint_is_order_independent_and_code_sensitive() -> None:
    first = rg.failure_fingerprint("code_a", ("tests/b.py::test_b", "tests/a.py::test_a"))
    second = rg.failure_fingerprint("code_a", ("tests/a.py::test_a", "tests/b.py::test_b"))
    other_code = rg.failure_fingerprint("code_b", ("tests/a.py::test_a", "tests/b.py::test_b"))
    fewer = rg.failure_fingerprint("code_a", ("tests/a.py::test_a",))

    assert first == second
    assert first != other_code
    assert first != fewer
    assert len(first) == 64


def test_evidence_key_separates_conditions_from_failures() -> None:
    base = _evidence()
    same_failure_new_revision = _evidence(revision=HEX_C)

    assert base.evidence_key != same_failure_new_revision.evidence_key
    assert base.conditions_digest != same_failure_new_revision.conditions_digest
    assert base.evidence_key == _evidence().evidence_key


def test_evidence_delta_reports_exactly_what_changed() -> None:
    previous = _evidence(failure_ids=("tests/a.py::test_a", "tests/b.py::test_b"))
    candidate = _evidence(transcript=HEX_C, failure_ids=("tests/b.py::test_b", "tests/c.py::test_c"))

    delta = rg.evidence_delta(previous, candidate)

    assert delta["revision_changed"] is False
    assert delta["transcript_changed"] is True
    assert delta["failures_added"] == ["tests/c.py::test_c"]
    assert delta["failures_removed"] == ["tests/a.py::test_a"]


def test_snapshot_rejects_a_malformed_digest_and_bounds_text() -> None:
    with pytest.raises(rg.RecoveryGuardError):
        rg.EvidenceSnapshot(revision_digest="not-a-digest")
    with pytest.raises(rg.RecoveryGuardError):
        rg.EvidenceSnapshot(failure_code="bad\x00code")

    bounded = rg.EvidenceSnapshot(failure_code="x" * 900)
    assert len(bounded.failure_code) == rg.MAX_TEXT


# ---------------------------------------------------------------------------
# 2. Durable failure memory
# ---------------------------------------------------------------------------


def test_attempt_history_is_durable_across_a_restart(guard: rg.RecoveryGuard, tmp_path: Path) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=10.0)
    guard.consume("task-1", "retry", now=11.0)

    restarted = rg.RecoveryGuard(tmp_path / "store")

    history = restarted.history("task-1")
    assert [record.sequence for record in history] == [1]
    assert history[0].kind == "verification"
    assert history[0].outcome == "failed"
    assert history[0].evidence.failure_ids == ("tests/test_calc.py::test_add",)
    assert restarted.budget("task-1").retries_used == 1


def test_records_are_private_and_atomic(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=10.0)

    directory = guard.directory
    record = directory / "task-1.json"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert not list(directory.glob("*.tmp"))
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["schema_version"] == rg.RECOVERY_SCHEMA_VERSION
    assert payload["task_id"] == "task-1"


def test_history_is_pruned_to_the_bound(guard: rg.RecoveryGuard) -> None:
    for index in range(rg.MAX_ATTEMPTS + 5):
        guard.record_attempt(
            "task-1",
            kind="verification",
            outcome="failed",
            evidence=_evidence(transcript=f"{index:064x}"),
            now=float(index),
        )

    history = guard.history("task-1")

    assert len(history) == rg.MAX_ATTEMPTS
    assert history[-1].sequence == rg.MAX_ATTEMPTS + 5


def test_unsafe_task_ids_corrupt_records_and_symlinks_are_refused(
    guard: rg.RecoveryGuard, tmp_path: Path
) -> None:
    for bad in ("../escape", "", "a" * 121, "with space"):
        with pytest.raises(rg.RecoveryGuardError):
            guard.history(bad)

    guard.directory.mkdir(parents=True, exist_ok=True)
    corrupt = guard.directory / "broken.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(rg.RecoveryGuardError):
        guard.history("broken")

    wrong_schema = guard.directory / "old.json"
    wrong_schema.write_text(json.dumps({"schema_version": 99, "task_id": "old", "attempts": []}), encoding="utf-8")
    with pytest.raises(rg.RecoveryGuardError):
        guard.history("old")

    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = guard.directory / "linked.json"
    os.symlink(target, link)
    with pytest.raises(rg.RecoveryGuardError):
        guard.history("linked")


# ---------------------------------------------------------------------------
# 3. Stagnation and oscillation
# ---------------------------------------------------------------------------


def test_first_attempt_is_not_stagnation_and_repeat_is(guard: rg.RecoveryGuard) -> None:
    _, first = guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    _, repeated = guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=2.0)
    _, changed = guard.record_attempt(
        "task-1",
        kind="verification",
        outcome="failed",
        evidence=_evidence(transcript=HEX_C),
        now=3.0,
    )

    assert first.state == rg.STAGNATION_FIRST
    assert first.escalate is False
    assert repeated.state == rg.STAGNATION_STAGNANT
    assert repeated.repeat_count == 2
    assert repeated.escalate is True
    assert changed.state == rg.STAGNATION_PROGRESS
    assert changed.escalate is False


def test_revisiting_an_abandoned_state_is_oscillation(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(transcript=HEX_A), now=1.0)
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(transcript=HEX_B), now=2.0)

    _, verdict = guard.record_attempt(
        "task-1", kind="verification", outcome="failed", evidence=_evidence(transcript=HEX_A), now=3.0
    )

    assert verdict.state == rg.STAGNATION_OSCILLATING
    assert verdict.escalate is True
    assert "oscillation" in verdict.reason


def test_verified_attempts_are_not_failure_memory(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    guard.record_attempt("task-1", kind="verification", outcome="verified", evidence=_evidence(), now=2.0)

    _, verdict = guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=3.0)

    # The duplicate is still a duplicate of the recorded failure, but the verified
    # attempt in between proves progress happened, so the loop is not silent.
    assert verdict.state in {rg.STAGNATION_STAGNANT, rg.STAGNATION_OSCILLATING}
    assert guard.summary("task-1")["failure_count"] == 2


def test_escalation_requests_a_durable_safe_pause(guard: rg.RecoveryGuard, tmp_path: Path) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=2.0)

    restarted = rg.RecoveryGuard(tmp_path / "store")
    budget = restarted.budget("task-1")

    assert budget.pause_requested is True
    assert "identical failure evidence" in budget.pause_reason


def test_identical_attempt_tolerance_is_configurable(tmp_path: Path) -> None:
    guard = rg.RecoveryGuard(
        tmp_path / "store",
        limits=rg.RecoveryLimits(max_identical_attempts=3),
    )
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    _, second = guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=2.0)
    _, third = guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=3.0)

    assert second.escalate is False
    assert third.repeat_count == 3
    assert third.escalate is True
    assert guard.budget("task-1").pause_requested is True


# ---------------------------------------------------------------------------
# 4. Budgets
# ---------------------------------------------------------------------------


def test_budgets_count_independently_and_persist(guard: rg.RecoveryGuard, tmp_path: Path) -> None:
    guard.consume("task-1", "retry", now=1.0)
    guard.consume("task-1", "retry", now=2.0)
    guard.consume("task-1", "rollback", now=3.0)

    budget = rg.RecoveryGuard(tmp_path / "store").budget("task-1")

    assert (budget.retries_used, budget.replans_used, budget.rollbacks_used) == (2, 0, 1)
    assert budget.violation_reason() == ""


def test_exhausted_budget_is_persisted_before_refusing(tmp_path: Path) -> None:
    guard = rg.RecoveryGuard(tmp_path / "store", limits=rg.RecoveryLimits(max_rollbacks=1))
    guard.consume("task-1", "rollback", now=1.0)

    with pytest.raises(rg.RecoveryRejected) as excinfo:
        guard.consume("task-1", "rollback", now=2.0)

    assert "rollback budget exhausted (1/1)" in str(excinfo.value)
    budget = rg.RecoveryGuard(tmp_path / "store").budget("task-1")
    assert budget.rollbacks_used == 1
    assert budget.pause_requested is True


def test_unknown_budget_kind_is_refused(guard: rg.RecoveryGuard) -> None:
    with pytest.raises(rg.RecoveryGuardError):
        guard.consume("task-1", "sing")


# ---------------------------------------------------------------------------
# 5. Recovery requires changed evidence
# ---------------------------------------------------------------------------


def test_recovery_with_unchanged_evidence_is_refused_and_pauses(guard: rg.RecoveryGuard, tmp_path: Path) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)

    with pytest.raises(rg.RecoveryRejected) as excinfo:
        guard.require_progress("task-1", _evidence(), action="retry")

    assert "retry refused" in str(excinfo.value)
    assert "Changed evidence or conditions are required" in str(excinfo.value)
    assert rg.RecoveryGuard(tmp_path / "store").budget("task-1").pause_requested is True


def test_recovery_with_changed_conditions_is_allowed(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)

    verdict = guard.require_progress("task-1", _evidence(revision=HEX_C), action="replan")

    assert verdict.state == rg.STAGNATION_PROGRESS
    assert verdict.escalate is False


def test_recovery_with_no_recorded_failure_is_allowed(guard: rg.RecoveryGuard) -> None:
    verdict = guard.require_progress("fresh-task", _evidence(), action="retry")

    assert verdict.state == rg.STAGNATION_FIRST


def test_oscillating_recovery_is_refused(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(transcript=HEX_A), now=1.0)
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(transcript=HEX_B), now=2.0)

    with pytest.raises(rg.RecoveryRejected):
        guard.require_progress("task-1", _evidence(transcript=HEX_A), action="recovery")


# ---------------------------------------------------------------------------
# 6. Read-only view
# ---------------------------------------------------------------------------


def test_summary_exposes_counters_verdict_and_recent_attempts(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    guard.record_attempt("task-1", kind="recovery", outcome="refused", evidence=_evidence(), now=2.0)
    guard.consume("task-1", "retry", now=3.0)

    summary = guard.summary("task-1", limit=5)

    assert summary["attempt_count"] == 2
    assert summary["failure_count"] == 1
    assert summary["budget"]["retries_used"] == 1
    assert summary["latest_verdict"]["state"] == rg.STAGNATION_FIRST
    assert [attempt["kind"] for attempt in summary["recent_attempts"]] == ["verification", "recovery"]
    assert summary["pause_requested"] is False


def test_summary_of_an_unknown_task_is_empty_but_valid(guard: rg.RecoveryGuard) -> None:
    summary = guard.summary("never-seen")

    assert summary["attempt_count"] == 0
    assert summary["budget"]["limits"]["max_retries"] == 3
    assert summary["latest_verdict"]["state"] == rg.STAGNATION_FIRST


def test_clear_removes_the_record(guard: rg.RecoveryGuard) -> None:
    guard.record_attempt("task-1", kind="verification", outcome="failed", evidence=_evidence(), now=1.0)
    guard.clear("task-1")

    assert guard.history("task-1") == ()
    assert guard.summary("task-1")["attempt_count"] == 0


# ---------------------------------------------------------------------------
# 7. Completion gate records attempts and escalates to a safe pause
# ---------------------------------------------------------------------------


@pytest.fixture()
def studio_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db_path = tmp_path / "p2_05.sqlite"
    store = tmp_path / "harness_store"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "t" * 40)
    studio.init_db()
    sm = workflow_state.DurableTaskStateMachine(
        studio.STORAGE, db_path, recovery_memory=studio.get_recovery_guard()
    )
    sm.store.create_task(
        "task-p2-05", policy="standard", state="running", workspace_dir=str(workspace), now=100.0
    )
    return SimpleNamespace(sm=sm, guard=studio.get_recovery_guard(), workspace=workspace, store=store)


def _failing_spec(workspace: Path, marker: str = "wrong") -> workflow_state.TaskVerificationSpec:
    target = workspace / "out.py"
    if not target.exists():
        target.write_text("result = 1\n", encoding="utf-8")
    return workflow_state.TaskVerificationSpec(
        require_managed_edit=False,
        required_modified_files=("out.py",),
        test_command=(
            sys.executable,
            "-c",
            f"print('FAILED tests/test_calc.py::test_add - {marker}'); raise SystemExit(1)",
        ),
    )


def test_failed_verification_is_recorded_with_its_evidence(studio_env: SimpleNamespace) -> None:
    spec = _failing_spec(studio_env.workspace)
    studio_env.sm.set_verification_spec("task-p2-05", spec)

    first = studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    second = studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    assert first.verified is False
    assert first.stagnation["state"] == rg.STAGNATION_FIRST
    assert second.stagnation["state"] == rg.STAGNATION_STAGNANT
    assert second.stagnation["repeat_count"] == 2
    assert second.stagnation["escalate"] is True
    history = studio_env.guard.history("task-p2-05")
    assert [record.kind for record in history] == ["verification", "verification"]
    assert history[-1].evidence.failure_ids == ("tests/test_calc.py::test_add",)
    assert len(history[-1].evidence.revision_digest) == 64


def test_escalated_stagnation_requests_a_safe_pause_on_the_task(studio_env: SimpleNamespace) -> None:
    spec = _failing_spec(studio_env.workspace)
    studio_env.sm.set_verification_spec("task-p2-05", spec)
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    second = studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    assert second.stagnation["pause_requested"] is True
    assert studio_env.sm.budgets.get_budget("task-p2-05").pause_requested is True
    # The pause is durable: a fresh process still sees it.
    reopened = rg.RecoveryGuard(studio_env.store)
    assert reopened.budget("task-p2-05").pause_requested is True


def test_changed_evidence_after_a_failure_is_progress_not_stagnation(studio_env: SimpleNamespace) -> None:
    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    studio_env.sm.set_verification_spec(
        "task-p2-05", _failing_spec(studio_env.workspace, marker="different failure text")
    )
    report = studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    assert report.verified is False
    assert report.stagnation["state"] == rg.STAGNATION_PROGRESS
    assert report.stagnation["escalate"] is False
    assert studio_env.sm.budgets.get_budget("task-p2-05").pause_requested is False


def test_verified_completion_is_recorded_as_progress(studio_env: SimpleNamespace) -> None:
    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    passing = workflow_state.TaskVerificationSpec(
        require_managed_edit=False,
        required_modified_files=("out.py",),
        test_command=(sys.executable, "-c", "print('1 passed')"),
    )
    studio_env.sm.set_verification_spec("task-p2-05", passing)
    report = studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)

    assert report.verified is True
    assert report.stagnation["state"] == rg.STAGNATION_PROGRESS
    assert studio_env.guard.history("task-p2-05")[-1].outcome == "verified"
    assert studio_env.guard.summary("task-p2-05")["failure_count"] == 1


# ---------------------------------------------------------------------------
# 8. Recovery requires changed evidence or conditions
# ---------------------------------------------------------------------------


def test_resume_is_refused_while_the_workspace_is_unchanged(studio_env: SimpleNamespace) -> None:
    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    studio_env.sm.transition_task("task-p2-05", "paused", allow_inactive_epoch=True, reason="turn_failed")

    with pytest.raises(workflow_state.WorkflowTransitionRejected) as excinfo:
        studio_env.sm.prepare_resume("task-p2-05")

    assert "resume refused" in str(excinfo.value)
    assert "Changed evidence or conditions" in str(excinfo.value)
    assert studio_env.sm.store.get_task("task-p2-05").state == "paused"
    assert studio_env.sm.budgets.get_budget("task-p2-05").pause_requested is True


def test_resume_is_allowed_after_the_workspace_changed(studio_env: SimpleNamespace) -> None:
    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    studio_env.sm.transition_task("task-p2-05", "paused", allow_inactive_epoch=True, reason="turn_failed")

    (studio_env.workspace / "out.py").write_text("result = 2\n", encoding="utf-8")
    plan = studio_env.sm.prepare_resume("task-p2-05")

    assert plan.task.task_id == "task-p2-05"
    assert plan.budget.recoveries_used == 1


# ---------------------------------------------------------------------------
# 9. Application wiring: retry/replan consumption, rollback budget, API view
# ---------------------------------------------------------------------------


def test_resumed_turn_consumes_retry_then_replan_after_stagnation(studio_env: SimpleNamespace) -> None:
    studio._consume_recovery_attempt_budget("task-p2-05")
    assert studio_env.guard.budget("task-p2-05").retries_used == 1
    assert studio_env.guard.budget("task-p2-05").replans_used == 0

    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    assert studio_env.guard.summary("task-p2-05")["latest_verdict"]["state"] == rg.STAGNATION_STAGNANT

    studio._consume_recovery_attempt_budget("task-p2-05")

    budget = studio_env.guard.budget("task-p2-05")
    assert (budget.retries_used, budget.replans_used) == (1, 1)


def test_exhausted_retry_budget_surfaces_as_http_409(studio_env: SimpleNamespace) -> None:
    for _ in range(3):
        studio._consume_recovery_attempt_budget("task-p2-05")

    with pytest.raises(studio.HTTPException) as excinfo:
        studio._consume_recovery_attempt_budget("task-p2-05")

    assert excinfo.value.status_code == 409
    assert "retry budget exhausted" in str(excinfo.value.detail)
    assert studio_env.guard.budget("task-p2-05").pause_requested is True


def test_rollback_spends_the_rollback_budget_and_refuses_when_exhausted(studio_env: SimpleNamespace) -> None:
    workspace = studio_env.workspace
    (workspace / "a.py").write_text("base = 0\n", encoding="utf-8")
    session = {
        "id": "sess-p2-05",
        "task_id": "task-p2-05",
        "worktree_dir": str(workspace),
        "policy": "standard",
    }

    def run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(studio._execute_harness_tool(None, session, tool, args))

    wrote = run("write_file", {"path": "a.py", "content": "base = 1\n"})
    assert wrote["ok"] is True

    for _ in range(3):
        studio_env.guard.consume("sess-p2-05", "rollback")

    refused = run("rollback_edits", {"dry_run": False})

    assert refused["ok"] is False
    assert "rollback budget exhausted" in refused["output"]
    # Nothing was written: the managed edit is still in place.
    assert (workspace / "a.py").read_text(encoding="utf-8") == "base = 1\n"


def test_task_state_endpoint_exposes_recovery_memory(studio_env: SimpleNamespace) -> None:
    studio_env.sm.set_verification_spec("task-p2-05", _failing_spec(studio_env.workspace))
    studio_env.sm.verify_task_completion("task-p2-05", workspace_dir=studio_env.workspace)
    session = studio.create_harness_session_db(
        title="P2-05 recovery",
        workspace_dir=str(studio_env.workspace),
        use_worktree=False,
        policy="standard",
        lead_model="test-model",
        critic_model="test-model",
    )
    session_id = session["id"]
    studio_env.guard.record_attempt(
        session_id,
        kind="verification",
        outcome="failed",
        evidence=rg.EvidenceSnapshot(
            revision_digest="a" * 64,
            transcript_sha256="b" * 64,
            failure_fingerprint=rg.failure_fingerprint("verification_test_failed", ("tests/a.py::test_a",)),
            failure_code="verification_test_failed",
            failure_ids=("tests/a.py::test_a",),
        ),
        now=1.0,
    )

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        response = client.get(
            f"/api/harness/sessions/{session_id}/task",
            headers={"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"},
        )

    assert response.status_code == 200
    recovery = response.json()["recovery"]
    assert recovery["attempt_count"] == 1
    assert recovery["budget"]["limits"]["max_retries"] == 3
    assert recovery["recent_attempts"][0]["evidence"]["failure_ids"] == ["tests/a.py::test_a"]


# ---------------------------------------------------------------------------
# 10. Standalone bundle parity
# ---------------------------------------------------------------------------


def test_recovery_guard_is_inlined_without_namespace_collisions() -> None:
    root = Path(__file__).resolve().parents[1]
    bundle = (root / "studio.py").read_text(encoding="utf-8")
    assert "class RecoveryGuard:" in bundle
    assert "def detect_stagnation(" in bundle

    build = (root / "scripts" / "build_studio.py").read_text(encoding="utf-8")
    block = build.split("INLINED_MODULES = (", 1)[1].split("\n)", 1)[0]
    inlined = tuple(re.findall(r'"([A-Za-z0-9_]+)"', block))
    assert "recovery_guard" in inlined

    def top_level_names(path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    mine = top_level_names(root / "recovery_guard.py")
    others: dict[str, str] = {}
    for module in inlined:
        if module == "recovery_guard":
            continue
        for name in top_level_names(root / f"{module}.py"):
            others.setdefault(name, module)
    collisions = {name: others[name] for name in mine if name in others}
    assert collisions == {}, f"recovery_guard globals collide inside the standalone namespace: {collisions}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
