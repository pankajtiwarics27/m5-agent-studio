# P3-03 supplemental evidence: re-confirm the pending approval

The main P3-03 implementation and its owner attribution are preserved from merged PR #149. Its approval-card and permissions evidence remains in [`uiux-2-0-harness-permissions.md`](uiux-2-0-harness-permissions.md) and [`media/harness-permissions/`](media/harness-permissions/). This branch adds one narrowly scoped client safeguard: immediately before the existing `POST /api/harness/approve`, the click handler fetches the existing session-state endpoint and confirms that the same session still reports this approval ID as pending. The approval API, server-side session scoping, durable task state, replay protection, action-admission rules, sandbox checks, and execution flow are unchanged.

If the session changed, the state request fails, or the server no longer reports the pending slot, the client sends no decision and keeps the controls disabled. A successful authoritative reload is the only path that can render an action again. The server remains authoritative and re-checks the approval when the existing POST arrives; this preflight is not an authorization grant and cannot replace those checks.

## Adversarial browser evidence

`tests/browser/accessibility_walk.mjs` constructs a deliberately false client cache containing a fabricated pending approval ID. The stubbed server `/state` response reports no pending slot and lists the ID as expired. The test keyboard-activates the visible approve control and verifies that the browser requests fresh state but sends neither `POST /api/harness/approve` nor any execution request; after re-sync, the card has no decision controls. This exercises a hostile client-side state assertion without minting a server approval or running a tool.

`tests/test_harness_approval_ui.py` additionally verifies that the current approval handler checks the session ID and matching live pending slot before reaching the single, unchanged approval POST, and that stale/failed paths do not re-enable cached controls. A disabled, never-cloned template anchor makes the dynamically rendered decision handler visible to the pane inventory; the regenerated preservation sign-off now attributes both `GET /api/harness/sessions/{session_id}/state` and `POST /api/harness/approve` to Harness.

On the reconciled tree after merging the intervening P3-04 main update, the full suite passed **2,147 tests, 1 skipped** (live sandbox requires explicit opt-in), with one existing Starlette deprecation warning. Ruff, mypy, frontend syntax, browser-backed six-pane and accessibility walks, docs/screenshots validation, and all generated UI/UX inventory checks passed.

This is supplemental implementation evidence for independent review. It does not alter the P3-03 status, owner session, or approval recorded on main, and it is not a substitute for owner review or sign-off.
