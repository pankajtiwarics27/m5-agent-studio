"""Offline AP-003 evaluation-brief, partition, and failure-fixture boundary.

The module validates repository-owned synthetic records and exposes only an
ordinary-optimizer-safe view. It does not run a model, workflow, optimizer,
provider, filesystem action, or network request. Real private traces must be
reduced to strict metric metadata by :func:`scrub_private_trace` before any
AP evaluation consumer receives them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from capability_negotiation import TaskRequirements
from history_privacy import redact_json
from model_comparison import EvalTask, RubricDimension

AP_EVALUATION_SCHEMA_VERSION = 1
AP_FAILURE_FIXTURE_SCHEMA_VERSION = 1
AP_QUALITY_CATEGORIES = ("technical", "factual", "creative", "practical")
AP_DATASET_PARTITIONS = ("development", "validation", "held_out")
AP_DATASET_ROLES = (
    "ordinary_optimizer",
    "evaluator",
    "independent_evaluator",
    "dataset_curator",
)
AP_REQUIRED_FAILURE_KINDS = (
    "prompt_injection",
    "invalid_artifact",
    "uncertain_write",
    "cancellation",
    "stale_worker",
    "lost_context",
)
AP_RESOURCE_ACCOUNTING_FIELDS = (
    "wall_time_ms",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "retries",
    "repair_attempts",
    "user_intervention_seconds",
    "cost_minor_units",
    "currency",
    "peak_memory_bytes",
)
AP_SCRUBBED_TRACE_FIELDS = (
    "strategy",
    "outcome",
    "task_success",
    "task_digest",
    "config_digest",
    "hardware_digest",
    "latency_ms",
    "cost_minor_units",
    "currency",
    "peak_memory_bytes",
    "input_tokens",
    "output_tokens",
    "tool_calls",
    "retries",
    "repair_attempts",
    "user_intervention_seconds",
    "failure_kinds",
)

DatasetRole = Literal[
    "ordinary_optimizer", "evaluator", "independent_evaluator", "dataset_curator"
]
DatasetPartition = Literal["development", "validation", "held_out"]
DatasetMaterial = Literal["prompt", "answer"]
DatasetOperation = Literal["read", "write"]

ROOT = Path(__file__).resolve().parent
DEFAULT_BRIEFS_PATH = ROOT / "product" / "M5_Autonomous_Production_Evaluation_Briefs.json"
DEFAULT_FIXTURES_PATH = ROOT / "product" / "M5_Autonomous_Production_Failure_Fixtures.json"
DEFAULT_ROADMAP_PATH = ROOT / "product" / "M5_Autonomous_Production_Roadmap.json"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class EvaluationRegistryError(ValueError):
    """Raised when AP evaluation records are ambiguous, stale, or unsafe."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise EvaluationRegistryError(f"{field} must be an object with string keys.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvaluationRegistryError(f"{field} must be an array.")
    return cast(Sequence[object], value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise EvaluationRegistryError(f"{field} is missing explicit field(s): {', '.join(missing)}.")
    if unknown:
        raise EvaluationRegistryError(f"{field} contains unknown field(s): {', '.join(unknown)}.")


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if not isinstance(value, str):
        raise EvaluationRegistryError(f"{field} must be a string.")
    normalized = " ".join("".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value).split())
    if not normalized:
        raise EvaluationRegistryError(f"{field} must not be empty.")
    if len(normalized) > limit:
        raise EvaluationRegistryError(f"{field} must be at most {limit} characters.")
    return normalized


def _identifier(value: object, field: str) -> str:
    identifier = _text(value, field, limit=128)
    if not _ID.fullmatch(identifier):
        raise EvaluationRegistryError(f"{field} must be a bounded opaque identifier.")
    return identifier


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationRegistryError(f"{field} must be an integer >= {minimum}.")
    return value


def _number(value: object, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationRegistryError(f"{field} must be a finite number >= {minimum}.")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise EvaluationRegistryError(f"{field} must be a finite number >= {minimum}.")
    return number


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationRegistryError(f"{field} must be a boolean.")
    return value


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(_text(item, f"{field}[]") for item in _sequence(value, field))
    if not result and not allow_empty:
        raise EvaluationRegistryError(f"{field} must not be empty.")
    if len(result) != len(set(result)):
        raise EvaluationRegistryError(f"{field} must not contain duplicates.")
    return result


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field, limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationRegistryError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise EvaluationRegistryError(f"{field} must include a timezone.")
    return text


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_sha(value: object, field: str) -> str:
    digest = _text(value, field, limit=71)
    if not _DIGEST.fullmatch(digest):
        raise EvaluationRegistryError(f"{field} must be a canonical sha256 digest.")
    return digest


def _load_json(path: Path, field: str) -> Mapping[str, Any]:
    if path.is_symlink():
        raise EvaluationRegistryError(f"{field} must not be a symbolic link.")
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), field)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationRegistryError(f"cannot read {field}: {path}") from exc


@dataclass(frozen=True, slots=True)
class QualityCriterion:
    criterion_id: str
    description: str
    evidence_types: tuple[str, ...]
    hard_gate: bool


@dataclass(frozen=True, slots=True)
class QualityCategory:
    category_id: str
    description: str
    criteria: tuple[QualityCriterion, ...]


@dataclass(frozen=True, slots=True)
class EvaluationBrief:
    brief_id: str
    version: int
    domain: str
    audience: str
    outcome: str
    category_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetTask:
    task_id: str
    task_version: int
    brief_id: str
    partition: DatasetPartition
    case_id: str
    answer_access: str


