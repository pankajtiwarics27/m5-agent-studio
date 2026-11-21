# AP-010 — Controlled browser worker, isolation and human takeover

**Status:** delivered and merged (PR #335, `e2bf81c`; story recorded `done` by
its completion record). It is a governed contract module plus a real-browser
bridge: **not activated**, **not wired into any production caller**, and **not a
production guarantee**. This page records what the worker
decides, what it proves with a real browser, and what it does not decide.

## Story and owner scope

AP-010 (Phase P2) delivers the controlled browser worker: a narrow interface
through which an agent may drive a browser, with per-project isolation, human
takeover, grant and budget confinement, hostile-page refusal and stop/restart
recovery. Owner approval for the story is recorded in the AP ledger's
`authorization_record`; its dependencies (AP-007, AP-009) are done and its
decision gates (U04, U06) were resolved by the owner on 2026-11-17 (issue #330).

Nothing here activates anything: `runtime_activation_authorized` stays `false`,
`SessionPolicy` **refuses to construct** with `activation_authorized=True`, and a
worker may only run in the `offline` or `pinned_local` class.

## The two halves

**`browser_worker.py` — the contract.** It decides. It opens no socket and
starts no process; the real browser is injected through a `BrowserDriver`, so
every rule is testable offline.

**`scripts/browser_worker_bridge.mjs` — the driver.** It performs exactly the
admitted work through the repository's own pinned browser tooling and reports
facts back. It never widens a policy and never retries around a refusal.

## What the contract decides

- **Isolation per project (AC2).** `allocate_profile` mints a fresh profile
  under `~/M5_AP_Workspace/browser/profiles/<session>`; a dedicated profile per
  session, never shared. `assert_not_personal_profile` refuses any path that
  looks like a real browser user-data directory (Chrome, Chromium, Edge, Brave,
  Firefox, any OS variant) and any path in the real home outside the AP
  workspace. There is no code path that reuses a personal profile.
- **Grants and nested budgets (AC3).** `SessionPolicy` carries the granted
  origins. Every `BrowserAction` is admitted **before** the driver sees it:
  navigation is capped, downloads are off unless granted, a download must
  declare its size, and the session's nested byte budget is enforced in
  addition to the per-download cap. A driver that writes more than the cap is a
  violation, not a correction.
- **Hostile pages gain nothing (AC5).** Private, link-local and metadata
  addresses are refused outright — a grant that names one is not honoured.
  Loopback is admitted only for an explicitly granted local origin (the Studio
  and the test fixtures). `file:`, `javascript:` and `chrome:` schemes are
  refused. Popups are **refused by default**; when granted they must stay on
  the opener's origin and off private space. In the real browser every popup is
  recorded and then closed, and the report lists the surfaces that actually
  survived, so a closed popup cannot be mistaken for a live window.
- **Takeover (AC4).** `take_over(human)` suspends all agent actions;
  `resume(human)` requires the *same* identified human; a second takeover, a
  resume by someone else, and a resume without a takeover all raise. Actions
  attempted during a takeover are denied with the holding human named.
- **Stop and restart (AC6).** `stop()` fences the session, closes the driver
  (a driver failure still fences), destroys the owned profile directory and is
  idempotent. A fenced session refuses to dispatch at all.
  `recover_session` requires a *fresh* profile and a *new* policy: previous
  navigation and download budgets are **not** restored, so recovery restores
  continuity, never authority. Journaled fields matching token/secret/password/
  cookie/credential patterns are redacted on write *and* on serialisation.
- **Bake-off (AC1).** `BakeOffHarness` runs identical `TaskScript`s through a
  baseline and a candidate driver under the *same* policy, then compares
  policy-visible outcomes only — executed steps, refusals, failures and bytes.
  A candidate cannot win by doing more than the policy allows, because refusals
  are counted on both sides.

## Why the profile guard is name-based

`assert_not_personal_profile` refuses personal application/config roots
(`~/.config`, `~/.local/share`, `~/Library/Application Support`, `~/AppData`,
`~/.mozilla`, `~/snap`, `~/.var/app`) and every known browser user-data marker.
It deliberately does **not** refuse "anything under `$HOME`", which was the first
implementation. GitHub Actions runs with `HOME=/home/runner` and places pytest's
temporary directory under `/home/runner/work/_temp`, so the broader rule refused
the runner's own scratch space and failed every real-browser test in CI while
passing locally, where the temp directory sits outside `HOME`. Naming the
personal roots refuses what matters without breaking legitimate scratch space.
`tests/test_browser_worker.py` pins both halves: the CI condition is allowed,
and a personal directory is still refused.

## Real-browser evidence

The evidence is produced against the repository's own pinned tooling, the same
that `.github/workflows/ci.yml` already installs: `@sparticuz/chromium` 153.0.0
(the Chromium binary ships **inside** the npm tarball, so no external CDN is
needed) and `puppeteer-core` 25.16.0.

`tests/test_browser_worker_real.py` launches that Chromium through the bridge and
proves, on a real local fixture server:

- navigation, click, typing and a real screenshot (a non-trivial PNG, not a
  blank frame) — AC1/AC3;
- the run profile is isolated and is removed afterwards — AC2/AC6;
- a hostile page's popup is recorded, refused and closed, and the surviving
  surface list shows it never became a window — AC5;
- a metadata/private target is refused **before** dispatch — AC5;
- takeover suspends the agent and stop fences it — AC4;
- two runs of the same task produce identical policy-visible outcomes — AC1.

The CI browser job runs these alongside the existing screenshot and adversarial
regressions, with `M5_REQUIRE_BROWSER_TESTS=1` so they cannot silently skip.

## What this story does not decide

- **No activation and no production caller.** No Studio, Harness or UI path
  imports `browser_worker`; the module is not wired into dispatch.
- **No real accounts, credentials or downloads.** The bridge deliberately
  refuses to transfer download bytes: AP-010 records download *decisions*
  against the budget and moves nothing. No personal profile, password store or
  authenticated session is touched.
- **No human qualification.** Takeover is a contract and a tested state machine;
  no human-factors study of the takeover UI was run, and none is claimed.
- **No browser bake-off winner.** AC1 is demonstrated by the deterministic
  harness and by two identical runs of the real driver; a scored comparison of
  Browser Use versus direct Playwright on production tasks is **not** claimed,
  because that needs the hosted/credentialed environments U03 still excludes.
- **No egress beyond the local fixture.** The bridge reaches a local fixture
  server; the public allowlist boundary remains AP-009's.

## Verification

```sh
# offline contract (no browser needed)
python -m pytest -q tests/test_browser_worker.py

# real browser, using the pinned, CDN-free tooling
npm ci --prefix tests/browser --ignore-scripts
export STUDIO_SCREENSHOT_CHROME=... STUDIO_PUPPETEER_CORE=... LD_LIBRARY_PATH=...
python -m pytest -q tests/test_browser_worker_real.py
```

No qualification of any installed runtime has been run and no claim is made
about the M5 host; ADR-0040–0045 remain `not-reviewed`.
