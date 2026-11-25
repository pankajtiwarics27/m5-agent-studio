"""AP-026: a budgeted improvement lab, and the seven ways it must refuse to become one.

The tests are organised around refusals, because "try an improvement" is easy and every expensive
failure here is a quiet one: a watcher nobody granted, a candidate called "latest", an experiment
with no problem behind it, a trial that starts before compatibility is checked, an optimiser that
edits its own exam, a weight-training path nobody authorised, and an optimisation that quietly
depends on someone else's cloud.
"""
from __future__ import annotations

import inspect

import pytest

import improvement_lab as il

SHA = "a" * 64
OTHER_SHA = "b" * 64


# -- fixtures ---------------------------------------------------------------------------


def grant(**kw) -> il.WatchGrant:
    return il.WatchGrant(
        **{
            "granted_by": "owner:pankaj",
            "purpose": "watch two repositories for releases relevant to local inference",
            "hosts": ("github.com", "pypi.org"),
            "max_requests": 6,
            "expires_on": "2026-12-31",
            **kw,
        }
    )


def card(**kw) -> il.CandidateCard:
    return il.CandidateCard(
        **{
            "name": "kv-cache-offload",
            "kind": "repo",
            "version": "v0.4.2",
            "source_url": "https://github.com/example/kv-cache-offload/tree/v0.4.2",
            "accessed_on": "2026-11-20",
            "license_id": "Apache-2.0",
            "limitations": ("needs a discrete GPU for the offload path",),
            **kw,
        }
    )


def hypothesis(**kw) -> il.Hypothesis:
    return il.Hypothesis(
        **{
            "statement": "a narrower retrieval window raises first-attempt success on failing imports",
            "problem_trace_ids": ("trace-17", "trace-22"),
            "metric": "first_attempt_success_rate",
            "baseline": 0.42,
            "threshold": 0.6,
            **kw,
        }
    )


def compatibility(**kw) -> il.CompatibilityCheck:
    return il.CompatibilityCheck(
        **{
            "runtime": "python-3.11",
            "runtime_installed": True,
            "declared_download_mb": 120.0,
            "download_cap_mb": 500.0,
            "disk_free_mb": 20_000.0,
            "memory_required_mb": 2_048.0,
            "memory_available_mb": 16_384.0,
            "license_compatible": True,
            **kw,
        }
    )


def canonical_fingerprint(mapping) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def guard(**kw) -> il.HeldOutGuard:
    return il.HeldOutGuard(
        **{
            "held_out_tasks_sha256": SHA,
            "evaluator_sha256": SHA,
            "authority_controls_sha256": SHA,
            "paths": ("benchmarks/held-out", "scripts/evaluate.py"),
            **kw,
        }
    )


def experiment(**kw) -> il.Experiment:
    return il.Experiment(
        **{
            "hypothesis": hypothesis(),
            "surface": "prompt",
            "candidate": card(kind="prompt"),
            "compatibility": compatibility(),
            "guard": guard(),
            "max_trials": 2,
            **kw,
        }
    )


class FakeFetcher:
    def __init__(self, entries=None):
        self.entries = entries or [{"name": "release-1"}]
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        return {"status": 200, "body": "ok"}

    def list_dir(self, url):
        self.calls.append(url)
        return self.entries


# -- AC1: no implicit or background egress ---------------------------------------------


def test_a_schedule_cannot_exist_without_a_grant() -> None:
    with pytest.raises(il.EgressRefused, match="needs a WatchGrant"):
        il.WatchSchedule(grant=None, locations=("https://github.com/x/y",))  # type: ignore[arg-type]


def test_constructing_a_watcher_makes_no_request() -> None:
    watcher = il.Watcher(schedules=(il.WatchSchedule(grant=grant(), locations=("https://github.com/x/y",)),))
    assert watcher.to_mapping()["starts_itself"] is False
    assert watcher.to_mapping()["background_threads"] == 0
    assert not hasattr(watcher, "start")
    assert not hasattr(il.WatchSchedule, "daemon")


def test_a_grant_by_a_machine_is_refused() -> None:
    with pytest.raises(il.EgressRefused, match="not a named person"):
        grant(granted_by="agent:planner")
    with pytest.raises(il.EgressRefused, match="not a named person"):
        grant(granted_by="owner:")


def test_a_grant_cannot_widen_egress_past_the_public_allowlist() -> None:
    with pytest.raises(il.EgressRefused, match="cannot widen egress"):
        grant(hosts=("github.com", "malware.example"))


