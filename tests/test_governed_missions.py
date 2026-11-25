"""AP-029 — governed recurring missions and external delivery.

Fixtures are values this file constructs against the module's own contracts. No clock, provider,
network or filesystem is touched: every time is a caller-supplied epoch second.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest

import governed_missions as gm


def d(char: str) -> str:
    return char * 64


NOW = 1_767_225_600  # 2026-01-01T00:00:00Z, a whole number of hourly slots
HOUR = 3_600
DAY = 86_400


def grant(**kw: Any) -> gm.ScheduleGrant:
    params: dict[str, Any] = {
        "grant_id": "grant-1",
        "granted_by": "owner:project-owner",
        "project_id": "proj-1",
        "purposes": ("draft",),
        "cadence": "daily",
        "expires_at": NOW + 30 * DAY,
        "max_runs": 30,
    }
    params.update(kw)
    return gm.ScheduleGrant(**params)


def mission(**kw: Any) -> gm.RecurringMission:
    params: dict[str, Any] = {"mission_id": "mission-1", "project_id": "proj-1", "purpose": "draft", "grant": grant()}
    params.update(kw)
    return gm.RecurringMission(**params)


def draft(**kw: Any) -> gm.DraftArtifact:
    params: dict[str, Any] = {
        "artifact_id": "art-1",
        "mission_id": "mission-1",
        "project_id": "proj-1",
        "content_digest": d("c"),
        "title": "Weekly draft",
    }
    params.update(kw)
    return gm.DraftArtifact(**params)


def intent(**kw: Any) -> gm.ExternalActionIntent:
    params: dict[str, Any] = {
        "intent_id": "intent-1",
        "kind": "publish",
        "account": "acct-1",
        "destination": "https://example.test/blog/1",
        "content_digest": d("c"),
        "policy_revision": "grant-1",
        "expires_at": NOW + HOUR,
    }
    params.update(kw)
    return gm.ExternalActionIntent(**params)


def run(**kw: Any) -> gm.MissionRun:
    params: dict[str, Any] = {
        "run_id": "run-1",
        "mission_id": "mission-1",
        "project_id": "proj-1",
        "purpose": "draft",
        "slot": 1,
        "state": "started",
    }
    params.update(kw)
    return gm.MissionRun(**params)


# ---------------------------------------------------------------------------
# AC1 — a schedule grants only its purpose; a draft never implies publication.
# ---------------------------------------------------------------------------


def test_a_schedule_needs_a_named_human_grant() -> None:
    with pytest.raises(gm.ScheduleRefused, match="named human"):
        grant(granted_by="the system")


def test_a_grant_must_name_its_purposes() -> None:
    with pytest.raises(gm.ScheduleRefused, match="at least one purpose"):
        grant(purposes=())


def test_an_unknown_purpose_is_refused() -> None:
    with pytest.raises(gm.ScheduleRefused, match="unknown schedule purpose"):
        grant(purposes=("whatever",))


def test_a_grant_only_permits_its_own_purpose() -> None:
    only_draft = grant(purposes=("draft",))
    assert only_draft.permits("draft", now=NOW, runs_used=0) is True
    assert only_draft.permits("publish", now=NOW, runs_used=0) is False


def test_an_expired_grant_permits_nothing() -> None:
    assert grant(expires_at=NOW).permits("draft", now=NOW, runs_used=0) is False


def test_a_spent_grant_permits_nothing() -> None:
    assert grant(max_runs=2).permits("draft", now=NOW, runs_used=2) is False


def test_a_grant_with_no_expiry_is_refused() -> None:
    with pytest.raises(gm.ScheduleRefused, match="real expiry"):
        grant(expires_at=0)


def test_a_mission_cannot_exist_without_a_grant() -> None:
    with pytest.raises(gm.ScheduleRefused, match="cannot exist without a ScheduleGrant"):
        gm.RecurringMission(mission_id="m", project_id="proj-1", purpose="draft", grant=None)  # type: ignore[arg-type]


def test_a_mission_cannot_borrow_another_projects_grant() -> None:
    with pytest.raises(gm.ScheduleRefused, match="borrowed across projects"):
        mission(project_id="proj-2")


def test_a_mission_cannot_exceed_its_grants_purposes() -> None:
    with pytest.raises(gm.ScheduleRefused, match="only do what it was granted for"):
        mission(purpose="publish", grant=grant(purposes=("draft",)))


def test_a_draft_is_not_a_publication() -> None:
    mapping = draft().to_mapping()
    assert mapping["published"] is False
    assert not hasattr(gm.DraftArtifact, "publish")


def test_a_draft_grant_cannot_produce_an_external_action() -> None:
    with pytest.raises(gm.PublishRefused, match="never implies permission to publish"):
        gm.requires_external_action(
            draft(),
            grant=grant(purposes=("draft",)),
            kind="publish",
            account="acct-1",
            destination="https://example.test/blog/1",
            now=NOW,
        )


def test_a_publish_grant_can_produce_an_external_action() -> None:
    produced = gm.requires_external_action(
        draft(),
        grant=grant(purposes=("draft", "publish")),
        kind="publish",
        account="acct-1",
        destination="https://example.test/blog/1",
        now=NOW,
    )
    assert produced.kind == "publish"
    assert produced.content_digest == d("c")
    assert produced.expires_at <= NOW + HOUR


def test_a_deploy_needs_its_own_purpose() -> None:
    with pytest.raises(gm.PublishRefused, match="needs 'deploy'"):
        gm.requires_external_action(
            draft(),
            grant=grant(purposes=("draft", "publish")),
            kind="deploy",
            account="acct-1",
            destination="site-1",
            now=NOW,
        )


def test_an_external_action_needs_an_expiry() -> None:
    with pytest.raises(gm.PublishRefused, match="no expiry"):
        gm.requires_external_action(
            draft(),
            grant=grant(purposes=("publish",)),
            kind="publish",
            account="acct-1",
            destination="https://example.test/x",
            now=NOW,
            ttl_seconds=0,
        )


def test_the_intent_expiry_never_outlives_its_grant() -> None:
    expiring = grant(purposes=("publish",), expires_at=NOW + 60)
    produced = gm.requires_external_action(
        draft(),
        grant=expiring,
        kind="publish",
        account="acct-1",
        destination="https://example.test/x",
        now=NOW,
        ttl_seconds=DAY,
    )
    assert produced.expires_at == NOW + 60


# ---------------------------------------------------------------------------
# AC2 — an external action binds its terms and invalidates on any change.
# ---------------------------------------------------------------------------


def test_an_intent_binds_account_destination_content_policy_and_expiry() -> None:
    bound = intent().to_mapping()
    assert bound["account"] == "acct-1"
    assert bound["destination"].startswith("https://")
    assert bound["content_digest"] == d("c")
    assert bound["policy_revision"] == "grant-1"
    assert bound["expires_at"] == NOW + HOUR


def test_changing_one_byte_of_content_invalidates_the_intent() -> None:
    assert (
        intent().is_current(
            account="acct-1",
            destination="https://example.test/blog/1",
            content_digest=d("f"),
            policy_revision="grant-1",
            now=NOW,
        )
        is False
    )


def test_a_repointed_destination_invalidates_the_intent() -> None:
    assert (
        intent().is_current(
            account="acct-1",
            destination="https://elsewhere.test/blog/1",
            content_digest=d("c"),
            policy_revision="grant-1",
            now=NOW,
        )
        is False
    )


def test_a_newer_policy_revision_invalidates_the_intent() -> None:
    assert "policy_revision" in intent().changed_since(
        account="acct-1",
        destination="https://example.test/blog/1",
        content_digest=d("c"),
        policy_revision="grant-2",
    )


def test_an_expired_intent_is_not_current() -> None:
    assert (
        intent(expires_at=NOW + 10).is_current(
            account="acct-1",
            destination="https://example.test/blog/1",
            content_digest=d("c"),
            policy_revision="grant-1",
            now=NOW + 20,
        )
        is False
    )


def test_an_unchanged_world_keeps_the_intent_current() -> None:
    assert (
        intent().is_current(
            account="acct-1",
            destination="https://example.test/blog/1",
            content_digest=d("c"),
            policy_revision="grant-1",
            now=NOW,
        )
        is True
    )


def test_a_malformed_comparison_is_not_current_rather_than_an_exception() -> None:
    assert (
        intent().is_current(
            account="acct-1", destination="https://example.test/blog/1", content_digest="nope", policy_revision="grant-1", now=NOW
        )
        is False
    )


def test_an_intent_derives_an_idempotency_key() -> None:
    derived = intent().idempotency_key
    assert derived.startswith("m5-")
    assert intent().idempotency_key == derived


def test_an_unknown_external_effect_kind_is_refused() -> None:
    with pytest.raises(gm.PublishRefused, match="unknown external effect kind"):
        intent(kind="webhook")


# ---------------------------------------------------------------------------
# AC3 — retries reconcile; they never resend blindly.
# ---------------------------------------------------------------------------


def test_an_unknown_outcome_is_reconciled_not_resent() -> None:
    decision = gm.decide_retry(action_id="act-1", state="uncertain", idempotency_supported=True)
    assert decision.decision == "reconcile"
    assert decision.sends_again is False


def test_a_dispatched_action_is_not_resent() -> None:
    assert gm.decide_retry(action_id="act-1", state="dispatched", idempotency_supported=True).decision == "reconcile"


def test_an_unresolved_reconciliation_stops_rather_than_guesses() -> None:
    decision = gm.decide_retry(
        action_id="act-1",
        state="uncertain",
        idempotency_supported=True,
        reconciliation=gm.Reconciliation(verdict="unresolved", evidence="provider returned 503 twice"),
    )
    assert decision.decision == "stop"
    assert "cannot duplicate" in decision.reason


def test_a_verified_commit_stops_without_retrying() -> None:
    decision = gm.decide_retry(
        action_id="act-1",
        state="uncertain",
        idempotency_supported=True,
        reconciliation=gm.Reconciliation(verdict="verified_committed", evidence="post id 42 exists"),
    )
    assert decision.decision == "stop"


def test_a_verified_absence_may_retry_only_with_idempotency() -> None:
    absent = gm.Reconciliation(verdict="verified_not_applied", evidence="the post is not there")
    allowed = gm.decide_retry(action_id="act-1", state="uncertain", idempotency_supported=True, reconciliation=absent)
    refused = gm.decide_retry(action_id="act-1", state="uncertain", idempotency_supported=False, reconciliation=absent)
    assert allowed.decision == "retry"
    assert refused.decision == "stop"
    assert "no idempotency key" in refused.reason


def test_a_committed_action_is_never_sent_again() -> None:
    assert gm.decide_retry(action_id="act-1", state="committed", idempotency_supported=True).decision == "stop"


def test_an_abandoned_action_is_not_resurrected() -> None:
    assert gm.decide_retry(action_id="act-1", state="abandoned", idempotency_supported=True).decision == "stop"


def test_a_failed_send_retries_only_with_idempotency() -> None:
    assert gm.decide_retry(action_id="act-1", state="failed", idempotency_supported=False).decision == "stop"
    assert gm.decide_retry(action_id="act-1", state="failed", idempotency_supported=True).decision == "retry"


def test_a_reconciliation_must_state_its_evidence() -> None:
    with pytest.raises(gm.GovernanceError, match="must not be empty"):
        gm.Reconciliation(verdict="verified_committed", evidence="  ")


# ---------------------------------------------------------------------------
# AC4 — restarts, missed slots and revocation avoid duplicate effects.
# ---------------------------------------------------------------------------


def test_a_restart_in_the_same_slot_is_the_same_run() -> None:
    a = mission().run_id_for(mission().slot_for(NOW))
    b = mission().run_id_for(mission().slot_for(NOW + 60))
    assert a == b


def test_a_different_slot_is_a_different_run() -> None:
    daily = mission()
    assert daily.run_id_for(daily.slot_for(NOW)) != daily.run_id_for(daily.slot_for(NOW + DAY))


def test_a_run_claimed_twice_is_a_duplicate_delivery() -> None:
    ledger = gm.RunLedger().claim_run(run())
    with pytest.raises(gm.DedupRefused, match="duplicate delivery"):
        ledger.claim_run(run())


def test_a_run_may_be_finished_once() -> None:
    finished = gm.RunLedger().claim_run(run()).finish_run("run-1", state="completed", artifacts=("art-1",))
    assert finished.runs["run-1"].state == "completed"
    with pytest.raises(gm.DedupRefused, match="already 'completed'"):
        finished.finish_run("run-1", state="failed")


def test_a_run_that_was_never_claimed_cannot_be_finished() -> None:
    with pytest.raises(gm.DedupRefused, match="never claimed"):
        gm.RunLedger().finish_run("run-1", state="completed")


def test_a_missed_slot_is_recorded_rather_than_backfilled() -> None:
    daily = mission()
    ledger = gm.RunLedger().record_missed(mission=daily, slot=5)
    recorded = ledger.runs_for("mission-1")[0]
    assert recorded.state == "missed"
    assert ledger.runs_used("mission-1") == 0


def test_a_missed_slot_does_not_double_count_as_a_run() -> None:
    daily = mission()
    ledger = gm.RunLedger().record_missed(mission=daily, slot=5)
    assert daily.grant.permits("draft", now=NOW, runs_used=ledger.runs_used("mission-1")) is True


def test_a_revoked_grant_stops_the_next_run() -> None:
    revoked = grant(expires_at=NOW)
    still_built = mission(grant=revoked)  # the mission may exist; the run is what the grant gates
    assert still_built.grant.permits("draft", now=NOW, runs_used=0) is False
    assert revoked.permits("draft", now=NOW, runs_used=0) is False


def test_changing_an_actions_terms_after_it_was_recorded_is_a_different_action() -> None:
    ledger = gm.RunLedger().record_action(intent(intent_id="intent-1"), state="dispatched")
    with pytest.raises(gm.DedupRefused, match="different action wearing the same name"):
        ledger.record_action(intent(intent_id="intent-1", content_digest=d("e")), state="prepared")


def test_re_recording_the_same_action_in_a_new_state_keeps_one_key() -> None:
    ledger = gm.RunLedger().record_action(intent(), state="dispatched").set_action_state("intent-1", state="committed")
    assert ledger.action_state("intent-1") == "committed"
    assert list(ledger.to_mapping()["idempotency_keys"].values()) == ["intent-1"]


def test_a_different_action_under_one_key_is_refused_by_construction() -> None:
    ledger = gm.RunLedger().record_action(intent(), state="dispatched")
    other = intent(intent_id="intent-2")
    with pytest.raises(gm.DedupRefused, match="already belongs to action"):
        ledger.record_action(gm.ExternalActionIntent(**{**other.to_mapping(), "idempotency_key": intent().idempotency_key}), state="prepared")


def test_a_committed_action_cannot_be_reopened_as_prepared() -> None:
    ledger = gm.RunLedger().record_action(intent(), state="committed")
    with pytest.raises(gm.DedupRefused, match="duplicate external effect"):
        ledger.record_action(intent(), state="prepared")


def test_an_unknown_action_state_cannot_be_advanced_from_nothing() -> None:
    with pytest.raises(gm.DedupRefused, match="never recorded"):
        gm.RunLedger().set_action_state("intent-9", state="committed")


def test_the_ledger_is_append_only_by_construction() -> None:
    base = gm.RunLedger().claim_run(run())
    grown = base.finish_run("run-1", state="completed")
    assert base.runs["run-1"].state == "started"
    assert grown.runs["run-1"].state == "completed"


def test_the_ledger_fingerprint_is_reproducible() -> None:
    first = gm.RunLedger().claim_run(run())
    assert first.fingerprint() == gm.RunLedger().claim_run(run()).fingerprint()
    json.loads(json.dumps(first.to_mapping()))


# ---------------------------------------------------------------------------
# AC5 — pause, per-project stop and notification thresholds.
# ---------------------------------------------------------------------------


def test_the_global_pause_stops_all_scheduled_work() -> None:
    reason = gm.Governor(globally_paused=True).refuse(project_id="proj-1")
    assert reason is not None and "global pause" in reason


def test_a_stopped_project_does_not_run() -> None:
    reason = gm.Governor(stopped_projects=("proj-1",)).refuse(project_id="proj-1")
    assert reason is not None and "stopped by the operator" in reason


def test_another_project_still_runs() -> None:
    assert gm.Governor(stopped_projects=("proj-1",)).refuse(project_id="proj-2") is None


def test_a_notification_threshold_names_a_recipient() -> None:
    with pytest.raises(gm.GovernorRefused, match="named recipient"):
        gm.Governor(notify_after_runs=3)


def test_reaching_the_threshold_requires_a_notification_before_the_next_run() -> None:
    governor = gm.Governor(notify_after_runs=2, notify_recipient="owner:project-owner")
    assert governor.refuse(project_id="proj-1", runs_in_window=1) is None
    reason = governor.refuse(project_id="proj-1", runs_in_window=2)
    assert reason is not None and "notification threshold" in reason


def test_an_idle_governor_refuses_nothing() -> None:
    assert gm.Governor().refuse(project_id="proj-1") is None


# ---------------------------------------------------------------------------
# AC6 — suggestions do not claim causal success or change public output.
# ---------------------------------------------------------------------------


def test_a_suggestion_cannot_claim_causation_without_an_experiment() -> None:
    with pytest.raises(gm.ClaimRefused, match="claims causation"):
        gm.Suggestion(
            suggestion_id="sug-1",
            kind="content_idea",
            proposal="publishing on Tuesdays caused a 20% revenue lift",
        )


def test_a_measured_suggestion_may_name_its_experiment() -> None:
    measured = gm.Suggestion(
        suggestion_id="sug-1",
        kind="content_idea",
        proposal="the Tuesday slot caused a measurable lift",
        experiment_id="exp-7",
    )
    assert measured.experiment_id == "exp-7"


def test_a_suggestion_never_changes_public_output() -> None:
    assert gm.Suggestion(suggestion_id="sug-1", kind="draft_change", proposal="shorten the intro").changes_public_output is False


def test_a_suggestion_becomes_a_draft_not_a_publication() -> None:
    produced = gm.Suggestion(suggestion_id="sug-1", kind="draft_change", proposal="shorten the intro").to_artifact(
        mission=mission(), content_digest=d("c")
    )
    assert isinstance(produced, gm.DraftArtifact)
    assert produced.to_mapping()["published"] is False


def test_an_unknown_suggestion_kind_is_refused() -> None:
    with pytest.raises(gm.ClaimRefused, match="unknown suggestion kind"):
        gm.Suggestion(suggestion_id="sug-1", kind="growth_hack", proposal="do more of it")


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    tree = ast.parse(open("governed_missions.py", encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}


def test_no_function_takes_a_clock_or_a_provider() -> None:
    tree = ast.parse(open("governed_missions.py", encoding="utf-8").read())
    parameters: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters.update(arg.arg for arg in node.args.args + node.args.kwonlyargs)
    assert parameters & {"clock", "now_fn", "time", "session", "client", "http"} == set()
