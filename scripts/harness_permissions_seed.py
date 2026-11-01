#!/usr/bin/env python3
"""Serve the isolated demo instance with a real, seeded approval gate (UIUX-P3-03 fixture).

    python3 scripts/harness_permissions_seed.py            # serve; prints one JSON line

No route mints a pending approval. `_PENDING_APPROVALS` is in-process state, written only
when a running turn reaches a tool gate, and the demo has no model runtime, so a turn cannot
be run to reach one. That is why this fixture exists: it boots the same throwaway environment
the other walks use, calls the app's own `_create_pending_tool_approval()` - the function a
turn calls at the gate - and drops the slot of a second approval the way the runtime drops it
when the wait times out. Both approvals are therefore the server's own records: a
`approval/request` event in the durable log, and a live or dead slot in the approval table.

Nothing here is a hand-made card, a stubbed endpoint, or a faked response. The pane reads the
same `/state` snapshot and the same event log it reads in a real session.

The JSON line it prints carries the loopback URL, the demo token, the session id and the two
approval ids, so `scripts/harness_permissions_walk.mjs` can drive the surface without
guessing:

    {"base_url": "http://127.0.0.1:PORT", "token": "...", "session_id": "hns-...",
     "approve_approval_id": "apr-...", "reject_approval_id": "apr-...",
     "expired_approval_id": "apr-..."}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def seed(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and give it one live gate and one expired offer."""
    cli = _demo_cli()
    studio = cli._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)

    session = studio.create_harness_session_db(
        title="P3-03 walk",
        workspace_dir=str(environment.workspace_dir),
        use_worktree=False,
        policy="standard",
        lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
        critic_model="Gemma-4-21B-REAP-4bit",
    )
    session_id = str(session["id"])

    # Two live gates: exactly what a turn creates when it reaches the approval boundary.
    # One is approved in the walk and one is refused, because the two decisions record
    # different things and the pane has to say which one it did.
    to_approve = studio._create_pending_tool_approval(
        session_id,
        3,
        "run_bash",
        {"command": "pytest -q tests/test_store.py"},
    )
    to_reject = studio._create_pending_tool_approval(
        session_id,
        4,
        "run_bash",
        {"command": "python3 -m pip install --upgrade requests"},
    )
    # An offer nobody answered: the slot is gone because the wait timed out, which is what
    # the runtime does after `SG_DEFAULT_GRANT_TTL_SECONDS`, and what a reload must read as
    # expired rather than as an approval that can still be decided.
    stale = studio._create_pending_tool_approval(
        session_id,
        5,
        "write_file",
        {"path": "docs/release-notes.md", "content": "# Release notes\n"},
    )
    studio._PENDING_APPROVALS.pop(str(stale["approval_id"]), None)

    return (
        studio,
        token,
        session_id,
        str(to_approve["approval_id"]),
        str(to_reject["approval_id"]),
        str(stale["approval_id"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 picks one)")
    parser.add_argument("--root", default="", help="reuse a directory as the throwaway environment")
    parser.add_argument("--ready-file", default="", help="also write the ready line to this file")
    args = parser.parse_args(argv)

    try:
        environment = demo_tour.make_environment(
            Path(args.root).expanduser() if args.root else None
        )
        studio, token, session_id, approve_id, reject_id, expired_id = seed(environment)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"harness permissions seed: {exc}", file=sys.stderr)
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
        "session_id": session_id,
        "approve_approval_id": approve_id,
        "reject_approval_id": reject_id,
        "expired_approval_id": expired_id,
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
