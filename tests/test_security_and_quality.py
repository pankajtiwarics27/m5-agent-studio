from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as studio


@pytest.fixture(autouse=True)
def _isolate_harness_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep security tests from writing the source checkout's harness store."""
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", tmp_path / "harness-sessions")


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, item: str, value: str) -> None:
        self.values[(service, item)] = value

    def get_password(self, service: str, item: str) -> str | None:
        return self.values.get((service, item))

    def delete_password(self, service: str, item: str) -> None:
        self.values.pop((service, item), None)


@pytest.fixture

def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    backend = MemoryKeyring()
    monkeypatch.setattr(studio.keyring, "set_password", backend.set_password)
    monkeypatch.setattr(studio.keyring, "get_password", backend.get_password)
    monkeypatch.setattr(studio.keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture

def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> Path:
    db = tmp_path / "state" / "studio.db"
    db.parent.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db)
    studio.init_db()
    return db


def test_host_binding_is_loopback_only_for_loopback_addresses() -> None:
    assert studio._bind_is_loopback("127.0.0.1")
    assert studio._bind_is_loopback("::1")
    assert studio._bind_is_loopback("localhost")
    assert not studio._bind_is_loopback("0.0.0.0")
    assert not studio._bind_is_loopback("192.168.1.20")


def test_url_validation_rejects_plain_http_and_embedded_credentials() -> None:
    assert studio._validate_url_syntax("https://api.example.test/v1") == "https://api.example.test/v1"
    with pytest.raises(ValueError):
        studio._validate_url_syntax("http://api.example.test/v1")
    with pytest.raises(ValueError):
        studio._validate_url_syntax("https://user:password@example.test/v1")
    with pytest.raises(ValueError, match="query parameters"):
        studio._validate_url_syntax("https://api.example.test/v1?api_key=leaked")
    with pytest.raises(ValueError, match="port"):
        studio._validate_url_syntax("https://api.example.test:0/v1")
    assert studio._validate_url_syntax("http://127.0.0.1:8080/mcp", allow_private=True)


def test_outbound_url_blocks_private_literal_addresses_by_default() -> None:
    import asyncio

    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(studio._validate_outbound_url("https://127.0.0.1/admin"))
    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(studio._validate_outbound_url("https://169.254.169.254/latest/meta-data"))