def test_a_location_outside_the_granted_hosts_is_refused() -> None:
    with pytest.raises(il.EgressRefused, match="outside the granted hosts"):
        il.WatchSchedule(grant=grant(hosts=("github.com",)), locations=("https://pypi.org/simple",))


def test_an_expired_grant_makes_no_requests() -> None:
    schedule = il.WatchSchedule(grant=grant(expires_on="2026-11-01"), locations=("https://github.com/x/y",))
    fetcher = FakeFetcher()
    with pytest.raises(il.EgressRefused, match="expired on 2026-11-01"):
        schedule.tick(now="2026-11-20", fetcher=fetcher)
    assert fetcher.calls == []


def test_the_request_cap_is_enforced_before_the_round_runs() -> None:
    schedule = il.WatchSchedule(
        grant=grant(max_requests=1),
        locations=("https://github.com/x/y", "https://pypi.org/simple"),
    )
    fetcher = FakeFetcher()
    with pytest.raises(il.EgressRefused, match="the cap is the cap"):
        schedule.tick(now="2026-11-20", fetcher=fetcher)
    assert fetcher.calls == []


def test_a_permitted_round_logs_what_it_fetched() -> None:
    schedule = il.WatchSchedule(grant=grant(), locations=("https://github.com/x/y",))
    fetcher = FakeFetcher()
    entries = schedule.tick(now="2026-11-20", fetcher=fetcher)
    assert fetcher.calls == ["https://github.com/x/y"]
    assert entries[0]["name"] == "release-1"
    assert schedule.to_mapping()["requests_made"] == 1
    assert schedule.to_mapping()["log"][0]["at"] == "2026-11-20"


def test_the_module_carries_no_network_or_background_machinery() -> None:
    il.assert_no_implicit_egress(inspect.getsource(il))


def test_every_location_must_be_a_named_url() -> None:
    with pytest.raises(il.EgressRefused, match="at least one location"):
        il.WatchSchedule(grant=grant(), locations=())


# -- AC2: candidates carry dates, exact versions, licences and limitations --------------


def test_a_candidate_records_version_date_licence_and_limits() -> None:
    mapping = card().to_mapping()
    assert mapping["version"] == "v0.4.2"
    assert mapping["accessed_on"] == "2026-11-20"
    assert mapping["license_id"] == "Apache-2.0"
    assert mapping["limitations"]
    assert mapping["triable_locally"] is True


def test_latest_is_not_a_version() -> None:
    for moving in ("latest", "main", "HEAD", "*"):
        with pytest.raises(il.CandidateRefused, match="moving target"):
            card(version=moving)


def test_a_candidate_with_no_stated_limits_is_a_recommendation() -> None:
    with pytest.raises(il.CandidateRefused, match="states no limitations"):
        card(limitations=())


def test_a_candidate_must_live_somewhere_this_programme_may_read() -> None:
    with pytest.raises(il.CandidateRefused, match="not on the public read allowlist"):
        card(source_url="https://blog.example/posts/new-thing")


def test_a_candidate_needs_a_real_access_date() -> None:
    with pytest.raises(il.ImprovementLabError, match="ISO date"):
        card(accessed_on="last week")
    with pytest.raises(il.ImprovementLabError, match="real calendar date"):
        card(accessed_on="2026-02-31")


def test_a_hosted_dependency_must_be_named_not_inferred() -> None:
    with pytest.raises(il.CandidateRefused, match="names no hosted_dependency"):
        card(summary="a wrapper around the OpenAI chat completions API")


def test_naming_the_dependency_makes_the_candidate_reference_only_in_effect() -> None:
    hosted = card(hosted_dependency="OpenAI chat completions")
    assert hosted.is_triable_locally is False
    assert hosted.to_mapping()["triable_locally"] is False


def test_a_reference_only_candidate_is_recorded_and_not_triable() -> None:
    reference = card(adaptation="reference_only", hosted_dependency="hosted reranker")
    assert reference.is_triable_locally is False


def test_a_local_adaptation_has_to_say_what_it_replaced() -> None:
    with pytest.raises(il.CandidateRefused, match="must say what was replaced"):
        card(adaptation="local_adaptation", hosted_dependency="")


# -- AC3: experiments start from a diagnosed problem ----------------------------------


def test_a_hypothesis_with_no_failing_trace_is_a_preference() -> None:
    with pytest.raises(il.HypothesisRefused, match="names no failing trace"):
        hypothesis(problem_trace_ids=())


