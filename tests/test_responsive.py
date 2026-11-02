"""Responsive and touch-target regression (UIUX-P5-03).

The panes were built at 1440 px. Narrowed to 480 and 360 they lost their right-hand third with no
scrollbar to reach it — a `<select>` is as wide as its longest `<option>`, and a pane shell centred
with `margin: 0 auto` sizes to its content instead of the pane — and a row of controls sat under the
24 px floor a finger needs, one of them because an inline `min-height: 20px` had escaped the density
tokens.

These tests read the inventory and break each rule in a copy of the source, so a rule that stopped
looking fails here rather than in production. The walk (`scripts/responsive_walk.mjs`) is what resizes
the window and taps with a finger; it is not run here, because it needs a browser and a live fixture.
"""

from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "responsive_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_responsive_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Responsive.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-responsive.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "responsive"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_responsive_inventory", INVENTORY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at a copy, so a rule can be broken in it."""

    def _make(html_transform=None, walk_transform=None, test_body=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        walk = WALK.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if walk_transform:
            changed = walk_transform(walk)
            assert changed != walk, "the transform did not change the walk, so the test proves nothing"
            walk = changed
        html_path = tmp_path / "index.html"
        walk_path = tmp_path / "responsive_walk.mjs"
        html_path.write_text(html, encoding="utf-8")
        walk_path.write_text(walk, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "WALK", walk_path)
        if test_body is not None:
            test_path = tmp_path / "test_responsive.py"
            test_path.write_text(test_body, encoding="utf-8")
            monkeypatch.setattr(inventory, "TEST", test_path)
        return inventory.build_inventory()

    return _make


def failing(report: dict) -> list[str]:
    return [entry["check"] for entry in report["findings"]]


# ── the inventory itself ───────────────────────────────────────────────────


def test_the_inventory_is_clean(payload):
    assert payload["findings"] == [], failing(payload)
    assert payload["check_count"] >= 18


def test_the_artifact_on_disk_is_the_one_the_inventory_builds(payload):
    assert JSON_PATH.exists(), "run scripts/uiux_responsive_inventory.py"
    assert json.loads(JSON_PATH.read_text(encoding="utf-8")) == json.loads(inventory.dumps(payload))


def test_the_doc_is_the_one_the_inventory_renders(payload):
    assert DOC_PATH.exists(), "run scripts/uiux_responsive_inventory.py"
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)


def test_the_doc_is_linked_from_the_docs_index():
    index = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert DOC_PATH.name in index


def test_the_inventory_passes_its_own_check_mode():
    assert inventory.main(["--check", "--quiet"]) == 0


# ── 1. the target-size floor ───────────────────────────────────────────────


def test_a_missing_target_token_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace("  --target-min: 24px;\n", "", 1)

    report = sources(html_transform=strip)
    assert "the stylesheet carries one target-size floor, and it is 24px" in failing(report)


def test_a_smaller_floor_is_a_finding(sources):
    def shrink(html: str) -> str:
        return html.replace("  --target-min: 24px;", "  --target-min: 16px;", 1)

    report = sources(html_transform=shrink)
    assert "the stylesheet carries one target-size floor, and it is 24px" in failing(report)


def test_density_moving_the_floor_is_a_finding(sources):
    def density(html: str) -> str:
        return html.replace(
            'body[data-density="compact"] {\n  --control-min-h: 32px;',
            'body[data-density="compact"] {\n  --target-min: 16px;\n  --control-min-h: 32px;',
            1,
        )

    report = sources(html_transform=density)
    assert "density does not move the floor: a compact layout may look tighter, not be harder to hit" in failing(report)


def test_a_control_that_stops_reading_the_token_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            ".harness-perm-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 10.8px; font-weight: 700; letter-spacing: 0.04em; padding: 3px 8px; border-radius: 999px; border: 1px solid var(--border); background: var(--bg-card); color: var(--text-dim); cursor: pointer; min-height: var(--target-min); }",
            ".harness-perm-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 10.8px; font-weight: 700; letter-spacing: 0.04em; padding: 3px 8px; border-radius: 999px; border: 1px solid var(--border); background: var(--bg-card); color: var(--text-dim); cursor: pointer; }",
            1,
        )

    report = sources(html_transform=strip)
    assert "every control that was under the floor reads the token" in failing(report)


def test_an_inline_style_under_the_floor_is_a_finding(sources):
    def sneaky(html: str) -> str:
        return html.replace(
            'style="padding:3px 6px;" onclick="saveWorkspaceDirectory()"',
            'style="padding:3px 6px; min-height:20px;" onclick="saveWorkspaceDirectory()"',
            1,
        )

    report = sources(html_transform=sneaky)
    assert "no inline style drops a control back under the floor" in failing(report)


# ── 2. a select may not set the width of the pane ─────────────────────────


def test_removing_the_select_shrink_rule_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace("select { min-width: 0; max-width: 100%; }\n", "", 1)

    report = sources(html_transform=strip)
    assert "a select and the flex row it sits in are allowed to shrink" in failing(report)


def test_a_content_sized_shell_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            "  .onboarding-shell { width: 100%; max-width: 860px; margin: 0 auto; padding: 18px 16px 28px; }",
            "  .onboarding-shell { max-width: 860px; margin: 0 auto; padding: 18px 16px 28px; }",
            1,
        )

    report = sources(html_transform=strip)
    assert "the Start shell has a definite width, so `margin: 0 auto` cannot size it to its content" in failing(report)


def test_a_pinned_shell_width_is_a_finding(sources):
    def pin(html: str) -> str:
        return html.replace(
            "  .onboarding-shell { width: 100%;",
            "  .onboarding-shell { width: 640px;",
            1,
        )

    report = sources(html_transform=pin)
    assert "no pane shell is pinned to a pixel width that a narrow pane would clip" in failing(report)


def test_an_arbiter_picker_that_cannot_shrink_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            '<div id="debateArbiterModel" style="min-width:0;">',
            '<div id="debateArbiterModel">',
            1,
        )

    report = sources(html_transform=strip)
    assert "the Compare arbiter picker is a flex item that may shrink below its model label" in failing(report)


# ── 3. the breakpoints are a list ──────────────────────────────────────────


def test_an_undocumented_breakpoint_is_a_finding(sources):
    def extra(html: str) -> str:
        return html.replace(
            "@media (max-width: 390px) {",
            "@media (max-width: 412px) { .brand { font-size: 12px; } }\n@media (max-width: 390px) {",
            1,
        )

    report = sources(html_transform=extra)
    assert "the stylesheet has the six documented breakpoints and no others" in failing(report)


def test_a_stepper_that_stopped_scrolling_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            ".stepper { grid-template-columns: repeat(5, minmax(112px, 1fr)); overflow-x: auto; }",
            ".stepper { grid-template-columns: repeat(5, minmax(112px, 1fr)); }",
            1,
        )

    report = sources(html_transform=strip)
    assert "the one strip that is allowed to overflow scrolls, so the cards past the edge are reachable" in failing(report)


# ── 4. the evidence ────────────────────────────────────────────────────────


def test_a_walk_that_forgets_a_width_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace('{ width: 1024, height: 768, label: "1024", touch: false },\n', "", 1)

    report = sources(walk_transform=forget)
    assert "the walk measures all five widths in all six panes" in failing(report)


def test_a_walk_that_measures_css_instead_of_the_box_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace("getBoundingClientRect", "offsetWidth")

    report = sources(walk_transform=forget)
    assert "the walk measures a hit area, not a CSS declaration" in failing(report)


def test_a_walk_without_a_finger_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace("page.tap(", "page.click(")

    report = sources(walk_transform=forget)
    assert "the walk uses a finger as well as a mouse" in failing(report)


def test_a_walk_that_hides_its_exceptions_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace("scrollersSeen.add(name)", "void name")

    report = sources(walk_transform=forget)
    assert "the walk names the strips that scroll, rather than silently excusing them" in failing(report)


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    walk = WALK.read_text(encoding="utf-8")
    assert "refuses to report a walk it did not perform" in walk
    assert "STUDIO_SCREENSHOT_CHROME" in walk
    assert "process.exitCode = 1" in walk


def test_every_capture_the_doc_lists_exists_and_is_a_png():
    payload = inventory.build_inventory()
    for name in payload["captures"]:
        path = MEDIA / name
        assert path.exists(), f"missing capture {name}: run node scripts/responsive_walk.mjs --screenshots docs/media/responsive"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        width, height = struct.unpack(">II", path.read_bytes()[16:24])
        assert width >= 300 and height >= 300, f"{name} is {width}x{height}"


def test_the_walk_names_every_capture_the_doc_lists():
    walk = WALK.read_text(encoding="utf-8")
    for name in inventory.CAPTURES:
        assert f'"{name[:-4]}"' in walk, f"the doc lists {name} but the walk never writes it"


def test_the_ledger_records_this_story_as_done():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next((entry for entry in ledger["stories"] if entry["id"] == "UIUX-P5-03"), None)
    assert story is not None
    tracking = story.get("tracking", {})
    assert tracking.get("status") == "done"
    assert tracking.get("evidence") and tracking.get("test_results")
