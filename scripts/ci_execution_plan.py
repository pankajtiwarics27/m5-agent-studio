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
from typing import Literal

Suite = Literal["full", "scoped", "delegated"]
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
) -> VerificationPlan:
    """Return a conservative workload without trusting missing PR metadata."""
    main_ref = f"refs/heads/{default_branch}"
    fallback_base = _scope_base(before, default_branch)

    if event_name == "pull_request":
        return VerificationPlan(True, "full", f"origin/{default_branch}", "pull_request_full_gate")
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
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = plan_for_event(
        event_name=args.event,
        ref=args.ref,
        default_branch=args.default_branch,
        before=args.before,
        open_pr_count=args.open_pr_count,
    )
    print(json.dumps(asdict(plan), sort_keys=True))
    if args.github_output is not None:
        _write_github_output(args.github_output, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
