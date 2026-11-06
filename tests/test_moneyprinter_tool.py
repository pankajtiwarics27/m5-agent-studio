from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import artifact_lineage
import moneyprinter_tool
from moneyprinter_tool import (
    MONEYPRINTER_VIDEO_TOOL,
    MoneyPrinterInputError,
    MoneyPrinterPreflightError,
    MoneyPrinterVideoRequest,
    build_cli_argv,
    preflight_ffmpeg,
    run_moneyprinter_video,
    validate_video_params,
)


# --------------------------------------------------------------------------- schema


def test_tool_descriptor_publishes_video_params_subset_schema() -> None:
    assert MONEYPRINTER_VIDEO_TOOL["name"] == "moneyprinter_video_generate"
    assert MONEYPRINTER_VIDEO_TOOL["description"]
    schema = MONEYPRINTER_VIDEO_TOOL["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["video_subject"]
    assert schema["properties"]["video_aspect"]["enum"] == ["9:16", "16:9"]
    assert schema["properties"]["video_aspect"]["default"] == "9:16"
    assert set(schema["properties"]) == {"video_subject", "video_aspect", "custom_audio_file"}


def test_validate_video_params_accepts_minimal_payload() -> None:
    request = validate_video_params({"video_subject": "How M5 chips change AI"})
    assert request.video_subject == "How M5 chips change AI"
    assert request.video_aspect == "9:16"
    assert request.custom_audio_file is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"video_aspect": "9:16"},
        {"video_subject": "   "},
        {"video_subject": ""},
        {"video_subject": 123},
        {"video_subject": "x" * 1001},
        {"video_subject": "bad\x00subject"},
        {"video_subject": "bad\x1b[31msubject"},
        {"video_subject": "ok", "video_aspect": "1:1"},  # MPT supports 1:1; the Studio contract does not
        {"video_subject": "ok", "video_aspect": "4:5"},
        {"video_subject": "ok", "video_aspect": 9},
        {"video_subject": "ok", "surprise": "nope"},
    ],
    ids=[
        "missing-subject",
        "missing-subject-with-aspect",
        "blank-subject",
        "empty-subject",
        "non-string-subject",
        "subject-too-long",
        "nul-byte",
        "ansi-escape",
        "aspect-111",
        "aspect-45",
        "aspect-non-string",
        "unknown-key",
    ],
)
def test_validate_video_params_rejects_bad_payloads(payload: dict[str, Any]) -> None:
    with pytest.raises(MoneyPrinterInputError):
        validate_video_params(payload)


def test_validate_video_params_requires_mapping() -> None:
    with pytest.raises(MoneyPrinterInputError):
        validate_video_params("not-a-mapping")  # type: ignore[arg-type]


def test_custom_audio_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(MoneyPrinterInputError, match="does not exist"):
        validate_video_params({"video_subject": "s", "custom_audio_file": str(tmp_path / "missing.mp3")})


def test_custom_audio_file_extension_is_restricted(tmp_path: Path) -> None:
    audio = tmp_path / "voiceover.txt"
    audio.write_bytes(b"fake")
    with pytest.raises(MoneyPrinterInputError, match="extensions"):
        validate_video_params({"video_subject": "s", "custom_audio_file": str(audio)})


def test_custom_audio_file_must_be_a_file(tmp_path: Path) -> None:
    with pytest.raises(MoneyPrinterInputError, match="regular file"):
        validate_video_params({"video_subject": "s", "custom_audio_file": str(tmp_path)})


def test_custom_audio_file_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "voiceover.mp3"
    audio.write_bytes(b"0123456789")
    monkeypatch.setattr(moneyprinter_tool, "_MAX_AUDIO_BYTES", 8)
    with pytest.raises(MoneyPrinterInputError, match="512 MiB"):
        validate_video_params({"video_subject": "s", "custom_audio_file": str(audio)})


def test_custom_audio_file_resolves_to_absolute_path(tmp_path: Path) -> None:
    audio = tmp_path / "voiceover.wav"
    audio.write_bytes(b"RIFF-fake")
    request = validate_video_params({"video_subject": "s", "custom_audio_file": str(audio)})
    assert request.custom_audio_file == str(audio.resolve())


