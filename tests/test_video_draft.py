"""AP-023: a sourced, original short-video draft, and the six things it must refuse to invent.

The tests are organised around refusals, because rendering an MP4 is the part ffmpeg already does.
What is hard is producing one whose topic claim is real, whose script still serves the approved
direction, whose every asset can be used, and whose captions line up with the file. Each test
below breaks one of those on purpose, so a test that stopped looking would fail rather than pass
vacuously.
"""
from __future__ import annotations

import pytest

import autonomous_production_research as research
import video_draft as vd
import website_draft as wd

SHA = "b" * 64
OTHER_SHA = "c" * 64


# -- fixtures ---------------------------------------------------------------------------


def bundle(*ids: str, metadata: object = None) -> research.ResearchBundle:
    """A research bundle holding the named sources, built through its own ledger API."""
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
    if metadata is not None:
        ledger.metadata = metadata  # type: ignore[attr-defined]
    return ledger


def signal(source_id: str = "src-a", **kw) -> vd.TopicSignal:
    return vd.TopicSignal(
        **{
            "source_id": source_id,
            "as_of": "2026-11-05",
            "region": "US",
            "metric": "search_interest_index",
            "value": 71.0,
            **kw,
        }
    )


def popularity(status: str = "supported") -> vd.TopicPopularity:
    if status == "supported":
        return vd.assess_topic(
            "small language models on laptops",
            [signal("src-a"), signal("src-b")],
            bundle=bundle("src-a", "src-b"),
            region="US",
            as_of="2026-11-10",
        )
    if status == "candidate":
        return vd.assess_topic(
            "small language models on laptops",
            [signal("src-a")],
            bundle=bundle("src-a"),
            region="US",
            as_of="2026-11-10",
        )
    return vd.assess_topic(
        "small language models on laptops",
        [],
        bundle=bundle("src-a"),
        region="US",
        as_of="2026-11-10",
    )


def direction(**kw) -> vd.CreativeDirection:
    return vd.CreativeDirection(
        **{
            "angle": "explain why local inference changed the laptop buying decision",
            "voice": "dry, factual, first person, no hype adjectives",
            "visual_direction": "screen recordings, no stock footage of people",
            "required_elements": ["a measured tokens-per-second number"],
            "forbidden_moves": ["calling any model 'the best'"],
            **kw,
        }
    )


def claim(text: str = "A 7B model runs at 40 tok/s on this laptop", **kw) -> vd.NarrationClaim:
    return vd.NarrationClaim(**{"text": text, "source_ids": ("src-a",), "important": True, **kw})


def scene(scene_id: str = "scene-1", **kw) -> vd.StoryboardScene:
    return vd.StoryboardScene(
        **{
            "scene_id": scene_id,
            "seconds": 6.0,
            "purpose": "open on the measured number",
            "direction_element": "a measured tokens-per-second number",
            **kw,
        }
    )


def script(**kw) -> vd.Script:
    return vd.Script(
        **{
            "direction": direction(),
            "narration": ["A 7B model runs at 40 tok/s on this laptop."],
            "scenes": [
                scene("scene-1"),
                scene("scene-2", direction_element="angle"),
                scene("scene-3", direction_element="voice"),
                scene("scene-4", direction_element="visual_direction"),
            ],
            **kw,
        }
    )


def asset(path: str = "footage/screen-recording.mp4", **kw) -> vd.MediaAsset:
    provenance = wd.AssetProvenance(
        **{
            "path": path,
            "role": "footage",
            "basis": "authored_in_run",
            **kw,
        }
    )
    role = str(kw.pop("media_role", "footage")) if "media_role" in kw else "footage"
    return vd.MediaAsset(asset=provenance, role=role)


def voice_asset(path: str = "audio/voiceover.wav", **kw) -> vd.MediaAsset:
    provenance = wd.AssetProvenance(
        path=path,
        role="voice",
        basis=str(kw.pop("basis", "authored_in_run")),
        source=str(kw.pop("source", "")),
        license_id=str(kw.pop("license_id", "")),
    )
    consent = vd.VoiceConsent(**{"kind": "self_recorded", "subject": "the owner", **kw})
    return vd.MediaAsset(asset=provenance, role="voice", voice=consent)


