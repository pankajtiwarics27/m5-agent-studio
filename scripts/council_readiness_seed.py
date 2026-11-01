#!/usr/bin/env python3
"""Serve the Council pane over a runtime that can actually answer (UIUX-P4-01).

    python3 scripts/council_readiness_seed.py               # a runtime that answers
    python3 scripts/council_readiness_seed.py --offline     # a runtime that does not
    python3 scripts/council_readiness_seed.py --json ...    # one ready line on stdout

The readiness rows claim things about a machine: which models the local runtime lists,
which of them are resident, how much of the unified-memory ceiling is left, and whether a
saved cloud alias holds a key. On a machine with no oMLX every one of those claims collapses
into the same answer - "the runtime is not answering" - and the other rows would go
unexercised and unproven.

So this fixture stages the *runtime*, not the pane. It serves a small stand-in for the
oMLX admin API on loopback and points the app at it with `OMLX_BASE_URL`, the app's own
configuration knob, so `/api/telemetry` reports what a loaded machine reports because the
runtime said so, not because the fixture edited the payload. It also saves one cloud
provider with no key through the app's own `upsert_cloud_provider`, which is the state a
provider saved before its key is.

Three facts the fixture manufactures, all through the real server:

* `Qwen3.8-27B-oQ4e-mtp` is resident (16.60 GB), so its seat is READY;
* `Qwen3.6-35B-REAP-19B` is listed but not resident and does not fit the 4.20 GB left under
  the 20.8 GB ceiling, so its seat asks for memory it does not have;
* `Gemma-4-21B-REAP-4bit` is listed, not resident, and fits (3.80 GB), so its seat is a load
  away - and `POST /admin/api/models/<id>/load` on the stand-in really does load it, so the
  pane's "Load now" can be seen to work;
* `groq` is saved with no key, so its alias is a seat that would fail when dispatched.

`--offline` serves the same app with no runtime behind it, which is the state the demo
preview runs in: every local seat is answered by `_stream_omlx_completion`'s canned text,
and the pane has to say so before the round is pressed and after each reply arrives.

The ready line is one JSON object, like the other fixtures:

    {"base_url": "http://127.0.0.1:PORT", "token": "...", "fake_runtime": "http://127.0.0.1:PORT",
     "loaded_model": "Qwen3.8-27B-oQ4e-mtp", "unloaded_fits_model": "Gemma-4-21B-REAP-4bit",
     "unloaded_over_model": "Qwen3.6-35B-REAP-19B", "keyless_alias": "groq",
     "keyless_model": "groq::llama-3.3-70b-versatile", "root": "..."}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import demo_tour  # noqa: E402  (the root module: environment isolation)

GIB = 1024**3

LOADED_MODEL = "Qwen3.8-27B-oQ4e-mtp"
OVER_BUDGET_MODEL = "Qwen3.6-35B-REAP-19B"
FITS_MODEL = "Gemma-4-21B-REAP-4bit"
KEYLESS_ALIAS = "groq"
KEYLESS_MODEL = "groq::llama-3.3-70b-versatile"
CEILING_GB = 20.8

CATALOG = {
    LOADED_MODEL: {"gb": 16.60, "loaded": True, "kind": "llm"},
    OVER_BUDGET_MODEL: {"gb": 12.05, "loaded": False, "kind": "llm"},
    FITS_MODEL: {"gb": 3.80, "loaded": False, "kind": "vlm"},
}


class RuntimeState:
    """What the stand-in runtime is serving right now."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.models = {mid: dict(meta) for mid, meta in CATALOG.items()}

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return {mid: dict(meta) for mid, meta in self.models.items()}

    def load(self, model_id: str) -> None:
        with self.lock:
            if model_id not in self.models:
                raise KeyError(model_id)
            self.models[model_id]["loaded"] = True

    def unload(self, model_id: str) -> None:
        with self.lock:
            if model_id in self.models:
                self.models[model_id]["loaded"] = False

    def used_gb(self) -> float:
        with self.lock:
            return round(sum(meta["gb"] for meta in self.models.values() if meta["loaded"]), 2)


def _models_payload(state: RuntimeState) -> dict:
    models = []
    for model_id, meta in state.snapshot().items():
        size_b = int(meta["gb"] * GIB)
        models.append(
            {
                "id": model_id,
                "display_name": model_id,
                "loaded": bool(meta["loaded"]),
                "is_loading": False,
                "actual_size": size_b,
                "estimated_size": size_b,
                "virtual": False,
                "is_helper": False,
                "is_hidden": False,
                "pinned": False,
                "is_default": model_id == LOADED_MODEL,
                "model_type": meta["kind"],
                "settings": {"ttl_seconds": 300, "mtp_enabled": "mtp" in model_id},
            }
        )
    return {"models": models}


