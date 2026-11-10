from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_execution_plan import concurrency_key, plan_for_event

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    ("event", "ref", "open_prs", "run", "suite", "reason"),
    [
        (
            "pull_request",
            "refs/pull/286/merge",
            -1,
            True,
            "full",
            "pull_request_changed_paths_unavailable_full_gate",
        ),
        ("push", "refs/heads/main", 3, True, "full", "main_push_full_gate"),
        ("push", "refs/heads/feature/no-pr", 0, True, "scoped", "feature_push_scoped_gate"),
        (
            "push",
            "refs/heads/feature/with-pr",
            1,
            False,
            "delegated",
            "open_pull_request_owns_required_checks",
        ),
        (
            "push",
            "refs/heads/feature/query-failed",
            -1,
            True,
            "full",
            "feature_push_pr_lookup_failed_run_full",
        ),
        ("schedule", "refs/heads/main", -1, True, "full", "scheduled_full_gate"),
        (
            "workflow_dispatch",
            "refs/heads/main",
            -1,
            True,
            "full",
            "manual_full_gate",
        ),
    ],
)
def test_event_selection_is_fail_closed(
    event: str,
    ref: str,
    open_prs: int,
    run: bool,
    suite: str,
    reason: str,
) -> None:
    plan = plan_for_event(
        event_name=event,
        ref=ref,
        default_branch="main",
        before="a" * 40,
        open_pr_count=open_prs,
    )

    assert plan.run_verification is run
    assert plan.suite == suite
    assert plan.reason == reason


def test_documentation_and_tracking_ledger_prs_use_scoped_gate() -> None:
    plan = plan_for_event(
        event_name="pull_request",
        ref="refs/pull/292/merge",
        default_branch="main",
        pull_request_paths=["AGENTS.md", "product/M5_Autonomous_Production_Roadmap.json"],
        pull_request_is_draft=True,
    )

    assert plan.run_verification is True
    assert plan.suite == "docs"
    assert plan.reason == "pull_request_documentation_gate"
    ready = plan_for_event(
        event_name="pull_request",
        ref="refs/pull/292/merge",
        default_branch="main",
        pull_request_paths=["AGENTS.md", "product/M5_Autonomous_Production_Roadmap.json"],
        pull_request_is_draft=False,
    )
    assert ready.suite == "docs"
    from scripts.run_scoped_tests import plan_for_paths

    selection = plan_for_paths(
        ["AGENTS.md", "product/M5_Autonomous_Production_Roadmap.json"]
    )
    assert selection.areas == ("quality.docs-ledger",)


def test_draft_feature_pr_is_scoped_but_ready_pr_uses_full_gate() -> None:
    paths = ["scoped_grants.py"]
    draft = plan_for_event(
        event_name="pull_request",
        ref="refs/pull/286/merge",
        default_branch="main",
        pull_request_paths=paths,
        pull_request_is_draft=True,
    )
    ready = plan_for_event(
        event_name="pull_request",
        ref="refs/pull/286/merge",
        default_branch="main",
        pull_request_paths=paths,
        pull_request_is_draft=False,
    )

    assert draft.suite == "scoped"
    assert draft.reason == "pull_request_draft_scoped_gate"
    assert ready.suite == "full"
    assert ready.reason == "pull_request_ready_full_gate"


def test_uncertain_and_runtime_product_changes_fail_closed_even_in_draft() -> None:
    for paths in (
        ["unmapped/new-input.bin"],
        ["product/M5_Autonomous_Production_Operating_Envelope.json"],
        ["./product/M5_Autonomous_Production_Operating_Envelope.json"],
    ):
        plan = plan_for_event(
            event_name="pull_request",
            ref="refs/pull/286/merge",
            default_branch="main",
            pull_request_paths=list(paths),
            pull_request_is_draft=True,
        )
        assert plan.suite == "full"
        assert plan.reason == "pull_request_scope_uncertain_full_gate"


def test_new_feature_branch_uses_main_as_scoped_base() -> None:
    plan = plan_for_event(
        event_name="push",
        ref="refs/heads/new-feature",
        default_branch="main",
        before="0" * 40,
        open_pr_count=0,
    )

    assert plan.scope_base == "origin/main"