def audit(*, reused: str = "cli_tool") -> vd.AdapterAudit:
    return vd.audit_existing_integrations(
        [
            vd.AdapterDisposition(
                surface=surface,
                disposition="reused" if surface == reused else "unsuitable",
                reason="" if surface == reused else "this draft renders one file through the CLI",
            )
            for surface in vd.EXISTING_MONEYPRINTER_SURFACES
        ],
        candidate=reused,
        candidate_kind="existing_surface",
    )


def captions(count: int = 2, *, until: float = 4.0) -> list[dict]:
    step = until / count
    return [
        {"start": round(i * step, 3), "end": round((i + 1) * step, 3), "text": f"cue {i}"}
        for i in range(count)
    ]


class Renderer:
    """A renderer that returns prepared bytes facts and records the spec it was handed."""

    def __init__(self, path: str = "/tmp/out/draft.mp4", digest: str = SHA, duration: float = 24.0) -> None:
        self.path = path
        self.digest = digest
        self.duration = duration
        self.specs: list[dict] = []

    def render(self, spec):
        self.specs.append(dict(spec))
        return {"path": self.path, "sha256": self.digest, "duration_seconds": self.duration}


class Probe:
    """A playback probe that reports prepared facts about the rendered file."""

    def __init__(
        self,
        *,
        digest: str = SHA,
        duration: float = 24.0,
        playable: bool = True,
        cues: list[dict] | None = None,
        detail: str = "h264 1080x1920, aac 48k",
    ) -> None:
        self.digest = digest
        self.duration = duration
        self.playable = playable
        self.cues = captions() if cues is None else cues
        self.detail = detail
        self.calls: list[tuple[str, str]] = []

    def inspect(self, path, sha256):
        self.calls.append((path, sha256))
        return {
            "sha256": self.digest,
            "duration_seconds": self.duration,
            "playable": self.playable,
            "captions": self.cues,
            "detail": self.detail,
        }


def render(**kw):
    """Render a draft that satisfies every rule, then let a test break exactly one."""
    options = {
        "renderer": Renderer(),
        "probe": Probe(),
        "bundle": bundle("src-a", "src-b"),
        "topic": popularity(),
        "direction": direction(),
        "script": script(),
        "assets": (asset(), voice_asset()),
        "audit": audit(),
        **kw,
    }
    return vd.render_video_draft(**options)


# -- AC1: topic popularity is dated, regional and sourced, or labelled -------------------


def test_a_topic_with_two_dated_regional_sources_is_supported() -> None:
    result = popularity()
    assert result.status == "supported"
    assert result.may_be_narrated_as_trending is True
    assert {s.source_id for s in result.signals} == {"src-a", "src-b"}


def test_one_source_is_a_candidate_and_says_why() -> None:
    result = vd.assess_topic(
        "small language models on laptops",
        [signal("src-a")],
        bundle=bundle("src-a"),
        region="US",
        as_of="2026-11-10",
    )
    assert result.status == "candidate"
    assert result.may_be_narrated_as_trending is False
    assert "one dated source" in result.reason


def test_no_signal_is_unknown_rather_than_assumed() -> None:
    result = popularity("unknown")
    assert result.status == "unknown"
    assert result.signals == ()
    assert "may not call the topic trending" in result.reason


def test_a_signal_from_a_source_the_run_never_retrieved_is_refused() -> None:
    with pytest.raises(vd.TrendRefused, match="not in the research bundle"):
        vd.assess_topic(
            "topic",
            [signal("src-ghost")],
            bundle=bundle("src-a"),
            region="US",
            as_of="2026-11-10",
        )


def test_a_signal_dated_after_the_assessment_is_refused() -> None:
    with pytest.raises(vd.TrendRefused, match="after the assessment date"):
        vd.assess_topic(
            "topic",
            [signal("src-a", as_of="2026-12-01")],
            bundle=bundle("src-a"),
            region="US",
            as_of="2026-11-10",
        )


