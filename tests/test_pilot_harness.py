"""AP-032 pilot harness — measuring what can be measured, and refusing the rest.

The tests that matter most here are the negative ones: a harness that cannot report a pass for
something it did not measure is the whole point. Every fixture is a small HTML string built here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import pilot_harness as ph
import website_quality as wq


CLEAN = {
    "index.html": (
        "<html><body><h1>Trailhead Coffee</h1>"
        '<a href="about.html">About</a>'
        '<img src="logo.png" alt="logo">'
        '<form action="/subscribe"><input name="email"></form>'
        "</body></html>"
    ),
    "about.html": '<html><body><a href="index.html">Home</a><p>We roast on Tuesdays.</p></body></html>',
}


def outcome(partial: ph.PilotPartial, criterion: str) -> ph.CriterionOutcome:
    return next(item for item in partial.outcomes if item.criterion == criterion)


# ---------------------------------------------------------------------------
# Coverage of the criterion set.
# ---------------------------------------------------------------------------


def test_the_two_criterion_sets_are_disjoint_and_cover_the_rubric() -> None:
    assert set(ph.OFFLINE_CRITERIA) & set(ph.BROWSER_CRITERIA) == set()
    assert set(ph.OFFLINE_CRITERIA) | set(ph.BROWSER_CRITERIA) == set(wq.CRITERIA)


def test_every_criterion_is_accounted_for_exactly_once() -> None:
    partial = ph.build_pilot_partial(CLEAN)
    assert len(partial.outcomes) == len(wq.CRITERIA)
    assert {item.criterion for item in partial.outcomes} == set(wq.CRITERIA)


def test_the_six_browser_criteria_are_never_reported_as_passing() -> None:
    """The refusal that makes the partial trustworthy."""
    partial = ph.build_pilot_partial(CLEAN)
    for criterion in ph.BROWSER_CRITERIA:
        item = outcome(partial, criterion)
        assert item.basis == "cannot-measure-here"
        assert item.passed is False
        assert item.criterion in partial.still_open


def test_an_unmeasured_criterion_cannot_be_reported_as_passing() -> None:
    with pytest.raises(ph.PilotHarnessError, match="only a measured criterion may be reported as passing"):
        ph.CriterionOutcome("contrast_ok", True, "cannot-measure-here", "guessed")


def test_an_unknown_basis_is_refused() -> None:
    with pytest.raises(ph.PilotHarnessError, match="unknown basis"):
        ph.CriterionOutcome("contrast_ok", False, "probably", "guessed")


# ---------------------------------------------------------------------------
# The offline measurements.
# ---------------------------------------------------------------------------


def test_a_clean_artifact_is_offline_clear() -> None:
    partial = ph.build_pilot_partial(CLEAN)
    assert partial.offline_clear is True
    assert partial.failed == ()


def test_offline_clear_does_not_mean_deliverable() -> None:
    """The distinction the whole module exists to keep."""
    mapping = ph.build_pilot_partial(CLEAN).to_mapping()
    assert mapping["offline_clear"] is True
    assert mapping["is_a_qualification_report"] is False
    assert mapping["still_open_count"] == 6
    assert mapping["what_a_pilot_report_still_needs"]


def test_placeholder_copy_is_caught_and_fails_the_criterion() -> None:
    partial = ph.build_pilot_partial({"index.html": "<html><body><p>lorem ipsum dolor</p></body></html>"})
    item = outcome(partial, "no_placeholder_content")
    assert item.passed is False
    assert partial.offline_clear is False
    assert "no_placeholder_content" in partial.failed


def test_a_secret_is_caught_and_masked_in_the_report() -> None:
    secret = "sk-" + "a" * 40
    partial = ph.build_pilot_partial({"index.html": f"<html><body><p>{secret}</p></body></html>"})
    item = outcome(partial, "secrets_absent")
    assert item.passed is False
    assert secret not in json.dumps(partial.to_mapping()), "the report must not re-leak the secret"


def test_a_dangling_internal_link_fails_links_resolve() -> None:
    partial = ph.build_pilot_partial({"index.html": '<html><body><a href="missing.html">x</a></body></html>'})
    item = outcome(partial, "links_resolve")
    assert item.passed is False
    assert "missing.html" in item.detail


def test_external_links_are_not_treated_as_dangling() -> None:
    partial = ph.build_pilot_partial(
        {"index.html": '<html><body><a href="https://example.com/x">x</a><a href="#top">t</a></body></html>'}
    )
    assert outcome(partial, "links_resolve").passed is True


def test_a_form_without_an_action_fails_forms_work() -> None:
    partial = ph.build_pilot_partial({"index.html": "<html><body><form><input name='q'></form></body></html>"})
    assert outcome(partial, "forms_work").passed is False


def test_an_unnamed_input_fails_forms_work() -> None:
    partial = ph.build_pilot_partial(
        {"index.html": '<html><body><form action="/s"><input type="text"></form></body></html>'}
    )
    assert outcome(partial, "forms_work").passed is False


def test_an_artifact_with_no_forms_says_so_instead_of_claiming_forms_work() -> None:
    partial = ph.build_pilot_partial({"index.html": "<html><body><p>Static.</p></body></html>"})
    item = outcome(partial, "forms_work")
    assert item.passed is True
    assert "not evidence that a form would work" in item.detail


def test_a_plain_http_external_reference_fails_the_dependency_review() -> None:
    partial = ph.build_pilot_partial(
        {"index.html": '<html><body><script src="http://insecure.example/a.js"></script></body></html>'}
    )
    assert outcome(partial, "dependencies_reviewed").passed is False


def test_https_external_references_pass_the_dependency_review() -> None:
    partial = ph.build_pilot_partial(
        {"index.html": '<html><body><script src="https://cdn.example/a.js"></script></body></html>'}
    )
    item = outcome(partial, "dependencies_reviewed")
    assert item.passed is True
    assert "1 external reference" in item.detail


# ---------------------------------------------------------------------------
# Inputs the harness refuses rather than guesses at.
# ---------------------------------------------------------------------------


def test_an_empty_artifact_set_is_refused() -> None:
    with pytest.raises(ph.ArtifactRefused, match="audit of nothing passes vacuously"):
        ph.audit_artifact({})


def test_an_empty_file_is_refused_rather_than_counted_as_clean() -> None:
    with pytest.raises(ph.ArtifactRefused, match="an empty artifact measures nothing"):
        ph.audit_artifact({"index.html": "   "})


def test_a_binary_extension_is_refused() -> None:
    with pytest.raises(ph.ArtifactRefused, match="reads text artifacts only"):
        ph.audit_artifact({"logo.png": "not really a png"})


def test_the_partial_digests_exactly_the_bytes_audited() -> None:
    first = ph.build_pilot_partial(CLEAN)
    second = ph.build_pilot_partial(CLEAN)
    changed = ph.build_pilot_partial({**CLEAN, "about.html": CLEAN["about.html"].replace("Tuesdays", "Mondays")})
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.artifact_sha256 != changed.artifact_sha256


def test_the_partial_refuses_an_incomplete_criterion_set() -> None:
    partial = ph.build_pilot_partial(CLEAN)
    with pytest.raises(ph.PilotHarnessError, match="must account for every criterion exactly once"):
        ph.PilotPartial(
            artifact_sha256=partial.artifact_sha256,
            outcomes=partial.outcomes[:-1],
            assets=(),
        )


def test_the_partial_refuses_a_non_digest() -> None:
    with pytest.raises(ph.ArtifactRefused, match="lowercase SHA-256"):
        ph.PilotPartial(artifact_sha256="nope", outcomes=ph.build_pilot_partial(CLEAN).outcomes, assets=())


def test_asset_references_record_their_location() -> None:
    partial = ph.build_pilot_partial(CLEAN)
    assert partial.assets
    assert all("@" in item.location for item in partial.assets)


def test_the_cli_refuses_a_missing_directory(tmp_path: Any, capsys: Any) -> None:
    assert ph.main([str(tmp_path / "absent")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_the_cli_refuses_an_empty_directory_rather_than_passing_vacuously(
    tmp_path: Any, capsys: Any
) -> None:
    (tmp_path / "notes.md").write_text("nothing to audit here", encoding="utf-8")
    assert ph.main([str(tmp_path)]) == 1
    assert "refused" in capsys.readouterr().err


def test_the_cli_returns_nonzero_when_offline_criteria_fail(tmp_path: Any) -> None:
    (tmp_path / "index.html").write_text("<html><body><p>lorem ipsum</p></body></html>", encoding="utf-8")
    assert ph.main([str(tmp_path)]) == 1


def test_the_cli_returns_zero_and_emits_partial_json(tmp_path: Any, capsys: Any) -> None:
    (tmp_path / "index.html").write_text(CLEAN["index.html"], encoding="utf-8")
    (tmp_path / "about.html").write_text(CLEAN["about.html"], encoding="utf-8")
    assert ph.main([str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_a_qualification_report"] is False
    assert payload["offline_clear"] is True
