"""AP-026 — research model, paper and tool improvements, and evaluate candidates.

An "improvement lab" is where a system starts changing itself, so it is the story where the quiet
failures are the expensive ones. Four of them, each already common:

* **A watcher nobody granted.** A background job polling a repository every hour looks like
  diligence and is an outbound request the owner never approved. There is no implicit schedule here:
  ``WatchSchedule`` cannot exist without a :class:`WatchGrant`, and the grant is a namespaced human
  decision with an allowlisted host set, a request cap and an expiry.
* **A candidate described by its name.** "Tool X, latest" is not a version. A :class:`CandidateCard`
  requires the exact version, the date it was observed, the licence, non-empty limitations and —
  named explicitly — whether it needs a hosted model, because that decides whether it may be trialled
  at all.
* **An experiment with no problem behind it.** "Let's try a better prompt" is not a hypothesis.
  A :class:`Hypothesis` must come from named failure traces, carry a measured baseline, and state a
  threshold it could fail. An unfalsifiable hypothesis is refused.
* **An optimiser that edits its own exam.** Held-out tasks, evaluators and authority controls are
  fingerprinted before a trial and compared after. Touching any of them raises, because an
  optimisation that improves the score by editing the scorer has not improved anything.

Two further lines are drawn deliberately. Optimisation surfaces are ``prompt``, ``skill`` and
``config``: memory-shaped updates. Weight training is refused by name — this programme has no
training path, and "fine-tune the model" is not an available move to quietly acquire. And candidate
generation, judging and optimisation inference stay local: a reference project that only works by
calling a hosted model may be read and recorded, labelled ``reference_only``, and never trialled.

Nothing here reads a clock or opens a socket. Fetching is injected through :class:`Fetcher` and runs
only through a granted schedule, which is what makes "no implicit outbound requests" checkable
rather than aspirational.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from autonomous_production_research import PUBLIC_READ_ALLOWLIST

__all__ = [
    "ImprovementLabError",
    "EgressRefused",
    "CandidateRefused",
    "HypothesisRefused",
    "CompatibilityRefused",
    "HeldOutRefused",
    "TrainingRefused",
    "LocalOnlyRefused",
    "WatchGrant",
    "WatchSchedule",
    "Watcher",
    "CandidateCard",
    "CANDIDATE_KINDS",
    "ADAPTATION_STATES",
    "Hypothesis",
    "CompatibilityCheck",
    "HeldOutGuard",
    "OPTIMISATION_SURFACES",
    "REFUSED_SURFACES",
    "Experiment",
    "TrialResult",
    "ImprovementReport",
    "run_experiment",
]

IL_SCHEMA_VERSION = 1
IL_MAX_TEXT = 2000

#: What a candidate may be. ``prompt``/``skill``/``config`` are the surfaces this programme may
#: change; ``model``, ``paper`` and ``repo`` are things it may *read about*.
CANDIDATE_KINDS: tuple[str, ...] = ("model", "paper", "repo", "prompt", "skill", "config")

#: Surfaces an experiment may optimise. Memory-shaped updates only.
OPTIMISATION_SURFACES: tuple[str, ...] = ("prompt", "skill", "config")

#: Surfaces that are refused by name rather than being absent from the enum, so the refusal can say
#: what was asked for. Weight training is the one people ask about.
REFUSED_SURFACES: tuple[str, ...] = ("weights", "fine_tune", "rl_rollout")

#: How a reference project's hosted-model dependency is handled. ``local_adaptation`` means someone
#: replaced the hosted call with a local one and recorded how; ``reference_only`` means it was read
#: and may not be trialled.
ADAPTATION_STATES: tuple[str, ...] = ("local_native", "local_adaptation", "reference_only")

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,119}$")
_HOSTED_MARKERS: tuple[str, ...] = ("openai", "anthropic", "gemini", "hosted", "api key", "saas")


class ImprovementLabError(ValueError):
    """The improvement lab record is malformed or claims something it cannot support."""


class EgressRefused(ImprovementLabError):
    """Something tried to make an outbound request without an explicit grant."""


class CandidateRefused(ImprovementLabError):
    """A candidate is described too vaguely, or cites a source outside the allowlist."""


class HypothesisRefused(ImprovementLabError):
    """An experiment has no diagnosed problem, no baseline, or no threshold it could fail."""


class CompatibilityRefused(ImprovementLabError):
    """A trial started before runtime compatibility and resource limits were checked."""


class HeldOutRefused(ImprovementLabError):
    """The optimiser touched the held-out set, the evaluator or the authority controls."""


class TrainingRefused(ImprovementLabError):
    """Something asked to train weights. This programme has no training path."""


class LocalOnlyRefused(ImprovementLabError):
    """A trial would have depended on a hosted model."""


def _il_text(value: object, field_name: str, limit: int = IL_MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ImprovementLabError(f"{field_name} must be a string.")
    if "\x00" in value:
        raise ImprovementLabError(f"{field_name} must not contain NUL characters.")
    stripped = value.strip()
    if not stripped:
        raise ImprovementLabError(f"{field_name} must not be empty.")
    if len(stripped) > limit:
        raise ImprovementLabError(f"{field_name} must be at most {limit} characters.")
    return stripped


def _il_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.strip().lower()):
        raise ImprovementLabError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return value.strip().lower()


def _il_date(value: object, field_name: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise ImprovementLabError(f"{field_name} must be an ISO date (YYYY-MM-DD).")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ImprovementLabError(f"{field_name} must be a real calendar date.") from exc


def _il_host(url: str) -> str:
    match = re.match(r"^https://([^/?#]+)", url)
    if not match:
        raise CandidateRefused(f"source {url!r} must be an https URL to an allowlisted host.")
    return match.group(1).lower()


# ---------------------------------------------------------------------------
# AC1 — no implicit or background egress; a schedule needs an explicit grant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchGrant:
    """An explicit, bounded permission to fetch public sources on a schedule.

    ``granted_by`` must be a namespaced human, exactly as every other decision in this programme is;
    ``hosts`` must be a subset of AP-009's public allowlist, so a watch job cannot become a general
    network capability; ``expires_on`` is required, because a watch that has no end is a habit.
    """

    granted_by: str
    purpose: str
    hosts: tuple[str, ...]
    max_requests: int
    expires_on: str

    def __post_init__(self) -> None:
        who = _il_text(self.granted_by, "granted_by", 200)
        prefix, _, name = who.partition(":")
        if prefix.strip().lower() not in {"owner", "user", "reviewer"} or not name.strip():
            raise EgressRefused(
                f"watch grant by {who!r} is not a named person (owner:, user: or reviewer:); a "
                "schedule is a decision and a decision has an author."
            )
        object.__setattr__(self, "granted_by", who)
        object.__setattr__(self, "purpose", _il_text(self.purpose, "purpose", 600))
        hosts: list[str] = []
        for raw in self.hosts:
            host = _il_text(raw, "host", 253).lower()
            if host not in PUBLIC_READ_ALLOWLIST:
                raise EgressRefused(
                    f"host {host!r} is not on the public read allowlist "
                    f"{sorted(PUBLIC_READ_ALLOWLIST)}; a watch grant cannot widen egress."
                )
            if host not in hosts:
                hosts.append(host)
        if not hosts:
            raise EgressRefused("a watch grant must name at least one allowlisted host.")
        object.__setattr__(self, "hosts", tuple(hosts))
        if not isinstance(self.max_requests, int) or isinstance(self.max_requests, bool) or self.max_requests < 1:
            raise EgressRefused("max_requests must be a positive integer.")
        _il_date(self.expires_on, "expires_on")
        object.__setattr__(self, "expires_on", self.expires_on.strip())

    def permits(self, url: str, *, on: date) -> bool:
        return (
            _il_host(url) in self.hosts
            and _il_date(self.expires_on, "expires_on") >= on
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "granted_by": self.granted_by,
            "purpose": self.purpose,
            "hosts": list(self.hosts),
            "max_requests": self.max_requests,
            "expires_on": self.expires_on,
        }


class Fetcher(Protocol):
    """The network hop this module refuses to perform itself."""

    def get(self, url: str) -> Mapping[str, Any]:
        """Return at least ``{"status": int, "body": str}`` for an allowlisted URL."""
        ...

    def list_dir(self, url: str) -> Sequence[Mapping[str, Any]]:
        """Return candidate entries for one watched location."""
        ...


@dataclass
class WatchSchedule:
    """A schedule that exists only because a grant does.

    There is no constructor path without a ``WatchGrant``, and :meth:`tick` performs **no** request
    unless it is given an explicit ``now`` date, an explicit fetch list and a fetcher. Nothing in
    this class runs at import, on construction, or on a background thread — a watcher that starts
    itself is the failure the story names.
    """

    grant: WatchGrant
    locations: tuple[str, ...]
    requests_made: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.grant, WatchGrant):
            raise EgressRefused(
                "a watch schedule needs a WatchGrant; an ungoverned poller is an outbound request "
                "nobody approved."
            )
        locations: list[str] = []
        for raw in self.locations:
            url = _il_text(raw, "location", 1000)
            if _il_host(url) not in self.grant.hosts:
                raise EgressRefused(
                    f"{url} is outside the granted hosts {list(self.grant.hosts)}; the grant names "
                    "where this may look, not merely that it may look."
                )
            locations.append(url)
        if not locations:
            raise EgressRefused("a watch schedule must name at least one location.")
        self.locations = tuple(locations)

    def tick(self, *, now: str, fetcher: Fetcher) -> tuple[Mapping[str, Any], ...]:
        """Perform one explicitly requested polling round, never more than the grant allows."""
        current = _il_date(now, "now")
        if current > _il_date(self.grant.expires_on, "expires_on"):
            raise EgressRefused(
                f"the watch grant expired on {self.grant.expires_on}; renewing it is a decision, not "
                "a default."
            )
        if self.requests_made + len(self.locations) > self.grant.max_requests:
            raise EgressRefused(
                f"this round would make {self.requests_made + len(self.locations)} requests against a "
                f"grant of {self.grant.max_requests}; the cap is the cap."
            )
        results: list[Mapping[str, Any]] = []
        for url in self.locations:
            if not self.grant.permits(url, on=current):
                continue
            response = fetcher.list_dir(url)
            self.requests_made += 1
            self.log.append({"url": url, "at": now, "count": len(response)})
            results.extend(response)
        return tuple(results)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "grant": self.grant.to_mapping(),
            "locations": list(self.locations),
            "requests_made": self.requests_made,
            "log": list(self.log),
        }


@dataclass
class Watcher:
    """A holder for schedules that refuses to start itself.

    The point of this class is the negative: constructing it makes no request, and there is no
    ``daemon``/``start``/``loop`` helper. A caller has to say ``tick(now=..., fetcher=...)``.
    """

    schedules: tuple[WatchSchedule, ...] = ()

    def __post_init__(self) -> None:
        for schedule in self.schedules:
            if not isinstance(schedule, WatchSchedule):
                raise EgressRefused("schedules must be WatchSchedule records.")

    def tick_all(self, *, now: str, fetcher: Fetcher) -> tuple[Mapping[str, Any], ...]:
        out: list[Mapping[str, Any]] = []
        for schedule in self.schedules:
            out.extend(schedule.tick(now=now, fetcher=fetcher))
        return tuple(out)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schedules": [schedule.to_mapping() for schedule in self.schedules],
            "starts_itself": False,
            "background_threads": 0,
        }


# ---------------------------------------------------------------------------
# AC2 — candidates carry dates, exact versions, licences and limitations.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateCard:
    """One candidate improvement, described precisely enough to be checked before it is tried.

    ``version`` is the exact version or commit — not "latest". ``accessed_on`` is required because a
    candidate record without a date cannot be re-checked later. ``hosted_dependency`` and
    ``adaptation`` together decide trial eligibility (AC7): a ``reference_only`` candidate may be
    read and may never be trialled.
    """

    name: str
    kind: str
    version: str
    source_url: str
    accessed_on: str
    license_id: str
    limitations: tuple[str, ...]
    adaptation: str = "local_native"
    hosted_dependency: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _il_text(self.name, "name", 120))
        kind = _il_text(self.kind, "kind", 40)
        if kind not in CANDIDATE_KINDS:
            raise CandidateRefused(f"kind {kind!r} is unknown; expected one of {list(CANDIDATE_KINDS)}.")
        object.__setattr__(self, "kind", kind)
        version = _il_text(self.version, "version", 120)
        if version.lower() in {"latest", "main", "master", "head", "newest", "*"}:
            raise CandidateRefused(
                f"version {version!r} is a moving target; name the release, commit or paper revision "
                "so the record can be re-checked."
            )
        object.__setattr__(self, "version", version)
        url = _il_text(self.source_url, "source_url", 1000)
        if _il_host(url) not in PUBLIC_READ_ALLOWLIST:
            raise CandidateRefused(
                f"source {url!r} is not on the public read allowlist; a candidate has to be somewhere "
                "this programme is already permitted to read."
            )
        object.__setattr__(self, "source_url", url)
        _il_date(self.accessed_on, "accessed_on")
        object.__setattr__(self, "accessed_on", self.accessed_on.strip())
        object.__setattr__(self, "license_id", _il_text(self.license_id, "license_id", 120))
        limitations: list[str] = []
        for raw in self.limitations:
            text = _il_text(raw, "limitation", 600)
            if text not in limitations:
                limitations.append(text)
        if not limitations:
            raise CandidateRefused(
                f"candidate {self.name!r} states no limitations; a candidate with no stated limits is "
                "being recommended rather than evaluated."
            )
        object.__setattr__(self, "limitations", tuple(limitations))
        adaptation = _il_text(self.adaptation, "adaptation", 40)
        if adaptation not in ADAPTATION_STATES:
            raise CandidateRefused(
                f"adaptation {adaptation!r} is unknown; expected one of {list(ADAPTATION_STATES)}."
            )
        object.__setattr__(self, "adaptation", adaptation)
        hosted = str(self.hosted_dependency or "").strip()
        lowered = f"{hosted} {url} {self.summary}".lower()
        if not hosted and any(marker in lowered for marker in _HOSTED_MARKERS):
            raise CandidateRefused(
                f"candidate {self.name!r} looks hosted but names no hosted_dependency; whether a "
                "trial would leave this machine is not a detail to infer."
            )
        if adaptation == "local_adaptation" and not self.summary.strip():
            raise CandidateRefused(
                "a local adaptation must say what was replaced; 'we adapted it' is not a record."
            )
        object.__setattr__(self, "hosted_dependency", hosted)
        object.__setattr__(self, "summary", str(self.summary or "").strip()[:600])

    @property
    def is_triable_locally(self) -> bool:
        """Whether candidate generation, judging and inference can stay on this machine."""
        return self.adaptation != "reference_only" and not self.hosted_dependency

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "version": self.version,
            "source_url": self.source_url,
            "accessed_on": self.accessed_on,
            "license_id": self.license_id,
            "limitations": list(self.limitations),
            "adaptation": self.adaptation,
            "hosted_dependency": self.hosted_dependency,
            "summary": self.summary,
            "triable_locally": self.is_triable_locally,
        }


# ---------------------------------------------------------------------------
# AC3 — experiments start from a diagnosed problem and a measurable hypothesis.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A falsifiable statement about a measured problem, with the trace ids that show it.

    ``threshold`` is the part people skip. A hypothesis with no threshold cannot fail, and one that
    cannot fail is a description of intent. ``direction`` says which way the metric should move so a
    regression cannot be read as an improvement.
    """

    statement: str
    problem_trace_ids: tuple[str, ...]
    metric: str
    baseline: float
    threshold: float
    direction: str = "increase"

    def __post_init__(self) -> None:
        object.__setattr__(self, "statement", _il_text(self.statement, "statement", 800))
        traces: list[str] = []
        for raw in self.problem_trace_ids:
            text = _il_text(raw, "problem trace id", 120)
            if not _ID_RE.fullmatch(text):
                raise HypothesisRefused(f"trace id {text!r} is not an identifier.")
            if text not in traces:
                traces.append(text)
        if not traces:
            raise HypothesisRefused(
                f"hypothesis {self.statement[:60]!r} names no failing trace; an improvement with no "
                "diagnosed problem is a preference."
            )
        object.__setattr__(self, "problem_trace_ids", tuple(traces))
        object.__setattr__(self, "metric", _il_text(self.metric, "metric", 200))
        for attr in ("baseline", "threshold"):
            value = getattr(self, attr)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise HypothesisRefused(f"{attr} must be a number; a baseline of 'low' is not a baseline.")
            object.__setattr__(self, attr, float(value))
        direction = _il_text(self.direction, "direction", 20)
        if direction not in {"increase", "decrease"}:
            raise HypothesisRefused(
                f"direction {direction!r} is unknown; expected 'increase' or 'decrease'."
            )
        object.__setattr__(self, "direction", direction)
        if direction == "increase" and self.threshold <= self.baseline:
            raise HypothesisRefused(
                f"threshold {self.threshold} does not exceed the baseline {self.baseline}; the "
                "experiment is unfalsifiable as written."
            )
        if direction == "decrease" and self.threshold >= self.baseline:
            raise HypothesisRefused(
                f"threshold {self.threshold} does not fall below the baseline {self.baseline}; the "
                "experiment is unfalsifiable as written."
            )

    def is_met(self, observed: float) -> bool:
        return observed >= self.threshold if self.direction == "increase" else observed <= self.threshold

    def to_mapping(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "problem_trace_ids": list(self.problem_trace_ids),
            "metric": self.metric,
            "baseline": self.baseline,
            "threshold": self.threshold,
            "direction": self.direction,
        }


