# MoneyPrinterTurbo integration

Two integration surfaces, one external system: [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo),
an all-in-one AI short-video generator. Facts below were verified against the
`main` branch source (2026-11-06); line references are to that revision.

| Fact (source) | Verified value |
|---|---|
| CLI entry | `uv run python cli.py` (or `mpt cli` when installed as a package) |
| Key flags | `--video-subject`, `--video-aspect {9:16,16:9,1:1}` (default `9:16`), `--custom-audio-file` |
| Success stdout | one JSON object: `{"task_id": "<uuid>", "result": {"state": "complete", "videos": ["…/final-1.mp4"], "script": …, "audio_file": …}}` |
| Exit codes | `0` success · `1` task failure (partial videos may still be printed) · `2` argument/manifest error (stdout is empty) |
| Logging | stderr (the CLI moves its terminal logger off stdout) |
| `VideoParams` | `video_subject: str` required; `video_aspect` defaults to portrait `9:16`; `custom_audio_file: Optional[str]` skips TTS |
| Native server API | FastAPI under **`/api/v1`** (script/terms/video controllers), default `listen_port = 8080` — **not** OpenAI-compatible |
| Toolchain | rendering delegates to `ffmpeg`; LLM + material (e.g. Pexels) keys come from MPT's own `config.toml` |

## Part 1 — `moneyprinter_tool.py` (native Studio tool contract)

`moneyprinter_tool.py` is a typed contract module for the Studio's workflow
orchestration loop. It is **not** an HTTP route and **not** an admission-boundary
registration; a caller (the orchestrator) invokes it under its own authorization
and OS-level ability to run processes.

### Tool schema (`VIDEO_PARAMS_SCHEMA` / `MONEYPRINTER_VIDEO_TOOL`)

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["video_subject"],
  "properties": {
    "video_subject":     { "type": "string", "minLength": 1, "maxLength": 1000 },
    "video_aspect":      { "type": "string", "enum": ["9:16", "16:9"], "default": "9:16" },
    "custom_audio_file": { "type": "string", "minLength": 1 }
  }
}
```

Notes on the contract:

- `video_aspect` is restricted to `9:16` / `16:9` per the Studio contract, although
  MPT itself also supports `1:1`. Unknown keys are rejected.
- `video_subject` rejects control characters (including ANSI escapes and NULs);
  it is passed as `--video-subject=<value>`, never as a bare next token, so a
  subject starting with `-` cannot be re-parsed as a flag.
- `custom_audio_file` must exist, be a regular file with a `.mp3/.wav/.m4a/.aac/.flac/.ogg`
  extension (mirroring MPT's own allowlist), and be ≤ 512 MiB.

### ffmpeg preflight

`preflight_ffmpeg()` runs **before** any child is spawned. Resolution order:

1. the `ffmpeg_path` argument,
2. the `MONEYPRINTER_FFMPEG` environment variable,
3. the first `ffmpeg` on `PATH`.

The resolved binary must exist, be a regular file, be executable, and answer
`ffmpeg -version` with exit `0` within the probe timeout (default 10 s).
Failure raises `MoneyPrinterPreflightError`; the CLI is never spawned.

### Execution

```python
result = await moneyprinter_tool.run_moneyprinter_video(
    {"video_subject": "How unified memory changes AI inference", "video_aspect": "9:16"},
    project_dir="/path/to/MoneyPrinterTurbo",   # required: checkout with cli.py + config.toml
    timeout_seconds=1800,                       # hard cap 3600
    lineage_store_root="$HARNESS_STORE_DIR",    # optional; defaults to env or ./.harnesses/sessions
)
```

- `asyncio.create_subprocess_exec`, argv `("uv", "run", "python", "cli.py", "--video-subject=…", "--video-aspect=…", …)` — no shell, no string interpolation.
- The child receives a minimal environment allowlist (`PATH`, `HOME`, `TMPDIR`, `LANG`/`LC_*`, Windows equivalents) plus caller-supplied `extra_env` (validated). Studio provider credentials are **not** inherited.
- stdout/stderr are read with hard byte caps (default 4 MiB / 1 MiB); overflow is drained and discarded, and flagged via `output_truncated`.
- A hard timeout kills the child; result is `status="failed"`.
- Lineage: every produced MP4 is SHA-256 hashed and recorded (`source_kind="moneyprinter-turbo"`, `trust="unverified"`, `confidentiality="internal"`, `task_id` propagated, `allowed_operations=("read",)`). A supplied `custom_audio_file` is recorded first as `moneyprinter-custom-audio` and linked as the `derived_from` parent of each video. Lineage failure is reported on `lineage_error` and never rewrites the generation outcome.

### Output contract

```json
{
  "tool": "moneyprinter_video_generate",
  "status": "succeeded",
  "task_id": "3f9a…",
  "result": "/abs/path/storage/tasks/3f9a…/final-1.mp4",
  "videos": ["/abs/path/storage/tasks/3f9a…/final-1.mp4"],
  "error": null,
  "summary": { "task_id": "3f9a…", "result": { "state": "complete", "videos": ["…"] } },
  "lineage_entry_ids": ["pe-…", "…"],
  "lineage_error": null,
  "output_truncated": false
}
```

`status` is `succeeded` only on exit `0` **and** parseable stdout **and** at least
one final MP4 path; anything else is `failed` with `error` set. Task failures raise
nothing — they are results. Configuration/usage failures (bad params, missing
ffmpeg, missing checkout) raise `MoneyPrinterInputError` /
`MoneyPrinterPreflightError` **before** any child exists.

Exit-code handling: `0` → success path; `1` → `failed` (MPT error message surfaced,
partial videos still recorded to lineage if present); `2` and non-JSON stdout →
`failed` with a fixed-category message. Raw stderr is never returned to the caller.

### Invocation preconditions

- A local MoneyPrinterTurbo checkout passed as `project_dir`, with its `config.toml`
  providing the LLM and material-provider configuration, and its environment installed (`uv run python cli.py --help` must work).
- `uv` on `PATH` (the launcher is not configurable by design).
- `ffmpeg` on `PATH` or via `MONEYPRINTER_FFMPEG`.
- The caller holds authorization to run a local subprocess; this module does not
  consult or widen the Studio admission boundary by itself.

### Tests

`tests/test_moneyprinter_tool.py` covers schema validation, argv construction,
environment hygiene, preflight (explicit/env/PATH resolution, non-executable,
probe failure, real `-version` probe, timeout), success/failure/timeout paths,
lineage recording (including partial videos and custom-audio parentage), output
caps, and spawn-suppression on preflight/input failure. No test contacts the
network or a real MPT installation.

## Part 2 — `MoneyPrinterAPI` (Multi-LLM Council provider profile)

### Profile

| Field | Value |
|---|---|
| Preset alias | `moneyprinterapi` (display name `MoneyPrinterAPI`) |
| `wire` / `tier` | `openai` (OpenAI-compatible chat completions + SSE) / `gateway` |
| `base_url` | `http://localhost:8080/v1` |
| Auth | none required by default (`MONEYPRINTER_API_KEY` honoured if exported) |
| Default model id | `moneyprinter-gateway` |

