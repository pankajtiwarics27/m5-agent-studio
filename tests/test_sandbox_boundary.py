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


# AP-007-R1.  The server observer is the missing host value in the isolation proof.
_R1_HOST_FIELDS = ("os", "machine", "cpu_model", "cpu_count", "total_memory_bytes")


def test_ap007_r1_observer_matches_the_qualification_suite_digest() -> None:
    from scripts import qualify_environment

    fingerprint = qualify_environment.environment_fingerprint()
    assert all(key in fingerprint for key in _R1_HOST_FIELDS)
    five_values = {key: fingerprint[key] for key in _R1_HOST_FIELDS}
    observed = sandbox_boundary.observe_host_fingerprint()
    assert observed == qualification_host_fingerprint(fingerprint)
    assert observed == qualification_host_fingerprint(five_values)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", observed)


def test_ap007_r1_every_host_value_contributes_to_the_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    description: dict[str, Any] = {
        "os": "FixtureOS",
        "machine": "fixture64",
        "cpu_model": "Fixture CPU",
        "cpu_count": 8,
        "total_memory_bytes": 17_179_869_184,
    }

    def set_fixture_values() -> None:
        monkeypatch.setattr(sandbox_boundary.platform, "system", lambda: description["os"])
        monkeypatch.setattr(sandbox_boundary.platform, "machine", lambda: description["machine"])
        monkeypatch.setattr(sandbox_boundary, "_sb_cpu_model", lambda: description["cpu_model"])
        monkeypatch.setattr(sandbox_boundary.os, "cpu_count", lambda: description["cpu_count"])
        monkeypatch.setattr(
            sandbox_boundary,
            "_sb_total_memory_bytes",
            lambda: description["total_memory_bytes"],
        )

    set_fixture_values()
    expected = qualification_host_fingerprint(description)
    assert sandbox_boundary.observe_host_fingerprint() == expected

    for field, changed in (
        ("os", "OtherOS"),
        ("machine", "other64"),
        ("cpu_model", "Other CPU"),
        ("cpu_count", 10),
        ("total_memory_bytes", 34_359_738_368),
    ):
        set_fixture_values()
        description[field] = changed
        set_fixture_values()
        assert sandbox_boundary.observe_host_fingerprint() != expected, field
        description[field] = {
            "os": "FixtureOS",
            "machine": "fixture64",
            "cpu_model": "Fixture CPU",
            "cpu_count": 8,
            "total_memory_bytes": 17_179_869_184,
        }[field]


def test_ap007_r1_observer_fails_closed_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    platform_system = sandbox_boundary.platform.system
    platform_machine = sandbox_boundary.platform.machine
    for attr, original, bad_value in (
        ("system", platform_system, ""),
        ("system", platform_system, None),
        ("machine", platform_machine, ""),
        ("machine", platform_machine, None),
    ):
        monkeypatch.setattr(sandbox_boundary.platform, attr, lambda value=bad_value: value)
        assert sandbox_boundary.observe_host_fingerprint() == "", (attr, bad_value)
        monkeypatch.setattr(sandbox_boundary.platform, attr, original)

    def fail() -> Any:
        raise OSError("host value unavailable")

    monkeypatch.setattr(sandbox_boundary.platform, "system", fail)
    assert sandbox_boundary.observe_host_fingerprint() == ""
    monkeypatch.setattr(sandbox_boundary.platform, "system", platform_system)
    monkeypatch.setattr(sandbox_boundary.platform, "machine", fail)
    assert sandbox_boundary.observe_host_fingerprint() == ""
    monkeypatch.setattr(sandbox_boundary.platform, "machine", platform_machine)

    for model in ("", "x" * 201, "bad\nmodel"):
        monkeypatch.setattr(sandbox_boundary, "_sb_cpu_model", lambda model=model: model)
        assert sandbox_boundary.observe_host_fingerprint() == "", model
    monkeypatch.setattr(sandbox_boundary, "_sb_cpu_model", lambda: "Fixture CPU")
    for count in (None, 0, True):
        monkeypatch.setattr(sandbox_boundary.os, "cpu_count", lambda count=count: count)
        assert sandbox_boundary.observe_host_fingerprint() == "", count
    monkeypatch.setattr(sandbox_boundary.os, "cpu_count", lambda: 8)
    for memory in (None, 0, -1):
        monkeypatch.setattr(sandbox_boundary, "_sb_total_memory_bytes", lambda memory=memory: memory)
        assert sandbox_boundary.observe_host_fingerprint() == "", memory

    monkeypatch.setattr(sandbox_boundary, "_sb_total_memory_bytes", lambda: 17_179_869_184)
    monkeypatch.setattr(sandbox_boundary, "qualification_host_fingerprint", fail)
    assert sandbox_boundary.observe_host_fingerprint() == ""


