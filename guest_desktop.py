"""Optional guest-desktop capability (AP-030).

Enable GUI-only tasks **only** when simpler APIs and browser workers are
insufficient.

This module is a **capability and authority contract**, not a driver. It has no
executor, launches no virtual machine, starts no service, contacts no provider
and touches no host desktop. Every platform fact in it is supplied by the
caller, and anything unsupplied is reported as *not measured* rather than
assumed.

What it decides:

1. Whether a task **justifies** desktop control at all — the simpler tools must
   be named and shown insufficient, per task, not per project (AC1).
2. Whether an exact platform, version and resource profile has been **observed**
   for this capability — a claim is not an observation, and an unqualified
   adapter stays disabled (AC2, AC6).
3. Whether host files, accounts, clipboard and screenshots stay **outside**
   untrusted guest authority unless separately granted (AC3, AC4).
4. Whether a guest failure can fall back to controlling the **host** — it never
   can; the failure is contained and reported (AC5).

The one thing it must never do is make an unqualified adapter look usable. So
``GuestDesktopAdapter`` refuses to be anything but disabled without a
qualification record, and its maturity is published as a limitation, not a
promise.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import history_privacy
import sandbox_boundary

GUEST_DESKTOP_SCHEMA_VERSION = 1

#: The simpler capabilities a desktop task must be shown to need more than.
SIMPLER_CAPABILITIES: tuple[str, ...] = (
    "http_api",
    "mcp_tool",
    "browser_worker",
    "cli",
)

#: Host surfaces that remain outside untrusted guest authority. Each one is a
#: separate grant; there is no package deal and no implicit sharing (AC3).
HOST_SURFACES: tuple[str, ...] = (
    "host_filesystem",
    "host_accounts",
    "host_clipboard",
    "host_desktop_input",
    "host_screenshots",
    "host_network",
)

#: Resource dimensions a platform profile must state to be qualified at all.
RESOURCE_DIMENSIONS: tuple[str, ...] = (
    "vcpus",
    "memory_mib",
    "disk_mib",
    "display_width",
    "display_height",
)

QualificationState = Literal["not_measured", "observed_unqualified", "qualified"]
QUALIFICATION_STATES: tuple[QualificationState, ...] = (
    "not_measured",
    "observed_unqualified",
    "qualified",
)

MaturityState = Literal["not_qualified", "provisional", "qualified"]
MATURITY_STATES: tuple[MaturityState, ...] = (
    "not_qualified",
    "provisional",
    "qualified",
)

#: Where screenshots and shared-folder bytes may live. The same root the AP
#: program confines its own I/O to; a guest path outside it is refused.
WORKSPACE_ROOT_TOKEN = "~/M5_AP_Workspace"

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class GuestDesktopError(ValueError):
    """Base error for the optional guest-desktop capability."""


class DesktopRefusedError(GuestDesktopError):
    """Raised when a task, profile or grant is refused. Carries a stable code."""

    def __init__(
        self, code: str, reason: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = str(code)
        self.reason = str(reason)
        self.details: dict[str, Any] = dict(details) if details is not None else {}

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "reason": self.reason}
        if self.details:
            out["details"] = dict(self.details)
        return out


class HostFallbackRefusedError(GuestDesktopError):
    """Raised when something tries to treat the host as a fallback for a guest (AC5)."""


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _clean_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise DesktopRefusedError(
            "invalid_identifier",
            f"{label} must be a bounded identifier matching {_ID_RE.pattern}: {value!r}.",
        )
    return text


def _text(value: object, label: str, *, optional: bool = False) -> str:
    text = str(value or "").strip()
    if not text and not optional:
        raise DesktopRefusedError("empty_field", f"{label} must not be empty.")
    return text


# ---------------------------------------------------------------------------
# AC1 — the task must justify desktop control over simpler tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimplerToolAttempt:
    """What a simpler capability was asked to do, and why it was insufficient."""

    capability: str
    attempted: bool
    outcome: str
    reason_insufficient: str = ""

    def __post_init__(self) -> None:
        if self.capability not in SIMPLER_CAPABILITIES:
            raise DesktopRefusedError(
                "unknown_simpler_capability",
                f"unknown capability {self.capability!r}; expected one of "
                f"{list(SIMPLER_CAPABILITIES)}.",
            )
        if not isinstance(self.attempted, bool):
            raise DesktopRefusedError(
                "invalid_attempt",
                "attempted must be a boolean, not a claim phrased as one.",
            )
        object.__setattr__(self, "outcome", _text(self.outcome, "outcome"))
        object.__setattr__(
            self,
            "reason_insufficient",
            str(self.reason_insufficient or "").strip(),
        )
        # A capability that was never tried cannot be evidence it was insufficient.
        if not self.attempted and self.reason_insufficient:
            raise DesktopRefusedError(
                "insufficiency_claimed_without_attempt",
                f"{self.capability!r} is claimed insufficient but was never attempted (AC1).",
            )
        if self.attempted and self.outcome == "sufficient":
            # Recorded as sufficient: this attempt defeats the justification.
            return
        if self.attempted and not self.reason_insufficient:
            raise DesktopRefusedError(
                "attempt_without_reason",
                f"{self.capability!r} was attempted and failed, but no reason says why that makes "
                "desktop control necessary (AC1).",
            )

    @property
    def blocks_desktop(self) -> bool:
        """True when this attempt succeeded, which means desktop is not justified."""
        return self.attempted and self.outcome == "sufficient"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "attempted": self.attempted,
            "outcome": self.outcome,
            "reason_insufficient": self.reason_insufficient,
        }


@dataclass(frozen=True, slots=True)
class DesktopJustification:
    """Why one specific task needs a guest desktop, with the alternatives recorded.

    The justification is per task. "This project is a design tool" is not a
    reason; a named task with named failed alternatives is (AC1).
    """

    task_id: str
    gui_requirement: str
    attempts: tuple[SimplerToolAttempt, ...]
    alternatives_considered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _clean_id(self.task_id, "task_id"))
        object.__setattr__(
            self, "gui_requirement", _text(self.gui_requirement, "gui_requirement")
        )
        attempts = tuple(self.attempts)
        if not attempts:
            raise DesktopRefusedError(
                "desktop_justification_without_alternatives",
                "a desktop task must record which simpler capabilities were tried and why each fell "
                "short; existing API and browser tools are the default (AC1).",
            )
        if any(not isinstance(a, SimplerToolAttempt) for a in attempts):
            raise TypeError("attempts must hold SimplerToolAttempt values.")
        for item in attempts:
            if item.blocks_desktop:
                raise DesktopRefusedError(
                    "simpler_capability_is_sufficient",
                    f"{item.capability!r} was attempted and was sufficient for this task, so desktop "
                    "control is not justified (AC1).",
                    details={"capability": item.capability},
                )
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(
            self,
            "alternatives_considered",
            tuple(
                str(a).strip() for a in self.alternatives_considered if str(a).strip()
            ),
        )

    @property
    def attempted_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted({a.capability for a in self.attempts}))

    @property
    def is_justified(self) -> bool:
        """True only when at least one simpler capability was actually attempted."""
        return bool(self.attempts) and not any(a.blocks_desktop for a in self.attempts)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "gui_requirement": self.gui_requirement,
            "attempts": [a.to_mapping() for a in self.attempts],
            "alternatives_considered": list(self.alternatives_considered),
            "justified": self.is_justified,
            "attempted_capabilities": list(self.attempted_capabilities),
        }


def require_desktop_justification(
    justification: DesktopJustification,
) -> DesktopJustification:
    """Refuse a desktop task that has not shown the simpler tools are insufficient."""
    if not isinstance(justification, DesktopJustification):
        raise TypeError("justification must be a DesktopJustification.")
    if not justification.is_justified:
        raise DesktopRefusedError(
            "desktop_not_justified",
            "desktop control is not justified for this task (AC1).",
        )
    return justification


# ---------------------------------------------------------------------------
# AC2 — exact platform, version and resources must be observed, not claimed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """A concrete guest platform identity with the resources it was given.

    ``observed`` means somebody ran the bake-off and recorded what the guest
    actually reported. A profile can be described without being observed; it
    just cannot be qualified (AC2).
    """

    profile_id: str
    guest_os: str
    guest_version: str
    resources: Mapping[str, int]
    host_fingerprint: str
    observed: bool = False
    setup_notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _clean_id(self.profile_id, "profile_id"))
        object.__setattr__(self, "guest_os", _text(self.guest_os, "guest_os").lower())
        object.__setattr__(
            self, "guest_version", _text(self.guest_version, "guest_version")
        )
        resources = {str(k): int(v) for k, v in dict(self.resources).items()}
        missing = sorted(set(RESOURCE_DIMENSIONS) - set(resources))
        if missing:
            raise DesktopRefusedError(
                "platform_profile_incomplete",
                f"a platform profile must state every resource dimension; missing {missing} (AC2).",
                details={"missing": missing},
            )
        extra = sorted(set(resources) - set(RESOURCE_DIMENSIONS))
        if extra:
            raise DesktopRefusedError(
                "platform_profile_unknown_dimension",
                f"unknown resource dimensions {extra}; an unmeasured field is not a resource (AC2).",
            )
        for name, value in resources.items():
            if value < 1:
                raise DesktopRefusedError(
                    "platform_profile_invalid_resource",
                    f"resource {name}={value} must be positive (AC2).",
                )
        object.__setattr__(self, "resources", dict(sorted(resources.items())))
        fingerprint = str(self.host_fingerprint or "").strip()
        if fingerprint and not _FINGERPRINT_RE.match(fingerprint):
            raise DesktopRefusedError(
                "platform_profile_invalid_fingerprint",
                "host_fingerprint must be a sha256:<hex> digest, as sandbox_boundary emits "
                "(AC2).",
            )
        object.__setattr__(self, "host_fingerprint", fingerprint)
        if not isinstance(self.observed, bool):
            raise DesktopRefusedError(
                "platform_profile_invalid_observation",
                "observed must be a boolean; a string 'true' is a claim, not an observation (AC2).",
            )
        object.__setattr__(self, "setup_notes", str(self.setup_notes or "").strip())

    @property
    def is_complete_for_qualification(self) -> bool:
        """A profile may be qualified only if it was observed with a host fingerprint."""
        return self.observed and bool(self.host_fingerprint)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "guest_os": self.guest_os,
            "guest_version": self.guest_version,
            "resources": dict(self.resources),
            "host_fingerprint": self.host_fingerprint,
            "observed": self.observed,
            "setup_notes": self.setup_notes,
            "complete_for_qualification": self.is_complete_for_qualification,
        }


@dataclass(frozen=True, slots=True)
class QualificationRecord:
    """Observed evidence for one profile, or an honest statement that none exists.

    ``qualified`` requires an observed profile, every binder check passed, and a
    named verifier. ``observed_unqualified`` has numbers but no qualification.
    ``not_measured`` has neither, and refuses to carry either (AC2).
    """

    state: QualificationState
    profile_id: str | None = None
    passed_checks: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    verified_by: str = ""
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in QUALIFICATION_STATES:
            raise DesktopRefusedError(
                "unknown_qualification_state",
                f"unknown qualification state: {self.state!r}.",
            )
        if self.state == "not_measured":
            if (
                self.profile_id
                or self.passed_checks
                or self.verified_by
                or self.observed_at
            ):
                raise DesktopRefusedError(
                    "not_measured_carries_evidence",
                    "a not_measured qualification must not carry a profile, checks, verifier or "
                    "timestamp; that is a measurement wearing a disclaimer (AC2).",
                )
            return
        if not self.profile_id:
            raise DesktopRefusedError(
                "qualification_without_profile",
                f"a {self.state} qualification must name the profile it observed (AC2).",
            )
        object.__setattr__(self, "profile_id", _clean_id(self.profile_id, "profile_id"))
        object.__setattr__(
            self,
            "passed_checks",
            tuple(sorted({str(c).strip() for c in self.passed_checks})),
        )
        object.__setattr__(
            self,
            "failed_checks",
            tuple(sorted({str(c).strip() for c in self.failed_checks})),
        )
        if self.state == "qualified":
            if not self.passed_checks:
                raise DesktopRefusedError(
                    "qualified_without_checks",
                    "a qualified platform must list the checks that passed (AC2).",
                )
            if self.failed_checks:
                raise DesktopRefusedError(
                    "qualified_with_failed_checks",
                    f"a qualified platform cannot carry failed checks: {list(self.failed_checks)} "
                    "(AC2).",
                )
            if not str(self.verified_by or "").strip():
                raise DesktopRefusedError(
                    "qualification_without_verifier",
                    "qualification must name who verified it; the agent preparing a bake-off is not "
                    "the verification (AC2, AC6).",
                )
        elif not self.failed_checks:
            raise DesktopRefusedError(
                "unqualified_without_failure",
                "an observed_unqualified record must name the check that failed (AC2).",
            )
        object.__setattr__(self, "verified_by", str(self.verified_by or "").strip())

    @property
    def is_qualified(self) -> bool:
        return self.state == "qualified"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "profile_id": self.profile_id,
            "passed_checks": list(self.passed_checks),
            "failed_checks": list(self.failed_checks),
            "verified_by": self.verified_by,
            "observed_at": self.observed_at,
            "qualified": self.is_qualified,
        }


def evaluate_platform_qualification(
    profile: PlatformProfile | None,
    record: QualificationRecord,
) -> tuple[bool, str]:
    """Decide whether this platform may be used, and say why in one sentence.

    Anything short of an observed profile with a ``qualified`` record refuses.
    A missing profile and a ``not_measured`` record both refuse, and so does a
    record that names a different profile than the one presented (AC2).
    """
    if not isinstance(record, QualificationRecord):
        raise TypeError("record must be a QualificationRecord.")
    if profile is None:
        return (
            False,
            "no platform profile was supplied, so no platform is qualified (AC2).",
        )
    if not isinstance(profile, PlatformProfile):
        raise TypeError("profile must be a PlatformProfile.")
    if record.state == "not_measured":
        return (
            False,
            "platform qualification is not measured; a claim is not an observation (AC2).",
        )
    if record.profile_id != profile.profile_id:
        return (
            False,
            f"the qualification record describes profile {record.profile_id!r}, not "
            f"{profile.profile_id!r}; evidence for another platform does not transfer (AC2).",
        )
    if not profile.is_complete_for_qualification:
        return (
            False,
            f"profile {profile.profile_id!r} was not observed with a host fingerprint, so it "
            "cannot be qualified (AC2).",
        )
    if record.is_qualified:
        return (
            True,
            f"platform {profile.profile_id!r} is qualified by {record.verified_by}.",
        )
    return (
        False,
        f"platform {profile.profile_id!r} is observed but unqualified: failed "
        f"{list(record.failed_checks)} (AC2).",
    )


# ---------------------------------------------------------------------------
# AC3/AC4 — host surfaces, shared folders, credentials, networking, screenshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SurfaceGrant:
    """A separate, explicit grant for one host surface to the guest.

    There is no bundle: granting screenshots says nothing about the clipboard,
    and each grant carries the scope it is limited to (AC3).
    """

    surface: str
    granted: bool
    scope: str = ""
    read_only: bool = True
    redacted: bool = True

    def __post_init__(self) -> None:
        if self.surface not in HOST_SURFACES:
            raise DesktopRefusedError(
                "unknown_host_surface",
                f"unknown host surface {self.surface!r}; expected one of {list(HOST_SURFACES)}.",
            )
        if not isinstance(self.granted, bool):
            raise DesktopRefusedError("invalid_grant", "granted must be a boolean.")
        object.__setattr__(self, "scope", str(self.scope or "").strip())
        if self.granted and not self.scope:
            raise DesktopRefusedError(
                "grant_without_scope",
                f"granting {self.surface!r} requires an explicit scope; an unbounded grant is a "
                "full handover (AC3).",
            )
        if not self.granted and (self.scope or not self.read_only):
            raise DesktopRefusedError(
                "refused_grant_carries_scope",
                f"a refused {self.surface!r} grant must not carry a scope or write access.",
            )
        # A surface that reveals host content must be redacted when granted, and
        # a read/write grant over host content is never implicit (AC4).
        if self.granted and self.surface in {"host_screenshots", "host_clipboard"}:
            if not self.redacted:
                raise DesktopRefusedError(
                    "unredacted_host_content_grant",
                    f"{self.surface!r} may only be granted to the guest redacted; raw host content "
                    "is not guest material (AC4).",
                )
            if not self.read_only:
                raise DesktopRefusedError(
                    "host_content_grant_is_writable",
                    f"{self.surface!r} is a read-only surface; a write grant would let the guest "
                    "drive the host (AC3, AC5).",
                )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "granted": self.granted,
            "scope": self.scope if self.granted else "",
            "read_only": self.read_only,
            "redacted": self.redacted,
        }


def ungoverned_host_surfaces(grants: Iterable[SurfaceGrant]) -> tuple[str, ...]:
    """Host surfaces that stay outside guest authority, from defaults alone.

    Every surface not explicitly granted is denied, and a surface is never
    inherited from another grant (AC3).
    """
    granted = {g.surface for g in grants if g.granted}
    return tuple(s for s in HOST_SURFACES if s not in granted)


@dataclass(frozen=True, slots=True)
class SharedFolder:
    """A shared folder exposed to the guest, confined and secret-filtered (AC4)."""

    share_id: str
    guest_path: str
    host_path: str
    read_only: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "share_id", _clean_id(self.share_id, "share_id"))
        object.__setattr__(self, "guest_path", _text(self.guest_path, "guest_path"))
        object.__setattr__(self, "host_path", _text(self.host_path, "host_path"))
        if not isinstance(self.read_only, bool):
            raise DesktopRefusedError("invalid_share", "read_only must be a boolean.")
        for part in (self.host_path, self.guest_path):
            if "\x00" in part:
                raise DesktopRefusedError(
                    "invalid_share_path",
                    "a share path must not contain NUL characters (AC4).",
                )
            segments = [s for s in part.replace("\\", "/").split("/") if s]
            if any(s in {".git", ".."} for s in segments):
                raise DesktopRefusedError(
                    "share_reaches_protected_metadata",
                    f"share path {part!r} reaches repository metadata or escapes upward (AC4).",
                )
        name = self.host_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name in sandbox_boundary.SECRET_NAMES:
            raise DesktopRefusedError(
                "share_exposes_secret_file",
                f"share exposes {name!r}, which sandbox_boundary treats as secret material (AC4).",
                details={"name": name},
            )
        if not self.host_path.startswith(WORKSPACE_ROOT_TOKEN):
            raise DesktopRefusedError(
                "share_outside_workspace_root",
                f"a shared host path must live under {WORKSPACE_ROOT_TOKEN}; a guest must not reach "
                "the rest of the host filesystem (AC4).",
                details={"host_path": self.host_path},
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "share_id": self.share_id,
            "guest_path": self.guest_path,
            "host_path": self.host_path,
            "read_only": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class GuestNetwork:
    """Guest networking: absent by default, and never host-bridging (AC4)."""

    mode: str = "none"
    allowed_hosts: tuple[str, ...] = ()
    forwards_host_credentials: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"none", "allowlist"}:
            raise DesktopRefusedError(
                "unsupported_guest_network",
                f"guest network mode {self.mode!r} is not supported; use 'none' or 'allowlist' (AC4).",
            )
        if not isinstance(self.forwards_host_credentials, bool):
            raise DesktopRefusedError(
                "invalid_guest_network", "forwards_host_credentials must be a boolean."
            )
        if self.forwards_host_credentials:
            raise DesktopRefusedError(
                "guest_network_forwards_credentials",
                "guest networking may not forward host credentials (AC4).",
            )
        allowed = tuple(
            sorted({str(h).strip() for h in self.allowed_hosts if str(h).strip()})
        )
        if self.mode == "none" and allowed:
            raise DesktopRefusedError(
                "no_network_with_allowlist",
                "guest network mode 'none' must not carry an allowlist (AC4).",
            )
        if self.mode == "allowlist" and not allowed:
            raise DesktopRefusedError(
                "allowlist_network_is_empty",
                "an 'allowlist' guest network must name the hosts it may reach (AC4).",
            )
        object.__setattr__(self, "allowed_hosts", allowed)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allowed_hosts": list(self.allowed_hosts),
            "forwards_host_credentials": False,
            "bridges_host_network": False,
        }


@dataclass(frozen=True, slots=True)
class ScreenshotPolicy:
    """How guest screenshots are produced, scoped and redacted (AC4)."""

    enabled: bool = False
    destination: str = ""
    redact: bool = True
    max_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise DesktopRefusedError(
                "invalid_screenshot_policy", "enabled must be a boolean."
            )
        object.__setattr__(self, "destination", str(self.destination or "").strip())
        if not self.enabled:
            if self.destination or self.max_bytes:
                raise DesktopRefusedError(
                    "disabled_screenshot_carries_target",
                    "a disabled screenshot policy must not name a destination or a size cap.",
                )
            return
        if not self.destination.startswith(WORKSPACE_ROOT_TOKEN):
            raise DesktopRefusedError(
                "screenshot_outside_workspace_root",
                f"screenshots must land under {WORKSPACE_ROOT_TOKEN}, not {self.destination!r} "
                "(AC4).",
            )
        if not self.redact:
            raise DesktopRefusedError(
                "unredacted_screenshots",
                "guest screenshots must be redacted before storage (AC4).",
            )
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 1
        ):
            raise DesktopRefusedError(
                "screenshot_cap_missing",
                "an enabled screenshot policy must cap its own size (AC4).",
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "destination": self.destination if self.enabled else "",
            "redact": self.redact,
            "max_bytes": self.max_bytes if self.enabled else 0,
            "captures_host_desktop": False,
        }


def redact_screenshot_text(text: str) -> tuple[str, int]:
    """Redact a screenshot's OCR text before storage, reusing history privacy."""
    return history_privacy.redact_text(text)


