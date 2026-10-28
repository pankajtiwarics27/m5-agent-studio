"""Fail-closed policy contract for optional private-network access."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from pathlib import Path


class GatewayConfigurationError(ValueError):
    """Raised when optional remote gateway configuration is unsafe or incomplete."""


PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _path(value: Path | str, field: str) -> Path:
    raw = str(value)
    if not raw or "\x00" in raw:
        raise GatewayConfigurationError(f"{field} must be a non-empty path without NUL characters.")
    return Path(raw).expanduser()


def _token(value: str, field: str = "api_token") -> str:
    if not isinstance(value, str) or len(value) < 32 or any(char.isspace() for char in value):
        raise GatewayConfigurationError(f"{field} must contain at least 32 non-whitespace characters.")
    return value


def _private_address(value: str, field: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise GatewayConfigurationError(f"{field} must be a private IP address.") from exc
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise GatewayConfigurationError(f"{field} must be inside a private network.")
    return address


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Validated optional gateway settings; disabled is the safe default."""

    enabled: bool = False
    bind_host: str = "127.0.0.1"
    api_token: str | None = None
    tls_certificate: Path | None = None
    tls_private_key: Path | None = None
    client_networks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise GatewayConfigurationError("enabled must be a boolean.")
        if not isinstance(self.bind_host, str) or not self.bind_host.strip():
            raise GatewayConfigurationError("bind_host must be a non-empty IP address.")
        bind_host = self.bind_host.strip()
        object.__setattr__(self, "bind_host", bind_host)
        if self.enabled:
            _private_address(bind_host, "bind_host")
            if self.api_token is None:
                raise GatewayConfigurationError("enabled gateway requires api_token.")
            _token(self.api_token)
            if self.tls_certificate is None or self.tls_private_key is None:
                raise GatewayConfigurationError("enabled gateway requires TLS certificate and private key.")
            for path, field in (
                (self.tls_certificate, "tls_certificate"),
                (self.tls_private_key, "tls_private_key"),
            ):
                normalized = _path(path, field)
                if not normalized.is_file() or not normalized.read_bytes():
                    raise GatewayConfigurationError(f"{field} must reference a non-empty regular file.")
                object.__setattr__(self, field, normalized)
        elif self.api_token is not None:
            _token(self.api_token)

        networks = []
        for raw_network in self.client_networks:
            try:
                network = ipaddress.ip_network(raw_network, strict=False)
            except ValueError as exc:
                raise GatewayConfigurationError("client_networks must contain valid IP networks.") from exc
            if network.version == 4:
                covered = any(
                    isinstance(private, ipaddress.IPv4Network) and network.subnet_of(private)
                    for private in PRIVATE_NETWORKS
                )
            else:
                covered = any(
                    isinstance(private, ipaddress.IPv6Network) and network.subnet_of(private)
                    for private in PRIVATE_NETWORKS
                )
            if not covered:
                raise GatewayConfigurationError("client_networks must be private networks.")
            networks.append(str(network))
        object.__setattr__(self, "client_networks", tuple(sorted(set(networks))))

    @property
    def effective_client_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        raw = self.client_networks or tuple(str(network) for network in PRIVATE_NETWORKS)
        return tuple(ipaddress.ip_network(network) for network in raw)

    def authenticate(self, supplied_token: str | None) -> bool:
        """Compare bearer material without revealing which check failed."""
        if not self.enabled or self.api_token is None or not isinstance(supplied_token, str):
            return False
        return hmac.compare_digest(self.api_token, supplied_token)

    def allows_client(self, client_ip: str) -> bool:
        """Allow only explicitly configured or default private-network clients."""
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return self.enabled and any(address in network for network in self.effective_client_networks)

    def authorize(self, client_ip: str, supplied_token: str | None) -> bool:
        return self.authenticate(supplied_token) and self.allows_client(client_ip)