def test_per_host_private_url_exception_is_narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    def resolve_private(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(None, None, None, None, ("10.4.5.6", 443))]

    monkeypatch.setattr(studio.socket, "getaddrinfo", resolve_private)
    monkeypatch.setattr(studio, "STUDIO_ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(studio, "STUDIO_PRIVATE_URL_HOSTS", set())
    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(studio._validate_outbound_url("https://mcp.internal.test/mcp"))
    monkeypatch.setattr(studio, "STUDIO_PRIVATE_URL_HOSTS", {"mcp.internal.test"})
    asyncio.run(studio._validate_outbound_url("https://mcp.internal.test/mcp"))


def test_workspace_context_rejects_sibling_prefix_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace-secrets"
    root.mkdir()
    sibling.mkdir()
    secret = sibling / "leak.py"
    secret.write_text("API_KEY = 'should-not-leak'", encoding="utf-8")
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", root.resolve())

    result = studio.read_workspace_context_block([str(secret)])
    assert "should-not-leak" not in result
    assert result == ""


def test_resolve_confined_path_rejects_traversal_and_accepts_child(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    child = root / "src"
    child.mkdir()
    assert studio._resolve_confined_path(root, "src") == child.resolve()
    with pytest.raises(PermissionError):
        studio._resolve_confined_path(root, "../outside")


def test_grep_and_workspace_scanners_reject_secret_and_external_symlinks(tmp_path: Path) -> None:
    import asyncio

    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    marker = "BOUNDARY_PROBE_SECRET_MARKER"
    secret = root / ".env"
    secret.write_text(f"OPENAI_API_KEY={marker}\n", encoding="utf-8")
    private = outside / "private.md"
    private.write_text(f"{marker} OUTSIDE_BOUNDARY_MARKER\n", encoding="utf-8")
    private_code = outside / "private.py"
    private_code.write_text("def outside_secret():\n    pass  # TODO: outside boundary\n", encoding="utf-8")
    try:
        (root / "notes.txt").symlink_to(secret)
        (root / "secret.py").symlink_to(secret)
        (root / "write-secret").symlink_to(secret)
        (root / "external.py").symlink_to(private_code)
        (root / "external.md").symlink_to(private)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    session = {"worktree_dir": str(root), "policy": "autonomous"}
    result = asyncio.run(
        studio._execute_harness_tool_inner(
            None, session, "grep_search", {"path": ".", "pattern": "BOUNDARY_PROBE"}
        )
    )
    assert marker not in result["output"]
    assert "OUTSIDE_BOUNDARY_MARKER" not in result["output"]
    read_result = asyncio.run(
        studio._execute_harness_tool_inner(None, session, "read_file", {"path": "notes.txt"})
    )
    assert read_result["ok"] is False

    write_result = asyncio.run(
        studio._execute_harness_tool_inner(
            None, session, "write_file", {"path": "write-secret", "content": "overwritten"}
        )
    )
    assert write_result["ok"] is False
    assert marker in secret.read_text(encoding="utf-8")

    listed = asyncio.run(
        studio._execute_harness_tool_inner(None, session, "list_files", {"path": "."})
    )
    assert "external.py" not in listed["output"]
    assert "external.md" not in listed["output"]
    assert "notes.txt" not in listed["output"]

    graph = studio.build_workspace_codegraph(root)
    assert not graph["symbols"]
    assert graph["files_scanned"] == 0
    assert studio.scan_shortcut_debt(root) == []


def test_grep_search_rejects_symlink_swapped_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    root = tmp_path / "workspace"
    outside = tmp_path / "outside-secret.py"
    root.mkdir()
    outside.write_text("RACE_SECRET_MARKER = 'must-not-be-read'\n", encoding="utf-8")
    victim = root / "victim.py"
    victim.write_text("ordinary workspace file\n", encoding="utf-8")
    session = {"worktree_dir": str(root), "policy": "autonomous"}
    real_open = os.open
    swapped = False

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "victim.py" and dir_fd is not None and not swapped:
            victim.unlink()
            victim.symlink_to(outside)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(studio.os, "open", swap_before_open)
    result = asyncio.run(
        studio._execute_harness_tool_inner(
            None, session, "grep_search", {"path": ".", "pattern": "RACE_SECRET_MARKER"}
        )
    )

    assert swapped
    assert "victim.py:1:" not in result["output"]
    assert "must-not-be-read" not in result["output"]


def test_child_process_environment_excludes_credential_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "never-pass-this")
    monkeypatch.setenv("CUSTOM_SECRET", "never-pass-this-either")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = studio._minimal_execution_env({"MCP_ACCESS_TOKEN": "explicitly-configured"})
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_SECRET" not in env
    assert env["LANG"] == "C.UTF-8"
    assert env["MCP_ACCESS_TOKEN"] == "explicitly-configured"
    assert str(Path(studio.sys.executable).resolve().parent) in env["PATH"].split(studio.os.pathsep)


def test_execution_mode_requires_explicit_host_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", False)
    assert studio._execution_mode() == "disabled"
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    assert studio._execution_mode() == "host"
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "unknown-mode")
    assert studio._execution_mode() == "disabled"