@dataclass(frozen=True, slots=True)
class GuestBoundary:
    """The full host/guest boundary for one guest, assembled from explicit grants."""

    grants: tuple[SurfaceGrant, ...] = ()
    shares: tuple[SharedFolder, ...] = ()
    network: GuestNetwork = GuestNetwork()
    screenshots: ScreenshotPolicy = ScreenshotPolicy()
    credentials_shared: bool = False

    def __post_init__(self) -> None:
        grants = tuple(self.grants)
        seen: set[str] = set()
        for grant in grants:
            if not isinstance(grant, SurfaceGrant):
                raise TypeError("grants must hold SurfaceGrant values.")
            if grant.surface in seen:
                raise DesktopRefusedError(
                    "duplicate_surface_grant",
                    f"{grant.surface!r} is granted twice; one surface, one decision (AC3).",
                )
            seen.add(grant.surface)
        object.__setattr__(self, "grants", grants)
        shares = tuple(self.shares)
        if any(not isinstance(s, SharedFolder) for s in shares):
            raise TypeError("shares must hold SharedFolder values.")
        paths = [s.host_path.rstrip("/") for s in shares]
        for i, left in enumerate(paths):
            for right in paths[i + 1 :]:
                if (
                    left == right
                    or left.startswith(right + "/")
                    or right.startswith(left + "/")
                ):
                    raise DesktopRefusedError(
                        "overlapping_shares",
                        f"shares {left!r} and {right!r} overlap; one guest path must not be reachable "
                        "through two shares (AC4).",
                    )
        object.__setattr__(self, "shares", shares)
        for attr in ("network", "screenshots"):
            if not isinstance(getattr(self, attr), (GuestNetwork, ScreenshotPolicy)):
                raise TypeError(f"{attr} has the wrong type.")
        if not isinstance(self.credentials_shared, bool):
            raise DesktopRefusedError(
                "invalid_credentials_flag", "credentials_shared must be a boolean."
            )
        if self.credentials_shared:
            raise DesktopRefusedError(
                "guest_receives_host_credentials",
                "host credentials are never shared with an untrusted guest; grant a scoped token "
                "instead (AC3, AC4).",
            )

    def granted_surfaces(self) -> tuple[str, ...]:
        return tuple(sorted(g.surface for g in self.grants if g.granted))

    def denied_surfaces(self) -> tuple[str, ...]:
        return ungoverned_host_surfaces(self.grants)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grants": [
                g.to_mapping() for g in sorted(self.grants, key=lambda g: g.surface)
            ],
            "shares": [
                s.to_mapping() for s in sorted(self.shares, key=lambda s: s.share_id)
            ],
            "network": self.network.to_mapping(),
            "screenshots": self.screenshots.to_mapping(),
            "credentials_shared": False,
            "granted_surfaces": list(self.granted_surfaces()),
            "denied_surfaces": list(self.denied_surfaces()),
        }


