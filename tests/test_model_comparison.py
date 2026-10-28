from __future__ import annotations

import json

import pytest

from capability_negotiation import TaskRequirements
from hardware_capabilities import HardwareProfile
from model_comparison import (
    DimensionScore,
    EvalConfig,
    EvalScore,
    EvalTask,
    ModelComparisonError,
    RubricDimension,
    compare_evaluations,
    score_evaluation,
)
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance


def _descriptor(
    model_id: str,
    *,
    context_tokens: int = 4096,
    capabilities: tuple[str, ...] = ("chat", "streaming"),
    supports_tools: bool = False,
    memory_bytes: int | None = None,
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=f"sha256:{model_id}",
        source="fixture",
        revision="rev",
        runtime="fixture",
        quantization="Q4_K_M",
        capabilities=capabilities,
        supports_tools=supports_tools,
        supports_streaming=True,
        limits=ModelLimits(context_tokens=context_tokens, max_output_tokens=256, memory_bytes=memory_bytes),
        provenance=ModelProvenance("fixture", model_id, "unverified"),
    )


def _task(**overrides: object) -> EvalTask:
    values: dict[str, object] = {
        "task_id": "summarize-news",
        "task_version": 3,
        "requirements": TaskRequirements(),
        "rubric": (RubricDimension("accuracy", 2.0), RubricDimension("fluency", 1.0)),
    }
    values.update(overrides)
    return EvalTask(**values)  # type: ignore[arg-type]


def _config(**overrides: object) -> EvalConfig:
    values: dict[str, object] = {"max_output_tokens": 256}
    values.update(overrides)
    return EvalConfig(**values)  # type: ignore[arg-type]


def _score(model: ModelDescriptor, *, values: dict[str, tuple[float, ...]] | None = None, **config_kwargs: object) -> EvalScore:
    task = _task()
    config = _config(**config_kwargs)  # type: ignore[arg-type]
    dimensions = values or {"accuracy": (0.9, 0.8, 0.85), "fluency": (0.6, 0.7)}
    return score_evaluation(model, task, config, dimensions)


def test_versioned_task_carries_digest_and_sorted_rubric() -> None:
    task = _task()
    mapping = task.to_mapping()
    assert mapping["task_id"] == "summarize-news"
    assert mapping["task_version"] == 3
    assert mapping["requirements"]["minimum_context_tokens"] == 1
    assert [item["name"] for item in mapping["rubric"]] == ["accuracy", "fluency"]  # sorted, not declaration order
    assert task.digest.startswith("sha256:")

    # Declaration order is normalized: same content, different order -> identical task.
    same = EvalTask("summarize-news", 3, TaskRequirements(), (RubricDimension("fluency", 1.0), RubricDimension("accuracy", 2.0)))
    assert same.digest == task.digest

    # Weights belong to their named dimension; swapping them is a different task.
    swapped = EvalTask("summarize-news", 3, TaskRequirements(), (RubricDimension("accuracy", 1.0), RubricDimension("fluency", 2.0)))
    assert swapped.digest != task.digest

    # A bumped version is a different task identity.
    bumped = EvalTask("summarize-news", 4, TaskRequirements(), task.rubric)
    assert bumped.digest != task.digest


def test_task_validation_is_fail_closed() -> None:
    with pytest.raises(ModelComparisonError, match="task_version must be a positive integer"):
        _task(task_version=0)
    with pytest.raises(ModelComparisonError, match="task_id must not be empty"):
        _task(task_id="  ")
    with pytest.raises(ModelComparisonError, match="at least one dimension"):
        _task(rubric=())
    with pytest.raises(ModelComparisonError, match="unique"):
        _task(rubric=(RubricDimension("accuracy"), RubricDimension("accuracy")))
    with pytest.raises(ModelComparisonError, match="weight must be a positive finite number"):
        _task(rubric=(RubricDimension("accuracy", 0.0),))
    with pytest.raises(TypeError, match="TaskRequirements"):
        _task(requirements={"requires_tools": True})
    with pytest.raises(ModelComparisonError, match="control characters"):
        _task(task_id="bad\x00id")


