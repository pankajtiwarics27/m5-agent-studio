"""Tests for scripts/model_probe.py (the M5-P4-01 measuring instrument).

The probe is the piece that turns "write your own harness" into a command the
owner can actually run against a local model server. These tests hold it to the
same standard as the qualification suite it feeds:

* a measurement is reported only when it was observed (first token, usage);
* an unreachable server, an HTTP error or an empty answer exits non-zero and
  prints no JSON line, so a failed probe cannot be read as a measurement;
* the qualification suite consumes its output and can derive a decode rate from
  it without inventing anything.

The fake server below is a *test double for the wire protocol*, not model
evidence: nothing here is a claim about any real model's performance.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "model_probe.py"

pytestmark = pytest.mark.skipif(shutil.which(sys.executable) is None, reason="needs a Python interpreter")


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible endpoint with switchable pathologies."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # keep the test output clean
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        server = self.server
        if server.mode == "no_models" or not self.path.rstrip("/").endswith("/models"):
            self._send(404, "application/json", b'{"error":"no models"}')
            return
        self._send(200, "application/json", json.dumps({"data": [{"id": server.model_id}]}).encode())

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        server.requests.append(payload)
        if server.mode == "http_error":
            self._send(500, "application/json", b'{"error":"boom"}')
            return
        reported_model = str(payload.get("model") or server.model_id)
        if server.mode == "buffered":
            body = {
                "model": reported_model,
                "choices": [{"message": {"role": "assistant", "content": "buffered answer"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
            self._send(200, "application/json", json.dumps(body).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in ("def fib", "onacci", "(n):"):
            time.sleep(server.chunk_delay)  # delay *before* each chunk, so the first byte is late too
            event = {"model": reported_model, "choices": [{"delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
        if server.mode != "no_usage":
            usage = {"model": reported_model, "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 5}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def fake_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    server.model_id = "test-model"  # type: ignore[attr-defined]
    server.mode = "stream"  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    server.chunk_delay = 0.0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _base_url(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/v1"


def _run(
    *args: str, env: dict[str, str] | None = None, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    merged = {key: value for key, value in os.environ.items() if not key.startswith("STUDIO_QUALIFY_")}
    merged.update(env or {})
    return subprocess.run(
        [sys.executable, str(PROBE), *args], capture_output=True, text=True, timeout=timeout, env=merged
    )


def _report(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    return json.loads(lines[0])


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_probe_measures_a_streaming_response(fake_server: ThreadingHTTPServer) -> None:
    result = _run("--base-url", _base_url(fake_server))
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert report["model_id"] == "test-model"
    assert report["runtime"] == "openai-compatible"
    assert report["temperature"] == 0.0
    # Observed, not assumed: a streamed first token was timed, and the server's
    # usage block supplied the token counts.
    assert report["first_token_ms"] >= 0.0
    assert report["prompt_tokens"] == 11
    assert report["output_tokens"] == 5
    assert report["output_chars"] == len("def fibonacci(n):")
    assert report["end_to_end_ms"] >= report["first_token_ms"]
    # No digest was supplied, so none was invented.
    assert "model_digest" not in report


def test_probe_uses_the_qualification_case_prompt(fake_server: ThreadingHTTPServer) -> None:
    result = _run(
        "--base-url",
        _base_url(fake_server),
        env={
            "STUDIO_QUALIFY_CASE_ID": "case-7",
            "STUDIO_QUALIFY_CASE_CATEGORY": "algorithms",
            "STUDIO_QUALIFY_CASE_PROMPT": "Implement binary search.",
        },
    )
    assert result.returncode == 0, result.stderr
    sent = fake_server.requests[-1]  # type: ignore[attr-defined]
    assert sent["messages"][0]["content"] == "Implement binary search."
    report = _report(result.stdout)
    assert report["case_id"] == "case-7"
    assert report["case_category"] == "algorithms"


def test_first_token_latency_includes_the_wait_for_the_first_token(
    fake_server: ThreadingHTTPServer,
) -> None:
    """Timed from the request, not from the headers: a slow first chunk must show."""
    fake_server.chunk_delay = 0.15  # type: ignore[attr-defined]
    result = _run("--base-url", _base_url(fake_server))
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert report["first_token_ms"] >= 100.0, report
    assert report["end_to_end_ms"] >= report["first_token_ms"]


def test_probe_does_not_invent_usage_the_server_did_not_report(fake_server: ThreadingHTTPServer) -> None:
    fake_server.mode = "no_usage"  # type: ignore[attr-defined]
    result = _run("--base-url", _base_url(fake_server))
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert "output_tokens" not in report
    assert "prompt_tokens" not in report
    assert any("usage" in note for note in report["notes"])


def test_probe_reports_an_unreachable_server_without_a_measurement() -> None:
    result = _run("--base-url", f"http://127.0.0.1:{_closed_port()}/v1", "--timeout", "5")
    assert result.returncode == 2
    assert "unreachable" in result.stderr
    assert "{" not in result.stdout


def test_probe_reports_an_http_error_as_a_failed_measurement(fake_server: ThreadingHTTPServer) -> None:
    fake_server.mode = "http_error"  # type: ignore[attr-defined]
    result = _run("--base-url", _base_url(fake_server), "--model", "test-model")
    assert result.returncode == 1
    assert "500" in result.stderr
    assert "{" not in result.stdout


def test_probe_needs_a_model_id_when_the_server_cannot_name_one(fake_server: ThreadingHTTPServer) -> None:
    fake_server.mode = "no_models"  # type: ignore[attr-defined]
    missing = _run("--base-url", _base_url(fake_server))
    assert missing.returncode == 2
    assert "--model" in missing.stderr
    assert "{" not in missing.stdout

    # With the id supplied the same server still measures.
    supplied = _run("--base-url", _base_url(fake_server), "--model", "explicit-model")
    assert supplied.returncode == 0, supplied.stderr
    assert _report(supplied.stdout)["model_id"] == "explicit-model"


def test_probe_refuses_to_time_a_first_token_it_did_not_see(fake_server: ThreadingHTTPServer) -> None:
    fake_server.mode = "buffered"  # type: ignore[attr-defined]
    result = _run("--base-url", _base_url(fake_server))
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert "first_token_ms" not in report
    assert report["output_tokens"] == 3
    assert any("stream=true" in note for note in report["notes"])


@pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps to sample RSS")
def test_probe_samples_the_server_process_memory(fake_server: ThreadingHTTPServer) -> None:
    fake_server.chunk_delay = 0.2  # type: ignore[attr-defined]
    result = _run("--base-url", _base_url(fake_server), "--server-pid", str(os.getpid()))
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert report["peak_memory_bytes"] > 0


def test_probe_never_reports_a_memory_figure_for_an_unreadable_pid(
    fake_server: ThreadingHTTPServer,
) -> None:
    result = _run("--base-url", _base_url(fake_server), "--server-pid", "999999")
    assert result.returncode == 0, result.stderr
    report = _report(result.stdout)
    assert "peak_memory_bytes" not in report
    assert any("999999" in note for note in report["notes"])


def test_qualification_suite_consumes_the_probe_output(
    fake_server: ThreadingHTTPServer, tmp_path: Path
) -> None:
    """The instrument and the evidence frame must fit together."""
    from scripts import qualify_environment as qe

    command = f"{sys.executable} {PROBE} --base-url {_base_url(fake_server)}"
    profile_path = tmp_path / "profile.jsonl"
    ok, detail, evidence = qe._model_probe(command, 1, 60.0, profile_path)
    assert ok, detail
    assert evidence["successful_runs"] == 1
    reported = evidence["runs"][0]["reported"]
    assert reported["first_token_ms"] >= 0.0
    assert reported["output_tokens"] == 5

    import model_calibration

    profile = model_calibration.load_profile(evidence["profile_path"])
    assert profile.failed_runs == 0
    # The rate is derived from the measured first token and the server's token
    # count; had either been missing this would be None rather than a guess.
    assert profile.aggregate("decode_tokens_per_s", "warm").median is not None


def test_probe_rss_helper_reads_this_process() -> None:
    from scripts import model_probe

    if shutil.which("ps") is None:
        pytest.skip("needs ps")
    assert model_probe._rss_bytes(os.getpid()) and model_probe._rss_bytes(os.getpid()) > 0
    assert model_probe._rss_bytes(999999) is None