def test_an_unfalsifiable_hypothesis_is_refused() -> None:
    with pytest.raises(il.HypothesisRefused, match="unfalsifiable"):
        hypothesis(threshold=0.42)
    with pytest.raises(il.HypothesisRefused, match="unfalsifiable"):
        hypothesis(direction="decrease", baseline=10.0, threshold=10.0)


def test_a_baseline_must_be_measured_not_described() -> None:
    with pytest.raises(il.HypothesisRefused, match="must be a number"):
        hypothesis(baseline="low")  # type: ignore[arg-type]


def test_a_decrease_hypothesis_has_to_fall_below_the_baseline() -> None:
    fewer = hypothesis(metric="steps_to_green", direction="decrease", baseline=9.0, threshold=6.0)
    assert fewer.is_met(5.5) is True
    assert fewer.is_met(7.0) is False


# -- AC4: compatibility and resource limits precede trials ---------------------------


def test_a_missing_runtime_stops_the_trial_before_it_starts() -> None:
    trialled: list[int] = []
    compatible = compatibility(runtime_installed=False)
    with pytest.raises(il.CompatibilityRefused, match="not installed"):
        il.run_experiment(
            experiment(compatibility=compatible),
            trialler=lambda e, i: trialled.append(i) or {"observed": 0.9, "passed": True},
            post_guard=guard(),
        )
    assert trialled == []


def test_a_download_larger_than_the_cap_or_the_disk_is_refused() -> None:
    over_cap = compatibility(declared_download_mb=900.0)
    assert any("exceeds the 500.0 MB cap" in reason for reason in over_cap.blocking_reasons())
    over_disk = compatibility(declared_download_mb=900.0, download_cap_mb=1000.0, disk_free_mb=100.0)
    assert any("free disk" in reason for reason in over_disk.blocking_reasons())


def test_memory_that_does_not_fit_is_refused() -> None:
    tight = compatibility(memory_required_mb=32_000.0)
    assert tight.fits is False
    assert any("only 16384.0 MB is available" in reason for reason in tight.blocking_reasons())


def test_the_fit_is_derived_rather_than_asserted() -> None:
    assert compatibility().fits is True
    with pytest.raises(TypeError):
        il.CompatibilityCheck(  # type: ignore[call-arg]
            runtime="x",
            runtime_installed=True,
            declared_download_mb=1.0,
            download_cap_mb=2.0,
            disk_free_mb=3.0,
            memory_required_mb=4.0,
            memory_available_mb=5.0,
            license_compatible=True,
            fits=True,
        )


# -- AC5: the optimiser cannot edit the exam ------------------------------------------


def test_a_bystander_cannot_be_asserted_unchanged() -> None:
    with pytest.raises(il.HeldOutRefused, match="the evaluator changed"):
        guard().assert_untouched(guard(evaluator_sha256=OTHER_SHA))


def test_a_removed_control_is_not_an_unchanged_control() -> None:
    with pytest.raises(il.HeldOutRefused, match="removed control"):
        guard().assert_untouched(guard(paths=("benchmarks/held-out",)))


def test_an_edited_evaluator_blocks_the_recommendation() -> None:
    report = il.ImprovementReport(
        experiment=experiment(),
        trials=(il.TrialResult(trial=1, observed=0.8, passed=True),),
        post_guard=guard(evaluator_sha256=OTHER_SHA),
    )
    assert report.recommendation() == "keep_incumbent"
    assert any("edits its own exam" in reason for reason in report.blocking_reasons())


def test_a_report_needs_the_post_trial_guard() -> None:
    with pytest.raises(il.HeldOutRefused, match="post-trial HeldOutGuard"):
        il.ImprovementReport(
            experiment=experiment(),
            trials=(il.TrialResult(trial=1, observed=0.8, passed=True),),
            post_guard=None,  # type: ignore[arg-type]
        )


def test_a_guard_digest_must_be_a_digest() -> None:
    with pytest.raises(il.ImprovementLabError, match="sha256"):
        guard(held_out_tasks_sha256="held-out-v1")


# -- AC6: memory/skill/config updates, never weight training ---------------------------


def test_weight_training_is_refused_by_name() -> None:
    for surface in il.REFUSED_SURFACES:
        with pytest.raises(il.TrainingRefused, match="no training path"):
            experiment(surface=surface, candidate=card())


