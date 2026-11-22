"""AP-011 — discover and inspect capability candidates without activation.

This module answers one question and refuses to answer any other: *what, if
anything, is actually missing, and what would we need to look at to fix it?* It
diagnoses gaps, records candidate proposals with their provenance and risk
surface, and assesses licenses and install hooks. It never installs, executes,
downloads, contacts a provider or activates anything — there is no code path in
this module that could, and the registry refuses to represent "installed".

The six AP-011 acceptance criteria map onto this module one-to-one:

1. *Distinguish gap kinds* — :class:`GapKind` separates missing **facts**,
   **method**, **model capability**, **context**, **tool** and **hardware**.
   The classifier never collapses them: a missing fact is not a model problem.
2. *Discovery may conclude nothing is needed* — :func:`classify_gap` returns
   :data:`NO_GAP` and :meth:`CandidateRegistry.recommendation` returns
   ``NO_NEW_DEPENDENCY``; no candidate is invented to justify itself.
3. *Candidates record their surface* — :class:`CapabilityCandidate` requires
   source revision, maintenance evidence, license, install hooks, resource cost,
   egress and privileges, and marks unknown values as unknown rather than
   guessing.
4. *Stars and annotations never establish trust* — popularity is stored as an
   unverified claim and is **excluded** from the trust decision;
   :func:`establish_trust` only accepts verified first-party evidence.
5. *Unlicensed or incompatible candidates are blocked* — for the affected reuse
   only, and never installed: :class:`Assessment` carries ``blocked`` plus the
   reason, and the registry has no install verb at all.
6. *No execution or activation authority* — construction refuses
   ``authorizes_execution``/``authorizes_activation``, and every assessment
   states that discovery granted nothing.

Design rules follow ``supply_chain.py``'s example: nothing is claimed that did
not happen, unknown is a first-class value, and output is deterministic so the
same inputs produce byte-identical proposals that are reviewable in a diff.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DiscoveryError",
    "ActivationRefusedError",
    "GapKind",
    "CapabilityGap",
    "NO_GAP",
    "classify_gap",
    "CandidateKind",
    "TrustBasis",
    "CapabilityCandidate",
    "Maintenance",
    "InstallHooks",
    "ResourceCost",
    "Egress",
    "Privileges",
    "LicenseAssessment",
    "assess_license",
    "CandidateRegistry",
    "Assessment",
    "Recommendation",
    "RecommendationKind",
    "establish_trust",
    "PROPOSAL_SCHEMA_VERSION",
    "render_proposal",
    "SPDX_REVIEWED",
]


class DiscoveryError(ValueError):
    """Caller misuse: an unusable or contradictory discovery input."""


class ActivationRefusedError(DiscoveryError):
    """A caller asked discovery to authorize execution or activation."""


# ---------------------------------------------------------------------------
# AC1 — gap classification
# ---------------------------------------------------------------------------


class GapKind:
    """The seven outcomes of a gap diagnosis. ``NONE`` is a real answer."""

    NONE = "none"
    MISSING_FACTS = "missing_facts"
    MISSING_METHOD = "missing_method"
    MODEL_CAPABILITY = "model_capability"
    CONTEXT = "context"
    TOOL = "tool"
    HARDWARE = "hardware"

    ALL = (NONE, MISSING_FACTS, MISSING_METHOD, MODEL_CAPABILITY, CONTEXT, TOOL, HARDWARE)


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    """One diagnosis, with the evidence that produced it and the remedy class."""

    kind: str
    reason: str
    #: What would actually close this gap. Discovery may name it; it may not do it.
    remedy: str
    #: True only when a *new dependency* is genuinely the remedy. A missing fact
    #: or a better method needs no dependency at all.
    needs_new_dependency: bool

    def __post_init__(self) -> None:
        if self.kind not in GapKind.ALL:
            raise DiscoveryError(f"unknown gap kind {self.kind!r}.")
        if not str(self.reason).strip():
            raise DiscoveryError("a gap must state its reason.")
        if not str(self.remedy).strip():
            raise DiscoveryError("a gap must state its remedy.")
        if self.kind == GapKind.NONE and self.needs_new_dependency:
            raise DiscoveryError("a 'no gap' diagnosis cannot require a new dependency.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "remedy": self.remedy,
            "needs_new_dependency": self.needs_new_dependency,
        }


NO_GAP: CapabilityGap = CapabilityGap(
    kind=GapKind.NONE,
    reason="the recorded evidence covers every requirement for this task.",
    remedy="none — proceed with the existing capability.",
    needs_new_dependency=False,
)

#: Signals ordered from cheapest to most invasive. The classifier reports the
#: earliest one that explains the failure, because installing a tool to fix a
#: missing fact is the exact mistake this story exists to prevent.
_SIGNAL_GAP: tuple[tuple[str, str, str, bool], ...] = (
    (
        "missing_facts",
        GapKind.MISSING_FACTS,
        "required information was never collected or is not in the workspace.",
        False,
    ),
    (
        "wrong_method",
        GapKind.MISSING_METHOD,
        "the evidence was available but the approach did not use it.",
        False,
    ),
    (
        "context_exceeded",
        GapKind.CONTEXT,
        "the required material exceeds the available context window.",
        False,
    ),
    (
        "model_incapable",
        GapKind.MODEL_CAPABILITY,
        "the selected model cannot perform this operation at all.",
        True,
    ),
    (
        "no_tool",
        GapKind.TOOL,
        "no available tool can perform this operation.",
        True,
    ),
    (
        "hardware_limit",
        GapKind.HARDWARE,
        "installed hardware cannot meet the resource requirement.",
        True,
    ),
)


def classify_gap(signals: Iterable[str], *, note: str | None = None) -> CapabilityGap:
    """Classify a failure from structured signals, or conclude there is no gap.

    Empty signals mean no gap was observed — which is a legitimate, expected
    outcome (AC2), not a failure to diagnose.
    """
    seen = {str(signal).strip().lower() for signal in signals if str(signal).strip()}
    for signal, kind, reason, needs_dep in _SIGNAL_GAP:
        if signal in seen:
            return CapabilityGap(
                kind=kind,
                reason=f"{reason}{(' ' + note.strip()) if note and note.strip() else ''}",
                remedy=_REMEDY[kind],
                needs_new_dependency=needs_dep,
            )
    if seen:
        unknown = ", ".join(sorted(seen))
        raise DiscoveryError(f"unrecognised gap signal(s): {unknown}.")
    return NO_GAP


_REMEDY = {
    GapKind.MISSING_FACTS: "collect the missing evidence; no dependency is needed.",
    GapKind.MISSING_METHOD: "change the approach; no dependency is needed.",
    GapKind.CONTEXT: "reduce or re-rank the material; no new dependency is required by default.",
    GapKind.MODEL_CAPABILITY: "propose a different model as a candidate for review.",
    GapKind.TOOL: "propose a tool or service as a candidate for review.",
    GapKind.HARDWARE: "record the hardware shortfall; a purchase is an owner decision.",
}


# ---------------------------------------------------------------------------
# AC3 — candidate proposals
# ---------------------------------------------------------------------------


class CandidateKind:
    TOOL = "tool"
    MODEL = "model"
    SKILL = "skill"
    SERVICE = "service"
    DATA = "data"

    ALL = (TOOL, MODEL, SKILL, SERVICE, DATA)


class TrustBasis:
    """How a candidate's trust was established. Popularity is not on this list."""

    VERIFIED_UPSTREAM_RELEASE = "verified_upstream_release"
    PINNED_SIGNED_ARTIFACT = "pinned_signed_artifact"
    FIRST_PARTY_AUDIT = "first_party_audit"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Maintenance:
    """Observed maintenance evidence. Unknown stays unknown."""

    last_release_at: str | None = None
    days_since_release: int | None = None
    open_issues: int | None = None
    archived: bool | None = None
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_release_at": self.last_release_at,
            "days_since_release": self.days_since_release,
            "open_issues": self.open_issues,
            "archived": self.archived,
            "evidence": self.evidence,
        }

    @property
    def is_stale(self) -> bool | None:
        """None when staleness cannot be judged from the recorded evidence."""
        if self.archived is True:
            return True
        if self.days_since_release is None:
            return None
        return self.days_since_release > 730


