from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from autonomous_production_decisions import (
    ActionRequest,
    OperatingEnvelopeError,
    evaluate_policy,
    load_operating_envelope,
)

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = ROOT / "product" / "M5_Autonomous_Production_Operating_Envelope.json"
POLICIES = ROOT / "product" / "M5_Autonomous_Production_Policy_Examples.json"
ROADMAP = ROOT / "product" / "M5_Autonomous_Production_Roadmap.json"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: dict[str, object]) -> Path:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _load_mutated(
    tmp_path: Path,
    *,
    envelope: dict[str, object] | None = None,
    policies: dict[str, object] | None = None,
):
    envelope_path = _write(tmp_path / "envelope.json", envelope or _read(ENVELOPE))
    policy_path = _write(tmp_path / "policies.json", policies or _read(POLICIES))
    return load_operating_envelope(
        envelope_path,
        roadmap_path=ROADMAP,
        policy_examples_path=policy_path,
    )


def test_repository_bundle_separates_owner_target_from_measured_inventory() -> None:
    bundle = load_operating_envelope()

    assert bundle.runtime_activation_authorized is False
    assert (
        bundle.owner_hardware.platform
        == "Apple M5 / 24 GB unified memory / 1 TB storage"
    )
    assert bundle.owner_hardware.physical_memory_bytes == 24 * 1024**3
    assert bundle.owner_hardware.storage_capacity_bytes == 1024**4
    assert bundle.inventory.status == "not_measured"
    assert bundle.inventory.hardware_profile is None
    assert [decision.id for decision in bundle.decisions] == [
        f"U{number:02d}" for number in range(1, 7)
    ]
    by_id = {decision.id: decision for decision in bundle.decisions}
    assert set(by_id) == {"U01", "U02", "U03", "U04", "U05", "U06"}
    for decision_id, decision in by_id.items():
        assert decision.status == "resolved", decision_id
        assert decision.resolution, decision_id
        assert decision.blocked_story_ids == (), decision_id
        assert decision.blocked_actions == (), decision_id
    assert not any(item.automatic_promotion for item in bundle.promotion_classes)


def test_decision_register_blocked_stories_are_source_bound_to_roadmap(
    tmp_path: Path,
) -> None:
    envelope = _read(ENVELOPE)
    decisions = envelope["decisions"]
    assert isinstance(decisions, list)
    first = decisions[0]
    assert isinstance(first, dict)
    blocked = first["blocked_story_ids"]
    assert isinstance(blocked, list)
    # U01 is resolved; resolved decisions must carry an empty blocker list,
    # so injecting one must trip the same strict source-binding check.
    blocked.append("AP-002")

    with pytest.raises(
        OperatingEnvelopeError, match="cannot retain blockers after resolution"
    ):
        _load_mutated(tmp_path, envelope=envelope)


def test_local_default_denies_when_no_explicit_grant_exists() -> None:
    bundle = load_operating_envelope()
    fixture = bundle.policy_example("fictional-local-offline-deny")

    verdict = evaluate_policy(
        fixture,
        ActionRequest(
            capability="public_research",
            provider="fictional-research-service",
            egress_destination="https://research.example.invalid",
            account="fictional-read-only-account",
            read_path="fixtures/project-b/queries/query.txt",
            write_path="fixtures/project-b/sources/result.json",
            data_classes=("public_query",),
        ),
    )

    assert verdict.allowed is False
    assert verdict.matched_grant_id is None
    assert verdict.reasons == ("no_explicit_grants",)


