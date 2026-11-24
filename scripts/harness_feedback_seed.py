#!/usr/bin/env python3
"""Serve the isolated demo instance with scoped feedback staged through the real routes (AP-019).

    python3 scripts/harness_feedback_seed.py            # serve; prints one JSON line

A feedback pane is not a card a walk can draw. Every fact it shows -- what was said, the version
it was said about, whether the reading was confirmed, which comments contradict each other and
which ones no longer describe what is on screen -- comes from durable record written by the
routes the product uses. The demo has no model runtime, so no turn can be run to produce them.

This fixture therefore does what the product does instead of faking a screenshot. It creates
sessions with a committed checkpoint carrying a real artifact reference (which is what the pane
binds feedback to), and it asks the real routes for every record the walk reads:

* ``main``     - an element-scoped correction whose reading is *not* confirmed (the pane must
                 ask which reading was meant), a plain-words correction with no selection at
                 all (plain text is never a second-class path), and a second correction on the
                 same region that requires the first one be read as something specific, so the
                 pair contradicts itself (the pane must show the conflict rather than average
                 it away);
* ``stale``    - feedback recorded against an *earlier* version of the same artifact, plus the
                 current version, so the pane has something that is genuinely out of date and
                 must show it as stale instead of applying it;
* ``hostile``  - feedback whose words, id and region are markup, to prove the pane draws text.

The records are staged from a **startup hook**, not before the server runs, and go over HTTP so
they pass through the same auth middleware a browser does.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "main_session_id": "hns-...", "stale_session_id": "hns-...",
     "hostile_session_id": "hns-...", "artifact_id": "hero.html",
     "current_sha256": "...", "old_sha256": "..."}
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

CURRENT_SOURCE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ledger</title></head>
<body><main><h1>Your money, in plain words</h1><p>Every entry, dated.</p></main></body></html>
"""

