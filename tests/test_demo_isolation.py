from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import demo_tour as tour

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo_source(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """A synthetic source tree; never seed or exercise the real checkout store."""
    source = tmp_path / "source"
    source.mkdir()
    for path in ROOT.glob("*.py"):
        shutil.copy2(path, source / path.name)
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "demo_tour.py", source / "scripts" / "demo_tour.py")
    (source / "static").mkdir()
    shutil.copy2(ROOT / "static" / "index.html", source / "static" / "index.html")
    (source / "docs" / "media").mkdir(parents=True)
    shutil.copy2(ROOT / "docs" / "media" / "demo-transcript.md", source / "docs" / "media" / "demo-transcript.md")
    store = source / ".harnesses" / "sessions"
    store.mkdir(parents=True)
    marker = tmp_path / "synthetic-private-workspace"
    marker.mkdir()
    prefs = store / "workspace_prefs.json"
    sentinel = json.dumps({"default_dir": str(marker), "recent_dirs": [str(marker)]}).encode()
    prefs.write_bytes(sentinel)
    return source, prefs, sentinel


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(tmp_path / "home")
    # A synthetic credential must not seed the demo's throwaway database.
    env["OPENAI_API_KEY"] = "synthetic-provider-token"
    return env


def test_in_process_tour_does_not_touch_source_store(demo_source: tuple[Path, Path, bytes], tmp_path: Path) -> None:
    source, prefs, sentinel = demo_source
    demo_root = tmp_path / "throwaway-demo"
    result = subprocess.run(
        [sys.executable, str(source / "scripts" / "demo_tour.py"), "--check", "--root", str(demo_root)],
        cwd=source, env=_env(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert prefs.read_bytes() == sentinel
    assert (demo_root / "data" / "harness-sessions").is_dir()
    assert not (source / "studio_history.db").exists()


def test_served_demo_is_authenticated_and_uses_its_own_store(
    demo_source: tuple[Path, Path, bytes], tmp_path: Path
) -> None:
    source, prefs, sentinel = demo_source
    demo_root = tmp_path / "throwaway-demo"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(source / "scripts" / "demo_tour.py"), "--serve", "--port", str(port), "--root", str(demo_root)],
        cwd=source, env=_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=45), "demo did not report a seeded server"
            line = process.stdout.readline()
        assert line, f"demo exited before startup (exit={process.poll()})"
        banner = json.loads(line)
        base = banner["url"]
        with pytest.raises(HTTPError) as unauthenticated:
            urlopen(base + "/api/workspace/directory", timeout=5)
        assert unauthenticated.value.code == 401
        headers = {"Authorization": f"Bearer {banner['token']}"}
        with urlopen(Request(base + "/api/providers", headers=headers), timeout=5) as response:
            assert json.load(response)["providers"] == []
        with urlopen(Request(base + "/api/workspace/directory", headers=headers), timeout=5) as response:
            directory = json.load(response)
        assert directory["workspace_dir"] == str(demo_root / "workspace")
        assert str(tmp_path / "synthetic-private-workspace") not in json.dumps(directory)
        new_workspace = demo_root / "another-workspace"
        new_workspace.mkdir()
        request = Request(
            base + "/api/workspace/directory", method="POST",
            headers={**headers, "Content-Type": "application/json", "Origin": base},
            data=json.dumps({"workspace_dir": str(new_workspace)}).encode(),
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["workspace_dir"] == str(new_workspace)
        assert prefs.read_bytes() == sentinel
        own_prefs = json.loads((demo_root / "data" / "harness-sessions" / "workspace_prefs.json").read_text())
        assert own_prefs["default_dir"] == str(new_workspace)
        assert not (source / "studio_history.db").exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_in_process_rejects_an_already_imported_app(demo_source: tuple[Path, Path, bytes], tmp_path: Path) -> None:
    source, prefs, sentinel = demo_source
    code = (
        "import app; from scripts import demo_tour as cli; "
        "env = cli.tour.make_environment(__import__('pathlib').Path(__import__('sys').argv[1])); "
        "cli.run_in_process(env)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "throwaway-demo")],
        cwd=source, env=_env(tmp_path), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "requires a fresh process" in result.stderr
    assert prefs.read_bytes() == sentinel


def test_refuses_demo_root_inside_source_or_symlink_escape(tmp_path: Path) -> None:
    with pytest.raises(tour.DemoEnvironmentError, match="separate"):
        tour.make_environment(ROOT / "not-a-throwaway-root")
    root = tmp_path / "demo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(tour.DemoEnvironmentError, match="escapes"):
        tour.make_environment(root)