def test_a_signal_from_another_market_is_not_popularity_here() -> None:
    with pytest.raises(vd.TrendRefused, match="not 'US'"):
        vd.assess_topic(
            "topic",
            [signal("src-a", region="DE"), signal("src-b", region="DE")],
            bundle=bundle("src-a", "src-b"),
            region="US",
            as_of="2026-11-10",
        )


def test_a_stale_signal_falls_out_of_the_window_instead_of_counting() -> None:
    result = vd.assess_topic(
        "topic",
        [signal("src-a", as_of="2026-01-01"), signal("src-b")],
        bundle=bundle("src-a", "src-b"),
        region="US",
        as_of="2026-11-10",
    )
    assert result.status == "candidate"
    assert {s.source_id for s in result.signals} == {"src-b"}


def test_a_signal_needs_a_region_a_date_and_a_number() -> None:
    with pytest.raises(vd.TrendRefused, match="not a region"):
        signal(region="everywhere")
    with pytest.raises(vd.TrendRefused, match="value must be a number"):
        signal(value="lots")  # type: ignore[arg-type]
    with pytest.raises(vd.VideoDraftError, match="ISO date"):
        signal(as_of="last tuesday")


def test_a_labelled_candidate_cannot_be_narrated_as_trending() -> None:
    with pytest.raises(vd.TrendRefused, match="while the popularity status is 'candidate'"):
        render(
            topic=popularity("candidate"),
            script=script(narration=["This topic is trending everywhere right now."]),
        )


def test_denying_the_trend_is_the_honest_sentence_and_is_not_refused() -> None:
    draft = render(
        topic=popularity("candidate"),
        script=script(narration=["This topic is not trending, and here is the one source that mentions it."]),
    )
    assert draft.topic.status == "candidate"
    assert draft.to_mapping()["topic"]["status"] == "candidate"


def test_asserting_a_trend_after_a_denial_in_another_clause_is_still_refused() -> None:
    with pytest.raises(vd.TrendRefused, match="while the popularity status is 'unknown'"):
        render(
            topic=popularity("unknown"),
            script=script(narration=["It is not hype: this topic is trending across every feed."]),
        )


# -- AC2: evidence links, contradictions and uncertainty stay visible -------------------


def test_an_important_claim_without_a_source_is_refused_at_record_time() -> None:
    with pytest.raises(vd.EvidenceRefused, match="important claim"):
        vd.NarrationClaim(text="The battery lasts eleven hours", important=True)


def test_a_supported_claim_links_to_a_source_in_the_bundle() -> None:
    ledger = bundle("src-a")
    assert claim().is_supported(ledger) is True
    assert claim(source_ids=("src-ghost",)).is_supported(ledger) is False


def test_an_important_claim_citing_a_source_the_run_never_read_is_refused() -> None:
    with pytest.raises(vd.EvidenceRefused, match="never retrieved"):
        render(script=script(scenes=[scene("scene-1", claims=[claim(source_ids=("src-ghost",))])]))


def test_the_render_needs_the_bundle_rather_than_being_asked_to_trust_the_caller() -> None:
    with pytest.raises(vd.EvidenceRefused, match="needs the ResearchBundle"):
        render(bundle=None)


def test_a_contradicted_claim_the_script_hides_is_refused() -> None:
    ledger = bundle("src-a", "src-b")
    record = ledger.add_claim("The laptop lasts eleven hours", supporting=["src-a"])
    ledger.record_contradiction(record.claim_id, ["src-a", "src-b"], note="vendors disagree")
    contradictions = vd.contradicted_claim_texts(ledger)
    assert contradictions == ("The laptop lasts eleven hours",)
    with pytest.raises(vd.EvidenceRefused, match="states no uncertainty"):
        render(observed_contradictions=contradictions)


def test_a_contradiction_stated_in_the_script_stays_visible() -> None:
    draft = render(
        observed_contradictions=("The laptop lasts eleven hours",),
        script=script(uncertainties=["The laptop lasts eleven hours: two sources disagree."]),
    )
    assert draft.deliverable() is True
    assert draft.script.uncertainties[0].startswith("The laptop lasts eleven hours")


