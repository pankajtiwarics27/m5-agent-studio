#!/usr/bin/env python3
"""Fail-closed contract and evidence binding for UIV2-28.

    python3 scripts/uiv2_accessibility_adversarial.py --import-evidence PATH
    python3 scripts/uiv2_accessibility_adversarial.py --check
    python3 scripts/uiv2_accessibility_adversarial.py --json

The browser walk is the behavioral proof. This module checks that its matrix,
safety boundaries and measured frontend repairs still exist, then binds an
all-green capture manifest to the exact frontend/walk SHA-256 digests. PNGs stay
local evidence; the small JSON run record and generated contract are committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATIC_PATH = ROOT / "static" / "index.html"
WALK_PATH = ROOT / "scripts" / "uiv2_accessibility_adversarial_walk.mjs"
JSON_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Accessibility_Adversarial.json"
RUN_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Accessibility_Adversarial_Run.json"
DOC_PATH = ROOT / "docs" / "uiv2-v2-accessibility-adversarial.md"
CROSS_TAB_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Reference.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
A11Y_WALK_PATH = ROOT / "scripts" / "a11y_walk.mjs"
RESPONSIVE_WALK_PATH = ROOT / "scripts" / "responsive_walk.mjs"

PANES = ("start", "harness", "hall", "council", "debate", "chat")
VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
EXPECTED_GROUPS = {"rendered": 36, "keyboard": 12, "zoom-200": 48, "long-recovery": 12, "browser": 12}
EXPECTED_CAPTURE_GROUPS = {"rendered": 12, "zoom-200": 48, "long-recovery": 2}
EXPECTED_TOTAL = sum(EXPECTED_GROUPS.values())
EXPECTED_CAPTURES = sum(EXPECTED_CAPTURE_GROUPS.values())
IMPACTED_CONTROLS = (
    ".brand-chip", "#harnessProjectSummary", "#startTaskInput", "#monitorToggleBtn",
    "#hallClearSessionBtn", "#chatPersona", "#chatReasoning", "#hallConclusionBody",
)

REQUIRED_FRONTEND = {
    "brand-contrast": ".brand-chip { width: 28px; height: 28px; border-radius: var(--radius-sm); background: var(--accent); color: var(--primary-text);",
    "start-recovery-scroll": ".onboarding-shell { flex: 1; min-height: 0; overflow-y: auto; width: 100%; max-width: 860px;",
    "start-input-shrink": ".start-launch input { flex: 1 1 320px; min-width: 0; }",
    "summary-target": "font-size: 12px; min-width: 0; min-height: var(--target-min);",
    "hall-action-scroll": ".hall-header > div:last-child { width: 100%; overflow-x: auto; overscroll-behavior-x: contain; }",
    "narrow-topbar-scroll": ".topbar { padding: 5px 8px; overflow-x: auto; overflow-y: hidden; }",
    "workspace-toolbar-wrap": ".workspace-toolbar { flex-wrap: wrap; }",
    "presentation-wrap": ".presentation > div { flex: 1 1 160px; min-width: 0; }",
    "chat-usable-minimum": ".chat-header-bar > div:nth-child(3) { min-width: 140px; }",
    "short-height-recovery": ".hall-main, #pane-chat { overflow-y: auto; overscroll-behavior: contain; }",
}

REQUIRED_WALK = {
    "pane-matrix": 'const PANES = ["start", "harness", "hall", "council", "debate", "chat"];',
    "viewport-matrix": '{ label: "1640x900", width: 1640, height: 900 },',
    "zoom-factor": "const ZOOM_FACTOR = 2;",
    "text-floor": "const TEXT_FLOOR = 4.5;",
    "large-text-floor": "const LARGE_TEXT_FLOOR = 3;",
    "target-floor": "const TARGET_MIN = 24;",
    "anonymous-boundary": "anonymous.status !== 401 || authenticated.status !== 200",
    "outbound-block": 'errors.push(`outbound request blocked: ${target.origin}`)',
    "reduced-motion": 'name: "prefers-reduced-motion", value: "reduce"',
    "css-color-4": '/^color\\(srgb\\s/.test(value)',
    "contrast-measurement": "passed: !background.nonSolid && ratio >= floor",
    "real-tab": 'await page.keyboard.press("Tab")',
    "real-enter": 'await page.keyboard.press("Enter")',
    "real-escape": 'await page.keyboard.press("Escape")',
    "focus-loop-detection": 'if (seen.has(key)) { repeated = key; break; }',
    "modal-containment": 'document.getElementById("hallConclusionModal").contains(document.activeElement)',
    "nested-scroll": "transcript and pinned conclusion hold independent nonzero nested scroll offsets",
    "responsive-scroll-host": 'streamHost === "hallStream"',
    "modal-end": "the long dialog scrolls to its final marker without moving the underlying regions",
    "exact-scroll-restoration": "restored.stream === before.stream && restored.conclusion === before.conclusion && restored.page === before.page",
    "source-binding": "source_sha256: sourceSha256",
    "capture-binding": '"capture-manifest.json"',
}

REQUIRED_WORKFLOW = {
    "ci-keyboard": "node scripts/a11y_walk.mjs",
    "ci-responsive": "node scripts/responsive_walk.mjs",
    "ci-adversarial": "node scripts/uiv2_accessibility_adversarial_walk.mjs",
    "ci-browser-job": "'Browser safety regressions'",
    "ci-required-browser": "M5_REQUIRE_BROWSER_TESTS=1",
}

MEASURED_REPAIRS = (
    ("dark brand chip", "2.41:1 computed contrast", "explicit var(--primary-text) foreground; every final rendered pair passes"),
    ("#harnessProjectSummary", "113×18 CSS px", "24px minimum target height; responsive walk passes"),
    ("#hallClearSessionBtn", "right=430px at a 360px viewport", "contained horizontal action scrolling; the control remains reachable"),
    ("#chatPersona", "20×44 CSS px at 200% phone zoom", "140px group minimum wraps only when two columns cannot fit"),
    ("#monitorToggleBtn", "clipped at a 195px effective viewport",
     "the header is one row and scrolls horizontally at narrow widths, so the toggle stays reachable at 200%; #hallClearSessionBtn uses the same contained-scroll remedy"),
    ("#startTaskInput", "220px fixed minimum caused horizontal overflow", "zero flex minimum with the desktop basis retained"),
    ("Start / Council / Chat recovery", "short-height content had no reachable scroll path", "native Start-shell and Council/Chat main overflow recovery"),
)


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_source_hashes() -> dict[str, str]:
    return {
        "static/index.html": sha256(STATIC_PATH),
        "scripts/uiv2_accessibility_adversarial_walk.mjs": sha256(WALK_PATH),
    }


def source_hashes() -> dict[str, str]:
    return {
        **run_source_hashes(),
        "scripts/a11y_walk.mjs": sha256(A11Y_WALK_PATH),
        "scripts/responsive_walk.mjs": sha256(RESPONSIVE_WALK_PATH),
        ".github/workflows/ci.yml": sha256(WORKFLOW_PATH),
    }


def source_findings(html: str, walk: str, workflow: str | None = None) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    ci = WORKFLOW_PATH.read_text(encoding="utf-8") if workflow is None else workflow
    for name, token in {**REQUIRED_FRONTEND, **REQUIRED_WALK, **REQUIRED_WORKFLOW}.items():
        haystack = html if name in REQUIRED_FRONTEND else ci if name in REQUIRED_WORKFLOW else walk
        if token not in haystack:
            findings.append({"check": name, "detail": f"required contract token is absent: {token}"})
    for token in ("const TEXT_FLOOR = 4.5", "const LARGE_TEXT_FLOOR = 3", "const TARGET_MIN = 24", "const ZOOM_FACTOR = 2"):
        if len(re.findall(re.escape(token), walk)) != 1:
            findings.append({"check": "single-threshold", "detail": f"{token} must occur exactly once"})
    return findings


def _matrix_errors(checks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    rendered = {(c.get("viewport"), c.get("theme"), c.get("pane")) for c in checks if c.get("group") == "rendered"}
    expected_rendered = {("1440x900", theme, pane) for theme in THEMES for pane in PANES}
    if rendered != expected_rendered:
        errors.append("rendered matrix does not equal 1440x900 × two themes × six panes")
    zoom = {(c.get("viewport"), c.get("theme"), c.get("pane")) for c in checks if c.get("group") == "zoom-200"}
    expected_zoom = {(viewport, theme, pane) for viewport in VIEWPORTS for theme in THEMES for pane in PANES}
    if zoom != expected_zoom:
        errors.append("200% matrix does not equal four viewports × two themes × six panes")
    recovery = {(c.get("viewport"), c.get("theme")) for c in checks if c.get("group") == "long-recovery"}
    if recovery != {("1280x800", "dark"), ("390x844", "light")}:
        errors.append("long recovery must cover desktop dark and phone light at 200%")
    return errors


def validate_run(payload: Any, hashes: dict[str, str] | None = None) -> list[str]:
    """Validate evidence semantics without requiring the local PNGs to still exist."""
    if not isinstance(payload, dict):
        return ["capture manifest is not an object"]
    errors: list[str] = []
    if payload.get("story") != "UIV2-28":
        errors.append("capture manifest story is not UIV2-28")
    checks = payload.get("checks")
    captures = payload.get("captures")
    if not isinstance(checks, list) or not isinstance(captures, list):
        return [*errors, "capture manifest checks/captures must be lists"]
    totals = payload.get("totals", {})
    expected_totals = {"checks": EXPECTED_TOTAL, "passed": EXPECTED_TOTAL, "failed": 0, "captures": EXPECTED_CAPTURES}
    if totals != expected_totals:
        errors.append(f"totals must be {expected_totals}, got {totals}")
    if len(checks) != EXPECTED_TOTAL or any(c.get("passed") is not True for c in checks):
        errors.append("all 120 behavioral checks must be present and true")
    groups = Counter(c.get("group") for c in checks)
    if dict(groups) != EXPECTED_GROUPS:
        errors.append(f"check groups must be {EXPECTED_GROUPS}, got {dict(groups)}")
    errors.extend(_matrix_errors(checks))
    if len(captures) != EXPECTED_CAPTURES:
        errors.append("exactly 62 captures are required")
    capture_groups = Counter(c.get("group") for c in captures)
    if dict(capture_groups) != EXPECTED_CAPTURE_GROUPS:
        errors.append(f"capture groups must be {EXPECTED_CAPTURE_GROUPS}, got {dict(capture_groups)}")
    capture_identities = {(c.get("group"), c.get("viewport"), c.get("theme"), c.get("pane")) for c in captures}
    if len(capture_identities) != len(captures):
        errors.append("capture identities must be unique")
    for capture in captures:
        if not isinstance(capture.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(capture.get("sha256", ""))):
            errors.append("every capture needs a path and SHA-256 digest")
            break
    expected_hashes = hashes if hashes is not None else run_source_hashes()
    if payload.get("source_sha256") != expected_hashes:
        errors.append("capture manifest is stale or its source digests do not match this checkout")
    method = payload.get("method", {})
    if method.get("contrast") != "WCAG 2 relative luminance on computed solid backgrounds":
        errors.append("contrast method is missing or weakened")
    if method.get("zoom") != "half CSS viewport with deviceScaleFactor=2":
        errors.append("zoom method is missing or weakened")
    if method.get("reducedMotion") != "prefers-reduced-motion: reduce":
        errors.append("reduced-motion method is missing")
    nested = [c for c in checks if c.get("step") == "transcript and pinned conclusion hold independent nonzero nested scroll offsets"]
    for check in nested:
        try:
            detail = json.loads(check.get("detail", ""))
        except json.JSONDecodeError:
            errors.append("nested-scroll detail is not measured JSON")
            continue
        if detail.get("stream", 0) <= 0 or detail.get("conclusion", 0) <= 0:
            errors.append("nested-scroll evidence must start from nonzero offsets")
    git = payload.get("git", {})
    if not re.fullmatch(r"[0-9a-f]{40}", str(git.get("head", ""))) or not isinstance(git.get("dirty"), bool):
        errors.append("capture manifest must record its Git head and dirty state")
    return errors


def load_run() -> dict[str, Any] | None:
    if not RUN_PATH.is_file():
        return None
    try:
        payload = json.loads(RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build() -> dict[str, Any]:
    html = STATIC_PATH.read_text(encoding="utf-8")
    walk = WALK_PATH.read_text(encoding="utf-8")
    findings = source_findings(html, walk)
    run = load_run()
    run_errors = ["no imported all-green browser run"] if run is None else validate_run(run)
    findings.extend({"check": "browser-evidence", "detail": error} for error in run_errors)
    cross_tab = json.loads(CROSS_TAB_PATH.read_text(encoding="utf-8"))
    state_coverage = {
        pane["id"]: {"title": pane["title"], "states": pane["state_support"]}
        for pane in cross_tab["panes"]
    }
    return {
        "schema_version": 1,
        "document_type": "m5_uiv2_accessibility_adversarial_contract",
        "story": "UIV2-28",
        "statement": "Measured runtime accessibility and recovery evidence, not a screen-reader certification or a cosmetic-only completion.",
        "execution_policy": {
            "contract_is_not_run_evidence": True,
            "run_manifest": str(RUN_PATH.relative_to(ROOT)),
            "browser_ci_job": "Browser safety regressions",
            "external_requests": "blocked except the isolated loopback fixture",
            "screen_reader_scope": "DOM names, roles, tab order and focus are asserted; this is not a claim of NVDA/JAWS/VoiceOver certification",
        },
        "thresholds": {"normal_text_contrast": 4.5, "large_text_contrast": 3.0, "target_css_px": 24, "zoom_factor": 2},
        "source_sha256": source_hashes(),
        "matrix": {
            "panes": list(PANES), "viewports": list(VIEWPORTS), "themes": list(THEMES),
            "rendered": "1440x900 × light/dark × six panes",
            "zoom_200": "four viewports × light/dark × six panes; half CSS viewport and deviceScaleFactor=2",
            "long_recovery": "1280x800 dark and 390x844 light at 200%, with route-created transcript and conclusion",
            "rendered_rows": [{"viewport": "1440x900", "theme": theme, "pane": pane} for theme in THEMES for pane in PANES],
            "zoom_rows": [
                {"viewport": viewport, "theme": theme, "pane": pane,
                 "effective_css_viewport": f"{round(int(viewport.split('x')[0]) / 2)}x{round(int(viewport.split('x')[1]) / 2)}",
                 "device_scale_factor": 2}
                for viewport in VIEWPORTS for theme in THEMES for pane in PANES
            ],
            "long_recovery_rows": [
                {"viewport": "1280x800", "theme": "dark", "device_scale_factor": 2},
                {"viewport": "390x844", "theme": "light", "device_scale_factor": 2},
            ],
            "expected_check_groups": EXPECTED_GROUPS,
            "expected_capture_groups": EXPECTED_CAPTURE_GROUPS,
            "expected_checks": EXPECTED_TOTAL, "expected_captures": EXPECTED_CAPTURES,
        },
        "behavioral_contract": {
            "contrast": "WCAG relative luminance on computed foreground and composited solid backgrounds; non-solid backgrounds fail rather than pass by inference",
            "semantics": "accessible names for every tabbable and no positive tabindex; browser checks are not a screen-reader certification",
            "keyboard": "real Tab/Enter/Escape input, no pane focus loop, intentional modal containment, exact focus restoration",
            "zoom": "every control reachable, at least 24 CSS px, and no document horizontal overflow at 200%",
            "nested_scroll": "nonzero active outer and pinned-conclusion offsets; modal reaches its final marker without moving either; Escape restores both exactly",
            "reduced_motion": "non-essential animation, transition and smooth scrolling are effectively disabled",
            "safety": "anonymous 401/authenticated 200 preflight, generated synthetic token, isolated fixture, outbound browser requests blocked, no real provider or user workspace",
        },
        "impacted_controls": list(IMPACTED_CONTROLS),
        "measured_repairs": [
            {"control": control, "before": before, "after_contract": after}
            for control, before, after in MEASURED_REPAIRS
        ],
        "state_coverage": state_coverage,
        "visual_reference": {
            "contract": "product/M5_2_0_UIUX_V2_Cross_Tab_Reference.json",
            "walk": "scripts/uiux_v2_cross_tab_reference_walk.mjs",
            "required": "48 actual/reference/annotated variants and explicit human structural approval after intentional target-size changes",
        },
        "preservation": {
            "matrix": "product/M5_2_0_UIUX_V2_Preservation_Matrix.json",
            "run": "product/M5_2_0_UIUX_Preservation_Run.json",
            "required": "all 21 rows, every source-mapped control/route/test/walk, no API/session/workspace/authorization/sandbox/ledger/fencing/model-memory regression",
        },
        "evidence": {
            "run": str(RUN_PATH.relative_to(ROOT)),
            "status": "passed" if not run_errors else "blocked",
            "totals": run.get("totals") if run and not run_errors else None,
            "git": run.get("git") if run and not run_errors else None,
            "validation_errors": run_errors,
        },
        "findings": findings,
    }


def render_doc(payload: dict[str, Any]) -> str:
    evidence = payload["evidence"]
    lines = [
        "# UIV2-28 — accessibility, scroll and responsive adversarial suite", "",
        payload["statement"], "",
        "## Gate and evidence policy", "",
        "- Real Chromium input drives six panes in light and dark; no focus loop is accepted outside an intentional modal.",
        "- Contrast is computed with WCAG relative luminance. A gradient/image or otherwise unmeasured background fails instead of being inferred from tokens.",
        "- The 200% matrix uses half the CSS viewport and `deviceScaleFactor=2` at 1640×900, 1440×900, 1280×800 and 390×844, in both themes. Every control must remain reachable and at least 24 CSS px.",
        "- Reduced motion is emulated with `prefers-reduced-motion: reduce`. Accessible-name checks support screen-reader semantics but are not a claim of screen-reader certification.",
        "- The isolated fixture requires bearer auth, uses a synthetic token, blocks outbound requests and never contacts a provider or user workspace.",
        "- PNGs and raw capture manifests are local evidence. The committed run record binds check results and capture SHA-256 digests to exact source digests; CI runs the browser behavior again.", "",
        "## Imported browser run", "",
    ]
    if evidence["status"] == "passed":
        totals = evidence["totals"]
        lines.extend([
            f"**{totals['passed']}/{totals['checks']} checks passed; {totals['captures']} captures; 0 failures.**", "",
            f"Run Git head: `{evidence['git']['head']}`; dirty worktree during evidence capture: `{str(evidence['git']['dirty']).lower()}`.", "",
            "Local after evidence: `/tmp/uiv2-28-adversarial-after/capture-manifest.json` and its 62 PNGs. Original baselines are `/tmp/uiv2-28-before-a11y.log`, `/tmp/uiv2-28-before-responsive.log`, and `/tmp/uiv2-28-contrast-before.json`; these `/tmp` paths are review evidence, not release inputs.", "",
        ])
    else:
        lines.extend(["**BLOCKED: no current all-green source-bound browser run.**", ""] + [f"- {error}" for error in evidence["validation_errors"]] + [""])
    lines.extend([
        "The final long-recovery pass added four checks beyond the initial 116-check matrix: nonzero nested offsets and scrolling to the modal's final marker at each of two 200% recovery variants.", "",
        "## Measured defects and repairs", "",
        "| Control / region | Reproduced before | Repaired contract |", "| --- | --- | --- |",
    ])
    for row in payload["measured_repairs"]:
        lines.append(f"| `{row['control']}` | {row['before']} | {row['after_contract']} |")
    lines.extend(["", "## Matrix accounting", "",
        "| Group | Checks | Captures |", "| --- | ---: | ---: |"])
    for group, count in payload["matrix"]["expected_check_groups"].items():
        lines.append(f"| `{group}` | {count} | {payload['matrix']['expected_capture_groups'].get(group, 0)} |")
    lines.extend([
        f"| **Total** | **{payload['matrix']['expected_checks']}** | **{payload['matrix']['expected_captures']}** |", "",
        "At desktop 200%, `hallStream` is the outer scroller. At phone 200%, `.hall-main` takes that role while the transcript grows with its content. The walk records the active host, starts with nonzero outer and pinned-conclusion offsets, scrolls the dialog to `uiv2-28-conclusion-end`, and requires Escape to restore the Expand trigger and every original offset exactly.", "",
        "## Empty, populated, loading, error and recovery coverage", "",
        "The adversarial walk supplies the common seeded state and route-created long/recovery state. The other real states retain their specialist producers from the approved cross-tab contract; the preservation suite runs them rather than painting states into screenshots.", "",
        "| Pane | State | Producer |", "| --- | --- | --- |",
    ])
    for pane in payload["state_coverage"].values():
        for state, producer in pane["states"].items():
            lines.append(f"| {pane['title']} | {state} | `{producer}` |")
    lines.extend(["", "## Visual approval and preservation", "",
        "The UIV2-26 48-variant actual/reference/annotated matrix remains mandatory. Accessibility changes that alter the approved structural signature require a fresh candidate and explicit human approval; a passing accessibility walk cannot approve its own pixels.", "",
        "The source-derived UIV2-02 control/API map (currently 273 controls and 121 mounted routes) and the UIV2-27 21-row preservation run remain mandatory. No backend route, payload, session/workspace rule, authorization, sandbox, action-ledger, lease-fencing or model-memory boundary is changed by this story.", "",
        "## Reproduce", "", "```bash",
        "node scripts/uiv2_accessibility_adversarial_walk.mjs --screenshots /tmp/uiv2-28-adversarial-after",
        "python3 scripts/uiv2_accessibility_adversarial.py --import-evidence /tmp/uiv2-28-adversarial-after/capture-manifest.json",
        "python3 scripts/uiv2_accessibility_adversarial.py --check",
        "node scripts/a11y_walk.mjs --screenshots /tmp/uiv2-28-final-a11y",
        "node scripts/responsive_walk.mjs --screenshots /tmp/uiv2-28-final-responsive",
        "node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out /tmp/uiv2-28-cross-tab-after --timeout-ms 60000",
        "```", "",
        "Browser environment and pinned Chromium setup are the same as `.github/workflows/ci.yml`. The browser-safety CI job runs this walk plus the legacy keyboard and responsive gates.", "",
        f"Contract findings: **{len(payload['findings'])}**.", "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--import-evidence", type=Path)
    args = parser.parse_args(argv)
    if args.import_evidence:
        try:
            run = json.loads(args.import_evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"evidence import refused: {exc}", file=sys.stderr)
            return 1
        errors = validate_run(run)
        if errors:
            print("evidence import refused:\n" + "\n".join(errors), file=sys.stderr)
            return 1
        RUN_PATH.write_text(dumps(run), encoding="utf-8")
    payload = build()
    if args.json:
        print(dumps(payload), end="")
        return 1 if payload["findings"] else 0
    if args.check:
        valid = JSON_PATH.is_file() and DOC_PATH.is_file() and RUN_PATH.is_file()
        if valid:
            valid = JSON_PATH.read_text(encoding="utf-8") == dumps(payload) and DOC_PATH.read_text(encoding="utf-8") == render_doc(payload)
        if not valid or payload["findings"]:
            for finding in payload["findings"]:
                print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
            print("UIV2-28 artifacts are stale or invalid; import a fresh green run and regenerate", file=sys.stderr)
            return 1
        print(f"UIV2-28 contract OK: {EXPECTED_TOTAL}/{EXPECTED_TOTAL} source-bound checks, {EXPECTED_CAPTURES} captures, 0 findings")
        return 0
    if payload["findings"]:
        for finding in payload["findings"]:
            print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
        return 1
    JSON_PATH.write_text(dumps(payload), encoding="utf-8")
    DOC_PATH.write_text(render_doc(payload), encoding="utf-8")
    print(f"wrote {JSON_PATH.relative_to(ROOT)}, {RUN_PATH.relative_to(ROOT)} and {DOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
