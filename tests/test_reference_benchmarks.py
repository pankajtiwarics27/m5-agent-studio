"""AP-020: versioned owner-approved reference benchmarks.

These tests are organised by what they stop, because a reference system fails by *permitting*
too much rather than by crashing: a machine-written approval, a widening scope, a rewritten
acceptance, or a style match being mistaken for evidence that new code works.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import reference_benchmarks as rb

SHA_A = "a" * 64
SHA_B = "b" * 64


def _artifact(sha: str = SHA_A, **overrides) -> rb.ArtifactVersion:
    base = dict(
        artifact_id="hero.html",
        content_sha256=sha,
        measured_checks={"layout": True, "overflow": True},
    )
    base.update(overrides)
    return rb.ArtifactVersion(**base)


def _approval(**overrides) -> rb.OwnerApproval:
    base = dict(
        actor="owner:pankaj",
        statement="this is the calm, plain tone I meant",
        granted=("use_as_reference", "final_acceptance"),
    )
    base.update(overrides)
    return rb.OwnerApproval(**base)


def _reference(**overrides) -> rb.Reference:
    base = dict(
        reference_id="ref/website",
        version=1,
        project_scope_id="proj-1",
        task_kind="website",
        brief="a landing page for the ledger app, for consumers",
        artifact=_artifact(),
        approval=_approval(),
        audience="consumer",
        language="en",
        style_tags=("calm", "plain"),
        checks=("layout",),
    )
    base.update(overrides)
    return rb.accept(**base)


# ── AC1: original brief, exact artifact, provenance, scope, checks, configuration ─────────


def test_a_reference_records_the_brief_artifact_provenance_scope_and_checks():
    record = _reference(
        artifact=_artifact(
            identity_sha256=SHA_B,
            identity_basis="content+provenance",
            producer="website-builder",
            producer_version="3.2.0",
            config_sha256="c" * 64,
        )
    )
    mapping = record.to_mapping()
    assert mapping["brief"].startswith("a landing page")
    assert mapping["artifact"]["content_sha256"] == SHA_A
    assert mapping["artifact"]["identity_basis"] == "content+provenance"
    assert mapping["artifact"]["producer"] == "website-builder"
    assert mapping["artifact"]["producer_version"] == "3.2.0"
    assert mapping["artifact"]["config_sha256"] == "c" * 64
    assert mapping["approval"]["actor"] == "owner:pankaj"
    assert mapping["approval"]["statement"] == "this is the calm, plain tone I meant"
    assert mapping["checks"] == ["layout"]
    assert mapping["project_scope_id"] == "proj-1"


def test_a_content_only_identity_never_presents_itself_as_a_full_one():
    record = _reference()
    # The record is entitled to say "these are the bytes". It is not entitled to claim producer
    # provenance it was never given, and the basis field is what carries that distinction.
    assert record.artifact.identity_basis == "content_only"
    assert record.artifact.identity_sha256 == ""


def test_a_provenance_identity_without_its_digest_is_refused():
    with pytest.raises(rb.ReferenceError, match="must carry its identity digest"):
        rb.ArtifactVersion(
            artifact_id="x.html",
            content_sha256=SHA_A,
            identity_basis="content+provenance",
        )


def test_a_check_that_was_never_measured_cannot_be_claimed():
    with pytest.raises(rb.ReferenceError, match="was not measured and passed"):
        _reference(checks=("layout", "a11y"))


def test_a_reference_cannot_point_at_an_artifact_with_no_digest():
    with pytest.raises(rb.ReferenceError, match="content_sha256"):
        rb.ArtifactVersion(artifact_id="x.html", content_sha256="not-a-digest")


# ── AC2: the four grants are separate ───────────────────────────────────────────────────


def test_approving_a_reference_is_not_approving_the_artifact_or_publishing_it():
    record = _reference()
    assert record.grants_publish_permission is False
    assert record.is_final_acceptance is False
    mapping = record.to_mapping()
    assert mapping["grants_publish_permission"] is False
    assert mapping["is_final_acceptance"] is False


def test_an_approval_that_accepted_the_artifact_but_did_not_adopt_it_is_refused():
    """The most tempting shortcut: "the owner already said they liked it"."""
    with pytest.raises(rb.ApprovalRefused, match="use_as_reference"):
        _reference(approval=_approval(granted=("final_acceptance",)))


def test_accepting_a_reference_does_not_grant_continuing_or_publishing():
    record = _reference()
    assert record.approval.grants("use_as_reference") is True
    # The approval recorded exactly what it granted; nothing else became true by implication.
    assert record.approval.granted == ("use_as_reference", "final_acceptance")
    assert record.approval.grants("publish") is False


def test_the_four_grants_are_distinct_names():
    assert len(set(rb.APPROVAL_KINDS)) == 4
    assert "publish" in rb.APPROVAL_KINDS and "use_as_reference" in rb.APPROVAL_KINDS


# ── AC5: an agent cannot synthesize owner approval ──────────────────────────────────────


@pytest.mark.parametrize(
    "actor", ["agent", "system", "automation", "assistant", "model", "scheduler"]
)
def test_a_machine_actor_cannot_approve_anything(actor):
    with pytest.raises(rb.ApprovalRefused, match="not a person"):
        _approval(actor=actor)


def test_an_approval_must_be_namespaced_as_a_person():
    with pytest.raises(rb.ApprovalRefused, match="namespaced"):
        _approval(actor="pankaj")


def test_an_approval_must_carry_what_the_owner_actually_said():
    with pytest.raises(rb.ReferenceError, match="approval statement must not be empty"):
        _approval(statement="   ")


def test_an_approval_must_grant_something():
    with pytest.raises(rb.ApprovalRefused, match="at least one thing"):
        _approval(granted=())


def test_an_unknown_grant_is_refused_rather_than_stored():
    with pytest.raises(rb.ApprovalRefused, match="unknown approval kind"):
        _approval(granted=("looks_good",))


def test_a_stored_record_claiming_publish_permission_is_refused_at_read_time():
    stored = _reference().to_mapping()
    stored["grants_publish_permission"] = True
    with pytest.raises(rb.ReferenceError, match="grants_publish_permission"):
        rb.Reference.from_mapping(stored)


def test_a_stored_record_claiming_final_acceptance_is_refused_at_read_time():
    stored = _reference().to_mapping()
    stored["is_final_acceptance"] = True
    with pytest.raises(rb.ReferenceError, match="is_final_acceptance"):
        rb.Reference.from_mapping(stored)


def test_a_stored_record_with_a_machine_approver_is_refused_at_read_time():
    stored = _reference().to_mapping()
    stored["approval"]["actor"] = "agent"
    with pytest.raises(rb.ApprovalRefused, match="not a person"):
        rb.Reference.from_mapping(stored)


def test_security_and_screenshot_approval_are_named_as_things_a_reference_cannot_give():
    assert "security_sign_off" in rb.REFERENCE_CANNOT_SETTLE
    assert "screenshot_approval" in rb.REFERENCE_CANNOT_SETTLE


# ── AC3: enhancements create a new version; prior acceptance is never rewritten ─────────


def test_enhancing_creates_a_new_version_with_its_own_approval():
    first = _reference()
    second = rb.enhance(
        first,
        new_version=2,
        artifact=_artifact(SHA_B),
        approval=_approval(
            actor="owner:pankaj", statement="the second draft is the one"
        ),
    )
    assert second.version == 2
    assert second.artifact.content_sha256 == SHA_B
    assert second.supersedes == "ref/website@v1"
    # The previous version is untouched: same artifact, same approval, still active.
    assert first.version == 1
    assert first.status == "active"
    assert first.artifact.content_sha256 == SHA_A


def test_a_new_version_must_be_greater_than_the_one_it_replaces():
    with pytest.raises(rb.ReferenceError, match="must be greater"):
        rb.enhance(
            _reference(version=2),
            new_version=2,
            artifact=_artifact(SHA_B),
            approval=_approval(),
        )


def test_superseding_links_forward_without_altering_the_earlier_acceptance():
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    retired = rb.supersede(first, second)

    assert retired.status == "superseded"
    assert retired.superseded_by == "ref/website@v2"
    # Everything an owner decided about v1 is exactly as it was.
    assert retired.approval.to_mapping() == first.approval.to_mapping()
    assert retired.artifact.to_mapping() == first.artifact.to_mapping()
    assert retired.brief == first.brief


def test_a_link_forward_that_is_already_recorded_does_not_move():
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    retired = rb.supersede(first, second)
    with pytest.raises(rb.AcceptanceRewriteRefused, match="does not move"):
        rb.supersede(retired, second)


def test_a_superseded_version_cannot_be_enhanced_again():
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    retired = rb.supersede(first, second)
    with pytest.raises(rb.ReferenceError, match="only an active version"):
        rb.enhance(retired, new_version=3, artifact=_artifact(), approval=_approval())


def test_revoking_keeps_the_record_and_names_who_did_it():
    revoked = rb.revoke(
        _reference(), reason="the audience changed", actor="owner:pankaj"
    )
    assert revoked.status == "revoked"
    assert revoked.revocation_reason == "the audience changed"
    assert revoked.artifact.content_sha256 == SHA_A


def test_a_revocation_must_name_a_person():
    with pytest.raises(rb.ApprovalRefused, match="names the person"):
        rb.revoke(_reference(), reason="changed", actor="agent")


# ── storage: the rewrite rule holds at the boundary, not only in the description ────────


def test_the_store_keeps_every_version(tmp_path):
    store = rb.ReferenceStore(tmp_path / "refs.db")
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    store.put(first)
    store.put(second)
    store.put(rb.supersede(first, second))

    history = store.history("ref/website")
    assert [row.version for row in history] == [1, 2]
    assert history[0].status == "superseded"
    assert history[1].status == "active"


def test_the_store_refuses_to_rewrite_an_acceptance(tmp_path):
    import dataclasses

    store = rb.ReferenceStore(tmp_path / "refs.db")
    store.put(_reference())
    changed = dataclasses.replace(
        _reference(),
        approval=_approval(statement="actually I never said that"),
    )
    with pytest.raises(rb.AcceptanceRewriteRefused, match="not amended in place"):
        store.put(changed)


def test_the_store_refuses_to_redirect_an_accepted_version(tmp_path):
    import dataclasses

    store = rb.ReferenceStore(tmp_path / "refs.db")
    store.put(_reference())
    redirected = dataclasses.replace(_reference(), artifact=_artifact(SHA_B))
    with pytest.raises(rb.AcceptanceRewriteRefused, match="different artifact"):
        store.put(redirected)


def test_the_store_lists_only_live_references_when_asked(tmp_path):
    store = rb.ReferenceStore(tmp_path / "refs.db")
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    store.put(rb.supersede(first, second))
    store.put(second)

    live = store.list(status="active")
    assert [(row.reference_id, row.version) for row in live] == [("ref/website", 2)]
    assert len(store.list()) == 2


# ── AC4: retrieval by audience/language/task/style, never forcing identical layouts ─────


def test_retrieval_prefers_a_reference_for_the_same_audience_language_and_task():
    matching = _reference()
    other = _reference(
        reference_id="ref/other",
        audience="internal",
        language="hi",
        style_tags=("formal",),
    )
    ranked = rb.rank(
        [other, matching],
        task_kind="website",
        audience="consumer",
        language="en",
        style_tags=["calm"],
    )
    assert ranked[0][0].reference_id == "ref/website"
    assert "same task kind" in ranked[0][1]
    assert "same audience" in ranked[0][1]
    assert any(
        "different language" in reason for _ref, reasons in ranked for reason in reasons
    )


def test_a_reference_for_another_language_is_labelled_rather_than_silently_offered():
    only = _reference(language="hi", audience="consumer")
    ranked = rb.rank([only], task_kind="website", audience="consumer", language="en")
    assert len(ranked) == 1
    assert any("different language (hi)" in reason for reason in ranked[0][1])


def test_retrieval_never_scores_layout_because_layout_must_not_be_copied():
    record = _reference(style_tags=("calm",), checks=("layout",))
    ranked = rb.rank([record], task_kind="website", style_tags=["calm"])
    reasons = " ".join(ranked[0][1]).lower()
    assert "layout" not in reasons
    assert "shared style: calm" in reasons


def test_retrieval_skips_revoked_and_superseded_versions():
    live = _reference(version=2)
    revoked = rb.revoke(
        _reference(version=1),
        reason="superseded by the calmer draft",
        actor="owner:pankaj",
    )
    ranked = rb.rank([revoked, live], task_kind="website")
    assert [row[0].version for row in ranked] == [2]


def test_the_current_version_of_each_reference_is_available():
    first = _reference()
    second = rb.enhance(
        first, new_version=2, artifact=_artifact(SHA_B), approval=_approval()
    )
    current = rb.latest_by_reference([first, second])
    assert current["ref/website"].version == 2


# ── AC6: changed facts, code or assets still need their own verification ───────────────


def test_a_style_reference_does_not_settle_changed_code():
    required = rb.verification_still_required(changed=["code"])
    assert "changed_code_works" in required
    assert "final_acceptance_of_new_work" in required
    assert "publish_permission" in required


def test_changed_facts_and_assets_carry_their_own_obligations():
    required = rb.verification_still_required(changed=["facts", "assets"])
    assert "changed_facts_are_true" in required
    assert "changed_assets_are_licensed" in required


def test_even_an_unrelated_change_still_requires_acceptance_and_publish_permission():
    required = rb.verification_still_required(changed=[])
    assert required == ("final_acceptance_of_new_work", "publish_permission")


def test_coverage_names_the_checks_it_does_not_settle():
    covered, missing = _reference().covers(
        task_kind="website", checks=("layout", "a11y")
    )
    assert covered is False
    assert missing == ("check:a11y",)


def test_coverage_requires_the_same_task_kind():
    covered, missing = _reference().covers(task_kind="document")
    assert covered is False
    assert missing == ("task_kind:document",)


def test_everything_a_reference_cannot_settle_is_named_in_one_place():
    for name in ("changed_code_works", "changed_facts_are_true", "security_sign_off"):
        assert name in rb.REFERENCE_CANNOT_SETTLE


# ── discipline: this module is a decision module, like its AP-018 sibling ──────────────


def test_the_module_reads_no_clock_and_opens_nothing():
    """A decision module that consults the wall clock cannot be replayed against a record."""
    source = Path("reference_benchmarks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {
        "time",
        "datetime",
        "os",
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "httpx",
    }
    # sqlite3 and pathlib are permitted only inside the store, which is where persistence lives.
    body_top = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    top_level = set()
    for node in body_top:
        if isinstance(node, ast.Import):
            top_level |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert "sqlite3" not in top_level
    assert "pathlib" not in top_level
    assert "json" in top_level and "hashlib" in top_level


def test_this_module_shares_no_top_level_name_with_app_py():
    """A collision in the single-file build once replaced the app's own SCHEMA_VERSION."""
    app = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    module = ast.parse(Path("reference_benchmarks.py").read_text(encoding="utf-8"))

    def names(tree: ast.Module) -> set[str]:
        found: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                found |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                found.add(node.name)
        return found

    clash = names(app) & names(module)
    assert not clash, (
        f"these names would overwrite app.py's in the single-file build: {sorted(clash)}"
    )


def test_a_record_round_trips_through_its_stored_form():
    record = _reference()
    restored = rb.Reference.from_mapping(record.to_mapping())
    assert restored.to_mapping() == record.to_mapping()
    assert restored.fingerprint() == record.fingerprint()


def test_a_stored_record_from_a_future_schema_is_refused():
    stored = _reference().to_mapping()
    stored["schema_version"] = rb.RB_SCHEMA_VERSION + 1
    with pytest.raises(rb.ReferenceError, match="schema"):
        rb.Reference.from_mapping(stored)
