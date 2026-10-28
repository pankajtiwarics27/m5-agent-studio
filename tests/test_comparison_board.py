from __future__ import annotations

import json

import pytest

from capability_negotiation import TaskRequirements
from comparison_board import (
    BOARD_METRICS,
    CELL_STATES,
    ComparisonBoardError,
    CostObservation,
    build_board,
)
from model_calibration import CalibrationInputs, CalibrationSample, ModelPerformanceProfile, calibrate
from model_comparison import DimensionScore, EvalConfig, EvalScore, EvalTask, RubricDimension, compare_evaluations, score_evaluation
from model_registry import ModelDescriptor, ModelLimits, ModelProvenance
from performance_profiles import BenchmarkMethodology, ProfileKey, ProfileRecord

HARDWARE = {"schema_version": 1, "operating_system": "macOS", "architecture": "arm64"}
METH = BenchmarkMethodology(name="studio-local", version=1)


def _descriptor(model_id: str = "llama", digest: str = "sha256:aa", *, runtime: str = "ollama", quantization: str | None = "Q4_K_M") -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        digest=digest,
        source=runtime,
        revision="rev-1",
        runtime=runtime,
        quantization=quantization,
        limits=ModelLimits(context_tokens=8192),
        provenance=ModelProvenance(runtime, model_id, "verified"),
    )


def _task() -> EvalTask:
    return EvalTask(
        task_id="summarize",
        task_version=1,
        requirements=TaskRequirements(),
        rubric=(RubricDimension("accuracy"), RubricDimension("fluency")),
    )


_CONFIG = EvalConfig(max_output_tokens=256, seed=7)


def _score(model: ModelDescriptor, accuracy: float, fluency: float, *, environment: tuple[tuple[str, str], ...] = ()) -> EvalScore:
    return score_evaluation(
        model,
        _task(),
        EvalConfig(max_output_tokens=256, seed=7, environment=environment) if environment else _CONFIG,
        {"accuracy": (accuracy,), "fluency": (fluency,)},
    )


def _profile(
    model: ModelDescriptor,
    *,
    hardware: dict | None = None,
    with_memory: bool = True,
    only_failures: bool = False,
) -> ModelPerformanceProfile:
    inputs = CalibrationInputs(
        model_id=model.model_id,
        model_digest=model.digest,
        runtime=model.runtime,
        context_length=8192,
        quantization=model.quantization,
        runtime_version="0.5.7",
        hardware=dict(HARDWARE if hardware is None else hardware),
    )
    if only_failures:
        samples = (CalibrationSample(sequence=1, kind="warm", ok=False, error="probe failure: TimeoutError"),)
    else:
        samples = (
            CalibrationSample(
                sequence=1,
                kind="warm",
                ok=True,
                ttft_ms=100.0,
                prefill_tokens_per_s=1000.0,
                decode_tokens_per_s=50.0,
                peak_memory_bytes=1234 if with_memory else None,
                prompt_tokens=100,
                output_tokens=50,
            ),
        )
    return calibrate(inputs, samples)


def _perf_entry(model: ModelDescriptor, **kwargs: object) -> tuple[ProfileKey, ProfileRecord]:
    profile = _profile(model, **kwargs)  # type: ignore[arg-type]
    key = ProfileKey.derive(profile, methodology=METH)
    return key, ProfileRecord(version=1, previous_digest="sha256:genesis", profile=profile)


def _states(row) -> dict[str, str]:
    return {metric: row.cell(metric).state for metric in BOARD_METRICS}


def test_full_board_measures_every_column() -> None:
    first, second = _descriptor("llama", "sha256:aa"), _descriptor("mistral", "sha256:bb")
    comparison = compare_evaluations([_score(first, 0.85, 0.7), _score(second, 0.6, 0.5)])
    board = build_board(
        (first, second),
        comparison=comparison,
        performance={first.model_id: _perf_entry(first), second.model_id: _perf_entry(second)},
        costs={
            first.model_id: CostObservation(0.0, "USD", "open weights, self-hosted"),
            second.model_id: CostObservation(0.25, "USD", "published price card"),
        },
        staleness={first.model_id: "active", second.model_id: "active"},
    )
    row = board.row("llama")
    assert _states(row) == dict.fromkeys(BOARD_METRICS, "measured")
    assert row.rank == 1 and row.comparable and row.verdict == "active"
    quality = row.cell("quality")
    assert quality.summary["overall"] == 0.775 and quality.summary["rank"] == 1
    assert quality.source_digest is not None and board.task_digest == comparison.task_digest
    latency = row.cell("latency").summary
    assert latency["warm"]["ttft_ms"] == {"median": 100.0, "spread": 0.0, "count": 1}
    assert latency["cold"]["decode_tokens_per_s"]["count"] == 0 and latency["profile_version"] == 1
    assert row.cell("memory").summary["warm"]["peak_memory_bytes"]["median"] == 1234.0
    assert row.cell("cost").summary["amount_per_million_tokens"] == 0.0
    assert board.rows[1].rank == 2
    assert board.disclosures == ()