def test_contradicted_claim_texts_refuses_a_foreign_bundle() -> None:
    with pytest.raises(vd.EvidenceRefused, match="ResearchBundle"):
        vd.contradicted_claim_texts({"claims": []})  # type: ignore[arg-type]


# -- AC3: script and storyboard preserve the approved direction -------------------------


def test_a_direction_that_forbids_nothing_is_not_a_direction() -> None:
    with pytest.raises(vd.DirectionRefused, match="forbids nothing"):
        direction(forbidden_moves=[])
    with pytest.raises(vd.DirectionRefused, match="required_elements"):
        direction(required_elements=[])


def test_a_scene_that_serves_nothing_approved_is_drift() -> None:
    drifted = vd.StoryboardScene(
        scene_id="scene-9",
        seconds=4.0,
        purpose="mention a sponsor",
        direction_element="n/a",
    )
    with pytest.raises(vd.DirectionRefused, match="is not part of the approved direction"):
        vd.Script(direction=direction(), narration=["One line."], scenes=[drifted])


def test_every_direction_element_must_be_served_by_a_scene() -> None:
    partial = vd.Script(
        direction=direction(),
        narration=["One line."],
        scenes=[scene("scene-1")],
    )
    assert set(partial.uncovered_elements()) == {"angle", "voice", "visual_direction"}


def test_a_script_written_against_another_direction_needs_an_owner_change() -> None:
    approved = direction()
    drifted = direction(angle="list the ten best laptops")
    with pytest.raises(vd.DirectionRefused, match="no owner change is recorded"):
        render(direction=approved, script=script(direction=drifted))


def test_an_owner_can_change_the_direction_on_the_record() -> None:
    changed = vd.OwnerDirectionChange(
        actor="owner:pankaj",
        reason="the angle moved from buying advice to measurement",
        approved_at="2026-11-09",
    )
    draft = render(
        direction=direction(),
        script=script(direction=direction(angle="move to measurement"), direction_change=changed),
    )
    assert draft.to_mapping()["script"]["direction_change"]["actor"] == "owner:pankaj"


def test_a_machine_cannot_approve_a_direction_change() -> None:
    with pytest.raises(vd.DirectionRefused, match="is not a person"):
        vd.OwnerDirectionChange(actor="agent:opus", reason="better angle", approved_at="2026-11-09")
    with pytest.raises(vd.DirectionRefused, match="must name who"):
        vd.OwnerDirectionChange(actor="owner:", reason="better angle", approved_at="2026-11-09")


# -- AC4: existing MoneyPrinter contracts are audited before a second integration -------


def test_the_audit_must_cover_every_existing_surface() -> None:
    with pytest.raises(vd.SecondIntegrationRefused, match="skipped"):
        vd.AdapterAudit(
            candidate="cli_tool",
            candidate_kind="existing_surface",
            dispositions=(
                vd.AdapterDisposition(surface="cli_tool", disposition="reused"),
            ),
        )


def test_a_second_integration_is_refused_while_a_surface_is_reused() -> None:
    with pytest.raises(vd.SecondIntegrationRefused, match="second integration"):
        vd.audit_existing_integrations(
            [
                vd.AdapterDisposition(surface="cli_tool", disposition="reused"),
                vd.AdapterDisposition(surface="mcp_stdio_server", disposition="unsuitable", reason="stdio only"),
                vd.AdapterDisposition(surface="openai_gateway_profile", disposition="unsuitable", reason="chat only"),
            ],
            candidate="new_video_api",
            candidate_kind="http_client",
        )


def test_a_second_integration_is_permitted_only_when_nothing_existing_fits() -> None:
    result = vd.audit_existing_integrations(
        [
            vd.AdapterDisposition(surface="cli_tool", disposition="unsuitable", reason="requires uv on the host"),
            vd.AdapterDisposition(surface="mcp_stdio_server", disposition="unsuitable", reason="45s client budget"),
            vd.AdapterDisposition(surface="openai_gateway_profile", disposition="unsuitable", reason="chat only"),
        ],
        candidate="hosted_render_api",
        candidate_kind="http_client",
    )
    assert result.adds_second_integration is True
    assert result.reused == ()


