#!/usr/bin/env python3
"""Serve the isolated demo instance with a pipeline that can be watched and stopped (UIUX-P3-06).

    python3 scripts/pipeline_artifacts_seed.py            # serve; prints one JSON line

The demo has no model runtime: `_stream_omlx_completion` answers from a canned reply when oMLX is
offline, which is exactly what makes the four-seat pipeline walkable without a GPU. But two of the
states this story has to draw cannot be produced by that reply, because it always answers:

* **an empty seat** - a stage that returns usage and no content at all. The pane must draw that as
  "produced nothing", not as a stage that has just started, because the seat after it is handed an
  empty section and the run line has to say so;
* **a run slow enough to stop** - Stop needs a stage that is still streaming when the press lands.

So this fixture does not invent a state: it wraps the server's own `stream_any_model` and changes
two things about the *arrival* of the demo reply - nothing about the state machine, the routes or
the records. A model named `critic-empty-seat-demo` yields a usage line and no content; a model
named `architect-slow-seat-demo` yields the same reply, one token at a time, slowly. Everything the
pane draws afterwards comes from the server: the SSE events, `save_run()`, and the run record.

    {"base_url": "http://127.0.0.1:PORT", "token": "...",
     "empty_model": "critic-empty-seat-demo", "slow_model": "architect-slow-seat-demo"}
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

EMPTY_MODEL = "critic-empty-seat-demo"
SLOW_MODEL = "architect-slow-seat-demo"
SLOW_SECONDS_PER_TOKEN = 0.03


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_artifacts", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def patch_stream(studio):
    """Wrap `stream_any_model` so two demo seats arrive empty or slow, and say so.

    The wrapper is a pass-through for every other model. It is installed on the module the route
    actually calls, which is the only way to stage the timing without editing the route.
    """
    original = studio.stream_any_model

    async def stream_any_model(client, model_spec, messages, temperature=0.0, max_tokens=4096,
                               reasoning_mode="off", _depth=0):
        slow = model_spec == SLOW_MODEL
        async for item in original(
            client, model_spec, messages, temperature, max_tokens, reasoning_mode, _depth
        ):
            if model_spec == EMPTY_MODEL:
                # Usage and no content: the seat ran, and produced nothing.
                if item.get("type") == "usage":
                    empty_usage = {**item, "completion_tokens": 0, "reasoning_tokens": 0}
                    yield empty_usage
                continue
            if slow and item.get("type") == "content":
                await asyncio.sleep(SLOW_SECONDS_PER_TOKEN)
            yield item

    studio.stream_any_model = stream_any_model
    return original


def seed(environment: demo_tour.DemoEnvironment):
    """Configure the isolated app and stage the two arrivals the walk needs."""
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
        print(f"pipeline artifacts seed: {exc}", file=sys.stderr)
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
        "empty_model": EMPTY_MODEL,
        "slow_model": SLOW_MODEL,
        "slow_seconds_per_token": SLOW_SECONDS_PER_TOKEN,
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
