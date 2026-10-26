from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    base_dir = tmp_path / "application"
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    base_dir.mkdir()
    cwd.mkdir()
    home.mkdir()
    return base_dir, cwd, home


def test_invalid_process_environment_fails_during_app_import_with_actionable_error() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "STUDIO_HOST": "127.0.0.1",
            "STUDIO_API_TOKEN": "",
            "STUDIO_AUTH_DISABLED": "0",
            "STUDIO_EXECUTION_MODE": "disabled",
            "STUDIO_PORT": "not-a-port",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "STUDIO_PORT must be an integer between 1 and 65535" in result.stderr
    assert "not-a-port" not in result.stderr


def test_typed_settings_defaults_and_environment_precedence(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    defaults = app.load_settings({}, base_dir=base_dir, cwd=cwd, home=home)

    assert defaults.port == 3000
    assert isinstance(defaults.port, int)
    assert defaults.host == "127.0.0.1"
    assert defaults.execution_mode == "disabled"
    assert defaults.database_path == base_dir / "studio_history.db"
    assert defaults.api_token_file == home / ".config" / "m5-agent-studio" / "api-token"
    assert defaults.workspace_root == cwd.resolve()
    assert defaults.allowed_hosts == frozenset({"localhost", "127.0.0.1", "[::1]"})

    explicit = app.load_settings(
        {
            "STUDIO_HOST": "0.0.0.0",
            "STUDIO_API_TOKEN": "t" * 48,
            "STUDIO_PORT": "4312",
            "STUDIO_DB_PATH": "~/state/studio.db",
            "STUDIO_WORKSPACE": "project",
            "STUDIO_ALLOWED_HOSTS": "EXAMPLE.test,localhost",
            "STUDIO_ALLOWED_ORIGINS": "https://ui.example.test/",
            "STUDIO_EXECUTION_MODE": "sandbox",
            "STUDIO_MAX_REQUEST_BYTES": "4096",
            "STUDIO_EXPENSIVE_REQUESTS_PER_MINUTE": "12",
        },
        base_dir=base_dir,
        cwd=cwd,
        home=home,
    )
    assert explicit.port == 4312
    assert explicit.host == "0.0.0.0"
    assert explicit.database_path == home / "state/studio.db"
    assert explicit.workspace_root == (cwd / "project").resolve()
    assert explicit.allowed_hosts == frozenset({"example.test", "localhost"})
    assert explicit.allowed_origins == frozenset({"https://ui.example.test"})
    assert explicit.execution_mode == "sandbox"
    assert explicit.max_request_bytes == 4096
    assert explicit.expensive_requests_per_minute == 12


def test_typed_settings_preserve_boolean_and_legacy_execution_precedence(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    settings = app.load_settings(
        {
            "STUDIO_ALLOW_HOST_EXECUTION": "yes",
            "STUDIO_EXECUTION_MODE": "disabled",
            "STUDIO_ENABLE_API_DOCS": "on",
            "STUDIO_EXTERNAL_STATIC": "1",
            "STUDIO_AUTH_DISABLED": "off",
        },
        base_dir=base_dir,
        cwd=cwd,
        home=home,
    )
    assert settings.allow_host_execution is True
    assert settings.execution_mode == "disabled"
    assert settings.enable_api_docs is True
    assert settings.external_static is True
    assert settings.auth_disabled is False

    legacy = app.load_settings(
        {"STUDIO_ALLOW_HOST_EXECUTION": "1"},
        base_dir=base_dir,
        cwd=cwd,
        home=home,
    )
    assert legacy.execution_mode == "host"


def test_settings_repr_does_not_disclose_api_token(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    token = "sensitive-api-token-value-0123456789"
    settings = app.load_settings(
        {"STUDIO_API_TOKEN": token},
        base_dir=base_dir,
        cwd=cwd,
        home=home,
    )
    assert settings.api_token == token
    assert token not in repr(settings)


def test_empty_boolean_keeps_default_and_settings_are_immutable(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    settings = app.load_settings(
        {"STUDIO_AUTH_DISABLED": "", "STUDIO_ENABLE_API_DOCS": ""},
        base_dir=base_dir,
        cwd=cwd,
        home=home,
    )
    assert settings.auth_disabled is False
    assert settings.enable_api_docs is False
    with pytest.raises((AttributeError, TypeError)):
        settings.port = 4000  # type: ignore[misc]


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"STUDIO_PORT": "three thousand"}, "STUDIO_PORT must be an integer"),
        ({"STUDIO_PORT": "65536"}, "STUDIO_PORT must be between 1 and 65535"),
        ({"STUDIO_MAX_REQUEST_BYTES": "100"}, "STUDIO_MAX_REQUEST_BYTES must be between 1024 and 104857600"),
        ({"STUDIO_AUTH_DISABLED": "sometimes"}, "STUDIO_AUTH_DISABLED must be a boolean"),
        ({"STUDIO_EXTERNAL_STATIC": "maybe"}, "STUDIO_EXTERNAL_STATIC must be a boolean"),
        ({"STUDIO_DB_PATH": ""}, "STUDIO_DB_PATH must be a non-empty filesystem path"),
        ({"STUDIO_API_TOKEN_FILE": "/invalid\x00path"}, "STUDIO_API_TOKEN_FILE must not contain a NUL character"),
        ({"STUDIO_EXECUTION_MODE": "host"}, "STUDIO_EXECUTION_MODE=host requires STUDIO_ALLOW_HOST_EXECUTION=1"),
        ({"STUDIO_HOST": "0.0.0.0"}, "non-loopback STUDIO_HOST requires STUDIO_API_TOKEN"),
        ({"STUDIO_API_TOKEN": "short"}, "STUDIO_API_TOKEN must contain at least 32 characters"),
        ({"OMLX_BASE_URL": "ftp://example.test/models"}, "OMLX_BASE_URL must be an absolute HTTP"),
        ({"STUDIO_ALLOWED_ORIGINS": "https://user:secret@example.test/path"}, "STUDIO_ALLOWED_ORIGINS entries must be HTTP"),
    ],
)
def test_invalid_configuration_fails_with_variable_specific_guidance(
    environ: dict[str, str],
    message: str,
    config_paths: tuple[Path, Path, Path],
) -> None:
    base_dir, cwd, home = config_paths
    with pytest.raises(app.ConfigurationError, match=message):
        app.load_settings(environ, base_dir=base_dir, cwd=cwd, home=home)


def test_invalid_origin_error_does_not_echo_configured_credentials(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    with pytest.raises(app.ConfigurationError) as error:
        app.load_settings(
            {"STUDIO_ALLOWED_ORIGINS": "https://user:sensitive-value@example.test/path"},
            base_dir=base_dir,
            cwd=cwd,
            home=home,
        )
    assert "STUDIO_ALLOWED_ORIGINS" in str(error.value)
    assert "sensitive-value" not in str(error.value)


def test_local_bind_cannot_disable_auth_for_remote_interface(config_paths: tuple[Path, Path, Path]) -> None:
    base_dir, cwd, home = config_paths
    with pytest.raises(app.ConfigurationError, match="only with a loopback STUDIO_HOST"):
        app.load_settings(
            {"STUDIO_HOST": "192.0.2.20", "STUDIO_AUTH_DISABLED": "true"},
            base_dir=base_dir,
            cwd=cwd,
            home=home,
        )
