#!/usr/bin/env python3
"""Measured model probe for the M5-P4-01 qualification suite (stdlib only).

This is a *measuring instrument*, not evidence: it performs one real request
against an OpenAI-compatible local model server and prints one JSON line
describing what it observed. The suite supplies the evidence frame
(``scripts/qualify_environment.py --hardware --model-command ...``), records each
run's exit code and wall time, and refuses to turn a missing field into a
number.

Usage::

    python3 scripts/model_probe.py --base-url http://127.0.0.1:8000/v1
    python3 scripts/model_probe.py --model mlx-community/Qwen3-8B-4bit --max-tokens 128
    python3 scripts/model_probe.py --server-pid 4711   # also sample the server's RSS

Typical M5-P4-01 run (with a coding-case file, see docs/qualification-suite.md)::

    python3 scripts/qualify_environment.py --hardware --pressure-gb 16 \\
        --coding-cases cases.json --case-runs 3 \\
        --model-command 'python3 scripts/model_probe.py --base-url http://127.0.0.1:8000/v1'

What it reports (every field is optional but honest — an unobserved value is
omitted, never defaulted):

``model_id``          the id the *server* reports for the response, else ``--model``;
``runtime``           ``openai-compatible`` (the wire protocol actually used);
``temperature``       the value sent with the request;
``first_token_ms``    measured time to the first non-empty content delta
                      (absent when the server does not stream);
``prompt_tokens``     from the server's ``usage`` block only;
``output_tokens``     from the server's ``usage`` block only;
``end_to_end_ms``     measured wall time of the request;
``output_chars``      characters received (a measurement, not a token count);
``peak_memory_bytes`` sampled RSS of ``--server-pid`` while the request ran;
``case_id`` / ``case_category``  echoed when the qualification suite supplies
                      ``STUDIO_QUALIFY_CASE_ID`` / ``STUDIO_QUALIFY_CASE_CATEGORY``;
``notes``             what could not be observed, in words.

Exit codes: ``0`` measured; ``1`` the server answered with an error or no
content; ``2`` could not measure (nothing listening, no model id, bad
arguments). On ``1``/``2`` the reason goes to stderr and **no** JSON line is
printed, so a failed probe can never be mistaken for a measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Sequence

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_PROMPT = (
    "Write a Python function that returns the nth Fibonacci number, then explain it in one sentence."
)
DEFAULT_RUNTIME = "openai-compatible"
EXIT_MEASURED = 0
EXIT_SERVER_ERROR = 1
EXIT_NOT_MEASURED = 2


def _rss_bytes(pid: int) -> int | None:
    """Resident set size of ``pid``, or ``None`` when it cannot be read."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        return None
    try:
        # ps reports RSS in KiB on both macOS and Linux.
        return int(text.split()[0]) * 1024
    except (ValueError, IndexError):
        return None


