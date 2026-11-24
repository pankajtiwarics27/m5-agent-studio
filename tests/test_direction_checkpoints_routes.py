"""AP-018: the direction check-in at the integrated routes.

`direction_checkpoints` decides whether to ask. These tests cover the part that decides
whether asking is *safe*: the pause is written into the task's own transition log so a
resume cannot leave it behind, an answer given about a different version is refused
instead of applied, and a refused answer never leaves a task that quietly moved on.

Each refusal is exercised, because a check-in that cannot be refused is a prompt.
"""

from __future__ import annotations

import json

import pytest

import direction_checkpoints

AUTH_TOKEN = "ap018-test-token"
SHA_A = "a" * 64
SHA_B = "b" * 64

from tests.test_harness_ap008_recovery import _harness_client  # noqa: E402  (shared fixture recipe)


def _running_session(studio):
    session = studio.create_harness_session_db(
        title="AP-018 direction",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        use_worktree=False,
        policy="standard",
        lead_model="",
        critic_model="",
    )
    sid = session["id"]
    sm = studio.get_task_state_machine()
    task, _ = sm.ensure_root_task(
        sid,
        session_id=sid,
        title="AP-018 direction",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        policy="standard",
    )
    if task.state != "running":
        sm.transition_task(sid, "running", reason="test_setup")
    return sid


def _sample(sha: str = SHA_A, checks: dict | None = None) -> dict:
    return {
        "artifact_id": "samples/hero.html",
        "content_sha256": sha,
        "technical_checks": checks if checks is not None else {"renders": True},
    }


def _checkpoint(client, sid: str, **body):
    payload = {"task_kind": "website", "sample": _sample()}
    payload.update(body)
    return client.post(
        f"/api/harness/sessions/{sid}/direction-checkpoint", json=payload
    )


def _answer(client, sid: str, **body):
    return client.post(f"/api/harness/sessions/{sid}/direction-answer", json=body)


