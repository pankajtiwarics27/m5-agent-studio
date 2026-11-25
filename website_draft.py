"""AP-021 — a complete website draft workflow.

The story is one sentence long and it is almost entirely about *refusal*: produce a website
draft, completely, without ever pretending to have more authority or more knowledge than the
run actually has.

A website is the first specialisation the owner selected (decision U01 in the operating
envelope), which makes two failure modes likely and both are boringly common in tools that
generate sites:

1. **Confident invention.** A testimonial from a customer who does not exist, a "trusted by"
   row of logo names, an integration badge for a service nobody connected. These are not
   cosmetic. They are false statements published on someone's behalf, so this module refuses
   to record them as content at all.
2. **Silent unsupported interaction.** A hamburger menu that does nothing, a form whose submit
   goes nowhere, a carousel with no script. The brief asked for interactions; the draft has
   markup that looks like them. This module requires every requested interaction to carry an
   explicit verdict, and "unsupported" is a first-class, visible answer rather than an omission.

Scope, provenance and spend are the other three halves of the same rule: the audience and
website scope are stated before work starts, every asset carries where it came from and what
rights it has, and nothing here deploys, resolves DNS or touches production credentials. There
is no function in this module that can produce a deployment; ``deployment`` is a field that can
only be recorded as refused, and the dataclass raises if a caller tries to write anything else.

The repair loop is bounded in the way the rest of the programme bounds work: a fixed attempt
count that must be stated, and a ``partial`` status that survives to the report instead of being
rounded up to "done". A draft that failed two of three checks reports ``partial``, names the
failures, and does not claim to be a complete draft.

Nothing in this module reads a clock, opens a file or touches the network. The execution it
describes is injected through :class:`DraftExecutor`, exactly as AP-010 injects its browser
driver; this module owns what must be identical on every run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Protocol, Sequence, cast

__all__ = [
    "WebsiteDraftError",
    "ScopeRefused",
    "ContentRefused",
    "ProvenanceRefused",
    "InteractionRefused",
    "DeploymentRefused",
    "RepairExhausted",
    "DraftAudience",
    "DraftScope",
    "AssetProvenance",
    "InteractionVerdict",
    "DraftCheck",
    "RepairAttempt",
    "DeploymentGrant",
    "WebsiteDraft",
    "DraftExecutor",
    "REQUIRED_CHECKS",
    "CANNOT_BE_CLAIMED",
    "FABRICATED_CONTENT_KINDS",
    "SOURCED_CLAIM_KINDS",
    "ContentClaim",
    "refuse_unsourced_claims",
    "draft_website",
]

WS_SCHEMA_VERSION = 1
WS_MAX_TEXT = 2000
WS_MAX_ID = 120
WS_MAX_ASSETS = 200
WS_MAX_INTERACTIONS = 40
WS_MAX_ATTEMPTS = 5

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,119}$")

#: The technical checks a draft must actually run before it can be called complete. They are
#: named rather than enumerated as "all checks pass" so a report can say *which* one failed.
REQUIRED_CHECKS: tuple[str, ...] = (
    "document_loads",
    "no_console_error",
    "no_broken_link",
    "no_horizontal_overflow",
)

#: Interactions that must have a verdict when the brief asks for them. The vocabulary is small
#: on purpose: an interaction is either implemented and tested, implemented and untested (which
#: is not done), or explicitly unsupported.
INTERACTION_STATES: tuple[str, ...] = ("supported", "supported_untested", "unsupported")

#: Claims a website draft can never make, whatever it was asked for. Named so "the draft is
#: complete" cannot be heard as any of them.
CANNOT_BE_CLAIMED: tuple[str, ...] = (
    "deployment",
    "dns_change",
    "production_credentials",
    "revenue_or_conversion_outcome",
    "copyright_clearance",
    "security_sign_off",
    "owner_acceptance",
    "publish_permission",
)

#: A provenance basis that lets a caller assert rights it does not have. ``unknown`` is
#: deliberately the only honest default and it blocks delivery rather than passing quietly.
PROVENANCE_BASES: tuple[str, ...] = (
    "authored_in_run",
    "owner_supplied",
    "licensed",
    "public_domain",
    "unknown",
)


class WebsiteDraftError(ValueError):
    """A website draft was malformed, out of scope, or asked to claim something it cannot."""


class ScopeRefused(WebsiteDraftError):
    """The audience or website scope was not stated before work started."""


class ContentRefused(WebsiteDraftError):
    """The draft contains invented content that would be a false public statement."""


class ProvenanceRefused(WebsiteDraftError):
    """An asset does not say where it came from, or claims rights it cannot show."""


class InteractionRefused(WebsiteDraftError):
    """A requested interaction has no honest verdict."""


class DeploymentRefused(WebsiteDraftError):
    """Something tried to record a deployment, DNS change or production credential use."""


class RepairExhausted(WebsiteDraftError):
    """The bounded repair loop ran out of attempts before the draft passed."""


def _ws_text(value: object, field_name: str, limit: int = WS_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise WebsiteDraftError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise WebsiteDraftError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise WebsiteDraftError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise WebsiteDraftError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _ws_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value.strip()):
        raise WebsiteDraftError(
            f"{field_name} must be a stable identifier (letters, digits, space, . _ : / -)."
        )
    return value.strip()


def _ws_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise WebsiteDraftError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _ws_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WebsiteDraftError(f"{field_name} must be an object.")
    for key in value:
        if not isinstance(key, str):
            raise WebsiteDraftError(f"{field_name} keys must be strings.")
    return {str(k): v for k, v in value.items()}


@dataclass(frozen=True, slots=True)
class DraftAudience:
    """Who the site is for. A site with no stated audience is a site nobody reviewed."""

    description: str
    language: str = "en"

    def __post_init__(self) -> None:
        object.__setattr__(self, "description", _ws_text(self.description, "audience", 400))
        language = _ws_text(self.language, "language", 32)
        if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language):
            raise WebsiteDraftError("language must be a BCP-47 style tag such as 'en' or 'pt-BR'.")
        object.__setattr__(self, "language", language)


@dataclass(frozen=True, slots=True)
class DraftScope:
    """What is being built, for whom, and what is explicitly not being built.

    ``out_of_scope`` is required to be non-empty. A brief that names nothing it excludes has
    not been narrowed, and an unnarrowed brief is how a draft turns into a different product
    halfway through.
    """

    site_name: str
    purpose: str
    audience: DraftAudience
    pages: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    no_layout_benchmark: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "site_name", _ws_text(self.site_name, "site_name", 200))
        object.__setattr__(self, "purpose", _ws_text(self.purpose, "purpose", 1000))
        if not isinstance(self.audience, DraftAudience):
            raise ScopeRefused("audience must be a DraftAudience: a site is for someone named.")
        pages = _ws_ids(self.pages, "pages")
        if not pages:
            raise ScopeRefused("pages must name at least one page; a site with no pages is a sketch.")
        object.__setattr__(self, "pages", pages)
        excluded = _ws_texts(self.out_of_scope, "out_of_scope")
        if not excluded:
            raise ScopeRefused(
                "out_of_scope must name at least one thing this draft is not: an unbounded brief "
                "cannot be finished or reviewed."
            )
        object.__setattr__(self, "out_of_scope", excluded)
        if not isinstance(self.no_layout_benchmark, bool):
            raise ScopeRefused("no_layout_benchmark must be a boolean.")


def _ws_ids(values: Iterable[object], field_name: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values or ():
        text = _ws_id(raw, field_name)
        if text not in out:
            out.append(text)
    return tuple(out)


def _ws_texts(values: Iterable[object], field_name: str) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values or ():
        text = _ws_text(raw, field_name, 400)
        if text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class AssetProvenance:
    """One asset, where it came from, and what may be done with it.

    An asset with ``basis="unknown"`` is recorded but blocks delivery: it can exist in a draft
    while the rights question is open, and cannot be part of a draft reported as complete.
    """

    path: str
    role: str
    basis: str
    source: str = ""
    license_id: str = ""
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _ws_id(self.path, "asset path"))
        object.__setattr__(self, "role", _ws_text(self.role, "asset role", 200))
        basis = _ws_text(self.basis, "asset basis", 40)
        if basis not in PROVENANCE_BASES:
            raise ProvenanceRefused(
                f"asset basis {basis!r} is unknown; expected one of {list(PROVENANCE_BASES)}"
            )
        object.__setattr__(self, "basis", basis)
        source = str(self.source or "").strip()
        license_id = str(self.license_id or "").strip()
        if basis != "authored_in_run":
            if not source:
                raise ProvenanceRefused(
                    f"asset {self.path!r} is {basis} but names no source; where it came from is "
                    "part of the record, not a detail to fill in later."
                )
        if basis == "unknown" and not license_id:
            # An asset whose rights are unknown is a question, and a question needs an owner and
            # a next step. Recording it without saying who has to answer it would make the
            # blocking state permanent and anonymous.
            raise ProvenanceRefused(
                f"asset {self.path!r} has unknown rights; record who must resolve them in "
                "license_id (for example 'pending:owner') so the block has an owner."
            )
        if basis == "licensed" and not license_id:
            raise ProvenanceRefused(
                f"asset {self.path!r} claims a licence but names none; 'licensed' without the "
                "licence is not a rights statement."
            )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "license_id", license_id)
        if self.sha256:
            object.__setattr__(self, "sha256", _ws_digest(self.sha256, "asset sha256"))

    def blocks_delivery(self) -> bool:
        return self.basis == "unknown"


@dataclass(frozen=True, slots=True)
class InteractionVerdict:
    """One requested interaction and what actually happened to it.

    ``supported_untested`` exists so a run cannot turn "I wrote the handler" into "it works".
    It is not a pass, and :meth:`WebsiteDraft.complete` treats it as unfinished.
    """

    name: str
    state: str
    evidence: tuple[str, ...] = ()
    limitation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _ws_text(self.name, "interaction name", 200))
        state = _ws_text(self.state, "interaction state", 40)
        if state not in INTERACTION_STATES:
            raise InteractionRefused(
                f"interaction state {state!r} is unknown; expected one of {list(INTERACTION_STATES)}"
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "evidence", _ws_texts(self.evidence, "interaction evidence"))
        limitation = str(self.limitation or "").strip()
        if state in {"supported_untested", "unsupported"} and not limitation:
            raise InteractionRefused(
                f"interaction {self.name!r} is {state} and must say so in words; an unexplained "
                "gap is the fake-interaction failure this record exists to prevent."
            )
        if state == "supported" and not self.evidence:
            raise InteractionRefused(
                f"interaction {self.name!r} claims to work but names no evidence; 'supported' "
                "means something exercised it."
            )
        object.__setattr__(self, "limitation", limitation)

    def is_working(self) -> bool:
        return self.state == "supported"


@dataclass(frozen=True, slots=True)
class DraftCheck:
    """One technical check that ran, and its outcome."""

    name: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _ws_text(self.name, "check name", 120))
        if not isinstance(self.passed, bool):
            raise WebsiteDraftError("check passed must be a boolean; a 'mostly passed' is not one.")
        object.__setattr__(self, "detail", _ws_text(self.detail, "check detail") if self.detail else "")


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One turn of the bounded repair loop."""

    attempt: int
    failures: tuple[str, ...]
    changed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise WebsiteDraftError("repair attempt must be a positive integer.")
        object.__setattr__(self, "failures", _ws_texts(self.failures, "repair failures"))