def test_an_unknown_surface_cannot_be_optimised() -> None:
    with pytest.raises(il.TrainingRefused, match="not optimisable"):
        experiment(surface="vibes", candidate=card())


def test_the_allowed_surfaces_are_memory_shaped() -> None:
    assert il.OPTIMISATION_SURFACES == ("prompt", "skill", "config")
    for surface in il.OPTIMISATION_SURFACES:
        assert experiment(surface=surface, candidate=card(kind=surface)).surface == surface


def test_every_report_states_that_weights_did_not_change() -> None:
    report = il.ImprovementReport(
        experiment=experiment(),
        trials=(il.TrialResult(trial=1, observed=0.8, passed=True),),
        post_guard=guard(),
    )
    assert report.to_mapping()["weights_changed"] is False


# -- AC7: candidate generation, judging and inference stay local ----------------------


def test_a_hosted_candidate_never_reaches_the_trialler() -> None:
    trialled: list[int] = []
    hosted = card(kind="prompt", hosted_dependency="hosted judge API")
    with pytest.raises(il.LocalOnlyRefused, match="stay local"):
        il.run_experiment(
            experiment(candidate=hosted),
            trialler=lambda e, i: trialled.append(i) or {"observed": 0.9, "passed": True},
            post_guard=guard(),
        )
    assert trialled == []


def test_a_reference_only_candidate_cannot_be_trialled() -> None:
    reference = card(kind="prompt", adaptation="reference_only", hosted_dependency="hosted model")
    with pytest.raises(il.LocalOnlyRefused):
        il.run_experiment(
            experiment(candidate=reference),
            trialler=lambda e, i: {"observed": 0.9, "passed": True},
            post_guard=guard(),
        )


def test_a_local_candidate_runs_and_every_trial_is_reported() -> None:
    seen: list[int] = []

    def trialler(_experiment, index):
        seen.append(index)
        return {"observed": 0.55 + 0.1 * index, "passed": index == 2, "notes": f"run {index}"}

    report = il.run_experiment(experiment(), trialler=trialler, post_guard=guard())
    assert seen == [1, 2]
    assert [trial.trial for trial in report.trials] == [1, 2]
    assert report.recommendation() == "adopt_candidate"


def test_a_trialler_that_reports_no_number_is_refused() -> None:
    with pytest.raises(il.ImprovementLabError, match="numeric 'observed'"):
        il.run_experiment(
            experiment(max_trials=1),
            trialler=lambda e, i: {"observed": "better", "passed": True},
            post_guard=guard(),
        )


def test_more_trials_than_the_budget_is_refused() -> None:
    with pytest.raises(il.ImprovementLabError, match="budget that is not enforced"):
        il.ImprovementReport(
            experiment=experiment(max_trials=1),
            trials=(
                il.TrialResult(trial=1, observed=0.5, passed=True),
                il.TrialResult(trial=2, observed=0.9, passed=True),
            ),
            post_guard=guard(),
        )


def test_an_unmet_threshold_keeps_the_incumbent() -> None:
    report = il.ImprovementReport(
        experiment=experiment(),
        trials=(il.TrialResult(trial=1, observed=0.5, passed=False),),
        post_guard=guard(),
    )
    assert report.recommendation() == "keep_incumbent"


def test_every_experiment_reports_its_runs_and_cost() -> None:
    report = il.ImprovementReport(
        experiment=experiment(),
        trials=(il.TrialResult(trial=1, observed=0.9, passed=True),),
        post_guard=guard(),
        runs_evaluated=3,
        intervention_minutes=12.0,
        limitations=("one corpus", "one model"),
    )
    mapping = report.to_mapping()
    assert mapping["runs_evaluated"] == 3
    assert mapping["limitations"] == ["one corpus", "one model"]
    assert mapping["max_trials"] == 2
    assert canonical_fingerprint(mapping) == report.fingerprint()




# -- discipline -----------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("improvement_lab.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "ast",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "datetime",
        "typing",
        "autonomous_production_research",
    }
    for forbidden in ("time", "os", "pathlib", "subprocess", "socket", "threading", "requests", "asyncio"):
        assert forbidden not in imported


def test_the_module_shares_no_top_level_name_with_app() -> None:
    import ast
    import pathlib

    def top_level(path: str) -> set[str]:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        return names

    shared = top_level("improvement_lab.py") & top_level("app.py")
    assert shared == set(), f"these names would collide in the single-file build: {sorted(shared)}"
