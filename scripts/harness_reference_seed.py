#!/usr/bin/env python3
"""Serve the isolated demo instance with reference benchmarks staged through the real routes (AP-020).

    python3 scripts/harness_reference_seed.py            # serve; prints one JSON line

A reference pane is not a card a walk can draw. Every fact it shows -- which artifact version the
owner approved, in whose words, whether the version was replaced, why a reference was offered for
a task, and what it therefore does *not* settle -- comes from durable record written by the routes
the product uses. This fixture does what the product does instead of faking a screenshot: it
creates sessions with a committed checkpoint carrying a real artifact reference (what the pane
binds a version to), and asks the real routes for every record the walk reads.

* ``main``     - a reference the owner approved, plus a second version of it that the owner
                 approved separately. Version 1 is superseded, so the pane must show the earlier
                 approval exactly as it was given and point forward;
* ``stale``    - a reference that was **revoked**, so the pane must keep the record and show the
                 status rather than dropping the row;
* ``hostile``  - a reference whose id, brief and approval words are markup, to prove the pane
                 draws text;
* ``other``    - a session in a **different project**, whose reference must not be offered to the
                 others: scope is derived, not named.

The records are staged from a **startup hook**, not before the server runs, and go over HTTP so
they pass through the same auth middleware a browser does.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "main_session_id": "hns-...", "stale_session_id": "hns-...",
     "hostile_session_id": "hns-...", "other_session_id": "hns-...",
     "artifact_id": "hero.html", "reference_id": "ref/website",
     "current_sha256": "...", "second_sha256": "...", "project_scope_id": "proj-..."}
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

SECOND_SOURCE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Ledger</title></head>
<body><main><h1>Your money, in plainer words</h1><p>Every entry, dated and explained.</p></main></body></html>
"""

# Words, an id and a brief a hostile run could put in the record. The pane draws them as text;
# the walk asserts no element, no handler and no page error appears.
HOSTILE = '<script>globalThis.pwned=1</script><img src=x onerror="globalThis.pwned=1">'

ARTIFACT_ID = "hero.html"
REFERENCE_ID = "ref/website"

APPROVAL_WORDS = "this is the calm, plain tone I meant"
SECOND_WORDS = "the plainer second draft is the one"


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_reference", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _build_workspace(root: Path) -> Path:
    """A workspace holding the current page, so the project scope is a real directory."""
    workspace = root / "reference-project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ARTIFACT_ID).write_text(CURRENT_SOURCE, encoding="utf-8")
    return workspace


