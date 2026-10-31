#!/usr/bin/env python3
"""Live isolation smoke test for the Studio execution boundary (M5-P0-04).

Answers one question honestly: which isolation guarantees are OBSERVED in
this environment, and which remain unqualified? It never claims container
isolation without live proof.

Always-run checks (no container required):

1. Environment fingerprint and container-runtime availability.
2. Sandbox invocation invariants (no network, read-only root, capability drop,
   least privilege, single workspace mount) as actually constructed by
   ``app._sandbox_command``.
3. Skill-code execution gates (``app._run_skill_script``): default-off denial,
   execution-disabled denial, host-mode opt-in denial, and sandbox-mode fail
   closed with no silent host fallback.
4. Filesystem confinement primitive denials (``sandbox_boundary.confined_path``):
   traversal, absolute outside-workspace paths, NUL bytes, and symlink escapes.

Live probes (only with ``--live`` AND a working container runtime):

5. Synthetic outside-workspace file access attempt from inside the sandbox.
6. Direct network attempt from inside the sandbox.

Exit code 0 when every observable check passes. The summary reports
``isolation_status: qualified`` only after probes 5-6 have been observed and
passed; otherwise ``isolation_status: unqualified`` and
``dependent_execution: blocked (fail closed)``.

Usage::

    python3 scripts/isolation_smoke.py            # observable checks only
    python3 scripts/isolation_smoke.py --live     # + container probes

The ``--live`` path requires the configured ``STUDIO_SANDBOX_RUNTIME``
(default ``docker``), the pre-pulled ``STUDIO_SANDBOX_IMAGE`` (default
``python:3.12-slim``), and ``STUDIO_EXECUTION_MODE=sandbox`` semantics; it is
opt-in so this script can run in environments without a container runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as studio  # noqa: E402
import sandbox_boundary  # noqa: E402

FAILURES: list[str] = []


def record(ok: bool, label: str, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def section(title: str) -> None:
    print(f"\n== {title} ==")


def environment_fingerprint() -> bool:
    """Check 1: fingerprint and container-runtime availability. Returns runtime-ok."""
    section("1. Environment fingerprint and runtime availability")
    mem = "unavailable"
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    print(f"  platform: {platform.platform()}")
    print(f"  machine: {platform.machine()}  python: {platform.python_version()}  cpus: {os.cpu_count()}")
    print(f"  memory_total: {mem}")
    runtime_name = studio.STUDIO_SANDBOX_RUNTIME
    runtime_path = shutil.which(runtime_name)
    runtime_ok = False
    if runtime_path:
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, local daemon query only
                [runtime_path, "info"], capture_output=True, text=True, timeout=10, check=False,
            )
            runtime_ok = probe.returncode == 0
            print(f"  runtime_available: {'yes' if runtime_ok else 'no'} "
                  f"({runtime_name} at {runtime_path}, `info` exit {probe.returncode})")
        except subprocess.TimeoutExpired:
            runtime_ok = False
            print(f"  runtime_available: no ({runtime_name} at {runtime_path}, `info` timed out)")
        except OSError as exc:
            runtime_ok = False
            print(f"  runtime_available: no ({runtime_name} at {runtime_path}, `info` error: {exc})")
    else:
        print(f"  runtime_available: no ({runtime_name!r} not on PATH)")
    return runtime_ok


def sandbox_argv_invariants() -> None:
    """Check 2: the constructed sandbox invocation enforces least privilege."""
    section("2. Sandbox invocation invariants (as constructed by app._sandbox_command)")
    with tempfile.TemporaryDirectory(prefix="m5-isolation-smoke-") as td:
        root = Path(td)
        # Point the runtime lookup at a harmless existing executable so argv can
        # be constructed without a real container runtime. Nothing is launched.
        with unittest.mock.patch.object(studio, "STUDIO_SANDBOX_RUNTIME", "/bin/sh"):
            argv = studio._sandbox_command(root, "true", container_name="m5-smoke-probe")
    joined = " ".join(argv)
    record("--network=none" in joined, "no network", "--network=none")
    record("--read-only" in joined, "read-only container root", "--read-only")
    record("--cap-drop=ALL" in joined, "all capabilities dropped", "--cap-drop=ALL")
    record("--security-opt=no-new-privileges" in joined, "no new privileges", "--security-opt=no-new-privileges")
    record("--pull=never" in joined, "no implicit image pull", "--pull=never")
    record("--pids-limit=128" in joined and "--memory=2g" in joined and "--cpus=2" in joined,
          "resource limits", "--pids-limit=128 --memory=2g --cpus=2")
    mounts = [part for part in argv if part.startswith("type=bind")]
    record(len(mounts) == 1 and f"source={root.resolve()}" in mounts[0] and "target=/workspace" in mounts[0],
          "single workspace bind mount", "; ".join(mounts) or "none")


def skill_execution_gates() -> None:
    """Check 3: skill-code gates deny by default and never host-fallback."""
    section("3. Skill-code execution gates (app._run_skill_script)")
    with tempfile.TemporaryDirectory(prefix="m5-isolation-smoke-") as td:
        smoke_db = Path(td) / "smoke-audit.db"
        smoke_store = Path(td) / "smoke-store"
        smoke_store.mkdir(parents=True, exist_ok=True)
        skill_dir = Path(td) / "demo-skill"
        (skill_dir / "scripts").mkdir(parents=True)
        script = skill_dir / "scripts" / "search.py"
        script.write_text("print('skill-script-ran')\n", encoding="utf-8")
        args = ["kw"]

        with unittest.mock.patch.object(studio, "DB_PATH", smoke_db), \
             unittest.mock.patch.object(studio, "HARNESS_STORE_DIR", smoke_store):
            studio.init_db()

            def expect_denial(label: str, exc_types: tuple[type[BaseException], ...]) -> None:
                try:
                    asyncio.run(studio._run_skill_script(script, args, cwd=skill_dir, timeout=8.0))
                except exc_types as exc:
                    record(True, label, f"{type(exc).__name__}")
                except Exception as exc:  # noqa: BLE001 - report unexpected classes as failures
                    record(False, label, f"unexpected {type(exc).__name__}: {exc}")
                else:
                    record(False, label, "executed instead of denying")

            with unittest.mock.patch.object(studio, "STUDIO_ALLOW_SKILL_CODE", False):
                expect_denial("default-off skill-code denial", (PermissionError,))
            with unittest.mock.patch.object(studio, "STUDIO_ALLOW_SKILL_CODE", True), \
                 unittest.mock.patch.object(studio, "STUDIO_EXECUTION_MODE", "disabled"):
                expect_denial("execution-disabled denial", (PermissionError,))
            with unittest.mock.patch.object(studio, "STUDIO_ALLOW_SKILL_CODE", True), \
                 unittest.mock.patch.object(studio, "STUDIO_EXECUTION_MODE", "host"), \
                 unittest.mock.patch.object(studio, "STUDIO_ALLOW_HOST_EXECUTION", False):
                expect_denial("host-mode-without-opt-in denial", (PermissionError,))

            # Sandbox mode with an unavailable runtime must fail closed without any
            # host subprocess: stub the container command builder to simulate the
            # missing runtime and spy on host process creation.
            spawned: list[str] = []

            def _no_runtime(*_a, **_k):
                raise RuntimeError("smoke: simulated missing container runtime")

            real_exec = asyncio.create_subprocess_exec

            async def _spy_exec(*args, **kwargs):
                spawned.append(str(args[0]))
                return await real_exec(*args, **kwargs)

            with unittest.mock.patch.object(studio, "STUDIO_ALLOW_SKILL_CODE", True), \
                 unittest.mock.patch.object(studio, "STUDIO_EXECUTION_MODE", "sandbox"), \
                 unittest.mock.patch.object(studio, "_sandbox_command", _no_runtime), \
                 unittest.mock.patch.object(asyncio, "create_subprocess_exec", _spy_exec):
                try:
                    asyncio.run(studio._run_skill_script(script, args, cwd=skill_dir, timeout=8.0))
                except Exception as exc:  # noqa: BLE001 - any failure is a closed gate here
                    record(not spawned, "sandbox-mode fails closed, no host fallback",
                           f"{type(exc).__name__}; host spawns: {spawned or 'none'}")
                else:
                    record(False, "sandbox-mode fails closed, no host fallback", "executed instead of denying")


def filesystem_confinement() -> None:
    """Check 4: confined_path denies escapes (policy primitive, module-tested)."""
    section("4. Filesystem confinement denials (sandbox_boundary.confined_path)")
    with tempfile.TemporaryDirectory(prefix="m5-isolation-smoke-") as td:
        root = Path(td)
        (root / "ok.txt").write_text("inside", encoding="utf-8")
        outside = root.parent / f"m5-outside-probe-{os.getpid()}.txt"
        outside.write_text("outside", encoding="utf-8")
        os.symlink(outside, root / "link.txt")
        try:
            sandbox_boundary.confined_path(root, "ok.txt")
            record(True, "inside-workspace path resolves")
        except sandbox_boundary.SandboxBoundaryError as exc:
            record(False, "inside-workspace path resolves", str(exc))
        for label, raw in (
            ("traversal denied", "../" + outside.name),
            ("absolute outside-workspace denied", str(outside)),
            ("NUL-byte path denied", "a\x00b"),
            ("symlink escape denied", "link.txt"),
        ):
            try:
                sandbox_boundary.confined_path(root, raw)
                record(False, label, "resolved instead of denying")
            except sandbox_boundary.SandboxBoundaryError:
                record(True, label)
            finally:
                if label == "symlink escape denied":
                    outside.unlink(missing_ok=True)


def live_container_probes(runtime_ok: bool) -> bool:
    """Checks 5-6: observed denials from inside a live sandbox container."""
    section("5-6. Live container probes (outside-workspace access, direct network)")
    if not runtime_ok:
        print("  SKIPPED: no working container runtime in this environment.")
        return False
    monkey_env = {"STUDIO_EXECUTION_MODE": "sandbox"}
    old = {k: os.environ.get(k) for k in monkey_env}
    os.environ.update(monkey_env)
    try:
        with tempfile.TemporaryDirectory(prefix="m5-isolation-smoke-ws-") as td:
            workspace = Path(td)
            outside = workspace.parent / f"m5-live-outside-probe-{os.getpid()}.txt"
            outside.write_text("host secret", encoding="utf-8")
            code = (
                "import os, pathlib, socket\n"
                f"outside = pathlib.Path({str(outside)!r})\n"
                "assert not outside.exists(), 'outside-workspace host file is visible'\n"
                "try:\n"
                "    outside.write_text('escape')\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    raise AssertionError('outside-workspace host file is writable')\n"
                "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
                "sock.settimeout(2.0)\n"
                "try:\n"
                "    sock.connect(('1.1.1.1', 443))\n"
                "except OSError:\n"
                "    pass\n"
                "else:\n"
                "    raise AssertionError('sandbox unexpectedly reached the public network')\n"
                "finally:\n"
                "    sock.close()\n"
                "print('live-probes-passed')\n"
            )
            try:
                with unittest.mock.patch.object(studio, "STUDIO_EXECUTION_MODE", "sandbox"):
                    rc, out, _truncated, timed_out = asyncio.run(
                        studio._execute_limited(workspace, code, timeout=30, max_output_bytes=8192, python_code=True)
                    )
            finally:
                outside.unlink(missing_ok=True)
            ok = rc == 0 and not timed_out and b"live-probes-passed" in out
            record(ok, "outside-workspace access and direct network denied in container",
                   out.decode("utf-8", errors="replace").strip()[-200:])
            return ok
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="also run the live container probes (requires a working container runtime)")
    args = parser.parse_args()

    print("M5-P0-04 isolation smoke test")
    runtime_ok = environment_fingerprint()
    sandbox_argv_invariants()
    skill_execution_gates()
    filesystem_confinement()
    live_ok = False
    if args.live:
        live_ok = live_container_probes(runtime_ok)
    else:
        section("5-6. Live container probes (outside-workspace access, direct network)")
        print("  SKIPPED: pass --live with a working container runtime to observe these denials.")

    qualified = bool(args.live and runtime_ok and live_ok)
    print("\n== Summary ==")
    print(f"  checks_failed: {len(FAILURES)}" + (f" ({'; '.join(FAILURES)})" if FAILURES else ""))
    print(f"  isolation_status: {'qualified' if qualified else 'unqualified'}")
    if qualified:
        print("  dependent_execution: released under the observed container boundary")
    else:
        print("  dependent_execution: blocked (fail closed)")
        print("  note: container-level isolation is UNQUALIFIED here; do not advertise it.")
    return 1 if FAILURES or (args.live and runtime_ok and not live_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