# ---------------------------------------------------------------------------
# AC4 — compatibility and resource limits precede trials.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompatibilityCheck:
    """Runtime compatibility and the resource envelope, checked before a trial is allowed.

    Every field is required. ``declared_download_mb`` against ``download_cap_mb`` is the check that
    stops a "small" candidate from pulling tens of gigabytes onto a laptop, and ``fits`` is derived
    so a caller cannot assert it.
    """

    runtime: str
    runtime_installed: bool
    declared_download_mb: float
    download_cap_mb: float
    disk_free_mb: float
    memory_required_mb: float
    memory_available_mb: float
    license_compatible: bool
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime", _il_text(self.runtime, "runtime", 120))
        for attr in ("runtime_installed", "license_compatible"):
            if not isinstance(getattr(self, attr), bool):
                raise CompatibilityRefused(f"{attr} must be a boolean.")
        for attr in (
            "declared_download_mb",
            "download_cap_mb",
            "disk_free_mb",
            "memory_required_mb",
            "memory_available_mb",
        ):
            value = getattr(self, attr)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CompatibilityRefused(f"{attr} must be a number.")
            if float(value) < 0:
                raise CompatibilityRefused(f"{attr} must not be negative.")
            object.__setattr__(self, attr, float(value))
        object.__setattr__(self, "notes", str(self.notes or "").strip()[:600])

    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.runtime_installed:
            reasons.append(f"runtime {self.runtime!r} is not installed on this host")
        if not self.license_compatible:
            reasons.append("the candidate licence is not compatible with this distribution")
        if self.declared_download_mb > self.download_cap_mb:
            reasons.append(
                f"declared download {self.declared_download_mb} MB exceeds the "
                f"{self.download_cap_mb} MB cap"
            )
        if self.declared_download_mb > self.disk_free_mb:
            reasons.append(
                f"declared download {self.declared_download_mb} MB exceeds the "
                f"{self.disk_free_mb} MB free disk"
            )
        if self.memory_required_mb > self.memory_available_mb:
            reasons.append(
                f"requires {self.memory_required_mb} MB memory but only "
                f"{self.memory_available_mb} MB is available"
            )
        return tuple(reasons)

    @property
    def fits(self) -> bool:
        return not self.blocking_reasons()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "runtime_installed": self.runtime_installed,
            "declared_download_mb": self.declared_download_mb,
            "download_cap_mb": self.download_cap_mb,
            "disk_free_mb": self.disk_free_mb,
            "memory_required_mb": self.memory_required_mb,
            "memory_available_mb": self.memory_available_mb,
            "license_compatible": self.license_compatible,
            "fits": self.fits,
            "blocking_reasons": list(self.blocking_reasons()),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# AC5 — the optimiser cannot edit the exam.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldOutGuard:
    """Digests of the held-out tasks, the evaluators and the authority controls.

    Taken before an experiment and supplied again afterwards. :meth:`assert_untouched` raises on any
    difference, including a *deletion*: an evaluator that disappeared is not "unchanged".
    """

    held_out_tasks_sha256: str
    evaluator_sha256: str
    authority_controls_sha256: str
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for attr in ("held_out_tasks_sha256", "evaluator_sha256", "authority_controls_sha256"):
            object.__setattr__(self, attr, _il_digest(getattr(self, attr), attr))
        paths: list[str] = []
        for raw in self.paths:
            text = _il_text(raw, "guarded path", 400)
            if text not in paths:
                paths.append(text)
        object.__setattr__(self, "paths", tuple(paths))

    def assert_untouched(self, after: "HeldOutGuard") -> None:
        if not isinstance(after, HeldOutGuard):
            raise HeldOutRefused("the post-trial guard must be a HeldOutGuard record.")
        for attr, label in (
            ("held_out_tasks_sha256", "held-out tasks"),
            ("evaluator_sha256", "evaluator"),
            ("authority_controls_sha256", "authority controls"),
        ):
            before = getattr(self, attr)
            now = getattr(after, attr)
            if before != now:
                raise HeldOutRefused(
                    f"the {label} changed during the experiment ({before[:12]} -> {now[:12]}); an "
                    "optimisation that edits its own exam has improved nothing."
                )
        if set(self.paths) != set(after.paths):
            raise HeldOutRefused(
                "the guarded path set changed during the experiment; a removed control is not an "
                "unchanged control."
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "held_out_tasks_sha256": self.held_out_tasks_sha256,
            "evaluator_sha256": self.evaluator_sha256,
            "authority_controls_sha256": self.authority_controls_sha256,
            "paths": list(self.paths),
        }


