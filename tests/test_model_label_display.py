"""UIV2-P7-03: the model pickers show a bounded alias and keep the raw id.

A local model id is often 60+ characters of slug. It was rendered verbatim in every
picker, so it set the width of each select and read as the loudest element on the
pane. The picker now shows a bounded alias that keeps the family at the front and the
quantisation at the back.

Two things must stay true, and this file is about the second one as much as the first:

1. The alias is bounded and keeps both ends, so a reader can still tell two models
   apart at a glance.
2. The `<option value>` is still the raw id, and the untruncated label is still
   reachable on the option's and the select's title. A display nickname must never be
   the only copy of the name that a payload is keyed on.

The behaviour is executed rather than string-matched where possible: the helper is
lifted out of `static/index.html` and run under node, because a test that only greps
for a function name would pass on a function that returns `""`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"

NODE = shutil.which("node")


def _html() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _helper_source() -> str:
    """The two helpers, lifted verbatim out of the frontend."""
    html = _html()
    start = html.index("const MODEL_LABEL_MAX = 44;")
    end = html.index("function selectedOptionFullLabel", start)
    end = html.index("\n  }\n", end) + len("\n  }\n")
    return html[start:end]


def _run_js(expression: str) -> object:
    """Evaluate an expression with the real helpers in scope, under node."""
    if NODE is None:  # pragma: no cover - environment without node
        pytest.skip("node is not available to execute the helper")
    source = _helper_source() + "\nprocess.stdout.write(JSON.stringify(" + expression + "));\n"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ── the behaviour ────────────────────────────────────────────────────────────────

def test_a_short_label_is_returned_unchanged():
    assert _run_js('modelDisplayLabel("Qwen3.8-27B", "x")') == "Qwen3.8-27B"


def test_a_long_slug_is_bounded_and_keeps_both_ends():
    slug = "Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-MTP-Q4_K_S"
    out = _run_js(f"modelDisplayLabel({json.dumps(slug)}, 'x')")
    assert len(out) <= 44, out
    assert out.startswith("Qwen3.8-27B-TurboFCFusion"), out
    assert out.endswith("Q4_K_S"), out
    assert "…" in out


def test_whitespace_is_collapsed_so_a_padded_label_cannot_widen_the_select():
    padded = "  Qwen3.8-27B   TurboFCFusion  "
    assert _run_js(f"modelDisplayLabel({json.dumps(padded)}, 'x')") == "Qwen3.8-27B TurboFCFusion"


def test_a_missing_label_falls_back_to_the_id_rather_than_rendering_empty():
    assert _run_js("modelDisplayLabel(null, 'fallback-id')") == "fallback-id"
    assert _run_js("modelDisplayLabel(undefined, 'fallback-id')") == "fallback-id"
    assert _run_js("modelDisplayLabel('', 'fallback-id')") == "fallback-id"


def test_two_models_sharing_a_prefix_stay_distinguishable():
    a = "Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-MTP-Q4_K_S"
    b = "Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-MTP-Q8_0"
    shown_a = _run_js(f"modelDisplayLabel({json.dumps(a)}, 'x')")
    shown_b = _run_js(f"modelDisplayLabel({json.dumps(b)}, 'x')")
    assert shown_a != shown_b, (shown_a, shown_b)


# ── the contract the payload depends on ─────────────────────────────────────────

def test_the_option_value_is_still_the_raw_id():
    html = _html()
    line = next(line for line in html.splitlines() if "modelDisplayLabel(it.label, it.id)" in line)
    assert 'value="${escapeHtml(it.id)}"' in line, line
    assert 'title="${escapeHtml(it.label || it.id)}"' in line, line


def test_the_unavailable_option_is_aliased_but_keeps_its_raw_value():
    html = _html()
    line = next(line for line in html.splitlines() if "[Unavailable]" in line and "optgroup" in line)
    assert 'value="${escapeHtml(prevVal)}"' in line, line
    assert 'title="${escapeHtml(prevVal)}"' in line, line
    assert "modelDisplayLabel(prevVal, prevVal)" in line, line


def test_the_picker_tooltip_keeps_the_untruncated_name():
    html = _html()
    assert 'model.title = selectedOptionFullLabel("harnessLeadModel")' in html


def test_the_helper_is_not_duplicated():
    assert _html().count("function modelDisplayLabel") == 1
    assert _html().count("function selectedOptionFullLabel") == 1


# ── every render site, not just the pickers ─────────────────────────────────────

def test_the_compare_rows_use_the_same_alias_and_keep_the_raw_value():
    """Compare renders its own list; one model must not read two widths in two panes."""
    html = _html()
    line = next(line for line in html.splitlines() if 'class="debate-m-chk"' in line)
    assert 'value="${escapeHtml(it.id)}"' in line, line
    assert "modelDisplayLabel(it.label, it.id)" in line, line
    assert 'title="${escapeHtml(it.label || it.id)}"' in line, line


def test_no_model_render_site_still_prints_the_raw_label_unbounded():
    """A guard against adding a third model list later that forgets the alias.

    The command palette is deliberately excluded: `renderCommandPaletteList()` draws
    `CMD_PALETTE_ITEMS`, which are mostly not models ("Open Harness", "Open Chat"),
    and each row already carries a shortcut hint. Truncating a command name there
    would cost discoverability to save width in an overlay that has the whole
    viewport. The exclusion is scoped to that one function and written down here so a
    future reader sees a decision rather than an oversight.
    """
    html = _html()
    start = html.index("function renderCommandPaletteList(")
    end = html.index("\n  function ", start + 10)
    palette = html[start:end]
    assert "${escapeHtml(it.label)}" in palette  # the exclusion is real, not stale

    remaining = html.replace(palette, "")
    offenders = [
        (n, line.strip())
        for n, line in enumerate(remaining.splitlines(), 1)
        if "${escapeHtml(it.label)}" in line
    ]
    assert offenders == [], offenders