@dataclass(frozen=True, slots=True)
class PartitionPolicy:
    partition: DatasetPartition
    task_ids: tuple[str, ...]
    prompt_read_roles: tuple[DatasetRole, ...]
    answer_read_roles: tuple[DatasetRole, ...]
    write_roles: tuple[DatasetRole, ...]


@dataclass(frozen=True, slots=True)
class BaselineProtocol:
    protocol_id: str
    version: int
    strategy: str
    agent_count: int
    resource_accounting: tuple[str, ...]
    acceptance_threshold: float
    acceptance_binding: bool


@dataclass(frozen=True, slots=True)
class FailureFixture:
    fixture_id: str
    version: int
    kind: str
    title: str
    expected_outcomes: tuple[str, ...]
    required_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationBundle:
    schema_version: int
    record_version: int
    recorded_at: str
    activation_authorized: bool
    quality_categories: tuple[QualityCategory, ...]
    briefs: tuple[EvaluationBrief, ...]
    tasks: tuple[DatasetTask, ...]
    partitions: tuple[PartitionPolicy, ...]
    baseline: BaselineProtocol
    fixtures: tuple[FailureFixture, ...]
    digest_sha256: str

    def brief(self, brief_id: str) -> EvaluationBrief:
        for item in self.briefs:
            if item.brief_id == brief_id:
                return item
        raise EvaluationRegistryError(f"unknown evaluation brief: {brief_id}.")

    def partition(self, partition: DatasetPartition) -> PartitionPolicy:
        for item in self.partitions:
            if item.partition == partition:
                return item
        raise EvaluationRegistryError(f"unknown dataset partition: {partition}.")


@dataclass(frozen=True, slots=True)
class DatasetAccessDecision:
    allowed: bool
    role: DatasetRole
    partition: DatasetPartition
    material: DatasetMaterial
    operation: DatasetOperation
    reason: str


@dataclass(frozen=True, slots=True)
class ScrubbedTrace:
    """Strict metric-only projection of a private trace; raw content is absent."""

    safe_metadata: Mapping[str, Any]
    dropped_field_count: int
    credential_like_replacements: int
    digest_sha256: str

    def __post_init__(self) -> None:
        frozen = {
            key: tuple(value) if isinstance(value, list) else value
            for key, value in self.safe_metadata.items()
        }
        object.__setattr__(self, "safe_metadata", MappingProxyType(frozen))

    def to_mapping(self) -> dict[str, Any]:
        safe = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.safe_metadata.items()
        }
        return {
            "schema_version": 1,
            "scrubber": "autonomous_production_evaluation.scrub_private_trace/v1",
            "source_class": "real_private_trace",
            "private_content_retained": False,
            "safe_metadata": safe,
            "dropped_field_count": self.dropped_field_count,
            "credential_like_replacements": self.credential_like_replacements,
            "digest_sha256": self.digest_sha256,
        }


def _parse_quality_model(value: object) -> tuple[QualityCategory, ...]:
    data = _object(value, "quality_model")
    _expect_keys(data, {"rubric_id", "version", "scoring_scale", "aggregation_policy", "categories"}, "quality_model")
    if _identifier(data["rubric_id"], "quality_model.rubric_id") != "ap-quality-v1":
        raise EvaluationRegistryError("quality_model.rubric_id must be ap-quality-v1.")
    if _integer(data["version"], "quality_model.version", minimum=1) != 1:
        raise EvaluationRegistryError("quality_model.version must be 1.")
    scoring = _object(data["scoring_scale"], "quality_model.scoring_scale")
    _expect_keys(scoring, {"minimum", "maximum", "labels"}, "quality_model.scoring_scale")
    if _integer(scoring["minimum"], "quality_model.scoring_scale.minimum") != 0 or _integer(
        scoring["maximum"], "quality_model.scoring_scale.maximum"
    ) != 4:
        raise EvaluationRegistryError("quality_model scoring scale must remain 0 through 4.")
    labels = _object(scoring["labels"], "quality_model.scoring_scale.labels")
    if set(labels) != {"0", "1", "2", "3", "4"} or any(not isinstance(item, str) or not item.strip() for item in labels.values()):
        raise EvaluationRegistryError("quality_model.scoring_scale.labels must define non-empty labels 0 through 4.")
    aggregation = _object(data["aggregation_policy"], "quality_model.aggregation_policy")
    expected_aggregation = {
        "category_scores_remain_separate",
        "hard_gates_cannot_be_averaged_away",
        "single_grader_cannot_certify_all_categories",
        "record_failures_and_variability",
    }
    _expect_keys(aggregation, expected_aggregation, "quality_model.aggregation_policy")
    if not all(_boolean(aggregation[key], f"quality_model.aggregation_policy.{key}") for key in expected_aggregation):
        raise EvaluationRegistryError("every quality aggregation safeguard must be true.")

    categories: list[QualityCategory] = []
    for index, raw in enumerate(_sequence(data["categories"], "quality_model.categories")):
        category = _object(raw, f"quality_model.categories[{index}]")
        _expect_keys(category, {"id", "description", "criteria"}, f"quality_model.categories[{index}]")
        category_id = _identifier(category["id"], f"quality_model.categories[{index}].id")
        criteria: list[QualityCriterion] = []
        for criterion_index, criterion_raw in enumerate(_sequence(category["criteria"], f"quality_model.categories[{index}].criteria")):
            field = f"quality_model.categories[{index}].criteria[{criterion_index}]"
            criterion = _object(criterion_raw, field)
            _expect_keys(criterion, {"id", "description", "evidence_types", "hard_gate"}, field)
            criteria.append(
                QualityCriterion(
                    _identifier(criterion["id"], f"{field}.id"),
                    _text(criterion["description"], f"{field}.description"),
                    _strings(criterion["evidence_types"], f"{field}.evidence_types"),
                    _boolean(criterion["hard_gate"], f"{field}.hard_gate"),
                )
            )
        if not criteria:
            raise EvaluationRegistryError(f"quality category {category_id} must define criteria.")
        if len({item.criterion_id for item in criteria}) != len(criteria):
            raise EvaluationRegistryError(f"quality category {category_id} has duplicate criterion IDs.")
        categories.append(QualityCategory(category_id, _text(category["description"], f"quality_model.categories[{index}].description"), tuple(criteria)))
    if tuple(item.category_id for item in categories) != AP_QUALITY_CATEGORIES:
        raise EvaluationRegistryError("quality categories must separately and exactly represent technical, factual, creative, and practical quality.")
    return tuple(categories)


