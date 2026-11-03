"""UIUX-P6-05: the release gate must run what it says it ran.

The gate's whole claim is that its lines were executed on this tree. So the tests here do two
things: they execute it and compare the result with the committed artifacts, and they falsify the
ledger one way at a time to prove the gate's bookkeeping checks actually fail closed.

The gate runs thirty-seven `--check` subprocesses (about half a minute), so the expensive fixture is
module-scoped and the negative tests work on in-memory copies rather than on the tree.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PROJECT_ROOT / "scripts" / "uiux_release_gate.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Release_Gate.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-release-gate.md"
HANDOVER_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-handover.md"
LEDGER_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"


def _load():
    spec = importlib.util.spec_from_file_location("uiux_release_gate", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load()


@pytest.fixture(scope="module")
def payload(gate) -> dict:
    return gate.build()


@pytest.fixture()
def ledger() -> dict:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def test_the_committed_gate_is_its_own_output(gate, payload) -> None:
    assert json.loads(JSON_PATH.read_text(encoding="utf-8")) == payload
    assert DOC_PATH.read_text(encoding="utf-8") == gate.render_doc(payload)
    assert HANDOVER_PATH.read_text(encoding="utf-8") == gate.render_handover(payload)


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 findings" in result.stdout


def test_the_gate_has_no_findings_because_every_check_passed(payload: dict) -> None:
    assert payload["findings"] == []
    assert payload["program_state"]["complete"], "the gate reported a finished program only after the last story closed"
    assert payload["totals"]["checks"] == 37
    assert payload["totals"]["checks_passed"] == payload["totals"]["checks"]
    for check in payload["checks"]:
        assert check["exit_code"] == 0, check
        assert check["summary"], f"{check['script']} printed nothing to summarise"
        assert check["command"].endswith("--check")


def test_every_check_names_a_script_and_an_artifact_that_exist(payload: dict) -> None:
    for check in payload["checks"]:
        assert (PROJECT_ROOT / check["script"]).is_file(), check["script"]
        assert check["artifacts"], check["script"]
        for artifact in check["artifacts"]:
            assert (PROJECT_ROOT / artifact).is_file(), artifact
        assert check["totals"]


def test_every_phase_gate_is_met_and_traces_to_checks(payload: dict) -> None:
    assert payload["totals"]["gates"] == 7
    for row in payload["gates"]:
        assert row["met"], row
        assert row["done"] == row["total"], row
        assert row["sentence"], row
    p6 = [row for row in payload["gates"] if row["id"] == "P6"][0]
    assert "release gate" in p6["sentence"] and "handover" in p6["sentence"]


def test_the_five_acceptance_criteria_each_point_at_evidence(payload: dict) -> None:
    assert [row["criterion"] for row in payload["acceptance"]] == [
        "All mapped existing controls remain reachable and functionally equivalent",
        "No console errors on tested journeys",
        "Keyboard operation and focus states work",
        "Failure and recovery states are actionable and honest",
        "Target UI behavior is demonstrable by screenshot or test",
    ]
    for row in payload["acceptance"]:
        assert row["artifacts"], row["criterion"]
        assert row["evidence"], row["criterion"]


def test_the_gate_names_what_it_does_not_run(payload: dict) -> None:
    """Silence is not coverage: the suites the gate does not run are listed with a reason."""
    named = " ".join(row["what"] for row in payload["not_run"])
    assert "pytest -q" in named
    assert "browser suites" in named
    assert "screenshot capture" in named
    for row in payload["not_run"]:
        assert row["why"]


def test_a_known_regression_is_attributed_to_the_story_that_recorded_it(payload: dict) -> None:
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    known = {story["id"] for story in ledger["stories"]}
    assert payload["known_regressions"], "the program recorded regressions; the gate must carry them"
    for row in payload["known_regressions"]:
        assert row["story"] in known, row
        assert row["regression"].strip()


def test_the_gate_catches_a_ledger_counter_that_disagrees_with_its_stories(gate, ledger) -> None:
    broken = copy.deepcopy(ledger)
    broken["overall_progress"]["done"] = broken["overall_progress"]["done"] - 1
    findings = gate.ledger_findings(broken)
    assert any(finding["check"] == "ledger-counters" for finding in findings), findings


def test_the_gate_catches_a_phase_counter_that_disagrees_with_its_stories(gate, ledger) -> None:
    broken = copy.deepcopy(ledger)
    phase = broken["phases"][0]
    phase["done"] = phase["done"] + 1
    findings = gate.ledger_findings(broken)
    assert any(finding["check"] == "ledger-phases" for finding in findings), findings


def test_the_gate_catches_a_done_story_without_a_commit_sha(gate, ledger) -> None:
    broken = copy.deepcopy(ledger)
    done = [story for story in broken["stories"] if story["tracking"]["status"] == "done"]
    done[0]["tracking"]["commit_sha"] = None
    findings = gate.ledger_findings(broken)
    assert any("commit sha" in finding["detail"] for finding in findings), findings


def test_the_gate_catches_a_done_story_without_evidence(gate, ledger) -> None:
    broken = copy.deepcopy(ledger)
    done = [story for story in broken["stories"] if story["tracking"]["status"] == "done"]
    done[-1]["tracking"]["evidence"] = []
    findings = gate.ledger_findings(broken)
    assert any("without evidence" in finding["detail"] for finding in findings), findings


def test_a_failing_artifact_check_unmeets_the_phase_gate_that_it_proves(gate, ledger) -> None:
    checks = [
        {"script": source[1], "exit_code": 0, "summary": "", "command": f"python3 {source[1]} --check"}
        for source in gate.SOURCES
    ]
    failing = next(source for source in gate.SOURCES if source[0] == "UIUX-P0-06")
    for check in checks:
        if check["script"] == failing[1]:
            check["exit_code"] = 1
    gates = gate.phase_gates(ledger, checks)
    p0 = [row for row in gates if row["id"] == "P0"][0]
    assert p0["met"] is False, p0


def test_a_check_that_cannot_run_is_recorded_as_a_failure_not_a_pass(gate) -> None:
    result = gate.run_check("scripts/this_script_does_not_exist.py")
    assert result["exit_code"] != 0
    assert result["command"].endswith("--check")


def test_the_gate_carries_no_clock_so_two_runs_agree(gate, payload) -> None:
    text = json.dumps(payload, sort_keys=True)
    for marker in ("generated_at", "timestamp", "checked_at", "2026-"):
        assert marker not in text, f"the gate embeds {marker!r}, so --check would fail on the next run"
