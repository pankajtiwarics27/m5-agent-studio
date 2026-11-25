"""AP-024: qualifying a rendered video technically and editorially, without inventing the verdict.

The tests are organised around refusals. A video that decodes is not a video worth publishing, and
the interesting behaviour here is every way a green-looking review is stopped: a check that never
ran, a frame defect with no position, a score from a machine, a "final" review of the first thirty
seconds, and a monetisation promise nobody can keep.
"""
from __future__ import annotations

import pytest

import autonomous_production_research as research
import video_draft as vd
import video_quality as vq
import website_draft as wd

SHA = "e" * 64
OTHER_SHA = "f" * 64


# -- fixtures ---------------------------------------------------------------------------


def bundle(*ids: str) -> research.ResearchBundle:
    ledger = research.ResearchBundle()
    for source_id in ids:
        ledger.add_source(
            research.SourceRecord(
                source_id=source_id,
                url=f"https://en.wikipedia.org/wiki/{source_id}",
                final_url=None,
                retrieved_at="2026-11-01T09:00:00Z",
                http_status=200,
                retrieval_status="ok",
                content_sha256="d" * 64,
                excerpt="an excerpt",
                publication_date="2026-10-30",
                byte_count=1200,
                reason=None,
            )
        )
    return ledger


def topic() -> vd.TopicPopularity:
    return vd.assess_topic(
        "small language models on laptops",
        [
            vd.TopicSignal(source_id="src-a", as_of="2026-11-05", region="US", metric="interest", value=71.0),
            vd.TopicSignal(source_id="src-b", as_of="2026-11-06", region="US", metric="interest", value=64.0),
        ],
        bundle=bundle("src-a", "src-b"),
        region="US",
        as_of="2026-11-10",
    )


def claim(text: str = "A 7B model runs at 40 tok/s on this laptop", **kw) -> vd.NarrationClaim:
    return vd.NarrationClaim(**{"text": text, "source_ids": ("src-a",), "important": True, **kw})


def asset(path: str = "footage/screen.mp4", **kw) -> vd.MediaAsset:
    return vd.MediaAsset(
        asset=wd.AssetProvenance(**{"path": path, "role": "footage", "basis": "authored_in_run", **kw}),
        role="footage",
    )


def score(axis: str, value: int = 4) -> vq.EditorialAxisScore:
    return vq.EditorialAxisScore(axis=axis, score=value, evidence=f"watched {axis} at 0:12")


def editorial(**kw) -> vq.EditorialReview:
    return vq.EditorialReview(
        **{
            "actor": "reviewer:sam",
            "scores": tuple(score(axis) for axis in vq.EDITORIAL_AXES),
            "watched_before_scoring": True,
            **kw,
        }
    )


def coverage(*ranges: tuple[float, float], duration: float = 24.0) -> vq.WatchedCoverage:
    spans = ranges or ((0.0, duration),)
    return vq.WatchedCoverage(
        duration_seconds=duration,
        ranges=tuple(vq.WatchedRange(start_seconds=s, end_seconds=e) for s, e in spans),
    )


