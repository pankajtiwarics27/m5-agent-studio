"""Offline AP operating-envelope validation and fictional policy evaluation.

AP-002 records decisions and examples; it does not activate a provider, runtime,
account, file scope, or promotion path.  This module validates repository-owned
JSON, separates owner statements from measured observations, and evaluates
fictional grants without issuing a Studio ``ScopedGrantRecord`` or performing
I/O beyond reading the requested local files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from hardware_capabilities import HardwareProfile

AP_OPERATING_ENVELOPE_SCHEMA_VERSION = 1
AP_POLICY_EXAMPLES_SCHEMA_VERSION = 1
AP_DECISION_IDS = ("U01", "U02", "U03", "U04", "U05", "U06")
AP_ALLOWED_CAPABILITIES = frozenset(
    {
        "application_api",
        "cli",
        "external_non_inference_media",
        "external_reasoning_model",
        "mcp",
        "public_research",
    }
)
AP_REVIEW_BOUNDARIES = frozenset(
    {"none", "owner", "owner_and_security", "independent_and_owner"}
)
AP_PERSISTENCE_EFFECTS = frozenset(
    {"none", "project", "project_executable", "system_authority"}
)

DecisionState = Literal["pending", "resolved"]
InventoryStatus = Literal["not_measured", "observed_unqualified", "qualified"]
ExternalReasoningState = Literal[
    "denied",
    "separate_explicit_policy_required",
    "explicitly_authorized",
]

ROOT = Path(__file__).resolve().parent
DEFAULT_ENVELOPE_PATH = (
    ROOT / "product" / "M5_Autonomous_Production_Operating_Envelope.json"
)
DEFAULT_ROADMAP_PATH = ROOT / "product" / "M5_Autonomous_Production_Roadmap.json"


class OperatingEnvelopeError(ValueError):
    """Raised when AP decision or policy data is ambiguous or unsafe."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperatingEnvelopeError(f"{field} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise OperatingEnvelopeError(f"{field} keys must be strings.")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OperatingEnvelopeError(f"{field} must be an array.")
    return cast(Sequence[object], value)


def _expect_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise OperatingEnvelopeError(
            f"{field} is missing explicit field(s): {', '.join(missing)}."
        )
    if unknown:
        raise OperatingEnvelopeError(
            f"{field} contains unknown field(s): {', '.join(unknown)}."
        )


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise OperatingEnvelopeError(f"{field} must be a string.")
    result = value.strip()
    if not result and not optional:
        raise OperatingEnvelopeError(f"{field} must not be empty.")
    if "\x00" in result:
        raise OperatingEnvelopeError(f"{field} must not contain NUL characters.")
    return result or None


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise OperatingEnvelopeError(f"{field} must be a boolean.")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise OperatingEnvelopeError(f"{field} must be an integer >= {minimum}.")
    return value


