"""LC-00 offline assumptions, not production adapter or hardware qualification."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_lifecycle_feasibility import (
    DEFAULT_CORPUS,
    ROOT,
    assess_release,
    assess_request,
    replay,
)

CORPUS = json.loads(DEFAULT_CORPUS.read_text())
CASES = CORPUS["cases"]
RELEASE = next(c["facts"] for c in CASES if c["id"] == "omlx-synthetic-complete-release")
REQUEST = next(c["facts"] for c in CASES if c["id"] == "omlx-synthetic-fitting-request")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_synthetic_contract_cases(case: dict) -> None:
    assess = assess_release if case["kind"] == "release" else assess_request
    before = copy.deepcopy(case)
    assert assess(case["facts"]) == case["expected"]
    assert case == before


@pytest.mark.parametrize("key", list(RELEASE))
def test_missing_release_evidence_never_becomes_empty_domain(key: str) -> None:
    facts = copy.deepcopy(RELEASE)
    del facts[key]
    assert assess_release(facts).startswith("blocked:")


@pytest.mark.parametrize("key", list(REQUEST))
def test_missing_request_evidence_never_becomes_ready(key: str) -> None:
    facts = copy.deepcopy(REQUEST)
    del facts[key]
    assert assess_request(facts).startswith("blocked:")


@pytest.mark.parametrize("bad", [True, False, -1, "8192", 8192.0, None])
def test_context_is_not_coerced(bad: object) -> None:
    facts = {**REQUEST, "active_context_tokens": bad}
    assert assess_request(facts) == "blocked:counts_unknown"


def test_output_reserve_is_part_of_fit_and_equal_boundary_is_allowed() -> None:
    facts = {**REQUEST, "input_tokens": 8000, "output_reserve_tokens": 192}
    assert assess_request(facts) == "candidate_only"
    facts["output_reserve_tokens"] = 193
    assert assess_request(facts) == "blocked:context_budget"


def test_acknowledgment_status_cannot_supply_release_evidence() -> None:
    for status in (200, 202, 400, 404):
        assert assess_release({"http_status": status}) == "blocked:operation_unsettled"


def test_replay_success_is_never_runtime_qualification() -> None:
    report = replay(CORPUS)
    assert report["expectations_match"] is True
    assert report["activation_allowed"] is False
    assert report["installed_runtime_qualification"] == "not_run"
    assert len(report["cases"]) == 19
    assert set(c["runtime"] for c in CASES) == {"omlx", "ollama", "lmstudio"}


@pytest.mark.parametrize("defect", ["duplicate", "kind", "not-synthetic", "id", "empty", "schema-bool"])
def test_invalid_corpora_fail_closed(defect: str) -> None:
    corpus = copy.deepcopy(CORPUS)
    if defect == "duplicate":
        corpus["cases"].append(corpus["cases"][0])
    elif defect == "kind":
        corpus["cases"][0]["kind"] = "load_live"
    elif defect == "not-synthetic":
        corpus["synthetic"] = False
    elif defect == "id":
        corpus["cases"][0]["id"] = "private upstream response\n"
    elif defect == "schema-bool":
        corpus["schema_version"] = True
    else:
        corpus["cases"] = []
    with pytest.raises(ValueError):
        replay(corpus)


def test_fixture_sources_resolve_to_the_pinned_register() -> None:
    import re
    document = (ROOT / "docs/model-lifecycle-capability-feasibility.md").read_text()
    source_ids = set(re.findall(r"^\| ([A-Z][A-Z0-9-]+) \|", document, re.MULTILINE))
    assert source_ids
    for case in CASES:
        assert case["source_refs"]
        assert set(case["source_refs"]) <= source_ids
    assert sum(c["kind"] == "release" for c in CASES) == 10
    assert sum(c["kind"] == "request" for c in CASES) == 9
    assert sum(c["expected"] == "candidate_only" for c in CASES) == 2


def test_runner_has_no_network_or_application_imports() -> None:
    import ast
    tree = ast.parse((ROOT / "scripts/check_lifecycle_feasibility.py").read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert imports <= {"__future__", "argparse", "json", "collections.abc", "pathlib"}


def test_cli_reports_only_synthetic_results_and_nonzero_on_mismatch(tmp_path: Path) -> None:
    corpus = copy.deepcopy(CORPUS)
    corpus["cases"][0]["wire_excerpt"]["body"]["message"] = "sensitive-example-not-for-output"
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(corpus))
    command = [sys.executable, str(ROOT / "scripts/check_lifecycle_feasibility.py"), str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["activation_allowed"] is False
    assert "sensitive-example-not-for-output" not in result.stdout + result.stderr
    corpus["cases"][0]["expected"] = "candidate_only"
    path.write_text(json.dumps(corpus))
    assert subprocess.run(command, capture_output=True, check=False).returncode == 1
    path.write_text("not json: sensitive-example-not-for-output")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "sensitive-example-not-for-output" not in result.stdout + result.stderr