def _parse_briefs(value: object) -> tuple[EvaluationBrief, ...]:
    briefs: list[EvaluationBrief] = []
    for index, raw in enumerate(_sequence(value, "evaluation_briefs")):
        field = f"evaluation_briefs[{index}]"
        item = _object(raw, field)
        _expect_keys(
            item,
            {
                "brief_id",
                "version",
                "domain",
                "status",
                "owner_scope_selected",
                "audience",
                "outcome",
                "rubric_id",
                "required_category_ids",
                "required_artifacts",
                "prohibited_claims",
            },
            field,
        )
        if item["status"] != "synthetic_provisional" or _boolean(item["owner_scope_selected"], f"{field}.owner_scope_selected"):
            raise EvaluationRegistryError(f"{field} must remain synthetic_provisional and not owner-selected.")
        if item["rubric_id"] != "ap-quality-v1":
            raise EvaluationRegistryError(f"{field}.rubric_id must reference ap-quality-v1.")
        categories = _strings(item["required_category_ids"], f"{field}.required_category_ids")
        if categories != AP_QUALITY_CATEGORIES:
            raise EvaluationRegistryError(f"{field} must require all four quality categories separately.")
        _strings(item["required_artifacts"], f"{field}.required_artifacts")
        _strings(item["prohibited_claims"], f"{field}.prohibited_claims")
        briefs.append(
            EvaluationBrief(
                _identifier(item["brief_id"], f"{field}.brief_id"),
                _integer(item["version"], f"{field}.version", minimum=1),
                _identifier(item["domain"], f"{field}.domain"),
                _text(item["audience"], f"{field}.audience"),
                _text(item["outcome"], f"{field}.outcome"),
                categories,
            )
        )
    if {item.domain for item in briefs} != {"research", "website", "short_video"}:
        raise EvaluationRegistryError("evaluation_briefs must contain the three provisional research, website, and short_video briefs.")
    if len({item.brief_id for item in briefs}) != len(briefs):
        raise EvaluationRegistryError("evaluation_briefs must have unique IDs.")
    return tuple(briefs)


def _roles(value: object, field: str) -> tuple[DatasetRole, ...]:
    roles = _strings(value, field)
    if any(role not in AP_DATASET_ROLES for role in roles):
        raise EvaluationRegistryError(f"{field} contains an unsupported dataset role.")
    return cast(tuple[DatasetRole, ...], roles)


