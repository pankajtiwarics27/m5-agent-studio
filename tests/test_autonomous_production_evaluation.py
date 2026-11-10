from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from autonomous_production_evaluation import (
    AP_DATASET_PARTITIONS,
    AP_QUALITY_CATEGORIES,
    AP_REQUIRED_FAILURE_KINDS,
    AP_RESOURCE_ACCOUNTING_FIELDS,
    DEFAULT_BRIEFS_PATH,
    DEFAULT_FIXTURES_PATH,
    DEFAULT_ROADMAP_PATH,
    EvaluationRegistryError,
    dataset_access_decision,
    eval_task_for_brief,
    load_evaluation_bundle,
    ordinary_optimizer_view,
    scrub_private_trace,
)

Mutator = Callable[[dict[str, Any]], None]


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _mutated_bundle(
    tmp_path: Path,
    *,
    briefs: Mutator | None = None,
    fixtures: Mutator | None = None,
    roadmap: Mutator | None = None,
):
    records = {
        "briefs": copy.deepcopy(_json(DEFAULT_BRIEFS_PATH)),
        "fixtures": copy.deepcopy(_json(DEFAULT_FIXTURES_PATH)),
        "roadmap": copy.deepcopy(_json(DEFAULT_ROADMAP_PATH)),
    }
    for name, mutate in (("briefs", briefs), ("fixtures", fixtures), ("roadmap", roadmap)):
        if mutate is not None:
            mutate(records[name])
    paths: dict[str, Path] = {}
    for name, record in records.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths[name] = path
    return load_evaluation_bundle(paths["briefs"], paths["fixtures"], paths["roadmap"])


def _private_trace(**updates: Any) -> dict[str, Any]:
    digest = "sha256:" + "ab" * 32
    trace: dict[str, Any] = {
        "strategy": "single_agent",
        "outcome": "completed",
        "task_success": True,
        "task_digest": digest,
        "config_digest": digest,
        "hardware_digest": digest,
        "latency_ms": 1250.5,
        "cost_minor_units": 12,
        "currency": "USD",
        "peak_memory_bytes": 4096,
        "input_tokens": 100,
        "output_tokens": 50,
        "tool_calls": 2,
        "retries": 1,
        "repair_attempts": 0,
        "user_intervention_seconds": 0.0,
        "failure_kinds": [],
    }
    trace.update(updates)
    return trace


def test_registry_has_versioned_four_category_briefs_and_existing_eval_task_identity() -> None:
    bundle = load_evaluation_bundle()
    assert bundle.schema_version == 1 and bundle.record_version == 1
    assert bundle.activation_authorized is False
    assert tuple(item.category_id for item in bundle.quality_categories) == AP_QUALITY_CATEGORIES
    assert all(item.criteria for item in bundle.quality_categories)
    assert {brief.domain for brief in bundle.briefs} == {"research", "website", "short_video"}
    assert all(brief.category_ids == AP_QUALITY_CATEGORIES for brief in bundle.briefs)

    task = eval_task_for_brief(bundle, "synthetic-research-brief")
    assert task.task_version == 1
    assert set(task.dimension_names()) == set(AP_QUALITY_CATEGORIES)
    assert task.digest == eval_task_for_brief(bundle, "synthetic-research-brief").digest


def test_single_agent_baseline_has_matched_resource_accounting_and_provisional_target() -> None:
    bundle = load_evaluation_bundle()
    assert bundle.baseline.strategy == "single_agent"
    assert bundle.baseline.agent_count == 1
    assert bundle.baseline.resource_accounting == AP_RESOURCE_ACCOUNTING_FIELDS
    assert bundle.baseline.acceptance_threshold == 0.8
    assert bundle.baseline.acceptance_binding is False
    with pytest.raises(EvaluationRegistryError, match="unknown evaluation brief"):
        eval_task_for_brief(bundle, "missing-brief")


def test_partition_counts_and_role_access_fail_closed_for_ordinary_optimizer() -> None:
    bundle = load_evaluation_bundle()
    assert {name: len(bundle.partition(name).task_ids) for name in AP_DATASET_PARTITIONS} == {
        "development": 11,
        "validation": 5,
        "held_out": 4,
    }
    assert dataset_access_decision(bundle, role="ordinary_optimizer", partition="development", material="answer", operation="read").allowed
    assert dataset_access_decision(bundle, role="ordinary_optimizer", partition="validation", material="prompt", operation="read").allowed
    assert not dataset_access_decision(bundle, role="ordinary_optimizer", partition="validation", material="answer", operation="read").allowed
    assert not dataset_access_decision(bundle, role="ordinary_optimizer", partition="held_out", material="prompt", operation="read").allowed
    assert not dataset_access_decision(bundle, role="ordinary_optimizer", partition="held_out", material="answer", operation="read").allowed
    assert not dataset_access_decision(bundle, role="ordinary_optimizer", partition="held_out", material="answer", operation="write").allowed
    curator = dataset_access_decision(bundle, role="dataset_curator", partition="held_out", material="answer", operation="write")
    assert curator.allowed and curator.reason == "curator_new_version_only"