def test_an_unbenchmarked_task_pauses_with_the_sample_and_the_question(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(client, sid)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "waiting"
    assert data["task"]["state"] == "waiting_approval"

    recorded = data["direction_checkpoint"]
    assert recorded["trigger"] == "no_benchmark"
    assert recorded["sample"]["content_sha256"] == SHA_A
    assert recorded["direction_approval_is_final_acceptance"] is False
    assert recorded["direction_approval_grants_publish_permission"] is False
    assert recorded["feedback_prompt"]

    # Recovered from the task's own transition log, not from a side table.
    task = client.get(f"/api/harness/sessions/{sid}/task").json()
    assert task["direction_checkpoint"]["sample"]["content_sha256"] == SHA_A
    assert task["direction_checkpoints_taken"] == 1
    waiting = [t for t in task["transitions"] if t["to_state"] == "waiting_approval"]
    assert len(waiting) == 1
    assert waiting[0]["reason"].startswith("direction_checkpoint:")


def test_a_covered_task_is_not_interrupted_but_the_decision_is_still_reported(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(
        client,
        sid,
        benchmarks=[
            {
                "reference_id": "ref/website-v1",
                "reference_sha256": "c" * 64,
                "covered_task_kinds": ["website"],
                "measured_checks": ["layout"],
            }
        ],
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "skipped"
    assert data["task"]["state"] == "running"
    assert data["direction_checkpoint"]["trigger"] == "benchmark_covered"
    assert (
        client.get(f"/api/harness/sessions/{sid}/task").json()[
            "direction_checkpoints_taken"
        ]
        == 0
    )


def test_material_uncertainty_escalates_even_with_a_covering_benchmark(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(
        client,
        sid,
        benchmarks=[
            {
                "reference_id": "ref/website-v1",
                "reference_sha256": "c" * 64,
                "covered_task_kinds": ["website"],
            }
        ],
        uncertainties=["new_format"],
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["direction_checkpoint"]["trigger"]
        == "uncertainty_exceeds_coverage"
    )


def test_a_check_in_on_a_task_that_is_not_running_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    sm = studio.get_task_state_machine()
    sm.transition_task(sid, "paused", reason="test_setup")

    response = _checkpoint(client, sid)
    assert response.status_code == 409
    assert "only taken on a running task" in response.json()["detail"]


def test_a_broken_sample_is_not_offered_for_judgement(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(client, sid, sample=_sample(checks={"renders": False}))
    assert response.status_code == 400
    assert "checks" in response.json()["detail"]


def test_a_sample_digest_that_is_not_a_digest_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(client, sid, sample=_sample(sha="not-a-digest"))
    assert response.status_code == 400


def test_an_invented_uncertainty_cannot_escalate(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _checkpoint(client, sid, uncertainties=["vibes"])
    assert response.status_code == 400
    assert "unknown material uncertainty" in response.json()["detail"]


def test_an_unknown_session_is_404_and_an_ill_formed_id_never_reaches_the_store(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert _checkpoint(client, "no-such-session").status_code == 404
    # A path-traversal shaped id is a 404, not a lookup.
    response = client.post(
        "/api/harness/sessions/..%2F..%2Fetc/direction-checkpoint",
        json={"task_kind": "website", "sample": _sample()},
    )
    assert response.status_code == 404


def test_two_pauses_cannot_stack_on_one_task(tmp_path, monkeypatch):
    """A second pause over a live one would let the first answer land on moved-on work."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    assert _checkpoint(client, sid).status_code == 200

    second = _checkpoint(client, sid)
    assert second.status_code == 409
    assert "already waiting on the owner" in second.json()["detail"]


def test_keep_continues_and_anything_else_redirects(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    _checkpoint(client, sid)

    kept = _answer(
        client,
        sid,
        answer="keep",
        answered_artifact_sha256=SHA_A,
        current_artifact_sha256=SHA_A,
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["outcome"] == "continue"
    assert kept.json()["task"]["state"] == "running"
    # Direction approval is never reported as acceptance or permission.
    assert kept.json()["direction_is_final_acceptance"] is False
    assert kept.json()["grants_publish_permission"] is False

    # A second question on the same task, answered with direction instead of assent.
    assert _checkpoint(client, sid).status_code == 200
    redirected = _answer(
        client,
        sid,
        answer="warmer, and shorter",
        answered_artifact_sha256=SHA_A,
        current_artifact_sha256=SHA_A,
    )
    assert redirected.status_code == 200, redirected.text
    assert redirected.json()["outcome"] == "redirect"


def test_an_answer_about_a_different_version_is_refused_and_leaves_the_pause_intact(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    _checkpoint(client, sid)

    stale = _answer(
        client,
        sid,
        answer="keep",
        answered_artifact_sha256=SHA_A,
        current_artifact_sha256=SHA_B,
    )
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]

    # The pause survives a refused answer: the owner can still answer the question they were asked.
    task = client.get(f"/api/harness/sessions/{sid}/task").json()
    assert task["task"]["state"] == "waiting_approval"
    assert (
        _answer(
            client,
            sid,
            answer="keep",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_A,
        ).status_code
        == 200
    )


def test_answering_a_task_that_was_never_asked_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)

    response = _answer(client, sid, answer="keep")
    assert response.status_code == 409
    assert "no direction check-in" in response.json()["detail"]


def test_an_empty_answer_is_not_a_direction(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    _checkpoint(client, sid)

    response = _answer(client, sid, answer="   ")
    assert response.status_code == 400
    assert (
        client.get(f"/api/harness/sessions/{sid}/task").json()["task"]["state"]
        == "waiting_approval"
    )


def test_a_forged_stored_flag_cannot_turn_a_direction_into_permission(
    tmp_path, monkeypatch
):
    """The stored record is re-validated, so editing the row is not a way to grant approval."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    recorded = _checkpoint(client, sid).json()["direction_checkpoint"]
    recorded["direction_approval_grants_publish_permission"] = True

    with pytest.raises(
        direction_checkpoints.DirectionError, match="never carries that"
    ):
        direction_checkpoints.DirectionCheckpoint.from_mapping(recorded)


def test_a_recorded_checkpoint_round_trips_through_its_stored_form(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    recorded = _checkpoint(client, sid).json()["direction_checkpoint"]

    rebuilt = direction_checkpoints.DirectionCheckpoint.from_mapping(recorded)
    assert rebuilt.to_mapping() == recorded
    assert (
        rebuilt.fingerprint()
        == direction_checkpoints.DirectionCheckpoint.from_mapping(
            json.loads(json.dumps(recorded))
        ).fingerprint()
    )


def test_the_answer_route_never_writes_acceptance_or_adopts_a_benchmark(
    tmp_path, monkeypatch
):
    """Any acceptance-shaped flag the log carries must be false, and no reference is adopted."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    _checkpoint(client, sid)
    _answer(
        client,
        sid,
        answer="keep",
        answered_artifact_sha256=SHA_A,
        current_artifact_sha256=SHA_A,
    )

    task = client.get(f"/api/harness/sessions/{sid}/task").json()
    flags: dict[str, object] = {}

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if "acceptance" in key.lower() or "publish" in key.lower():
                    flags[key] = value
                collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    for row in task["transitions"]:
        collect(row["evidence"])

    assert flags, (
        "the checkpoint record should carry the acceptance flags it refuses, not omit them"
    )
    assert all(value is False for value in flags.values()), flags
    # Direction answers never adopt a reference: that is AP-019/AP-020's decision, with its own approval.
    assert task["direction_checkpoint"]["benchmark_reference_id"] == ""
    assert task["task"]["state"] == "running"


def test_the_pause_survives_a_fresh_process_read(tmp_path, monkeypatch):
    """The pause is durable: it is in the task log, so a new reader sees it without the writer."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _running_session(studio)
    _checkpoint(client, sid)

    studio._reset_task_state_machine_for_tests() if hasattr(
        studio, "_reset_task_state_machine_for_tests"
    ) else None
    reloaded = client.get(f"/api/harness/sessions/{sid}/task").json()
    assert reloaded["task"]["state"] == "waiting_approval"
    assert reloaded["direction_checkpoint"]["sample"]["content_sha256"] == SHA_A


PROBE = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "scripts"
    / "ui-probes"
    / "direction-panel.mjs"
)


def _probe_runs() -> bool:
    import subprocess

    return (
        subprocess.run(
            ["node", "-e", "require.resolve(process.env.JSDOM_PATH || 'jsdom')"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def test_the_shipped_direction_panel_escapes_data_and_states_the_limit():
    """The panel is proven at the DOM level when jsdom is reachable, and skipped loudly otherwise.

    A skipped probe is not a passing one: CI's Chromium walks remain the evidence for layout.
    What this pins is the property that matters here -- a sample id, a question or a rationale
    from outside the pane becomes text, and the panel still says approval is not acceptance.
    """
    import os
    import subprocess

    if not _probe_runs():
        pytest.skip("jsdom is not installed; the DOM probe cannot run here")
    env = dict(os.environ)
    env.setdefault("JSDOM_PATH", "jsdom")
    result = subprocess.run(
        ["node", str(PROBE)],
        capture_output=True,
        text=True,
        cwd=PROBE.parents[2],
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PROBE CHECKS PASSED" in result.stdout