@dataclass(frozen=True, slots=True)
class InstallHooks:
    """What would run at install time. Empty is the safe default and must be stated."""

    build_scripts: tuple[str, ...] = ()
    post_install_scripts: tuple[str, ...] = ()
    native_extensions: tuple[str, ...] = ()
    network_at_install: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "build_scripts", tuple(sorted(set(self.build_scripts))))
        object.__setattr__(
            self, "post_install_scripts", tuple(sorted(set(self.post_install_scripts)))
        )
        object.__setattr__(
            self, "native_extensions", tuple(sorted(set(self.native_extensions)))
        )

    @property
    def has_hooks(self) -> bool:
        return bool(self.build_scripts or self.post_install_scripts or self.native_extensions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_scripts": list(self.build_scripts),
            "post_install_scripts": list(self.post_install_scripts),
            "native_extensions": list(self.native_extensions),
            "network_at_install": self.network_at_install,
            "has_hooks": self.has_hooks,
        }


@dataclass(frozen=True, slots=True)
class ResourceCost:
    """Estimated footprint. Estimates are labelled estimates, never measurements."""

    download_bytes: int | None = None
    disk_bytes: int | None = None
    memory_bytes: int | None = None
    runtime_seconds_per_call: float | None = None
    basis: str = "estimated"

    def __post_init__(self) -> None:
        for name in ("download_bytes", "disk_bytes", "memory_bytes"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise DiscoveryError(f"ResourceCost.{name} must be a non-negative integer or None.")
        if self.basis not in {"estimated", "measured", "unknown"}:
            raise DiscoveryError("ResourceCost.basis must be estimated, measured or unknown.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_bytes": self.download_bytes,
            "disk_bytes": self.disk_bytes,
            "memory_bytes": self.memory_bytes,
            "runtime_seconds_per_call": self.runtime_seconds_per_call,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class Egress:
    """Outbound reach the candidate would need. Default is none at all."""

    hosts: tuple[str, ...] = ()
    schemes: tuple[str, ...] = ()
    account_required: bool | None = None
    spend_required: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hosts": list(self.hosts),
            "schemes": list(self.schemes),
            "account_required": self.account_required,
            "spend_required": self.spend_required,
        }

    @property
    def is_networked(self) -> bool:
        return bool(self.hosts or self.schemes)


@dataclass(frozen=True, slots=True)
class Privileges:
    """Authorities the candidate would need if it were ever used."""

    filesystem_write: bool = False
    code_execution: bool = False
    external_mcp: bool = False
    secret_access: bool = False
    #: Capabilities the candidate would exercise, using the repository's own
    #: canonical vocabulary from scoped_grants.resolve_tool_capabilities.
    capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "filesystem_write": self.filesystem_write,
            "code_execution": self.code_execution,
            "external_mcp": self.external_mcp,
            "secret_access": self.secret_access,
            "capabilities": list(self.capabilities),
        }

    @property
    def is_broad(self) -> bool:
        return bool(
            self.code_execution or self.external_mcp or self.secret_access
        )