def test_declaring_a_surface_unsuitable_needs_a_reason() -> None:
    with pytest.raises(vd.SecondIntegrationRefused, match="no reason"):
        vd.AdapterDisposition(surface="cli_tool", disposition="unsuitable")


def test_a_surface_that_does_not_exist_cannot_be_audited() -> None:
    with pytest.raises(vd.SecondIntegrationRefused, match="not an existing surface"):
        vd.AdapterDisposition(surface="invented_client", disposition="reused")


def test_the_audit_is_recorded_on_the_draft() -> None:
    draft = render()
    recorded = draft.to_mapping()["adapter_audit"]
    assert recorded["candidate"] == "cli_tool"
    assert recorded["adds_second_integration"] is False
    assert set(recorded["reused"]) == {"cli_tool"}


# -- AC5: every asset has rights and provenance; no unauthorised voice cloning ----------


def test_an_unknown_rights_asset_blocks_delivery() -> None:
    unresolved = vd.MediaAsset(
        asset=wd.AssetProvenance(
            path="music/bed.mp3",
            role="music",
            basis="unknown",
            license_id="pending:owner",
            source="found in a shared folder",
        ),
        role="music",
    )
    draft = render(assets=(asset(), voice_asset(), unresolved))
    assert draft.deliverable() is False
    assert any("rights unknown (pending:owner)" in reason for reason in draft.readiness())


def test_a_voice_asset_must_name_whose_voice_it_is() -> None:
    with pytest.raises(vd.RightsRefused, match="names no consent"):
        vd.MediaAsset(
            asset=wd.AssetProvenance(path="audio/vo.wav", role="voice", basis="authored_in_run"),
            role="voice",
        )


def test_unauthorised_voice_cloning_is_refused_by_name() -> None:
    with pytest.raises(vd.RightsRefused, match="cloning without consent"):
        vd.VoiceConsent(kind="cloned_from_youtube", subject="a podcast host")
    with pytest.raises(vd.RightsRefused, match="cloning without consent"):
        vd.VoiceConsent(kind="deepfake_impersonation", subject="a podcast host")


def test_cloned_voice_consent_needs_the_consent_record() -> None:
    with pytest.raises(vd.RightsRefused, match="needs the consent record"):
        vd.VoiceConsent(kind="owner_cloned_with_consent", subject="the owner")
    consent = vd.VoiceConsent(
        kind="owner_cloned_with_consent",
        subject="the owner",
        evidence="signed release stored at owners/voice-consent-2026-11.pdf",
    )
    assert consent.evidence.startswith("signed release")


def test_a_licence_claim_without_a_licence_is_refused_upstream() -> None:
    with pytest.raises(wd.ProvenanceRefused, match="names none"):
        wd.AssetProvenance(path="music/bed.mp3", role="music", basis="licensed", source="a library")


def test_a_non_voice_asset_cannot_carry_voice_consent() -> None:
    with pytest.raises(vd.RightsRefused, match="carries voice consent"):
        vd.MediaAsset(
            asset=wd.AssetProvenance(path="footage/a.mp4", role="footage", basis="authored_in_run"),
            role="footage",
            voice=vd.VoiceConsent(kind="self_recorded", subject="the owner"),
        )


def test_one_asset_path_is_recorded_once() -> None:
    with pytest.raises(vd.RightsRefused, match="one asset path, one record"):
        render(assets=(asset(), asset()))


# -- AC6: a real playable draft with captions; upload stays a separate grant -----------


def test_a_complete_draft_is_deliverable_and_says_what_it_is() -> None:
    draft = render()
    assert draft.deliverable() is True
    report = draft.to_mapping()
    assert report["artifact"]["playable"] is True
    assert len(report["artifact"]["captions"]) == 2
    assert report["upload_granted"] is False
    assert report["publication_granted"] is False


def test_a_file_that_did_not_probe_as_playable_is_not_a_draft() -> None:
    draft = render(probe=Probe(playable=False, detail="moov atom not found"))
    assert draft.deliverable() is False
    assert any("moov atom not found" in reason for reason in draft.readiness())


def test_a_draft_without_captions_is_not_the_package_the_story_promises() -> None:
    draft = render(probe=Probe(cues=[]))
    assert draft.deliverable() is False
    assert "no captions" in draft.readiness()[0]


