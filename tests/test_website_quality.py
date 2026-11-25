"""AP-022: qualifying a website's quality and delivery without inventing the parts it cannot see.

The interesting behaviour is not "a report exists" — it is the ways a green-looking report is
refused. Each test below breaks one rule on purpose, so a test that stopped looking would fail
rather than pass vacuously.
"""
from __future__ import annotations

import pytest

import website_quality as wq

SHA = "a" * 64


def outcomes(value: bool = True) -> dict[str, bool]:
    return {name: value for name in wq.CRITERIA}


def cells(*, failing: tuple[str, ...] = ()) -> list[dict]:
    rows = []
    for name, _w, _h in wq.VIEWPORT_MATRIX:
        for zoom in (1, wq.ZOOM_FACTOR):
            cell_id = f"{name}-{'x'.join(str(v) for v in next(e[1:] for e in wq.VIEWPORT_MATRIX if e[0] == name))}@{zoom}x"
            rows.append(
                {
                    "viewport": name,
                    "zoom": zoom,
                    "clipped": cell_id in failing,
                    "overlapping": False,
                    "horizontal_overflow_px": 12 if cell_id in failing else 0,
                }
            )
    return rows


def probe(
    *,
    failing: tuple[str, ...] = (),
    layout_failing: tuple[str, ...] = (),
    interactions: list[dict] | None = None,
    problems: list[dict] | None = None,
    documents: dict[str, str] | None = None,
    digest: str = SHA,
) -> dict:
    return {
        "artifact_sha256": digest,
        "outcomes": outcomes(),
        "layout": cells(failing=layout_failing),
        "interactions": interactions or [],
        "problems": problems or [],
        "documents": documents or {},
    }


