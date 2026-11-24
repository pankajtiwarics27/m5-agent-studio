"""AP-019: a comment on one thing becomes a bounded revision, never a policy.

The behaviour worth pinning is the refusal to widen. An owner pointing at one region and
saying it is too loud must not end up rewriting the product, and must not end up as a
"preference" nobody asked for. Each rule below is broken on purpose, so a test that stopped
looking would fail rather than pass quietly.
"""

from __future__ import annotations

import pytest

import scoped_feedback as ps

SHA_A = "a" * 64
SHA_B = "b" * 64


def _selection(locator: str = "h1.hero", sha: str = SHA_A, kind: str = "element") -> ps.ElementSelection:
    return ps.ElementSelection(locator_kind=kind, locator=locator, artifact_sha256=sha)


def _feedback(**overrides):
    base = dict(
        feedback_id="f1",
        session_id="hns-run-a",
        artifact_id="hero.html",
        artifact_sha256=SHA_A,
        verbatim="this heading is too loud",
    )
    base.update(overrides)
    return ps.capture(**base)


# ── scope follows what was pointed at ────────────────────────────────────────────────────


def test_a_selection_makes_the_feedback_element_scoped():
    record = _feedback(selection=_selection())
    assert record.scope_kind == "element"
    assert record.verbatim == "this heading is too loud"


def test_a_comment_with_no_selection_is_about_the_whole_artifact_not_everything():
    record = _feedback()
    assert record.scope_kind == "artifact"
    assert record.scope_kind != "preference"


def test_a_preference_cannot_be_captured_from_one_comment():
    """The one shortcut that would make every later read a guess."""
    with pytest.raises(ps.FeedbackError, match="then promote it explicitly"):
        _feedback(scope_kind="preference")


def test_a_preference_cannot_carry_a_selection():
    with pytest.raises(ps.FeedbackError, match="cannot carry a selection"):
        ps.Feedback(
            feedback_id="p1",
            session_id="hns-run-a",
            artifact_id="hero.html",
            artifact_sha256=SHA_A,
            scope_kind="preference",
            verbatim="prefer short headings",
            selection=_selection(),
        )


def test_element_scope_without_a_locator_is_refused():
    with pytest.raises(ps.FeedbackError, match="needs a locator"):
        _feedback(scope_kind="element")


# ── the owner's words are kept ───────────────────────────────────────────────────────────


def test_the_original_wording_is_preserved_beside_the_interpretation():
    record = _feedback(interpretation="reduce the heading's font size")
    assert record.verbatim == "this heading is too loud"
    assert record.interpretation == "reduce the heading's font size"
    assert record.to_mapping()["verbatim"] == "this heading is too loud"


def test_a_quick_action_is_a_complete_statement_without_typing():
    record = ps.capture(
        feedback_id="f2",
        session_id="hns-run-a",
        artifact_id="hero.html",
        artifact_sha256=SHA_A,
        quick_action="shorter",
    )
    assert record.verbatim == "make this shorter"
    assert record.quick_action == "shorter"
    # A control that names its own change needs no interpretation, so nothing is pending.
    assert record.material is False


def test_an_empty_record_is_refused():
    with pytest.raises(ps.FeedbackError, match="needs either words or a quick action"):
        ps.capture(
            feedback_id="f3", session_id="hns-run-a", artifact_id="hero.html", artifact_sha256=SHA_A
        )


def test_an_unknown_quick_action_is_refused():
    with pytest.raises(ps.FeedbackError, match="unknown quick action"):
        ps.capture(
            feedback_id="f4",
            session_id="hns-run-a",
            artifact_id="hero.html",
            artifact_sha256=SHA_A,
            quick_action="shoutier",
        )


# ── interpretation must be confirmed before it counts ────────────────────────────────────


def test_words_are_material_by_default_and_block_a_revision():
    record = _feedback()
    assert record.material is True
    assert record.needs_confirmation is True
    revision = ps.plan_revision(artifact_id="hero.html", base_sha256=SHA_A, feedback=[record])
    assert revision.ready is False
    assert revision.awaiting_confirmation == ("f1",)


def test_confirming_the_interpretation_unblocks_the_revision():
    confirmed = ps.confirm(_feedback(), confirmed_by="owner")
    revision = ps.plan_revision(artifact_id="hero.html", base_sha256=SHA_A, feedback=[confirmed])
    assert revision.ready is True
    assert revision.awaiting_confirmation == ()


