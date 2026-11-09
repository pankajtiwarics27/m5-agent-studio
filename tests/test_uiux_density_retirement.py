"""UIV2-P7-05: Density is retired, and no stored preference may strand a reader.

The owner removed the Density control (2026-11-09) because nothing used it. Removing a
control is not the same as removing the state it wrote, and the difference matters:

* `applyDensity()` used to read `localStorage["m5_ui_density"]` at boot. Without the
  button, anyone who had ever toggled compact would come back to compact spacing with
  **no control to undo it** - a persisted state with no reachable inverse. So the boot
  path no longer consults the store at all; the pane is always comfortable.
* The `body[data-density="compact"]` token block **stays**. `scripts/uiux_tokens.py`
  requires the density block to re-point exactly its five tokens and nothing else, so
  deleting the block would break the token contract over a view nothing reaches. The
  block is unreachable, not absent - and this test pins that it is still there, so a
  later cleanup cannot delete it and take the token contract with it.

The behaviour is executed under node with a stubbed body, so a `applyDensity` that quietly
honoured its old argument again would fail rather than merely differ from a string.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
TOKENS = PROJECT_ROOT / "scripts" / "uiux_tokens.py"
NODE = shutil.which("node")


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _apply_density_source() -> str:
    """The shipped `applyDensity` function, lifted verbatim."""
    html = _html()
    start = html.index("function applyDensity(")
    end = html.index("\n  }\n", start) + len("\n  }\n")
    return html[start:end]


# ── the control is gone ─────────────────────────────────────────────────────────

def test_the_density_control_is_not_in_the_markup() -> None:
    assert "densityToggleBtn" not in _html()


def test_the_toggle_function_is_gone_with_it() -> None:
    html = _html()
    assert "function toggleDensity(" not in html
    assert "toggleDensity()" not in html


def test_the_dead_narrow_width_hide_rule_is_gone() -> None:
    """A rule hiding a control that no longer exists is dead weight in a media query."""
    assert "#densityToggleBtn { display: none; }" not in _html()


# ── no stored preference can strand a reader ────────────────────────────────────

def test_the_boot_path_never_consults_the_stored_density() -> None:
    html = _html()
    assert 'localStorage.getItem("m5_ui_density")' not in html
    assert "m5_ui_density" not in html


def test_applying_density_always_yields_comfortable() -> None:
    """Called with `true` (what a stored compact used to pass in), it must still be comfortable."""
    if NODE is None:  # pragma: no cover - environment without node
        pytest.skip("node is not available to execute the helper")
    script = (
        "const document = { body: { dataset: {} } };\n"
        + _apply_density_source()
        + "\napplyDensity(true);\n"
        + "const a = document.body.dataset.density;\n"
        + "applyDensity(false);\n"
        + "process.stdout.write(JSON.stringify([a, document.body.dataset.density]));\n"
    )
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["comfortable", "comfortable"]


# ── the token contract must survive the retirement ──────────────────────────────

def test_the_compact_token_block_is_still_present_and_still_five_tokens() -> None:
    html = _html()
    assert 'body[data-density="compact"]' in html
    # The five tokens scripts/uiux_tokens.py requires, in the block itself.
    start = html.index('body[data-density="compact"] {')
    block = html[start : html.index("}", start)]
    for token in ("control-min-h", "control-min-h-sm", "strip-pad-y", "card-pad", "row-gap"):
        assert f"--{token}:" in block, token


def test_the_token_contract_still_names_density_as_five_tokens() -> None:
    source = TOKENS.read_text(encoding="utf-8")
    assert '"density": ("control-min-h", "control-min-h-sm", "strip-pad-y", "card-pad", "row-gap")' in source


def test_the_retirement_is_recorded_where_the_decision_lives() -> None:
    decisions = (PROJECT_ROOT / "docs" / "ui-decisions.md").read_text(encoding="utf-8")
    assert "UIV2-P7-05" in decisions
    assert "retired" in decisions.lower()
