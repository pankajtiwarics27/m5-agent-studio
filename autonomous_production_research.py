"""AP-009 — source-backed safe search, reading and repository inspection.

`AP-009` (Phase P2) adds source-backed research to the Autonomous Production
program. Everything public goes through one admit-by-default boundary; the
module never opens a socket of its own. A caller injects a fetcher
(``Callable[[FetchRequest], FetchResponse]``) and a DNS resolver
(``Callable[[str], Sequence[str]]``); this module validates every request,
every response hop and every resolved address against one policy derived
from the owner decisions recorded 2026-11-17 (issues #330 and #332):

* public ``HTTPS GET`` only, to exactly the standard public allowlist:
  ``github.com``, ``codeload.github.com``, ``api.github.com``,
  ``en.wikipedia.org``, ``pypi.org``, ``registry.npmjs.org``;
* zero spend, no accounts, no writes, no credentialed services (the module
  refuses URLs carrying userinfo and refuses any method other than GET);
* redirects are validated hop-by-hop, capped, and never allowed to leave the
  allowlist or the literal-IP/DNS block rules;
* private, loopback, link-local (cloud metadata service included), CGNAT,
  documentation, benchmark, multicast, reserved and IPv4-mapped-IPv6
  addresses are unreachable from this module;
* textual content is bounded; downloads are capped by bytes; excerpts and
  record sizes are bounded; repository inspection is read-only with explicit
  file, per-file, total-byte and count caps;
* missing sources are kept explicit: no invented citations, no invented
  publication dates, no silent contradiction resolution, no invented
  success claims. Provider failure produces an honest ``incomplete`` bundle,
  never invented facts.

This module does not wire anything into the running Studio, the Harness, any
dormancy layer or any UI surface; it is the contract layer only. Activation,
installation, or any other use requires its own story and owner decision.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit


class ResearchContractError(ValueError):
    """Raised when the caller's request or configuration violates the contract.

    Policy *refusals* are recorded on the produced records (``refused``); this
    exception is for programmer/caller misuse only.
    """


# ---------------------------------------------------------------------------
# Policy: owner decision U03 (2026-11-17, issue #330) verbatim allowlist.
# ---------------------------------------------------------------------------

PUBLIC_READ_ALLOWLIST: frozenset[str] = frozenset(
    {
        "github.com",
        "codeload.github.com",
        "api.github.com",
        "en.wikipedia.org",
        "pypi.org",
        "registry.npmjs.org",
    }
)

#: CIDR blocks no ordinary public page may resolve into. Presence on the
#: allowlist does not excuse a host whose dns ever resolves here.
_BLOCKED_V4: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_BLOCKED_V6: tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(cidr)
    for cidr in (
        "::1/128",
        "::/128",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "2001:db8::/32",
    )
)

DEFAULT_MAX_SOURCES = 12
DEFAULT_MAX_BYTES_PER_SOURCE = 512 * 1024
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_EXCERPT_CHARS = 4000
DEFAULT_MAX_EXPIRY_DAYS = 30  # retention cap for captured source snapshots (U04)

_MAX_URL_CHARS = 2048
_MAX_TEXT_CHARS = 20_000
_ALLOWED_PORTS = {443}


@dataclass(frozen=True, slots=True)
class SourceBudget:
    """Hard limits for one research bundle; constructed from U03/U04 defaults."""

    max_sources: int = DEFAULT_MAX_SOURCES
    max_bytes_per_source: int = DEFAULT_MAX_BYTES_PER_SOURCE
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS
    max_source_snapshot_days: int = DEFAULT_MAX_EXPIRY_DAYS

    def __post_init__(self) -> None:
        for name in (
            "max_sources",
            "max_bytes_per_source",
            "max_total_bytes",
            "max_redirects",
            "max_excerpt_chars",
            "max_source_snapshot_days",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ResearchContractError(f"SourceBudget.{name} must be a non-negative integer.")


# ---------------------------------------------------------------------------
# Egress boundary helpers (pure; unit-testable without network).
# ---------------------------------------------------------------------------


def is_blocked_ip(value: str) -> bool:
    """True when `value` is an IP inside a blocked range (v4, v6, or mapped)."""
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        mapped_check = _is_blocked_v4(addr)
        return mapped_check
    # Any IPv4-mapped/translated v6 address must trip the v4 rules.
    if addr.ipv4_mapped is not None:
        return _is_blocked_v4(addr.ipv4_mapped)
    return any(addr in net for net in _BLOCKED_V6)


def _is_blocked_v4(addr: ipaddress.IPv4Address) -> bool:
    return any(addr in net for net in _BLOCKED_V4)


def _parse_http_url(raw: str) -> tuple[str, str, int]:
    """Split/normalize; does not consult DNS or the allowlist."""
    if not isinstance(raw, str):
        raise ResearchContractError("URL must be a string.")
    url = raw.strip()
    if not url or len(url) > _MAX_URL_CHARS or any(ord(ch) < 0x20 for ch in url):
        raise ResearchContractError("URL is empty, too long or contains control characters.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ResearchContractError(f"URL does not parse: {exc}.") from exc
    scheme = (parts.scheme or "").lower()
    if scheme != "https":
        raise ResearchContractError("Only https:// URLs are allowed.")
    host = parts.hostname
    if not host:
        raise ResearchContractError("URL must contain a host.")
    host = host.lower().rstrip(".")
    if parts.username or parts.password:
        raise ResearchContractError("URLs carrying userinfo/credentials are refused.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ResearchContractError(f"URL port is invalid: {exc}.") from exc
    effective_port = port if port is not None else 443
    if effective_port not in _ALLOWED_PORTS:
        raise ResearchContractError("Only TCP port 443 is allowed.")
    return host, parts.path or "/", effective_port


def validate_public_url(
    raw: str,
    *,
    allowlist: Iterable[str] = PUBLIC_READ_ALLOWLIST,
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
) -> tuple[str, str, int]:
    """Validate a URL against the public allowlist & DNS rules. Returns host, path, port.

    Raises ResearchContractError on any policy violation. `dns_resolver`, when
    supplied, MUST return every address the name maps to; any one of them
    being blocked makes the URL refused. Literal-IP hosts are checked with
    the same rules and never reach DNS.
    """
    host, path, port = _parse_http_url(raw)
    allowed = {h.lower() for h in allowlist}
    if host in allowed:
        # Allowlisted host: DNS, when resolvable, must stay public.
        if dns_resolver is not None and not _literal_ip(host):
            for address in _safe_resolve(host, dns_resolver):
                if is_blocked_ip(address):
                    raise ResearchContractError(f"{host} resolves to a blocked address ({address}).")
        return host, path, port
    if _literal_ip(host):
        if host not in allowed or is_blocked_ip(host):
            raise ResearchContractError(f"Literal IP host {host} is not allowed.")
        return host, path, port
    raise ResearchContractError(f"Host {host} is not on the public allowlist.")


def _literal_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _safe_resolve(
    host: str, resolver: Callable[[str], Sequence[str]]
) -> list[str]:
    try:
        results = list(resolver(host))
    except ResearchContractError:
        raise
    except Exception as exc:  # resolver failure must fail closed, never open the gate
        raise ResearchContractError(f"DNS resolution for {host} failed: {exc}.") from exc
    results = [str(r) for r in results if str(r).strip()]
    if not results:
        raise ResearchContractError(f"DNS resolution for {host} returned no addresses.")
    return results


def validate_redirect_chain(
    start_url: str,
    redirects: Sequence[str],
    *,
    allowlist: Iterable[str] = PUBLIC_READ_ALLOWLIST,
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> str:
    """Validate the whole hop sequence; returns the final URL. Raises on violation."""
    if len(redirects) > max_redirects:
        raise ResearchContractError(f"Redirect chain exceeds {max_redirects} hops.")
    current = start_url
    validate_public_url(current, allowlist=allowlist, dns_resolver=dns_resolver)
    for hop in redirects:
        validate_public_url(hop, allowlist=allowlist, dns_resolver=dns_resolver)
        current = hop
    return current


# ---------------------------------------------------------------------------
# Fetch boundary. The module never opens sockets; all I/O is injected.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    method: str = "GET"
    timeout_s: float = 15.0
    max_body_bytes: int = DEFAULT_MAX_BYTES_PER_SOURCE


@dataclass(frozen=True, slots=True)
class FetchResponse:
    status: int
    final_url: str
    redirects: tuple[str, ...]
    body: bytes
    headers: Mapping[str, str]
    retrieved_at: str


class Fetcher(Protocol):
    def __call__(self, request: FetchRequest) -> FetchResponse: ...


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _excerpt(body: bytes, limit: int) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("utf-8", errors="replace")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _json_object(body: bytes) -> dict[str, Any] | None:
    """Parse a JSON object from the retained body, or None.

    Deliberately independent of ``Content-Type`` (untrusted, and adapters must
    not silently degrade when a provider omits it). The body is already bounded
    by the per-source byte cap, so this can never read an unbounded payload.
    """
    if not body:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


_HTTP_PUBDATE = [
    "date",
    "last-modified",
]


def _publication_date_from_headers(headers: Mapping[str, str]) -> str | None:
    for key in _HTTP_PUBDATE:
        value = headers.get(key) or headers.get(key.title())
        if value:
            parsed = _parse_http_date(value.strip())
            if parsed:
                return parsed
    return None


def _parse_http_date(value: str) -> str | None:
    for fmt in (
        "%a, %d %b %Y %H:%M:%S GMT",
        "%A, %d-%b-%y %H:%M:%S GMT",
        "%a %b %d %H:%M:%S %Y",
    ):
        try:
            return _dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One observation of one public source. Publication date is never invented."""

    source_id: str
    url: str
    final_url: str | None
    retrieved_at: str
    http_status: int | None
    retrieval_status: str  # ok | partial | failed | refused
    content_sha256: str | None
    excerpt: str | None
    publication_date: str | None
    byte_count: int
    reason: str | None
    #: Reported body length before the per-source cap was applied. Equal to
    #: ``byte_count`` when nothing was dropped, so truncation is never silent.
    upstream_byte_count: int | None = None

    @property
    def is_failure(self) -> bool:
        return self.retrieval_status in ("failed", "refused")

    @property
    def is_incomplete(self) -> bool:
        return self.retrieval_status != "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "final_url": self.final_url,
            "retrieved_at": self.retrieved_at,
            "http_status": self.http_status,
            "retrieval_status": self.retrieval_status,
            "content_sha256": self.content_sha256,
            "excerpt": self.excerpt,
            "publication_date": self.publication_date,
            "byte_count": self.byte_count,
            "reason": self.reason,
            "upstream_byte_count": self.upstream_byte_count,
        }


