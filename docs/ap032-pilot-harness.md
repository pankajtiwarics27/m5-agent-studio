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

## What it refuses (6 of 11)

`keyboard_reachable`, `focus_visible`, `contrast_ok`, `reduced_motion_respected`, `console_clean` and
`navigation_works` are emitted as `passed=False, basis="cannot-measure-here"` with the reason. A focus
ring is a computed style; contrast is a computed colour pair; a console error only exists once scripts
run. A regex standing in for any of those would be a green row that means nothing.

They are never silently dropped, either. `PilotPartial` refuses to be constructed unless it carries an
outcome for all 11, so "unmeasured" can never render as "absent".

## What it emits

A `PilotPartial`, whose mapping leads with `is_a_qualification_report: false` and lists
`what_a_pilot_report_still_needs`: owner review, the layout/interaction measurements, the six browser
criteria, and the artifact head the run applied to. The mapping is JSON, so it can be attached to the
release evidence as-is without being mistaken for a qualification report.

```bash
python pilot_harness.py path/to/built/site            # JSON on stdout, exit 1 if any offline criterion fails
python pilot_harness.py path/to/site --out partial.json
```

Exit codes: `0` offline axes clear, `1` an offline criterion failed or the artifact was refused (empty,
binary, or containing nothing to audit), `2` the path is not a directory. A refusal is never `0`.

## How to use it at a real head

1. Build the site at the head you want to qualify.
2. Run the harness against the built directory. It audits files on disk and never fetches, renders or
   executes them.
3. Fix anything red on the offline axes and re-run. This is the cheap loop.
4. Only then open the site in a browser and decide the six remaining criteria by hand, recording what
   you did: which viewport, which keyboard path, which contrast tool, what the console showed.
5. Run the real pilot through `website_quality` to produce the `PilotReport`, and attach the partial
   beside it. The partial does not replace the report; it explains the report's offline rows.

## What this does not do

- It does not start a browser, take screenshots, or produce the `screenshot` approval.
- It does not review the artifact. `OwnerReview` refuses agent-shaped actors, and this harness does not
  attempt one.
- It does not make the pilot blocker disappear. Six criteria and the owner review still need a human
  at a head; the harness makes those the *only* things left.

Tests: `tests/test_pilot_harness.py` (27 cases). They are mostly negative — a browser criterion passed
with a non-measured basis, a partial missing a criterion, a binary file, an empty artifact, a secret
re-leaked in the mapping — because the failure mode worth guarding is a partial that looks complete.

## The operator side

The release drills the harness's sibling `release_drill_witness.py` exercises are written up as an
actual procedure in [release-rollback-runbook.md](release-rollback-runbook.md).
