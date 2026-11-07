"""Keyboard and screen-reader regression (UIUX-P5-02).

The shell could be driven by a keyboard since P1 — the rail is a tablist, the arrows move it, Alt+N
opens a pane — and nothing measured what happened after the rail. Tabbing through the six panes found
three defects of one shape: the two composers took focus with the textarea's ring off and nothing in
its place, the starter cards and preset chips were `div`/`span` elements with an `onclick` and no way
in from a keyboard, and the event stream took focus without drawing where it went.

These tests read the inventory and break each rule in a copy of the source, so a rule that stopped
looking fails here rather than in production. The walk (`scripts/a11y_walk.mjs`) is what presses the
keys; it is not run here, because it needs a browser and a live fixture, and a test that skipped
itself and reported success would be the very failure this story is about.
"""

from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "a11y_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_keyboard_a11y_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Keyboard_A11y.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-keyboard-a11y.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "keyboard-a11y"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_keyboard_a11y_inventory", INVENTORY)
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
        walk_path = tmp_path / "a11y_walk.mjs"
        html_path.write_text(html, encoding="utf-8")
        walk_path.write_text(walk, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "WALK", walk_path)
        if test_body is not None:
            test_path = tmp_path / "test_keyboard_a11y.py"
            test_path.write_text(test_body, encoding="utf-8")
            monkeypatch.setattr(inventory, "TEST", test_path)
        return inventory.build_inventory()

    return _make


def failing(report: dict) -> list[str]:
    return [entry["check"] for entry in report["findings"]]


def check_named(report: dict, needle: str) -> dict:
    hits = [entry for entry in report["checks"] if needle in entry["check"]]
    assert hits, f"no check matched {needle!r}"
    return hits[0]


# ── the inventory itself ────────────────────────────────────────────────────


def test_the_inventory_is_clean(payload):
    assert payload["findings"] == [], failing(payload)
    assert payload["check_count"] >= 25


def test_the_artifact_on_disk_is_the_one_the_inventory_builds(payload):
    assert JSON_PATH.exists(), "run scripts/uiux_keyboard_a11y_inventory.py"
    assert json.loads(JSON_PATH.read_text(encoding="utf-8")) == json.loads(
        inventory.dumps(payload)
    ), "product/M5_2_0_UIUX_Keyboard_A11y.json has drifted; regenerate it"


def test_the_doc_is_the_one_the_inventory_renders(payload):
    assert DOC_PATH.exists(), "run scripts/uiux_keyboard_a11y_inventory.py"
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)


def test_the_doc_is_linked_from_the_docs_index():
    index = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert DOC_PATH.name in index, "a doc nobody can find is not documentation"


def test_the_inventory_passes_its_own_check_mode():
    assert inventory.main(["--check", "--quiet"]) == 0


# ── 1. nothing is mouse-only ───────────────────────────────────────────────


def test_a_click_target_without_the_activation_attributes_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            'role="button" tabindex="0" data-key-activate onclick="toggleSkill(',
            'onclick="toggleSkill(',
            1,
        )

    report = sources(html_transform=strip)
    assert failing(report), "a pointer-only control went unreported"


def test_a_backdrop_without_a_declared_exception_would_be_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace('class="modal-backdrop"', 'class="modal-sheet"')

    report = sources(html_transform=strip)
    assert "the click targets that take no tab stop are only the declared dismiss-only backdrops" in failing(report) or (
        "every click target is a native control or carries the role, the tab stop and the activation hook"
        in failing(report)
    )


def test_the_count_of_non_native_controls_is_pinned(sources):
    def add_one(html: str) -> str:
        return html.replace(
            '<div class="chronicle-item ${s.id === activeHarnessId ? \'active\' : \'\'}" role="button" tabindex="0" data-key-activate onclick="selectHarnessSession',
            '<div class="chronicle-item ${s.id === activeHarnessId ? \'active\' : \'\'}" role="button" tabindex="0" data-key-activate onclick="selectHarnessSession',
            1,
        ).replace(
            '<span class="chip" role="button" tabindex="0" data-key-activate onclick="selectProviderPreset',
            '<span class="chip" role="button" tabindex="0" data-key-activate onclick="selectProviderPreset',
            1,
        ) + '<span class="chip" role="button" tabindex="0" data-key-activate onclick="noop()">x</span>'

    report = sources(html_transform=add_one)
    assert "the remaining seven non-native controls retain delegated keyboard activation" in failing(report)


# ── 2. one delegated handler ───────────────────────────────────────────────


def test_a_handler_that_forgets_space_is_a_finding(sources):
    def forget_space(html: str) -> str:
        return html.replace(
            'if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;',
            'if (event.key !== "Enter") return;',
            1,
        )

    report = sources(html_transform=forget_space)
    assert "one delegated keydown turns Enter and Space into the click the pointer would have made" in failing(report)


