#!/usr/bin/env python3
"""Serve the isolated demo instance with a pipeline that can be seen to fail (UIUX-P3-07).

    python3 scripts/pipeline_recovery_seed.py            # serve; prints one JSON line

A failure is the one pipeline state the demo cannot produce. `_stream_omlx_completion` always
answers when oMLX is offline, so no run ever raises, and the pane's failure surface would be
evidence for nothing if a fixture merely drew it.

So this fixture stages the *provider*, not the pane: it wraps the server's own
`stream_any_model` and raises from inside the wrapped stream for two model names.

* `critic-fails-demo` yields a few content tokens and then raises, which is the shape of a
  provider that dies mid-seat - the pane has output to salvage and a seat to name;
* `architect-fails-fast-demo` raises before a single token, which is the shape of a provider that
  refuses the request, and leaves nothing to salvage.

The exception propagates through `WorkflowOrchestrator.run_stages` exactly as the orchestrator's
docstring says it will ("A provider exception is intentionally allowed to propagate. The caller
owns its existing error-to-SSE/API contract"), and the route's own `except` turns it into the
`error` event the pane reads. Nothing in the route, the state machine or the records is patched:
`save_run()` still writes only after the fourth seat returns, so a failed run really does leave
nothing on record - which is the fact the surface has to state.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "failing_model": "critic-fails-demo", "failing_fast_model": "architect-fails-fast-demo",
     "failure_message": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)

FAILING_MODEL = "critic-fails-demo"
FAILING_FAST_MODEL = "architect-fails-fast-demo"
FAILURE_MESSAGE = "provider stream ended unexpectedly (HTTP 502 from the inference endpoint)"
TOKENS_BEFORE_FAILURE = 6
# UIV2-25 additive fixtures: the same provider wrapper drives a real slow first seat and a
# genuinely long final answer through the unchanged orchestrator and SSE route.
SLOW_MODEL = "architect-loading-demo"
LONG_MODEL = "judge-long-content-demo"
LONG_REPLY = """## Durable queue review

The lease must be fenced before a worker can write. Recovery replays only acknowledged entries,
and a worker that loses its lease cannot acknowledge new work.

""" + "\n\n".join(
    f"### Check {index}: recovery decision {index}\nLease fencing, backpressure, replay ordering and audit evidence remain explicit at step {index}. "
    f"The queue records the observed transition, preserves the idempotency key, and refuses a stale writer before committing result {index}."
    for index in range(1, 13)
) + "\n\nThe final response ends with Check 12 and the complete audit trail."


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_recovery", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def patch_stream(studio):
    """Wrap `stream_any_model` so two demo seats fail the way a real provider would."""
    original = studio.stream_any_model

    async def stream_any_model(client, model_spec, messages, temperature=0.0, max_tokens=4096,
                               reasoning_mode="off", _depth=0):
        if model_spec == FAILING_FAST_MODEL:
            raise RuntimeError(FAILURE_MESSAGE)
        emitted = 0
        slow_delayed = False
        long_sent = False
        async for item in original(
            client, model_spec, messages, temperature, max_tokens, reasoning_mode, _depth
        ):
            if model_spec == SLOW_MODEL and item.get("type") == "content" and not slow_delayed:
                slow_delayed = True
                await asyncio.sleep(2.0)
            if model_spec == LONG_MODEL and item.get("type") == "content":
                if not long_sent:
                    long_sent = True
                    yield {**item, "text": LONG_REPLY}
                continue
            if model_spec == FAILING_MODEL:
                if item.get("type") == "content":
                    emitted += 1
                    if emitted > TOKENS_BEFORE_FAILURE:
                        raise RuntimeError(FAILURE_MESSAGE)
                elif item.get("type") == "usage":
                    # The seat never finished, so no usage line may arrive: a usage line is what
                    # the pane reads as "this seat is done".
                    continue
            yield item

    studio.stream_any_model = stream_any_model
    return original


def seed(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and stage the two failures."""
    cli = _demo_cli()
    studio = cli._isolated_app(environment)
    studio.init_db()
    token = studio._load_or_create_local_api_token(environment.token_path)
    patch_stream(studio)
    return studio, token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 picks one)")
    parser.add_argument("--root", default="", help="reuse a directory as the throwaway environment")
    parser.add_argument("--ready-file", default="", help="also write the ready line to this file")
    args = parser.parse_args(argv)

    try:
        environment = demo_tour.make_environment(Path(args.root).expanduser() if args.root else None)
        studio, token = seed(environment)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"pipeline recovery seed: {exc}", file=sys.stderr)
        return 1

    port = args.port
    if port == 0:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

    ready = {
        "base_url": f"http://127.0.0.1:{port}",
        "token": token,
        "failing_model": FAILING_MODEL,
        "failing_fast_model": FAILING_FAST_MODEL,
        "failure_message": FAILURE_MESSAGE,
        "slow_model": SLOW_MODEL,
        "long_model": LONG_MODEL,
        "long_reply_chars": len(LONG_REPLY),
        "seeded_at": round(time.time(), 3),
        "root": str(environment.root),
    }
    line = json.dumps(ready)
    print(line, flush=True)
    if args.ready_file:
        Path(args.ready_file).write_text(line + "\n", encoding="utf-8")

    import uvicorn

    uvicorn.run(studio.app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
