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


def test_ci_keeps_full_pull_request_gate_and_scopes_only_feature_pushes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "SCOPE_ON_PUSH" in workflow
    assert "scripts/run_scoped_tests.py --base" in workflow
    assert 'python -m pytest -q --tb=short -rf' in workflow
    assert "Browser safety regressions" in workflow


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
