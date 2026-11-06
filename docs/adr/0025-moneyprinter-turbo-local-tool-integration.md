---
id: ADR-0025
title: moneyprinter-turbo-local-tool-integration
status: accepted
date: 2026-11-06
review_status: not-reviewed
reviewed_by: none
reviewed_on: none
---

# ADR-0025: MoneyPrinterTurbo local video tool contract and MoneyPrinterAPI provider profile

## Context

Owner integration request (2026-11-06): expose the local MoneyPrinterTurbo AI
video generator to the M5 Agent Studio workflow orchestration loop as a typed
tool, and register a Multi-LLM Council provider profile (`MoneyPrinterAPI`)
targeting the MoneyPrinterTurbo host on `http://localhost:8080/v1`.

Verified facts (checked against the MoneyPrinterTurbo source at `main`,
harry0703/MoneyPrinterTurbo, 2026-11-06):

- The CLI (`cli.py`, invoked as `uv run python cli.py`) accepts `--video-subject`,
  `--video-aspect` (`9:16` / `16:9` / `1:1`, default `9:16`), and
  `--custom-audio-file`. It prints one JSON object on stdout on success
  (`{"task_id": …, "result": {"state": …, "videos": ["…/final-1.mp4"], …}}`),
  logs to stderr, and exits `0` (success), `1` (task failure, possibly with
  partial videos printed), or `2` (argument/manifest error).
- `VideoParams` (Pydantic) requires `video_subject`; `video_aspect` defaults to
  `9:16`; `custom_audio_file` is optional and bypasses TTS.
- Stock MoneyPrinterTurbo serves a **native** FastAPI REST API under `/api/v1`
  (script/terms/video controllers), **not** an OpenAI-compatible
  `/v1/chat/completions` surface. Its default `listen_port` is `8080`.
- Rendering is delegated to ffmpeg; the pipeline also needs the LLM and
  material-provider keys configured in MoneyPrinterTurbo's own `config.toml`.

Constraints from this repository's rules: local-first privacy, no implicit
outbound requests, hardware/vendor independence, fail-closed execution,
evidence over claims.

## Decision

1. **New contract module `moneyprinter_tool.py`** (stdlib + `artifact_lineage`).
   It implements a structured-params subset of `VideoParams`
   (`video_subject` required, `video_aspect` restricted to `9:16`/`16:9`,
   optional `custom_audio_file`), publishes `VIDEO_PARAMS_SCHEMA` (JSON Schema,
   unknown keys rejected), and executes via `asyncio.create_subprocess_exec`
   with an explicit argv tuple (`--flag=value` form; no shell). stdout/stderr
   are hard-capped; a hard timeout kills the child; the child gets a minimal
   environment allowlist. `ffmpeg` is validated by a preflight
   (`preflight_ffmpeg`: resolution order caller path → `MONEYPRINTER_FFMPEG` →
   `PATH`; existence, regular file, executable bit, `-version` probe) **before**
   any child is spawned. Final MP4s are hashed and recorded in the artifact
   lineage store (`source_kind="moneyprinter-turbo"`, `trust="unverified"`,
   `confidentiality="internal"`; custom audio recorded as a `derived_from`
   parent). Preflight/configuration failures raise typed errors; task failures
   return `MoneyPrinterVideoResult` with `status="failed"` — the module never
   claims success without an MP4 path. The module is a library contract, **not**
   a new HTTP route and **not** a registered built-in tool of the admission
   boundary; callers (the orchestration loop) invoke it under their own
   authorization.
2. **Provider profile `MoneyPrinterAPI`** in `app.py` `PROVIDER_PRESETS`:
   alias `moneyprinterapi`, `wire="openai"`, `tier="gateway"`,
   `base_url="http://localhost:8080/v1"`, no key required, discovery through
   the standard OpenAI-shaped `/v1/models` abstraction. A shared
   `_is_builtin_local_gateway` helper pins the private-network bypass to the
   exact `(alias, base_url)` pair, matching the pre-existing `omniroute`
   pattern; no other local gateway inherits it.
3. **`local_provider_adapters.py`**: a `_OpenAIModelsAdapter` base now carries
   the shared `/v1/models` parser (llama.cpp behavior unchanged) and the new
   `MoneyPrinterAPIAdapter` (`runtime_name="moneyprinter"`). Discovery requests
   consume one tool unit against an optional, caller-injected
   `execution_budget.ExecutionBudget`; exhaustion fails closed
   (`"execution budget exceeded"`), cancellation propagates. The adapter
   performs no retries and no concurrency of its own.
4. **`model_discovery.py`**: registers the gateway capability surface
   (`material-search`, `script-generation`, `speech-synthesis`,
   `subtitle-generation`, `video-generation`) as a static declaration merged
   into descriptors discovered from `moneyprinter`, so negotiation can route
   script/speech work through the gateway. Budget cancellation aborts the
   discovery fan-out instead of degrading into a generic snapshot.
5. No new runtime dependencies, no new persistence schema, no change to
   `provider_switching.py`/`model_registry.py` (both already provider-neutral).
   The MoneyPrinterAPI profile advertises streaming, no tools, no structured
   output, no reasoning.

## Alternatives

- **MCP server wrapper instead of a native contract.** Rejected for now: MCP
  adds transport/schema versioning surface for a single local tool; the
  orchestration loop already consumes typed contract modules. An MCP wrapper can
  be layered on later without changing this contract.
- **Teaching the Studio the native `/api/v1` REST shape directly.** Rejected; it
  couples the Council to MoneyPrinterTurbo's internal API revision and bypasses
  the OpenAI-compatible chat-completions path the Council already speaks. A
  `/v1` OpenAI-compatible shim (or gateway) in front of MoneyPrinterTurbo is the
  documented requirement for Council traffic.
- **Wiring the tool into `app.py` as a new approval-gated route in the same
  change.** Rejected as scope creep against the deny-by-default execution
  boundary; registering `skill_execute_script`-style admission for this tool is
  a separate, reviewable change.
- **Hard-coding the ffmpeg path.** Rejected (platform independence); resolution
  is argument → `MONEYPRINTER_FFMPEG` → `PATH`, always probed.
- **Status quo (no integration).** Rejected by owner request.

## Tradeoffs

- **Correctness over convenience:** MoneyPrinterAPI will report `unavailable`
  on a stock MoneyPrinterTurbo install because stock MPT does not serve
  `/v1/models`. This is deliberate fail-closed behavior, not a defect; see the
  integration doc for the shim requirement.
- **Unified-memory hygiene:** the adapter consumes budget tool units and adds
  no concurrency, but it does **not** itself enforce GPU/unified-memory
  admission — that remains the role of `resource_admission`/`model_admission`.
- **Lineage trust:** outputs are recorded `unverified`; nothing about MP4 bytes
  is promoted to trusted because they were produced locally.
- **No independent review yet:** this integration has not received an
  independent security review; any future admission-boundary widening requires
  that review plus positive and negative tests.
- Capability tags such as `script-generation` are routing hints, not benchmarks
  of the underlying LLM (e.g. Qwen/DeepSeek) that MPT may be configured with.

## Review Status

Not reviewed. Independent review required before treating this tool as
authorization-widening or enabling unattended execution of the MPT pipeline.
