#!/usr/bin/env python3
"""Serve the isolated demo instance with real direction check-ins staged (AP-018).

    python3 scripts/harness_direction_seed.py            # serve; prints one JSON line

A direction panel is not a card a walk can draw. Every fact it shows - the sample, the digest of
the exact version shown, the question, the trigger and the count of check-ins taken - comes from
a durable record: the `running -> waiting_approval` transition's evidence, written by the route
the product uses. The demo has no model runtime, so no turn can be run to produce them.

This fixture therefore does what the product does instead of faking a screenshot. It drives the
real state machine through `ensure_root_task` and `transition_task`, and it asks the real
`POST …/direction-checkpoint` route for each check-in, so the record the walk reads is the one the
route wrote. Three states are staged:

* ``waiting`` - an unbenchmarked task paused on an early sample, which the walk answers;
* ``covered`` - a task an owner-approved reference already covers. It must *not* be interrupted,
  and the walk presses for exactly that: a covered task keeps running;
* ``hostile`` - a sample id and question that are markup, to prove the panel draws text.

The check-ins are staged from a **startup hook**, not before the server runs. The app's startup
recovery calls `recover_incomplete_transitions()`, which correctly pauses a `waiting_approval`
task that no worker is behind; staging earlier would have the fixture's own evidence rewritten
by the runtime's recovery before the walk ever saw it.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "waiting_session_id": "hns-...", "covered_session_id": "hns-...",
     "hostile_session_id": "hns-...", "sample_sha256": "..."}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)

SAMPLE_SOURCE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Direction sample</title></head>
<body><main><h1>Ledger statement</h1><p>An early, cheap sample.</p></main></body></html>
"""

# A sample id and a check name a hostile run could put in the record. The panel draws them as
# text; the walk asserts no element and no handler appears.
HOSTILE = '<script>globalThis.pwned=1</script><img src=x onerror="globalThis.pwned=1">'


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_direction", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sample_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and create three running sessions with a sample workspace."""
    studio = _demo_cli()._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    # The middleware compares against the module constant, which is read at import time. The
    # fixture asks the real routes in this same process, so it binds the token it just created.
    studio.STUDIO_API_TOKEN = token

    workspace = environment.root / "website"
    workspace.mkdir(parents=True, exist_ok=True)
    sample_path = workspace / "hero.html"
    sample_path.write_text(SAMPLE_SOURCE, encoding="utf-8")
    # The covered task needs its own project: a reference covers the project it was approved
    # for, so an unbenchmarked task must not be sitting in that same project or it inherits
    # the coverage. Two workspaces also let the walk prove the rule is per project.
    covered_workspace = environment.root / "covered-website"
    covered_workspace.mkdir(parents=True, exist_ok=True)
    (covered_workspace / "hero.html").write_text(SAMPLE_SOURCE, encoding="utf-8")

    sm = studio.get_task_state_machine()
    ids: dict[str, str] = {}
    for key, title, task_workspace in (
        ("waiting", "AP-018 walk: unbenchmarked", workspace),
        ("covered", "AP-018 walk: benchmark covered", covered_workspace),
        ("hostile", "AP-018 walk: hostile text", workspace),
    ):
        session = studio.create_harness_session_db(
            title=title,
            workspace_dir=str(task_workspace),
            use_worktree=False,
            policy="standard",
            lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
            critic_model="Gemma-4-21B-REAP-4bit",
        )
        session_id = str(session["id"])
        sm.ensure_root_task(
            session_id,
            session_id=session_id,
            title=title,
            workspace_dir=str(task_workspace),
            policy="standard",
        )
        if sm.store.get_task(session_id).state == "pending":
            sm.transition_task(session_id, "running", reason="turn_started")
        studio._append_harness_event(
            session_id,
            "turn/start",
            {"prompt": f"{title}: produce the website", "policy": "standard"},
            dedupe_key=f"seed-direction-turn-start:{session_id}",
        )
        ids[key] = session_id

    return studio, token, ids, sample_path, covered_workspace