def test_fictional_media_grant_allows_only_its_exact_combined_scope() -> None:
    bundle = load_operating_envelope()
    fixture = bundle.policy_example("fictional-reviewed-media-export")
    request = ActionRequest(
        capability="external_non_inference_media",
        provider="fictional-media-service",
        egress_destination="https://media.example.invalid",
        account="fictional-media-review-account",
        read_path="fixtures/project-a/input/storyboard.json",
        write_path="fixtures/project-a/output/draft.mp4",
        currency="USD",
        spend_minor_units=2000,
        data_classes=("generated_draft", "public_source"),
    )

    allowed = evaluate_policy(fixture, request)
    assert allowed.allowed is True
    assert allowed.matched_grant_id == "fictional-grant-media-001"
    assert allowed.reasons == ()

    incomplete = evaluate_policy(
        fixture,
        ActionRequest(
            capability=request.capability,
            provider=request.provider,
        ),
    )
    assert incomplete.allowed is False
    assert set(incomplete.reasons) == {
        "account_scope_missing",
        "data_class_scope_missing",
        "egress_destination_scope_missing",
        "read_path_scope_missing",
        "spending_scope_missing",
        "write_path_scope_missing",
    }

    partial_media = copy.deepcopy(fixture)
    object.__setattr__(partial_media, "grants", (fixture.grants[0],))
    partial_verdict = evaluate_policy(
        partial_media,
        ActionRequest(capability="public_research"),
    )
    assert partial_verdict.allowed is False
    assert partial_verdict.matched_grant_id is None
    assert "capability_not_granted" in partial_verdict.reasons
    assert "account_scope_missing" in partial_verdict.reasons

    wrong_account = evaluate_policy(
        fixture,
        ActionRequest(
            capability=request.capability,
            provider=request.provider,
            egress_destination=request.egress_destination,
            account="another-account",
            read_path=request.read_path,
            write_path=request.write_path,
            currency=request.currency,
            spend_minor_units=request.spend_minor_units,
            data_classes=request.data_classes,
        ),
    )
    assert wrong_account.allowed is False
    assert "account_not_granted" in wrong_account.reasons

    over_budget = evaluate_policy(
        fixture,
        ActionRequest(
            capability=request.capability,
            provider=request.provider,
            egress_destination=request.egress_destination,
            account=request.account,
            read_path=request.read_path,
            write_path=request.write_path,
            currency="USD",
            spend_minor_units=2501,
            data_classes=request.data_classes,
        ),
    )
    assert over_budget.allowed is False
    assert over_budget.reasons == ("fixture_spending_budget_exceeded",)


def test_external_reasoning_needs_a_separate_explicit_policy_even_in_scoped_fixture() -> (
    None
):
    bundle = load_operating_envelope()
    fixture = bundle.policy_example("fictional-reviewed-media-export")

    verdict = evaluate_policy(
        fixture,
        ActionRequest(
            capability="external_reasoning_model",
            provider="fictional-media-service",
        ),
    )

    assert verdict.allowed is False
    assert verdict.reasons == ("external_reasoning_requires_separate_explicit_policy",)


def test_policy_examples_require_every_scope_and_reject_unknown_fields(
    tmp_path: Path,
) -> None:
    policies = _read(POLICIES)
    examples = policies["examples"]
    assert isinstance(examples, list)
    media = examples[1]
    assert isinstance(media, dict)
    grants = media["grants"]
    assert isinstance(grants, list)
    grant = grants[0]
    assert isinstance(grant, dict)
    del grant["accounts"]

    with pytest.raises(
        OperatingEnvelopeError, match="missing explicit field.*accounts"
    ):
        _load_mutated(tmp_path, policies=policies)

    envelope = _read(ENVELOPE)
    envelope["implicit_consent"] = True
    with pytest.raises(OperatingEnvelopeError, match="unknown field.*implicit_consent"):
        _load_mutated(tmp_path, envelope=envelope)


def test_pending_promotion_decision_forbids_automatic_promotion(tmp_path: Path) -> None:
    # U05 is resolved in the repository (all automatic flags false). Exercise
    # the pending-U05 guard with a synthetic pending pair bound to a mutated
    # roadmap so the guard stays covered.
    envelope = _read(ENVELOPE)
    roadmap = _read(ROADMAP)
    roadmap_stories = roadmap["stories"]
    assert isinstance(roadmap_stories, list)
    affected = 0
    for story in roadmap_stories:
        assert isinstance(story, dict)
        if story.get("id") in ("AP-002", "AP-026", "AP-028"):
            story["blocked_by_decisions"] = ["U05"]
            affected += 1
    assert affected == 3
    for decision in envelope["decisions"]:
        assert isinstance(decision, dict)
        if decision["id"] == "U05":
            decision["status"] = "pending"
            decision["resolution"] = None
            decision["blocked_story_ids"] = ["AP-002", "AP-026", "AP-028"]
            decision["blocked_actions"] = [
                "Automatically promote a lesson, skill, adapter, model route, "
                "evaluator or authority change into persistent production use."
            ]
    promotion = envelope["promotion_policy"]
    assert isinstance(promotion, dict)
    promotion["decision_status"] = "pending"
    classes = promotion["classes"]
    assert isinstance(classes, list)
    project_lesson = classes[1]
    assert isinstance(project_lesson, dict)
    project_lesson["automatic_promotion"] = True

    envelope_path = _write(tmp_path / "envelope.json", envelope)
    roadmap_path = _write(tmp_path / "roadmap.json", roadmap)
    policy_path = _write(tmp_path / "policies.json", _read(POLICIES))
    with pytest.raises(OperatingEnvelopeError, match="U05 is pending"):
        load_operating_envelope(
            envelope_path, roadmap_path=roadmap_path, policy_examples_path=policy_path
        )


