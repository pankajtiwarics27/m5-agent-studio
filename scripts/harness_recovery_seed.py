#!/usr/bin/env python3
"""Serve the isolated demo instance with real paused / blocked / cancelled / orphaned tasks (UIUX-P3-05).

    python3 scripts/harness_recovery_seed.py            # serve; prints one JSON line

A recovery panel is not a card a walk can draw. Every field it shows comes from a durable record:
the task state and its transitions, the newest checkpoint, the recovery memory, the containment
store and the task budget. The demo has no model runtime, so no turn can be run to produce them.
This fixture therefore does what the product does instead of faking a screenshot: it creates four
small git workspaces, drives the real state machine through `ensure_root_task`,
`transition_task`, `save_checkpoint`, `request_pause` and `cancel_task`, and raises a real
containment barrier with `ProcessSupervisor.persist_barrier_hold()`. The states a run can be left
in - paused, blocked, cancelled, and "running with no worker behind it" - are therefore produced
by the same code the routes call, and the walk's presses go through `POST …/pause`, `…/resume`,
`…/cancel` like an operator's would.

What is staged is the arrival: the `turn/start` bookends are written here because the demo cannot
run a model. Everything the panel reads - and every answer the walk's presses get - is the
server's own.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "paused_session_id": "hns-...", "blocked_session_id": "hns-...",
     "orphan_session_id": "hns-...", "cancelled_session_id": "hns-...",
     "injected_session_id": "hns-..."}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)
import process_supervision  # noqa: E402  (the barrier a cancel raises is this type)

PATCHED_SOURCE = '''"""Module the paused run was editing."""


def total(values):
    return sum(values)
'''

# A reason a hostile run could put in the record. The panel and the event card draw
# reasons, so they must draw them as text: an id or a message that renders as markup
# is how a log becomes an attack surface.
HOSTILE_REASON = '<img src=x onerror="window.__recoveryXss=1">'


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_recovery", PROJECT_ROOT / "scripts" / "demo_tour.py"
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


def _build_workspace(root: Path) -> Path:
    """A committed git workspace with one uncommitted change, so a diff is real."""
    workspace = root / "project"
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "src" / "total.py").write_text('"""Module."""\n\n\ndef total(values):\n    return 0\n', encoding="utf-8")
    (workspace / "tests" / "test_total.py").write_text(
        "from src.total import total\n\n\ndef test_total_sums_a_list():\n    assert total([1, 2, 3]) == 6\n",
        encoding="utf-8",
    )
    _git(workspace, "init", "-q")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "baseline")
    (workspace / "src" / "total.py").write_text(PATCHED_SOURCE, encoding="utf-8")
    return workspace


def seed(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and leave five sessions in five real recovery states."""
    cli = _demo_cli()
    studio = cli._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    sm = studio.get_task_state_machine()
    supervisor = studio.get_process_supervisor()
    made: dict[str, dict[str, object]] = {}

    def new_session(key: str, title: str) -> tuple[str, dict, Path]:
        workspace = _build_workspace(environment.root / key)
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
        return session_id, task, workspace

    def bookends(session_id: str, prompt: str, state: str, steps: int) -> None:
        studio._append_harness_event(
            session_id,
            "turn/start",
            {"prompt": prompt, "policy": "standard"},
            dedupe_key=f"seed-turn-start:{session_id}",
        )
        studio._append_harness_event(
            session_id,
            "turn/end",
            {"elapsed_s": 21.5, "steps": steps, "state": state},
            dedupe_key=f"seed-turn-end:{session_id}",
        )

    # ── 1. Paused at a safe boundary, with a checkpoint to resume from ────────
    paused_id, paused_task, paused_ws = new_session("paused", "P3-05 walk: paused at a safe boundary")
    sm.transition_task(paused_task.task_id, "running", reason="seed_turn_started")
    sm.checkpoints.save_checkpoint(
        paused_task.task_id,
        step=3,
        payload={
            "workflow": "harness",
            "completed_step": 3,
            "messages_len": 8,
            "note": "src/total.py was edited; the test had not been run",
        },
    )
    pause_outcome = sm.request_pause(paused_id, reason="operator_pause_requested")
    assert not pause_outcome.deferred_to_safe_boundary, "the seeded pause must land immediately"
    studio._update_harness_session_state(paused_id, status="paused")
    bookends(paused_id, "Add total() to src/total.py and prove it with tests/test_total.py.", "paused", 3)
    studio._append_harness_event(
        paused_id,
        "turn/paused",
        {"reason": "operator_pause_requested", "step": 3},
        dedupe_key=f"seed-turn-paused:{paused_id}",
    )
    made["paused"] = {
        "session_id": paused_id,
        "workspace": str(paused_ws),
        "state": s_state(sm, paused_id),
        "checkpoint_step": 3,
    }

    # ── 2. Blocked on an effect the runtime cannot account for ───────────────
    blocked_id, blocked_task, blocked_ws = new_session("blocked", "P3-05 walk: blocked on an unresolved action")
    sm.transition_task(blocked_task.task_id, "running", reason="seed_turn_started")
    sm.checkpoints.save_checkpoint(
        blocked_task.task_id,
        step=2,
        payload={"workflow": "harness", "completed_step": 2, "messages_len": 5},
    )
    hold_action = f"act-seed-{blocked_id[-8:]}"
    supervisor.store.persist_barrier_hold(
        process_supervision.PersistedBarrierHold(
            hold_id=f"hold-{hold_action}",
            session_id=blocked_id,
            task_id=blocked_id,
            epoch=None,
            worker_id=None,
            action_id=hold_action,
            tool_name="run_bash",
            footprint_scope="conservative_workspace",
            workspace_root=str(blocked_ws.resolve()),
            barriers=((f"workspace:{blocked_ws.resolve()}", "exclusive"),),
            reason="worker_died_mid_action",
        )
    )
    sm.transition_task(
        blocked_task.task_id,
        "blocked",
        allow_inactive_epoch=True,
        reason="worker_died_mid_action",
    )
    studio._update_harness_session_state(blocked_id, status="blocked")
    bookends(blocked_id, "Run the migration inside the worktree.", "blocked", 2)
    made["blocked"] = {
        "session_id": blocked_id,
        "workspace": str(blocked_ws),
        "state": s_state(sm, blocked_id),
        "unresolved_action_id": hold_action,
    }

    # ── 3. Running, with no worker behind it (the crash shape) ───────────────
    # Left `running` with no lease and no worker. The product's own startup pass,
    # `TaskStateMachine.recover_incomplete_transitions()`, pauses exactly this shape at the
    # checkpoint before the first request is served - so what the walk reads is not a state
    # this fixture made up, it is what a crashed run looks like after a restart.
    orphan_id, orphan_task, orphan_ws = new_session("orphan", "P3-05 walk: worker died, task still running")
    sm.transition_task(orphan_task.task_id, "running", reason="seed_turn_started")
    sm.checkpoints.save_checkpoint(
        orphan_task.task_id,
        step=2,
        payload={"workflow": "harness", "completed_step": 2, "messages_len": 6},
    )
    studio._update_harness_session_state(orphan_id, status="running")
    bookends(orphan_id, "Refactor the exporter and keep the tests green.", "running", 2)
    made["orphan"] = {
        "session_id": orphan_id,
        "workspace": str(orphan_ws),
        "state": s_state(sm, orphan_id),
        # What the restart will write; the walk asserts the pane prints it.
        "expected_reason": "recovered_incomplete_transition",
    }

    # ── 4. Cancelled through the state machine, with its own record ──────────
    cancelled_id, cancelled_task, cancelled_ws = new_session("cancelled", "P3-05 walk: cancelled")
    sm.transition_task(cancelled_task.task_id, "running", reason="seed_turn_started")
    sm.checkpoints.save_checkpoint(
        cancelled_task.task_id,
        step=1,
        payload={"workflow": "harness", "completed_step": 1, "messages_len": 3},
    )
    cancel_outcome = sm.cancel_task(cancelled_id, reason="operator_cancelled")
    studio._update_harness_session_state(cancelled_id, status="cancelled")
    bookends(cancelled_id, "Try the release script.", "cancelled", 1)
    studio._append_harness_event(
        cancelled_id,
        "turn/cancelled",
        {
            "reason": "operator_cancelled",
            "unresolved_action_ids": list(cancel_outcome.unresolved_action_ids),
            "contained_processes": 0,
        },
        dedupe_key=f"seed-turn-cancelled:{cancelled_id}",
    )
    made["cancelled"] = {
        "session_id": cancelled_id,
        "workspace": str(cancelled_ws),
        "state": s_state(sm, cancelled_id),
    }

    # ── 5. A paused task whose reason is a fragment of markup ────────────────
    injected_id, injected_task, injected_ws = new_session("injected", "P3-05 walk: a reason that is markup")
    sm.transition_task(injected_task.task_id, "running", reason="seed_turn_started")
    sm.checkpoints.save_checkpoint(
        injected_task.task_id,
        step=1,
        payload={"workflow": "harness", "completed_step": 1, "messages_len": 2},
    )
    sm.request_pause(injected_id, reason=HOSTILE_REASON)
    studio._update_harness_session_state(injected_id, status="paused")
    bookends(injected_id, "Pause with a reason that is not text.", "paused", 1)
    studio._append_harness_event(
        injected_id,
        "turn/paused",
        {"reason": HOSTILE_REASON, "step": 1},
        dedupe_key=f"seed-turn-paused:{injected_id}",
    )
    made["injected"] = {
        "session_id": injected_id,
        "workspace": str(injected_ws),
        "state": s_state(sm, injected_id),
        "reason": HOSTILE_REASON,
    }

    return studio, token, made


def s_state(sm, session_id: str) -> str:
    task = sm.store.get_task(session_id)
    return str(task.state) if task is not None else "missing"


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
        print(f"harness recovery seed: {exc}", file=sys.stderr)
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
        "paused_session_id": made["paused"]["session_id"],
        "blocked_session_id": made["blocked"]["session_id"],
        "orphan_session_id": made["orphan"]["session_id"],
        "cancelled_session_id": made["cancelled"]["session_id"],
        "injected_session_id": made["injected"]["session_id"],
        "paused": made["paused"],
        "blocked": made["blocked"],
        "orphan": made["orphan"],
        "cancelled": made["cancelled"],
        "injected": made["injected"],
        "seeded_at": round(time.time(), 3),
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
