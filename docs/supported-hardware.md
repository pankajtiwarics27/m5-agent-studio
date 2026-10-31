# Supported hardware

Studio is capability-driven: it asks the host what it has and degrades
explicitly. No chip, RAM size, model name, or vendor is hard-coded, and no
hardware is *required* beyond a Python 3.11+ runtime and a writable filesystem.

This page is deliberately split into what has been **validated** and what is
merely **expected**, because those are different claims.

## Requirements

| Requirement | Value | Notes |
|---|---|---|
| Python | `>=3.11` | Declared in `pyproject.toml`; CI runs 3.11, 3.12, 3.13 |
| Disk | A few GB | Model weights dominate and are stored by your runtime, not by Studio |
| Network | Not required | Loopback only by default; cloud/MCP calls happen only when you trigger them |
| OS keychain | Recommended | Without one, credential writes fail closed rather than storing plaintext |
| Container runtime | Optional | Only for `STUDIO_EXECUTION_MODE=sandbox` (Docker/Podman) |

Memory is not a fixed requirement: it depends entirely on the models you run.
Studio reports memory pressure rather than refusing to start.

## Platform status

| Platform | Status | Evidence |
|---|---|---|
| macOS (Apple Silicon) | **First target** | User-reported runs on macOS / Python 3.15 at `b7d841a`: full suite 438 passed, 1 skipped; macOS Keychain round-trip at `b452441`; Docker Desktop live sandbox test at `b452441`. Operator qualification run on an Apple M5 / 24 GiB at `d0e47f0` (2026-10-31): `--hardware` `qualified`, `--sandbox` `unqualified` ([record](m5-2-0-hardware-qualification-evidence.md)) |
| Linux x86_64 | **Validated in CI** | Full suite green on Python 3.11/3.12/3.13 for every push (see `.github/workflows/ci.yml`) |
| Linux arm64 | Expected, untested | Nothing is arch-specific, but no evidence exists |
| Windows | **Untested** | Code avoids Unix-only assumptions where it can, and descriptor/no-follow workspace operations fail closed when unavailable — so workspace file tools may refuse to run |
| Intel macOS | Untested | Local runtimes there generally lack Metal acceleration; Studio itself is unaffected |

"Untested" is not "unsupported": the code should run, but no claim is made and no
bug found there has been triaged. This table is a statement of evidence, not a
compatibility guarantee.

## Capability model

Host facts are normalized into an immutable `HardwareProfile`
([`hardware_capabilities.py`](../hardware_capabilities.py), schema version 1):
`operating_system`, `architecture`, `accelerators` (generic kind/vendor/memory,
no required names), `physical_memory_bytes`, `available_memory_bytes`,
`memory_pressure`, `installed_runtimes`, and `permissions`. Unknown values are
allowed explicitly rather than guessed.

Consumers — model recommendation, resource admission, routing — read that
profile instead of probing the host themselves, so a new platform only needs a
profiler, not changes across the app. See
[`hardware-capability-schema.md`](hardware-capability-schema.md),
[`hardware-validation.md`](hardware-validation.md), and
[`performance-profiles.md`](performance-profiles.md).

Local runtimes are selected by configured URL, never by detection heuristic:
`OMLX_BASE_URL` defaults to `http://127.0.0.1:8000` and any OpenAI-compatible
server on a loopback port works.

## Model sizing

Model choice is yours and is not validated by Studio. As a rule of thumb, weight
memory is roughly the parameter count times bytes per weight (≈0.5 GB per 1B at
4-bit, ≈2 GB per 1B at FP16), and you need headroom for the KV cache on top.

| Host memory | Practical local ceiling (rough) |
|---|---|
| 16 GiB | ~7B at 4-bit, short context |
| 24 GiB | ~13–14B at 4-bit, or a small MoE |
| 48 GiB+ | ~30B at 4-bit |

These numbers are arithmetic guidance, not measured claims, and a cloud provider
sidesteps them entirely. Studio's hardware-validation suite
(`tests/test_hardware_validation.py`) models three memory tiers through
caller-supplied profiles — it does not measure a machine.

## Evidence

- `tests/test_hardware_capabilities.py` — schema round trips, generic
  architectures/accelerators, immutability, invalid values.
- `tests/test_hardware_validation.py` — 16/24/48 GiB tier logic from supplied
  profiles, with refusals instead of optimistic assumptions.
- `tests/test_macos_hardware_profiler.py` — profiler parsing against fixtures, no
  live host probe.
- CI matrix (`.github/workflows/ci.yml`) is the actual Linux evidence.

What is **not** claimed: no benchmark numbers, no per-chip performance figures,
and no measured model throughput across models. One measured data point exists
for arm64 — an Apple M5 / 24 GiB operator run at `d0e47f0` on 2026-10-31, where
a single 31-token prompt against `Gemma-4-21B-REAP-4bit` took 16.9 s to first
token cold and 159–185 ms warm, with 1.29 GiB of swap growth
([`m5-2-0-hardware-qualification-evidence.md`](m5-2-0-hardware-qualification-evidence.md)).
That is one model, one prompt, one machine: it validates the hardware suite on
arm64, not a throughput claim. Benchmark methodology and results publication are
tracked separately in the roadmap.