def _no_body(record: SourceRecord) -> tuple[SourceRecord, bytes]:
    """Bundle a record that produced no retained body (refused/failed fetch)."""
    return record, b""


def _source_id(url: str, retrieved_at: str) -> str:
    return "src-" + hashlib.sha256(f"{url}|{retrieved_at}".encode()).hexdigest()[:24]


def guarded_fetch(
    fetcher: Fetcher,
    request: FetchRequest,
    *,
    budget: SourceBudget = SourceBudget(),
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
    allowlist: Iterable[str] = PUBLIC_READ_ALLOWLIST,
    clock: Callable[[], str] = _now_iso,
) -> SourceRecord:
    """Fetch one source through policy gates; records refusals instead of raising."""
    record, _ = guarded_fetch_with_body(
        fetcher,
        request,
        budget=budget,
        dns_resolver=dns_resolver,
        allowlist=allowlist,
        clock=clock,
    )
    return record


def guarded_fetch_with_body(
    fetcher: Fetcher,
    request: FetchRequest,
    *,
    budget: SourceBudget = SourceBudget(),
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
    allowlist: Iterable[str] = PUBLIC_READ_ALLOWLIST,
    clock: Callable[[], str] = _now_iso,
) -> tuple[SourceRecord, bytes]:
    """As :func:`guarded_fetch`, also returning the retained (byte-capped) body.

    Adapters need the body to parse provider payloads. It is the single body
    that was hashed into the record, so a parsed result can always be traced to
    the exact bytes the record describes — and it is already bounded by the
    per-source cap, so no adapter can hold an unbounded payload.
    """
    retrieved = clock()
    try:
        host, path, port = _parse_http_url(request.url)
        if request.method != "GET":
            raise ResearchContractError("Only GET is allowed.")
        validate_public_url(
            request.url, allowlist=allowlist, dns_resolver=dns_resolver
        )
    except ResearchContractError as exc:
        refusal_reason = str(exc)
        return _no_body(SourceRecord(
            source_id=_source_id(getattr(request, "url", "invalid"), retrieved),
            url=getattr(request, "url", "invalid"),
            final_url=None,
            retrieved_at=retrieved,
            http_status=None,
            retrieval_status="refused",
            content_sha256=None,
            excerpt=None,
            publication_date=None,
            byte_count=0,
            reason=refusal_reason,
        ))

    try:
        response = fetcher(
            FetchRequest(
                url=request.url,
                method="GET",
                timeout_s=request.timeout_s,
                max_body_bytes=min(int(request.max_body_bytes), budget.max_bytes_per_source),
            )
        )
    except Exception as exc:
        return _no_body(SourceRecord(
            source_id=_source_id(request.url, retrieved),
            url=request.url,
            final_url=None,
            retrieved_at=retrieved,
            http_status=None,
            retrieval_status="failed",
            content_sha256=None,
            excerpt=None,
            publication_date=None,
            byte_count=0,
            reason=f"fetcher error: {exc}",
        ))

    try:
        final = validate_redirect_chain(
            request.url,
            response.redirects,
            allowlist=allowlist,
            dns_resolver=dns_resolver,
            max_redirects=budget.max_redirects,
        )
        # The final hop must be what the fetcher actually served.
        validate_public_url(response.final_url, allowlist=allowlist, dns_resolver=dns_resolver)
        if response.final_url != final:
            raise ResearchContractError(
                "fetcher's reported final_url does not match the validated redirect chain."
            )
    except ResearchContractError as exc:
        return _no_body(SourceRecord(
            source_id=_source_id(request.url, retrieved),
            url=request.url,
            final_url=response.final_url,
            retrieved_at=retrieved,
            http_status=response.status,
            retrieval_status="refused",
            content_sha256=None,
            excerpt=None,
            publication_date=None,
            byte_count=0,
            reason=str(exc),
        ))

    body = response.body
    upstream_byte_count = len(body)
    byte_count = upstream_byte_count
    status: str = "ok"
    reason: str | None = None
    if byte_count > budget.max_bytes_per_source:
        body = body[: budget.max_bytes_per_source]
        status = "partial"
        reason = f"response exceeded the {budget.max_bytes_per_source}-byte source cap; truncated."
    elif byte_count == 0:
        status = "partial"
        reason = "empty response body."
    if response.status >= 400:
        status = "failed" if response.status >= 500 else "partial"
        reason = f"upstream returned HTTP {response.status}."

    return SourceRecord(
        source_id=_source_id(request.url, retrieved),
        url=request.url,
        final_url=response.final_url,
        retrieved_at=retrieved,
        http_status=response.status,
        retrieval_status=status,
        content_sha256=_sha256(body),
        excerpt=_excerpt(body, budget.max_excerpt_chars),
        publication_date=_publication_date_from_headers(response.headers),
        byte_count=len(body),
        reason=reason,
        upstream_byte_count=upstream_byte_count,
    ), body