def test_sandbox_preflight_refuses_secret_files_and_overbroad_mounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=not-for-the-container", encoding="utf-8")
    with pytest.raises(PermissionError, match="Known secret-bearing"):
        studio._sandbox_workspace_preflight(tmp_path)
    secret.unlink()
    (tmp_path / ".env.example").write_text("TOKEN=placeholder", encoding="utf-8")
    studio._sandbox_workspace_preflight(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = https://user:token@git.example/repo.git\\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="Git configuration contains credentials"):
        studio._sandbox_workspace_preflight(tmp_path)
    (tmp_path / ".git" / "config").unlink()
    (tmp_path / ".git").rmdir()
    monkeypatch.setattr(studio.Path, "home", lambda: tmp_path)
    with pytest.raises(PermissionError, match="home directory"):
        studio._sandbox_workspace_preflight(tmp_path)


def test_sandbox_command_enforces_container_isolation_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_IMAGE", "python:3.12-slim")
    argv = studio._sandbox_command(tmp_path, "print('ok')", python_code=True, container_name="test-sandbox")
    joined = " ".join(argv)
    for required in ("--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=128", "--memory=2g", "--cpus=2", "--security-opt=no-new-privileges", "target=/workspace", "python:3.12-slim"):
        assert required in joined
    assert studio.shlex.split(argv[-1]) == ["exec", "python3", "-c", "print('ok')"]
    assert "--entrypoint" in argv


def test_sandbox_runner_dispatches_to_runtime_and_cleans_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    runtime = tmp_path / "bin" / "fake-container"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nprintf 'RUNTIME:%s\\n' \"$*\"\n", encoding="utf-8")
    runtime.chmod(0o755)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "sandbox")
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_RUNTIME", str(runtime))
    monkeypatch.setattr(studio, "STUDIO_SANDBOX_IMAGE", "trusted:test")

    code, output, _truncated, timed_out = asyncio.run(
        studio._execute_limited(tmp_path, "printf sandbox-ok", timeout=5, max_output_bytes=8192)
    )
    assert code == 0
    assert not timed_out
    assert b"RUNTIME:run --rm --pull=never" in output
    assert b"--network=none" in output
    assert b"--read-only" in output
    assert b"--mount type=bind,source=" in output


def test_subprocess_output_is_drained_but_memory_bounded() -> None:
    import asyncio
    import sys

    async def run() -> tuple[bytes, bool]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('x' * 200000)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return await studio._communicate_limited(proc, 4096)

    output, truncated = asyncio.run(run())
    assert truncated is True
    assert len(output) < 4200
    assert b"output truncated" in output


def test_skill_installer_only_accepts_github_sources() -> None:
    parsed = studio.parse_skill_install_spec("owner/repo")
    assert parsed["git_url"] == "https://github.com/owner/repo.git"
    parsed_url = studio.parse_skill_install_spec("https://github.com/owner/repo.git")
    assert parsed_url["git_url"] == "https://github.com/owner/repo.git"
    with pytest.raises(ValueError, match="github.com"):
        studio.parse_skill_install_spec("https://evil.example/repo.git")
    with pytest.raises(ValueError, match="github.com"):
        studio.parse_skill_install_spec("file:///tmp/evil")


def test_keyring_reference_never_contains_secret(memory_keyring: MemoryKeyring) -> None:
    secret = "sk-super-secret-value"
    ref = studio._keyring_store("cloud:test:api-key", secret)
    assert secret not in ref
    assert studio._keyring_read(ref) == secret
    assert memory_keyring.values[(studio.KEYRING_SERVICE, "cloud:test:api-key")] == secret


def test_versioned_schema_upgrade_adds_columns_to_legacy_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    db = tmp_path / "prior-schema.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mcp_servers(alias TEXT PRIMARY KEY, name TEXT NOT NULL, transport TEXT, url TEXT, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO mcp_servers(alias, name, transport, url, updated_at) VALUES('old', 'Old MCP', 'http', 'https://mcp.example.test', 1)")
    studio.init_db()
    with sqlite3.connect(db) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {r[1] for r in conn.execute("PRAGMA table_info(mcp_servers)")}
        auth_token = conn.execute("SELECT auth_token FROM mcp_servers WHERE alias='old'").fetchone()[0]
        recorded = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert version == studio.SCHEMA_VERSION
    assert recorded == [(1,), (2,), (studio.SCHEMA_VERSION,)]
    assert studio.SCHEMA_VERSION == 3
    assert {"auth_token", "approval_policy", "headers_json", "env_json", "enabled_tools_json"} <= columns
    assert auth_token == ""


def test_future_database_schema_fails_without_mutating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "newer.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="newer than supported"):
        studio.init_db()
    with sqlite3.connect(db) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == []


