# AP-008: evidence modal session fencing

## Existing behavior and risk

The verification modal uses session `/task`, `/diff`, and cached durable verdict events. Its single busy flag prevents a second refresh while an earlier read is in flight. A session switch, close/reopen, or newer refresh can therefore leave previous-session content visible or make the new session wait for a stale request; switching away and back can also let a late response look current. This slice fences every read by a monotonically increasing request serial and selected session, clears previous content immediately on open/refresh and session reset, and invalidates in-flight reads on close. It changes no server route, data authority, policy, permission, storage, runtime, provider, or active preview. Text and source data keep the existing escaped rendering; canceled reads have no effects.

Alternatives rejected: retaining previous content with only a busy indicator, or adding another control plane. A sequence fence allows a fresh request while an older request is slow; no claim is made that canceling a fetch cancels server-side work. No state-changing operation is involved. Existing overlay/focus behavior and all panes remain intact.

## Tests and rollback

Test overlapping same-session and A-to-B-to-A reads, close/reopen, failed requests, clearing stale content, and positive task/diff rendering; test the browser modal and existing keyboard/focus behavior. Regenerate the source-bound UI artifacts through their owners and run existing safety/visual gates. Rollback reverts this frontend slice and regenerated artifacts; no migration or persisted state changes occur.