@dataclass(frozen=True, slots=True)
class LicenseAssessment:
    """AC5: what a license permits, and which reuse it blocks."""

    spdx_id: str | None
    status: str  # reviewed-permissive | reviewed-copyleft | reviewed-proprietary | unknown | unlicensed
    blocked_reuse: tuple[str, ...] = ()
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.blocked_reuse and self.blocks_any_reuse is False and self.status in {
            "unknown",
            "unlicensed",
        }:
            raise DiscoveryError(
                "an unknown or unlicensed candidate must state which reuse it blocks."
            )

    @property
    def blocks_any_reuse(self) -> bool:
        return bool(self.blocked_reuse)

    @property
    def is_blocking(self) -> bool:
        return self.status in {"unknown", "unlicensed"} or bool(self.blocked_reuse)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spdx_id": self.spdx_id,
            "status": self.status,
            "blocked_reuse": list(self.blocked_reuse),
            "note": self.note,
            "is_blocking": self.is_blocking,
        }


#: Licenses this repository's own distribution terms are compatible with. Kept
#: deliberately small and explicit: a license not on this list is *unknown*, not
#: "probably fine". The repository is MIT-licensed (see LICENSE).
SPDX_REVIEWED: Mapping[str, str] = {
    "MIT": "reviewed-permissive",
    "BSD-2-Clause": "reviewed-permissive",
    "BSD-3-Clause": "reviewed-permissive",
    "Apache-2.0": "reviewed-permissive",
    "ISC": "reviewed-permissive",
    "0BSD": "reviewed-permissive",
    "PSF-2.0": "reviewed-permissive",
    "MPL-2.0": "reviewed-copyleft",
    "LGPL-3.0-only": "reviewed-copyleft",
    "GPL-3.0-only": "reviewed-copyleft",
    "AGPL-3.0-only": "reviewed-copyleft",
    "Unlicense": "reviewed-permissive",
}