def _parse_dataset(value: object, brief_ids: set[str]) -> tuple[tuple[DatasetTask, ...], tuple[PartitionPolicy, ...]]:
    data = _object(value, "dataset")
    _expect_keys(
        data,
        {
            "dataset_id",
            "version",
            "status",
            "synthetic_only",
            "calibration_required",
            "accepted_input_classes",
            "initial_size_plan",
            "partitions",
            "tasks",
            "protected_answer_policy",
        },
        "dataset",
    )
    _identifier(data["dataset_id"], "dataset.dataset_id")
    _integer(data["version"], "dataset.version", minimum=1)
    if data["status"] != "provisional_until_calibrated" or not _boolean(data["synthetic_only"], "dataset.synthetic_only") or not _boolean(
        data["calibration_required"], "dataset.calibration_required"
    ):
        raise EvaluationRegistryError("dataset must remain synthetic, provisional, and calibration-required.")
    if set(_strings(data["accepted_input_classes"], "dataset.accepted_input_classes")) != {"synthetic", "scrubbed_private_trace_metadata"}:
        raise EvaluationRegistryError("dataset accepted_input_classes must exclude raw private traces.")

    size_plan = _object(data["initial_size_plan"], "dataset.initial_size_plan")
    _expect_keys(size_plan, {"research", "website", "short_video", "total", "status", "broad_reliability_claim_allowed"}, "dataset.initial_size_plan")
    counts = {name: _integer(size_plan[name], f"dataset.initial_size_plan.{name}", minimum=1) for name in ("research", "website", "short_video")}
    if counts != {"research": 10, "website": 5, "short_video": 5} or _integer(size_plan["total"], "dataset.initial_size_plan.total") != 20:
        raise EvaluationRegistryError("initial dataset size plan must remain the provisional 10 research, 5 website, and 5 short-video cases.")
    if size_plan["status"] != "provisional_until_baseline_calibration" or _boolean(
        size_plan["broad_reliability_claim_allowed"], "dataset.initial_size_plan.broad_reliability_claim_allowed"
    ):
        raise EvaluationRegistryError("initial dataset sizes must be provisional and must not support broad reliability claims.")

    tasks: list[DatasetTask] = []
    for index, raw in enumerate(_sequence(data["tasks"], "dataset.tasks")):
        field = f"dataset.tasks[{index}]"
        item = _object(raw, field)
        _expect_keys(item, {"task_id", "task_version", "brief_id", "partition", "synthetic", "case_id", "answer_embedded", "answer_access", "immutable_version"}, field)
        partition = item["partition"]
        if partition not in AP_DATASET_PARTITIONS:
            raise EvaluationRegistryError(f"{field}.partition is unsupported.")
        brief_id = _identifier(item["brief_id"], f"{field}.brief_id")
        if brief_id not in brief_ids:
            raise EvaluationRegistryError(f"{field}.brief_id does not reference a registered brief.")
        if not _boolean(item["synthetic"], f"{field}.synthetic") or _boolean(item["answer_embedded"], f"{field}.answer_embedded") or not _boolean(
            item["immutable_version"], f"{field}.immutable_version"
        ):
            raise EvaluationRegistryError(f"{field} must be synthetic, answer-free, and immutable by version.")
        expected_access = {
            "development": "optimizer_visible_synthetic_answer",
            "validation": "evaluator_only",
            "held_out": "independent_evaluator_only_sealed",
        }[cast(str, partition)]
        if item["answer_access"] != expected_access:
            raise EvaluationRegistryError(f"{field}.answer_access violates its partition policy.")
        tasks.append(
            DatasetTask(
                _identifier(item["task_id"], f"{field}.task_id"),
                _integer(item["task_version"], f"{field}.task_version", minimum=1),
                brief_id,
                cast(DatasetPartition, partition),
                _identifier(item["case_id"], f"{field}.case_id"),
                expected_access,
            )
        )
    if len(tasks) != 20 or len({item.task_id for item in tasks}) != len(tasks):
        raise EvaluationRegistryError("dataset must contain exactly 20 uniquely identified provisional tasks.")
    observed_domain_counts = {domain: sum(task.brief_id == f"synthetic-{domain.replace('_', '-')}-brief" for task in tasks) for domain in counts}
    if observed_domain_counts != counts:
        raise EvaluationRegistryError("dataset task counts must match the provisional domain size plan.")

    partitions_data = _object(data["partitions"], "dataset.partitions")
    if set(partitions_data) != set(AP_DATASET_PARTITIONS):
        raise EvaluationRegistryError("dataset.partitions must exactly contain development, validation, and held_out.")
    partitions: list[PartitionPolicy] = []
    tasks_by_id = {item.task_id: item for item in tasks}
    for name in AP_DATASET_PARTITIONS:
        field = f"dataset.partitions.{name}"
        item = _object(partitions_data[name], field)
        _expect_keys(item, {"task_ids", "prompt_read_roles", "answer_read_roles", "write_roles", "mutation_policy"}, field)
        task_ids = _strings(item["task_ids"], f"{field}.task_ids")
        if any(task_id not in tasks_by_id or tasks_by_id[task_id].partition != name for task_id in task_ids):
            raise EvaluationRegistryError(f"{field}.task_ids must exactly reference tasks in that partition.")
        expected_ids = tuple(task.task_id for task in tasks if task.partition == name)
        if task_ids != expected_ids:
            raise EvaluationRegistryError(f"{field}.task_ids must preserve the registered partition membership and order.")
        prompt_roles = _roles(item["prompt_read_roles"], f"{field}.prompt_read_roles")
        answer_roles = _roles(item["answer_read_roles"], f"{field}.answer_read_roles")
        write_roles = _roles(item["write_roles"], f"{field}.write_roles")
        if item["mutation_policy"] != "new_version_only" or write_roles != ("dataset_curator",):
            raise EvaluationRegistryError(f"{field} must permit only curator-created new versions.")
        if name == "held_out" and (
            "ordinary_optimizer" in prompt_roles or "ordinary_optimizer" in answer_roles or set(prompt_roles) != {"independent_evaluator", "dataset_curator"} or set(answer_roles) != {"independent_evaluator", "dataset_curator"}
        ):
            raise EvaluationRegistryError("held_out prompts and answers must be sealed from ordinary optimizers and ordinary evaluators.")
        if name == "validation" and "ordinary_optimizer" in answer_roles:
            raise EvaluationRegistryError("validation answers must be hidden from ordinary optimizers.")
        partitions.append(PartitionPolicy(cast(DatasetPartition, name), task_ids, prompt_roles, answer_roles, write_roles))

    policy = _object(data["protected_answer_policy"], "dataset.protected_answer_policy")
    _expect_keys(
        policy,
        {
            "answers_embedded_in_registry",
            "ordinary_optimizer_read",
            "ordinary_optimizer_write",
            "storage",
            "evaluator_mode",
            "curator_mode",
            "release_to_optimizer",
            "integrity",
        },
        "dataset.protected_answer_policy",
    )
    if _boolean(policy["answers_embedded_in_registry"], "dataset.protected_answer_policy.answers_embedded_in_registry") or _boolean(
        policy["ordinary_optimizer_read"], "dataset.protected_answer_policy.ordinary_optimizer_read"
    ) or _boolean(policy["ordinary_optimizer_write"], "dataset.protected_answer_policy.ordinary_optimizer_write"):
        raise EvaluationRegistryError("protected answers must not be embedded, readable, or writable by ordinary optimizers.")
    if policy["evaluator_mode"] != "read_only_during_scoring" or policy["curator_mode"] != "new_version_only" or policy["release_to_optimizer"] != "never":
        raise EvaluationRegistryError("protected answer access modes must remain read-only scoring, curator new-version-only, and never released to optimizers.")
    _text(policy["storage"], "dataset.protected_answer_policy.storage")
    _text(policy["integrity"], "dataset.protected_answer_policy.integrity")
    return tuple(tasks), tuple(partitions)


