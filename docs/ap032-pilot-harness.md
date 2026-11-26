# AP-032 pilot harness: what it measures, and what it refuses to

`pilot_harness.py` prepares the website pilot so that a head *you* control can be qualified quickly.
It exists because AP-032's remaining release blocker is "run the pilot at a real head and review the
artifact", and the honest part of that work can be done before you have a head.

The constraint it is built around: `website_quality.PilotReport` refuses to invent a result. Every one
of its 11 criteria needs an outcome, every layout cell needs a measurement, and `deliverable` stays
false without an `OwnerReview` from a human. A harness that filled those in would produce the exact
artifact AP-022 forbids. So this one does the decidable half and names the rest.

## What it measures (5 of 11)

These are answerable from the artifact's own bytes and structure.

| Criterion | What "passed" means here |
|---|---|
| `no_placeholder_content` | No lorem ipsum, no leftover in-code task markers, no "Coming soon", no unfilled placeholder copy and no bracket-stub insertions in any text file |
| `secrets_absent` | No provider-shaped key (`sk-`, `ghp_`, `AKIA`, etc.) in any file; a hit is reported masked, never echoed |
| `dependencies_reviewed` | Every external reference is enumerated and judged: `https://` and protocol-relative pass, anything else is listed for review |
| `forms_work` | Every `<form>` has an `action`; every `<input>` has a `name` or an associated `<label>` |
| `links_resolve` | Every internal `href`/`src` resolves to a file inside the artifact |

`dependencies_reviewed` deliberately does not mean "the CDN is trustworthy". It means nothing external
went unreviewed — reviewable scheme or it is listed.

## What it refuses — and what measures it instead (6 of 11)

`keyboard_reachable`, `focus_visible`, `contrast_ok`, `reduced_motion_respected`, `console_clean` and
`navigation_works` are emitted as `passed=False, basis="cannot-measure-here"` with the reason. A focus
ring is a computed style; contrast is a computed colour pair; a console error only exists once scripts
run. A regex standing in for any of those would be a green row that means nothing.

They are never silently dropped, either. `PilotPartial` refuses to be constructed unless it carries an
outcome for all 11, so "unmeasured" can never render as "absent".

**These six are measurable, just not here.** `scripts/pilot_browser_walk.mjs` drives headless Chromium
at the artifact and answers them:

| Criterion | How the walk decides it |
|---|---|
| `keyboard_reachable` | presses Tab and records every stop; a click handler on a non-focusable element counts as mouse-only |
| `focus_visible` | each stop's computed `outline`/`box-shadow` must differ from its unfocused rendering |
| `contrast_ok` | every text node against its effective background, WCAG AA (4.5:1, or 3:1 for large text) |
| `reduced_motion_respected` | emulates `prefers-reduced-motion: reduce`; no transition/animation over 0.01s, none running |
| `console_clean` | loads and clicks every control; any `console.error` or uncaught error fails it |
| `navigation_works` | follows every same-origin link; `ERR_FILE_NOT_FOUND` or a missing title/landmark fails it |

```bash
STUDIO_SCREENSHOT_CHROME=/path/to/chromium \
STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core \
node scripts/pilot_browser_walk.mjs path/to/built/site --out browser-partial.json
```

Exit `0` only when all six cleared; `1` when a criterion failed; `2` when it refused (no browser, no
puppeteer, no HTML, not a directory). It carries the same refusal as the harness: a criterion it did
not measure is `basis: "not-measured"`, `passed: false`, and the JSON states
`is_a_qualification_report: false`. A pass counts its documents and a failure names the page, so one
green page cannot read as "the artifact passed".

The walk measures one viewport, 1440x900, in headless Chromium. It does not judge design, copy, or
whether the site is worth shipping.

## What it emits

A `PilotPartial`, whose mapping leads with `is_a_qualification_report: false` and lists
`what_a_pilot_report_still_needs`: owner review, the layout/interaction measurements, the six browser
criteria, and the artifact head the run applied to. The mapping is JSON, so it can be attached to the
release evidence as-is without being mistaken for a qualification report.

```bash
python pilot_harness.py path/to/built/site            # JSON on stdout, exit 1 if any offline criterion fails
python pilot_harness.py path/to/site
```

(`pilot_harness.py` prints to stdout; redirect it if you want a file.)

Exit codes: `0` offline axes clear, `1` an offline criterion failed or the artifact was refused (empty,
binary, or containing nothing to audit), `2` the path is not a directory. A refusal is never `0`.

## Joining the two partials: `pilot_preflight.py`

Two partials are only evidence if they describe the same bytes. `pilot_preflight.py` joins them and
refuses the join when it would invent a result:

```bash
python pilot_harness.py build/site > offline.json
node scripts/pilot_browser_walk.mjs build/site --out browser.json
python pilot_preflight.py offline.json browser.json --out preflight.json
```

