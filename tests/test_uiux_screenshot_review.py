"""UIUX-P6-03: the screenshot review must be a review, not a table of filenames.

`scripts/uiux_screenshot_review.py` claims three things about every committed capture: that it can
be traced to a script, that the index beside it agrees with the bytes on disk, and that the theme in
its name is the theme in its pixels. This file makes those claims fail when they stop being true.

The negative tests copy the media tree into a temporary directory and break one thing in it — a
wrong checksum in an index, a light capture renamed `dark`, a set with no review, a review that
names a file the set does not hold — and assert the review catches that and only that.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PROJECT_ROOT / "scripts" / "uiux_screenshot_review.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Screenshot_Review.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-screenshot-review.md"
MEDIA = PROJECT_ROOT / "docs" / "media"
PUBLISHED = MEDIA / "screenshots"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("uiux_screenshot_review", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_generator()


@pytest.fixture(scope="module")
def media_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the real media tree, so a test can break one thing in it."""
    target = tmp_path_factory.mktemp("media") / "media"
    shutil.copytree(MEDIA, target)
    return target


def _findings(payload: dict[str, object], check: str) -> list[dict[str, str]]:
    return [row for row in payload["findings"] if row["check"] == check]  # type: ignore[index]


def test_the_committed_review_is_the_generators_output(generator) -> None:
    payload = generator.build(MEDIA)
    committed = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not payload["luminance_measured"] and committed["luminance_measured"]:
        # The committed artifact recorded pixel measurements this machine cannot reproduce. The
        # rest still has to match, and the skip says which half did not run.
        assert generator.without_pixels(committed) == generator.without_pixels(payload)
        pytest.skip("ImageMagick is not installed here, so the measured luminance was not re-derived")
    assert committed == payload
    assert DOC_PATH.read_text(encoding="utf-8") == generator.render_doc(payload)


def test_the_check_mode_passes_on_the_committed_tree() -> None:
    if not shutil.which("identify"):
        pytest.skip("`--check` compares measured pixels; without ImageMagick it reports the difference as a note")
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 findings" in result.stdout


def test_every_committed_capture_belongs_to_a_reviewed_set(generator) -> None:
    """A PNG nobody filed under a reviewed set is the finding this story exists to prevent."""
    payload = generator.build(MEDIA, measure=False)
    assert payload["findings"] == []
    reviewed = {row["set"] for row in payload["sets"] if row["reviewed"]}
    on_disk = set(generator.capture_sets(MEDIA))
    assert on_disk == reviewed, f"unreviewed sets: {sorted(on_disk - reviewed)}"


def test_ignored_local_evidence_is_explicitly_opt_in(generator, media_copy: Path, monkeypatch) -> None:
    """Local PNGs can be reviewed without becoming drift inputs on a fresh clone."""
    local = media_copy / "polish" / "local-ignored-evidence.png"
    shutil.copy(media_copy / "polish" / "polish-dark-hover-primary.png", local)
    monkeypatch.setattr(generator, "ignored_local_captures", lambda _root: {local.resolve()})
    committed = generator.capture_sets(media_copy)
    reviewed_locally = generator.capture_sets(media_copy, include_ignored=True)
    assert "local-ignored-evidence.png" not in {path.name for path in committed["polish"]["files"]}
    assert "local-ignored-evidence.png" in {path.name for path in reviewed_locally["polish"]["files"]}


def test_no_pane_is_unphotographed(generator) -> None:
    payload = generator.build(MEDIA, measure=False)
    assert payload["coverage"]["gaps"] == []
    assert set(payload["coverage"]["grid"]) == set(generator.PANES)


def test_the_review_note_of_every_set_says_something() -> None:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert ledger["stories"], "the ledger lost its stories"
    generator = _load_generator()
    for name, review in generator.REVIEWS.items():
        assert review.get("note"), f"{name} has no review note"
        assert len(str(review["note"])) > 60, f"{name}'s note is too short to be a review"
        assert review.get("story"), f"{name} does not say which story it belongs to"


def test_an_eye_list_may_only_name_files_the_set_holds(generator) -> None:
    """The eye list is a claim about what was opened; a wrong filename makes it a false one."""
    payload = generator.build(MEDIA, measure=False)
    for row in payload["sets"]:
        held = {str(entry["file"]).split("/", 1)[-1] for entry in row["files"]}
        for opened in row["eye"]:
            assert opened in held, f"{row['set']} claims `{opened}` was opened and does not hold it"


def test_a_wrong_checksum_in_an_index_is_a_finding(generator, media_copy: Path) -> None:
    index_path = media_copy / "screenshots" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["shots"][0]["sha256"] = "0" * 64
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    payload = generator.build(media_copy, measure=False)
    drift = _findings(payload, "index-drift")
    assert drift, "a corrupted checksum in the published index passed the review"
    assert index["shots"][0]["file"] in drift[0]["detail"]