def _activity_payload(state: RuntimeState) -> dict:
    active = [
        {"id": model_id, "generating": [], "is_loading": False, "active_requests": 0, "ttl_remaining_seconds": 300.0}
        for model_id, meta in state.snapshot().items()
        if meta["loaded"]
    ]
    # The app reads `active_models` and everything it needs from *inside* it: the memory
    # totals and the pressure level live there in oMLX's own payload, not beside it.
    return {
        "active_models": {
            "models": active,
            "model_memory_used": int(state.used_gb() * GIB),
            "model_memory_max": int(CEILING_GB * GIB),
            "total_active_requests": 0,
            "total_waiting_requests": 0,
            "memory_pressure": {"pressure_level": "ok"},
        }
    }


# UIUX-P4-03: what a model says in the fixture. P4-01/P4-02 only needed a reply that streamed;
# a verdict needs four replies that can be *told apart*, and one pair that cannot - two seats
# pointing at the same model must produce the same words, or the pane's "same words as another
# seat" reading could never be seen. The texts differ in substance and vocabulary, not in a model
# id, so a shared-wording measure has something real to measure.
REPLY_TEXTS = {
    LOADED_MODEL: (
        "I would put the durable queue behind a single append-only log and let a consumer group "
        "track its own offsets. Backpressure comes from the log's byte budget rather than from a "
        "broker, so a slow worker stalls its partition and nothing else. Delivery is at least once, "
        "and the consumer deduplicates on the message key it already carries."
    ),
    OVER_BUDGET_MODEL: (
        "Do not build this on one log. Shard by tenant, keep a per-shard sequence number, and make "
        "the lease a row with an expiry the database enforces. A worker that dies loses the lease, "
        "not the message. The cost is a second round trip on claim, which is cheaper than the "
        "duplicate everyone discovers in production."
    ),
    FITS_MODEL: (
        "The interesting failure is the poison message, not throughput. Cap the attempts, move the "
        "message to a quarantine stream with the reason attached, and let a human release it. At "
        "five hundred per second the log is fine; what breaks is the retry storm after a downstream "
        "outage, so the retry budget has to be shared per dependency."
    ),
}


def _reply_words(model: str) -> str:
    """What the stand-in runtime says for ``model``; a distinct paragraph per catalogue entry."""
    text = REPLY_TEXTS.get(model)
    if text:
        return text
    return f"The fixture runtime answered as {model}. It streams, so the pane can see a real seat finish."