def test_confirmation_names_who_confirmed_it():
    # An anonymous confirmation cannot be attributed, which is the same defect as none.
    with pytest.raises(ps.FeedbackError, match="confirmed_by must not be empty"):
        ps.confirm(_feedback(), confirmed_by="  ")


def test_an_unambiguous_record_needs_no_confirmation():
    record = _feedback(material=False)
    assert record.needs_confirmation is False
    # Confirming something that was never a judgement would be a ceremony that proves nothing.
    with pytest.raises(ps.FeedbackError, match="not material"):
        ps.confirm(record, confirmed_by="owner")


# ── conflicts are reported, never averaged ───────────────────────────────────────────────


def test_two_different_changes_to_one_region_conflict():
    first = _feedback(feedback_id="f1", selection=_selection(), verbatim="too small")
    second = _feedback(feedback_id="f2", selection=_selection(), verbatim="too large")
    pairs = ps.conflicts([first, second])
    assert len(pairs) == 1
    revision = ps.plan_revision(artifact_id="hero.html", base_sha256=SHA_A, feedback=[first, second])
    assert revision.ready is False
    assert revision.conflicts


def test_keep_and_change_on_one_region_conflict():
    keep = _feedback(feedback_id="f1", selection=_selection(), quick_action="keep", material=False)
    change = _feedback(feedback_id="f2", selection=_selection(), verbatim="make it louder")
    assert ps.conflicts([keep, change])


def test_the_same_comment_twice_is_not_a_conflict():
    assert ps.conflicts([_feedback(feedback_id="f1"), _feedback(feedback_id="f2")]) == []


def test_comments_about_different_regions_do_not_conflict():
    first = _feedback(feedback_id="f1", selection=_selection("h1"), verbatim="louder")
    second = _feedback(feedback_id="f2", selection=_selection("footer"), verbatim="shorter")
    assert ps.conflicts([first, second]) == []


# ── only the affected version is invalidated ─────────────────────────────────────────────


def test_feedback_on_an_older_version_is_stale_not_applied():
    record = _feedback()
    assert ps.stale_for(feedback=[record], artifact_id="hero.html", current_sha256=SHA_B) == [record]
    revision = ps.plan_revision(artifact_id="hero.html", base_sha256=SHA_B, feedback=[record])
    assert revision.changes == ()
    assert revision.ready is False


def test_a_selection_on_a_different_version_than_the_feedback_pins_is_refused():
    with pytest.raises(ps.FeedbackError, match="different artifact version"):
        _feedback(selection=_selection(sha=SHA_B))


def test_only_evidence_naming_the_replaced_version_is_invalidated():
    references = [
        {"kind": "checkpoint", "artifact_sha256": SHA_A, "ref_id": "ck-1"},
        {"kind": "checkpoint", "artifact_sha256": SHA_B, "ref_id": "ck-2"},
        {"kind": "approval", "artifact_sha256": SHA_A, "ref_id": "ap-1"},
        {"kind": "note", "ref_id": "no-digest"},
    ]
    affected = ps.affected_evidence(references=references, invalidated_version=SHA_A)
    assert [item["ref_id"] for item in affected] == ["ck-1", "ap-1"]


# ── promotion is the only way to widen ───────────────────────────────────────────────────


def test_a_scoped_correction_read_as_a_general_rule_is_refused():
    record = _feedback(selection=_selection())
    ps.assert_not_generalised(record, applying_scope="element")
    with pytest.raises(ps.PreferenceDrift, match="widening requires promote"):
        ps.assert_not_generalised(record, applying_scope="preference")


def test_promotion_requires_a_stated_basis_and_a_person():
    record = _feedback(material=False)
    with pytest.raises(ps.FeedbackError, match="basis"):
        ps.promote(record, basis="  ", promoted_by="owner")
    with pytest.raises(ps.FeedbackError, match="promoted_by"):
        ps.promote(record, basis="they said it twice", promoted_by="")
    promoted = ps.promote(record, basis="said the same about three artifacts", promoted_by="owner")
    assert promoted.scope_kind == "preference"
    assert promoted.feedback_id.endswith("@preference")
    assert promoted.confirmed is True


def test_promotion_of_an_unconfirmed_interpretation_is_refused():
    """Widening a guess multiplies it, which is the worst version of this failure."""
    with pytest.raises(ps.FeedbackError, match="confirm what the feedback means before widening"):
        ps.promote(_feedback(), basis="seems general", promoted_by="owner")