# ---------------------------------------------------------------------------
# AC6/AC7 — memory-shaped updates only, and only local inference.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Experiment:
    """One bounded attempt to improve a measured problem, on an allowed surface.

    Constructing this refuses weight training (``surface`` outside ``OPTIMISATION_SURFACES``) and a
    trial whose candidate needs a hosted model, so neither can reach a runner.
    """

    hypothesis: Hypothesis
    surface: str
    candidate: CandidateCard
    compatibility: CompatibilityCheck
    guard: HeldOutGuard
    max_trials: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.hypothesis, Hypothesis):
            raise HypothesisRefused("an experiment needs a Hypothesis.")
        surface = _il_text(self.surface, "surface", 40)
        if surface in REFUSED_SURFACES:
            raise TrainingRefused(
                f"surface {surface!r} means changing the model's weights. This programme has no "
                "training path, no RL rollout, and no budget authority for either; memory, prompt, "
                "skill and config updates are the available moves."
            )
        if surface not in OPTIMISATION_SURFACES:
            raise TrainingRefused(
                f"surface {surface!r} is not optimisable; allowed surfaces are "
                f"{list(OPTIMISATION_SURFACES)}."
            )
        object.__setattr__(self, "surface", surface)
        if not isinstance(self.candidate, CandidateCard):
            raise CandidateRefused("an experiment needs a CandidateCard.")
        if self.surface in {"prompt", "skill", "config"} and self.candidate.kind not in {
            "prompt",
            "skill",
            "config",
            "repo",
            "paper",
        }:
            raise CandidateRefused(
                f"a {self.surface} experiment cannot be driven by a {self.candidate.kind} candidate."
            )
        if not isinstance(self.compatibility, CompatibilityCheck):
            raise CompatibilityRefused("an experiment needs a CompatibilityCheck before any trial runs.")
        if not isinstance(self.guard, HeldOutGuard):
            raise HeldOutRefused("an experiment needs a HeldOutGuard so the exam can be shown untouched.")
        if not isinstance(self.max_trials, int) or isinstance(self.max_trials, bool) or not 1 <= self.max_trials <= 5:
            raise ImprovementLabError("max_trials must be an integer between 1 and 5; the budget is bounded.")
        if self.surface == "skill" and self.candidate.kind not in {"skill", "repo", "paper"}:
            raise CandidateRefused("a skill experiment has to name a skill-shaped candidate.")


