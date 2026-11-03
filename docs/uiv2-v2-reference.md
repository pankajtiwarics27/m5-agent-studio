# UIV2-03 — encoded reference: three-region Harness + per-tab layouts

This page encodes the **user-approved conceptual three-region Harness reference**
(approved by the user before this roadmap was written; see
[`product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json`](../product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json)
`design_north_star` and `reference_assets`) as a reproducible, annotated wireframe
plus rendered screenshot fixtures. It is the visual contract for phases P1–P6.

> **Approval gate.** Structural implementation of this reference requires explicit
> user approval of *this encoded artifact* — the roadmap's rule is "no inferred
> approval". Until the user approves this page and its fixtures, no frontend file
> is edited. UIV2-03 stays `blocked` in the ledger until that approval is recorded.

## The three-region desktop structure (≥ 1440 px)

```
┌────────────────────────────────────────────────────────────────────┐
│ header ≤ 64 px: logo · Telemetry ▾ (popover) · search · theme       │
├──────────┬──────────────┬─────────────────────────────┬─────────────┤
│ global   │ session /    │ main task area (≥ 640 px):  │ context     │
│ nav      │ workspace    │ Plan → Execute → Verify     │ inspector   │
│ 192–224px│ list         │ + activity timeline         │ 272–320 px  │
│          │              │ + composer (one dominant    │             │
│          │              │ action per state)           │             │
└──────────┴──────────────┴─────────────────────────────┴─────────────┘
```

- **Global telemetry is a header popover, not a permanent right rail** — this is
  the direct fix for the verified defect "persistent telemetry rail consumes
  main-task width" (see [uiv2-v2-baseline-evidence.md](uiv2-v2-baseline-evidence.md)).
- Breakpoints: `1440+` four regions; `1280–1439` inspector becomes a drawer;
  `800–1279` nav collapses to icons, session list hidden; `< 800` single column
  with a top tab row (breakpoints 390 / 800 / 1280 / 1440 per the north star).
- Header max height 64 px; nav 192–224 px; inspector 272–320 px; main ≥ 640 px.

## Design tokens (from the roadmap's north star, verbatim)

| Token | Light | Dark |
|---|---|---|
| canvas | `#F8F9FB` | `#111318` |
| surface | `#FFFFFF` | `#1A1D24` |
| secondary | `#F1F3F5` | `#242832` |
| text | `#1B2430` | `#F3F5F7` |
| muted | `#596579` | `#AAB4C3` |
| border | `#DDE2E8` | `#343B48` |
| accent | `#4F5FE8` | `#93A0FF` |

Principles: task content wins over diagnostics; one dominant action per state;
progressive disclosure for expert controls; hierarchy by typography and whitespace,
not colored cards; color denotes meaning; readable long-form content and predictable
scrolling; no implied Cowork computer-control capabilities; light and dark verified
separately.

## Per-tab main-area intent

| Tab | Reference intent | Current-state defect this fixes |
|---|---|---|
| Harness | three-region cockpit: session list, Plan→Execute→Verify, timeline, composer, approvals in inspector | dense single-column form + marketing cards |
| Start | concise command center: one next action + one collapsed status line | checklist-heavy six-card stack |
| Council | session list + transcript + pinned conclusion that scrolls with it + **Clear session** control | pinned-conclusion scroll lock (user-reported); no clear control (verified absent) |
| Pipeline | stage-focused workspace with artifacts | underutilized canvas |
| Compare | question-first balanced layout; results side by side; evidence in inspector | uneven whitespace/alignment |
| Chat | focused single conversation; persona picker lists **every registered persona** | picker shows only 3 hard-coded options (verified absent registry) |
| MCP hub | searchable catalog + connected-server dashboard; guided connect wizard | giant all-in-one dialog (verified in overlay screenshots) |

## Reproducible annotated wireframe and fixtures

- Wireframe source (self-contained, no network): `docs/media/uiv2-v2/reference/wireframe.html`.
  Query params: `?pane=harness|start|hall|council|debate|chat` and `&theme=light|dark`.
- Fixture capture: `scripts/uiux_v2_reference_capture.mjs` with
  `STUDIO_SCREENSHOT_CHROME` + `STUDIO_PUPPETEER_CORE` (+ optional
  `STUDIO_SCREENSHOT_CHROME_ARGS="--no-zygote --single-process --disable-gpu"`),
  e.g.:

  ```bash
  STUDIO_SCREENSHOT_CHROME=/tmp/chromium \
  STUDIO_PUPPETEER_CORE=/path/to/puppeteer-core/lib/puppeteer/puppeteer-core.js \
    node scripts/uiux_v2_reference_capture.mjs --out docs/media/uiv2-v2/reference
  ```

  The script refuses to write a partial set and fails if a pane does not render or
  the requested theme is not applied.

