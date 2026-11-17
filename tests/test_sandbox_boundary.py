from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import pytest

import sandbox_boundary
from sandbox_boundary import (
    SandboxBoundaryError,
    build_container_invocation,
    confined_path,
    qualification_host_fingerprint,
    sandbox_command,
    validate_workspace,
)


def test_confined_path_accepts_children_and_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = workspace / "src"
    child.mkdir()
    assert confined_path(workspace, "src") == child.resolve()
    with pytest.raises(SandboxBoundaryError, match="escapes"):
        confined_path(workspace, "../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxBoundaryError, match="symlink"):
        confined_path(workspace, "link/file.txt")


def test_workspace_validation_rejects_broad_secret_and_oversized_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(SandboxBoundaryError, match="secret"):
        validate_workspace(workspace)

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "file").write_text("data", encoding="utf-8")
    with pytest.raises(SandboxBoundaryError, match="entry scan"):
        validate_workspace(clean, max_entries=0)


def test_sandbox_command_enforces_isolation_for_docker_and_podman(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for runtime in ("docker", "podman"):
        args = sandbox_command(workspace, "printf ok", image="trusted/image:1", runtime=runtime, container_name="test-box")
        assert args[:2] == [runtime, "run"]
        for required in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g", "--cpus=2"):
            assert required in args
        assert "type=bind" in args[args.index("--mount") + 1]
        assert args[-3:] == ["sh", "-lc", "printf ok"]


def test_invalid_runtime_and_container_name_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SandboxBoundaryError, match="runtime"):
        sandbox_command(workspace, "true", image="image", runtime="other")
    with pytest.raises(SandboxBoundaryError, match="container_name"):
        sandbox_command(workspace, "true", image="image", container_name="bad name")


def test_build_container_invocation_matches_app_sandbox_command_and_rejects_unsafe_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as studio

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "harness-sessions")
    monkeypatch.setattr(studio.shutil, "which", lambda _rt: "/usr/bin/docker")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_IMAGE", "python:3.12-slim")

    direct = build_container_invocation(
        workspace.resolve(),
        "print('ok')",
        runtime_path="/usr/bin/docker",
        image="python:3.12-slim",
        python_code=True,
        container_name="m5-unified-box",
    )
    via_app = studio._sandbox_command(
        workspace,
        "print('ok')",
        python_code=True,
        container_name="m5-unified-box",
    )
    assert via_app == direct
    for flag in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--ipc=private",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
    ):
        assert flag in direct

    comma_ws = tmp_path / "bad,ws"
    comma_ws.mkdir()
    with pytest.raises(SandboxBoundaryError, match="comma"):
        build_container_invocation(
            comma_ws, "true", runtime_path="/usr/bin/docker", image="python:3.12-slim"
        )
    with pytest.raises(SandboxBoundaryError, match="STUDIO_SANDBOX_IMAGE"):
        build_container_invocation(
            workspace, "true", runtime_path="/usr/bin/docker", image="--privileged"
        )



# --- execution-mode gate: no path from "sandbox" to the host -------------------


def _run_untrusted(tmp_path: Path, command: str) -> tuple[object, object]:
    import asyncio
    import app as studio

    return asyncio.run(studio._execute_limited(tmp_path, command, timeout=10, max_output_bytes=4096))


def test_disabled_mode_refuses_without_running_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "disabled")
    marker = tmp_path / "EXECUTED"
    with pytest.raises(PermissionError, match="Code execution is disabled"):
        _run_untrusted(tmp_path, f"touch {marker}")
    assert not marker.exists()