# ---------------------------------------------------------------------------
# Claims, sources and contradictions — kept explicit, never invented.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    text: str
    status: str  # supported | unsupported | contradicted
    supporting: tuple[str, ...]
    contradicting: tuple[str, ...]
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "status": self.status,
            "supporting": list(self.supporting),
            "contradicting": list(self.contradicting),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ContradictionRecord:
    contradiction_id: str
    claim_id: str
    source_ids: tuple[str, ...]
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contradiction_id": self.contradiction_id,
            "claim_id": self.claim_id,
            "source_ids": list(self.source_ids),
            "note": self.note,
        }


def _clip(value: str, limit: int = _MAX_TEXT_CHARS) -> str:
    value = str(value or "")
    return value[:limit]


@dataclass
class ResearchBundle:
    """One research session's ledger of sources, claims and contradictions."""

    budget: SourceBudget = field(default_factory=SourceBudget)
    sources: list[SourceRecord] = field(default_factory=list)
    claims: list[ClaimRecord] = field(default_factory=list)
    contradictions: list[ContradictionRecord] = field(default_factory=list)

    def add_source(self, record: SourceRecord) -> str:
        if len(self.sources) >= self.budget.max_sources:
            raise ResearchContractError(
                f"research bundle exceeds the {self.budget.max_sources}-source cap."
            )
        self.sources.append(record)
        return record.source_id

    def source_by_id(self, source_id: str) -> SourceRecord | None:
        for record in self.sources:
            if record.source_id == source_id:
                return record
        return None

    def add_claim(
        self,
        text: str,
        *,
        supporting: Sequence[str] = (),
        note: str | None = None,
    ) -> ClaimRecord:
        text = _clip(text.strip())
        if not text:
            raise ResearchContractError("claim text must not be empty.")
        ids = tuple(dict.fromkeys(str(i) for i in supporting))
        for sid in ids:
            if self.source_by_id(sid) is None:
                raise ResearchContractError(f"claim references unknown source {sid}.")
        status = "supported" if ids else "unsupported"
        record = ClaimRecord(
            claim_id="claim-" + hashlib.sha256(text.encode()).hexdigest()[:24],
            text=text,
            status=status,
            supporting=ids,
            contradicting=(),
            note=_clip(note) if note else None,
        )
        self.claims.append(record)
        return record

    def record_contradiction(
        self,
        claim_id: str,
        source_ids: Sequence[str],
        *,
        note: str | None = None,
    ) -> ContradictionRecord:
        claim = next((c for c in self.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ResearchContractError(f"contradiction references unknown claim {claim_id}.")
        ids = tuple(dict.fromkeys(str(i) for i in source_ids))
        if len(ids) < 2:
            raise ResearchContractError(
                "a contradiction needs at least two distinct sources."
            )
        for sid in ids:
            if self.source_by_id(sid) is None:
                raise ResearchContractError(f"contradiction references unknown source {sid}.")
        record = ContradictionRecord(
            contradiction_id="contra-" + hashlib.sha256(
                (claim_id + "|" + ",".join(ids)).encode()
            ).hexdigest()[:24],
            claim_id=claim_id,
            source_ids=ids,
            note=_clip(note) if note else None,
        )
        self.contradictions.append(record)
        # Mark the claim contradicted, keeping the contradicting ids; the
        # original supporting tuple is preserved untouched.
        self.claims = [
            c if c.claim_id != claim_id
            else ClaimRecord(
                claim_id=c.claim_id,
                text=c.text,
                status="contradicted",
                supporting=c.supporting,
                contradicting=tuple(dict.fromkeys((*c.contradicting, *ids))),
                note=c.note,
            )
            for c in self.claims
        ]
        return record

    def unsupported_claims(self) -> tuple[ClaimRecord, ...]:
        return tuple(c for c in self.claims if c.status == "unsupported")

    def contradicted_claims(self) -> tuple[ClaimRecord, ...]:
        return tuple(c for c in self.claims if c.status == "contradicted")

    def failed_sources(self) -> tuple[SourceRecord, ...]:
        return tuple(s for s in self.sources if s.is_failure)

    def incomplete_sources(self) -> tuple[SourceRecord, ...]:
        return tuple(s for s in self.sources if s.is_incomplete)

    @property
    def total_bytes(self) -> int:
        return sum(s.byte_count for s in self.sources)

    @property
    def is_incomplete(self) -> bool:
        return (
            bool(self.failed_sources())
            or bool(self.incomplete_sources())
            or bool(self.unsupported_claims())
            or bool(self.contradicted_claims())
            or self.total_bytes > self.budget.max_total_bytes
        )

    def incomplete_reason(self) -> list[str]:
        reasons: list[str] = []
        for s in self.failed_sources():
            reasons.append(f"source {s.source_id} ({s.url}) failed: {s.reason or 'unknown error'}")
        for s in self.incomplete_sources():
            if not s.is_failure:
                reasons.append(f"source {s.source_id} ({s.url}) incomplete: {s.reason or s.retrieval_status}")
        for c in self.unsupported_claims():
            reasons.append(f"claim {c.claim_id} has no supporting source.")
        for c in self.contradicted_claims():
            reasons.append(f"claim {c.claim_id} is contradicted by {list(c.contradicting)}.")
        if self.total_bytes > self.budget.max_total_bytes:
            reasons.append(
                f"bundle consumed {self.total_bytes} bytes, over the {self.budget.max_total_bytes}-byte cap."
            )
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "budget": {
                "max_sources": self.budget.max_sources,
                "max_bytes_per_source": self.budget.max_bytes_per_source,
                "max_total_bytes": self.budget.max_total_bytes,
                "max_redirects": self.budget.max_redirects,
                "max_excerpt_chars": self.budget.max_excerpt_chars,
                "max_source_snapshot_days": self.budget.max_source_snapshot_days,
            },
            "sources": [s.to_dict() for s in self.sources],
            "claims": [c.to_dict() for c in self.claims],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "total_bytes": self.total_bytes,
            "is_incomplete": self.is_incomplete,
            "incomplete_reasons": self.incomplete_reason(),
        }


# ---------------------------------------------------------------------------
# Read-only repository inspection, bounded and confined.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoFileRecord:
    path: str  # POSIX relative to the inspected root
    size_bytes: int
    sha256: str | None
    skipped_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True, slots=True)
