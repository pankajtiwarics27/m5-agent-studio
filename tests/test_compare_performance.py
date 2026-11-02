"""P4-05 response-metric provenance rules for the Compare pane."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "compare_criteria_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_compare_performance_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Performance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-performance.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-performance"

SPEC = importlib.util.spec_from_file_location("uiux_compare_performance_inventory", INVENTORY)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def _checks(payload: dict) -> set[str]:
    return {item["check"] for item in payload["findings"]}


def _with_frontend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transform) -> dict:
    source = FRONTEND.read_text(encoding="utf-8")
    changed = transform(source)
    assert changed != source, "the transform did not change the source, so the test proves nothing"
    path = tmp_path / "index.html"
    path.write_text(changed, encoding="utf-8")
    monkeypatch.setattr(inventory, "FRONTEND", path)
    return inventory.build_inventory()


def test_inventory_matches_the_source_and_generated_docs():
    result = subprocess.run(
        [sys.executable, str(INVENTORY), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_inventory_covers_the_response_fields_and_captures():
    payload = inventory.build_inventory()
    assert payload["status"] == "clean", payload["findings"]
    assert payload["check_count"] == len(payload["checks"]) >= 16
    assert payload["fields"] == [
        "ttft",
        "completion_tokens",
        "decode_tps",
        "prefill_tps",
        "elapsed_s (server measured)",
        "finish_reason",
    ]
    assert payload["captures"] == [
        "compare-performance-before.png",
        "compare-performance-partial-failure.png",
        "compare-performance.png",
        "compare-performance-narrow.png",
        "compare-performance-arbiter-narrow.png",
        "compare-performance-light.png",
        "compare-performance-demo.png",
        "compare-performance-demo-arbiter.png",
        "compare-performance-unavailable.png",
    ]


def test_browser_clock_performance_is_a_finding(tmp_path, monkeypatch):
    broken = _with_frontend(
        tmp_path,
        monkeypatch,
        lambda source: source.replace(
            'function renderDebatePerformance(usage, status, modelId, modelSource, usageSource) {',
            'function renderDebatePerformance(usage, status, modelId, modelSource, usageSource) {\n    const started = performance.now();',
            1,
        ),
    )
    assert "the renderer never measures a response with a browser clock" in _checks(broken)


def test_demo_marker_removal_is_a_finding(tmp_path, monkeypatch):
    broken = _with_frontend(
        tmp_path,
        monkeypatch,
        lambda source: source.replace(
            'status === "demo" || (usage && (usage.demo === true',
            'status === "demo" || (usage && (usage.demo === false',
            1,
        ),
    )
    assert "demo metrics are suppressed even when synthetic usage fields are present" in _checks(broken)


def test_missing_source_provenance_is_a_finding(tmp_path, monkeypatch):
    broken = _with_frontend(
        tmp_path,
        monkeypatch,
        lambda source: source.replace("Performance basis: ${source}", "Performance basis: browser", 1),
    )
    assert "the source line names each model and the exact SSE usage event" in _checks(broken)


def test_bare_arbiter_rate_badge_is_a_finding(tmp_path, monkeypatch):
    broken = _with_frontend(
        tmp_path,
        monkeypatch,
        lambda source: source.replace(
            'const badge = isDemo ? "Demo text · no model metrics"',
            'const decode_tps = usage && usage.decode_tps;\n      const badge = isDemo ? "Demo text · no model metrics"',
            1,
        ),
    )
    assert "the arbiter uses the same response-evidence renderer and no bare decode_tps badge" in _checks(broken)


def test_monetary_cost_claim_is_a_finding(tmp_path, monkeypatch):
    broken = _with_frontend(
        tmp_path,
        monkeypatch,
        lambda source: source.replace("Cost: not reported", "Cost: $0.02", 1),
    )
    assert "cost stays explicitly unavailable and no price is calculated" in _checks(broken)


def test_a_missing_narrow_capture_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "MEDIA", tmp_path)
    broken = inventory.build_inventory()
    assert "the documented after-captures are readable PNGs from the browser walk" in _checks(broken)
    assert "a narrow result-table capture was taken at the tested 480 px viewport" in _checks(broken)


def test_rendered_doc_and_payload_are_generated_artifacts():
    payload = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert payload["story"] == "UIUX-P4-05"
    assert "synthesis_done.usage" in doc
    assert "browser clock" in doc
    assert "not a model benchmark" in doc
    assert "scripts/capture_screenshots.mjs" in doc
    assert "6/6 complete" in doc
    assert "media/compare-performance-shell/before/compare.png" in doc
    assert "media/compare-performance-shell/after/compare.png" in doc
    for name in payload["captures"]:
        assert name in doc
        assert (MEDIA / name).is_file()


def test_fixture_rates_are_distinct_test_values_not_model_claims():
    spec = importlib.util.spec_from_file_location("council_readiness_seed_compare_performance", SEED)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rates = [module.RESPONSE_METRICS[model]["generation_tokens_per_second"] for model in (
        module.LOADED_MODEL,
        module.OVER_BUDGET_MODEL,
        module.FITS_MODEL,
    )]
    assert rates == [41.2, 22.4, 55.8]
    assert len(set(rates)) == 3
    assert "fixture numbers, not benchmark results" in SEED.read_text(encoding="utf-8")


def test_walk_checks_provenance_missing_data_demo_and_responsive_rendering():
    source = WALK.read_text(encoding="utf-8")
    for step in (
        "per-model performance is copied from panel_done usage",
        "each model's row uses its own response usage",
        "the arbiter card uses synthesis_done usage",
        "the results keep cost unavailable",
        "a stopped panel keeps its partial text but receives no invented performance figures",
        "demo usage suppresses synthetic speed and latency values",
        "the response records and arbiter evidence fit a 480 px viewport",
        "the response evidence and its provenance remain visible in the light theme",
    ):
        assert step in source
    assert "This script refuses to report a walk it did not perform." in source
