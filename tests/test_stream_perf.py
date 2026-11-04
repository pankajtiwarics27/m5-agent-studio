"""SSE performance and bounded DOM updates (UIUX-P5-04).

Both panes drew their whole self for every frame of a stream: the chat transcript re-rendered every
message for every token (12 ms a token on a 60-message conversation, growing with the conversation),
and the harness stream rebuilt every card for every event over a cache that sorted itself on every
read and kept every event it had ever seen.

These tests read the inventory and break each rule in a copy of the source, so a rule that stopped
looking fails here rather than in production. The walk (`scripts/stream_perf_walk.mjs`) is what
times a render on a real streamed reply; it is not run here, because it needs a browser and a live
fixture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK = PROJECT_ROOT / "scripts" / "stream_perf_walk.mjs"
INVENTORY = PROJECT_ROOT / "scripts" / "uiux_stream_perf_inventory.py"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Stream_Performance.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-stream-performance.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "stream-perf"


def _load_module():
    spec = importlib.util.spec_from_file_location("uiux_stream_perf_inventory", INVENTORY)
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
    """Point the inventory at a copy, so a rule can be broken in it."""

    def _make(html_transform=None, walk_transform=None, test_body=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        walk = WALK.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if walk_transform:
            changed = walk_transform(walk)
            assert changed != walk, "the transform did not change the walk, so the test proves nothing"
            walk = changed
        html_path = tmp_path / "index.html"
        walk_path = tmp_path / "stream_perf_walk.mjs"
        html_path.write_text(html, encoding="utf-8")
        walk_path.write_text(walk, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "WALK", walk_path)
        if test_body is not None:
            test_path = tmp_path / "test_stream_perf.py"
            test_path.write_text(test_body, encoding="utf-8")
            monkeypatch.setattr(inventory, "TEST", test_path)
        return inventory.build_inventory()

    return _make


def failing(report: dict) -> list[str]:
    return [entry["check"] for entry in report["findings"]]


def swap(html: str, old: str, new: str) -> str:
    assert html.count(old) == 1, f"the source moved: {old[:60]!r} appears {html.count(old)} times"
    return html.replace(old, new, 1)


# ── the inventory itself ───────────────────────────────────────────────────


def test_the_inventory_is_clean(payload):
    assert payload["findings"] == [], failing(payload)
    assert payload["check_count"] >= 28


def test_the_artifact_on_disk_is_the_one_the_inventory_builds(payload):
    assert JSON_PATH.exists(), "run scripts/uiux_stream_perf_inventory.py"
    assert json.loads(JSON_PATH.read_text(encoding="utf-8")) == json.loads(inventory.dumps(payload))


def test_the_doc_is_the_one_the_inventory_renders(payload):
    assert DOC_PATH.exists(), "run scripts/uiux_stream_perf_inventory.py"
    assert DOC_PATH.read_text(encoding="utf-8") == inventory.render_doc(payload)


def test_the_doc_is_linked_from_the_docs_index():
    index = (PROJECT_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert DOC_PATH.name in index


def test_the_inventory_passes_its_own_check_mode():
    assert inventory.main(["--check", "--quiet"]) == 0


# ── 1. one render per frame, and never for the frame that ends a turn ──────


def test_a_scheduler_that_renders_on_every_call_is_a_finding(sources):
    def unqueue(html: str) -> str:
        return swap(html, "  function scheduleChatRender() {\n    if (chatRenderQueued) return;", "  function scheduleChatRender() {")

    report = sources(html_transform=unqueue)
    assert "a token schedules a render for the next frame instead of drawing one" in failing(report)


def test_a_scheduler_that_never_draws_is_a_finding(sources):
    def broken(html: str) -> str:
        return swap(
            html,
            "    requestAnimationFrame(() => {\n      chatRenderQueued = false;\n      renderChatMessages();\n    });",
            "    requestAnimationFrame(() => {\n      chatRenderQueued = false;\n    });",
        )

    report = sources(html_transform=broken)
    assert "a token schedules a render for the next frame instead of drawing one" in failing(report)


def test_a_terminal_frame_that_is_queued_is_a_finding(sources):
    def defer(html: str) -> str:
        return swap(
            html,
            "  function flushChatRender() {\n    chatRenderQueued = false;\n    renderChatMessages();\n  }",
            "  function flushChatRender() {\n    chatRenderQueued = false;\n    requestAnimationFrame(() => renderChatMessages());\n  }",
        )

    report = sources(html_transform=defer)
    assert "the frame that ends a turn is drawn immediately, not queued behind the next frame" in failing(report)


def test_a_token_that_renders_immediately_is_a_finding(sources):
    def eager(html: str) -> str:
        return swap(
            html,
            '        if (d.kind === "reasoning") asstMsg.reasoning += d.text; else asstMsg.content += d.text;\n        scheduleChatRender();',
            '        if (d.kind === "reasoning") asstMsg.reasoning += d.text; else asstMsg.content += d.text;\n        flushChatRender();',
        )

    report = sources(html_transform=eager)
    assert "the handlers that fire many times per second defer their render" in failing(report)


def test_a_turn_that_ends_on_a_queued_frame_is_a_finding(sources):
    def queue(html: str) -> str:
        return swap(
            html,
            '        asstMsg.timing.endedAt = performance.now();\n        flushChatRender();\n      },\n      stopped() {',
            '        asstMsg.timing.endedAt = performance.now();\n        scheduleChatRender();\n      },\n      stopped() {',
        )

    report = sources(html_transform=queue)
    assert "the handlers that end a turn render exactly once, now" in failing(report)


# ── 2. a token costs the message, not the conversation ─────────────────────


def test_a_shape_that_ignores_tool_calls_is_a_finding(sources):
    def narrow(html: str) -> str:
        return swap(html, "${(m.tools || []).length}|", "|")

    report = sources(html_transform=narrow)
    assert "the transcript's shape names every part of a turn that is not its words" in failing(report)


def test_a_render_that_does_not_compare_the_shape_is_a_finding(sources):
    def always(html: str) -> str:
        return swap(html, "      shape === chatRenderedShape\n", "      true\n")

    report = sources(html_transform=always)
    assert "the render decides on the shape, the last node and the turn count" in failing(report)


def test_rebuilding_every_message_for_one_token_is_a_finding(sources):
    def rebuild(html: str) -> str:
        return swap(
            html,
            "const indices = onlyTheLastChanged ? [chatHistory.length - 1] : chatHistory.map((_unused, index) => index);",
            "const indices = chatHistory.map((_unused, index) => index);",
        )

    report = sources(html_transform=rebuild)
    assert "it builds the one turn that changed, not the turns around it" in failing(report)


def test_rewriting_the_whole_transcript_on_the_cheap_path_is_a_finding(sources):
    def full(html: str) -> str:
        return swap(
            html,
            "    if (onlyTheLastChanged) {\n      previousLast.outerHTML = parts[0];\n    } else {",
            "    if (false) {\n      previousLast.outerHTML = parts[0];\n    } else {",
        )

    report = sources(html_transform=full)
    assert "and replaces that one node rather than rewriting the transcript" in failing(report)


def test_an_empty_transcript_that_keeps_its_shape_is_a_finding(sources):
    def forget(html: str) -> str:
        return swap(html, '      chatRenderedShape = "";\n      updateChatTranscriptBar();', "      updateChatTranscriptBar();")

    report = sources(html_transform=forget)
    assert "an empty transcript forgets the shape, so the next message is not drawn into a stale node" in failing(report)


# ── 3. the stream draws a window, and says what it is holding back ─────────


def test_a_window_of_a_different_size_is_a_finding(sources):
    def huge(html: str) -> str:
        return swap(html, "  const HARNESS_RENDER_WINDOW = 300;", "  const HARNESS_RENDER_WINDOW = 100000;")

    report = sources(html_transform=huge)
    assert "the window and the cache have one declared size each (300 cards, 5000 events)" in failing(report)


def test_a_render_that_draws_the_whole_log_is_a_finding(sources):
    def unwindow(html: str) -> str:
        return swap(
            html,
            "const shown = visible.length > HARNESS_RENDER_WINDOW ? visible.slice(visible.length - HARNESS_RENDER_WINDOW) : visible;",
            "const shown = visible;",
        )

    report = sources(html_transform=unwindow)
    assert "the stream draws the newest window of the log" in failing(report)


def test_a_note_that_does_not_say_where_the_rest_is_is_a_finding(sources):
    def mute(html: str) -> str:
        return swap(html, "Replay re-reads the whole log from the server.", "The rest is elsewhere.")

    report = sources(html_transform=mute)
    assert "the pane says how much of the log is not on screen, and where to read it" in failing(report)


def test_a_note_that_is_never_updated_is_a_finding(sources):
    def stale(html: str) -> str:
        return swap(html, "      harnessUpdateWindowNote(shown.length, visible.length);\n", "")

    report = sources(html_transform=stale)
    assert "the note is updated as the window slides, so it never counts cards it is not showing" in failing(report)


def test_a_timeline_that_draws_every_cached_event_is_a_finding(sources):
    def unwindow(html: str) -> str:
        return swap(html, "rows.slice(-HARNESS_RENDER_WINDOW)", "rows")

    report = sources(html_transform=unwindow)
    assert "the timeline's own list is windowed by the same number" in failing(report)


# ── 4. a live event is one card in, one card out ───────────────────────────


def test_an_append_that_ignores_approval_state_is_a_finding(sources):
    """The decision moved into `harnessStreamAppendWindow`: the mutation follows it there."""

    def blind(html: str) -> str:
        return swap(
            html,
            "    if (Number(rendered && rendered.pending) !== Number(shown && shown.pending)) return null;\n",
            "",
        )

    report = sources(html_transform=blind)
    assert "the append is refused when approval state moved, because those cards are already drawn" in failing(report)


def test_an_append_that_swallows_an_approval_event_is_a_finding(sources):
    def allow(html: str) -> str:
        return swap(
            html,
            '    if (appended.some(ev => String((ev && ev.type) || "").startsWith("approval/"))) return null;\n',
            "",
        )

    report = sources(html_transform=allow)
    assert "the append is also refused for an approval event, which changes a card that already exists" in failing(report)


def test_an_append_that_cannot_slide_is_a_finding(sources):
    def sticky(html: str) -> str:
        return swap(html, "    const dropped = drawn.filter(seq => seq < incoming[0]).length;", "    const dropped = 0;")

    report = sources(html_transform=sticky)
    assert "the run on screen may slide forward: cards leaving are counted, cards arriving are added" in failing(report)


def test_a_layout_read_per_card_is_a_finding(sources):
    def thrash(html: str) -> str:
        return swap(html, "      for (const card of leaving) card.remove();", "      for (const card of leaving) { card.getBoundingClientRect(); card.remove(); }")

    report = sources(html_transform=thrash)
    assert "the cards that leave are measured in one pass, and not at all for a reader at the bottom" in failing(report)


def test_a_reader_who_is_not_put_back_is_a_finding(sources):
    def jump(html: str) -> str:
        return swap(html, "        stream.scrollTop = Math.max(0, previousTop - removedHeight);", "        stream.scrollTop = stream.scrollHeight;")

    report = sources(html_transform=jump)
    assert "a reader who scrolled up is put back where they were reading" in failing(report)


# ── 5. the cache is bounded, and the cursor is still the truth ─────────────


def test_a_cache_without_a_ceiling_is_a_finding(sources):
    def grow(html: str) -> str:
        section = html[html.find("    if (harnessEventCache.size > HARNESS_CACHE_LIMIT) {") :]
        end = section.find("\n    }\n")
        return swap(html, section[: end + len("\n    }\n")], "")

    report = sources(html_transform=grow)
    assert "the cache drops its oldest events instead of growing without a ceiling" in failing(report)


def test_a_cache_that_decides_the_cursor_is_a_finding(sources):
    def couple(html: str) -> str:
        return swap(html, "      if (seq > harnessCursor) harnessCursor = seq;", "      if (harnessEventCache.has(seq) && seq > harnessCursor) harnessCursor = seq;")

    report = sources(html_transform=couple)
    assert "the cursor still advances past an event the cache refused to keep" in failing(report)


def test_sorting_the_cache_on_every_read_is_a_finding(sources):
    def sort(html: str) -> str:
        return swap(
            html,
            "    if (!harnessCachedSnapshot) harnessCachedSnapshot = harnessEventOrder.map(seq => harnessEventCache.get(seq));",
            "    harnessCachedSnapshot = [...harnessEventCache.keys()].sort((a, b) => a - b).map(seq => harnessEventCache.get(seq));",
        )

    report = sources(html_transform=sort)
    assert "drawing the window does not sort the whole cache" in failing(report)


def test_a_merge_that_always_sorts_is_a_finding(sources):
    def always_sort(html: str) -> str:
        return swap(html, "    if (!ascending) seqs.sort((a, b) => a - b);", "    seqs.sort((a, b) => a - b);")

    report = sources(html_transform=always_sort)
    assert "an out-of-order page is still merged correctly, it just pays for the sort" in failing(report)


def test_a_reset_that_keeps_ordering_state_is_a_finding(sources):
    def leaky(html: str) -> str:
        return swap(
            html,
            "    harnessEventCache = new Map();\n    harnessEventOrder = [];\n    harnessCachedSnapshot = null;\n    harnessCursor = 0;",
            "    harnessEventCache = new Map();\n    harnessCursor = 0;",
        )

    report = sources(html_transform=leaky)
    assert "a reset drops the order and the snapshot with the cache, so a second session cannot inherit them" in failing(report)


# ── 6. nothing durable was simplified away ─────────────────────────────────


def test_an_authoritative_refresh_that_is_queued_is_a_finding(sources):
    def queued(html: str) -> str:
        return swap(
            html,
            "harnessSnapshot = state; applyHarnessSnapshotState(state); renderStoredHarnessEvents(harnessCachedEvents());",
            "harnessSnapshot = state; applyHarnessSnapshotState(state); scheduleHarnessStreamRender();",
        )

    report = sources(html_transform=queued)
    assert "the snapshot fetch after a turn still draws synchronously, so it cannot be raced by a frame" in failing(report)


def test_losing_the_pruning_note_is_a_finding(sources):
    def mute(html: str) -> str:
        return swap(html, "Earlier events were pruned; showing the retained log from seq", "Retained log from seq")

    report = sources(html_transform=mute)
    assert "a pruned log still says it was pruned, and still defers to task state" in failing(report)


def test_losing_a_recovery_path_is_a_finding(sources):
    def drop(html: str) -> str:
        return swap(html, "  async function harnessResyncFromSnapshot(reason) {", "  async function resyncHarnessSnapshot(reason) {")

    report = sources(html_transform=drop)
    assert "replay and resync still read the log from the server rather than the window on screen" in failing(report)


# ── 7. the evidence ────────────────────────────────────────────────────────


def test_a_walk_that_stops_timing_a_token_is_a_finding(sources):
    def rename(walk: str) -> str:
        return swap(walk, "a token costs at most ${TOKEN_BUDGET_MS}ms on a 60-message transcript", "a token is rendered")

    report = sources(walk_transform=rename)
    assert "the walk times a token, a burst, a live event and a full window" in failing(report)


def test_a_walk_that_could_report_success_without_running_is_a_finding(sources):
    def quiet(walk: str) -> str:
        return swap(walk, "This script refuses to report a walk it did not perform.", "This script needs a browser.")

    report = sources(walk_transform=quiet)
    assert "the walk refuses to report a walk it did not perform" in failing(report)


def test_a_walk_that_never_sends_a_message_is_a_finding(sources):
    def fixture_only(walk: str) -> str:
        return swap(walk, 'await page.click("#chatSendBtn");', "await sleep(200);")

    report = sources(walk_transform=fixture_only)
    assert "the walk drives a real streamed reply, not a fixture of a transcript" in failing(report)


def test_the_captures_are_the_ones_the_walk_writes(payload):
    for name in payload["captures"]:
        assert (MEDIA / name).exists(), f"{name} is listed but not on disk: run the walk"
        assert name[:-4] in WALK.read_text(encoding="utf-8")