@dataclass(frozen=True, slots=True)
class DeploymentGrant:
    """Deployment is a separate grant, and this record can only ever hold ``False``.

    There is no constructor argument and no mapping that makes it true. A draft workflow has no
    code path that deploys, so the honest representation of that is a constant, not a flag a
    caller could flip.
    """

    deployment: bool = field(default=False, init=False)
    dns_change: bool = field(default=False, init=False)
    production_credentials: bool = field(default=False, init=False)

    @property
    def granted(self) -> bool:
        return self.deployment or self.dns_change or self.production_credentials

    def refuse(self, action: str) -> None:
        raise DeploymentRefused(
            f"{action!r} is not part of a website draft: deployment, DNS and production "
            "credentials are separate grants (AP-021 scope), and nothing here can provide them."
        )


@dataclass(frozen=True, slots=True)
class WebsiteDraft:
    """A complete draft, or an honest partial one.

    ``status`` is derived, never assigned: it is ``complete`` only when every required check
    passed, every requested interaction is ``supported``, and no asset is still ``unknown``.
    Anything else is ``partial`` and names why.
    """

    scope: DraftScope
    artifact_sha256: str
    checks: tuple[DraftCheck, ...]
    interactions: tuple[InteractionVerdict, ...] = ()
    assets: tuple[AssetProvenance, ...] = ()
    repairs: tuple[RepairAttempt, ...] = ()
    attempt_budget: int = 2
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, DraftScope):
            raise ScopeRefused("a draft needs a DraftScope; the audience is not optional.")
        object.__setattr__(self, "artifact_sha256", _ws_digest(self.artifact_sha256, "artifact_sha256"))
        if not isinstance(self.attempt_budget, int) or isinstance(self.attempt_budget, bool):
            raise WebsiteDraftError("attempt_budget must be an integer.")
        if not 1 <= self.attempt_budget <= WS_MAX_ATTEMPTS:
            raise WebsiteDraftError(
                f"attempt_budget must be between 1 and {WS_MAX_ATTEMPTS}; an unbounded repair "
                "loop is how a run spends without deciding."
            )
        if len(self.repairs) > self.attempt_budget:
            raise RepairExhausted(
                f"{len(self.repairs)} repair attempts exceed the stated budget of "
                f"{self.attempt_budget}"
            )
        for index, attempt in enumerate(self.repairs, start=1):
            if attempt.attempt != index:
                raise WebsiteDraftError("repair attempts must be numbered consecutively from 1.")
        if len(self.assets) > WS_MAX_ASSETS:
            raise ProvenanceRefused(f"a draft may carry at most {WS_MAX_ASSETS} assets.")
        if len(self.interactions) > WS_MAX_INTERACTIONS:
            raise InteractionRefused(
                f"a draft may carry at most {WS_MAX_INTERACTIONS} interaction verdicts."
            )
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise WebsiteDraftError("each check may appear once; a duplicate would double-count.")
        for name in REQUIRED_CHECKS:
            if name not in names:
                raise WebsiteDraftError(
                    f"required check {name!r} did not run; a draft cannot omit a check and still "
                    "report on it."
                )
        if self.run_id:
            object.__setattr__(self, "run_id", _ws_id(self.run_id, "run_id"))

    # -- derived state ---------------------------------------------------

    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def unfinished_interactions(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.interactions if not item.is_working())

    def unprovenanced_assets(self) -> tuple[str, ...]:
        return tuple(asset.path for asset in self.assets if asset.blocks_delivery())

    def status(self) -> str:
        if self.failed_checks() or self.unfinished_interactions() or self.unprovenanced_assets():
            return "partial"
        return "complete"

    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        for name in self.failed_checks():
            reasons.append(f"check failed: {name}")
        for name in self.unfinished_interactions():
            reasons.append(f"interaction not working: {name}")
        for path in self.unprovenanced_assets():
            reasons.append(f"asset rights unknown: {path}")
        return tuple(reasons)

    def complete(self) -> bool:
        return self.status() == "complete"

    # -- reporting -------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": WS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status(),
            "reasons": list(self.reasons()),
            "scope": {
                "site_name": self.scope.site_name,
                "purpose": self.scope.purpose,
                "audience": self.scope.audience.description,
                "language": self.scope.audience.language,
                "pages": list(self.scope.pages),
                "out_of_scope": list(self.scope.out_of_scope),
                "no_layout_benchmark": self.scope.no_layout_benchmark,
            },
            "artifact_sha256": self.artifact_sha256,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
            "interactions": [
                {
                    "name": i.name,
                    "state": i.state,
                    "evidence": list(i.evidence),
                    "limitation": i.limitation,
                }
                for i in self.interactions
            ],
            "assets": [
                {
                    "path": a.path,
                    "role": a.role,
                    "basis": a.basis,
                    "source": a.source,
                    "license_id": a.license_id,
                    "sha256": a.sha256,
                    "blocks_delivery": a.blocks_delivery(),
                }
                for a in self.assets
            ],
            "repairs": [
                {"attempt": r.attempt, "failures": list(r.failures), "changed": list(r.changed)}
                for r in self.repairs
            ],
            "attempt_budget": self.attempt_budget,
            # Restated on every report so no reader has to infer it from absence.
            "deployment": False,
            "dns_change": False,
            "production_credentials": False,
            "publish_permission": False,
            "cannot_be_claimed": list(CANNOT_BE_CLAIMED),
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class DraftExecutor(Protocol):
    """The execution this module refuses to perform itself.

    A real implementation writes files and drives a browser. The module never does either, so
    the same decision logic runs against a recording double in tests and CI.
    """

    def run(self, scope: DraftScope, attempt: int, previous_failures: Sequence[str]) -> Mapping[str, Any]:
        """Produce one attempt and return the raw facts: digest, checks, interactions, assets."""
        ...