def test_optimizer_view_omits_held_out_identity_and_all_answer_material() -> None:
    bundle = load_evaluation_bundle()
    view = ordinary_optimizer_view(bundle)
    exported = json.dumps(view, sort_keys=True)
    for task_id in bundle.partition("held_out").task_ids:
        assert task_id not in exported
    assert view["held_out"] == {
        "count": 4,
        "prompts_exposed": False,
        "answers_exposed": False,
        "identity_commitment": view["held_out"]["identity_commitment"],
    }
    assert view["validation_answers_exposed"] is False
    assert view["dataset_write_allowed"] is False
    assert "optimizer_visible_synthetic_answer" not in exported
    assert "independent_evaluator_only_sealed" not in exported


def test_failure_inventory_exactly_covers_required_fault_and_security_cases() -> None:
    bundle = load_evaluation_bundle()
    assert tuple(item.kind for item in bundle.fixtures) == AP_REQUIRED_FAILURE_KINDS
    assert all(item.expected_outcomes and item.required_evidence for item in bundle.fixtures)


def test_private_trace_scrubber_keeps_only_typed_metrics_and_is_deterministic() -> None:
    trace = _private_trace(
        prompt="Private customer prompt: token=abcdefghijklmnop",
        response="private answer",
        tool_arguments={"path": "/Users/example/private.txt", "api_key": "abcdefgh12345678"},
        environment={"username": "private-person"},
    )
    first = scrub_private_trace(trace)
    second = scrub_private_trace(dict(reversed(tuple(trace.items()))))
    mapping = first.to_mapping()
    exported = json.dumps(mapping, sort_keys=True)
    assert first.digest_sha256 == second.digest_sha256
    assert first.dropped_field_count == 4
    assert first.credential_like_replacements >= 1
    assert mapping["private_content_retained"] is False
    assert mapping["safe_metadata"]["latency_ms"] == 1250.5
    mapping["safe_metadata"]["latency_ms"] = -1
    assert first.safe_metadata["latency_ms"] == 1250.5
    for secret in ("Private customer", "private answer", "/Users/example", "abcdefgh12345678", "private-person"):
        assert secret not in exported


def test_private_trace_scrubber_rejects_missing_or_incomparable_metrics() -> None:
    missing = _private_trace()
    missing.pop("peak_memory_bytes")
    with pytest.raises(EvaluationRegistryError, match="missing required safe metric"):
        scrub_private_trace(missing)
    with pytest.raises(EvaluationRegistryError, match="canonical sha256"):
        scrub_private_trace(_private_trace(task_digest="not-a-digest"))
    with pytest.raises(EvaluationRegistryError, match="cannot be task successes"):
        scrub_private_trace(_private_trace(outcome="cancelled", task_success=True))
    with pytest.raises(EvaluationRegistryError, match="unsupported unbounded value"):
        scrub_private_trace(_private_trace(failure_kinds=["customer-name-as-failure"] ))


def test_unknown_registry_and_nested_fields_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(EvaluationRegistryError, match="unknown field"):
        _mutated_bundle(tmp_path, briefs=lambda value: value.__setitem__("future_default", True))

    def nested(value: dict[str, Any]) -> None:
        value["dataset"]["tasks"][0]["raw_answer"] = "leak"

    with pytest.raises(EvaluationRegistryError, match="unknown field"):
        _mutated_bundle(tmp_path, briefs=nested)


def test_quality_categories_cannot_be_collapsed_or_certified_by_one_grader(tmp_path: Path) -> None:
    def collapse(value: dict[str, Any]) -> None:
        value["quality_model"]["categories"].pop()

    with pytest.raises(EvaluationRegistryError, match="separately and exactly"):
        _mutated_bundle(tmp_path, briefs=collapse)

    def weaken(value: dict[str, Any]) -> None:
        value["quality_model"]["aggregation_policy"]["single_grader_cannot_certify_all_categories"] = False

    with pytest.raises(EvaluationRegistryError, match="safeguard"):
        _mutated_bundle(tmp_path, briefs=weaken)