# An earlier version of the same artifact. The pane must not apply feedback about this to the
# bytes above: "quieter heading" was said about a heading that no longer exists.
OLD_SOURCE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ledger</title></head>
<body><main><h1>LEDGER</h1><p>Every entry, dated.</p></main></body></html>
"""

# Words, an id and a region a hostile run could put in the record. The pane draws them as text;
# the walk asserts no element, no handler and no page error appears.
HOSTILE = '<script>globalThis.pwned=1</script><img src=x onerror="globalThis.pwned=1">'

ARTIFACT_ID = "hero.html"


def _git(workspace: Path, *argv: str) -> None:
    """Run one git command in the fixture workspace, failing loudly if it does not work."""
    import subprocess

    done = subprocess.run(
        ["git", *argv], cwd=str(workspace), capture_output=True, text=True, timeout=60
    )
    if done.returncode != 0:
        raise demo_tour.DemoEnvironmentError(
            f"git {' '.join(argv)} failed in the fixture workspace: {done.stderr[:200]}"
        )


def _build_workspace(root: Path) -> Path:
    """A committed git workspace with one uncommitted change, so the pane can draw a real diff.

    A region a reader can select is a file in the change, so the change has to exist. It is a
    real worktree with a real `git diff`, not a fabricated payload: the pane reads the same
    `/diff` route the product reads.
    """
    workspace = root / "current"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ARTIFACT_ID).write_text(OLD_SOURCE, encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "walk@example.invalid")
    _git(workspace, "config", "user.name", "AP-019 walk")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "baseline")
    # The change the run made, uncommitted, so the diff the pane draws is the worktree's own.
    (workspace / ARTIFACT_ID).write_text(CURRENT_SOURCE, encoding="utf-8")
    return workspace


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_feedback", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def prepare(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and create three sessions that each record a real version."""
    studio = _demo_cli()._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    # The middleware compares against the module constant, which is read at import time. The
    # fixture asks the real routes in this same process, so it binds the token it just created.
    studio.STUDIO_API_TOKEN = token

    current_workspace = _build_workspace(environment.root)

    # The earlier bytes are not needed on disk: what makes them a version is the digest the
    # feedback record pins, and the product never opens the file to check it.

    current_sha = hashlib.sha256(CURRENT_SOURCE.encode("utf-8")).hexdigest()
    old_sha = hashlib.sha256(OLD_SOURCE.encode("utf-8")).hexdigest()

    sm = studio.get_task_state_machine()
    ids: dict[str, str] = {}
    # The `stale` session records the CURRENT version -- that is the whole scenario: feedback was
    # given about the earlier bytes, the artifact has since changed, so the comment no longer
    # describes what is on screen. Pinning its checkpoint to the old digest would make the old
    # comment current, and the walk would be asserting a situation the product never claimed.
    for key, title, digest, workspace in (
        ("main", "AP-019 walk: scoped corrections", current_sha, current_workspace),
        ("stale", "AP-019 walk: an older version", current_sha, current_workspace),
        ("hostile", "AP-019 walk: hostile text", current_sha, current_workspace),
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
        # A checkpoint that names the artifact is what gives the pane a version to bind to. The
        # digest is the real one for the file this session's workspace holds.
        sm.checkpoints.save_checkpoint(
            task.task_id,
            step=1,
            payload={
                "workflow": "harness",
                "completed_step": 1,
                "note": "produced the page",
            },
            evidence_refs=[
                studio.workflow_checkpoints.EvidenceRef(
                    kind="artifact",
                    ref_id=ARTIFACT_ID,
                    sha256=digest,
                    summary="the page this session produced",
                )
            ],
        )
        studio._append_harness_event(
            session_id,
            "turn/start",
            {"prompt": f"{title}: build the page", "policy": "standard"},
            dedupe_key=f"seed-feedback-turn-start:{session_id}",
        )
        ids[key] = session_id

    return studio, token, ids, {"current": current_sha, "old": old_sha}


def _request(
    base_url: str, token: str, path: str, body: dict | None, method: str
) -> tuple[int, dict]:
    """Ask a real route over HTTP, so staging goes through the middleware too."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{base_url}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")[:400]}


def _post(base_url: str, token: str, path: str, body: dict) -> tuple[int, dict]:
    return _request(base_url, token, path, body, "POST")


def stage(
    studio, token: str, ids: dict[str, str], base_url: str, digests: dict[str, str]
) -> dict[str, str]:
    """Ask the real feedback routes for every record the walk reads."""
    main = ids["main"]
    base = f"/api/harness/sessions/{main}/feedback"

    # 1. A region-scoped correction. The words admit more than one reading, so the record is
    #    material and the revision is not ready until the owner says which reading was meant.
    status, body = _post(
        base_url,
        token,
        base,
        {
            "feedback_id": "fb-walk-quieter",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": digests["current"],
            "verbatim": "the heading is a bit much",
            "selection": {
                "locator_kind": "element",
                "locator": "main > h1",
                "artifact_sha256": digests["current"],
            },
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"element feedback was refused: {status} {str(body)[:200]}"
        )
    # The derived list, not a stored flag: `needs_confirmation` is computed from material and
    # confirmed, so the fixture checks the answer a reader would actually act on.
    if "fb-walk-quieter" not in (body.get("awaiting_confirmation") or []):
        raise demo_tour.DemoEnvironmentError(
            "the ambiguous correction was not recorded as needing confirmation"
        )

    # 2. Correcting a different region, in a paragraph, with no selection at all. This is the
    #    plain-text path: no region, no precision, and still a first-class record.
    status, body = _post(
        base_url,
        token,
        base,
        {
            "feedback_id": "fb-walk-date",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": digests["current"],
            "verbatim": "say where the dates come from, in the line under the heading",
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"plain-text feedback was refused: {status} {str(body)[:200]}"
        )
    if body.get("feedback", {}).get("scope_kind") != "artifact":
        raise demo_tour.DemoEnvironmentError(
            "a comment with no selection was not recorded at artifact scope"
        )

    # 3. A second correction on the *same* region, through a quick action, that keeps what the
    #    first one was ambiguous about. Two live comments on `main > h1` that cannot both be
    #    acted on: the pane must show the pair rather than silently picking one.
    status, body = _post(
        base_url,
        token,
        base,
        {
            "feedback_id": "fb-walk-keep",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": digests["current"],
            "quick_action": "keep",
            "selection": {
                "locator_kind": "element",
                "locator": "main > h1",
                "artifact_sha256": digests["current"],
            },
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"quick-action feedback was refused: {status} {str(body)[:200]}"
        )
    if "fb-walk-keep" in (body.get("awaiting_confirmation") or []):
        raise demo_tour.DemoEnvironmentError(
            "a bare quick action should not need its reading confirmed"
        )

    status, body = _request(
        base_url,
        token,
        f"{base}?artifact_id={ARTIFACT_ID}&artifact_sha256={digests['current']}",
        None,
        "GET",
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the feedback list could not be read: {status} {str(body)[:200]}"
        )
    if not body.get("conflicts"):
        raise demo_tour.DemoEnvironmentError(
            "two contradictory corrections on one region did not register as a conflict"
        )
    if body.get("revision", {}).get("ready") is not False:
        raise demo_tour.DemoEnvironmentError(
            "a revision was reported ready while a reading was still unconfirmed"
        )

    # 4. An earlier version. Feedback was given about the OLD heading; the session now records
    #    the new bytes. The pane must show the old comments as stale, not apply them.
    stale = ids["stale"]
    stale_base = f"/api/harness/sessions/{stale}/feedback"
    status, body = _post(
        base_url,
        token,
        stale_base,
        {
            "feedback_id": "fb-walk-old-heading",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": digests["old"],
            "verbatim": "the all-caps heading shouts",
            "selection": {
                "locator_kind": "element",
                "locator": "main > h1",
                "artifact_sha256": digests["old"],
            },
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"older-version feedback was refused: {status} {str(body)[:200]}"
        )
    status, body = _request(
        base_url,
        token,
        f"{stale_base}?artifact_id={ARTIFACT_ID}&artifact_sha256={digests['current']}",
        None,
        "GET",
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the stale view could not be read: {status} {str(body)[:200]}"
        )
    stale_items = body.get("stale") or []
    if not any(
        item.get("feedback_id") == "fb-walk-old-heading" for item in stale_items
    ):
        raise demo_tour.DemoEnvironmentError(
            "feedback on the older version was not reported as stale"
        )

    # 5. Hostile text: an id, words and a region that are markup. They must be stored verbatim
    #    and drawn as text -- the record keeps what was said, not a cleaned-up version of it.
    hostile = ids["hostile"]
    status, body = _post(
        base_url,
        token,
        f"/api/harness/sessions/{hostile}/feedback",
        {
            "feedback_id": "fb-walk-hostile",
            "artifact_id": ARTIFACT_ID,
            "artifact_sha256": digests["current"],
            "verbatim": HOSTILE,
            "selection": {
                "locator_kind": "element",
                "locator": HOSTILE,
                "artifact_sha256": digests["current"],
            },
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"hostile-text feedback was refused: {status} {str(body)[:200]}"
        )

    return {"current_sha256": digests["current"], "old_sha256": digests["old"]}


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
        studio, token, ids, digests = prepare(environment)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"harness feedback seed: {exc}", file=sys.stderr)
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
        "main_session_id": ids["main"],
        "stale_session_id": ids["stale"],
        "hostile_session_id": ids["hostile"],
        "artifact_id": ARTIFACT_ID,
        "hostile_text": HOSTILE,
        "root": str(environment.root),
    }

    # Stage once the server is up: the records the walk reads are ones the running server
    # accepted over HTTP, not rows written into a database before it booted. The ready line is
    # printed only after staging succeeds, so a fixture that failed to stage cannot be mistaken
    # for one that staged.
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
                "harness feedback seed: the server never became healthy",
                file=sys.stderr,
            )
            return
        try:
            made = stage(studio, token, ids, f"http://127.0.0.1:{port}", digests)
        except Exception as exc:  # pragma: no cover - reported, not swallowed
            print(f"harness feedback seed: staging failed: {exc}", file=sys.stderr)
            return
        ready.update(made)
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
