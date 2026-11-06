"""MoneyPrinterTurbo CLI integration: a typed native Studio tool contract.

This module exposes the local `MoneyPrinterTurbo <https://github.com/harry0703/MoneyPrinterTurbo>`_
video pipeline to the Studio's workflow orchestration loop. It implements the
subset of MoneyPrinterTurbo's ``VideoParams`` contract that a video-generation
tool needs and nothing more:

- ``video_subject`` (string, required) — the video topic/keyword.
- ``video_aspect`` (``"9:16"`` or ``"16:9"``, optional, default ``"9:16"``) — portrait or landscape output.
- ``custom_audio_file`` (string, optional) — a local MP3/WAV/M4A/AAC/FLAC/OGG
  voiceover that replaces TTS rendering.

The tool shells out to ``uv run python cli.py`` inside a caller-supplied
MoneyPrinterTurbo checkout. It never uses a shell, never interpolates the
subject into a command string, and passes ``--video-subject=<value>`` style
arguments so an adversarial subject cannot be re-parsed as a flag. No child
process is spawned before the ffmpeg preflight and the checkout validation pass.

Contract summary
----------------

``MoneyPrinterTurbo``'s single-task CLI prints exactly one JSON object on
stdout when the task finished (or produced partial videos); logs go to stderr:

.. code-block:: json

   {"task_id": "<uuid>", "result": {"state": "complete", "videos": ["…/final-1.mp4"], …}}

Exit codes: ``0`` success, ``1`` task failure (only when no partial videos exist
is stdout empty), ``2`` argument/manifest errors (stdout empty). This module
normalizes that into :class:`MoneyPrinterVideoResult` with top-level ``status``
(``"succeeded"``/``"failed"``), ``task_id`` and ``result`` (the first final MP4
path, or ``None``), embeds the parsed stdout payload under ``summary``, and
never surfaces raw stderr to the caller.

ffmpeg preflight
----------------

:func:`preflight_ffmpeg` validates the system ``ffmpeg`` binary *before* any
child process is spawned: resolution order is (1) the caller-provided
``ffmpeg_path``, (2) the ``MONEYPRINTER_FFMPEG`` environment variable, (3) the
first ``ffmpeg`` on ``PATH``. The resolved executable must exist, be a regular
file, and be executable, and ``ffmpeg -version`` must exit ``0`` within the
probe timeout. ffmpeg is required because MoneyPrinterTurbo renders and muxes
video through it. A preflight failure raises :class:`MoneyPrinterPreflightError`
and the CLI is never spawned.

Artifact lineage
----------------

On every execution that produced at least one MP4, the final videos (and the
custom audio file, when one was supplied) are hashed with
:func:`artifact_lineage.file_content_digest` and recorded in the Studio's
artifact lineage store (:class:`artifact_lineage.ArtifactRegistry`) with
``source_kind="moneyprinter-turbo"``, ``trust="unverified"``, and
``confidentiality="internal"``. Videos derived from a custom audio file link
that audio file as a ``derived_from`` parent, so the Studio tracks the full
generation trail. ``task_id`` from the CLI is propagated to every provenance
entry. If lineage recording fails, the video result is still returned and the
failure is reported on ``lineage_error`` — lineage failure never rewrites the
outcome of the generation itself.

Security model
--------------

- No shell; ``asyncio.create_subprocess_exec`` with an explicit argv tuple.
- ``video_subject`` rejects control characters, is length-capped, and is passed
  as ``--video-subject=<value>`` (never as the next bare token).
- ``custom_audio_file`` must exist, be a regular file with an allowed audio
  extension, and be at most 512 MiB.
- stdout/stderr are read with hard byte caps; overflow kills the drain loop,
  and the child is told nothing new (output is discarded beyond the cap).
- A hard timeout (default 1800s, capped at 3600s) kills the child on expiry and
  returns ``status="failed"``.
- The child environment is a minimal allowlist (PATH, HOME, TMPDIR/LANG and
  platform equivalents) plus caller-supplied ``extra_env`` entries; the
  Studio's own environment — including any cloud provider credentials — is not
  inherited by the child.
- Lineage trust is recorded as ``unverified``: bytes produced by an external
  pipeline are described as they are, never promoted.

What this module is not
-----------------------

It is a contract module, not an HTTP route and not a built-in approval-gated
tool registration. Calling it still requires the caller (e.g. the Studio
orchestrator) to hold the appropriate authorization for running a local
subprocess; it deliberately does not bypass the Studio's admission boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

import artifact_lineage

TOOL_NAME = "moneyprinter_video_generate"
TOOL_DESCRIPTION = (
    "Generate a short-form video with a local MoneyPrinterTurbo checkout "
    "(`uv run python cli.py`). Runs the full pipeline (script, voiceover, "
    "materials, render) and returns the final MP4 path. The output is recorded "
    "in the Studio artifact lineage store. Requires a configured MoneyPrinterTurbo "
    "project directory, its LLM/material provider keys, and ffmpeg on the host."
)

_SCHEMA_ID = "https://m5-agent-studio.local/schemas/moneyprinter-video-params.json"
_MAX_SUBJECT_CHARS = 1000
_MAX_AUDIO_BYTES = 512 * 1024 * 1024
_MAX_STDOUT_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 1024 * 1024
_MAX_EXTRA_ENV_VARS = 64
_MAX_EXTRA_ENV_VALUE_CHARS = 4096
_ERROR_TEXT_LIMIT = 500
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARACTERS = frozenset(chr(code) for code in (*range(0, 32), 127))

# Keep in sync with MoneyPrinterTurbo's cli.py `_CUSTOM_AUDIO_EXTENSIONS`.
CUSTOM_AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})

VIDEO_ASPECTS = ("9:16", "16:9")
DEFAULT_ASPECT = "9:16"

VIDEO_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": _SCHEMA_ID,
    "title": "MoneyPrinterTurbo VideoParams (Studio tool subset)",
    "description": (
        "Structured input for the MoneyPrinterTurbo video-generation tool. Mirrors the "
        "`VideoParams` fields the tool supports: video_subject, video_aspect and an "
        "optional custom_audio_file. Unknown fields are rejected."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["video_subject"],
    "properties": {
        "video_subject": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_SUBJECT_CHARS,
            "description": "Video topic or keyword; passed as --video-subject to the MoneyPrinterTurbo CLI.",
        },
        "video_aspect": {
            "type": "string",
            "enum": list(VIDEO_ASPECTS),
            "default": DEFAULT_ASPECT,
            "description": "Output aspect ratio: 9:16 (portrait, default) or 16:9 (landscape).",
        },
        "custom_audio_file": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional path to an existing local voiceover file (.mp3, .wav, .m4a, .aac, "
                ".flac, .ogg). Skips TTS; passed as --custom-audio-file. Maximum 512 MiB."
            ),
        },
    },
}

MONEYPRINTER_VIDEO_TOOL: Mapping[str, Any] = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "input_schema": VIDEO_PARAMS_SCHEMA,
}

# Environment variables inherited by the child process. Everything else —
# including Studio provider keys — is stripped.
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


class MoneyPrinterToolError(RuntimeError):
    """Base error for the MoneyPrinterTurbo tool contract."""


class MoneyPrinterInputError(MoneyPrinterToolError, ValueError):
    """The caller supplied parameters that fail the VideoParams subset contract."""


class MoneyPrinterPreflightError(MoneyPrinterToolError):
    """A local prerequisite (ffmpeg, project checkout, uv launch) is not satisfied."""


@dataclass(frozen=True, slots=True)
class MoneyPrinterVideoRequest:
    """Validated, normalized input for one MoneyPrinterTurbo video task."""

    video_subject: str
    video_aspect: str = DEFAULT_ASPECT
    custom_audio_file: str | None = None  # resolved absolute path, or None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "video_subject": self.video_subject,
            "video_aspect": self.video_aspect,
            "custom_audio_file": self.custom_audio_file,
        }


@dataclass(frozen=True, slots=True)
class FfmpegPreflight:
    """Result of validating the system ffmpeg binary."""

    path: str
    version: str
    source: str  # "argument" | "environment" | "path"

    def to_mapping(self) -> dict[str, str]:
        return {"path": self.path, "version": self.version, "source": self.source}


@dataclass(frozen=True, slots=True)
class MoneyPrinterVideoResult:
    """Normalized outcome of one tool execution.

    ``status`` is ``"succeeded"`` only when the CLI exited ``0``, its stdout
    parsed as JSON, and at least one final MP4 path was reported. ``result``
    carries the first final MP4 path (the value the orchestration loop should
    thread into downstream stages).
    """

    status: str
    task_id: str | None
    result: str | None
    videos: tuple[str, ...]
    error: str | None
    summary: Mapping[str, Any] | None
    lineage_entry_ids: tuple[str, ...]
    lineage_error: str | None
    output_truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tool": TOOL_NAME,
            "status": self.status,
            "task_id": self.task_id,
            "result": self.result,
            "videos": list(self.videos),
            "error": self.error,
            "summary": dict(self.summary) if isinstance(self.summary, Mapping) else None,
            "lineage_entry_ids": list(self.lineage_entry_ids),
            "lineage_error": self.lineage_error,
            "output_truncated": self.output_truncated,
        }


def _contains_control_characters(value: str) -> bool:
    return any(character in _CONTROL_CHARACTERS for character in value)


def _clean_text(value: object, *, limit: int = _ERROR_TEXT_LIMIT) -> str:
    text = str(value)
    text = "".join(" " if character in _CONTROL_CHARACTERS else character for character in text)
    text = " ".join(text.split())
    return text[:limit]


def _validate_subject(value: object) -> str:
    if not isinstance(value, str):
        raise MoneyPrinterInputError("video_subject must be a string.")
    subject = value.strip()
    if not subject:
        raise MoneyPrinterInputError("video_subject must not be empty.")
    if len(subject) > _MAX_SUBJECT_CHARS:
        raise MoneyPrinterInputError(f"video_subject must be at most {_MAX_SUBJECT_CHARS} characters.")
    if _contains_control_characters(subject):
        raise MoneyPrinterInputError("video_subject must not contain control characters.")
    return subject


def _validate_aspect(value: object) -> str:
    if value is None:
        return DEFAULT_ASPECT
    if not isinstance(value, str):
        raise MoneyPrinterInputError("video_aspect must be a string.")
    aspect = value.strip()
    if aspect not in VIDEO_ASPECTS:
        raise MoneyPrinterInputError(f"video_aspect must be one of: {', '.join(VIDEO_ASPECTS)}.")
    return aspect


def _validate_custom_audio_file(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MoneyPrinterInputError("custom_audio_file must be a path string.")
    audio = value.strip()
    if not audio:
        raise MoneyPrinterInputError("custom_audio_file must not be empty when provided.")
    if _contains_control_characters(audio):
        raise MoneyPrinterInputError("custom_audio_file must not contain control characters.")
    try:
        resolved = Path(audio).expanduser().resolve(strict=True)
    except OSError as exc:
        raise MoneyPrinterInputError("custom_audio_file does not exist or cannot be resolved.") from exc
    if not resolved.is_file():
        raise MoneyPrinterInputError("custom_audio_file must be a regular file.")
    if resolved.suffix.lower() not in CUSTOM_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(CUSTOM_AUDIO_EXTENSIONS))
        raise MoneyPrinterInputError(f"custom_audio_file must use one of these extensions: {allowed}.")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise MoneyPrinterInputError("custom_audio_file cannot be stat'ed.") from exc
    if size > _MAX_AUDIO_BYTES:
        raise MoneyPrinterInputError("custom_audio_file exceeds the 512 MiB limit.")
    return str(resolved)


def validate_video_params(payload: Mapping[str, Any]) -> MoneyPrinterVideoRequest:
    """Validate a JSON-decoded payload against the VideoParams subset contract."""
    if not isinstance(payload, Mapping):
        raise MoneyPrinterInputError("video params must be a JSON object.")
    unknown = sorted(set(payload) - {"video_subject", "video_aspect", "custom_audio_file"})
    if unknown:
        raise MoneyPrinterInputError(f"unsupported video params keys: {', '.join(unknown)}.")
    if "video_subject" not in payload:
        raise MoneyPrinterInputError("video_subject is required.")
    return MoneyPrinterVideoRequest(
        video_subject=_validate_subject(payload.get("video_subject")),
        video_aspect=_validate_aspect(payload.get("video_aspect")),
        custom_audio_file=_validate_custom_audio_file(payload.get("custom_audio_file")),
    )


def build_cli_argv(request: MoneyPrinterVideoRequest) -> tuple[str, ...]:
    """Build the exact argv tuple used for the child process.

    ``--flag=value`` form is deliberate: an argument value that starts with a
    dash (e.g. a subject like ``-5% rates``) can never be re-parsed as a flag.
    """
    if not isinstance(request, MoneyPrinterVideoRequest):
        raise TypeError("request must be a MoneyPrinterVideoRequest value.")
    argv = [
        "uv",
        "run",
        "python",
        "cli.py",
        f"--video-subject={request.video_subject}",
        f"--video-aspect={request.video_aspect}",
    ]
    if request.custom_audio_file:
        argv.append(f"--custom-audio-file={request.custom_audio_file}")
    return tuple(argv)


def _resolve_project_dir(project_dir: Path | str) -> Path:
    if not isinstance(project_dir, (str, Path)):
        raise TypeError("project_dir must be a path string or Path.")
    try:
        resolved = Path(project_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise MoneyPrinterPreflightError("project_dir does not exist or cannot be resolved.") from exc
    if not resolved.is_dir():
        raise MoneyPrinterPreflightError("project_dir must be a directory containing the MoneyPrinterTurbo checkout.")
    if not (resolved / "cli.py").is_file():
        raise MoneyPrinterPreflightError("cli.py not found; project_dir must point at a MoneyPrinterTurbo checkout.")
    return resolved


def _resolve_lineage_root(lineage_store_root: Path | str | None) -> Path:
    if lineage_store_root is not None:
        return Path(lineage_store_root).expanduser()
    env_root = os.environ.get("HARNESS_STORE_DIR", "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return Path(__file__).resolve().parent / ".harnesses" / "sessions"


class FfmpegVersionProbe(Protocol):
    async def __call__(self, ffmpeg: str, timeout: float) -> tuple[int, str]: ...


async def _probe_ffmpeg_version(ffmpeg: str, timeout: float) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=64 * 1024,
        )
    except OSError as exc:
        raise MoneyPrinterPreflightError("ffmpeg was found but could not be executed.") from exc
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise MoneyPrinterPreflightError("ffmpeg -version timed out.") from None
    lines = out.decode("utf-8", "replace").splitlines()
    return int(process.returncode or 0), (lines[0] if lines else "")


async def preflight_ffmpeg(
    ffmpeg_path: Path | str | None = None,
    *,
    timeout: float = 10.0,
    probe: FfmpegVersionProbe | None = None,
) -> FfmpegPreflight:
    """Validate the system ffmpeg binary before any video work is attempted.

    Resolution order: explicit ``ffmpeg_path`` argument, then the
    ``MONEYPRINTER_FFMPEG`` environment variable, then the first ``ffmpeg`` on
    ``PATH``. Raises :class:`MoneyPrinterPreflightError` on any failure.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive.")
    if ffmpeg_path is not None and isinstance(ffmpeg_path, str) and not ffmpeg_path.strip():
        raise MoneyPrinterPreflightError("ffmpeg_path must not be an empty string.")

    if ffmpeg_path is not None:
        candidate, source = str(ffmpeg_path), "argument"
    else:
        env_candidate = os.environ.get("MONEYPRINTER_FFMPEG", "").strip()
        if env_candidate:
            candidate, source = env_candidate, "environment"
        else:
            which = shutil.which("ffmpeg")
            if which:
                candidate, source = which, "path"
            else:
                raise MoneyPrinterPreflightError(
                    "ffmpeg was not found on PATH; install ffmpeg or set MONEYPRINTER_FFMPEG."
                )

    if "\x00" in candidate:
        raise MoneyPrinterPreflightError("ffmpeg path must not contain NUL characters.")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except OSError as exc:
        raise MoneyPrinterPreflightError("ffmpeg path does not exist or cannot be resolved.") from exc
    if not resolved.is_file():
        raise MoneyPrinterPreflightError("ffmpeg path must be a regular file.")
    if not os.access(resolved, os.X_OK):
        raise MoneyPrinterPreflightError("ffmpeg path is not executable.")

    runner = probe or _probe_ffmpeg_version
    returncode, first_line = await runner(str(resolved), timeout)
    if returncode != 0:
        raise MoneyPrinterPreflightError(f"ffmpeg -version exited with code {returncode}.")
    version = _clean_text(first_line, limit=200) or "ffmpeg (version banner unavailable)"
    return FfmpegPreflight(path=str(resolved), version=version, source=source)