# --------------------------------------------------------------------------- argv/env


def test_build_cli_argv_uses_equals_form_and_fixed_prefix(tmp_path: Path) -> None:
    request = MoneyPrinterVideoRequest(video_subject="-5% rates explained", video_aspect="16:9")
    argv = build_cli_argv(request)
    assert argv[:4] == ("uv", "run", "python", "cli.py")
    assert "--video-subject=-5% rates explained" in argv
    assert "--video-aspect=16:9" in argv
    assert all(not arg.startswith("--custom") for arg in argv)


def test_build_cli_argv_includes_custom_audio_file(tmp_path: Path) -> None:
    audio = tmp_path / "voice.m4a"
    audio.write_bytes(b"fake")
    request = validate_video_params({"video_subject": "s", "custom_audio_file": str(audio)})
    argv = build_cli_argv(request)
    assert f"--custom-audio-file={audio.resolve()}" in argv


def test_child_env_strips_unrelated_studio_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("M5_TEST_SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = moneyprinter_tool._build_child_env(None)
    assert "M5_TEST_SECRET_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_child_env_extra_env_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    env = moneyprinter_tool._build_child_env({"MPT_PROXY": "http://127.0.0.1:9"})
    assert env["MPT_PROXY"] == "http://127.0.0.1:9"
    with pytest.raises(MoneyPrinterInputError):
        moneyprinter_tool._build_child_env({"BAD KEY": "x"})
    with pytest.raises(MoneyPrinterInputError):
        moneyprinter_tool._build_child_env({"GOOD_KEY": "bad\x00value"})
    with pytest.raises(MoneyPrinterInputError):
        moneyprinter_tool._build_child_env({"OK": 1})  # type: ignore[dict-item]


# --------------------------------------------------------------------------- ffmpeg preflight


def _make_executable(path: Path, content: bytes = b"#!/bin/sh\necho ffmpeg version fixture 6.1\nexit 0\n") -> Path:
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def test_preflight_ffmpeg_with_explicit_path(tmp_path: Path) -> None:
    fake = _make_executable(tmp_path / "ffmpeg")
    probe_calls: list[tuple[str, float]] = []

    async def probe(path: str, timeout: float) -> tuple[int, str]:
        probe_calls.append((path, timeout))
        return 0, "ffmpeg version 6.1 fixture"

    preflight = asyncio.run(preflight_ffmpeg(fake, probe=probe))
    assert preflight.path == str(fake.resolve())
    assert preflight.version == "ffmpeg version 6.1 fixture"
    assert preflight.source == "argument"
    assert probe_calls == [(str(fake.resolve()), 10.0)]


def test_preflight_ffmpeg_rejects_missing_and_non_executable(tmp_path: Path) -> None:
    with pytest.raises(MoneyPrinterPreflightError):
        asyncio.run(preflight_ffmpeg(tmp_path / "nope"))
    not_exec = tmp_path / "ffmpeg-notexec"
    not_exec.write_bytes(b"#!/bin/sh\n")
    not_exec.chmod(0o644)
    with pytest.raises(MoneyPrinterPreflightError, match="not executable"):
        asyncio.run(preflight_ffmpeg(not_exec))


def test_preflight_ffmpeg_rejects_empty_string(tmp_path: Path) -> None:
    with pytest.raises(MoneyPrinterPreflightError):
        asyncio.run(preflight_ffmpeg(""))


def test_preflight_ffmpeg_probe_failure_raises(tmp_path: Path) -> None:
    fake = _make_executable(tmp_path / "ffmpeg")

    async def probe(_path: str, _timeout: float) -> tuple[int, str]:
        return 3, ""

    with pytest.raises(MoneyPrinterPreflightError, match="code 3"):
        asyncio.run(preflight_ffmpeg(fake, probe=probe))


def test_preflight_ffmpeg_uses_environment_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_executable(tmp_path / "ffmpeg")
    monkeypatch.setenv("MONEYPRINTER_FFMPEG", str(fake))

    async def probe(_path: str, _timeout: float) -> tuple[int, str]:
        return 0, "ffmpeg version env-fixture"

    preflight = asyncio.run(preflight_ffmpeg(probe=probe))
    assert preflight.source == "environment"


def test_preflight_ffmpeg_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _make_executable(tmp_path / "ffmpeg")
    monkeypatch.delenv("MONEYPRINTER_FFMPEG", raising=False)
    monkeypatch.setattr(moneyprinter_tool.shutil, "which", lambda _name: str(fake))

    async def probe(_path: str, _timeout: float) -> tuple[int, str]:
        return 0, "ffmpeg version path-fixture"

    preflight = asyncio.run(preflight_ffmpeg(probe=probe))
    assert preflight.source == "path"


def test_preflight_ffmpeg_not_found_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MONEYPRINTER_FFMPEG", raising=False)
    monkeypatch.setattr(moneyprinter_tool.shutil, "which", lambda _name: None)
    with pytest.raises(MoneyPrinterPreflightError, match="not found"):
        asyncio.run(preflight_ffmpeg())


def test_preflight_ffmpeg_real_probe_parses_version_banner(tmp_path: Path) -> None:
    # A stand-in ffmpeg that answers `-version` like the real binary would; this
    # exercises the default async probe (no injected runner) without requiring ffmpeg.
    fake = _make_executable(tmp_path / "ffmpeg", b"#!/bin/sh\necho ffmpeg version 6.1.1-test-fixture\nexit 0\n")
    preflight = asyncio.run(preflight_ffmpeg(fake, timeout=10.0))
    assert preflight.version == "ffmpeg version 6.1.1-test-fixture"
    assert preflight.source == "argument"


def test_preflight_ffmpeg_real_probe_times_out(tmp_path: Path) -> None:
    fake = _make_executable(tmp_path / "ffmpeg", b"#!/bin/sh\nsleep 5\n")
    with pytest.raises(MoneyPrinterPreflightError, match="timed out"):
        asyncio.run(preflight_ffmpeg(fake, timeout=0.2))


# --------------------------------------------------------------------------- execution


def _project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "MoneyPrinterTurbo"
    project.mkdir()
    (project / "cli.py").write_text("# fixture cli\n", encoding="utf-8")
    return project


def _mp4(tmp_path: Path, name: str = "final-1.mp4") -> Path:
    video = tmp_path / name
    video.write_bytes(b"\x00\x00\x00\x20ftypmp42-fixture-bytes")
    return video


def _ok_outcome(stdout_payload: Mapping[str, Any]) -> moneyprinter_tool._SpawnOutcome:
    return moneyprinter_tool._SpawnOutcome(
        returncode=0,
        stdout=json.dumps(stdout_payload).encode(),
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
    )


def _fake_spawn(outcome: moneyprinter_tool._SpawnOutcome, seen: dict[str, Any] | None = None):
    async def fake_spawn(argv: tuple[str, ...], *, cwd: str, env: Mapping[str, str], timeout_seconds: float, max_stdout_bytes: int, max_stderr_bytes: int):
        if seen is not None:
            seen.update(argv=argv, cwd=cwd, env=dict(env), timeout_seconds=timeout_seconds, max_stdout_bytes=max_stdout_bytes, max_stderr_bytes=max_stderr_bytes)
        return outcome

    return fake_spawn


def _patch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_preflight(*_args: Any, **_kwargs: Any) -> moneyprinter_tool.FfmpegPreflight:
        return moneyprinter_tool.FfmpegPreflight(path="/fake/ffmpeg", version="ffmpeg fixture", source="argument")

    monkeypatch.setattr(moneyprinter_tool, "preflight_ffmpeg", fake_preflight)


def test_successful_run_normalizes_summary_and_records_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    video = _mp4(tmp_path)
    outcome = _ok_outcome({"task_id": "abc-123", "result": {"state": "complete", "videos": [str(video)]}})
    seen: dict[str, Any] = {}
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome, seen))

    result = asyncio.run(
        run_moneyprinter_video(
            {"video_subject": "unified memory demo", "video_aspect": "16:9"},
            project_dir=project,
            lineage_store_root=tmp_path / "lineage",
        )
    )

    assert result.succeeded
    assert result.status == "succeeded"
    assert result.task_id == "abc-123"
    assert result.result == str(video)
    assert result.videos == (str(video),)
    assert result.error is None
    assert result.summary is not None and result.summary["task_id"] == "abc-123"
    assert seen["argv"][:4] == ("uv", "run", "python", "cli.py")
    assert "--video-subject=unified memory demo" in seen["argv"]
    assert "--video-aspect=16:9" in seen["argv"]
    assert seen["cwd"] == str(project.resolve())

    registry = artifact_lineage.ArtifactRegistry(tmp_path / "lineage")
    digest = artifact_lineage.file_content_digest(video)
    entries = registry.entries_for(digest)
    assert len(entries) == 1
    assert entries[0].source_kind == "moneyprinter-turbo"
    assert entries[0].task_id == "abc-123"
    assert entries[0].trust == "unverified"
    assert result.lineage_entry_ids