class Scripted:
    """A probe that returns prepared facts and records what it was asked to inspect."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.asked: list[str] = []

    def inspect(self, artifact_sha256: str):
        self.asked.append(artifact_sha256)
        return self.payload


def owner(*, useful: bool = True, **kw) -> wq.OwnerReview:
    return wq.OwnerReview(
        **{"actor": "owner:priya", "statement": "I would hand this to my cousin who runs the café.", "useful": useful, **kw}
    )


# -- AC1: critical flows pass real tests ------------------------------------------------


def test_a_passing_flow_must_say_what_exercised_it() -> None:
    with pytest.raises(wq.WebsiteQualityError, match="names its evidence"):
        wq.InteractionCheck(name="newsletter form", kind="form", passed=True)


def test_a_flow_is_classified_so_it_can_be_triaged() -> None:
    with pytest.raises(wq.WebsiteQualityError, match="kind .* is unknown"):
        wq.InteractionCheck(name="hover effect", kind="vibes", passed=True, detail="it moves")


def test_a_failing_flow_makes_the_report_undeliverable() -> None:
    report = wq.evaluate_website(
        probe=Scripted(
            probe(
                interactions=[
                    {"name": "contact form", "kind": "form", "passed": False, "detail": "submit 405"},
                    {"name": "main nav", "kind": "navigation", "passed": True, "detail": "three targets reached"},
                ]
            )
        ),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert report.deliverable() is False
    assert "interaction failed: contact form" in report.reasons()


# -- AC2: phone/tablet/desktop and zoom, per cell ----------------------------------------


def test_an_unmeasured_cell_is_not_a_passing_cell() -> None:
    with pytest.raises(wq.LayoutRefused, match="layout matrix is incomplete"):
        wq.PilotReport(
            artifact_sha256=SHA,
            outcomes=outcomes(),
            layout=tuple(wq.LayoutCell(viewport=wq.Viewport.named("phone"), zoom=1, clipped=False, overlapping=False) for _ in range(1)),
        )


def test_a_broken_phone_layout_fails_even_when_desktop_passes() -> None:
    """The exact shortcut this story exists to stop: 'it looks fine on my screen'."""
    report = wq.evaluate_website(
        probe=Scripted(probe(layout_failing=("phone-390x844@1x",))),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert report.automated_pass() is False
    assert "layout failed: phone-390x844@1x" in report.reasons()
    assert report.deliverable() is False


def test_the_zoom_cell_is_judged_separately_from_the_base_cell() -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe(layout_failing=("desktop-1440x900@2x",))),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert "layout failed: desktop-1440x900@2x" in report.reasons()


def test_an_unknown_zoom_factor_is_refused_not_rounded() -> None:
    with pytest.raises(wq.LayoutRefused, match="zoom must be 1 or 2"):
        wq.LayoutCell(viewport=wq.Viewport.named("desktop"), zoom=3, clipped=False, overlapping=False)


def test_a_viewport_outside_the_matrix_is_refused() -> None:
    with pytest.raises(wq.LayoutRefused, match="not in the matrix"):
        wq.Viewport(name="watch", width=200, height=200)


def test_overflow_and_no_clipping_cannot_both_be_reported() -> None:
    with pytest.raises(wq.LayoutRefused, match="disagree"):
        wq.LayoutCell(
            viewport=wq.Viewport.named("phone"), zoom=1, clipped=False, overlapping=False,
            horizontal_overflow_px=40,
        )


def test_a_cell_may_be_recorded_once_only() -> None:
    duplicated = [*cells(), cells()[0]]
    with pytest.raises(wq.LayoutRefused, match="may appear once"):
        wq.PilotReport(artifact_sha256=SHA, outcomes=outcomes(), layout=tuple(_cells_from(duplicated)))


def _cells_from(rows: list[dict]) -> list[wq.LayoutCell]:
    return [
        wq.LayoutCell(
            viewport=wq.Viewport.named(row["viewport"]),
            zoom=row["zoom"],
            clipped=row["clipped"],
            overlapping=row["overlapping"],
            horizontal_overflow_px=row["horizontal_overflow_px"],
        )
        for row in rows
    ]


# -- AC3: keyboard, focus, contrast, reduced motion -------------------------------------


def test_the_contrast_floors_are_the_same_numbers_the_studio_gate_uses() -> None:
    """Quoted, not re-invented: a second copy of 4.5 would drift."""
    assert wq.CONTRAST_FLOORS == {"normal_text": 4.5, "large_text": 3.0}
    assert wq.TARGET_MIN_PX == 24
    assert wq.ZOOM_FACTOR == 2


@pytest.mark.parametrize(
    "criterion", ["keyboard_reachable", "focus_visible", "contrast_ok", "reduced_motion_respected"]
)
def test_an_accessibility_criterion_that_failed_blocks_delivery(criterion: str) -> None:
    payload = probe()
    payload["outcomes"] = {**outcomes(), criterion: False}
    report = wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())
    assert report.deliverable() is False
    assert f"criterion did not pass: {criterion}" in report.reasons()


# -- AC4: console, network, secrets, dependencies, placeholder content -------------------


def test_a_missing_criterion_outcome_is_refused_not_assumed() -> None:
    """'We didn't check that' and 'that failed' must look the same to the reader."""
    payload = probe()
    payload["outcomes"] = {k: v for k, v in outcomes().items() if k != "console_clean"}
    with pytest.raises(wq.RubricRefused, match="have no outcome"):
        wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())


def test_an_invented_criterion_measures_nothing() -> None:
    payload = probe()
    payload["outcomes"] = {**outcomes(), "looks_nice": True}
    with pytest.raises(wq.RubricRefused, match="invented criterion"):
        wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())


def test_an_outcome_that_is_mostly_true_is_not_a_boolean() -> None:
    payload = probe()
    payload["outcomes"] = {**outcomes(), "contrast_ok": "mostly"}
    with pytest.raises(wq.ProbeRefused, match="must be a boolean"):
        wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())


def test_an_api_key_in_the_shipped_page_is_found_and_reported_masked() -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe(documents={"index.html": "<script>const k='sk-abcdefghijklmnopqrstuvwx'</script>"})),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert report.automated_pass() is False
    secret = [p for p in report.blocking_problems() if p.kind == "secret"]
    assert secret, report.problems
    assert "sk-a" not in secret[0].detail or "offset" in secret[0].detail


def test_a_secret_can_never_be_a_warning() -> None:
    with pytest.raises(wq.SecretsRefused, match="always blocking"):
        wq.AssetProblem(kind="secret", location="index.html", detail="key", severity="warning")


