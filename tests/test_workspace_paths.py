"""Offline positive/negative boundary evidence for explicitly approved host paths."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

import app as studio
import workspace_paths as wp


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "state")
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "studio.db")
    monkeypatch.setattr(studio, "SETTINGS", dataclasses.replace(studio.SETTINGS, path_picker_roots=(tmp_path,)))
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", set())
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "fixture-not-a-real-token-01234567890123456789")
    monkeypatch.setattr(studio, "_RATE_WINDOWS", {})
    monkeypatch.setattr(studio, "_heavy_model_preflight", lambda _: None)
    monkeypatch.setattr(studio.app.state, "http", None, raising=False)
    studio.init_db()
    return tmp_path


@pytest.fixture
def client(host):
    client = TestClient(studio.app, headers={"Authorization": "Bearer fixture-not-a-real-token-01234567890123456789"})
    yield client
    client.close()


def test_browse_and_cancel_create_nothing_and_read_no_file_contents(host, client):
    (host / "project's folder").mkdir()
    (host / "README.md").write_text("FILE_PAYLOAD_MUST_NOT_BE_READ")
    (host / ".ssh").mkdir()
    (host / "private.key").write_text("SECRET")
    before = sorted(host.rglob("*"))
    result = client.get("/api/workspace/picker").json()
    assert result["filesystem"] == "studio_host"
    assert result["path"] == str(host)
    assert result["parent"] is None
    assert [e["name"] for e in result["entries"]] == ["project's folder"]
    files = client.get("/api/workspace/picker", params={"kind": "file"}).json()
    assert "README.md" in [e["name"] for e in files["entries"]]
    assert "FILE_PAYLOAD_MUST_NOT_BE_READ" not in json.dumps(files)
    assert "private.key" not in json.dumps(files) and ".ssh" not in json.dumps(files)
    assert sorted(host.rglob("*")) == before
    assert not (host / "state").exists()


@pytest.mark.parametrize("tail", ["../outside", ".ssh", "sub/../../outside", ".env", "x\x00y"])
def test_navigation_refuses_hidden_secrets_and_traversal(host, client, tail):
    result = client.get("/api/workspace/picker", params={"path": str(host / tail)})
    assert result.status_code == 400


def test_outside_root_and_directory_symlinks_are_never_followed(host, client):
    outside = host.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "CONFIDENTIAL_NAME").mkdir(exist_ok=True)
    (host / "link").symlink_to(outside, target_is_directory=True)
    (host / "filelink").symlink_to(outside / "hidden-file")
    listing = client.get("/api/workspace/picker", params={"kind": "file"}).json()
    assert "link" not in [e["name"] for e in listing["entries"]]
    for path in (outside, host / "link", host / "link" / "CONFIDENTIAL_NAME"):
        assert client.get("/api/workspace/picker", params={"path": str(path)}).status_code == 400
    result = client.post("/api/workspace/picker/folders", json={"parent": str(host / "link"), "name": "injected", "confirmed": True})
    assert result.status_code == 400 and not (outside / "injected").exists()


@pytest.mark.parametrize("name", [".", "..", "../escape", "a/b", "a\\b", "/tmp/x", ".ssh", "secrets", " leading", "trailing ", "ends.", "NUL", "COM1.txt", "a\nname", "x" * 241])
def test_create_rejects_non_basename_and_sensitive_names(host, client, name):
    before = set(host.iterdir())
    result = client.post("/api/workspace/picker/folders", json={"parent": str(host), "name": name, "confirmed": True})
    assert result.status_code == 400
    # Audit writes may update SQLite, never add the proposed folder.
    assert not (set(host.iterdir()) - before - {host / "state"})


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_folder_creation_requires_exact_boolean_confirmation(host, client, confirmed):
    assert client.post("/api/workspace/picker/folders", json={"parent": str(host), "name": "not-created", "confirmed": confirmed}).status_code == 400
    assert not (host / "not-created").exists()


def test_confirmed_creation_is_single_level_private_audited_and_not_workspace_switch(host, client):
    result = client.post("/api/workspace/picker/folders", json={"parent": str(host), "name": "Pankaj's new project", "confirmed": True})
    assert result.status_code == 201
    path = Path(result.json()["path"])
    assert path.is_dir() and path.stat().st_mode & 0o777 == 0o700
    assert result.json()["workspace_changed"] is False
    assert not (host / "state" / "workspace_prefs.json").exists()
    assert client.get("/api/workspace/picker", params={"path": str(path)}).json()["parent"] == str(host)
    assert client.post("/api/workspace/picker/folders", json={"parent": str(host), "name": path.name, "confirmed": True}).status_code == 400
    with sqlite3.connect(studio.DB_PATH) as conn:
        audit = conn.execute("SELECT tool_name,explicit_approval,status FROM tool_audit_events").fetchall()
    assert ("workspace_create_folder", 1, "completed") in audit


def test_bounded_search_and_kind_validation(host, client):
    for i in range(205):
        (host / f"folder-{i:03}").mkdir()
    result = client.get("/api/workspace/picker").json()
    assert len(result["entries"]) == 200 and result["truncated"]
    result = client.get("/api/workspace/picker", params={"query": "folder-204"}).json()
    assert [e["name"] for e in result["entries"]] == ["folder-204"]
    assert client.get("/api/workspace/picker", params={"kind": "everything"}).status_code == 400
    assert client.get("/api/workspace/picker", params={"query": "x" * 101}).status_code == 400


def test_unsupported_platform_fails_closed(host, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(wp.PickerError, match="unavailable"):
        wp.browse(str(host), [host])


def test_suggestion_requires_consent_and_sends_description_only(host, client, monkeypatch):
    sent = []
    async def model(*args):
        sent.append(args)
        assert studio._CHAT_NATIVE_TOOLS.get() is None
        yield {"type": "content", "text": '{"name":"suggested project"}'}
    monkeypatch.setattr(studio, "stream_any_model", model)
    payload = {"model": "fixture-provider::fixture-model", "description": "A small diary", "confirmed": False}
    assert client.post("/api/workspace/picker/suggest", json=payload).status_code == 400
    assert not sent
    payload["confirmed"] = True
    result = client.post("/api/workspace/picker/suggest", json=payload)
    assert result.status_code == 200
    assert result.json() == {"name": "suggested project", "created": False, "workspace_changed": False}
    assert len(sent) == 1 and sent[0][1] == payload["model"]
    assert sent[0][2][-1] == {"role": "user", "content": payload["description"]}
    assert str(host) not in json.dumps(sent[0][2])
    assert not (host / "suggested project").exists()


@pytest.mark.parametrize("answer", ['{"name":"../escape"}', '{"name":"/tmp/x"}', '{"name":"ok","command":"mkdir x"}', 'mkdir project', '{"name":".ssh"}'])
def test_model_output_is_never_action_authority(host, client, monkeypatch, answer):
    async def model(*args):
        yield {"type": "content", "text": answer}
    monkeypatch.setattr(studio, "stream_any_model", model)
    before = sorted(host.rglob("*"))
    assert client.post("/api/workspace/picker/suggest", json={"model": "fixture", "description": "diary", "confirmed": True}).status_code == 400
    assert sorted(host.rglob("*")) == before


def test_reasoning_only_suggestion_and_provider_failure_fail_without_creation(host, client, monkeypatch):
    async def reasoning(*args):
        yield {"type": "reasoning", "text": '{"name":"not-authority"}'}
    monkeypatch.setattr(studio, "stream_any_model", reasoning)
    payload = {"model": "fixture", "description": "diary", "confirmed": True}
    assert client.post("/api/workspace/picker/suggest", json=payload).status_code == 400
    async def failure(*args):
        raise RuntimeError("PRIVATE_PROVIDER_ERROR")
        yield
    monkeypatch.setattr(studio, "stream_any_model", failure)
    result = client.post("/api/workspace/picker/suggest", json=payload)
    assert result.status_code == 502 and "PRIVATE_PROVIDER_ERROR" not in result.text
    assert not (host / "not-authority").exists()


def test_api_auth_origin_rate_limit_and_invalid_json(host, client, monkeypatch):
    assert client.get("/api/workspace/picker", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for route in ("folders", "suggest"):
        url = f"/api/workspace/picker/{route}"
        assert client.post(url, headers={"Origin": "https://hostile.invalid"}, json={"confirmed": True}).status_code == 403
        assert client.post(url, json=[]).status_code == 400
        assert client.post(url, content="not json").status_code == 400
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 1)
    monkeypatch.setattr(studio, "_RATE_WINDOWS", {})
    client.post("/api/workspace/picker/folders", json={})
    result = client.post("/api/workspace/picker/suggest", json={})
    assert result.status_code == 429 and "Retry-After" in result.headers


@pytest.mark.parametrize("raw", ['', '[""]', json.dumps(['/'] * 33), '{}', '["relative"]', '[123]', '["/tmp/../etc"]', '["/tmp/\\u0000bad"]', 'not-json'])
def test_picker_root_configuration_is_explicit_and_strict(raw, tmp_path):
    with pytest.raises(studio.ConfigurationError, match="STUDIO_PATH_PICKER_ROOTS"):
        studio.load_settings({"STUDIO_PATH_PICKER_ROOTS": raw}, home=tmp_path)


def test_picker_root_configuration_preserves_spaces_and_is_pure(tmp_path):
    path = tmp_path / "extra binaries"
    settings = studio.load_settings({"STUDIO_PATH_PICKER_ROOTS": json.dumps([str(path)])}, home=tmp_path)
    assert settings.path_picker_roots == (path,)
    assert not path.exists()


@pytest.mark.parametrize("name", ["line\nbreak", "back\x7fspace", "rtl\u202ename", "zero\u200bwidth", "invalid\udcff", "application_default_credentials.json"])
def test_control_spoofing_and_credential_names_are_not_exposed(host, client, name):
    (host / name).mkdir()
    listing = client.get("/api/workspace/picker", params={"kind": "file"})
    assert listing.status_code == 200
    assert name not in [entry["name"] for entry in listing.json()["entries"]]
    assert client.post("/api/workspace/picker/folders", content=json.dumps({"parent": str(host), "name": name, "confirmed": True}), headers={"Content-Type": "application/json"}).status_code == 400


def test_symlink_swap_before_component_open_fails_closed(host, monkeypatch):
    (host / "sub").mkdir()
    outside = host.parent / "outside-race"
    outside.mkdir()
    original = os.open
    def swap(path, flags, *args, **kwargs):
        if path == "sub":
            (host / "sub").rmdir()
            (host / "sub").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)
    # Capability detection must still recognize the wrapped syscall.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swap})
    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(wp.PickerError):
        wp.create_folder(str(host / "sub"), "never-created", [host], confirmed=True)
    assert not (outside / "never-created").exists()


def test_symlink_swap_after_open_uses_original_directory_capability(host, monkeypatch):
    parent = host / "parent"
    parent.mkdir()
    outside = host.parent / "outside-after-open"
    outside.mkdir()
    original = os.mkdir
    def swap(name, *args, **kwargs):
        parent.rename(host / "held")
        parent.symlink_to(outside, target_is_directory=True)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swap})
    monkeypatch.setattr(os, "mkdir", swap)
    wp.create_folder(str(parent), "confirmed", [host], confirmed=True)
    assert (host / "held" / "confirmed").is_dir()
    assert not (outside / "confirmed").exists()


def test_unsupported_secure_fd_platform_fails_closed(host, monkeypatch):
    monkeypatch.setattr(os, "supports_fd", set())
    with pytest.raises(wp.PickerError, match="manual path entry"):
        wp.browse(str(host), [host])


def test_audit_failure_prevents_folder_creation(host, client, monkeypatch):
    def refused(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(studio, "_audit_tool_start", refused)
    result = client.post("/api/workspace/picker/folders", json={"parent": str(host), "name": "not-created", "confirmed": True})
    assert result.status_code == 400 and not (host / "not-created").exists()


def test_standalone_runs_same_picker_boundary_suite_without_sibling_modules(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "standalone"
    source.mkdir()
    shutil.copy2(root / "studio.py", source / "app.py")
    shutil.copy2(Path(__file__), source / "test_picker_contract.py")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(HOME=str(tmp_path / "home"), STUDIO_DB_PATH=str(tmp_path / "standalone.db"))
    code = "import app, sys, pytest; assert app.workspace_paths is app; sys.modules['workspace_paths'] = app; raise SystemExit(pytest.main(['-q', 'test_picker_contract.py', '-k', 'not standalone', '--tb=short']))"
    result = subprocess.run([sys.executable, "-c", code], cwd=source, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "59 passed" in result.stdout


@pytest.mark.parametrize("default", ["/invalid\x00preference", "/missing/old/workspace"])
def test_stale_or_invalid_saved_location_does_not_block_default_browsing(host, client, default):
    studio.HARNESS_STORE_DIR.mkdir()
    preferences = studio.HARNESS_STORE_DIR / "workspace_prefs.json"
    preferences.write_text(json.dumps({"default_dir": default, "recent_dirs": [default]}))
    before = preferences.read_bytes()
    result = client.get("/api/workspace/picker")
    assert result.status_code == 200 and result.json()["path"] == str(host)
    assert preferences.read_bytes() == before
