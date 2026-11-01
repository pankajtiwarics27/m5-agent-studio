# UIUX-P2-04 — Chat composer and conversation hierarchy

## Acceptance evidence

| Acceptance area | Evidence | Result / limit |
|---|---|---|
| Empty conversation, explicit model/persona/reasoning mode, retained tools/unload/clear controls | [`after-dark/chat.png`](media/uiux-p2-04/after-dark/chat.png), [`after-dark-mobile/chat.png`](media/uiux-p2-04/after-dark-mobile/chat.png), [`after-light/chat.png`](media/uiux-p2-04/after-light/chat.png); capture metadata is in each adjacent `index.json` | Captured by `scripts/capture_screenshots.mjs` against `scripts/demo_tour.py --serve`; dark/light and 390px wide states. The screenshots show the empty state, not a live model response. |
| Before/after layout at matching viewport sizes | [`before-dark/chat.png`](media/uiux-p2-04/before-dark/chat.png), [`before-dark-mobile/chat.png`](media/uiux-p2-04/before-dark-mobile/chat.png), compared with the after captures above | Both sets were captured by the prescribed script from disposable source copies and separate isolated demo servers. |
| Role, model, local time and token metadata; streaming and successful completion | `scripts/chat_composer_walk.mjs` asserts the role-bearing user/assistant entries, model, `<time datetime>`, completion-token count and decode rate against synthetic SSE frames | Browser test passed without contacting a model provider. |
| Stop, partial-output retention and focus recovery | Browser walk sends a synthetic partial token, activates Stop, then checks `Stopped`, retained text, restored Send state, and focus returning to the composer | Passed at 1280px; the same walk checks the narrow layout after recovery. |
| Failure, retry, unexpected stream close and duplicate prevention | Browser walk covers a partial SSE error, retries the same turn and verifies a single user entry in both transcript and request payload; it also closes a stream without a terminal event. `scripts/presentation_walk.mjs --json` separately exercises the refused-request presentation and Dismiss path | The chat walk passed; presentation walk passed 20 checks with 0 failures and 0 blocked. All text and credentials were synthetic/throwaway. |
| Keyboard and narrow viewport | Browser walk checks Enter to send, Shift+Enter newline, Stop focus return, and all Chat controls within a 390×844 viewport with no document-wide horizontal overflow | Passed. The actual app was loaded in Chromium; only `/api/chat/stream` was replaced in-page with controlled synthetic SSE. |
| Backend/API/authorization preservation | `app.py` was not changed. `tests/test_uiux_chat_composer.py`, the route map, authorization trace, preservation sign-off and existing Chat route tests verify the unchanged `/api/chat/stream` boundary and request fields | No new route, dependency, authorization, persistence or provider behavior was introduced. The browser walk sends only to the isolated demo server; its Chat stream is mocked locally. |

## Reproduction

The browser test is included in `tests/test_screenshot_capture_browser.py` and runs in the browser-safety CI job. Locally, install the pinned browser tooling described in `docs/demo.md`, set `STUDIO_SCREENSHOT_CHROME` and `STUDIO_PUPPETEER_CORE`, then run:

```bash
.venv/bin/python -m pytest -q tests/test_screenshot_capture_browser.py::test_chat_loading_stream_success_stop_failure_retry_and_mobile --tb=short
```

The test starts `scripts/demo_tour.py --serve`, which creates a disposable workspace and throwaway token. The walk accepts only loopback HTTP origins and aborts any browser request to another origin. It never calls a provider or takes screenshots.

For fresh visual captures, start the isolated demo and use `scripts/capture_screenshots.mjs --panes chat --emulate-theme dark --viewport 390x844 --out <separate-output-directory>` (repeat with `--viewport 1280x900`, and `--emulate-theme light` for variants). The screenshot tool is the only source of the images linked here.

## Scope and approval

Only the Chat frontend, its generated standalone bundle, story tests, and derived evidence artifacts changed. `app.py` and the Chat endpoint contract did not. The P0 preservation package still reports `historical-approval-unbound`; this evidence is not owner approval or a claim of sign-off. P2-04 remains `in_progress` pending PR review.
