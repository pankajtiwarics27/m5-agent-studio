# Pinned baseline and clean-checkout verification

## Source baseline

This repository's AS-001 source baseline is pinned in [`BASELINE_COMMIT`](BASELINE_COMMIT):

`7904892d759bb66b817bb17b6f4f377874bd6e25` (`Redesign Agent Studio UI and responsive design system`, 2026-10-26).

The pin is a Git commit, not a moving branch or tag. Later changes must retain it as an ancestor. Verify the pin from a checkout with the commit object available:

```bash
python3 scripts/verify_baseline.py
```

The verifier rejects malformed pins, missing commit objects, and histories that do not descend from the pinned baseline. CI fetches full Git history so this check is meaningful on shallow-runner environments.

## Clean checkout setup

The supported project declaration is Python `>=3.11` (`pyproject.toml`). The commands below create an isolated environment; no model, provider, MCP, or cloud endpoint is needed for the checks:

```bash
git clone https://github.com/pankajtiwarics27/m5-agent-studio.git
cd m5-agent-studio
git checkout --detach 7904892d759bb66b817bb17b6f4f377874bd6e25
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/verify_baseline.py
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m compileall -q app.py scripts tests
.venv/bin/python scripts/check_frontend_syntax.py
.venv/bin/pytest -q
.venv/bin/python scripts/smoke_local.py
.venv/bin/python scripts/smoke_local.py --standalone
.venv/bin/python scripts/validate_security_signoff.py
.venv/bin/python scripts/build_studio.py
.venv/bin/python scripts/generate_route_matrix.py
.venv/bin/python scripts/generate_subsystem_inventory.py
.venv/bin/python scripts/generate_roadmap_status.py --check
git diff --exit-code -- studio.py docs/route-security-matrix.md docs/subsystem-dependency-inventory.md
```

The product roadmap and its per-story status are tracked in
`product/Adaptive_Runtime_Product_Roadmap_v2.json` with a generated dashboard at
`docs/product-roadmap-status.md`; `scripts/generate_roadmap_status.py --check`
fails when the committed dashboard no longer matches the JSON. See
[`AGENTS.md`](AGENTS.md) for the full agent/contributor workflow and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the commit-message policy.

The smoke checks bind disposable servers to loopback and use temporary token/database paths. They do not call a configured LLM/provider or alter user data. The live container test remains explicitly opt-in and is skipped by the default suite.

## Verification evidence for the pinned checkout

Reproduced from a fresh clone at the pinned commit in a disposable Python 3.13.16 environment on 2026-10-26:

- `python -m pip install -e '.[dev]'` — passed. Resolved direct versions: FastAPI 0.162.0, Uvicorn 0.54.0, HTTPX 0.28.1, httpcore 1.0.9, keyring 25.7.0, pytest 8.4.2.
- `python -m compileall -q app.py scripts tests` — passed.
- `python scripts/check_frontend_syntax.py` — passed (one inline JavaScript block).
- `pytest -q` — 48 passed, 1 skipped (live container runtime is opt-in), 1 upstream Starlette/httpx deprecation warning.
- `python scripts/smoke_local.py` and `python scripts/smoke_local.py --standalone` — passed; each verified loopback startup, health, token permissions, protected API, cross-origin rejection, isolated DB path, and UI token prompt/retry behavior.
- `python scripts/build_studio.py` and `python scripts/generate_route_matrix.py`, followed by `git diff --exit-code -- studio.py docs/route-security-matrix.md` — passed; generated artifacts were unchanged.
- `python scripts/check_keyring.py` — unavailable in this headless environment: the active backend was `keyring.backends.fail.Keyring`, which correctly refused credential storage. Desktop OS keyring behavior is not claimed as verified.

After adding the AS-001 baseline verifier and its positive/negative tests, the same local environment produced `51 passed, 1 skipped` (the same opt-in container skip and deprecation warning). `python scripts/verify_baseline.py`, both smoke paths, compile, frontend syntax, generated-artifact comparison, and `git diff --check` passed in this implementation checkout.

## Reproducibility boundary

The Git baseline and repository checks are pinned and repeatable. **Python dependency resolution is not bit-for-bit locked:** `pyproject.toml` intentionally retains version ranges and there is no cross-platform lockfile. The resolved versions above are evidence for this run, not a promise that a later package-index resolution selects identical transitive versions. A universal lock format/tooling policy should be decided separately before adding a lockfile. AS-001 added no dependencies; AS-002 adds Ruff and mypy only to the development extra, not to runtime requirements. CI currently tests Python 3.11, 3.12, and 3.13 on Ubuntu; macOS, the initial target platform, still requires its own verification.
