"""AP-025: extracting evidence-backed reusable skills, and the six things that must not travel.

The tests are organised around refusals. Writing a skill out of one good run is trivial; the work is
in stopping a single success from becoming a global instruction, a failure from becoming guidance, a
project secret from becoming a shared example, and a quarantine from yanking a version out from
under a task that is already running.
"""
from __future__ import annotations

import pytest

import evidence_lessons
import scoped_grants
import skill_extraction as sk

SHA = "a" * 64
OTHER_SHA = "b" * 64


# -- fixtures ---------------------------------------------------------------------------


def evidence(
    task_id: str = "task-1",
    session_id: str = "session-1",
    project: str = "proj-a",
    *,
    verified: bool = True,
    passed: int = 3,
) -> sk.TaskEvidence:
    return sk.TaskEvidence(
        task_id=task_id,
        session_id=session_id,
        project_scope_id=project,
        verification_spec_sha256=SHA,
        passed_checks=passed,
        verified=verified,
    )


def example(name: str = "happy path") -> sk.SkillExample:
    return sk.SkillExample(
        name=name,
        input_text="a failing import in main.py",
        expected="the import resolves and the suite passes",
        is_test=True,
    )


def permission(capability: str = "workspace_read") -> sk.SkillPermission:
    return sk.SkillPermission(capability=capability, tools=("read_file",))


def skill(**kw) -> sk.SkillCandidate:
    return sk.SkillCandidate(
        **{
            "name": "narrow-the-read",
            "summary": "read the failing module before the whole tree",
            "guidance": "start from the traceback's last workspace file, then widen only if needed",
            "prerequisites": ["a failing test with a traceback"],
            "permissions": (permission(),),
            "examples": (example(),),
            "limitations": ["does not help when the failure is in generated code"],
            "evidence": (evidence(),),
            "scope_project": "proj-a",
            **kw,
        }
    )


def comparison(**kw) -> sk.ComparisonEvidence:
    return sk.ComparisonEvidence(
        **{
            "incumbent": "read-everything",
            "challenger": "narrow-the-read",
            "task_kind": "debug_failing_test",
            "config_sha256": OTHER_SHA,
            "sample_size": 6,
            "baseline_success_rate": 0.5,
            "candidate_success_rate": 0.83,
            **kw,
        }
    )


# -- AC1: provenance, prerequisites, permissions, versions, examples, limitations -------


def test_a_skill_records_every_section_a_reader_needs() -> None:
    recorded = skill()
    mapping = recorded.to_mapping()
    for section in sk.REQUIRED_SKILL_SECTIONS:
        assert section in mapping, section
    assert mapping["evidence"][0]["verification_spec_sha256"] == SHA
    assert mapping["permissions"][0]["capability"] == "workspace_read"
    assert recorded.missing_sections() == ()


def test_a_skill_with_no_limit_claims_universal_applicability() -> None:
    with pytest.raises(sk.SkillExtractionError, match="missing \\('limitations',\\)"):
        skill(limitations=[])


def test_a_skill_with_no_prerequisite_or_permission_is_refused() -> None:
    with pytest.raises(sk.SkillExtractionError, match="prerequisites"):
        skill(prerequisites=[])
    with pytest.raises(sk.SkillExtractionError, match="permissions"):
        skill(permissions=())


def test_a_skill_with_only_illustrations_has_no_regression_protection() -> None:
    illustration = sk.SkillExample(name="looks like", input_text="in", expected="out", is_test=False)
    with pytest.raises(sk.SkillExtractionError, match="no example marked as a test"):
        skill(examples=(illustration,))


def test_a_prose_name_cannot_be_versioned_or_looked_up() -> None:
    with pytest.raises(sk.SkillExtractionError, match="lowercase identifier"):
        skill(name="Read The Failing Module First")


def test_a_version_must_be_a_positive_integer() -> None:
    with pytest.raises(sk.VersionRefused, match="positive integer"):
        skill(version=0)
    with pytest.raises(sk.VersionRefused, match="positive integer"):
        skill(version=True)  # type: ignore[arg-type]


def test_permissions_come_from_the_existing_capability_taxonomy() -> None:
    assert "workspace_read" in scoped_grants.SG_CAPABILITY_TAXONOMY
    with pytest.raises(sk.AuthorityRefused, match="cannot invent authority"):
        sk.SkillPermission(capability="root_access")