def test_config_digest_covers_knobs_not_environment() -> None:
    base = _config(temperature=0.2, seed=7)
    same_elsewhere = _config(temperature=0.2, seed=7, environment={"machine": "mbp-16"})
    assert base.digest == same_elsewhere.digest  # environment is disclosed, not hashed
    assert same_elsewhere.to_mapping()["environment"] == {"machine": "mbp-16"}
    assert base.to_mapping()["environment"] == {}

    hotter = _config(temperature=0.3, seed=7)
    assert hotter.digest != base.digest
    assert base.digest == _config(temperature=0.2, seed=7).digest  # deterministic across constructions


def test_config_validation_is_fail_closed() -> None:
    with pytest.raises(ModelComparisonError, match="temperature must be within"):
        _config(temperature=-0.1)
    with pytest.raises(ModelComparisonError, match="temperature must be within"):
        _config(temperature=float("nan"))
    with pytest.raises(ModelComparisonError, match="temperature must be within"):
        _config(temperature=2.5)
    with pytest.raises(ModelComparisonError, match="max_output_tokens must be a positive integer"):
        _config(max_output_tokens=0)
    with pytest.raises(ModelComparisonError, match="seed must be a non-negative integer"):
        _config(seed=True)
    with pytest.raises(ModelComparisonError, match="unique after normalization"):
        _config(environment=(("os", "a"), ("os ", "b")))
    with pytest.raises(TypeError, match="object or a sequence"):
        _config(environment=42)  # type: ignore[arg-type]


def test_score_evaluation_aggregates_median_spread_and_overall() -> None:
    score = _score(_descriptor("model-a:latest"))
    by_name = {item.name: item for item in score.dimensions}
    assert by_name["accuracy"].median == 0.85 and by_name["accuracy"].spread == 0.1
    assert by_name["fluency"].median == pytest.approx(0.65) and by_name["fluency"].spread == pytest.approx(0.1)
    # accuracy (weight 2) and fluency (weight 1) medians: (2*0.85 + 1*0.65) / 3
    assert score.overall == pytest.approx(0.783333)
    assert score.compatibility == ()
    assert score.inputs_digest is None

    mapping = score.to_mapping()
    assert mapping["task_digest"] == score.task.digest
    assert mapping["config_digest"] == score.config.digest
    assert mapping["model"]["model_id"] == "model-a:latest"
    assert mapping["overall"] == score.overall
    assert json.dumps(mapping)  # JSON-safe
    assert score.digest == _score(_descriptor("model-a:latest")).digest  # identical inputs -> identical record


