"""AP-021: a complete website draft, and the five things it must refuse to invent.

The tests are organised around refusals rather than happy paths, because a draft workflow that
produces a page is not hard; producing one that does not quietly lie is. Each refusal below is
broken on purpose, so a test that stopped looking would fail rather than pass vacuously.
"""
from __future__ import annotations

import pytest

import website_draft as wd

SHA = "a" * 64


def audience(**kw) -> wd.DraftAudience:
    return wd.DraftAudience(**{"description": "independent café owners in Lisbon", **kw})


def scope(**kw) -> wd.DraftScope:
    return wd.DraftScope(
        **{
            "site_name": "Cafe Vert",
            "purpose": "a one-page site that explains the roastery and its opening hours",
            "audience": audience(),
            "pages": ["index"],
            "out_of_scope": ["online ordering", "a customer login"],
            **kw,
        }
    )


def checks(*, failing: tuple[str, ...] = ()) -> list[dict]:
    return [
        {"name": name, "passed": name not in failing, "detail": "measured in a browser"}
        for name in wd.REQUIRED_CHECKS
    ]


def facts(
    *,
    failing: tuple[str, ...] = (),
    interactions: list[dict] | None = None,
    assets: list[dict] | None = None,
    digest: str = SHA,
) -> dict:
    return {
        "artifact_sha256": digest,
        "checks": checks(failing=failing),
        "interactions": interactions or [],
        "assets": assets or [],
        "run_id": "run-1",
    }


class Scripted:
    """An executor that returns prepared attempts, and records what it was asked for."""

    def __init__(self, *attempts: dict) -> None:
        self._attempts = list(attempts)
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def run(self, scope, attempt, previous_failures):
        self.calls.append((attempt, tuple(previous_failures)))
        index = min(attempt, len(self._attempts)) - 1
        return self._attempts[index]


# -- AC1: scope is explicit, and an unbenchmarked site samples first ---------------------


def test_a_site_without_an_audience_cannot_be_described() -> None:
    with pytest.raises(wd.WebsiteDraftError, match="audience must be a DraftAudience"):
        wd.DraftScope(
            site_name="Cafe Vert",
            purpose="a roastery site",
            audience="café owners",  # type: ignore[arg-type]
            pages=["index"],
            out_of_scope=["ordering"],
        )


def test_a_brief_that_excludes_nothing_is_refused() -> None:
    """An unbounded brief cannot be finished, so it cannot be reviewed either."""
    with pytest.raises(wd.ScopeRefused, match="out_of_scope must name at least one thing"):
        scope(out_of_scope=[])


def test_a_site_with_no_pages_is_refused() -> None:
    with pytest.raises(wd.ScopeRefused, match="at least one page"):
        scope(pages=[])


def test_a_language_tag_that_is_not_a_tag_is_refused() -> None:
    with pytest.raises(wd.WebsiteDraftError, match="BCP-47"):
        wd.DraftAudience(description="café owners", language="Portuguese please")


def test_an_unbenchmarked_site_is_marked_as_having_no_layout_benchmark() -> None:
    """The sample direction is a separate gate (AP-018); the draft says whether one existed."""
    assert scope().no_layout_benchmark is False
    assert scope(no_layout_benchmark=True).no_layout_benchmark is True


# -- AC2a: interactions either work or say they do not ----------------------------------


def test_a_requested_interaction_with_no_verdict_is_refused_not_dropped() -> None:
    executor = Scripted(facts(interactions=[]))
    with pytest.raises(wd.InteractionRefused, match="gave no verdict"):
        wd.draft_website(scope=scope(), executor=executor, requested_interactions=["menu toggle"])


def test_an_interaction_that_claims_to_work_must_name_its_evidence() -> None:
    with pytest.raises(wd.InteractionRefused, match="names no evidence"):
        wd.InteractionVerdict(name="menu toggle", state="supported")


def test_an_unsupported_interaction_must_say_so_in_words() -> None:
    with pytest.raises(wd.InteractionRefused, match="must say so in words"):
        wd.InteractionVerdict(name="newsletter signup", state="unsupported")


def test_written_but_untested_is_not_working() -> None:
    verdict = wd.InteractionVerdict(
        name="newsletter signup", state="supported_untested", limitation="the POST target is unbuilt"
    )
    assert verdict.is_working() is False


def test_an_unknown_interaction_state_is_refused() -> None:
    with pytest.raises(wd.InteractionRefused, match="is unknown"):
        wd.InteractionVerdict(name="carousel", state="mostly works", limitation="it mostly works")