def test_missing_measurements_are_marked_not_zero() -> None:
    board = build_board((_descriptor(),))
    row = board.row("llama")
    assert _states(row) == dict.fromkeys(BOARD_METRICS, "missing")
    assert all(row.cell(metric).summary is None for metric in BOARD_METRICS)
    assert all(row.cell(metric).note for metric in BOARD_METRICS)
    assert row.rank is None and not row.comparable and row.verdict is None
    assert board.task_digest is None and board.config_digest is None


def test_stale_verdicts_flip_measured_cells_but_keep_numbers() -> None:
    model = _descriptor()
    board = build_board(
        (model,),
        comparison=compare_evaluations([_score(model, 0.85, 0.7)]),
        performance={model.model_id: _perf_entry(model)},
        costs={model.model_id: CostObservation(0.1, "USD", "price card")},
        staleness={model.model_id: "invalidated"},
    )
    row = board.row("llama")
    assert row.verdict == "invalidated"
    for metric in BOARD_METRICS:
        cell = row.cell(metric)
        assert cell.state == "stale" and cell.note == "revision invalidated"
        assert cell.summary is not None  # faded numbers, still available
    assert row.rank == 1  # rank stays visible; the staleness badge is per-cell


def test_informational_verdicts_do_not_mark_cells_stale() -> None:
    model = _descriptor()
    board = build_board((model,), staleness={model.model_id: "missing"})
    assert board.row("llama").verdict == "missing"
    assert _states(board.row("llama")) == dict.fromkeys(BOARD_METRICS, "missing")


def test_score_recorded_for_a_different_build_is_stale() -> None:
    scored = _descriptor("llama", "sha256:old")
    candidate = _descriptor("llama", "sha256:new")
    board = build_board((candidate,), comparison=compare_evaluations([_score(scored, 0.85, 0.7)]))
    row = board.row("llama")
    cell = row.cell("quality")
    assert cell.state == "stale" and "different build" in cell.note
    assert row.rank is None  # an old-build score must not occupy the rank column


def test_profile_pinned_to_another_digest_is_stale() -> None:
    other = _profile(_descriptor("llama", "sha256:old")).to_mapping()
    assert other["inputs"]["model_digest"] == "sha256:old"
    stale_entry = (ProfileKey.derive(_profile(_descriptor("llama", "sha256:old")), methodology=METH), ProfileRecord(version=4, previous_digest="sha256:genesis", profile=_profile(_descriptor("llama", "sha256:old"))))
    candidate = _descriptor("llama", "sha256:new")
    board = build_board((candidate,), performance={candidate.model_id: stale_entry})
    for metric in ("latency", "memory"):
        cell = board.row("llama").cell(metric)
        assert cell.state == "stale" and "different model digest" in cell.note
        assert cell.source_digest is not None


def test_incomparable_contexts_are_marked_not_mixed() -> None:
    first, second = _descriptor("llama", "sha256:aa"), _descriptor("mistral", "sha256:bb")
    hw_first = _perf_entry(first)
    second_profile = _profile(second, hardware={"schema_version": 1, "operating_system": "Linux", "architecture": "x86_64"})
    hw_mismatch = (ProfileKey.derive(second_profile, methodology=METH), ProfileRecord(version=1, previous_digest="sha256:genesis", profile=second_profile))
    board = build_board((first, second), performance={first.model_id: hw_first, second.model_id: hw_mismatch})
    assert _states(board.row("llama"))["latency"] == "measured"
    for metric in ("latency", "memory"):
        cell = board.row("mistral").cell(metric)
        assert cell.state == "incomparable" and "hardware" in cell.note

    # A methodology difference is flagged the same way.
    second_key = ProfileKey.derive(_profile(second), methodology=BenchmarkMethodology(name="studio-local", version=2))
    second_rec = ProfileRecord(version=1, previous_digest="sha256:genesis", profile=_profile(second))
    board2 = build_board((first, second), performance={first.model_id: hw_first, second.model_id: (second_key, second_rec)})
    assert "methodology" in board2.row("mistral").cell("latency").note

    # An explicit different os_id also separates contexts.
    os_key = ProfileKey.derive(_profile(second), methodology=METH, os_id="linux x86_64")
    board3 = build_board((first, second), performance={first.model_id: hw_first, second.model_id: (os_key, second_rec)})
    assert "os" in board3.row("mistral").cell("memory").note


def test_zero_count_profile_reports_missing_with_failure_context() -> None:
    model = _descriptor()
    board = build_board((model,), performance={model.model_id: _perf_entry(model, only_failures=True)})
    row = board.row("llama")
    for metric in ("latency", "memory"):
        cell = row.cell(metric)
        assert cell.state == "missing" and "no successful" in cell.note and "failure rate 1.0" in cell.note
        assert cell.source_digest is not None  # the failed attempt is still attributable
    assert row.cell("quality").state == "missing"