def draft_website(
    *,
    scope: DraftScope,
    executor: DraftExecutor,
    requested_interactions: Sequence[str] = (),
    attempt_budget: int = 2,
    run_id: str = "",
) -> WebsiteDraft:
    """Run a bounded repair loop and return the draft it actually produced.

    The loop stops for one of two reasons and both are reported: every required check passed,
    or the budget ran out. A budget that runs out yields a ``partial`` draft that names the
    failures — never a completed one, and never an exception that hides how far it got. An
    implementation that raised on exhaustion would let a caller catch it and report success.

    ``requested_interactions`` are the brief's asks. Each must come back with a verdict from the
    executor; a requested interaction the executor never answers is refused rather than
    silently dropped, because a missing verdict is indistinguishable from a missing feature.
    """
    if not isinstance(scope, DraftScope):
        raise ScopeRefused("draft_website needs a DraftScope.")
    if not isinstance(attempt_budget, int) or isinstance(attempt_budget, bool):
        raise WebsiteDraftError("attempt_budget must be an integer.")
    if not 1 <= attempt_budget <= WS_MAX_ATTEMPTS:
        raise WebsiteDraftError(
            f"attempt_budget must be between 1 and {WS_MAX_ATTEMPTS}."
        )
    wanted = _ws_texts(requested_interactions, "requested interaction")

    repairs: list[RepairAttempt] = []
    failures: tuple[str, ...] = ()
    last: Mapping[str, Any] | None = None
    for attempt in range(1, attempt_budget + 1):
        raw = executor.run(scope, attempt, failures)
        if not isinstance(raw, Mapping):
            raise WebsiteDraftError("the executor must return a mapping of facts.")
        facts = _ws_mapping(raw, "executor result")
        checks = _parse_checks(facts.get("checks"))
        interactions = _parse_interactions(facts.get("interactions"))
        assets = _parse_assets(facts.get("assets"))
        seen = {item.name for item in interactions}
        missing = [name for name in wanted if name not in seen]
        if missing:
            raise InteractionRefused(
                "the brief asked for interactions this attempt gave no verdict on: "
                + ", ".join(sorted(missing))
            )
        draft = WebsiteDraft(
            scope=scope,
            # The constructors validate these; the cast is for the type checker, which cannot
            # see through the mapping. A wrong value is refused, not coerced.
            artifact_sha256=cast(str, facts.get("artifact_sha256")),
            checks=checks,
            interactions=interactions,
            assets=assets,
            repairs=tuple(repairs),
            attempt_budget=attempt_budget,
            run_id=str(facts.get("run_id") or run_id or ""),
        )
        last = facts
        if draft.complete():
            return draft
        # Record the attempt, then decide whether there is budget left to try again. The final
        # attempt is a legitimate member of the repair history, not a stray extra one.
        repairs.append(
            RepairAttempt(
                attempt=attempt,
                failures=draft.reasons(),
                changed=tuple(str(c) for c in (facts.get("changed") or ())),
            )
        )
        failures = draft.reasons()
        if attempt == attempt_budget:
            return replace(draft, repairs=tuple(repairs))
    assert last is not None  # the loop always runs at least once
    raise WebsiteDraftError("the repair loop produced no attempt; this is a bug, not a refusal.")