def _strings(
    value: object, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    items = tuple(_text(item, f"{field}[]") for item in _sequence(value, field))
    normalized = tuple(cast(str, item) for item in items)
    if not normalized and not allow_empty:
        raise OperatingEnvelopeError(f"{field} must not be empty.")
    if len(normalized) != len(set(normalized)):
        raise OperatingEnvelopeError(f"{field} must not contain duplicates.")
    return normalized


def _timestamp(value: object, field: str) -> str:
    text = cast(str, _text(value, field))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatingEnvelopeError(f"{field} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise OperatingEnvelopeError(f"{field} must include a timezone.")
    return text


def _relative_scope(value: object, field: str) -> str:
    text = cast(str, _text(value, field))
    path = PurePosixPath(text.replace("\\", "/"))
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise OperatingEnvelopeError(f"{field} must be a confined relative path.")
    return path.as_posix()


def _https_url(value: object, field: str) -> str:
    text = cast(str, _text(value, field))
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OperatingEnvelopeError(
            f"{field} must be an absolute HTTPS URL without credentials or a fragment."
        )
    return text.rstrip("/")


def _currency(value: object, field: str, *, optional: bool = False) -> str | None:
    text = _text(value, field, optional=optional)
    if text is None:
        return None
    if len(text) != 3 or text.upper() != text or not text.isalpha():
        raise OperatingEnvelopeError(
            f"{field} must be a three-letter uppercase currency code."
        )
    return text


def _load_json(path: Path, field: str) -> Mapping[str, Any]:
    if path.is_symlink():
        raise OperatingEnvelopeError(f"{field} must not be a symbolic link.")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatingEnvelopeError(f"cannot read {field}: {path}") from exc
    return _object(value, field)


@dataclass(frozen=True, slots=True)
class OwnerHardwareStatement:
    """A nominal owner statement, never promoted to an observed profile."""

    statement_id: str
    platform: str
    physical_memory_bytes: int
    storage_capacity_bytes: int
    recorded_at: str
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InventoryEvidence:
    """Observed host facts kept separate from the nominal owner statement."""

    status: InventoryStatus
    observed_at: str | None
    collector: str | None
    hardware_profile: HardwareProfile | None
    available_storage_bytes: int | None
    qualified_headroom_bytes: int | None
    qualification_record: str | None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: str
    status: DecisionState
    question: str
    resolution: str | None
    blocked_story_ids: tuple[str, ...]
    blocked_actions: tuple[str, ...]
    safe_work: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromotionClass:
    id: str
    description: str
    automatic_promotion: bool
    review_boundary: str
    persistence_effect: str


@dataclass(frozen=True, slots=True)
class SpendingLimit:
    currency: str | None
    max_minor_units: int


@dataclass(frozen=True, slots=True)
class PolicyGrant:
    grant_id: str
    version: int
    capabilities: tuple[str, ...]
    providers: tuple[str, ...]
    egress_destinations: tuple[str, ...]
    accounts: tuple[str, ...]
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    spending: SpendingLimit
    ttl_seconds: int
    max_uses: int
    data_classes: tuple[str, ...]
    retention_days: int


@dataclass(frozen=True, slots=True)
class PolicyBudget:
    max_elapsed_seconds: int
    max_tokens: int
    max_tool_calls: int
    max_retries: int
    spending: SpendingLimit


@dataclass(frozen=True, slots=True)
class PolicyExample:
    fixture_id: str
    description: str
    policy_version: int
    external_reasoning_state: ExternalReasoningState
    external_reasoning_policy_id: str | None
    grants: tuple[PolicyGrant, ...]
    budget: PolicyBudget


@dataclass(frozen=True, slots=True)
class OperatingEnvelopeBundle:
    schema_version: int
    record_version: int
    recorded_at: str
    runtime_activation_authorized: bool
    owner_hardware: OwnerHardwareStatement
    inventory: InventoryEvidence
    decisions: tuple[DecisionRecord, ...]
    promotion_classes: tuple[PromotionClass, ...]
    policy_examples: tuple[PolicyExample, ...]
    policy_fixture_path: str
    digest_sha256: str

    def decision(self, decision_id: str) -> DecisionRecord:
        for decision in self.decisions:
            if decision.id == decision_id:
                return decision
        raise KeyError(decision_id)

    def policy_example(self, fixture_id: str) -> PolicyExample:
        for example in self.policy_examples:
            if example.fixture_id == fixture_id:
                return example
        raise KeyError(fixture_id)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """One fictional action checked against one example policy."""

    capability: str
    provider: str | None = None
    egress_destination: str | None = None
    account: str | None = None
    read_path: str | None = None
    write_path: str | None = None
    currency: str | None = None
    spend_minor_units: int = 0
    data_classes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability = cast(str, _text(self.capability, "request.capability"))
        if capability not in AP_ALLOWED_CAPABILITIES:
            raise OperatingEnvelopeError(
                f"request.capability is unsupported: {capability}."
            )
        object.__setattr__(self, "capability", capability)
        for name in ("provider", "account"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, f"request.{name}"))
        if self.egress_destination is not None:
            object.__setattr__(
                self,
                "egress_destination",
                _https_url(self.egress_destination, "request.egress_destination"),
            )
        for name in ("read_path", "write_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _relative_scope(value, f"request.{name}")
                )
        object.__setattr__(
            self,
            "spend_minor_units",
            _integer(self.spend_minor_units, "request.spend_minor_units"),
        )
        object.__setattr__(
            self,
            "currency",
            _currency(self.currency, "request.currency", optional=True),
        )
        if self.spend_minor_units and self.currency is None:
            raise OperatingEnvelopeError(
                "request.currency is required when spend_minor_units is non-zero."
            )
        normalized_data = tuple(sorted(set(self.data_classes)))
        if any(
            not isinstance(item, str) or not item.strip() for item in normalized_data
        ):
            raise OperatingEnvelopeError(
                "request.data_classes must contain non-empty strings."
            )
        object.__setattr__(self, "data_classes", normalized_data)


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    allowed: bool
    fixture_id: str
    matched_grant_id: str | None
    reasons: tuple[str, ...]


def _parse_owner_hardware(value: object) -> OwnerHardwareStatement:
    data = _object(value, "owner_hardware_statement")
    _expect_keys(
        data,
        {
            "evidence_level",
            "statement_id",
            "platform",
            "physical_memory_bytes",
            "storage_capacity_bytes",
            "recorded_at",
            "limitations",
        },
        "owner_hardware_statement",
    )
    if data["evidence_level"] != "owner_statement":
        raise OperatingEnvelopeError(
            "owner_hardware_statement.evidence_level must be owner_statement."
        )
    return OwnerHardwareStatement(
        statement_id=cast(
            str, _text(data["statement_id"], "owner_hardware_statement.statement_id")
        ),
        platform=cast(
            str, _text(data["platform"], "owner_hardware_statement.platform")
        ),
        physical_memory_bytes=_integer(
            data["physical_memory_bytes"],
            "owner_hardware_statement.physical_memory_bytes",
            minimum=1,
        ),
        storage_capacity_bytes=_integer(
            data["storage_capacity_bytes"],
            "owner_hardware_statement.storage_capacity_bytes",
            minimum=1,
        ),
        recorded_at=_timestamp(
            data["recorded_at"], "owner_hardware_statement.recorded_at"
        ),
        limitations=_strings(
            data["limitations"], "owner_hardware_statement.limitations"
        ),
    )


def _validate_inventory_recipe(value: object) -> None:
    data = _object(value, "inventory_recipe")
    _expect_keys(
        data,
        {
            "schema_version",
            "collector_mode",
            "hardware_profile_schema",
            "required_observations",
            "measurement_rules",
            "qualification_requirements",
        },
        "inventory_recipe",
    )
    if (
        _integer(data["schema_version"], "inventory_recipe.schema_version", minimum=1)
        != 1
    ):
        raise OperatingEnvelopeError("inventory_recipe.schema_version must be 1.")
    if data["collector_mode"] != "operator_run_local_only":
        raise OperatingEnvelopeError(
            "inventory_recipe.collector_mode must be operator_run_local_only."
        )
    if data["hardware_profile_schema"] != "hardware_capabilities.HardwareProfile/v1":
        raise OperatingEnvelopeError(
            "inventory_recipe.hardware_profile_schema must reference HardwareProfile/v1."
        )
    observations = set(
        _strings(
            data["required_observations"], "inventory_recipe.required_observations"
        )
    )
    required = {
        "architecture",
        "available_memory_bytes",
        "available_storage_bytes",
        "collected_at",
        "collector_version",
        "configuration_fingerprint",
        "installed_runtime_versions",
        "memory_pressure",
        "operating_system",
        "physical_memory_bytes",
        "qualification_record",
        "qualified_headroom_bytes",
    }
    if observations != required:
        missing = sorted(required - observations)
        extra = sorted(observations - required)
        raise OperatingEnvelopeError(
            "inventory_recipe.required_observations must be exact; "
            f"missing={missing}, extra={extra}."
        )
    _strings(data["measurement_rules"], "inventory_recipe.measurement_rules")
    _strings(
        data["qualification_requirements"],
        "inventory_recipe.qualification_requirements",
    )


def _parse_inventory(value: object) -> InventoryEvidence:
    data = _object(value, "measured_inventory")
    _expect_keys(
        data,
        {
            "status",
            "observed_at",
            "collector",
            "hardware_profile",
            "available_storage_bytes",
            "qualified_headroom_bytes",
            "qualification_record",
        },
        "measured_inventory",
    )
    status = data["status"]
    if status not in {"not_measured", "observed_unqualified", "qualified"}:
        raise OperatingEnvelopeError("measured_inventory.status is unsupported.")
    typed_status = cast(InventoryStatus, status)
    detail_fields = (
        "observed_at",
        "collector",
        "hardware_profile",
        "available_storage_bytes",
        "qualified_headroom_bytes",
        "qualification_record",
    )
    if typed_status == "not_measured":
        populated = [field for field in detail_fields if data[field] is not None]
        if populated:
            raise OperatingEnvelopeError(
                "not_measured inventory must keep observation fields null: "
                + ", ".join(populated)
            )
        return InventoryEvidence(typed_status, None, None, None, None, None, None)

    observed_at = _timestamp(data["observed_at"], "measured_inventory.observed_at")
    collector = cast(str, _text(data["collector"], "measured_inventory.collector"))
    profile_data = _object(
        data["hardware_profile"], "measured_inventory.hardware_profile"
    )
    try:
        profile = HardwareProfile.from_mapping(profile_data)
    except (TypeError, ValueError) as exc:
        raise OperatingEnvelopeError(
            f"measured_inventory.hardware_profile is invalid: {exc}"
        ) from exc
    if profile.physical_memory_bytes is None or profile.available_memory_bytes is None:
        raise OperatingEnvelopeError(
            "measured inventory requires physical and available memory observations."
        )
    if any(runtime.version is None for runtime in profile.installed_runtimes):
        raise OperatingEnvelopeError(
            "measured inventory runtime entries require explicit versions."
        )
    storage = _integer(
        data["available_storage_bytes"], "measured_inventory.available_storage_bytes"
    )
    if typed_status == "observed_unqualified":
        if (
            data["qualified_headroom_bytes"] is not None
            or data["qualification_record"] is not None
        ):
            raise OperatingEnvelopeError(
                "observed_unqualified inventory must not claim qualified headroom or a qualification record."
            )
        return InventoryEvidence(
            typed_status, observed_at, collector, profile, storage, None, None
        )

    headroom = _integer(
        data["qualified_headroom_bytes"], "measured_inventory.qualified_headroom_bytes"
    )
    qualification = cast(
        str,
        _text(data["qualification_record"], "measured_inventory.qualification_record"),
    )
    return InventoryEvidence(
        typed_status, observed_at, collector, profile, storage, headroom, qualification
    )


def _parse_decisions(value: object) -> tuple[DecisionRecord, ...]:
    decisions: list[DecisionRecord] = []
    for index, item in enumerate(_sequence(value, "decisions")):
        field = f"decisions[{index}]"
        data = _object(item, field)
        _expect_keys(
            data,
            {
                "id",
                "status",
                "question",
                "resolution",
                "blocked_story_ids",
                "blocked_actions",
                "safe_work",
            },
            field,
        )
        decision_id = cast(str, _text(data["id"], f"{field}.id"))
        status = data["status"]
        if status not in {"pending", "resolved"}:
            raise OperatingEnvelopeError(f"{field}.status must be pending or resolved.")
        resolution = _text(data["resolution"], f"{field}.resolution", optional=True)
        blocked_story_ids = _strings(
            data["blocked_story_ids"],
            f"{field}.blocked_story_ids",
            allow_empty=status == "resolved",
        )
        blocked_actions = _strings(
            data["blocked_actions"],
            f"{field}.blocked_actions",
            allow_empty=status == "resolved",
        )
        if status == "pending" and resolution is not None:
            raise OperatingEnvelopeError(
                f"{field}.resolution must be null while pending."
            )
        if status == "resolved" and resolution is None:
            raise OperatingEnvelopeError(
                f"{field}.resolution is required when resolved."
            )
        if status == "resolved" and (blocked_story_ids or blocked_actions):
            raise OperatingEnvelopeError(
                f"{field} cannot retain blockers after resolution."
            )
        decisions.append(
            DecisionRecord(
                id=decision_id,
                status=cast(DecisionState, status),
                question=cast(str, _text(data["question"], f"{field}.question")),
                resolution=resolution,
                blocked_story_ids=blocked_story_ids,
                blocked_actions=blocked_actions,
                safe_work=_strings(data["safe_work"], f"{field}.safe_work"),
            )
        )
    ids = tuple(decision.id for decision in decisions)
    if ids != AP_DECISION_IDS:
        raise OperatingEnvelopeError(
            f"decisions must appear exactly once in order: {', '.join(AP_DECISION_IDS)}."
        )
    return tuple(decisions)


def _validate_decision_mapping(
    decisions: tuple[DecisionRecord, ...], roadmap: Mapping[str, Any]
) -> None:
    open_decisions = _object(roadmap.get("open_decisions"), "roadmap.open_decisions")
    stories = _sequence(roadmap.get("stories"), "roadmap.stories")
    expected_questions = {
        key: value for key, value in open_decisions.items() if key in AP_DECISION_IDS
    }
    if set(expected_questions) != set(AP_DECISION_IDS):
        raise OperatingEnvelopeError("roadmap.open_decisions must define U01-U06.")
    blocked: dict[str, list[str]] = {decision_id: [] for decision_id in AP_DECISION_IDS}
    for index, story_value in enumerate(stories):
        story = _object(story_value, f"roadmap.stories[{index}]")
        story_id = cast(str, _text(story.get("id"), f"roadmap.stories[{index}].id"))
        decision_ids = _strings(
            story.get("blocked_by_decisions", []),
            f"roadmap.stories[{index}].blocked_by_decisions",
            allow_empty=True,
        )
        for decision_id in decision_ids:
            if decision_id in blocked:
                blocked[decision_id].append(story_id)
    for decision in decisions:
        if decision.question != expected_questions[decision.id]:
            raise OperatingEnvelopeError(
                f"{decision.id}.question drifted from the AP roadmap."
            )
        expected_story_ids = (
            tuple(blocked[decision.id]) if decision.status == "pending" else ()
        )
        if decision.blocked_story_ids != expected_story_ids:
            raise OperatingEnvelopeError(
                f"{decision.id}.blocked_story_ids must match the AP roadmap exactly; "
                f"expected {list(expected_story_ids)}."
            )


def _parse_promotion_policy(
    value: object, decisions: tuple[DecisionRecord, ...]
) -> tuple[PromotionClass, ...]:
    data = _object(value, "promotion_policy")
    _expect_keys(
        data,
        {
            "policy_version",
            "decision_id",
            "decision_status",
            "default_effect",
            "classes",
        },
        "promotion_policy",
    )
    _integer(data["policy_version"], "promotion_policy.policy_version", minimum=1)
    if data["decision_id"] != "U05":
        raise OperatingEnvelopeError("promotion_policy.decision_id must be U05.")
    u05 = next(decision for decision in decisions if decision.id == "U05")
    if data["decision_status"] != u05.status:
        raise OperatingEnvelopeError("promotion_policy.decision_status must match U05.")
    if data["default_effect"] != "deny_promotion":
        raise OperatingEnvelopeError(
            "promotion_policy.default_effect must be deny_promotion."
        )
    classes: list[PromotionClass] = []
    for index, item in enumerate(
        _sequence(data["classes"], "promotion_policy.classes")
    ):
        field = f"promotion_policy.classes[{index}]"
        item_data = _object(item, field)
        _expect_keys(
            item_data,
            {
                "id",
                "description",
                "automatic_promotion",
                "review_boundary",
                "persistence_effect",
            },
            field,
        )
        review = cast(
            str, _text(item_data["review_boundary"], f"{field}.review_boundary")
        )
        persistence = cast(
            str, _text(item_data["persistence_effect"], f"{field}.persistence_effect")
        )
        if review not in AP_REVIEW_BOUNDARIES:
            raise OperatingEnvelopeError(f"{field}.review_boundary is unsupported.")
        if persistence not in AP_PERSISTENCE_EFFECTS:
            raise OperatingEnvelopeError(f"{field}.persistence_effect is unsupported.")
        automatic = _boolean(
            item_data["automatic_promotion"], f"{field}.automatic_promotion"
        )
        if u05.status == "pending" and automatic:
            raise OperatingEnvelopeError(
                "U05 is pending; automatic promotion cannot be enabled."
            )
        if persistence != "none" and review == "none":
            raise OperatingEnvelopeError(
                f"{field} has a persistent effect and requires a review boundary."
            )
        classes.append(
            PromotionClass(
                id=cast(str, _text(item_data["id"], f"{field}.id")),
                description=cast(
                    str, _text(item_data["description"], f"{field}.description")
                ),
                automatic_promotion=automatic,
                review_boundary=review,
                persistence_effect=persistence,
            )
        )
    if not classes or len({item.id for item in classes}) != len(classes):
        raise OperatingEnvelopeError(
            "promotion_policy.classes must be non-empty with unique ids."
        )
    return tuple(classes)


def _parse_spending(value: object, field: str) -> SpendingLimit:
    data = _object(value, field)
    _expect_keys(data, {"currency", "max_minor_units"}, field)
    limit = _integer(data["max_minor_units"], f"{field}.max_minor_units")
    currency = _currency(data["currency"], f"{field}.currency", optional=True)
    if (limit == 0) != (currency is None):
        raise OperatingEnvelopeError(
            f"{field} must use currency=null exactly when max_minor_units is zero."
        )
    return SpendingLimit(currency, limit)


def _parse_grant(value: object, field: str) -> PolicyGrant:
    data = _object(value, field)
    _expect_keys(
        data,
        {
            "grant_id",
            "version",
            "capabilities",
            "providers",
            "egress_destinations",
            "accounts",
            "read_roots",
            "write_roots",
            "spending",
            "ttl_seconds",
            "max_uses",
            "data_classes",
            "retention_days",
        },
        field,
    )
    capabilities = _strings(data["capabilities"], f"{field}.capabilities")
    unknown = sorted(set(capabilities) - AP_ALLOWED_CAPABILITIES)
    if unknown:
        raise OperatingEnvelopeError(
            f"{field}.capabilities contains unsupported values: {', '.join(unknown)}."
        )
    return PolicyGrant(
        grant_id=cast(str, _text(data["grant_id"], f"{field}.grant_id")),
        version=_integer(data["version"], f"{field}.version", minimum=1),
        capabilities=capabilities,
        providers=_strings(data["providers"], f"{field}.providers", allow_empty=True),
        egress_destinations=tuple(
            _https_url(item, f"{field}.egress_destinations[]")
            for item in _sequence(
                data["egress_destinations"], f"{field}.egress_destinations"
            )
        ),
        accounts=_strings(data["accounts"], f"{field}.accounts", allow_empty=True),
        read_roots=tuple(
            _relative_scope(item, f"{field}.read_roots[]")
            for item in _sequence(data["read_roots"], f"{field}.read_roots")
        ),
        write_roots=tuple(
            _relative_scope(item, f"{field}.write_roots[]")
            for item in _sequence(data["write_roots"], f"{field}.write_roots")
        ),
        spending=_parse_spending(data["spending"], f"{field}.spending"),
        ttl_seconds=_integer(data["ttl_seconds"], f"{field}.ttl_seconds", minimum=1),
        max_uses=_integer(data["max_uses"], f"{field}.max_uses", minimum=1),
        data_classes=_strings(
            data["data_classes"], f"{field}.data_classes", allow_empty=True
        ),
        retention_days=_integer(data["retention_days"], f"{field}.retention_days"),
    )


def _parse_policy_examples(value: Mapping[str, Any]) -> tuple[PolicyExample, ...]:
    _expect_keys(
        value,
        {"schema_version", "document_type", "fictional_only", "examples"},
        "policy_examples",
    )
    if value["schema_version"] != AP_POLICY_EXAMPLES_SCHEMA_VERSION:
        raise OperatingEnvelopeError(
            f"policy_examples.schema_version must be {AP_POLICY_EXAMPLES_SCHEMA_VERSION}."
        )
    if value["document_type"] != "autonomous_production_fictional_policy_examples":
        raise OperatingEnvelopeError("policy_examples.document_type is unsupported.")
    if value["fictional_only"] is not True:
        raise OperatingEnvelopeError("policy_examples.fictional_only must be true.")
    examples: list[PolicyExample] = []
    for index, item in enumerate(
        _sequence(value["examples"], "policy_examples.examples")
    ):
        field = f"policy_examples.examples[{index}]"
        data = _object(item, field)
        _expect_keys(
            data,
            {
                "fixture_id",
                "description",
                "policy_version",
                "fictional",
                "external_reasoning_models",
                "grants",
                "budget",
            },
            field,
        )
        if data["fictional"] is not True:
            raise OperatingEnvelopeError(f"{field}.fictional must be true.")
        reasoning = _object(
            data["external_reasoning_models"], f"{field}.external_reasoning_models"
        )
        _expect_keys(
            reasoning, {"state", "policy_id"}, f"{field}.external_reasoning_models"
        )
        reasoning_state = reasoning["state"]
        if reasoning_state not in {
            "denied",
            "separate_explicit_policy_required",
            "explicitly_authorized",
        }:
            raise OperatingEnvelopeError(
                f"{field}.external_reasoning_models.state is unsupported."
            )
        policy_id = _text(
            reasoning["policy_id"],
            f"{field}.external_reasoning_models.policy_id",
            optional=True,
        )
        if (reasoning_state == "explicitly_authorized") != (policy_id is not None):
            raise OperatingEnvelopeError(
                f"{field}.external_reasoning_models.policy_id is required only for explicit authorization."
            )
        grants = tuple(
            _parse_grant(grant, f"{field}.grants[{grant_index}]")
            for grant_index, grant in enumerate(
                _sequence(data["grants"], f"{field}.grants")
            )
        )
        if len({grant.grant_id for grant in grants}) != len(grants):
            raise OperatingEnvelopeError(
                f"{field}.grants contains duplicate grant ids."
            )
        if reasoning_state != "explicitly_authorized" and any(
            "external_reasoning_model" in grant.capabilities for grant in grants
        ):
            raise OperatingEnvelopeError(
                f"{field} cannot grant external_reasoning_model without a separate explicit policy."
            )
        budget_data = _object(data["budget"], f"{field}.budget")
        _expect_keys(
            budget_data,
            {
                "max_elapsed_seconds",
                "max_tokens",
                "max_tool_calls",
                "max_retries",
                "spending",
            },
            f"{field}.budget",
        )
        budget = PolicyBudget(
            max_elapsed_seconds=_integer(
                budget_data["max_elapsed_seconds"],
                f"{field}.budget.max_elapsed_seconds",
                minimum=1,
            ),
            max_tokens=_integer(
                budget_data["max_tokens"], f"{field}.budget.max_tokens"
            ),
            max_tool_calls=_integer(
                budget_data["max_tool_calls"], f"{field}.budget.max_tool_calls"
            ),
            max_retries=_integer(
                budget_data["max_retries"], f"{field}.budget.max_retries"
            ),
            spending=_parse_spending(
                budget_data["spending"], f"{field}.budget.spending"
            ),
        )
        for grant in grants:
            if grant.spending.max_minor_units > budget.spending.max_minor_units:
                raise OperatingEnvelopeError(
                    f"{grant.grant_id} spending exceeds its fixture budget."
                )
            if (
                grant.spending.currency != budget.spending.currency
                and grant.spending.max_minor_units
            ):
                raise OperatingEnvelopeError(
                    f"{grant.grant_id} currency differs from its fixture budget."
                )
        examples.append(
            PolicyExample(
                fixture_id=cast(str, _text(data["fixture_id"], f"{field}.fixture_id")),
                description=cast(
                    str, _text(data["description"], f"{field}.description")
                ),
                policy_version=_integer(
                    data["policy_version"], f"{field}.policy_version", minimum=1
                ),
                external_reasoning_state=cast(ExternalReasoningState, reasoning_state),
                external_reasoning_policy_id=policy_id,
                grants=grants,
                budget=budget,
            )
        )
    if not examples or len({item.fixture_id for item in examples}) != len(examples):
        raise OperatingEnvelopeError(
            "policy_examples.examples must be non-empty with unique ids."
        )
    return tuple(examples)


def _scope_contains(root: str, target: str) -> bool:
    root_path = PurePosixPath(root)
    target_path = PurePosixPath(target)
    return target_path == root_path or root_path in target_path.parents


def _grant_denials(grant: PolicyGrant, request: ActionRequest) -> tuple[str, ...]:
    reasons: list[str] = []
    if request.capability not in grant.capabilities:
        reasons.append("capability_not_granted")
    if grant.providers and request.provider is None:
        reasons.append("provider_scope_missing")
    elif request.provider is not None and request.provider not in grant.providers:
        reasons.append("provider_not_granted")
    if grant.egress_destinations and request.egress_destination is None:
        reasons.append("egress_destination_scope_missing")
    elif (
        request.egress_destination is not None
        and request.egress_destination not in grant.egress_destinations
    ):
        reasons.append("egress_destination_not_granted")
    if grant.accounts and request.account is None:
        reasons.append("account_scope_missing")
    elif request.account is not None and request.account not in grant.accounts:
        reasons.append("account_not_granted")
    if grant.read_roots and request.read_path is None:
        reasons.append("read_path_scope_missing")
    elif request.read_path is not None and not any(
        _scope_contains(root, request.read_path) for root in grant.read_roots
    ):
        reasons.append("read_path_not_granted")
    if grant.write_roots and request.write_path is None:
        reasons.append("write_path_scope_missing")
    elif request.write_path is not None and not any(
        _scope_contains(root, request.write_path) for root in grant.write_roots
    ):
        reasons.append("write_path_not_granted")
    if grant.spending.max_minor_units and request.currency is None:
        reasons.append("spending_scope_missing")
    elif request.currency is not None and request.currency != grant.spending.currency:
        reasons.append("spending_currency_not_granted")
    if request.spend_minor_units > grant.spending.max_minor_units:
        reasons.append("spending_limit_exceeded")
    if grant.data_classes and not request.data_classes:
        reasons.append("data_class_scope_missing")
    elif not set(request.data_classes).issubset(grant.data_classes):
        reasons.append("data_class_not_granted")
    return tuple(reasons)


def evaluate_policy(example: PolicyExample, request: ActionRequest) -> PolicyVerdict:
    """Evaluate one fictional request; never issue or consume a real grant."""

    if (
        request.capability == "external_reasoning_model"
        and example.external_reasoning_state != "explicitly_authorized"
    ):
        return PolicyVerdict(
            False,
            example.fixture_id,
            None,
            ("external_reasoning_requires_separate_explicit_policy",),
        )
    if request.spend_minor_units:
        if request.currency != example.budget.spending.currency:
            return PolicyVerdict(
                False, example.fixture_id, None, ("budget_currency_mismatch",)
            )
        if request.spend_minor_units > example.budget.spending.max_minor_units:
            return PolicyVerdict(
                False, example.fixture_id, None, ("fixture_spending_budget_exceeded",)
            )
    if not example.grants:
        return PolicyVerdict(False, example.fixture_id, None, ("no_explicit_grants",))
    denials: set[str] = set()
    for grant in example.grants:
        reasons = _grant_denials(grant, request)
        if not reasons:
            return PolicyVerdict(True, example.fixture_id, grant.grant_id, ())
        denials.update(reasons)
    return PolicyVerdict(False, example.fixture_id, None, tuple(sorted(denials)))


def load_operating_envelope(
    envelope_path: Path = DEFAULT_ENVELOPE_PATH,
    *,
    roadmap_path: Path = DEFAULT_ROADMAP_PATH,
    policy_examples_path: Path | None = None,
) -> OperatingEnvelopeBundle:
    """Load and cross-check the AP-002 register, roadmap, and fictional examples."""

    envelope = _load_json(envelope_path, "operating envelope")
    _expect_keys(
        envelope,
        {
            "schema_version",
            "record_version",
            "document_type",
            "program",
            "story_id",
            "recorded_at",
            "runtime_activation_authorized",
            "owner_hardware_statement",
            "inventory_recipe",
            "measured_inventory",
            "decisions",
            "promotion_policy",
            "policy_fixture_path",
            "notes",
        },
        "operating envelope",
    )
    if envelope["schema_version"] != AP_OPERATING_ENVELOPE_SCHEMA_VERSION:
        raise OperatingEnvelopeError(
            f"operating envelope schema_version must be {AP_OPERATING_ENVELOPE_SCHEMA_VERSION}."
        )
    record_version = _integer(envelope["record_version"], "record_version", minimum=1)
    if envelope["document_type"] != "autonomous_production_operating_envelope":
        raise OperatingEnvelopeError("operating envelope document_type is unsupported.")
    if envelope["program"] != "AP" or envelope["story_id"] != "AP-002":
        raise OperatingEnvelopeError("operating envelope must belong to AP-002.")
    runtime_activation = _boolean(
        envelope["runtime_activation_authorized"], "runtime_activation_authorized"
    )
    if runtime_activation:
        raise OperatingEnvelopeError(
            "AP-002 cannot authorize product runtime activation."
        )
    owner_hardware = _parse_owner_hardware(envelope["owner_hardware_statement"])
    _validate_inventory_recipe(envelope["inventory_recipe"])
    inventory = _parse_inventory(envelope["measured_inventory"])
    decisions = _parse_decisions(envelope["decisions"])
    roadmap = _load_json(roadmap_path, "AP roadmap")
    _validate_decision_mapping(decisions, roadmap)
    promotion_classes = _parse_promotion_policy(envelope["promotion_policy"], decisions)
    fixture_ref = _relative_scope(
        envelope["policy_fixture_path"], "policy_fixture_path"
    )
    fixture_path = policy_examples_path or (ROOT / fixture_ref)
    policy_mapping = _load_json(fixture_path, "policy examples")
    policy_examples = _parse_policy_examples(policy_mapping)
    _strings(envelope["notes"], "notes")
    digest_payload = {
        "envelope": envelope,
        "policy_examples": policy_mapping,
        "roadmap_decisions": roadmap.get("open_decisions"),
        "roadmap_story_blockers": [
            {
                "id": story.get("id"),
                "blocked_by_decisions": story.get("blocked_by_decisions"),
            }
            for story in cast(Sequence[Mapping[str, Any]], roadmap.get("stories", []))
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return OperatingEnvelopeBundle(
        schema_version=AP_OPERATING_ENVELOPE_SCHEMA_VERSION,
        record_version=record_version,
        recorded_at=_timestamp(envelope["recorded_at"], "recorded_at"),
        runtime_activation_authorized=runtime_activation,
        owner_hardware=owner_hardware,
        inventory=inventory,
        decisions=decisions,
        promotion_classes=promotion_classes,
        policy_examples=policy_examples,
        policy_fixture_path=fixture_ref,
        digest_sha256=digest,
    )


def _summary(bundle: OperatingEnvelopeBundle) -> dict[str, object]:
    return {
        "schema_version": bundle.schema_version,
        "record_version": bundle.record_version,
        "recorded_at": bundle.recorded_at,
        "runtime_activation_authorized": bundle.runtime_activation_authorized,
        "inventory_status": bundle.inventory.status,
        "decision_states": {item.id: item.status for item in bundle.decisions},
        "blocked_story_counts": {
            item.id: len(item.blocked_story_ids) for item in bundle.decisions
        },
        "policy_fixtures": [item.fixture_id for item in bundle.policy_examples],
        "automatic_promotion_classes": [
            item.id for item in bundle.promotion_classes if item.automatic_promotion
        ],
        "digest_sha256": bundle.digest_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", type=Path, default=DEFAULT_ENVELOPE_PATH)
    parser.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP_PATH)
    parser.add_argument("--policy-examples", type=Path)
    args = parser.parse_args(argv)
    try:
        bundle = load_operating_envelope(
            args.envelope,
            roadmap_path=args.roadmap,
            policy_examples_path=args.policy_examples,
        )
    except OperatingEnvelopeError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"valid": True, **_summary(bundle)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
