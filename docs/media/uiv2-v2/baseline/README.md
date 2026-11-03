# UIV2-01 baseline screenshots

Before-screenshots of the M5 2.0 Agent Studio UI at pinned commit
`9a584a36f1fbe29ba467562dd12a705626769f00`, captured 2026-11-03 from the real app
(`scripts/demo_tour.py --serve` on a disposable `git archive` copy — see
[#108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108) and
[#117](https://github.com/pankajtiwarics27/m5-agent-studio/issues/117)) through
`scripts/capture_screenshots.mjs` plus a small overlay driver for the MCP hub, with
HeadlessChrome 153 (`@sparticuz/chromium`) and `puppeteer-core`.

Layout:

- `<viewport>-<theme>/` — one directory per capture set (1640x900, 1440x900, 1280x800,
  390x844; light and dark). Contains `start.png`, `harness.png`, `hall.png` (Council),
  `council.png` (Pipeline), `debate.png` (Compare), `chat.png`, and an `index.json`
  with full sha256, byte size, viewport and capture time per shot.
- `overlay/<viewport>-<theme>/mcp-hub.png` — the MCP Tools Hub dialog opened through
  the real header control (`select#integrationsQuickMenu` set to `mcp`), with its own
  `index.json`.

Provenance, commands and the full verification table:
[`docs/uiv2-v2-baseline-evidence.md`](../../../uiv2-v2-baseline-evidence.md).