def _parse_baseline(value: object) -> BaselineProtocol:
    data = _object(value, "baseline_protocol")
    _expect_keys(
        data,
        {
            "protocol_id",
            "version",
            "status",
            "baseline",
            "candidates_must_include_baseline",
            "matched_inputs",
            "resource_accounting",
            "existing_contract_mapping",
            "repetitions",
            "acceptance_target",
            "hard_gates",
            "reporting",
        },
        "baseline_protocol",
    )
    if data["status"] != "provisional_until_calibrated" or not _boolean(data["candidates_must_include_baseline"], "baseline_protocol.candidates_must_include_baseline"):
        raise EvaluationRegistryError("baseline protocol must remain provisional and must preserve the baseline candidate.")
    baseline = _object(data["baseline"], "baseline_protocol.baseline")
    _expect_keys(baseline, {"strategy", "agent_count", "delegation_allowed", "unchanged_incumbent_required"}, "baseline_protocol.baseline")
    if baseline["strategy"] != "single_agent" or _integer(baseline["agent_count"], "baseline_protocol.baseline.agent_count", minimum=1) != 1:
        raise EvaluationRegistryError("baseline must be a single-agent strategy.")
    if _boolean(baseline["delegation_allowed"], "baseline_protocol.baseline.delegation_allowed") or not _boolean(
        baseline["unchanged_incumbent_required"], "baseline_protocol.baseline.unchanged_incumbent_required"
    ):
        raise EvaluationRegistryError("single-agent baseline must disable delegation and preserve the unchanged incumbent.")
    matched = set(_strings(data["matched_inputs"], "baseline_protocol.matched_inputs"))
    required_matched = {"dataset_id", "dataset_version", "task_id", "task_version", "rubric_id", "rubric_version", "model_runtime_config", "hardware_profile", "resource_budget"}
    if matched != required_matched:
        raise EvaluationRegistryError("baseline matched_inputs must pin dataset, task, rubric, runtime, hardware, and budget identity.")
    resources = _strings(data["resource_accounting"], "baseline_protocol.resource_accounting")
    if resources != AP_RESOURCE_ACCOUNTING_FIELDS:
        raise EvaluationRegistryError("baseline resource_accounting must use the complete comparable AP field set.")
    contract_mapping = _object(data["existing_contract_mapping"], "baseline_protocol.existing_contract_mapping")
    _expect_keys(contract_mapping, {"workflow_evaluation", "execution_budget", "ap_extension_required_from_runner"}, "baseline_protocol.existing_contract_mapping")
    for key in contract_mapping:
        _strings(contract_mapping[key], f"baseline_protocol.existing_contract_mapping.{key}")
    repetitions = _object(data["repetitions"], "baseline_protocol.repetitions")
    _expect_keys(repetitions, {"minimum_per_task_candidate", "report_all_runs", "report_variability", "status"}, "baseline_protocol.repetitions")
    if _integer(repetitions["minimum_per_task_candidate"], "baseline_protocol.repetitions.minimum_per_task_candidate", minimum=2) != 3 or not _boolean(
        repetitions["report_all_runs"], "baseline_protocol.repetitions.report_all_runs"
    ) or not _boolean(repetitions["report_variability"], "baseline_protocol.repetitions.report_variability") or repetitions["status"] != "provisional_until_baseline_calibration":
        raise EvaluationRegistryError("baseline repetitions must remain the provisional three-run, report-all plan.")
    target = _object(data["acceptance_target"], "baseline_protocol.acceptance_target")
    _expect_keys(target, {"metric", "threshold", "binding", "status", "denominator_required"}, "baseline_protocol.acceptance_target")
    threshold = _number(target["threshold"], "baseline_protocol.acceptance_target.threshold")
    if target["metric"] != "accepted_without_user_repair" or threshold != 0.8 or _boolean(target["binding"], "baseline_protocol.acceptance_target.binding") or target[
        "status"
    ] != "provisional_until_calibrated_and_owner_approved" or not _boolean(target["denominator_required"], "baseline_protocol.acceptance_target.denominator_required"):
        raise EvaluationRegistryError("the 80% acceptance target must remain non-binding and provisional until calibration and owner approval.")
    _strings(data["hard_gates"], "baseline_protocol.hard_gates")
    _strings(data["reporting"], "baseline_protocol.reporting")
    return BaselineProtocol(
        _identifier(data["protocol_id"], "baseline_protocol.protocol_id"),
        _integer(data["version"], "baseline_protocol.version", minimum=1),
        "single_agent",
        1,
        resources,
        threshold,
        False,
    )


