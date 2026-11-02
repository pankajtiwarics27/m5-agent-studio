# Compare history browser — additive follow-up

This note documents the distinct history-browsing addition to the already merged P4-06 Compare continuity work. It does not reopen or replace the roadmap record for P4-06.

## What it adds

- **Find saved comparisons** reads the existing authenticated `GET /api/history/export` only after the user requests it.
- The picker filters `tables.runs` to Compare/debate rows and shows up to the 50 most recent records. It is available before a new run, so it can reach older comparisons rather than only the run currently on screen.
- **Revisit selected** restores every saved opinion and the arbiter synthesis as separate panels. It reads the returned record; it does not run a model or write history.
- **Download full history export** downloads all tables from that same endpoint, not only one Compare run. Credential redaction is performed by the existing server exporter and remains best-effort.

## Boundaries and recovery

The server record does not retain user-defined Compare criteria. When revisiting a server record, the criteria strip shows defaults and the pane says they are not recovered. The existing current-run status bar, browser recovery, per-comparison Markdown/JSON export, and server read-back controls remain in place.

A failed or malformed history response is announced in the history status region; it does not replace the comparison already on screen. The feature adds no route, authorization mechanism, database write, model call, or durable state schema.

## Verification

- `tests/test_compare_history.py` checks the controls, existing route boundary, filtering, and separate-opinion/synthesis restoration contract.
- `scripts/compare_history_walk.mjs` is a browser walk against an isolated demo with a mocked history export. It exercises prior-run selection, full-export download, malformed/failed-fetch feedback, and verifies no model or history write is sent.