def test_legacy_database_credentials_migrate_to_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(studio, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cloud_providers(alias TEXT, api_key TEXT)")
        conn.execute("INSERT INTO cloud_providers VALUES('legacy', 'legacy-secret')")
        conn.execute("CREATE TABLE mcp_servers(alias TEXT, auth_token TEXT, headers_json TEXT, env_json TEXT)")
        conn.execute(
            "INSERT INTO mcp_servers VALUES(?, ?, ?, ?)",
            ("legacy", "mcp-secret", json.dumps({"Authorization": "Bearer mcp-header-secret"}), json.dumps({"API_TOKEN": "mcp-env-secret", "AWS_ACCESS_KEY_ID": "mcp-access-key-secret"})),
        )
    studio._migrate_legacy_secrets()
    with sqlite3.connect(db) as conn:
        provider_value = conn.execute("SELECT api_key FROM cloud_providers").fetchone()[0]
        token_value, headers_value, env_value = conn.execute("SELECT auth_token, headers_json, env_json FROM mcp_servers").fetchone()
    assert provider_value.startswith(studio.KEYRING_REF_PREFIX)
    assert studio._keyring_read(provider_value) == "legacy-secret"
    assert "mcp-secret" not in token_value
    assert studio._keyring_read(token_value) == "mcp-secret"
    assert studio._keyring_read(json.loads(headers_value)["Authorization"]) == "Bearer mcp-header-secret"
    assert studio._keyring_read(json.loads(env_value)["API_TOKEN"]) == "mcp-env-secret"
    assert studio._keyring_read(json.loads(env_value)["AWS_ACCESS_KEY_ID"]) == "mcp-access-key-secret"


def test_seeded_combo_label_matches_its_targets(isolated_db: Path) -> None:
    combo = next(c for c in studio.list_model_combos() if c["alias"] == "hybrid-resilient")
    assert combo["name"] == "Local M5 Multi-Model Fallback"
    assert all("::" not in target and "/" not in target for target in combo["targets"])


def test_empty_telemetry_history_does_not_invent_benchmarks(isolated_db: Path) -> None:
    perf = studio._recent_perf_summary()
    assert perf == {"samples": [], "leaderboard": []}


def test_provider_secret_is_keyring_backed_and_not_returned(isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    studio.upsert_cloud_provider(
        alias="test-provider",
        name="Test Provider",
        wire="openai",
        base_url="https://api.example.test/v1",
        api_key="test-api-secret",
        models=["model-a"],
    )
    listed = studio.list_cloud_providers(include_secret=False)
    assert "test-api-secret" not in json.dumps(listed)
    assert listed[0]["has_key"] is True
    assert studio.list_cloud_providers(include_secret=True)[0]["effective_key"] == "test-api-secret"
    with sqlite3.connect(isolated_db) as conn:
        stored = conn.execute("SELECT api_key FROM cloud_providers").fetchone()[0]
    assert stored.startswith(studio.KEYRING_REF_PREFIX)
    assert "test-api-secret" not in stored


def test_credential_write_fails_closed_without_keyring(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("backend unavailable")

    monkeypatch.setattr(studio.keyring, "set_password", unavailable)
    with pytest.raises(RuntimeError, match="refusing to persist a secret"):
        studio.upsert_cloud_provider("keyring-fail", "Failure Test", "openai", "https://api.example.test/v1", "should-not-persist", ["m"])
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT count(*) FROM cloud_providers WHERE alias='keyring-fail'").fetchone()[0] == 0


def test_mcp_credentials_and_sensitive_headers_are_masked(isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    saved = studio.upsert_mcp_server({
        "alias": "test-mcp",
        "transport": "http",
        "url": "https://mcp.example.test/mcp",
        "auth_token": "mcp-token-secret",
        "headers": {"X-Api-Key": "header-key-secret", "X-Region": "west"},
        "env": {"SERVICE_PASSWORD": "env-password-secret", "LOG_LEVEL": "info"},
    })
    public = studio.get_mcp_server("test-mcp", include_secret=False)
    private = studio.get_mcp_server("test-mcp", include_secret=True)
    serialized = json.dumps(public)
    for secret in ("mcp-token-secret", "header-key-secret", "env-password-secret"):
        assert secret not in serialized
    assert private["effective_token"] == "mcp-token-secret"
    assert private["headers"]["X-Api-Key"] == "header-key-secret"
    assert private["env"]["SERVICE_PASSWORD"] == "env-password-secret"
    assert saved["masked_token"]


def test_mcp_calls_enforce_enabled_and_registered_tool_allowlist(isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    import asyncio

    studio.upsert_mcp_server({"alias": "tool-server", "transport": "http", "url": "https://mcp.example.test/mcp"})
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE mcp_servers SET tools_json = ?, enabled_tools_json = ? WHERE alias = ?", (json.dumps([{"name": "safe_read"}]), json.dumps(["safe_read"]), "tool-server"))
    with pytest.raises(PermissionError, match="allowlist"):
        asyncio.run(studio.execute_mcp_tool_call(None, "tool-server", "dangerous_write", {}))
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE mcp_servers SET enabled = 0 WHERE alias = ?", ("tool-server",))
    with pytest.raises(PermissionError, match="disabled"):
        asyncio.run(studio.execute_mcp_tool_call(None, "tool-server", "safe_read", {}))


def test_shared_approval_flow_resolves_and_returns_session_to_running(tmp_path: Path, isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    import asyncio

    session = studio.create_harness_session_db("approval test", str(tmp_path), False, "autonomous", "local", "critic")
    session = studio._set_harness_session_policy(session["id"], "plan")
    assert session["policy"] == "plan"
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "pytest -q"})
    slot = studio._PENDING_APPROVALS[pending["approval_id"]]
    slot["decision"] = "approve"
    slot["event"].set()
    result = asyncio.run(studio._await_tool_approval(pending))
    assert result["approved"] is True
    assert studio.get_harness_session_db(session["id"])["status"] == "running"
    assert pending["approval_id"] not in studio._PENDING_APPROVALS


def test_abandoned_approval_is_rejected_and_cleaned_up(tmp_path: Path, isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    session = studio.create_harness_session_db("cancel approval", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "pytest"})
    studio._abandon_pending_tool_approval(pending, "Client disconnected")
    assert pending["approval_id"] not in studio._PENDING_APPROVALS
    assert pending["event"].is_set()
    assert studio.get_harness_session_db(session["id"])["status"] == "running"


def test_central_approval_decision_applies_across_tool_types(isolated_db: Path, memory_keyring: MemoryKeyring) -> None:
    studio.upsert_mcp_server({"alias": "approve-mcp", "transport": "http", "url": "https://mcp.example.test/mcp", "approval_policy": "inherit"})
    assert studio.tool_requires_approval("run_bash", {"command": "pytest"}, "standard")
    assert not studio.tool_requires_approval("run_bash", {"command": "pytest"}, "autonomous")
    assert studio.tool_requires_approval("run_bash", {"command": "pytest"}, "invalid-policy")
    assert studio.tool_requires_approval("mcp__approve-mcp__write", {}, "standard")
    assert not studio.tool_requires_approval("mcp__approve-mcp__write", {}, "plan")


def test_effective_workflow_policy_overrides_existing_session_safely() -> None:
    session = {"policy": "autonomous"}
    assert studio._effective_harness_policy("plan", session) == "plan"
    assert studio._effective_harness_policy("standard", session) == "standard"
    assert studio._effective_harness_policy("invalid", session) == "standard"
    assert studio._effective_harness_policy(None, session) == "autonomous"


def test_plan_and_unapproved_standard_policy_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_db: Path) -> None:
    import asyncio

    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    base = {"id": "test-session", "worktree_dir": str(tmp_path), "workspace_dir": str(tmp_path), "policy": "plan"}
    plan_write = asyncio.run(studio._execute_harness_tool(None, base, "write_file", {"path": "x.txt", "content": "x"}))
    plan_mcp = asyncio.run(studio._execute_harness_tool(None, base, "mcp__server__write", {}))
    assert plan_write["ok"] is False
    assert plan_mcp["ok"] is False
    standard = {**base, "policy": "standard"}
    shell = asyncio.run(studio._execute_harness_tool(None, standard, "run_bash", {"command": "echo unsafe"}))
    assert shell["ok"] is False
    assert "approval" in shell["output"]


def test_sdlc_pass_requires_successful_verification_and_explicit_first_line_pass() -> None:
    assert studio._sdlc_verdict_is_pass("VERDICT: PASS\nEvidence")
    assert not studio._sdlc_verdict_is_pass("PASS: all good")
    assert not studio._sdlc_verdict_is_pass("Notes\nVERDICT: PASS")
    assert not studio._sdlc_verdict_is_pass("VERDICT: NEEDS_REVISION\n...")
    assert studio._sdlc_story_can_pass(True, "VERDICT: PASS")
    assert not studio._sdlc_story_can_pass(False, "VERDICT: PASS")
    assert not studio._sdlc_story_can_pass(True, "", verification_required=False)


def test_expensive_request_rate_limit_uses_sliding_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(studio, "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE", 2)
    with studio._RATE_LOCK:
        studio._RATE_WINDOWS.clear()
    assert studio._rate_limit_exceeded("client-a", now=10) == (False, 0)
    assert studio._rate_limit_exceeded("client-a", now=11) == (False, 0)
    blocked, retry_after = studio._rate_limit_exceeded("client-a", now=12)
    assert blocked and retry_after == 58
    assert studio._rate_limit_exceeded("client-a", now=70) == (False, 0)


def test_local_api_token_is_random_persistent_and_private(tmp_path: Path) -> None:
    token_path = tmp_path / "config" / "api-token"
    first = studio._load_or_create_local_api_token(token_path)
    second = studio._load_or_create_local_api_token(token_path)
    assert len(first) >= 32
    assert first == second
    if studio.os.name == "posix":
        assert studio.stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert studio.stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    symlink = tmp_path / "token-link"
    symlink.symlink_to(token_path)
    with pytest.raises(RuntimeError, match="symlink"):
        studio._load_or_create_local_api_token(symlink)


def test_api_requires_bearer_token_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "t" * 40)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    with TestClient(studio.app) as client:
        assert studio.app.state.http.follow_redirects is False
        assert isinstance(studio.app.state.http._transport, studio._PinnedAsyncHTTPTransport)
        assert client.get("/healthz").status_code == 200
        rendered_ui = client.get("/").text
        assert "Studio API token" in rendered_ui
        assert "t" * 40 not in rendered_ui
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/providers").status_code == 401
        auth_headers = {"Authorization": "Bearer " + "t" * 40}
        response = client.get("/api/providers", headers=auth_headers)
        assert response.status_code == 200
        audit = client.get("/api/harness/audit", headers=auth_headers)
        assert audit.status_code == 200 and audit.json()["events"] == []


def test_startup_creates_and_enforces_loopback_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "startup-auth.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN_FILE", tmp_path / "config" / "api-token")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_HOST", "127.0.0.1")
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    with TestClient(studio.app) as client:
        token = (tmp_path / "config" / "api-token").read_text(encoding="utf-8").strip()
        assert len(token) >= 32
        assert client.get("/api/providers").status_code == 401
        assert client.get("/api/providers", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_auth_cannot_be_disabled_on_remote_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "remote.db")
    monkeypatch.setattr(studio, "STUDIO_HOST", "0.0.0.0")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    with pytest.raises(RuntimeError, match="only on loopback"):
        with TestClient(studio.app):
            pass


def test_oversized_request_body_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "size.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_MAX_REQUEST_BYTES", 1024)
    with TestClient(studio.app) as client:
        response = client.post("/api/combos", json={"alias": "x", "targets": ["a"], "padding": "x" * 2048})
    assert response.status_code == 413


