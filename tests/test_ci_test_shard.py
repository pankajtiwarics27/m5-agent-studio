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