class Probe:
    """A media probe that reports prepared measurements and records what it was asked to inspect."""

    def __init__(
        self,
        *,
        digest: str = SHA,
        duration: float = 24.0,
        width: int = 1080,
        height: int = 1920,
        aspect: str = "9:16",
        decodes: bool = True,
        black: int = 0,
        frozen: int = 0,
        missing: int = 0,
        first_defect: float | None = None,
        lufs: float = -14.0,
        peak: float = -1.5,
        clipping: int = 0,
        cues: int = 8,
        out_of_bounds: int = 0,
        overlaps: int = 0,
        unreadable: int = 0,
        failed: tuple[str, ...] = (),
        omitted: tuple[str, ...] = (),
    ) -> None:
        self.digest = digest
        self.duration = duration
        self.width = width
        self.height = height
        self.aspect = aspect
        self.decodes = decodes
        self.black = black
        self.frozen = frozen
        self.missing = missing
        self.first_defect = first_defect
        self.lufs = lufs
        self.peak = peak
        self.clipping = clipping
        self.cues = cues
        self.out_of_bounds = out_of_bounds
        self.overlaps = overlaps
        self.unreadable = unreadable
        self.failed = failed
        self.omitted = omitted
        self.calls: list[tuple[str, str]] = []

    def inspect(self, path, sha256):
        self.calls.append((path, sha256))
        checks = {
            name: name not in self.failed
            for name in (*vq.MEDIA_CHECKS, *vq.AUDIO_CHECKS)
            if name not in self.omitted
        }
        return {
            "sha256": self.digest,
            "duration_seconds": self.duration,
            "width": self.width,
            "height": self.height,
            "aspect": self.aspect,
            "decodes": self.decodes,
            "frames": {
                "black_frames": self.black,
                "frozen_frames": self.frozen,
                "missing_frames": self.missing,
                "first_defect_seconds": self.first_defect,
            },
            "audio": {
                "integrated_lufs": self.lufs,
                "true_peak_dbtp": self.peak,
                "clipping_events": self.clipping,
                "silence_seconds": 0.0,
            },
            "subtitles": {
                "cue_count": self.cues,
                "out_of_bounds": self.out_of_bounds,
                "overlaps": self.overlaps,
                "unreadable_cues": self.unreadable,
            },
            "checks": checks,
        }


def review(**kw):
    """A review that satisfies every rule, so a test can break exactly one."""
    options = {
        "probe": Probe(),
        "artifact_sha256": SHA,
        "path": "/tmp/out/draft.mp4",
        "topic": topic(),
        "bundle": bundle("src-a", "src-b"),
        "claims": (claim(),),
        "assets": (asset(),),
        "coverage": coverage(),
        "editorial": editorial(),
        **kw,
    }
    return vq.evaluate_video(**options)


# -- AC1: the file itself ---------------------------------------------------------------


def test_a_clean_pilot_is_ready_and_says_what_it_measured() -> None:
    result = review()
    assert result.ready() is True
    report = result.to_mapping()
    assert report["media"]["frames"]["defect_count"] == 0
    assert report["media"]["decodes"] is True
    assert report["ready"] is True
    assert report["upload_granted"] is False


def test_a_file_that_does_not_decode_is_never_ready() -> None:
    result = review(probe=Probe(decodes=False))
    assert result.ready() is False
    assert "does not decode" in result.readiness()[0]


def test_dimensions_that_contradict_the_claimed_aspect_are_a_failure() -> None:
    result = review(probe=Probe(width=1920, height=1080, aspect="9:16"))
    assert result.ready() is False
    assert any("claims 9:16" in reason for reason in result.readiness())


def test_black_frozen_and_missing_frames_are_counted_separately_with_a_position() -> None:
    result = review(probe=Probe(black=2, frozen=1, missing=0, first_defect=6.5))
    assert result.ready() is False
    defect = next(r for r in result.readiness() if "frame scan found" in r)
    assert "2 black" in defect and "1 frozen" in defect and "0 missing" in defect
    assert "6.5s" in defect


def test_a_defect_count_with_no_position_is_refused() -> None:
    with pytest.raises(vq.MediaRefused, match="must say where the first one is"):
        review(probe=Probe(black=3, first_defect=None))


def test_a_check_that_never_ran_is_not_a_check_that_passed() -> None:
    result = review(probe=Probe(omitted=("no_frozen_frames",)))
    assert result.ready() is False
    assert any("never ran: no_frozen_frames" in reason for reason in result.readiness())


def test_a_failed_check_names_itself() -> None:
    result = review(probe=Probe(failed=("subtitle_timing",)))
    assert result.ready() is False
    assert "media check failed: subtitle_timing" in result.readiness()