def _validate_source_and_privacy(value: Mapping[str, Any]) -> None:
    source = _object(value["source_policy"], "source_policy")
    _expect_keys(source, {"requirements_source", "existing_evaluator_contracts", "limitations"}, "source_policy")
    _text(source["requirements_source"], "source_policy.requirements_source")
    contracts = set(_strings(source["existing_evaluator_contracts"], "source_policy.existing_evaluator_contracts"))
    if contracts != {"model_comparison.EvalTask/v1", "workflow_evaluation.WorkflowComparison/v1"}:
        raise EvaluationRegistryError("source_policy must reuse both existing evaluator contracts.")
    _strings(source["limitations"], "source_policy.limitations")
    privacy = _object(value["privacy_policy"], "privacy_policy")
    _expect_keys(privacy, {"synthetic_fixture_default", "real_private_trace_use", "scrubber_contract", "raw_trace_persistence_allowed", "allowed_scrubbed_fields", "dropped_content"}, "privacy_policy")
    if not _boolean(privacy["synthetic_fixture_default"], "privacy_policy.synthetic_fixture_default") or privacy["real_private_trace_use"] != "scrub_before_use" or privacy[
        "scrubber_contract"
    ] != "autonomous_production_evaluation.scrub_private_trace/v1" or _boolean(privacy["raw_trace_persistence_allowed"], "privacy_policy.raw_trace_persistence_allowed"):
        raise EvaluationRegistryError("privacy policy must default to synthetic data, scrub real traces before use, and forbid raw trace persistence.")
    if _strings(privacy["allowed_scrubbed_fields"], "privacy_policy.allowed_scrubbed_fields") != AP_SCRUBBED_TRACE_FIELDS:
        raise EvaluationRegistryError("privacy_policy.allowed_scrubbed_fields must match the strict scrubber projection.")
    _strings(privacy["dropped_content"], "privacy_policy.dropped_content")


def _parse_fixtures(value: Mapping[str, Any]) -> tuple[FailureFixture, ...]:
    _expect_keys(value, {"schema_version", "record_version", "document_type", "recorded_at", "synthetic_only", "activation_authorized", "fixture_protocol", "fixtures"}, "failure_fixture_registry")
    if _integer(value["schema_version"], "failure_fixture_registry.schema_version", minimum=1) != AP_FAILURE_FIXTURE_SCHEMA_VERSION or _integer(
        value["record_version"], "failure_fixture_registry.record_version", minimum=1
    ) != 1:
        raise EvaluationRegistryError("failure fixture registry schema_version and record_version must be 1.")
    if value["document_type"] != "autonomous_production_failure_fixtures":
        raise EvaluationRegistryError("failure fixture registry document_type is unsupported.")
    _timestamp(value["recorded_at"], "failure_fixture_registry.recorded_at")
    if not _boolean(value["synthetic_only"], "failure_fixture_registry.synthetic_only") or _boolean(
        value["activation_authorized"], "failure_fixture_registry.activation_authorized"
    ):
        raise EvaluationRegistryError("failure fixtures must remain synthetic and activation-disabled.")
    protocol = _object(value["fixture_protocol"], "failure_fixture_registry.fixture_protocol")
    _expect_keys(protocol, {"input_class", "network_allowed", "filesystem_side_effects_allowed", "external_accounts_allowed", "expected_result_policy", "held_out_variants"}, "failure_fixture_registry.fixture_protocol")
    if protocol["input_class"] != "synthetic_untrusted_data" or any(
        _boolean(protocol[key], f"failure_fixture_registry.fixture_protocol.{key}")
        for key in ("network_allowed", "filesystem_side_effects_allowed", "external_accounts_allowed")
    ):
        raise EvaluationRegistryError("failure fixture protocol must deny network, filesystem effects, and external accounts.")
    _text(protocol["expected_result_policy"], "failure_fixture_registry.fixture_protocol.expected_result_policy")
    _text(protocol["held_out_variants"], "failure_fixture_registry.fixture_protocol.held_out_variants")
    fixtures: list[FailureFixture] = []
    for index, raw in enumerate(_sequence(value["fixtures"], "failure_fixture_registry.fixtures")):
        field = f"failure_fixture_registry.fixtures[{index}]"
        item = _object(raw, field)
        _expect_keys(item, {"fixture_id", "version", "kind", "title", "injection_point", "stimulus", "expected_outcomes", "required_evidence", "existing_anchors", "candidate_partitions"}, field)
        partitions = _strings(item["candidate_partitions"], f"{field}.candidate_partitions")
        if partitions != AP_DATASET_PARTITIONS:
            raise EvaluationRegistryError(f"{field} must have development, validation, and protected held-out variants.")
        _identifier(item["injection_point"], f"{field}.injection_point")
        _text(item["stimulus"], f"{field}.stimulus")
        _strings(item["existing_anchors"], f"{field}.existing_anchors")
        fixtures.append(
            FailureFixture(
                _identifier(item["fixture_id"], f"{field}.fixture_id"),
                _integer(item["version"], f"{field}.version", minimum=1),
                _identifier(item["kind"], f"{field}.kind"),
                _text(item["title"], f"{field}.title"),
                _strings(item["expected_outcomes"], f"{field}.expected_outcomes"),
                _strings(item["required_evidence"], f"{field}.required_evidence"),
            )
        )
    if tuple(item.kind for item in fixtures) != AP_REQUIRED_FAILURE_KINDS:
        raise EvaluationRegistryError("failure fixture inventory must exactly include injection, invalid artifacts, uncertain writes, cancellation, stale workers, and lost context.")
    if len({item.fixture_id for item in fixtures}) != len(fixtures):
        raise EvaluationRegistryError("failure fixture IDs must be unique.")
    return tuple(fixtures)