def test_a_handler_that_does_not_click_is_a_finding(sources):
    def no_click(html: str) -> str:
        return html.replace("target.click();", "void target;", 1)

    report = sources(html_transform=no_click)
    assert "one delegated keydown turns Enter and Space into the click the pointer would have made" in failing(report)


def test_an_inline_key_handler_is_a_finding(sources):
    def inline(html: str) -> str:
        return html.replace(
            'type="button" class="chip" onclick="setCouncilPreset',
            'type="button" class="chip" onkeydown="return false" onclick="setCouncilPreset',
            1,
        )

    report = sources(html_transform=inline)
    assert "no inline key handler runs from markup" in failing(report)


# ── 3. the ring ────────────────────────────────────────────────────────────


def test_removing_the_composer_box_ring_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            "  .prompt-box:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }",
            "",
            1,
        )

    report = sources(html_transform=strip)
    assert "the composer draws the focus on the box instead, at the width the global ring uses" in failing(report)


def test_a_hairline_composer_ring_is_a_finding(sources):
    def thin(html: str) -> str:
        return html.replace(
            ".prompt-box:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }",
            ".prompt-box:focus-within { outline: 1px solid var(--ring); outline-offset: 2px; }",
            1,
        )

    report = sources(html_transform=thin)
    assert "the composer draws the focus on the box instead, at the width the global ring uses" in failing(report)


def test_removing_the_stream_ring_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace(
            "  .harness-stream:focus-visible, .hall-stream:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }",
            "",
            1,
        )

    report = sources(html_transform=strip)
    assert "a scroll region that takes focus draws a ring of its own" in failing(report)


def test_a_hairline_global_ring_is_a_finding(sources):
    def thin(html: str) -> str:
        return html.replace(
            "    outline: 2px solid var(--ring); outline-offset: 2px;\n",
            "    outline: 1px solid var(--ring); outline-offset: 2px;\n",
            1,
        )

    report = sources(html_transform=thin)
    assert "the global focus ring is drawn with `:focus-visible` at 2px or more" in failing(report)


# ── 4. the rail, the panes, the order ──────────────────────────────────────


def test_a_pane_labelled_by_the_wrong_tab_is_a_finding(sources):
    def wrong(html: str) -> str:
        return html.replace(
            '<section id="pane-start" role="tabpanel" aria-labelledby="tab-start"',
            '<section id="pane-start" role="tabpanel" aria-labelledby="tab-chat"',
            1,
        )

    report = sources(html_transform=wrong)
    assert "each pane is a `tabpanel` labelled by the tab that controls it" in failing(report)


def test_a_second_tabbable_tab_is_a_finding(sources):
    def second(html: str) -> str:
        return html.replace(
            'aria-selected="true" tabindex="0" onclick="switchTab(\'harness\')"',
            'aria-selected="true" tabindex="0" onclick="switchTab(\'harness\')"',
            1,
        ).replace(
            'aria-selected="false" tabindex="-1" onclick="switchTab(\'chat\')"',
            'aria-selected="false" tabindex="0" onclick="switchTab(\'chat\')"',
            1,
        )

    report = sources(html_transform=second)
    assert "exactly one tab is in the tab order, so a pane is one Tab away and not six" in failing(report)


def test_a_positive_tabindex_is_a_finding(sources):
    def positive(html: str) -> str:
        return html.replace('<textarea id="councilPrompt"', '<textarea tabindex="3" id="councilPrompt"', 1)

    report = sources(html_transform=positive)
    assert "nothing is pushed to the front of the tab order with a positive tabindex" in failing(report)


# ── 5. names ───────────────────────────────────────────────────────────────


def test_an_unlabelled_control_is_a_finding(sources):
    def strip(html: str) -> str:
        return html.replace('<label class="field-lbl" for="provWire">', '<label class="field-lbl">', 1)

    report = sources(html_transform=strip)
    assert "every form control in the markup carries a name source a screen reader can read" in failing(report)


def test_a_control_wrapped_in_its_own_label_still_counts_as_named(payload):
    # `#ocPinned` has no `for` and no `aria-label`: the `<label>` wraps it. The rule has to see that,
    # or it would push the fix towards a second name instead of the one the reader already has.
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'for="ocPinned"' not in html
    assert 'aria-label="' not in html.split('id="ocPinned"', 1)[0].rsplit("<input", 1)[-1]
    assert payload["names"]["unnamed"] == 0


def test_the_composer_names_itself(payload):
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'id="debatePrompt" aria-label="What to compare"' in html
    assert 'id="councilPrompt" aria-label="Pipeline task"' in html


