"""AP-030 — optional guest-desktop capability, offline.

No virtual machine, desktop session, service or provider is launched here. Every
platform fact, bake-off entry and grant is a value the test constructs, and the
assertions are about the contract's refusals — including that this story ships a
disabled adapter because nothing was observed.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import guest_desktop as gd
from guest_desktop import (
    BakeOffEntry,
    BakeOffReport,
    DesktopJustification,
    DesktopRefusedError,
    GuestBoundary,
    GuestDesktopAdapter,
    GuestFailure,
    GuestNetwork,
    HostFallbackRefusedError,
    PlatformProfile,
    QualificationRecord,
    ScreenshotPolicy,
    SharedFolder,
    SimplerToolAttempt,
    SurfaceGrant,
    default_disabled_adapter,
    evaluate_platform_qualification,
    redact_screenshot_text,
    render_capability,
    require_desktop_justification,
    ungoverned_host_surfaces,
)

FINGERPRINT = "sha256:" + "d" * 64
WORKSPACE = "~/M5_AP_Workspace"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def failed_attempt(capability: str = "browser_worker") -> SimplerToolAttempt:
    return SimplerToolAttempt(
        capability=capability,
        attempted=True,
        outcome="failed",
        reason_insufficient="the page only exposes the control through a native dialog",
    )


def justification(**overrides: Any) -> DesktopJustification:
    values: dict[str, Any] = {
        "task_id": "task-render",
        "gui_requirement": "export requires a native dialog that has no API",
        "attempts": (failed_attempt(), failed_attempt("http_api")),
        "alternatives_considered": ("manual operator export",),
    }
    values.update(overrides)
    return DesktopJustification(**values)


def resources(**overrides: Any) -> dict[str, int]:
    values = {
        "vcpus": 2,
        "memory_mib": 4096,
        "disk_mib": 20480,
        "display_width": 1280,
        "display_height": 800,
    }
    values.update(overrides)
    return values


def observed_profile(**overrides: Any) -> PlatformProfile:
    values: dict[str, Any] = {
        "profile_id": "guest-a",
        "guest_os": "linux",
        "guest_version": "6.5.0",
        "resources": resources(),
        "host_fingerprint": FINGERPRINT,
        "observed": True,
    }
    values.update(overrides)
    return PlatformProfile(**values)


def qualified_record(**overrides: Any) -> QualificationRecord:
    values: dict[str, Any] = {
        "state": "qualified",
        "profile_id": "guest-a",
        "passed_checks": ("guest.boots", "host.isolation_holds"),
        "verified_by": "operator-a",
        "observed_at": 1_700_000_000.0,
    }
    values.update(overrides)
    return QualificationRecord(**values)


# ---------------------------------------------------------------------------
# AC1 — the task must justify desktop control over simpler tools
# ---------------------------------------------------------------------------


def test_a_task_with_failed_simpler_alternatives_is_justified() -> None:
    assert justification().is_justified is True


def test_a_desktop_task_without_any_alternative_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must record which simpler"):
        justification(attempts=())


def test_a_sufficient_simpler_tool_defeats_the_justification() -> None:
    with pytest.raises(DesktopRefusedError, match="was sufficient"):
        justification(
            attempts=(
                failed_attempt(),
                SimplerToolAttempt("http_api", True, "sufficient"),
            )
        )


def test_an_untried_capability_cannot_be_claimed_insufficient() -> None:
    with pytest.raises(DesktopRefusedError, match="never attempted"):
        SimplerToolAttempt(
            "cli",
            attempted=False,
            outcome="failed",
            reason_insufficient="not available",
        )


def test_an_attempt_without_a_reason_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="no reason"):
        SimplerToolAttempt("browser_worker", attempted=True, outcome="failed")


def test_an_unknown_simpler_capability_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="unknown capability"):
        SimplerToolAttempt(
            "vibes", attempted=True, outcome="failed", reason_insufficient="x"
        )


def test_the_justification_records_what_was_attempted() -> None:
    assert justification().attempted_capabilities == ("browser_worker", "http_api")


def test_require_desktop_justification_returns_the_record() -> None:
    assert require_desktop_justification(justification()).task_id == "task-render"


def test_a_task_is_justified_only_per_task_not_per_project() -> None:
    first = justification(task_id="task-a")
    second = justification(task_id="task-b", attempts=(failed_attempt("cli"),))
    assert first.task_id != second.task_id
    assert first.attempted_capabilities != second.attempted_capabilities


# ---------------------------------------------------------------------------
# AC2 — exact platform, version and resources are observed, not claimed
# ---------------------------------------------------------------------------


def test_a_complete_observed_profile_is_qualifiable() -> None:
    assert observed_profile().is_complete_for_qualification is True


def test_a_profile_missing_a_resource_dimension_is_refused() -> None:
    partial = resources()
    del partial["display_width"]
    with pytest.raises(DesktopRefusedError, match="every resource dimension"):
        observed_profile(resources=partial)


def test_a_profile_with_an_unknown_dimension_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="unknown resource dimensions"):
        observed_profile(resources=resources(gpu_mib=1024))


def test_a_non_positive_resource_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must be positive"):
        observed_profile(resources=resources(vcpus=0))


def test_a_malformed_host_fingerprint_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="sha256"):
        observed_profile(host_fingerprint="macbook-pro")


def test_a_claimed_observation_must_be_a_boolean() -> None:
    with pytest.raises(DesktopRefusedError, match="claim, not an observation"):
        observed_profile(observed=cast(Any, "true"))


def test_a_qualified_record_needs_passed_checks() -> None:
    with pytest.raises(DesktopRefusedError, match="list the checks"):
        qualified_record(passed_checks=())


def test_a_qualified_record_cannot_carry_failed_checks() -> None:
    with pytest.raises(DesktopRefusedError, match="cannot carry failed checks"):
        qualified_record(failed_checks=("host.isolation_holds",))


def test_a_qualified_record_needs_a_named_verifier() -> None:
    with pytest.raises(DesktopRefusedError, match="who verified it"):
        qualified_record(verified_by="")


def test_an_unqualified_record_must_name_the_failure() -> None:
    with pytest.raises(DesktopRefusedError, match="must name the check that failed"):
        QualificationRecord(state="observed_unqualified", profile_id="guest-a")


def test_a_not_measured_record_cannot_smuggle_evidence() -> None:
    with pytest.raises(DesktopRefusedError, match="must not carry"):
        QualificationRecord(state="not_measured", profile_id="guest-a")


def test_an_unknown_qualification_state_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="unknown qualification state"):
        QualificationRecord(state=cast(Any, "probably_fine"))


def test_an_observed_qualified_platform_is_admitted() -> None:
    admitted, reason = evaluate_platform_qualification(
        observed_profile(), qualified_record()
    )
    assert admitted is True
    assert "operator-a" in reason


def test_a_not_measured_platform_is_refused() -> None:
    admitted, reason = evaluate_platform_qualification(
        observed_profile(), QualificationRecord(state="not_measured")
    )
    assert admitted is False
    assert "not measured" in reason


def test_an_unobserved_profile_cannot_be_qualified_even_with_a_record() -> None:
    admitted, reason = evaluate_platform_qualification(
        observed_profile(observed=False), qualified_record()
    )
    assert admitted is False
    assert "not observed" in reason


def test_evidence_for_another_platform_does_not_transfer() -> None:
    admitted, reason = evaluate_platform_qualification(
        observed_profile(profile_id="guest-b"), qualified_record(profile_id="guest-a")
    )
    assert admitted is False
    assert "does not transfer" in reason


def test_no_profile_means_no_platform_is_qualified() -> None:
    admitted, reason = evaluate_platform_qualification(
        None, QualificationRecord(state="not_measured")
    )
    assert admitted is False
    assert "no platform profile" in reason


def test_an_observed_but_failed_platform_is_refused() -> None:
    admitted, reason = evaluate_platform_qualification(
        observed_profile(),
        QualificationRecord(
            state="observed_unqualified",
            profile_id="guest-a",
            failed_checks=("host.isolation_holds",),
        ),
    )
    assert admitted is False
    assert "unqualified" in reason


# ---------------------------------------------------------------------------
# AC3 — host surfaces stay outside guest authority unless separately granted
# ---------------------------------------------------------------------------


def test_every_host_surface_is_denied_by_default() -> None:
    assert ungoverned_host_surfaces(()) == gd.HOST_SURFACES
    assert GuestBoundary().granted_surfaces() == ()
    assert GuestBoundary().denied_surfaces() == gd.HOST_SURFACES


def test_a_grant_requires_an_explicit_scope() -> None:
    with pytest.raises(DesktopRefusedError, match="requires an explicit scope"):
        SurfaceGrant("host_filesystem", granted=True)


def test_a_refused_grant_cannot_carry_a_scope() -> None:
    with pytest.raises(DesktopRefusedError, match="must not carry a scope"):
        SurfaceGrant("host_filesystem", granted=False, scope="everything")


def test_an_unknown_host_surface_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="unknown host surface"):
        SurfaceGrant("host_secrets", granted=True, scope="all")


def test_one_grant_does_not_imply_another() -> None:
    boundary = GuestBoundary(
        grants=(SurfaceGrant("host_filesystem", granted=True, scope="share-a"),)
    )
    assert boundary.granted_surfaces() == ("host_filesystem",)
    assert "host_accounts" in boundary.denied_surfaces()
    assert "host_clipboard" in boundary.denied_surfaces()


def test_host_credentials_are_never_shared_with_a_guest() -> None:
    with pytest.raises(DesktopRefusedError, match="never shared"):
        GuestBoundary(credentials_shared=True)


def test_a_surface_cannot_be_granted_twice() -> None:
    grant = SurfaceGrant("host_filesystem", granted=True, scope="share-a")
    with pytest.raises(DesktopRefusedError, match="one surface, one decision"):
        GuestBoundary(grants=(grant, grant))


def test_host_accounts_grant_is_auditable() -> None:
    boundary = GuestBoundary(
        grants=(
            SurfaceGrant(
                "host_accounts", granted=True, scope="account-demo", read_only=True
            ),
        )
    )
    assert boundary.to_mapping()["granted_surfaces"] == ["host_accounts"]


# ---------------------------------------------------------------------------
# AC4 — shared folders, credentials, networking and screenshots are scoped
# ---------------------------------------------------------------------------


def test_a_share_outside_the_workspace_root_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must live under"):
        SharedFolder("share-a", "/mnt/guest/a", "/Users/owner/Documents")


def test_a_share_exposing_a_secret_file_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="secret material"):
        SharedFolder("share-a", "/mnt/guest/env", f"{WORKSPACE}/.env")


def test_a_share_reaching_git_metadata_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="repository metadata"):
        SharedFolder("share-a", "/mnt/guest/git", f"{WORKSPACE}/repo/.git")


def test_a_share_with_traversal_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="repository metadata or escapes"):
        SharedFolder("share-a", "/mnt/guest/up", f"{WORKSPACE}/../secrets")


def test_a_confined_read_only_share_is_accepted() -> None:
    share = SharedFolder("share-a", "/mnt/guest/a", f"{WORKSPACE}/share-a")
    assert share.read_only is True


def test_overlapping_shares_are_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="overlap"):
        GuestBoundary(
            shares=(
                SharedFolder("share-a", "/mnt/guest/a", f"{WORKSPACE}/shared"),
                SharedFolder("share-b", "/mnt/guest/b", f"{WORKSPACE}/shared/deeper"),
            )
        )


def test_guest_network_defaults_to_none() -> None:
    mapping = GuestNetwork().to_mapping()
    assert mapping["mode"] == "none"
    assert mapping["bridges_host_network"] is False


def test_guest_network_may_not_forward_host_credentials() -> None:
    with pytest.raises(DesktopRefusedError, match="may not forward host credentials"):
        GuestNetwork(
            mode="allowlist",
            allowed_hosts=("a.example",),
            forwards_host_credentials=True,
        )


def test_an_unsupported_network_mode_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="not supported"):
        GuestNetwork(mode="bridged")


def test_no_network_mode_cannot_carry_an_allowlist() -> None:
    with pytest.raises(DesktopRefusedError, match="must not carry an allowlist"):
        GuestNetwork(mode="none", allowed_hosts=("a.example",))


def test_an_allowlist_network_must_name_its_hosts() -> None:
    with pytest.raises(DesktopRefusedError, match="must name the hosts"):
        GuestNetwork(mode="allowlist")


def test_screenshots_are_disabled_by_default() -> None:
    mapping = ScreenshotPolicy().to_mapping()
    assert mapping["enabled"] is False
    assert mapping["captures_host_desktop"] is False


def test_screenshots_outside_the_workspace_root_are_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must land under"):
        ScreenshotPolicy(enabled=True, destination="/tmp/shots", max_bytes=1024)


def test_unredacted_screenshots_are_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must be redacted"):
        ScreenshotPolicy(
            enabled=True, destination=f"{WORKSPACE}/shots", redact=False, max_bytes=1024
        )


def test_an_enabled_screenshot_policy_must_cap_its_size() -> None:
    with pytest.raises(DesktopRefusedError, match="cap its own size"):
        ScreenshotPolicy(enabled=True, destination=f"{WORKSPACE}/shots", max_bytes=0)


def test_a_disabled_screenshot_policy_cannot_name_a_target() -> None:
    with pytest.raises(DesktopRefusedError, match="must not name a destination"):
        ScreenshotPolicy(enabled=False, destination=f"{WORKSPACE}/shots")


def test_a_scoped_redacted_screenshot_policy_is_accepted() -> None:
    policy = ScreenshotPolicy(
        enabled=True, destination=f"{WORKSPACE}/shots", max_bytes=2_000_000
    )
    assert policy.to_mapping()["redact"] is True


def test_screenshot_text_is_redacted_before_storage() -> None:
    redacted, count = redact_screenshot_text("owner token=abc123secret")
    assert count >= 1
    assert "abc123secret" not in redacted


def test_a_raw_host_clipboard_grant_is_refused() -> None:
    with pytest.raises(
        DesktopRefusedError, match="only be granted to the guest redacted"
    ):
        SurfaceGrant("host_clipboard", granted=True, scope="clip", redacted=False)


def test_a_writable_host_capture_grant_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="read-only surface"):
        SurfaceGrant("host_screenshots", granted=True, scope="screen", read_only=False)


# ---------------------------------------------------------------------------
# AC5 — a guest failure cannot fall back to controlling the host
# ---------------------------------------------------------------------------


def test_a_contained_guest_failure_offers_no_host_fallback() -> None:
    failure = GuestFailure(reason="dialog never appeared", at_stage="export")
    assert failure.host_fallback_available is False
    assert failure.to_mapping()["host_actions_taken"] == 0
    assert "do not retry on the host" in failure.to_mapping()["next_step"]


def test_a_host_action_after_a_guest_failure_is_refused() -> None:
    with pytest.raises(HostFallbackRefusedError, match="may never fall back"):
        GuestFailure(
            reason="dialog never appeared", at_stage="export", host_actions_taken=1
        )


def test_an_uncontained_guest_failure_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="inside the guest boundary"):
        GuestFailure(reason="escaped", at_stage="boot", contained=False)


def test_a_failure_without_a_reason_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must not be empty"):
        GuestFailure(reason="", at_stage="boot")


def test_no_host_fallback_accepts_zero_host_actions() -> None:
    gd.assert_no_host_fallback(host_actions_taken=0)


def test_no_host_fallback_refuses_any_host_action() -> None:
    with pytest.raises(HostFallbackRefusedError, match="stop and report"):
        gd.assert_no_host_fallback(host_actions_taken=2)


# ---------------------------------------------------------------------------
# AC6 — publish maturity/limitations, keep the adapter disabled unless qualified
# ---------------------------------------------------------------------------


def test_the_shipped_adapter_is_disabled_and_not_qualified() -> None:
    adapter = default_disabled_adapter()
    mapping = adapter.to_mapping()
    assert mapping["enabled"] is False
    assert mapping["qualified"] is False
    assert mapping["maturity"] == "not_qualified"
    assert mapping["has_executor"] is False


def test_the_shipped_adapter_publishes_its_limitations() -> None:
    limitations = default_disabled_adapter().limitations
    assert len(limitations) >= 3
    assert any("no guest desktop was launched" in line for line in limitations)


def test_an_unqualified_adapter_cannot_be_enabled() -> None:
    with pytest.raises(DesktopRefusedError, match="may not be enabled"):
        GuestDesktopAdapter(
            adapter_id="guest-desktop",
            qualification=QualificationRecord(state="not_measured"),
            limitations=("not observed",),
            enabled_flag=True,
        )


def test_an_adapter_cannot_claim_qualified_maturity_without_a_record() -> None:
    with pytest.raises(DesktopRefusedError, match="requires a qualified record"):
        GuestDesktopAdapter(
            adapter_id="guest-desktop",
            qualification=QualificationRecord(state="not_measured"),
            maturity="qualified",
            limitations=("not observed",),
        )


def test_an_adapter_without_limitations_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must publish its limitations"):
        GuestDesktopAdapter(
            adapter_id="guest-desktop",
            qualification=QualificationRecord(state="not_measured"),
            limitations=(),
        )


def test_a_qualified_adapter_may_only_be_enabled_explicitly() -> None:
    adapter = GuestDesktopAdapter(
        adapter_id="guest-desktop",
        qualification=qualified_record(),
        maturity="qualified",
        enabled_flag=True,
    )
    assert adapter.enabled is True


def test_a_qualified_adapter_is_not_enabled_by_default() -> None:
    adapter = GuestDesktopAdapter(
        adapter_id="guest-desktop",
        qualification=qualified_record(),
        maturity="qualified",
    )
    assert adapter.enabled is False


def test_an_unknown_maturity_state_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="unknown maturity"):
        GuestDesktopAdapter(
            adapter_id="guest-desktop",
            qualification=QualificationRecord(state="not_measured"),
            maturity=cast(Any, "beta"),
            limitations=("x",),
        )


def test_an_empty_bake_off_recommends_nothing() -> None:
    report = BakeOffReport()
    assert report.measured is False
    outcome = report.recommend()
    assert outcome["measured"] is False
    assert outcome["recommendation"] is None


def test_a_bake_off_recommends_the_fewest_host_interventions() -> None:
    report = BakeOffReport(
        entries=(
            BakeOffEntry("export", "api", True, 120.0, 3),
            BakeOffEntry("export", "guest-desktop", True, 900.0, 0),
        ),
        candidates=("api", "guest-desktop"),
    )
    assert report.recommend()["recommendation"]["export"]["driver"] == "guest-desktop"


def test_a_bake_off_prefers_lower_latency_on_a_tie() -> None:
    report = BakeOffReport(
        entries=(
            BakeOffEntry("export", "api", True, 900.0, 0),
            BakeOffEntry("export", "guest-desktop", True, 400.0, 0),
        )
    )
    assert report.recommend()["recommendation"]["export"]["driver"] == "guest-desktop"


def test_a_bake_off_with_no_completed_driver_recommends_none() -> None:
    report = BakeOffReport(
        entries=(
            BakeOffEntry(
                "export", "guest-desktop", False, 30.0, 2, failure="guest hung"
            ),
        )
    )
    assert report.recommend()["recommendation"]["export"]["driver"] is None


def test_a_failed_bake_off_entry_must_state_why() -> None:
    with pytest.raises(DesktopRefusedError, match="must state why"):
        BakeOffEntry("export", "guest-desktop", False, 30.0, 2)


def test_a_negative_latency_is_refused() -> None:
    with pytest.raises(DesktopRefusedError, match="must not be negative"):
        BakeOffEntry("export", "api", True, -1.0, 0)


def test_the_bake_off_digest_is_stable() -> None:
    first = BakeOffReport(entries=(BakeOffEntry("export", "api", True, 120.0, 3),))
    second = BakeOffReport(entries=(BakeOffEntry("export", "api", True, 120.0, 3),))
    assert first.digest() == second.digest()


# ---------------------------------------------------------------------------
# Reviewability
# ---------------------------------------------------------------------------


def test_the_rendered_capability_is_deterministic_json() -> None:
    render = lambda: render_capability(  # noqa: E731
        justification=justification(),
        profile=observed_profile(),
        qualification=QualificationRecord(state="not_measured"),
        boundary=GuestBoundary(),
        bake_off=BakeOffReport(),
    )
    first = render()
    assert first == render()
    assert first.endswith("\n")
    parsed = __import__("json").loads(first)
    assert list(parsed) == sorted(parsed)


def test_the_rendered_capability_states_every_boundary() -> None:
    import json

    rules = json.loads(
        render_capability(
            justification=justification(),
            profile=observed_profile(),
            qualification=QualificationRecord(state="not_measured"),
        )
    )["desktop_rules"]
    assert rules["desktop_permitted_without_task_justification"] is False
    assert rules["platform_admitted_without_observation"] is False
    assert rules["host_surfaces_granted_implicitly"] is False
    assert rules["host_credentials_shared_with_guest"] is False
    assert rules["unredacted_host_capture_permitted"] is False
    assert rules["guest_failure_may_fall_back_to_host"] is False
    assert rules["adapter_enabled_without_qualification"] is False


def test_the_rendered_capability_does_not_overclaim() -> None:
    import json

    payload = json.loads(
        render_capability(
            justification=None,
            profile=None,
            qualification=QualificationRecord(state="not_measured"),
        )
    )
    assert len(payload["does_not_prove"]) == 3
    assert payload["platform"]["admitted"] is False
    assert payload["adapter"]["enabled"] is False


def test_the_rendered_capability_shows_an_unevidenced_task_as_unjustified() -> None:
    import json

    payload = json.loads(
        render_capability(
            justification=None,
            profile=None,
            qualification=QualificationRecord(state="not_measured"),
        )
    )
    assert payload["justification"] is None
    assert (
        payload["desktop_rules"]["desktop_permitted_without_task_justification"]
        is False
    )


def test_no_module_verb_launches_a_vm_or_drives_the_host() -> None:
    forbidden = (
        "launch_vm",
        "start_vm",
        "run_task",
        "execute",
        "activate",
        "control_host",
        "inject_input",
        "open_display",
    )
    assert not [name for name in forbidden if hasattr(gd, name)]
