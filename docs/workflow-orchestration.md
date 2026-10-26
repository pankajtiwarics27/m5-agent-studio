# Workflow orchestration contract

`workflow_orchestration.py` contains the provider-neutral sequential stage
orchestrator used by the Council and Debate streaming APIs. It coordinates
stage order, prompt context, normalized content/reasoning/usage events, local
model recovery, disconnect handling, and optional local-model cleanup.

## Dependency boundaries

The module performs no network, database, filesystem, subprocess, credential,
or tool operation. The application injects:

- `ModelStreamer`, backed by the existing provider adapter and local-runtime
  router (`stream_any_model`);
- `UsageRecorder`, backed by the existing `record_perf` storage path;
- local-model detection and cleanup callbacks;
- the request disconnect check.

Provider errors propagate to the existing route-level error/SSE contract. A
disconnect stops starting later stages. Tool authorization and approval remain
at the existing central execution boundary and are not callable through this
module.

## Compatibility and persistence

The API routes retain their existing paths, SSE event names/payloads, model
route syntax, and `runs`/`perf_samples` history writes. No SQLite table,
column, schema version, provider wire, or credential contract changes are
required. The Harness, GSD, and SDLC loops remain specialized state machines
because they combine approvals, file/tool execution, local audit events, and
recovery state; this story only extracts their shared provider-stage pattern.

The standalone `studio.py` build embeds this module so the generated artifact
continues to run without an adjacent source checkout. The installed package
also exposes `workflow_orchestration` as a normal Python module.

## Evidence

Run:

```bash
python -m pytest -q tests/test_workflow_orchestration.py
python -m py_compile app.py workflow_orchestration.py studio.py
```

The focused tests cover normal multi-stage context and usage recording,
disconnect fail-closed behavior, provider failure propagation and retry
recovery, and local reasoning-only content recovery.
