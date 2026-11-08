"""UIV2-29 source, evidence and fail-closed performance/backend safety tests."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uiv2_performance_safety.py"
WALK = ROOT / "scripts" / "uiv2_performance_safety_walk.mjs"
spec = importlib.util.spec_from_file_location("uiv2_performance_safety", SCRIPT)
assert spec and spec.loader
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)


def browser_run() -> dict:
    return json.loads(contract.RUN_PATH.read_text(encoding="utf-8"))


def backend_run() -> dict:
    return json.loads(contract.BACKEND_RUN_PATH.read_text(encoding="utf-8"))


def test_contract_is_clean_source_bound_and_reproducible() -> None:
    payload = contract.build()
    assert payload["findings"] == []
    assert payload["browser_evidence"]["status"] == "passed"
    assert payload["backend_evidence"]["status"] == "passed"
    assert payload["browser_evidence"]["totals"] == {"checks": 21, "passed": 21, "failed": 0, "captures": 11}
    assert payload["backend_evidence"]["totals"]["tests_passed"] >= 200
    assert payload["visual_reference"]["approved_manifest"]
    assert payload["visual_reference"]["approval_reference"].startswith("user:")
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_committed_artifacts_are_exactly_generated() -> None:
    payload = contract.build()
    assert contract.JSON_PATH.read_text(encoding="utf-8") == contract.dumps(payload)
    assert contract.DOC_PATH.read_text(encoding="utf-8") == contract.render_doc(payload)


def test_browser_matrix_metrics_and_captures_are_real() -> None:
    payload = browser_run()
    assert contract.validate_browser_run(payload) == []
    assert payload["source_sha256"] == contract.browser_source_hashes()
    assert payload["environment"]["memory_method"].startswith("CDP Performance.getMetrics")
    assert payload["environment"]["chrome_version"]
    assert payload["metrics"]["chat"]["fast"]["sse_per_second"] >= 50
    assert payload["metrics"]["harness"]["steady"]["mean_ms"] <= 4
    assert payload["metrics"]["harness"]["memory"]["collection"].startswith("CDP HeapProfiler.collectGarbage")
    layout = {(row["viewport"], row["theme"]) for row in payload["checks"] if row["group"] == "layout"}
    assert layout == {(viewport, theme) for viewport in contract.VIEWPORTS for theme in contract.THEMES}
    for capture in payload["captures"]:
        assert capture["path"].endswith(".png") and len(capture["sha256"]) == 64


def test_backend_run_covers_safety_and_never_promotes_unprobed_isolation() -> None:
    payload = backend_run()
    assert contract.validate_backend_run(payload) == []
    assert payload["source_sha256"] == contract.backend_source_hashes()
    assert payload["pytest"]["exit_code"] == 0
    assert payload["totals"]["tests_passed"] >= 200
    assert payload["totals"]["failed"] == 0
    assert payload["isolation"]["checks_failed"] == 0
    assert payload["isolation"]["status"] == "unqualified"
    assert payload["isolation"]["dependent_execution"] == "blocked (fail closed)"
    assert payload["isolation"]["live_probes"] == "not requested; container isolation is not claimed"
    assert set(payload["coverage"]) == {
        "high_frequency_sse", "bounded_dom_and_memory", "permissions_and_authorization",
        "crash_and_stream_recovery", "action_ledger_and_lease_fencing", "blocked_isolation",
    }


@pytest.mark.parametrize("requirement", sorted(contract.REQUIRED_WALK))
def test_removing_each_browser_gate_requirement_fails_closed(requirement: str) -> None:
    source = WALK.read_text(encoding="utf-8")
    needle = contract.REQUIRED_WALK[requirement]
    assert needle in source
    findings = contract.source_findings(contract.STATIC_PATH.read_text(encoding="utf-8"), source.replace(needle, ""))
    assert any(row["check"] == requirement for row in findings)


@pytest.mark.parametrize("requirement", sorted(contract.REQUIRED_FRONTEND))
def test_removing_each_frontend_performance_contract_fails_closed(requirement: str) -> None:
    html = contract.STATIC_PATH.read_text(encoding="utf-8")
    needle = contract.REQUIRED_FRONTEND[requirement]
    assert needle in html
    findings = contract.source_findings(html.replace(needle, ""), WALK.read_text(encoding="utf-8"))
    assert any(row["check"] == requirement for row in findings)


def test_disclosure_scan_cannot_move_back_into_incremental_frame() -> None:
    html = contract.STATIC_PATH.read_text(encoding="utf-8")
    render_start = html.index("  function renderStoredHarnessEvents(events)")
    render_end = html.index("  function appendWorkflowApprovalCard", render_start)
    render = html[render_start:render_end]
    assert render.index('querySelectorAll("details")') > render.index("if (plan)")
    open_line = next(line for line in html.splitlines() if "const openDetails =" in line)
    moved = html.replace(open_line + "\n", "", 1).replace("    const decisions = {};", open_line + "\n    const decisions = {};", 1)
    findings = contract.source_findings(moved, WALK.read_text(encoding="utf-8"))
    assert any(row["check"] == "incremental-open-details" for row in findings)
    # Removing the snapshot guard entirely must be caught even if the general merge remains.
    findings = contract.source_findings(html.replace("supplied === harnessCachedSnapshot && harnessCachedSnapshot", "false"), WALK.read_text(encoding="utf-8"))
    assert any(row["check"] == "snapshot-render-guard" for row in findings)


def test_workflow_executes_legacy_browser_new_browser_and_backend_gates() -> None:
    workflow = contract.WORKFLOW_PATH.read_text(encoding="utf-8")
    for needle in contract.REQUIRED_WORKFLOW.values():
        assert needle in workflow
    assert workflow.index("python scripts/uiv2_performance_safety.py --run-backend") < workflow.index("node scripts/uiv2_performance_safety_walk.mjs")
    assert "timeout-minutes: 15" in workflow


def test_failed_partial_or_stale_browser_run_is_rejected() -> None:
    original = browser_run()
    failed = copy.deepcopy(original)
    failed["checks"][0]["passed"] = False
    assert contract.validate_browser_run(failed)
    partial = copy.deepcopy(original)
    partial["checks"] = partial["checks"][:-1]
    assert contract.validate_browser_run(partial)
    stale = copy.deepcopy(original)
    stale["source_sha256"]["static/index.html"] = "0" * 64
    assert any("stale" in error for error in contract.validate_browser_run(stale))
    missing_layout = copy.deepcopy(original)
    missing_layout["checks"] = [row for row in missing_layout["checks"] if row["group"] != "layout"]
    assert contract.validate_browser_run(missing_layout)


def test_browser_evidence_rejects_unbounded_memory_and_slow_frames() -> None:
    growing = browser_run()
    memory = growing["metrics"]["harness"]["memory"]
    memory["after_cycle_2"]["Nodes"] = memory["after_cycle_1"]["Nodes"] + 65
    assert any("DOM nodes" in error for error in contract.validate_browser_run(growing))
    heap = browser_run()
    memory = heap["metrics"]["harness"]["memory"]
    memory["after_cycle_2"]["JSHeapUsedSize"] = memory["after_cycle_1"]["JSHeapUsedSize"] + contract.THRESHOLDS["heap_plateau_bytes"] + 1
    assert any("heap" in error for error in contract.validate_browser_run(heap))
    slow = browser_run()
    slow["metrics"]["harness"]["steady"]["mean_ms"] = 4.01
    assert any("timing" in error for error in contract.validate_browser_run(slow))


def test_browser_evidence_rejects_fake_sse_layout_or_weakened_thresholds() -> None:
    slow_sse = browser_run()
    slow_sse["metrics"]["chat"]["fast"]["sse_per_second"] = 49.9
    assert any("high-frequency" in error for error in contract.validate_browser_run(slow_sse))
    shifted = browser_run()
    row = next(row for row in shifted["checks"] if row["group"] == "layout")
    detail = json.loads(row["detail"])
    detail["cls_external_shell"] = 0.02
    row["detail"] = json.dumps(detail)
    assert any("layout metrics" in error for error in contract.validate_browser_run(shifted))
    weakened = browser_run()
    weakened["thresholds"]["frame_budget_ms"] = 16.67
    assert any("thresholds" in error for error in contract.validate_browser_run(weakened))


def test_browser_evidence_requires_complete_capture_and_environment_accounting() -> None:
    missing_capture = browser_run()
    missing_capture["captures"] = missing_capture["captures"][:-1]
    assert contract.validate_browser_run(missing_capture)
    no_environment = browser_run()
    no_environment["environment"]["chrome_version"] = ""
    assert any("chrome_version" in error for error in contract.validate_browser_run(no_environment))
    duplicate = browser_run()
    duplicate["captures"][1]["path"] = duplicate["captures"][0]["path"]
    assert any("unique" in error for error in contract.validate_browser_run(duplicate))


def test_failed_stale_or_reduced_backend_run_is_rejected() -> None:
    failed = backend_run()
    failed["pytest"]["exit_code"] = 1
    assert contract.validate_backend_run(failed)
    stale = backend_run()
    stale["source_sha256"]["app.py"] = "0" * 64
    assert any("stale" in error for error in contract.validate_backend_run(stale))
    reduced = backend_run()
    reduced["test_modules"] = reduced["test_modules"][:-1]
    assert any("coverage" in error for error in contract.validate_backend_run(reduced))
    skipped = backend_run()
    skipped["pytest"]["output_tail"].append("299 passed, 1 skipped")
    assert any("skip" in error for error in contract.validate_backend_run(skipped))


def test_isolation_cannot_be_silently_released_or_have_failures_removed() -> None:
    released = backend_run()
    released["isolation"]["status"] = "qualified"
    released["isolation"]["dependent_execution"] = "released"
    assert any("unqualified and blocked" in error for error in contract.validate_backend_run(released))
    failed = backend_run()
    failed["isolation"]["checks_failed"] = 1
    assert contract.validate_backend_run(failed)
    overclaim = backend_run()
    overclaim["isolation"]["live_probes"] = "passed"
    assert any("overclaims" in error for error in contract.validate_backend_run(overclaim))


def test_docs_index_links_the_gate_and_test_map_owns_it() -> None:
    assert contract.DOC_PATH.name in (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    scopes = json.loads((ROOT / "tests" / "test_scopes.json").read_text(encoding="utf-8"))
    shared = scopes["areas"]["ui.shared"]
    execution = scopes["areas"]["security.execution"]
    assert "test_uiv2_performance_safety.py" in shared
    assert "test_uiv2_performance_safety.py" in execution