def test_an_invented_check_measures_nothing() -> None:
    probe = Probe()
    original = probe.inspect

    def tampered(path, sha256):
        facts = original(path, sha256)
        facts["checks"]["looks_nice"] = True
        return facts

    probe.inspect = tampered  # type: ignore[method-assign]
    with pytest.raises(vq.MediaRefused, match="invented check"):
        review(probe=probe)


def test_a_subtitle_scan_with_no_cues_is_not_a_passing_scan() -> None:
    with pytest.raises(vq.MediaRefused, match="no cues is not a passing scan"):
        review(probe=Probe(cues=0))


def test_subtitle_defects_are_reported_with_their_kinds() -> None:
    result = review(probe=Probe(out_of_bounds=2, overlaps=1, unreadable=3))
    assert result.ready() is False
    reason = next(r for r in result.readiness() if "subtitle scan is not clean" in r)
    assert "2 out of bounds" in reason and "1 overlapping" in reason and "3 unreadable" in reason


# -- AC2: audio -------------------------------------------------------------------------


def test_loudness_outside_the_band_blocks_readiness() -> None:
    result = review(probe=Probe(lufs=-24.0))
    assert result.ready() is False
    assert any("outside the" in reason for reason in result.readiness())


def test_a_true_peak_above_the_ceiling_blocks_readiness() -> None:
    result = review(probe=Probe(peak=0.4))
    assert result.ready() is False
    assert any("exceeds the" in reason for reason in result.readiness())


def test_clipping_is_counted_rather_than_absorbed_into_the_peak() -> None:
    result = review(probe=Probe(clipping=4))
    assert result.ready() is False
    assert any("clips 4 times" in reason for reason in result.readiness())


def test_the_loudness_band_and_ceiling_are_named_values() -> None:
    assert vq.LOUDNESS_TARGET_LUFS == (-16.0, -12.0)
    assert vq.TRUE_PEAK_CEILING_DBTP == -1.0
    assert vq.AudioLevels(integrated_lufs=-14.0, true_peak_dbtp=-2.0).loudness_in_range() is True
    assert vq.AudioLevels(integrated_lufs=-14.0, true_peak_dbtp=0.0).peak_ok() is False


def test_pronunciation_and_script_match_are_recorded_checks_not_measurements() -> None:
    result = review(probe=Probe(failed=("pronunciation_reviewed",)))
    assert result.ready() is False
    assert "media check failed: pronunciation_reviewed" in result.readiness()
    failed = review(probe=Probe(failed=("narration_matches_script",)))
    assert "media check failed: narration_matches_script" in failed.readiness()


# -- AC3: editorial rubric -------------------------------------------------------------


def test_every_axis_needs_a_score_and_an_absence_is_not_a_pass() -> None:
    with pytest.raises(vq.EditorialRefused, match="no score"):
        vq.EditorialReview(
            actor="reviewer:sam",
            scores=(score("pacing"),),
            watched_before_scoring=True,
        )


def test_an_axis_nobody_named_cannot_be_scored() -> None:
    with pytest.raises(vq.EditorialRefused, match="axis .* is unknown"):
        vq.EditorialAxisScore(axis="vibes", score=4, evidence="felt fine")


def test_a_score_without_an_anchor_value_is_refused() -> None:
    with pytest.raises(vq.EditorialRefused, match="integer 1-5"):
        vq.EditorialAxisScore(axis="pacing", score=7, evidence="very good")
    with pytest.raises(vq.EditorialRefused, match="integer 1-5"):
        vq.EditorialAxisScore(axis="pacing", score=True, evidence="very good")  # type: ignore[arg-type]


def test_every_score_carries_its_anchor_and_its_evidence() -> None:
    reviewed = editorial()
    mapping = reviewed.to_mapping()
    assert mapping["scores"][0]["anchor"] == vq.EDITORIAL_ANCHORS[4]
    assert all(row["evidence"] for row in mapping["scores"])
    assert mapping["blocking_axes"] == []