def _build_child_env(extra_env: Mapping[str, str] | None) -> dict[str, str]:
    env = {name: os.environ[name] for name in _CHILD_ENV_ALLOWLIST if name in os.environ}
    if extra_env:
        if not isinstance(extra_env, Mapping):
            raise TypeError("extra_env must be a mapping of environment variables.")
        if len(extra_env) > _MAX_EXTRA_ENV_VARS:
            raise MoneyPrinterInputError(f"extra_env must contain at most {_MAX_EXTRA_ENV_VARS} entries.")
        for key, value in extra_env.items():
            if not isinstance(key, str) or not _ENV_VAR_NAME.match(key):
                raise MoneyPrinterInputError(f"extra_env key {key!r} is not a valid environment variable name.")
            if not isinstance(value, str):
                raise MoneyPrinterInputError(f"extra_env value for {key!r} must be a string.")
            if len(value) > _MAX_EXTRA_ENV_VALUE_CHARS:
                raise MoneyPrinterInputError(f"extra_env value for {key!r} exceeds {_MAX_EXTRA_ENV_VALUE_CHARS} characters.")
            if _contains_control_characters(value):
                raise MoneyPrinterInputError(f"extra_env value for {key!r} must not contain control characters.")
            env[key] = value
    return env


@dataclass(frozen=True, slots=True)
class _SpawnOutcome:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > limit:
            keep = limit - (total - len(chunk))
            if keep > 0:
                chunks.append(chunk[:keep])
            while await stream.read(64 * 1024):
                # Drain so the child never blocks on a full pipe; the bytes are discarded.
                pass
            return b"".join(chunks), True
        chunks.append(chunk)


