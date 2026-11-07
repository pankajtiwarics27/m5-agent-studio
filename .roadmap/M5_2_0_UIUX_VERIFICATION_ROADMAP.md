# M5 2.0 UI/UX verification roadmap

Authoritative machine-readable tracker: `product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json`.
This index records the latest completed story and the remaining queue; it does not create a second
claim ledger. Local PNGs and capture manifests are evidence, not Git release inputs.

## Completed

### UIV2-27 — Functional preservation and defect closure

**Status:** done locally at implementation commit
`e11c902a0ee896bc0f0c76137736caaa6e79b5c0`; integration is in PR #233 and must have four green CI
jobs before merge.

- **Defects closed:** successful Council terminal SSE frames no longer become dropped turns; refused
  deletion cannot discard the active pane; the populated Harness trajectory keeps a 90 px short-window
  floor; Compare waits for actual finalization; current native disclosures, the complete Chat handoff,
  and all six Harness decisions are exercised without weakening existing assertions.
- **Functional proof:** clean-commit preservation run **21/21 rows, 32/32 steps, 427.8 s**, including
  13 route probes, 14 pytest nodes and 18/18 walks. The original complete baseline was **9/21** and is
  retained as defect-finding history, not as approval evidence.
- **Executable map:** **21 rows / 73 provers / 0 findings**, with **243 controls and 118 routes** still
  inventoried. `app.py` and backend authorization/payload contracts are unchanged.
- **Focused proof:** 151 Council/Harness/frontend/cross-tab/Pipeline tests passed; 142 inventory/contract tests
  passed with one environment-only historical-commit skip. The sole initial-CI generated-artifact
  failure was reproduced, fixed by the existing UIV2-25 generator, and its 28 checks plus focused test
  now pass.
- **Approved visual reference:** six tabs × four viewports × light/dark, **48 captures and
  2,200/2,200 checks**. The Council recovery matrix passed **184/184** with 40 captures; the long
  conclusion matrix passed **72/72** with 16 captures. Real detached-`660b504` before-images reproduce
  the 80 px Harness floor and Council verdict failures; after-images prove the fix.
- **Generated gates:** deterministic standalone and source artifacts; release gate **37/37 checks,
  7/7 phases, 0 findings**; committed screenshot review **1,784 captures, 0 findings**.
- **Evidence guide:** `docs/media/uiv2-27-preservation/README.md`. Run record SHA-256:
  `adad9bcd876f6a43b52d60eb72ffc3f5f64490d56bd489270a6d42786ccca3de`.
- **Integration note:** initial CI run `42396138225` passed Browser safety and found one stale
  UIV2-25 generated inventory in all three Python jobs. That artifact and its documentation are
  regenerated in the final tracking commit; no test was relaxed. Current `origin/main` `22a07ee`
  (host-path selection and Chat recovery/completion fixes) is integrated; all derived inventories
  regenerate cleanly and the combined focused suite is **299 passed**.
- **Rollback:** revert the story's merged PR with a merge-commit revert. Do not delete sessions,
  databases, workspaces, credentials, migrations, or user files. This story added no migration.

Earlier completions UIV2-01 through UIV2-26 remain recorded in the machine-readable tracker. UIV2-26's
human-approved structural baseline is reused by UIV2-27; no new visual approval was inferred from a
passing test.

## Current queue

1. **UIV2-28 — Accessibility, scroll and responsive adversarial suite.** Start only after PR #233 is
   green and merged. Preserve the new terminal-SSE, deletion, long-conclusion, six-decision Harness,
   and cross-tab checks.
2. **UIV2-29 — Performance and backend safety regression gate.** Depends on UIV2-28; retain explicit
   environment classifications and all authorization/model-admission boundaries.
3. **UIV2-30 — Release review, truthful README and agent handoff.** Depends on UIV2-29; review actual
   product state and evidence rather than copying roadmap claims.

**Progress:** 27/30 stories done (90%). P6 is 2/5 done (40%); three stories remain not started.