class RepoInspection:
    root: str
    files: tuple[RepoFileRecord, ...]
    total_bytes: int
    truncated: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": [f.to_dict() for f in self.files],
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "errors": list(self.errors),
        }


def inspect_repository(
    root: Path | str,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 1024 * 1024,
    max_total_bytes: int = 64 * 1024 * 1024,
) -> RepoInspection:
    """Walk `root` read-only; symlinks and escapes are refused; caps stop early.

    Never writes to the target. Objects without a regular file identity are
    recorded with `skipped_reason`; nothing is followed outside the root.
    """
    root_path = Path(root)
    if not str(root_path).strip() or "\x00" in str(root_path):
        raise ResearchContractError("root must be a non-empty path without NUL characters.")
    try:
        resolved = root_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ResearchContractError(f"root does not resolve: {exc}.") from exc
    if not resolved.is_dir():
        raise ResearchContractError("root must be a directory.")

    entries: list[RepoFileRecord] = []
    errors: list[str] = []
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
        # Refuse symlinked directories (walk would not descend anyway with
        # followlinks=False, but the entries must be recorded as refused).
        for name in list(dirnames):
            full = Path(dirpath) / name
            try:
                if full.is_symlink():
                    entries.append(
                        RepoFileRecord(
                            path=_rel_posix(resolved, full),
                            size_bytes=0,
                            sha256=None,
                            skipped_reason="symlink_refused",
                        )
                    )
                    dirnames.remove(name)
            except OSError as exc:
                errors.append(f"{_rel_posix(resolved, full)}: {exc}")
        for name in filenames:
            full = Path(dirpath) / name
            rel = _rel_posix(resolved, full)
            try:
                if full.is_symlink():
                    entries.append(RepoFileRecord(rel, 0, None, "symlink_refused"))
                    continue
                st = full.stat()
                if not full.is_file():
                    entries.append(RepoFileRecord(rel, 0, None, "not_regular_file"))
                    continue
                if st.st_size > max_file_bytes:
                    entries.append(RepoFileRecord(rel, st.st_size, None, "file_too_large"))
                    continue
                remaining = max_total_bytes - total
                if st.st_size > remaining:
                    truncated = True
                    break
                with open(full, "rb") as handle:
                    payload = handle.read(max_file_bytes + 1)
                if len(payload) > st.st_size:
                    payload = payload[: st.st_size]
                digest = hashlib.sha256(payload).hexdigest()
                entries.append(RepoFileRecord(rel, st.st_size, digest, None))
                total += st.st_size
                if len(entries) >= max_files:
                    truncated = True
                    break
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        if truncated or len(entries) >= max_files:
            truncated = True
            break
    return RepoInspection(
        root=str(resolved), files=tuple(entries), total_bytes=total, truncated=truncated, errors=tuple(errors)
    )


