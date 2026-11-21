"""AP-012 offline tests: disposable experiment lab, receipts and refusals.

No test installs anything, starts a real process or touches a host path. The
runner is injected in every case, which is the point: the lab's job is the
decision and the record, not the execution.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import autonomous_production_lab as L
from autonomous_production_lab import (
    CandidateArtifact,
    ContainmentViolation,
    ExperimentError,
    ExperimentGrant,
    HostQualification,
    Lab,
    PromotionRecord,
    QualificationState,
    TrialOutcome,
    TrialReceipt,
    TrialSpec,
    evaluate_qualification,
    inspect_candidate,
)

FIXED = "2026-11-21T17:00:00Z"


def clock() -> str:
    return FIXED


def grant(**overrides: Any) -> ExperimentGrant:
    base: dict[str, Any] = dict(
        experiment_id="exp-1",
        granted_by="owner",
        granted_at="2026-11-21T00:00:00Z",
        allow_install=True,
        allow_code_execution=True,
        max_disk_bytes=10_000,
        max_runtime_seconds=60,
    )
    base.update(overrides)
    return ExperimentGrant(**base)


def qualified() -> HostQualification:
    return HostQualification(
        status=QualificationState.QUALIFIED,
        qualification_record="qual-2026-11-21.json",
        host_fingerprint="abc123",
        observed_at="2026-11-21T06:00:00Z",
    )


def artifact(**overrides: Any) -> CandidateArtifact:
    base: dict[str, Any] = dict(
        candidate_id="cand-1",
        name="Example Tool",
        version="1.2.3",
        source="https://github.com/example/tool",
        digest="a" * 64,
    )
    base.update(overrides)
    return CandidateArtifact(**base)


def spec(**overrides: Any) -> TrialSpec:
    base: dict[str, Any] = dict(
        trial_id="trial-1", candidate=artifact(), task_id="task-1"
    )
    base.update(overrides)
    return TrialSpec(**base)


class FakeRunner:
    """A runner that records what it was asked to do and cleans up honestly."""

    def __init__(
        self,
        *,
        succeeded: bool = True,
        cleanup_ok: bool = True,
        raises: Exception | None = None,
    ):
        self.ran: list[str] = []
        self.cleaned: list[str] = []
        self.succeeded = succeeded
        self.cleanup_ok = cleanup_ok
        self.raises = raises

    def run(self, trial: TrialSpec, *, workdir: Path) -> TrialOutcome:
        self.ran.append(trial.trial_id)
        assert workdir.is_dir()
        if self.raises:
            raise self.raises
        return TrialOutcome(
            versions={"python": "3.11.2", "tool": trial.candidate.version},
            commands=("tool --version",),
            effects=("wrote scratch file",),
            checks=({"name": "exit_code", "ok": self.succeeded},),
            cost={"disk_bytes": 4096, "runtime_seconds": 2},
            succeeded=self.succeeded,
        )

    def cleanup(self, trial: TrialSpec, *, workdir: Path) -> dict:
        self.cleaned.append(trial.trial_id)
        return {"attempted": True, "succeeded": self.cleanup_ok}


def lab(tmp_path: Path, *, qualification=None, g=None, runner=None) -> Lab:
    return Lab(
        grant=g or grant(),
        qualification=qualification or qualified(),
        runner=runner if runner is not None else FakeRunner(),
        root=tmp_path / "experiments",
        clock=clock,
    )


# --------------------------------------------------------------- AC5: the gate


def test_not_measured_host_admits_nothing():
    admitted, reason = evaluate_qualification(
        HostQualification(status=QualificationState.NOT_MEASURED)
    )
    assert admitted is False
    assert "not measured" in reason


def test_observed_but_unqualified_host_admits_nothing():
    """Numbers without a qualification record are not qualification."""
    admitted, reason = evaluate_qualification(
        HostQualification(
            status=QualificationState.OBSERVED_UNQUALIFIED,
            observed_at="2026-11-21T06:00:00Z",
        )
    )
    assert admitted is False
    assert "unqualified" in reason


def test_qualified_host_admits_trials(tmp_path):
    admitted, reason = evaluate_qualification(qualified())
    assert admitted is True, reason
    assert lab(tmp_path).admit(spec()) == (True, None)


def test_a_qualified_host_must_name_its_record_and_fingerprint():
    with pytest.raises(ExperimentError, match="qualification record"):
        HostQualification(status=QualificationState.QUALIFIED, host_fingerprint="abc")
    with pytest.raises(ExperimentError, match="fingerprint"):
        HostQualification(
            status=QualificationState.QUALIFIED, qualification_record="q.json"
        )


def test_not_measured_cannot_carry_observations():
    with pytest.raises(ExperimentError):
        HostQualification(status=QualificationState.NOT_MEASURED, observed_at=FIXED)


def test_refused_trial_on_unmeasured_host_is_recorded_not_run(tmp_path):
    runner = FakeRunner()
    real = lab(
        tmp_path,
        qualification=HostQualification(status=QualificationState.NOT_MEASURED),
        runner=runner,
    )
    receipt = real.run_trial(spec())
    assert receipt.admission == "refused"
    assert "not measured" in (receipt.refusal_reason or "")
    assert runner.ran == []  # nothing executed


def test_the_real_shipped_gate_refuses_with_the_current_inventory(tmp_path):
    """The repository's own inventory is not_measured, so the lab must refuse.

    This is the honest state of AP-012: the lab is built, the gate is closed
    until an operator qualifies the host.
    """
    admitted, reason = evaluate_qualification(
        HostQualification(status=QualificationState.NOT_MEASURED)
    )
    assert admitted is False
    assert "collection recipe" in reason


def test_admission_answer_does_not_depend_on_call_order(tmp_path):
    """Admission is a pure question; asking twice must not change the answer."""
    real = lab(tmp_path)
    assert real.admit(spec()) == real.admit(spec())


def test_there_is_no_host_fallback_parameter():
    """No argument anywhere lets a caller substitute the current sandbox."""
    forbidden = {"host_fallback", "allow_unqualified", "skip_qualification", "force"}
    for cls in (Lab, TrialSpec, ExperimentGrant):
        assert not (forbidden & set(cls.__dataclass_fields__)), cls


# --------------------------------------------------------------- AC1: grants


def test_trial_needs_an_explicit_install_grant(tmp_path):
    real = lab(tmp_path, g=grant(allow_install=False))
    admitted, reason = real.admit(spec())
    assert admitted is False
    assert "does not permit installation" in (reason or "")


def test_trial_needs_an_explicit_execution_grant(tmp_path):
    real = lab(tmp_path, g=grant(allow_code_execution=False))
    admitted, reason = real.admit(spec())
    assert admitted is False
    assert "code execution" in (reason or "")


def test_a_grant_without_a_resource_budget_admits_nothing(tmp_path):
    """An unbounded trial is not a bounded experiment."""
    real = lab(tmp_path, g=grant(max_disk_bytes=0))
    admitted, reason = real.admit(spec())
    assert admitted is False
    assert "no resource budget" in (reason or "")


def test_expired_grant_admits_nothing(tmp_path):
    real = lab(tmp_path, g=grant(expires_at="2026-11-20T00:00:00Z"))
    admitted, reason = real.admit(spec())
    assert admitted is False
    assert "expired" in (reason or "")


def test_network_grant_must_name_its_hosts():
    with pytest.raises(ExperimentError, match="must name the hosts"):
        ExperimentGrant(
            experiment_id="e", granted_by="o", granted_at=FIXED, allow_network=True
        )


def test_hosts_without_a_network_grant_are_contradictory():
    with pytest.raises(ExperimentError):
        ExperimentGrant(
            experiment_id="e",
            granted_by="o",
            granted_at=FIXED,
            allow_network=False,
            allowed_hosts=("github.com",),
        )


def test_negative_budgets_are_refused():
    with pytest.raises(ExperimentError):
        grant(max_disk_bytes=-1)


# --------------------------------------------------------------- AC2: containment


@pytest.mark.parametrize(
    "mounts",
    [("/",), ("/etc",), ("/root",), ("/var/run/docker.sock",), ("/proc",), ("/dev",)],
)
def test_host_mounts_are_refused(mounts):
    with pytest.raises(ContainmentViolation, match="no host mounts"):
        spec(host_mounts=mounts)


def test_a_subpath_of_a_sensitive_root_is_also_refused():
    with pytest.raises(ContainmentViolation):
        spec(host_mounts=("/etc/ssl/private",))


def test_production_credentials_are_refused():
    with pytest.raises(ContainmentViolation, match="production credentials"):
        spec(uses_production_credentials=True)


@pytest.mark.parametrize(
    "sock",
    ["/var/run/docker.sock", "/run/containerd/containerd.sock", "/run/podman.sock"],
)
def test_privileged_runtime_sockets_are_refused(sock):
    with pytest.raises(ContainmentViolation, match="privileged runtime socket"):
        spec(privileged_sockets=(sock,))


@pytest.mark.parametrize(
    "key", ["API_TOKEN", "GITHUB_SECRET", "DB_PASSWORD", "AWS_CREDENTIAL"]
)
def test_credential_shaped_environment_keys_are_refused(key):
    with pytest.raises(ContainmentViolation, match="looks like a credential"):
        spec(environment={key: "x"})


def test_ordinary_environment_keys_are_allowed():
    s = spec(environment={"LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
    assert sorted(s.to_dict()["environment_keys"]) == [
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
    ]


# --------------------------------------------------------------- AC3: inspection


def test_uninspected_hooks_block_the_trial(tmp_path):
    cand = artifact(install_hooks=("setup.sh",), binaries=("tool",))
    real = lab(tmp_path)
    admitted, reason = real.admit(spec(candidate=cand))
    assert admitted is False
    assert "not fully inspected" in (reason or "")


def test_inspecting_the_surface_allows_the_trial(tmp_path):
    cand = artifact(install_hooks=("setup.sh",), binaries=("tool",))
    real = lab(tmp_path)
    admitted, reason = real.admit_with_inspection(
        spec(candidate=cand), ["setup.sh", "tool"]
    )
    assert admitted is True, reason


def test_missing_digest_is_a_blocking_provenance_finding():
    findings = inspect_candidate(artifact(digest=None))
    assert any(f.kind == "provenance" and f.is_blocking for f in findings)


def test_inspection_records_each_surface_item():
    cand = artifact(install_hooks=("h1",), custom_loaders=("l1",), binaries=("b1",))
    findings = inspect_candidate(cand, inspected_paths=("h1",))
    kinds = {f.kind for f in findings}
    assert kinds == {"install_hook", "custom_loader", "binary"}
    blocking = [f.subject for f in findings if f.is_blocking]
    assert set(blocking) == {"l1", "b1"}


def test_bad_digest_format_is_refused():
    with pytest.raises(ExperimentError):
        artifact(digest="not-a-digest")


# --------------------------------------------------------------- AC4: receipts


def test_completed_trial_records_versions_effects_checks_cost_and_cleanup(tmp_path):
    receipt = lab(tmp_path).run_trial(spec(), inspected_paths=())
    payload = receipt.to_dict()
    assert payload["admission"] == "completed"
    assert payload["versions"]["tool"] == "1.2.3"
    assert payload["effects"] and payload["checks"] and payload["cost"]
    assert payload["cleanup"]["succeeded"] is True
    assert payload["cleanup"]["lab_scratch_removed"] is True
    assert payload["schema_version"] == L.RECEIPT_SCHEMA_VERSION


def test_a_failed_trial_is_not_a_completed_trial(tmp_path):
    receipt = lab(tmp_path, runner=FakeRunner(succeeded=False)).run_trial(spec())
    assert receipt.admission == "failed"


def test_a_trial_with_failed_cleanup_is_failed_even_if_the_run_succeeded(tmp_path):
    receipt = lab(tmp_path, runner=FakeRunner(cleanup_ok=False)).run_trial(spec())
    assert receipt.admission == "failed"


def test_a_crashing_runner_is_a_failed_trial_with_the_error_recorded(tmp_path):
    real = lab(tmp_path, runner=FakeRunner(raises=RuntimeError("segfault")))
    receipt = real.run_trial(spec())
    assert receipt.admission == "failed"
    assert "segfault" in (receipt.refusal_reason or "")


def test_the_lab_removes_its_own_scratch_directory(tmp_path):
    real = lab(tmp_path)
    receipt = real.run_trial(spec())
    assert receipt.cleanup["lab_scratch_removed"] is True
    workdir = tmp_path / "experiments" / "exp-1" / "trial-1"
    assert not workdir.exists()


def test_receipt_states_what_it_does_not_prove(tmp_path):
    payload = lab(tmp_path).run_trial(spec()).to_dict()
    assert payload["grants_global_capability"] is False
    assert len(payload["does_not_prove"]) == 3
    assert any("owner host" in item for item in payload["does_not_prove"])


def test_receipt_redacts_credential_shaped_content_at_serialisation():
    """A receipt built with a secret-shaped field never emits the value.

    Redaction happens on the way out, so it covers whatever the caller stuffed
    into the grant/candidate/check mappings — not merely the fields this module
    happens to populate itself.
    """
    receipt = TrialReceipt(
        trial_id="t",
        experiment_id="e",
        admission="refused",
        started_at=FIXED,
        finished_at=None,
        candidate={"name": "x"},
        grant={"api_token": "supersecret", "experiment_id": "e"},
        findings=(),
        commands=(),
        effects=(),
        checks=({"name": "auth", "authorization": "Bearer supersecret"},),
        versions={},
        cost={},
        cleanup={"attempted": False, "succeeded": None},
        refusal_reason="test",
    )
    blob = json.dumps(receipt.to_dict())
    assert "supersecret" not in blob
    assert blob.count("redacted") >= 2


def test_a_receipt_that_ran_must_record_when_it_finished():
    with pytest.raises(ExperimentError):
        TrialReceipt(
            trial_id="t",
            experiment_id="e",
            admission="completed",
            started_at=FIXED,
            finished_at=None,
            candidate={},
            grant={},
            findings=(),
            commands=(),
            effects=(),
            checks=(),
            versions={},
            cost={},
            cleanup={"succeeded": True},
        )


def test_a_completed_receipt_requires_successful_cleanup():
    with pytest.raises(ExperimentError, match="successful cleanup"):
        TrialReceipt(
            trial_id="t",
            experiment_id="e",
            admission="completed",
            started_at=FIXED,
            finished_at=FIXED,
            candidate={},
            grant={},
            findings=(),
            commands=(),
            effects=(),
            checks=(),
            versions={},
            cost={},
            cleanup={"succeeded": False},
        )


def test_receipt_digest_is_stable(tmp_path):
    receipt = lab(tmp_path).run_trial(spec())
    assert receipt.digest() == receipt.digest()


def test_rendered_receipts_are_json_with_sorted_keys(tmp_path):
    real = lab(tmp_path)
    real.run_trial(spec())
    text = real.render_receipts()
    payload = json.loads(text)
    assert list(payload.keys()) == sorted(payload.keys())
    assert payload["lab_grants"] == {
        "production_activation": False,
        "global_capability": False,
        "host_fallback": False,
    }


# --------------------------------------------------------------- cleanup paths


def test_quarantine_records_a_marker_and_cites_the_receipt(tmp_path):
    real = lab(tmp_path)
    real.run_trial(spec())
    payload = real.quarantine("trial-1", reason="suspicious network attempt")
    assert real.is_quarantined("trial-1") is True
    assert payload["receipt_digest"]
    assert real.is_quarantined("other") is False


def test_quarantine_of_an_unknown_trial_is_refused(tmp_path):
    with pytest.raises(ExperimentError, match="no receipt"):
        lab(tmp_path).quarantine("nope", reason="x")


# --------------------------------------------------------------- AC6: promotion


def test_a_successful_trial_does_not_promote_itself(tmp_path):
    real = lab(tmp_path)
    receipt = real.run_trial(spec())
    assert receipt.admission == "completed"
    assert real.promotions == []
    assert receipt.to_dict()["grants_global_capability"] is False


def test_promotion_requires_owner_approval(tmp_path):
    with pytest.raises(ExperimentError, match="owner approval"):
        PromotionRecord(
            trial_id="t",
            capability="c",
            promoted_by="agent",
            promoted_at=FIXED,
            owner_approved=False,
        )


def test_only_a_completed_trial_can_be_promoted(tmp_path):
    real = lab(tmp_path, runner=FakeRunner(succeeded=False))
    real.run_trial(spec())
    with pytest.raises(ExperimentError, match="only a completed trial"):
        real.record_promotion(
            "trial-1", capability="c", promoted_by="owner", owner_approved=True
        )


def test_recorded_promotion_names_the_owner_and_scope(tmp_path):
    real = lab(tmp_path)
    real.run_trial(spec())
    record = real.record_promotion(
        "trial-1", capability="pdf-parsing", promoted_by="owner", owner_approved=True
    )
    payload = record.to_dict()
    assert payload["owner_approved"] is True
    assert payload["scope"] == "task-local"
    assert real.promotions == [record]


# --------------------------------------------------------------- integrity


def test_running_without_a_runner_is_a_configuration_error(tmp_path):
    real = Lab(
        grant=grant(),
        qualification=qualified(),
        runner=None,
        root=tmp_path,
        clock=clock,
    )
    with pytest.raises(ExperimentError, match="does not execute anything itself"):
        real.run_trial(spec())


def test_bad_trial_id_is_refused():
    with pytest.raises(ExperimentError):
        spec(trial_id="../escape")


def test_missing_task_id_is_refused():
    with pytest.raises(ExperimentError):
        spec(task_id=" ")


def test_unknown_qualification_status_is_refused():
    with pytest.raises(ExperimentError):
        HostQualification(status="probably_fine")
