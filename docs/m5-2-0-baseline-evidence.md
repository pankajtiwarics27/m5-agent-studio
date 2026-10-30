# M5 2.0 baseline evidence (M5-P0-05)

Evidence record for roadmap story `M5-P0-05` (Baseline tests and 24 GB
telemetry). All commands were executed on 2026-10-30 against the repository
state at commit `1e2e0bd876409a9e4eeb4b0128b403422bac7013` (plus this story's
documentation changes, which touch only Markdown and JSON files). Nothing
below is extrapolated: skipped or environment-blocked checks are labeled as
such.

## 1. Environment fingerprint

| Property | Observed value |
|---|---|
| Kernel | `Linux 6.1.158+ x86_64` (sandbox container `e2b.local`) |
| CPU / memory | 2 vCPU, `MemTotal: 4034452 kB` (~4 GB) |
| Python | 3.11.2 (project requires `>=3.11`) |
| Toolchain | ruff 0.14.14, mypy 1.20.2, pytest 9.2.0, pip-audit 2.x (installed via `pip install -e '.[dev,supply-chain]'`) |
| Node.js | present (used by `check_frontend_syntax.py`) |
| Container runtime (Docker/Podman) | none available in this environment |
| oMLX / MLX / Metal | not available (not macOS, not Apple Silicon) |
| Git history | full (shallow clone un-shallowed with `git fetch --unshallow`, which is the repository's documented prerequisite for baseline verification) |

**Hardware measurement status:** the roadmap's target is peak unified-memory
and swap behavior on a 24 GB Apple Silicon Mac. This environment is a ~4 GB
Linux x86_64 container. Per the story acceptance criteria ("Peak memory and
swap recorded or explicitly blocked", "No invented 18 GB pass criterion"), the
24 GB measurements are **explicitly blocked and remain unverified** — no
memory numbers were taken here and none are claimed. The same applies to
"one real coding task with one resident oMLX model": no oMLX runtime exists
here, so that measurement is **blocked**, not fabricated.

## 2. Command log (command → exit code → result)

| Command | Exit | Result |
|---|---|---|
| `python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev,supply-chain]'` | 0 | Environment built from `pyproject.toml`. |
| `git fetch --unshallow origin` | 0 | Fetched full history (initial clone was shallow). |
| `.venv/bin/python scripts/verify_baseline.py` | 0 | `Baseline verified: 7904892d759bb66b817bb17b6f4f377874bd6e25`. (Before un-shallowing this failed with "Pinned commit … is not present in this checkout" — a checkout-depth artifact, recorded because it happened.) |
| `.venv/bin/ruff check .` | 0 | Clean. |
| `.venv/bin/mypy` | 0 | Clean over the typed contract modules (mypy `files` list). |
| `.venv/bin/python -m compileall -q app.py scripts tests` | 0 | Clean. |
| `.venv/bin/python scripts/check_frontend_syntax.py` | 0 | `static/index.html` syntax valid (Node check). |
| `.venv/bin/python -m pytest -q` | 0 | **717 passed, 1 skipped, 1 warning in 19.13 s.** The skip is `tests/test_live_sandbox.py` — "requires explicit live container-runtime opt-in" (no container runtime here). |
| `.venv/bin/python scripts/smoke_local.py` | 0 | Disposable loopback `app.py` instance: auth/security path verified. |
| `.venv/bin/python scripts/smoke_local.py --standalone` | 0 | Same for the generated `studio.py` artifact in an isolated directory. |
| `.venv/bin/python scripts/validate_governance.py` | 0 | Governance package complete. |
| `.venv/bin/python scripts/validate_security_signoff.py` | 0 | AS-031 record/release-gate consistent. |
| `.venv/bin/python scripts/validate_docs.py` | 0 | Required docs, sections, links, placeholder scan pass. |
| `.venv/bin/python scripts/generate_route_matrix.py` + `git diff --exit-code -- docs/route-security-matrix.md docs/subsystem-dependency-inventory.md studio.py` | 0 | Generated artifacts regenerate byte-for-byte (no drift). |
| `.venv/bin/python scripts/generate_roadmap_status.py --check` | 0 | Committed dashboard matches the AS roadmap JSON. |
| `.venv/bin/python scripts/demo_tour.py --check` | 0 | Committed demo transcript has not drifted. |
| `.venv/bin/python scripts/validate_screenshots.py` | 0 | Screenshot manifest matches captured images. |
| `.venv/bin/python scripts/validate_adrs.py` | 0 | ADR set valid. |
| `.venv/bin/python -m pip_audit` | 1 | **21 known vulnerabilities in 2 packages** — see §3. |

## 3. Dependency-audit result (reported as found)

`pip-audit` over the current environment exits 1 and reports 21 advisories in
exactly two packages: `pip` 23.0.1 (14 advisories) and `setuptools` 66.1.1
(7 advisories). Both are **venv bootstrap tooling** inside `.venv`, not
project dependencies. The declared runtime dependencies (`fastapi` 0.162.0,
`uvicorn` 0.54.0, `httpx` 0.28.1, `httpcore`, `keyring` 25.7.0) and the
dev/supply-chain extras produced **no advisories**. Remediation (upgrading
the venv's pip/setuptools) is environment hygiene, tracked as an open item;
the result is recorded here without dilution because the exit code is non-zero.

## 4. Measurements deliberately not claimed

| Measurement | Status | Reason |
|---|---|---|
| Peak unified memory / swap on 24 GB Apple Silicon | blocked (unverified) | Wrong platform for the claim (see §1). |
| Resident-oMLX coding-task run (latency, memory pressure) | blocked (unverified) | No oMLX/Metal runtime available. |
| SQLite busy/latency under FTS5 load | blocked (unverified) | No target-device workload; the test suite exercises FTS5 functionally only. |
| Live container-isolation behavior | blocked (unverified) | No Docker/Podman; `tests/test_live_sandbox.py` skipped by design. |

These gaps are exactly what stories `M5-P0-04` (live isolation smoke test) and
`M5-P4-01` (measured model qualification and RAM admission) must close on real
target hardware. Per the README maintenance rule, none of them may be promoted
to a guarantee until measured.

## 5. Reproduction

From a clean checkout with full history:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,supply-chain]'
git fetch --unshallow origin   # if the clone is shallow
.venv/bin/python scripts/verify_baseline.py
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m compileall -q app.py scripts tests
.venv/bin/python scripts/check_frontend_syntax.py
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_local.py
.venv/bin/python scripts/smoke_local.py --standalone
.venv/bin/python scripts/validate_governance.py
.venv/bin/python scripts/validate_security_signoff.py
.venv/bin/python scripts/validate_docs.py
.venv/bin/python -m pip_audit
```

Related records: [`m5-2-0-integration-audit.md`](m5-2-0-integration-audit.md)
(call-path audit), [`m5-2-0-roadmap-reconciliation.md`](m5-2-0-roadmap-reconciliation.md)
(plan reconciliation), [`../BASELINE.md`](../BASELINE.md) (pinned baseline
contract).
