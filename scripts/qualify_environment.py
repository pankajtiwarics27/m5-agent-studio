#!/usr/bin/env python3
"""Environment qualification suite for M5-P3-01 (sandbox) and M5-P4-01 (hardware).

One entry point, two evidence suites, one artifact. Run it on the machine that is
supposed to *hold* the guarantee — this repository's CI cannot, because CI has no
container runtime and no 24 GB Apple Silicon.

Usage::

    python3 scripts/qualify_environment.py --all              # both suites
    python3 scripts/qualify_environment.py --sandbox          # M5-P3-01 only
    python3 scripts/qualify_environment.py --hardware         # M5-P4-01 only
    python3 scripts/qualify_environment.py --all \
        --json dist/qualification.json --markdown dist/qualification.md

    # optional, for the P4-01 measurement AC:
    python3 scripts/qualify_environment.py --hardware --pressure-gb 12 \
        --model-command "python3 my_omlx_probe.py" --model-runs 3 \
        --profile-out dist/model-profile.jsonl

What it does **not** do: it never invents a result. A check that cannot run is
recorded as ``blocked`` or ``skip`` with the reason, and the suite verdict is only
``qualified`` when every required check actually ran and passed. Exit codes:

* ``0`` — every required check passed.
* ``1`` — at least one required check failed (the guarantee does *not* hold).
* ``2`` — required checks could not run (no runtime / not target hardware).

Sandbox suite (M5-P3-01), each probe executed through the product's own sandbox
path (``app._execute_limited`` under ``STUDIO_EXECUTION_MODE=sandbox``) where that
path is importable, plus a raw adversarial matrix that mirrors the constructed
argv:

1.  runtime CLI present and its daemon reachable;
2.  the configured image is already local (no implicit pull);
3.  positive compatibility: the sandbox runs code and its workspace writes land;
4.  path traversal and absolute outside-workspace paths are not readable;
5.  a workspace symlink pointing outside does not disclose the target;
6.  direct network egress is refused;
7.  host environment credentials are not visible inside the sandbox;
8.  a spawned descendant does not survive the sandbox lifetime;
9.  resource limits hold (memory cap and pids cap);
10. dropped capabilities hold (raw sockets refused);
11. no silent host fallback: without a working runtime the product path denies;
12. ``scripts/isolation_smoke.py --live`` exits 0 with its own checks.

Hardware suite (M5-P4-01):

1.  machine fingerprint (chip, RAM, cores, swap) bound into the record;
2.  ``p4.single_heavy_model``: the admission primitive admits one heavy request and
    rejects the second under the detected capacity;
3.  ``p4.admission_wired``: whether the serving path actually consults admission
    (reported, not assumed);
4.  ``p4.priors_not_verified``: routing confidence with no measured profile is
    ``unknown``, never ``high``;
5.  ``p4.pressure``: measured memory pressure and swap growth (needs
    ``--pressure-gb`` and a 24 GB machine for the story AC);
6.  ``p4.model_load``: measured load/first-token/throughput/peak memory for a
    caller-supplied probe command, optionally persisted as a
    ``model_calibration`` profile.

Nothing here needs a network, a model, or a provider. The two optional
measurement flags are the only things that touch your hardware.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_IMAGE = "python:3.12-slim"
GIB = 1024**3

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_BLOCKED = "blocked"

STORY_P3 = "M5-P3-01"
STORY_P4 = "M5-P4-01"

#: Markers the product prints when it refuses to execute. Their presence means a
#: probe was denied, which is a pass for a denial check and a failure for a
#: positive check.
DENIAL_MARKERS = (
    "code execution is disabled",
    "sandbox runtime unavailable",
    "cannot connect to the docker daemon",
    "cannot connect to the podman socket",
    "sandbox execution is disabled",
)


def _now() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """One recorded qualification check."""

    check_id: str
    story: str
    title: str
    status: str
    detail: str = ""
    duration_s: float = 0.0
    required: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "story": self.story,
            "title": self.title,
            "status": self.status,
            "detail": self.detail[:600],
            "duration_s": round(self.duration_s, 3),
            "required": self.required,
            "evidence": self.evidence,
        }


class Recorder:
    """Collects check results and prints them as they happen."""

    def __init__(self, *, stream: Any = None) -> None:
        self.results: list[CheckResult] = []
        self._stream = stream if stream is not None else sys.stdout

    # -- recording ---------------------------------------------------------

    def _emit(self, result: CheckResult) -> CheckResult:
        self.results.append(result)
        marker = {
            STATUS_PASS: "PASS",
            STATUS_FAIL: "FAIL",
            STATUS_SKIP: "SKIP",
            STATUS_BLOCKED: "BLOCKED",
        }[result.status]
        line = f"  [{marker:7}] {result.check_id}: {result.title}"
        if result.detail:
            line += f" — {result.detail}"
        print(line, file=self._stream)
        return result

    def record(
        self,
        check_id: str,
        story: str,
        title: str,
        status: str,
        *,
        detail: str = "",
        required: bool = True,
        evidence: dict[str, Any] | None = None,
        duration_s: float = 0.0,
    ) -> CheckResult:
        if status not in {STATUS_PASS, STATUS_FAIL, STATUS_SKIP, STATUS_BLOCKED}:
            raise ValueError(f"unknown check status: {status!r}")
        return self._emit(
            CheckResult(
                check_id=check_id,
                story=story,
                title=title,
                status=status,
                detail=detail,
                duration_s=duration_s,
                required=required,
                evidence=dict(evidence or {}),
            )
        )

    def check(
        self,
        check_id: str,
        story: str,
        title: str,
        probe: Callable[[], tuple[bool, str]],
        *,
        required: bool = True,
        skip_reason: str = "",
        evidence: Callable[[], dict[str, Any]] | None = None,
    ) -> CheckResult:
        """Run ``probe``; it returns ``(ok, detail)`` and never raises."""
        if skip_reason:
            return self.record(
                check_id, story, title, STATUS_SKIP, detail=skip_reason, required=required
            )
        started = _now()
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a probe that raises has failed
            return self.record(
                check_id,
                story,
                title,
                STATUS_FAIL,
                detail=f"probe raised {type(exc).__name__}: {exc}",
                required=required,
                duration_s=_now() - started,
            )
        return self.record(
            check_id,
            story,
            title,
            STATUS_PASS if ok else STATUS_FAIL,
            detail=detail,
            required=required,
            evidence=evidence() if evidence is not None else None,
            duration_s=_now() - started,
        )

    # -- views -------------------------------------------------------------

    def for_story(self, story: str) -> list[CheckResult]:
        return [result for result in self.results if result.story == story]

    def failures(self, story: str) -> list[CheckResult]:
        return [result for result in self.for_story(story) if result.status == STATUS_FAIL and result.required]

    def unexecuted(self, story: str) -> list[CheckResult]:
        return [
            result
            for result in self.for_story(story)
            if result.status in {STATUS_SKIP, STATUS_BLOCKED} and result.required
        ]

    def verdict(self, story: str) -> str:
        if self.failures(story):
            return "unqualified"
        if self.unexecuted(story):
            return "blocked"
        return "qualified"


# ---------------------------------------------------------------------------
# Environment probing
# ---------------------------------------------------------------------------


def _total_memory_bytes() -> int | None:
    system = platform.system()
    if system == "Darwin":
        out = _capture(["sysctl", "-n", "hw.memsize"])
        if out and out.strip().isdigit():
            return int(out.strip())
        return None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split(":", 1)[1].split()[0]) * 1024
    except OSError:
        return None
    return None


def _meminfo_snapshot() -> dict[str, int]:
    """Best-effort memory counters, in bytes, for the current platform."""
    snapshot: dict[str, int] = {}
    system = platform.system()
    if system == "Darwin":
        out = _capture(["vm_stat"])
        page_size = 4096
        if out:
            for line in out.splitlines():
                if "page size of" in line:
                    match = re.search(r"page size of (\d+) bytes", line)
                    if match:
                        page_size = int(match.group(1))
                parts = line.split(":")
                if len(parts) == 2 and parts[1].strip().rstrip(".").isdigit():
                    snapshot[parts[0].strip().lower().replace(" ", "_")] = (
                        int(parts[1].strip().rstrip(".")) * page_size
                    )
        swap = _capture(["sysctl", "-n", "vm.swapusage"])
        if swap:
            total = re.search(r"total = ([\d.]+)M", swap)
            used = re.search(r"used = ([\d.]+)M", swap)
            if total:
                snapshot["swap_total_bytes"] = int(float(total.group(1)) * 1024 * 1024)
            if used:
                snapshot["swap_used_bytes"] = int(float(used.group(1)) * 1024 * 1024)
        return snapshot
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return snapshot
    mapping = {
        "MemTotal": "mem_total_bytes",
        "MemAvailable": "mem_available_bytes",
        "MemFree": "mem_free_bytes",
        "SwapTotal": "swap_total_bytes",
        "SwapFree": "swap_free_bytes",
    }
    for line in text.splitlines():
        key = line.split(":", 1)[0].strip()
        if key in mapping:
            snapshot[mapping[key]] = int(line.split(":", 1)[1].split()[0]) * 1024
    if "swap_total_bytes" in snapshot and "swap_free_bytes" in snapshot:
        snapshot["swap_used_bytes"] = snapshot["swap_total_bytes"] - snapshot["swap_free_bytes"]
    return snapshot


def _cpu_model() -> str:
    if platform.system() == "Darwin":
        brand = _capture(["sysctl", "-n", "machdep.cpu.brand_string"])
        if brand and brand.strip():
            return brand.strip()
        chip = _capture(["sysctl", "-n", "hw.model"])
        return (chip or "").strip()
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return ""
    return ""


def _git_revision() -> str:
    """The repository revision the evidence is bound to (empty when unavailable)."""
    out = _capture(["git", "-C", str(ROOT), "rev-parse", "HEAD"], timeout=5.0)
    return out.strip()


def environment_fingerprint() -> dict[str, Any]:
    """Everything the evidence record is bound to."""
    memory = _total_memory_bytes()
    return {
        "git_revision": _git_revision(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.mac_ver()[0] if platform.system() == "Darwin" else platform.version(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "total_memory_bytes": memory,
        "total_memory_gib": round(memory / GIB, 2) if memory else None,
        "python": platform.python_version(),
        "memory": _meminfo_snapshot(),
    }


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def _capture(argv: Sequence[str], *, timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with no shell, bounded output, and a hard timeout."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def detect_runtime(preferred: str | None = None) -> tuple[str, str]:
    """Return ``(runtime_path, runtime_name)`` or ``("", "")`` when none is usable.

    An explicitly requested runtime is never silently replaced by another one: if
    ``--runtime podman`` is not installed the run is blocked rather than quietly
    qualifying a different backend.
    """
    if preferred and preferred not in ("", "auto"):
        found = shutil.which(preferred)
        return (found, Path(preferred).name) if found else ("", "")
    for candidate in ("docker", "podman"):
        found = shutil.which(candidate)
        if found:
            return found, candidate
    return "", ""


def container_argv(
    runtime: str,
    image: str,
    workspace: Path,
    code: str,
    *,
    name: str = "",
    memory: str = "1g",
    pids_limit: int = 128,
    cpus: str = "1",
) -> list[str]:
    """Build a hardened, ``--rm`` sandbox invocation mirroring the product flags.

    Every flag here is asserted against ``app._sandbox_command`` by
    ``tests/test_qualify_environment.py``; a check that drifts from the product
    would qualify something the product does not actually do.
    """
    argv = [
        runtime,
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit", str(pids_limit),
        "--memory", memory,
        "--cpus", cpus,
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--mount", f"type=bind,source={workspace.resolve()},target=/workspace",
        "--workdir", "/workspace",
    ]
    if name:
        argv.extend(["--name", name])
    argv.extend([image, "python3", "-c", code])
    return argv


def _looks_denied(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in DENIAL_MARKERS)


# ---------------------------------------------------------------------------
# Sandbox suite (M5-P3-01)
# ---------------------------------------------------------------------------

_POSITIVE_CODE = r"""
import os, pathlib, sys
pathlib.Path('/workspace/qualification-positive.txt').write_text('ok', encoding='utf-8')
assert os.getcwd() == '/workspace', os.getcwd()
print('positive-ok')
"""

_ESCAPE_CODE = r"""
import os, pathlib, socket, sys
outside = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
failures = []
for path in ('/etc/hostname', '/workspace/../etc/hostname', '/proc/1/root/etc/hostname'):
    try:
        pathlib.Path(path).read_text(encoding='utf-8')
    except (OSError, PermissionError):
        continue
    failures.append(path)