@pytest.mark.parametrize(
    "text",
    ["Lorem ipsum dolor sit amet", "TODO: replace this copy", "YOUR TEXT HERE"],
)
def test_shipped_placeholder_copy_blocks_delivery(text: str) -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe(documents={"index.html": f"<p>{text}</p>"})),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert report.automated_pass() is False
    assert any(p.kind == "placeholder" for p in report.blocking_problems())


def test_scanning_does_not_need_the_probe_to_remember() -> None:
    """The module scans documents itself; a probe that forgets cannot produce a clean report."""
    payload = probe(documents={"about.html": "<p>AKIAIOSFODNN7EXAMPLE</p>"})
    assert payload["problems"] == []
    report = wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())
    assert any(p.kind == "secret" for p in report.blocking_problems())


def test_a_dependency_finding_is_classified_and_reported() -> None:
    report = wq.evaluate_website(
        probe=Scripted(
            probe(problems=[{"kind": "dependency", "location": "cdn.example/x.js", "detail": "unpinned", "severity": "warning"}])
        ),
        artifact_sha256=SHA,
        owner_review=owner(),
    )
    # A warning does not block; it is still reported, which is the point of the severity field.
    assert report.automated_pass() is True
    assert any(p.severity == "warning" for p in report.problems)


# -- AC5: owner reviews usefulness; accepted references are scoped and versioned --------


def test_no_owner_review_means_not_deliverable_however_green() -> None:
    report = wq.evaluate_website(probe=Scripted(probe()), artifact_sha256=SHA)
    assert report.automated_pass() is True
    assert report.deliverable() is False
    assert "no owner review" in " ".join(report.reasons())


def test_an_agent_cannot_review_its_own_work() -> None:
    with pytest.raises(wq.OwnerReviewRefused, match="not a person"):
        wq.OwnerReview(actor="agent", statement="looks great", useful=True)


def test_the_review_actor_must_be_namespaced() -> None:
    with pytest.raises(wq.OwnerReviewRefused, match="must be namespaced"):
        wq.OwnerReview(actor="priya", statement="looks great", useful=True)


def test_an_owner_who_says_it_is_not_useful_blocks_delivery() -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe()), artifact_sha256=SHA, owner_review=owner(useful=False)
    )
    assert report.deliverable() is False
    assert "owner judged the site not useful" in report.reasons()


def test_an_accepted_reference_must_be_versioned() -> None:
    with pytest.raises(wq.OwnerReviewRefused, match="versioned id"):
        wq.OwnerReview(
            actor="owner:priya", statement="use this one again", useful=True,
            accepted_reference="reference/cafe-site",
        )


def test_an_accepted_reference_is_recorded_but_grants_nothing() -> None:
    review = owner(accepted_reference="reference/cafe-site@v2")
    mapping = review.to_mapping()
    assert mapping["accepted_reference"] == "reference/cafe-site@v2"
    assert mapping["is_final_acceptance"] is False
    assert mapping["grants_publish_permission"] is False


def test_the_shortlist_reuses_ap020_ranking_rather_than_reimplementing_it() -> None:
    """Two answers to 'which examples apply here' would be one too many."""
    import reference_benchmarks as rb

    approval = rb.OwnerApproval(
        actor="owner:priya", statement="reuse this style", granted=("use_as_reference",)
    )
    references = [
        rb.accept(
            reference_id="ref/cafe", version=1, project_scope_id="proj-1", task_kind="website",
            brief="a café site", artifact=rb.ArtifactVersion(artifact_id="index.html", content_sha256="b" * 64),
            approval=approval, audience="café owners", language="en", style_tags=("calm",),
        )
    ]
    shortlist = wq.reference_shortlist(
        references, task_kind="website", audience="café owners", language="en", style_tags=["calm"]
    )
    assert shortlist[0]["reference"] == "ref/cafe@v1"
    assert "same task kind" in shortlist[0]["reasons"]


# -- AC6: report every run, intervention, cost and time; no revenue claims --------------


def test_a_revenue_claim_is_refused_by_name() -> None:
    with pytest.raises(wq.ClaimRefused, match="not a claim this programme can measure"):
        wq.refuse_claims(["revenue"])


def test_every_forbidden_claim_is_refused() -> None:
    for claim in wq.FORBIDDEN_CLAIMS:
        with pytest.raises(wq.ClaimRefused):
            wq.refuse_claims([claim])