def _rel_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Public adapters. Both are thin, allowlist-bound wrappers over guarded_fetch.
# ---------------------------------------------------------------------------


def github_search_repositories(
    query: str,
    *,
    bundle: ResearchBundle | None = None,
    fetcher: Fetcher,
    limit: int = 5,
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
    clock: Callable[[], str] = _now_iso,
) -> tuple[SourceRecord, tuple[dict[str, Any], ...]]:
    """Search public repositories via api.github.com (no auth, zero spend).

    Returns (search_source_record, repositories). The payload is parsed from
    the retained body, which is the same byte-capped body that was hashed into
    the source record — never from the human-readable excerpt. A payload that
    cannot be parsed leaves the record ``partial`` with **no** repositories
    listed: the adapter never fabricates results and never silently refetches.
    """
    from urllib.parse import quote_plus

    query = _clip(str(query or "").strip(), 256)
    if not query:
        raise ResearchContractError("search query must not be empty.")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10:
        raise ResearchContractError("limit must be an integer between 1 and 10.")
    record_bundle = bundle if bundle is not None else ResearchBundle()
    raw = (
        "https://api.github.com/search/repositories"
        f"?q={quote_plus(query)}&per_page={limit}&sort=updated"
    )
    record, body = guarded_fetch_with_body(
        fetcher,
        FetchRequest(url=raw),
        budget=record_bundle.budget,
        dns_resolver=dns_resolver,
        clock=clock,
    )

    payload = None
    if record.retrieval_status == "ok" and record.http_status is not None and record.http_status < 400:
        payload = _json_object(body)

    repos: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        for item in payload["items"][:limit]:
            if not isinstance(item, dict):
                continue
            full = _clip(item.get("full_name") or "", 256).strip()
            url = _clip(item.get("html_url") or "", 1024).strip()
            updated = _clip(item.get("updated_at") or "", 64).strip() or None
            if not full or not url.startswith("https://github.com/"):
                continue
            stars = item.get("stargazers_count")
            repos.append(
                {
                    "full_name": full,
                    "url": url,
                    "updated_at": updated,
                    "stargazers_count": stars if isinstance(stars, int) else None,
                    "search_source_id": record.source_id,
                    "note": "provider metadata returned by the search API; verify before relying",
                }
            )
    elif record.retrieval_status == "ok":
        record = SourceRecord(
            source_id=record.source_id,
            url=record.url,
            final_url=record.final_url,
            retrieved_at=record.retrieved_at,
            http_status=record.http_status,
            retrieval_status="partial",
            content_sha256=record.content_sha256,
            excerpt=record.excerpt,
            publication_date=record.publication_date,
            byte_count=record.byte_count,
            reason="search response could not be parsed as a JSON object; no repositories listed.",
        )

    record_bundle.add_source(record)
    return record, tuple(repos)


