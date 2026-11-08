"""LC-08 routing fence; no discovery, native I/O, qualification or activation.

Legacy is the unchanged rollout default. Coordinated construction is an internal
composition seam, not runtime/grant authority. No production backend is installed.
Backend implementations must enforce LC-02--07 before native effects; the generic
HTTP/provider escape hatches remain forbidden even when a backend is present.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit


_DISPATCH_CODES = frozenset(
    {
        "coordinated_backend_unavailable",
        "canonical_consumer_required",
        "legacy_transport_blocked",
        "coordinated_combo_unsupported",
        "unowned_eviction_forbidden",
        "invalid_dispatch_request",
        "coordinated_execution_unavailable",
    }
)


class RuntimeDispatchDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = (
            code if code in _DISPATCH_CODES else "coordinated_execution_unavailable"
        )
        super().__init__(self.code)

    def to_mapping(self) -> dict[str, Any]:
        return {"code": self.code, "retryable": False}


def _dispatch_origin(url: str) -> tuple[str, str, int | None]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError
        return (
            parsed.scheme,
            parsed.hostname.lower().rstrip("."),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except (TypeError, ValueError):
        raise RuntimeDispatchDenied("invalid_dispatch_request") from None


def _dispatch_private(url: str) -> bool:
    _, host, _ = _dispatch_origin(url)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False  # Actual DNS/private authorization remains the pinned transport's job.


@dataclass(frozen=True, slots=True)
class RuntimeDispatchCall:
    """Owned application input, NOT a prepared wire request or load authorization."""

    runtime: str
    model: str
    action: Literal["infer", "load", "unload", "unload_all", "clear_cache"]
    payload: bytes = b"{}"

    def __post_init__(self) -> None:
        if (
            self.runtime not in ("omlx", "ollama", "lmstudio")
            or not isinstance(self.model, str)
            or not self.model
            or len(self.model) > 512
            or not self.model.isprintable()
            or self.action
            not in ("infer", "load", "unload", "unload_all", "clear_cache")
            or not isinstance(self.payload, bytes)
            or len(self.payload) > 4 * 1024 * 1024
        ):
            raise RuntimeDispatchDenied("invalid_dispatch_request")


class CoordinatedDispatchBackend(Protocol):
    """Trusted composition only. No generic HTTP client is passed to the backend.

    A production implementation must own LC-02/03 coordination/admission, LC-04--06
    qualified transports, LC-07 full final-wire preparation/count and independent
    native terminality. Returning events is not a substitute for those proofs.
    This protocol alone grants no native-operation authority.
    """

    def stream(self, call: RuntimeDispatchCall) -> AsyncIterator[dict[str, Any]]: ...
    async def command(self, call: RuntimeDispatchCall) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RuntimeDispatchBoundary:
    coordinated: bool = False
    backends: Mapping[str, CoordinatedDispatchBackend] | None = None
    # Installed composition binds provider aliases to native backends. Never infer
    # LM Studio identity or native qualification just from a port/model name.
    provider_runtimes: Mapping[str, str] | None = None
    runtime_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.coordinated) is not bool:
            raise RuntimeDispatchDenied("invalid_dispatch_request")
        backends = dict(self.backends or {})
        aliases = dict(self.provider_runtimes or {})
        if set(backends) - {"omlx", "ollama", "lmstudio"} or any(
            not isinstance(k, str) or not k or v not in ("omlx", "ollama", "lmstudio")
            for k, v in aliases.items()
        ):
            raise RuntimeDispatchDenied("invalid_dispatch_request")
        for backend in backends.values():
            if not callable(getattr(backend, "stream", None)) or not callable(
                getattr(backend, "command", None)
            ):
                raise RuntimeDispatchDenied("invalid_dispatch_request")
        object.__setattr__(self, "runtime_origins", tuple(self.runtime_origins))
        for url in self.runtime_origins:
            _dispatch_origin(url)
        object.__setattr__(self, "backends", MappingProxyType(backends))
        object.__setattr__(self, "provider_runtimes", MappingProxyType(aliases))

    def target(self, kind: str, provider: Mapping[str, Any] | None) -> str | None:
        if not self.coordinated:
            return None
        if kind == "combo":
            # Until a complete consent/fit-aware combo executor is installed, do
            # not convert a local refusal/uncertain effect into another target.
            raise RuntimeDispatchDenied("coordinated_combo_unsupported")
        if kind == "omlx":
            return "omlx"
        if kind != "cloud" or provider is None:
            raise RuntimeDispatchDenied("invalid_dispatch_request")
        alias = str(provider.get("alias") or "")
        assert self.provider_runtimes is not None
        if alias in self.provider_runtimes:
            return self.provider_runtimes[alias]
        if alias == "ollama":
            return "ollama"
        url = str(provider.get("base_url") or "")
        origin = _dispatch_origin(url)
        if (
            origin in {_dispatch_origin(u) for u in self.runtime_origins}
            or _dispatch_private(url)
            or provider.get("allow_private")
            or provider.get("builtin_private_ok")
        ):
            # A generic local/proxy provider is not qualified cloud/native routing.
            raise RuntimeDispatchDenied("coordinated_backend_unavailable")
        return None

    def _backend(self, runtime: str) -> CoordinatedDispatchBackend:
        assert self.backends is not None
        backend = self.backends.get(runtime)
        if not self.coordinated or backend is None:
            raise RuntimeDispatchDenied("coordinated_backend_unavailable")
        return backend

    async def stream(
        self,
        runtime: str,
        model: str,
        messages: object,
        *,
        tools: object,
        temperature: float,
        max_tokens: int,
        reasoning_mode: str,
    ) -> AsyncIterator[dict[str, Any]]:
        backend = self._backend(runtime)
        try:
            # No sanitizer/compactor/cap/reasoning downgrade. Own one immutable
            # copy of all supplied application input before an async boundary.
            payload = json.dumps(
                dict(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_mode=reasoning_mode,
                ),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
            call = RuntimeDispatchCall(runtime, model, "infer", payload)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise RuntimeDispatchDenied("invalid_dispatch_request") from None
        iterator = None
        try:
            iterator = backend.stream(call)
            async for item in iterator:
                yield item
        except RuntimeDispatchDenied:
            raise
        except Exception:
            # An unsupported/failed backend can never fall through to legacy I/O.
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await (
                    close()
                )  # Cancellation/consumer close must reach the native bridge.

    async def command(
        self,
        runtime: str,
        model: str,
        action: Literal["load", "unload", "unload_all", "clear_cache"],
    ) -> dict[str, Any]:
        backend = self._backend(runtime)
        try:
            result = await backend.command(RuntimeDispatchCall(runtime, model, action))
            if (
                not isinstance(result, dict)
                or set(result) != {"status"}
                or result["status"]
                not in ("confirmed", "pending", "refused", "uncertain")
            ):
                raise RuntimeDispatchDenied("coordinated_execution_unavailable")
            return result
        except RuntimeDispatchDenied:
            raise
        except Exception:
            raise RuntimeDispatchDenied("coordinated_execution_unavailable") from None

    def forbid_legacy(self) -> None:
        if self.coordinated:
            raise RuntimeDispatchDenied("legacy_transport_blocked")

    def guard_http(
        self, method: str, url: str, *, resolved_addresses: tuple[str, ...] = ()
    ) -> None:
        if not self.coordinated:
            return
        parsed = urlsplit(url)
        origin = _dispatch_origin(url)
        known = origin in {_dispatch_origin(u) for u in self.runtime_origins}
        path = unquote(parsed.path).lower().rstrip("/")
        private_pin = any(
            not ipaddress.ip_address(ip).is_global for ip in resolved_addresses
        )
        effect_path = any(
            s in path
            for s in (
                "/chat/completions",
                "/completions",
                "/messages",
                "/generate",
                "/tokenize",
                "/count_tokens",
                "/api/chat",
                "/load",
                "/unload",
                "/admin/api",
            )
        )
        # Known runtime origins have no raw effect path. For unknown private/proxy
        # origins, forbid recognizable generation/tokenizer/control endpoints.
        # Metadata GETs (tags/ps/models) remain observations, not readiness proof.
        observed_paths = {
            "/v1/models",
            "/api/tags",
            "/api/ps",
            "/api/version",
            "/health",
            "/healthz",
        }
        if (
            known and not (method.upper() in ("GET", "HEAD") and path in observed_paths)
        ) or (effect_path and (known or private_pin or _dispatch_private(url))):
            self.forbid_legacy()
