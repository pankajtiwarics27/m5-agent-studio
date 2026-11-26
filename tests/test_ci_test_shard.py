from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ci_test_shard

ROOT = Path(__file__).resolve().parents[1]


def test_committed_shard_manifest_partitions_every_test_module_once() -> None:
    manifest = ci_test_shard.load_manifest()
    stats = ci_test_shard.validate_manifest(manifest)

    assert stats["modules"] == len(tuple((ROOT / "tests").glob("test_*.py")))
    assert set(stats["module_counts"]) == set(ci_test_shard.SHARD_IDS)
    assert all(count > 0 for count in stats["module_counts"].values())
    balance = manifest["balance"]
    assert balance["module_counts"] == [stats["module_counts"][shard] for shard in ci_test_shard.SHARD_IDS]
    assert len(balance["fit_run_ids"]) >= 2
    assert set(balance["input_partition_observed"]["seconds_by_run"]) == {
        str(run_id) for run_id in balance["fit_run_ids"]
    }
    assert len(balance["projected_median_aggregate_seconds"]) == len(ci_test_shard.SHARD_IDS)
    assert balance["observed_run_id"] > 0
    assert len(balance["observed_shard_seconds"]["aggregate"]) == len(ci_test_shard.SHARD_IDS)


def test_canonical_outcome_hash_includes_outcomes_and_skip_reasons() -> None:
    baseline = {
        "tests/test_a.py::test_pass": {"outcome": "passed", "skip_reason": ""},
        "tests/test_b.py::test_skip": {"outcome": "skipped", "skip_reason": "needs browser"},
    }
    reordered = dict(reversed(list(baseline.items())))
    changed = {**baseline, "tests/test_b.py::test_skip": {"outcome": "skipped", "skip_reason": "wrong reason"}}

    assert ci_test_shard.canonical_outcome_hash(baseline) == ci_test_shard.canonical_outcome_hash(reordered)
    assert ci_test_shard.canonical_outcome_hash(baseline) != ci_test_shard.canonical_outcome_hash(changed)
    assert ci_test_shard.canonical_skip_hash(baseline) != ci_test_shard.canonical_skip_hash(changed)