def _post_json(base_url: str, token: str, path: str, body: dict) -> tuple[int, dict]:
    """POST to the running server over HTTP, so staging goes through the middleware too."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")[:400]}


# The reference the `covered` task relies on. It is a real record written through the real
# route, because that is the only thing the direction route will accept as coverage.
COVERED_REFERENCE_ID = "reference/website-2026-11"


def _stage_covering_reference(
    studio, token: str, base_url: str, workspace: Path, digest: str
) -> str:
    """Record the owner-approved reference that makes the `covered` task covered.

    This used to be sent in the direction request as an inline ``benchmarks`` list, which
    AP-020 removed on purpose: a caller that can type a reference id can announce that the
    owner already approved work the owner never saw. The fixture therefore has to produce
    the state the product produces -- a reference in the store, approved for this project.
    """
    scope = studio.evidence_lessons.derive_project_scope_id(workspace)
    status, body = _post_json(
        base_url,
        token,
        "/api/reference-benchmarks",
        {
            "reference_id": COVERED_REFERENCE_ID,
            "version": 1,
            "project_scope_id": scope,
            "task_kind": "website",
            "brief": "the approved website direction this task should follow",
            "artifact": {
                "artifact_id": "hero.html",
                "content_sha256": digest,
                "measured_checks": {"layout": True, "copy": True},
            },
            "approval": {
                "actor": "owner:demo-fixture",
                "statement": "this page is the direction I approved; reuse its style",
                "granted": ["use_as_reference"],
            },
            "audience": "consumer",
            "language": "en",
            "style_tags": ["calm"],
            "checks": ["layout", "copy"],
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the covering reference could not be recorded: {status} {str(body)[:300]}"
        )
    return scope


def stage(
    studio, token: str, ids: dict[str, str], base_url: str, sample_path: Path,
    covered_workspace: Path,
) -> dict[str, str]:
    """Ask the real direction route for the three check-ins, on the running server."""
    digest = _sample_digest(sample_path)
    _stage_covering_reference(studio, token, base_url, covered_workspace, digest)

    # Startup recovery pauses a task that says `running` with no worker behind it, which is
    # correct: this fixture runs no model. A check-in is only taken on a running task, so the
    # walk's precondition is re-established through the state machine -- the same transition a
    # turn start uses -- rather than by writing the state directly.
    sm = studio.get_task_state_machine()
    for key, session_id in ids.items():
        state = sm.store.get_task(session_id).state
        if state != "running":
            sm.transition_task(session_id, "running", reason="turn_started")
            state = sm.store.get_task(session_id).state
        if state != "running":
            raise demo_tour.DemoEnvironmentError(
                f"the {key} session could not be put back to running (it is {state})"
            )

    # 1. Unbenchmarked work: the route writes the pause, and the walk answers it.
    waiting_status, waiting_body = _post_json(
        base_url,
        token,
        f"/api/harness/sessions/{ids['waiting']}/direction-checkpoint",
        {
            "task_kind": "website",
            "sample": {
                "artifact_id": "hero.html",
                "content_sha256": digest,
                "technical_checks": {"renders": True, "no_overflow": True},
            },
        },
    )
    if waiting_status != 200 or waiting_body.get("status") != "waiting":
        raise demo_tour.DemoEnvironmentError(
            f"the direction route did not stage a check-in: {waiting_status} {str(waiting_body)[:200]}"
        )

    # 2. Covered work: an owner-approved reference in this project already answers the
    #    question, so the run keeps going. The caller names the reference it is relying on;
    #    the route looks it up rather than believing the request.
    covered_status, covered_body = _post_json(
        base_url,
        token,
        f"/api/harness/sessions/{ids['covered']}/direction-checkpoint",
        {
            "task_kind": "website",
            "sample": {
                "artifact_id": "hero.html",
                "content_sha256": digest,
                "technical_checks": {"renders": True},
            },
            "reference_ids": [COVERED_REFERENCE_ID],
        },
    )
    if covered_status != 200 or covered_body.get("status") != "skipped":
        raise demo_tour.DemoEnvironmentError(
            f"a covered task was not left alone: {covered_status} {str(covered_body)[:200]}"
        )

    # 3. A hostile sample id and check name, to prove the panel draws text rather than markup.
    hostile_status, hostile_body = _post_json(
        base_url,
        token,
        f"/api/harness/sessions/{ids['hostile']}/direction-checkpoint",
        {
            "task_kind": "website",
            "sample": {
                "artifact_id": HOSTILE,
                "content_sha256": digest,
                "technical_checks": {HOSTILE: True},
            },
        },
    )
    if hostile_status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the hostile record could not be staged: {hostile_status} {str(hostile_body)[:200]}"
        )

    # The staged states are the assertion. A route that silently declined would leave the walk
    # reading a session that was never asked, and every panel check would pass vacuously.
    sm = studio.get_task_state_machine()
    if sm.store.get_task(ids["waiting"]).state != "waiting_approval":
        raise demo_tour.DemoEnvironmentError(
            "the unbenchmarked task was not left waiting_approval"
        )
    if sm.store.get_task(ids["covered"]).state != "running":
        raise demo_tour.DemoEnvironmentError("the covered task was not left running")
    return {"sample_sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 picks one)"
    )
    parser.add_argument(
        "--root", default="", help="reuse a directory as the throwaway environment"
    )
    parser.add_argument(
        "--ready-file", default="", help="also write the ready line to this file"
    )
    args = parser.parse_args(argv)

    try:
        environment = demo_tour.make_environment(
            Path(args.root).expanduser() if args.root else None
        )
        studio, token, ids, sample_path, covered_workspace = prepare(environment)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"harness direction seed: {exc}", file=sys.stderr)
        return 1

    port = args.port
    if port == 0:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

    ready: dict[str, object] = {
        "base_url": f"http://127.0.0.1:{port}",
        "token": token,
        "waiting_session_id": ids["waiting"],
        "covered_session_id": ids["covered"],
        "hostile_session_id": ids["hostile"],
        "root": str(environment.root),
    }

    # Stage the check-ins once the server is up and its own startup recovery has run. A thread
    # waits for health, then asks the real routes over HTTP: the states the walk reads are ones
    # the running server accepted, not rows written into a database before it booted. The ready
    # line is printed only after staging succeeds, so a fixture that failed to stage cannot be
    # mistaken for one that staged.
    def _stage_when_healthy() -> None:  # pragma: no cover - exercised by the walk
        import time
        import urllib.error
        import urllib.request

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2
                ):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            print(
                "harness direction seed: the server never became healthy",
                file=sys.stderr,
            )
            return
        try:
            made = stage(
                studio, token, ids, f"http://127.0.0.1:{port}", sample_path, covered_workspace
            )
        except Exception as exc:  # pragma: no cover - reported, not swallowed
            print(f"harness direction seed: staging failed: {exc}", file=sys.stderr)
            return
        ready["sample_sha256"] = made["sample_sha256"]
        line = json.dumps(ready)
        print(line, flush=True)
        if args.ready_file:
            Path(args.ready_file).write_text(line + "\n", encoding="utf-8")

    import threading

    threading.Thread(target=_stage_when_healthy, daemon=True).start()

    import uvicorn

    uvicorn.run(studio.app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