# ---------------------------------------------------------------------------
# AC5 — a guest failure never falls back to controlling the host
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuestFailure:
    """What went wrong in the guest, and what the caller may do next.

    ``host_fallback_available`` is always false: it is not a field a caller can
    set. There is no host fallback for a guest task, so a guest that fails has
    failed (AC5).
    """

    reason: str
    at_stage: str
    contained: bool = True
    host_actions_taken: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "at_stage", _clean_id(self.at_stage, "at_stage"))
        if not isinstance(self.contained, bool):
            raise DesktopRefusedError("invalid_failure", "contained must be a boolean.")
        if not self.contained:
            raise DesktopRefusedError(
                "guest_failure_not_contained",
                "a guest failure must be contained inside the guest boundary; an uncontained "
                "failure is an incident, not a result (AC5).",
            )
        if isinstance(self.host_actions_taken, bool) or not isinstance(
            self.host_actions_taken, int
        ):
            raise DesktopRefusedError(
                "invalid_failure", "host_actions_taken must be an integer."
            )
        if self.host_actions_taken:
            raise HostFallbackRefusedError(
                "host_fallback_occurred",
                f"{self.host_actions_taken} host action(s) were taken after a guest failure; a guest "
                "failure may never fall back to controlling the host (AC5).",
            )

    @property
    def host_fallback_available(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "at_stage": self.at_stage,
            "contained": self.contained,
            "host_actions_taken": 0,
            "host_fallback_available": False,
            "next_step": "report the failure to the operator; do not retry on the host (AC5)",
        }


