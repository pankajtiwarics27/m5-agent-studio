# Parked work register — UI/UX P7

**Owner:** repository owner
**Parked:** 2026-11-08
**Tracker:** PR #258
**Status:** 1 item parked — P7-PARK-03 (P7-PARK-01 resolved 2026-11-08, P7-PARK-02 resolved 2026-11-09)

This page exists so that work deliberately parked during `UIV2-P7` has an owner, a
reason and a fix procedure written down in the repository rather than a merged PR
description. Each entry names the exact unblock condition; when it is met, the entry
is deleted rather than annotated.

---

## P7-PARK-03 — UIV2-30 release review assertions parked during the phone Hall fix — **PARKED 2026-11-09**

| Field | Value |
|---|---|
| Artifact | `tests/test_uiv2_release_review.py` (21 assertions) |
| Generator | `scripts/uiv2_release_review.py` (left in place, still runnable) |
| Decision | owner, 2026-11-09 — asked after the re-captured phone Hall baseline was approved, answered "Park tests/test_uiv2_release_review.py again" |
| Mechanism | module-level `pytest.mark.skip`; no test deleted, no assertion edited |
| Unblock condition | a green CI run of a commit that already contains the phone Hall fix, with fresh Compare and cross-tab evidence imported against that run |
| Blocks | nothing in P7; the module is the release-handoff contract, not product behaviour |

### Why it cannot pass as written

Same bootstrap as P7-PARK-02, for a different reason. `validate_evidence()` requires the
committed evidence to cite the green CI run of the commit that contains it. The phone Hall fix
changes `static/index.html`, so the evidence imported at merge commit `6aca516` no longer binds
the current browser sources and `findings` is non-empty. The module runs on every push, so it
asserts "the evidence is current" while being the thing that runs before the evidence can be
current. Satisfying it inside one commit would mean writing a `ci.headSha` for a run that never
happened.

**Nothing here is a product regression, and nothing here relaxes a visual claim.** The change it
waits on was measured, not assumed: the cross-tab walk captured at a clean HEAD with
2104/2104 structural checks passing, and exactly two of 48 captures moved — one field each,
`required.#hallStream.y` 2273.61 -> 1740.42 at 390x844, in both themes. The owner approved that
re-capture on 2026-11-09 and the approval is committed as
`product/M5_2_0_UIUX_V2_Cross_Tab_Approval.json`, pinned to head `99fada2`.

### Unblock procedure

1. Merge the fix; note the merge commit's green run.
2. Run `scripts/compare_continuity_walk.mjs` and
   `scripts/uiux_v2_cross_tab_reference_walk.mjs --check` at that commit (both need a browser).
3. `scripts/uiv2_release_review.py --import-evidence --compare-run … --visual-run … --ci-run …`,
   then regenerate and `--check`.
4. Delete the `pytestmark` block and this entry, and re-run the module: 21 assertions, unedited.

While parked, `scripts/uiv2_release_review.py --check` reports the contract as stale — read
`findings` before concluding anything, and do not regenerate
`product/M5_2_0_UIUX_V2_Release_Review.json` while parked.

---

## P7-PARK-01 — UIV2-30 release review assertions retired — **UNPARKED 2026-11-08**

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

### Unparked — 2026-11-08

The unblock condition was met and the assertions are live again. `main` went green at
merge commit `5fb978b`, which gave the evidence a legitimate implementation HEAD it could
cite, because the evidence import is now a normal follow-up commit on top of an
already-green CI run rather than an attempt to cite a run of itself.

Procedure followed, in the documented order:

1. `compare_continuity_walk.mjs` — 15/15 checks, 6 captures, run from a clean tree at `5fb978b`.
2. `uiux_v2_cross_tab_reference_walk.mjs --check` — 48 captures, 2200/2200 checks.
3. `gh run view 42517037242` — the real green run of `5fb978b`, all four required jobs.
4. `uiv2_release_review.py --import-evidence` — imported all three inputs.
5. Ledger regenerated from that run's measured triples, then the contract.

Result: `uiv2_release_review.py --check` reports **7/7 local checks, 2200/2200 visual,
15/15 Compare, 0 findings**, decision `ready`. The module's 21 tests pass.

One defect was fixed rather than re-parked:
`test_uiv2_29_final_summary_uses_actual_metrics_not_flat_transcription` hardcoded a heap
triple from a single run, so any legitimate regeneration of the UIV2-29 record broke it.
It now asserts the ledger quotes the triple recorded in the committed run — the property
that matters — instead of one particular sample.

**Do not re-park this module without an owner decision, and never satisfy it by citing a
CI run that did not happen.**

### Unblock procedure (kept for reference if it is ever re-parked)

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

**Do not regenerate its committed artifacts while parked.**
`product/M5_2_0_UIUX_V2_Release_Review.json` and `docs/uiv2-v2-release-review.md` are
left exactly as they were produced at `dfa6647`, when their evidence was current. They
are a historical record. Regenerating them now would overwrite that record with a
`blocked` decision caused only by the parking, not by a real change in readiness. The
generator's `--check` therefore fails by design while parked; nothing in CI runs it.

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
