# Guided onboarding and preflight (AS-039)

**Story:** AS-039 — *Guided onboarding and preflight*.
**Acceptance criterion:** a new user completes a guided first task without editing code.

A new install is not usable until three things are true: the server is
protected, a model is reachable, and there is somewhere to write. Finding that
out currently means reading `README.md`, knowing which environment variable is
missing, and guessing whether a provider was saved correctly. This preflight
answers it in one read-only call and names exactly one next action.

## 1. Where it lives

| Piece | Contract |
|---|---|
| `onboarding_preflight.py` | Pure, offline planner: `build_report(PreflightInputs) -> PreflightReport`. Mypy-gated, no I/O. |
| `GET /api/onboarding/preflight` | Adapter route: collects local state, returns `{"status": "ok", "preflight": {...}}`. Bearer-authenticated like every `/api/*` route. |
| `GET /api/start/command-center` | UIV2-24: the preflight composed with what is known about the runtime, reduced to one verdict, one next action and any contradictions. Read-only; `?probe=local` asks the runtime once, and `?telemetry=` echoes which health payload the page is holding. |
| `start_command_center.py` | Pure builder for that verdict: `build_readiness(preflight, runtime, telemetry) -> dict`. No I/O. |
| `static/index.html` → **Start** tab | Opens with the task composer, the verdict line and at most one blocker; the checklist, health and workspace are disclosures under them. "Run the first task" composes the guided prompt into Chat. |
| `tests/test_onboarding_preflight.py` | 29 tests: pure contract, adapter isolation, route auth, probe behaviour, and failure modes. |

There are no new privileged operations. The guided first task reuses the
existing `/api/chat/stream` path, so onboarding does not add a second executor,
a new tool surface, or a new network egress point.

## 2. The steps

Order is fixed (`STEP_ORDER`) so the payload is deterministic and diffable.

| # | Step | `ready` when | Otherwise |
|---|---|---|---|
| 1 | `auth` | a token exists, or authentication is deliberately disabled on loopback | `blocked` — protected routes fail closed; restart Studio |
| 2 | `model-source` | a provider is **enabled** and has a credential, or the local runtime listed models when asked | `action-required` — open providers; names which aliases lack a key or are disabled |
| 3 | `model-available` | a provider lists models, or an explicit probe found local models | `action-required` — fetch models, check local models, or open Chat to pick one |
| 4 | `workspace` | `WORKSPACE_ROOT` is a directory and writable | `action-required` — fix the workspace path |
| 5 | `execution` | sandbox mode with a container CLI on `PATH` | `optional` when execution is disabled or in host mode; `action-required` when sandbox mode has no runtime |
| 6 | `first-task` | a model is selectable | `action-required` |

Statuses: `ready` · `optional` · `action-required` · `blocked`. A report is
`ready` only when every step is `ready` or `optional`; the first step that is
neither is returned as `next_step`.

## 3. Design decisions and tradeoffs

1. **Local-only, offline, secret-free.** The adapter reads SQLite configuration,
   environment-derived settings, `PATH`, and a filesystem permission bit. It does
   not resolve DNS, contact a provider, or read the keyring. Provider rows are
   reduced to `alias`, `enabled`, `has_credential`, `model_count`; the key itself
   never enters the report. Tests assert both properties (a monkeypatched
   transport that raises if called, and a serialized-payload scan for secret
   shapes).
2. **Fail explicit.** Unknown state is `action-required`, never `ready`. A
   provider saved with no models does **not** satisfy "models are available" — it
   asks for *Fetch models*. A configured local runtime URL is not treated as
   proof that a runtime is running.
3. **Combos are presets, not models.** `init_db` seeds a `hybrid-resilient`
   combo on a fresh install. Counting combo aliases as available models made the
   preflight report *ready* against targets that did not exist locally — caught by
   running the server, not by the unit tests. Combos are now reported in `notes`
   and never counted; this is the one place the report deliberately under-claims.
4. **One explicit, user-triggered probe.** Only asking the local runtime can
   reveal which local models exist, so `GET /api/onboarding/preflight?probe=local`
   performs a single `GET {OMLX_BASE}/v1/models` (2.5 s timeout, the same call
   `/v1/models` already makes) through the validated client. It runs only when the
   user presses *Check local models*, sends no credentials, and reports
   `local_probe: ran | unavailable | not-run`. The default call makes no network
   request at all.
5. **Execution is optional for the first task.** A chat turn needs a model, not a
   sandbox. Blocking onboarding on a container runtime would push new users
   toward `STUDIO_EXECUTION_MODE=host`, which is the one option we do not want
   people to choose by accident. Sandbox mode with no runtime is still surfaced.
