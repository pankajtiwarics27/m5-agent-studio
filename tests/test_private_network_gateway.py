from __future__ import annotations

from pathlib import Path

import pytest

from private_network_gateway import GatewayConfig, GatewayConfigurationError

TOKEN = "t" * 48


def _tls_files(tmp_path: Path) -> tuple[Path, Path]:
    certificate = tmp_path / "server.crt"
    private_key = tmp_path / "server.key"
    certificate.write_text("certificate", encoding="utf-8")
    private_key.write_text("private-key", encoding="utf-8")
    return certificate, private_key


def test_gateway_is_disabled_by_default() -> None:
    gateway = GatewayConfig()
    assert gateway.enabled is False
    assert gateway.authenticate(TOKEN) is False
    assert gateway.allows_client("192.168.1.10") is False


def test_enabled_gateway_requires_tls_strong_token_and_private_client(tmp_path: Path) -> None:
    certificate, private_key = _tls_files(tmp_path)
    gateway = GatewayConfig(
        enabled=True,
        bind_host="192.168.1.20",
        api_token=TOKEN,
        tls_certificate=certificate,
        tls_private_key=private_key,
        client_networks=("192.168.1.0/24",),
    )
    assert gateway.authenticate(TOKEN) is True
    assert gateway.authorize("192.168.1.42", TOKEN) is True
    assert gateway.authorize("192.168.2.42", TOKEN) is False
    assert gateway.authorize("192.168.1.42", "wrong") is False


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True, "bind_host": "0.0.0.0"},
        {"enabled": True, "bind_host": "8.8.8.8"},
        {"enabled": True, "api_token": "short"},
        {"enabled": True, "client_networks": ("8.8.8.0/24",)},
    ],
)
def test_invalid_private_gateway_settings_fail_closed(tmp_path: Path, changes: dict[str, object]) -> None:
    certificate, private_key = _tls_files(tmp_path)
    values: dict[str, object] = {
        "enabled": True,
        "bind_host": "10.0.0.5",
        "api_token": TOKEN,
        "tls_certificate": certificate,
        "tls_private_key": private_key,
    }
    values.update(changes)
    with pytest.raises(GatewayConfigurationError):
        GatewayConfig(**values)  # type: ignore[arg-type]


def test_enabled_gateway_rejects_missing_or_empty_tls_files(tmp_path: Path) -> None:
    certificate = tmp_path / "empty.crt"
    certificate.touch()
    with pytest.raises(GatewayConfigurationError, match="non-empty"):
        GatewayConfig(
            enabled=True,
            bind_host="10.0.0.5",
            api_token=TOKEN,
            tls_certificate=certificate,
            tls_private_key=tmp_path / "missing.key",
        )
