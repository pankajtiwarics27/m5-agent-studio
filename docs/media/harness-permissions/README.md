# Harness permissions: the captures

Four states of the approval surface, photographed by `scripts/harness_permissions_walk.mjs`
against a real Studio instance. They are not mock-ups: the walk starts
`scripts/harness_permissions_seed.py`, which boots the ordinary isolated demo environment and
gives it two live approval gates and one expired offer through the app's own
`_create_pending_tool_approval()` - the function a running turn calls at the boundary. The
pane then reads its own `/state` snapshot and event log, and the walk only presses the buttons
an operator would press.

Regenerate them with:

```bash
export STUDIO_SCREENSHOT_CHROME=/path/to/chromium
export STUDIO_PUPPETEER_CORE=$PWD/tests/browser/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js
node scripts/harness_permissions_walk.mjs --screenshots docs/media/harness-permissions
```

| Capture | What it shows |
|---|---|
| `approval-card.png` | An approval before any decision: the policy in force, `step 3`, the tool, the exact arguments, the capability and resource the click records, the one-use limit, the expiry clock with a live countdown, and the sentence that says what approving does, what it does not do, and that the turn is waiting. |
| `expired.png` | An offer nobody answered. The card is drawn, it is not shaded as a live gate, its decision buttons are gone, and it says the run it belonged to ended and to re-run the turn - nothing was granted. |
| `permissions-open.png` | The permissions surface, opened from the badge in the session strip: the session and policy revision at the top, the grant the approval recorded with its capability, canonical resource scope, limit and `operator_approval` issuer, and the expired offer beneath. |
| `revoked.png` | The same surface after a two-step revoke: no permission held, the revocation rule that refuses `code_execution` on the session's workspace for the rest of the session, and the grant in the history with how it ended. |

The walk's own transcript is the evidence for the rest of the story - that the decision is
posted with the session that owns the approval, that a refusal records no permission, that
the badge the pane shows is the permission the approval recorded, and that the trajectory
draws the `approval/decision` and `security/grant_revoked` events the server appended.
`node scripts/harness_permissions_walk.mjs` runs all 24 checks and exits non-zero on any of
them.