def test_sandbox_mode_without_a_runtime_refuses_instead_of_falling_back_to_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release constraint is "unverified isolation stays unavailable".

    A missing container runtime is the moment where a convenient fallback would be
    tempting: the command is already admitted, the workspace exists, and spawning it
    on the host would work. It must not.
    """
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_RUNTIME", "definitely-not-a-container-runtime")
    marker = tmp_path / "EXECUTED"
    with pytest.raises(RuntimeError, match="not installed or not on PATH"):
        _run_untrusted(tmp_path, f"touch {marker}")
    assert not marker.exists()


def test_sandbox_mode_never_takes_the_host_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a runnable argv, sandbox mode must go through the container shape."""
    import asyncio
    import sys

    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "_sandbox_command", lambda *a, **k: [sys.executable, "-c", "pass"])

    def _host_shell_used(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sandbox mode used the host shell")

    monkeypatch.setattr(studio.asyncio, "create_subprocess_shell", _host_shell_used)

    async def _go() -> tuple[object, object]:
        proc, container = await studio._start_code_process(tmp_path, "echo hi")
        await proc.communicate()
        return proc, container

    proc, container = asyncio.run(_go())
    assert container is not None, "sandbox mode must return the container name it created"
    assert getattr(proc, "returncode", None) == 0


def test_host_mode_without_the_explicit_opt_in_degrades_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent gates: the mode *and* the opt-in, both fail closed."""
    import app as studio

    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", False)
    assert studio._execution_mode() == "disabled"

    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    assert studio._execution_mode() == "host"


def test_settings_reject_host_mode_without_the_opt_in() -> None:
    from app import ConfigurationError, load_settings

    with pytest.raises(ConfigurationError, match="STUDIO_ALLOW_HOST_EXECUTION"):
        load_settings({"STUDIO_EXECUTION_MODE": "host"}, base_dir=Path("."))


# AP-007-F3.  A machine description written by this test; it describes no real machine.
_F3_MACHINE: dict[str, Any] = {
    "os": "FixtureOS",
    "machine": "fixture64",
    "cpu_model": "Fixture CPU - not a real machine",
    "cpu_count": 8,
    "total_memory_bytes": 17_179_869_184,
}


def test_ap007_f3_host_fingerprint_identifies_the_machine_a_report_describes() -> None:
    digest = qualification_host_fingerprint(_F3_MACHINE)
    # A known answer pins the recipe: SHA-256 over the canonical JSON of the five values.
    assert digest == "sha256:d0e6477327db02803c288631c5bd3ad085ce68fd8b898cfe7e66957af4ed5440"
    canonical = json.dumps(_F3_MACHINE, sort_keys=True, separators=(",", ":"))
    assert digest == "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)

    # What changes while the host stays the same is not part of the identity, and neither is key order.
    noisy = {
        **_F3_MACHINE,
        "git_revision": "1" * 40,
        "os_release": "9.9.9",
        "os_version": "9",
        "python": "3.13.0",
        "total_memory_gib": 16.0,
        "memory": {"swap_used_bytes": 123_456},
        "hostname": "any-name",
    }
    assert qualification_host_fingerprint(noisy) == digest
    assert qualification_host_fingerprint(dict(reversed(list(_F3_MACHINE.items())))) == digest
    assert qualification_host_fingerprint({**_F3_MACHINE, "os": "  FixtureOS\n"}) == digest
    assert qualification_host_fingerprint(MappingProxyType(_F3_MACHINE)) == digest

    # The digested bytes are written out here: sorted keys, no spaces, text as UTF-8 and not escaped.
    accented = {**_F3_MACHINE, "cpu_model": "Fixture CPU \u00fc\u4e2d"}
    written_out = (
        '{"cpu_count":8,"cpu_model":"Fixture CPU \u00fc\u4e2d","machine":"fixture64",'
        '"os":"FixtureOS","total_memory_bytes":17179869184}'
    )
    assert "\\u" not in written_out
    assert qualification_host_fingerprint(accented) == "sha256:" + hashlib.sha256(written_out.encode("utf-8")).hexdigest()

    # Each of the five values matters, and each is compared exactly.
    different = (
        ("os", "OtherOS"),
        ("os", "fixtureos"),
        ("machine", "other64"),
        ("cpu_model", "Fixture CPU - another one"),
        ("cpu_count", 10),
        ("total_memory_bytes", 34_359_738_368),
    )
    seen = {digest}
    for key, other in different:
        changed = qualification_host_fingerprint({**_F3_MACHINE, key: other})
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", changed), key
        assert changed not in seen, key
        seen.add(changed)


def test_ap007_f3_a_description_that_identifies_no_host_has_no_fingerprint() -> None:
    for key in _F3_MACHINE:
        without = {name: value for name, value in _F3_MACHINE.items() if name != key}
        assert qualification_host_fingerprint(without) == "", key
    malformed: tuple[tuple[str, Any], ...] = (
        ("os", ""),
        ("os", "   "),
        ("os", None),
        ("os", 7),
        ("machine", "x" * 201),
        ("machine", b"fixture64"),
        ("cpu_model", "two\nlines"),
        ("cpu_model", ["Fixture CPU"]),
        ("cpu_count", 0),
        ("cpu_count", -8),
        ("cpu_count", True),
        ("cpu_count", 8.0),
        ("cpu_count", "8"),
        ("cpu_count", None),
        ("total_memory_bytes", 0),
        ("total_memory_bytes", False),
        ("total_memory_bytes", 1.7e10),
        ("total_memory_bytes", "17179869184"),
        ("total_memory_bytes", None),
    )
    for key, bad in malformed:
        assert qualification_host_fingerprint({**_F3_MACHINE, key: bad}) == "", (key, bad)
    not_a_description: tuple[Any, ...] = (None, "", "FixtureOS", 7, [_F3_MACHINE], tuple(_F3_MACHINE.items()))
    for value in not_a_description:
        assert qualification_host_fingerprint(value) == ""


def test_ap007_f3_a_report_of_the_qualification_suite_states_its_time_and_its_machine() -> None:
    """The keys the provenance is read from are the keys ``scripts/qualify_environment.py`` writes.

    The suite assembles a record with no checks; nothing is probed, installed or
    qualified.  The machine description is the one the suite reads on this host.
    """
    from scripts import qualify_environment as qe

    described = qe.environment_fingerprint()
    record = qe.build_record(
        qe.Recorder(stream=io.StringIO()),
        fingerprint=described,
        suites=[],
        runtime_name="",
        runtime_path="",
        image="python:3.12-slim",
        invocation=["qualify_environment.py"],
    )
    time_stated, host = sandbox_boundary._sb_report_provenance(record)
    assert time_stated is True
    assert host == qualification_host_fingerprint(record["fingerprint"])
    # A second reading on the same host gives the same fingerprint although its memory snapshot differs.
    assert qualification_host_fingerprint(qe.environment_fingerprint()) == host
    # The suite never invents a value: a host it cannot describe completely has no fingerprint.
    def _usable_text(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        return 0 < len(text) <= 200 and not any(ord(char) < 32 or ord(char) == 127 for char in text)

    complete = all(
        _usable_text(described.get(key)) for key in sandbox_boundary.QUALIFICATION_HOST_TEXT_FIELDS
    ) and all(
        isinstance(described.get(key), int) and not isinstance(described.get(key), bool) and described[key] > 0
        for key in sandbox_boundary.QUALIFICATION_HOST_COUNT_FIELDS
    )
    assert (host != "") is complete

    # A report without the time keys, or without the machine block, states neither.
    undated = {key: value for key, value in record.items() if key != "generated_at_utc"}
    assert sandbox_boundary._sb_report_provenance(undated) == (False, host)
    undescribed = {key: value for key, value in record.items() if key != "fingerprint"}
    assert sandbox_boundary._sb_report_provenance(undescribed) == (True, "")
    for not_a_report in (None, "", [], 7):
        assert sandbox_boundary._sb_report_provenance(not_a_report) == (False, "")  # type: ignore[arg-type]