def test_state_changing_cross_origin_request_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "origin.db")
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "")
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", True)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", set())
    with TestClient(studio.app) as client:
        response = client.post("/api/combos", headers={"Origin": "https://evil.example"}, json={"alias": "x", "targets": ["m"]})
    assert response.status_code == 403


# =============================================================================
# AS-027: Remote tool approvals and policy enforcement
# Sensitive remote actions require scoped authenticated approval and replay
# protection: an approval decision can only be made by the same session that
# created the pending request, and a resolved approval id can never be
# re-submitted.
# =============================================================================


@pytest.fixture
def approval_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring) -> None:
    """Authenticate a TestClient against the harness approval endpoint."""
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "approve.db")
    studio.init_db()
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "t" * 40)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_HOSTS", {"testserver"})
    monkeypatch.setattr(studio, "STUDIO_ALLOWED_ORIGINS", {"http://testserver", "https://testserver"})


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_keyring: MemoryKeyring, approval_auth) -> Path:
    """Fresh initialized isolated DB path for session-creating approval tests."""
    db = tmp_path / "state" / "studio.db"
    db.parent.mkdir()
    monkeypatch.setattr(studio, "DB_PATH", db)
    studio.init_db()
    return db


def _approve_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_approve_endpoint_rejects_cross_session_submission(tmp_path: Path, approval_auth, memory_keyring: MemoryKeyring) -> None:
    session_a = studio.create_harness_session_db("scope-a", str(tmp_path), False, "standard", "local", "critic")
    pending_a = studio._create_pending_tool_approval(session_a["id"], 1, "run_bash", {"command": "echo a"})
    session_b = studio.create_harness_session_db("scope-b", str(tmp_path), False, "standard", "local", "critic")
    token = "t" * 40
    with TestClient(studio.app) as client:
        # Cross-session decision is rejected with a distinct 403 (scoped).
        response = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": pending_a["approval_id"], "decision": "approve", "session_id": session_b["id"]},
        )
        assert response.status_code == 403
        assert "scoped" in response.json()["detail"]
        # The pending request for session A is still intact.
        assert pending_a["approval_id"] in studio._PENDING_APPROVALS
        # Same-session submission succeeds.
        response = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": pending_a["approval_id"], "decision": "approve", "session_id": session_a["id"]},
        )
        assert response.status_code == 200
        assert response.json()["decision"] == "approve"
        assert studio._PENDING_APPROVALS.get(pending_a["approval_id"]) is None


