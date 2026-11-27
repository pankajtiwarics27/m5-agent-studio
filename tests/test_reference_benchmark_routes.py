"""AP-020: the reference-benchmark routes.

The module tests prove the rules. These prove the routes cannot be talked around: an approval
is the only way in, a second approval is a second version, and coverage never becomes
permission to ship.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SHA_A = "a" * 64
SHA_B = "b" * 64

from tests.test_harness_ap008_recovery import _harness_client  # noqa: E402  (shared fixture recipe)


def _scope(studio) -> str:
    """The project scope the server derives for this project.

    A reference is project-scoped and the scope is derived from the project root rather than
    accepted as a nickname, so the tests derive it the same way the pane does.
    """
    return studio.evidence_lessons.derive_project_scope_id(studio.WORKSPACE_ROOT)


def _record(client, scope: str, **overrides):
    payload = {
        "reference_id": "ref/website",
        "version": 1,
        "project_scope_id": scope,
        "task_kind": "website",
        "brief": "a landing page for the ledger app, for consumers",
        "artifact": {
            "artifact_id": "benchmarks/website-v1.html",
            "content_sha256": SHA_A,
            "measured_checks": {"layout": True, "overflow": True},
        },
        "approval": {
            "actor": "owner:pankaj",
            "statement": "this is the calm, plain tone I meant",
            "granted": ["use_as_reference", "final_acceptance"],
        },
        "audience": "consumer",
        "language": "en",
        "style_tags": ["calm", "plain"],
        "checks": ["layout"],
    }
    payload.update(overrides)
    return client.post("/api/reference-benchmarks", json=payload)


def _approval(**overrides):
    base = {
        "actor": "owner:pankaj",
        "statement": "this one is the tone",
        "granted": ["use_as_reference"],
    }
    base.update(overrides)
    return base


# ── recording ────────────────────────────────────────────────────────────────────────────


def test_an_approved_reference_is_recorded_and_reported_as_no_permission(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = _record(client, _scope(studio))
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["status"] == "recorded"
    assert data["reference"]["artifact"]["content_sha256"] == SHA_A
    assert data["reference"]["approval"]["actor"] == "owner:pankaj"
    # Reuse of a style is restated as not-permission on the way out.
    assert data["grants_publish_permission"] is False
    assert data["is_final_acceptance"] is False
    assert "changed_code_works" in data["cannot_settle"]


def test_an_agent_cannot_record_its_own_approval(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = _record(client, _scope(studio), approval=_approval(actor="agent"))
    assert response.status_code == 400
    assert "not a person" in response.json()["detail"]

    listed = client.get(
        "/api/reference-benchmarks", params={"project_scope_id": _scope(studio)}
    )
    assert listed.json()["count"] == 0


def test_an_approval_that_accepted_but_did_not_adopt_is_refused_at_the_route(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = _record(
        client, _scope(studio), approval=_approval(granted=["final_acceptance"])
    )
    assert response.status_code == 400
    assert "use_as_reference" in response.json()["detail"]


def test_a_reference_needs_the_exact_artifact_and_its_digest(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    missing = _record(client, _scope(studio), artifact={"artifact_id": "x.html"})
    assert missing.status_code == 400
    assert "content_sha256" in missing.json()["detail"]

    no_artifact = _record(client, _scope(studio), artifact=None)
    assert no_artifact.status_code == 400
    assert "artifact and approval" in no_artifact.json()["detail"]


def test_a_claim_of_publish_permission_in_the_body_changes_nothing(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = _record(
        client, _scope(studio), grants_publish_permission=True, is_final_acceptance=True
    )
    assert response.status_code == 200, response.text
    stored = client.get("/api/reference-benchmarks/ref/website").json()["versions"][0]
    assert stored["grants_publish_permission"] is False
    assert stored["is_final_acceptance"] is False


def test_the_same_version_cannot_be_re_approved_with_different_words(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200

    rewritten = _record(
        client,
        scope,
        approval=_approval(statement="I never said that", actor="owner:someone-else"),
    )
    assert rewritten.status_code == 409
    assert "not amended in place" in rewritten.json()["detail"]


def test_a_reference_belongs_to_a_project_and_a_nickname_is_refused(
    tmp_path, monkeypatch
):
    """A caller cannot pick another project's scope by naming it."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = _record(client, "proj-someone-else")
    assert response.status_code == 400
    assert "derived" in response.json()["detail"]


# ── versions ────────────────────────────────────────────────────────────────────────────


