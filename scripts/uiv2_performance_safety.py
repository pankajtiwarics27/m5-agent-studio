#!/usr/bin/env python3
"""UIV2-29 performance and backend-safety evidence gate.

    python3 scripts/uiv2_performance_safety.py --run-backend
    python3 scripts/uiv2_performance_safety.py --import-evidence /tmp/uiv2-29/capture-manifest.json
    python3 scripts/uiv2_performance_safety.py --check

The browser walk proves real SSE, DOM/heap and layout behavior. This runner also
executes the backend safety suite and isolation smoke test, then binds both run
records to exact source digests. Static checks alone are never completion evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATIC_PATH = ROOT / "static" / "index.html"
WALK_PATH = ROOT / "scripts" / "uiv2_performance_safety_walk.mjs"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
JSON_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Performance_Safety.json"
RUN_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Performance_Run.json"
BACKEND_RUN_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Backend_Safety_Run.json"
DOC_PATH = ROOT / "docs" / "uiv2-v2-performance-safety.md"
CROSS_TAB_PATH = ROOT / "product" / "M5_2_0_UIUX_V2_Cross_Tab_Reference.json"

VIEWPORTS = ("1640x900", "1440x900", "1280x800", "390x844")
THEMES = ("light", "dark")
EXPECTED_GROUPS = {"browser": 2, "sse": 4, "dom-memory": 7, "layout": 8}
EXPECTED_CAPTURE_GROUPS = {"sse": 3, "layout": 8}
EXPECTED_TOTAL = sum(EXPECTED_GROUPS.values())
EXPECTED_CAPTURES = sum(EXPECTED_CAPTURE_GROUPS.values())
THRESHOLDS = {
    "render_window": 300,
    "cache_limit": 5000,
    "frame_budget_ms": 4,
    "heap_plateau_bytes": 8 * 1024 * 1024,
    "heap_baseline_bytes": 16 * 1024 * 1024,
    "dom_plateau_nodes": 64,
    "layout_shift_budget": 0.01,
    "min_fast_sse_per_second": 50,
}
TEST_MODULES = (
    "tests/test_stream_perf.py",
    "tests/test_task_events.py",
    "tests/test_harness_permissions.py",
    "tests/test_scoped_grants.py",
    "tests/test_harness_recovery.py",
    "tests/test_recovery_guard.py",
    "tests/test_process_supervision.py",
    "tests/test_side_effect_ledger.py",
    "tests/test_durable_intent_ledger.py",
    "tests/test_worker_leases_and_fencing.py",
    "tests/test_isolation_smoke.py",
    "tests/test_chat_recovery.py",
    "tests/test_pipeline_recovery.py",
)
BACKEND_PRODUCTION_SOURCES = (
    "static/index.html",
    "app.py",
    "task_events.py",
    "scoped_grants.py",
    "recovery_guard.py",
    "process_supervision.py",
    "side_effect_ledger.py",
    "sandbox_boundary.py",
    "scripts/isolation_smoke.py",
)
REQUIRED_FRONTEND = {
    "ordered-merge-fast-path": "if (!(existing || []).length) {",
    "stable-readonly-snapshot": "return harnessCachedSnapshot;",
    "incremental-ordered-append": "if (appendToSnapshot) harnessCachedSnapshot.push(ev);",
    "out-of-order-invalidation": "else harnessCachedSnapshot = null;",
    "incremental-prune": "harnessCachedSnapshot.splice(0, dropped.length);",
    "snapshot-render-guard": "supplied === harnessCachedSnapshot && harnessCachedSnapshot",
    "general-merge-retained": ": harnessMergeEvents([], supplied);",
    "note-write-only-on-change": "if (note && note.textContent !== text) note.textContent = text;",
    "timeline-write-only-on-change": "if (count.textContent !== countText) count.textContent = countText;",
    "bounded-window": "const HARNESS_RENDER_WINDOW = 300;",
    "bounded-cache": "const HARNESS_CACHE_LIMIT = 5000;",
}
REQUIRED_WALK = {
    "real-fixture": '"--slow-model", SLOW_MODEL',
    "fast-fixture": 'const FAST_MODEL = "Gemma-4-21B-REAP-4bit";',
    "high-frequency-floor": "const MIN_FAST_SSE_PER_SECOND = 50;",
    "production-render-wrap": "window.__uiv29OriginalRender = window.renderChatMessages;",
    "production-schedule-wrap": "window.__uiv29OriginalSchedule = window.scheduleChatRender;",
    "heap-collection": 'await cdp.send("HeapProfiler.collectGarbage");',
    "cdp-metrics": 'await cdp.send("Performance.getMetrics");',
    "layout-observer": 'observer.observe({ type: "layout-shift", buffered: false });',
    "external-attribution": "cls_external_shell: Number(layout.externalCls.toFixed(5))",
    "external-budget": "layout.externalCls <= LAYOUT_SHIFT_BUDGET",
    "dom-plateau": "nodeDelta <= DOM_PLATEAU_NODES",
    "heap-plateau": "heapCycleDelta <= HEAP_PLATEAU_BYTES",
    "two-stress-cycles": "const cycle2 = await stressCycle(18001);",
    "thirty-thousand-cursor": "cycle2.cursor === 30000",
    "p95-nearest-rank": "Math.ceil(sorted.length * 0.95) - 1",
    "source-binding": "source_sha256: sourceHashes()",
    "outbound-block": "outbound.push(target.origin);",
    "anonymous-refusal": "anonymous.status === 401 && authenticated.status === 404",
    "capture-manifest": '"capture-manifest.json"',
}
REQUIRED_WORKFLOW = {
    "ci-legacy-stream": "node scripts/stream_perf_walk.mjs",
    "ci-browser-performance": "node scripts/uiv2_performance_safety_walk.mjs",
    "ci-backend-performance": "python scripts/uiv2_performance_safety.py --run-backend",
    "ci-backend-output": "--backend-output",
    "ci-required-browser": "M5_REQUIRE_BROWSER_TESTS=1",
}
COVERAGE = {
    "high_frequency_sse": ["scripts/uiv2_performance_safety_walk.mjs", "scripts/stream_perf_walk.mjs"],
    "bounded_dom_and_memory": ["scripts/uiv2_performance_safety_walk.mjs", "tests/test_stream_perf.py"],
    "permissions_and_authorization": ["tests/test_harness_permissions.py", "tests/test_scoped_grants.py", "tests/test_durable_intent_ledger.py"],
    "crash_and_stream_recovery": ["tests/test_recovery_guard.py", "tests/test_process_supervision.py", "tests/test_harness_recovery.py", "tests/test_chat_recovery.py", "tests/test_pipeline_recovery.py"],
    "action_ledger_and_lease_fencing": ["tests/test_side_effect_ledger.py", "tests/test_durable_intent_ledger.py", "tests/test_worker_leases_and_fencing.py"],
    "blocked_isolation": ["tests/test_isolation_smoke.py", "scripts/isolation_smoke.py"],
}


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def browser_source_hashes() -> dict[str, str]:
    return {"static/index.html": sha256(STATIC_PATH), "scripts/uiv2_performance_safety_walk.mjs": sha256(WALK_PATH)}


def backend_source_hashes() -> dict[str, str]:
    paths = [*BACKEND_PRODUCTION_SOURCES, *TEST_MODULES]
    return {name: sha256(ROOT / name) for name in paths}


def source_hashes() -> dict[str, str]:
    return {**browser_source_hashes(), ".github/workflows/ci.yml": sha256(WORKFLOW_PATH)}


def source_findings(html: str, walk: str, workflow: str | None = None) -> list[dict[str, str]]:
    ci = WORKFLOW_PATH.read_text(encoding="utf-8") if workflow is None else workflow
    findings = []
    for name, token in {**REQUIRED_FRONTEND, **REQUIRED_WALK, **REQUIRED_WORKFLOW}.items():
        haystack = html if name in REQUIRED_FRONTEND else walk if name in REQUIRED_WALK else ci
        if token not in haystack:
            findings.append({"check": name, "detail": f"required token is absent: {token}"})
    render = html[html.find("  function renderStoredHarnessEvents(events)"):html.find("  function appendWorkflowApprovalCard")]
    if "if (plan)" not in render or render.find('querySelectorAll("details")') < render.find("if (plan)"):
        findings.append({"check": "incremental-open-details", "detail": "disclosure scanning must happen only on the full-rebuild path"})
    return findings


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def validate_browser_run(payload: Any, hashes: dict[str, str] | None = None) -> list[str]:
    if not isinstance(payload, dict):
        return ["browser manifest is not an object"]
    errors = []
    if payload.get("story") != "UIV2-29":
        errors.append("browser manifest story is not UIV2-29")
    checks, captures = payload.get("checks"), payload.get("captures")
    if not isinstance(checks, list) or not isinstance(captures, list):
        return [*errors, "browser checks/captures must be lists"]
    expected_totals = {"checks": EXPECTED_TOTAL, "passed": EXPECTED_TOTAL, "failed": 0, "captures": EXPECTED_CAPTURES}
    if payload.get("totals") != expected_totals:
        errors.append(f"browser totals must be {expected_totals}")
    if len(checks) != EXPECTED_TOTAL or any(row.get("passed") is not True for row in checks):
        errors.append("all 21 browser checks must be present and true")
    groups = Counter(row.get("group") for row in checks)
    if dict(groups) != EXPECTED_GROUPS:
        errors.append(f"browser groups must be {EXPECTED_GROUPS}, got {dict(groups)}")
    capture_groups = Counter(row.get("group") for row in captures)
    if len(captures) != EXPECTED_CAPTURES or dict(capture_groups) != EXPECTED_CAPTURE_GROUPS:
        errors.append(f"capture groups must be {EXPECTED_CAPTURE_GROUPS}")
    if len({row.get("path") for row in captures}) != EXPECTED_CAPTURES:
        errors.append("capture paths must be unique")
    for capture in captures:
        if not str(capture.get("path", "")).endswith(".png") or not re.fullmatch(r"[0-9a-f]{64}", str(capture.get("sha256", ""))):
            errors.append("every capture needs a PNG path and SHA-256")
            break
    layout = {(row.get("viewport"), row.get("theme")) for row in checks if row.get("group") == "layout"}
    if layout != {(viewport, theme) for viewport in VIEWPORTS for theme in THEMES}:
        errors.append("layout matrix must equal four viewports × two themes")
    for row in (row for row in checks if row.get("group") == "layout"):
        try:
            detail = json.loads(row.get("detail", ""))
        except json.JSONDecodeError:
            errors.append("layout detail is not measured JSON")
            continue
        if detail.get("cls_external_shell", 1) > THRESHOLDS["layout_shift_budget"] or detail.get("overflow", 1) > 0 or detail.get("max_geometry_delta", 1) > 0.5 or detail.get("cards") != 300:
            errors.append(f"layout metrics are over budget for {row.get('viewport')} {row.get('theme')}")
    if payload.get("source_sha256") != (hashes if hashes is not None else browser_source_hashes()):
        errors.append("browser manifest is stale or its source digests do not match this checkout")
    if payload.get("thresholds") != THRESHOLDS:
        errors.append("browser thresholds were removed or weakened")
    git = payload.get("git", {})
    if not re.fullmatch(r"[0-9a-f]{40}", str(git.get("head", ""))) or not isinstance(git.get("dirty"), bool):
        errors.append("browser manifest must record Git head and dirty state")
    environment = payload.get("environment", {})
    for key in ("node", "platform", "arch", "chrome_version", "python", "user_agent", "memory_method", "layout_method"):
        if not environment.get(key):
            errors.append(f"browser environment is missing {key}")
    if not _finite(environment.get("cpu_count")) or environment.get("cpu_count", 0) <= 0:
        errors.append("browser environment must record a positive CPU count")
    metrics = payload.get("metrics", {})
    fast = metrics.get("chat", {}).get("fast", {})
    if not (_finite(fast.get("schedules")) and fast.get("schedules", 0) >= 20 and _finite(fast.get("renders")) and 0 < fast.get("renders", 0) < fast.get("schedules", 0) and _finite(fast.get("sse_per_second")) and fast.get("sse_per_second", 0) >= 50 and _finite(fast.get("elapsed_ms")) and fast.get("elapsed_ms", 0) > 0):
        errors.append("high-frequency SSE metrics are absent, impossible or below the measured floor")
    harness = metrics.get("harness", {})
    steady = harness.get("steady", {})
    if not (_finite(steady.get("mean_ms")) and 0 < steady.get("mean_ms", 0) <= 4 and steady.get("calls") == 20 and steady.get("cards") == 300 and _finite(steady.get("p95_ms")) and _finite(steady.get("max_ms")) and steady.get("max_ms", -1) >= steady.get("p95_ms", 0)):
        errors.append("steady full-window timing is missing or over budget")
    memory = harness.get("memory", {})
    baseline, cycle1, cycle2 = (memory.get(name, {}) for name in ("baseline", "after_cycle_1", "after_cycle_2"))
    for name, sample in (("baseline", baseline), ("cycle1", cycle1), ("cycle2", cycle2)):
        for key in ("Nodes", "JSHeapUsedSize"):
            if not _finite(sample.get(key)) or sample.get(key, 0) < 0:
                errors.append(f"memory {name} lacks a measured {key}")
    stress2 = memory.get("cycle_2", {})
    if stress2.get("cursor") != 30000 or stress2.get("cached") != 5000 or stress2.get("cards") != 300:
        errors.append("30,000-event bounded cache/cursor/DOM metrics are missing")
    if all(_finite(sample.get("Nodes")) for sample in (baseline, cycle1, cycle2)):
        if cycle2["Nodes"] - cycle1["Nodes"] > 64 or cycle2["Nodes"] > baseline["Nodes"] + 128:
            errors.append("post-GC DOM nodes exceed the plateau budget")
    if all(_finite(sample.get("JSHeapUsedSize")) for sample in (baseline, cycle1, cycle2)):
        if cycle2["JSHeapUsedSize"] - cycle1["JSHeapUsedSize"] > THRESHOLDS["heap_plateau_bytes"] or cycle2["JSHeapUsedSize"] - baseline["JSHeapUsedSize"] > THRESHOLDS["heap_baseline_bytes"]:
            errors.append("post-GC heap exceeds the plateau budget")
    return sorted(set(errors))


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    return {"head": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def _pytest_passed(output: str) -> int:
    matches = re.findall(r"(\d+) passed", output)
    return int(matches[-1]) if matches else 0


def run_backend(output_path: Path = BACKEND_RUN_PATH) -> dict[str, Any]:
    started = time.monotonic()
    pytest_command = [sys.executable, "-m", "pytest", "-q", "--tb=short", *TEST_MODULES]
    pytest_result = subprocess.run(pytest_command, cwd=ROOT, capture_output=True, text=True, timeout=900, check=False)
    isolation_command = [sys.executable, "scripts/isolation_smoke.py"]
    isolation = subprocess.run(isolation_command, cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    pytest_output = (pytest_result.stdout + pytest_result.stderr).strip()
    isolation_output = (isolation.stdout + isolation.stderr).strip()
    status_match = re.search(r"isolation_status:\s*(\w+)", isolation_output)
    dependent_match = re.search(r"dependent_execution:\s*(.+)", isolation_output)
    failed_match = re.search(r"checks_failed:\s*(\d+)", isolation_output)
    runtime_match = re.search(r"runtime_available:\s*(yes|no)", isolation_output)
    passed = _pytest_passed(pytest_output)
    payload = {
        "story": "UIV2-29",
        "kind": "backend-safety-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_state(),
        "source_sha256": backend_source_hashes(),
        "environment": {
            "python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "container_runtime_available": runtime_match.group(1) if runtime_match else "unknown",
            "external_services": "none; fakes, temporary paths and loopback only",
        },
        "commands": {"pytest": "python -m pytest -q --tb=short " + " ".join(TEST_MODULES), "isolation": "python scripts/isolation_smoke.py"},
        "test_modules": list(TEST_MODULES),
        "pytest": {"exit_code": pytest_result.returncode, "passed": passed, "seconds": None, "output_tail": pytest_output.splitlines()[-45:]},
        "isolation": {
            "exit_code": isolation.returncode,
            "status": status_match.group(1) if status_match else "missing",
            "dependent_execution": dependent_match.group(1).strip() if dependent_match else "missing",
            "checks_failed": int(failed_match.group(1)) if failed_match else -1,
            "pass_count": len(re.findall(r"\[PASS\]", isolation_output)),
            "live_probes": "not requested; container isolation is not claimed",
            "output": isolation_output.splitlines(),
        },
        "coverage": COVERAGE,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    payload["totals"] = {
        "test_modules": len(TEST_MODULES), "tests_passed": passed,
        "failed": int(pytest_result.returncode != 0) + int(isolation.returncode != 0),
        "isolation_pass_checks": payload["isolation"]["pass_count"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dumps(payload), encoding="utf-8")
    return payload


def validate_backend_run(payload: Any, hashes: dict[str, str] | None = None) -> list[str]:
    if not isinstance(payload, dict):
        return ["backend run is not an object"]
    errors = []
    if payload.get("story") != "UIV2-29" or payload.get("kind") != "backend-safety-run":
        errors.append("backend run identity is not UIV2-29")
    if payload.get("source_sha256") != (hashes if hashes is not None else backend_source_hashes()):
        errors.append("backend run is stale or its source digests do not match this checkout")
    if payload.get("test_modules") != list(TEST_MODULES):
        errors.append("backend safety module coverage drifted")
    pytest = payload.get("pytest", {})
    if pytest.get("exit_code") != 0 or not isinstance(pytest.get("passed"), int) or pytest.get("passed", 0) < 200:
        errors.append("backend pytest must execute at least 200 tests with exit code zero")
    if re.search(r"\b(failed|error|skipped)\b", "\n".join(pytest.get("output_tail", [])), re.I):
        # A summary such as "0 failed" is not printed by pytest; any such word in the tail is
        # treated as unsafe rather than trying to make a passing sentence look like a result.
        errors.append("backend pytest output contains a failure/error/skip marker")
    isolation = payload.get("isolation", {})
    if isolation.get("exit_code") != 0 or isolation.get("checks_failed") != 0 or isolation.get("pass_count", 0) < 15:
        errors.append("isolation smoke checks did not all pass")
    if isolation.get("status") != "unqualified" or isolation.get("dependent_execution") != "blocked (fail closed)":
        errors.append("unprobed container isolation must remain unqualified and blocked")
    if isolation.get("live_probes") != "not requested; container isolation is not claimed":
        errors.append("isolation evidence overclaims live container probes")
    if payload.get("coverage") != COVERAGE:
        errors.append("backend acceptance coverage map drifted")
    environment = payload.get("environment", {})
    if not environment.get("python") or not environment.get("platform") or not environment.get("cpu_count"):
        errors.append("backend environment fingerprint is incomplete")
    git = payload.get("git", {})
    if not re.fullmatch(r"[0-9a-f]{40}", str(git.get("head", ""))) or not isinstance(git.get("dirty"), bool):
        errors.append("backend run must record Git head and dirty state")
    return errors


def load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build() -> dict[str, Any]:
    findings = source_findings(STATIC_PATH.read_text(encoding="utf-8"), WALK_PATH.read_text(encoding="utf-8"))
    browser = load(RUN_PATH)
    backend = load(BACKEND_RUN_PATH)
    browser_errors = ["no imported all-green browser run"] if browser is None else validate_browser_run(browser)
    backend_errors = ["no executed backend safety run"] if backend is None else validate_backend_run(backend)
    findings.extend({"check": "browser-evidence", "detail": error} for error in browser_errors)
    findings.extend({"check": "backend-evidence", "detail": error} for error in backend_errors)
    cross_tab = json.loads(CROSS_TAB_PATH.read_text(encoding="utf-8"))
    approval = cross_tab.get("approval", {})
    return {
        "schema_version": 1,
        "document_type": "m5_uiv2_performance_safety_contract",
        "story": "UIV2-29",
        "statement": "Measured streaming, DOM/heap, layout and backend safety evidence; unprobed container isolation remains unqualified and blocked.",
        "source_sha256": source_hashes(),
        "thresholds": THRESHOLDS,
        "matrix": {"viewports": list(VIEWPORTS), "themes": list(THEMES), "expected_check_groups": EXPECTED_GROUPS, "expected_capture_groups": EXPECTED_CAPTURE_GROUPS, "expected_checks": EXPECTED_TOTAL, "expected_captures": EXPECTED_CAPTURES},
        "measured_repair": {
            "before": "isolated current-Chromium baseline: 4.67ms for a ten-event frame at a full 300-card/5000-event window; 14/15 legacy checks",
            "implementation": "ordered snapshot append/prune, stable read-only snapshots, cached-snapshot render guard, no redundant note/count writes, disclosure scans only on full rebuilds",
            "preserved": "general out-of-order/dedupe merge, approval-decision scan, cursor semantics, 300-card window, 5000-event cache, reader offset and open disclosures",
        },
        "coverage": COVERAGE,
        "visual_reference": {
            "contract": "product/M5_2_0_UIUX_V2_Cross_Tab_Reference.json",
            "approved_manifest": approval.get("candidate", {}).get("manifest_sha256"),
            "approval_reference": approval.get("human_approval", {}).get("reference"),
            "policy": "the approved 48-variant structural gate remains mandatory; performance-only changes may not approve their own pixels",
        },
        "browser_evidence": {"run": str(RUN_PATH.relative_to(ROOT)), "status": "passed" if not browser_errors else "blocked", "totals": browser.get("totals") if browser and not browser_errors else None, "environment": browser.get("environment") if browser and not browser_errors else None, "metrics": browser.get("metrics") if browser and not browser_errors else None, "validation_errors": browser_errors},
        "backend_evidence": {"run": str(BACKEND_RUN_PATH.relative_to(ROOT)), "status": "passed" if not backend_errors else "blocked", "totals": backend.get("totals") if backend and not backend_errors else None, "environment": backend.get("environment") if backend and not backend_errors else None, "isolation": backend.get("isolation") if backend and not backend_errors else None, "validation_errors": backend_errors},
        "findings": findings,
    }


def _mib(value: float) -> str:
    return f"{value / (1024 * 1024):.2f} MiB"


def render_doc(payload: dict[str, Any]) -> str:
    browser, backend = payload["browser_evidence"], payload["backend_evidence"]
    lines = [
        "# UIV2-29 — performance and backend safety regression gate", "",
        payload["statement"], "",
        "## Fail-closed evidence policy", "",
        "The contract does not create its own evidence. A green result requires a source-bound real-Chromium run, an executed backend pytest suite, and the isolation smoke test. Stale, partial, failed or threshold-weakened records are rejected.", "",
        "PNG captures remain local review evidence; the committed browser record carries their SHA-256 digests and exact frontend/walk digests. CI reruns both the legacy stream walk and this gate.", "",
    ]
    if browser["status"] == "passed":
        totals, metrics, env = browser["totals"], browser["metrics"], browser["environment"]
        fast, steady, memory = metrics["chat"]["fast"], metrics["harness"]["steady"], metrics["harness"]["memory"]
        lines += [
            "## Measured browser run", "",
            f"**{totals['passed']}/{totals['checks']} checks passed; {totals['captures']} captures; 0 failures.**", "",
            f"Environment: `{env['platform']}` / `{env['arch']}`, {env['cpu_count']} CPUs, Node `{env['node']}`, `{env['python']}`, `{env['chrome_version']}` headless Chromium.", "",
            "| Metric | Actual | Gate |", "| --- | ---: | ---: |",
            f"| Fast real SSE schedules | {fast['schedules']} in {fast['elapsed_ms']:.1f}ms ({fast['sse_per_second']:.1f}/s) | ≥20 and ≥50/s |",
            f"| Coalesced chat renders | {fast['renders']} | fewer than {fast['schedules']} schedules, complete text retained |",
            f"| Full-window ten-event frame | mean {steady['mean_ms']:.2f}ms; nearest-rank p95 {steady['p95_ms']:.2f}ms; max {steady['max_ms']:.2f}ms | mean ≤4ms |",
            f"| Events / cache / cards | 30,000 / {memory['cycle_2']['cached']:,} / {memory['cycle_2']['cards']} | 5,000 / 300 ceilings |",
            f"| Post-GC document nodes | {memory['baseline']['Nodes']} → {memory['after_cycle_1']['Nodes']} → {memory['after_cycle_2']['Nodes']} | repeated-cycle growth ≤64 |",
            f"| Post-GC JS heap | {_mib(memory['baseline']['JSHeapUsedSize'])} → {_mib(memory['after_cycle_1']['JSHeapUsedSize'])} → {_mib(memory['after_cycle_2']['JSHeapUsedSize'])} | cycle growth ≤8 MiB; baseline growth ≤16 MiB |", "",
            "Memory readings use CDP `Performance.getMetrics` immediately after `HeapProfiler.collectGarbage`. They are repeated-cycle plateau checks, not a claim that a browser never allocates during a frame.", "",
            "### Layout stability by viewport and theme", "",
            "Streaming intentionally moves cards inside the trajectory scroller. The gate reports that internal movement separately and requires zero meaningful shift **outside** it, stable topbar/stream/composer rectangles, no document overflow, and 300 cards. Detached attribution nodes are classified by their previous/current rectangles; unattributable entries count as external and fail closed.", "",
            "| Viewport | Theme | Total shift | Internal stream | External shell | Geometry delta | Overflow |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in metrics["layout"]:
            lines.append(f"| {row['viewport']} | {row['theme']} | {row['cls_total']:.5f} | {row['cls_internal_stream']:.5f} | {row['cls_external_shell']:.5f} | {row['max_geometry_delta']:.2f}px | {row['overflow']}px |")
        lines.append("")
    else:
        lines += ["**BLOCKED: browser evidence is missing or invalid.**", ""] + [f"- {error}" for error in browser["validation_errors"]] + [""]
    if backend["status"] == "passed":
        totals, env, isolation = backend["totals"], backend["environment"], backend["isolation"]
        lines += [
            "## Executed backend and isolation run", "",
            f"**{totals['tests_passed']} tests passed across {totals['test_modules']} owning modules; {isolation['pass_count']} isolation checks passed; 0 failures.**", "",
            f"Environment: Python `{env['python']}`, `{env['platform']}`, {env['cpu_count']} CPUs. No external service, provider, user workspace or live container probe is used.", "",
            f"Container runtime available: **{env['container_runtime_available']}**. Isolation status: **{isolation['status']}**; dependent execution: **{isolation['dependent_execution']}**. This is intentional fail-closed behavior, not an isolation qualification.", "",
            "| Acceptance area | Executed owners |", "| --- | --- |",
        ]
        for area, owners in payload["coverage"].items():
            lines.append(f"| {area.replace('_', ' ')} | " + ", ".join(f"`{owner}`" for owner in owners) + " |")
        lines.append("")
    else:
        lines += ["**BLOCKED: backend evidence is missing or invalid.**", ""] + [f"- {error}" for error in backend["validation_errors"]] + [""]
    lines += [
        "## Measured rendering repair", "",
        f"- **Before:** {payload['measured_repair']['before']}.",
        f"- **Implementation:** {payload['measured_repair']['implementation']}.",
        f"- **Preserved:** {payload['measured_repair']['preserved']}.", "",
        "The pre-existing 4ms budget was not relaxed. The general merge still handles out-of-order, duplicate and invalid input, and the approval-decision scan was not cached away.", "",
        "## Visual and preservation contract", "",
        "The approved UIV2-26 cross-tab structural gate remains binding at 1640×900, 1440×900, 1280×800 and 390×844 in light and dark. This story's eleven actual captures add loading, populated and 30,000-event long-content evidence; they do not self-approve a structural delta. The UIV2-27 preservation run and UIV2-28 accessibility suites remain mandatory.", "",
        "## Reproduce", "", "```bash",
        "python3 scripts/uiv2_performance_safety.py --run-backend",
        "node scripts/stream_perf_walk.mjs --screenshots /tmp/uiv2-29-stream",
        "node scripts/uiv2_performance_safety_walk.mjs --screenshots /tmp/uiv2-29-performance",
        "python3 scripts/uiv2_performance_safety.py --import-evidence /tmp/uiv2-29-performance/capture-manifest.json",
        "python3 scripts/uiv2_performance_safety.py --check",
        "node scripts/uiux_v2_cross_tab_reference_walk.mjs --check --out /tmp/uiv2-29-cross-tab", "```", "",
        f"Contract findings: **{len(payload['findings'])}**.", "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-backend", action="store_true")
    parser.add_argument("--backend-output", type=Path, default=BACKEND_RUN_PATH)
    parser.add_argument("--import-evidence", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.run_backend:
        payload = run_backend(args.backend_output)
        errors = validate_backend_run(payload)
        if errors:
            print("backend safety gate failed:\n" + "\n".join(errors), file=sys.stderr)
            return 1
        print(f"UIV2-29 backend gate OK: {payload['totals']['tests_passed']} tests, {payload['isolation']['pass_count']} isolation checks, isolation unqualified and blocked")
        return 0
    if args.import_evidence:
        try:
            run = json.loads(args.import_evidence.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"evidence import refused: {exc}", file=sys.stderr)
            return 1
        errors = validate_browser_run(run)
        if errors:
            print("evidence import refused:\n" + "\n".join(errors), file=sys.stderr)
            return 1
        RUN_PATH.write_text(dumps(run), encoding="utf-8")
    payload = build()
    if args.json:
        print(dumps(payload), end="")
        return 1 if payload["findings"] else 0
    if args.check:
        valid = JSON_PATH.is_file() and DOC_PATH.is_file() and RUN_PATH.is_file() and BACKEND_RUN_PATH.is_file()
        if valid:
            valid = JSON_PATH.read_text(encoding="utf-8") == dumps(payload) and DOC_PATH.read_text(encoding="utf-8") == render_doc(payload)
        if not valid or payload["findings"]:
            for finding in payload["findings"]:
                print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
            print("UIV2-29 artifacts are stale or invalid; execute fresh green runs and regenerate", file=sys.stderr)
            return 1
        print(f"UIV2-29 contract OK: {EXPECTED_TOTAL}/{EXPECTED_TOTAL} browser checks, {EXPECTED_CAPTURES} captures, {payload['backend_evidence']['totals']['tests_passed']} backend tests, 0 findings")
        return 0
    if payload["findings"]:
        for finding in payload["findings"]:
            print(f"{finding['check']}: {finding['detail']}", file=sys.stderr)
        return 1
    JSON_PATH.write_text(dumps(payload), encoding="utf-8")
    DOC_PATH.write_text(render_doc(payload), encoding="utf-8")
    print(f"wrote {JSON_PATH.relative_to(ROOT)} and {DOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
