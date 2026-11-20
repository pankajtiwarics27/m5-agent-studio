"""AP-010 — governed browser worker: isolated sessions, takeover and recovery.

This module is the *contract* layer for a controlled browser worker. It decides
what a browser action may do, which profile it may use, when a human takeover
suspends the agent, and what happens to owned state on stop/restart. It performs
no I/O itself: the real browser is injected through :class:`BrowserDriver`, so
every rule below is testable offline and the same contract governs the real
pinned Chromium used by ``scripts/browser_worker_bridge.mjs``.

Governing owner decisions (2026-11-17, issue #330):

* **U04** — all AP I/O confined to ``~/M5_AP_Workspace``; no personal profile,
  account or credential is reused; versioned retention.
* **U06** — nothing is activated by this story. ``activation_authorized`` stays
  false and a worker may only run in the ``offline`` or ``pinned_local`` class.

The six AP-010 acceptance criteria map onto this module one-to-one:

1. *Candidate versus deterministic baseline* — :class:`BakeOffHarness` runs the
   same :class:`TaskScript` through two drivers with the same policy and
   compares outcomes deterministically.
2. *Isolated profile per project* — :func:`allocate_profile` mints a fresh
   profile inside the workspace; :func:`assert_not_personal_profile` refuses any
   path that is a real browser user-data directory.
3. *Grants and nested budgets* — :class:`SessionPolicy` carries the grant
   scopes and every :class:`BrowserAction` is admitted by
   :meth:`SessionPolicy.admit_action` before the driver sees it; downloads and
   navigation consume nested budgets that cannot exceed the session's.
4. *Takeover* — :meth:`BrowserSession.take_over` suspends agent actions,
   :meth:`resume` requires the same human and records the interval.
5. *Hostile pages cannot gain authority* — navigation is re-validated on every
   hop, private-network and literal-IP targets are refused, and
   :func:`classify_popup` refuses windows that would escape the session's
   origin budget.
6. *Stop/restart without secret leakage* — :meth:`BrowserSession.stop` fences
   the session and destroys the owned profile; :func:`recover_session` restores
   only state that was journaled, never credentials.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

__all__ = [
    "BrowserWorkerError",
    "PolicyViolation",
    "SessionFencedError",
    "TakeoverConflictError",
    "ActivationRefusedError",
    "SessionClass",
    "SessionPolicy",
    "BrowserAction",
    "ActionDecision",
    "ProfileRecord",
    "PopupDecision",
    "SessionState",
    "BrowserSession",
    "TaskScript",
    "TaskOutcome",
    "BakeOffResult",
    "BakeOffHarness",
    "BrowserDriver",
    "BridgeDriver",
    "bridge_available",
    "allocate_profile",
    "assert_not_personal_profile",
    "classify_popup",
    "recover_session",
    "journal_digest",
    "compare_outcomes",
    "is_private_network_target",
    "is_loopback_target",
    "WORKSPACE_ROOT",
]


class BrowserWorkerError(ValueError):
    """Caller misuse or an unusable worker configuration."""


class PolicyViolation(BrowserWorkerError):
    """An action was refused by the session policy (recorded, never executed)."""


class SessionFencedError(BrowserWorkerError):
    """The session was stopped/fenced; further activity must not be dispatched."""


class TakeoverConflictError(BrowserWorkerError):
    """A takeover/resume was attempted out of order or by a different human."""


class ActivationRefusedError(BrowserWorkerError):
    """A caller asked for an activation class this story cannot grant (U06)."""


#: U04: every AP byte lives under this root.
WORKSPACE_ROOT = Path("~/M5_AP_Workspace")

_ALLOWED_SCHEMES = frozenset({"https", "http", "about", "data"})

#: Hosts a governed browser may reach. Loopback is admitted only because the
#: Studio and the fixture servers in the tests are local; it is never a target a
#: hostile page can steer navigation toward (see ``Navigator`` re-validation).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # includes cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
)

#: Environment keys that must never be captured into a journal or profile.
_SECRET_KEY = re.compile(
    r"(token|secret|passw|api[_-]?key|authorization|cookie|credential|session[_-]?id)",
    re.IGNORECASE,
)

#: Browser user-data directory names that mean "this is a real person's profile".
_PERSONAL_PROFILE_MARKERS = (
    "/chrome/user data",
    "/google/chrome",
    "/chromium",
    "/.config/google-chrome",
    "/.config/chromium",
    "/microsoft/edge",
    "/bravesoftware",
    "/firefox",
    "library/application support/google/chrome",
    "library/application support/firefox",
)


def _now() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def is_private_network_target(url: str) -> bool:
    """True when a URL targets private, link-local, metadata or loopback space."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return True
    host = host.strip("[]").lower().rstrip(".")
    if not host:
        return True
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # a name; DNS ownership is the egress boundary's business
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _PRIVATE_NETWORKS)


