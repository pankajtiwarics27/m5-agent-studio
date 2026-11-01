"""The Harness's durable timeline, event replay and jump-to-latest (UIUX-P3-02).

The Harness's trajectory is a view of a durable log. Before this story the pane dropped
every event type it had no card for - `session/init`, all the `security/*` types, the
`edit/*`, `delegation/*`, `handoff/*` and `mcp/*` families - so a session with a log looked
like an empty trajectory; it threw the reader to the bottom on every frame; and it never
said how much of the log it was holding or where the cursor was.

Each rule is broken in a copy of the source, because a rule that passes because it stopped
looking is the defect it was written to catch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "harness_timeline_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Timeline.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-timeline.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_harness_timeline_inventory",
        PROJECT_ROOT / "scripts" / "uiux_harness_timeline_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, (
                "the transform did not change the markup, so the test proves nothing"
            )
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, (
                "the transform did not change the server source, so the test proves nothing"
            )
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ─────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_harness_timeline_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# The Harness: the durable log, and the trajectory as a view of it")
    assert "never hand-edit this page" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P3-02"
    assert data["totals"]["findings"] == 0
    # This story added no endpoint: the log, the stream, the snapshot and the session
    # PATCH are the ones the pane already read and wrote.
    assert [(row["method"], row["path"]) for row in data["routes"]] == [
        ("GET", "/api/harness/sessions/{session_id}/events"),
        ("GET", "/api/harness/sessions/{session_id}/events/stream"),
        ("GET", "/api/harness/sessions/{session_id}/state"),
        ("PATCH", "/api/harness/sessions/{session_id}"),
    ]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the strip states what the trajectory is ─────────────────────────────────


def test_the_strip_sits_above_what_it_describes(payload):
    strip = payload["frontend"]["strip"]
    assert strip["arrives_hidden"] is True
    assert strip["sits_above_the_trajectory"] is True
    assert strip["is_a_disclosure"] is True
    assert strip["states_the_count"] is True
    assert strip["names_the_list"] is True
    assert strip["order_is_stated"] is True
    assert strip["row_is_a_button"] is True


def test_the_strip_is_never_the_thing_that_gets_dropped(payload):
    strip = payload["frontend"]["strip"]
    assert strip["never_shrinks_away"] is True, (
        "a short window can shrink the record away and leave the trajectory with no provenance"
    )
    assert strip["keeps_the_trajectory_room"] is True
    assert strip["hidden_is_not_displayed"] is True, (
        "a control that is not offered must not be drawn either"
    )


def test_the_strip_states_how_the_view_was_filled(payload):
    honesty = payload["frontend"]["honesty"]
    assert honesty["states_the_source_live"] is True
    assert honesty["states_the_count_and_range"] is True
    assert honesty["states_the_cursor"] is True
    assert honesty["says_when_the_log_did_not_resume"] is True
    assert honesty["says_what_is_pruned"] is True
    assert honesty["says_this_view_holds_less"] is True
    assert honesty["counts_the_replays"] is True
    assert honesty["following_needs_a_cursor"] is True
    assert honesty["pruning_wins_the_colour"] is True
    assert honesty["no_network_in_the_strip"] is True
    assert sorted(mode for mode in payload["modes"]) == ["idle", "live", "pruned", "replay"]


def test_the_strip_knows_how_it_was_filled(payload):
    telemetry = payload["frontend"]["telemetry"]
    assert all(telemetry.values()), [
        name for name, present in telemetry.items() if not present
    ]


# ── nothing a reader can hold is dropped ────────────────────────────────────


def test_every_retained_event_draws(payload):
    coverage = payload["frontend"]["coverage"]
    assert coverage["fallback_exists"] is True
    assert coverage["renderer_ends_in_the_fallback"] is True, (
        "the renderer can still end in nothing, so a retained event can vanish"
    )
    assert coverage["no_event_renders_nothing"] is True
    assert coverage["a_missing_type_still_draws"] is True
    assert coverage["summary_reads_the_event"] is True
    assert coverage["no_dead_branches"] is True, coverage["dead_branches"]


def test_the_coverage_split_is_counted(payload):
    coverage = payload["frontend"]["coverage"]
    emitted = coverage["types_emitted"]
    assert emitted > 0
    # Every emitted type is either drawn by name or reaches the fallback: nothing is left
    # in the space between the two.
    assert coverage["types_described_by_name"] <= emitted
    assert coverage["types_described_by_name"] <= coverage["types_with_a_card"] + emitted
    assert (
        coverage["types_described_by_name"] + coverage["types_with_only_the_generic_preview"]
        == emitted
    ), "an emitted type is neither described by name nor left to the generic preview"
    names = payload["frontend"]["types"]
    assert names["dead_branches"] == []
    assert set(names["with_a_card"]) <= set(names["emitted"])


def test_a_row_points_at_its_card(payload):
    trajectory = payload["frontend"]["trajectory"]
    assert all(trajectory.values()), [
        name for name, present in trajectory.items() if not present
    ]


# ── place: a frame must not move the reader ─────────────────────────────────


def test_a_frame_never_moves_a_reading_reader(payload):
    place = payload["frontend"]["place"]
    assert place["near_bottom_is_one_rule"] is True
    assert place["a_pinned_view_follows"] is True
    assert place["a_reading_view_is_left_alone"] is True, (
        "a frame that lands while the reader is scrolled up must not move them"
    )
    assert place["open_cards_stay_open"] is True


def test_the_jump_is_the_only_thing_that_scrolls(payload):
    place = payload["frontend"]["place"]
    assert place["the_jump_is_offered_only_when_needed"] is True
    assert place["the_jump_is_the_only_thing_that_scrolls"] is True
    assert place["the_jump_follows_a_scroll"] is True
    assert place["the_jump_follows_a_render"] is True


# ── replay rebuilds from the log, or says it cannot ─────────────────────────


def test_replay_rebuilds_from_the_log(payload):
    replay = payload["frontend"]["replay"]
    assert replay["re_reads_from_the_start"] is True
    assert replay["rebuilds_the_view_from_the_log"] is True
    assert replay["counts_the_replay"] is True
    assert replay["will_not_claim_another_session"] is True


def test_replay_refuses_a_log_it_could_not_resume(payload):
    replay = payload["frontend"]["replay"]
    assert replay["refuses_a_log_it_could_not_resume"] is True
    assert replay["says_so_then_resyncs"] is True, (
        "a failed replay must say so and reload the authoritative snapshot"
    )


# ── nothing that worked stopped working ────────────────────────────────────


def test_the_cursor_dedupe_and_gap_semantics_are_intact(payload):
    preserved = payload["frontend"]["preserved"]
    assert preserved["gap_reasons"] == [
        "no_page",
        "snapshot_required",
        "cursor_ahead",
        "events_pruned",
        "sequence_gap",
        "missing_events",
    ]
    assert preserved["dedupes_by_seq"] is True, (
        "a replayed or duplicated record must still render at most once"
    )
    assert preserved["keeps_the_cursor"] is True
    assert preserved["snapshot_required_handled"] is True
    assert preserved["resync_still_reloads_state"] is True
    assert preserved["resync_reopens_the_stream"] is True
    assert preserved["pruned_notice_kept"] is True
    assert preserved["stream_frames_unchanged"] is True


def test_the_routes_the_strip_reads_are_still_mounted(payload):
    missing = [
        f"{row['method']} {row['path']}" for row in payload["routes"] if not row["mounted"]
    ]
    assert missing == [], f"routes gone: {missing}"


def test_the_styles_the_surfaces_need_exist(payload):
    styles = payload["frontend"]["styles"]
    assert all(styles.values()), [name for name, present in styles.items() if not present]


def test_the_strip_controls_are_still_wired(payload):
    html = FRONTEND.read_text(encoding="utf-8")
    for element_id, needle in inventory.STRIP_CONTROLS.items():
        assert needle in html, element_id


# ── the walk covers the same ground ────────────────────────────────────────


def test_the_walk_reads_the_log_and_replays_it():
    assert WALK.exists(), "scripts/harness_timeline_walk.mjs is missing"
    source = WALK.read_text(encoding="utf-8")
    for needle in (
        "harnessTimeline",
        "harnessTimelineList",
        "harnessTimelineState",
        "harnessTimelineJump",
        "replayHarnessLog",
        "harness-timeline-row",
        "harnessLogState",
        "api/harness/sessions",
        "no page error occurred during the walk",
    ):
        assert needle in source, needle
    # The walk must build the events from the server rather than fabricate them.
    assert "security/policy_revision" in source
    assert "session/init" in source


def test_the_media_directory_documents_its_captures():
    readme = PROJECT_ROOT / "docs" / "media" / "harness-timeline" / "README.md"
    assert readme.exists(), "docs/media/harness-timeline/README.md is missing"
    text = readme.read_text(encoding="utf-8")
    for name in ("log-open.png", "replayed.png", "pruned.png", "short-window.png"):
        assert name in text, name
        assert (readme.parent / name).exists(), name
    assert "harness_timeline_walk.mjs" in text


# ── negative: a rule that stops looking ────────────────────────────────────


def test_a_renderer_that_drops_an_unknown_event_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    return renderRecordedHarnessEventHtml(ev);\n  }",
            '    return "";\n  }',
            1,
        )
    )
    assert "coverage" in _checks(payload)


def test_a_card_for_an_event_nothing_emits_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "    return renderRecordedHarnessEventHtml(ev);\n  }",
            '    if (ev.type === "nothing/ever") {\n      return "<div></div>";\n    }\n'
            "    return renderRecordedHarnessEventHtml(ev);\n  }",
            1,
        )
    )
    assert "coverage" in _checks(payload)


def test_a_frame_that_always_jumps_to_the_bottom_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      stream.scrollTop = previousTop;\n",
            "",
            1,
        )
    )
    assert "place" in _checks(payload)


def test_a_strip_that_claims_to_follow_an_unread_log_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "&& !harnessRunActive && harnessCursor > 0;",
            "&& !harnessRunActive;",
            1,
        )
    )
    assert "honesty" in _checks(payload)


def test_a_hidden_control_that_is_still_drawn_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            ".harness-timeline-actions .btn[hidden] { display: none; }",
            "",
            1,
        )
    )
    assert "strip" in _checks(payload)


def test_a_lost_gap_reason_is_caught(sources):
    def drop_events_pruned(text: str) -> str:
        return text.replace(
            '    if (start > 0 && retained && start + 1 < retained) return { gap: true, reason: "events_pruned" };\n',
            "",
            1,
        )

    payload = sources(html_transform=drop_events_pruned, app_transform=None)
    assert "gap-detection-lost" in _checks(payload)


def test_an_unmounted_log_route_is_caught(sources):
    def unmount(server: str) -> str:
        return server.replace(
            '@app.get("/api/harness/sessions/{session_id}/events")',
            '@app.get("/api/harness/sessions/{session_id}/events_gone")',
            1,
        )

    payload = sources(app_transform=unmount)
    assert "route-missing" in _checks(payload)