class _RuntimeHandler(BaseHTTPRequestHandler):
    state: RuntimeState
    # UIUX-P4-02: a model whose replies the runtime stops at its length limit. A real runtime
    # truncates; the fixture has to be able to, or the pane's "cut at the length limit" row can
    # only ever be exercised by a unit test and never seen on a screen.
    cap_model: str = ""
    # UIUX-P4-02: a model that answers in reasoning only, which is what the local runtime
    # actually does sometimes - the orchestrator's `content or reasoning` fallback exists
    # because of it, and the pane drew that fallback as a 0-character handoff.
    reasoning_model: str = ""
    # UIUX-P4-04: a model that answers slowly enough to be stopped mid-panel. A comparison the
    # reader stops is a state the pane has to draw (and used to leave saying "Streaming"), and a
    # fixture that answers in 200 ms cannot produce it.
    slow_model: str = ""
    slow_word_seconds: float = 0.35

    def log_message(self, *_args) -> None:  # noqa: D401 - silence the access log
        return

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        if path == "/v1/models":
            self._send({"object": "list", "data": [{"id": mid, "object": "model"} for mid in self.state.snapshot()]})
        elif path == "/admin/api/models":
            self._send(_models_payload(self.state))
        elif path == "/admin/api/activity":
            self._send(_activity_payload(self.state))
        elif path == "/admin/api/stats":
            self._send({"total_requests": 0, "total_completion_tokens": 0, "total_cached_tokens": 0, "runtime_cache": {}})
        elif path == "/admin/api/global-settings":
            self._send({"memory": {"memory_guard_custom_ceiling_gb": CEILING_GB, "memory_guard_tier": "custom"}})
        else:
            self._send({"detail": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = urlsplit(self.path).path
        if path == "/v1/chat/completions":
            self._stream_completion()
        elif path.startswith("/admin/api/models/") and path.endswith("/load"):
            model_id = path[len("/admin/api/models/") : -len("/load")]
            try:
                self.state.load(model_id)
            except KeyError:
                self._send({"detail": "unknown model"}, status=404)
                return
            self._send({"status": "ok", "model_id": model_id})
        elif path.startswith("/v1/models/") and path.endswith("/unload"):
            model_id = path[len("/v1/models/") : -len("/unload")]
            self.state.unload(model_id)
            self._send({"status": "ok"})
        else:
            self._send({"detail": "not found"}, status=404)

    def _stream_completion(self) -> None:
        """Answer a chat completion the way oMLX does: OpenAI-shaped SSE, ending in usage."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        model = str(body.get("model") or "")
        if model and model not in self.state.snapshot():
            self._send({"detail": f"no such model: {model}"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        capped = bool(self.cap_model) and model == self.cap_model
        reasoning_only = bool(self.reasoning_model) and model == self.reasoning_model
        pace = self.slow_word_seconds if (self.slow_model and model == self.slow_model) else 0.004
        words = _reply_words(model).split(" ")
        if capped:
            # Cut where a real ceiling would cut it: the reply stops mid-sentence and the finish
            # reason says why. The token count stays the truth of what was actually written.
            words = words[:6]

        try:
            for word in words:
                delta = {"reasoning_content": word + " "} if reasoning_only else {"content": word + " "}
                packet = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(packet).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(pace)
            usage = {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length" if capped else "stop"}],
                "usage": {
                    "completion_tokens": len(words),
                    "prompt_tokens": 128,
                    "time_to_first_token": 0.04,
                    "generation_tokens_per_second": 41.2,
                    "prompt_tokens_per_second": 180.0,
                },
            }
            self.wfile.write(b"data: " + json.dumps(usage).encode("utf-8") + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def start_runtime(
    cap_model: str = "", reasoning_model: str = "", slow_model: str = ""
) -> tuple[ThreadingHTTPServer, RuntimeState, str]:
    """Serve the stand-in admin API on loopback and return its base URL."""
    state = RuntimeState()
    handler = type(
        "RuntimeHandler",
        (_RuntimeHandler,),
        {
            "state": state,
            "cap_model": cap_model,
            "reasoning_model": reasoning_model,
            "slow_model": slow_model,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}"


def _demo_cli():
    """Load `scripts/demo_tour.py`, which knows how to configure the app for a demo."""
    spec = importlib.util.spec_from_file_location(
        "m5_demo_tour_cli_council", PROJECT_ROOT / "scripts" / "demo_tour.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def seed(environment: demo_tour.DemoEnvironment, runtime_url: str, *, provider: bool = True):
    """Point the isolated app at the runtime, then save one alias with no key.

    ``provider=False`` is the out-of-the-box machine: no runtime, no saved alias, so every
    seat is a local model the demo path answers for.
    """
    import os

    os.environ["OMLX_BASE_URL"] = runtime_url
    cli = _demo_cli()
    studio = cli._isolated_app(environment)
    if studio.OMLX_BASE != runtime_url:
        raise demo_tour.DemoEnvironmentError(
            f"The app ignored OMLX_BASE_URL: it reads {studio.OMLX_BASE!r}, the fixture serves {runtime_url!r}."
        )
    studio.init_db()
    if provider:
        studio.upsert_cloud_provider(
            alias=KEYLESS_ALIAS,
            name="Groq (saved without a key)",
            wire="openai",
            base_url="https://api.groq.com/openai/v1",
            api_key=None,
            models=["llama-3.3-70b-versatile"],
            enabled=True,
        )
    token = studio._load_or_create_local_api_token(environment.token_path)
    return studio, token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="serve with no runtime behind the app")
    parser.add_argument(
        "--cap-model",
        default="",
        help="a model whose replies the stand-in runtime stops at its length limit (finish_reason=length)",
    )
    parser.add_argument(
        "--reasoning-model",
        default="",
        help="a model the stand-in runtime answers in reasoning only, with no content (finish_reason=stop)",
    )
    parser.add_argument(
        "--slow-model",
        default="",
        help="a model the stand-in runtime answers slowly, so a comparison can be stopped mid-panel",
    )
    parser.add_argument("--port", type=int, default=0, help="port to bind on 127.0.0.1 (0 picks one)")
    parser.add_argument("--root", default="", help="reuse a directory as the throwaway environment")
    parser.add_argument("--ready-file", default="", help="also write the ready line to this file")
    args = parser.parse_args(argv)

    runtime: ThreadingHTTPServer | None = None
    runtime_url = ""
    if not args.offline:
        runtime, _state, runtime_url = start_runtime(
            cap_model=args.cap_model,
            reasoning_model=args.reasoning_model,
            slow_model=args.slow_model,
        )

    try:
        environment = demo_tour.make_environment(Path(args.root).expanduser() if args.root else None)
        studio, token = seed(environment, runtime_url or "http://127.0.0.1:1", provider=not args.offline)
    except demo_tour.DemoEnvironmentError as exc:  # pragma: no cover
        print(f"council readiness seed: {exc}", file=sys.stderr)
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
        "fake_runtime": runtime_url,
        "loaded_model": LOADED_MODEL,
        "unloaded_over_model": OVER_BUDGET_MODEL,
        "unloaded_fits_model": FITS_MODEL,
        "keyless_alias": KEYLESS_ALIAS,
        "keyless_model": KEYLESS_MODEL,
        "capped_model": args.cap_model,
        "reasoning_model": args.reasoning_model,
        "slow_model": args.slow_model,
        "seeded_at": round(time.time(), 3),
        "root": str(environment.root),
    }
    line = json.dumps(ready)
    print(line, flush=True)
    if args.ready_file:
        Path(args.ready_file).write_text(line + "\n", encoding="utf-8")

    import uvicorn

    try:
        uvicorn.run(studio.app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        if runtime is not None:
            runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