def test_approve_endpoint_rejects_replay_of_resolved_id(tmp_path: Path, approval_auth, memory_keyring: MemoryKeyring) -> None:
    session = studio.create_harness_session_db("replay", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "echo"})
    token = "t" * 40
    with TestClient(studio.app) as client:
        first = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": session["id"]},
        )
        assert first.status_code == 200
        # Replay of the same id now fails closed with a 409.
        replayed = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": pending["approval_id"], "decision": "reject", "session_id": session["id"]},
        )
        assert replayed.status_code == 409
        assert "replay" in replayed.json()["detail"].lower() or "already resolved" in replayed.json()["detail"].lower()


def test_approve_endpoint_requires_authentication(tmp_path: Path, approval_auth, memory_keyring: MemoryKeyring) -> None:
    session = studio.create_harness_session_db("unauth", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "echo"})
    with TestClient(studio.app) as client:
        # No bearer token (or wrong one) is rejected at the auth boundary.
        no_auth = client.post("/api/harness/approve", json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": session["id"]})
        assert no_auth.status_code == 401
        bad_auth = client.post("/api/harness/approve", headers=_approve_headers("wrong" + "x" * 33), json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": session["id"]})
        assert bad_auth.status_code == 401
        # The pending request remains intact for a valid authenticated call.
        assert pending["approval_id"] in studio._PENDING_APPROVALS


def test_approve_endpoint_unknown_id_is_not_found(tmp_path: Path, approval_auth, memory_keyring: MemoryKeyring) -> None:
    session = studio.create_harness_session_db("unknown", str(tmp_path), False, "standard", "local", "critic")
    token = "t" * 40
    with TestClient(studio.app) as client:
        response = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": "apr-does-not-exist", "decision": "approve", "session_id": session["id"]},
        )
        assert response.status_code == 404


def test_abort_resolves_pending_approvals_and_tombstones_replay(tmp_path: Path, approval_auth, memory_keyring: MemoryKeyring) -> None:
    session = studio.create_harness_session_db("abort", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "echo"})
    token = "t" * 40
    with TestClient(studio.app) as client:
        aborted = client.post(f"/api/harness/abort/{session['id']}", headers=_approve_headers(token))
        assert aborted.status_code == 200
        # The approval is resolved (slot consumed) and tombstoned as a replay.
        assert studio._PENDING_APPROVALS.get(pending["approval_id"]) is None
        assert studio._is_replayed_approval(pending["approval_id"])
        # A late replay of the aborted id now fails closed.
        replayed = client.post(
            "/api/harness/approve",
            headers=_approve_headers(token),
            json={"approval_id": pending["approval_id"], "decision": "approve", "session_id": session["id"]},
        )
        assert replayed.status_code == 409


