"""AP-012 — disposable capability experiments with receipts.

An experiment lab is where an unproven candidate is allowed to run *once*, in
isolation, with a receipt that says exactly what happened and what it cost. The
point is to make "try it and see" a bounded, reviewable operation instead of an
informal one.

This module is the contract and the bookkeeping. It performs no installation and
starts no process: the sandbox execution is injected through :class:`TrialRunner`,
exactly as AP-009 injects its fetcher and AP-010 injects its browser driver. What
it owns is the part that must be identical every time:

* **Grants (AC1).** A trial needs an explicit experiment grant, and a network or
  resource grant if it wants either. No grant, no trial.
* **Containment (AC2).** An untrusted candidate may not receive host mounts,
  production credentials or privileged runtime sockets. Those are refused at
  construction, so a trial cannot be described with them at all.
* **Inspection (AC3).** Install hooks, custom loaders and binaries are recorded
  and constrained; an uninspected hook blocks the trial.
* **Receipts (AC4).** Every trial records exact versions, effects, checks, cost
  and cleanup outcome, and the receipt names what it does *not* prove.
* **Qualification gate (AC5).** A trial is refused while the host inventory is
  not qualified. There is no host fallback: a Linux sandbox is never substituted
  for the owner's host.
* **No silent promotion (AC6).** A successful trial yields a task-local result
  only. Promotion to a global capability is a separate, explicit act that this
  module can record but never perform on its own.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "ExperimentError",
    "TrialRefusedError",
    "QualificationUnavailableError",
    "ContainmentViolation",
    "QualificationState",
    "HostQualification",
    "ExperimentGrant",
    "CandidateArtifact",
    "InspectionFinding",
    "inspect_candidate",
    "TrialSpec",
    "TrialReceipt",
    "TrialRunner",
    "Lab",
    "PromotionRecord",
    "evaluate_qualification",
    "RECEIPT_SCHEMA_VERSION",
    "LAB_ROOT",
]

#: U04: every lab byte lives under the AP workspace.
LAB_ROOT = Path("~/M5_AP_Workspace") / "experiments"

_SECRET_KEY = re.compile(
    r"(token|secret|passw|api[_-]?key|authorization|cookie|credential|session[_-]?id)",
    re.IGNORECASE,
)

#: Paths a candidate may never be granted. Kept explicit so a refusal can cite
#: the exact one that was asked for.
FORBIDDEN_MOUNTS = (
    "/",
    "/etc",
    "/root",
    "/home",
    "/Users",
    "/var/run/docker.sock",
    "/run/containerd/containerd.sock",
    "/proc",
    "/sys",
    "/dev",
)

FORBIDDEN_SOCKETS = (
    "docker.sock",
    "containerd.sock",
    "podman.sock",
    "crio.sock",
    "kubelet.sock",
)


class ExperimentError(ValueError):
    """Caller misuse: an unusable or contradictory experiment description."""


class TrialRefusedError(ExperimentError):
    """The trial was refused by policy. Nothing was installed and nothing ran."""


class QualificationUnavailableError(TrialRefusedError):
    """AC5: the host is not qualified, so no trial may be admitted."""


class ContainmentViolation(ExperimentError):
    """AC2: the trial asked for something an untrusted candidate may never have."""


class QualificationState:
    """The three states the operating envelope defines. No fourth 'probably fine'."""

    NOT_MEASURED = "not_measured"
    OBSERVED_UNQUALIFIED = "observed_unqualified"
    QUALIFIED = "qualified"

    ALL = (NOT_MEASURED, OBSERVED_UNQUALIFIED, QUALIFIED)


def _now() -> str:
    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact(value: Any) -> Any:
    """Strip credential-shaped content before anything is written to a receipt."""
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if _SECRET_KEY.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# AC5 — qualification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HostQualification:
    """What the host inventory actually says. Mirrors the operating envelope."""

    status: str
    qualification_record: str | None = None
    host_fingerprint: str | None = None
    observed_at: str | None = None

    def __post_init__(self) -> None:
        if self.status not in QualificationState.ALL:
            raise ExperimentError(
                f"qualification status must be one of {QualificationState.ALL}."
            )
        if self.status == QualificationState.QUALIFIED:
            if not (self.qualification_record or "").strip():
                raise ExperimentError(
                    "a qualified host must name the qualification record that binds it."
                )
            if not (self.host_fingerprint or "").strip():
                raise ExperimentError(
                    "a qualified host must carry the fingerprint the record binds."
                )
        if self.status == QualificationState.NOT_MEASURED and (
            self.observed_at or self.qualification_record
        ):
            raise ExperimentError(
                "a not_measured host must keep its observation fields empty."
            )

    @property
    def is_qualified(self) -> bool:
        return self.status == QualificationState.QUALIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "qualification_record": self.qualification_record,
            "host_fingerprint": self.host_fingerprint,
            "observed_at": self.observed_at,
        }


def evaluate_qualification(qualification: HostQualification) -> tuple[bool, str]:
    """Decide whether trials may run, and say why in one sentence.

    Returns ``(admitted, reason)``. Anything short of ``qualified`` refuses: an
    ``observed_unqualified`` host has numbers but no qualification record, and
    numbers alone are exactly what the envelope refuses to treat as qualified.
    """
    if qualification.status == QualificationState.QUALIFIED:
        return True, "host is qualified and the record binds its fingerprint."
    if qualification.status == QualificationState.OBSERVED_UNQUALIFIED:
        return (
            False,
            "host is observed but unqualified: observations exist without a "
            "qualification record, so no trial may be admitted (AC5).",
        )
    return (
        False,
        "host inventory is not measured, so no trial may be admitted (AC5). "
        "Run the operator-run collection recipe on the target host first.",
    )


# ---------------------------------------------------------------------------
# AC1 — grants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentGrant:
    """The explicit grant a trial runs under. Empty by default, on purpose."""

    experiment_id: str
    granted_by: str
    granted_at: str
    allow_install: bool = False
    allow_network: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allow_code_execution: bool = False
    max_disk_bytes: int = 0
    max_download_bytes: int = 0
    max_runtime_seconds: int = 0
    expires_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("experiment_id", "granted_by", "granted_at"):
            if not str(getattr(self, name) or "").strip():
                raise ExperimentError(f"ExperimentGrant.{name} must not be empty.")
        for name in (
            "max_disk_bytes",
            "max_download_bytes",
            "max_runtime_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ExperimentError(
                    f"ExperimentGrant.{name} must be a non-negative integer."
                )
        if self.allow_network and not self.allowed_hosts:
            raise ExperimentError(
                "a network grant must name the hosts it allows; there is no open network."
            )
        if not self.allow_network and self.allowed_hosts:
            raise ExperimentError(
                "allowed_hosts without allow_network is contradictory; state both."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "allow_install": self.allow_install,
            "allow_network": self.allow_network,
            "allowed_hosts": list(self.allowed_hosts),
            "allow_code_execution": self.allow_code_execution,
            "max_disk_bytes": self.max_disk_bytes,
            "max_download_bytes": self.max_download_bytes,
            "max_runtime_seconds": self.max_runtime_seconds,
            "expires_at": self.expires_at,
        }

    def expired(self, *, now: str | None = None) -> bool:
        if not self.expires_at:
            return False
        return (now or _now()) > self.expires_at


# ---------------------------------------------------------------------------
# AC2/AC3 — the candidate and its inspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """What is about to be tried, recorded exactly enough to reproduce the trial."""

    candidate_id: str
    name: str
    version: str
    source: str
    digest: str | None = None
    install_hooks: tuple[str, ...] = ()
    binaries: tuple[str, ...] = ()
    custom_loaders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidate_id", "name", "version", "source"):
            if not str(getattr(self, name) or "").strip():
                raise ExperimentError(f"CandidateArtifact.{name} must not be empty.")
        if self.digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ExperimentError(
                "digest must be a lowercase sha256 hex string or None."
            )
        for name in ("install_hooks", "binaries", "custom_loaders"):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "digest": self.digest,
            "install_hooks": list(self.install_hooks),
            "binaries": list(self.binaries),
            "custom_loaders": list(self.custom_loaders),
        }


@dataclass(frozen=True, slots=True)
class InspectionFinding:
    """One inspection result. ``inspected=False`` is a blocker, never a pass."""

    subject: str
    kind: str
    inspected: bool
    detail: str

    @property
    def is_blocking(self) -> bool:
        return not self.inspected

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "kind": self.kind,
            "inspected": self.inspected,
            "detail": self.detail,
            "is_blocking": self.is_blocking,
        }


def inspect_candidate(
    artifact: CandidateArtifact,
    *,
    inspected_paths: Sequence[str] = (),
) -> tuple[InspectionFinding, ...]:
    """Record inspection of the artifact's risky surface (AC3).

    Every hook, loader and binary must be named as inspected. Anything left
    uninspected produces a blocking finding, because "we did not look" and "we
    looked and it was fine" are different facts and only the second may proceed.
    """
    inspected = {str(item).strip() for item in inspected_paths if str(item).strip()}
    findings: list[InspectionFinding] = []
    surface: tuple[tuple[str, str], ...] = (
        *(("install_hook", item) for item in artifact.install_hooks),
        *(("custom_loader", item) for item in artifact.custom_loaders),
        *(("binary", item) for item in artifact.binaries),
    )
    for kind, subject in surface:
        findings.append(
            InspectionFinding(
                subject=subject,
                kind=kind,
                inspected=subject in inspected,
                detail=(
                    "inspected before the trial"
                    if subject in inspected
                    else "not inspected; a trial may not proceed with an uninspected surface"
                ),
            )
        )
    if artifact.digest is None:
        findings.append(
            InspectionFinding(
                subject=artifact.source,
                kind="provenance",
                inspected=False,
                detail="no pinned digest; the artifact cannot be bound to what was reviewed",
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Trial specification, including the containment refusals (AC2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrialSpec:
    """One trial request. Constructing it already enforces containment."""

    trial_id: str
    candidate: CandidateArtifact
    task_id: str
    host_mounts: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    privileged_sockets: tuple[str, ...] = ()
    uses_production_credentials: bool = False

    def __post_init__(self) -> None:
        if not str(self.trial_id).strip() or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", self.trial_id
        ):
            raise ExperimentError("trial_id must be 1-64 url-safe characters.")
        if not str(self.task_id).strip():
            raise ExperimentError("task_id must not be empty.")

        for mount in self.host_mounts:
            clean = str(mount).strip()
            if not clean:
                raise ContainmentViolation("a host mount entry must not be empty.")
            resolved = str(Path(clean).expanduser())
            if (
                clean in FORBIDDEN_MOUNTS
                or any(
                    resolved == forbidden
                    or resolved.startswith(forbidden.rstrip("/") + "/")
                    for forbidden in FORBIDDEN_MOUNTS
                    if forbidden not in {"/"}
                )
                or resolved == "/"
            ):
                raise ContainmentViolation(
                    f"host mount {clean!r} is forbidden: an untrusted candidate gets "
                    "no host mounts (AC2)."
                )

        for item in self.privileged_sockets:
            clean = str(item).strip().lower()
            if any(marker in clean for marker in FORBIDDEN_SOCKETS):
                raise ContainmentViolation(
                    f"privileged runtime socket {item!r} is forbidden (AC2)."
                )

        if self.uses_production_credentials:
            raise ContainmentViolation(
                "an untrusted candidate may not receive production credentials (AC2)."
            )

        for key in self.environment:
            if _SECRET_KEY.search(str(key)):
                raise ContainmentViolation(
                    f"environment key {key!r} looks like a credential; refusing to pass it (AC2)."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "candidate": self.candidate.to_dict(),
            "task_id": self.task_id,
            "host_mounts": list(self.host_mounts),
            "environment_keys": sorted(_redact(dict(self.environment))),
            "privileged_sockets": list(self.privileged_sockets),
            "uses_production_credentials": self.uses_production_credentials,
        }


# ---------------------------------------------------------------------------
# AC4 — receipts
# ---------------------------------------------------------------------------


RECEIPT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TrialReceipt:
    """The record of one trial: what was tried, what happened, what it cost."""

    trial_id: str
    experiment_id: str
    admission: str  # admitted | refused | failed | completed
    started_at: str
    finished_at: str | None
    candidate: Mapping[str, Any]
    grant: Mapping[str, Any]
    findings: tuple[Mapping[str, Any], ...]
    commands: tuple[str, ...]
    effects: tuple[str, ...]
    checks: tuple[Mapping[str, Any], ...]
    versions: Mapping[str, str]
    cost: Mapping[str, Any]
    cleanup: Mapping[str, Any]
    refusal_reason: str | None = None
    qualification: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.admission not in {"admitted", "refused", "failed", "completed"}:
            raise ExperimentError(f"unknown admission outcome {self.admission!r}.")
        if self.admission in {"failed", "completed"} and self.finished_at is None:
            raise ExperimentError("a trial that ran must record when it finished.")
        if self.admission == "refused" and not (self.refusal_reason or "").strip():
            raise ExperimentError("a refused trial must record why it was refused.")
        if self.admission == "completed" and not self.cleanup.get("succeeded"):
            raise ExperimentError(
                "a completed trial must record a successful cleanup; otherwise it is failed."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "admission": self.admission,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "candidate": _redact(dict(self.candidate)),
            "grant": _redact(dict(self.grant)),
            "qualification": dict(self.qualification),
            "findings": [_redact(dict(f)) for f in self.findings],
            "commands": list(self.commands),
            "effects": list(self.effects),
            "checks": [_redact(dict(c)) for c in self.checks],
            "versions": dict(self.versions),
            "cost": dict(self.cost),
            "cleanup": dict(self.cleanup),
            "refusal_reason": self.refusal_reason,
            "grants_global_capability": False,
            "does_not_prove": [
                "that the candidate is safe in general",
                "that the result holds on the owner host if the host was not qualified",
                "that the candidate may be promoted; promotion is a separate recorded act",
            ],
        }

    def digest(self) -> str:
        return _sha256(
            json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False).encode()
        )


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    """What the injected runner reports back. It cannot report 'passed' by omission."""

    versions: Mapping[str, str]
    commands: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    checks: tuple[Mapping[str, Any], ...] = ()
    cost: Mapping[str, Any] = field(default_factory=dict)
    succeeded: bool = False
    error: str | None = None


class TrialRunner(Protocol):
    """The injected execution boundary. The lab never starts a process itself."""

    def run(self, spec: TrialSpec, *, workdir: Path) -> TrialOutcome: ...

    def cleanup(self, spec: TrialSpec, *, workdir: Path) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# AC6 — promotion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    """A promotion is a separate, explicit act. This record refuses to be automatic."""

    trial_id: str
    capability: str
    promoted_by: str
    promoted_at: str
    owner_approved: bool
    scope: str = "task-local"

    def __post_init__(self) -> None:
        if not self.owner_approved:
            raise ExperimentError(
                "promotion requires explicit owner approval; a successful trial is not one."
            )
        if self.scope not in {"task-local", "experiment", "global"}:
            raise ExperimentError(f"unknown promotion scope {self.scope!r}.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "capability": self.capability,
            "promoted_by": self.promoted_by,
            "promoted_at": self.promoted_at,
            "owner_approved": self.owner_approved,
            "scope": self.scope,
        }


# ---------------------------------------------------------------------------
# The lab
# ---------------------------------------------------------------------------


@dataclass
class Lab:
    """Admits trials, keeps receipts and owns cleanup. Never installs on its own."""

    grant: ExperimentGrant
    qualification: HostQualification
    runner: TrialRunner | None = None
    root: Path = LAB_ROOT
    receipts: list[TrialReceipt] = field(default_factory=list)
    promotions: list[PromotionRecord] = field(default_factory=list)
    clock: Any = field(default=_now, repr=False)

    def __post_init__(self) -> None:
        root = Path(str(self.root)).expanduser()
        if not str(root).strip():
            raise ExperimentError("lab root must not be empty.")
        self.root = root

    # -- admission ---------------------------------------------------------

    def admit(self, spec: TrialSpec) -> tuple[bool, str | None]:
        """Would this trial be allowed as specified? Returns (admitted, reason)."""
        return self.admit_with_inspection(spec, ())

    def refused_receipt(self, spec: TrialSpec, reason: str) -> TrialReceipt:
        """Record a refusal as a receipt. A refusal is evidence too."""
        receipt = TrialReceipt(
            trial_id=spec.trial_id,
            experiment_id=self.grant.experiment_id,
            admission="refused",
            started_at=self.clock(),
            finished_at=None,
            candidate=spec.candidate.to_dict(),
            grant=self.grant.to_dict(),
            qualification=self.qualification.to_dict(),
            findings=tuple(f.to_dict() for f in inspect_candidate(spec.candidate)),
            commands=(),
            effects=(),
            checks=(),
            versions={},
            cost={},
            cleanup={"attempted": False, "succeeded": None},
            refusal_reason=reason,
        )
        self.receipts.append(receipt)
        return receipt

    # -- running -----------------------------------------------------------

    def run_trial(
        self, spec: TrialSpec, *, inspected_paths: Sequence[str] = ()
    ) -> TrialReceipt:
        """Admit, run and record one trial. Refusals are receipts, not exceptions."""
        admitted, reason = self.admit_with_inspection(spec, inspected_paths)
        if not admitted:
            return self.refused_receipt(spec, reason or "refused")
        if self.runner is None:
            raise ExperimentError(
                "no trial runner is injected; the lab does not execute anything itself."
            )

        workdir = self._workdir(spec)
        started = self.clock()
        outcome: TrialOutcome | None = None
        error: str | None = None
        try:
            outcome = self.runner.run(spec, workdir=workdir)
        except Exception as exc:  # a crashing runner is a failed trial, not a crash
            error = f"runner error: {exc}"
        cleanup = self._cleanup(spec, workdir)
        finished = self.clock()

        succeeded = bool(outcome and outcome.succeeded and not error)
        admission = (
            "completed" if (succeeded and cleanup.get("succeeded")) else "failed"
        )
        receipt = TrialReceipt(
            trial_id=spec.trial_id,
            experiment_id=self.grant.experiment_id,
            admission=admission,
            started_at=started,
            finished_at=finished,
            candidate=spec.candidate.to_dict(),
            grant=self.grant.to_dict(),
            qualification=self.qualification.to_dict(),
            findings=tuple(
                f.to_dict()
                for f in inspect_candidate(
                    spec.candidate, inspected_paths=inspected_paths
                )
            ),
            commands=tuple(outcome.commands) if outcome else (),
            effects=tuple(outcome.effects) if outcome else (),
            checks=tuple(outcome.checks) if outcome else (),
            versions=dict(outcome.versions) if outcome else {},
            cost=dict(outcome.cost) if outcome else {},
            cleanup=cleanup,
            refusal_reason=error,
        )
        self.receipts.append(receipt)
        return receipt

    def admit_with_inspection(
        self, spec: TrialSpec, inspected_paths: Sequence[str] = ()
    ) -> tuple[bool, str | None]:
        """As :meth:`admit`, but honour the paths actually inspected for this trial."""
        admitted_q, reason_q = evaluate_qualification(self.qualification)
        if not admitted_q:
            return False, reason_q
        if self.grant.expired(now=self.clock()):
            return False, "the experiment grant has expired; no trial may run under it."
        if not self.grant.allow_install:
            return (
                False,
                "the experiment grant does not permit installation; a disposable lab "
                "still needs the install grant to be explicit (AC1).",
            )
        if not self.grant.allow_code_execution:
            return False, "the experiment grant does not permit code execution (AC1)."
        if self.grant.max_disk_bytes <= 0 or self.grant.max_runtime_seconds <= 0:
            return (
                False,
                "the experiment grant states no resource budget; a trial needs an "
                "explicit disk and runtime allowance to be bounded (AC1).",
            )
        findings = inspect_candidate(spec.candidate, inspected_paths=inspected_paths)
        blocking = [f for f in findings if f.is_blocking]
        if blocking:
            return (
                False,
                "candidate surface is not fully inspected: "
                + ", ".join(f"{f.kind}:{f.subject}" for f in blocking),
            )
        return True, None

    def _workdir(self, spec: TrialSpec) -> Path:
        workdir = self.root / self.grant.experiment_id / spec.trial_id
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def _cleanup(self, spec: TrialSpec, workdir: Path) -> dict[str, Any]:
        if self.runner is None:
            return {"attempted": False, "succeeded": None}
        try:
            result = dict(self.runner.cleanup(spec, workdir=workdir) or {})
        except Exception as exc:
            return {"attempted": True, "succeeded": False, "error": str(exc)}
        result.setdefault("attempted", True)
        result.setdefault("succeeded", False)
        # The lab removes its own scratch directory regardless of what the runner said.
        removed = True
        try:
            if workdir.exists():
                for child in sorted(
                    workdir.rglob("*"), key=lambda p: len(p.parts), reverse=True
                ):
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                workdir.rmdir()
        except OSError:
            removed = False
        result["lab_scratch_removed"] = removed
        if not removed:
            result["succeeded"] = False
        return result

    # -- quarantine --------------------------------------------------------

    def quarantine(self, trial_id: str, *, reason: str) -> dict[str, Any]:
        """Revoke a trial's artifacts: record it, and refuse to run it again.

        Quarantine is recorded state plus a marker file inside the lab root. It
        never deletes anything outside that root.
        """
        receipt = next((r for r in self.receipts if r.trial_id == trial_id), None)
        if receipt is None:
            raise ExperimentError(f"no receipt for trial {trial_id!r}.")
        marker_dir = self.root / self.grant.experiment_id / "quarantine"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / f"{trial_id}.json"
        payload = {
            "trial_id": trial_id,
            "reason": reason,
            "quarantined_at": self.clock(),
            "receipt_digest": receipt.digest(),
        }
        marker.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload

    def is_quarantined(self, trial_id: str) -> bool:
        return (
            self.root / self.grant.experiment_id / "quarantine" / f"{trial_id}.json"
        ).is_file()

    # -- promotion ---------------------------------------------------------

    def record_promotion(
        self, trial_id: str, *, capability: str, promoted_by: str, owner_approved: bool
    ) -> PromotionRecord:
        """Record a promotion the owner made. The lab never promotes by itself."""
        receipt = next((r for r in self.receipts if r.trial_id == trial_id), None)
        if receipt is None:
            raise ExperimentError(f"no receipt for trial {trial_id!r}.")
        if receipt.admission != "completed":
            raise ExperimentError(
                "only a completed trial can be promoted; a refused or failed trial proves nothing."
            )
        record = PromotionRecord(
            trial_id=trial_id,
            capability=capability,
            promoted_by=promoted_by,
            promoted_at=self.clock(),
            owner_approved=owner_approved,
        )
        self.promotions.append(record)
        return record

    # -- reporting ---------------------------------------------------------

    def render_receipts(self) -> str:
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "experiment_id": self.grant.experiment_id,
            "qualification": self.qualification.to_dict(),
            "receipts": [r.to_dict() for r in self.receipts],
            "promotions": [p.to_dict() for p in self.promotions],
            "lab_grants": {
                "production_activation": False,
                "global_capability": False,
                "host_fallback": False,
            },
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