@dataclass(frozen=True, slots=True)
class TrialResult:
    """One trial's measured outcome, reported whether it worked or not."""

    trial: int
    observed: float
    passed: bool
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.trial, int) or isinstance(self.trial, bool) or self.trial < 1:
            raise ImprovementLabError("trial must be a positive integer.")
        if not isinstance(self.observed, (int, float)) or isinstance(self.observed, bool):
            raise ImprovementLabError("observed must be a number; a trial with no measurement is an anecdote.")
        object.__setattr__(self, "observed", float(self.observed))
        if not isinstance(self.passed, bool):
            raise ImprovementLabError("passed must be a boolean.")
        object.__setattr__(self, "notes", str(self.notes or "").strip()[:600])

    def to_mapping(self) -> dict[str, Any]:
        return {"trial": self.trial, "observed": self.observed, "passed": self.passed, "notes": self.notes}


@dataclass(frozen=True, slots=True)
class ImprovementReport:
    """Everything an experiment established, everything it did not, and what it may not do next.

    ``recommendation`` is derived: ``adopt_candidate`` only when every trial is reported, the
    hypothesis threshold was met, the post-trial guard matches the pre-trial one, and the candidate is
    triable locally. Otherwise it is ``keep_incumbent`` or ``inconclusive`` with the reasons.
    """

    experiment: Experiment
    trials: tuple[TrialResult, ...]
    post_guard: HeldOutGuard
    runs_evaluated: int = 1
    intervention_minutes: float = 0.0
    limitations: tuple[str, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.experiment, Experiment):
            raise ImprovementLabError("a report needs the Experiment it describes.")
        trials: list[TrialResult] = []
        for raw in self.trials:
            if not isinstance(raw, TrialResult):
                raise ImprovementLabError("trials must be TrialResult records.")
            trials.append(raw)
        if not trials:
            raise ImprovementLabError("a report must include every trial, including the failed ones.")
        if len(trials) > self.experiment.max_trials:
            raise ImprovementLabError(
                f"{len(trials)} trials reported against a budget of {self.experiment.max_trials}; a "
                "budget that is not enforced is a suggestion."
            )
        object.__setattr__(self, "trials", tuple(trials))
        if not isinstance(self.post_guard, HeldOutGuard):
            raise HeldOutRefused("a report needs the post-trial HeldOutGuard.")
        if not isinstance(self.runs_evaluated, int) or isinstance(self.runs_evaluated, bool) or self.runs_evaluated < 1:
            raise ImprovementLabError("runs_evaluated must be a positive integer.")
        if not isinstance(self.intervention_minutes, (int, float)) or isinstance(self.intervention_minutes, bool):
            raise ImprovementLabError("intervention_minutes must be a number.")
        object.__setattr__(self, "limitations", tuple(str(x).strip() for x in self.limitations if str(x).strip()))

    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = list(self.experiment.compatibility.blocking_reasons())
        if not self.experiment.candidate.is_triable_locally:
            reasons.append(
                f"candidate {self.experiment.candidate.name!r} needs a hosted service "
                f"({self.experiment.candidate.hosted_dependency or self.experiment.candidate.adaptation}); "
                "reference-only candidates may not be trialled"
            )
        try:
            self.experiment.guard.assert_untouched(self.post_guard)
        except HeldOutRefused as exc:
            reasons.append(str(exc))
        return tuple(reasons)

    def best_observed(self) -> float | None:
        if not self.trials:
            return None
        values = [trial.observed for trial in self.trials]
        return max(values) if self.experiment.hypothesis.direction == "increase" else min(values)

    def recommendation(self) -> str:
        if self.blocking_reasons():
            return "keep_incumbent"
        best = self.best_observed()
        if best is None:
            return "inconclusive"
        if self.experiment.hypothesis.is_met(best) and any(trial.passed for trial in self.trials):
            return "adopt_candidate"
        return "keep_incumbent"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": IL_SCHEMA_VERSION,
            "run_id": self.run_id,
            "hypothesis": self.experiment.hypothesis.to_mapping(),
            "surface": self.experiment.surface,
            "candidate": self.experiment.candidate.to_mapping(),
            "compatibility": self.experiment.compatibility.to_mapping(),
            "held_out_guard_before": self.experiment.guard.to_mapping(),
            "held_out_guard_after": self.post_guard.to_mapping(),
            "trials": [trial.to_mapping() for trial in self.trials],
            "max_trials": self.experiment.max_trials,
            "best_observed": self.best_observed(),
            "runs_evaluated": self.runs_evaluated,
            "intervention_minutes": self.intervention_minutes,
            "limitations": list(self.limitations),
            "blocking_reasons": list(self.blocking_reasons()),
            "recommendation": self.recommendation(),
            "weights_changed": False,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_experiment(
    experiment: Experiment,
    *,
    trialler: Callable[[Experiment, int], Mapping[str, Any]],
    post_guard: HeldOutGuard,
) -> ImprovementReport:
    """Run the trial(s) a trialler returns, and refuse to start when any precondition fails.

    The preconditions are checked *before* the trialler is called, so an incompatible or hosted
    candidate never reaches it: a check that runs after the work is a comment. ``trialler`` receives
    the experiment and a 1-based trial number.
    """
    if not isinstance(experiment, Experiment):
        raise ImprovementLabError("run_experiment needs an Experiment.")
    reasons = experiment.compatibility.blocking_reasons()
    if reasons:
        raise CompatibilityRefused(
            "the trial was not started: " + "; ".join(reasons) + ". Compatibility and resource limits "
            "precede trials, not follow them."
        )
    if not experiment.candidate.is_triable_locally:
        raise LocalOnlyRefused(
            f"candidate {experiment.candidate.name!r} depends on a hosted service "
            f"({experiment.candidate.hosted_dependency or experiment.candidate.adaptation}); candidate "
            "generation, judging and optimisation inference stay local."
        )
    if not isinstance(post_guard, HeldOutGuard):
        raise HeldOutRefused("run_experiment needs the post-trial HeldOutGuard.")

    trials: list[TrialResult] = []
    for index in range(1, experiment.max_trials + 1):
        observed = trialler(experiment, index)
        if not isinstance(observed, Mapping):
            raise ImprovementLabError("the trialler must return a mapping of observations.")
        value = observed.get("observed")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ImprovementLabError("the trialler must report a numeric 'observed' value.")
        passed = observed.get("passed")
        if not isinstance(passed, bool):
            raise ImprovementLabError("the trialler must report 'passed' as a boolean.")
        trials.append(
            TrialResult(
                trial=index,
                observed=float(value),
                passed=passed,
                notes=str(observed.get("notes") or ""),
            )
        )
    return ImprovementReport(
        experiment=experiment,
        trials=tuple(trials),
        post_guard=post_guard,
    )