def test_a_score_below_the_floor_blocks_readiness() -> None:
    weak = editorial(scores=tuple(score(axis, 4 if axis == "pacing" else 2) for axis in vq.EDITORIAL_AXES))
    result = review(editorial=weak)
    assert result.ready() is False
    assert any("below the floor" in reason for reason in result.readiness())


def test_a_machine_cannot_score_its_own_video() -> None:
    with pytest.raises(vq.EditorialRefused, match="not a named person"):
        editorial(actor="agent:planner")
    with pytest.raises(vq.EditorialRefused, match="not a named person"):
        editorial(actor="reviewer:")


def test_a_score_given_before_watching_describes_the_expectation() -> None:
    with pytest.raises(vq.EditorialRefused, match="before watching"):
        editorial(watched_before_scoring=False)


def test_an_axis_scored_twice_has_one_judgement_only() -> None:
    with pytest.raises(vq.EditorialRefused, match="scored twice"):
        editorial(
            scores=(
                score("pacing", 4),
                score("pacing", 2),
                score("scene_relevance"),
                score("originality"),
                score("audience_fit"),
            )
        )


def test_no_editorial_review_means_not_ready_however_clean_the_file() -> None:
    result = review(editorial=None)
    assert result.ready() is False
    assert "no editorial review" in result.readiness()[-1]


# -- AC4: rights and consequential claims block readiness -------------------------------


def test_unresolved_rights_block_readiness_whatever_the_scores() -> None:
    unresolved = vd.MediaAsset(
        asset=wd.AssetProvenance(
            path="music/bed.mp3",
            role="music",
            basis="unknown",
            source="shared folder",
            license_id="pending:owner",
        ),
        role="music",
    )
    result = review(assets=(asset(), unresolved))
    assert result.ready() is False
    assert any("pending:owner" in reason for reason in result.readiness())


def test_a_consequential_claim_citing_an_unretrieved_source_blocks_readiness() -> None:
    # AP-023 already refuses a no-source claim at construction; this is the later gate catching a
    # citation that never resolved, which a reviewer cannot see by watching.
    bare = vd.NarrationClaim(
        text="This is the fastest laptop ever made", source_ids=("src-ghost",), important=True
    )
    result = review(claims=(claim(), bare))
    assert result.ready() is False
    assert any("no evidence" in reason for reason in result.readiness())


def test_ap023_still_refuses_a_consequential_claim_with_no_source_at_all() -> None:
    with pytest.raises(vd.EvidenceRefused, match="names no source"):
        vd.NarrationClaim(text="This is the fastest laptop ever made", important=True)


def test_the_review_needs_the_bundle_rather_than_trusting_the_caller() -> None:
    with pytest.raises(vq.ReadinessRefused, match="ResearchBundle"):
        review(bundle=None)


def test_an_unsourced_non_consequential_claim_does_not_block() -> None:
    aside = vd.NarrationClaim(text="Let's open the terminal", important=False)
    assert review(claims=(claim(), aside)).ready() is True


def test_an_unmet_platform_constraint_blocks_and_says_what_is_missing() -> None:
    constraint = vq.PlatformConstraint(
        platform="short-form video platform",
        constraint="overtly sexual or violent thumbnail imagery is prohibited",
        met=False,
        evidence="thumbnail 4 contains a stylised weapon; needs replacement",
    )
    result = review(constraints=(constraint,))
    assert result.ready() is False
    assert any("unmet platform constraint" in reason for reason in result.readiness())


def test_an_unmet_constraint_with_no_stated_gap_is_refused() -> None:
    with pytest.raises(vq.VideoQualityError, match="unmet and unnamed"):
        vq.PlatformConstraint(platform="a platform", constraint="a rule", met=False)


# -- AC5: the whole pilot is watched ---------------------------------------------------


def test_watched_coverage_is_measured_on_the_timeline() -> None:
    partial = coverage((0.0, 10.0))
    assert partial.is_complete() is False
    assert partial.gaps() == ((10.0, 24.0),)
    assert partial.coverage_ratio() == pytest.approx(0.417)
    assert coverage((0.0, 12.0), (12.0, 24.0)).is_complete() is True


