# Parked work register — UI/UX P7

**Owner:** repository owner
**Parked:** 2026-11-08
**Tracker:** PR #258
**Status:** 1 item parked, 0 items silently forgotten

This page exists so that work deliberately parked during `UIV2-P7` has an owner, a
reason and a fix procedure written down in the repository rather than a merged PR
description. Each entry names the exact unblock condition; when it is met, the entry
is deleted rather than annotated.

---

## P7-PARK-01 — UIV2-30 release review assertions retired

| Field | Value |
|---|---|
| Artifact | `tests/test_uiv2_release_review.py` (21 assertions) |
| Generator | `scripts/uiv2_release_review.py` (left in place, still runnable) |
| Decision | owner, PR #258, option A — retire rather than keep the branch red |
| Mechanism | module-level `pytest.mark.skip`; no test deleted, no assertion edited |
| Blocks | nothing in P7; the module is the release-handoff contract, not product behaviour |

### Why it cannot pass as written

`validate_evidence()` requires the committed evidence (`product/M5_2_0_UIUX_V2_Release_Evidence.json`)
to name a commit whose **own** green CI run it cites, and to bind
`ci.headSha == implementation_head`. The module reads that evidence and runs on every
push; the scoped selector includes it for any change to `static/index.html`, to the
evidence JSON, or even to a docs file:

```
product/M5_2_0_UIUX_V2_Release_Evidence.json   includes_release_review=True
tests/test_uiv2_release_review.py              includes_release_review=True
docs/uiux-2-0-competitive-research.md          includes_release_review=True
```

So the test asserts "the evidence is current" while being the thing that runs before
the evidence can be current. Satisfying it inside one commit would require writing a
`ci.headSha` for a run that never existed — the exact fabrication the contract exists
to prevent. That is the only reason it is parked rather than fixed.

A second, independent reason: `test_uiv2_29_final_summary_uses_actual_metrics_not_flat_transcription`
hardcodes the previous run's heap triple (`3375100 -> 3337896 -> 3342772`). The UIV2-29
run record had to be regenerated (its source digest pins `static/index.html`), and heap
figures move run to run, so legitimately refreshing evidence fails that assertion too.
Its replacement must read the triple from the run record instead of hardcoding it.

### Unblock procedure

Run these in order once CI has been green on this branch at a known commit:

1. Note that run's `headSha` — the implementation HEAD the evidence may legally cite.
2. Import the three evidence inputs:

   ```
   python3 scripts/uiv2_release_review.py --import-evidence \
     --compare-run <compare.json> \
     --visual-run <check-manifest.json> \
     --ci-run <ci.json>
   ```

3. Regenerate the ledger from that run's measured triples, then the contract:

   ```
   python3 scripts/uiv2_release_review.py
   python3 scripts/uiv2_release_review.py --check
   ```

4. Delete the `pytestmark` block in `tests/test_uiv2_release_review.py` and the module
   docstring's `RETIRED` paragraph, then re-run the module.

**Anti-regression while parked:** the contract still reports its own decision. Run
`python3 scripts/uiv2_release_review.py --json` and read `decision`; while parked it
reports `blocked` for the two bootstrap findings. That is expected, not a new defect.

---

## What is *not* parked

Recorded here so this register is not read as a broader waiver:

* `scripts/uiux_v2_cross_tab_reference.py` — **active and green at 2200/2200**, on an
  owner-approved baseline (PR #258, "1. Approved"). Not parked.
* `scripts/uiv2_performance_safety.py`, `scripts/uiv2_accessibility_adversarial.py` —
  **active and green** from real Chromium 153 runs (2104/2104 checks, 120/120 checks,
  302 backend tests, 16 isolation checks). Not parked.
* Every `scripts/uiux_*.py --check` inventory gate — **all passing**. Not parked.
* Production security release — remains blocked by `scripts/security_release_gate.py`,
  which is a pre-existing, intentional block and has nothing to do with this register.