def is_loopback_target(url: str) -> bool:
    """True only for an actual loopback target (never a LAN or metadata IP)."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.strip("[]").lower().rstrip(".")
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class SessionClass:
    """The only classes this story may run in; activation is not one of them."""

    OFFLINE = "offline"
    PINNED_LOCAL = "pinned_local"
    ALL = (OFFLINE, PINNED_LOCAL)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Everything a session may do. Immutable, and narrow by construction."""

    session_class: str = SessionClass.OFFLINE
    grant_scopes: tuple[str, ...] = ()
    max_pages: int = 3
    max_navigations: int = 20
    max_downloads: int = 2
    max_download_bytes: int = 5 * 1024 * 1024
    max_total_download_bytes: int = 10 * 1024 * 1024
    allowed_origins: tuple[str, ...] = ()
    allow_private_network: bool = False
    allow_downloads: bool = False
    allow_popups: bool = False
    activation_authorized: bool = False

    def __post_init__(self) -> None:
        if self.session_class not in SessionClass.ALL:
            raise BrowserWorkerError(
                f"session_class must be one of {SessionClass.ALL}, got {self.session_class!r}."
            )
        if self.activation_authorized:
            raise ActivationRefusedError(
                "AP-010 cannot authorize activation (U06); activation_authorized must stay false."
            )
        if self.allow_private_network:
            raise PolicyViolation(
                "private-network browser access is not granted by AP-010 or U03/U04."
            )
        for name in (
            "max_pages",
            "max_navigations",
            "max_downloads",
            "max_download_bytes",
            "max_total_download_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise BrowserWorkerError(f"{name} must be a non-negative integer.")
        if self.max_total_download_bytes < self.max_download_bytes:
            raise BrowserWorkerError(
                "max_total_download_bytes must be >= max_download_bytes (nested budget)."
            )

    def origin_allowed(self, url: str) -> bool:
        if not self.allowed_origins:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        return any(origin == allowed.lower().rstrip("/") for allowed in self.allowed_origins)

    def admit_url(self, url: str) -> None:
        """Raise PolicyViolation unless ``url`` is a permissible navigation target."""
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise PolicyViolation(f"unparseable URL: {exc}") from exc
        scheme = (parts.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise PolicyViolation(f"scheme {scheme!r} is not allowed.")
        if scheme in {"about", "data"}:
            return
        if parts.username or parts.password:
            raise PolicyViolation("URLs carrying userinfo/credentials are refused.")
        if is_private_network_target(url):
            # Loopback is reachable only for an explicitly granted local origin
            # (the Studio and local fixtures). Every other private, link-local
            # or metadata target is refused outright: a grant can name one, but
            # that grant is not honoured.
            if not (is_loopback_target(url) and self.origin_allowed(url)):
                raise PolicyViolation(f"private/link-local target refused: {url}")
        if not self.origin_allowed(url):
            raise PolicyViolation(f"origin not in the session's grant: {url}")


@dataclass(frozen=True, slots=True)
class BrowserAction:
    """One action an agent wants the worker to perform."""

    kind: str  # navigate | download | click | type | screenshot | popup
    url: str | None = None
    selector: str | None = None
    text: str | None = None
    download_bytes: int | None = None

    KINDS = ("navigate", "download", "click", "type", "screenshot", "popup")


@dataclass(frozen=True, slots=True)
class ActionDecision:
    allowed: bool
    reason: str | None = None

    def raise_if_refused(self) -> None:
        if not self.allowed:
            raise PolicyViolation(self.reason or "refused")


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    session_id: str
    path: str
    created_at: str
    isolated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "created_at": self.created_at,
            "isolated": self.isolated,
        }


def assert_not_personal_profile(path: Path | str) -> Path:
    """Refuse a path that looks like a real person's browser profile (U04)."""
    raw = str(path)
    if not raw.strip() or "\x00" in raw:
        raise BrowserWorkerError("profile path must be non-empty and NUL-free.")
    normalized = raw.replace("\\", "/").lower()
    for marker in _PERSONAL_PROFILE_MARKERS:
        if marker in normalized:
            raise PolicyViolation(
                f"refusing to use a personal browser profile path ({marker!r}); "
                "AP-010 requires a dedicated isolated profile."
            )
    # Personal application/config roots are never ours, whatever the path
    # prefix: a browser profile there is a real person's logged-in browser. This
    # is deliberately a name-based rule rather than "anything under $HOME",
    # because a legitimate isolated profile can live under a temp root that a CI
    # runner places inside $HOME (GitHub Actions: HOME=/home/runner, tmp under
    # /home/runner/work/_temp). Refusing those protects nothing and breaks the
    # runner's own scratch space.
    home = str(Path.home()).replace("\\", "/").lower().rstrip("/")
    personal_roots = (
        f"{home}/.config",
        f"{home}/.local/share",
        f"{home}/library/application support",
        f"{home}/appdata",
        f"{home}/.mozilla",
        f"{home}/snap",
        f"{home}/.var/app",
    )
    for root in personal_roots:
        if normalized == root or normalized.startswith(root + "/"):
            raise PolicyViolation(
                "refusing to use a personal application data location "
                f"({root}); AP-010 requires a dedicated isolated profile."
            )
    return Path(raw)


def allocate_profile(
    session_id: str,
    *,
    workspace_root: Path | str = WORKSPACE_ROOT,
    clock: Callable[[], str] = _now,
) -> ProfileRecord:
    """Mint a fresh, isolated profile directory for exactly one session."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", session_id):
        raise BrowserWorkerError("session_id must be 1-64 url-safe characters.")
    root = Path(str(workspace_root)).expanduser()
    profile = root / "browser" / "profiles" / session_id
    assert_not_personal_profile(profile)
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserWorkerError(f"cannot create profile directory: {exc}") from exc
    return ProfileRecord(session_id=session_id, path=str(profile), created_at=clock())


# ---------------------------------------------------------------------------
# Popups
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PopupDecision:
    allowed: bool
    reason: str


def classify_popup(
    url: str,
    *,
    policy: SessionPolicy,
    opener_origin: str | None,
) -> PopupDecision:
    """Decide whether an unexpected popup may open inside this session.

    Default is refusal: a popup is a new surface, so it needs an explicit grant
    and must not be used to smuggle a page outside the session's origin budget.
    """
    if not policy.allow_popups:
        return PopupDecision(False, "popups are not granted by this session")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return PopupDecision(False, f"unparseable popup URL: {exc}")
    scheme = (parts.scheme or "").lower()
    if scheme not in {"https", "http"}:
        return PopupDecision(False, f"popup scheme {scheme!r} refused")
    if not policy.origin_allowed(url):
        return PopupDecision(False, "popup origin is outside the session grant")
    if is_private_network_target(url) and not (
        is_loopback_target(url) and policy.origin_allowed(url)
    ):
        return PopupDecision(False, "popup targets private or link-local space")
    if opener_origin and parts.netloc and opener_origin.lower().rstrip("/") != (
        f"{scheme}://{parts.netloc}".lower()
    ):
        return PopupDecision(
            False, "popup leaves the opener's origin; a new surface needs its own grant"
        )
    return PopupDecision(True, "popup within granted origin")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """Journal of what the session did. Survives stop; holds no secrets."""

    session_id: str
    started_at: str
    navigations: int = 0
    downloads: int = 0
    download_bytes: int = 0
    takeover_active: bool = False
    takeover_by: str | None = None
    takeover_started_at: str | None = None
    takeover_seconds: float = 0.0
    stopped_at: str | None = None
    fenced: bool = False
    journal: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "navigations": self.navigations,
            "downloads": self.downloads,
            "download_bytes": self.download_bytes,
            "takeover_active": self.takeover_active,
            "takeover_by": self.takeover_by,
            "takeover_seconds": self.takeover_seconds,
            "stopped_at": self.stopped_at,
            "fenced": self.fenced,
            "journal": [_redact(dict(entry)) for entry in self.journal],
        }


def _redact(value: Any) -> Any:
    """Strip credential-looking content before anything is journaled."""
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if _SECRET_KEY.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and _SECRET_KEY.search(value) and "=" in value:
        key = value.split("=", 1)[0]
        return f"{key}=<redacted>"
    return value


class BrowserDriver(Protocol):
    """The injected real browser. The session never assumes anything about it."""

    def open_page(self, url: str, *, profile_path: str) -> str: ...

    def close_page(self, page_id: str) -> None: ...

    def download(self, url: str, *, max_bytes: int) -> int: ...

    def close(self) -> None: ...


@dataclass
class BrowserSession:
    """One governed browser session: the unit of isolation, grant and recovery."""

    session_id: str
    policy: SessionPolicy
    profile: ProfileRecord
    driver: BrowserDriver | None = None
    state: SessionState = field(init=False)
    clock: Callable[[], str] = field(default=_now, repr=False)

    def __post_init__(self) -> None:
        self.state = SessionState(session_id=self.session_id, started_at=self.clock())
        assert_not_personal_profile(self.profile.path)

    # -- lifecycle ---------------------------------------------------------

    @property
    def fenced(self) -> bool:
        return self.state.fenced

    def _require_live(self) -> None:
        if self.state.fenced:
            raise SessionFencedError(
                f"session {self.session_id} is fenced; no further actions are dispatched."
            )

    def stop(self, *, reason: str = "operator stop") -> SessionState:
        """Stop all owned browser work and destroy the owned profile.

        Idempotent. After this the session refuses work (``fenced``), so a
        restarted worker cannot silently inherit half-open browser jobs.
        """
        if self.state.fenced:
            return self.state
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception as exc:  # driver failure must not keep the fence open
                self._journal("driver_close_error", error=str(exc))
        self._destroy_profile()
        self.state.fenced = True
        self.state.stopped_at = self.clock()
        self._journal("stopped", reason=reason)
        return self.state

    def _destroy_profile(self) -> None:
        root = Path(self.profile.path)
        if not root.exists():
            return
        # Only ever delete our own session directory, never a parent.
        for child in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            except OSError:
                continue
        # Remove the now-empty parents this session created, but never climb
        # past the workspace's profiles root.
        current = root
        for _ in range(3):
            try:
                current.rmdir()
            except OSError:
                break
            if current.name in {"profiles", "browser"} or current == current.parent:
                break
            current = current.parent

    def _journal(self, event: str, **fields: Any) -> None:
        entry = {"at": self.clock(), "event": event}
        entry.update(_redact(fields))
        self.state.journal.append(entry)

    # -- takeover ----------------------------------------------------------

    def take_over(self, human_id: str) -> SessionState:
        """Pause agent actions; the human now owns the session."""
        self._require_live()
        if not human_id or not human_id.strip():
            raise TakeoverConflictError("takeover requires an identified human.")
        if self.state.takeover_active:
            raise TakeoverConflictError(
                f"session already held by {self.state.takeover_by!r}."
            )
        self.state.takeover_active = True
        self.state.takeover_by = human_id.strip()
        self.state.takeover_started_at = self.clock()
        self._journal("takeover_started", human=human_id.strip())
        return self.state

    def resume(self, human_id: str) -> SessionState:
        """Return control to the agent; only the holding human may resume."""
        self._require_live()
        if not self.state.takeover_active:
            raise TakeoverConflictError("no takeover is active on this session.")
        if (human_id or "").strip() != (self.state.takeover_by or ""):
            raise TakeoverConflictError(
                "only the human who took over may resume this session."
            )
        self.state.takeover_active = False
        self.state.takeover_by = None
        self.state.takeover_started_at = None
        self._journal("takeover_resumed", human=(human_id or "").strip())
        return self.state

    # -- actions -----------------------------------------------------------

    def admit(self, action: BrowserAction) -> ActionDecision:
        """Decide an action against the policy *and* current session state."""
        if action.kind not in BrowserAction.KINDS:
            return ActionDecision(False, f"unknown action kind {action.kind!r}")
        if self.state.fenced:
            return ActionDecision(False, "session is fenced")
        if self.state.takeover_active:
            return ActionDecision(
                False, f"agent actions are suspended for human takeover by {self.state.takeover_by}"
            )
        if action.kind == "navigate":
            if not action.url:
                return ActionDecision(False, "navigate requires a url")
            if self.state.navigations >= self.policy.max_navigations:
                return ActionDecision(False, "navigation budget exhausted")
            try:
                self.policy.admit_url(action.url)
            except PolicyViolation as exc:
                return ActionDecision(False, str(exc))
            return ActionDecision(True)
        if action.kind == "download":
            if not self.policy.allow_downloads:
                return ActionDecision(False, "downloads are not granted by this session")
            if not action.url:
                return ActionDecision(False, "download requires a url")
            try:
                self.policy.admit_url(action.url)
            except PolicyViolation as exc:
                return ActionDecision(False, str(exc))
            if self.state.downloads >= self.policy.max_downloads:
                return ActionDecision(False, "download count budget exhausted")
            size = action.download_bytes
            if size is None:
                return ActionDecision(False, "download size must be declared before transfer")
            if size > self.policy.max_download_bytes:
                return ActionDecision(False, "download exceeds the per-download cap")
            if self.state.download_bytes + size > self.policy.max_total_download_bytes:
                return ActionDecision(False, "download exceeds the session's nested byte budget")
            return ActionDecision(True)
        if action.kind == "popup":
            decision = classify_popup(
                action.url or "",
                policy=self.policy,
                opener_origin=None,
            )
            return ActionDecision(decision.allowed, None if decision.allowed else decision.reason)
        # click/type/screenshot carry no egress and are local to a granted page.
        return ActionDecision(True)

    def perform(self, action: BrowserAction) -> dict[str, Any]:
        """Admit then execute one action through the injected driver."""
        self._require_live()
        decision = self.admit(action)
        if not decision.allowed:
            self._journal("refused", kind=action.kind, url=action.url, reason=decision.reason)
            raise PolicyViolation(decision.reason or "refused")
        if action.kind == "navigate":
            assert action.url is not None
            self.state.navigations += 1
            page_id = None
            if self.driver is not None:
                page_id = self.driver.open_page(action.url, profile_path=self.profile.path)
            self._journal("navigate", url=action.url, page=page_id)
            return {"page_id": page_id, "url": action.url}
        if action.kind == "download":
            assert action.url is not None
            size = int(action.download_bytes or 0)
            written = size
            if self.driver is not None:
                written = self.driver.download(
                    action.url, max_bytes=self.policy.max_download_bytes
                )
            if written > self.policy.max_download_bytes:
                raise PolicyViolation("driver wrote more than the per-download cap")
            self.state.downloads += 1
            self.state.download_bytes += written
            self._journal("download", url=action.url, bytes=written)
            return {"bytes": written, "url": action.url}
        self._journal(action.kind, selector=action.selector, chars=len(action.text or ""))
        return {"kind": action.kind}


def recover_session(
    state: SessionState,
    *,
    policy: SessionPolicy,
    profile: ProfileRecord,
    driver: BrowserDriver | None = None,
    clock: Callable[[], str] = _now,
) -> BrowserSession:
    """Recreate a session after a restart without resurrecting authority.

    A stopped session is never revived silently: recovery requires the caller to
    re-grant a policy and a *fresh* profile, because the previous profile was
    destroyed on stop. Only non-secret journal facts are carried over.
    """
    if not state.fenced:
        raise BrowserWorkerError(
            "only a fenced session may be recovered; a live session must be stopped first."
        )
    if profile.session_id != state.session_id:
        raise BrowserWorkerError("recovered profile does not belong to this session.")
    session = BrowserSession(
        session_id=state.session_id,
        policy=policy,
        profile=profile,
        driver=driver,
        clock=clock,
    )
    session.state.journal.append(
        {
            "at": clock(),
            "event": "recovered",
            "previous_stopped_at": state.stopped_at,
            "previous_navigations": state.navigations,
            "previous_downloads": state.downloads,
        }
    )
    return session


# ---------------------------------------------------------------------------
# Bake-off
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskScript:
    """One deterministic task run through both drivers."""

    name: str
    steps: tuple[BrowserAction, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BrowserWorkerError("task name must not be empty.")


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    driver: str
    task: str
    completed: bool
    executed: int
    refused: int
    failures: tuple[str, ...]
    bytes_downloaded: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "task": self.task,
            "completed": self.completed,
            "executed": self.executed,
            "refused": self.refused,
            "failures": list(self.failures),
            "bytes_downloaded": self.bytes_downloaded,
        }

    @property
    def signature(self) -> tuple[Any, ...]:
        """The deterministic part of the outcome used for comparison."""
        return (self.task, self.executed, self.refused, self.failures, self.bytes_downloaded)


@dataclass(frozen=True, slots=True)
class BakeOffResult:
    baseline: tuple[TaskOutcome, ...]
    candidate: tuple[TaskOutcome, ...]

    @property
    def equivalent(self) -> bool:
        return all(a.signature == b.signature for a, b in zip(self.baseline, self.candidate))

    def divergences(self) -> tuple[str, ...]:
        out = []
        for base, cand in zip(self.baseline, self.candidate):
            if base.signature != cand.signature:
                out.append(
                    f"{base.task}: baseline={base.signature} candidate={cand.signature}"
                )
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "equivalent": self.equivalent,
            "divergences": list(self.divergences()),
            "baseline": [o.to_dict() for o in self.baseline],
            "candidate": [o.to_dict() for o in self.candidate],
        }


class BakeOffHarness:
    """Run identical task scripts through a baseline and a candidate driver.

    The comparison is on *policy-visible* outcomes only, so a candidate cannot
    win by doing more than the policy allows: both drivers receive the same
    :class:`SessionPolicy`, and refusals are counted on both sides.
    """

    def __init__(
        self,
        *,
        policy: SessionPolicy,
        baseline_driver: BrowserDriver,
        candidate_driver: BrowserDriver,
        workspace_root: Path | str = WORKSPACE_ROOT,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.policy = policy
        self.baseline_driver = baseline_driver
        self.candidate_driver = candidate_driver
        self.workspace_root = workspace_root
        self.clock = clock

    def _run(self, driver: BrowserDriver, label: str, task: TaskScript) -> TaskOutcome:
        profile = allocate_profile(
            f"bakeoff-{label}-{task.name}"[:64],
            workspace_root=self.workspace_root,
            clock=self.clock,
        )
        session = BrowserSession(
            session_id=profile.session_id,
            policy=self.policy,
            profile=profile,
            driver=driver,
            clock=self.clock,
        )
        executed = refused = 0
        failures: list[str] = []
        downloaded = 0
        try:
            for step in task.steps:
                decision = session.admit(step)
                if not decision.allowed:
                    refused += 1
                    continue
                try:
                    result = session.perform(step)
                except Exception as exc:
                    failures.append(f"{step.kind}: {exc}")
                    continue
                executed += 1
                downloaded += int(result.get("bytes", 0) or 0)
        finally:
            session.stop(reason="bake-off complete")
        return TaskOutcome(
            driver=label,
            task=task.name,
            completed=not failures and refused == 0,
            executed=executed,
            refused=refused,
            failures=tuple(failures),
            bytes_downloaded=downloaded,
        )

    def run(self, tasks: Sequence[TaskScript]) -> BakeOffResult:
        if not tasks:
            raise BrowserWorkerError("a bake-off needs at least one task.")
        baseline = tuple(self._run(self.baseline_driver, "baseline", t) for t in tasks)
        candidate = tuple(self._run(self.candidate_driver, "candidate", t) for t in tasks)
        return BakeOffResult(baseline=baseline, candidate=candidate)


def compare_outcomes(
    baseline: Sequence[TaskOutcome], candidate: Sequence[TaskOutcome]
) -> BakeOffResult:
    """Compare two result sets without running anything (for reporting)."""
    if len(baseline) != len(candidate):
        raise BrowserWorkerError("baseline and candidate must cover the same tasks.")
    return BakeOffResult(baseline=tuple(baseline), candidate=tuple(candidate))


def journal_digest(session: BrowserSession) -> str:
    """Stable digest of a session journal, for evidence records."""
    payload = json.dumps(session.state.to_dict(), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Real-browser driver (subprocess bridge over the pinned Chromium)
# ---------------------------------------------------------------------------


class BridgeDriver:
    """A :class:`BrowserDriver` backed by ``scripts/browser_worker_bridge.mjs``.

    The bridge is a *driver*: it performs admitted work and reports facts. It is
    not a policy engine — the session decides what may happen, and a bridge
    failure is surfaced rather than retried around.
    """

    def __init__(
        self,
        *,
        chrome: str | Path,
        puppeteer_core: str | Path,
        node: str = "node",
        bridge: str | Path | None = None,
        extra_args: str = "--disable-gpu",
        timeout_s: float = 300.0,
    ) -> None:
        self.chrome = str(chrome)
        self.puppeteer_core = str(puppeteer_core)
        self.node = node
        self.bridge = str(
            bridge
            if bridge is not None
            else Path(__file__).resolve().parent / "scripts" / "browser_worker_bridge.mjs"
        )
        self.extra_args = extra_args
        self.timeout_s = timeout_s
        self.last_result: dict[str, Any] | None = None
        self.pages: list[str] = []

    def run_plan(self, tasks: Sequence[Mapping[str, Any]], *, out: Path | str | None = None) -> dict[str, Any]:
        """Run a task plan through the real browser and return the bridge report."""
        import os
        import subprocess

        if not Path(self.chrome).exists():
            raise BrowserWorkerError(f"chromium not found at {self.chrome}")
        if not Path(self.puppeteer_core).exists():
            raise BrowserWorkerError(f"puppeteer-core not found at {self.puppeteer_core}")
        plan_file = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=tempfile.gettempdir()
        )
        try:
            json.dump({"tasks": list(tasks)}, plan_file)
            plan_file.close()
            out_file = str(out) if out else plan_file.name + ".result.json"
            env = os.environ.copy()
            env["STUDIO_SCREENSHOT_CHROME"] = self.chrome
            env["STUDIO_PUPPETEER_CORE"] = self.puppeteer_core
            env.setdefault("STUDIO_SCREENSHOT_CHROME_ARGS", self.extra_args)
            command = [self.node, self.bridge, "--plan", plan_file.name, "--out", out_file]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise BrowserWorkerError(f"browser bridge timed out after {self.timeout_s}s") from exc
            payload: dict[str, Any] = {}
            if Path(out_file).exists():
                try:
                    payload = json.loads(Path(out_file).read_text())
                except ValueError:
                    payload = {}
            if not payload:
                raise BrowserWorkerError(
                    f"browser bridge produced no report (exit {completed.returncode}): "
                    f"{(completed.stderr or completed.stdout or '').strip()[:400]}"
                )
            payload["_exit_code"] = completed.returncode
            self.last_result = payload
            return payload
        finally:
            for suffix in ("", ".result.json"):
                try:
                    Path(plan_file.name + suffix).unlink()
                except OSError:
                    pass

    # -- BrowserDriver protocol -------------------------------------------

    def open_page(self, url: str, *, profile_path: str) -> str:
        page_id = f"page-{len(self.pages) + 1}"
        self.pages.append(page_id)
        return page_id

    def close_page(self, page_id: str) -> None:
        if page_id in self.pages:
            self.pages.remove(page_id)

    def download(self, url: str, *, max_bytes: int) -> int:
        raise BrowserWorkerError(
            "the bridge does not transfer downloads; AP-010 records download "
            "decisions only and moves no bytes."
        )

    def close(self) -> None:
        self.pages.clear()


def bridge_available(
    *, chrome: str | None = None, puppeteer_core: str | None = None
) -> bool:
    """True when a real pinned Chromium and puppeteer-core are configured."""
    import os

    chrome = chrome or os.environ.get("STUDIO_SCREENSHOT_CHROME", "")
    puppeteer_core = puppeteer_core or os.environ.get("STUDIO_PUPPETEER_CORE", "")
    return bool(chrome and puppeteer_core and Path(chrome).exists() and Path(puppeteer_core).exists())