def test_score_evaluation_rejects_dimension_mismatches_and_bad_values() -> None:
    model = _descriptor("model-a:latest")
    with pytest.raises(ModelComparisonError, match="not in the task rubric"):
        score_evaluation(model, _task(), _config(), {"accuracy": (0.5,), "typo": (0.5,)})
    with pytest.raises(ModelComparisonError, match="missing measured dimension"):
        score_evaluation(model, _task(), _config(), {"fluency": (0.5,)})
    with pytest.raises(ModelComparisonError, match="at least one measured run value"):
        score_evaluation(model, _task(), _config(), {"accuracy": (), "fluency": (0.5,)})
    with pytest.raises(ModelComparisonError, match=r"\[0, 1\]"):
        score_evaluation(model, _task(), _config(), {"accuracy": (1.5,), "fluency": (0.5,)})
    with pytest.raises(ModelComparisonError, match="sequence of measured run values"):
        score_evaluation(model, _task(), _config(), {"accuracy": 0.5, "fluency": (0.5,)})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="object of dimension name"):
        score_evaluation(model, _task(), _config(), ["accuracy"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="EvalTask"):
        score_evaluation(model, "summarize-news@3", _config(), {})  # type: ignore[arg-type]


def test_dimension_score_derives_aggregates_only_from_runs() -> None:
    honest = DimensionScore("accuracy", (0.4, 0.8, 0.6))
    assert honest.median == 0.6 and honest.spread == 0.4

    # Median/spread are re-derived from runs, never accepted as truth.
    dishonest = DimensionScore("accuracy", (0.4, 0.8, 0.6), median=1.0, spread=0.0)
    assert dishonest.median == 0.6 and dishonest.spread == 0.4
    assert dishonest.to_mapping() == honest.to_mapping()


def test_incompatible_models_still_score_with_recorded_reasons() -> None:
    toolless = _descriptor("no-tools:latest")
    task = _task(requirements=TaskRequirements(requires_tools=True))
    score = score_evaluation(toolless, task, _config(), {"accuracy": (0.9,), "fluency": (0.9,)})
    assert score.overall == 0.9
    assert score.compatibility == ("model does not support tool calls",)

    # The score digest pins model identity too: same task/config on another model differs.
    other = score_evaluation(_descriptor("no-tools:other", capabilities=("chat",)), task, _config(), {"accuracy": (0.9,), "fluency": (0.9,)})
    assert other.digest != score.digest
    assert other.compatibility == score.compatibility


def test_manual_evalscore_consistency_checks() -> None:
    task = _task()
    good = _score(_descriptor("model-a:latest"))
    with pytest.raises(ModelComparisonError, match="match the task rubric"):
        EvalScore(good.model, task, good.config, (DimensionScore("fluency", (0.5,)),))
    with pytest.raises(ModelComparisonError, match="non-empty reason"):
        EvalScore(good.model, task, good.config, good.dimensions, compatibility=("",))
    with pytest.raises(TypeError, match="EvalConfig"):
        EvalScore(good.model, task, "config", good.dimensions)  # type: ignore[arg-type]


def test_compare_ranks_only_compatible_same_task_same_config_scores() -> None:
    weaker = _score(_descriptor("model-b:latest"), values={"accuracy": (0.5, 0.5, 0.5), "fluency": (0.5, 0.5)})
    stronger = _score(_descriptor("model-a:latest"), values={"accuracy": (0.9, 0.9, 0.9), "fluency": (0.9, 0.9)})
    blocked = _score(_descriptor("model-c:latest"), values={"accuracy": (1.0, 1.0), "fluency": (1.0, 1.0)})

    task = _task(requirements=TaskRequirements(requires_tools=True))
    tool_capable_a = score_evaluation(
        _descriptor("model-a:latest", supports_tools=True, capabilities=("chat", "tools")), task, _config(), {"accuracy": (0.9, 0.9, 0.9), "fluency": (0.9, 0.9)}
    )
    tool_capable_b = score_evaluation(
        _descriptor("model-b:latest", supports_tools=True, capabilities=("chat", "tools")), task, _config(), {"accuracy": (0.5, 0.5, 0.5), "fluency": (0.5, 0.5)}
    )
    no_tools_c = score_evaluation(_descriptor("model-c:latest"), task, _config(), {"accuracy": (1.0, 1.0), "fluency": (1.0, 1.0)})

    result = compare_evaluations([weaker, stronger, blocked])
    assert [entry.score.model.model_id for entry in result.ranked] == ["model-c:latest", "model-a:latest", "model-b:latest"]
    assert [entry.rank for entry in result.ranked] == [1, 2, 3]
    assert result.excluded == ()
    mapping = result.to_mapping()
    assert mapping["ranked"][1]["score_digest"] == stronger.digest  # rank 2: 0.9 overall
    assert mapping["task_digest"] == stronger.task.digest

    filtered = compare_evaluations([no_tools_c, tool_capable_a, tool_capable_b])
    assert [entry.score.model.model_id for entry in filtered.ranked] == ["model-a:latest", "model-b:latest"]
    assert [item.model_id for item in filtered.excluded] == ["model-c:latest"]
    assert filtered.excluded[0].reasons == ("model does not support tool calls",)


def test_compare_hardware_filter_and_environment_disclosure() -> None:
    small_ram = HardwareProfile("macOS", "arm64", available_memory_bytes=1_000)
    hungry = _descriptor("hungry:latest", memory_bytes=5_000)
    lean = _descriptor("lean:latest", memory_bytes=500)
    task = _task()
    scores = [
        score_evaluation(hungry, task, _config(environment={"machine": "dev-01"}), {"accuracy": (0.9,), "fluency": (0.9,)}),
        score_evaluation(lean, task, _config(environment={"machine": "dev-02"}), {"accuracy": (0.5,), "fluency": (0.5,)}),
    ]

    plain = compare_evaluations(scores)
    assert len(plain.ranked) == 2
    assert [item.key for item in plain.environment_differences] == ["machine"]
    assert plain.environment_differences[0].values == (
        ("hungry:latest@sha256:hungry:latest", "dev-01"),
        ("lean:latest@sha256:lean:latest", "dev-02"),
    )

    constrained = compare_evaluations(scores, hardware=small_ram)
    assert [entry.score.model.model_id for entry in constrained.ranked] == ["lean:latest"]
    assert constrained.excluded[0].reasons[0].startswith("model memory requirement 5000 exceeds available hardware memory 1000")
    json.dumps(constrained.to_mapping())


def test_compare_refuses_incomparable_and_malformed_inputs() -> None:
    a = _score(_descriptor("model-a:latest"))

    with pytest.raises(ModelComparisonError, match="non-empty sequence"):
        compare_evaluations([])
    with pytest.raises(TypeError, match="EvalScore"):
        compare_evaluations([a, "not-a-score"])  # type: ignore[list-item]
    different_task = score_evaluation(_descriptor("model-b:latest"), EvalTask("summarize-news", 4, TaskRequirements(), _task().rubric), _config(), {"accuracy": (0.5,), "fluency": (0.5,)})
    with pytest.raises(ModelComparisonError, match="different task versions"):
        compare_evaluations([a, different_task])
    hotter = score_evaluation(_descriptor("model-b:latest"), _task(), _config(temperature=1.0), {"accuracy": (0.5,), "fluency": (0.5,)})
    with pytest.raises(ModelComparisonError, match="2 distinct configs"):
        compare_evaluations([a, hotter])
    duplicate_model = _score(_descriptor("model-a:latest"), values={"accuracy": (0.1, 0.2), "fluency": (0.1, 0.2)})
    with pytest.raises(ModelComparisonError, match="duplicate model identity"):
        compare_evaluations([a, duplicate_model])


def test_comparison_ties_and_single_score_are_deterministic() -> None:
    task = _task()
    config = _config()
    twin_x = score_evaluation(_descriptor("twin-x:latest"), task, config, {"accuracy": (0.5,), "fluency": (0.5,)})
    twin_y = score_evaluation(_descriptor("twin-y:latest"), task, config, {"accuracy": (0.5,), "fluency": (0.5,)})
    first = compare_evaluations([twin_y, twin_x])
    second = compare_evaluations([twin_x, twin_y])
    assert [entry.score.model.model_id for entry in first.ranked] == ["twin-x:latest", "twin-y:latest"]
    assert first.to_mapping() == second.to_mapping()

    solo = compare_evaluations([twin_x])
    assert len(solo.ranked) == 1 and solo.ranked[0].rank == 1
    # Equal environments across scores are not noise; nothing to disclose.
    shared_env = [
        score_evaluation(_descriptor("twin-x:latest"), task, _config(environment={"machine": "same"}), {"accuracy": (0.5,), "fluency": (0.5,)}),
        score_evaluation(_descriptor("twin-y:latest"), task, _config(environment={"machine": "same"}), {"accuracy": (0.4,), "fluency": (0.4,)}),
    ]
    assert compare_evaluations(shared_env).environment_differences == ()


def test_environment_normalization_accepts_both_shapes() -> None:
    from_pairs = _config(environment=(("os", "macos"), ("cpu", "m5")))
    from_mapping = _config(environment={"cpu": "m5", "os": "macos"})
    assert from_pairs.digest == from_mapping.digest
    assert from_pairs.environment == (("cpu", "m5"), ("os", "macos"))
    assert from_pairs.environment == from_mapping.environment


def test_inputs_digest_pins_the_prompt_set() -> None:
    task = _task()
    with_digest = score_evaluation(_descriptor("model-a:latest"), task, _config(), {"accuracy": (0.8,), "fluency": (0.8,)}, inputs_digest="sha256:prompts-42")
    assert with_digest.inputs_digest == "sha256:prompts-42"
    without = score_evaluation(_descriptor("model-a:latest"), task, _config(), {"accuracy": (0.8,), "fluency": (0.8,)})
    assert with_digest.digest != without.digest  # different input set -> different record
    with pytest.raises(ModelComparisonError, match="inputs_digest must not be empty"):
        score_evaluation(_descriptor("model-a:latest"), task, _config(), {"accuracy": (0.8,), "fluency": (0.8,)}, inputs_digest="   ")