def assert_no_host_fallback(*, host_actions_taken: int) -> None:
    """Refuse any attempt to continue a failed guest task on the host (AC5)."""
    if isinstance(host_actions_taken, bool) or not isinstance(host_actions_taken, int):
        raise DesktopRefusedError(
            "invalid_host_action_count", "host_actions_taken must be an integer."
        )
    if host_actions_taken < 0:
        raise DesktopRefusedError(
            "invalid_host_action_count", "host_actions_taken must not be negative."
        )
    if host_actions_taken:
        raise HostFallbackRefusedError(
            "host_fallback_refused",
            "a guest failure may not be retried by controlling the host; stop and report (AC5).",
        )


# ---------------------------------------------------------------------------
# AC6 — publish maturity/limitations, keep the adapter disabled unless qualified
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuestDesktopAdapter:
    """The optional adapter. It has no executor and is disabled unless qualified.

    There is no method to run a task, because this story does not implement a
    driver. ``enabled`` is derived, never set: it is true only for a qualified
    adapter whose platform checks passed (AC6).
    """

    adapter_id: str
    qualification: QualificationRecord
    maturity: MaturityState = "not_qualified"
    limitations: tuple[str, ...] = ()
    enabled_flag: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _clean_id(self.adapter_id, "adapter_id"))
        if not isinstance(self.qualification, QualificationRecord):
            raise TypeError("qualification must be a QualificationRecord.")
        if self.maturity not in MATURITY_STATES:
            raise DesktopRefusedError(
                "unknown_maturity", f"unknown maturity state: {self.maturity!r}."
            )
        object.__setattr__(
            self,
            "limitations",
            tuple(str(x).strip() for x in self.limitations if str(x).strip()),
        )
        if not isinstance(self.enabled_flag, bool):
            raise DesktopRefusedError(
                "invalid_enabled_flag", "enabled_flag must be a boolean."
            )
        if self.enabled_flag and not self.is_qualified:
            raise DesktopRefusedError(
                "adapter_enabled_without_qualification",
                "an unqualified guest-desktop adapter may not be enabled; qualification is the "
                "only path to enabling it (AC6).",
            )
        if self.maturity == "qualified" and not self.is_qualified:
            raise DesktopRefusedError(
                "maturity_claims_qualification",
                "maturity 'qualified' requires a qualified record (AC6).",
            )
        if self.maturity == "not_qualified" and self.is_qualified:
            raise DesktopRefusedError(
                "qualified_record_reported_unqualified",
                "a qualified record must not be published as 'not_qualified' (AC6).",
            )
        if not self.limitations and self.maturity != "qualified":
            raise DesktopRefusedError(
                "maturity_without_limitations",
                "an adapter that is not fully qualified must publish its limitations (AC6).",
            )

    @property
    def is_qualified(self) -> bool:
        return self.qualification.is_qualified

    @property
    def enabled(self) -> bool:
        """Derived, never asserted: an adapter is enabled only when qualified."""
        return self.is_qualified and self.maturity == "qualified" and self.enabled_flag

    @property
    def has_executor(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "maturity": self.maturity,
            "enabled": self.enabled,
            "enabled_flag": self.enabled_flag,
            "qualified": self.is_qualified,
            "has_executor": False,
            "qualification": self.qualification.to_mapping(),
            "limitations": list(self.limitations),
        }


