"""AP-019: scoped feedback at the integrated routes.

The module decides what a correction is allowed to mean. These tests cover the part that
decides whether recording it is *safe*: that the version is pinned, that a preference cannot be
smuggled in through a comment, that withdrawing feedback keeps the record, and that nothing
written here can be read later as acceptance or permission.
"""

from __future__ import annotations

SHA_A = "a" * 64
SHA_B = "b" * 64

from tests.test_harness_ap008_recovery import _harness_client  # noqa: E402  (shared fixture recipe)


def _session(studio) -> str:
    session = studio.create_harness_session_db(
        title="AP-019 scoped feedback",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        use_worktree=False,
        policy="standard",
        lead_model="",
        critic_model="",
    )
    return str(session["id"])


def _capture(client, sid: str, **body):
    payload = {
        "artifact_id": "hero.html",
        "artifact_sha256": SHA_A,
        "verbatim": "this heading is too loud",
    }
    payload.update(body)
    return client.post(f"/api/harness/sessions/{sid}/feedback", json=payload)


def _list(client, sid: str, sha: str = SHA_A, artifact: str = "hero.html"):
    return client.get(
        f"/api/harness/sessions/{sid}/feedback?artifact_id={artifact}&artifact_sha256={sha}"
    )


def test_a_comment_is_recorded_against_exactly_one_version(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    response = _capture(client, sid)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "captured"
    assert body["feedback"]["artifact_sha256"] == SHA_A
    assert body["feedback"]["verbatim"] == "this heading is too loud"
    assert body["feedback"]["scope_kind"] == "artifact"
    assert body["feedback"]["is_final_acceptance"] is False
    assert body["feedback"]["grants_publish_permission"] is False

    listed = _list(client, sid).json()
    assert len(listed["items"]) == 1
    assert listed["revision"]["ready"] is False  # words are material until confirmed


def test_a_selection_makes_it_element_scoped_and_never_blocks_the_comment(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    scoped = _capture(
        client,
        sid,
        selection={
            "locator_kind": "element",
            "locator": "h1.hero",
            "artifact_sha256": SHA_A,
        },
    )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["feedback"]["scope_kind"] == "element"

    # The plain-text fallback is the same route with no selection: precision is optional.
    plain = _capture(client, sid, feedback_id="f2", verbatim="the footer feels rushed")
    assert plain.status_code == 200, plain.text
    assert plain.json()["feedback"]["scope_kind"] == "artifact"


def test_a_preference_cannot_be_written_through_the_capture_route(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    response = _capture(client, sid, scope_kind="preference")
    assert response.status_code == 400
    assert "promote it explicitly" in response.json()["detail"]


def test_feedback_without_a_version_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    response = client.post(
        f"/api/harness/sessions/{sid}/feedback",
        json={"artifact_id": "hero.html", "verbatim": "x"},
    )
    assert response.status_code == 400
    assert "pinned to the version" in response.json()["detail"]

    listing = client.get(f"/api/harness/sessions/{sid}/feedback?artifact_id=hero.html")
    assert listing.status_code == 400
    assert "cannot be checked for staleness" in listing.json()["detail"]


def test_a_selection_from_a_different_version_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    response = _capture(
        client,
        sid,
        selection={
            "locator_kind": "element",
            "locator": "h1",
            "artifact_sha256": SHA_B,
        },
    )
    assert response.status_code == 400
    assert "different artifact version" in response.json()["detail"]


def test_a_bare_quick_action_records_without_typing(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)

    response = client.post(
        f"/api/harness/sessions/{sid}/feedback",
        json={
            "artifact_id": "hero.html",
            "artifact_sha256": SHA_A,
            "quick_action": "shorter",
        },
    )
    assert response.status_code == 200, response.text
    feedback = response.json()["feedback"]
    assert feedback["verbatim"] == "make this shorter"
    assert feedback["quick_action"] == "shorter"
    # A control that names its own change is not a judgement, so a revision can proceed.
    assert response.json()["revision"]["ready"] is True


def test_confirming_the_reading_unblocks_the_revision(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    recorded = _capture(client, sid).json()["feedback"]
    assert recorded["needs_confirmation"] if "needs_confirmation" in recorded else True

    confirmed = client.post(
        f"/api/harness/sessions/{sid}/feedback/{recorded['feedback_id']}/confirm",
        json={"confirmed_by": "owner"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["feedback"]["confirmed"] is True
    assert confirmed.json()["revision"]["ready"] is True
    assert confirmed.json()["awaiting_confirmation"] == []


def test_confirming_a_missing_record_is_404(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    response = client.post(
        f"/api/harness/sessions/{sid}/feedback/nope/confirm", json={"confirmed_by": "o"}
    )
    assert response.status_code == 404


def test_promotion_requires_a_basis_and_is_the_only_way_to_widen(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    recorded = client.post(
        f"/api/harness/sessions/{sid}/feedback",
        json={
            "artifact_id": "hero.html",
            "artifact_sha256": SHA_A,
            "quick_action": "shorter",
            "material": False,
        },
    ).json()["feedback"]

    missing = client.post(
        f"/api/harness/sessions/{sid}/feedback/{recorded['feedback_id']}/promote",
        json={"basis": ""},
    )
    assert missing.status_code == 400
    assert "basis" in missing.json()["detail"]

    promoted = client.post(
        f"/api/harness/sessions/{sid}/feedback/{recorded['feedback_id']}/promote",
        json={
            "basis": "they asked for the same in three artifacts",
            "promoted_by": "owner",
        },
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["feedback"]["scope_kind"] == "preference"
    assert promoted.json()["feedback"]["confirmed_by"] == "owner"


def test_withdrawing_feedback_keeps_the_record_and_clears_the_live_set(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    recorded = _capture(client, sid).json()["feedback"]

    removed = client.delete(
        f"/api/harness/sessions/{sid}/feedback/{recorded['feedback_id']}"
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["feedback"]["deleted"] is True
    assert removed.json()["live"] == []
    # The words remain readable: "what was asked for, and when was it withdrawn".
    assert removed.json()["feedback"]["verbatim"] == "this heading is too loud"

    again = client.delete(f"/api/harness/sessions/{sid}/feedback/missing")
    assert again.status_code == 404


def test_feedback_on_an_older_version_is_reported_stale_not_applied(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    _capture(client, sid)

    current = _list(client, sid, sha=SHA_B).json()
    assert len(current["stale"]) == 1
    assert current["revision"]["changes"] == []
    assert current["revision"]["ready"] is False


def test_two_opposing_corrections_are_reported_rather_than_merged(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    selection = {"locator_kind": "element", "locator": "h1", "artifact_sha256": SHA_A}
    _capture(client, sid, feedback_id="f1", selection=selection, verbatim="too small")
    _capture(client, sid, feedback_id="f2", selection=selection, verbatim="too large")

    listed = _list(client, sid).json()
    assert listed["conflicts"], "two different changes to one region must be reported"
    assert listed["revision"]["ready"] is False


def test_a_session_that_does_not_exist_is_404(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert _capture(client, "no-such-session").status_code == 404
    assert _list(client, "no-such-session").status_code == 404


def test_the_routes_never_write_acceptance_or_a_benchmark(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    sid = _session(studio)
    recorded = _capture(client, sid, material=False).json()
    confirmed = client.post(
        f"/api/harness/sessions/{sid}/feedback/{recorded['feedback']['feedback_id']}/confirm",
        json={"confirmed_by": "owner"},
    ).json()

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

    for payload in (recorded, confirmed, _list(client, sid).json()):
        collect(payload)

    assert flags, "the record should carry the flags it refuses, not omit them"
    assert all(value is False for value in flags.values()), flags
    assert (
        "benchmark_reference_id" not in confirmed
        and "benchmark_adoption" not in confirmed
    )


def test_one_run_cannot_confirm_or_withdraw_another_runs_comment(tmp_path, monkeypatch):
    """A comment belongs to the run it was given in, and its id is not a key to other runs."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    owner = _session(studio)
    other = _session(studio)
    assert _capture(client, owner, feedback_id="fb-owner").status_code == 200

    # The other run cannot see it, cannot settle it and cannot withdraw it.
    listed = _list(client, other)
    assert listed.status_code == 200
    assert [item["feedback_id"] for item in listed.json()["items"]] == []
    for path in ("confirm", "promote"):
        response = client.post(
            f"/api/harness/sessions/{other}/feedback/fb-owner/{path}",
            json={"basis": "b", "confirmed_by": "owner"},
        )
        assert response.status_code == 404, (path, response.status_code)
    assert (
        client.delete(f"/api/harness/sessions/{other}/feedback/fb-owner").status_code
        == 404
    )

    # And it is still the owner run's feedback, untouched: a refused request changed nothing.
    still = _list(client, owner).json()
    assert [item["feedback_id"] for item in still["items"]] == ["fb-owner"]
    assert still["items"][0]["deleted"] is False
