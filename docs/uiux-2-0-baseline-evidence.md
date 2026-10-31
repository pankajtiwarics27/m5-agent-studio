# M5 2.0 UI/UX baseline evidence

> **Review hold (2026-10-31, answered point by point below):** Do not run the
> capture recipe below from a real checkout while
> [#108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108) remains
> open: the demo can read that checkout's harness data. Use a disposable source
> copy with an empty `.harnesses` directory and resolve the capture
> authentication-prompt timeout before accepting screenshots. A passing test can
> also modify checkout harness data despite isolated HOME and database settings;
> until [#117](https://github.com/pankajtiwarics27/m5-agent-studio/issues/117) is
> fixed, run the full suite in a disposable clone or detached worktree with an
> empty store. The dry run only validates manifest shape and prints a plan; it
> does **not** check tab IDs against the UI (`scripts/validate_screenshots.py`
> checks those separately). Earlier route-map claims also have a
> [review hold](https://github.com/pankajtiwarics27/m5-agent-studio/pull/115#issuecomment-6543099994).
>
> | Ask in the hold | Where it stands |
> |---|---|
> | Capture from a disposable copy, not a real checkout (#108) | **done** — the published set comes from a `git archive` copy of `fa2a31c` at `/tmp/iso` with no `.harnesses` store; the first attempt from the working checkout was discarded. #108 itself stays open |
> | Resolve the capture authentication-prompt timeout | **done** — root-caused to an unauthenticated retry loop, fixed in `scripts/capture_screenshots.mjs` (below) |
> | Clean-copy test results until #117 is fixed | **done** — the 1,640-test run below was made in a `git clone` of this branch with no `.harnesses` store. #117 stays open |
> | Reconcile the test table's provenance | **done** — every command in the table is re-measured at `fa2a31c` with its own pin, and the story revision each set belongs to is named |
> | The dry run is not a tab-ID check | **kept** — the dry run proves manifest shape; tab IDs are a separate check in `validate_screenshots.py` |
>
> Acceptance of the six images is the reviewer's and the owner's call, not the
> capturing session's. The story records that explicitly instead of closing itself.

This page records the baseline `UIUX-P0-05` exists to establish: what the test
suite and the repository checks actually produce, and the six screenshots of the
six panes. Both halves are measured — the suite by running it, the visual half by
rendering it through the repository's own capture script.

It is evidence, not a plan: the phases, gates and per-story change shape live in
[`uiux-2-0-program.md`](uiux-2-0-program.md) and
[`uiux-2-0-transformation-plan.md`](uiux-2-0-transformation-plan.md).

## Environment

| Fact | Value |
|---|---|
| Source | `main` at `fa2a31c` (merge of #114), pinned in the UI/UX ledger |
| Python | 3.11.2, virtual environment built with `pip install -e '.[dev]'` |
| Node | 22.22.3 |
| Platform | Linux sandbox, no display, no root |
| Browser | none on `PATH`. Captured with Chromium 131 from the npm package `@sparticuz/chromium` plus its `al2023` NSS libraries, driven by `puppeteer-core` 23 — see [Capturing without a system browser](#capturing-without-a-system-browser) |
| Test-tree provenance | the suite below ran in a fresh `git clone` (`/tmp/clean`) of the branch at `ce86334`, which had **no** `.harnesses` store when cloned and **had one** after the run — the reproduction of #117, recorded as a fact about the repository rather than hidden |
| Git history | a shallow checkout fails `tests/test_baseline.py` until `git fetch --unshallow`; the clone used for this run is full |

## Baseline test results

Every count below was produced on 2026-10-31 at `fa2a31c`, in a clean clone. A
later story compares against this table; a story that changes the suite states the
new numbers and why. Two earlier revisions of this table (1,541 and 1,598 tests)
were measured at `0b4b1a4` and `95cc01d` respectively — the numbers moved with the
sign-off package (#116) and the route-map rewrite (#114), and those revisions are
not interchangeable with this one.

| Command | Exit | Result |
|---|---|---|
| `.venv/bin/python -m pytest -q` | 0 | 1,640 passed, 1 skipped (opt-in live container test), 131 s |
| `.venv/bin/python -m ruff check .` | 0 | all checks passed |
| `.venv/bin/python -m mypy` | 0 | no issues in 58 source files |
| `.venv/bin/python scripts/validate_docs.py` | 0 | 5 required pages, 124 files scanned |
| `.venv/bin/python scripts/check_frontend_syntax.py` | 0 | one inline script block parses |
| `.venv/bin/python scripts/demo_tour.py --check` | 0 | committed transcript is current |
| `.venv/bin/python scripts/uiux_source_manifest.py --check` | 0 | 14 files, 6 tabs, 115 routes, pinned to `fa2a31cec070` |
| `.venv/bin/python scripts/uiux_control_inventory.py --check` | 0 | 170 markup controls (101 chrome, 69 panes), 30 script-rendered |
| `.venv/bin/python scripts/uiux_route_map.py --check` | 0 | 53 call sites, 60 exact / 1 dynamic / 0 unknown-method, 0 findings, 58 routes without an observed caller, 0 unknown |
| `.venv/bin/python scripts/uiux_authorization_trace.py --check` | 0 | 37 state-changing routes, 12 with an authority subsystem, 30 with durable state, 22 that can leave the machine |
| `.venv/bin/python scripts/uiux_preservation_signoff.py --check` | 0 | unsigned package, 21 capabilities, 6 panes |
| `.venv/bin/python scripts/validate_screenshots.py` | 0 | **6 of 6 captured and consistent with the manifest** |

Every command in that table was run in that clone, not in the author's checkout.

The `.harnesses` detail is worth stating plainly, because it is the reason the
hold exists: `ls .harnesses` before the run failed with "No such file or
directory", and after the run the directory existed. `tests/test_audit_log.py`
monkeypatches the database path but not `HARNESS_STORE_DIR`, so a passing test
writes into the source tree. That does not make the counts above wrong — file
counts do not depend on the store — but it is why the table was measured in a
throwaway clone and why the six images were captured from a throwaway copy, and it
is filed as [#117](https://github.com/pankajtiwarics27/m5-agent-studio/issues/117).

The "58 routes without an observed caller" figure is the schema-2 map's wording
and means exactly that: no caller was observed by the lexical scan, not that none
exists. The map publishes unresolved edges as `unknown` rather than absent, and
the trace inherits that limit.

## The six screenshots

Captured on 2026-10-31 at `17:54:48Z`, one image per pane, from a disposable copy
of `fa2a31c`, through `scripts/capture_screenshots.mjs`, validated by
`scripts/validate_screenshots.py`:

| Shot | Pane (`data-tab`) | Viewport | Bytes | sha256 |
|---|---|---|---|---|
| `start.png` | Start (`start`) | 1440×900 @2 | 348,544 | `70ab13111454e1aa…` |
| `harness.png` | Harness (`harness`) | 1440×900 @2 | 359,794 | `6ee91662ce7b217c…` |
| `hall.png` | Council (`hall`) | 1440×900 @2 | 285,246 | `489664c019af2893…` |
| `pipeline.png` | Pipeline (`council`) | 1440×900 @2 | 263,852 | `e807e09e0bf17c61…` |
| `compare.png` | Compare (`debate`) | 1440×900 @2 | 251,863 | `23865a9969dd1fe2…` |
| `chat.png` | Chat (`chat`) | 1440×900 @2 | 248,564 | `66488f4e34f678f1…` |

`docs/media/screenshots/index.json` carries the full digests, sizes and viewport;
`validate_screenshots.py` re-checks every one of them, so a swapped or resized
image fails CI rather than passing unnoticed.

What the images show: the demo's deliberate first-run state — the Start pane's
preflight at "3 of 6 steps ready" with the workspace path of the disposable copy,
the Harness pane with no session yet, and the memory and container readouts in the
inspector. That is the demo's design (no provider, no key, no model call), not a
broken render.

## Capturing without a system browser

Chromium is not on `PATH` and there is no root to install one, but npm can supply
a working browser. `@sparticuz/chromium` ships a Chromium build whose `al2023`
layer carries the NSS libraries it links against; `puppeteer-core` drives it.

```bash
# once, outside the repository
npm i puppeteer-core @sparticuz/chromium
node -e 'require("@sparticuz/chromium").executablePath().then(console.log)'
tar xf /tmp/al2023.tar -C /tmp/chromium-libs          # libnss3, libnspr4, libnssutil3, ...

# a disposable source copy — never a working checkout (#108, #117)
git archive fa2a31c | tar -x -C /tmp/iso
cd /tmp/iso && /path/to/venv/bin/python scripts/demo_tour.py --serve     # prints URL + throwaway token

# the capture itself, into the repository's media directory
LD_LIBRARY_PATH=/tmp/chromium-libs/lib \
STUDIO_SCREENSHOT_CHROME=/tmp/chromium \
STUDIO_PUPPETEER_CORE=/scratch/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
STUDIO_SCREENSHOT_CHROME_ARGS="--no-zygote --single-process --disable-gpu" \
  node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN

python3 scripts/validate_screenshots.py
```

Two defects in the pipeline had to be fixed to get there, and both are in the
script rather than worked around in a local harness:

* **The token is seeded before the first document exists.** The shell fetches as it
  loads; unauthenticated it retries without backoff, which starved the renderer so
  the old "load, store the token, reload" sequence hung. `evaluateOnNewDocument`
  removes the unauthenticated state entirely.
* **`networkidle2` was replaced by a settle window.** The shell holds streaming
  connections open for its lifetime, so the network never goes idle and navigation
  could never complete. The script waits `--settle-ms` after `domcontentloaded`,
  the same window it already used between panes.
* **Extra Chrome flags come from `STUDIO_SCREENSHOT_CHROME_ARGS`**, so a
  constrained container supplies its launch flags without a per-machine recipe
  being hard-coded in the repository.

The pipeline still refuses to write a partial set, invent an image, or accept a
capture whose digest, size or dimensions disagree with the manifest.

## What this page does not claim

* The six images are of the *demo* state, not of a working session with a model:
  streamed output, approvals, diffs and recovery are not pictured because they need
  a provider, and the tour deliberately runs without one.
* A green suite is still not a green interface: it covers contracts, not contrast,
  keyboard order or screen-reader semantics. The images show what renders; they do
  not prove it is usable.
* The captures came from one viewport (1440×900 @2). Narrow viewports and the
  density and theme variants are later stories.
* The demo's source-relative `.harnesses` store is still not isolated (#108), and
  a test can still write one (#117). This page's advice — disposable copy for both
  — mitigates the baseline work; it does not fix the product.