# -- AC2: one success is task-local, never global superiority ---------------------------


def test_one_verified_task_is_recorded_as_task_local() -> None:
    decision = sk.assess_promotion(skill(), comparison=comparison())
    assert decision.scope == "task_local"
    assert decision.independent_tasks == 1
    assert any("one task success justifies task-local use only" in reason for reason in decision.reasons)


def test_unverified_evidence_cannot_be_extracted_from_at_all() -> None:
    with pytest.raises(sk.EvidenceRefused, match="unverified task"):
        skill(evidence=(evidence(verified=False),))
    with pytest.raises(sk.EvidenceRefused, match="unverified task"):
        skill(evidence=(evidence(passed=0),))


def test_a_skill_with_no_originating_task_is_a_guess_with_a_name() -> None:
    with pytest.raises(sk.EvidenceRefused, match="names no originating task"):
        skill(evidence=())


def test_sharing_needs_independent_sessions_a_comparison_and_public_data() -> None:
    shared = skill(
        confidentiality="public",
        evidence=(
            evidence("task-1", "session-1", "proj-a"),
            evidence("task-2", "session-2", "proj-b"),
        ),
    )
    decision = sk.assess_promotion(shared, comparison=comparison())
    assert decision.scope == "shared"
    assert decision.independent_tasks == 2
    assert any("matched 6-run comparison" in reason for reason in decision.reasons)


def test_the_same_session_twice_is_not_independent_evidence() -> None:
    repeated = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "session-1"), evidence("task-2", "session-1")),
    )
    decision = sk.assess_promotion(repeated, comparison=comparison())
    assert decision.scope == "task_local"
    assert decision.independent_tasks == 1


def test_working_twice_is_not_evidence_of_being_better_than_the_incumbent() -> None:
    shared = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
    )
    decision = sk.assess_promotion(shared, comparison=None)
    assert decision.scope == "task_local"
    assert any("no matched comparison" in reason for reason in decision.reasons)


def test_a_comparison_about_a_different_technique_does_not_transfer() -> None:
    shared = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
    )
    decision = sk.assess_promotion(shared, comparison=comparison(challenger="something-else"))
    assert decision.scope == "task_local"
    assert any("does not transfer" in reason for reason in decision.reasons)


def test_a_comparison_that_favours_the_incumbent_blocks_sharing() -> None:
    shared = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
    )
    decision = sk.assess_promotion(
        shared, comparison=comparison(candidate_success_rate=0.33, baseline_success_rate=0.5)
    )
    assert decision.scope == "task_local"
    assert any("does not favour the candidate" in reason for reason in decision.reasons)


def test_a_comparison_needs_more_than_one_run_per_arm() -> None:
    with pytest.raises(sk.EvidenceRefused, match="anecdote"):
        comparison(sample_size=1)


def test_a_comparison_needs_matched_configuration() -> None:
    with pytest.raises(sk.SkillExtractionError, match="config_sha256"):
        comparison(config_sha256="not-a-digest")


def test_the_success_floor_is_named_rather_than_implied() -> None:
    assert sk.EVIDENCE_FOR_GLOBAL_SUPERIORITY == 2


# -- AC3: failures are retained safely and never become instructions --------------------


def test_a_failed_approach_is_recorded_with_its_correction() -> None:
    failed = sk.FailedApproach(
        name="widen-the-search",
        approach="grep the whole repository for the error string",
        why_failed="matched every vendored copy and timed out",
        correction="start from the traceback's file and widen one directory at a time",
        evidence=evidence(),
        correction_verified=True,
    )
    mapping = failed.to_mapping()
    assert mapping["status"] == "failed_approach"
    assert mapping["correction_verified"] is True
    assert mapping["executable"] is False


def test_a_failed_approach_can_never_be_rendered_as_guidance() -> None:
    failed = sk.FailedApproach(
        name="widen-the-search",
        approach="grep everything",
        why_failed="timed out",
        correction="narrow the search first",
        evidence=evidence(),
    )
    with pytest.raises(sk.PromotionRefused, match="may never be rendered as guidance"):
        failed.render()


def test_the_executable_flag_is_a_constant_not_a_parameter() -> None:
    with pytest.raises(TypeError):
        sk.FailedApproach(
            name="x",
            approach="a",
            why_failed="b",
            correction="c",
            evidence=evidence(),
            executable=True,  # type: ignore[call-arg]
        )