def test_inventory_recipe_accepts_generic_observation_without_changing_owner_statement(
    tmp_path: Path,
) -> None:
    envelope = _read(ENVELOPE)
    envelope["measured_inventory"] = {
        "status": "observed_unqualified",
        "observed_at": "2026-11-09T16:15:00Z",
        "collector": "fictional-collector/1",
        "hardware_profile": {
            "schema_version": 1,
            "operating_system": "ExampleOS",
            "architecture": "future-architecture",
            "accelerators": [],
            "physical_memory_bytes": 16 * 1024**3,
            "available_memory_bytes": 7 * 1024**3,
            "memory_pressure": "nominal",
            "installed_runtimes": [
                {
                    "name": "fictional-runtime",
                    "version": "1.2.3",
                    "capabilities": [],
                }
            ],
            "permissions": [],
        },
        "available_storage_bytes": 500 * 1024**3,
        "qualified_headroom_bytes": None,
        "qualification_record": None,
    }

    bundle = _load_mutated(tmp_path, envelope=envelope)

    assert bundle.inventory.status == "observed_unqualified"
    assert bundle.inventory.hardware_profile is not None
    assert bundle.inventory.hardware_profile.architecture == "future-architecture"
    assert bundle.inventory.qualified_headroom_bytes is None
    assert bundle.owner_hardware.platform.startswith("Apple M5")


def test_not_measured_inventory_cannot_smuggle_observed_values(tmp_path: Path) -> None:
    envelope = _read(ENVELOPE)
    inventory = envelope["measured_inventory"]
    assert isinstance(inventory, dict)
    inventory["available_storage_bytes"] = 1

    with pytest.raises(
        OperatingEnvelopeError, match="not_measured.*must keep observation fields null"
    ):
        _load_mutated(tmp_path, envelope=envelope)


def test_policy_paths_are_confined_and_cannot_escape(tmp_path: Path) -> None:
    policies = _read(POLICIES)
    examples = policies["examples"]
    assert isinstance(examples, list)
    media = examples[1]
    assert isinstance(media, dict)
    grants = media["grants"]
    assert isinstance(grants, list)
    grant = grants[0]
    assert isinstance(grant, dict)
    grant["read_roots"] = ["../../real-home"]

    with pytest.raises(OperatingEnvelopeError, match="confined relative path"):
        _load_mutated(tmp_path, policies=policies)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_roots", ["fixtures/project-ab"]),
        ("write_roots", ["fixtures/project-ab"]),
    ],
)
def test_policy_path_prefixes_do_not_cross_sibling_boundaries(
    field: str,
    value: list[str],
) -> None:
    bundle = load_operating_envelope()
    fixture = copy.deepcopy(bundle.policy_example("fictional-reviewed-media-export"))
    grant = fixture.grants[0]
    object.__setattr__(grant, field, tuple(value))
    request = ActionRequest(
        capability="external_non_inference_media",
        provider="fictional-media-service",
        egress_destination="https://media.example.invalid",
        account="fictional-media-review-account",
        read_path="fixtures/project-a/input/storyboard.json",
        write_path="fixtures/project-a/output/draft.mp4",
        currency="USD",
        spend_minor_units=2000,
        data_classes=("generated_draft", "public_source"),
    )

    verdict = evaluate_policy(fixture, request)

    assert verdict.allowed is False
    denial = (
        "read_path_not_granted" if field == "read_roots" else "write_path_not_granted"
    )
    assert denial in verdict.reasons


