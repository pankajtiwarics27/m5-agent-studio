# M5 2.0 UI/UX baseline evidence

This page records the baseline `UIUX-P0-05` exists to establish: what the test
suite and the repository checks actually produce on this checkout, and what the
screenshot pipeline would capture if a browser were available. The test half is
measured. The visual half is **blocked in this environment** and is recorded as
blocked rather than implied.

It is evidence, not a plan: the phases, gates and per-story change shape live in
[`uiux-2-0-program.md`](uiux-2-0-program.md) and
[`uiux-2-0-transformation-plan.md`](uiux-2-0-transformation-plan.md).

## Environment

| Fact | Value |
|---|---|
| Source | `main` at `0b4b1a4` (merge of #115), pinned in the UI/UX ledger |
| Python | 3.11.2, virtual environment at `.venv`, installed with `pip install -e '.[dev]'` |
| Node | 22.22.3 |
| Platform | Linux sandbox, no display |
| Browser | **none**: no Chrome/Chromium binary on `PATH`, `puppeteer-core` not installed |
| Git history | full clone required the baseline test; a shallow checkout fails `tests/test_baseline.py` until `git fetch --unshallow` |

## Baseline test results

Every count below is from this checkout on 2026-10-31, not from a previous
session. A later story compares against this table; a story that changes the
suite states the new numbers and why.

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -m pytest -q` | 0 | 1,598 passed, 1 skipped (opt-in live container test), 88 s |
| `.venv/bin/python -m ruff check .` | 0 | all checks passed |
| `.venv/bin/python -m mypy` | 0 | no issues in 58 source files |
| `.venv/bin/python scripts/validate_docs.py` | 0 | 5 required pages, 124 files scanned |
| `.venv/bin/python scripts/check_frontend_syntax.py` | 0 | one inline script block parses |
| `.venv/bin/python scripts/demo_tour.py --check` | 0 | committed transcript is current |
| `.venv/bin/python scripts/uiux_source_manifest.py --check` | 0 | 14 files, 6 tabs, 115 routes |
| `.venv/bin/python scripts/uiux_control_inventory.py --check` | 0 | 170 markup controls, 101 of them chrome; 30 script-rendered |
| `.venv/bin/python scripts/uiux_route_map.py --check` | 0 | 53 call sites, 60 exact and 1 dynamic match, 0 findings, 0 unresolved, 58 routes with no caller |
| `.venv/bin/python scripts/uiux_authorization_trace.py --check` | 0 | 37 state-changing routes traced |
| `.venv/bin/python scripts/uiux_preservation_signoff.py --check` | 0 | unsigned package, 21 capabilities, 6 panes |
| `.venv/bin/python scripts/validate_screenshots.py` | 0 | 0 of 6 captured |

The suite counts include the four UI/UX inventory stories' own tests
(`tests/test_uiux_roadmap.py`, `tests/test_uiux_source_manifest.py`,
`tests/test_uiux_control_inventory.py`, `tests/test_uiux_route_map.py`,
`tests/test_uiux_authorization_trace.py`).

## The capture plan (measured without a browser)

`node scripts/capture_screenshots.mjs --dry-run` resolves the manifest, the six
tab ids and the output paths and prints the plan without launching anything:

* **6 shots**, one per pane, in document order: `start.png`, `harness.png`,
  `hall.png`, `pipeline.png`, `compare.png`, `chat.png`;
* viewport **1440 × 900** at device scale factor 2, from
  `docs/media/screenshots/manifest.json`;
* the demo instance is expected at `python3 scripts/demo_tour.py --serve`;
* the capture writes `docs/media/screenshots/index.json` with the digest, size,
  viewport and capture time of each PNG, and refuses to write a partial set.

That is evidence that the manifest, the tab ids and the output paths agree. It is
**not** evidence that any pane renders: nothing was drawn.

## What a machine with a browser has to run

The visual half of this story closes only with a real capture and a validation
run. On a machine with Chrome/Chromium:

```bash
# 1. a demo instance, with its token
python3 scripts/demo_tour.py --serve

# 2. capture all six panes
STUDIO_SCREENSHOT_CHROME=/path/to/chrome \
  node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN

# 3. prove the capture matches the manifest
python3 scripts/validate_screenshots.py

# 4. the repository checks that must stay green with the new images
python3 scripts/build_studio.py && git diff --exit-code
python3 scripts/uiux_source_manifest.py
```

If any pane fails to capture, the script writes nothing and exits non-zero; a
partial set is worse than no set, because the manifest would then describe
screenshots that do not exist.

## What this page does not claim

* No pane was rendered, so nothing here describes how the interface looks.
* A green test suite is not a green interface: the suite covers contracts, not
  appearance, keyboard order or contrast.
* The 0-of-6 screenshot state is the honest current state, not a pass.