def _validate_roadmap(value: Mapping[str, Any]) -> None:
    registration = _object(value.get("program_registration"), "roadmap.program_registration")
    if _boolean(registration.get("runtime_activation_authorized"), "roadmap.program_registration.runtime_activation_authorized"):
        raise EvaluationRegistryError("AP runtime activation must remain false.")
    stories = _sequence(value.get("stories"), "roadmap.stories")
    by_id = {_text(_object(item, "roadmap.stories[]").get("id"), "roadmap.stories[].id"): _object(item, "roadmap.stories[]") for item in stories}
    story = by_id.get("AP-003")
    if story is None or story.get("status") not in {"in_progress", "in_review", "done"}:
        raise EvaluationRegistryError(
            "AP-003 records require their owning story to be active or truthfully completed."
        )
    tracking = _object(story.get("tracking"), "roadmap.AP-003.tracking")
    if tracking.get("owner") != "arena/1c8a9253-m5-agent-studio" or tracking.get("branch") != "arena/1c8a9253-m5-agent-studio":
        raise EvaluationRegistryError("AP-003 tracking must remain bound to its fixed Arena branch.")
    if story.get("status") == "done" and tracking.get("delivery_status") != "merged":
        raise EvaluationRegistryError("Completed AP-003 records require merged delivery evidence.")


def load_evaluation_bundle(
    briefs_path: Path = DEFAULT_BRIEFS_PATH,
    fixtures_path: Path = DEFAULT_FIXTURES_PATH,
    roadmap_path: Path = DEFAULT_ROADMAP_PATH,
) -> EvaluationBundle:
    """Load and strictly validate AP-003 records without activating a runner."""
    registry = _load_json(briefs_path, "evaluation_registry")
    _expect_keys(
        registry,
        {
            "schema_version",
            "record_version",
            "document_type",
            "recorded_at",
            "lifecycle",
            "activation_authorized",
            "owner_scope_selected",
            "source_policy",
            "quality_model",
            "evaluation_briefs",
            "dataset",
            "baseline_protocol",
            "privacy_policy",
        },
        "evaluation_registry",
    )
    if _integer(registry["schema_version"], "evaluation_registry.schema_version", minimum=1) != AP_EVALUATION_SCHEMA_VERSION or _integer(
        registry["record_version"], "evaluation_registry.record_version", minimum=1
    ) != 1:
        raise EvaluationRegistryError("evaluation registry schema_version and record_version must be 1.")
    if registry["document_type"] != "autonomous_production_evaluation_briefs" or registry["lifecycle"] != "provisional_uncalibrated":
        raise EvaluationRegistryError("evaluation registry document_type or lifecycle is unsupported.")
    recorded_at = _timestamp(registry["recorded_at"], "evaluation_registry.recorded_at")
    activation = _boolean(registry["activation_authorized"], "evaluation_registry.activation_authorized")
    if activation or _boolean(registry["owner_scope_selected"], "evaluation_registry.owner_scope_selected"):
        raise EvaluationRegistryError("AP-003 records must not activate runtime or imply an owner-selected scope.")
    _validate_source_and_privacy(registry)
    categories = _parse_quality_model(registry["quality_model"])
    briefs = _parse_briefs(registry["evaluation_briefs"])
    tasks, partitions = _parse_dataset(registry["dataset"], {item.brief_id for item in briefs})
    baseline = _parse_baseline(registry["baseline_protocol"])
    fixtures_record = _load_json(fixtures_path, "failure_fixture_registry")
    fixtures = _parse_fixtures(fixtures_record)
    roadmap = _load_json(roadmap_path, "roadmap")
    _validate_roadmap(roadmap)
    digest = _canonical_digest({"evaluation_registry": registry, "failure_fixture_registry": fixtures_record})
    return EvaluationBundle(
        AP_EVALUATION_SCHEMA_VERSION,
        1,
        recorded_at,
        activation,
        categories,
        briefs,
        tasks,
        partitions,
        baseline,
        fixtures,
        digest,
    )


def dataset_access_decision(
    bundle: EvaluationBundle,
    *,
    role: DatasetRole,
    partition: DatasetPartition,
    material: DatasetMaterial,
    operation: DatasetOperation,
) -> DatasetAccessDecision:
    """Evaluate AP dataset policy; this decision does not issue a real grant."""
    if role not in AP_DATASET_ROLES:
        raise EvaluationRegistryError("unsupported dataset role.")
    if partition not in AP_DATASET_PARTITIONS:
        raise EvaluationRegistryError("unsupported dataset partition.")
    if material not in {"prompt", "answer"}:
        raise EvaluationRegistryError("dataset material must be prompt or answer.")
    if operation not in {"read", "write"}:
        raise EvaluationRegistryError("dataset operation must be read or write.")
    policy = bundle.partition(partition)
    if operation == "write":
        allowed = role in policy.write_roles
        reason = "curator_new_version_only" if allowed else "role_cannot_modify_dataset"
    else:
        roles = policy.prompt_read_roles if material == "prompt" else policy.answer_read_roles
        allowed = role in roles
        reason = "partition_read_allowed" if allowed else "protected_partition_material"
    return DatasetAccessDecision(allowed, role, partition, material, operation, reason)


