# Discover and install models

AS-053 defines cross-runtime discovery aggregation and the runtime-owned
install contract in [`model_discovery.py`](../model_discovery.py). It builds on
the AS-017 registry descriptors and the AS-020 Ollama/llama.cpp adapters.

`discover_models()` fans out to any number of injected discovery sources
(structurally, the AS-020 adapters), isolates per-source failures, and merges
the snapshots into one deterministic `ModelCatalog`: sorted descriptors keyed
by `(model_id, digest)`, per-runtime availability with safe errors, conflict
entries, and ambiguous-identity reporting. `ModelCatalog.registry()` bridges to
the AS-017 `ModelRegistry`, and `to_mapping()` yields JSON-safe output for
future API and UI surfaces.

Discovery is data-driven: a model that a runtime begins to report (however it
was installed — CLI pull, directory import, UI action) appears in the next
catalog without any application code change. Duplicate identities that
runtimes disagree on collapse to the first entry in runtime-name order and
are flagged in `conflicts`, so the registry bridge never raises on a catalog
produced by discovery.

## Install contract

Install remains an explicit user intent delegated to the runtime itself:

- `InstallRequest` accepts only runtimes in `SUPPORTED_INSTALL_RUNTIMES`
  (today: `ollama`, whose CLI owns `pull`) and validates the model reference
  fail-closed: length- and shape-bounded `name[:tag]` path segments, no
  metacharacters. A reference without an explicit tag receives the runtime's
  default tag (`latest`) at planning time.
- `plan_install()` returns an **inert** `InstallPlan` whose `argv` is the
  runtime-native command (`("ollama", "pull", "<name:tag>")`). The module
  never executes it — no subprocess, no shell, no network, no model loading.
  Transport selection, execution, and audit live with the caller.
- `observe_install(before, after, plan)` classifies the outcome by comparing
  catalogs captured around the runtime action: `installed`,
  `already_present` (idempotent re-runs), or `not_installed` (a failed or
  unfinished runtime action never fabricates a model record).

Untrusted exception text from a failing runtime is contained: sources that
raise produce the fixed safe error `discovery failed`, and a snapshot whose
runtime name does not match its source is reported as a contract violation
instead of being merged.

## Evidence

`tests/test_model_discovery.py` covers multi-runtime union and deterministic
ordering, unavailable-runtime reporting, safe-error containment of upstream
exception text, source fan-out validation (empty, non-sequence, duplicate,
contract violation), cancellation propagation, descriptor conflict
deduplication, ambiguous identities, install-request rejection of injection
and path shapes, inert argv planning with default-tag normalization, the
three observation statuses, and — the acceptance proof — an end-to-end run
where a fake `ollama` CLI executed through `subprocess` (no shell, disposable
tmp directory) mutates only runtime state and immediate re-discovery with the
identical code path reports the new model, classifies a re-run as
`already_present`, and a failed runtime action as `not_installed`.

Runtime-native installation on a target machine with a real `ollama` daemon is
exercised at deployment time (same convention as AS-019's live-runtime
verification); no CI test contacts a live runtime, model, or network endpoint.