def test_an_honest_unsupported_verdict_still_makes_the_draft_partial() -> None:
    """Honesty is required, and it is not the same as being done."""
    verdict = wd.InteractionVerdict(
        name="newsletter signup",
        state="unsupported",
        limitation="the brief asks for a signup but no endpoint or provider was granted",
    )
    draft = wd.draft_website(
        scope=scope(), executor=Scripted(facts(interactions=[_as_dict(verdict)]))
    )
    assert draft.status() == "partial"
    assert "interaction not working: newsletter signup" in draft.reasons()
    assert draft.complete() is False


def _as_dict(verdict: wd.InteractionVerdict) -> dict:
    return {
        "name": verdict.name,
        "state": verdict.state,
        "evidence": list(verdict.evidence),
        "limitation": verdict.limitation,
    }


def test_a_working_interaction_with_evidence_completes() -> None:
    verdict = wd.InteractionVerdict(
        name="menu toggle", state="supported", evidence=["playwright click expands the menu"]
    )
    draft = wd.draft_website(
        scope=scope(), executor=Scripted(facts(interactions=[_as_dict(verdict)]))
    )
    assert draft.complete() is True
    assert draft.status() == "complete"


# -- AC2b: no invented testimonials, logos, integrations or certifications ---------------


@pytest.mark.parametrize("kind", ["testimonial", "case_study", "customer_logo"])
def test_sourced_claim_kinds_cannot_exist_without_a_source(kind: str) -> None:
    with pytest.raises(wd.ContentRefused, match="names no source"):
        wd.ContentClaim(kind=kind, text="Best coffee in Lisbon.")


def test_a_sourced_testimonial_is_admissible() -> None:
    claim = wd.ContentClaim(
        kind="testimonial", text="Best coffee in Lisbon.", source_ref="owner-statement:12"
    )
    assert claim.admissible() is True


def test_a_fabricated_placeholder_is_recordable_but_never_admissible() -> None:
    """A run may be honest that content is a placeholder; it may not ship it as real."""
    claim = wd.ContentClaim(kind="testimonial", text="Best coffee in Lisbon.", fabricated=True)
    assert claim.admissible() is False


def test_an_integration_badge_needs_a_real_source_too() -> None:
    """A badge for a service nobody connected is the same lie as a fabricated testimonial."""
    with pytest.raises(wd.ContentRefused, match="names no source"):
        wd.ContentClaim(kind="integration_badge", text="Works with Stripe")
    sourced = wd.ContentClaim(
        kind="integration_badge", text="Works with Stripe", source_ref="mcp-server:stripe"
    )
    assert sourced.admissible() is True


def test_an_unknown_claim_kind_is_refused() -> None:
    with pytest.raises(wd.ContentRefused, match="is unknown"):
        wd.ContentClaim(kind="vibe", text="artisanal")


def test_a_security_claim_cannot_be_made_by_a_draft_at_all() -> None:
    """A draft may describe, but a security claim needs its own evidence and sign-off."""
    assert "security_sign_off" in wd.CANNOT_BE_CLAIMED
    assert "owner_acceptance" in wd.CANNOT_BE_CLAIMED
    assert "publish_permission" in wd.CANNOT_BE_CLAIMED


# -- AC3: assets carry provenance and rights ---------------------------------------------


def test_an_asset_from_elsewhere_must_name_where_it_came_from() -> None:
    with pytest.raises(wd.ProvenanceRefused, match="names no source"):
        wd.AssetProvenance(path="img/hero.jpg", role="hero photograph", basis="licensed")


def test_a_licence_claim_without_the_licence_is_refused() -> None:
    with pytest.raises(wd.ProvenanceRefused, match="'licensed' without the licence"):
        wd.AssetProvenance(
            path="img/hero.jpg", role="hero photograph", basis="licensed", source="stock site"
        )


def test_a_complete_licence_is_accepted_and_recorded() -> None:
    asset = wd.AssetProvenance(
        path="img/hero.jpg",
        role="hero photograph",
        basis="licensed",
        source="owner's stock account",
        license_id="CC-BY-4.0",
        sha256=SHA,
    )
    assert asset.blocks_delivery() is False
    assert asset.license_id == "CC-BY-4.0"


def test_an_authored_asset_needs_no_external_source() -> None:
    asset = wd.AssetProvenance(path="index.html", role="the page", basis="authored_in_run")
    assert asset.blocks_delivery() is False


