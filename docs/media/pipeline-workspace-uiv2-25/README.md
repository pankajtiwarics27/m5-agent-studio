# UIV2-25 — Pipeline workspace visual evidence

This directory is a **local evidence policy and reproduction recipe**, not a request to commit
screenshots. `.gitignore` excludes every `*.png`, `capture-manifest.json` and generic capture
`index.json` below it. The PNGs stay available for review in the workspace without adding binary
or generated-evidence inputs to Git.

## Producers

- `scripts/pipeline_workspace_walk.mjs` starts `scripts/pipeline_recovery_seed.py` and Chromium,
  drives the existing controls and `/api/council/stream`, and takes every capture. It never paints
  `stageBuffers` or substitutes markup for a running state.
- `after/` is the strict verification run: six states × four viewports × light/dark, **50/50 checks
  per run, 400/400 overall, 48/48 captures**. The final manifest was captured at
  `2026-11-07T06:17:53.824Z`; its SHA-256 is
  `3314183788000adf881d46f98b74c4ed87103b41ca2a46df84f28f0020bea5ff`.
- `before-uiv2-25/` uses the same final driver in `--capture-only` against a detached worktree at
  pre-change commit `605b977a25e430d6b2cbe9608a9c3df578a66c8a`. Only the new walk and additive
  provider fixture are overlaid there; `app.py` and `static/index.html` are the pre-change files.
  Capture-only writes **0 checks**, is labelled `capture-only-pre-change`, and is not a pass claim.
  Its 48-capture manifest was written at `2026-11-07T03:40:41.709Z` with SHA-256
  `3fa10a2c638df7537dc146cfbc3c9a3ab484bb4115e8b90a2a18686f238dbb3d`.

Each theme directory holds `empty`, `loading`, `populated`, `long-content`, `error`, and `recovery`
at `1640x900`, `1440x900`, `1280x800`, and `390x844`. Desktop frames retain the task-to-output
hierarchy. Narrow populated/long frames scroll to the selected workspace, and narrow error/recovery
frames scroll to the recovery decision, so the image shows the element that proves the state.

## Reproduce

```bash
node scripts/pipeline_workspace_walk.mjs \
  --out docs/media/pipeline-workspace-uiv2-25/after

# In a detached 605b977 worktree, with the final walk and recovery fixture overlaid:
node scripts/pipeline_workspace_walk.mjs --capture-only \
  --out /home/user/m5-agent-studio/docs/media/pipeline-workspace-uiv2-25/before-uiv2-25

python3 scripts/uiux_screenshot_review.py --review-local
python3 scripts/uiux_screenshot_review.py --check
```

Set `STUDIO_SCREENSHOT_CHROME`, `STUDIO_PUPPETEER_CORE`, and `STUDIO_PYTHON` when the browser,
Puppeteer, or Python environment is not on the default path. The normal walk requires every check
and every expected capture; capture-only is solely for the historical comparison.

`--review-local` explicitly includes Git-ignored PNGs, applies the authored review in
`scripts/uiux_screenshot_review.py`, and does not read or write committed review artifacts.
Normal generation and `--check` cover committed captures only. Consequently these local images can
be reviewed without making a fresh clone drift from `product/M5_2_0_UIUX_Screenshot_Review.json`.

## What was opened and read

- `after/light/empty-1440x900.png`: task first, setup closed with its actual configuration summary,
  All Stages plus exactly four stages, and a meaningful Architect empty state with no invented log.
- `after/dark/populated-1440x900.png`: Builder selected, one output card beside model/input/artifact
  facts and observed activity; the run summary and Inspector both say completed/done, with no stale
  Running, Streaming, Queued, or pending overview.
- `after/dark/long-content-390x844.png`: the real 3,312-character Judge stream is visible in the
  selected internal reader, including the durable-queue answer and numbered checks, with no page
  horizontal overflow; the walk separately verifies the untruncated Check 12 tail.
- `after/light/error-1280x800.png`: failed Critic selected, terminal Failed summary, persistent
  Retry/Forget actions after the toast has gone, and the browser-checkpoint/server-record distinction.
- `after/dark/recovery-390x844.png`: checkpoint provenance and bounded retained characters, disabled
  server reload with its factual reason, and restored Critic output marked recovered/partial.
- `before-uiv2-25/light/populated-1440x900.png`: the pre-change stage view used for comparison.

The full per-capture bytes and SHA-256 values, every assertion, terminal diagnostics, and exact run
metadata are in the two ignored `capture-manifest.json` files. The story's source/state inventory is
`product/M5_2_0_UIUX_Pipeline_Workspace.json`; its readable review is
`docs/uiux-2-5-pipeline-workspace.md`.
