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

**Status in this repository: the manifest is committed, the images are not.**
Capturing needs a browser binary, and the environment where this story was
implemented has neither a browser nor the system libraries or root access to
install one. Rather than check in placeholder images or hand-drawn mock-ups, the
manifest ships alone and the images are captured by a maintainer who has a browser:

```bash
# terminal 1
python3 scripts/demo_tour.py --serve

# terminal 2
npm i puppeteer-core
STUDIO_SCREENSHOT_CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  node scripts/capture_screenshots.mjs --base-url http://127.0.0.1:PORT --token TOKEN

python3 scripts/validate_screenshots.py
```

`capture_screenshots.mjs` sets the token in `sessionStorage`, switches to each
pane through the UI's own tab control, and writes the PNGs plus
`media/screenshots/index.json` (sha256, size, viewport, capture time for each
image). It fails closed: if no browser or no `puppeteer-core` is available, or any
pane fails, it writes nothing and exits non-zero, so a partial or fabricated set
cannot be mistaken for the real thing. `--dry-run` prints the capture plan without
a browser.

`validate_screenshots.py` is what keeps the set honest over time:

```bash
python3 scripts/validate_screenshots.py        # CI runs this
```

- It fails if the manifest contradicts itself, if a `tab` is not a tab in
  `static/index.html`, or if a UI tab has no manifest entry.
- It fails if `index.json` and the files disagree: missing image, wrong sha256,
  wrong size, wrong dimensions for the declared viewport, a captured image the
  manifest does not plan, or a planned image with no capture.
- It does **not** fail when nothing has been captured yet. It reports
  `0 of 6 captured` and exits 0, because "not captured here" is a fact about the
  environment, not a defect in the repository.

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

- Screenshots are not captured in this repository yet. The pipeline is real and
  testable, but the images require a browser this environment does not have.
- The tour covers the first-run and workspace path. Streamed model output needs a
  provider, so it is shown in the quickstart instead of the demo.
- `--serve` prints a bearer token so a local capture tool can attach; do not run it
  on a shared machine or leave it running.