def test_successful_run_links_custom_audio_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    video = _mp4(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"fake-mp3-bytes")
    outcome = _ok_outcome({"task_id": "task-9", "result": {"videos": [str(video)]}})
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(
        run_moneyprinter_video(
            {"video_subject": "s", "custom_audio_file": str(audio)},
            project_dir=project,
            lineage_store_root=tmp_path / "lineage",
        )
    )

    assert result.succeeded
    registry = artifact_lineage.ArtifactRegistry(tmp_path / "lineage")
    audio_digest = artifact_lineage.file_content_digest(audio)
    video_digest = artifact_lineage.file_content_digest(video)
    audio_entries = registry.entries_for(audio_digest)
    video_entries = registry.entries_for(video_digest)
    assert [entry.source_kind for entry in audio_entries] == ["moneyprinter-custom-audio"]
    assert video_entries[0].derived_from == (audio_digest,)
    assert len(result.lineage_entry_ids) == 2


def test_failed_cli_run_reports_payload_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    outcome = moneyprinter_tool._SpawnOutcome(
        returncode=1,
        stdout=json.dumps({"task_id": "t-1", "result": {"state": "failed", "error": "materials exploded"}}).encode(),
        stderr=b"secret stderr logs",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
    )
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert result.error == "materials exploded"
    assert result.result is None
    assert result.task_id == "t-1"
    assert result.lineage_entry_ids == ()