def test_a_selection_bound_correction_cannot_be_promoted():
    with pytest.raises(ps.FeedbackError, match="selection would be lost"):
        ps.promote(_feedback(selection=_selection(), material=False), basis="general", promoted_by="owner")


def test_promoting_something_already_general_is_refused():
    promoted = ps.promote(_feedback(material=False), basis="stated", promoted_by="owner")
    with pytest.raises(ps.FeedbackError, match="already a preference"):
        ps.promote(promoted, basis="again", promoted_by="owner")
    with pytest.raises(ps.FeedbackError, match="already applies to the whole artifact"):
        ps.promote(_feedback(feedback_id="a1", material=False), basis="x", promoted_by="o", scope_kind="artifact")


# ── correction, deletion and supersession ────────────────────────────────────────────────


def test_superseding_keeps_the_earlier_words_readable():
    first = _feedback(feedback_id="f1")
    second = _feedback(feedback_id="f2", verbatim="actually keep the heading")
    replaced = ps.supersede(first, by=second)
    assert replaced.superseded_by == "f2"
    assert replaced.verbatim == "this heading is too loud"
    assert replaced.is_live is False
    assert ps.live([replaced, second]) == [second]


def test_feedback_cannot_supersede_itself_or_another_artifact():
    record = _feedback()
    with pytest.raises(ps.FeedbackError, match="cannot supersede itself"):
        ps.supersede(record, by=record)
    other = _feedback(feedback_id="f9", artifact_id="other.html")
    with pytest.raises(ps.FeedbackError, match="cannot supersede feedback on another"):
        ps.supersede(record, by=other)


def test_deleted_feedback_leaves_the_live_set_but_keeps_its_record():
    from dataclasses import replace as _replace

    record = _feedback()
    deleted = _replace(record, deleted=True)
    assert ps.live([deleted]) == []
    assert deleted.to_mapping()["verbatim"] == "this heading is too loud"
    assert deleted.is_live is False


# ── the record cannot be read as permission ──────────────────────────────────────────────


def test_no_record_claims_acceptance_or_permission():
    record = _feedback()
    payload = record.to_mapping()
    assert payload["is_final_acceptance"] is False
    assert payload["grants_publish_permission"] is False
    revision = ps.plan_revision(artifact_id="hero.html", base_sha256=SHA_A, feedback=[record])
    assert revision.to_mapping()["is_final_acceptance"] is False
    assert revision.to_mapping()["grants_publish_permission"] is False


def test_a_forged_stored_flag_is_refused_on_read():
    payload = _feedback().to_mapping()
    payload["grants_publish_permission"] = True
    with pytest.raises(ps.FeedbackError, match="never carries that"):
        ps.Feedback.from_mapping(payload)


def test_a_record_round_trips_through_its_stored_form():
    record = _feedback(selection=_selection())
    assert ps.Feedback.from_mapping(record.to_mapping()).to_mapping() == record.to_mapping()
    assert ps.Feedback.from_mapping(record.to_mapping()).fingerprint() == record.fingerprint()


def test_the_module_reads_no_clock_and_opens_nothing():
    """A pure decision cannot drift, and cannot claim work it did not do."""
    import pathlib

    text = pathlib.Path(ps.__file__).read_text(encoding="utf-8")
    for forbidden in ("time.time", "datetime.now", "os.environ", "subprocess", "requests", "httpx"):
        assert forbidden not in text, f"scoped_feedback must stay pure: found {forbidden}"