def _parse_checks(raw: object) -> tuple[DraftCheck, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise WebsiteDraftError("executor checks must be a sequence.")
    out: list[DraftCheck] = []
    for item in raw:
        row = _ws_mapping(item, "check")
        out.append(
            DraftCheck(
                name=cast(str, row.get("name")),
                passed=cast(bool, row.get("passed")),
                detail=str(row.get("detail") or ""),
            )
        )
    return tuple(out)


def _parse_interactions(raw: object) -> tuple[InteractionVerdict, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise InteractionRefused("executor interactions must be a sequence.")
    out: list[InteractionVerdict] = []
    for item in raw:
        row = _ws_mapping(item, "interaction")
        out.append(
            InteractionVerdict(
                name=cast(str, row.get("name")),
                state=cast(str, row.get("state")),
                evidence=tuple(row.get("evidence") or ()),
                limitation=str(row.get("limitation") or ""),
            )
        )
    return tuple(out)


def _parse_assets(raw: object) -> tuple[AssetProvenance, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ProvenanceRefused("executor assets must be a sequence.")
    out: list[AssetProvenance] = []
    for item in raw:
        row = _ws_mapping(item, "asset")
        out.append(
            AssetProvenance(
                path=cast(str, row.get("path")),
                role=cast(str, row.get("role")),
                basis=cast(str, row.get("basis")),
                source=str(row.get("source") or ""),
                license_id=str(row.get("license_id") or ""),
                sha256=str(row.get("sha256") or ""),
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------------------
# AC2, second half: claims a draft may not make, and the bounded repair loop's clock
# --------------------------------------------------------------------------------------

#: The kinds of invented content that turn a draft into a false public statement. Each is
#: refused at record time, not flagged for review: a testimonial from a fabricated customer is
#: not a quality problem to iterate on, it is content that must not exist.
FABRICATED_CONTENT_KINDS: tuple[str, ...] = (
    "testimonial",
    "customer_logo",
    "integration_badge",
    "certification",
    "security_claim",
    "award",
    "case_study",
)

#: Claim kinds that require the site to name the real source the owner actually holds. A
#: ``testimonial`` may exist in a draft, but only when it points at a supplied source: the rule
#: is "no unsourced claims", not "no claims", because refusing all social proof would be its own
#: inaccuracy.
#:
#: ``integration_badge`` belongs here for the same reason. "Works with Stripe" is a factual claim
#: about a connection, and a badge for a service nobody connected is the same lie as a
#: testimonial from a customer who does not exist -- it is just aimed at a different audience.
SOURCED_CLAIM_KINDS: tuple[str, ...] = (
    "testimonial",
    "case_study",
    "customer_logo",
    "integration_badge",
)


@dataclass(frozen=True, slots=True)
class ContentClaim:
    """One piece of persuasive content and where the evidence for it lives.

    A claim is admissible only when ``source_ref`` names something the owner actually supplied
    (an asset path or an owner statement id). ``fabricated`` exists so a caller can be *honest*
    about a placeholder, and an honest placeholder is still refused for a complete draft — the
    point is that a run cannot quietly dress a placeholder as real.
    """

    kind: str
    text: str
    source_ref: str = ""
    fabricated: bool = False

    def __post_init__(self) -> None:
        kind = _ws_text(self.kind, "claim kind", 60)
        if kind not in FABRICATED_CONTENT_KINDS:
            raise ContentRefused(
                f"claim kind {kind!r} is unknown; expected one of {list(FABRICATED_CONTENT_KINDS)}"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", _ws_text(self.text, "claim text", 600))
        if not isinstance(self.fabricated, bool):
            raise ContentRefused("fabricated must be a boolean.")
        source = str(self.source_ref or "").strip()
        if kind in SOURCED_CLAIM_KINDS and not self.fabricated and not source:
            raise ContentRefused(
                f"{kind} content {self.text[:60]!r} names no source. A testimonial, logo or case "
                "study without a source the owner holds is an invented public statement."
            )
        object.__setattr__(self, "source_ref", source)

    def admissible(self) -> bool:
        """Whether this claim may appear in a draft reported as complete."""
        if self.fabricated:
            return False
        if self.kind in SOURCED_CLAIM_KINDS and not self.source_ref:
            return False
        return True


def refuse_unsourced_claims(claims: Iterable[ContentClaim]) -> tuple[ContentClaim, ...]:
    """Validate a batch of claims, raising on the ones that are not even recordable.

    ``fabricated=True`` is recordable (a placeholder is a fact about the draft) but never
    admissible. A sourced-kind claim with no source is not recordable at all: it is the exact
    shape of an invented testimonial, and the difference between it and a real one is the
    source, which the caller either has or does not.
    """
    out: list[ContentClaim] = []
    for claim in claims:
        if not isinstance(claim, ContentClaim):
            raise ContentRefused("claims must be ContentClaim records.")
        out.append(claim)
    return tuple(out)
