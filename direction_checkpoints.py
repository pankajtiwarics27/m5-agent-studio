#!/usr/bin/env python3
"""Early direction samples: ask before expensive production, and only when it is worth asking.

An autonomous builder that runs to completion before showing anything burns a large
budget on work the owner may not want. The obvious fix -- show a sample before every
job -- is worse: it turns autonomy back into supervision and trains the owner to
approve without reading.

So this module makes one decision, explicitly, and refuses to make it by default:

* **Sample when there is no benchmark covering the task.** An unbenchmarked task has no
  established direction to follow, so a small representative sample is the cheapest way
  to find out whether the direction is right.
* **Skip the check-in when a benchmark covers it.** An owner-approved reference already
  settles direction for that class of work; asking again spends the owner's attention on
  a question they have already answered. That is the "skip unnecessary check-ins" path.
* **Escalate anyway on material uncertainty,** even when benchmarked. A benchmark covers
  what it measured. A task that diverges from it -- a new format, an unsupported
  interaction, a changed audience -- is outside the covered surface, and silence would be
  the check-in failing open.

What a pause is *not*: it is not acceptance. ``DirectionDecision`` carries
``direction_approval_is_final_acceptance = False`` and
``direction_approval_grants_publish_permission = False`` as constants, because the one
mistake that would matter here is a direction "yes" being read later as "ship it". The
artifact is preserved by the caller through the existing checkpoint; this module records
which artifact version a question is about so a late answer cannot be applied to a
different version (AP-019's stale-selection rule starts here).

The module is pure: it reads no clock, opens no file, starts no job and loads no model.
A caller supplies the evidence; nothing here can manufacture it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1

# Reportable reasons a direction checkpoint fired or was skipped. Free-form strings would
# make the "did we ask too much / too little" question unanswerable later.
TRIGGERS = frozenset(
    {
        "no_benchmark",
        "uncertainty_exceeds_coverage",
        "benchmark_covered",
    }
)

# Uncertainties a benchmark can be blind to even when it nominally covers the task class.
MATERIAL_UNCERTAINTIES = frozenset(
    {
        "new_format",
        "unsupported_interaction",
        "audience_change",
        "scope_change",
        "rights_unresolved",
        "external_dependency",
    }
)

MAX_FEEDBACK_POINTS = 5
MAX_TEXT = 400
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STEP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .:/-]{0,119}$")


class DirectionError(ValueError):
    """Raised when a direction record would state something it cannot support."""


def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if not text and not allow_empty:
        raise DirectionError(f"{field_name} must not be empty.")
    if len(text) > MAX_TEXT:
        raise DirectionError(f"{field_name} must be at most {MAX_TEXT} characters.")
    return text


def _digest_or_empty(value: object, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not _DIGEST_RE.fullmatch(text):
        raise DirectionError(f"{field_name} must be a lowercase SHA-256 hex digest.")
    return text


@dataclass(frozen=True, slots=True)
class BenchmarkCoverage:
    """What an owner-approved reference already settles, and how far that reaches.

    ``covered_task_kinds`` are the task classes the reference was approved for.
    ``measured_checks`` are the specific checks it actually established. A caller cannot
    claim coverage for a check that was never measured, which is what keeps "benchmarked"
    from quietly widening into "anything we have ever done once".
    """

    reference_id: str
    reference_sha256: str
    covered_task_kinds: tuple[str, ...]
    measured_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.reference_id, "reference_id")
        _digest_or_empty(self.reference_sha256, "reference_sha256")
        if not self.reference_sha256:
            raise DirectionError(
                "A benchmark reference must carry the digest of the version it covers."
            )
        if not self.covered_task_kinds:
            raise DirectionError(
                "A benchmark must name at least one task kind it covers."
            )

    def covers(self, task_kind: str) -> bool:
        return task_kind in self.covered_task_kinds

    def to_mapping(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "reference_sha256": self.reference_sha256,
            "covered_task_kinds": list(self.covered_task_kinds),
            "measured_checks": list(self.measured_checks),
        }


@dataclass(frozen=True, slots=True)
class SampleArtifact:
    """The cheap thing shown to the owner, and what the checks actually found.

    ``technical_checks`` maps a check name to whether it passed. The sample is only
    presentable when every check recorded for it passed: showing a broken sample and
    asking "is this the right direction?" invites a judgement about a defect.
    """

    artifact_id: str
    content_sha256: str
    technical_checks: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.artifact_id, "artifact_id")
        if not _DIGEST_RE.fullmatch(
            _digest_or_empty(self.content_sha256, "content_sha256")
        ):
            raise DirectionError(
                "A sample must name the exact artifact version it shows."
            )

    @property
    def passed_checks(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, ok in self.technical_checks.items() if ok))

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, ok in self.technical_checks.items() if not ok)
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "technical_checks": dict(
                sorted((str(k), bool(v)) for k, v in self.technical_checks.items())
            ),
        }


@dataclass(frozen=True, slots=True)
class DirectionCheckpoint:
    """The decision to pause for direction, or the reason not to.

    Built only by :func:`decide`, so the trigger and the evidence behind it cannot be
    paired up arbitrarily by a caller.
    """

    task_id: str
    task_kind: str
    trigger: str
    rationale: str
    sample: SampleArtifact | None
    feedback_prompt: str
    feedback_points: tuple[str, ...]
    benchmark_reference_id: str = ""
    benchmark_reference_sha256: str = ""
    escalated_uncertainties: tuple[str, ...] = ()

    # Constants, not fields: there is no supported way to construct a checkpoint in which
    # a direction answer implies acceptance or permission to publish.
    direction_approval_is_final_acceptance: bool = field(default=False, init=False)
    direction_approval_grants_publish_permission: bool = field(
        default=False, init=False
    )

    @property
    def pauses_production(self) -> bool:
        return self.trigger in {"no_benchmark", "uncertainty_exceeds_coverage"}

    @property
    def is_resumable_from_checkpoint(self) -> bool:
        """A pause is only safe if the work behind it is preserved and identifiable."""
        return self.sample is not None and self.pauses_production

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "task_kind": self.task_kind,
            "trigger": self.trigger,
            "rationale": self.rationale,
            "sample": self.sample.to_mapping() if self.sample else None,
            "feedback_prompt": self.feedback_prompt,
            "feedback_points": list(self.feedback_points),
            "benchmark_reference_id": self.benchmark_reference_id,
            "benchmark_reference_sha256": self.benchmark_reference_sha256,
            "escalated_uncertainties": list(self.escalated_uncertainties),
            "pauses_production": self.pauses_production,
            "direction_approval_is_final_acceptance": self.direction_approval_is_final_acceptance,
            "direction_approval_grants_publish_permission": self.direction_approval_grants_publish_permission,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide(
    *,
    task_id: str,
    task_kind: str,
    sample: SampleArtifact | None,
    feedback_points: Sequence[str] = (),
    benchmarks: Sequence[BenchmarkCoverage] = (),
    uncertainties: Sequence[str] = (),
) -> DirectionCheckpoint:
    """Decide whether this task needs a direction check-in, and say why.

    The ordering matters. Material uncertainty is checked before coverage, because a
    benchmark that covers the task class does not cover a divergence from it. Coverage is
    checked next. Only an uncovered task that also has no sample falls through to a
    refusal -- asking the owner to judge a direction with nothing to look at is not a
    check-in, it is a prompt.
    """
    _text(task_id, "task_id")
    _text(task_kind, "task_kind")

    unknown = [u for u in uncertainties if u not in MATERIAL_UNCERTAINTIES]
    if unknown:
        raise DirectionError(
            f"unknown material uncertainty {unknown[0]!r}; a free-form string cannot escalate"
        )
    escalation = tuple(sorted(set(uncertainties)))

    covering = next((b for b in benchmarks if b.covers(task_kind)), None)

    prompt = _text(_prompt_for(task_kind, escalation), "feedback_prompt")
    points = tuple(_text(p, "feedback_point") for p in feedback_points)[
        :MAX_FEEDBACK_POINTS
    ]

    if covering is not None and not escalation:
        return DirectionCheckpoint(
            task_id=task_id,
            task_kind=task_kind,
            trigger="benchmark_covered",
            rationale=(
                f"An owner-approved reference ({covering.reference_id}) already settles direction for "
                f"{task_kind!r}; asking again would spend the owner's attention on a settled question."
            ),
            sample=None,
            feedback_prompt="",
            feedback_points=(),
            benchmark_reference_id=covering.reference_id,
            benchmark_reference_sha256=covering.reference_sha256,
        )

    if sample is None:
        raise DirectionError(
            "a direction check-in needs a sample to look at; refusing to pause production on nothing"
        )
    if sample.failed_checks:
        raise DirectionError(
            "sample failed its own technical checks ("
            + ", ".join(sample.failed_checks)
            + "); a direction question about a broken sample is not a direction question"
        )

    if escalation:
        trigger = "uncertainty_exceeds_coverage"
        if covering is None:
            rationale = (
                f"No reference covers {task_kind!r}, and the sample raises "
                + ", ".join(escalation)
                + "; direction is genuinely undecided."
            )
        else:
            rationale = (
                f"{covering.reference_id} covers {task_kind!r} but not "
                + ", ".join(escalation)
                + ", so the covered direction cannot be assumed to apply."
            )
    else:
        trigger = "no_benchmark"
        rationale = f"No owner-approved reference covers {task_kind!r}; the sample is the cheapest way to fix direction."

    return DirectionCheckpoint(
        task_id=task_id,
        task_kind=task_kind,
        trigger=trigger,
        rationale=rationale,
        sample=sample,
        feedback_prompt=prompt,
        feedback_points=points,
        benchmark_reference_id=covering.reference_id if covering else "",
        benchmark_reference_sha256=covering.reference_sha256 if covering else "",
        escalated_uncertainties=escalation,
    )


def _prompt_for(task_kind: str, escalation: Sequence[str]) -> str:
    """Name what feedback is needed, rather than asking an open "what do you think?"."""
    base = f"Is this the right direction for {task_kind}? Say keep, redirect, or what to change."
    if escalation:
        base += " Unsettled: " + ", ".join(escalation).replace("_", " ") + "."
    return base


def resume(
    checkpoint: DirectionCheckpoint,
    *,
    answer: str,
    answered_artifact_sha256: str,
    current_artifact_sha256: str,
) -> str:
    """Resolve a direction pause to ``continue`` or ``redirect``, refusing a stale answer.

    An answer given about one version of the sample may not be applied to another. This
    is the same rule AP-019 applies to scoped feedback, enforced here so the first place
    a version can drift is caught.
    """
    if not checkpoint.pauses_production:
        raise DirectionError(
            f"checkpoint {checkpoint.trigger!r} did not pause production; nothing to resume"
        )
    if checkpoint.sample is None:
        raise DirectionError("checkpoint has no sample; it cannot be resumed")
    answered = _digest_or_empty(answered_artifact_sha256, "answered_artifact_sha256")
    current = _digest_or_empty(current_artifact_sha256, "current_artifact_sha256")
    if not answered or not current:
        raise DirectionError(
            "resuming needs both the answered and the current artifact digest"
        )
    if answered != current:
        raise DirectionError(
            "the sample changed after the question was asked; the answer applies to a different version"
        )
    if answered != checkpoint.sample.content_sha256:
        raise DirectionError(
            "the answered artifact is not the sample this checkpoint showed"
        )

    verdict = " ".join(str(answer or "").split()).lower()
    if not verdict:
        raise DirectionError("a direction answer must say keep or redirect")
    if verdict in {"keep", "continue", "approve", "yes", "lgtm"}:
        return "continue"
    return "redirect"
