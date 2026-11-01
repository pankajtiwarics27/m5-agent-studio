# UIUX-P3-03 approval UX evidence

The Harness approval card now makes the decision boundary visible before either action: who requested the call, the tool and argument preview, its session-bound one-use scope, request/grant expiry, the consequence of rejection, and the fact that policy, action-admission, and sandbox checks remain server-authoritative. Pending controls require a live, same-session approval in the latest server snapshot before the existing approval endpoint is called. A refused or unconfirmed request stays disabled until authoritative state is reloaded. The controls come from an inert, in-pane template so the preservation inventory and route attribution can map them: Harness `/state` verification and `/api/harness/approve` remain visible as the two sides of the approval journey.

## Screenshots

- [Before](media/uiux-p3-03/approval-before.png): original approval card, captured before implementation.
- [After](media/uiux-p3-03/approval-after.png): updated disclosure and decision controls.

Both captures were made against a disposable `scripts/demo_tour.py --serve` instance. The approval request and `run_bash` arguments were synthetic display data (`echo synthetic preview`) injected only into the browser DOM; no approval endpoint was called, no tool was executed, no provider/model was contacted, and no real token or user workspace was used. These screenshots demonstrate presentation only; they are not authorization or execution evidence.

## Verification

- `tests/test_harness_approval_ui.py` checks that the disclosure matches the server's current 600-second approval wait and grant TTL, and that the client verifies fresh server state before using the unchanged approval endpoint.
- The browser accessibility walk checks the live-card copy and controls, no controls for expired or cross-session requests, a 420px layout, and an adversarial fabricated approval. In that adversarial case, the server snapshot reports no pending slot; the client sends neither `/api/harness/approve` nor an execution request and leaves the action disabled.
- Existing server-side approval endpoint tests remain authoritative for session scoping, replay protection, authentication, unknown approvals, abort, and timeout. No backend authorization or approval semantics were changed.

This is implementation evidence for review, not owner sign-off. `UIUX-P3-03` remains in progress pending independent review and owner approval.