#: Imports that would let this module reach the network or start work on its own.
_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {"socket", "ssl", "threading", "asyncio", "sched", "schedule", "requests", "httpx", "urllib", "subprocess"}
)

#: Calls that start background work or a polling loop.
_FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"create_task", "ensure_future", "Thread", "Timer", "start_background_task", "sleep"}
)


def assert_no_implicit_egress(module_source: str) -> None:
    """Static check helper: no socket, thread, scheduler or polling loop in this module.

    Parsed rather than grepped, because this function's own text necessarily contains the names it
    forbids -- a substring scan would flag itself and prove nothing.
    """
    tree = ast.parse(module_source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.split(".")[0] in _FORBIDDEN_IMPORTS)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _FORBIDDEN_IMPORTS:
                found.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _FORBIDDEN_CALLS:
                found.append(f"{name}()")
        elif isinstance(node, ast.While):
            test = node.test
            if isinstance(test, ast.Constant) and test.value is True:
                found.append("while True")
    if found:
        raise EgressRefused(
            "the improvement lab must not carry its own network or background machinery; found "
            f"{sorted(set(found))}."
        )


def _as_sequence(value: object) -> Iterable[object]:
    if isinstance(value, (list, tuple)):
        return value
    raise ImprovementLabError("expected a sequence of candidate entries.")
