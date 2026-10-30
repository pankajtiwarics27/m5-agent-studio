# Durable harness task events

Status: implemented in `M5-P1-07` (2026-10-30). This page documents observed
behaviour of the shipped code, the endpoints that expose it, and the guarantees
it does **not** provide. It is not a durability or security certification.

## Why this exists

The harness UI shows a live transcript while a turn runs. Before this story the
only durable record was a per-session JSONL file that the UI re-read after a
reload, and the only live channel was the `POST /api/harness/run` (and
`gsd-wave` / `sdlc/run`) event stream. Three problems followed from that:

1. A reload could show a transcript whose completeness could not be judged: a
   dropped live frame or a pruned log looked identical to "nothing happened".
2. There was no cursor, so a reconnecting client had no way to say "I have
   applied everything up to sequence N".
3. Terminal outcomes could be recorded twice (a resolver and a waiter both wrote
   an approval decision), and an approval id could be re-submitted after a
   process restart because the replay tombstone lived only in memory.

`task_events.py` turns that file into a cursor-addressable durable event stream
and the UI consumes it as the authoritative transcript.

## Storage layout

| Path (under the harness store) | Content |
|---|---|
| `<session_id>.jsonl` | One JSON object per line: the payload fields plus `seq`, `ts`, `type`, and (when idempotent) `dedupe_key` |
| `<session_id>.seq` | Durable last-allocated sequence number, replaced atomically |
| `<session_id>.messages.json` | Conversation snapshot (unchanged; not part of this stream) |

The default harness store is `.harnesses/sessions/` beside the entry point
(`HARNESS_STORE_DIR`). Directory and files are created owner-only (`0700` /
`0600`).

Session ids are validated, not sanitized: anything containing a path separator,
`..`, or a non `[A-Za-z0-9._-]` character is rejected with
`task_events.TaskEventError`, because a normalized id would silently address a
different session.

## Sequence and cursor semantics

- `seq` starts at 1 per session and strictly increases. A reader that has applied
  `seq = N` requests `> N`.
- Allocation is durable: the next number is written to `<session_id>.seq` before
  the record is appended, and appends use `O_APPEND` + `fsync`. A crash between
  allocation and append therefore shows up as a **gap**, never as a renumbering.
- A read answers with an explicit verdict, not just a list:

| Field | Meaning |
|---|---|
| `events` | Records with `seq > cursor`, ascending, at most `limit` |
| `next_cursor` | Highest applied `seq` after this page (`cursor` when empty) |
| `latest_seq` | Highest `seq` in the store |
| `retained_from_seq` | Oldest `seq` still retained after pruning |
| `has_more` | More records exist past `next_cursor` |
| `truncated` | Older history was pruned; the retained tail is still a valid starting point for a fresh reader |
| `snapshot_required` | The requested cursor cannot be resumed; the caller must reload the snapshot |
| `resync_reason` | `events_pruned`, `sequence_gap`, or `cursor_ahead` when `snapshot_required` is true |
| `missing_ranges` | The exact `from_seq`/`to_seq` ranges that are unrecoverable |

Two rules make this fail closed:

- A **fresh reader** (`cursor = 0`) receives the retained tail with
  `truncated = true` instead of a refusal: it has no prior state to contradict.
- A **returning reader** whose `cursor + 1` is older than `retained_from_seq`, or
  whose cursor is ahead of the store, is refused
  (`snapshot_required = true`) rather than shown a partial history.

## At-most-once terminal records

`append(..., dedupe_key=...)` is idempotent: if a retained record already carries
that key, the durable record is returned unchanged and nothing is written. The
runtime uses this for:

| Record | Dedupe key |
|---|---|
| `approval/decision` | `approval-decision:<approval_id>` |
| `turn/end` | `turn-end:<epoch>:<state>` |
| `turn/verified` | `turn-verified:<epoch>` |
| `turn/verification_failed` | `turn-verification-failed:<epoch>` |
| `turn/cancelled` | `turn-cancelled:<epoch>` (or `:<epoch>:<step>` inside a turn) |
| `turn/error` | `turn-error:<epoch>` |

Because the key index is rebuilt from the log, the guarantee survives a restart.
The window is the retained log: a pruned key is forgotten, and the record it
described is gone from the store as well.

## HTTP endpoints

All three routes are `/api/*`, so they require the standard bearer token, host
allowlist, and Origin policy. They expose no new trust boundary.

### `GET /api/harness/sessions/{session_id}/events?cursor=0&limit=200`

Returns the page described above (plus `session_id` and `stream_version`). `limit`
is clamped to 1..1000. Unknown sessions are `404`; malformed ids are `400`.

### `GET /api/harness/sessions/{session_id}/events/stream?cursor=<n>&drain=false`

Server-sent events for one session.

- Each event frame carries the sequence as the SSE `id` and is typed
  `task_event`: `id: 42` / `event: task_event` / `data: {...}`.
- The cursor comes from the `cursor` query parameter, else the `Last-Event-ID`
  header (what a reconnecting browser sends), else 0.
- `resync_required` is emitted and the stream closes when the cursor cannot be
  resumed; the payload carries `reason`, `latest_seq`, `retained_from_seq`, and
  `missing_ranges`.
- `heartbeat` frames are emitted at most every 10 seconds while idle;
  `stream_timeout` closes a follow stream that reaches `max_seconds` (default
  120, clamped to 0..900); `stream_caught_up` is emitted instead when
  `drain=true`, which closes as soon as the client is current.

### `GET /api/harness/sessions/{session_id}/state`