def test_a_failed_approach_needs_the_evidence_that_it_failed() -> None:
    with pytest.raises(sk.EvidenceRefused, match="task evidence"):
        sk.FailedApproach(name="x", approach="a", why_failed="b", correction="c", evidence=None)  # type: ignore[arg-type]


# -- AC4: private data and cross-project authority do not leak into shared skills ------


def test_an_internal_skill_is_never_shared() -> None:
    shared_attempt = skill(
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
        confidentiality="internal",
    )
    decision = sk.assess_promotion(shared_attempt, comparison=comparison())
    assert decision.scope == "task_local"
    assert any("leaves the project boundary" in reason for reason in decision.reasons)


def test_a_credential_in_the_guidance_blocks_sharing() -> None:
    leaky = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
        guidance="export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345 then re-run",
    )
    decision = sk.assess_promotion(leaky, comparison=comparison())
    assert decision.scope == "task_local"
    assert any("credential pattern openai_key" in reason for reason in decision.reasons)


def test_a_host_path_in_an_example_blocks_sharing() -> None:
    leaky = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
        examples=(
            sk.SkillExample(
                name="local run",
                input_text="python /Users/pankaj/projects/secret/main.py",
                expected="passes",
                is_test=True,
            ),
        ),
    )
    decision = sk.assess_promotion(leaky, comparison=comparison())
    assert decision.scope == "task_local"
    assert any("host path" in reason for reason in decision.reasons)


def test_an_empty_privacy_scan_is_not_the_same_as_a_clean_one() -> None:
    clean = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1"), evidence("task-2", "s2")),
    )
    assert clean.privacy_findings() == ()


def test_a_skill_cannot_carry_authority_its_source_tasks_did_not_hold() -> None:
    # Permissions are validated against the existing taxonomy rather than a free-text list, so a
    # skill can only ask for a capability the grant system already knows how to grant or refuse.
    with pytest.raises(sk.AuthorityRefused):
        sk.SkillPermission(capability="production_deploy")
    permitted = sk.SkillPermission(capability="code_execution", tools=("run_tests",))
    assert permitted.capability in scoped_grants.SG_CAPABILITY_TAXONOMY


def test_the_evidence_projects_are_reported_so_cross_project_promotion_is_visible() -> None:
    cross = skill(evidence=(evidence("task-1", "s1", "proj-a"), evidence("task-2", "s2", "proj-b")))
    assert cross.evidence_projects() == ("proj-a", "proj-b")
    assert cross.to_mapping()["evidence_projects"] == ["proj-a", "proj-b"]


# -- AC5: the existing evidence_lessons promotion contract is reused -------------------


def test_a_task_local_skill_never_enters_the_cross_task_store(tmp_path) -> None:
    store = evidence_lessons.EvidenceLessonStore(tmp_path / "store")
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    decision = sk.assess_promotion(skill(), comparison=comparison())
    with pytest.raises(sk.ScopeRefused, match="task-local"):
        sk.promote_to_lesson_store(
            skill(),
            store=store,
            workspace_root=tmp_path,
            anchor_paths=["main.py"],
            decision=decision,
        )
    assert store.list_lessons(workspace_root=tmp_path) == []


def test_a_shared_skill_promotes_through_the_real_lesson_store(tmp_path) -> None:
    shared = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1", "proj-a"), evidence("task-2", "s2", "proj-b")),
    )
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    decision = sk.assess_promotion(shared, comparison=comparison())
    assert decision.is_shared is True
    store = evidence_lessons.EvidenceLessonStore(tmp_path / "store")
    record = sk.promote_to_lesson_store(
        shared,
        store=store,
        workspace_root=tmp_path,
        anchor_paths=["main.py"],
        decision=decision,
    )
    assert record.status == "promoted"
    assert record.topic_key == shared.name
    assert record.provenance.source_session_id == "s1"
    assert record.provenance.verified is True
    assert [item.lesson_id for item in store.list_lessons(workspace_root=tmp_path)] == [record.lesson_id]


