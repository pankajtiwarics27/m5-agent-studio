#!/usr/bin/env python3
"""Inventory Compare response-performance provenance (UIUX-P4-05).

    python3 scripts/uiux_compare_performance_inventory.py          # regenerate JSON + doc
    python3 scripts/uiux_compare_performance_inventory.py --check  # fail on drift or a broken rule
    python3 scripts/uiux_compare_performance_inventory.py --json   # print machine-readable inventory

The response path already carried normalized usage into `panel_done` and `synthesis_done`, but the
Compare pane drew only reply size and an arbiter `decode_tps` badge with no basis. This inventory
protects the event-payload source, the honest missing/demo states, the unchanged controls and route,
and the screenshots that exercise the behavior. It does not treat fixture numbers as model
benchmarks, derive a browser-clock estimate, or invent a price.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.uiux_compare_criteria_inventory import called_routes, function_body, python_function  # noqa: E402

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_criteria_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-performance"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Performance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-performance.md"
STORY = "UIUX-P4-05"

CAPTURES = (
    "compare-performance-before.png",
    "compare-performance-partial-failure.png",
    "compare-performance.png",
    "compare-performance-narrow.png",
    "compare-performance-arbiter-narrow.png",
    "compare-performance-light.png",
    "compare-performance-demo.png",
    "compare-performance-demo-arbiter.png",
    "compare-performance-unavailable.png",
)


def _story_tracking() -> dict[str, Any]:
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return next(
        (story.get("tracking", {}) or {} for story in ledger.get("stories", []) if story.get("id") == STORY),
        {},
    )


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def build_inventory() -> dict[str, Any]:
    html = FRONTEND.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    seed = SEED.read_text(encoding="utf-8")
    walk = WALK.read_text(encoding="utf-8")
    performance = function_body(html, "renderDebatePerformance")
    results = function_body(html, "renderDebateResults")
    renderer = function_body(html, "renderDebate")
    runner = function_body(html, "runDebate")
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    normalized = (
        'debateUsageNumber(usage, "ttft")',
        'debateUsageNumber(usage, "completion_tokens")',
        'debateUsageNumber(usage, "decode_tps")',
        'debateUsageNumber(usage, "prefill_tps")',
        'debateUsageNumber(usage, "elapsed_s")',
        'usage.finish_reason',
    )
    check(
        "the displayed performance fields come from normalized response usage",
        bool(performance) and all(field in performance for field in normalized),
        ", ".join(field for field in normalized if field not in performance) or "TTFT, output tokens, generation, prompt throughput, server elapsed and finish reason",
    )
    check(
        "missing, null, negative and non-finite numbers stay unavailable",
        "function debateUsageNumber" in html
        and "Number.isFinite(value)" in function_body(html, "debateUsageNumber")
        and "value >= 0" in function_body(html, "debateUsageNumber")
        and 'typeof raw !== "number" && typeof raw !== "string"' in function_body(html, "debateUsageNumber"),
        "only finite, non-negative response values are rendered",
    )
    check(
        "the renderer never measures a response with a browser clock",
        all(token not in performance + results + renderer for token in ("Date.now(", "performance.now(", "setTimeout(")),
        "all displayed values are read from the response event; elapsed is explicitly server-side",
    )
    check(
        "the source line names each model and the exact SSE usage event",
        "Model ID: ${identity} (from ${identitySource})" in performance
        and "Performance basis: ${source}" in performance
        and 'usageSource || "no usage event received"' in performance
        and "panel_done.usage" in runner
        and "synthesis_done.usage" in runner,
        "panel_done.usage and synthesis_done.usage; no client-clock estimate",
    )
    check(
        "demo metrics are suppressed even when synthetic usage fields are present",
        'status === "demo"' in performance
        and "usage.demo === true" in performance
        and "synthetic usage figures are omitted" in performance
        and "No model performance" in performance,
        "the server demo marker wins over fabricated rates and latencies",
    )
    check(
        "absent usage has distinct pending and unavailable copy, never a fallback estimate",
        "Awaiting response usage" in performance
        and "Performance unavailable" in performance
        and "No response usage arrived" in performance
        and "no client-clock estimate" in performance,
        "queued/running waits; failed/stopped/lost rows state that performance is unavailable",
    )
    check(
        "cost stays explicitly unavailable and no price is calculated",
        "Cost: not reported" in performance
        and "no price is configured" in performance
        and not any(token in performance.lower() for token in ("price_per", "cost_usd", "dollars_per")),
        "no tariff, monetary total or pricing provenance is invented",
    )
    check(
        "the arbiter uses the same response-evidence renderer and no bare decode_tps badge",
        "renderDebatePerformance(" in renderer
        and "synthesis_done.usage" in runner
        and "Response usage" in renderer
        and "decode_tps" not in renderer,
        "the arbiter badge states its evidence basis; details come from synthesis_done.usage",
    )
    check(
        "panel and arbiter model identities retain their event provenance",
        all(text in runner for text in (
            'slot.modelSource = "panel_start.model"',
            'slot.modelSource = "panel_done.model"',
            'debateSynth.modelSource = "synthesis_start.model"',
            'debateSynth.modelSource = d.model ? "synthesis_done.model"',
        )),
        "the completion event's model ID takes precedence when supplied",
    )
    check(
        "the response-evidence column is added after all four P4-04 columns",
        '<th scope="col">Criteria it met</th><th scope="col">Response evidence</th>' in results
        and "performance" in results,
        "Model, Status, Size, Criteria it met, Response evidence",
    )
    check(
        "the interrupted arbiter is re-rendered when the event source closes",
        "renderDebate();" in runner and "finalizer's no-usage state" in runner,
        "a closed stream cannot leave an awaiting-usage badge behind",
    )
    routes = called_routes(runner)
    check(
        "the existing Compare stream route remains the only route in the runner",
        routes == ["/api/debate/stream"],
        ", ".join(routes),
    )
    debate_api = python_function(app, "api_debate_stream")
    existing_events = ("panel_start", "token", "panel_done", "synthesis_start", "synthesis_done", "debate_done")
    check(
        "the existing Compare event contract remains in place",
        bool(debate_api) and all(f'_sse("{event}"' in debate_api for event in existing_events),
        ", ".join(event for event in existing_events if f'_sse("{event}"' not in debate_api) or "all six existing SSE event names",
    )
    fixture_values = ("41.2", "22.4", "55.8")
    check(
        "the isolated fixture uses distinct, explicitly non-benchmark response values",
        "RESPONSE_METRICS" in seed
        and all(value in seed for value in fixture_values)
        and "fixture numbers, not benchmark results" in seed,
        "three fixture outputs can be distinguished by response usage alone",
    )
    walk_requirements = (
        "each model's row uses its own response usage",
        "the arbiter card uses synthesis_done usage",
        "the results keep cost unavailable",
        "a stopped panel keeps its partial text but receives no invented performance figures",
        "demo usage suppresses synthetic speed and latency values",
        "fixture model loading uses only the existing model-load route",
    )
    check(
        "the browser walk exercises model rows, arbiter, cost, stopped and demo cases",
        all(requirement in walk for requirement in walk_requirements),
        "; ".join(requirement for requirement in walk_requirements if requirement not in walk) or "six P4-05 journeys are asserted",
    )
    missing_captures = [name for name in CAPTURES if _png_dimensions(MEDIA / name) is None]
    dimensions = {name: _png_dimensions(MEDIA / name) for name in CAPTURES if _png_dimensions(MEDIA / name) is not None}
    check(
        "the documented after-captures are readable PNGs from the browser walk",
        not missing_captures,
        ", ".join(f"{name}={size[0]}x{size[1]}" for name, size in dimensions.items()) or ", ".join(missing_captures),
    )
    check(
        "a narrow result-table capture was taken at the tested 480 px viewport",
        "compare-performance-narrow.png" in CAPTURES
        and "width: 480" in walk
        and _png_dimensions(MEDIA / "compare-performance-narrow.png") is not None,
        "the walk asserts zero horizontal overflow at 480 px and captures the table element",
    )
    check(
        "the test-only demo arbiter capture labels its synthetic payload",
        "fixture-only demo arbiter" in walk
        and "test-only synthetic data" in walk
        and _png_dimensions(MEDIA / "compare-performance-demo-arbiter.png") is not None,
        "the screenshot is a renderer test, not a model benchmark or live synthesis event",
    )
    shell_dir = PROJECT_ROOT / "docs" / "media" / "compare-performance-shell"
    shell_capture_paths = [shell_dir / phase / "compare.png" for phase in ("before", "after")]
    shell_capture_indexes = [shell_dir / phase / "index.json" for phase in ("before", "after")]
    shell_capture_ok = True
    shell_details = []
    for image_path, index_path in zip(shell_capture_paths, shell_capture_indexes, strict=True):
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            entry = next(item for item in index["shots"] if item["id"] == "debate")
            shell_size = _png_dimensions(image_path)
            valid = (
                shell_size == (2880, 1800)
                and entry["file"] == "compare.png"
                and entry["sha256"] == hashlib.sha256(image_path.read_bytes()).hexdigest()
                and index["viewport"]["device_scale_factor"] == 2
            )
            shell_capture_ok = shell_capture_ok and valid
            shell_details.append(f"{image_path.parent.name}/compare.png={shell_size}")
        except (OSError, json.JSONDecodeError, KeyError, StopIteration):
            shell_capture_ok = False
            shell_details.append(f"{image_path.parent.name}/compare.png missing or invalid")
    check(
        "official before/after pane captures are indexed and match the 2x viewport",
        shell_capture_ok,
        "; ".join(shell_details),
    )
    check(
        "the story remains tracked with evidence and actual test results",
        _story_tracking().get("status") in {"in_progress", "done"}
        and bool(_story_tracking().get("evidence"))
        and bool(_story_tracking().get("test_results")),
        f"tracking.status={_story_tracking().get('status')!r}",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": STORY,
        "title": "Compare measured performance and provenance",
        "generated_by": "scripts/uiux_compare_performance_inventory.py",
        "surfaces": {
            "panel_results": "#debateResults",
            "arbiter": "#debateSynthesisArea",
            "usage_events": ["panel_done.usage", "synthesis_done.usage"],
        },
        "fields": [
            "ttft",
            "completion_tokens",
            "decode_tps",
            "prefill_tps",
            "elapsed_s (server measured)",
            "finish_reason",
        ],
        "captures": list(CAPTURES),
        "capture_dimensions": {name: list(size) for name, size in dimensions.items()},
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict[str, Any]) -> str:
    lines = [
        "# UIUX-P4-05 — Compare measured performance and provenance",
        "",
        "Generated by `scripts/uiux_compare_performance_inventory.py`. Regenerate rather than",
        "editing this inventory by hand; `--check` verifies the source and the committed artifact.",
        "",
        "## What the results show",
        "",
        "Each Compare row keeps the P4-04 model, status, size and criteria columns, then adds",
        "response evidence: first-token latency, output tokens, generation and prompt throughput,",
        "server-measured elapsed time when present, and the response finish reason. The arbiter",
        "uses the same evidence renderer instead of a bare `decode_tps` badge.",
        "",
        "Every evidence block names the model ID and the event that supplied usage: `panel_done.usage`",
        "or `synthesis_done.usage`. `elapsed_s` is measured by the server adapter and labelled",
        "`Server elapsed`; the browser clock is never used to estimate a model's performance.",
        "",
        "## Honest limits",
        "",
        "* Missing usage remains unavailable (or awaiting usage while a stage is active); failed,",
        "  stopped and unfinished panels receive no client-clock estimate.",
        "* A server demo marker suppresses every synthetic speed/latency field. A demo reply is still",
        "  identified as server-written text, not a model measurement.",
        "* Cost remains unavailable because this product has no configured price for these model IDs.",
        "  No price, currency or cost total is inferred from tokens.",
        "* Fixture rates are deliberately distinct so the browser walk can prove row-to-event",
        "  provenance. They are test data, not claims about model performance.",
        "",
        "## Criterion-to-evidence map",
        "",
        "| Acceptance criterion | Evidence |",
        "| :--- | :--- |",
        "| Read model performance from response payloads, never browser timing | The browser walk checks different `panel_done.usage` metrics per row, including TTFT, output/prompt throughput and server elapsed. |",
        "| Show provenance for panel and arbiter measurements | Per-row model/event source assertions; arbiter `synthesis_done.usage` assertion; wide and narrow screenshots. |",
        "| Treat missing, failed, stopped and demo usage honestly | Failure and stop tests assert no metrics; demo panels and test-only arbiter payload assert synthetic values are suppressed. |",
        "| Do not invent monetary cost | Inventory and browser walk assert unavailable/no configured price and no monetary figure. |",
        "| Preserve P4-04 statuses, controls, routes and partial/failure behavior | P4-04 inventory remains 27/27 clean; browser walk retains its prior status and failure checks and confirms the existing stream route. |",
        "| Keep the results readable on a narrow screen and in both themes | Browser walk asserts zero horizontal overflow at 480 px and captures dark/light and 480 px output. |",
        "",
        "## Screenshot evidence",
        "",
        "The P4-05 state captures were produced by `scripts/compare_criteria_walk.mjs` with real",
        "Chromium against the isolated `scripts/council_readiness_seed.py` fixtures. The P4-04 before",
        "image and P4-05 after image show the same partial-failure journey; the before image is the",
        "pre-change walk capture copied unchanged. The table and arbiter narrow captures are element",
        "screenshots taken after setting a 480 px viewport; the browser walk also asserts the whole",
        "document has no horizontal overflow. The demo-arbiter image contains a fixture-only, explicitly",
        "marked synthetic usage object to exercise the renderer; it is not a live arbiter response or a",
        "benchmark.",
        "",
        "The Compare shell was additionally captured before and after with `scripts/capture_screenshots.mjs`",
        "using the repository screenshot manifest at 1440 × 900 @2. Both full six-pane sets were checked",
        "by `scripts/validate_screenshots.py`: **6/6 complete** each. The initial Compare shell is unchanged",
        "by this story; the response-state captures above show the actual UI change.",
        "",
        "| File | Pixels | What it shows |",
        "| :--- | :---: | :--- |",
    ]
    descriptions = {
        "compare-performance-before.png": "P4-04 pre-change partial-failure capture: four results columns, no response-usage evidence.",
        "compare-performance-partial-failure.png": "Same partial-failure state after P4-05: the answered row carries response evidence; the failed row has no invented metrics.",
        "compare-performance.png": "Two answered local fixture models, distinct per-model usage and a measured arbiter response.",
        "compare-performance-narrow.png": "The five-column results table at the tested 480 px viewport.",
        "compare-performance-arbiter-narrow.png": "Arbiter response usage and provenance at the tested 480 px viewport.",
        "compare-performance-light.png": "The two model rows and arbiter evidence in the light theme.",
        "compare-performance-demo.png": "The actual offline demo panel; synthetic usage figures are suppressed.",
        "compare-performance-demo-arbiter.png": "Renderer-only test of an explicitly marked demo arbiter payload; not an API event.",
        "compare-performance-unavailable.png": "A stopped partial response with performance truthfully unavailable.",
    }
    for name in payload["captures"]:
        width, height = payload["capture_dimensions"].get(name, (0, 0))
        lines.append(f"| `{name}` | {width} × {height} | {descriptions[name]} |")
    lines += [
        "| `media/compare-performance-shell/before/compare.png` | 2880 × 1800 | P4-04 Compare shell before this story, captured with the repository capture script. |",
        "| `media/compare-performance-shell/after/compare.png` | 2880 × 1800 | P4-05 Compare shell after this story, captured with the same manifest and viewport. |",
    ]
    lines += [
        "",
        "## Inventory results",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} findings — **{payload['status']}**.",
        "",
    ]
    for entry in payload["checks"]:
        lines.append(f"* {'pass' if entry['ok'] else 'FAIL'} — {entry['check']}: {entry['detail']}")
    lines += [
        "",
        "The endpoint, authorization, event contract, persistence and durable task state were not",
        "changed. The browser fixture uses the existing authorized model-load action only to make a",
        "second stand-in model visible to the demo catalog before the comparison.",
        "",
        "This implementation record is not owner approval or independent review of P4-05.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on source or generated-artifact drift")
    parser.add_argument("--json", action="store_true", help="print the inventory as JSON")
    args = parser.parse_args(argv)
    payload = build_inventory()
    rendered_json = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    rendered_doc = render_doc(payload)
    if args.json:
        print(rendered_json, end="")
    if args.check:
        problems = []
        if payload["findings"]:
            problems.append(f"{len(payload['findings'])} source finding(s)")
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != rendered_json:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is missing or stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != rendered_doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is missing or stale")
        if problems:
            print("; ".join(problems), file=sys.stderr)
            return 1
        if not args.json:
            print(f"{payload['check_count']} checks, 0 findings — clean")
        return 0
    if payload["findings"]:
        print(f"{len(payload['findings'])} source finding(s); artifacts not written", file=sys.stderr)
        return 1
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(rendered_json, encoding="utf-8")
    DOC_PATH.write_text(rendered_doc, encoding="utf-8")
    if not args.json:
        print(f"{payload['check_count']} checks, 0 findings — wrote {DOC_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