def default_disabled_adapter(
    *, qualification: QualificationRecord | None = None
) -> GuestDesktopAdapter:
    """The state this story ships in: present, described, and disabled (AC6)."""
    record = qualification or QualificationRecord(state="not_measured")
    limitations = (
        "no guest desktop was launched or observed in this environment, so no platform is qualified",
        "there is no executor: this module registers capability and authority, it does not drive a desktop",
        "a GUI-only task still needs a per-task justification against the API and browser workers",
        "host files, accounts, clipboard and screenshots remain outside guest authority unless separately granted and redacted",
    )
    maturity: MaturityState = (
        "qualified"
        if record.is_qualified
        else (
            "provisional" if record.state == "observed_unqualified" else "not_qualified"
        )
    )
    return GuestDesktopAdapter(
        adapter_id="guest-desktop",
        qualification=record,
        maturity=maturity,
        limitations=limitations,
        enabled_flag=False,
    )


# ---------------------------------------------------------------------------
# Bake-off record (AC1, AC2, AC6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BakeOffEntry:
    """One use case measured through one driver, with what it cost to run it."""

    use_case: str
    driver: str
    completed: bool
    latency_ms: float
    host_interventions: int
    failure: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "use_case", _clean_id(self.use_case, "use_case"))
        object.__setattr__(self, "driver", _clean_id(self.driver, "driver"))
        if not isinstance(self.completed, bool):
            raise DesktopRefusedError(
                "invalid_bake_off_entry", "completed must be a boolean."
            )
        if isinstance(self.latency_ms, bool) or not isinstance(
            self.latency_ms, (int, float)
        ):
            raise DesktopRefusedError(
                "invalid_bake_off_entry", "latency_ms must be a number."
            )
        if float(self.latency_ms) < 0:
            raise DesktopRefusedError(
                "invalid_bake_off_entry", "latency_ms must not be negative."
            )
        if isinstance(self.host_interventions, bool) or not isinstance(
            self.host_interventions, int
        ):
            raise DesktopRefusedError(
                "invalid_bake_off_entry", "host_interventions must be an integer."
            )
        if self.host_interventions < 0:
            raise DesktopRefusedError(
                "invalid_bake_off_entry", "host_interventions must not be negative."
            )
        object.__setattr__(self, "failure", str(self.failure or "").strip())
        if not self.completed and not self.failure:
            raise DesktopRefusedError(
                "failed_entry_without_reason",
                "an incomplete bake-off entry must state why; a silent failure is not a result (AC6).",
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "use_case": self.use_case,
            "driver": self.driver,
            "completed": self.completed,
            "latency_ms": round(float(self.latency_ms), 6),
            "host_interventions": self.host_interventions,
            "failure": self.failure,
        }