_COPYLEFT = {"MPL-2.0", "LGPL-3.0-only", "GPL-3.0-only", "AGPL-3.0-only"}


def assess_license(
    spdx_id: str | None,
    *,
    reuse: Sequence[str] = ("internal_use",),
    note: str | None = None,
) -> LicenseAssessment:
    """Assess a declared license for the named reuse; unknown blocks by default.

    ``unlicensed`` (no license at all) is distinct from ``unknown`` (a license
    this repository has not reviewed) — both block, with different reasons,
    because "nobody said" and "we did not read it" are different problems.
    """
    reuse = tuple(dict.fromkeys(str(item).strip() for item in reuse if str(item).strip()))
    if not reuse:
        raise DiscoveryError("at least one reuse class must be assessed.")
    if spdx_id is None or not str(spdx_id).strip():
        return LicenseAssessment(
            spdx_id=None,
            status="unlicensed",
            blocked_reuse=reuse,
            note=note or "no license declared; every reuse is blocked until one is established.",
        )
    clean = str(spdx_id).strip()
    status = SPDX_REVIEWED.get(clean)
    if status is None:
        return LicenseAssessment(
            spdx_id=clean,
            status="unknown",
            blocked_reuse=reuse,
            note=note or f"license {clean} has not been reviewed for this repository.",
        )
    if clean in _COPYLEFT:
        blocked = tuple(r for r in reuse if r in {"redistribution", "bundling", "commercial_use"})
        if clean == "AGPL-3.0-only" and "network_service" in reuse:
            blocked = blocked + ("network_service",)
        return LicenseAssessment(
            spdx_id=clean,
            status=status,
            blocked_reuse=blocked,
            note=note or "copyleft: permitted for internal use, restricted for distribution.",
        )
    return LicenseAssessment(spdx_id=clean, status=status, blocked_reuse=(), note=note)


@dataclass(frozen=True, slots=True)
class CapabilityCandidate:
    """A proposed dependency. Descriptive only — it cannot be installed by this module."""

    candidate_id: str
    kind: str
    name: str
    source_url: str
    source_revision: str
    maintainer: str | None
    maintenance: Maintenance
    install_hooks: InstallHooks
    resource_cost: ResourceCost
    egress: Egress
    privileges: Privileges
    license: LicenseAssessment
    trust_basis: str = TrustBasis.UNVERIFIED
    #: Popularity, recorded as an untrusted claim. Never used for trust (AC4).
    popularity_claim: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CandidateKind.ALL:
            raise DiscoveryError(f"unknown candidate kind {self.kind!r}.")
        for field_name in ("candidate_id", "name", "source_url", "source_revision"):
            if not str(getattr(self, field_name) or "").strip():
                raise DiscoveryError(f"candidate.{field_name} must not be empty.")
        if self.trust_basis not in {
            TrustBasis.VERIFIED_UPSTREAM_RELEASE,
            TrustBasis.PINNED_SIGNED_ARTIFACT,
            TrustBasis.FIRST_PARTY_AUDIT,
            TrustBasis.UNVERIFIED,
        }:
            raise DiscoveryError(f"unknown trust basis {self.trust_basis!r}.")
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", self.candidate_id):
            raise DiscoveryError(
                "candidate_id must be 1-64 lowercase characters (letters, digits, . _ -)."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "name": self.name,
            "source_url": self.source_url,
            "source_revision": self.source_revision,
            "maintainer": self.maintainer,
            "maintenance": self.maintenance.to_dict(),
            "install_hooks": self.install_hooks.to_dict(),
            "resource_cost": self.resource_cost.to_dict(),
            "egress": self.egress.to_dict(),
            "privileges": self.privileges.to_dict(),
            "license": self.license.to_dict(),
            "trust_basis": self.trust_basis,
            "popularity_claim": self.popularity_claim,
            "popularity_establishes_trust": False,
        }


# ---------------------------------------------------------------------------
# AC4 — trust
# ---------------------------------------------------------------------------