The authoritative reload/resync snapshot: the session row (without its inline
event list), the root task record, budget, verification spec, latest
hash-verified checkpoint, milestones, transitions, the event-stream summary, and
the approval state.

`status` comes from the durable task state machine, overlaid with
`waiting_approval` while a live approval slot is waiting. A recorded completion
event alone never makes a task "completed": the runtime commit is the authority.

`approvals` reconciles intent with outcome using the durable `approval/request`
and `approval/decision` records:

| Bucket | Meaning | Actionable |
|---|---|---|
| `pending` | Request event with no decision **and** a live in-memory slot | Yes |
| `expired` | Request event with no decision and no live slot (restart, abort) | No — re-run the turn |
| `recent_decisions` | Last 10 durable decisions | No |

An approval id that already has a durable decision is rejected with `409` even
after the in-memory tombstone has expired, so a stale approval can never be
accepted after a restart.

## How the UI uses it

The vanilla-JS UI keeps three pieces of state: the merged event cache keyed by
`seq`, the applied cursor, and the last snapshot.

- **Reload / session select** — `selectHarnessSession` fetches `/state`, applies
  the authoritative status badge and approval buckets, then pages `/events` from
  `cursor = 0` and renders the merged log. Rendering is sequence-keyed
  (`harnessMergeEvents`), so a replayed record renders at most once.
- **While a turn streams** — the POST stream owns the transcript
  (`setHarnessRunActive(true)`). The follow stream is deliberately closed so two
  renderers never fight over the same DOM.
- **After a turn** — `setHarnessRunActive(false)` re-renders from the durable log
  and reopens the follow stream. A dropped live frame therefore cannot leave a
  missing or duplicated completion on screen.
- **Gaps** — every page and every follow-stream frame is checked with
  `harnessEventGap(page, cursor)`. A `resync_required` frame, a pruned history, a
  sequence jump, or a cursor ahead of the store all route to
  `harnessResyncFromSnapshot`, which replaces the transcript instead of
  continuing from a partial log.
- **Elsewhere** — returning to the tab (`visibilitychange`) or focusing the
  window re-anchors to the snapshot, and an expired approval is rendered without
  action buttons.

## Retention and resource limits

- A log that grows past 8 MiB is pruned to the newest 4000 events, atomically
  (temporary file + `os.replace`). Pruning is what makes a cursor stale, and a
  stale cursor is reported, not silently patched.
- Torn or non-JSON lines are skipped and counted (`skipped_records`) in
  `reconstruct`; they are never repaired into invented events.
- Reads are bounded (`limit` ≤ 1000); the follow stream polls at
  `poll_interval` (0.05–5 s, default 0.5 s).

## Limits — do not overclaim

- **Single writer.** The store assumes one application process owns a session at
  a time, matching the runtime's worker-lease model. Concurrent appends from
  separate processes are not supported and are not claimed to be safe.
- **Not encrypted.** Events can contain prompts, tool arguments, and tool output.
  They inherit the harness store's retention/privacy posture
  ([`privacy.md`](privacy.md)); they are not covered by the SQLite export.
- **Not exactly-once external effects.** Dedupe keys prevent duplicate *records*;
  they are not a distributed transaction and say nothing about the side effect a
  tool performed. The
  [bypass inventory](m5-2-0-bypass-inventory.md) still governs what is inside the
  execution boundary.
- **Not a full audit trail.** `tool_audit_events` remains the privacy-preserving
  security audit table; this stream is the operator-facing transcript.
- **Retention window.** A dedupe guarantee only covers records still retained.

## Verification

`tests/test_task_events.py` (16 tests) pins the contract:

| Test | Covers |
|---|---|
| `test_event_log_cursor_reads_are_monotonic_and_survive_reopen` | Cursor paging, `has_more`, seq continuity across a new process |
| `test_event_log_dedupe_key_is_idempotent_across_restart` | At-most-once terminal records, distinct outcomes preserved |
| `test_event_log_pruning_forces_resync_for_stale_cursor_and_truncates_fresh_reader` | `events_pruned`, `truncated`, `cursor_ahead`, `missing_ranges` |
| `test_event_log_detects_sequence_gaps_and_skips_torn_records` | Torn-line tolerance, `sequence_gap` |
| `test_event_log_rejects_unsafe_session_ids` | Path-traversal refusal |
| `test_reconstruct_reports_undecided_and_decided_approvals` | Event-derived approval and turn state |
| `test_state_snapshot_reconstructs_task_status_for_reload` | `/state` and `/events` over HTTP, 404/401 boundaries |
| `test_state_snapshot_tracks_runtime_task_transitions_not_rendered_text` | Status follows the task state machine, not the transcript |
| `test_pending_approvals_are_reconstructed_from_the_durable_log` | Pending vs expired after a restart |
| `test_stale_approval_submission_is_rejected_after_in_memory_state_is_gone` | Durable `409` replay rejection |
| `test_terminal_records_are_never_duplicated_in_the_durable_log` | No duplicate completion/verification records |
| `test_event_stream_replays_from_cursor_and_last_event_id` | SSE ids, `Last-Event-ID` resume, `resync_required` |
| `test_pruned_history_triggers_snapshot_resync_over_http` | Prune → resync over both endpoints |
| `test_frontend_gap_predicate_requires_snapshot_resync_and_merges_without_duplicates` | Client gap predicate and sequence-keyed merge (Node) |
| `test_frontend_reload_and_resync_paths_use_the_durable_endpoints` | UI reload/resync wiring |
| `test_standalone_bundle_inlines_the_task_event_store` | `studio.py` carries the store |

The full suite state in this checkout is recorded in
[`README.md`](../README.md#tests-and-measured-results).