def test_partial_measurements_stay_partial() -> None:
    model = _descriptor()
    board = build_board((model,), performance={model.model_id: _perf_entry(model, with_memory=False)})
    row = board.row("llama")
    assert row.cell("latency").state == "measured"
    assert row.cell("memory").state == "missing" and "no successful memory samples" in row.cell("memory").note


def test_excluded_model_shows_reasons_and_no_rank() -> None:
    good, bad = _descriptor("llama", "sha256:aa"), _descriptor("mistral", "sha256:bb")
    compatible = _score(good, 0.8, 0.6)
    incompatible = EvalScore(
        model=bad,
        task=_task(),
        config=_CONFIG,
        dimensions=(DimensionScore(name="accuracy", runs=(0.9,)), DimensionScore(name="fluency", runs=(0.9,))),
        compatibility=("model does not support tools",),
    )
    comparison = compare_evaluations([compatible, incompatible])
    board = build_board((good, bad), comparison=comparison)
    assert board.row("llama").rank == 1
    excluded_row = board.row("mistral")
    assert excluded_row.rank is None and not excluded_row.comparable
    cell = excluded_row.cell("quality")
    assert cell.state == "excluded" and "model does not support tools" in cell.note
    assert excluded_row.excluded_reasons == ("model does not support tools",)


def test_environment_differences_and_outside_scores_are_disclosed() -> None:
    first, second, extra = _descriptor("a", "sha256:1"), _descriptor("b", "sha256:2"), _descriptor("c", "sha256:3")
    comparison = compare_evaluations(
        [
            _score(first, 0.8, 0.7, environment=(("host", "studio-mbp"),)),
            _score(second, 0.7, 0.6, environment=(("host", "studio-mini"),)),
            _score(extra, 0.65, 0.55, environment=(("host", "cloud"),)),
        ]
    )
    board = build_board((first, second), comparison=comparison)
    text = "\n".join(board.disclosures)
    assert "environment differs on host" in text and "a@sha256:1=studio-mbp" in text and "cloud" in text
    assert "outside the candidate list: c" in text


def test_input_validation_covers_every_contract() -> None:
    model = _descriptor()
    with pytest.raises(ComparisonBoardError, match="non-empty sequence"):
        build_board(())
    with pytest.raises(TypeError, match="ModelDescriptor"):
        build_board(("not-a-descriptor",))
    dup = _descriptor("llama", "sha256:aa")
    with pytest.raises(ComparisonBoardError, match="duplicate candidate"):
        build_board((model, dup))
    with pytest.raises(TypeError, match="ComparisonResult"):
        build_board((model,), comparison="nope")  # type: ignore[arg-type]
    with pytest.raises(ComparisonBoardError, match="not a candidate"):
        build_board((model,), performance={"ghost": _perf_entry(model)})
    with pytest.raises(ComparisonBoardError, match="not a candidate"):
        build_board((model,), staleness={"ghost": "active"})
    with pytest.raises(ComparisonBoardError, match="must be one of"):
        build_board((model,), staleness={model.model_id: "definitely-fresh"})
    with pytest.raises(ComparisonBoardError, match="pairs"):
        build_board((model,), performance={model.model_id: (_perf_entry(model)[0],)})  # type: ignore[dict-item]
    foreign_key = ProfileKey.derive(_profile(_descriptor("llama", "sha256:old")), methodology=METH)
    with pytest.raises(ComparisonBoardError, match="does not agree with its key"):
        build_board((model,), performance={model.model_id: (foreign_key, _perf_entry(model)[1])})
    with pytest.raises(ComparisonBoardError, match="finite non-negative"):
        CostObservation(-1.0, "USD", "price card")
    with pytest.raises(ComparisonBoardError, match="three-letter"):
        CostObservation(0.1, "usd", "price card")
    with pytest.raises(ComparisonBoardError, match="must not be empty"):
        CostObservation(0.1, "USD", "   ")
    with pytest.raises(ComparisonBoardError, match="not on this board"):
        build_board((model,)).row("ghost")
    with pytest.raises(ComparisonBoardError, match="unknown board metric"):
        build_board((model,)).row("llama").cell("vibes")


def test_board_is_deterministic_and_content_addressed() -> None:
    model = _descriptor()
    args = dict(
        comparison=compare_evaluations([_score(model, 0.85, 0.7)]),
        performance={model.model_id: _perf_entry(model)},
        costs={model.model_id: CostObservation(0.1, "USD", "price card")},
    )
    first = build_board((model,), **args)
    second = build_board((model,), **args)
    assert first.digest == second.digest
    payload = json.dumps(first.to_mapping(), sort_keys=True)
    assert "schema_version" in payload and all(state in CELL_STATES for state in _states(first.row("llama")).values())

    moved = build_board((model,), **{**args, "costs": {model.model_id: CostObservation(0.1, "USD", "different source")}})
    assert moved.digest != first.digest

    other = _descriptor("mistral", "sha256:bb")
    solo = build_board((model,))
    pair = build_board((model, other))
    assert pair.digest != solo.digest  # candidate ordering and breadth are part of board identity
