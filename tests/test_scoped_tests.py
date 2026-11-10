from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.run_scoped_tests import (
    ROOT,
    _git_worktree_paths,
    _parse_name_status_z,
    plan_for_paths,
    validate_scope_map,
)


def test_scope_map_covers_every_test_module_and_area_pattern() -> None:
    assert validate_scope_map(ROOT) == []


def test_compare_area_is_feature_scoped() -> None:
    plan = plan_for_paths(["tests/test_compare_history.py"])
    compare_names = {Path(path).name for path in plan.tests}

    assert not plan.full_suite
    assert plan.areas == ("ui.compare",)
    assert "test_compare_history.py" in compare_names
    assert "test_compare_continuity.py" in compare_names
    assert "test_start_health.py" not in compare_names


def test_frontend_changes_select_all_six_panes_and_shared_ui_regressions() -> None:
    plan = plan_for_paths(["static/index.html"])

    assert not plan.full_suite
    assert set(plan.areas) == {
        "ui.start",
        "ui.harness",
        "ui.council",
        "ui.pipeline",
        "ui.compare",
        "ui.chat",
        "ui.shared",
    }
    assert "tests/test_start_health.py" in plan.tests
    assert "tests/test_compare_history.py" in plan.tests
    assert "tests/test_keyboard_a11y.py" in plan.tests


def test_module_change_selects_its_feature_area() -> None:
    plan = plan_for_paths(["routing_policy.py"])

    assert not plan.full_suite
    assert "models.providers" in plan.areas
    assert "security.network" in plan.areas
    assert "tests/test_routing_policy.py" in plan.tests
    assert "tests/test_redirect_safety.py" in plan.tests


def test_docs_changes_select_documentation_checks() -> None:
    plan = plan_for_paths(["docs/quickstart.md", "README.md"])

    assert not plan.full_suite
    assert plan.areas == ("quality.docs-ci",)
    assert "tests/test_docs.py" in plan.tests
    assert "tests/test_governance.py" in plan.tests


def test_high_risk_and_unmapped_changes_fall_back_to_full_suite() -> None:
    app_change = plan_for_paths(["app.py"])
    unknown_change = plan_for_paths(["new_subsystem.py"])
    ci_change = plan_for_paths([".github/workflows/ci.yml"])
    empty_change = plan_for_paths([])

    assert app_change.full_suite
    assert "app.py" in app_change.reason
    assert unknown_change.full_suite
    assert ci_change.full_suite
    assert empty_change.full_suite


def test_test_harness_configuration_and_deleted_tests_fail_closed() -> None:
    scope_map_change = plan_for_paths(["tests/test_scopes.json"])
    deleted_test = plan_for_paths(["tests/test_obsolete.py"])
    support_fixture = plan_for_paths(["tests/helpers.py"])

    assert scope_map_change.full_suite
    assert deleted_test.full_suite
    assert support_fixture.full_suite


def test_git_name_status_parser_handles_additions_deletions_and_renames() -> None:
    parsed = _parse_name_status_z("M\0static/index.html\0R100\0old.py\0new.py\0D\0old_test.py\0")

    assert parsed == ["static/index.html", "old.py", "new.py", "old_test.py"]


def test_ci_keeps_full_gates_and_scopes_only_unclaimed_feature_pushes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:" in workflow
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "python scripts/ci_execution_plan.py" in workflow
    assert "SELECTED_SUITE" in workflow
    assert "scripts/run_scoped_tests.py --base" in workflow
    assert "python -m pytest -q --tb=short -rf" in workflow
    assert "Browser safety regressions" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow


def test_ci_does_not_run_the_heavy_jobs_twice_for_one_commit() -> None:
    """A push and the pull request for it used to run the same commit twice.

    The push run is the duplicate: the pull request run is the one carrying the
    full suite, so a pushed branch that already has an open pull request must not
    pay for the heavy jobs again. `main`, schedules, manual runs and every pull
    request still run, and a failed lookup must fail open.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "plan:" in workflow
    assert 'gh api --method GET "repos/$REPOSITORY/pulls"' in workflow
    # Fail closed: lookup failure selects scoped verification instead of
    # suppressing the push or pretending its required contexts ran.
    assert "Pull-request lookup failed" in workflow
    assert "python scripts/ci_execution_plan.py" in workflow
    assert "Delegate duplicate feature-push work to the pull request gate" in workflow
    # Delegated push placeholders use non-required names; full gates still
    # render the exact protected identities.
    assert "'Branch test' || 'test'" in workflow
    assert "'Branch browser safety regressions' || 'Browser safety regressions'" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow


def test_ci_concurrency_supersedes_within_an_event_lane_only() -> None:
    """Supersession must never trade the full pull request run for a scoped push.

    Keying the group on `github.ref` put `refs/heads/X` and `refs/pull/N/merge`
    in unrelated groups, so the two runs never superseded each other. Keying it
    on the branch alone would let a cheap push cancel the authoritative pull
    request run. The group therefore carries the event name, and the scheduled
    and manual lanes are excluded from cancellation.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "ci-${{ github.workflow }}-${{" in workflow
    assert "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'" in workflow
    assert "github.run_id" in workflow
    assert "format('pr-{0}', github.event.pull_request.number)" in workflow
    assert "cancel-in-progress: ${{ github.ref != format('refs/heads/{0}', github.event.repository.default_branch)" in workflow
    assert "github.event_name != 'schedule'" in workflow
    assert "github.event_name != 'workflow_dispatch'" in workflow
    # The old ref-only group is what let the duplicate pair run side by side.
    assert "group: ci-${{ github.ref }}" not in workflow


def test_ci_streams_pytest_and_reports_its_own_timing() -> None:
    """A ten-minute step that prints only its last 40 lines is unobservable."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'tee "$log"' in workflow
    # GitHub runs `run:` with `bash -e`; without turning it off, the first
    # failing command (the pytest pipeline itself) skips every diagnostic below.
    assert "set +e\n          set -uo pipefail" in workflow
    assert "PYTHONUNBUFFERED" in workflow
    assert "--durations=15" in workflow
    assert "tail -n 1" in workflow
    # Annotation properties are comma-delimited. A comma inside title= silently
    # truncated the displayed title before its Python lane; keep it delimiter-free.
    assert "::notice title=Pytest ${mode} suite - ${elapsed}s - Python ${{ matrix.python-version }}::" in workflow
    assert "suite (${elapsed}s," not in workflow
    assert "status=${PIPESTATUS[0]}" in workflow
    # The slowest measured leg is 13.6 minutes; 15 left almost no headroom.
    assert "timeout-minutes: 25" in workflow


def test_local_scope_selector_includes_staged_unstaged_and_untracked_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("before = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Scope Test", "-c", "user.email=scope@example.test", "commit", "-qm", "baseline"],
        cwd=tmp_path,
        check=True,
    )

    tracked.write_text("after = True\n", encoding="utf-8")
    staged = tmp_path / "staged.py"
    staged.write_text("staged = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=tmp_path, check=True)
    untracked = tmp_path / "untracked.py"
    untracked.write_text("untracked = True\n", encoding="utf-8")

    assert set(_git_worktree_paths(tmp_path)) == {"tracked.py", "staged.py", "untracked.py"}


def test_screenshot_collection_cannot_shadow_the_runtime_security_module() -> None:
    """A fresh scoped collection must not import scripts/security_release_gate.py.

    Full-suite collection can mask this bug by caching the runtime module first.
    Keep this child clean and execute the actual screenshot test module's imports.
    """
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            "import runpy, sys",
            "from pathlib import Path",
            "before = list(sys.path)",
            "runpy.run_path('tests/test_screenshots.py')",
            "assert sys.path == before, 'screenshot collection polluted sys.path'",
            "import security_release_gate as gate",
            "assert Path(gate.__file__).resolve() == Path('security_release_gate.py').resolve()",
            "assert callable(gate.evaluate_security_signoff_gate)",
        ])],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