def prepare(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and create four sessions, each in the project it belongs to."""
    studio = _demo_cli()._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    # The middleware compares against the module constant, which is read at import time. The
    # fixture asks the real routes in this same process, so it binds the token it just created.
    studio.STUDIO_API_TOKEN = token

    workspace = _build_workspace(environment.root)
    other_workspace = environment.root / "another-project"
    other_workspace.mkdir(parents=True, exist_ok=True)
    (other_workspace / ARTIFACT_ID).write_text(CURRENT_SOURCE, encoding="utf-8")

    current_sha = hashlib.sha256(CURRENT_SOURCE.encode("utf-8")).hexdigest()
    second_sha = hashlib.sha256(SECOND_SOURCE.encode("utf-8")).hexdigest()

    sm = studio.get_task_state_machine()
    ids: dict[str, str] = {}
    for key, title, workspace_dir in (
        ("main", "AP-020 walk: approved references", workspace),
        ("stale", "AP-020 walk: a revoked reference", workspace),
        ("hostile", "AP-020 walk: hostile text", workspace),
        ("other", "AP-020 walk: another project", other_workspace),
    ):
        session = studio.create_harness_session_db(
            title=title,
            workspace_dir=str(workspace_dir),
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
            workspace_dir=str(workspace_dir),
            policy="standard",
        )
        # A checkpoint naming the artifact is what gives the pane a version to adopt. The
        # digest is the real one for the file this session's workspace holds.
        sm.checkpoints.save_checkpoint(
            task.task_id,
            step=1,
            payload={"workflow": "harness", "completed_step": 1, "note": "produced the page"},
            evidence_refs=[
                studio.workflow_checkpoints.EvidenceRef(
                    kind="artifact",
                    ref_id=ARTIFACT_ID,
                    sha256=current_sha,
                    summary="the page this session produced",
                )
            ],
        )
        studio._append_harness_event(
            session_id,
            "turn/start",
            {"prompt": f"{title}: build the page", "policy": "standard"},
            dedupe_key=f"seed-reference-turn-start:{session_id}",
        )
        ids[key] = session_id

    scope = studio.evidence_lessons.derive_project_scope_id(workspace)
    other_scope = studio.evidence_lessons.derive_project_scope_id(other_workspace)
    return studio, token, ids, {
        "current": current_sha,
        "second": second_sha,
        "scope": scope,
        "other_scope": other_scope,
    }


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
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:400]}


def _post(base_url: str, token: str, path: str, body: dict) -> tuple[int, dict]:
    return _request(base_url, token, path, body, "POST")


def _approval(actor: str, statement: str, *, adopt: bool = True) -> dict:
    granted = ["use_as_reference"] if adopt else []
    return {"actor": actor, "statement": statement, "granted": granted}


def _record(
    base_url: str,
    token: str,
    *,
    reference_id: str,
    version: int,
    scope: str,
    brief: str,
    artifact_id: str,
    digest: str,
    approval: dict,
) -> dict:
    status, body = _post(
        base_url,
        token,
        "/api/reference-benchmarks",
        {
            "reference_id": reference_id,
            "version": version,
            "project_scope_id": scope,
            "task_kind": "website",
            "brief": brief,
            "artifact": {
                "artifact_id": artifact_id,
                "content_sha256": digest,
                "measured_checks": {"layout": True, "overflow": True},
            },
            "approval": approval,
            "audience": "consumer",
            "language": "en",
            "style_tags": ["calm", "plain"],
            "checks": ["layout"],
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"recording {reference_id}@v{version} was refused: {status} {str(body)[:300]}"
        )
    return body


def stage(studio, token: str, ids: dict[str, str], base_url: str, digests: dict) -> dict:
    """Write every record the walk reads, through the routes the product uses."""
    scope = digests["scope"]

    # 1. The approved reference, then an owner-approved second version of it. Version 1 becomes
    #    superseded, and its approval must still read as the words it was given in.
    _record(
        base_url,
        token,
        reference_id=REFERENCE_ID,
        version=1,
        scope=scope,
        brief="a landing page for the ledger app, for consumers",
        artifact_id=ARTIFACT_ID,
        digest=digests["current"],
        approval=_approval("owner:priya", APPROVAL_WORDS),
    )
    status, body = _post(
        base_url,
        token,
        f"/api/reference-benchmarks/{REFERENCE_ID}/enhance",
        {
            "from_version": 1,
            "new_version": 2,
            "artifact": {
                "artifact_id": ARTIFACT_ID,
                "content_sha256": digests["second"],
                "measured_checks": {"layout": True, "overflow": True},
            },
            "approval": _approval("owner:priya", SECOND_WORDS),
            "brief": "a landing page for the ledger app, plainer",
        },
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the enhancement was refused: {status} {str(body)[:300]}"
        )
    history = _request(base_url, token, f"/api/reference-benchmarks/{REFERENCE_ID}", None, "GET")[1]
    versions = history.get("versions") or []
    if [row.get("version") for row in versions] != [1, 2]:
        raise demo_tour.DemoEnvironmentError(
            f"the reference does not keep both versions: {versions!r}"
        )
    if versions[0].get("approval", {}).get("statement") != APPROVAL_WORDS:
        raise demo_tour.DemoEnvironmentError(
            "the first version's approval was rewritten by the enhancement"
        )

    # 2. A reference that was revoked. The pane must keep the record and show the status.
    _record(
        base_url,
        token,
        reference_id="ref/revoked-banner",
        version=1,
        scope=scope,
        brief="a banner that was later judged wrong for this audience",
        artifact_id=ARTIFACT_ID,
        digest=digests["current"],
        approval=_approval("owner:priya", "this banner was right at the time"),
    )
    status, body = _post(
        base_url,
        token,
        "/api/reference-benchmarks/ref/revoked-banner/revoke",
        {"version": 1, "reason": "the audience changed", "actor": "owner:priya"},
    )
    if status != 200:
        raise demo_tour.DemoEnvironmentError(
            f"the revocation was refused: {status} {str(body)[:300]}"
        )
    if body.get("reference", {}).get("status") != "revoked":
        raise demo_tour.DemoEnvironmentError("the revoked reference did not report its status")

    # 3. Hostile text in the id, the brief and the owner's words. The record keeps what was said;
    #    the pane must draw it as text.
    _record(
        base_url,
        token,
        reference_id=f"ref/hostile-{HOSTILE[:24]}",
        version=1,
        scope=scope,
        brief=HOSTILE,
        artifact_id=ARTIFACT_ID,
        digest=digests["current"],
        approval=_approval(f"owner:{HOSTILE[:20]}", HOSTILE),
    )

    # 4. Another project's reference. It must never be offered to the sessions above.
    _record(
        base_url,
        token,
        reference_id="ref/other-project",
        version=1,
        scope=digests["other_scope"],
        brief="a page for a different project entirely",
        artifact_id=ARTIFACT_ID,
        digest=digests["current"],
        approval=_approval("owner:priya", "approved for the other project"),
    )

    # 5. What the pane must be able to say about coverage, read from the routes: the project is
    #    covered for website/layout and not for a check nobody measured.
    covered = _post(
        base_url,
        token,
        "/api/reference-benchmarks/coverage",
        {"project_scope_id": scope, "task_kind": "website", "checks": ["layout"]},
    )[1]
    if not covered.get("covered"):
        raise demo_tour.DemoEnvironmentError(
            f"the approved reference did not cover the task: {covered!r}"
        )
    partial = _post(
        base_url,
        token,
        "/api/reference-benchmarks/coverage",
        {"project_scope_id": scope, "task_kind": "website", "checks": ["layout", "a11y"]},
    )[1]
    if partial.get("covered") or partial.get("missing") != ["check:a11y"]:
        raise demo_tour.DemoEnvironmentError(
            f"an unmeasured check was not reported missing: {partial!r}"
        )

    return {
        "project_scope_id": scope,
        "other_project_scope_id": digests["other_scope"],
        "approval_words": APPROVAL_WORDS,
        "second_words": SECOND_WORDS,
    }


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
        print(f"harness reference seed: {exc}", file=sys.stderr)
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
        "other_session_id": ids["other"],
        "artifact_id": ARTIFACT_ID,
        "reference_id": REFERENCE_ID,
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
                "harness reference seed: the server never became healthy",
                file=sys.stderr,
            )
            return
        try:
            made = stage(studio, token, ids, f"http://127.0.0.1:{port}", digests)
        except Exception as exc:  # pragma: no cover - reported, not swallowed
            print(f"harness reference seed: staging failed: {exc}", file=sys.stderr)
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