def test_an_enhancement_adds_a_version_and_leaves_the_first_acceptance_intact(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert _record(client, _scope(studio)).status_code == 200

    enhanced = client.post(
        "/api/reference-benchmarks/ref/website/enhance",
        json={
            "from_version": 1,
            "new_version": 2,
            "artifact": {
                "artifact_id": "benchmarks/website-v2.html",
                "content_sha256": SHA_B,
                "measured_checks": {"layout": True, "overflow": True},
            },
            "approval": _approval(statement="the second draft is the calm one"),
        },
    )
    assert enhanced.status_code == 200, enhanced.text
    data = enhanced.json()
    assert data["reference"]["version"] == 2
    assert data["reference"]["supersedes"] == "ref/website@v1"
    assert data["superseded"]["status"] == "superseded"
    assert data["superseded"]["superseded_by"] == "ref/website@v2"

    history = client.get("/api/reference-benchmarks/ref/website").json()["versions"]
    assert [row["version"] for row in history] == [1, 2]
    # Version 1 is exactly as the owner approved it: same artifact, same statement.
    assert history[0]["artifact"]["content_sha256"] == SHA_A
    assert history[0]["approval"]["statement"] == "this is the calm, plain tone I meant"
    assert history[0]["superseded_by"] == "ref/website@v2"


def test_an_enhancement_without_its_own_approval_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert _record(client, _scope(studio)).status_code == 200
    response = client.post(
        "/api/reference-benchmarks/ref/website/enhance",
        json={
            "from_version": 1,
            "new_version": 2,
            "artifact": {
                "artifact_id": "benchmarks/website-v2.html",
                "content_sha256": SHA_B,
                "measured_checks": {"layout": True},
            },
        },
    )
    assert response.status_code == 400
    assert "its own decision" in response.json()["detail"]


def test_enhancing_a_version_that_was_never_recorded_is_404(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/reference-benchmarks/ref/website/enhance",
        json={
            "from_version": 1,
            "new_version": 2,
            "artifact": {
                "artifact_id": "x.html",
                "content_sha256": SHA_B,
                "measured_checks": {},
            },
            "approval": _approval(),
        },
    )
    assert response.status_code == 404


def test_an_unknown_reference_is_404_not_an_empty_reference(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert client.get("/api/reference-benchmarks/ref/nope").status_code == 404
    assert (
        client.get(
            "/api/reference-benchmarks/ref/nope", params={"version": 1}
        ).status_code
        == 404
    )


# ── revocation ──────────────────────────────────────────────────────────────────────────


def test_revocation_keeps_the_record_and_names_a_person(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    assert _record(client, _scope(studio)).status_code == 200

    by_machine = client.post(
        "/api/reference-benchmarks/ref/website/revoke",
        json={"version": 1, "reason": "not right", "actor": "agent"},
    )
    assert by_machine.status_code == 400
    assert "names the person" in by_machine.json()["detail"]

    revoked = client.post(
        "/api/reference-benchmarks/ref/website/revoke",
        json={"version": 1, "reason": "the audience changed", "actor": "owner:pankaj"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["reference"]["status"] == "revoked"
    # Revoked is not deleted: the artifact and the decision are both still readable.
    assert revoked.json()["reference"]["artifact"]["content_sha256"] == SHA_A
    assert (
        revoked.json()["reference"]["approval"]["statement"]
        == "this is the calm, plain tone I meant"
    )


def test_retrieval_defaults_to_what_is_still_usable(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    client.post(
        "/api/reference-benchmarks/ref/website/revoke",
        json={"version": 1, "reason": "changed direction", "actor": "owner:pankaj"},
    )
    answer = client.get(
        "/api/reference-benchmarks", params={"project_scope_id": scope}
    ).json()
    assert answer["count"] == 1  # revocation is a status, not a disappearance

    live = client.get(
        "/api/reference-benchmarks",
        params={"project_scope_id": scope, "status": "active"},
    ).json()
    assert live["count"] == 0


# ── retrieval ───────────────────────────────────────────────────────────────────────────


def test_a_session_resolves_to_its_own_project_scope(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    session = studio.create_harness_session_db(
        title="AP-020 scope",
        workspace_dir=str(studio.WORKSPACE_ROOT),
        use_worktree=False,
        policy="standard",
        lead_model="",
        critic_model="",
    )
    answer = client.get(
        "/api/reference-benchmarks", params={"session_id": session["id"]}
    ).json()
    assert answer["project_scope_id"] == scope
    assert answer["count"] == 1


def test_retrieval_prefers_the_right_audience_language_and_task(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    assert (
        _record(
            client,
            scope,
            reference_id="ref/internal-doc",
            task_kind="document",
            audience="internal",
            language="hi",
            style_tags=["formal"],
        ).status_code
        == 200
    )

    answer = client.get(
        "/api/reference-benchmarks",
        params={
            "project_scope_id": scope,
            "task_kind": "website",
            "audience": "consumer",
            "language": "en",
            "style_tags": "calm",
        },
    ).json()
    assert answer["items"][0]["reference_id"] == "ref/website"
    reasons = " ".join(answer["items"][0]["why_offered"])
    assert "same task kind" in reasons
    assert "same audience" in reasons


def test_a_reference_for_another_language_is_offered_with_the_reason_visible(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope, language="hi").status_code == 200

    answer = client.get(
        "/api/reference-benchmarks",
        params={"project_scope_id": scope, "task_kind": "website", "language": "en"},
    ).json()
    assert answer["count"] == 1
    assert any(
        "different language" in reason for reason in answer["items"][0]["why_offered"]
    )


def test_retrieval_never_offers_a_layout_to_copy(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    answer = client.get(
        "/api/reference-benchmarks",
        params={
            "project_scope_id": scope,
            "task_kind": "website",
            "style_tags": "calm",
        },
    ).json()
    reasons = " ".join(answer["items"][0]["why_offered"]).lower()
    assert "shared style: calm" in reasons
    assert "layout" not in reasons


def test_the_catalogue_lists_only_this_project(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    # A second workspace is a different project.
    other_ws = tmp_path / "other-project"
    other_ws.mkdir()
    other_scope = studio.evidence_lessons.derive_project_scope_id(other_ws)
    assert other_scope != scope
    assert _record(client, other_scope, reference_id="ref/other").status_code == 200

    answer = client.get(
        "/api/reference-benchmarks", params={"project_scope_id": scope}
    ).json()
    assert [row["reference_id"] for row in answer["items"]] == ["ref/website"]


def test_listing_without_a_project_is_refused(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    response = client.get("/api/reference-benchmarks")
    assert response.status_code == 400
    assert "belongs to one project" in response.json()["detail"]


# ── coverage ────────────────────────────────────────────────────────────────────────────


def test_coverage_reports_what_is_settled_and_what_still_is_not(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope, checks=["layout"]).status_code == 200

    covered = client.post(
        "/api/reference-benchmarks/coverage",
        json={"project_scope_id": scope, "task_kind": "website", "checks": ["layout"]},
    ).json()
    assert covered["covered"] is True
    assert covered["missing"] == []
    # Coverage is never permission: it is returned with the obligations beside it.
    assert covered["grants_publish_permission"] is False
    assert "publish_permission" in covered["still_required"]

    partial = client.post(
        "/api/reference-benchmarks/coverage",
        json={
            "project_scope_id": scope,
            "task_kind": "website",
            "checks": ["layout", "a11y"],
        },
    ).json()
    assert partial["covered"] is False
    assert partial["missing"] == ["check:a11y"]


def test_coverage_names_the_obligations_a_changed_artifact_brings(
    tmp_path, monkeypatch
):
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    answer = client.post(
        "/api/reference-benchmarks/coverage",
        json={
            "project_scope_id": scope,
            "task_kind": "website",
            "changed": ["code", "facts"],
        },
    ).json()
    assert "changed_code_works" in answer["still_required"]
    assert "changed_facts_are_true" in answer["still_required"]
    assert "security_sign_off" in answer["still_required"]


def test_an_empty_project_is_not_covered(tmp_path, monkeypatch):
    studio, client = _harness_client(tmp_path, monkeypatch)
    answer = client.post(
        "/api/reference-benchmarks/coverage",
        json={"project_scope_id": _scope(studio), "task_kind": "website"},
    ).json()
    assert answer["covered"] is False
    assert answer["references"] == []


# ── the shipped pane, proven at the DOM level ───────────────────────────────────────────


PROBE = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "scripts"
    / "ui-probes"
    / "reference-panel.mjs"
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


def test_the_shipped_reference_pane_escapes_data_and_states_the_limit():
    """The pane is proven at the DOM level when jsdom is reachable, and skipped loudly otherwise.

    A skipped probe is not a passing one: the Chromium walks remain the evidence for layout.
    What this pins is the property that matters here -- a reference id, a brief or the owner's
    words from outside the pane become text, and the pane still says what a reference cannot
    settle.
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


def test_coverage_reports_the_same_missing_check_once(tmp_path, monkeypatch):
    """Two references that both lack a check mean one thing is unmeasured, not two."""
    studio, client = _harness_client(tmp_path, monkeypatch)
    scope = _scope(studio)
    assert _record(client, scope).status_code == 200
    assert _record(client, scope, reference_id="ref/website-alt").status_code == 200

    answer = client.post(
        "/api/reference-benchmarks/coverage",
        json={"project_scope_id": scope, "task_kind": "website", "checks": ["a11y"]},
    ).json()
    assert answer["missing"] == ["check:a11y"]
    assert answer["covered"] is False


def test_reference_view_fences_races_and_keeps_approval_drafts_by_version():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "tests/reference_fencing_probe.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reference fencing probe passed" in result.stdout