def test_failed_and_error_outcomes_emit_one_error_annotation_each(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ci_test_shard._emit_failure_annotations(
        {
            "tests/test_alpha.py::test_pass": {"outcome": "passed", "skip_reason": ""},
            "tests/test_alpha.py::test_failure[100%]": {"outcome": "failed", "skip_reason": ""},
            "tests/test_beta.py::test_error": {"outcome": "error", "skip_reason": ""},
            "tests/test_beta.py::test_skip": {"outcome": "skipped", "skip_reason": "expected"},
        }
    )

    assert capsys.readouterr().out.splitlines() == [
        "::error title=Pytest failure::tests/test_alpha.py::test_failure[100%25]: failed "
        "(no detail recorded for this report)",
        "::error title=Pytest failure::tests/test_beta.py::test_error: error "
        "(no detail recorded for this report)",
    ]


def test_a_failure_annotation_carries_the_reason_not_only_the_outcome(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #393: a bare `: failed` annotation left a CI-only flake unexplainable.

    The run failed once, the job log and the artifact download both errored, and the annotation said
    only that the test failed. The reason must survive in the annotation, because it may be the only
    surface an operator can reach.
    """
    ci_test_shard._emit_failure_annotations(
        {
            "tests/test_alpha.py::test_whys": {
                "outcome": "failed",
                "skip_reason": "",
                "failure": "tests/test_alpha.py:12: in test_whys\n    assert value[\"b\"] == 2\nE   KeyError: 'b'",
            }
        }
    )
    out = capsys.readouterr().out
    assert ": failed" in out, out
    assert "KeyError: 'b'" in out, "the exception was dropped from the annotation: " + out
    assert "assert value" in out, "the failing assertion was dropped from the annotation: " + out


def test_a_failure_with_no_recorded_detail_says_so_rather_than_looking_bare(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`no detail recorded` and `the reason was X` must not be indistinguishable."""
    ci_test_shard._emit_failure_annotations(
        {"tests/test_alpha.py::test_x": {"outcome": "error", "skip_reason": ""}}
    )
    out = capsys.readouterr().out
    assert "no detail recorded" in out
    assert out.strip().endswith("(no detail recorded for this report)")


def test_the_recorder_captures_the_detail_of_a_real_failing_report() -> None:
    """The emitter is not the only half: the recorder must keep the reason in the first place.

    Tested with an actual pytest report rather than a stub, because the detail comes from
    `report.longrepr`, and a stub would only prove the stub right.
    """
    import _pytest.reports

    recorder = ci_test_shard.ResultRecorder()

    class _Item:
        nodeid = "tests/test_alpha.py::test_whys"

    failed = _pytest.reports.TestReport(
        nodeid=_Item.nodeid,
        location=("tests/test_alpha.py", 12, "test_whys"),
        keywords={},
        outcome="failed",
        longrepr="tests/test_alpha.py:12: in test_whys\n    assert value[\"b\"] == 2\nE   KeyError: 'b'",
        when="call",
        sections=[],
        duration=0.01,
    )
    recorder.pytest_runtest_logreport(failed)
    recorded = recorder.outcomes[_Item.nodeid]
    assert recorded["outcome"] == "failed"
    assert "KeyError: 'b'" in recorded["failure"], (
        "the recorder dropped the reason, so no annotation can carry it: " + repr(recorded)
    )


def test_the_recorder_captures_the_detail_of_a_real_error_report() -> None:
    import _pytest.reports

    recorder = ci_test_shard.ResultRecorder()
    errored = _pytest.reports.TestReport(
        nodeid="tests/test_beta.py::test_setup",
        location=("tests/test_beta.py", 3, "test_setup"),
        keywords={},
        outcome="failed",
        longrepr="E   RuntimeError: fixture exploded",
        when="setup",
        sections=[],
        duration=0.01,
    )
    recorder.pytest_runtest_logreport(errored)
    recorded = recorder.outcomes["tests/test_beta.py::test_setup"]
    assert recorded["outcome"] == "error"
    assert "RuntimeError: fixture exploded" in recorded["failure"], repr(recorded)


def test_the_failure_detail_is_bounded_and_keeps_the_exception() -> None:
    """A huge traceback must not produce an unbounded annotation, and must keep its last line."""

    class _Report:
        longrepr = "\n".join([f"frame {index}: " + "x" * 80 for index in range(400)]) + "\nE   ValueError: the end"

    detail = ci_test_shard._failure_detail(_Report())
    assert len(detail) <= ci_test_shard._ANNOTATION_LIMIT + 3
    assert detail.endswith("ValueError: the end"), "the exception line was cut off"


def test_a_failure_detail_does_not_appear_in_the_outcome_hash() -> None:
    """A traceback carries file paths and timings; identity must not depend on it."""
    base = {"tests/test_a.py::test_x": {"outcome": "failed", "skip_reason": ""}}
    with_detail = {
        "tests/test_a.py::test_x": {"outcome": "failed", "skip_reason": "", "failure": "E   boom"}
    }
    assert ci_test_shard.canonical_outcome_hash(base) == ci_test_shard.canonical_outcome_hash(with_detail)
    assert ci_test_shard.canonical_skip_hash(base) == ci_test_shard.canonical_skip_hash(with_detail)


def test_result_verifier_rejects_duplicates_and_proves_collection_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = "3.11"
    nodes = [f"tests/test_example.py::test_{index}" for index in range(3)]
    monkeypatch.setattr(ci_test_shard, "collect_node_ids", lambda _modules: set(nodes))
    for index, shard in enumerate(ci_test_shard.SHARD_IDS):
        payload = {
            "exit_code": 0,
            "label": f"shard {shard}",
            "node_ids": [nodes[index]],
            "outcomes": {nodes[index]: {"outcome": "passed", "skip_reason": ""}},
        }
        (tmp_path / f"pytest-result-{version}-{shard}.json").write_text(json.dumps(payload), encoding="utf-8")

    output = tmp_path / "combined.json"
    result = ci_test_shard.verify_results(result_dir=tmp_path, python_version=version, output=output)
    assert result["node_count"] == 3
    assert result["duplicate_count"] == 0
    assert result["outcome_counts"] == {"passed": 3}
    assert json.loads(output.read_text(encoding="utf-8"))["outcome_sha256"] == result["outcome_sha256"]

    duplicate_path = tmp_path / f"pytest-result-{version}-3.json"
    duplicate = json.loads(duplicate_path.read_text(encoding="utf-8"))
    duplicate["node_ids"] = [nodes[0]]
    duplicate["outcomes"] = {nodes[0]: {"outcome": "passed", "skip_reason": ""}}
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        ci_test_shard.verify_results(result_dir=tmp_path, python_version=version)


def test_workflow_uses_job_shards_and_retains_required_aggregate_names() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Python test shard (${{ matrix.python-version }}, ${{ matrix.shard }})" in workflow
    assert "python-version: [\"3.11\", \"3.12\", \"3.13\"]" in workflow
    assert "shard: [\"1\", \"2\", \"3\"]" in workflow
    assert "python scripts/ci_test_shard.py" in workflow
    assert '--run-shard "${{ matrix.shard }}"' in workflow
    assert '--verify-results "${RUNNER_TEMP}/pytest-results"' in workflow
    assert "unsharded_baseline" in workflow
    assert "baseline_run_id" in workflow
    assert "python scripts/verify_test_identity.py" in workflow
    assert "Refuse a baseline from another commit" in workflow
    assert "test' }} (${{ matrix.python-version }})" in workflow
    assert "pytest-xdist" not in workflow
    assert " -k " not in workflow
    assert "--deselect" not in workflow