def test_ap007_r1_darwin_observer_uses_one_bounded_sysctl_and_suite_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_boundary.platform, "system", lambda: "Darwin")
    machine = sandbox_boundary.platform.machine()
    cpu_count = sandbox_boundary.os.cpu_count()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    output = {
        "machdep.cpu.brand_string": "machdep.cpu.brand_string: Apple M5\n",
        "hw.model": "hw.model: Mac17,2\n",
        "hw.memsize": "hw.memsize: 25769803776\n",
    }

    def fake_sysctl(argv: Any, **kwargs: Any) -> str:
        calls.append((tuple(argv), kwargs))
        return "".join(output.values())

    monkeypatch.setattr(sandbox_boundary, "_sb_sysctl", fake_sysctl)
    assert sandbox_boundary._sb_darwin_host_values() == ("Apple M5", 25_769_803_776)
    assert calls[-1][0] == ("sysctl", "machdep.cpu.brand_string", "hw.model", "hw.memsize")
    expected = qualification_host_fingerprint(
        {
            "os": "Darwin",
            "machine": machine,
            "cpu_model": "Apple M5",
            "cpu_count": cpu_count,
            "total_memory_bytes": 25_769_803_776,
        }
    )
    assert sandbox_boundary.observe_host_fingerprint() == expected
    assert len(calls) == 2  # one sysctl invocation per observation

    output["machdep.cpu.brand_string"] = "machdep.cpu.brand_string: \n"
    assert sandbox_boundary._sb_darwin_host_values() == ("Mac17,2", 25_769_803_776)
    output["hw.memsize"] = "hw.memsize: malformed\n"
    assert sandbox_boundary._sb_darwin_host_values() == ("Mac17,2", None)
    assert sandbox_boundary.observe_host_fingerprint() == ""


def test_ap007_r1_darwin_sysctl_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(sandbox_boundary.platform, "system", lambda: "Darwin")

    def timed_out(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("sysctl", 10.0)

    monkeypatch.setattr(sandbox_boundary.subprocess, "run", timed_out)
    assert sandbox_boundary.observe_host_fingerprint() == ""


def test_ap007_r1_non_darwin_readers_mirror_proc_cpu_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    real_read_text = Path.read_text

    def fake_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/cpuinfo":
            return "processor: 0\nmodel name: Fixture CPU 9000\n"
        if str(path) == "/proc/meminfo":
            return "MemTotal: 16777216 kB\nMemFree: 1024 kB\n"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert sandbox_boundary._sb_cpu_model() == "Fixture CPU 9000"
    assert sandbox_boundary._sb_total_memory_bytes() == 17_179_869_184


def test_ap007_r1_sysctl_uses_a_shell_free_command_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="hw.memsize: 17179869184\n")

    monkeypatch.setattr(sandbox_boundary.subprocess, "run", fake_run)
    assert sandbox_boundary._sb_sysctl(["sysctl", "hw.memsize"]) == "hw.memsize: 17179869184\n"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["sysctl", "hw.memsize"]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 10.0, "check": False}
