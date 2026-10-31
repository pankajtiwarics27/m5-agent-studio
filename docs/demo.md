# Demo and onboarding media (AS-045)

**Story:** AS-045 — *Demonstration and onboarding media*.
**Acceptance criterion:** Quickstart, screenshots and reproducible demo.

The [quickstart](quickstart.md) explains how to install and run the studio. This
page adds the two things a reader usually wants next: a **reproducible demo** they
can run in one command without a provider, and the **screenshot set** that shows
what the six panes look like.

## 1. Run the demo

```bash
python3 scripts/demo_tour.py
```

That is the whole setup. The tour starts the real application in-process against a
throwaway database with a generated bearer token, walks ten steps, and prints a
narrated transcript. It configures no provider, reads no key, calls no model, and
makes no network request.

The committed transcript — the same output, byte for byte — is
[`media/demo-transcript.md`](media/demo-transcript.md):

```bash
python3 scripts/demo_tour.py --write    # regenerate it
python3 scripts/demo_tour.py --check    # exit 1 if it drifted; CI runs this
python3 scripts/demo_tour.py --json     # machine-readable observations
```

### What the tour walks through

| # | Pane | What it shows |
|---|---|---|
| 1 | Start | The preflight checklist: what is ready, what still needs you. Nothing here contacts a model. |
| 2 | Start | Provider presets are descriptions, not connections — the configured list stays empty. |
| 3 | Harness | Creating a combo: a name for a set of targets usable as one model id. |
| 4 | Harness | Adding a workspace note to the local open-context store. |
| 5 | Harness | Full-text search over that store, offline. |
| 6 | Harness | Lint: orphans, thin pages, dangling links. |
| 7 | Hall | MCP presets versus connected servers — a connection is an explicit action. |
| 8 | Hall | Opening a council session, which records a local transcript with no model call. |
| 9 | Start | The redacted history export, which omits credentials and audit trails. |
| 10 | Harness | Cleanup, so repeated runs start from identical state. |

The tour **refuses to run** outside an isolated environment. It raises
`DemoEnvironmentError` if `STUDIO_EXECUTION_MODE` is not `disabled`, if host
execution is enabled, if authentication is disabled, if `STUDIO_DB_PATH` points
anywhere but its own throwaway database, or if a token is configured instead of
generated. A demo that can mutate real data is not a demo.

### Determinism

The transcript is an artifact: it is committed, and `--check` fails CI when it
drifts. Two runs produce identical bytes, which required two decisions:

- **Observations are curated, not dumped.** Raw payloads carry timestamps and
  random document ids; the transcript records counts, statuses, and named facts.
- **Volatile tokens are scrubbed.** `demo_tour.scrub()` replaces generated ids,
  temporary paths, and epoch timestamps with stable placeholders.

Nothing in the transcript depends on the machine: the preflight statuses are
derived from a fresh temporary `HOME` with execution disabled, so CI and a laptop
agree.

## 2. Serve the demo for screenshots

```bash
python3 scripts/demo_tour.py --serve
```

This runs the real server on loopback with the same throwaway database, seeds it
with the same ten steps over real HTTP, then prints one JSON line:

```json
{"url": "http://127.0.0.1:PORT", "token": "…", "panes": ["start", "harness", "hall", "council", "debate", "chat"], "steps": ["preflight", "…"]}
```

It stays in the foreground until you stop it. The token is a throwaway token for a
throwaway instance — it is printed to your terminal and invalidated when the
process exits.

## 3. Screenshots

The screenshot set is defined by
[`media/screenshots/manifest.json`](media/screenshots/manifest.json): one entry per
pane, with the UI tab id, a title, the file name, and what the shot is meant to
show, plus the viewport (1440×900 at 2× device scale).

