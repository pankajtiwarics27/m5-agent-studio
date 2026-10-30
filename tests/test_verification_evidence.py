"""M5-P2-04 tests: verification evidence and baseline-aware tests.

The behaviours pinned here are the story's acceptance criteria:

* a verified result is **tied to a source revision** — the report carries the
  revision digest, the transcript hash covers it, and a workspace that changes
  while the authoritative command runs cannot be verified (AC1);
* **pre-existing failures are not misattributed** — a failure the caller recorded
  before the task is classified ``pre_existing`` (and only an explicit opt-in lets
  such a run verify), an environment problem is ``environmental``, and anything
  else is ``introduced`` (AC2);
* **no success claim on sandbox denial** — output showing the runtime refused to
  execute yields a denied verdict and a failed report even at exit code 0 (AC3).
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app as studio
import runtime_persistence as rp
import verification_evidence as ve
import workflow_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(workspace.parent),
        },
    )


def _init_repo(workspace: Path) -> None:
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "init", "--allow-empty")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# 1. Source revision
# ---------------------------------------------------------------------------


def test_source_revision_digest_is_stable_and_content_bound(workspace: Path) -> None:
    target = workspace / "calc.py"
    target.write_text("value = 1\n", encoding="utf-8")

    first = ve.capture_source_revision(workspace, ["calc.py"])
    second = ve.capture_source_revision(workspace, ["calc.py"])

    assert first.digest == second.digest
    assert first.git_head == ""
    assert first.file_digests == (("calc.py", hashlib.sha256(b"value = 1\n").hexdigest()),)

    target.write_text("value = 2\n", encoding="utf-8")
    changed = ve.capture_source_revision(workspace, ["calc.py"])
    assert changed.digest != first.digest


def test_source_revision_records_git_head_and_dirty_paths(workspace: Path) -> None:
    (workspace / "calc.py").write_text("value = 1\n", encoding="utf-8")
    _init_repo(workspace)
    (workspace / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    revision = ve.capture_source_revision(workspace, ["calc.py"])
    head = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    assert revision.git_head == head
    assert "scratch.txt" in revision.git_dirty_paths
    assert revision.to_dict()["git_head"] == head


def test_source_revision_refuses_traversal_symlinks_and_nul(workspace: Path) -> None:
    (workspace / "real.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "link.py").symlink_to(workspace / "real.py")
    outside = workspace.parent / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    (workspace / "escape").symlink_to(workspace.parent)

    with pytest.raises(ve.VerificationEvidenceError):
        ve.capture_source_revision(workspace, ["../outside.py"])
    with pytest.raises(ve.VerificationEvidenceError):
        ve.capture_source_revision(workspace, [str(outside)])
    with pytest.raises(ve.VerificationEvidenceError):
        ve.capture_source_revision(workspace, ["link.py"])
    with pytest.raises(ve.VerificationEvidenceError):
        ve.capture_source_revision(workspace, ["escape/outside.py"])
    with pytest.raises(ve.VerificationEvidenceError):
        ve.capture_source_revision(workspace, ["bad\x00name.py"])
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_test_tool_artifacts_do_not_count_as_a_revision_change(workspace: Path) -> None:
    (workspace / "calc.py").write_text("value = 1\n", encoding="utf-8")
    _init_repo(workspace)

    before = ve.capture_source_revision(workspace, ["calc.py"])
    (workspace / ".pytest_cache").mkdir()
    (workspace / ".pytest_cache" / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "calc.cpython-311.pyc").write_text("x", encoding="utf-8")
    after = ve.capture_source_revision(workspace, ["calc.py"])

    assert before.git_dirty_paths == ()
    assert after.digest == before.digest

    (workspace / "calc.py").write_text("value = 2\n", encoding="utf-8")
    assert ve.capture_source_revision(workspace, ["calc.py"]).digest != before.digest


def test_missing_revision_file_is_recorded_as_absent(workspace: Path) -> None:
    revision = ve.capture_source_revision(workspace, ["missing.py"])
    assert revision.file_digests == (("missing.py", None),)


# ---------------------------------------------------------------------------
# 2. Failure classification
# ---------------------------------------------------------------------------


_PYTEST_FAILURE_OUTPUT = """
=========================== short test summary info ============================
FAILED tests/test_invoice.py::test_discount - AssertionError: assert 99 == 100
FAILED tests/test_legacy.py::test_known_broken - AssertionError: pre-existing
FAILED tests/test_env.py::test_optional_dep - ModuleNotFoundError: No module named 'numpy'
"""


def test_failure_classification_separates_pre_existing_environmental_and_introduced() -> None:
    records = ve.classify_test_output(
        _PYTEST_FAILURE_OUTPUT,
        exit_code=1,
        pre_existing=["tests/test_legacy.py::test_known_broken"],
    )
    kinds = {record.test_id: record.kind for record in records}

    assert kinds["tests/test_invoice.py::test_discount"] == ve.CLASSIFICATION_INTRODUCED
    assert kinds["tests/test_legacy.py::test_known_broken"] == ve.CLASSIFICATION_PRE_EXISTING
    assert kinds["tests/test_env.py::test_optional_dep"] == ve.CLASSIFICATION_ENVIRONMENTAL


def test_unparsed_failing_output_is_never_treated_as_clean() -> None:
    records = ve.classify_test_output("Segmentation fault\n", exit_code=139)
    assert len(records) == 1
    assert records[0].kind == ve.CLASSIFICATION_INTRODUCED
    assert ve.classify_test_output("", exit_code=0) == ()


def test_denial_markers_are_detected_and_never_count_as_passed() -> None:
    output = "Denied by `plan` (read-only) policy: `run_bash` is disabled."

    assert ve.detect_denial(output) == "denied by `plan`"
    evidence = ve.build_test_evidence(("pytest",), exit_code=0, output=output)

    assert evidence.denied
    assert evidence.classification == ve.RUN_DENIED
    assert evidence.passed is False
    assert evidence.failures == ()


def test_transcript_digest_is_tied_to_the_revision(workspace: Path) -> None:
    (workspace / "calc.py").write_text("value = 1\n", encoding="utf-8")
    first = ve.capture_source_revision(workspace, ["calc.py"])
    (workspace / "calc.py").write_text("value = 2\n", encoding="utf-8")
    second = ve.capture_source_revision(workspace, ["calc.py"])

    kwargs: dict[str, Any] = {"exit_code": 0, "output": "1 passed"}
    ev_a = ve.build_test_evidence(("pytest", "-q"), revision_before=first, **kwargs)
    ev_b = ve.build_test_evidence(("pytest", "-q"), revision_before=second, **kwargs)

    assert ev_a.output_sha256 != ev_b.output_sha256
    # The same revision and transcript reproduce the same evidence hash.
    assert ve.build_test_evidence(("pytest", "-q"), revision_before=first, **kwargs).output_sha256 == ev_a.output_sha256


# ---------------------------------------------------------------------------
# 3. Running the authoritative command
# ---------------------------------------------------------------------------


def test_run_verification_command_captures_evidence(workspace: Path) -> None:
    (workspace / "calc.py").write_text("value = 1\n", encoding="utf-8")

    evidence = ve.run_verification_command(
        (sys.executable, "-c", "print('1 passed')"),
        workspace=workspace,
        timeout_s=30.0,
        files=["calc.py"],
    )

    assert evidence.exit_code == 0
    assert evidence.classification == ve.RUN_PASSED
    assert evidence.passed is True
    assert evidence.revision_before is not None and evidence.revision_after is not None
    assert evidence.revision_changed is False
    assert evidence.environment["python"]
    assert evidence.to_dict()["classification"] == ve.RUN_PASSED


def test_run_verification_command_classifies_a_failing_run(workspace: Path) -> None:
    evidence = ve.run_verification_command(
        (sys.executable, "-c", "import sys; print('FAILED tests/x.py::t - boom'); sys.exit(3)"),
        workspace=workspace,
        timeout_s=30.0,
    )

    assert evidence.exit_code == 3
    assert evidence.classification == ve.RUN_INTRODUCED
    assert [record.test_id for record in evidence.failures] == ["tests/x.py::t"]
    assert evidence.passed is False


def test_run_verification_command_reports_a_timeout(workspace: Path) -> None:
    evidence = ve.run_verification_command(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        workspace=workspace,
        timeout_s=0.5,
    )

    assert evidence.exit_code is None
    assert evidence.classification == ve.RUN_ENVIRONMENT_BLOCKED
    assert "timed out" in evidence.failures[0].detail


def test_run_verification_command_rejects_an_empty_command(workspace: Path) -> None:
    with pytest.raises(ve.VerificationEvidenceError):
        ve.run_verification_command((), workspace=workspace, timeout_s=5.0)


# ---------------------------------------------------------------------------
# 4. The completion gate consumes the evidence
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db_path = tmp_path / "p2_04.sqlite"
    store_root = tmp_path / "store"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_root)
    studio.init_db()
    sm = workflow_state.DurableTaskStateMachine(studio.STORAGE, db_path)
    store = rp.RuntimePersistenceStore(studio.STORAGE, db_path)
    store.create_task("task-p2-04", policy="standard", state="running", now=100.0)
    return SimpleNamespace(sm=sm, store=store, root=store_root)


def _spec(workspace: Path, command: tuple[str, ...], **kwargs: Any) -> workflow_state.TaskVerificationSpec:
    target = workspace / "out.py"
    if not target.exists():
        target.write_text("result = 1\n", encoding="utf-8")
    return workflow_state.TaskVerificationSpec(
        require_managed_edit=False,
        required_modified_files=("out.py",),
        test_command=command,
        **kwargs,
    )


def test_verified_report_records_revision_environment_and_test_identity(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(workspace, (sys.executable, "-c", "print('1 passed')"))
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is True
    assert report.failure_classification == ve.RUN_PASSED
    assert report.denied is False
    assert len(report.source_revision) == 64
    assert report.revision["files"]["out.py"] == hashlib.sha256(b"result = 1\n").hexdigest()
    assert report.test_evidence["command"][0] == sys.executable
    assert report.test_evidence["exit_code"] == 0
    assert report.test_evidence["environment"]["python"]
    assert report.test_sha256 == report.test_evidence["output_sha256"]
    test_refs = [ref for ref in report.evidence_refs if ref.kind == "test_verification"]
    assert test_refs and report.source_revision[:12] in test_refs[0].summary
    assert report.to_dict()["source_revision"] == report.source_revision


def test_pre_existing_failure_is_classified_and_only_verifies_when_allowed(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    script = (
        "print('FAILED tests/test_legacy.py::test_known_broken - boom'); raise SystemExit(1)"
    )
    strict = _spec(
        workspace,
        (sys.executable, "-c", script),
        pre_existing_failures=("tests/test_legacy.py::test_known_broken",),
    )
    state_machine.sm.set_verification_spec("task-p2-04", strict)

    failed = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert failed.verified is False
    # The failure is attributed to the baseline, not to this change.
    assert failed.failure_code == "verification_test_failed_pre_existing"
    assert failed.failure_classification == ve.RUN_PRE_EXISTING_ONLY
    assert [record["kind"] for record in failed.test_evidence["failures"]] == [ve.CLASSIFICATION_PRE_EXISTING]

    tolerant = _spec(
        workspace,
        (sys.executable, "-c", script),
        pre_existing_failures=("tests/test_legacy.py::test_known_broken",),
        allow_pre_existing_failures=True,
    )
    state_machine.sm.set_verification_spec("task-p2-04", tolerant)

    tolerated = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert tolerated.verified is True
    assert tolerated.failure_classification == ve.RUN_PRE_EXISTING_ONLY


def test_introduced_failure_is_not_blamed_on_the_baseline(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('FAILED tests/test_invoice.py::test_discount - wrong'); raise SystemExit(1)"),
        pre_existing_failures=("tests/test_legacy.py::test_known_broken",),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "verification_test_failed_introduced"
    assert report.failure_classification == ve.RUN_INTRODUCED


def test_denied_execution_never_produces_a_success_claim(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('Code execution is disabled. Configure STUDIO_EXECUTION_MODE'); "),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "verification_test_denied"
    assert report.denied is True
    assert report.failure_classification == ve.RUN_DENIED


def test_workspace_change_during_verification_is_rejected(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    script = (
        "from pathlib import Path; "
        "Path('out.py').write_text('result = 2\\n', encoding='utf-8'); "
        "print('1 passed')"
    )
    spec = _spec(workspace, (sys.executable, "-c", script))
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "workspace_changed_during_verification"
    assert report.test_evidence["revision_changed"] is True


def test_revision_stability_can_be_disabled_explicitly(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    script = (
        "from pathlib import Path; "
        "Path('out.py').write_text('result = 2\\n', encoding='utf-8'); "
        "print('1 passed')"
    )
    spec = _spec(workspace, (sys.executable, "-c", script), require_revision_stability=False)
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is True
    assert report.test_evidence["revision_changed"] is True


# ---------------------------------------------------------------------------
# 5. Contract plumbing
# ---------------------------------------------------------------------------


def test_spec_round_trips_the_new_verification_fields(workspace: Path) -> None:
    spec = workflow_state.TaskVerificationSpec(
        require_managed_edit=False,
        test_command=("pytest", "-q"),
        pre_existing_failures=("tests/a.py::test_b", "tests/a.py::test_b", ""),
        allow_pre_existing_failures=True,
        require_revision_stability=False,
    )

    restored = workflow_state.TaskVerificationSpec.from_dict(spec.to_dict())

    assert restored.pre_existing_failures == ("tests/a.py::test_b",)
    assert restored.allow_pre_existing_failures is True
    assert restored.require_revision_stability is False
    assert restored.sha256() == spec.sha256()
    assert "pre_existing_failures" in spec.to_dict()


def test_api_parser_accepts_the_new_contract_fields(workspace: Path) -> None:
    parsed = studio._parse_task_verification_spec(
        {
            "require_managed_edit": False,
            "pre_existing_failures": ["tests/a.py::test_b"],
            "allow_pre_existing_failures": True,
            "require_revision_stability": False,
        },
        workspace_dir=workspace,
    )

    assert parsed is not None
    assert parsed.pre_existing_failures == ("tests/a.py::test_b",)
    assert parsed.allow_pre_existing_failures is True
    assert parsed.require_revision_stability is False

    with pytest.raises(Exception):
        studio._parse_task_verification_spec(
            {"pre_existing_failures": "tests/a.py::test_b"}, workspace_dir=workspace
        )


def test_standalone_bundle_inlines_verification_evidence() -> None:
    root = Path(__file__).resolve().parents[1]
    bundle = (root / "studio.py").read_text(encoding="utf-8")

    assert "class TestEvidence:" in bundle
    assert "def capture_source_revision(" in bundle
    assert "verification_test_denied" in bundle
    assert "class SourceRevision:" in bundle



def test_targeted_stage_runs_first_and_blocks_the_broader_stage(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    marker = workspace / "broader_ran.txt"
    broader = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8'); "
        "print('5 passed')"
    )
    spec = _spec(
        workspace,
        (sys.executable, "-c", broader),
        targeted_test_command=(
            sys.executable,
            "-c",
            "print('FAILED tests/test_calc.py::test_add - nope'); raise SystemExit(1)",
        ),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "verification_targeted_test_failed"
    # Fail fast: the broader stage never executed.
    assert marker.exists() is False
    assert report.staged_test_evidence["targeted"]["exit_code"] == 1
    assert report.test_evidence == {}
    kinds = {ref.kind for ref in report.evidence_refs}
    assert "targeted_test_verification" in kinds


def test_targeted_stage_passing_lets_the_broader_stage_verify(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('tests/test_calc.py::test_add PASSED')"),
        targeted_test_command=(
            sys.executable,
            "-c",
            "print('tests/test_calc.py::test_add PASSED')",
        ),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is True
    assert report.staged_test_evidence["targeted"]["classification"] == ve.RUN_PASSED
    assert report.test_evidence["classification"] == ve.RUN_PASSED
    staged = [ref for ref in report.evidence_refs if ref.kind == "targeted_test_verification"]
    assert staged and staged[0].sha256 == report.staged_test_evidence["targeted"]["output_sha256"]


def test_denied_targeted_stage_never_claims_success(state_machine: SimpleNamespace, workspace: Path) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('1 passed')"),
        targeted_test_command=(
            sys.executable,
            "-c",
            "print('Code execution is disabled. Configure STUDIO_EXECUTION_MODE')",
        ),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "verification_test_denied"
    assert report.denied is True


def test_acceptance_coverage_is_recorded_when_every_criterion_is_demonstrated(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('tests/test_calc.py::test_add PASSED')"),
        acceptance_coverage=(
            ("AC1: revision recorded", ("tests/test_calc.py::test_add",)),
        ),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is True
    assert report.acceptance_coverage == (
        {
            "criterion": "AC1: revision recorded",
            "test_ids": ["tests/test_calc.py::test_add"],
            "covered": True,
            "missing_test_ids": [],
        },
    )
    assert report.to_dict()["acceptance_coverage"][0]["covered"] is True


def test_declared_acceptance_criterion_without_evidence_fails_closed(
    state_machine: SimpleNamespace, workspace: Path
) -> None:
    spec = _spec(
        workspace,
        (sys.executable, "-c", "print('tests/test_calc.py::test_add PASSED')"),
        acceptance_coverage=(
            ("AC1: revision recorded", ("tests/test_calc.py::test_add",)),
            ("AC2: denial refused", ("tests/test_calc.py::test_denial",)),
        ),
    )
    state_machine.sm.set_verification_spec("task-p2-04", spec)

    report = state_machine.sm.verify_task_completion("task-p2-04", workspace_dir=workspace)

    assert report.verified is False
    assert report.failure_code == "verification_acceptance_coverage_missing"
    assert "AC2: denial refused" in report.failure_detail
    assert [entry["covered"] for entry in report.acceptance_coverage] == [True, False]


def test_spec_round_trips_staged_and_coverage_fields(workspace: Path) -> None:
    spec = workflow_state.TaskVerificationSpec(
        require_managed_edit=False,
        test_command=("pytest", "-q"),
        targeted_test_command=("pytest", "-q", "tests/test_calc.py"),
        acceptance_coverage=(("AC1", ("tests/test_calc.py::test_add",)), ("AC1", ("dup",))),
    )

    restored = workflow_state.TaskVerificationSpec.from_dict(spec.to_dict())

    assert restored.targeted_test_command == ("pytest", "-q", "tests/test_calc.py")
    assert restored.acceptance_coverage == (("AC1", ("tests/test_calc.py::test_add",)),)
    assert restored.sha256() == spec.sha256()


def test_api_parser_accepts_staged_and_coverage_fields(workspace: Path) -> None:
    parsed = studio._parse_task_verification_spec(
        {
            "require_managed_edit": False,
            "targeted_test_command": ["pytest", "-q", "tests/test_calc.py"],
            "acceptance_coverage": [{"criterion": "AC1", "test_ids": ["tests/test_calc.py::test_add"]}],
        },
        workspace_dir=workspace,
    )

    assert parsed is not None
    assert parsed.targeted_test_command == ("pytest", "-q", "tests/test_calc.py")
    assert parsed.acceptance_coverage == (("AC1", ("tests/test_calc.py::test_add",)),)

    with pytest.raises(Exception):
        studio._parse_task_verification_spec(
            {"acceptance_coverage": ["AC1"]}, workspace_dir=workspace
        )
    with pytest.raises(Exception):
        studio._parse_task_verification_spec(
            {"acceptance_coverage": [{"criterion": "AC1", "test_ids": []}]}, workspace_dir=workspace
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))


def test_acceptance_coverage_only_counts_tests_seen_passing_in_the_transcript() -> None:
    declared = (
        ("AC1: revision is recorded", ("tests/test_a.py::test_rev",)),
        ("AC2: denial is refused", ("tests/test_a.py::test_denied",)),
        ("AC3: failures are classified", ("tests/test_a.py::test_unmentioned",)),
    )
    coverage = ve.evaluate_acceptance_coverage(
        declared,
        output="tests/test_a.py::test_rev PASSED\ntests/test_a.py::test_denied FAILED\n",
        failures=(ve.FailureRecord(test_id="tests/test_a.py::test_denied", kind=ve.CLASSIFICATION_INTRODUCED),),
    )

    assert [entry["covered"] for entry in coverage] == [True, False, False]
    assert coverage[1]["missing_test_ids"] == ["tests/test_a.py::test_denied"]
    assert coverage[2]["missing_test_ids"] == ["tests/test_a.py::test_unmentioned"]


def test_acceptance_coverage_requires_at_least_one_test_id() -> None:
    with pytest.raises(workflow_state.WorkflowStateError):
        workflow_state.TaskVerificationSpec(acceptance_coverage=(("AC1", ()),))