6. **No hardware, provider, or vendor assumptions.** Nothing is keyed on a chip,
   RAM size, model name, or vendor. The sandbox check asks the configured
   `STUDIO_SANDBOX_RUNTIME` for availability and degrades explicitly.
7. **Guidance is clicks, not commands.** Every non-ready step carries an
   `action_label` and an action token (`open-providers`, `open-chat`,
   `probe-local-models`, `fix-workspace`, `run-first-task`, `restart-server`,
   `review-execution`) that
   the UI maps to an existing control. No step instructs the user to edit a file.
8. **Completion is not persisted server-side.** The browser records that the
   guided prompt was launched (`localStorage`). There is no new user-profile,
   consent, or telemetry record, and nothing is reported anywhere.

## 3a. The command center: a port that answers is not a runtime that works (UIV2-24)

The preflight answers *what should a first-run reader click*. It does not answer *is the
local runtime able to run a model*: a process can hold the loopback port, answer
`/v1/models`, and report zero models, and every step above would still list a model
source. Start used to lead with the six rows, so a reader met the checklist before the
composer, and a runtime that answered with nothing still looked like a green card.

`GET /api/start/command-center` composes the same preflight with two separate
observations and returns:

| Field | Meaning |
|---|---|
| `verdict` | `ready` · `needs-you` · `blocked` · `contradiction` |
| `headline`, `detail` | one sentence each, printed verbatim by the pane |
| `counts` | `ready` / `total` / `optional` / `unchecked`, from the preflight |
| `basis` | `measured` only when the runtime was asked and answered; `derived` otherwise |
| `runtime` | `configured`, `url`, `port_answered`, `model_count`, `probe` — the socket and the model list are kept apart |
| `checks` | five rows: model source, model available, workspace, execution, local runtime |
| `contradictions` | where two honest observations disagree, each with `title`, `detail`, `evidence` and the action that settles it |
| `next` | the single action that advances the reader; `null` when nothing is in the way |

The contradictions it can print, all reachable in `tests/test_start_command_center.py`:

| id | When |
|---|---|
| `port-without-models` | the runtime answered and reported zero models — a turn would fail even though a port check passes |
| `runtime-models-without-usable-selection` | the runtime listed models the readiness check could not use |
| `checks-passed-without-runtime` | every local check passed and the runtime did not answer when asked |
| `demo-health-with-live-runtime` | the health card is the simulated payload while a separate probe found the runtime answering |

A verdict of `contradiction` is never `ready`, and the pane prints the evidence
(`port_answered=True · model_count=0`) rather than smoothing it into a green word. Without
`?probe=local` the runtime row says *not asked yet* and the basis says `derived`, so
"ready" is never an inference from a socket.

## 4. What this does not claim

- It does not prove a model **works**; that is what the guided first task is for,
  and it is a single call the user triggers. A runtime that answers the model
  listing can still fail the completion itself.
- It does not validate provider credentials, network reachability, or model
  quality. Those remain deployment checks.
- It does not replace the container runtime or the live sandbox test, and it does
  not evaluate the security boundary (see `security-review-package.md`).
- The onboarding UI is exercised by the frontend syntax check, the loopback smoke
  tests, and the route tests; a full real-browser walkthrough remains part of the
  final end-to-end verification.

## 5. Verification

```bash
python -m pytest -q tests/test_onboarding_preflight.py     # 29 passed
python -m pytest -q tests/test_start_command_center.py     # 27 passed (UIV2-24)
python -m pytest -q tests/test_start_readiness.py  # the 22 pane-contract tests (P2-01)
python -m pytest -q tests/test_start_health.py  # the 3 bases and 2 routes the health card reads
python -m pytest -q tests/test_start_recent.py  # recent work, the composer and its handoff
python scripts/uiux_start_command_center_inventory.py --check  # the story's own artifact is current
node scripts/start_walk.mjs --base-url URL --token TOKEN  # 31/31 checks against a running app
node scripts/start_command_center_walk.mjs --out DIR  # 31/31 per run, 56 captures
python scripts/check_frontend_syntax.py                    # inline JS parses
python scripts/smoke_local.py                              # loopback startup + auth
python scripts/build_studio.py                             # regenerate studio.py
```

Full suite in this workspace: `495 passed, 1 skipped` (the opt-in live container
test), plus Ruff, mypy (36 contract modules), compileall, both smoke checks, and
byte-identical regeneration of `studio.py`, the route matrix, the subsystem
inventory, and the roadmap dashboard.