- **Both partials carry `artifact_sha256`**, computed by the same scheme in both tools: sorted relative
  path, NUL, file content, NUL, over `.html/.htm/.css/.js/.txt`. If those digests differ the pair is
  refused — combining them would report 11/11 for a build nobody measured.
- **A criterion that was never measured is a refusal**, not an empty section. The preflight will not
  print a hole as an absence.
- **A partial that claims to be a qualification report is refused**, so the preflight cannot inherit
  one.
- Exit `0` only when every criterion was measured and none failed; `1` on a refusal or a red criterion;
  `2` on bad usage. It prints nothing on stdout when it refuses.

What it produces is `ready_for_owner_review: true` when the *machine* work is finished — and it says in
its own note that this is a statement about the evidence, not about the artifact. It names what a human
still owes: the review, the `PilotReport` layout/interaction measurements, and both independent
approvals.

## How to use it at a real head

1. Build the site at the head you want to qualify.
2. Run the harness against the built directory. It audits files on disk and never fetches, renders or
   executes them.
3. Fix anything red on the offline axes and re-run. This is the cheap loop.
4. Run `pilot_browser_walk.mjs` against the same directory for the six browser criteria. Fix and
   re-run — this is still a machine loop.
5. Open the site yourself and review what a walk cannot: design, copy, whether the thing is worth
   shipping. The walk does not do this and does not claim to.
6. Assemble the `PilotReport` **in Python** — this step is not a command, and there is no CLI for it.
   Attach both partials beside the report: they do not replace it, they are the machine evidence behind
   its eleven criteria rows.

Step 6 is written as a step because the report is required, not because it is runnable. What is true
today, so that nobody goes looking for a command that does not exist:

- `website_quality` is a library. It has no `main`, no `__main__` and no argument parser, so
  "run the pilot through `website_quality`" cannot be done by any invocation. The report is
  constructed in Python, and step 6 is done by whoever writes that code.
- `PilotReport` will not accept a partial evidence set: it needs all eleven outcomes **and** all six
  layout cells (three viewports from `VIEWPORT_MATRIX` × zoom 1 and 2). An incomplete matrix raises
  `LayoutRefused`, and an incomplete outcome set raises rather than defaulting to a pass.
- **Nothing in this repository measures the layout matrix.** The browser walk measures the six
  browser criteria; it does not walk viewports or page zoom, and no script here emulates 200% zoom.
  So the six layout cells are measurements a human has to take — they are not a summary of the two
  partials, and a report that filled them in from the walk would be inventing them.
- `deliverable()` stays false until an `OwnerReview` exists, and `OwnerReview` refuses agent-shaped
  actors. A report built without one is correct and expected: construction succeeding does not mean
  the pilot is qualified.

`pilot_preflight.py` states the same thing in its `what_a_human_still_owes` list — "supply the
`PilotReport` layout and interaction measurements" is a human item, not a tool's.

## What this does not do

- It does not produce the `screenshot` approval. Neither does the walk: a machine can measure a focus
  ring, but "this looks right" is a reviewer's judgement.
- It does not review the artifact. `OwnerReview` refuses agent-shaped actors, and this harness does not
  attempt one.
- It does not make the pilot blocker disappear. With both partials in hand the machine-checkable
  criteria are covered, and the owner review and the two independent approvals remain — a human still
  has to decide the artifact is good, and no tool here is allowed to do that for them.

## Keeping the three criterion lists one list

The eleven criteria are named in three places: `website_quality.CRITERIA` (what the report demands),
`pilot_preflight.EXPECTED_CRITERIA` (what the join expects) and the harness/walk split (who measures
what). They agree, and nothing forced them to until `tests/test_pilot_criteria_alignment.py` (10 cases)
pinned it. A criterion renamed in one place would not have failed anything — the join only reads what
the tools emit and the report only checks what it is given, so a renamed criterion would retire a check
while every tool stayed green.

That module also pins the negative claims this page makes, so the page cannot drift from the code:
there is no script that measures page zoom, `PilotReport` refuses a partial layout matrix, a complete
matrix is still not deliverable without an owner review, and step 6 does not read like a command. Each
claim is a test that fails if the claim stops being true — including `website_quality` gaining a `main`.

Tests: `tests/test_pilot_harness.py` (27 cases) and `tests/test_pilot_browser_walk.py` (16). Both are
mostly negative — a browser criterion passed with a non-measured basis, a partial missing a criterion,
a binary file, an empty artifact, a secret re-leaked in the mapping; and on the walk, a mouse-only
control, a missing focus ring, pale text, an animation that ignores reduced motion, a console error and
a broken link, each of which must fail its criterion and name the page.

The walk's tests that need a browser skip unless `M5_PILOT_BROWSER` points at a Chromium binary, and
the skip says so. A test that invented a pass in the browser's absence would be the defect it guards.

## The operator side

The release drills the harness's sibling `release_drill_witness.py` exercises are written up as an
actual procedure in [release-rollback-runbook.md](release-rollback-runbook.md).