def test_baseline_and_provisional_calibration_cannot_be_silently_promoted(tmp_path: Path) -> None:
    def multi_agent(value: dict[str, Any]) -> None:
        value["baseline_protocol"]["baseline"]["strategy"] = "multi_agent"

    with pytest.raises(EvaluationRegistryError, match="single-agent"):
        _mutated_bundle(tmp_path, briefs=multi_agent)

    def bind_target(value: dict[str, Any]) -> None:
        value["baseline_protocol"]["acceptance_target"]["binding"] = True

    with pytest.raises(EvaluationRegistryError, match="non-binding and provisional"):
        _mutated_bundle(tmp_path, briefs=bind_target)

    def claim_reliability(value: dict[str, Any]) -> None:
        value["dataset"]["initial_size_plan"]["broad_reliability_claim_allowed"] = True

    with pytest.raises(EvaluationRegistryError, match="broad reliability"):
        _mutated_bundle(tmp_path, briefs=claim_reliability)


def test_held_out_answers_and_mutation_cannot_be_exposed_to_optimizer(tmp_path: Path) -> None:
    def expose_answer(value: dict[str, Any]) -> None:
        value["dataset"]["partitions"]["held_out"]["answer_read_roles"].append("ordinary_optimizer")

    with pytest.raises(EvaluationRegistryError, match="held_out prompts and answers"):
        _mutated_bundle(tmp_path, briefs=expose_answer)

    def expose_prompt(value: dict[str, Any]) -> None:
        value["dataset"]["partitions"]["held_out"]["prompt_read_roles"].append("ordinary_optimizer")

    with pytest.raises(EvaluationRegistryError, match="held_out prompts and answers"):
        _mutated_bundle(tmp_path, briefs=expose_prompt)

    def optimizer_write(value: dict[str, Any]) -> None:
        value["dataset"]["partitions"]["held_out"]["write_roles"] = ["ordinary_optimizer"]

    with pytest.raises(EvaluationRegistryError, match="curator-created new versions"):
        _mutated_bundle(tmp_path, briefs=optimizer_write)

    def embed(value: dict[str, Any]) -> None:
        value["dataset"]["tasks"][-1]["answer_embedded"] = True

    with pytest.raises(EvaluationRegistryError, match="answer-free"):
        _mutated_bundle(tmp_path, briefs=embed)


def test_raw_private_trace_policy_cannot_be_weakened(tmp_path: Path) -> None:
    def raw_allowed(value: dict[str, Any]) -> None:
        value["privacy_policy"]["raw_trace_persistence_allowed"] = True

    with pytest.raises(EvaluationRegistryError, match="forbid raw trace persistence"):
        _mutated_bundle(tmp_path, briefs=raw_allowed)

    def raw_input(value: dict[str, Any]) -> None:
        value["dataset"]["accepted_input_classes"].append("raw_private_trace")

    with pytest.raises(EvaluationRegistryError, match="exclude raw private traces"):
        _mutated_bundle(tmp_path, briefs=raw_input)


def test_fixture_inventory_rejects_missing_cases_or_live_effects(tmp_path: Path) -> None:
    def missing(value: dict[str, Any]) -> None:
        value["fixtures"].pop()

    with pytest.raises(EvaluationRegistryError, match="exactly include"):
        _mutated_bundle(tmp_path, fixtures=missing)

    def network(value: dict[str, Any]) -> None:
        value["fixture_protocol"]["network_allowed"] = True

    with pytest.raises(EvaluationRegistryError, match="deny network"):
        _mutated_bundle(tmp_path, fixtures=network)


def test_roadmap_drift_cannot_activate_runtime_or_falsify_ap003_completion(tmp_path: Path) -> None:
    def activate(value: dict[str, Any]) -> None:
        value["program_registration"]["runtime_activation_authorized"] = True

    with pytest.raises(EvaluationRegistryError, match="runtime activation"):
        _mutated_bundle(tmp_path, roadmap=activate)

    def reopen_ap003(value: dict[str, Any]) -> None:
        next(story for story in value["stories"] if story["id"] == "AP-003")["status"] = "not_started"

    with pytest.raises(EvaluationRegistryError, match="active or truthfully completed"):
        _mutated_bundle(tmp_path, roadmap=reopen_ap003)

    def unmerge_completed_ap003(value: dict[str, Any]) -> None:
        story = next(story for story in value["stories"] if story["id"] == "AP-003")
        story["status"] = "done"
        story["tracking"]["delivery_status"] = "pr_open"

    with pytest.raises(EvaluationRegistryError, match="merged delivery evidence"):
        _mutated_bundle(tmp_path, roadmap=unmerge_completed_ap003)


def test_record_paths_must_not_be_symbolic_links(tmp_path: Path) -> None:
    briefs_link = tmp_path / "briefs.json"
    try:
        briefs_link.symlink_to(DEFAULT_BRIEFS_PATH)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(EvaluationRegistryError, match="symbolic link"):
        load_evaluation_bundle(briefs_link, DEFAULT_FIXTURES_PATH, DEFAULT_ROADMAP_PATH)