def test_approval_timeout_remembers_replay_tombstone(tmp_path: Path, isolated_app: Path, memory_keyring: MemoryKeyring) -> None:
    import asyncio

    session = studio.create_harness_session_db("timeout", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "echo"})
    result = asyncio.run(studio._await_tool_approval(pending, timeout=0.01))
    assert result["approved"] is False
    assert "timed out" in result["note"].lower()
    # A timed-out approval is remembered: re-submission fails closed.
    assert studio._is_replayed_approval(pending["approval_id"])
    assert pending["approval_id"] not in studio._PENDING_APPROVALS


def test_approval_decision_leaves_durable_audit_and_event_log(tmp_path: Path, isolated_app: Path, memory_keyring: MemoryKeyring) -> None:
    import asyncio

    session = studio.create_harness_session_db("audit", str(tmp_path), False, "standard", "local", "critic")
    pending = studio._create_pending_tool_approval(session["id"], 1, "run_bash", {"command": "echo"})
    slot = studio._PENDING_APPROVALS[pending["approval_id"]]
    slot["decision"] = "approve"
    slot["event"].set()
    result = asyncio.run(studio._await_tool_approval(pending))
    assert result["approved"] is True
    events = studio._read_harness_events(session["id"])
    decision_events = [e for e in events if e.get("type") == "approval/decision" and e.get("approval_id") == pending["approval_id"]]
    assert decision_events, "approval/decision event was not recorded"
    assert any(e.get("decision") == "approve" for e in decision_events)