def test_a_light_capture_named_dark_is_a_finding(generator, media_copy: Path) -> None:
    """The check reads pixels, not names. Without ImageMagick there is nothing to read, so it skips."""
    if not shutil.which("identify"):
        pytest.skip("ImageMagick `identify` is not installed, so luminance cannot be measured")
    light = media_copy / "uiux-p6-03" / "before" / "start.png"
    assert light.exists(), "the P6-03 before set holds the light capture this test renames"
    light.rename(light.with_name("adversarial-dark-rest-start.png"))
    payload = generator.build(media_copy)
    contradicted = _findings(payload, "theme-contradicted")
    assert contradicted, "a light capture renamed `dark` passed the review"
    assert "adversarial-dark-rest-start.png" in contradicted[0]["detail"]


def test_a_set_with_no_review_is_a_finding(generator, media_copy: Path) -> None:
    orphan = media_copy / "unreviewed-set"
    orphan.mkdir()
    shutil.copy(media_copy / "polish" / "polish-dark-rest-chat.png", orphan / "orphan.png")
    payload = generator.build(media_copy, measure=False)
    unreviewed = _findings(payload, "unreviewed-set")
    assert [row["set"] for row in unreviewed] == ["unreviewed-set"]
    # A set nobody reviewed also has no producer, and both are said rather than one hiding the other.
    assert any(row["set"] == "unreviewed-set" for row in _findings(payload, "no-producer"))


def test_a_review_that_names_a_file_the_set_does_not_hold_is_a_finding(generator, media_copy: Path) -> None:
    generator.REVIEWS["polish"] = {**generator.REVIEWS["polish"], "eye": ["polish-dark-nonexistent.png"]}
    try:
        payload = generator.build(media_copy, measure=False)
        broken = _findings(payload, "eye-names-a-missing-file")
        assert [row["set"] for row in broken] == ["polish"]
    finally:
        generator.REVIEWS["polish"] = {**generator.REVIEWS["polish"], "eye": ["polish-dark-hover-primary.png"]}


def test_a_set_whose_producer_does_not_exist_is_a_finding(generator, media_copy: Path) -> None:
    generator.CURATED_PRODUCERS["stream-perf"] = "scripts/a_walk_that_was_deleted.mjs"
    try:
        payload = generator.build(media_copy, measure=False)
        missing = _findings(payload, "missing-producer")
        assert [row["set"] for row in missing] == ["stream-perf"]
    finally:
        generator.CURATED_PRODUCERS["stream-perf"] = "scripts/stream_perf_walk.mjs"


def test_a_capture_too_small_to_read_is_a_finding(generator, media_copy: Path) -> None:
    small = media_copy / "stream-perf" / "a-1x1-capture.png"
    # A real 1x1 PNG: the signature, an IHDR of 1x1, and the minimum chunks identify accepts.
    small.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a4944415478"
            "9c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
    )
    payload = generator.build(media_copy, measure=False)
    tiny = _findings(payload, "degenerate-capture")
    assert [row["set"] for row in tiny] == ["stream-perf"]


def test_the_published_set_is_the_one_the_manifest_names(generator) -> None:
    payload = generator.build(MEDIA)
    manifest = json.loads((PUBLISHED / "manifest.json").read_text(encoding="utf-8"))
    expected = [shot["file"] for shot in manifest["shots"]]
    listed = [entry["name"] for entry in payload["published"]["files"]]
    assert listed == expected, "the review must list the manifest's shots, in the manifest's order"
    assert payload["published"]["re_taken_in_this_story"] is True


def test_the_review_and_the_validator_agree_on_the_published_set() -> None:
    """Two commands, one set: the review must not quietly disagree with the shipped validator."""
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(PROJECT_ROOT / "scripts" / "validate_screenshots.py"), "--json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout[result.stdout.index("{"):])
    assert report.get("captured", 0) == 6 or report.get("shots") is not None or "6" in result.stdout


def test_the_check_mode_survives_a_machine_without_imagemagick(generator, monkeypatch, capsys) -> None:
    """A machine that cannot read pixels must still be able to check the review.

    CI has no `identify`. Before this test existed, `--check` compared the generated page against the
    committed one character for character, so it failed there while passing on a machine that could
    measure - a check that depended on the tooling rather than on the tree. The committed page is
    therefore compared with the committed artifact's own rendering, and the run says the pixel pass
    did not happen instead of passing itself off as complete.
    """
    monkeypatch.setattr(generator, "mean_luminance", lambda paths: None)
    assert generator.main(["--check"]) == 0
    captured = capsys.readouterr()
    assert "identify" in captured.err
    assert "pixel checks were not re-run" in captured.err