def ordinary_optimizer_view(bundle: EvaluationBundle) -> dict[str, Any]:
    """Return metadata usable for optimization without held-out material."""
    visible: dict[str, list[str]] = {}
    for partition in ("development", "validation"):
        decision = dataset_access_decision(bundle, role="ordinary_optimizer", partition=partition, material="prompt", operation="read")
        visible[partition] = list(bundle.partition(partition).task_ids) if decision.allowed else []
    held_out = bundle.partition("held_out")
    return {
        "schema_version": bundle.schema_version,
        "record_version": bundle.record_version,
        "quality_categories": [category.category_id for category in bundle.quality_categories],
        "briefs": [{"brief_id": brief.brief_id, "version": brief.version, "domain": brief.domain} for brief in bundle.briefs],
        "visible_prompt_task_ids": visible,
        "validation_answers_exposed": False,
        "held_out": {
            "count": len(held_out.task_ids),
            "prompts_exposed": False,
            "answers_exposed": False,
            "identity_commitment": _canonical_digest({"held_out_task_ids": held_out.task_ids}),
        },
        "dataset_write_allowed": False,
        "baseline": {
            "strategy": bundle.baseline.strategy,
            "agent_count": bundle.baseline.agent_count,
            "resource_accounting": list(bundle.baseline.resource_accounting),
            "acceptance_target_binding": bundle.baseline.acceptance_binding,
        },
    }


def eval_task_for_brief(bundle: EvaluationBundle, brief_id: str) -> EvalTask:
    """Project an AP brief onto the existing pure EvalTask identity contract."""
    brief = bundle.brief(brief_id)
    return EvalTask(
        brief.brief_id,
        brief.version,
        TaskRequirements(),
        tuple(RubricDimension(category_id) for category_id in brief.category_ids),
    )


def scrub_private_trace(trace: Mapping[str, Any]) -> ScrubbedTrace:
    """Drop all free text/private context and retain only strict comparable metrics."""
    source = _object(trace, "private_trace")
    missing = [field for field in AP_SCRUBBED_TRACE_FIELDS if field not in source]
    if missing:
        raise EvaluationRegistryError(f"private_trace is missing required safe metric field(s): {', '.join(missing)}.")
    strategy = source["strategy"]
    outcome = source["outcome"]
    if strategy not in {"single_agent", "multi_agent"}:
        raise EvaluationRegistryError("private_trace.strategy must be single_agent or multi_agent.")
    if outcome not in {"completed", "failed", "cancelled"}:
        raise EvaluationRegistryError("private_trace.outcome must be completed, failed, or cancelled.")
    task_success = _boolean(source["task_success"], "private_trace.task_success")
    if outcome != "completed" and task_success:
        raise EvaluationRegistryError("failed or cancelled private traces cannot be task successes.")
    currency = _text(source["currency"], "private_trace.currency", limit=3)
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise EvaluationRegistryError("private_trace.currency must be a three-letter uppercase code.")
    failure_kinds = _strings(source["failure_kinds"], "private_trace.failure_kinds", allow_empty=True)
    if any(kind not in AP_REQUIRED_FAILURE_KINDS for kind in failure_kinds):
        raise EvaluationRegistryError("private_trace.failure_kinds contains an unsupported unbounded value.")
    safe: dict[str, Any] = {
        "strategy": strategy,
        "outcome": outcome,
        "task_success": task_success,
        "task_digest": _canonical_sha(source["task_digest"], "private_trace.task_digest"),
        "config_digest": _canonical_sha(source["config_digest"], "private_trace.config_digest"),
        "hardware_digest": _canonical_sha(source["hardware_digest"], "private_trace.hardware_digest"),
        "latency_ms": _number(source["latency_ms"], "private_trace.latency_ms"),
        "cost_minor_units": _integer(source["cost_minor_units"], "private_trace.cost_minor_units"),
        "currency": currency,
        "peak_memory_bytes": _integer(source["peak_memory_bytes"], "private_trace.peak_memory_bytes"),
        "input_tokens": _integer(source["input_tokens"], "private_trace.input_tokens"),
        "output_tokens": _integer(source["output_tokens"], "private_trace.output_tokens"),
        "tool_calls": _integer(source["tool_calls"], "private_trace.tool_calls"),
        "retries": _integer(source["retries"], "private_trace.retries"),
        "repair_attempts": _integer(source["repair_attempts"], "private_trace.repair_attempts"),
        "user_intervention_seconds": _number(source["user_intervention_seconds"], "private_trace.user_intervention_seconds"),
        "failure_kinds": list(failure_kinds),
    }
    dropped = {key: value for key, value in source.items() if key not in AP_SCRUBBED_TRACE_FIELDS}
    _, replacements = redact_json(dropped)
    digest = _canonical_digest({"scrubber": 1, "safe_metadata": safe})
    return ScrubbedTrace(safe, len(dropped), replacements, digest)


def _summary(bundle: EvaluationBundle) -> dict[str, Any]:
    return {
        "valid": True,
        "schema_version": bundle.schema_version,
        "record_version": bundle.record_version,
        "recorded_at": bundle.recorded_at,
        "activation_authorized": bundle.activation_authorized,
        "quality_categories": [item.category_id for item in bundle.quality_categories],
        "brief_ids": [item.brief_id for item in bundle.briefs],
        "partition_counts": {partition: len(bundle.partition(cast(DatasetPartition, partition)).task_ids) for partition in AP_DATASET_PARTITIONS},
        "baseline_strategy": bundle.baseline.strategy,
        "acceptance_target": {"threshold": bundle.baseline.acceptance_threshold, "binding": bundle.baseline.acceptance_binding},
        "fixture_kinds": [item.kind for item in bundle.fixtures],
        "digest_sha256": bundle.digest_sha256.removeprefix("sha256:"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--briefs", type=Path, default=DEFAULT_BRIEFS_PATH)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_PATH)
    parser.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP_PATH)
    args = parser.parse_args(argv)
    try:
        bundle = load_evaluation_bundle(args.briefs, args.fixtures, args.roadmap)
    except EvaluationRegistryError as exc:
        parser.error(str(exc))
    print(json.dumps(_summary(bundle), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
