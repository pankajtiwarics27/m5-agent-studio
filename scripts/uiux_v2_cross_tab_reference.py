#!/usr/bin/env python3
"""Contract, candidate validation, and human approval for the UIV2-26 visual gate.

    python3 scripts/uiux_v2_cross_tab_reference.py            # regenerate contract JSON + review
    python3 scripts/uiux_v2_cross_tab_reference.py --check    # fail on contract/approval drift
    python3 scripts/uiux_v2_cross_tab_reference.py --validate-candidate PATH
    python3 scripts/uiux_v2_cross_tab_reference.py --approve-candidate PATH \
        --approval-reference 'user:Arena approval, YYYY-MM-DD'

The browser walk produces the candidate. This module deliberately does not infer approval from
passing checks. Approval is a separate, explicit operation after a human reviews the annotated
comparisons. The approved JSON contains structural signatures and image digests, not PNG binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Reference.json"
DOC_PATH = ROOT / "docs" / "uiv2-v2-cross-tab-reference.md"
APPROVAL_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Approval.json"
WALK_PATH = ROOT / "scripts" / "uiux_v2_cross_tab_reference_walk.mjs"
STATIC_PATH = ROOT / "static" / "index.html"
WIREFRAME_PATH = ROOT / "docs" / "media" / "uiv2-v2" / "reference" / "wireframe.html"
REFERENCE_DOC_PATH = ROOT / "docs" / "uiv2-v2-reference.md"
REFERENCE_ROOT = ROOT / "docs" / "media" / "uiv2-v2" / "reference"
ROADMAP_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json"
SCHEMA_VERSION = 1
VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")

PANES: dict[str, dict[str, Any]] = {
    "start": {
        "tab": "start", "title": "Start", "file": "start.png",
        "required": ("#startLaunchCard", "#startTaskBtn", "#startDiagnostics"),
        "order": ("#startLaunchCard", "#startNextBlocker", "#startDiagnostics"),
        "counts": {},
        "state_walk": "scripts/start_command_center_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "Command-center evidence follows the action",
            "reference": "One next action and one collapsed status line.",
            "live": "The task and one blocker lead; recent work and measured diagnostics remain below them in native disclosures.",
            "story": "UIV2-24",
        },
    },
    "harness": {
        "tab": "harness", "title": "Harness", "file": "harness.png",
        "required": (
            "#harnessPromptInput", "#harnessLeadModel", "#harnessPolicy",
            "#harnessWorkflowMode", "#harnessProjectSummary", "#harnessRunBtn",
        ),
        "order": ("#harnessPromptInput", "#harnessLeadModel", "#harnessPolicy", "#harnessRunBtn"),
        "counts": {"#harnessStages .harness-stage": 3},
        "state_walk": "scripts/harness_recovery_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "A unified task composer leads the empty state",
            "reference": "A populated Plan → Execute → Verify cockpit with timeline and composer.",
            "live": "A short greeting and one composer keep lead model, policy, workflow and Run visible. The three stages return for a real active task; settings and run details disclose on demand. Sessions share the desktop navigation column and use a dismissible narrow-screen panel.",
            "story": "UIV2-08 / UIV2-09 / UIV2-10 / owner-requested task-first layout",
        },
    },
    "hall": {
        "tab": "hall", "title": "Council", "file": "hall.png",
        "required": (".hall-sidebar", "#hallStream", "#hallClearSessionBtn"),
        "order": ("#hallStream", "#hallVerdict", "#hallHumanInput"),
        "counts": {},
        "state_walk": "scripts/council_state_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "Readiness precedes deliberation",
            "reference": "Sessions, transcript, pinned conclusion and Clear session.",
            "live": "The same session and transcript regions also retain role readiness and model evidence before the composer; no fabricated conclusion is shown.",
            "story": "UIV2-12 / UIV2-13 / UIV2-14 / UIV2-15",
        },
    },
    "council": {
        "tab": "council", "title": "Pipeline", "file": "pipeline.png",
        "required": ("#councilPrompt", "#pipelineStageStrip", "#pipelineWorkspace"),
        "order": ("#councilPrompt", "#pipelineStageStrip", "#pipelineWorkspace"),
        "counts": {"#pipelineStageStrip .step-card:not(.pipeline-overview-button)": 4, "#pipelineStageStrip .pipeline-overview-button": 1},
        "state_walk": "scripts/pipeline_workspace_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "The real four-stage output replaces the placeholder",
            "reference": "A stage-focused canvas with artifacts.",
            "live": "Task, All stages plus exactly four stages, selected output, observed activity and factual artifacts fill the canvas; empty states stay empty.",
            "story": "UIV2-25",
        },
    },
    "debate": {
        "tab": "debate", "title": "Compare", "file": "compare.png",
        "required": ("#debatePrompt", "#debateRunBtn", "#debateCriteriaDetails"),
        "order": ("#debatePrompt", "#debateCriteriaDetails", "#debateGrid"),
        "counts": {},
        "state_walk": "scripts/compare_evidence_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "Model and criteria controls stay explicit",
            "reference": "Question first, balanced results, evidence in the inspector.",
            "live": "The question leads; model choices and a closed criteria disclosure follow before the result region, preserving the actual run contract.",
            "story": "UIV2-16 / UIV2-17",
        },
    },
    "chat": {
        "tab": "chat", "title": "Chat", "file": "chat.png",
        "required": ("#chatPersona", "#chatMessages", "#chatInput", "#chatSendBtn"),
        "order": ("#chatMessages", "#chatInput", "#chatSendBtn"),
        "counts": {},
        "state_walk": "scripts/chat_layout_walk.mjs",
        "difference": {
            "id": "D3", "anchor": "pane", "title": "Conversation controls remain above the focused column",
            "reference": "A focused single conversation with every registered persona available.",
            "live": "Model, registry-driven persona and reasoning controls remain above the focused transcript and composer; the generic inspector stays collapsed for Chat, and the demo shows its real registry size.",
            "story": "UIV2-18 / UIV2-19 / UIV2-20",
        },
    },
}

COMMON_DIFFERENCES = (
    {
        "id": "D1", "anchor": "integrations", "title": "On-demand tools and integrations",
        "reference": "The conceptual wireframe leaves the integrations row out.",
        "live": "One closed-by-default Tools & integrations disclosure preserves Skills, MCP, provider, document and unload controls without a permanent band or horizontal overflow.",
        "story": "UIV2-05 / owner-requested task-first layout",
    },
    {
        "id": "D2", "anchor": "header", "title": "Preserved global actions and explicit Inspector",
        "reference": "Telemetry, search and theme are the conceptual header actions; desktop Inspector is persistent.",
        "live": "Density and model-unload controls remain reachable in the tools disclosure; telemetry is a closed popover and Inspector is closed initially at every width with an explicit toggle and Close action.",
        "story": "UIV2-06 / existing feature preservation / owner-requested task-first layout",
    },
)

STATE_SUPPORT = {
    "start": {
        "empty": "scripts/start_command_center_walk.mjs", "populated": "scripts/start_command_center_walk.mjs",
        "long-content": "scripts/start_command_center_walk.mjs", "loading": "scripts/start_command_center_walk.mjs",
        "error": "scripts/start_command_center_walk.mjs", "recovery": "scripts/start_command_center_walk.mjs",
    },
    "harness": {
        "empty": "scripts/harness_settings_walk.mjs", "populated": "scripts/harness_timeline_walk.mjs",
        "long-content": "scripts/harness_evidence_walk.mjs", "loading": "scripts/harness_timeline_walk.mjs",
        "error": "scripts/harness_recovery_walk.mjs", "recovery": "scripts/harness_recovery_walk.mjs",
    },
    "hall": {
        "empty": "scripts/council_state_walk.mjs", "populated": "scripts/council_deliberation_walk.mjs",
        "long-content": "scripts/council_deliberation_walk.mjs", "loading": "scripts/council_state_walk.mjs",
        "error": "scripts/council_state_walk.mjs", "recovery": "scripts/council_state_walk.mjs",
    },
    "council": {
        state: "scripts/pipeline_workspace_walk.mjs"
        for state in ("empty", "populated", "long-content", "loading", "error", "recovery")
    },
    "debate": {
        "empty": "scripts/compare_balanced_walk.mjs", "populated": "scripts/compare_evidence_walk.mjs",
        "long-content": "scripts/compare_provenance_walk.mjs", "loading": "scripts/compare_evidence_walk.mjs",
        "error": "scripts/compare_history_walk.mjs", "recovery": "scripts/compare_continuity_walk.mjs",
    },
    "chat": {
        state: "scripts/chat_layout_walk.mjs"
        for state in ("empty", "populated", "long-content", "loading", "error", "recovery")
    },
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def matrix_keys() -> list[str]:
    return [f"{theme}/{viewport}/{pane}" for theme in THEMES for viewport in VIEWPORTS for pane in PANES]


def contract_fingerprint(payload: dict[str, Any]) -> str:
    """Hash semantics, not the mutable approval status or the app revision being tested.

    A future app commit must be allowed to run against the old approved structural baseline;
    changing the matrix, assertions, annotations, or approved reference still invalidates it.
    """
    identity = {
        "schema_version": payload["schema_version"],
        "story": payload["story"],
        "matrix": payload["matrix"],
        "layout_contract": payload["layout_contract"],
        "panes": payload["panes"],
        "approved_reference": payload["approved_reference"],
    }
    return hashlib.sha256(dumps(identity).encode("utf-8")).hexdigest()


def differences_for(pane: str) -> list[dict[str, str]]:
    return [*COMMON_DIFFERENCES, PANES[pane]["difference"]]


def reference_files() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index_path in sorted(REFERENCE_ROOT.glob("*-light/index.json")) + sorted(REFERENCE_ROOT.glob("*-dark/index.json")):
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for shot in index["shots"]:
            path = index_path.parent / shot["file"]
            rows.append({
                "set": index_path.parent.name, "pane": shot["id"].removeprefix("ref-"), "file": path.relative_to(ROOT).as_posix(),
                "sha256": sha256(path), "bytes": path.stat().st_size,
                "recorded_sha256": shot["sha256"], "viewport": index["viewport"],
                "theme": index["variant"]["theme"],
            })
    return rows


def build() -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    html = STATIC_PATH.read_text(encoding="utf-8")
    walk = WALK_PATH.read_text(encoding="utf-8")
    for pane, contract in PANES.items():
        for selector in (*contract["required"], *contract["order"], *contract["counts"]):
            if selector.startswith("#"):
                token = f'id="{selector[1:]}"'
            elif selector.startswith("."):
                token = selector[1:]
            else:
                continue
            # Compound count selectors only need their leading ID/class verified here; the browser
            # performs the actual count on every run.
            if " " in selector:
                selector = selector.split(" ", 1)[0]
                token = f'id="{selector[1:]}"' if selector.startswith("#") else selector[1:]
            if token not in html:
                findings.append({"check": "missing-live-selector", "detail": f"{pane}: {selector} ({token})"})
        if not (ROOT / contract["state_walk"]).exists():
            findings.append({"check": "missing-state-walk", "detail": f"{pane}: {contract['state_walk']}"})
    for script in {script for rows in STATE_SUPPORT.values() for script in rows.values()}:
        if not (ROOT / script).exists():
            findings.append({"check": "missing-state-support", "detail": script})
    for snippet in (
        "demo_tour.py", "sessionStorage", "Authorization", "pageerror", "candidate",
        "approved", "comparison.png", "reference.png", "actual.png", "scrollWidth",
        "No Chrome/Chromium", "--check", "--out",
    ):
        if snippet not in walk:
            findings.append({"check": "walk-contract-missing", "detail": snippet})
    references = reference_files()
    if len(references) != 36:
        findings.append({"check": "approved-reference-coverage", "detail": f"expected 36 committed fixtures, found {len(references)}"})
    for row in references:
        if row["sha256"] != row["recorded_sha256"]:
            findings.append({"check": "approved-reference-hash", "detail": row["file"]})
    roadmap = json.loads(ROADMAP_PATH.read_text(encoding="utf-8"))
    reference_story = next(story for story in roadmap["stories"] if story["id"] == "UIV2-03")
    reference_approval = any(
        "AC2 resolved: explicit user approval" in row for row in reference_story["tracking"]["acceptance_evidence"]
    )
    if not reference_approval:
        findings.append({"check": "reference-not-approved", "detail": "UIV2-03 has no explicit user approval"})
    approval = approval_summary(findings)
    panes = []
    for key, contract in PANES.items():
        panes.append({
            "id": key, **{field: contract[field] for field in ("tab", "title", "file", "required", "order", "counts", "state_walk")},
            "differences": differences_for(key), "state_support": STATE_SUPPORT[key],
        })
    payload = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiv2_cross_tab_visual_reference_gate",
        "story": "UIV2-26",
        "generated_by": "scripts/uiux_v2_cross_tab_reference.py",
        "regenerate_with": "python3 scripts/uiux_v2_cross_tab_reference.py",
        "check_with": "python3 scripts/uiux_v2_cross_tab_reference.py --check",
        "candidate_walk": "scripts/uiux_v2_cross_tab_reference_walk.mjs",
        "statement": (
            "The actual app and the explicitly approved UIV2-03 wireframe are captured at the same six-tab "
            "viewport/theme matrix. Pixel differences are measured and annotated, never hidden by a blanket "
            "tolerance. Independent structural rules must pass, and future --check runs also require a live "
            "structural baseline approved by a human. New PNGs are local evidence, not Git inputs."
        ),
        "source": {
            "static": "static/index.html", "static_sha256": sha256(STATIC_PATH),
            "walk": "scripts/uiux_v2_cross_tab_reference_walk.mjs", "walk_sha256": sha256(WALK_PATH),
        },
        "approved_reference": {
            "wireframe": WIREFRAME_PATH.relative_to(ROOT).as_posix(), "wireframe_sha256": sha256(WIREFRAME_PATH),
            "spec": REFERENCE_DOC_PATH.relative_to(ROOT).as_posix(), "spec_sha256": sha256(REFERENCE_DOC_PATH),
            "approval": "UIV2-03 explicit user approval on 2026-11-04 (ask_user answer approve)",
            "committed_fixtures": references,
        },
        "matrix": {
            "panes": list(PANES), "viewports": list(VIEWPORTS), "themes": list(THEMES),
            "keys": matrix_keys(), "live_captures": 48, "reference_captures": 48,
            "annotated_comparisons": 48, "total_pngs_per_run": 144,
            "device_scale_factor": 1, "pixel_policy": "measure and annotate; no pixel pass/fail tolerance",
        },
        "layout_contract": {
            "header_max_height": 64, "desktop_nav_width": [192, 224],
            "harness_shared_left_nav_width": [192, 240],
            "desktop_task_min_width": 640, "desktop_breakpoint": 1440, "narrow_breakpoint": 800,
            "horizontal_overflow": "forbidden", "telemetry": "closed header popover by default",
            "integrations": "#studioTools closed by default; existing tools remain inside the disclosure",
            "inspector": "closed by default at every width; #monitorToggleBtn exposes explicit open and Close action",
            "harness_sessions": "share navigation column at >=1280px; dismissible Sessions panel below 1280px",
            "harness_empty_state": "unified task controls visible; three stages retained in DOM but collapsed until a real active task",
        },
        "panes": panes,
        "approval": approval,
        "totals": {
            "panes": len(PANES), "viewports": len(VIEWPORTS), "themes": len(THEMES),
            "matrix_captures": 48, "difference_annotations": len(PANES) * 3,
            "state_support_links": sum(len(rows) for rows in STATE_SUPPORT.values()),
            "approved_reference_fixtures": len(references), "findings": len(findings),
        },
        "findings": findings,
    }
    if approval["status"] == "approved":
        record = json.loads(APPROVAL_PATH.read_text(encoding="utf-8"))
        binding_errors = []
        if record.get("contract_sha256") != contract_fingerprint(payload):
            binding_errors.append("approval belongs to different matrix/rules/reference semantics")
        if record.get("wireframe_sha256") != sha256(WIREFRAME_PATH):
            binding_errors.append("approval belongs to a different wireframe")
        if binding_errors:
            findings.extend({"check": "approval-contract-binding", "detail": error} for error in binding_errors)
            payload["approval"] = {**approval, "status": "invalid", "errors": binding_errors}
    payload["totals"]["findings"] = len(findings)
    return payload


def approval_summary(findings: list[dict[str, str]]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "required": True, "status": "pending", "artifact": APPROVAL_PATH.relative_to(ROOT).as_posix(),
        "rule": "A human must approve the annotated candidate; the agent cannot infer or self-certify this decision.",
    }
    if not APPROVAL_PATH.exists():
        return base
    try:
        approval = json.loads(APPROVAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append({"check": "approval-unreadable", "detail": str(exc)})
        return {**base, "status": "invalid"}
    errors = validate_approval(approval)
    if errors:
        findings.extend({"check": "invalid-live-approval", "detail": error} for error in errors)
        return {**base, "status": "invalid", "errors": errors}
    return {
        **base, "status": "approved", "human_approval": approval["human_approval"],
        "candidate": approval["candidate"], "captures": len(approval["captures"]),
        "approval_sha256": sha256(APPROVAL_PATH),
    }


def validate_approval(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("document_type") != "m5_uiv2_live_structural_approval":
        errors.append("approval schema/document type is wrong")
    human = payload.get("human_approval") or {}
    if human.get("decision") != "approved" or not str(human.get("reference", "")).startswith("user:"):
        errors.append("approval must record an explicit user: reference")
    if not human.get("recorded_at"):
        errors.append("approval timestamp is missing")
    candidate = payload.get("candidate") or {}
    if len(str(candidate.get("manifest_sha256", ""))) != 64 or len(str(candidate.get("git_head", ""))) != 40:
        errors.append("candidate manifest hash or git HEAD is missing")
    verification = payload.get("verification") or {}
    if verification.get("failures") != 0 or verification.get("captures") != 48:
        errors.append("approval is not bound to 48 captures with zero failures")
    captures = payload.get("captures")
    if not isinstance(captures, list) or len(captures) != 48:
        errors.append("approval must contain exactly 48 structural signatures")
    elif {row.get("key") for row in captures} != set(matrix_keys()):
        errors.append("approval matrix keys are incomplete or duplicated")
    for row in captures or []:
        if not isinstance(row.get("signature"), dict) or not isinstance(row.get("reference_signature"), dict):
            errors.append(f"{row.get('key', '?')}: live/reference signature missing")
        if len(row.get("differences", [])) != 3:
            errors.append(f"{row.get('key', '?')}: expected three authored difference annotations")
    return errors


def validate_candidate(payload: dict[str, Any], manifest_path: Path, *, verify_files: bool = True) -> list[str]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("document_type") != "m5_uiv2_cross_tab_candidate":
        errors.append("candidate schema/document type is wrong")
    if payload.get("story") != "UIV2-26" or payload.get("mode") not in {"candidate", "approved-check"}:
        errors.append("candidate story/mode is wrong")
    if payload.get("complete") is not True:
        errors.append("partial matrices cannot be approved or checked")
    matrix = payload.get("matrix") or {}
    if matrix.get("keys") != matrix_keys():
        errors.append("candidate matrix does not equal six panes × four viewports × two themes")
    if not JSON_PATH.exists():
        errors.append("contract artifact is missing")
    else:
        contract_payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        if payload.get("contract_sha256") != contract_fingerprint(contract_payload):
            errors.append("candidate was produced from a different contract")
    if payload.get("wireframe_sha256") != sha256(WIREFRAME_PATH):
        errors.append("candidate was produced from a different approved wireframe")
    captures = payload.get("captures")
    if not isinstance(captures, list) or len(captures) != 48:
        errors.append("candidate must contain exactly 48 captures")
        captures = captures if isinstance(captures, list) else []
    if {row.get("key") for row in captures} != set(matrix_keys()):
        errors.append("candidate capture keys are incomplete or duplicated")
    checks = payload.get("checks") or {}
    if checks.get("total", 0) <= 0 or checks.get("passed") != checks.get("total") or payload.get("failures"):
        errors.append("candidate has failed or unexecuted structural checks")
    root = manifest_path.parent
    for row in captures:
        key = row.get("key", "?")
        if len(row.get("differences", [])) != 3:
            errors.append(f"{key}: difference annotations are incomplete")
        if not isinstance(row.get("signature"), dict) or not isinstance(row.get("reference_signature"), dict):
            errors.append(f"{key}: structural signature missing")
        if not isinstance(row.get("pixel_difference"), dict):
            errors.append(f"{key}: measured pixel difference missing")
        for kind in ("actual", "reference", "comparison"):
            image = row.get("images", {}).get(kind) or {}
            relative = image.get("file")
            if not relative or len(image.get("sha256", "")) != 64:
                errors.append(f"{key}: {kind} image/digest missing")
                continue
            path = root / relative
            if verify_files and (not path.is_file() or sha256(path) != image["sha256"]):
                errors.append(f"{key}: {kind} image missing or digest mismatch")
    return errors


def approve_candidate(
    manifest_path: Path, approval_reference: str, *, output_path: Path = APPROVAL_PATH,
) -> dict[str, Any]:
    current_contract = build()
    if current_contract["findings"]:
        raise ValueError(f"approval refused: contract has findings: {current_contract['findings']}")
    if (JSON_PATH.read_text(encoding="utf-8") != dumps(current_contract)
            or DOC_PATH.read_text(encoding="utf-8") != render_doc(current_contract)):
        raise ValueError("approval refused: regenerate and check the contract artifacts first")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_candidate(payload, manifest_path)
    if payload.get("mode") != "candidate":
        errors.append("only a candidate, not an approved-check, can establish a new baseline")
    git = payload.get("git") or {}
    if git.get("dirty") is not False:
        errors.append("approval requires a clean tracked worktree")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if git.get("head") != head:
        errors.append(f"candidate HEAD {git.get('head')} is not current HEAD {head}")
    if not approval_reference.startswith("user:") or len(approval_reference) < 20:
        errors.append("--approval-reference must be a specific 'user:…' record of explicit human approval")
    if errors:
        raise ValueError("; ".join(errors))
    captures = []
    for row in payload["captures"]:
        captures.append({
            "key": row["key"], "signature": row["signature"], "reference_signature": row["reference_signature"],
            "differences": row["differences"], "pixel_difference": row["pixel_difference"], "images": row["images"],
        })
    approval = {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiv2_live_structural_approval",
        "story": "UIV2-26",
        "human_approval": {
            "decision": "approved", "reference": approval_reference,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Structural fidelity of all 48 annotated live/reference comparisons; not a pixel-clone claim.",
        },
        "candidate": {
            "manifest": manifest_path.relative_to(ROOT).as_posix() if manifest_path.is_relative_to(ROOT) else str(manifest_path),
            "manifest_sha256": sha256(manifest_path), "git_head": git["head"],
        },
        "contract_sha256": payload["contract_sha256"], "wireframe_sha256": payload["wireframe_sha256"],
        "verification": {
            "checks_total": payload["checks"]["total"], "checks_passed": payload["checks"]["passed"],
            "failures": 0, "captures": 48,
        },
        "captures": captures,
    }
    atomic_write(output_path, dumps(approval))
    return approval


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# UIV2-26 — cross-tab visual regression reference gate", "",
        payload["statement"], "",
        "## Matrix and evidence policy", "",
        "- Six tabs: Start, Harness, Council, Pipeline, Compare and Chat.",
        "- Four viewports: 1640×900, 1440×900, 1280×800 and 390×844; light and dark separately.",
        "- Every variant produces `actual.png`, `reference.png` and an annotated `comparison.png`: **48 of each, 144 PNGs total**.",
        "- PNGs and candidate manifests are Git-ignored local evidence. The committed approval stores structural signatures and SHA-256 digests only.",
        "- Pixel RGB delta and changed-pixel ratio are reported for review. There is deliberately **no pixel-tolerance pass** because the approved target is structural, not a pixel clone.", "",
        "## Independent layout contract", "",
        "- Header ≤64px; desktop nav 192–224px; visible desktop inspector 272–320px; task region ≥640px where the desktop reference requires it.",
        "- Exactly one selected tab and visible pane; all six tabs remain reachable; no document horizontal overflow; telemetry starts as a closed popover.",
        "- Pane-specific selectors, DOM order and counts are asserted in the running app, not inferred from source inspection.", "",
        "## Authored differences shown on every comparison", "",
        "| Pane | ID | Difference | Reference intent | Live, feature-preserving behavior |",
        "| --- | --- | --- | --- | --- |",
    ]
    for pane in payload["panes"]:
        for row in pane["differences"]:
            lines.append(f"| {pane['title']} | {row['id']} | {row['title']} | {row['reference']} | {row['live']} |")
    lines.extend(["", "## Existing state and interaction support", "",
        "The cross-tab matrix is the common empty/seeded state. Populated, long-content, loading, error and recovery remain real product states exercised by their existing walks; no state is painted into a screenshot.", "",
        "| Pane | State | Existing producer |", "| --- | --- | --- |"])
    for pane in payload["panes"]:
        for state, producer in pane["state_support"].items():
            lines.append(f"| {pane['title']} | {state} | `{producer}` |")
    approval = payload["approval"]
    lines.extend(["", "## Human structural-fidelity approval", ""])
    if approval["status"] == "approved":
        human = approval["human_approval"]
        lines.extend([
            f"**Approved.** `{human['reference']}` at `{human['recorded_at']}`.", "",
            f"- Candidate: `{approval['candidate']['manifest']}`",
            f"- Candidate SHA-256: `{approval['candidate']['manifest_sha256']}`",
            f"- HEAD: `{approval['candidate']['git_head']}`", "",
            "Future `--check` runs compare all 48 live structural signatures with this approval and still rerun every independent reference/layout assertion.", "",
        ])
    else:
        lines.extend([
            "**Pending. No live structural-fidelity approval is recorded.** The candidate is not an approved baseline and `--check` fails closed.", "",
            "Review the 48 annotated comparisons. Only after an explicit user decision, bind that exact candidate with:", "",
            "```bash", "python3 scripts/uiux_v2_cross_tab_reference.py \\", "  --approve-candidate docs/media/cross-tab-reference-uiv2-26/candidate/candidate-manifest.json \\", "  --approval-reference 'user:<conversation and date of the explicit approval>'", "```", "",
        ])
    lines.extend(["## Reproduce", "", "```bash",
        "node scripts/uiux_v2_cross_tab_reference_walk.mjs --candidate --out docs/media/cross-tab-reference-uiv2-26/candidate",
        "python3 scripts/uiux_v2_cross_tab_reference.py --validate-candidate docs/media/cross-tab-reference-uiv2-26/candidate/candidate-manifest.json",
        "# After approval exists:",
        "node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out docs/media/cross-tab-reference-uiv2-26/check",
        "python3 scripts/uiux_v2_cross_tab_reference.py --check", "```", "",
        "The walk starts and stops its own isolated `scripts/demo_tour.py --serve` instance, requires bearer authentication, blocks unexpected navigation, and fails on browser page errors or partial captures.", "",
        f"Contract findings: **{len(payload['findings'])}**.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate-candidate", type=Path)
    parser.add_argument("--approve-candidate", type=Path)
    parser.add_argument("--approval-reference", default="")
    args = parser.parse_args(argv)
    if args.validate_candidate:
        path = args.validate_candidate.resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_candidate(payload, path)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"candidate OK: 48 captures, {payload['checks']['total']} checks, 0 failures")
        return 0
    if args.approve_candidate:
        try:
            approval = approve_candidate(args.approve_candidate.resolve(), args.approval_reference)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"approval refused: {exc}", file=sys.stderr)
            return 1
        print(f"approved: 48 structural signatures from {approval['candidate']['manifest_sha256']}")
        return 0
    payload = build()
    if args.json:
        print(dumps(payload), end="")
        return 1 if payload["findings"] else 0
    if args.check:
        valid = JSON_PATH.exists() and DOC_PATH.exists()
        if valid:
            valid = JSON_PATH.read_text(encoding="utf-8") == dumps(payload) and DOC_PATH.read_text(encoding="utf-8") == render_doc(payload)
        if not valid or payload["findings"]:
            for finding in payload["findings"]:
                print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
            print("cross-tab reference artifacts are stale or invalid; regenerate without --check", file=sys.stderr)
            return 1
        print(f"cross-tab reference OK: 48 matrix captures, {payload['totals']['difference_annotations']} authored differences, approval={payload['approval']['status']}, 0 findings")
        return 0
    atomic_write(JSON_PATH, dumps(payload))
    atomic_write(DOC_PATH, render_doc(payload))
    print(f"wrote {JSON_PATH.relative_to(ROOT)} and {DOC_PATH.relative_to(ROOT)}: 48 matrix captures, approval={payload['approval']['status']}, {len(payload['findings'])} findings")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
