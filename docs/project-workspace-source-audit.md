# Project workspace: source audit and first implementation boundary

This audit supports [the delivery plan](project-workspace-delivery-plan.md). It describes
source behavior, not a qualification of the website pilot or an approval of the designs.

## Existing surfaces and ownership

- `static/index.html` owns all six pane routes. Harness owns the session sidebar,
  task composer, trajectory, event log, recovery and the existing workspace disclosure.
  Start can hand a task to Harness; Chat can hand a conversation over. A new project
  screen must not strand these routes or duplicate their execution state.
- Harness reads `GET /api/harness/sessions/{session_id}/state`, `/task`, `/events`,
  `/diff` and `/snapshots`. The existing `harnessWorkspaceReview` shows progress,
  declared verification files, workspace recovery snapshots and an inert Git diff.
  A workspace snapshot is not an artifact version; the current diff is not a frozen
  artifact or proof that a recorded file still matches its digest.
- The verification and artifacts modal renders `session_artifact_versions` from the
  session task response. These are derived from checkpoint references and recorded
  provenance, with missing identity axes explicitly reported. The digest is not
  rechecked against current file contents. Global lineage is separately labelled and
  cannot be substituted for this session's artifacts.
- The same modal contains version-scoped feedback. The session feedback endpoints
  support read, submit, confirmation and withdrawal; a text comment does not itself
  accept an artifact. The separate reference-benchmark routes have their own scope
  and authority. These are existing capabilities, not evidence that an integrated
  side-by-side review flow has been completed.
- The existing workspace preview is text-only. This audit does not establish a safe,
  session-bound active website preview route. Do not load an arbitrary project page
  into the Studio origin, or imply that the diff is a website preview.

## Review-state blocker discovered

Before the working-tree fix, `loadHarnessFeedback` checked the active session after a
successful response, but its error path assigned and rendered the old target without
that check. A deterministic
local probe invoked the actual function, started a read for session A, switched the
active session to B, then rejected A's request: the resulting payload was for A while
B was active. This proves a stale error can be drawn for a different session. It does
not prove that a write occurred.

The initial implementation also lacked a request-generation check for overlapping
reads of multiple artifact versions, and mutation handlers could redraw after switching
views. The current working-tree slice fences feedback reads and mutation responses with
a view generation, target and active session. It keeps unsent feedback drafts by target
in page memory only; a page reload still loses those drafts. It reports an uncertain
submission when the user returns to a target after navigating away mid-request. A late
write may still have reached the server; view fencing does not cancel or undo it.
Server-side version/authority checks remain necessary; client-side fencing is not a
replacement. The reference panel now separately fences project-scope reads and approval
responses by session, target and view generation; it refuses to combine a reference
scope with a different feedback target. Approval text and approver drafts are retained
per target in page memory only, not across reload. An approval sent before leaving may
still have been recorded and is reported as uncertain when returning.

## Next bounded slice

1. The current feedback slice covers delayed and reordered responses, modal invalidation,
   session and version changes, and page-memory draft isolation. A Node probe exercises
   the actual extracted handlers; the existing browser walk exercises real routes. The
   probe is not itself a full browser race or evidence of durable drafts. Full CI and a
   current-head release review remain outstanding.
2. The current reference slice exercises reordered reads, stale write success/failure,
   project-scope mismatch and unsent approval text through a Node probe of the actual
   handlers. The existing browser walk covers the normal approval flow, not delayed
   browser races. Approval and reference-use authority are unchanged. Browser race
   coverage, full CI and current-head review remain outstanding.
3. Design a read-only adjacent artifact surface using the session-bound
   data. It must remain honest about missing previews and provenance. Test the real
   layout at desktop, mobile and 200% zoom, including keyboard/focus and reconnect.

This work changes no permission policy, recorded owner approval, provider grant or
runtime activation. Any change to those boundaries requires its own authority and review.