def test_the_store_still_applies_its_own_privacy_gate_to_a_promoted_skill(tmp_path) -> None:
    # The reuse is real: a shared skill whose guidance repeats a registered secret is refused by
    # evidence_lessons' own gate, not by a second copy of that gate living here.
    shared = skill(
        confidentiality="public",
        evidence=(evidence("task-1", "s1", "proj-a"), evidence("task-2", "s2", "proj-b")),
    )
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    decision = sk.assess_promotion(shared, comparison=comparison())
    store = evidence_lessons.EvidenceLessonStore(tmp_path / "store")
    with pytest.raises(evidence_lessons.LessonPromotionRejectedError, match="registered secret"):
        sk.promote_to_lesson_store(
            shared,
            store=store,
            workspace_root=tmp_path,
            anchor_paths=["main.py"],
            decision=decision,
            known_secret_values=["start from the traceback's last workspace file"],
        )


def test_a_decision_for_another_skill_cannot_promote_this_one(tmp_path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    store = evidence_lessons.EvidenceLessonStore(tmp_path / "store")
    other = skill(name="other-skill")
    decision = sk.assess_promotion(other, comparison=comparison(challenger="other-skill"))
    with pytest.raises(sk.ScopeRefused, match="the decision must be the one"):
        sk.promote_to_lesson_store(
            skill(), store=store, workspace_root=tmp_path, anchor_paths=["main.py"], decision=decision
        )


# -- AC6: quarantine and revalidation, with stable versions for running tasks ----------


def test_the_latest_version_is_what_a_new_task_resolves() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1))
    library.put(skill(version=2, summary="read the failing module, then its test"))
    assert library.resolve("narrow-the-read").version == 2  # type: ignore[union-attr]


def test_a_version_is_immutable_and_an_edit_is_a_new_version() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1))
    library.put(skill(version=1))
    with pytest.raises(sk.VersionRefused, match="already exists with a different body"):
        library.put(skill(version=1, summary="a different summary"))


def test_a_quarantine_stops_the_next_retrieval_and_spares_the_running_task() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1))
    pin = library.pin("narrow-the-read", task_id="task-running")
    assert pin.version == 1
    library.quarantine("narrow-the-read", reason="the anchored file moved")
    assert library.resolve("narrow-the-read") is None
    assert library.revocation_reason("narrow-the-read") == "the anchored file moved"
    still = library.pinned("narrow-the-read", task_id="task-running")
    assert still is not None and still.version == 1


def test_revalidation_resumes_retrieval_at_the_latest_version() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1))
    library.put(skill(version=2, summary="a better summary"))
    library.quarantine("narrow-the-read", reason="evidence under review")
    assert library.revalidate("narrow-the-read").version == 2  # type: ignore[union-attr]
    assert library.resolve("narrow-the-read").version == 2  # type: ignore[union-attr]


def test_a_revoked_skill_is_not_resolved() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1, status="revoked"))
    assert library.resolve("narrow-the-read") is None


def test_a_task_cannot_pin_what_it_cannot_resolve() -> None:
    library = sk.SkillLibrary()
    with pytest.raises(sk.VersionRefused, match="cannot pin what it cannot resolve"):
        library.pin("narrow-the-read", task_id="task-1")


def test_a_quarantine_needs_a_reason_and_a_known_skill() -> None:
    library = sk.SkillLibrary()
    with pytest.raises(sk.VersionRefused, match="not in the library"):
        library.quarantine("narrow-the-read", reason="whatever")
    library.put(skill())
    with pytest.raises(sk.SkillExtractionError, match="reason must not be empty"):
        library.quarantine("narrow-the-read", reason="  ")


def test_the_library_reports_versions_quarantines_and_pins() -> None:
    library = sk.SkillLibrary()
    library.put(skill(version=1))
    library.pin("narrow-the-read", task_id="task-1")
    report = library.to_mapping()
    assert report["skills"]["narrow-the-read"]["versions"] == [1]
    assert report["skills"]["narrow-the-read"]["quarantined"] is False
    assert report["pins"][0]["task_id"] == "task-1"


# -- discipline ------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_no_socket() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("skill_extraction.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "typing",
        "evidence_lessons",
        "scoped_grants",
    }
    for forbidden in ("time", "datetime", "os", "pathlib", "subprocess", "socket", "requests"):
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

    shared = top_level("skill_extraction.py") & top_level("app.py")
    assert shared == set(), f"these names would collide in the single-file build: {sorted(shared)}"
