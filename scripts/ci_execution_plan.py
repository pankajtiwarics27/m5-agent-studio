#!/usr/bin/env python3
"""Choose the fail-closed CI workload for one GitHub Actions event.

The workflow queries open pull requests only for same-repository feature pushes,
then passes the count here.  A query failure is represented by ``-1`` and never
suppresses verification.  This module performs no network access so every event
shape can be tested locally.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Suite = Literal["full", "scoped", "docs", "delegated"]

# These product JSON files are status/specification ledgers, not runtime policy
# inputs. Other product JSON stays on the conservative ready-PR full gate.
_TRACKING_LEDGER_PATHS = {
    "product/M5_Autonomous_Production_Roadmap.json",
}
_ROOT_DOCUMENTATION_PATHS = {
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "IMPLEMENTATION_STATUS.md",
    "M5_2_0_AGENT_PROMPT.md",
    "M5_2_0_UIUX_AGENT_PROMPT.md",
    "M5_AUTONOMOUS_PRODUCTION_AGENT_PROMPT.md",
}


def _select_changed_paths(paths: list[str]) -> Any:
    """Use the repository's single scoped-test selector in module or CLI mode."""
    try:
        from .run_scoped_tests import plan_for_paths
    except ImportError:
        from run_scoped_tests import plan_for_paths
    return plan_for_paths(paths)


def _is_documentation_only(paths: list[str]) -> bool:
    """Return true only for known prose and tracking-ledger paths."""
    if not paths:
        return False
    for path in paths:
        normalized = path.replace("\\", "/").removeprefix("./")
        if normalized in _ROOT_DOCUMENTATION_PATHS | _TRACKING_LEDGER_PATHS:
            continue
        if normalized.startswith("docs/") and normalized.endswith((".md", ".rst")):
            continue
        return False
    return True


_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_ZERO_SHA = "0" * 40


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    run_verification: bool
    suite: Suite
    scope_base: str
    reason: str


def _scope_base(before: str, default_branch: str) -> str:
    if _GIT_SHA.fullmatch(before) and before != _ZERO_SHA:
        return before
    return f"origin/{default_branch}"


def plan_for_event(
    *,
    event_name: str,
    ref: str,
    default_branch: str,
    before: str = "",
    open_pr_count: int = -1,
    pull_request_paths: list[str] | None = None,
    pull_request_is_draft: bool = False,
) -> VerificationPlan:
    """Return a conservative workload without trusting missing PR metadata."""
    main_ref = f"refs/heads/{default_branch}"
    fallback_base = _scope_base(before, default_branch)

    if event_name == "pull_request":
        if pull_request_paths is None:
            return VerificationPlan(
                True,
                "full",
                f"origin/{default_branch}",
                "pull_request_changed_paths_unavailable_full_gate",
            )
        normalized_paths = [
            path.replace("\\", "/").removeprefix("./") for path in pull_request_paths
        ]
        selection = _select_changed_paths(normalized_paths)
        if selection.full_suite or any(
            path.startswith("product/")
            and path.endswith(".json")
            and path not in _TRACKING_LEDGER_PATHS
            for path in normalized_paths
        ):
            return VerificationPlan(
                True,
                "full",
                f"origin/{default_branch}",
                "pull_request_scope_uncertain_full_gate",
            )
        if _is_documentation_only(normalized_paths):
            return VerificationPlan(
                True,
                "docs",
                f"origin/{default_branch}",
                "pull_request_documentation_gate",
            )
        if pull_request_is_draft:
            return VerificationPlan(
                True,
                "scoped",
                f"origin/{default_branch}",
                "pull_request_draft_scoped_gate",
            )
        return VerificationPlan(
            True,
            "full",
            f"origin/{default_branch}",
            "pull_request_ready_full_gate",
        )
    if event_name == "push" and ref == main_ref:
        return VerificationPlan(True, "full", f"origin/{default_branch}", "main_push_full_gate")
    if event_name == "push":
        if open_pr_count > 0:
            return VerificationPlan(
                False,
                "delegated",
                fallback_base,
                "open_pull_request_owns_required_checks",
            )
        if open_pr_count == 0:
            return VerificationPlan(True, "scoped", fallback_base, "feature_push_scoped_gate")
        return VerificationPlan(
            True,
            "full",
            f"origin/{default_branch}",
            "feature_push_pr_lookup_failed_run_full",
        )
    if event_name == "schedule":
        return VerificationPlan(True, "full", f"origin/{default_branch}", "scheduled_full_gate")
    if event_name == "workflow_dispatch":
        return VerificationPlan(True, "full", f"origin/{default_branch}", "manual_full_gate")
    return VerificationPlan(True, "full", f"origin/{default_branch}", "unknown_event_full_gate")


def concurrency_key(
    *,
    workflow: str,
    event_name: str,
    ref_name: str,
    pr_number: int | None = None,
) -> str:
    """Document the intended key shape; the workflow expression is authoritative."""
    if event_name == "pull_request" and pr_number is not None:
        identity = f"pr-{pr_number}"
    else:
        identity = f"{event_name}-{ref_name}"
    return f"ci-{workflow}-{identity}"


def _write_github_output(path: Path, plan: VerificationPlan) -> None:
    values = {
        "run_verification": str(plan.run_verification).lower(),
        "suite": plan.suite,
        "scope_base": plan.scope_base,
        "reason": plan.reason,
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument("--before", default="")
    parser.add_argument("--open-pr-count", type=int, default=-1)
    parser.add_argument("--pull-request-paths-file", type=Path)
    parser.add_argument("--pull-request-is-draft", action="store_true")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pull_request_paths: list[str] | None = None
    if args.pull_request_paths_file is not None:
        try:
            raw_paths = json.loads(args.pull_request_paths_file.read_text(encoding="utf-8"))
            if isinstance(raw_paths, list) and all(isinstance(path, str) for path in raw_paths):
                pull_request_paths = raw_paths
        except (OSError, json.JSONDecodeError):
            pass
    plan = plan_for_event(
        event_name=args.event,
        ref=args.ref,
        default_branch=args.default_branch,
        before=args.before,
        open_pr_count=args.open_pr_count,
        pull_request_paths=pull_request_paths,
        pull_request_is_draft=args.pull_request_is_draft,
    )
    print(json.dumps(asdict(plan), sort_keys=True))
    if args.github_output is not None:
        _write_github_output(args.github_output, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