class RssSampler:
    """Background peak-RSS sampler for the model *server* process."""

    def __init__(self, pid: int, interval: float = 0.05) -> None:
        self.pid = pid
        self.interval = interval
        self.peak: int | None = None
        self.readable = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rss-sampler", daemon=True)

    def _sample_once(self) -> None:
        value = _rss_bytes(self.pid)
        if value is None:
            return
        self.readable = True
        self.peak = value if self.peak is None else max(self.peak, value)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample_once()

    def start(self) -> None:
        # Sample synchronously *before* the measured request begins. Otherwise the
        # first sample waits a full interval and then depends on how long the `ps`
        # spawn takes: when that spawn is slow (a loaded CI runner), the stream can
        # finish first and a readable pid is reported as unmeasurable. A single `ps`
        # at t=0 removes that window without inflating the measured latency, because
        # this runs before the clock starts.
        self._sample_once()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def _request(url: str, body: bytes, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def first_model_id(base_url: str, timeout: float) -> str:
    """Ask the server which model it is serving; empty when it cannot say.

    A server that *answers* without a model list returns ``""`` (the caller then
    asks for ``--model``). A server that cannot be reached raises, so the caller
    can say so instead of blaming a missing model id.
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError:
        return ""
    except ValueError:
        return ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return str(data[0].get("id") or "")
    return ""


def _unreachable(base_url: str, reason: object) -> None:
    print(
        f"model_probe: no measurement taken - {base_url} is unreachable ({reason}). "
        "Start the local model server, or pass --base-url.",
        file=sys.stderr,
    )


def resolve_prompt(explicit: str) -> tuple[str, dict[str, str]]:
    """Explicit prompt, else the qualification suite's case prompt, else default.

    The suite exports ``STUDIO_QUALIFY_CASE_PROMPT`` per case; returning the case
    identity as well lets the record say which case produced this measurement.
    """
    case = {
        key: value
        for key, value in (
            ("case_id", os.environ.get("STUDIO_QUALIFY_CASE_ID", "")),
            ("case_category", os.environ.get("STUDIO_QUALIFY_CASE_CATEGORY", "")),
        )
        if value
    }
    case_prompt = os.environ.get("STUDIO_QUALIFY_CASE_PROMPT", "")
    return (explicit or case_prompt or DEFAULT_PROMPT), case


def _stream_measurement(response: Any, started: float) -> dict[str, Any]:
    """Read an SSE chat-completion stream and measure first-token latency.

    ``started`` is when the request was *sent*, not when the headers arrived:
    timing from the first byte of the response would report a first-token
    latency of ~0 ms and silently exclude connection setup and prefill.
    """
    first_token_at: float | None = None
    usage: dict[str, Any] | None = None
    reported_model = ""
    characters = 0
    deltas = 0
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        reported_model = str(event.get("model") or reported_model)
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            content = delta.get("content") if isinstance(delta, dict) else None
            if content:
                deltas += 1
                characters += len(str(content))
                if first_token_at is None:
                    first_token_at = time.perf_counter()
    ended = time.perf_counter()
    return {
        "started": started,
        "ended": ended,
        "first_token_at": first_token_at,
        "usage": usage,
        "reported_model": reported_model,
        "characters": characters,
        "deltas": deltas,
        "streamed": True,
    }


def _buffered_measurement(response: Any, started: float) -> dict[str, Any]:
    """Fallback for a server that ignores ``stream: true``.

    The body is one JSON document, so there is no first token to time: the field
    stays absent instead of being approximated from the total.
    """
    body = response.read().decode("utf-8", "replace")
    ended = time.perf_counter()
    try:
        payload = json.loads(body or "{}")
    except ValueError:
        payload = {}
    content = ""
    reported_model = ""
    usage = None
    if isinstance(payload, dict):
        reported_model = str(payload.get("model") or "")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        for choice in payload.get("choices") or []:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and message.get("content"):
                content = str(message["content"])
                break
    return {
        "started": started,
        "ended": ended,
        "first_token_at": None,
        "usage": usage,
        "reported_model": reported_model,
        "characters": len(content),
        "deltas": 1 if content else 0,
        "streamed": False,
    }


def _finish(
    response: Any,
    args: argparse.Namespace,
    model: str,
    notes: list[str],
    sampler: RssSampler | None,
    started: float,
) -> tuple[dict[str, Any], int]:
    headers = getattr(response, "headers", None)
    content_type = str(headers.get("Content-Type", "")) if headers is not None else ""
    try:
        if "event-stream" in content_type:
            observed = _stream_measurement(response, started)
        else:
            observed = _buffered_measurement(response, started)
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    if observed["deltas"] == 0:
        print("model_probe: the server returned no content; nothing was measured.", file=sys.stderr)
        return {}, EXIT_SERVER_ERROR
    if not observed["streamed"]:
        notes.append("the server ignored stream=true, so first_token_ms was not measured")

    report: dict[str, Any] = {
        "model_id": observed["reported_model"] or model,
        "runtime": DEFAULT_RUNTIME,
        "temperature": args.temperature,
        "end_to_end_ms": round((observed["ended"] - observed["started"]) * 1000.0, 1),
        "output_chars": observed["characters"],
    }
    if observed["first_token_at"] is not None:
        report["first_token_ms"] = round((observed["first_token_at"] - observed["started"]) * 1000.0, 1)
    usage = observed["usage"] or {}
    if usage.get("prompt_tokens") is not None:
        report["prompt_tokens"] = int(usage["prompt_tokens"])
    if usage.get("completion_tokens") is not None:
        report["output_tokens"] = int(usage["completion_tokens"])
    if "output_tokens" not in report:
        notes.append("the server reported no usage block, so output_tokens was not measured")
    if args.model_digest:
        report["model_digest"] = args.model_digest
    else:
        notes.append("pass --model-digest to name the weights; the profile records 'unknown' without it")
    if args.context_length:
        report["context_length"] = args.context_length
    if sampler is not None:
        if sampler.readable and sampler.peak is not None:
            report["peak_memory_bytes"] = sampler.peak
        else:
            notes.append(
                f"RSS of --server-pid {sampler.pid} was not readable, so peak_memory_bytes was not measured"
            )
    if notes:
        report["notes"] = notes
    return report, EXIT_MEASURED


def measure(args: argparse.Namespace, prompt: str, model: str) -> tuple[dict[str, Any], int]:
    """Perform one request and return the report plus the exit code."""
    url = f"{args.base_url.rstrip('/')}/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode("utf-8")

    notes: list[str] = []
    sampler = RssSampler(args.server_pid) if args.server_pid else None
    if sampler is not None:
        sampler.start()
    started = time.perf_counter()
    try:
        response = _request(url, body, args.timeout)
        return _finish(response, args, model, notes, sampler, started)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - the status line is the useful part
            detail = ""
        print(f"model_probe: the server replied {exc.code} {exc.reason}. {detail}".rstrip(), file=sys.stderr)
        return {}, EXIT_SERVER_ERROR
    except (urllib.error.URLError, OSError) as exc:
        _unreachable(args.base_url, getattr(exc, "reason", exc))
        return {}, EXIT_NOT_MEASURED
    finally:
        if sampler is not None:
            sampler.stop()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One measured request against an OpenAI-compatible local model server.",
        epilog="Prints one JSON line describing what was observed; unobserved fields are omitted.",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"OpenAI-compatible base URL (default {DEFAULT_BASE_URL})"
    )
    parser.add_argument("--model", default="", help="model id (default: ask the server's /models endpoint)")
    parser.add_argument(
        "--prompt", default="", help="prompt text (default: STUDIO_QUALIFY_CASE_PROMPT, else a fixed prompt)"
    )
    parser.add_argument("--max-tokens", type=int, default=64, help="max tokens to generate (default 64)")
    parser.add_argument("--temperature", type=float, default=0.0, help="sampling temperature (default 0.0)")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-request timeout in seconds (default 300)")
    parser.add_argument("--model-digest", default="", help="optional digest of the model weights, recorded verbatim")
    parser.add_argument("--context-length", type=int, default=None, help="optional declared context length")
    parser.add_argument("--server-pid", type=int, default=None, help="sample this process's RSS for peak memory")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_tokens < 1:
        print("model_probe: --max-tokens must be at least 1", file=sys.stderr)
        return EXIT_NOT_MEASURED
    prompt, case = resolve_prompt(args.prompt)
    model = args.model
    if not model:
        try:
            model = first_model_id(args.base_url, min(args.timeout, 30.0))
        except (urllib.error.URLError, OSError) as exc:
            _unreachable(args.base_url, getattr(exc, "reason", exc))
            return EXIT_NOT_MEASURED
    if not model:
        print(
            "model_probe: could not determine a model id (the /models endpoint did not answer); pass --model.",
            file=sys.stderr,
        )
        return EXIT_NOT_MEASURED
    report, code = measure(args, prompt, model)
    if code != EXIT_MEASURED:
        return code
    report.update(case)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return EXIT_MEASURED


if __name__ == "__main__":
    raise SystemExit(main())
