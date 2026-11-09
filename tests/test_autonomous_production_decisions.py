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
    assert {decision.status for decision in bundle.decisions} == {"pending"}
    assert {
        decision.id: len(decision.blocked_story_ids) for decision in bundle.decisions
    } == {
        "U01": 6,
        "U02": 8,
        "U03": 10,
        "U04": 10,
        "U05": 3,
        "U06": 9,
    }
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
    blocked.pop()

    with pytest.raises(
        OperatingEnvelopeError, match="must match the AP roadmap exactly"
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
    envelope = _read(ENVELOPE)
    promotion = envelope["promotion_policy"]
    assert isinstance(promotion, dict)
    classes = promotion["classes"]
    assert isinstance(classes, list)
    project_lesson = classes[1]
    assert isinstance(project_lesson, dict)
    project_lesson["automatic_promotion"] = True

    with pytest.raises(OperatingEnvelopeError, match="U05 is pending"):
        _load_mutated(tmp_path, envelope=envelope)


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
        decision_id: "pending" for decision_id in [f"U{n:02d}" for n in range(1, 7)]
    }
