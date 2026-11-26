"""AP-031 — progressive configuration, honest readiness and operable state.

Every fixture is a value this file constructs. Nothing contacts a provider, renders HTML or reads the
real preflight snapshot: the assertions are about what the surface is allowed to claim.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest

import operations_surface as ops


NOW = 1_767_225_600


def observation(**kw: Any) -> ops.CapabilityObservation:
    params: dict[str, Any] = {"capability_id": "cap-1", "label": "Local chat", "configured": True}
    params.update(kw)
    return ops.CapabilityObservation(**params)


def check(**kw: Any) -> ops.ReadinessCheck:
    params: dict[str, Any] = {"check_id": "c-1", "label": "Workspace writable", "basis": "measured", "ok": True}
    params.update(kw)
    return ops.ReadinessCheck(**params)


def settings(**kw: Any) -> ops.SettingEntry:
    params: dict[str, Any] = {"setting_id": "s-1", "label": "Reasoning depth", "level": "simple"}
    params.update(kw)
    return ops.SettingEntry(**params)


def status(**kw: Any) -> ops.CapabilityStatus:
    return ops.status_for(observation(**kw))


# ---------------------------------------------------------------------------
# AC1 — the six states are distinct and derived from evidence.
# ---------------------------------------------------------------------------


def test_the_states_are_a_closed_ordered_set() -> None:
    assert set(ops.CAPABILITY_STATES) == {
        "configured",
        "tested",
        "qualified",
        "experimental",
        "blocked",
        "unavailable",
    }
    assert ops.STATE_PRECEDENCE[0] == "blocked"
    assert ops.STATE_PRECEDENCE[-1] == "qualified"


def test_configured_without_a_test_is_not_tested() -> None:
    assert status().state == "configured"


def test_tested_without_qualification_is_not_qualified() -> None:
    assert status(tested=True).state == "tested"


def test_qualification_requires_a_test() -> None:
    with pytest.raises(ops.StateRefused, match="without a test"):
        observation(configured=True, qualified=True, tested=False)


def test_a_test_requires_something_configured() -> None:
    with pytest.raises(ops.StateRefused, match="could not have run"):
        observation(configured=False, tested=True)


def test_blocked_beats_configured() -> None:
    assert status(blocked=True, tested=True, qualified=True).state == "blocked"


def test_unavailable_beats_experimental() -> None:
    assert status(unavailable=True, experimental=True).state == "unavailable"


def test_blocked_and_unavailable_are_different_answers() -> None:
    with pytest.raises(ops.StateRefused, match="different answers"):
        observation(blocked=True, unavailable=True)


def test_an_unconfigured_capability_is_unavailable_not_ready() -> None:
    assert ops.status_for(ops.CapabilityObservation(capability_id="cap-1", label="X")).state == "unavailable"


def test_experimental_is_usable_but_not_promised() -> None:
    result = status(tested=True, experimental=True)
    assert result.state == "experimental"
    assert result.usable is True
    assert "not promised" in result.reason


def test_a_blocked_capability_is_not_usable() -> None:
    assert status(blocked=True).usable is False


def test_evidence_appears_in_the_reason() -> None:
    assert "probe returned 3 models" in status(tested=True, evidence="probe returned 3 models").reason


def test_a_qualified_capability_says_it_was_measured_here() -> None:
    result = status(tested=True, qualified=True)
    assert result.state == "qualified"
    assert "measured qualification" in result.reason


# ---------------------------------------------------------------------------
# AC3 — readiness reports measured results and never hides a cost.
# ---------------------------------------------------------------------------


def test_a_measured_check_must_report_a_result() -> None:
    with pytest.raises(ops.OperationsError, match="measured basis but reports no result"):
        check(ok=None)


def test_an_unmeasured_check_cannot_report_a_result() -> None:
    with pytest.raises(ops.OperationsError, match="a result requires having looked"):
        check(basis="unmeasured", ok=True)


def test_a_check_that_spends_money_needs_an_opt_in() -> None:
    with pytest.raises(ops.OperationsError, match="must not bill or phone anyone"):
        check(spends_money=True)


def test_a_check_that_uses_the_network_needs_an_opt_in() -> None:
    with pytest.raises(ops.OperationsError, match="must not bill or phone anyone"):
        check(uses_network=True)


def test_an_opted_in_costly_check_is_allowed_and_reported() -> None:
    opted = check(spends_money=True, opt_in_recorded=True)
    assert opted.hidden_cost is False


def test_a_report_is_not_ready_when_anything_is_unmeasured() -> None:
    report = ops.build_readiness([check(), check(check_id="c-2", basis="unmeasured", ok=None)])
    assert report.ready is False
    assert report.verdict == "partly-verified"
    assert "not a clean bill of health" in report.summary


def test_a_report_is_ready_only_when_every_row_was_measured() -> None:
    report = ops.build_readiness([check(), check(check_id="c-2", label="Runtime", ok=True)])
    assert report.ready is True
    assert report.verdict == "ready"


def test_a_failing_check_makes_the_report_need_attention() -> None:
    report = ops.build_readiness([check(), check(check_id="c-2", label="Space", ok=False, detail="disk full")])
    assert report.verdict == "needs-you"
    assert "disk full" in report.summary


def test_an_empty_readiness_report_is_refused() -> None:
    with pytest.raises(ops.OperationsError, match="says nothing and looks fine"):
        ops.build_readiness([])


def test_a_check_reported_twice_is_refused() -> None:
    with pytest.raises(ops.OperationsError, match="reported twice"):
        ops.build_readiness([check(), check()])


def test_preflight_rows_are_declared_not_measured() -> None:
    report = ops.build_readiness_from_preflight(
        {
            "steps": [
                {"id": "auth", "title": "Workspace protected", "status": "ready"},
                {"id": "model-available", "title": "Model ready", "status": "action-required", "detail": "pick one"},
            ],
            "local_probe": "not-run",
        }
    )
    assert all(row.basis == "declared" for row in report.checks)
    assert report.ready is False


def test_a_runtime_probe_that_ran_is_the_one_measured_row() -> None:
    report = ops.build_readiness_from_preflight(
        {"steps": [{"id": "auth", "title": "Auth", "status": "ready"}], "local_probe": "ran", "local_models_available": 3}
    )
    measured = [row for row in report.checks if row.basis == "measured"]
    assert len(measured) == 1
    assert measured[0].ok is True


def test_a_failed_runtime_probe_is_reported_as_failed() -> None:
    report = ops.build_readiness_from_preflight(
        {"steps": [], "local_probe": "ran", "local_models_available": 0}
    )
    assert report.failing[0].check_id == "preflight:local-runtime-probe"


# ---------------------------------------------------------------------------
# AC2 — grants are inspectable where they are revocable; preferences are the reader's.
# ---------------------------------------------------------------------------


def grant(**kw: Any) -> ops.GrantView:
    params: dict[str, Any] = {
        "grant_id": "grant-1",
        "granted_by": "owner:project-owner",
        "capabilities": ("workspace_write",),
        "resource_scopes": ("ws:proj/out",),
        "expires_at": NOW + 600,
        "remaining_uses": 1,
        "revocable": True,
    }
    params.update(kw)
    return ops.GrantView(**params)


def test_a_grant_names_who_granted_it() -> None:
    with pytest.raises(ops.GrantRefused, match="must name who granted it"):
        grant(granted_by="the system")


def test_a_grant_with_no_capabilities_describes_nothing() -> None:
    with pytest.raises(ops.GrantRefused, match="describes nothing"):
        grant(capabilities=())


def test_a_irrevocable_grant_must_say_why() -> None:
    with pytest.raises(ops.GrantRefused, match="must say why"):
        grant(revocable=False)


def test_an_irrevocable_grant_with_a_reason_is_viewable() -> None:
    view = grant(revocable=False, revocation_blocked_reason="sealed by the host policy")
    assert view.to_mapping()["revoke_route"] == ""


def test_a_revocable_grant_exposes_its_revoke_route() -> None:
    assert grant().to_mapping()["revoke_route"] == "revoke:grant-1"


def test_preferences_can_be_corrected() -> None:
    store = ops.PreferenceStore().correct(key="theme", value="dark", now=NOW)
    assert store.preferences["theme"].value == "dark"
    assert store.preferences["theme"].source == "user"


def test_preferences_can_be_exported_with_a_digest() -> None:
    exported = ops.PreferenceStore().correct(key="theme", value="dark", now=NOW).export(now=NOW)
    assert exported["preferences"][0]["key"] == "theme"
    assert len(exported["digest"]) == 64


def test_preferences_can_be_deleted() -> None:
    store = ops.PreferenceStore().correct(key="theme", value="dark", now=NOW).delete(key="theme")
    assert store.preferences == {}


def test_deleting_an_unknown_preference_is_refused() -> None:
    with pytest.raises(ops.OperationsError, match="no preference"):
        ops.PreferenceStore().delete(key="theme")


def test_a_reference_can_be_deleted_by_id() -> None:
    reference = ops.ReferenceRecord(reference_id="ref-1", title="Brand guide", digest="a" * 64, added_at=NOW)
    store = ops.PreferenceStore(references={"ref-1": reference})
    assert store.delete(reference_id="ref-1").references == {}


def test_delete_takes_exactly_one_target() -> None:
    with pytest.raises(ops.OperationsError, match="exactly one of"):
        ops.PreferenceStore().delete()
    with pytest.raises(ops.OperationsError, match="exactly one of"):
        ops.PreferenceStore().delete(key="a", reference_id="b")


def test_the_reader_is_told_which_actions_exist() -> None:
    assert ops.PreferenceStore().actions_available() == ("correct", "export", "delete")


# ---------------------------------------------------------------------------
# AC4 — progressive disclosure does not remove anything.
# ---------------------------------------------------------------------------


def test_a_setting_at_an_unknown_level_is_refused() -> None:
    with pytest.raises(ops.DisclosureRefused, match="unknown disclosure level"):
        settings(level="wizard")


def test_simple_shows_only_simple_settings() -> None:
    panel = ops.build_disclosure([settings(), settings(setting_id="s-2", level="expert")])
    assert [entry.setting_id for entry in panel.visible_at("simple")] == ["s-1"]


def test_expert_reveals_everything() -> None:
    panel = ops.build_disclosure([settings(), settings(setting_id="s-2", level="expert")])
    assert len(panel.visible_at("expert")) == 2


def test_nothing_is_unreachable_at_the_top_level() -> None:
    panel = ops.build_disclosure(
        [settings(), settings(setting_id="s-2", level="advanced"), settings(setting_id="s-3", level="expert")]
    )
    assert panel.unreachable() == ()


def test_the_hidden_count_is_stated_in_the_open() -> None:
    mapping = ops.build_disclosure(
        [settings(), settings(setting_id="s-2", level="advanced"), settings(setting_id="s-3", level="expert")]
    ).to_mapping()
    assert mapping["hidden_by_default_count"] == 2
    assert mapping["settings_removed"] == 0


def test_a_setting_listed_twice_is_refused() -> None:
    with pytest.raises(ops.DisclosureRefused, match="listed twice"):
        ops.build_disclosure([settings(), settings()])


def test_a_disclosure_with_no_settings_is_refused() -> None:
    with pytest.raises(ops.DisclosureRefused, match="no settings"):
        ops.build_disclosure([])


def test_existing_settings_are_marked_as_preexisting() -> None:
    assert settings().to_mapping()["existed_before"] is True


# ---------------------------------------------------------------------------
# AC6 — help describes the build, and acceptance is not authority.
# ---------------------------------------------------------------------------


def help_entry(**kw: Any) -> ops.HelpEntry:
    params: dict[str, Any] = {
        "help_id": "h-1",
        "title": "Send a chat message",
        "capability_id": "cap-1",
        "describes_state": "configured",
    }
    params.update(kw)
    return ops.HelpEntry(**params)


def test_help_about_a_capability_the_build_lacks_is_refused() -> None:
    with pytest.raises(ops.HelpRefused, match="does not report"):
        ops.build_help([help_entry(capability_id="cap-ghost")], [status()])


def test_help_calling_an_unavailable_capability_available_is_refused() -> None:
    unavailable = ops.status_for(ops.CapabilityObservation(capability_id="cap-1", label="X"))
    with pytest.raises(ops.HelpRefused, match="inventory reports 'unavailable'"):
        ops.build_help([help_entry(describes_state="tested")], [unavailable])


def test_help_may_be_honest_about_a_worse_state() -> None:
    qualified = status(tested=True, qualified=True)
    assert ops.build_help([help_entry(describes_state="configured")], [qualified])


def test_help_cannot_grant_external_authority() -> None:
    with pytest.raises(ops.HelpRefused, match="cannot grant external authority"):
        help_entry(grants_external_authority=True)


def test_accepting_an_artifact_is_not_publishing_it() -> None:
    entry = help_entry(accepts_artifacts=True)
    assert entry.to_mapping()["accepts_artifacts"] is True
    assert entry.to_mapping()["grants_external_authority"] is False


def test_an_unknown_state_in_help_is_refused() -> None:
    with pytest.raises(ops.HelpRefused, match="unknown capability state"):
        help_entry(describes_state="probably-fine")


# ---------------------------------------------------------------------------
# The composed report.
# ---------------------------------------------------------------------------


def report(**kw: Any) -> ops.OperationsReport:
    params: dict[str, Any] = {
        "statuses": [status()],
        "readiness": ops.build_readiness([check()]),
        "disclosure": ops.build_disclosure([settings()]),
        "grants": [grant()],
        "help_entries": [help_entry()],
        "limits": ("local models only; no hosted inference",),
    }
    params.update(kw)
    return ops.build_report(**params)


def test_the_report_states_that_acceptance_is_not_authority() -> None:
    assert report().to_mapping()["acceptance_is_not_authority"] is True


def test_the_report_states_that_costs_were_disclosed_before_running() -> None:
    assert report().to_mapping()["costs_stated_before_running"] is True


def test_a_report_with_no_capabilities_says_nothing() -> None:
    with pytest.raises(ops.OperationsError, match="says nothing"):
        report(statuses=[])


def test_the_report_lists_the_states_in_use_without_collapsing_them() -> None:
    composed = report(statuses=[status(), status(capability_id="cap-2", label="Video", blocked=True)])
    assert composed.states_in_use == ("blocked", "configured")


def test_the_report_carries_the_readers_limits_in_words() -> None:
    assert report().to_mapping()["limits"] == ["local models only; no hosted inference"]


def test_the_report_exposes_preference_counts_and_actions() -> None:
    store = ops.PreferenceStore().correct(key="theme", value="dark", now=NOW)
    mapping = report(store=store).to_mapping()
    assert mapping["preferences"]["count"] == 1
    assert mapping["preferences"]["actions"] == ["correct", "export", "delete"]


def test_the_report_is_reproducible_and_serializable() -> None:
    first, second = report(), report()
    assert first.fingerprint() == second.fingerprint()
    json.loads(json.dumps(first.to_mapping()))


# ---------------------------------------------------------------------------
# Discipline.
# ---------------------------------------------------------------------------


def test_the_module_renders_nothing_and_opens_no_socket() -> None:
    tree = ast.parse(open("operations_surface.py", encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}


def test_the_module_has_no_html_or_template_strings() -> None:
    source = open("operations_surface.py", encoding="utf-8").read()
    for token in ("<div", "<span", "<button", "innerHTML", "text/html"):
        assert token not in source


# ---------------------------------------------------------------------------
# Ingesting a foreign snapshot (previously: the skip branches had no coverage).
# ---------------------------------------------------------------------------


def test_preflight_steps_without_an_id_are_skipped_not_guessed() -> None:
    """A step that cannot name itself cannot become a readiness row."""
    report = ops.build_readiness_from_preflight(
        {
            "steps": [
                {"title": "Nameless", "status": "ready"},
                {"id": "auth", "title": "Auth", "status": "ready"},
            ],
            "local_probe": "not-run",
        }
    )
    assert [row.check_id for row in report.checks] == ["preflight:auth"]


def test_non_mapping_preflight_steps_are_skipped() -> None:
    """A snapshot is foreign data; a malformed entry must not crash or be invented."""
    report = ops.build_readiness_from_preflight(
        {"steps": ["not-a-step", 42, {"id": "auth", "title": "Auth", "status": "ready"}], "local_probe": "not-run"}
    )
    assert [row.check_id for row in report.checks] == ["preflight:auth"]


def test_a_preflight_snapshot_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ops.OperationsError, match="preflight snapshot must be a mapping"):
        ops.build_readiness_from_preflight(["auth", "model-source"])  # type: ignore[arg-type]


def test_a_step_with_no_title_falls_back_to_its_id() -> None:
    report = ops.build_readiness_from_preflight(
        {"steps": [{"id": "auth", "status": "ready"}], "local_probe": "not-run"}
    )
    assert report.checks[0].label == "auth"
