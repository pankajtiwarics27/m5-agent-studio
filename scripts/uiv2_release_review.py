#!/usr/bin/env python3
"""UIV2-30: fail-closed UI/UX V2 release review and agent handoff.

    python3 scripts/uiv2_release_review.py --import-evidence \
      --compare-run /tmp/uiv2-30/compare.json \
      --visual-run /tmp/uiv2-30/visual/check-manifest.json \
      --ci-run /tmp/uiv2-30/ci.json
    python3 scripts/uiv2_release_review.py
    python3 scripts/uiv2_release_review.py --check

The importer records real browser and CI outcomes; it never creates approval or
turns a failed/partial run into evidence. The generator then executes the
source-bound contracts and renders one scoped decision: UI/UX V2 handoff may be
ready while the independent production/security release remains blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uiv2_release_review.py"
ROADMAP_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
EVIDENCE_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Release_Evidence.json"
JSON_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Release_Review.json"
DOC_PATH = ROOT / "docs" / "uiv2-v2-release-review.md"
README_PATH = ROOT / "README.md"
DOCS_INDEX_PATH = ROOT / "docs" / "README.md"
CROSS_TAB_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Reference.json"
APPROVAL_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Approval.json"
PERFORMANCE_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Performance_Safety.json"
PERFORMANCE_RUN_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Performance_Run.json"
BACKEND_RUN_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Backend_Safety_Run.json"
ACCESSIBILITY_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Accessibility_Adversarial.json"
PRESERVATION_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Preservation_Matrix.json"
PRESERVATION_E2E_PATH = ROOT / "product" / "M5_2_0_UIUX_Preservation_E2E.json"
OLD_RELEASE_GATE_PATH = ROOT / "product" / "M5_2_0_UIUX_Release_Gate.json"
STATIC_PATH = ROOT / "static" / "index.html"
COMPARE_WALK_PATH = ROOT / "scripts" / "compare_continuity_walk.mjs"
VISUAL_WALK_PATH = ROOT / "scripts" / "uiux_v2_cross_tab_reference_walk.mjs"

STORY = "UIV2-30"
DEPENDENCIES = ("UIV2-26", "UIV2-27", "UIV2-28", "UIV2-29")
REQUIRED_CI_JOBS = {"test (3.11)", "test (3.12)", "test (3.13)", "Browser safety regressions"}
EVIDENCE_SOURCES = (
    "static/index.html",
    "scripts/compare_continuity_walk.mjs",
    "scripts/uiux_v2_cross_tab_reference_walk.mjs",
)
LOCAL_CHECKS = (
    ("performance-safety", "scripts/uiv2_performance_safety.py", "--check"),
    ("accessibility", "scripts/uiv2_accessibility_adversarial.py", "--check"),
    ("approved-visual-contract", "scripts/uiux_v2_cross_tab_reference.py", "--check"),
    ("preservation-map", "scripts/uiux_preservation_e2e.py", "--check"),
    ("readme-onboarding", "scripts/uiux_docs_verification.py", "--check"),
    ("documentation-links", "scripts/validate_docs.py",),
)
README_REQUIRED = (
    "[UI/UX V2 release review](docs/uiv2-v2-release-review.md)",
    "not a production security approval",
    "not container-isolation qualification",
    "production security release remains blocked",
)
README_FORBIDDEN = (
    "production-ready",
    "fully accessible",
    "all tests pass",
    "pixel-perfect",
    "qualified container isolation",
)


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_source_hashes() -> dict[str, str]:
    return {name: sha256(ROOT / name) for name in EVIDENCE_SOURCES}


def cross_tab_fingerprint(payload: dict[str, Any]) -> str:
    identity = {
        "schema_version": payload["schema_version"],
        "story": payload["story"],
        "matrix": payload["matrix"],
        "layout_contract": payload["layout_contract"],
        "panes": payload["panes"],
        "approved_reference": payload["approved_reference"],
    }
    return hashlib.sha256(dumps(identity).encode("utf-8")).hexdigest()


def _is_sha(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value)))


def _is_commit(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", str(value)))


def validate_compare_run(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        return ["Compare continuity run must be an object with checks"]
    checks = payload["checks"]
    errors = []
    if len(checks) != 15 or payload.get("passed") != 15 or payload.get("failed") != 0:
        errors.append("Compare continuity must report 15 passed and zero failed")
    if any(row.get("passed") is not True or row.get("status") != "pass" for row in checks):
        errors.append("Every Compare continuity check must have passed")
    return errors


def validate_visual_run(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["visual manifest is not an object"]
    errors = []
    if payload.get("story") != "UIV2-26" or payload.get("mode") != "approved-check" or payload.get("complete") is not True:
        errors.append("visual run must be a complete UIV2-26 approved-check")
    if payload.get("checks") != {"total": 2200, "passed": 2200, "failed": 0} or payload.get("failures") != []:
        errors.append("approved visual run must pass 2200/2200 checks")
    matrix = payload.get("matrix", {})
    if matrix.get("keys") is None or len(matrix.get("keys", [])) != 48 or len(set(matrix.get("keys", []))) != 48:
        errors.append("approved visual run must contain the complete 48-variant matrix")
    if (matrix.get("live_captures"), matrix.get("reference_captures"), matrix.get("annotated_comparisons"), matrix.get("total_pngs_per_run")) != (48, 48, 48, 144):
        errors.append("approved visual run must account for all 144 PNGs")
    captures = payload.get("captures")
    if not isinstance(captures, list) or len(captures) != 48:
        errors.append("approved visual run must contain 48 capture records")
    else:
        for row in captures:
            if set(row.get("images", {})) != {"actual", "reference", "comparison"}:
                errors.append("every visual variant needs actual/reference/comparison images")
                break
    approval = payload.get("approval", {})
    if approval.get("status") != "approved-check" or not str(approval.get("reference", "")).startswith("user:"):
        errors.append("visual run must bind an explicit user approval")
    git = payload.get("git", {})
    if not _is_commit(git.get("head")) or git.get("dirty") is not False:
        errors.append("visual run must be bound to a clean 40-character Git HEAD")
    if not _is_sha(payload.get("contract_sha256")) or not _is_sha(payload.get("wireframe_sha256")):
        errors.append("visual run must bind contract and wireframe digests")
    return errors


def validate_ci_run(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["CI run is not an object"]
    errors = []
    if payload.get("status") != "completed" or payload.get("conclusion") != "success" or not _is_commit(payload.get("headSha")):
        errors.append("CI run must be completed, successful and source-pinned")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return [*errors, "CI jobs are missing"]
    by_name = {row.get("name"): row for row in jobs}
    if set(by_name) != REQUIRED_CI_JOBS:
        errors.append(f"CI jobs must be exactly {sorted(REQUIRED_CI_JOBS)}")
    for name in REQUIRED_CI_JOBS:
        row = by_name.get(name, {})
        if row.get("status") != "completed" or row.get("conclusion") != "success":
            errors.append(f"CI job did not pass: {name}")
    if not str(payload.get("url", "")).startswith("https://github.com/"):
        errors.append("CI run needs its GitHub URL")
    return errors


def import_evidence(compare_path: Path, visual_path: Path, ci_path: Path) -> dict[str, Any]:
    compare = read_json(compare_path)
    visual = read_json(visual_path)
    ci = read_json(ci_path)
    errors = [*validate_compare_run(compare), *validate_visual_run(visual), *validate_ci_run(ci)]
    if errors:
        raise ValueError("; ".join(errors))
    contract = read_json(CROSS_TAB_PATH)
    approval = read_json(APPROVAL_PATH)
    if visual["contract_sha256"] != cross_tab_fingerprint(contract) or visual["contract_sha256"] != approval["contract_sha256"]:
        raise ValueError("visual contract fingerprint does not match the current approved contract")
    if visual["wireframe_sha256"] != approval["wireframe_sha256"]:
        raise ValueError("visual wireframe digest does not match the approval")
    if visual["approval"]["reference"] != approval["human_approval"]["reference"]:
        raise ValueError("visual approval reference does not match the committed human approval")
    capture_root = compare_path.parent / compare_path.stem
    if not capture_root.is_dir():
        capture_root = compare_path.parent
    compare_captures = [
        {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
        for path in sorted(capture_root.glob("*.png"))
    ]
    if len(compare_captures) != 6:
        raise ValueError(f"Compare continuity must have six actual PNG captures, found {len(compare_captures)}")
    browser = visual.get("browser", {})
    payload = {
        "schema_version": 1,
        "document_type": "m5_uiv2_release_evidence",
        "story": STORY,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": evidence_source_hashes(),
        "compare": {
            "run": str(compare_path),
            "run_sha256": sha256(compare_path),
            "script": "scripts/compare_continuity_walk.mjs",
            "checks": 15,
            "passed": 15,
            "failed": 0,
            "captures": compare_captures,
        },
        "visual": {
            "run": str(visual_path),
            "manifest_sha256": sha256(visual_path),
            "git": visual["git"],
            "contract_sha256": visual["contract_sha256"],
            "wireframe_sha256": visual["wireframe_sha256"],
            "approval": visual["approval"],
            "matrix": visual["matrix"],
            "checks": visual["checks"],
            "capture_records": len(visual["captures"]),
            "browser": {
                "version": browser.get("version"),
                "fonts": browser.get("fonts", {}).get("files", {}),
            },
            "external_provider_calls": visual.get("demo", {}).get("external_provider_calls"),
        },
        "ci": {
            "headSha": ci["headSha"],
            "status": ci["status"],
            "conclusion": ci["conclusion"],
            "url": ci["url"],
            "createdAt": ci.get("createdAt"),
            "updatedAt": ci.get("updatedAt"),
            "jobs": [
                {key: row.get(key) for key in ("name", "status", "conclusion", "url")}
                for row in ci["jobs"]
            ],
        },
    }
    remaining = validate_evidence(payload)
    if remaining:
        raise ValueError("; ".join(remaining))
    EVIDENCE_PATH.write_text(dumps(payload), encoding="utf-8")
    return payload


def validate_evidence(payload: Any, hashes: dict[str, str] | None = None) -> list[str]:
    if not isinstance(payload, dict):
        return ["release evidence is not an object"]
    errors = []
    if payload.get("story") != STORY or not payload.get("recorded_at"):
        errors.append("release evidence story/recorded_at is missing")
    if payload.get("source_sha256") != (hashes if hashes is not None else evidence_source_hashes()):
        errors.append("release evidence is stale for the browser sources")
    compare = payload.get("compare", {})
    if (compare.get("checks"), compare.get("passed"), compare.get("failed")) != (15, 15, 0):
        errors.append("release Compare evidence must be 15/15")
    if not _is_sha(compare.get("run_sha256")):
        errors.append("Compare run digest is missing")
    captures = compare.get("captures")
    if not isinstance(captures, list) or len(captures) != 6:
        errors.append("Compare evidence needs six captures")
    elif any(not str(row.get("path", "")).endswith(".png") or not _is_sha(row.get("sha256")) or row.get("bytes", 0) <= 0 for row in captures):
        errors.append("Compare captures need PNG paths, hashes and sizes")
    visual = payload.get("visual", {})
    if visual.get("checks") != {"total": 2200, "passed": 2200, "failed": 0} or visual.get("capture_records") != 48:
        errors.append("release visual evidence must be 2200/2200 with 48 capture records")
    if visual.get("matrix", {}).get("total_pngs_per_run") != 144 or not _is_sha(visual.get("manifest_sha256")):
        errors.append("release visual evidence must account for 144 PNGs and its manifest")
    approval = visual.get("approval", {})
    if approval.get("status") != "approved-check" or not str(approval.get("reference", "")).startswith("user:"):
        errors.append("release visual evidence lacks explicit user approval")
    git = visual.get("git", {})
    if not _is_commit(git.get("head")) or git.get("dirty") is not False:
        errors.append("release visual evidence must be clean and source-pinned")
    if visual.get("external_provider_calls") is not False or not visual.get("browser", {}).get("version"):
        errors.append("release visual environment/provider accounting is missing")
    if len(visual.get("browser", {}).get("fonts", {})) != 3:
        errors.append("release visual evidence must pin all three font files")
    ci = payload.get("ci", {})
    if ci.get("status") != "completed" or ci.get("conclusion") != "success" or not _is_commit(ci.get("headSha")):
        errors.append("release CI evidence is not successful and source-pinned")
    if ci.get("headSha") != git.get("head"):
        errors.append("release CI and approved visual evidence must pin the same implementation HEAD")
    jobs = ci.get("jobs", [])
    by_name = {row.get("name"): row for row in jobs}
    if set(by_name) != REQUIRED_CI_JOBS or any(row.get("status") != "completed" or row.get("conclusion") != "success" for row in jobs):
        errors.append("release CI evidence must contain all four successful jobs")
    return errors


def run_local_checks() -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    rows = []
    findings = []
    security: dict[str, Any] = {}
    for name, script, *args in LOCAL_CHECKS:
        completed = subprocess.run(
            [sys.executable, script, *args], cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        rows.append({
            "name": name,
            "command": f"python3 {script}" + (" " + " ".join(args) if args else ""),
            "exit_code": completed.returncode,
            "failure_output": output.splitlines()[-1] if completed.returncode and output else None,
        })
        if completed.returncode:
            findings.append({"check": name, "detail": f"exit {completed.returncode}: {output.splitlines()[-1] if output else 'no output'}"})
    completed = subprocess.run(
        [sys.executable, "scripts/security_release_gate.py", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
    )
    try:
        security = json.loads(completed.stdout)
    except json.JSONDecodeError:
        security = {}
    rows.append({
        "name": "production-security-gate",
        "command": "python3 scripts/security_release_gate.py --json",
        "exit_code": completed.returncode,
        "failure_output": completed.stderr.splitlines()[-1] if completed.returncode and completed.stderr else None,
    })
    if completed.returncode or security.get("gate_invariants_satisfied") is not True:
        findings.append({"check": "production-security-gate", "detail": "security invariants could not be executed"})
    if security.get("release_ready") is not False or security.get("release_decision") != "release-blocked" or not security.get("blocking_reasons"):
        findings.append({"check": "production-release-scope", "detail": "production release must remain explicitly blocked pending independent signoff"})
    return rows, security, findings


def roadmap_findings(document: dict[str, Any]) -> list[dict[str, str]]:
    findings = []
    stories = document.get("stories", [])
    by_id = {story.get("id"): story for story in stories}
    if len(stories) != 30 or set(by_id) != {f"UIV2-{index:02d}" for index in range(1, 31)}:
        findings.append({"check": "roadmap-shape", "detail": "V2 roadmap must contain UIV2-01 through UIV2-30"})
    for story_id in DEPENDENCIES:
        story = by_id.get(story_id, {})
        tracking = story.get("tracking", {})
        if tracking.get("status") != "done" or not _is_commit(tracking.get("commit_sha")):
            findings.append({"check": "dependency", "detail": f"{story_id} is not done at a pinned commit"})
        for field in ("changed_files", "tests_executed", "visual_evidence", "acceptance_evidence"):
            if not tracking.get(field):
                findings.append({"check": "dependency-evidence", "detail": f"{story_id} lacks {field}"})
        if tracking.get("blockers"):
            findings.append({"check": "dependency-blocker", "detail": f"{story_id} still has blockers"})
    if by_id.get(STORY, {}).get("tracking", {}).get("status") not in {"in_progress", "done"}:
        findings.append({"check": "story-started", "detail": "UIV2-30 must be in progress or done, never prematurely skipped"})
    statuses = [story.get("tracking", {}).get("status") for story in stories]
    expected = {
        "total": 30,
        "done": statuses.count("done"),
        "in_progress": statuses.count("in_progress"),
        "blocked": statuses.count("blocked"),
        "not_started": statuses.count("not_started"),
        "percent_complete": round(100 * statuses.count("done") / 30),
    }
    if document.get("progress") != expected or expected["blocked"]:
        findings.append({"check": "roadmap-counters", "detail": f"progress must be {expected} with no blocked story"})
    for story in stories:
        for regression in story.get("tracking", {}).get("known_regressions", []):
            if isinstance(regression, dict) and "pending" in str(regression.get("status", "")):
                findings.append({"check": "stale-pending-regression", "detail": f"{story['id']}: {regression['status']}"})
    performance = read_json(PERFORMANCE_RUN_PATH)
    memory = performance["metrics"]["harness"]["memory"]
    nodes = " -> ".join(str(memory[key]["Nodes"]) for key in ("baseline", "after_cycle_1", "after_cycle_2"))
    heap = " -> ".join(str(memory[key]["JSHeapUsedSize"]) for key in ("baseline", "after_cycle_1", "after_cycle_2"))
    tracking = by_id.get("UIV2-29", {}).get("tracking", {})
    final = next((row for row in tracking.get("tests_executed", []) if row.startswith("Final full-window timing")), "")
    acceptance = " ".join(tracking.get("acceptance_evidence", []))
    if nodes not in final or heap not in final:
        findings.append({"check": "metric-summary", "detail": f"UIV2-29 final test summary must contain nodes {nodes} and heap {heap}"})
    if nodes not in acceptance or heap not in acceptance or "flat at 8481" in acceptance:
        findings.append({"check": "metric-summary", "detail": "UIV2-29 acceptance summary repeats the corrected node transcription"})
    return findings


def artifact_findings() -> list[dict[str, str]]:
    findings = []
    contract = read_json(CROSS_TAB_PATH)
    approval = read_json(APPROVAL_PATH)
    performance = read_json(PERFORMANCE_PATH)
    backend = read_json(BACKEND_RUN_PATH)
    accessibility = read_json(ACCESSIBILITY_PATH)
    preservation = read_json(PRESERVATION_PATH)
    preservation_e2e = read_json(PRESERVATION_E2E_PATH)
    old_gate = read_json(OLD_RELEASE_GATE_PATH)
    static_hash = sha256(STATIC_PATH)
    if contract.get("source", {}).get("static_sha256") != static_hash or contract.get("findings"):
        findings.append({"check": "visual-source", "detail": "approved cross-tab contract is stale or has findings"})
    human = approval.get("human_approval", {})
    if human.get("decision") != "approved" or not str(human.get("reference", "")).startswith("user:") or approval.get("verification") != {"checks_total": 2104, "checks_passed": 2104, "failures": 0, "captures": 48}:
        findings.append({"check": "visual-approval", "detail": "explicit human structural approval is missing or incomplete"})
    if performance.get("findings") or performance.get("browser_evidence", {}).get("totals") != {"checks": 21, "passed": 21, "failed": 0, "captures": 11}:
        findings.append({"check": "performance", "detail": "UIV2-29 browser contract is not green"})
    if performance.get("source_sha256", {}).get("static/index.html") != static_hash:
        findings.append({"check": "performance-source", "detail": "UIV2-29 browser evidence is stale"})
    if backend.get("totals", {}).get("tests_passed", 0) < 300 or backend.get("totals", {}).get("failed") != 0:
        findings.append({"check": "backend", "detail": "backend safety run is missing or failed"})
    isolation = backend.get("isolation", {})
    if isolation.get("status") != "unqualified" or isolation.get("dependent_execution") != "blocked (fail closed)":
        findings.append({"check": "isolation", "detail": "this environment's unprobed isolation must remain unqualified and blocked"})
    if accessibility.get("findings") or accessibility.get("evidence", {}).get("totals") != {"checks": 120, "passed": 120, "failed": 0, "captures": 62}:
        findings.append({"check": "accessibility", "detail": "UIV2-28 accessibility evidence is not green"})
    if preservation.get("totals", {}).get("controls") != 272 or preservation.get("totals", {}).get("mounted_routes") != 121:
        findings.append({"check": "preservation", "detail": "six-pane control/route inventory drifted"})
    if preservation_e2e.get("totals", {}).get("matrix_rows") != 21 or preservation_e2e.get("totals", {}).get("mapped_rows") != 21 or preservation_e2e.get("findings"):
        findings.append({"check": "preservation-e2e", "detail": "all 21 preservation rows must be mapped without findings"})
    if old_gate.get("findings") or old_gate.get("totals", {}).get("checks_passed") != 37 or old_gate.get("totals", {}).get("gates_met") != 7:
        findings.append({"check": "old-release-gate", "detail": "the 40-story UI/UX gate must remain 37/37 and 7/7"})
    return findings


def readme_findings(text: str) -> list[dict[str, str]]:
    findings = []
    normalized = re.sub(r"\s+", " ", text)
    for token in README_REQUIRED:
        if token not in normalized:
            findings.append({"check": "readme-claim", "detail": f"missing release-scope token: {token}"})
    lowered = normalized.lower()
    for token in README_FORBIDDEN:
        if token in lowered:
            findings.append({"check": "readme-overclaim", "detail": f"unverified README claim: {token}"})
    if DOC_PATH.name not in DOCS_INDEX_PATH.read_text(encoding="utf-8"):
        findings.append({"check": "readme-index", "detail": "release review is not published in docs/README.md"})
    return findings


def followup_findings() -> list[dict[str, str]]:
    findings = []
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    html = STATIC_PATH.read_text(encoding="utf-8")
    post_tool = app[app.index("            final_text = ", app.index("tool_resp_blocks.append")):app.index("        except Exception as exc:", app.index("tool_resp_blocks.append"))]
    if 'final_text = "".join(final_content_acc).strip()' not in post_tool or 'yield _sse("done"' not in post_tool or "if not final_text" in post_tool:
        findings.append({"check": "chat-post-tool-followup", "detail": "the recorded empty-post-tool completion follow-up changed and needs re-review"})
    if "function copyStageMarkdown(s, btn) { navigator.clipboard.writeText" not in html:
        findings.append({"check": "clipboard-followup", "detail": "Pipeline clipboard rejection handling changed and needs release-note review"})
    if "MCP_ACTIVITY_CAP = 12" not in html:
        findings.append({"check": "mcp-activity-followup", "detail": "MCP Activity volatility limitation changed and needs release-note review"})
    return findings


def build() -> dict[str, Any]:
    roadmap = read_json(ROADMAP_PATH)
    evidence = read_json(EVIDENCE_PATH)
    performance = read_json(PERFORMANCE_RUN_PATH)
    local_checks, security, findings = run_local_checks()
    findings = [
        *findings,
        *({"check": "release-evidence", "detail": error} for error in validate_evidence(evidence)),
        *roadmap_findings(roadmap),
        *artifact_findings(),
        *readme_findings(README_PATH.read_text(encoding="utf-8")),
        *followup_findings(),
    ]
    visual = evidence["visual"]
    ci = evidence["ci"]
    statuses = [story["tracking"]["status"] for story in roadmap["stories"]]
    memory = performance["metrics"]["harness"]["memory"]
    steady = performance["metrics"]["harness"]["steady"]
    fast = performance["metrics"]["chat"]["fast"]
    decision = "ready" if not findings else "blocked"
    payload = {
        "schema_version": 1,
        "document_type": "m5_uiv2_release_review",
        "story": STORY,
        "generated_by": "scripts/uiv2_release_review.py",
        "regenerate_with": "python3 scripts/uiv2_release_review.py",
        "check_with": "python3 scripts/uiv2_release_review.py --check",
        "statement": "UI/UX V2 handoff is evidence-gated; this review never grants production security approval or container-isolation qualification.",
        "decision": {
            "uiux_v2_handoff": decision,
            "production_security_release": security.get("release_decision", "unknown"),
            "scope": "merge/handoff of the 30-story UI/UX V2 program only; tagging/publishing a production release remains under scripts/security_release_gate.py and scripts/release.py",
        },
        "pinned_release": {
            "implementation_head": visual["git"]["head"],
            "dirty": visual["git"]["dirty"],
            "static_sha256": evidence["source_sha256"]["static/index.html"],
            "evidence_recorded_at": evidence["recorded_at"],
            "note": "The evidence commit may contain only later release docs/tracker changes; executable browser sources are bound by digest.",
        },
        "program_state": {
            "total": 30,
            "done": statuses.count("done"),
            "in_progress": statuses.count("in_progress"),
            "blocked": statuses.count("blocked"),
            "not_started": statuses.count("not_started"),
            "percent_complete": round(100 * statuses.count("done") / 30),
        },
        "critical_ci": ci,
        "local_checks": local_checks,
        "security_release": {
            "release_ready": security.get("release_ready"),
            "release_decision": security.get("release_decision"),
            "gate_invariants_satisfied": security.get("gate_invariants_satisfied"),
            "blocking_reasons": security.get("blocking_reasons", []),
            "container_isolation_qualified": security.get("acceptance_criteria", {}).get("container_isolation_qualified"),
        },
        "visual_evidence": visual,
        "compare_recovery": evidence["compare"],
        "measured_performance": {
            "environment": performance["environment"],
            "fast_sse": fast,
            "steady_frame": steady,
            "memory": memory,
            "backend_tests": read_json(BACKEND_RUN_PATH)["totals"],
            "local_isolation": read_json(BACKEND_RUN_PATH)["isolation"],
        },
        "readme_claims": [
            {"claim": "Quickstart and documentation links resolve", "evidence": "scripts/uiux_docs_verification.py --check and scripts/validate_docs.py", "verified": True},
            {"claim": "UI/UX V2 review is scoped to merge/handoff, not production security approval", "evidence": "scripts/security_release_gate.py --json remains release-blocked", "verified": True},
            {"claim": "Visual conformity is structural, not a pixel-perfect or self-approved claim", "evidence": visual["approval"]["reference"], "verified": True},
            {"claim": "No new runtime behavior is asserted by UIV2-30", "evidence": "static/index.html and both browser walks match the imported source digests", "verified": True},
        ],
        "corrections": [
            {
                "id": "uiv2-29-node-transcription",
                "status": "corrected",
                "detail": "The roadmap previously copied flat 8481 nodes into the final summary; the actual committed run is 8505 -> 8484 -> 8505. The run remained within its no-growth budget; only the summary was wrong.",
            },
            {
                "id": "uiv2-26-pending-ci",
                "status": "closed",
                "detail": "The trusted-transformation mitigation is no longer pending: the merged 703945e CI run passed all three Python versions and Browser safety regressions.",
            },
            {
                "id": "compare-restore-history",
                "status": "verified-resolved",
                "detail": "Historical UIV2-16/17 restore-crash notes are not current behavior: the boot-order guard is present and this review's real Compare continuity walk passed 15/15 with six captures.",
            },
            {
                "id": "uiv2-30-fixture-process-group",
                "status": "corrected",
                "detail": "PR CI run 42458307474 exposed a Python 3.12 fixture-teardown race; the duplicate push run 42458278773 at the same SHA passed all four jobs, but the failure was not waived. preservation_suite.stop_server now retains the process-group id after parent exit, escalates surviving descendants to SIGKILL and waits for group exit, with synchronized graceful and SIGTERM-ignoring grandchild regressions.",
            },
            {
                "id": "uiv2-30-generated-source-manifest",
                "status": "corrected",
                "detail": "PR CI run 42459934879 passed Browser safety but failed all three Python jobs because the preservation helper edit had not been propagated through the UI source manifest, preservation signoff and release gate. Those owner artifacts were regenerated in dependency order; source-manifest, signoff, release-gate and release-review tests then passed, including the historical ancestry check after restoring the shallow clone's history.",
            },
        ],
        "follow_ups": [
            {
                "id": "chat-post-tool-empty-completion",
                "severity": "high-follow-up",
                "status": "open-pre-existing",
                "detail": "A provider that ends after a tool call without producing text can still reach done with an empty reply. Do not claim that provider-failure case is recovered; give it a dedicated backend/streaming regression story.",
                "evidence": "UIV2-20 tracking and app.py final_text/done path",
            },
            {
                "id": "clipboard-rejection-parity",
                "severity": "medium-follow-up",
                "status": "open-pre-existing",
                "detail": "Pipeline copyStageMarkdown and some other non-Chat copy helpers do not all handle clipboard promise rejection with Chat's user-visible parity.",
                "evidence": "UIV2-20 tracking and static/index.html copy helpers",
            },
            {
                "id": "mcp-activity-volatility",
                "severity": "low-follow-up",
                "status": "open-documented-limitation",
                "detail": "MCP Activity is an in-memory twelve-entry view, not durable per-call audit; failed connection tests and sync-note presentation are not represented uniformly.",
                "evidence": "UIV2-21/22 tracking and MCP_ACTIVITY_CAP = 12",
            },
            {
                "id": "visual-evidence-scope",
                "severity": "evidence-limitation",
                "status": "accepted-not-a-waiver",
                "detail": "Approval covers 48 structural comparisons, not pixel identity or every conceivable application state. Raw UIV2-30 PNGs are local review evidence; committed JSON binds their manifests and hashes.",
                "evidence": "product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json human_approval.scope",
            },
        ],
        "rollback": {
            "story": "Revert the UIV2-30 PR merge commit. This removes the release contract, importer, README pointers and corrected release notes without deleting sessions, database state, MCP secrets or workspaces.",
            "runtime": "UIV2-30 changes no frontend or backend runtime source. Rolling it back does not roll back UIV2-29 or any earlier approved UI behavior.",
            "whole_program": "Do not git reset or discard user data to roll back the V2 program. Revert individual story merge commits in reverse order with their owning evidence gates and reviewer approval.",
        },
        "next_work": [
            "Production release: obtain the independent AS-031 human security signoff and satisfy scripts/security_release_gate.py --enforce plus artifact-signing requirements; this agent cannot sign it.",
            "Runtime follow-up: reproduce and fix the empty post-tool Chat completion with an executed provider-failure stream, then prove no duplicate tool execution or false successful reply.",
            "UX follow-ups: bring non-Chat clipboard rejection handling to parity and decide whether MCP Activity needs durable audit rather than expanding the in-memory label.",
            "Roadmap: UIV2-30 is the final V2 story. Start no UIV2-31 by implication; write a new scoped roadmap for any follow-up work.",
        ],
        "totals": {
            "local_checks": len(local_checks),
            "local_checks_passed": sum(row["exit_code"] == 0 for row in local_checks),
            "ci_jobs": len(ci["jobs"]),
            "visual_checks": visual["checks"]["passed"],
            "visual_captures": visual["capture_records"],
            "compare_checks": evidence["compare"]["passed"],
            "compare_captures": len(evidence["compare"]["captures"]),
            "findings": len(findings),
        },
        "findings": findings,
    }
    return payload


def render_doc(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    pinned = payload["pinned_release"]
    visual = payload["visual_evidence"]
    performance = payload["measured_performance"]
    memory = performance["memory"]
    fast = performance["fast_sse"]
    steady = performance["steady_frame"]
    nodes = [memory[key]["Nodes"] for key in ("baseline", "after_cycle_1", "after_cycle_2")]
    heap = [memory[key]["JSHeapUsedSize"] for key in ("baseline", "after_cycle_1", "after_cycle_2")]
    state = payload["program_state"]
    lines = [
        "# UIV2-30 — release review, truthful README and agent handoff",
        "",
        "Generated by [`scripts/uiv2_release_review.py`](../scripts/uiv2_release_review.py) from real browser/CI evidence and executed source contracts. Do not hand-edit.",
        "",
        f"**UI/UX V2 handoff: `{decision['uiux_v2_handoff'].upper()}`. Production/security release: `{decision['production_security_release'].upper()}`.**",
        "",
        "The first decision covers only merge/handoff of the UI/UX V2 program. It is not production security approval, container-isolation qualification, permission to tag v1.0, or a pixel-perfect claim.",
        "",
        "## Pinned release and decision",
        "",
        "| Field | Verified value |",
        "| --- | --- |",
        f"| Implementation HEAD | `{pinned['implementation_head']}` (dirty=`{str(pinned['dirty']).lower()}`) |",
        f"| Frontend SHA-256 | `{pinned['static_sha256']}` |",
        f"| Roadmap at generation | {state['done']}/30 done, {state['in_progress']} in progress, {state['blocked']} blocked |",
        f"| Critical CI | [`{payload['critical_ci']['headSha']}`]({payload['critical_ci']['url']}) — {payload['critical_ci']['conclusion']} |",
        f"| Local executed checks | {payload['totals']['local_checks_passed']}/{payload['totals']['local_checks']} exit 0 |",
        f"| Production release | `{payload['security_release']['release_decision']}`; release_ready=`{str(payload['security_release']['release_ready']).lower()}` |",
        "",
        "The imported implementation HEAD and critical CI HEAD must match. A later evidence/tracker commit may differ only as the recorded handoff revision; executable browser sources remain pinned by SHA-256 and final PR CI must pass separately.",
        "",
        "### Production blockers preserved",
        "",
        *[f"- {reason}" for reason in payload["security_release"]["blocking_reasons"]],
        "",
        "## What the V2 program changed",
        "",
        "- Preserved six existing panes and their backend/session/workspace contracts while converging navigation, evidence, responsive behavior and recovery presentation.",
        "- UIV2-26 established the explicit owner-approved 48-variant structural visual baseline; UIV2-27 mapped all 21 preservation rows to executed provers.",
        "- UIV2-28 measured keyboard, contrast, zoom, reduced-motion and nested-scroll recovery; UIV2-29 added real-SSE, post-GC memory, layout and backend safety gates.",
        "- UIV2-30 changes release documentation and verification only. It does not change `app.py`, `static/index.html`, `studio.py`, API payloads, sessions or workspace data.",
        "",
        "## Actual screenshot and recovery proof",
        "",
        "| Evidence | Actual result | Binding |",
        "| --- | --- | --- |",
        f"| Approved structural matrix | {visual['checks']['passed']}/{visual['checks']['total']} checks; 48 variants / 144 PNGs | manifest `{visual['manifest_sha256']}` |",
        f"| Human approval | {visual['approval']['status']} | `{visual['approval']['reference']}` |",
        f"| Compare restore/export continuity | {payload['compare_recovery']['passed']}/15 checks; six PNGs | run `{payload['compare_recovery']['run_sha256']}` |",
        f"| Browser / fonts | {visual['browser']['version']} | three font digests recorded in the release evidence |",
        "",
        f"Local review manifests: `{visual['run']}` and `{payload['compare_recovery']['run']}`. PNGs are not committed; their hashes, source digests and actual outcomes are. Approval is structural fidelity, not pixel identity or self-certification.",
        "",
        "### Compare captures",
        "",
        *[f"- `{row['path']}` — `{row['sha256']}` ({row['bytes']} bytes)" for row in payload["compare_recovery"]["captures"]],
        "",
        "## Critical checks that actually ran",
        "",
        "| Check | Command | Exit |",
        "| --- | --- | ---: |",
        *[f"| {row['name']} | `{row['command']}` | {row['exit_code']} |" for row in payload["local_checks"]],
        "",
        "CI jobs in the imported merged-release run:",
        "",
        *[f"- **{row['name']}** — {row['conclusion']} ([job]({row['url']}))" for row in payload["critical_ci"]["jobs"]],
        "",
        "The older 40-story UI/UX gate remains separately owned by `scripts/uiux_release_gate.py`; this review requires its committed 37/37 checks and 7/7 gates and also runs it during final verification. It does not pretend that artifact generation is a full browser or three-interpreter test run.",
        "",
        "## Measured performance and safety carried into release",
        "",
        f"Environment: `{performance['environment']['platform']}` / `{performance['environment']['arch']}`, {performance['environment']['cpu_count']} CPUs, `{performance['environment']['chrome_version']}`; disposable loopback fixture, no external provider calls.",
        "",
        "| Metric | Actual | Gate/scope |",
        "| --- | ---: | --- |",
        f"| Fast real SSE | {fast['schedules']} schedules in {fast['elapsed_ms']:.1f}ms = {fast['sse_per_second']:.2f}/s | at least 50/s, complete reply retained |",
        f"| Full-window frame | mean {steady['mean_ms']:.2f}ms; p95 {steady['p95_ms']:.2f}ms; max {steady['max_ms']:.2f}ms | unchanged mean ≤4ms |",
        f"| Post-GC nodes | {' → '.join(str(value) for value in nodes)} | no repeated-cycle growth beyond the declared budget |",
        f"| Post-GC JS heap | {' → '.join(str(value) for value in heap)} bytes | repeated-cycle plateau, not zero allocation |",
        f"| Backend safety | {performance['backend_tests']['tests_passed']} tests; {performance['backend_tests']['isolation_pass_checks']} isolation checks | zero failures |",
        f"| Local container isolation | {performance['local_isolation']['status']}; {performance['local_isolation']['dependent_execution']} | live probes not requested; not advertised as qualified |",
        "",
        "## Corrections and resolved historical defects",
        "",
        "| Item | Status | Release truth |",
        "| --- | --- | --- |",
        *[f"| {row['id']} | {row['status']} | {row['detail']} |" for row in payload["corrections"]],
        "",
        "## Open follow-ups and evidence limits",
        "",
        "These are not hidden behind the handoff decision. Historical roadmap entries remain history; this table states what still needs work now.",
        "",
        "| Item | Severity/status | Current truth and next owner |",
        "| --- | --- | --- |",
        *[f"| {row['id']} | {row['severity']} / {row['status']} | {row['detail']} Evidence: `{row['evidence']}`. |" for row in payload["follow_ups"]],
        "",
        "## README claims",
        "",
        "| Claim | Verified by |",
        "| --- | --- |",
        *[f"| {row['claim']} | {row['evidence']} |" for row in payload["readme_claims"]],
        "",
        "README deliberately does not say production-ready, fully accessible, all-tests-pass, pixel-perfect, or qualified container isolation. Its release link points here, and its safety section continues to describe opt-in execution and external-provider data sharing.",
        "",
        "## Rollback",
        "",
        f"- **Story:** {payload['rollback']['story']}",
        f"- **Runtime:** {payload['rollback']['runtime']}",
        f"- **Whole program:** {payload['rollback']['whole_program']}",
        "",
        "## Reproduce the review",
        "",
        "Use the pinned browser environment from `tests/browser/`. The browser walks start disposable loopback fixtures and use no user workspace or external provider.",
        "",
        "```bash",
        "node scripts/compare_continuity_walk.mjs --screenshots /tmp/uiv2-30-compare --json > /tmp/uiv2-30-compare.json",
        "node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out /tmp/uiv2-30-visual --timeout-ms 60000",
        "gh run view RUN_ID --json status,conclusion,headSha,url,jobs,createdAt,updatedAt > /tmp/uiv2-30-ci.json",
        "python3 scripts/uiv2_release_review.py --import-evidence \\",
        "  --compare-run /tmp/uiv2-30-compare.json \\",
        "  --visual-run /tmp/uiv2-30-visual/check-manifest.json \\",
        "  --ci-run /tmp/uiv2-30-ci.json",
        "python3 scripts/uiux_release_gate.py --check",
        "python3 scripts/uiv2_release_review.py --check",
        "python3 -m pytest -q tests/test_uiv2_release_review.py",
        "```",
        "",
        "## Agent handoff — exact next work",
        "",
        *[f"{index}. {row}" for index, row in enumerate(payload["next_work"], 1)],
        "",
        "## Findings",
        "",
        "None: all required release evidence and local checks passed." if not payload["findings"] else "\n".join(f"- **{row['check']}**: {row['detail']}" for row in payload["findings"]),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--import-evidence", action="store_true")
    parser.add_argument("--compare-run", type=Path)
    parser.add_argument("--visual-run", type=Path)
    parser.add_argument("--ci-run", type=Path)
    args = parser.parse_args()
    if args.import_evidence:
        if not all((args.compare_run, args.visual_run, args.ci_run)):
            parser.error("--import-evidence requires --compare-run, --visual-run and --ci-run")
        try:
            payload = import_evidence(args.compare_run, args.visual_run, args.ci_run)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"UIV2-30 evidence import failed: {exc}", file=sys.stderr)
            return 1
        print(f"Imported UIV2-30 evidence: Compare 15/15, visual 2200/2200, CI {payload['ci']['conclusion']}")
        return 0
    payload = build()
    if args.json:
        print(dumps(payload), end="")
        return 0 if not payload["findings"] else 1
    if args.check:
        stale = not JSON_PATH.exists() or not DOC_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != dumps(payload) or DOC_PATH.read_text(encoding="utf-8") != render_doc(payload)
        if stale or payload["findings"]:
            print("UIV2-30 release review is stale, incomplete or blocked; import fresh green evidence and regenerate", file=sys.stderr)
            return 1
        print(f"UIV2-30 release review OK: {payload['totals']['local_checks_passed']}/{payload['totals']['local_checks']} local checks, 2200/2200 visual, 15/15 Compare, 0 findings")
        return 0
    JSON_PATH.write_text(dumps(payload), encoding="utf-8")
    DOC_PATH.write_text(render_doc(payload), encoding="utf-8")
    print(f"Wrote {JSON_PATH.relative_to(ROOT)} and {DOC_PATH.relative_to(ROOT)}: {payload['decision']['uiux_v2_handoff']}, {payload['totals']['findings']} findings")
    return 0 if not payload["findings"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