def test_a_final_review_of_a_sample_is_refused_not_softened() -> None:
    with pytest.raises(vq.CoverageRefused, match="record it as a sample"):
        review(coverage=coverage((0.0, 9.0)), editorial=editorial(kind="final"))


def test_a_sample_review_is_recorded_and_visibly_insufficient() -> None:
    result = review(coverage=coverage((0.0, 9.0)), editorial=editorial(kind="sample"))
    assert result.ready() is False
    assert result.watched_sample_only() is True
    assert any("is a sample of the pilot" in reason for reason in result.readiness())
    assert result.to_mapping()["editorial"]["kind"] == "sample"


def test_coverage_of_a_different_duration_means_a_different_video() -> None:
    with pytest.raises(vq.CoverageRefused, match="reviewer watched something else"):
        review(coverage=coverage(duration=90.0))


def test_a_range_nobody_could_have_watched_is_refused() -> None:
    with pytest.raises(vq.CoverageRefused, match="nobody could have watched"):
        vq.WatchedCoverage(
            duration_seconds=24.0,
            ranges=(vq.WatchedRange(start_seconds=0.0, end_seconds=100.0),),
        )


def test_an_empty_watched_range_is_refused() -> None:
    with pytest.raises(vq.CoverageRefused, match="end after it starts"):
        vq.WatchedRange(start_seconds=5.0, end_seconds=5.0)


# -- AC6: reporting, no guarantees ------------------------------------------------------


def test_the_report_states_the_guarantees_it_did_not_make() -> None:
    report = review().to_mapping()
    assert report["guarantees_made"] == []
    assert report["guarantees_refused"] == list(vq.FORBIDDEN_GUARANTEES)


def test_a_monetisation_promise_is_refused_by_name() -> None:
    with pytest.raises(vq.GuaranteeRefused, match="can promise"):
        vq.refuse_guarantees(["monetisation"])


def test_every_forbidden_guarantee_is_refused() -> None:
    for guarantee in vq.FORBIDDEN_GUARANTEES:
        with pytest.raises(vq.GuaranteeRefused):
            vq.refuse_guarantees([guarantee])


def test_every_pilot_and_its_cost_is_reported() -> None:
    result = review(runs_evaluated=4, intervention_minutes=33.0, limitations=["one narrator", "US English only"])
    report = result.to_mapping()
    assert report["runs_evaluated"] == 4
    assert report["intervention_minutes"] == 33.0
    assert report["limitations"] == ["one narrator", "US English only"]
    with pytest.raises(vq.VideoQualityError, match="runs_evaluated"):
        review(runs_evaluated=0)


def test_a_probe_that_measured_another_file_is_refused() -> None:
    with pytest.raises(vq.ProbeRefused, match="different file"):
        review(probe=Probe(digest=OTHER_SHA))


def test_a_probe_that_omits_its_checks_is_refused() -> None:
    probe = Probe()

    def tampered(path, sha256):
        facts = probe.inspect.__self__ and None
        del facts
        return {}

    class Bare:
        def inspect(self, path, sha256):
            return {"sha256": SHA, "duration_seconds": 24.0}

    with pytest.raises(vq.ProbeRefused, match="checks mapping"):
        review(probe=Bare())


# -- discipline ------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_nothing() -> None:
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("video_quality.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # AP-024 composes AP-023's draft contract and AP-009's research bundle rather than
    # re-implementing either.
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "typing",
        "autonomous_production_research",
        "video_draft",
    }
    for forbidden in ("time", "datetime", "os", "pathlib", "subprocess", "socket", "requests", "httpx"):
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

    shared = top_level("video_quality.py") & top_level("app.py")
    assert shared == set(), f"these names would collide in the single-file build: {sorted(shared)}"


def test_the_module_has_no_publish_or_revenue_surface() -> None:
    assert not hasattr(vq, "publish")
    assert not hasattr(vq, "upload")
    assert not hasattr(vq, "revenue")