def test_an_unknown_basis_blocks_delivery_rather_than_passing_quietly() -> None:
    asset = wd.AssetProvenance(
        path="img/mystery.png", role="background", basis="unknown", source="found in an old folder",
        license_id="pending:owner",
    )
    assert asset.blocks_delivery() is True
    draft = wd.draft_website(scope=scope(), executor=Scripted(facts(assets=[_asset_dict(asset)])))
    assert draft.status() == "partial"
    assert "asset rights unknown: img/mystery.png" in draft.reasons()


def _asset_dict(asset: wd.AssetProvenance) -> dict:
    return {
        "path": asset.path,
        "role": asset.role,
        "basis": asset.basis,
        "source": asset.source,
        "license_id": asset.license_id,
        "sha256": asset.sha256,
    }


def test_an_unknown_provenance_basis_name_is_refused() -> None:
    with pytest.raises(wd.ProvenanceRefused, match="is unknown"):
        wd.AssetProvenance(path="x.png", role="icon", basis="probably fine")


# -- AC4: the checks are named, required, and run ----------------------------------------


def test_a_draft_that_omits_a_required_check_is_refused() -> None:
    """A report cannot speak to a check that never ran."""
    partial = [c for c in checks() if c["name"] != "no_broken_link"]
    with pytest.raises(wd.WebsiteDraftError, match="required check 'no_broken_link' did not run"):
        wd.WebsiteDraft(
            scope=scope(), artifact_sha256=SHA, checks=tuple(
                wd.DraftCheck(name=c["name"], passed=c["passed"]) for c in partial
            )
        )


def test_a_duplicate_check_would_double_count_and_is_refused() -> None:
    rows = [wd.DraftCheck(name=n, passed=True) for n in wd.REQUIRED_CHECKS] + [
        wd.DraftCheck(name="document_loads", passed=True)
    ]
    with pytest.raises(wd.WebsiteDraftError, match="each check may appear once"):
        wd.WebsiteDraft(scope=scope(), artifact_sha256=SHA, checks=tuple(rows))


def test_a_mostly_passed_check_is_not_a_boolean() -> None:
    with pytest.raises(wd.WebsiteDraftError, match="must be a boolean"):
        wd.DraftCheck(name="document_loads", passed="mostly")


def test_every_required_check_present_and_passing_is_complete() -> None:
    draft = wd.draft_website(scope=scope(), executor=Scripted(facts()))
    assert draft.complete() is True
    assert draft.failed_checks() == ()


# -- AC5: the repair loop is bounded and partial failure is visible -----------------------


def test_the_loop_retries_within_the_budget_and_reports_each_attempt() -> None:
    executor = Scripted(facts(failing=("no_horizontal_overflow",)), facts())
    draft = wd.draft_website(scope=scope(), executor=executor, attempt_budget=2)
    assert draft.complete() is True
    assert [call[0] for call in executor.calls] == [1, 2]
    # The second attempt is told what failed, rather than guessing.
    assert "check failed: no_horizontal_overflow" in executor.calls[1][1]
    assert len(draft.repairs) == 1
    assert draft.repairs[0].failures == ("check failed: no_horizontal_overflow",)


def test_an_exhausted_budget_yields_partial_not_an_exception() -> None:
    executor = Scripted(facts(failing=("no_broken_link",)))
    draft = wd.draft_website(scope=scope(), executor=executor, attempt_budget=3)
    assert draft.status() == "partial"
    assert draft.complete() is False
    assert len(executor.calls) == 3
    assert len(draft.repairs) == 3
    assert "check failed: no_broken_link" in draft.reasons()


def test_the_budget_cannot_be_unbounded_or_zero() -> None:
    for bad in (0, wd.WS_MAX_ATTEMPTS + 1):
        with pytest.raises(wd.WebsiteDraftError, match="attempt_budget must be between"):
            wd.draft_website(scope=scope(), executor=Scripted(facts()), attempt_budget=bad)


def test_the_budget_cannot_be_a_boolean() -> None:
    with pytest.raises(wd.WebsiteDraftError, match="attempt_budget must be an integer"):
        wd.draft_website(scope=scope(), executor=Scripted(facts()), attempt_budget=True)


def test_more_repairs_than_the_budget_is_refused() -> None:
    repairs = tuple(wd.RepairAttempt(attempt=i, failures=("x",)) for i in (1, 2, 3))
    with pytest.raises(wd.RepairExhausted, match="exceed the stated budget"):
        wd.WebsiteDraft(
            scope=scope(),
            artifact_sha256=SHA,
            checks=tuple(wd.DraftCheck(name=n, passed=True) for n in wd.REQUIRED_CHECKS),
            repairs=repairs,
            attempt_budget=2,
        )


