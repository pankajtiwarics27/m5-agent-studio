#!/usr/bin/env python3
"""Serve the isolated demo instance with two real, recorded verification verdicts (UIUX-P3-04).

    python3 scripts/harness_evidence_seed.py            # serve; prints one JSON line

A verdict is not a card a walk can draw: `turn/verified` and `turn/verification_failed` carry
a `TaskVerificationReport`, and that report is only produced by
`TaskStateMachine.verify_task_completion()` after it has looked at the workspace. The demo has
no model runtime, so no turn can be run to produce one. This fixture therefore does the honest
thing instead of the convenient one: it builds two small git workspaces, sets a
`TaskVerificationSpec` on each session, and lets the product's own verifier decide - once on a
workspace whose test passes, once on one whose test fails. The reports (revision, environment,
exit code, transcript digest, classified failures, acceptance coverage) are computed by that
verifier, and the events are appended with `_append_harness_event()`, the writer a turn uses.

What is staged is the arrival: the `turn/start` and `turn/end` bookends are written here
because the demo cannot run a model. Everything between them is the server's own.

The JSON line it prints carries the loopback URL, the demo token, and both session ids:

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "verified_session_id": "hns-...", "rejected_session_id": "hns-...",
     "pending_session_id": "hns-..."}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)
import workflow_state  # noqa: E402  (the contract and report types the verifier speaks)

BASELINE_SOURCE = '''"""Tiny module the walk's verification command exercises."""


def total(values):
    return 0
'''

FIXED_SOURCE = '''"""Tiny module the walk's verification command exercises."""


def total(values):
    return sum(values)
'''

BROKEN_SOURCE = '''"""Tiny module the walk's verification command exercises."""


def total(values):
    return sum(values) + 1
'''

TESTS = '''from src.total import total


def test_total_sums_a_list():
    assert total([1, 2, 3]) == 6
'''


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "M5 Harness Walk",
            "GIT_AUTHOR_EMAIL": "walk@example.invalid",
            "GIT_COMMITTER_NAME": "M5 Harness Walk",
            "GIT_COMMITTER_EMAIL": "walk@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(workspace),
        },
    )


def _build_workspace(root: Path, before: str, after: str) -> Path:
    """A committed git workspace with one uncommitted change, so the diff is real."""
    workspace = root / "project"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "src" / "total.py").write_text(before, encoding="utf-8")
    (workspace / "tests" / "test_total.py").write_text(TESTS, encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "baseline")
    # The change the run made: uncommitted, so `git diff HEAD` and `git status --porcelain`
    # both see it and the diff the pane draws is the worktree's own.
    (workspace / "src" / "total.py").write_text(after, encoding="utf-8")
    return workspace


def seed(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and record one verified and one rejected verdict."""
    cli = _demo_cli()
    studio = cli._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    sm = studio.get_task_state_machine()

    verified_ws = _build_workspace(environment.root / "verified", BASELINE_SOURCE, FIXED_SOURCE)
    rejected_ws = _build_workspace(environment.root / "rejected", FIXED_SOURCE, BROKEN_SOURCE)

    def spec_for() -> workflow_state.TaskVerificationSpec:
        return workflow_state.TaskVerificationSpec(
            require_managed_edit=False,
            require_git_diff=True,
            required_modified_files=("src/total.py",),
            required_files=("tests/test_total.py",),
            require_agent_test_action=False,
            test_command=(sys.executable, "-m", "pytest", "-v", "tests"),
            timeout_s=60.0,
            acceptance_coverage=(("total() sums a list", ("tests/test_total.py::test_total_sums_a_list",)),),
        )

    made = {}
    for key, workspace, title in (
        ("verified", verified_ws, "P3-04 walk: verified"),
        ("rejected", rejected_ws, "P3-04 walk: rejected"),
    ):
        session = studio.create_harness_session_db(
            title=title,
            workspace_dir=str(workspace),
            use_worktree=False,
            policy="standard",
            lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
            critic_model="Gemma-4-21B-REAP-4bit",
        )
        session_id = str(session["id"])
        task, _budget = sm.ensure_root_task(
            session_id,
            session_id=session_id,
            title=title,
            workspace_dir=str(workspace),
            policy="standard",
        )
        spec = spec_for()
        sm.set_verification_spec(session_id, spec)
        sm.checkpoints.save_checkpoint(
            task.task_id,
            step=1,
            payload={
                "workflow": "harness",
                "completed_step": 1,
                "messages_len": 2,
                "note": "the workspace was edited before the authoritative command ran",
            },
        )
        report = sm.verify_task_completion(session_id, workspace_dir=workspace)
        studio._append_harness_event(
            session_id,
            "turn/start",
            {"prompt": "Add total() to src/total.py and prove it with tests/test_total.py.", "policy": "standard"},
            dedupe_key=f"seed-turn-start:{session_id}",
        )
        studio._append_harness_event(
            session_id,
            "turn/end",
            {"elapsed_s": 12.4, "steps": 2, "state": "completed" if report.verified else "error"},
            dedupe_key=f"seed-turn-end:{session_id}",
        )
        studio._append_harness_event(
            session_id,
            "turn/verified" if report.verified else "turn/verification_failed",
            report.to_dict(),
            dedupe_key=f"seed-verdict:{session_id}",
        )
        studio._update_harness_session_state(
            session_id, status="completed" if report.verified else "error"
        )
        made[key] = {
            "session_id": session_id,
            "verified": bool(report.verified),
            "failure_code": report.failure_code,
            "workspace": str(workspace),
        }

    # A third session that has a contract and no verdict: the surface has to draw an absence
    # as an absence, not as a pass. Nothing is verified here because nothing ran.
    pending_ws = _build_workspace(environment.root / "pending", BASELINE_SOURCE, FIXED_SOURCE)
    pending = studio.create_harness_session_db(
        title="P3-04 walk: no verdict yet",
        workspace_dir=str(pending_ws),
        use_worktree=False,
        policy="standard",
        lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
        critic_model="Gemma-4-21B-REAP-4bit",
    )
    pending_id = str(pending["id"])
    pending_task, _pending_budget = sm.ensure_root_task(
        pending_id,
        session_id=pending_id,
        title="P3-04 walk: no verdict yet",
        workspace_dir=str(pending_ws),
        policy="standard",
    )
    sm.set_verification_spec(pending_id, spec_for())
    made["pending"] = {
        "session_id": pending_id,
        "verified": False,
        "failure_code": "",
        "workspace": str(pending_ws),
    }

    return studio, token, made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 picks one)")
    parser.add_argument("--root", default="", help="reuse a directory as the throwaway environment")
    parser.add_argument("--ready-file", default="", help="also write the ready line to this file")
    args = parser.parse_args(argv)

    try:
        environment = demo_tour.make_environment(Path(args.root).expanduser() if args.root else None)
        studio, token, made = seed(environment)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"harness evidence seed: {exc}", file=sys.stderr)
        return 1

    port = args.port
    if port == 0:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

    ready = {
        "base_url": f"http://127.0.0.1:{port}",
        "token": token,
        "verified_session_id": made["verified"]["session_id"],
        "rejected_session_id": made["rejected"]["session_id"],
        "pending_session_id": made["pending"]["session_id"],
        "verified": made["verified"],
        "rejected": made["rejected"],
        "pending": made["pending"],
        "root": str(environment.root),
    }
    line = json.dumps(ready)
    print(line, flush=True)
    if args.ready_file:
        Path(args.ready_file).write_text(line + "\n", encoding="utf-8")

    import uvicorn

    uvicorn.run(studio.app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
