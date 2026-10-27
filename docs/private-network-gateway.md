# Private-network remote gateway

AS-024 defines the optional gateway policy in
[`private_network_gateway.py`](../private_network_gateway.py). `GatewayConfig`
is disabled by default. Enabling it requires:

- a private, non-wildcard bind address;
- a bearer token with at least 32 non-whitespace characters;
- existing non-empty TLS certificate and private-key files; and
- optional client CIDRs that are subsets of RFC1918 or ULA private networks.

Authentication and client-network checks are both required. Public addresses,
wildcard binds, invalid CIDRs, missing TLS files, weak tokens, loopback clients,
and unconfigured clients fail closed. The module validates policy only; it does
not bind sockets, terminate TLS, or expose an HTTP route.

## Evidence

`tests/test_private_network_gateway.py` covers the disabled default, complete
TLS/token requirements, private bind/client enforcement, token comparison,
CIDR allowlists, and invalid configuration rejection.