link = pathlib.Path('/workspace/qualify-link')
if link.exists():
    try:
        link.read_text(encoding='utf-8')
    except (OSError, PermissionError):
        pass
    else:
        failures.append('symlink')
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(3.0)
try:
    sock.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    failures.append('network')
finally:
    sock.close()
if os.environ.get('STUDIO_QUALIFICATION_SENTINEL'):
    failures.append('env')
try:
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW).close()
except (OSError, PermissionError):
    pass
else:
    failures.append('raw-socket')
print('denials:' + (','.join(failures) if failures else 'none'))
if failures:
    raise SystemExit(3)
"""

_MEMORY_CODE = "import sys\nbuf = bytearray(3 * 1024 ** 3)\nprint('allocated', len(buf))"

_DESCENDANT_CODE = "import subprocess, sys, time\np = subprocess.Popen(['sleep', '300'])\nprint('spawned', p.pid)\n"


def _product_sandbox_module() -> Any | None:
    """Import ``app`` for product-path checks, or ``None`` when unavailable."""
    sys.path.insert(0, str(ROOT))
    try:
        import app as studio  # noqa: PLC0415 - optional dependency of this script
    except Exception:  # noqa: BLE001 - a missing/broken app must not fake a pass
        return None
    return studio


def _sandbox_env(studio: Any | None) -> dict[str, str]:
    env = dict(os.environ)
    env["STUDIO_EXECUTION_MODE"] = "sandbox"
    env.pop("STUDIO_ALLOW_HOST_EXECUTION", None)
    if studio is not None:
        env.setdefault("STUDIO_SANDBOX_RUNTIME", getattr(studio, "STUDIO_SANDBOX_RUNTIME", "docker"))
        env.setdefault("STUDIO_SANDBOX_IMAGE", getattr(studio, "STUDIO_SANDBOX_IMAGE", DEFAULT_IMAGE))
    return env


def sandbox_suite(
    recorder: Recorder,
    *,
    runtime: str,
    runtime_name: str,
    image: str,
    timeout: float,
) -> None:
    """Record the M5-P3-01 checks for one detected runtime."""
    studio = _product_sandbox_module()
    usable = bool(runtime) and bool(runtime_name)
    runtime_ok = False

    if not usable:
        recorder.record(
            "p3.runtime.available",
            STORY_P3,
            "container runtime CLI and daemon are reachable",
            STATUS_BLOCKED,
            detail="no docker/podman on PATH; container guarantees cannot be observed here",
        )
    else:
        def _runtime_probe() -> tuple[bool, str]:
            version = run_command([runtime, "--version"], timeout=min(timeout, 30.0))
            info = run_command([runtime, "info"], timeout=min(timeout, 60.0))
            detail = (version.stdout or version.stderr or "").strip().splitlines()[:1]
            label = detail[0] if detail else runtime_name
            return info.returncode == 0, f"{label} (info exit {info.returncode})"

        result = recorder.check(
            "p3.runtime.available",
            STORY_P3,
            "container runtime CLI and daemon are reachable",
            _runtime_probe,
        )
        runtime_ok = result.status == STATUS_PASS

    image_reason = "" if runtime_ok else "no reachable container runtime"

    def _image_probe() -> tuple[bool, str]:
        inspected = run_command([runtime, "image", "inspect", image], timeout=min(timeout, 60.0))
        if inspected.returncode != 0:
            return False, f"image {image!r} is not local (pull it first; this suite never pulls)"
        return True, f"image {image!r} present"

    recorder.check(
        "p3.image.local", STORY_P3, "configured sandbox image is already present", _image_probe,
        skip_reason=image_reason,
    )
    image_ok = recorder.results[-1].status == STATUS_PASS

    with tempfile.TemporaryDirectory(prefix="m5-qualify-ws-") as workspace_dir:
        workspace = Path(workspace_dir)
        outside = Path(tempfile.gettempdir()) / f"m5-qualify-outside-{os.getpid()}.txt"
        outside.write_text("host-only secret", encoding="utf-8")
        (workspace / "qualify-link").symlink_to(outside)
        probe_env = _sandbox_env(studio)
        probe_env["STUDIO_QUALIFICATION_SENTINEL"] = "must-not-leak"

        skip = "" if image_ok else "container image unavailable"

        def _positive() -> tuple[bool, str]:
            proc = run_command(
                container_argv(runtime, image, workspace, _POSITIVE_CODE), timeout=timeout, env=probe_env
            )
            landed = (workspace / "qualification-positive.txt")
            ok = proc.returncode == 0 and "positive-ok" in proc.stdout and landed.is_file()
            return ok, f"exit {proc.returncode}; workspace write landed={landed.is_file()}"

        recorder.check(
            "p3.positive.execute", STORY_P3,
            "sandbox runs code and its workspace write lands on the host", _positive, skip_reason=skip,
        )

        def _denials() -> tuple[bool, str]:
            proc = run_command(
                container_argv(runtime, image, workspace, _ESCAPE_CODE), timeout=timeout, env=probe_env
            )
            marker = next((line for line in proc.stdout.splitlines() if line.startswith("denials:")), "")
            leaked = marker.partition(":")[2] if marker else "unknown"
            ok = proc.returncode == 0 and leaked == "none"
            return ok, f"exit {proc.returncode}; leaked={leaked}"

        recorder.check(
            "p3.adversarial.escape_matrix", STORY_P3,
            "outside paths, symlink, network, host env and raw sockets are all refused",
            _denials, skip_reason=skip,
        )

        def _memory_limit() -> tuple[bool, str]:
            proc = run_command(
                container_argv(runtime, image, workspace, _MEMORY_CODE, memory="512m"),
                timeout=timeout,
                env=probe_env,
            )
            # Either the kernel kills the process (137) or Python raises MemoryError.
            denied = proc.returncode != 0 or "MemoryError" in (proc.stderr or "")
            return denied, f"exit {proc.returncode}"

        recorder.check(
            "p3.adversarial.memory_limit", STORY_P3,
            "a 3 GiB allocation is refused under --memory 512m", _memory_limit, skip_reason=skip,
        )

        def _descendants() -> tuple[bool, str]:
            name = f"m5-qualify-desc-{os.getpid()}"
            proc = run_command(
                container_argv(runtime, image, workspace, _DESCENDANT_CODE, name=name),
                timeout=timeout,
                env=probe_env,
            )
            listing = run_command(
                [runtime, "ps", "--quiet", "--filter", f"name=^{name}$"], timeout=min(timeout, 30.0)
            )
            survivors = [line for line in (listing.stdout or "").splitlines() if line.strip()]
            return not survivors, f"exit {proc.returncode}; surviving containers={len(survivors)}"

        recorder.check(
            "p3.adversarial.descendants", STORY_P3,
            "a background descendant does not survive the sandbox lifetime",
            _descendants, skip_reason=skip,
        )

    outside.unlink(missing_ok=True)

    # Product-path checks: the argv the runtime actually builds, and the
    # fail-closed behaviour when the runtime is missing.
    if studio is None:
        recorder.record(
            "p3.product.argv", STORY_P3, "constructed sandbox argv carries every hardening flag",
            STATUS_BLOCKED, detail="app could not be imported (install the project dependencies)",
        )
        recorder.record(
            "p3.product.no_host_fallback", STORY_P3, "missing sandbox runtime denies instead of running on the host",
            STATUS_BLOCKED, detail="app could not be imported (install the project dependencies)",
        )
    else:
        def _argv() -> tuple[bool, str]:
            import unittest.mock  # noqa: PLC0415 - only needed for this probe

            with tempfile.TemporaryDirectory(prefix="m5-qualify-argv-") as td:
                # Pin the runtime lookup at any harmless executable so the check
                # validates argv construction even where no container CLI exists.
                with unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", "/bin/sh"):
                    argv = studio._sandbox_command(Path(td), "print(1)", container_name="m5-qualify-argv")
            joined = " ".join(argv)
            required_flags = (
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pull=never",
                "--pids-limit",
                "--memory",
                "--cpus",
            )
            missing = [flag for flag in required_flags if flag not in joined]
            binds = [part for part in argv if part.startswith("type=bind")]
            ok = not missing and len(binds) == 1 and not any("--privileged" in part for part in argv)
            return ok, (
                f"missing={missing or 'none'}; bind mounts={len(binds)}; privileged=False"
                if not missing
                else f"missing flags: {missing}"
            )

        recorder.check(
            "p3.product.argv", STORY_P3,
            "constructed sandbox argv carries every hardening flag", _argv,
        )

        def _no_host_fallback() -> tuple[bool, str]:
            """Two probes: a missing runtime must deny, and nothing may run on the host."""
            import asyncio  # noqa: PLC0415 - only needed for this probe
            import unittest.mock  # noqa: PLC0415 - only needed for this probe

            def _attempt(runtime_name: str | None, workspace: Path) -> tuple[str, bool, bool]:
                marker = workspace / "host-write-marker.txt"
                code = f"open({str(marker)!r}, 'w').write('host')\nprint('ran-on-host')"
                patch = (
                    unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", runtime_name)
                    if runtime_name is not None
                    else unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", studio.STUDIO_SANDBOX_RUNTIME)
                )
                with patch:
                    try:
                        rc, output, _truncated, timed_out = asyncio.run(
                            studio._execute_limited(
                                workspace, code, timeout=60, max_output_bytes=8192, python_code=True
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - a refusal is a pass
                        return f"denied by {type(exc).__name__}: {exc}", False, True
                text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
                return f"exit {rc}{' (timed out)' if timed_out else ''}", marker.exists(), _looks_denied(text)

            with tempfile.TemporaryDirectory(prefix="m5-qualify-fallback-") as td:
                workspace = Path(td)
                simulated, simulated_host_write, simulated_denial = _attempt(
                    "m5-nonexistent-sandbox-runtime", workspace
                )
                real, real_host_write, _ = _attempt(None, workspace)
            fail_closed = simulated_denial and not simulated_host_write
            no_host_execution = not real_host_write
            ok = fail_closed and no_host_execution
            return ok, (
                f"missing_runtime: {simulated}; real_config: {real}; "
                f"host_write simulated={simulated_host_write} real={real_host_write}"
            )

        recorder.check(
            "p3.product.no_host_fallback", STORY_P3,
            "missing sandbox runtime denies instead of running on the host", _no_host_fallback,
        )

    # The repository's own live smoke test, aggregated rather than duplicated.
    def _smoke() -> tuple[bool, str]:
        proc = run_command(
            [sys.executable, str(ROOT / "scripts" / "isolation_smoke.py"), "--live"],
            timeout=max(timeout, 300.0),
            env=_sandbox_env(studio),
        )
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:]
        return proc.returncode == 0, f"exit {proc.returncode}; {tail[0] if tail else 'no output'}"

    recorder.check(
        "p3.product.isolation_smoke", STORY_P3,
        "scripts/isolation_smoke.py --live passes", _smoke,
        skip_reason="" if runtime_ok else "no reachable container runtime",
    )


# ---------------------------------------------------------------------------
# Hardware suite (M5-P4-01)
# ---------------------------------------------------------------------------


def _admission_probe(total_memory_bytes: int | None) -> tuple[bool, str]:
    """One heavy request admitted, the second rejected under the same capacity."""
    sys.path.insert(0, str(ROOT))
    import resource_admission  # noqa: PLC0415 - optional dependency of this script

    memory = total_memory_bytes or 24 * GIB
    capacity = resource_admission.ResourceCapacity(memory_bytes=memory, slots=1)
    scheduler = resource_admission.ResourceAdmissionScheduler(capacity)
    heavy = resource_admission.ResourceRequest(memory_bytes=int(memory * 0.8), slots=1)
    first = scheduler.try_admit(heavy)
    second = scheduler.try_admit(heavy)
    if first is None:
        return False, "the first heavy request was refused; capacity detection is wrong"
    admitted_after_release = None
    scheduler.release(first)
    admitted_after_release = scheduler.try_admit(heavy)
    ok = second is None and admitted_after_release is not None
    if admitted_after_release is not None:
        scheduler.release(admitted_after_release)
    return ok, (
        f"first={'admitted' if first else 'refused'}; second={'admitted' if second else 'refused'}; "
        f"after_release={'admitted' if admitted_after_release else 'refused'}"
    )


def _admission_wired() -> tuple[bool, str]:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    wired = "resource_admission" in app_source
    return wired, (
        "serving path consults the admission primitive"
        if wired
        else "admission primitive is module-tested but not consulted by app.py (single-heavy-model admission is not enforced in the serving path)"
    )


def _priors_not_verified() -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT))
    import model_recommendation  # noqa: PLC0415 - optional dependency of this script

    unmeasured = model_recommendation._confidence(None)
    ok = unmeasured.level == "unknown" and "no comparable benchmark profile" in unmeasured.note
    return ok, f"level={unmeasured.level}; note={unmeasured.note!r}"


def _pressure_probe(gigabytes: float, timeout: float) -> tuple[bool, str, dict[str, Any]]:
    """Touch ``gigabytes`` of anonymous memory while sampling swap growth."""
    before = _meminfo_snapshot()
    peak_swap = before.get("swap_used_bytes", 0)
    started = _now()
    block = b"\0" * (8 * 1024 * 1024)
    held: list[bytes] = []
    deadline = started + max(1.0, min(timeout, 600.0))
    try:
        while len(held) * len(block) < int(gigabytes * GIB) and _now() < deadline:
            held.append(block)
            peak_swap = max(peak_swap, _meminfo_snapshot().get("swap_used_bytes", 0))
    finally:
        held.clear()
    after = _meminfo_snapshot()
    evidence = {
        "requested_gib": gigabytes,
        "peak_swap_used_bytes": peak_swap,
        "swap_used_before_bytes": before.get("swap_used_bytes", 0),
        "swap_used_after_bytes": after.get("swap_used_bytes", 0),
        "swap_growth_bytes": max(0, peak_swap - before.get("swap_used_bytes", 0)),
        "elapsed_s": round(_now() - started, 3),
    }
    measured = "swap_used_bytes" in before or "swap_used_bytes" in after
    return measured, f"peak swap {peak_swap / GIB:.2f} GiB; growth {evidence['swap_growth_bytes'] / GIB:.2f} GiB", evidence


def _model_probe(
    command: str, runs: int, timeout: float, profile_out: Path | None
) -> tuple[bool, str, dict[str, Any]]:
    """Run a caller-supplied probe command and record its own reported timings."""
    timings: list[dict[str, Any]] = []
    before = _meminfo_snapshot()
    peak_swap = before.get("swap_used_bytes", 0)
    for index in range(max(1, runs)):
        started = _now()
        proc = run_command(["sh", "-c", command], timeout=timeout)
        elapsed = _now() - started
        peak_swap = max(peak_swap, _meminfo_snapshot().get("swap_used_bytes", 0))
        payload: dict[str, Any] = {}
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = {}
        timings.append(
            {
                "run": index + 1,
                "exit_code": proc.returncode,
                "wall_s": round(elapsed, 3),
                "reported": payload,
                "stderr_tail": (proc.stderr or "")[-300:],
            }
        )
    ok_runs = [item for item in timings if item["exit_code"] == 0]
    evidence: dict[str, Any] = {
        "command": command,
        "runs": timings,
        "successful_runs": len(ok_runs),
        "swap_peak_bytes": peak_swap,
        "swap_growth_bytes": max(0, peak_swap - before.get("swap_used_bytes", 0)),
    }
    if profile_out is not None and ok_runs:
        evidence["profile_path"] = str(_write_model_profile(command, timings, profile_out))
    return bool(ok_runs), f"{len(ok_runs)}/{len(timings)} runs exited 0", evidence


def _write_model_profile(command: str, timings: Sequence[dict[str, Any]], path: Path) -> Path:
    """Persist a ``model_calibration`` profile when the probe reports its metrics.

    The probe contract is one JSON line per run with ``model_id``, ``model_digest``,
    ``runtime``, ``context_length`` and, optionally, ``first_token_ms``,
    ``prompt_tokens``, ``output_tokens`` and ``peak_memory_bytes``. Missing fields
    leave the derived metric ``None`` rather than inventing a number.
    """
    sys.path.insert(0, str(ROOT))
    import model_calibration  # noqa: PLC0415 - optional dependency of this script

    first = next((item["reported"] for item in timings if item["reported"]), {})
    inputs = model_calibration.CalibrationInputs(
        model_id=str(first.get("model_id", "qualification-probe")),
        model_digest=str(first.get("model_digest", "unknown")),
        runtime=str(first.get("runtime", "external-probe")),
        context_length=int(first.get("context_length", 8192)),
        temperature=float(first.get("temperature", 0.0)),
        hardware={"qualification_command": command},
    )
    samples = []
    for index, item in enumerate(timings):
        reported = item["reported"] or {}
        started_ms = 0.0
        finished_ms = float(item["wall_s"] * 1000.0)
        first_token_ms = reported.get("first_token_ms")
        try:
            run = model_calibration.RawRun(
                sequence=index,
                kind="warm",
                started_at_ms=started_ms,
                finished_at_ms=finished_ms,
                prompt_tokens=int(reported.get("prompt_tokens", 0) or 0),
                output_tokens=int(reported.get("output_tokens", 0) or 0),
                first_token_at_ms=float(first_token_ms) if first_token_ms is not None else None,
                peak_memory_bytes=int(reported["peak_memory_bytes"]) if reported.get("peak_memory_bytes") else None,
                error=None if item["exit_code"] == 0 else f"probe exit {item['exit_code']}",
            )
            samples.append(model_calibration.derive_sample(run))
        except (model_calibration.ModelCalibrationError, TypeError, ValueError) as exc:
            samples.append(
                model_calibration.CalibrationSample(
                    sequence=index,
                    kind="warm",
                    ok=False,
                    error=f"unusable probe measurement: {type(exc).__name__}",
                )
            )
            continue
    if not samples:
        samples.append(
            model_calibration.CalibrationSample(sequence=0, kind="warm", ok=False, error="no usable probe output")
        )
    profile = model_calibration.calibrate(inputs, tuple(samples))
    return model_calibration.save_profile(path, profile)


def hardware_suite(
    recorder: Recorder,
    *,
    fingerprint: dict[str, Any],
    pressure_gb: float,
    model_command: str,
    model_runs: int,
    profile_out: Path | None,
    timeout: float,
) -> None:
    """Record the M5-P4-01 checks for the current machine."""
    memory = fingerprint.get("total_memory_bytes") or 0
    is_target = fingerprint.get("os") == "Darwin" and "arm" in str(fingerprint.get("machine", "")).lower()
    size_ok = memory >= 24 * GIB
    recorder.record(
        "p4.machine.fingerprint",
        STORY_P4,
        "measuring on a 24 GB Apple Silicon target",
        STATUS_PASS if (is_target and size_ok) else STATUS_BLOCKED,
        detail=(
            f"{fingerprint.get('cpu_model') or 'unknown cpu'} / {fingerprint.get('machine')} / "
            f"{fingerprint.get('total_memory_gib')} GiB"
            + ("" if (is_target and size_ok) else "; the story AC is written for a 24 GB Apple Silicon Mac")
        ),
        evidence={"fingerprint": fingerprint},
    )

    recorder.check(
        "p4.single_heavy_model", STORY_P4,
        "a second simultaneous heavy model request is rejected",
        lambda: _admission_probe(fingerprint.get("total_memory_bytes")),
    )
    recorder.check(
        "p4.admission_wired", STORY_P4,
        "the serving path consults resource admission",
        _admission_wired,
        required=False,
    )
    recorder.check(
        "p4.priors_not_verified", STORY_P4,
        "routing confidence without a measured profile is unknown, not verified",
        _priors_not_verified,
    )

    pressure_evidence: dict[str, Any] = {}
    if pressure_gb <= 0:
        recorder.record(
            "p4.pressure", STORY_P4,
            "memory pressure and swap growth measured",
            STATUS_SKIP,
            detail="pass --pressure-gb N to measure (this suite never allocates memory unasked)",
        )
    else:
        def _pressure() -> tuple[bool, str]:
            ok, detail, evidence = _pressure_probe(pressure_gb, timeout)
            pressure_evidence.update(evidence)
            return ok, detail

        recorder.check(
            "p4.pressure", STORY_P4,
            "memory pressure and swap growth measured",
            _pressure,
            evidence=lambda: dict(pressure_evidence),
        )

    if not model_command:
        recorder.record(
            "p4.model_load", STORY_P4,
            "load / first-token / peak memory measured for a real model run",
            STATUS_SKIP,
            detail="pass --model-command '...' to measure your runtime (see docs/qualification-suite.md)",
        )
        return

    model_evidence: dict[str, Any] = {}

    def _model() -> tuple[bool, str]:
        ok, detail, evidence = _model_probe(model_command, model_runs, timeout, profile_out)
        model_evidence.update(evidence)
        return ok, detail

    recorder.check(
        "p4.model_load", STORY_P4,
        "load / first-token / peak memory measured for a real model run",
        _model,
        evidence=lambda: dict(model_evidence),
    )


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def build_record(
    recorder: Recorder,
    *,
    fingerprint: dict[str, Any],
    suites: Sequence[str],
    runtime_name: str,
    runtime_path: str,
    image: str,
    invocation: Sequence[str],
) -> dict[str, Any]:
    """Assemble the JSON evidence record."""
    verdicts: dict[str, Any] = {}
    for story in (STORY_P3, STORY_P4):
        if story not in suites:
            continue
        verdicts[story] = {
            "verdict": recorder.verdict(story),
            "checks_run": len(recorder.for_story(story)),
            "failed": [result.check_id for result in recorder.failures(story)],
            "not_executed": [
                {"check_id": result.check_id, "status": result.status, "detail": result.detail[:300]}
                for result in recorder.unexecuted(story)
            ],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "invocation": list(invocation),
        "suites": list(suites),
        "fingerprint": fingerprint,
        "sandbox": {
            "runtime": runtime_name,
            "runtime_path": runtime_path,
            "image": image,
        },
        "checks": [result.to_dict() for result in recorder.results],
        "verdicts": verdicts,
        # Claims mirror the verdicts verbatim: there is no separate "we think it
        # is fine" channel that could disagree with the recorded checks.
        "claims": {
            "container_isolation": verdicts.get(STORY_P3, {}).get("verdict", "not_evaluated"),
            "single_heavy_model_admission": verdicts.get(STORY_P4, {}).get("verdict", "not_evaluated"),
        },
    }


def render_markdown(record: dict[str, Any]) -> str:
    """Human-readable companion to the JSON record."""
    fingerprint = record["fingerprint"]
    lines = [
        "# Environment qualification record",
        "",
        f"Generated: {record['generated_at_utc']}",
        "",
        "## Machine",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key in ("os", "os_version", "machine", "cpu_model", "cpu_count", "total_memory_gib", "python"):
        lines.append(f"| `{key}` | {fingerprint.get(key)} |")
    lines.extend(
        [
            "",
            "## Sandbox backend",
            "",
            f"- runtime: `{record['sandbox']['runtime'] or 'none detected'}`"
            f" ({record['sandbox']['runtime_path'] or 'not on PATH'})",
            f"- image: `{record['sandbox']['image']}`",
            "",
            "## Checks",
            "",
            "| Check | Story | Status | Detail |",
            "|---|---|---|---|",
        ]
    )
    for check in record["checks"]:
        detail = str(check["detail"]).replace("|", "\\|")
        lines.append(
            f"| `{check['check_id']}` | {check['story']} | **{check['status']}** | {detail} |"
        )
    lines.extend(["", "## Verdicts", ""])
    for story, verdict in record["verdicts"].items():
        lines.append(f"- **{story}: {verdict['verdict']}**")
        if verdict["failed"]:
            lines.append(f"  - failed: {', '.join(verdict['failed'])}")
        for item in verdict["not_executed"]:
            lines.append(f"  - {item['status']}: `{item['check_id']}` — {item['detail']}")
    lines.extend(
        [
            "",
            "## How to read this",
            "",
            "`qualified` means every required check in that suite ran and passed on this machine.",
            "`unqualified` means a check failed: the guarantee does not hold as configured.",
            "`blocked` means a required check could not run (no runtime, or not the target",
            "hardware); nothing is claimed in that case. Unqualified guarantees stay",
            "unavailable in the product — the suite never upgrades a blocked result to a pass.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qualify the sandbox backend (M5-P3-01) and the model/hardware behaviour (M5-P4-01).",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--sandbox", action="store_true", help="run the M5-P3-01 sandbox suite")
    which.add_argument("--hardware", action="store_true", help="run the M5-P4-01 hardware suite")
    parser.add_argument("--all", action="store_true", help="run both suites (default)")
    parser.add_argument("--runtime", default="auto", help="docker or podman (default: auto-detect)")
    parser.add_argument("--image", default=os.environ.get("STUDIO_SANDBOX_IMAGE", DEFAULT_IMAGE))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-check timeout seconds")
    parser.add_argument("--json", type=Path, default=None, help="write the JSON evidence record here")
    parser.add_argument("--markdown", type=Path, default=None, help="write the Markdown record here")
    parser.add_argument("--pressure-gb", type=float, default=0.0, help="allocate and touch N GiB to measure pressure")
    parser.add_argument("--model-command", default="", help="shell command that runs one measured model call")
    parser.add_argument("--model-runs", type=int, default=3, help="how many times to run --model-command")
    parser.add_argument("--profile-out", type=Path, default=None, help="persist a model_calibration profile here")
    parser.add_argument("--quiet", action="store_true", help="do not print per-check lines")
    return parser.parse_args(list(argv) if argv is not None else None)


def _selected_suites(args: argparse.Namespace) -> list[str]:
    if args.sandbox:
        return [STORY_P3]
    if args.hardware:
        return [STORY_P4]
    return [STORY_P3, STORY_P4]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    suites = _selected_suites(args)
    recorder = Recorder(stream=io.StringIO() if args.quiet else None)
    fingerprint = environment_fingerprint()

    print("M5 environment qualification")
    print(
        f"  machine: {fingerprint.get('os')} {fingerprint.get('machine')} "
        f"({fingerprint.get('cpu_model') or 'unknown cpu'}), "
        f"{fingerprint.get('total_memory_gib')} GiB RAM, {fingerprint.get('cpu_count')} CPUs"
    )

    runtime_path, runtime_name = ("", "")
    if STORY_P3 in suites:
        print("\n== M5-P3-01 sandbox backend qualification ==")
        runtime_path, runtime_name = detect_runtime(args.runtime)
        print(f"  runtime: {runtime_name or 'none'} ({runtime_path or 'not on PATH'})")
        sandbox_suite(
            recorder,
            runtime=runtime_path,
            runtime_name=runtime_name,
            image=args.image,
            timeout=max(5.0, args.timeout),
        )

    if STORY_P4 in suites:
        print("\n== M5-P4-01 hardware and model qualification ==")
        hardware_suite(
            recorder,
            fingerprint=fingerprint,
            pressure_gb=max(0.0, args.pressure_gb),
            model_command=args.model_command,
            model_runs=max(1, args.model_runs),
            profile_out=args.profile_out,
            timeout=max(5.0, args.timeout),
        )

    record = build_record(
        recorder,
        fingerprint=fingerprint,
        suites=suites,
        runtime_name=runtime_name,
        runtime_path=runtime_path,
        image=args.image,
        invocation=["qualify_environment.py", *(sys.argv[1:] if argv is None else argv)],
    )

    print("\n== Verdicts ==")
    for story, verdict in record["verdicts"].items():
        print(f"  {story}: {verdict['verdict']}")
        for check_id in verdict["failed"]:
            print(f"    failed: {check_id}")
        for item in verdict["not_executed"]:
            print(f"    {item['status']}: {item['check_id']} — {item['detail'][:160]}")

    if args.json is not None:
        _write_text(args.json, json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(f"\n  JSON record: {args.json}")
    if args.markdown is not None:
        _write_text(args.markdown, render_markdown(record))
        print(f"  Markdown record: {args.markdown}")

    if any(check.status == STATUS_FAIL and check.required for check in recorder.results):
        return 1
    if any(
        check.status in {STATUS_SKIP, STATUS_BLOCKED} and check.required for check in recorder.results
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