### Fixture inventory (36 images, six panes × six viewport/theme sets)

| Set | File | Bytes | sha256 (first 16) | Theme |
|---|---|---|---|---|
| `1280x800-light` | `harness.png` | 48,565 | `abd08de75f4b8ee3…` | light |
| `1280x800-light` | `start.png` | 38,655 | `8be9bd92a0aacef4…` | light |
| `1280x800-light` | `hall.png` | 41,201 | `12e4b7dbbe04d93d…` | light |
| `1280x800-light` | `council.png` | 36,981 | `8705eb6087ccd656…` | light |
| `1280x800-light` | `debate.png` | 39,488 | `70b0fd3d7b5c13d2…` | light |
| `1280x800-light` | `chat.png` | 36,828 | `72ee707654fbdda8…` | light |
| `1440x900-dark` | `harness.png` | 67,858 | `bcaa303baaa167fd…` | dark |
| `1440x900-dark` | `start.png` | 57,422 | `2d45dd5cd9d86ef4…` | dark |
| `1440x900-dark` | `hall.png` | 59,006 | `aa82cee3cd923c14…` | dark |
| `1440x900-dark` | `council.png` | 55,005 | `0663ac699b859293…` | dark |
| `1440x900-dark` | `debate.png` | 56,911 | `3438fe3d0eac41f6…` | dark |
| `1440x900-dark` | `chat.png` | 54,397 | `a66975234c44b16a…` | dark |
| `1440x900-light` | `harness.png` | 67,296 | `e0c8d08f5e6a1dbc…` | light |
| `1440x900-light` | `start.png` | 57,557 | `0c4e4908140d5d96…` | light |
| `1440x900-light` | `hall.png` | 59,096 | `a6febd6892f02e41…` | light |
| `1440x900-light` | `council.png` | 54,715 | `411565e9858048bb…` | light |
| `1440x900-light` | `debate.png` | 57,107 | `ed170f3e4e59dcc9…` | light |
| `1440x900-light` | `chat.png` | 54,311 | `addf45d1de45524b…` | light |
| `1640x900-dark` | `harness.png` | 68,583 | `e0fe0cf795899c57…` | dark |
| `1640x900-dark` | `start.png` | 56,960 | `a40efbac244b890a…` | dark |
| `1640x900-dark` | `hall.png` | 59,684 | `fe66a54da0034863…` | dark |
| `1640x900-dark` | `council.png` | 55,443 | `82f7ddcc53d7673b…` | dark |
| `1640x900-dark` | `debate.png` | 57,441 | `4ae29cdf1689bbd9…` | dark |
| `1640x900-dark` | `chat.png` | 55,015 | `950fa01108a749b7…` | dark |
| `1640x900-light` | `harness.png` | 67,962 | `416265a18206020c…` | light |
| `1640x900-light` | `start.png` | 57,143 | `2e5e7fa768638286…` | light |
| `1640x900-light` | `hall.png` | 59,749 | `e7fecfd97a55a8ad…` | light |
| `1640x900-light` | `council.png` | 55,310 | `4ab38d726ef3919f…` | light |
| `1640x900-light` | `debate.png` | 57,636 | `f7597f37b9b98694…` | light |
| `1640x900-light` | `chat.png` | 54,919 | `27bffcc4ba591e15…` | light |
| `390x844-light` | `harness.png` | 30,644 | `083f2957170028f9…` | light |
| `390x844-light` | `start.png` | 21,505 | `e4f49e40b61d3857…` | light |
| `390x844-light` | `hall.png` | 23,159 | `99d91a8320080b40…` | light |
| `390x844-light` | `council.png` | 18,394 | `903543830eefa357…` | light |
| `390x844-light` | `debate.png` | 21,002 | `a7f4d6ebc1c24581…` | light |
| `390x844-light` | `chat.png` | 18,428 | `8624d69b65f4ac0a…` | light |

Full sha256 digests are in each set's `index.json`.

## What this reference deliberately does not claim

- It is a structure/typography/spacing contract, not a pixel clone of any product.
- It does not add backend capability: the Council clear-session control, for
  example, needs a runtime story (delete route) before the UI can render it —
  recorded in the ledger, never fabricated here.
- The seven user screenshots from 2026-11-03 are current-state evidence only; the
  approved target is this encoded reference, pending the user's explicit approval.
