# Compare's results, verdict and evidence rail (UIV2-17)

Nothing here was drawn by hand, cropped or re-touched. Every file is a screenshot a script wrote.

## The after set

```bash
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js

for vp in 1640x900 1440x900 1280x800 390x844; do
  for theme in light dark; do
    node scripts/compare_evidence_walk.mjs --viewport "$vp" --theme "$theme" \
      --screenshots docs/media/compare-evidence
  done
done
```

The walk starts its own fixtures through `scripts/council_readiness_seed.py` and closes with
`25/25 checks passed` on each of the eight runs. Nine shots are taken per run, seventy-two in all,
which land as sixty-six files: the `390x844` narrow shot is re-taken on every run, so its one file
per theme is overwritten rather than multiplied. Each run starts up to four throwaway servers:
the default one (two loaded local models), one with `--slow-model`, one with `--long-model`, and
the catalogue's keyless provider row for the failure state — which fails the moment it is called,
so no state here needs a network.

| File | What it shows |
| :--- | :--- |
| `evidence-empty-<viewport>-<theme>.png` | Before a run: no results table, no provenance table, and a rail that says there is no comparison yet instead of counting zeroes |
| `evidence-populated-<viewport>-<theme>.png` | Two replies answered: one row each in both tables, at one type scale, with the criteria counts above them |
| `evidence-verdict-<viewport>-<theme>.png` | The verdict card: the model that judged, the catalogue row it came from, the replies named as its evidence, and its own figures |
| `evidence-error-<viewport>-<theme>.png` | One reply beside a provider that failed: a dash where the size would be, the failure attributed to its row, and the reason where a figure would be |
| `evidence-loading-<viewport>-<theme>.png` | A panel still streaming: its figures described as arriving with the reply, not as zeroes |
| `evidence-stopped-<viewport>-<theme>.png` | A run the reader stopped: attributed in the pane, explained in the rail, Start ready again |
| `evidence-long-content-<viewport>-<theme>.png` | A reply long enough to scroll inside its card, and a size the table reports in full |
| `evidence-narrow-390x844-<theme>.png` | The same tables after the viewport drops to 390x844: every row stacked, every cell labelled, nothing clipping |
| `evidence-inspector-<viewport>-<theme>.png` | The rail framed on its own: the evidence lines it now carries |

## The before set

`before-uiv2-17/<viewport>-<theme>/` holds the same states served from `origin/main` at `faff1e9`,
before this story, captured with this walk in `--capture-only --states populated,error` against a
detached worktree of that commit. The app in those files is `main`; only the fixture is the story's
own, because `--long-model` is a fixture capability this story added. The unmodified pane passes
11 of the 18 checks that subset runs: the `0 ch` for a reply that does not exist, the verdict with
no provenance, the rail that points at the pane, and the 65px overflow at 390x844 are all visible
there.