The `(alias, base_url)` pair participates in the same builtin-loopback-gateway
private-network pin as `omniroute` (`_is_builtin_local_gateway` in `app.py`):
the outbound-URL validator permits loopback for this exact preset only. No other
preset inherits the pin.

### Discovery and gateway capabilities

`MoneyPrinterAPIAdapter` (`local_provider_adapters.py`, `runtime_name="moneyprinter"`)
implements the OpenAI-shaped `/v1/models` contract — the same abstraction llama.cpp
uses — and parses it through the shared `_OpenAIModelsAdapter` base. In
`model_discovery.py`, descriptors from `moneyprinter` additionally advertise the
gateway capability surface `material-search`, `script-generation`,
`speech-synthesis`, `subtitle-generation`, `video-generation`
(`GATEWAY_RUNTIME_CAPABILITIES`, single source `MONEYPRINTER_GATEWAY_CAPABILITIES`),
so capability negotiation can route script/speech work through the gateway even
when the endpoint does not declare capabilities itself. These are capability
tags, not model or vendor identifiers — MPT typically fronts an OpenAI-compatible
LLM (e.g. Qwen/DeepSeek) configured in its own `config.toml`; nothing here
requires or verifies that backend.

### Execution budget

Each remote discovery request consumes **one tool unit** against an optional,
caller-injected `execution_budget.ExecutionBudget` (`budget=` kwarg on the
adapter) and re-checks elapsed time/tokens/cost/retries. Exhaustion fails closed
(`LocalRuntimeSnapshot` with `error="execution budget exceeded"`) **before** the
transport is called; a cancelled budget propagates `ExecutionCancelled` through
both `discover()` and `discover_models()`. The adapter starts no background work
and adds no concurrency of its own — at most one in-flight request per adapter.
Admission control for unified-memory/VRAM residency is deliberately **not**
reimplemented here; that remains `resource_admission` / `model_admission`.

### Honest availability status

Stock MoneyPrinterTurbo does **not** serve `/v1/chat/completions` or
`/v1/models`; it serves `/api/v1/...` (script/terms/video). The
`MoneyPrinterAPI` profile therefore requires an OpenAI-compatible shim or
gateway in front of MPT on port 8080 (for example, an OmniRoute-style local
gateway, or any reverse proxy mapping the OpenAI surface onto MPT's routes).
Against a stock install the adapter reports `unavailable` — it never invents
models. Live video rendering is always available through Part 1's CLI contract
without any shim.

### Tests

`tests/test_moneyprinter_api.py` covers the preset pin, builtin-gateway
exact-match semantics, adapter discovery success/unavailable/malformed paths,
gateway-capability merging in `discover_models`, negotiation feasibility for
`script-generation`, the tool-unit/time budget behavior, and cancellation
propagation.

## Limitations and follow-ups

- Registering `moneyprinter_video_generate` as an admission-boundary built-in
  (like `skill_execute_script`) is intentionally out of scope; it needs its own
  review and negative tests.
- The Studio does not (yet) ship an OpenAI→MPT shim; until one is deployed,
  Council traffic to `MoneyPrinterAPI` will reflect `unavailable`.
- `provider_switching.py` and `model_registry.py` are unchanged by design;
  MoneyPrinterAPI is streaming-only, without tools/structured output/reasoning.
- Non-goals: no new persistence schema, no new runtime dependency, no change to
  the standalone `studio.py` module wiring (the tool module is entered by the
  orchestration loop, not imported by `studio.py`).

Decision record: [ADR-0025](adr/0025-moneyprinter-turbo-local-tool-integration.md).