def test_cli_validates_repository_bundle_without_host_or_network_probe() -> None:
    result = subprocess.run(
        [sys.executable, "autonomous_production_decisions.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["runtime_activation_authorized"] is False
    assert payload["inventory_status"] == "not_measured"
    assert payload["automatic_promotion_classes"] == []
    assert payload["decision_states"] == {
        decision_id: "resolved" for decision_id in [f"U{n:02d}" for n in range(1, 7)]
    }


def _marker(story: dict[str, object]) -> dict[str, object]:
    tracking = story.get("tracking")
    assert isinstance(tracking, dict)
    marker = tracking.get("acceptance_outstanding")
    assert isinstance(marker, dict)
    return marker


def test_owner_acceptance_pending_is_never_silently_done() -> None:
    """A story may be done with acceptance outstanding, but only if it says so.

    Revision 3.3 records AP-031 and AP-032 as done because their bounded
    contracts and tests merged, while their own deliverables and owner
    acceptance are outstanding. The marker is what keeps that honest: a done
    story carrying it must name the owner as pending, the deliverables it did
    not deliver, the actions that remain, and the release effect. If any of
    that disappears, "done" starts reading as "delivered", which is the
    exact defect the marker exists to prevent.
    """
    roadmap = _read(ROADMAP)
    stories = roadmap["stories"]
    assert isinstance(stories, list)
    marked = 0
    for story in stories:
        assert isinstance(story, dict)
        tracking = story.get("tracking")
        assert isinstance(tracking, dict)
        if "acceptance_outstanding" not in tracking:
            continue
        marked += 1
        assert story["status"] == "done", f"{story['id']}: marker on a story that is not done"
        assert tracking["delivery_status"] == "merged"
        marker = _marker(story)
        assert marker["owner_acceptance"] == "pending"
        assert marker["release_effect"] == "release-blocked"
        assert marker["recorded_by"] and marker["recorded_at"]
        assert marker["deliverables_not_delivered"], (
            f"{story['id']}: an outstanding-acceptance marker that names nothing undelivered is decoration"
        )
        assert marker["outstanding_actions"]
        for item in marker["deliverables_not_delivered"]:
            assert item in story["deliverables"], (
                f"{story['id']}: {item!r} is not one of this story's deliverables, so the marker is describing "
                "some other story's gap"
            )
    assert marked == 2, f"expected AP-031 and AP-032 to carry the marker, found {marked}"


def _check_marker(story: dict[str, object]) -> None:
    """The rule itself, applied to any story so mutations can be tested against it."""
    tracking = story["tracking"]
    assert isinstance(tracking, dict)
    marker = tracking.get("acceptance_outstanding")
    if marker is None:
        return
    assert isinstance(marker, dict)
    assert story["status"] == "done", f"{story['id']}: marker on a story that is not done"
    assert tracking["delivery_status"] == "merged"
    assert marker["recorded_by"] and marker["recorded_at"]
    if marker["owner_acceptance"] == "pending":
        assert marker["release_effect"] == "release-blocked", (
            f"{story['id']}: owner acceptance is pending but the release effect says otherwise"
        )
        assert marker["deliverables_not_delivered"], (
            f"{story['id']}: an outstanding-acceptance marker that names nothing undelivered is decoration"
        )
        assert marker["outstanding_actions"], f"{story['id']}: pending acceptance with no next action is a dead end"
        known = story["deliverables"]
        for item in marker["deliverables_not_delivered"]:
            assert item in known, (
                f"{story['id']}: {item!r} is not one of this story's deliverables, so the marker is describing "
                "some other story's gap"
            )
    else:
        assert marker["release_effect"] != "release-blocked", (
            f"{story['id']}: acceptance recorded as given but the release is still marked blocked"
        )
        assert not marker["outstanding_actions"], (
            f"{story['id']}: acceptance recorded as given while actions are still outstanding"
        )


def test_acceptance_outstanding_markers_are_self_consistent() -> None:
    """Revision 3.3: AP-031 and AP-032 are done because their contracts merged, not their deliveries."""
    roadmap = _read(ROADMAP)
    stories = roadmap["stories"]
    assert isinstance(stories, list)
    marked = [story for story in stories if isinstance(story, dict) and "acceptance_outstanding" in story["tracking"]]
    for story in marked:
        _check_marker(story)
    assert [story["id"] for story in marked] == ["AP-031", "AP-032"], (
        f"expected exactly AP-031 and AP-032 to carry the marker, found {[s['id'] for s in marked]}"
    )


def test_the_marker_rule_rejects_acceptance_claimed_without_removal() -> None:
    """The rule must catch the mutation that matters: acceptance marked given while the gap stays open."""
    roadmap = copy.deepcopy(_read(ROADMAP))
    story = next(s for s in roadmap["stories"] if isinstance(s, dict) and s["id"] == "AP-032")
    marker = story["tracking"]["acceptance_outstanding"]
    marker["owner_acceptance"] = "given"

    with pytest.raises(AssertionError, match="still marked blocked"):
        _check_marker(story)

    marker["release_effect"] = "release-qualified"
    with pytest.raises(AssertionError, match="actions are still outstanding"):
        _check_marker(story)

    marker["outstanding_actions"] = []
    _check_marker(story)  # acceptance given, nothing outstanding: consistent


def test_a_marker_naming_a_foreign_deliverable_is_rejected() -> None:
    roadmap = copy.deepcopy(_read(ROADMAP))
    story = next(s for s in roadmap["stories"] if isinstance(s, dict) and s["id"] == "AP-031")
    story["tracking"]["acceptance_outstanding"]["deliverables_not_delivered"].append("Land on the moon")

    with pytest.raises(AssertionError, match="not one of this story's deliverables"):
        _check_marker(story)


def test_unmarked_done_stories_do_not_claim_outstanding_owner_acceptance() -> None:
    """The inverse: a story without the marker must not disclose pending acceptance in prose only."""
    roadmap = _read(ROADMAP)
    stories = roadmap["stories"]
    assert isinstance(stories, list)
    for story in stories:
        assert isinstance(story, dict)
        tracking = story["tracking"]
        assert isinstance(tracking, dict)
        if story["status"] != "done" or "acceptance_outstanding" in tracking:
            continue
        handoff = str(tracking.get("handoff") or "")
        assert "must be completed by the owner" not in handoff, (
            f"{story['id']}: discloses owner acceptance as incomplete in prose while carrying no marker; "
            "either record the marker or stop claiming the disclosure where nothing checks it"
        )


def test_outstanding_actions_never_request_an_already_resolved_decision() -> None:
    """A marker may not ask the owner for something the owner already decided.

    AP-032's marker originally said "Owner selects or defers the launch
    specialisation in this ledger." U01 was resolved on 2026-11-17 (issue #330:
    websites first, then video). Asking again is the same defect as claiming
    something unstarted that is done: a reader who trusts it will either redo a
    decision or conclude the record was never made.
    """
    envelope = load_operating_envelope()
    resolved = {decision.id for decision in envelope.decisions if decision.status == "resolved"}
    roadmap = _read(ROADMAP)
    stories = roadmap["stories"]
    assert isinstance(stories, list)

    # U01 is the decision that names the specialisation order.
    assert "U01" in resolved, "this check assumes U01 stays resolved; revisit it if that changes"

    for story in stories:
        assert isinstance(story, dict)
        marker = (story.get("tracking") or {}).get("acceptance_outstanding")
        if not marker:
            continue
        for action in marker["outstanding_actions"]:
            text = action.lower()
            if "launch specialisation" in text and ("select" in text or "defer" in text):
                pytest.fail(
                    f"{story['id']}: outstanding action asks for a launch-specialisation decision while U01 is "
                    f"resolved: {action!r}"
                )


def test_the_marker_records_the_specialisation_u01_selected() -> None:
    """The order is an owner selection with named provenance, not a suggestion."""
    roadmap = _read(ROADMAP)
    story = next(s for s in roadmap["stories"] if isinstance(s, dict) and s["id"] == "AP-032")
    recorded = story["tracking"]["acceptance_outstanding"]["specialisation"]
    assert recorded["selected_by"] == "owner"
    assert recorded["decision_id"] == "U01"
    assert recorded["decision_status"] == "resolved"
    assert recorded["order"] == ["website", "video"]
    assert recorded["first"] == "website"
    assert "issue #330" in recorded["recorded"]