def test_partial_failure_is_visible_in_the_report_not_rounded_up() -> None:
    draft = wd.draft_website(
        scope=scope(), executor=Scripted(facts(failing=("no_console_error",))), attempt_budget=1
    )
    report = draft.to_mapping()
    assert report["status"] == "partial"
    assert report["reasons"] == ["check failed: no_console_error"]
    assert [c["passed"] for c in report["checks"]].count(False) == 1


# -- AC6: no deployment, DNS or production credential use -------------------------------


def test_deployment_is_a_constant_that_cannot_be_flipped() -> None:
    grant = wd.DeploymentGrant()
    assert grant.deployment is False
    assert grant.dns_change is False
    assert grant.production_credentials is False
    assert grant.granted is False
    with pytest.raises(TypeError):
        wd.DeploymentGrant(deployment=True)  # type: ignore[call-arg]


def test_asking_the_grant_to_deploy_is_refused_in_words() -> None:
    with pytest.raises(wd.DeploymentRefused, match="separate grants"):
        wd.DeploymentGrant().refuse("deploy to production")


@pytest.mark.parametrize(
    "claim", ["deployment", "dns_change", "production_credentials", "publish_permission"]
)
def test_the_report_never_claims_what_the_draft_cannot_do(claim: str) -> None:
    draft = wd.draft_website(scope=scope(), executor=Scripted(facts()))
    report = draft.to_mapping()
    assert report[claim] is False
    assert claim in report["cannot_be_claimed"]


# -- reporting integrity ----------------------------------------------------------------


def test_the_report_restates_the_scope_so_a_reader_can_judge_it() -> None:
    report = wd.draft_website(scope=scope(), executor=Scripted(facts())).to_mapping()
    assert report["scope"]["audience"] == "independent café owners in Lisbon"
    assert report["scope"]["out_of_scope"] == ["online ordering", "a customer login"]


def test_a_digest_that_is_not_a_digest_is_refused() -> None:
    with pytest.raises(wd.WebsiteDraftError, match="lowercase SHA-256"):
        wd.draft_website(scope=scope(), executor=Scripted(facts(digest="not-a-digest")))


def test_the_fingerprint_changes_when_the_draft_changes() -> None:
    a = wd.draft_website(scope=scope(), executor=Scripted(facts()))
    b = wd.draft_website(
        scope=scope(), executor=Scripted(facts(failing=("no_console_error",))), attempt_budget=1
    )
    assert a.fingerprint() != b.fingerprint()


def test_the_two_halves_of_the_story_are_both_named() -> None:
    """The module's own vocabulary: what it requires, and what it can never claim."""
    assert set(wd.REQUIRED_CHECKS) == {
        "document_loads",
        "no_console_error",
        "no_broken_link",
        "no_horizontal_overflow",
    }
    assert "deployment" in wd.CANNOT_BE_CLAIMED
    assert "revenue_or_conversion_outcome" in wd.CANNOT_BE_CLAIMED


# -- discipline: the module decides, the injected executor executes ---------------------


def test_the_module_reads_no_clock_and_opens_nothing() -> None:
    """The same decision logic has to run against a recording double in CI.

    A module that opened files or read the clock could not be replayed, and a replayed report
    is the only kind that can be checked after the fact.
    """
    import ast
    import pathlib

    source = pathlib.Path("website_draft.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing"}
    # No clock, no filesystem, no process, no network.
    for forbidden in ("datetime", "time", "os", "pathlib", "subprocess", "socket", "requests"):
        assert forbidden not in imported


def test_the_module_shares_no_top_level_name_with_app() -> None:
    """This module is inlined into the single-file build, so a name collision is a real bug.

    `direction_checkpoints` lost a day to this: a module-level ``SCHEMA_VERSION`` replaced the
    application's, so ``init_db`` created no runtime tables.
    """
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

    shared = top_level("website_draft.py") & top_level("app.py")
    assert shared == set(), f"these names would collide in the single-file build: {sorted(shared)}"


def test_the_executor_protocol_is_the_only_place_work_happens() -> None:
    draft = wd.draft_website(scope=scope(), executor=Scripted(facts()))
    assert draft.complete() is True
    # The module produced a record, not a page: there is no write, deploy or serve surface.
    assert not hasattr(wd, "write_site")
    assert not hasattr(wd, "deploy")
