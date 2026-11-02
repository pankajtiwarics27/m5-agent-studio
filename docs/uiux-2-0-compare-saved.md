# UIUX-P4-06 — Save, revisit and export a Compare run

## Scope kept

This is a browser-only change in `static/index.html`. It adds no endpoint, schema, migration, authorization rule, or server-side persistence path. Compare still starts through the existing `POST /api/debate/stream` SSE lifecycle; a run is confirmed as saved only when its existing `debate_done` event provides a positive `run_id`.

## What is saved and where

- **Reload recovery in this tab:** the latest visible comparison is copied to `sessionStorage` under `m5_compare_last_view_v1`. The copy keeps the prompt, distinct model panels, synthesis, response usage fields, current model/judge selections, and criteria visible in the tab. It is browser-local, can contain unredacted model text, is not the server export, and can be removed with **Forget this tab's copy**. Storage denial, invalid data, or a copy over the 256 Ki-character limit is stated without changing server history.
- **Completed-run revisit:** **Find saved comparisons** reads the existing authenticated `GET /api/history/export` only when requested, filters `mode: "debate"`, and lists the newest 50 rows. **Revisit selected** reconstructs the saved prompt, each opinion as its own card, synthesis as a separate verdict, and server-reported elapsed time. It does not rerun models, load a model, or alter history. Original model choices that are no longer in the current catalog are disclosed and not loaded.
- **Criteria limit:** the existing server record does not persist custom Compare criteria. A history revisit therefore shows the default criteria and explicitly says those defaults were not recovered from the record.
- **Download:** **Download full history export** downloads the existing full JSON bundle (all exported tables), not a Compare-only file. The server's credential redaction is best-effort; the UI states this before and after the action.

A partial, stopped, failed, interrupted, or completed-without-run-id view may still be restored from this tab's session copy. The status differentiates that case from a server-confirmed completed record. When a restored copy precedes catalog loading, the original choices remain pending until the catalog arrives; unavailable choices are reported rather than loaded.

## Recovery and keyboard behavior

The labelled history selector is associated with the live status region. Find, Revisit, Download, and Forget are native buttons/selects. A history read or download failure leaves the current comparison and server history untouched, restores the Find/Download controls for retry, and names the existing route and failure. The empty-state presentation no longer overwrites a comparison recovered during page initialization.

## Evidence

- `tests/browser/compare_saved_walk.mjs` runs the real page in Chromium against `scripts/demo_tour.py`'s disposable loopback instance and synthetic bearer token. It mocks only the authenticated history-export response at the browser boundary, verifies reload restoration, separate opinions and synthesis, history error/retry, history revisit, download authorization, keyboard focus, zero console errors, and that no model stream starts.
- `docs/media/compare-saved/after/index.json` records hashes and dimensions for the two browser-walk captures. The after images use fabricated fixture responses and a mocked export; they illustrate the UI and are **not** evidence of a real saved server record or a provenance attestation. `docs/media/compare-saved/before/` is the separately validated six-pane baseline.
- Validation run: `2330 passed, 27 skipped` across the Python suite (the repository's opt-in/live-runtime tests account for skips), plus `3 passed` in the required Compare browser walk. Chromium reported no console errors on the tested journeys. The broader pre-existing browser regression suite was not forced on in the full run.
- The generated UI source, control, route, authorization, presentation, status, token, and preservation inventories were refreshed from their generators. The preservation package remains `historical-approval-unbound`; no owner approval, PR review, or independent review is asserted here, and the roadmap ledger's story tracking was not edited.
