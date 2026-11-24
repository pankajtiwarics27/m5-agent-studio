"""AP-018: a direction sample before expensive work, and silence when it is settled.

The behaviour worth pinning is not "a pause exists" -- it is the two refusals around it:
asking when a reference already answered the question, and *not* asking when the task has
drifted outside what that reference measured. Both are exercised here, each rule broken
on purpose so a test that stopped looking would fail.
"""

from __future__ import annotations

import pytest

import direction_checkpoints as dc

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _sample(
    sha: str = SHA_A, checks: dict[str, bool] | None = None
) -> dc.SampleArtifact:
    return dc.SampleArtifact(
        artifact_id="samples/hero.html",
        content_sha256=sha,
        technical_checks=checks
        if checks is not None
        else {"renders": True, "no_overflow": True},
    )


def _benchmark(kinds: tuple[str, ...] = ("website",)) -> dc.BenchmarkCoverage:
    return dc.BenchmarkCoverage(
        reference_id="ref/website-v1",
        reference_sha256=SHA_C,
        covered_task_kinds=kinds,
        measured_checks=("layout", "copy"),
    )


def test_an_unbenchmarked_task_proposes_a_sample_instead_of_producing():
    checkpoint = dc.decide(task_id="t1", task_kind="website", sample=_sample())
    assert checkpoint.trigger == "no_benchmark"
    assert checkpoint.pauses_production is True
    assert checkpoint.is_resumable_from_checkpoint is True
    assert "website" in checkpoint.feedback_prompt
    assert "keep, redirect" in checkpoint.feedback_prompt


def test_a_benchmarked_task_skips_the_check_in():
    checkpoint = dc.decide(
        task_id="t1", task_kind="website", sample=_sample(), benchmarks=[_benchmark()]
    )
    assert checkpoint.trigger == "benchmark_covered"
    assert checkpoint.pauses_production is False
    assert checkpoint.sample is None
    assert checkpoint.benchmark_reference_id == "ref/website-v1"
    assert checkpoint.is_resumable_from_checkpoint is False


def test_material_uncertainty_escalates_even_when_benchmarked():
    """A reference covers what it measured. Divergence from it is not covered."""
    checkpoint = dc.decide(
        task_id="t1",
        task_kind="website",
        sample=_sample(),
        benchmarks=[_benchmark()],
        uncertainties=["new_format"],
    )
    assert checkpoint.trigger == "uncertainty_exceeds_coverage"
    assert checkpoint.pauses_production is True
    assert checkpoint.escalated_uncertainties == ("new_format",)
    assert "ref/website-v1" in checkpoint.rationale
    assert "new format" in checkpoint.feedback_prompt


def test_a_benchmark_for_another_task_kind_does_not_cover_this_one():
    checkpoint = dc.decide(
        task_id="t1",
        task_kind="video",
        sample=_sample(),
        benchmarks=[_benchmark(("website",))],
    )
    assert checkpoint.trigger == "no_benchmark"
    assert checkpoint.pauses_production is True


def test_a_check_in_without_a_sample_is_refused():
    with pytest.raises(dc.DirectionError, match="needs a sample"):
        dc.decide(task_id="t1", task_kind="website", sample=None)


def test_a_sample_that_failed_its_checks_is_not_offered_for_judgement():
    with pytest.raises(dc.DirectionError, match="failed its own technical checks"):
        dc.decide(
            task_id="t1", task_kind="website", sample=_sample(checks={"renders": False})
        )


def test_a_free_form_uncertainty_cannot_escalate():
    with pytest.raises(dc.DirectionError, match="unknown material uncertainty"):
        dc.decide(
            task_id="t1", task_kind="website", sample=_sample(), uncertainties=["vibes"]
        )


def test_a_benchmark_must_pin_the_version_it_covers():
    with pytest.raises(dc.DirectionError, match="digest of the version"):
        dc.BenchmarkCoverage(
            reference_id="ref/x", reference_sha256="", covered_task_kinds=("website",)
        )


def test_direction_approval_never_implies_acceptance_or_publish_permission():
    checkpoint = dc.decide(task_id="t1", task_kind="website", sample=_sample())
    payload = checkpoint.to_mapping()
    assert payload["direction_approval_is_final_acceptance"] is False
    assert payload["direction_approval_grants_publish_permission"] is False
    # Constants, not defaults: there is no constructor argument that flips them.
    with pytest.raises(TypeError):
        dc.DirectionCheckpoint(
            task_id="t",
            task_kind="website",
            trigger="no_benchmark",
            rationale="x",
            sample=_sample(),
            feedback_prompt="q",
            feedback_points=(),
            direction_approval_is_final_acceptance=True,
        )


def test_resume_continues_on_keep_and_redirects_on_anything_else():
    checkpoint = dc.decide(task_id="t1", task_kind="website", sample=_sample())
    assert (
        dc.resume(
            checkpoint,
            answer="keep",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_A,
        )
        == "continue"
    )
    assert (
        dc.resume(
            checkpoint,
            answer="LGTM",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_A,
        )
        == "continue"
    )
    assert (
        dc.resume(
            checkpoint,
            answer="make it warmer",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_A,
        )
        == "redirect"
    )


def test_a_late_answer_cannot_be_applied_to_a_different_version():
    checkpoint = dc.decide(task_id="t1", task_kind="website", sample=_sample())
    with pytest.raises(dc.DirectionError, match="changed after the question"):
        dc.resume(
            checkpoint,
            answer="keep",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_B,
        )


def test_an_answer_about_an_unrelated_artifact_is_refused():
    checkpoint = dc.decide(task_id="t1", task_kind="website", sample=_sample())
    with pytest.raises(
        dc.DirectionError, match="not the sample this checkpoint showed"
    ):
        dc.resume(
            checkpoint,
            answer="keep",
            answered_artifact_sha256=SHA_B,
            current_artifact_sha256=SHA_B,
        )


def test_a_skipped_check_in_cannot_be_resumed():
    checkpoint = dc.decide(
        task_id="t1", task_kind="website", sample=_sample(), benchmarks=[_benchmark()]
    )
    with pytest.raises(dc.DirectionError, match="did not pause production"):
        dc.resume(
            checkpoint,
            answer="keep",
            answered_artifact_sha256=SHA_A,
            current_artifact_sha256=SHA_A,
        )


def test_fingerprint_is_stable_and_version_sensitive():
    first = dc.decide(task_id="t1", task_kind="website", sample=_sample(SHA_A))
    again = dc.decide(task_id="t1", task_kind="website", sample=_sample(SHA_A))
    other = dc.decide(task_id="t1", task_kind="website", sample=_sample(SHA_B))
    assert first.fingerprint() == again.fingerprint()
    assert first.fingerprint() != other.fingerprint()


def test_the_module_reads_no_clock_and_opens_nothing():
    """A pure decision cannot drift or claim evidence it did not receive."""
    source = (dc.__file__,)
    import pathlib

    text = pathlib.Path(source[0]).read_text(encoding="utf-8")
    for forbidden in (
        "time.time",
        "datetime.now",
        "open(",
        "os.environ",
        "subprocess",
        "requests",
        "httpx",
    ):
        assert forbidden not in text, (
            f"direction_checkpoints must stay pure: found {forbidden}"
        )
