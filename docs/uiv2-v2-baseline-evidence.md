# M5 2.0 UI/UX V2 — visual-fidelity baseline evidence (UIV2-01)

This page is the evidence for story **UIV2-01** ("Pin repository HEAD and capture
complete baseline") of [`product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json`](../product/M5_2_0_UIUX_V2_Visual_Fidelity_Roadmap.json).
UIV2-01 makes **no code edits**: it pins the source commit, enumerates the shipped
interface and its known defects, and captures before-screenshots of all six tabs and
the MCP hub from the actually running app. Every screenshot below was rendered by a
real headless Chromium against a real demo server; nothing here is a mock or a
description.

## Source pin

| Fact | Value |
|---|---|
| Pinned commit | `9a584a36f1fbe29ba467562dd12a705626769f00` |
| Session branch | `arena/74aace20-m5-agent-studio` (worktree clean before this story) |
| GitHub `main` at capture time | `9a584a36f1fb` — 0 commits ahead of the pin (`scripts/uiux_coordination.py --github`) |
| `BASELINE_COMMIT` | `7904892d759bb66b817bb17b6f4f377874bd6e25` (untouched, per AGENTS.md §9) |
| `static/index.html` sha256 | `05ea9a0c451fa1d35b5e9d7b368b7391963966921416ff578d580f90b2c5d9cd` (identical to the hash pinned in `product/M5_2_0_UIUX_Control_Inventory.json`, so the existing inventories still describe this frontend) |
| `app.py` sha256 | `0e6ecd89d84b7779f91513d8077934b08c8e90bccda919a6fac49ff6e44e2962` (identical to the hash pinned in `product/M5_2_0_UIUX_Route_Map.json`) |
| Prior UI/UX ledger | [`product/M5_2_0_UIUX_Autonomous_Roadmap.json`](../product/M5_2_0_UIUX_Autonomous_Roadmap.json): 40/40 stories `done` — recorded as history, **not** as visual acceptance, per the V2 roadmap baseline note |

## Isolation protocol (#108, #117)

The demo can read a checkout's harness store while those issues are open, and a test
run can write one, so nothing was run against the real checkout:

- Screenshots: `git archive 9a584a3… | tar -x -C /tmp/uiv2-iso` (no `.git`, no
  `.harnesses`), demo served from that copy with its throwaway HOME/database/token and
  patched harness store (`scripts/demo_tour.py` sets `studio.HARNESS_STORE_DIR` to the
  throwaway root).
- Test suite: a fresh full `git clone` at the pinned commit (`/tmp/uiv2-clean`, no
  `.harnesses` before the run; the run created none).
- After all runs: the real checkout still reports a clean `git status` and contains no
  `.harnesses` directory.

## Environment

| Fact | Value |
|---|---|
| Python | 3.11.2, venv at `/tmp/uiv2-venv` (`fastapi`, `uvicorn`, `httpx`, `httpcore`, `keyring`, `pytest`, `ruff` 0.14.14, `mypy`) |
| Node | 22.22.3 |
| Browser | none on PATH; HeadlessChrome 153.0.8010.0 from npm `@sparticuz/chromium` 153.0.0, driven by `puppeteer-core` 25.16.0, `al2023` NSS libs extracted to `/tmp/chromium-libs` and supplied via `LD_LIBRARY_PATH`, launch flags `--no-zygote --single-process --disable-gpu` |
| Platform | Linux x86_64 sandbox, no display, no root |
| Demo | `python3 scripts/demo_tour.py --serve --port 8734` from the disposable copy — loopback only, throwaway bearer token, no provider, no model call, no outbound request |

## Verification results at the pinned commit

All commands below were run in the disposable clone, not in the working checkout.
Captured 2026-11-03.

| Command | Exit | Result |
|---|---|---|
| `python -m pytest -q` | 0 (after `--unshallow`) | 2612 passed, 29 skipped, in 472 s; the single initial failure (`tests/test_baseline.py`, shallow-clone ancestry) passes 3/3 after `git fetch --unshallow` |
| `python -m pytest -q tests/test_screenshot_capture_browser.py tests/test_uiux_adversarial_browser.py` with `STUDIO_SCREENSHOT_CHROME`/`STUDIO_PUPPETEER_CORE` set | 0 | 32 passed in 121 s (the browser legs that the plain run skips) |
| `ruff check .` | 0 | All checks passed |
| `python -m mypy` | 0 | no issues in 58 source files |
| `git ls-files -z -- '*.py' \| xargs -0 -r python -m compileall -q` | 0 | clean |
| `python scripts/check_frontend_syntax.py` | 0 | JavaScript syntax OK (1 inline block) |
| `python scripts/demo_tour.py --check` | 0 | transcript up to date |
| `python scripts/validate_docs.py` | 0 | 5 required pages, 192 files scanned |
| `python scripts/validate_screenshots.py` | 0 | 6 of 6 committed screenshots consistent with the manifest |
| `python scripts/validate_governance.py` | 0 | 7 required files |
| `python scripts/validate_security_signoff.py` | 0 | record consistent (state=deferred, decision=pending) |
| `python scripts/uiux_source_manifest.py --check` | 0 | 64 files, 6 tabs, 115 routes, up to date |
| `python scripts/uiux_control_inventory.py --check` | 0 | 194 markup controls across 6 panes (106 chrome, 42 script-rendered), up to date |
| `python scripts/uiux_route_map.py --check` | 0 | 66 call sites, 72 exact + 2 dynamic matches, 0 findings, 46 routes without an observed caller |
| `python scripts/uiux_authorization_trace.py --check` | 0 | 46 state-changing routes, 20 with an authority subsystem, 39 with durable state, 23 that can leave the machine |
| `python scripts/uiux_preservation_signoff.py --check` | 0 | package up to date (21 capabilities, 6 panes) |
| `python scripts/build_studio.py` then sha256 compare | 0 | `studio.py` regenerated byte-identical — no drift |
| `python scripts/generate_route_matrix.py` / `generate_subsystem_inventory.py` / `generate_roadmap_status.py` | 0 | no drift in the three generated docs |

## Capture recipe (reproducible)

```bash
# disposable source copy at the pinned commit
git archive 9a584a36f1fbe29ba467562dd12a705626769f00 | tar -x -C /tmp/uiv2-iso
cd /tmp/uiv2-iso && /tmp/uiv2-venv/bin/python scripts/demo_tour.py --serve --port 8734
# prints a JSON line with "url": "http://127.0.0.1:8734" and a throwaway "token"

# npm browser tooling, outside the repository
mkdir /tmp/shot-tools && cd /tmp/shot-tools && npm i puppeteer-core @sparticuz/chromium
node -e 'require("@sparticuz/chromium").default.executablePath().then(console.log)'   # /tmp/chromium
# decompress bin/al2023.tar.br into /tmp/chromium-libs (libnss3 and friends)

# per viewport/theme set (six tabs each)
cd /tmp/uiv2-iso
LD_LIBRARY_PATH=/tmp/chromium-libs/lib \
STUDIO_SCREENSHOT_CHROME=/tmp/chromium \
STUDIO_PUPPETEER_CORE=/tmp/shot-tools/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js \
STUDIO_SCREENSHOT_CHROME_ARGS="--no-zygote --single-process --disable-gpu" \
node scripts/capture_screenshots.mjs --manifest /tmp/uiv2-manifest.json \
  --base-url http://127.0.0.1:8734 --token "$TOKEN" \
  --emulate-theme light --viewport 1440x900 --out /tmp/uiv2-shots/1440x900-light
# repeat for 1640x900 / 1280x800 / 390x844 in light, and all four viewports in dark

# MCP hub overlay: drive the real header control (select#integrationsQuickMenu -> "mcp")
node /tmp/uiv2-shots/capture_mcp.mjs --base-url http://127.0.0.1:8734 --token "$TOKEN" \
  --viewport 1440x900 --theme light --out /tmp/uiv2-shots/overlay/1440x900-light --file mcp-hub.png
```

`scripts/capture_screenshots.mjs` verifies bearer auth (401 without, 200 with the
token), refuses unexpected navigation/dialogs/page errors, requires the clicked tab to
be the selected one with a visible pane, and writes the PNGs plus an `index.json` with
sha256/size/viewport per shot; a partial set is never written. The overlay capture
fails unless `#mcpModal` actually carries the `open` class with a visible dialog after
dispatching `change` on the real select.

## Screenshot inventory (53 images, all under `docs/media/uiv2-v2/baseline/`)

Viewports: 1640x900, 1440x900, 1280x800, 390x844; themes: light and dark; device scale
factor 1. Each directory's `index.json` carries the full sha256, byte size, viewport
and capture time. Full digests are in the `index.json` files next to the images.

| Directory | File | Bytes | sha256 (first 16) | Theme | Interaction |
|---|---|---|---|---|---|
| `1280x800-dark` | `start.png` | 135,308 | `ea68828edb6a5012…` | dark | tab switch |
| `1280x800-dark` | `harness.png` | 137,667 | `b5e12febbb90c1c3…` | dark | tab switch |
| `1280x800-dark` | `hall.png` | 141,193 | `66746f85db0dafdb…` | dark | tab switch |
| `1280x800-dark` | `council.png` | 108,060 | `292d630661e8ebb3…` | dark | tab switch |
| `1280x800-dark` | `debate.png` | 128,012 | `f8d59e04f435f21b…` | dark | tab switch |
| `1280x800-dark` | `chat.png` | 108,611 | `ada7aa7e57f5f2c5…` | dark | tab switch |
| `1280x800-light` | `start.png` | 132,824 | `7a889a8068fddec1…` | light | tab switch |
| `1280x800-light` | `harness.png` | 135,655 | `d3d72b3fa855df05…` | light | tab switch |
| `1280x800-light` | `hall.png` | 139,217 | `ba0bb1573769fd59…` | light | tab switch |
| `1280x800-light` | `council.png` | 106,121 | `1c2aae6c00dc4cbb…` | light | tab switch |
| `1280x800-light` | `debate.png` | 125,595 | `1af13b95759a02d9…` | light | tab switch |
| `1280x800-light` | `chat.png` | 107,075 | `2c99349bac77c2e2…` | light | tab switch |
| `1440x900-dark` | `start.png` | 149,413 | `c02b3f1bc6a821ad…` | dark | tab switch |
| `1440x900-dark` | `harness.png` | 166,486 | `e47555193fab6a6b…` | dark | tab switch |
| `1440x900-dark` | `hall.png` | 143,801 | `43ecda690093a664…` | dark | tab switch |
| `1440x900-dark` | `council.png` | 107,027 | `df8b8ccb9ad9cb32…` | dark | tab switch |
| `1440x900-dark` | `debate.png` | 140,890 | `1c485c8cd7067513…` | dark | tab switch |
| `1440x900-dark` | `chat.png` | 110,508 | `e2d18fd9a2e1b604…` | dark | tab switch |
| `1440x900-light` | `start.png` | 147,017 | `0f51d7d05f00d12b…` | light | tab switch |
| `1440x900-light` | `harness.png` | 163,210 | `34bfe694c2675c26…` | light | tab switch |
| `1440x900-light` | `hall.png` | 141,873 | `cd85c6e2613b8679…` | light | tab switch |
| `1440x900-light` | `council.png` | 105,488 | `16c4dae2bf72a72b…` | light | tab switch |
| `1440x900-light` | `debate.png` | 139,072 | `d2a5419f4a8cf8f3…` | light | tab switch |
| `1440x900-light` | `chat.png` | 108,646 | `a391af29da0c86fe…` | light | tab switch |
| `1640x900-dark` | `start.png` | 150,926 | `04d85f8ad0280b07…` | dark | tab switch |
| `1640x900-dark` | `harness.png` | 165,388 | `3eaf05700ac86b1a…` | dark | tab switch |
| `1640x900-dark` | `hall.png` | 156,578 | `d2668b75d5e80bd4…` | dark | tab switch |
| `1640x900-dark` | `council.png` | 108,157 | `9c973f84c9965eba…` | dark | tab switch |
| `1640x900-dark` | `debate.png` | 148,540 | `55dcc6ae6aac2180…` | dark | tab switch |
| `1640x900-dark` | `chat.png` | 111,353 | `c1a210f7a5ab070f…` | dark | tab switch |
| `1640x900-light` | `start.png` | 148,678 | `d5183672fb38258c…` | light | tab switch |
| `1640x900-light` | `harness.png` | 162,350 | `eeefd93e96bc1ecd…` | light | tab switch |
| `1640x900-light` | `hall.png` | 154,418 | `c93907ed1a9b4bab…` | light | tab switch |
| `1640x900-light` | `council.png` | 106,691 | `516cffd2b2574e46…` | light | tab switch |
| `1640x900-light` | `debate.png` | 146,273 | `d1db6e80fd4c624c…` | light | tab switch |
| `1640x900-light` | `chat.png` | 110,354 | `49f5e222a324efe1…` | light | tab switch |
| `390x844-dark` | `start.png` | 57,531 | `3a205272e46255d6…` | dark | tab switch |
| `390x844-dark` | `harness.png` | 61,461 | `f25c68b9cc598bf0…` | dark | tab switch |
| `390x844-dark` | `hall.png` | 56,317 | `630af08b9af35832…` | dark | tab switch |
| `390x844-dark` | `council.png` | 52,342 | `7a52791f47d100f8…` | dark | tab switch |
| `390x844-dark` | `debate.png` | 57,041 | `4450ece9d0c62670…` | dark | tab switch |
| `390x844-dark` | `chat.png` | 53,298 | `86824c4f26e16902…` | dark | tab switch |
| `390x844-light` | `start.png` | 55,927 | `7770d75d89c403cb…` | light | tab switch |
| `390x844-light` | `harness.png` | 59,913 | `42ca23c4215cfa11…` | light | tab switch |
| `390x844-light` | `hall.png` | 54,207 | `c931eae567f24bb2…` | light | tab switch |
| `390x844-light` | `council.png` | 51,316 | `37d5cde3c8b218d5…` | light | tab switch |
| `390x844-light` | `debate.png` | 56,211 | `e75a733202652a32…` | light | tab switch |
| `390x844-light` | `chat.png` | 52,893 | `12edf13ebcda4a53…` | light | tab switch |
| `overlay/1280x800-light` | `mcp-hub.png` | 128,051 | `100c318c005c75dd…` | light | select#integrationsQuickMenu value 'mcp' -> openMcpModal() |
| `overlay/1440x900-dark` | `mcp-hub.png` | 158,504 | `350a51b1d62e19d5…` | dark | select#integrationsQuickMenu value 'mcp' -> openMcpModal() |
| `overlay/1440x900-light` | `mcp-hub.png` | 156,303 | `dbf026634c925c4b…` | light | select#integrationsQuickMenu value 'mcp' -> openMcpModal() |
| `overlay/1640x900-light` | `mcp-hub.png` | 187,105 | `75347c4cea168962…` | light | select#integrationsQuickMenu value 'mcp' -> openMcpModal() |
| `overlay/390x844-light` | `mcp-hub.png` | 66,646 | `7ff43acc471e448c…` | light | select#integrationsQuickMenu value 'mcp' -> openMcpModal() |


## Feature enumeration (what exists at the pin)

Six tabs declared by `static/index.html` (`data-tab` / `pane-*`), confirmed live by the
capture script's selected-tab check: **Start** (`start`), **Harness** (`harness`),
**Council** (`hall`), **Pipeline** (`council`), **Compare** (`debate`), **Chat**
(`chat`). Above them a shell with search, density toggle, theme toggle; a
skills/MCP strip; a "Tools & Integrations" menu opening five overlays (Providers,
**MCP Tools Hub**, Skills, Knowledge & CodeGraph, External Harness CLI Bridge) plus
SDLC and Council spawn dialogs; and a persistent right telemetry rail (memory,
oMLX residency, throughput, OrbStack containers) toggled by the Telemetry button.

Machine-readable surface, all `--check` green at this commit (see table above):

- [`product/M5_2_0_UIUX_Control_Inventory.json`](../product/M5_2_0_UIUX_Control_Inventory.json): 194 markup controls (106 chrome + 42 script-rendered) across 6 panes.
- [`product/M5_2_0_UIUX_Route_Map.json`](../product/M5_2_0_UIUX_Route_Map.json): 66 `fetch` call sites, 72 exact + 2 dynamic route matches, 46 mounted routes without an observed caller.
- [`product/M5_2_0_UIUX_Source_Manifest.json`](../product/M5_2_0_UIUX_Source_Manifest.json): 64 pinned files, 6 tabs, 115 API routes.
- [`product/M5_2_0_UIUX_Authorization_Trace.json`](../product/M5_2_0_UIUX_Authorization_Trace.json): 46 state-changing routes; 20 carry an authority subsystem, 39 durable state, 23 can leave the machine.

Targeted source findings for the V2 known defects:

- Chat persona picker (`select#chatPersona`, static/index.html line 2590) has exactly
  three hard-coded `<option>`s — Principal Engineer & UI Architect, Bug & UX Auditor,
  Code Only (Zero Fluff). There is no persona registry anywhere in the file.
- Council (hall) pane offers only `openSpawnModal`, `refreshCouncilReadiness`,
  `runHallFullRound`, `stopActiveStream`, `submitHallComposer`, `summonConclusion`,
  `summonSingleAgent`. The backend exposes `POST /api/hall/sessions/{{id}}/close` but no
  delete/clear route, so no session-clear control exists to render.
- MCP hub is one `max-width:860px` dialog (`#mcpModal`) combining presets, the
  connect form, the configured-servers list and the test-call playground.

## Known defects at the pin (roadmap baseline, verified where marked)

| Defect (from the V2 roadmap baseline) | Evidence here | Status |
|---|---|---|
| Council pinned conclusion scroll lock | user-reported; the demo runs without a provider, so no full council round was reproducible | user-reported, not reproduced |
| Council lacks session clear control | no clear/delete handler in the hall pane; no delete route under `/api/hall/` | confirmed at source |
| Compare and Chat uneven whitespace/alignment | `debate.png`, `chat.png` in every viewport set | confirmed in screenshots |
| Chat persona dropdown shows only three options | 3 hard-coded options at line 2590; visible in `390x844-light/chat.png` | confirmed at source + screenshot |
| MCP giant all-in-one dialog | `overlay/*/mcp-hub.png` — presets + form + servers + test call in one modal | confirmed in screenshots |
| Persistent telemetry rail consumes main-task width | right rail visible in all 48 tab shots; Telemetry toggle is the only control | confirmed in screenshots |
| Start checklist-heavy; Pipeline underutilizes canvas | `start.png` (six-step checklist cards), `council.png` (four-stage strip over a mostly empty pane) | confirmed in screenshots |

## Preservation statement

UIV2-01 changed no code: `git status` after this story shows only the added evidence
doc, the baseline media, and the V2 ledger copy. All existing controls, routes,
permission semantics and recovery paths are untouched, and every check in the
verification table above ran green at the pinned commit.