def test_the_module_shares_no_top_level_name_with_the_application():
    """The single-file build inlines modules into app.py's namespace; a collision breaks it."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    app = ast.parse((root / "app.py").read_text(encoding="utf-8"))
    module = ast.parse((root / "scoped_feedback.py").read_text(encoding="utf-8"))

    def names(tree):
        found: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                found |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.add(node.name)
        return found

    clash = names(app) & names(module)
    assert not clash, f"these names would overwrite app.py's in the single-file build: {sorted(clash)}"


# ── persistence ─────────────────────────────────────────────────────────────────────────


def test_the_store_round_trips_records_and_scopes_the_listing(tmp_path):
    store = ps.FeedbackStore(tmp_path / "feedback.db")
    first = store.put(_feedback(feedback_id="f1"))
    second = store.put(_feedback(feedback_id="f2", artifact_id="other.html"))

    assert store.get("f1").to_mapping() == first.to_mapping()
    assert [item.feedback_id for item in store.list()] == ["f1", "f2"]
    assert [item.feedback_id for item in store.list(artifact_id="hero.html")] == ["f1"]
    assert store.get("f2").artifact_id == "other.html"
    assert second.feedback_id == "f2"


def test_a_record_without_a_run_is_refused_rather_than_guessed(tmp_path):
    """A record that does not name its run cannot be attributed to a reader's session.

    Guessing would be worse than refusing: two runs can produce an artifact of the same name at
    the same version, and an unattributed correction would be drawn for whichever of them asked.
    """
    recorded = _feedback().to_mapping()
    recorded.pop("session_id")
    with pytest.raises(ps.FeedbackError, match="session_id must not be empty"):
        ps.Feedback.from_mapping(recorded)


def test_two_runs_producing_the_same_artifact_name_do_not_see_each_other(tmp_path):
    """The same name and the same version, produced by two runs, are still two records."""
    store = ps.FeedbackStore(tmp_path / "feedback.db")
    store.put(_feedback(feedback_id="run-a-1", session_id="hns-a"))
    store.put(_feedback(feedback_id="run-b-1", session_id="hns-b"))

    assert [item.feedback_id for item in store.list(session_id="hns-a")] == ["run-a-1"]
    assert [item.feedback_id for item in store.list(session_id="hns-b")] == ["run-b-1"]
    listed = store.list(session_id="hns-a", artifact_id="hero.html")
    assert [item.feedback_id for item in listed] == ["run-a-1"]
    # Reading without a session still returns everything: the filter narrows, it never hides.
    assert len(store.list()) == 2


def test_a_store_created_before_runs_were_recorded_is_migrated_not_dropped(tmp_path):
    """An existing database gains the column. Old rows are kept and belong to no run."""
    import sqlite3

    database = tmp_path / "feedback.db"
    legacy = sqlite3.connect(database)
    legacy.execute(
        "CREATE TABLE scoped_feedback (feedback_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, "
        "artifact_sha256 TEXT NOT NULL, scope_kind TEXT NOT NULL, verbatim TEXT NOT NULL, "
        "record_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    legacy.execute(
        "INSERT INTO scoped_feedback (feedback_id, artifact_id, artifact_sha256, scope_kind, verbatim, record_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("old-1", "hero.html", SHA_A, "artifact", "older words", "{}"),
    )
    legacy.commit()
    legacy.close()

    store = ps.FeedbackStore(database)
    store.put(_feedback(feedback_id="new-1", session_id="hns-a"))
    columns = {row[1] for row in sqlite3.connect(database).execute("PRAGMA table_info(scoped_feedback)")}
    assert "session_id" in columns
    # The legacy row survives; it simply belongs to no run, so no session's listing shows it.
    assert [item.feedback_id for item in store.list(session_id="hns-a")] == ["new-1"]


def test_putting_the_same_id_twice_updates_rather_than_forks(tmp_path):
    store = ps.FeedbackStore(tmp_path / "feedback.db")
    store.put(_feedback())
    confirmed = ps.confirm(_feedback(), confirmed_by="owner")
    store.put(confirmed)
    assert len(store.list()) == 1
    assert store.get("f1").confirmed is True


def test_deleting_keeps_the_record_and_drops_it_from_the_live_set(tmp_path):
    store = ps.FeedbackStore(tmp_path / "feedback.db")
    store.put(_feedback())
    deleted = store.delete("f1")
    assert deleted.deleted is True
    assert store.get("f1").verbatim == "this heading is too loud"
    assert ps.live(store.list()) == []
    # Deleting again is refused: a second delete would report work it did not do.
    with pytest.raises(ps.FeedbackError, match="no feedback"):
        store.delete("missing")
    store.delete("f1")  # deleting an already-deleted record is idempotent, not an error
    assert store.get("f1").deleted is True


def test_a_retained_preference_can_be_inspected_and_removed(tmp_path):
    """The criterion in plain terms: what does this preference apply to, and can I undo it."""
    store = ps.FeedbackStore(tmp_path / "feedback.db")
    preference = ps.promote(_feedback(material=False), basis="said it about three artifacts", promoted_by="owner")
    store.put(preference)

    recorded = store.get(preference.feedback_id)
    assert recorded.scope_kind == "preference"
    assert recorded.confirmed_by == "owner"
    assert recorded.scope_kind == "preference"  # its reach is readable, not implied

    store.delete(preference.feedback_id)
    assert ps.live(store.list()) == []
