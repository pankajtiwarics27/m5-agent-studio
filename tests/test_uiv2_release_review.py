"""UIV2-30 release evidence, truthful scope and fail-closed handoff tests.

UNPARKED 2026-11-08. These assertions were retired on PR #258 and restored here
once their unblock condition was met -- which was the point of parking them
rather than deleting them.

What unblocked it: the review's evidence must cite a green CI run of the commit
that contains it, so it can never be satisfied inside the commit that produces
it. It becomes satisfiable once ``main`` has a green run at a known commit (the
merge commit ``5fb978b``), because the evidence imported afterwards is a normal
follow-up commit whose implementation HEAD is already green in CI.

The second parked defect is fixed here rather than parked again:
``test_uiv2_29_final_summary_uses_actual_metrics_not_flat_transcription`` used
to hardcode a heap triple from one specific run. Heap usage varies per run, so
any legitimate regeneration of the UIV2-29 record broke it. It now asserts that
the ledger quotes the triple recorded in the committed run, which is the
property that actually matters: the summary must not drift from the measurement.

If this module fails again, treat it as a real signal and read ``findings`` in
``product/M5_2_0_UIUX_V2_Release_Review.json`` -- do not re-park it without an
owner decision, and never satisfy it by citing a CI run that did not happen.

PARKED AGAIN 2026-11-09 (P7-PARK-03). The phone Hall fix moved the seat grid's collapse from
390px to 340px, which changes the pinned structural signature of the two 390x844 hall captures
(`required.#hallStream.y` 2273.61 -> 1740.42). The baseline was re-captured and approved by the
owner in the same branch, but the release evidence (`product/M5_2_0_UIUX_V2_Release_Evidence.json`)
still cites the pre-fix run, so `findings` is non-empty and this module cannot pass yet. The
unblock condition is the one P7-PARK-02 had: land the change, then import evidence against a green
run of a commit that already contains it. Owner decision recorded in docs/uiux-2-0-p7-parked.md.

UNPARKED AGAIN 2026-09-11 -> 2026-11-09. P7-PARK-02 parked these assertions while the
UIV2-P7-07 header fold made the committed release evidence stale. The unblock condition
was met: ``main`` went green at merge commit ``6aca516``, a commit that already contains
this ``static/index.html``, so the evidence could be imported against that run
(Compare 15/15, visual 2200/2200, CI success). The assertions were never edited while
parked, and the contract now reports ``ready`` with zero findings.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# PARKED 2026-11-09 (P7-PARK-03, see docs/uiux-2-0-p7-parked.md). Skipped, never edited: the
# assertions below are the release-handoff contract and must be restored, unchanged, as soon as the
# evidence can cite a green run of a commit that contains them.
pytestmark = pytest.mark.skip(
    reason="P7-PARK-03: release evidence is pre-fix; import it against a green run of the commit that carries the phone Hall fix, then unpark"
)

SCRIPT = ROOT / "scripts" / "uiv2_release_review.py"

spec = importlib.util.spec_from_file_location("uiv2_release_review", SCRIPT)
assert spec and spec.loader
contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contract
spec.loader.exec_module(contract)


@pytest.fixture(scope="module")
def payload() -> dict:
    return contract.build()


def evidence() -> dict:
    return contract.read_json(contract.EVIDENCE_PATH)


def roadmap() -> dict:
    return contract.read_json(contract.ROADMAP_PATH)


def test_contract_is_ready_without_overturning_production_security_block(payload: dict) -> None:
    assert payload["findings"] == []
    assert payload["decision"] == {
        "uiux_v2_handoff": "ready",
        "production_security_release": "release-blocked",
        "scope": "merge/handoff of the 30-story UI/UX V2 program only; tagging/publishing a production release remains under scripts/security_release_gate.py and scripts/release.py",
    }
    assert payload["security_release"]["release_ready"] is False
    assert payload["security_release"]["gate_invariants_satisfied"] is True
    assert payload["security_release"]["blocking_reasons"]
    assert payload["totals"]["local_checks"] == payload["totals"]["local_checks_passed"] == 7
    assert payload["totals"]["ci_jobs"] == 4


def test_committed_contract_and_handoff_are_exactly_generated(payload: dict) -> None:
    assert contract.JSON_PATH.read_text(encoding="utf-8") == contract.dumps(payload)
    assert contract.DOC_PATH.read_text(encoding="utf-8") == contract.render_doc(payload)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_is_pinned_to_clean_full_sha_and_current_browser_sources(payload: dict) -> None:
    pinned = payload["pinned_release"]
    recorded = evidence()
    assert contract._is_commit(pinned["implementation_head"])
    assert pinned["dirty"] is False
    if recorded["review_scope"] == contract.REVIEW_SCOPE_INTEGRATED:
        expected = contract.evidence_source_hashes_at_commit(contract.UIV2_30_INTEGRATED_HEAD)
        assert pinned["implementation_head"] == contract.UIV2_30_INTEGRATED_HEAD
        assert pinned["current_browser_sources"] is False
        assert pinned["post_handoff_evidence"] == "pending-clean-implementation-and-ci"
    else:
        assert recorded["review_scope"] == contract.REVIEW_SCOPE_CURRENT
        expected = contract.evidence_source_hashes()
        assert pinned["current_browser_sources"] is True
        assert pinned["post_handoff_evidence"] == "current"
    assert pinned["static_sha256"] == expected["static/index.html"]
    assert payload["visual_evidence"]["git"]["head"] == pinned["implementation_head"]
    assert recorded["source_sha256"] == expected == contract.evidence_source_hashes_at_commit(pinned["implementation_head"])
    # Judge UIV2-30 by its own integrated source range. A follow-up either has fresh current
    # evidence or is explicitly pending; neither state may make the historical story claim that
    # it changed the runtime itself.
    changed = subprocess.run(
        ["git", "diff", "--name-only", "703945e496c38b6bf0fb148f18fb213f00d20015", "c01d01bd0a02c9617c7b08524f24be7f3fa3eddd"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert not {"app.py", "static/index.html", "studio.py"}.intersection(changed)


def test_visual_evidence_is_actual_complete_and_explicitly_approved(payload: dict) -> None:
    visual = payload["visual_evidence"]
    assert visual["checks"] == {"total": 2200, "passed": 2200, "failed": 0}
    assert visual["capture_records"] == 48
    assert visual["matrix"]["total_pngs_per_run"] == 144
    assert len(visual["matrix"]["keys"]) == 48
    assert visual["approval"]["status"] == "approved-check"
    assert visual["approval"]["reference"].startswith("user:")
    assert visual["browser"]["version"] == "HeadlessChrome/153.0.8010.0"
    assert len(visual["browser"]["fonts"]) == 3
    assert visual["external_provider_calls"] is False
    approval = contract.read_json(contract.APPROVAL_PATH)
    assert approval["human_approval"]["decision"] == "approved"
    assert approval["human_approval"]["reference"] == visual["approval"]["reference"]


def test_compare_restore_defect_is_closed_by_real_walk_and_boot_guard(payload: dict) -> None:
    compare = payload["compare_recovery"]
    assert (compare["checks"], compare["passed"], compare["failed"]) == (15, 15, 0)
    assert len(compare["captures"]) == 6
    assert {row["path"] for row in compare["captures"]} == {
        "compare-record-live.png", "compare-record-restored.png", "compare-record-server.png",
        "compare-record-forgotten.png", "compare-record-narrow.png", "compare-record-light.png",
    }
    boot_tests = (ROOT / "tests" / "test_frontend_boot_order.py").read_text(encoding="utf-8")
    assert "test_ui_status_table_is_declared_above_the_debate_boot_path" in boot_tests
    assert "test_stored_comparison_restore_cannot_abort_the_boot" in boot_tests


def test_ci_evidence_contains_every_required_successful_job(payload: dict) -> None:
    ci = payload["critical_ci"]
    assert ci["conclusion"] == "success"
    assert contract._is_commit(ci["headSha"])
    assert ci["headSha"] == payload["pinned_release"]["implementation_head"]
    assert {row["name"] for row in ci["jobs"]} == contract.REQUIRED_CI_JOBS
    assert all(row["conclusion"] == "success" and row["status"] == "completed" for row in ci["jobs"])


def test_issue_185_cannot_close_from_the_historical_scope(payload: dict) -> None:
    correction = next(row for row in payload["corrections"] if row["id"] == "issue-185-harness-view-cache-wording")
    if evidence()["review_scope"] == contract.REVIEW_SCOPE_INTEGRATED:
        assert correction["status"] == "implementation-complete-evidence-refresh-pending"
        assert "must not be closed" in correction["detail"]
        assert "**Post-handoff #185 evidence is still pending.**" in contract.render_doc(payload)
    else:
        assert correction["status"] == "resolved-post-handoff"
        assert payload["pinned_release"]["current_browser_sources"] is True


def test_ci_teardown_failure_is_disclosed_and_process_group_cleanup_is_fixed(payload: dict) -> None:
    correction = next(row for row in payload["corrections"] if row["id"] == "uiv2-30-fixture-process-group")
    assert correction["status"] == "corrected"
    assert "42458307474" in correction["detail"]
    assert "42458278773" in correction["detail"]
    assert "was not waived" in correction["detail"]
    source = (ROOT / "scripts" / "preservation_suite.py").read_text(encoding="utf-8")
    assert "pgid = process.pid" in source
    assert "os.killpg(pgid, 0)" in source
    assert "survived SIGKILL" in source
    tests = (ROOT / "tests" / "test_uiux_preservation_e2e.py").read_text(encoding="utf-8")
    assert 'ids=["graceful", "ignored-sigterm"]' in tests
    assert "signal.SIG_IGN" in tests


def test_uiv2_29_final_summary_uses_actual_metrics_not_flat_transcription(payload: dict) -> None:
    memory = payload["measured_performance"]["memory"]
    nodes = [memory[key]["Nodes"] for key in ("baseline", "after_cycle_1", "after_cycle_2")]
    # Node counts are deterministic for a given markup, so the plateau is pinned rather than
    # derived: UIV2-P7-07's single-row header and Settings overlay moved it from 8481 to 8544
    # without repeated-cycle growth. The summary must quote the newest committed run.
    assert nodes == [8544, 8544, 8544]
    tracking = next(story for story in roadmap()["stories"] if story["id"] == "UIV2-29")["tracking"]
    final = [row for row in tracking["tests_executed"] if row.startswith("Final full-window timing")][-1]
    assert "8544 -> 8544 -> 8544" in final
    # The heap figures vary run to run, so the property worth asserting is that the ledger
    # quotes *the committed run*, not one particular sample. Hardcoding a sample meant any
    # legitimate regeneration of the UIV2-29 record failed this test for no real reason.
    heap = " -> ".join(str(memory[key]["JSHeapUsedSize"]) for key in ("baseline", "after_cycle_1", "after_cycle_2"))
    assert heap in final
    assert "flat at 8481" not in " ".join(tracking["acceptance_evidence"])
    assert contract.roadmap_findings(roadmap()) == []


def test_pending_ci_regression_is_closed_by_the_real_merged_run(payload: dict) -> None:
    tracking = next(story for story in roadmap()["stories"] if story["id"] == "UIV2-26")["tracking"]
    assert tracking["commit_sha"] == "289a4fbc7348fd7410ee823b1dbd8322745d9d74"
    statuses = [row["status"] for row in tracking["known_regressions"] if isinstance(row, dict)]
    assert "closed_in_merged_ci_42454692827" in statuses
    assert not any("pending" in status for status in statuses)
    correction = next(row for row in payload["corrections"] if row["id"] == "uiv2-26-pending-ci")
    assert "703945e" in correction["detail"]
    assert "CI run passed all three Python versions and Browser safety regressions" in correction["detail"]


def test_readme_is_linked_truthful_and_free_of_release_overclaims(payload: dict) -> None:
    text = contract.README_PATH.read_text(encoding="utf-8")
    assert contract.readme_findings(text) == []
    assert "docs/uiv2-v2-release-review.md" in text
    assert "not a production security approval" in text
    assert "production security release remains blocked" in text
    assert contract.DOC_PATH.name in contract.DOCS_INDEX_PATH.read_text(encoding="utf-8")
    for row in payload["readme_claims"]:
        assert row["verified"] is True


def test_open_followups_and_rollback_are_not_hidden(payload: dict) -> None:
    followups = {row["id"]: row for row in payload["follow_ups"]}
    assert followups["chat-post-tool-empty-completion"]["status"] == "open-pre-existing"
    assert followups["clipboard-rejection-parity"]["status"] == "open-pre-existing"
    assert followups["mcp-activity-volatility"]["status"] == "open-documented-limitation"
    assert followups["visual-evidence-scope"]["status"] == "accepted-not-a-waiver"
    assert contract.followup_findings() == []
    rollback = " ".join(payload["rollback"].values())
    assert "without deleting sessions, database state, MCP secrets or workspaces" in rollback
    assert "does not roll back UIV2-29" in rollback
    assert "Do not git reset" in rollback
    assert "no UIV2-31" in payload["next_work"][-1]


def test_failed_or_partial_compare_run_is_rejected() -> None:
    failed = {"checks": [{"passed": False, "status": "fail"}] * 15, "passed": 14, "failed": 1}
    assert contract.validate_compare_run(failed)
    partial = {"checks": [{"passed": True, "status": "pass"}] * 14, "passed": 14, "failed": 0}
    assert contract.validate_compare_run(partial)


def test_failed_missing_or_unpinned_ci_job_is_rejected() -> None:
    failed = copy.deepcopy(evidence()["ci"])
    failed["jobs"][0]["conclusion"] = "failure"
    assert contract.validate_ci_run(failed)
    missing = copy.deepcopy(evidence()["ci"])
    missing["jobs"] = missing["jobs"][:-1]
    assert contract.validate_ci_run(missing)
    unpinned = copy.deepcopy(evidence()["ci"])
    unpinned["headSha"] = "short"
    assert contract.validate_ci_run(unpinned)


def test_visual_run_requires_real_approval_and_all_48_capture_records() -> None:
    raw = {
        "story": "UIV2-26", "mode": "approved-check", "complete": True,
        "checks": {"total": 2200, "passed": 2200, "failed": 0}, "failures": [],
        "matrix": {**evidence()["visual"]["matrix"]},
        "captures": [{"images": {"actual": {}, "reference": {}, "comparison": {}}} for _ in range(48)],
        "approval": evidence()["visual"]["approval"],
        "git": evidence()["visual"]["git"],
        "contract_sha256": evidence()["visual"]["contract_sha256"],
        "wireframe_sha256": evidence()["visual"]["wireframe_sha256"],
    }
    assert contract.validate_visual_run(raw) == []
    unapproved = copy.deepcopy(raw)
    unapproved["approval"]["reference"] = "agent:self-approved"
    assert contract.validate_visual_run(unapproved)
    partial = copy.deepcopy(raw)
    partial["captures"] = partial["captures"][:-1]
    assert contract.validate_visual_run(partial)
    failed = copy.deepcopy(raw)
    failed["checks"]["failed"] = 1
    assert contract.validate_visual_run(failed)


def test_release_evidence_rejects_stale_sources_dirty_tree_and_missing_captures() -> None:
    stale = evidence()
    stale["source_sha256"]["static/index.html"] = "0" * 64
    assert any("stale" in error for error in contract.validate_evidence(stale))
    dirty = evidence()
    dirty["visual"]["git"]["dirty"] = True
    assert any("clean" in error for error in contract.validate_evidence(dirty))
    missing = evidence()
    missing["compare"]["captures"] = missing["compare"]["captures"][:-1]
    assert any("six captures" in error for error in contract.validate_evidence(missing))
    fonts = evidence()
    fonts["visual"]["browser"]["fonts"] = {}
    assert any("font" in error for error in contract.validate_evidence(fonts))


def test_release_evidence_rejects_scope_relabeling_and_wrong_integrated_head() -> None:
    relabeled = copy.deepcopy(evidence())
    relabeled["review_scope"] = (
        contract.REVIEW_SCOPE_CURRENT
        if evidence()["review_scope"] == contract.REVIEW_SCOPE_INTEGRATED
        else contract.REVIEW_SCOPE_INTEGRATED
    )
    assert contract.validate_evidence(relabeled)
    wrong_head = copy.deepcopy(evidence())
    wrong_head["review_scope"] = contract.REVIEW_SCOPE_INTEGRATED
    wrong_head["visual"]["git"]["head"] = "0" * 40
    assert any("integrated" in error or "cannot read" in error for error in contract.validate_evidence(wrong_head))


def test_release_evidence_rejects_lost_approval_failed_visual_or_ci() -> None:
    approval = evidence()
    approval["visual"]["approval"]["reference"] = "agent:inferred"
    assert any("approval" in error for error in contract.validate_evidence(approval))
    visual = evidence()
    visual["checks"] = {}
    visual["visual"]["checks"]["failed"] = 1
    assert contract.validate_evidence(visual)
    ci = evidence()
    ci["ci"]["conclusion"] = "failure"
    assert any("CI" in error for error in contract.validate_evidence(ci))
    mismatched = evidence()
    mismatched["ci"]["headSha"] = "0" * 40
    assert any("same implementation HEAD" in error for error in contract.validate_evidence(mismatched))


def test_roadmap_gate_rejects_unpinned_dependency_pending_status_and_bad_metrics() -> None:
    unpinned = roadmap()
    next(story for story in unpinned["stories"] if story["id"] == "UIV2-26")["tracking"]["commit_sha"] = "289a4fb"
    assert any(row["check"] == "dependency" for row in contract.roadmap_findings(unpinned))
    pending = roadmap()
    regression = next(story for story in pending["stories"] if story["id"] == "UIV2-26")["tracking"]["known_regressions"][0]
    regression["status"] = "mitigated_pending_final_ci"
    assert any(row["check"] == "stale-pending-regression" for row in contract.roadmap_findings(pending))
    wrong = roadmap()
    tracking = next(story for story in wrong["stories"] if story["id"] == "UIV2-29")["tracking"]
    tracking["acceptance_evidence"][0] = "post-GC nodes are flat at 8481"
    assert any(row["check"] == "metric-summary" for row in contract.roadmap_findings(wrong))


def test_readme_gate_rejects_missing_scope_and_production_overclaims() -> None:
    text = contract.README_PATH.read_text(encoding="utf-8")
    assert contract.readme_findings(text.replace("not a production security approval", "a release note"))
    assert contract.readme_findings(text + "\nThis build is production-ready.\n")
    assert contract.readme_findings(text + "\nAll tests pass on every machine.\n")


def test_scope_map_owns_release_review() -> None:
    scopes = json.loads((ROOT / "tests" / "test_scopes.json").read_text(encoding="utf-8"))
    assert "test_uiv2_release_review.py" in scopes["areas"]["ui.shared"]
    assert "test_uiv2_release_review.py" in scopes["areas"]["quality.docs-ci"]