def test_concurrency_cancels_superseded_runs_without_cross_event_races() -> None:
    push = concurrency_key(
        workflow="CI",
        event_name="push",
        ref_name="feature/demo",
    )
    same_branch_push = concurrency_key(
        workflow="CI",
        event_name="push",
        ref_name="feature/demo",
    )
    pull_request = concurrency_key(
        workflow="CI",
        event_name="pull_request",
        ref_name="286/merge",
        pr_number=286,
    )
    same_pull_request = concurrency_key(
        workflow="CI",
        event_name="pull_request",
        ref_name="286/merge",
        pr_number=286,
    )
    another_pull_request = concurrency_key(
        workflow="CI",
        event_name="pull_request",
        ref_name="287/merge",
        pr_number=287,
    )
    main = concurrency_key(
        workflow="CI",
        event_name="push",
        ref_name="main",
    )
    scheduled = concurrency_key(
        workflow="CI",
        event_name="schedule",
        ref_name="main",
    )
    manual = concurrency_key(
        workflow="CI",
        event_name="workflow_dispatch",
        ref_name="main",
    )

    assert push == same_branch_push == "ci-CI-push-feature/demo"
    assert pull_request == same_pull_request == "ci-CI-pr-286"
    assert push != pull_request
    assert len({push, pull_request, another_pull_request, main, scheduled, manual}) == 6


def test_cli_writes_machine_readable_github_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/ci_execution_plan.py",
            "--event",
            "push",
            "--ref",
            "refs/heads/feature/no-pr",
            "--default-branch",
            "main",
            "--before",
            "b" * 40,
            "--open-pr-count",
            "0",
            "--github-output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["suite"] == "scoped"
    assert output.read_text(encoding="utf-8").splitlines() == [
        "run_verification=true",
        "suite=scoped",
        f"scope_base={'b' * 40}",
        "reason=feature_push_scoped_gate",
    ]


def test_workflow_preserves_required_checks_and_shards_browser_work() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.pull_request.number" in workflow
    assert "github.ref_name" in workflow
    assert "cancel-in-progress: ${{" in workflow
    assert "github.event_name != 'schedule'" in workflow
    assert "open_pull_request_owns_required_checks" not in workflow
    assert "python scripts/ci_execution_plan.py" in workflow
    assert "PR_IS_DRAFT: ${{ github.event.pull_request.draft || false }}" in workflow
    assert "--pull-request-is-draft" in workflow
    assert "pull_request_documentation_gate" in (ROOT / "scripts" / "ci_execution_plan.py").read_text(encoding="utf-8")
    assert "needs.plan.outputs.suite == 'full'" in workflow
    assert "needs.plan.outputs.suite == 'docs'" in workflow
    assert "--paths-file \"$PR_PATHS_FILE\"" in workflow
    scope_map = json.loads((ROOT / "tests" / "test_scopes.json").read_text(encoding="utf-8"))
    assert scope_map["areas"]["quality.docs-ledger"] == [
        "test_adr_process.py",
        "test_autonomous_production_decisions.py",
        "test_docs.py",
        "test_governance.py",
    ]
    assert "gh api --paginate \"repos/$REPOSITORY/pulls/$PR_NUMBER/files\"" in workflow
    assert "pr-changed-paths-${{ github.event.pull_request.number }}" in workflow
    assert "--paths-file \"$PR_PATHS_FILE\"" in workflow
    assert "&& 'Branch test' || 'test'" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow
    assert "Python test shard (${{ matrix.python-version }}, ${{ matrix.shard }})" in workflow
    assert "needs.python_identity.result" in workflow
    assert "&& 'Branch browser safety regressions' || 'Browser safety regressions'" in workflow
    assert "Browser safety shard (${{ matrix.shard }})" in workflow
    assert "needs.browser_safety_shards.result" in workflow
    assert "python scripts/ci_test_shard.py" in workflow
    assert "python -m pip install -e '.[dev,supply-chain]'" in workflow
