# UIUX-P2-04 — supplemental Chat stream and recovery evidence

## Scope and tracking

The P2-04 story and its owner/session tracking remain authoritative in the current-main roadmap. P2-04 is already recorded there as **done** by the session and commit that completed PR #142. This reconciliation does not reopen the story, replace its owner, claim owner approval, or change the preservation sign-off. It carries forward only distinct Chat stream/recovery behavior from PR #141 that remains useful with the merged #142/#143 implementation. Current-main P2-06 work from PR #144 and its owner/session tracking are preserved as well; this change does not modify that story.

The mainline Chat screenshots and story evidence remain in [`docs/uiux-2-0-chat.md`](uiux-2-0-chat.md) and [`docs/media/chat/README.md`](media/chat/README.md). They belong to the already-merged implementation and are not presented here as before/after proof of this supplemental change. No screenshots were captured for this reconciliation; the synthetic browser walk below does not take screenshots. Visual evidence for a later change must use `scripts/capture_screenshots.mjs` and `scripts/validate_screenshots.py`.

## Supplemental behavior and evidence

| Behavior retained from #141 | Evidence | Result / limit |
|---|---|---|
| Timestamped, explicit user/assistant turns and visible stream state | `scripts/chat_composer_walk.mjs` checks role, model, local `<time datetime>`, and `streaming` / `complete` states in the real Chat DOM | Chromium walk uses synthetic SSE only; no model call. |
| Stop retains partial output and returns focus after the stream finalizer releases Send | Browser walk streams a partial token, activates Stop, and verifies retained text, `Stopped`, Send enabled, Stop hidden, and focus returned to the composer | Tested in the isolated demo at desktop size. |
| Partial failure and EOF before a terminal SSE event remain recoverable | Browser walk injects a partial error and an unexpected EOF, and verifies the failure presentation | Synthetic errors only; no provider request. |
| Retry reuses one user turn rather than duplicating it | Browser walk retries, then counts the user turn in both the transcript and the retried request payload | One user turn observed in each. |
| Clear during failure finalization does not resurrect a stale failure | Browser walk clears while the synthetic stream is closing, waits past the delayed close, and verifies the empty state and the existing Undo/Dismiss recovery presentation | Clear remains undoable; stale error is not presented. |
| Enter, Shift+Enter, keyboard focus, and a 390×844 viewport | Browser walk uses keyboard input and checks the composer controls fit without document-wide horizontal overflow | The browser runs the actual UI; only `/api/chat/stream` is replaced in-page. |
| API, authorization, and durable-state preservation | `app.py` is unchanged; supplemental contract test checks `/api/chat/stream` and the existing request fields | No route, backend, persistence, approval, authorization, or workspace behavior was added or altered. |

## Reproduction

The walk accepts only a loopback HTTP demo URL, uses a throwaway demo token, replaces only `/api/chat/stream` in-page with controlled synthetic SSE, and aborts outbound browser requests. It makes no model/provider call and does not capture screenshots.

```bash
.venv/bin/python -m pytest -q tests/test_uiux_chat_composer.py tests/test_chat_composer.py tests/test_chat_context.py
node scripts/chat_composer_walk.mjs --base-url http://127.0.0.1:<port> --token <throwaway>
```

For the browser-walk environment, install the pinned tooling described in `docs/demo.md`, set `STUDIO_SCREENSHOT_CHROME` and `STUDIO_PUPPETEER_CORE`, and start the demo with `scripts/demo_tour.py --serve`.

## Approval and preservation caveat

The P0 preservation package remains `historical-approval-unbound`. This evidence is not owner approval, does not bind issue #126, and is not a sign-off. The merge/reconciliation itself must remain subject to review and required checks.
