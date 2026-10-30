# M5 2.0 isolation smoke-test evidence (M5-P0-04)

Evidence record for roadmap story `M5-P0-04` (Live isolation smoke test),
executed 2026-10-30 on branch session `arena/13166201-m5-agent-studio` against
`main` at `7b2dc2c` (post `M5-P0-03`). The reproducible tool for this story is
[`scripts/isolation_smoke.py`](../scripts/isolation_smoke.py); its regression
tests are `tests/test_isolation_smoke.py` (3 tests).

## 1. Environment fingerprint (observed)

| Property | Observed value |
|---|---|
| Platform | `Linux-6.1.158+-x86_64-with-glibc2.36` |
| CPU / memory | 2 vCPU, `MemTotal: 4034452 kB` (~4 GB) |
| Python | 3.11.2 |
| Container runtime | **none** — `docker`, `podman`, `nerdctl` not on `PATH`; no `/var/run/docker.sock` |
| Target platform (story intent) | macOS / Apple Silicon — **not** this environment |

## 2. Story tasks → observed results

| Task | Result | Evidence |
|---|---|---|
| Identify actual Docker/Podman availability | **None available** | `shutil.which` + `docker info` probe in `scripts/isolation_smoke.py` check 1 → `runtime_available: no ('docker' not on PATH)` |
| Test synthetic outside-workspace file access and direct network attempt | **Container-level: attempted, environment-blocked.** Policy-level denials observed live** | Live attempt: `STUDIO_SANDBOX_INTEGRATION=1 STUDIO_EXECUTION_MODE=sandbox pytest -q tests/test_live_sandbox.py` → `RuntimeError: Sandbox runtime 'docker' is not installed or not on PATH` (fail closed, **no silent host fallback**). Observed policy-level denials: `sandbox_boundary.confined_path` rejects traversal, absolute outside-workspace paths, NUL bytes, and symlink escapes (smoke check 4); the constructed sandbox invocation enforces `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pull=never`, resource limits, and a single `/workspace` bind mount (smoke check 2) |
| Test skill execution path and default-off host bypass | **Observed, all denials** | Smoke check 3: default-off skill-code denial (`PermissionError`), execution-disabled denial, host-mode-without-opt-in denial, sandbox-mode fail-closed with zero host spawns (simulated missing runtime) — consistent with the 9 `M5-P0-03` regression tests |
| Record environment fingerprint and unsupported guarantees | **Recorded** (this document + smoke output) | §1 and §3 |

Full smoke output (16/16 observable checks `[PASS]`, live probes skipped):
`.venv/bin/python scripts/isolation_smoke.py` → exit 0, summary
`checks_failed: 0`, `isolation_status: unqualified`,
`dependent_execution: blocked (fail closed)`.

## 3. Acceptance-criteria disposition

1. **Observed denial for each claimed smoke-test boundary** — satisfied *for
   every boundary observable without a container* (skill gates, host bypass,
   confinement primitive, sandbox invocation invariants — all observed live,
   see §2). The container-level denials (mount confinement, no-network,
   read-only root **as enforced by a live runtime**) were **attempted and
   environment-blocked**; they are explicitly **not claimed as observed**.
2. **No silent host fallback** — observed: the live-sandbox test fails closed
   with `RuntimeError` when the runtime is missing; sandbox-mode skill
   execution fails closed with zero host spawns; `STUDIO_EXECUTION_MODE=host`
   still requires `STUDIO_ALLOW_HOST_EXECUTION=1`.
3. **If no live runtime, mark isolation unqualified and block dependent
   execution** — done: `isolation_status: unqualified` and
   `dependent_execution: blocked (fail closed)` are the recorded outcomes;
   the roadmap marks the isolation-dependent stories `M5-P3-01`, `M5-P3-02`,
   `M5-P3-03` in `blocked_by_unknowns` until a live runtime qualifies the
   boundary; README § Security repeats the unqualified status.

## 4. Unsupported / unqualified guarantees (do not advertise)

- Container-level filesystem confinement, no-network enforcement, and
  read-only-root enforcement **as runtime guarantees** — unqualified until
  `python3 scripts/isolation_smoke.py --live` passes on a machine with a
  working container runtime and the pre-pulled `STUDIO_SANDBOX_IMAGE`.
- `sandbox_boundary.py` confinement primitives are unit-tested and
  smoke-observed but **not integrated into `app.py`** (audit §4) — the
  live container boundary is the enforcement that matters for tool code.
- macOS / Apple Silicon behavior (the story's intended platform) — untested
  here; repeat this smoke on target hardware.

## 5. How to qualify isolation (next evidence, `M5-P3-01`)

On a machine with Docker or Podman and the pre-pulled image
(`docker pull python:3.12-slim`, or set `STUDIO_SANDBOX_IMAGE`):

```bash
python3 scripts/isolation_smoke.py --live     # must print isolation_status: qualified
STUDIO_SANDBOX_INTEGRATION=1 STUDIO_EXECUTION_MODE=sandbox \
  python3 -m pytest -q tests/test_live_sandbox.py
```

Only after both pass may the dependent execution block be reconsidered — and
the README must then be updated with the observed results, not before.