def test_a_pane_that_stopped_announcing_is_a_finding(sources):
    def silent(html: str) -> str:
        return html.replace('aria-live="polite"', 'aria-live="off"', 3)

    report = sources(html_transform=silent)
    assert "the panes announce their state instead of only drawing it" in failing(report)


# ── 6. the dialog contract ─────────────────────────────────────────────────


def test_an_overlay_that_forgets_its_opener_is_a_finding(sources):
    def forget(html: str) -> str:
        return html.replace(
            "const opener = returnTarget(document.activeElement) || returnTarget(lastFocusable);",
            "const opener = null;",
            1,
        )

    report = sources(html_transform=forget)
    assert "a dialog takes `aria-modal` when it opens and remembers what had focus" in failing(report)


def test_a_closer_that_strands_focus_is_a_finding(sources):
    def strand(html: str) -> str:
        start = html.find("  function closeOverlay(id) {")
        assert start > 0
        end = html.find("\n  }", start)
        body = html[start:end]
        assert ".focus(" in body
        return html[:start] + body.replace(".focus(", ".blur(") + html[end:]

    report = sources(html_transform=strand)
    assert "closing a dialog gives focus back to the control that opened it" in failing(report)


def test_a_trap_that_ignores_shift_tab_is_a_finding(sources):
    def unshifted(html: str) -> str:
        start = html.find("  function overlayTrapTab(event) {")
        assert start > 0
        body = inventory.function_body(html, "overlayTrapTab")
        assert "shiftKey" in body
        return html[:start] + body.replace("shiftKey", "shiftedKey") + html[start + len(body):]

    report = sources(html_transform=unshifted)
    assert "Tab is trapped inside the open dialog rather than wandering into the page behind it" in failing(report)


# ── 7. the evidence ────────────────────────────────────────────────────────


def test_a_walk_that_forgets_the_names_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace("name: nameOf(el),", "name: \"\",", 1)

    report = sources(walk_transform=forget)
    assert "the walk presses keys on a real browser and answers the three questions per stop" in failing(report)


def test_a_walk_that_forgets_a_pane_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace('"hall", "council"', '"council"', 1)

    report = sources(walk_transform=forget)
    assert "the walk covers all six panes and the two controls that were mouse-only" in failing(report)


def test_a_walk_that_can_pass_without_pressing_escape_is_a_finding(sources):
    def forget(walk: str) -> str:
        return walk.replace('page.keyboard.press("Escape");', "await sleep(1);")

    report = sources(walk_transform=forget)
    assert "the walk proves the overlay traps focus, closes on Escape and gives focus back" in failing(report)


def test_the_walk_refuses_to_report_a_walk_it_did_not_perform():
    walk = WALK.read_text(encoding="utf-8")
    assert "refuses to report a walk it did not perform" in walk
    assert "STUDIO_SCREENSHOT_CHROME" in walk
    assert "process.exitCode = 1" in walk
    # A walk that skips on a missing dependency and reports success is the failure mode here.
    assert "process.exit(0)" not in walk.replace("process.exit(0);\n        break;", "")


def test_every_capture_the_doc_lists_exists_and_is_a_png():
    payload = inventory.build_inventory()
    for name in payload["captures"]:
        path = MEDIA / name
        assert path.exists(), f"missing capture {name}: run node scripts/a11y_walk.mjs --screenshots docs/media/keyboard-a11y"
        head = path.read_bytes()[:8]
        assert head == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        width, height = struct.unpack(">II", path.read_bytes()[16:24])
        assert width >= 400 and height >= 300, f"{name} is {width}x{height}"


def test_the_walk_names_every_capture_the_doc_lists():
    walk = WALK.read_text(encoding="utf-8")
    for name in inventory.CAPTURES:
        assert f'"{name[:-4]}"' in walk, f"the doc lists {name} but the walk never writes it"


def test_the_ledger_records_this_story_as_done():
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    story = next((entry for entry in ledger["stories"] if entry["id"] == "UIUX-P5-02"), None)
    assert story is not None
    tracking = story.get("tracking", {})
    assert tracking.get("status") == "done", f"status is {tracking.get('status')!r}"
    assert tracking.get("evidence"), "a done story carries evidence"
    assert tracking.get("test_results"), "a done story carries the commands that ran"


def test_configured_integration_toggles_cannot_revert_to_non_native_pills(sources):
    report = sources(html_transform=lambda html: html.replace(
        '<button type="button" class="skill-pill integration-row',
        '<span class="skill-pill integration-row', 1,
    ))
    assert "configured integration toggles are native pressed-state buttons" in failing(report)