**Status in this repository: the manifest and all six images are committed.**
The set was captured at `2026-10-31T17:54:48Z` from a disposable `git archive`
copy of the pinned commit. The demo isolates its harness and workspace state in a
throwaway root ([`demo-isolation-safety.md`](demo-isolation-safety.md)); use a
disposable source checkout for capture pending independent review of
[#108](https://github.com/pankajtiwarics27/m5-agent-studio/issues/108).
`python3 scripts/validate_screenshots.py` reports `6 of 6 captured and consistent
with the manifest`: the index checks hashes, sizes and dimensions, not the origin,
authentication or pane identity. The historical capture predates the safeguards
below; [#122](https://github.com/pankajtiwarics27/m5-agent-studio/issues/122)
tracks its outstanding provenance and safety review.

```bash
# terminal 1
python3 scripts/demo_tour.py --serve

# terminal 2
npm i puppeteer-core
STUDIO_SCREENSHOT_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN

python3 scripts/validate_screenshots.py
```

### Capturing where no browser is installed

A container with no Chrome and no root access can still capture honestly, from
npm alone. `@sparticuz/chromium` ships a Chromium build (and, in its `al2023`
layer, the NSS libraries that build links against); `puppeteer-core` drives it.
Extract the binary and its libraries, then point the script at them:

```bash
npm i puppeteer-core @sparticuz/chromium          # in a scratch directory
node -e 'require("@sparticuz/chromium").executablePath().then(console.log)'   # extracts the binary
tar xf <extracted al2023.tar> -C /path/to/chromium-libs                        # libnss3, libnspr4, ...

LD_LIBRARY_PATH=/path/to/chromium-libs/lib \
STUDIO_SCREENSHOT_CHROME=/path/to/chromium \
STUDIO_PUPPETEER_CORE=/scratch/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js \
STUDIO_SCREENSHOT_CHROME_ARGS="--no-zygote --single-process --disable-gpu" \
  node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN
```

Those three extra flags are what this Chromium build needs to run without a
network namespace; the script reads them from the environment so no per-machine
launch recipe is hard-coded into the repository. The committed images were
produced exactly this way, with no hand-editing afterwards.

`capture_screenshots.mjs` accepts only a loopback demo origin, checks that the
API rejects an unauthenticated request and accepts the supplied throwaway token,
then seeds that token only in the main frame at that origin. It rejects main-frame
redirects, checks browser authentication, and verifies each tab's active, visible
pane before buffering the images. If a pane or those checks fail, it does not write
the capture set. A filesystem failure during publication can still leave partial
output; verify the result before publishing. These checks are not a cryptographic
attestation of server identity or image contents. Use only a trusted, isolated demo;
command-line tokens are visible to other local processes, so never pass a real
credential. `--dry-run` prints the capture plan without a browser.

To run the opt-in browser regressions, set `STUDIO_SCREENSHOT_CHROME` and
`STUDIO_PUPPETEER_CORE` (plus the platform-specific flags above if needed), then
run `python3 -m pytest -q tests/test_screenshot_capture_browser.py`. They use
synthetic tokens and loopback servers, plus a demo in a disposable source copy;
they skip when the browser paths are not configured.

`validate_screenshots.py` checks the set's file consistency over time:

```bash
python3 scripts/validate_screenshots.py        # CI runs this
```

- It fails if the manifest contradicts itself, if a `tab` is not a tab in
  `static/index.html`, or if a UI tab has no manifest entry.
- It fails if `index.json` and the files disagree: missing image, wrong sha256,
  wrong size, wrong dimensions for the declared viewport, a captured image the
  manifest does not plan, or a planned image with no capture.
- It reports how much of the set exists rather than assuming: `0 of 6 captured` in
  a tree without images (exit 0, because "not captured here" is a fact about the
  environment, not a defect), and `6 of 6 captured` once the set is committed.

## 4. Privacy and independence

- **No provider, no key, no model call.** The tour works with the configured
  provider list empty; every step is local SQLite or static metadata.
- **No network.** In-process steps never open a socket. `--serve` binds
  `127.0.0.1` only, with the Host allowlist and bearer token the app normally
  requires.
- **No real data.** The database, config, token, and temp files live under a fresh
  temporary directory, and the tour deletes the one row it creates.
- **Hardware and model independence.** The tour reads no hardware capability data
  and selects no model; the panes render the same on any supported machine.

## 5. Limits

- The six screenshots are real captures of a seeded demo instance, not mock-ups:
  each was written by `scripts/capture_screenshots.mjs` and checked by
  `scripts/validate_screenshots.py`. They show the demo's empty-ish state (no
  provider, no session), which is the point of the tour and not a defect.
- The tour covers the first-run and workspace path. Streamed model output needs a
  provider, so it is shown in the quickstart instead of the demo.
- `--serve` prints a bearer token so a local capture tool can attach; do not run it
  on a shared machine or leave it running.