def establish_trust(
    candidate: CapabilityCandidate,
    *,
    evidence: Sequence[str] = (),
    basis: str = TrustBasis.UNVERIFIED,
) -> CapabilityCandidate:
    """Return a candidate with a trust basis established only from named evidence.

    Stars, download counts and MCP annotations are explicitly not evidence: a
    candidate carrying only popularity stays ``unverified`` no matter how it is
    described, and the attempt is recorded rather than silently accepted.
    """
    clean = [str(item).strip() for item in evidence if str(item).strip()]
    popularity = [item for item in clean if re.search(r"star|popular|download|annotation", item, re.I)]
    if popularity:
        raise DiscoveryError(
            "popularity, stars and MCP annotations never establish trust; "
            f"remove {popularity!r} and cite a verified release, signature or audit."
        )
    if basis == TrustBasis.UNVERIFIED:
        return candidate
    if not clean:
        raise DiscoveryError(f"trust basis {basis!r} requires named evidence.")
    return _replace_candidate(candidate, trust_basis=basis, popularity_claim=candidate.popularity_claim)


def _replace_candidate(candidate: CapabilityCandidate, **changes: Any) -> CapabilityCandidate:
    values = {f: getattr(candidate, f) for f in candidate.__dataclass_fields__}
    values.update(changes)
    return CapabilityCandidate(**values)


# ---------------------------------------------------------------------------
# AC5 — assessment and registry
# ---------------------------------------------------------------------------


class RecommendationKind:
    NO_NEW_DEPENDENCY = "no_new_dependency"
    PROPOSE_FOR_REVIEW = "propose_for_review"
    BLOCKED = "blocked"

    ALL = (NO_NEW_DEPENDENCY, PROPOSE_FOR_REVIEW, BLOCKED)


