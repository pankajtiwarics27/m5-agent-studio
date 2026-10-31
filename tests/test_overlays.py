"""UIUX-P1-04: one overlay controller, one shortcut registry, one drawer rule.

Nine dialogs shared a pattern but not a controller. Each open function added `.open`,
each close function removed it, Escape removed it from all nine at once, none of them
trapped focus, and exactly one remembered where focus came from - so a keyboard user
tabbed out of an open dialog into the page behind it and, on close, landed wherever
the browser decided. The shortcuts had the same shape: the rail's keys lived in
`data-shortcut`, the palette's key lived in the handler, and the palette's *hints*
were string literals beside neither, which is how Alt+6 stayed advertised and dead
until P1-02.

These tests pin the replacement, and they are deliberately source-level where the
inventory is source-level:

* the committed inventory and doc are the generator's output, and `--check` agrees;
* every check in the generator is proved by breaking a *temporary copy* of the
  stylesheet - a rule that cannot fail is not a rule;
* no handler but the controller writes `.open`, and Escape closes the top overlay
  rather than every overlay;
* the shortcut registry owns every advertised key, the rail reads its own from the
  markup, and no palette hint is a literal any more;
* the drawer declares itself modal only while it covers the page.

What these tests cannot do is press a key: that is `scripts/overlay_walk.mjs`, which
opens all nine dialogs with real key events and reported 78 passing, 1 blocked on the
Harness-session prerequisite the app enforces, 0 failing.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "product" / "M5_2_0_UIUX_Overlays.json"
DOC = ROOT / "docs" / "uiux-2-0-overlays.md"
GENERATOR = ROOT / "scripts" / "uiux_overlay_inventory.py"
WALK = ROOT / "scripts" / "overlay_walk.mjs"
FRONTEND = ROOT / "static" / "index.html"

DIALOG_IDS = {
    "cmdPaletteModal",
    "contextModal",
    "mcpModal",
    "externalHarnessModal",
    "harnessDiffModal",
    "providersModal",
    "skillsModal",
    "sdlcModal",
    "spawnModal",
}


def _payload() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_overlay_inventory", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *replacements: tuple[str, str]):
    """Point the generator at a modified copy of the frontend, asserting each anchor."""
    source = FRONTEND.read_text(encoding="utf-8")
    for old, new in replacements:
        assert source.count(old) == 1, f"the stylesheet no longer contains {old[:70]!r}"
        source = source.replace(old, new)
    copy = tmp_path / "index.html"
    copy.write_text(source, encoding="utf-8")
    module = _load_generator()
    monkeypatch.setattr(module, "FRONTEND", copy)
    return module


def test_the_committed_artifacts_are_the_generators_output() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 findings" in result.stdout


def test_the_generator_reports_no_findings_on_the_tree() -> None:
    assert _load_generator().build_inventory()["findings"] == []


def test_all_nine_dialogs_are_named_labelled_and_wired() -> None:
    payload = _payload()
    ids = {dialog["id"] for dialog in payload["dialogs"]}
    assert ids == DIALOG_IDS
    assert payload["totals"]["dialogs_via_controller"] == 9
    for dialog in payload["dialogs"]:
        assert dialog["role"] == "dialog", dialog["id"]
        assert dialog["label"], dialog["id"]
        assert dialog["open_via_controller"] and dialog["close_via_controller"], dialog["id"]
        assert dialog["callers"], f'#{dialog["id"]} has nothing that opens it'


def test_a_dialog_that_touches_dot_open_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('  function closeSpawnModal() { closeOverlay("spawnModal"); }',
         '  function closeSpawnModal() { document.getElementById("spawnModal").classList.remove("open"); }'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "dialog-bypasses-controller" for finding in findings), findings
    assert any(finding["check"] == "open-written-outside-controller" for finding in findings), findings


def test_an_unlabelled_dialog_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('<div class="modal" role="dialog" aria-label="Spawn Council Session">',
         '<div class="modal" aria-label="Spawn Council Session">'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "dialog-unlabelled-role" for finding in findings), findings


def test_a_duplicate_shortcut_key_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('{ id: "command-palette", keys: ["Ctrl+K", "Meta+K"], label: "Command palette", where: "global" },',
         '{ id: "command-palette", keys: ["Ctrl+K", "Meta+K", "Escape"], label: "Command palette", where: "global" },'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "shortcut-duplicate-key" for finding in findings), findings


def test_a_palette_hint_that_is_a_literal_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('{ label: "Open Harness", shortcut: "tab-harness", run: () => switchTab("harness") },',
         '{ label: "Open Harness", hint: "Alt+1", run: () => switchTab("harness") },'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "shortcut-literal-hint" for finding in findings), findings


def test_a_palette_item_naming_an_unknown_shortcut_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('{ label: "Open Chat", shortcut: "tab-chat", run: () => switchTab("chat") },',
         '{ label: "Open Chat", shortcut: "tab-nope", run: () => switchTab("chat") },'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "shortcut-unknown-ref" for finding in findings), findings


def test_escape_that_closes_every_dialog_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ("        closeTopOverlay();\n        return;",
         '        document.querySelectorAll(".modal-backdrop.open").forEach(m => m.classList.remove("open"));\n        return;'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "escape-closes-everything" for finding in findings), findings


def test_the_inspector_must_declare_whether_it_is_a_drawer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _patched(
        monkeypatch,
        tmp_path,
        ('      if (rail) {\n        if (expanded) rail.setAttribute("aria-modal", "true");\n        else rail.removeAttribute("aria-modal");\n      }',
         '      void rail;'),
    )
    findings = module.build_inventory()["findings"]
    assert any(finding["check"] == "inspector-drawer" for finding in findings), findings


def test_the_controller_is_the_only_writer_of_open() -> None:
    html = FRONTEND.read_text(encoding="utf-8")
    # openOverlay paints, closeOverlay unpaints, and closeOverlay has one fallback for
    # a backdrop closed without a stack entry. Three, all inside the controller.
    writers = html.count('classList.add("open")') + html.count('classList.remove("open")')
    assert writers == 3, "only the controller may paint a backdrop"
    assert "function openOverlay(" in html and "function closeOverlay(" in html
    assert "inert = true" in html and "aria-modal" in html


def test_the_key_handler_reads_the_registry_instead_of_restating_keys() -> None:
    html = FRONTEND.read_text(encoding="utf-8")
    handler_start = html.index('document.addEventListener("keydown", e => {')
    handler = html[handler_start : html.index("let knownModels = [];", handler_start)]
    assert 'shortcutMatches(e, "dismiss-overlay")' in handler
    assert 'shortcutMatches(e, "command-palette")' in handler
    assert "overlayTrapTab(e)" in handler
    assert '"Alt+" + e.key' not in handler, "the handler must not rebuild a key string of its own"
    assert 'e.key === "Escape"' not in handler, "Escape is a registry entry, not a literal in the handler"


def test_every_rail_tab_announces_its_shortcut() -> None:
    payload = _payload()
    rail = payload["shortcuts"]["rail"]
    assert {entry["id"] for entry in rail} == {f"tab-{tab}" for tab in ("start", "harness", "hall", "council", "debate", "chat")}
    assert all(entry["keys"] for entry in rail)
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'tab.setAttribute("aria-keyshortcuts", tab.dataset.shortcut);' in html


def test_the_palette_advertises_the_key_it_answers_to() -> None:
    payload = _payload()
    assert payload["shortcuts"]["advertised_actions"] == ["command-palette"]
    assert set(payload["shortcuts"]["palette_shortcut_refs"]) <= {entry["id"] for entry in payload["shortcuts"]["rail"]}
    html = FRONTEND.read_text(encoding="utf-8")
    assert 'node.setAttribute("aria-keyshortcuts", keys.join(" "));' in html
    assert "shortcutLabel(item.shortcut)" in html


def test_the_doc_says_what_the_inventory_cannot_prove() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "never hand-edit" in text
    limits = _payload()["limits"]
    assert any("not that focus lands" in limit for limit in limits)
    assert any("reads the source" in limit for limit in limits)


def test_the_walk_fails_closed_without_a_browser() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    env = {"PATH": str(Path(node).parent), "HOME": "/tmp", "STUDIO_SCREENSHOT_CHROME": "/nonexistent/chrome"}
    result = subprocess.run(
        [node, str(WALK), "--base-url", "http://127.0.0.1:9", "--token", "x"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert any(phrase in combined for phrase in ("does not exist", "No Chrome", "ENOENT", "not importable")), combined


def test_the_walk_covers_every_dialog_and_reports_blocked_separately() -> None:
    source = WALK.read_text(encoding="utf-8")
    for probe in (
        "focus never leaves the dialog",
        "focus returns to where it came from",
        "the page behind is inert",
        "a refusal says why instead of opening empty",
        "Escape closes the drawer and returns focus to its toggle",
        "no page errors during the walk",
        'status: "blocked"',
    ):
        assert probe in source, probe
    assert "window.M5Overlays" in source, "the walk asserts the controller's stack, not a guess from classes"