def test_failed_cli_run_records_partial_videos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    video = _mp4(tmp_path, "partial.mp4")
    outcome = moneyprinter_tool._SpawnOutcome(
        returncode=1,
        stdout=json.dumps({"task_id": "t-2", "result": {"state": "failed", "videos": [str(video)], "error": "encode failed"}}).encode(),
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
    )
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert result.result is None
    assert result.videos == (str(video),)
    assert result.lineage_entry_ids, "partial outputs must still enter the lineage trail"


def test_cli_exit_code_two_with_no_json_fails_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    outcome = moneyprinter_tool._SpawnOutcome(returncode=2, stdout=b"", stderr=b"usage: cli.py ...", stdout_truncated=False, stderr_truncated=False, timed_out=False)
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert result.error == "moneyprinter-turbo exited with code 2."
    assert result.summary is None
    assert result.task_id is None


def test_cli_success_without_mp4_fails_instead_of_claiming_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    outcome = _ok_outcome({"task_id": "t-3", "result": {"state": "complete", "script": "only a script"}})
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert "no final MP4" in (result.error or "")
    assert result.result is None


def test_cli_success_with_non_json_stdout_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    outcome = moneyprinter_tool._SpawnOutcome(returncode=0, stdout=b"not json", stderr=b"", stdout_truncated=False, stderr_truncated=False, timed_out=False)
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert "not valid JSON" in (result.error or "")