def test_a_caption_scheduled_after_the_video_ends_is_refused() -> None:
    with pytest.raises(vd.PlaybackRefused, match="after the 24.0s"):
        render(probe=Probe(cues=[{"start": 20.0, "end": 90.0, "text": "too late"}]))


def test_overlapping_captions_are_refused() -> None:
    with pytest.raises(vd.PlaybackRefused, match="starts before the previous cue ends"):
        render(
            probe=Probe(
                cues=[
                    {"start": 0.0, "end": 5.0, "text": "first"},
                    {"start": 4.0, "end": 8.0, "text": "second"},
                ]
            )
        )


def test_a_renderer_and_a_probe_that_disagree_about_captions_are_refused() -> None:
    class Conflicting(Renderer):
        def render(self, spec):
            facts = super().render(spec)
            facts["captions"] = [{"start": 0.0, "end": 24.0, "text": "something else entirely"}]
            return facts

    with pytest.raises(vd.PlaybackRefused, match="disagree about what is said when"):
        render(renderer=Conflicting())


def test_a_probe_that_hashed_another_file_is_refused() -> None:
    with pytest.raises(vd.PlaybackRefused, match="different file"):
        render(probe=Probe(digest=OTHER_SHA))


def test_a_renderer_that_returns_no_caption_payload_is_refused() -> None:
    class Silent(Probe):
        def inspect(self, path, sha256):
            facts = super().inspect(path, sha256)
            del facts["captions"]
            return facts

    with pytest.raises(vd.PlaybackRefused, match="no caption payload"):
        render(probe=Silent())


def test_the_renderer_is_told_the_approved_direction_and_the_reused_surface() -> None:
    renderer = Renderer()
    render(renderer=renderer)
    spec = renderer.specs[0]
    assert spec["engine"] == "cli_tool"
    assert spec["aspect"] == "9:16"
    assert spec["scenes"][0]["scene_id"] == "scene-1"


def test_an_unsupported_aspect_is_refused() -> None:
    with pytest.raises(vd.VideoDraftError, match="aspect must be 9:16 or 16:9"):
        render(aspect="1:1")


def test_upload_is_a_constant_that_cannot_be_set() -> None:
    grant = vd.UploadGrant()
    assert grant.granted is False
    with pytest.raises(vd.UploadRefused, match="separate grants"):
        grant.refuse("publish to a channel")
    with pytest.raises(TypeError):
        vd.UploadGrant(upload=True)  # type: ignore[call-arg]


def test_every_run_and_its_cost_is_reported_not_just_the_best() -> None:
    draft = render(runs_evaluated=3, intervention_minutes=18.5, limitations=["one voice, one language"])
    report = draft.to_mapping()
    assert report["runs_evaluated"] == 3
    assert report["intervention_minutes"] == 18.5
    assert report["limitations"] == ["one voice, one language"]
    with pytest.raises(vd.VideoDraftError, match="runs_evaluated"):
        render(runs_evaluated=0)


# -- discipline ------------------------------------------------------------------------


def test_the_module_reads_no_clock_and_opens_nothing() -> None:
    """There is no clock, filesystem, process or socket reachable from this module."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("video_draft.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # AP-023 composes two existing contracts rather than re-implementing them: AP-009's research
    # bundle and AP-021's asset provenance.
    assert imported <= {
        "__future__",
        "hashlib",
        "json",
        "re",
        "dataclasses",
        "datetime",
        "typing",
        "autonomous_production_research",
        "website_draft",
    }
    for forbidden in ("time", "os", "pathlib", "subprocess", "socket", "requests", "httpx", "urllib"):
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

    shared = top_level("video_draft.py") & top_level("app.py")
    assert shared == set(), f"these names would collide in the single-file build: {sorted(shared)}"


def test_the_renderer_protocol_is_the_only_place_work_happens() -> None:
    draft = render()
    assert draft.deliverable() is True
    # The module produced a record, not a video: there is no upload, publish or serve surface.
    assert not hasattr(vd, "upload")
    assert not hasattr(vd, "publish")
