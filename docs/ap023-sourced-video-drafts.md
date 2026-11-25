# AP-023 — delivering sourced original short-video drafts

AP-023 exists because a short video is the easiest artifact in this programme to fake. The two
things that make one worth watching — *why this topic* and *whose words these are* — are invisible
in the MP4 that gets delivered. A file that renders and plays proves that ffmpeg ran and nothing
else.

The module is the record that has to exist beside the MP4. Rendering and probing are the
renderer's and the probe's job.

## The rule the story applies

**A draft is deliverable only when its topic claim is dated and sourced, its script still serves
the approved direction, every asset can actually be used, and the file that came back really
plays with captions that line up.**

`VideoDraft.readiness()` is derived and returns the reasons in the order a reviewer should fix
them; `deliverable()` is `not readiness()`. Neither can be assigned, so a run cannot decide it is
finished — the same shape AP-021's `status()` and AP-022's `deliverable()` take, deliberately.

## "Trending" is a factual claim, so it needs a date, a place and a source

`TopicSignal` names a source id from the **AP-009 research bundle**, the calendar date the number
describes, the market it describes, the metric and the value. `assess_topic()` derives the status:

| status | meaning | may be narrated as trending |
| --- | --- | --- |
| `supported` | ≥ 2 distinct in-region sources dated inside the window | yes |
| `candidate` | exactly one such source | **no** |
| `unknown` | none | **no** |

One source is a candidate because a single observation cannot separate a trend from that source's
own interest. A `candidate` or `unknown` topic is recordable — the draft may proceed — but its
narration may not call the topic trending, and `assess_topic` requires the downgrade to say why.

Three things are refused rather than downgraded, because each makes the number meaningless rather
than merely weak:

- a signal whose source is not in the bundle (an uncited trend);
- a signal dated **after** the assessment (the clock or the source is wrong);
- a signal describing **another market** (popularity in Germany is not popularity in the US).

A signal older than `MAX_SIGNAL_AGE_DAYS` (90) simply falls out of the window and is reported as
outside it — it is not deleted from the record.

### The narration guard is negation-aware, and that is the whole point

"**This topic is not trending yet**" is the honest sentence for a candidate topic, and a plain
substring scan would refuse it — punishing exactly the narration this rule exists to encourage.
`_is_negated` looks only at the clause the marker sits in, so the denial passes and
"It is not hype: this topic is trending" is still refused (the colon starts a new clause). Both
behaviours have a test; a guardrail that only ever fires is a guardrail a model learns to phrase
around.

## Evidence, contradictions and uncertainty

`NarrationClaim(important=True)` with no source is refused **at construction**, so the gap cannot
be discovered mid-render. `render_video_draft()` also requires the `ResearchBundle` and refuses a
claim citing a source the run never retrieved — "cited" and "read" are different things, and only
one of them is evidence.

A contradiction found during research cannot simply disappear between research and narration.
`contradicted_claim_texts(bundle)` reads them off the bundle (the module collects the fact; the
caller is not asked to remember it), and a draft carrying a contradiction whose text appears
nowhere in `Script.uncertainties` raises `EvidenceRefused`. **A contradiction that vanished was not
resolved, it was deleted.**

## Direction drift is refused, and an owner can still change the direction

`CreativeDirection` requires the angle, the voice, the visual direction, **and** a non-empty
`forbidden_moves` list — a direction that forbids nothing has not been narrowed. Every
`StoryboardScene` names the direction element it serves, so a scene that serves nothing approved is
drift with a storyboard attached; `uncovered_elements()` catches the other half, where the brief
asks for something no scene ever shows.

A script written against a *different* direction raises unless it carries an
`OwnerDirectionChange`: a namespaced human actor (`owner:`, `user:`, `reviewer:`), a reason and a
date. A machine cannot create one.

## Reuse the integration that exists; audit before adding another

The repository already ships three MoneyPrinterTurbo surfaces — the CLI tool contract
(`moneyprinter_tool.py`), the MCP stdio server (`moneyprinter_mcp.py`) and the
`MoneyPrinterAPI` OpenAI-gateway profile. AC4 is not a suggestion to look at them once; it is a
checked precondition:

- `AdapterAudit` refuses an audit that omits **any** of the three;
- `unsuitable` requires a reason ("we did not look" and "it cannot do this" are different
  findings, and only one justifies new code);
- a candidate that would be a **second** integration is refused while a surface is reused.

A fourth client for the same external tool is not extra capability, it is a second source of
truth about it.

## Rights and provenance, and the one question only a voice asks

`MediaAsset` composes AP-021's `AssetProvenance` rather than re-implementing rights vocabulary, so
`unknown` rights block delivery identically in websites and in video. It adds the role
(`footage`, `music`, `font`, `voice`, `graphic`) and, for `voice`, a `VoiceConsent`.

A voice asset without consent is refused: whose voice it is and who agreed is the whole question a
voice poses. `owner_cloned_with_consent` additionally needs the consent record named in
`evidence` — the word "consent" in a field is not a consent. `cloned_from_youtube` or
`deepfake_impersonation` raise `RightsRefused`: unauthorised cloning is not a state this programme
records, it is one it refuses to produce.

## Playback: the file, the timing, and the caption package

`PlaybackProbe` returns the facts the module will not measure itself (container, duration streams,
playable, cues). The module then refuses:

- a probe whose digest is not the renderer's digest — a record bound to the wrong bytes describes a
  different video;
- overlapping caption cues, a cue ending after the video does, and the same cues stated twice with
  different timings in the renderer's payload and the probe's observation;
- **a render payload with no caption payload at all** — captions are part of the deliverable, and
  a missing field here is the silent-skip failure an earlier module already shipped once.

## What the draft is not

`UploadGrant` has no constructor argument that makes it true. Upload, publication and channel
credentials belong to AP-029, and there is no code path here that produces them. Every rendered
record states `upload_granted: false` and `publication_granted: false`, reports `runs_evaluated`
and `intervention_minutes` for **every** run rather than the best one, and carries its
`limitations` into the report.

## What this story does not claim

- **No route, pane or orchestrator consumes `video_draft.py` yet.** It is a contract with two ports
  and no surface, the same state `website_draft.py` and `website_quality.py` were in before their
  first callers. Wiring is a separate, reviewable change.
- **No real ffmpeg renderer or ffprobe probe ships with this story.** The module decides; a
  renderer that shells out to `ffmpeg` and a probe that reads `ffprobe` JSON is the next concrete
  step, and neither is exercised by the unit suite — the tests use recording doubles.
- **No video was rendered, watched, published or monetised for this story.** AP-024 owns technical
  and editorial qualification of a real clip.

## Verification

| Check | Result |
| --- | --- |
| `tests/test_video_draft.py` | 52 passed |
| `tests/test_ci_test_shard.py`, `tests/test_scoped_tests.py` | 20 passed |
| mypy (94 modules), `ruff check .` | clean |
| `scripts/validate_docs.py` | passed, 322 files |