@dataclass(frozen=True, slots=True)
class BakeOffReport:
    """A use-case-specific driver/guest comparison, or an honest empty one.

    ``measured`` is false when no entry exists, and a recommendation cannot be
    drawn from an unmeasured bake-off (AC6).
    """

    entries: tuple[BakeOffEntry, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        return bool(self.entries)

    def drivers_seen(self) -> tuple[str, ...]:
        return tuple(sorted({e.driver for e in self.entries}))

    def per_use_case(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for entry in self.entries:
            out.setdefault(entry.use_case, {})[entry.driver] = entry.to_mapping()
        return out

    def recommend(self) -> dict[str, Any]:
        """Recommend only where a driver completed with fewer host interventions."""
        if not self.measured:
            return {
                "measured": False,
                "recommendation": None,
                "reason": "no bake-off was run, so no driver is recommended (AC6).",
            }
        best: dict[str, Any] = {}
        for use_case, by_driver in sorted(self.per_use_case().items()):
            completed = {d: v for d, v in by_driver.items() if v["completed"]}
            if not completed:
                best[use_case] = {
                    "driver": None,
                    "reason": "no driver completed this use case (AC6).",
                }
                continue
            winner = min(
                completed.items(),
                key=lambda kv: (kv[1]["host_interventions"], kv[1]["latency_ms"]),
            )
            best[use_case] = {
                "driver": winner[0],
                "host_interventions": winner[1]["host_interventions"],
                "latency_ms": winner[1]["latency_ms"],
                "reason": "completed with the fewest host interventions, then lowest latency.",
            }
        return {"measured": True, "recommendation": best}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "measured": self.measured,
            "candidates": list(self.candidates),
            "drivers_seen": list(self.drivers_seen()),
            "entries": [e.to_mapping() for e in self.entries],
            "per_use_case": self.per_use_case(),
            "recommendation": self.recommend(),
        }

    def digest(self) -> str:
        return _canonical_sha256(self.to_mapping())


# ---------------------------------------------------------------------------
# Reviewability
# ---------------------------------------------------------------------------


def render_capability(
    *,
    justification: DesktopJustification | None,
    profile: PlatformProfile | None,
    qualification: QualificationRecord,
    boundary: GuestBoundary | None = None,
    bake_off: BakeOffReport | None = None,
    adapter: GuestDesktopAdapter | None = None,
) -> str:
    """Deterministic sorted-key JSON describing the capability and its limits."""
    admitted, reason = evaluate_platform_qualification(profile, qualification)
    shipped = adapter or default_disabled_adapter(qualification=qualification)
    payload: dict[str, Any] = {
        "schema_version": GUEST_DESKTOP_SCHEMA_VERSION,
        "platform": {
            "admitted": admitted,
            "reason": reason,
            "profile": profile.to_mapping() if profile is not None else None,
            "qualification": qualification.to_mapping(),
        },
        "justification": justification.to_mapping()
        if justification is not None
        else None,
        "boundary": boundary.to_mapping() if boundary is not None else None,
        "bake_off": bake_off.to_mapping() if bake_off is not None else None,
        "adapter": shipped.to_mapping(),
        "desktop_rules": {
            "desktop_permitted_without_task_justification": False,
            "platform_admitted_without_observation": False,
            "host_surfaces_granted_implicitly": False,
            "host_credentials_shared_with_guest": False,
            "unredacted_host_capture_permitted": False,
            "guest_failure_may_fall_back_to_host": False,
            "adapter_enabled_without_qualification": False,
        },
        "does_not_prove": [
            "no virtual machine, desktop, service or provider was launched; every platform fact is caller-supplied",
            "an unqualified adapter here means the story shipped no driver, not that the platform failed a test that was never run",
            "no GUI-only task was executed on a real guest, so no use case is demonstrated to need desktop control",
        ],
    }
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "GUEST_DESKTOP_SCHEMA_VERSION",
    "HOST_SURFACES",
    "MATURITY_STATES",
    "QUALIFICATION_STATES",
    "RESOURCE_DIMENSIONS",
    "SIMPLER_CAPABILITIES",
    "GuestDesktopError",
    "DesktopRefusedError",
    "HostFallbackRefusedError",
    "SimplerToolAttempt",
    "DesktopJustification",
    "PlatformProfile",
    "QualificationRecord",
    "SurfaceGrant",
    "SharedFolder",
    "GuestNetwork",
    "ScreenshotPolicy",
    "GuestBoundary",
    "GuestFailure",
    "GuestDesktopAdapter",
    "BakeOffEntry",
    "BakeOffReport",
    "default_disabled_adapter",
    "evaluate_platform_qualification",
    "redact_screenshot_text",
    "render_capability",
    "require_desktop_justification",
    "ungoverned_host_surfaces",
]