def test_cli_timeout_returns_failed_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    outcome = moneyprinter_tool._SpawnOutcome(returncode=-1, stdout=b"", stderr=b"", stdout_truncated=False, stderr_truncated=False, timed_out=True)
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, timeout_seconds=5, lineage_store_root=tmp_path / "lin"))

    assert result.status == "failed"
    assert "timed out" in (result.error or "")


def test_output_truncated_flag_is_propagated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    video = _mp4(tmp_path)
    outcome = moneyprinter_tool._SpawnOutcome(
        returncode=0,
        stdout=json.dumps({"task_id": "t", "result": {"videos": [str(video)]}}).encode(),
        stderr=b"",
        stdout_truncated=False,
        stderr_truncated=True,
        timed_out=False,
    )
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))
    assert result.output_truncated is True


def test_preflight_failure_prevents_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_dir(tmp_path)

    async def fail_preflight(*_args: Any, **_kwargs: Any):
        raise MoneyPrinterPreflightError("no ffmpeg")

    async def forbidden_spawn(*_args: Any, **_kwargs: Any):
        raise AssertionError("child process must not be spawned when preflight fails")

    monkeypatch.setattr(moneyprinter_tool, "preflight_ffmpeg", fail_preflight)
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", forbidden_spawn)

    with pytest.raises(MoneyPrinterPreflightError):
        asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project))


def test_input_failure_prevents_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_dir(tmp_path)

    async def forbidden_spawn(*_args: Any, **_kwargs: Any):
        raise AssertionError("child process must not be spawned for invalid input")

    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", forbidden_spawn)

    with pytest.raises(MoneyPrinterInputError):
        asyncio.run(run_moneyprinter_video({"video_subject": "x", "video_aspect": "1:1"}, project_dir=project))


def test_project_dir_must_exist_and_contain_cli(tmp_path: Path) -> None:
    with pytest.raises(MoneyPrinterPreflightError):
        asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=tmp_path / "missing"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(MoneyPrinterPreflightError, match="cli.py"):
        asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=empty))


def test_timeout_seconds_bounds(tmp_path: Path) -> None:
    project = _project_dir(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, timeout_seconds=0))
    with pytest.raises(ValueError):
        asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, timeout_seconds=3601))


def test_lineage_failure_is_reported_not_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_preflight(monkeypatch)
    project = _project_dir(tmp_path)
    video = _mp4(tmp_path)
    outcome = _ok_outcome({"task_id": "t-4", "result": {"videos": [str(video)]}})
    monkeypatch.setattr(moneyprinter_tool, "_spawn_and_collect", _fake_spawn(outcome))

    def broken_digest(_path: Any) -> str:
        raise OSError("disk unreadable")

    monkeypatch.setattr(artifact_lineage, "file_content_digest", broken_digest)

    result = asyncio.run(run_moneyprinter_video({"video_subject": "s"}, project_dir=project, lineage_store_root=tmp_path / "lin"))

    assert result.status == "succeeded"  # the generation outcome stands
    assert result.lineage_error is not None
    assert video.name in result.lineage_error
    assert result.lineage_entry_ids == ()


def test_spawn_and_collect_enforces_hard_timeout(tmp_path: Path) -> None:
    outcome = asyncio.run(
        moneyprinter_tool._spawn_and_collect(
            (sys.executable, "-c", "import time; time.sleep(5)"),
            cwd=str(tmp_path),
            env={"PATH": os.environ.get("PATH", "")},
            timeout_seconds=0.2,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )
    )
    assert outcome.timed_out is True


def test_read_capped_truncates_oversized_streams() -> None:
    async def run() -> tuple[bytes, bool]:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 200)
        reader.feed_eof()
        return await moneyprinter_tool._read_capped(reader, 100)

    data, truncated = asyncio.run(run())
    assert truncated is True
    assert len(data) == 100


def test_child_env_allowlist_keeps_path_and_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    env = moneyprinter_tool._build_child_env(None)
    assert env.get("PATH") == "/bin"
    assert env.get("TMPDIR") == "/tmp"
    assert "OPENAI_API_KEY" not in env