async def _spawn_and_collect(
    argv: tuple[str, ...],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> _SpawnOutcome:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
    except OSError as exc:
        raise MoneyPrinterPreflightError(
            "unable to launch `uv`; ensure uv is on PATH and the MoneyPrinterTurbo environment is installed."
        ) from exc

    async def collect() -> _SpawnOutcome:
        (stdout, stdout_truncated), (stderr, stderr_truncated), _ = await asyncio.gather(
            _read_capped(process.stdout, max_stdout_bytes),
            _read_capped(process.stderr, max_stderr_bytes),
            process.wait(),
        )
        return _SpawnOutcome(
            returncode=int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=False,
        )

    try:
        return await asyncio.wait_for(collect(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return _SpawnOutcome(
            returncode=-1,
            stdout=b"",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=True,
        )


def _parse_stdout_json(stdout: bytes) -> Mapping[str, Any] | None:
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    candidates = [text]
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        candidates.append(lines[-1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _video_paths(result_payload: Mapping[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key in ("videos", "combined_videos"):
        raw = result_payload.get(key)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str):
                candidate = item.strip()
                if candidate and candidate not in paths:
                    paths.append(candidate)
    return tuple(paths)


def _record_lineage(
    registry: artifact_lineage.ArtifactRegistry,
    *,
    videos: tuple[str, ...],
    request: MoneyPrinterVideoRequest,
    task_id: str,
) -> tuple[tuple[str, ...], str | None]:
    entry_ids: list[str] = []
    problems: list[str] = []
    audio_sha256: str | None = None
    audio_path = request.custom_audio_file
    if audio_path:
        try:
            audio_sha256 = artifact_lineage.file_content_digest(audio_path)
            audio_entry = registry.record_artifact(
                audio_sha256,
                source_kind="moneyprinter-custom-audio",
                source_ref=f"custom audio for moneyprinter-turbo task {task_id}",
                trust="unverified",
                confidentiality="internal",
                allowed_operations=("read",),
                task_id=task_id,
                size=Path(audio_path).stat().st_size,
            )
            entry_ids.append(audio_entry.entry_id)
        except (OSError, artifact_lineage.ArtifactLineageError):
            audio_sha256 = None
            problems.append("custom audio")
    for video in videos:
        try:
            sha256 = artifact_lineage.file_content_digest(video)
            size = Path(video).stat().st_size
            entry = registry.record_artifact(
                sha256,
                source_kind="moneyprinter-turbo",
                source_ref=f"moneyprinter-turbo cli task {task_id}",
                trust="unverified",
                confidentiality="internal",
                allowed_operations=("read",),
                task_id=task_id,
                size=size,
                derived_from=(audio_sha256,) if audio_sha256 else (),
            )
            entry_ids.append(entry.entry_id)
        except (OSError, artifact_lineage.ArtifactLineageError):
            problems.append(Path(video).name or "video")
    lineage_error = None
    if problems:
        lineage_error = f"artifact lineage could not be recorded for: {', '.join(problems)}"
    return tuple(entry_ids), lineage_error


def _resolve_request(payload: Mapping[str, Any] | MoneyPrinterVideoRequest) -> MoneyPrinterVideoRequest:
    if isinstance(payload, MoneyPrinterVideoRequest):
        return payload
    return validate_video_params(payload)


async def run_moneyprinter_video(
    payload: Mapping[str, Any] | MoneyPrinterVideoRequest,
    *,
    project_dir: Path | str,
    ffmpeg_path: Path | str | None = None,
    ffmpeg_timeout: float = 10.0,
    timeout_seconds: float = 1800.0,
    max_output_bytes: int = _MAX_STDOUT_BYTES,
    lineage: artifact_lineage.ArtifactRegistry | None = None,
    lineage_store_root: Path | str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> MoneyPrinterVideoResult:
    """Execute the MoneyPrinterTurbo pipeline as a local tool and record lineage.

    Order of operations: validate input, validate the project checkout, run the
    ffmpeg preflight, then (and only then) spawn the CLI. Any preflight failure
    raises before a child process exists; task failures are reported as
    ``status="failed"`` results instead of exceptions.
    """
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number.")
    if timeout_seconds > 3600:
        raise ValueError("timeout_seconds must be at most 3600.")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive.")

    request = _resolve_request(payload)
    project = _resolve_project_dir(project_dir)
    await preflight_ffmpeg(ffmpeg_path, timeout=ffmpeg_timeout)

    registry = lineage
    if registry is None:
        registry = artifact_lineage.ArtifactRegistry(_resolve_lineage_root(lineage_store_root))
    elif not isinstance(registry, artifact_lineage.ArtifactRegistry):
        raise TypeError("lineage must be an artifact_lineage.ArtifactRegistry value.")

    argv = build_cli_argv(request)
    env = _build_child_env(extra_env)
    outcome = await _spawn_and_collect(
        argv,
        cwd=str(project),
        env=env,
        timeout_seconds=float(timeout_seconds),
        max_stdout_bytes=max_output_bytes,
        max_stderr_bytes=_MAX_STDERR_BYTES,
    )

    summary = _parse_stdout_json(outcome.stdout)
    task_id_raw = summary.get("task_id") if summary else None
    task_id = _clean_text(task_id_raw, limit=120) if isinstance(task_id_raw, str) and task_id_raw.strip() else None

    result_payload = summary.get("result") if summary else None
    result_mapping = result_payload if isinstance(result_payload, Mapping) else {}
    videos = _video_paths(result_mapping)
    final_video = videos[0] if videos else None

    if outcome.timed_out:
        status = "failed"
        error = f"moneyprinter-turbo timed out after {timeout_seconds:.0f} seconds."
    elif outcome.returncode == 0:
        if summary is not None and final_video:
            status, error = "succeeded", None
        elif summary is None:
            status, error = "failed", "moneyprinter-turbo exited 0 but its stdout was not valid JSON."
        else:
            status, error = "failed", "moneyprinter-turbo exited 0 but reported no final MP4 output."
    else:
        status = "failed"
        payload_error = result_mapping.get("error") if result_mapping else None
        mp_error = _clean_text(payload_error) if isinstance(payload_error, str) and payload_error.strip() else None
        error = mp_error or f"moneyprinter-turbo exited with code {outcome.returncode}."

    entry_ids: tuple[str, ...] = ()
    lineage_error: str | None = None
    if videos:
        try:
            entry_ids, lineage_error = await asyncio.to_thread(
                _record_lineage,
                registry,
                videos=videos,
                request=request,
                task_id=task_id or "",
            )
        except (OSError, artifact_lineage.ArtifactLineageError) as exc:
            lineage_error = f"artifact lineage recording failed ({type(exc).__name__})."

    return MoneyPrinterVideoResult(
        status=status,
        task_id=task_id,
        result=final_video if status == "succeeded" else None,
        videos=videos,
        error=error,
        summary=summary,
        lineage_entry_ids=entry_ids,
        lineage_error=lineage_error,
        output_truncated=outcome.stdout_truncated or outcome.stderr_truncated,
    )


__all__ = [
    "CUSTOM_AUDIO_EXTENSIONS",
    "DEFAULT_ASPECT",
    "FfmpegPreflight",
    "MONEYPRINTER_VIDEO_TOOL",
    "MoneyPrinterInputError",
    "MoneyPrinterPreflightError",
    "MoneyPrinterToolError",
    "MoneyPrinterVideoRequest",
    "MoneyPrinterVideoResult",
    "TOOL_NAME",
    "VIDEO_ASPECTS",
    "VIDEO_PARAMS_SCHEMA",
    "build_cli_argv",
    "preflight_ffmpeg",
    "run_moneyprinter_video",
    "validate_video_params",
]
