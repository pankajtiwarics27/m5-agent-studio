---
id: ADR-0005
title: durable-harness-task-event-stream
status: accepted
date: 2026-10-30
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0005: Durable harness task event stream

## Context

The harness records each session's progress in an append-only JSONL file under
`.harnesses/sessions/`, and the UI re-rendered that file after a reload. The
file was treated as a debug trace, not as a stream: the sequence number was
recomputed by counting lines on every append, there was no cursor, no way to
detect a pruned or interrupted history, and terminal records or approval
decisions could be written twice. `M5-P1-07` needs the UI to reconstruct task
status on reload, to recover when the live stream drops events, and never to
accept a stale approval or show a duplicated completion.

Stored task state already lives in SQLite (schema v3, ADR-0003) with a durable
task state machine (`M5-P1-05`). The question was where the *event transcript*
belongs and how a client proves it is current.

## Decision

Keep the per-session JSONL file as the storage format and make it a
cursor-addressable stream through a new stdlib-only module, `task_events.py`,
which the app and the standalone bundle both use:

- A durable sequence sidecar (the per-session `.seq` file, atomic replace) plus writes
  with `O_APPEND` and `fsync`, so `seq` is monotonic and a lost append is
  detectable as a gap rather than renumbering history.
- Every read returns an explicit verdict: `events`, `next_cursor`, `latest_seq`,
  `retained_from_seq`, `has_more`, `truncated`, `snapshot_required`,
  `resync_reason`, and `missing_ranges`. A returning reader whose history was
  pruned or whose cursor is ahead of the store is refused instead of being shown
  a partial log; a fresh reader gets the retained tail flagged as truncated.
- Idempotent appends via `dedupe_key` for terminal turn records and approval
  decisions, with the key index rebuilt from the log after a restart.
- Bounded storage: prune past 8 MiB to the newest 4000 events, atomically.
- Three authenticated HTTP surfaces: `GET .../events` (cursor page),
  `GET .../events/stream` (SSE with `id` = sequence, `Last-Event-ID` resume,
  `resync_required` on an unresumable cursor), and `GET .../state` (authoritative
  snapshot: task row, budget, checkpoint, event summary, pending/expired
  approvals).
- The task state machine remains the authority for status; the event log is the
  authoritative *transcript* and the source of turn/approval outcomes.

## Alternatives

- **New SQLite table for events (schema v4).** Rejected for this story: it would
  add a migration and a second copy of large tool outputs inside the database that
  is exported and backed up, while the JSONL files already hold that data and are
  deliberately outside the SQLite export. It also does not by itself deliver
  cursors, gap detection, or idempotency.
- **Keep counting lines for `seq` and add a cursor endpoint only.** Rejected: the
  count is O(n) per event, races with concurrent appends, and cannot distinguish
  "history pruned" from "nothing happened".
- **Browser `EventSource` for the live stream.** Rejected: `EventSource` cannot
  send the bearer token, and the app's auth boundary is header-based. The client
  uses `fetch` with a reader and parses frames itself.
- **Treat the live POST stream as the source of truth.** Rejected: it is
  per-connection. A reload, a backgrounded tab, or a dropped socket would lose
  events with no way to notice.
- **Mark a task completed when a `turn/end` completion record exists.** Rejected:
  the runtime commit is the authority; a recorded event alone must not create a
  completion claim.

## Tradeoffs

- The store assumes a single application process per session, matching the
  worker-lease model. Cross-process concurrent appends are not supported and are
  not claimed to be safe; the in-process append lock plus atomic cursor replace
  is the whole concurrency story.
- A crash between sequence allocation and append leaves a permanent gap. That is
  the intended fail-closed direction (a resync, not a silent hole), at the cost
  of one spurious snapshot reload.
- Dedupe guarantees only extend over the retained window; pruning forgets keys
  along with the records they described.
- Two places now describe session state (SQLite task rows and the event log).
  They are reconciled in one direction only: `/state` derives status from the
  task rows and reports the event-derived turn/approval outcomes beside it, so a
  disagreement is visible instead of being overwritten.
- The transcript remains unencrypted and may contain prompts, tool arguments,
  and tool output. It inherits the harness store's retention posture and is not
  covered by the SQLite export.

## Review Status

Not reviewed. Implementation, tests, and documentation were produced by the
authoring agent; no independent reviewer has examined this decision, and the
durability claims above are limited to what `tests/test_task_events.py` and
[`docs/task-events.md`](../task-events.md) demonstrate in this checkout.
