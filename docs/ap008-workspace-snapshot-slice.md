# AP-008: workspace snapshot disclosure slice

## Scope and blast radius

The existing Harness workspace disclosure reads the current Git diff and task snapshot. This slice adds read-only workspace snapshot summaries from the existing session-scoped `GET /api/harness/sessions/{session_id}/snapshots` route. The route lists integrity-verified durable snapshot summaries; they are workspace recovery versions, not artifact versions or approval records. The Git diff remains a current, potentially truncated view, not a preview of a recorded snapshot. No new endpoint, file read, restore, authority, grant, provider, external destination, worker, or runtime activation is added. Snapshot labels and IDs are untrusted and must be rendered only as text.

The new section shares the disclosure's session and request serial fence. It must independently report loading, empty, malformed, denied and offline outcomes without suppressing the diff. Switching sessions or a newer refresh invalidates old results. The panel identifies the latest recorded workspace snapshot without asserting it matches the live workspace; sources and artifact versions remain unavailable where no existing integrated authority exposes them. Existing conversations, composer, recovery, streaming, model/tool controls, Settings and five other panes remain untouched. A snapshot identifier confers no restore permission.

## Alternatives, checks, and rollback

Showing a live site in an iframe or adding a workspace-file endpoint would expand origin and file-read authority; neither is necessary for this bounded slice. Inventing artifact version identifiers from Git status would be misleading. Reuse snapshot metadata, retain inert text-only Git preview, and state the remaining limitation.

Tests: focused Harness snapshot/approval/recovery and workspace snapshot tests, source/route/control generators, scope and shard maps; Puppeteer recovery walk with actual snapshot route, session switching, injected text, denied/offline and mobile/zoom coverage; cross-tab structural walk and browser safety. Run the owning source-bound evidence generators and CI gates. Review the rendered disclosure. Risks: old reads showing a previous session, corrupt payload being treated as empty, untrusted labels interpreted as HTML, and claims of freshness not supported by the snapshot API. All are covered by fail-closed rendering and tests. Rollback: revert this slice and its generated artifacts; no data migration or backend state change occurs.