def wikipedia_raw_article(
    title: str,
    *,
    bundle: ResearchBundle | None = None,
    fetcher: Fetcher,
    dns_resolver: Callable[[str], Sequence[str]] | None = None,
    clock: Callable[[], str] = _now_iso,
) -> SourceRecord:
    """Read one public article from en.wikipedia.org (read-only, zero spend)."""
    from urllib.parse import quote

    title = _clip(str(title or "").strip(), 256)
    if not title:
        raise ResearchContractError("article title must not be empty.")
    raw = (
        "https://en.wikipedia.org/w/index.php"
        f"?title={quote(title)}&action=raw"
    )
    record, _ = guarded_fetch_with_body(
        fetcher,
        FetchRequest(url=raw),
        budget=(bundle.budget if bundle is not None else SourceBudget()),
        dns_resolver=dns_resolver,
        clock=clock,
    )
    if bundle is not None:
        bundle.add_source(record)
    return record


__all__ = [
    "PUBLIC_READ_ALLOWLIST",
    "SourceBudget",
    "SourceRecord",
    "ClaimRecord",
    "ContradictionRecord",
    "ResearchBundle",
    "RepoFileRecord",
    "RepoInspection",
    "FetchRequest",
    "FetchResponse",
    "Fetcher",
    "ResearchContractError",
    "is_blocked_ip",
    "validate_public_url",
    "validate_redirect_chain",
    "guarded_fetch",
    "guarded_fetch_with_body",
    "inspect_repository",
    "github_search_repositories",
    "wikipedia_raw_article",
]