def _init_test_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Studio Tests"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)


def test_worktree_removal_uses_registered_root_and_removes_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_test_git_repo(repo)
    worktree, branch, _ = studio._create_harness_worktree("hns-abc12345", repo)
    (worktree / "uncommitted.txt").write_text("session work", encoding="utf-8")

    studio._remove_harness_worktree(repo, worktree, branch)

    assert not worktree.exists()
    branches = subprocess.check_output(["git", "branch", "--list", branch], cwd=repo, text=True)
    assert not branches.strip()


def test_worktree_cleanup_failure_preserves_data_and_nested_paths(tmp_path: Path, monkeypatch) -> None:
    """Git errors and nested lookalike paths never trigger recursive deletion."""
    repo = tmp_path / "repo"
    _init_test_git_repo(repo)
    worktree, branch, _ = studio._create_harness_worktree("hns-abc12345", repo)
    marker = worktree / "uncommitted.txt"
    marker.write_text("precious session work", encoding="utf-8")
    nested = worktree / "project" / ".harnesses" / "wt-victim"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_text("not a registered worktree", encoding="utf-8")
    real_run = subprocess.run

    def fail_git_cleanup(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "worktree", "remove"]:
            raise subprocess.TimeoutExpired(cmd, 8.0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(studio.subprocess, "run", fail_git_cleanup)
    studio._remove_harness_worktree(repo, nested / "child", branch)

    assert marker.read_text(encoding="utf-8") == "precious session work"
    assert (nested / "keep.txt").read_text(encoding="utf-8") == "not a registered worktree"
    assert worktree.exists()
    listed = subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=repo, text=True)
    assert str(worktree.resolve()) in listed


def test_worktree_removal_refuses_nested_repository_lookalike(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_test_git_repo(repo)
    worktree, branch, _ = studio._create_harness_worktree("hns-abc12345", repo)
    nested = repo / "project" / ".harnesses" / "wt-victim"
    nested.mkdir(parents=True)
    marker = nested / "data.txt"
    marker.write_text("keep", encoding="utf-8")

    studio._remove_harness_worktree(repo, nested, branch)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert worktree.exists()