def test_the_report_states_which_claims_it_did_not_make() -> None:
    report = wq.evaluate_website(probe=Scripted(probe()), artifact_sha256=SHA, owner_review=owner())
    mapping = report.to_mapping()
    assert mapping["claims_made"] == []
    assert mapping["claims_refused"] == list(wq.FORBIDDEN_CLAIMS)


def test_every_evaluated_run_is_reported_not_just_the_best() -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe()), artifact_sha256=SHA, owner_review=owner(),
        runs_evaluated=3, intervention_minutes=42.5,
    )
    mapping = report.to_mapping()
    assert mapping["runs_evaluated"] == 3
    assert mapping["intervention_minutes"] == 42.5


def test_runs_evaluated_cannot_be_zero_or_a_boolean() -> None:
    for bad in (0, True):
        with pytest.raises(wq.WebsiteQualityError, match="runs_evaluated must be a positive integer"):
            wq.evaluate_website(probe=Scripted(probe()), artifact_sha256=SHA, runs_evaluated=bad)


def test_limitations_are_carried_into_the_report() -> None:
    report = wq.evaluate_website(
        probe=Scripted(probe()), artifact_sha256=SHA, owner_review=owner(),
        limitations=("no screen-reader certification was performed",),
    )
    assert report.to_mapping()["limitations"] == ["no screen-reader certification was performed"]


# -- integrity -------------------------------------------------------------------------


def test_a_report_about_a_different_artifact_is_refused() -> None:
    with pytest.raises(wq.ProbeRefused, match="different artifact"):
        wq.evaluate_website(
            probe=Scripted(probe(digest="b" * 64)), artifact_sha256=SHA, owner_review=owner()
        )


def test_the_digest_must_be_a_digest() -> None:
    with pytest.raises(wq.WebsiteQualityError, match="lowercase SHA-256"):
        wq.evaluate_website(probe=Scripted(probe()), artifact_sha256="not-a-digest")


def test_the_fingerprint_changes_when_the_verdict_changes() -> None:
    good = wq.evaluate_website(probe=Scripted(probe()), artifact_sha256=SHA, owner_review=owner())
    bad = wq.evaluate_website(
        probe=Scripted(probe(layout_failing=("phone-390x844@1x",))), artifact_sha256=SHA,
        owner_review=owner(),
    )
    assert good.fingerprint() != bad.fingerprint()


def test_a_fully_green_report_with_an_owner_is_deliverable() -> None:
    report = wq.evaluate_website(
        probe=Scripted(
            probe(interactions=[{"name": "contact form", "kind": "form", "passed": True, "detail": "submitted and acknowledged"}])
        ),
        artifact_sha256=SHA,
        owner_review=owner(accepted_reference="reference/cafe-site@v1"),
    )
    assert report.deliverable() is True
    assert report.to_mapping()["deliverable"] is True


# -- discipline ------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_nothing() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("website_quality.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "json", "re", "dataclasses", "typing", "reference_benchmarks"}
    for forbidden in ("datetime", "time", "os", "pathlib", "subprocess", "socket", "requests", "urllib"):
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

    shared = top_level("website_quality.py") & top_level("app.py")
    assert shared == set(), f"these would collide in the single-file build: {sorted(shared)}"


def test_masking_keeps_a_secret_out_of_the_finding() -> None:
    masked = wq.mask_secret("sk-abcdefghijklmnopqrstuvwx")
    assert masked.startswith("sk-a") and masked.endswith("uvwx")
    assert "bcdefghijklmnopqrst" not in masked


def test_documents_of_the_wrong_shape_are_refused_rather_than_skipped() -> None:
    """A probe returning a list would otherwise bypass both scanners entirely.

    "No documents" and "no findings" must not look the same; that is the same rule as a missing
    criterion, applied to the text the scanners read.
    """
    payload = probe()
    payload["documents"] = ["<p>Lorem ipsum</p>"]
    with pytest.raises(wq.ProbeRefused, match="documents must be a mapping"):
        wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())


def test_a_non_text_document_is_refused() -> None:
    payload = probe()
    payload["documents"] = {"index.html": b"<p>bytes</p>"}
    with pytest.raises(wq.ProbeRefused, match="must be text"):
        wq.evaluate_website(probe=Scripted(payload), artifact_sha256=SHA, owner_review=owner())