@dataclass(frozen=True, slots=True)
class Assessment:
    """The full judgement for one candidate, and what it does not grant."""

    candidate_id: str
    usable_for_reuse: tuple[str, ...]
    blocked_reuse: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    authorizes_execution: bool = False
    authorizes_activation: bool = False
    authorizes_installation: bool = False

    def __post_init__(self) -> None:
        if self.authorizes_execution or self.authorizes_activation or self.authorizes_installation:
            raise ActivationRefusedError(
                "AP-011 discovery grants no execution, activation or installation authority."
            )

    @property
    def is_blocked_entirely(self) -> bool:
        return bool(self.blockers) and not self.usable_for_reuse

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "usable_for_reuse": list(self.usable_for_reuse),
            "blocked_reuse": list(self.blocked_reuse),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "authorizes_execution": self.authorizes_execution,
            "authorizes_activation": self.authorizes_activation,
            "authorizes_installation": self.authorizes_installation,
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    kind: str
    gap: CapabilityGap
    candidates: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RecommendationKind.ALL:
            raise DiscoveryError(f"unknown recommendation kind {self.kind!r}.")
        if self.kind == RecommendationKind.PROPOSE_FOR_REVIEW and not self.candidates:
            raise DiscoveryError("a 'propose for review' recommendation must name candidates.")
        if self.kind != RecommendationKind.PROPOSE_FOR_REVIEW and self.candidates:
            raise DiscoveryError(
                "only a 'propose for review' recommendation may name candidates."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "gap": self.gap.to_dict(),
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


def assess_candidate(
    candidate: CapabilityCandidate,
    *,
    reuse: Sequence[str] = ("internal_use",),
    allowed_hosts: Sequence[str] = (),
    allowed_spend: bool = False,
) -> Assessment:
    """Assess one candidate against the repository's standing authority."""
    reuse = tuple(dict.fromkeys(str(item).strip() for item in reuse if str(item).strip()))
    if not reuse:
        raise DiscoveryError("at least one reuse class must be assessed.")
    blockers: list[str] = []
    warnings: list[str] = []
    blocked: list[str] = []

    licence = assess_license(candidate.license.spdx_id, reuse=reuse, note=candidate.license.note)
    blocked.extend(licence.blocked_reuse)
    if licence.status == "unlicensed":
        blockers.append("no license declared: reuse is blocked until one is established.")
    elif licence.status == "unknown":
        blockers.append(
            f"license {licence.spdx_id!r} has not been reviewed for this repository."
        )

    if candidate.trust_basis == TrustBasis.UNVERIFIED:
        warnings.append("trust is unverified; popularity and annotations do not change that.")
    if candidate.maintenance.is_stale is True:
        warnings.append("maintenance evidence suggests the candidate is stale or archived.")
    elif candidate.maintenance.is_stale is None:
        warnings.append("maintenance cannot be judged from the recorded evidence.")
    if candidate.install_hooks.has_hooks:
        warnings.append(
            "candidate runs install-time hooks; installation would need its own review."
        )
    if candidate.install_hooks.network_at_install:
        blockers.append("candidate fetches from the network at install time.")
    if candidate.egress.is_networked:
        hosts = set(candidate.egress.hosts)
        outside = sorted(hosts - set(allowed_hosts))
        if outside:
            blocked.append("egress")
            warnings.append(
                f"egress to {outside} is outside the granted allowlist; it would be refused."
            )
        else:
            warnings.append("egress is inside the granted allowlist but still requires a grant.")
    if candidate.egress.account_required:
        blockers.append("candidate requires an account, which no standing grant permits.")
    if candidate.egress.spend_required and not allowed_spend:
        blockers.append("candidate requires spend, which no standing grant permits.")
    if candidate.privileges.secret_access:
        blockers.append("candidate requests secret access.")

    usable = tuple(item for item in reuse if item not in set(blocked))
    return Assessment(
        candidate_id=candidate.candidate_id,
        usable_for_reuse=usable,
        blocked_reuse=tuple(dict.fromkeys(blocked)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


@dataclass
class CandidateRegistry:
    """A proposal registry. It records candidates; it cannot install them."""

    gap: CapabilityGap = NO_GAP
    allowed_hosts: tuple[str, ...] = ()
    allowed_spend: bool = False
    _candidates: dict[str, CapabilityCandidate] = field(default_factory=dict)

    # -- registration ------------------------------------------------------

    def register(self, candidate: CapabilityCandidate) -> Assessment:
        if candidate.candidate_id in self._candidates:
            raise DiscoveryError(f"candidate {candidate.candidate_id!r} is already registered.")
        if self.gap.kind == GapKind.NONE:
            raise DiscoveryError(
                "no gap was diagnosed; registering a candidate would invent a dependency."
            )
        self._candidates[candidate.candidate_id] = candidate
        return self.assess(candidate.candidate_id)

    def get(self, candidate_id: str) -> CapabilityCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise DiscoveryError(f"unknown candidate {candidate_id!r}.") from exc

    @property
    def candidates(self) -> tuple[CapabilityCandidate, ...]:
        return tuple(self._candidates[key] for key in sorted(self._candidates))

    def assess(self, candidate_id: str) -> Assessment:
        return assess_candidate(
            self.get(candidate_id),
            allowed_hosts=self.allowed_hosts,
            allowed_spend=self.allowed_spend,
        )

    def assessments(self) -> tuple[Assessment, ...]:
        return tuple(self.assess(c.candidate_id) for c in self.candidates)

    # -- recommendation ----------------------------------------------------

    def recommendation(self) -> Recommendation:
        if self.gap.kind == GapKind.NONE:
            return Recommendation(
                kind=RecommendationKind.NO_NEW_DEPENDENCY,
                gap=self.gap,
                reason="no gap was diagnosed; the existing capability is sufficient.",
            )
        if not self.gap.needs_new_dependency:
            return Recommendation(
                kind=RecommendationKind.NO_NEW_DEPENDENCY,
                gap=self.gap,
                reason=(
                    f"a {self.gap.kind} gap is closed by {self.gap.remedy} "
                    "No new dependency is needed."
                ),
            )
        proposed: list[str] = []
        for candidate in self.candidates:
            assessment = self.assess(candidate.candidate_id)
            if assessment.is_blocked_entirely:
                continue
            proposed.append(candidate.candidate_id)
        if not proposed:
            return Recommendation(
                kind=RecommendationKind.BLOCKED,
                gap=self.gap,
                reason=(
                    "every registered candidate is blocked for the requested reuse; "
                    "nothing is proposed and nothing is installed."
                ),
            )
        return Recommendation(
            kind=RecommendationKind.PROPOSE_FOR_REVIEW,
            gap=self.gap,
            candidates=tuple(proposed),
            reason="candidates are proposed for owner review only; discovery grants nothing.",
        )

    # -- output ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "gap": self.gap.to_dict(),
            "recommendation": self.recommendation().to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "assessments": [a.to_dict() for a in self.assessments()],
            "discovery_grants": {
                "execution": False,
                "installation": False,
                "activation": False,
                "network": False,
            },
        }

    def digest(self) -> str:
        return hashlib.sha256(render_proposal(self).encode()).hexdigest()

    #: Deliberately absent: any method that installs, downloads or executes.
    #: A caller cannot reach a capability through this registry, only a proposal.


PROPOSAL_SCHEMA_VERSION = 1


def render_proposal(registry: CandidateRegistry) -> str:
    """Deterministic JSON (sorted keys, trailing newline) for review in a diff."""
    return json.dumps(registry.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _now() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
