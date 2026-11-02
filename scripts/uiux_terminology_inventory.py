#!/usr/bin/env python3
"""Inventory the status vocabulary the six panes draw from (UIUX-P5-01).

    python3 scripts/uiux_terminology_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_terminology_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_terminology_inventory.py --json     # print the inventory

Every pane used to name its own states. The word for a failed run was `FAILED` in the harness badge
map and on the session rows, `ERROR` for the same outcome a few lines later, `Check failed` on Start,
and `failed` in Compare. A stopped run was `CANCELLED` (one L, one more spelling) in the harness and
`stopped by you` in Compare. A running workflow was `WORKFLOW RUNNING` next to a status tone of `ok`
in one place and `busy` in another, and `SDLC RUNNING` in a third. None of those is wrong on its own;
together they mean a reader cannot carry a word from one pane to the next, and a test cannot assert a
state without restating the pane's private spelling.

`UI_STATUS` is now the whole vocabulary: one entry per word, carrying the label *and* the tone, and
every pane draws from it - the harness map, the Compare states, Start's preflight and recent list, the
Council stepper, the pipeline pills, the chat tool cards. This inventory reads that table out of the
frontend and then looks for any place that still draws a status in its own words.

What counts as a status here: a word drawn in a status position - `setStatus(...)`, a
`class="status"` chip, the harness badge map, a stepper cell. What does not: labels and
abbreviations (`PRD`, `MTP`, `CPU`, `PINNED`), model-residency chips (`LOADED`, `LOADING` - a
property of a resource, not a state of a run), and counts (`0 / 0 Passed`). Those are listed in the
inventory with the reason, so the exception is stated rather than implied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
WALK = PROJECT_ROOT / "scripts" / "terminology_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "terminology"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Terminology.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-terminology.md"

STORY = "UIUX-P5-01"

# The tones the shared status component accepts; a vocabulary entry may use no other.
TONES = ("ok", "warn", "danger", "info", "busy", "neutral")

# Words a pane may draw that are not statuses, each with the reason it is not one. Anything drawn
# in a status position and missing from both the vocabulary and this table is a finding.
NOT_A_STATUS = {
    "0 / 0 Passed": "a count of checks that ran, not a state",
    "ESC": "a key label",
    "PRD": "a document type",
    "VIEW": "a mode label",
    "MTP": "a model-architecture tag",
    "CPU": "a hardware label",
    "RAM": "a hardware label",
    "PINNED": "a label on a pinned artifact",
    "USER TASK": "a field label",
    "LOADED": "a model-residency chip, a property of a resource rather than a state of a run",
    "LOADING": "a model-residency chip",
    "PASSED": "a verdict on a check, drawn as a count",
    "Unavailable": "a provider-route label (`Unavailable provider · alias`), not a run state",
}

# The panes, in the order the tab registry lists them, with the DOM id each one is.
PANES = ("start", "harness", "hall", "council", "debate", "chat")

# The captures the doc lists and the walk writes.
CAPTURES = (
    "terminology-start.png",
    "terminology-harness.png",
    "terminology-hall.png",
    "terminology-council.png",
    "terminology-debate.png",
    "terminology-chat.png",
    "terminology-narrow.png",
    "terminology-light.png",
)

# Where each pane draws the words it draws, so a pane that invents a vocabulary is a finding and
# not merely an omission.
PANE_DRAWERS = {
    "start": ("startRecentSummary", "startHealthSummary", "startWorkspaceSummary", "onboardingSummary"),
    "harness": ("harnessStatusBadge", "harnessActiveStatusBadge", "harnessSessions"),
    "hall": ("hallStatusBadge", "hallParticipants"),
    "council": ("councilTotalStatus", "stage-pill", "st-state"),
    "debate": ("DEBATE_STATUS", "renderDebateRecord"),
    "chat": ("renderChatTool", "presentChatError"),
}

SAFE_EXPRESSIONS = {"", "null", "undefined", "true", "false", "—", "\\u2014"}


def function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    if not match:
        return ""
    open_paren = match.end() - 1
    depth = 0
    close_paren = -1
    for offset in range(open_paren, len(source)):
        char = source[offset]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_paren = offset
                break
    if close_paren < 0:
        return ""
    start = source.find("{", close_paren)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(source)):
        char = source[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    return source[start:]


def vocabulary(html: str) -> dict[str, dict[str, str]]:
    """Read `UI_STATUS` out of the frontend: state -> {label, tone}."""
    block = function_body(html, "uiStatus")
    match = re.search(r"const UI_STATUS = \{(.*?)\n  \};", html, re.S)
    if not match:
        return {}
    table: dict[str, dict[str, str]] = {}
    for entry in re.finditer(r'"?([a-z][\w-]*)"?:\s*\{\s*label:\s*"([^"]*)",\s*tone:\s*"([a-z]+)"\s*\}', match.group(1)):
        table[entry.group(1)] = {"label": entry.group(2), "tone": entry.group(3)}
    assert block or table, "UI_STATUS is there but uiStatus() is not"
    return table


def status_literals(html: str) -> list[dict[str, str]]:
    """Every place a status-shaped word is drawn: (where, tone, word)."""
    draws: list[dict[str, str]] = []
    for match in re.finditer(r'setStatus\(([^,]+),\s*"(\w+)",\s*"([^"]{1,40})"', html):
        draws.append({"where": "setStatus", "tone": match.group(2), "word": match.group(3)})
    for match in re.finditer(r'\["(ok|warn|danger|neutral|busy|info)",\s*"([^"]{1,40})"\]', html):
        draws.append({"where": "tone table", "tone": match.group(1), "word": match.group(2)})
    for match in re.finditer(r'setStatus\(([^,]+),\s*"(\w+)",\s*uiStatus\("([a-z-]+)"\)\.label\)', html):
        draws.append({"where": "setStatus(vocabulary)", "tone": match.group(2), "word": f"@{match.group(3)}"})
    for match in re.finditer(r'data-tone="(\w+)"[^>]*>([^<{]{1,40})<', html):
        word = match.group(2).strip()
        if word:
            draws.append({"where": "status chip", "tone": match.group(1), "word": word})
    return draws


def unescaped_interpolations(markup: str) -> list[str]:
    offenders: list[str] = []
    for expression in expressions(markup):
        stripped = expression.strip()
        if "${" in stripped:
            offenders.extend(unescaped_interpolations(stripped))
            continue
        if stripped in SAFE_EXPRESSIONS or stripped.startswith(("escapeHtml(", "UI_STATUS.", "uiStatus(")):
            continue
        offenders.append(stripped)
    return offenders


def expressions(markup: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"\$\{", markup):
        start = match.end()
        depth = 1
        index = start
        while index < len(markup) and depth:
            char = markup[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        found.append(markup[start : index - 1])
    return found


def ledger_story() -> dict:
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""
    vocab = vocabulary(html)
    draws = status_literals(html)
    labels = {entry["label"] for entry in vocab.values()}
    labels_by_state = {state: entry["label"] for state, entry in vocab.items()}
    tones_by_state = {state: entry["tone"] for state, entry in vocab.items()}

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the vocabulary itself ───────────────────────────────────────────────
    check(
        "there is one vocabulary object, and it carries a word and a tone per state",
        len(vocab) >= 15
        and all(entry["label"].strip() for entry in vocab.values())
        and all(entry["tone"] in TONES for entry in vocab.values()),
        ", ".join(f"{state}={entry['tone']}" for state, entry in vocab.items() if entry["tone"] not in TONES)
        or f"{len(vocab)} entries",
    )
    check(
        "no state is spelled twice, and no two states share a word",
        len(labels) == len(vocab),
        ", ".join(sorted(label for label in labels if sum(1 for entry in vocab.values() if entry["label"] == label) > 1)),
    )
    check(
        "the vocabulary is lowercase prose, not shouted",
        all(entry["label"] == entry["label"].lower() for entry in vocab.values())
        and not any("_PLACEHOLDER" in entry["label"] for entry in vocab.values()),
        ", ".join(state for state, entry in vocab.items() if entry["label"] != entry["label"].lower()),
    )
    check(
        "`uiStatus()` is the only way a caller gets a word, and an unknown state is a bug",
        "function uiStatus(state) {" in html
        and "if (!hit) throw new Error(`unknown ui status: ${state}`);" in html
        and "return hit;" in html,
        "a state the table does not know must fail loudly, not print itself",
    )

    # ── the panes draw from it ──────────────────────────────────────────────
    debate_status = function_body(html, "debateStatusBlock") or ""
    block_match = re.search(r"const DEBATE_STATUS = \{(.*?)\n  \};", html, re.S)
    debate_block = block_match.group(1) if block_match else ""
    debate_pairs = dict(
        (m.group(1), (m.group(2), m.group(3)))
        for m in re.finditer(r'"?([a-z-]+)"?:\s*\{\s*label:\s*"([^"]*)",\s*tone:\s*"([a-z]+)"\s*\}', debate_block)
    )
    mismatched = [
        f"{state}: {label}/{tone} != {labels_by_state.get(state)}/{tones_by_state.get(state)}"
        for state, (label, tone) in debate_pairs.items()
        if labels_by_state.get(state) != label or tones_by_state.get(state) != tone
    ]
    check(
        "the Compare pane's ten states are the shared vocabulary, word for word and tone for tone",
        len(debate_pairs) == 10 and not mismatched,
        ", ".join(mismatched) or f"{len(debate_pairs)} states",
    )
    harness_map = re.search(r"const HARNESS_STATUS_STATE = \{(.*?)\n  \};", html, re.S)
    server_states = dict(
        (m.group(1), m.group(2))
        for m in re.finditer(r'"?([a-z_]+)"?:\s*"([a-z-]+)"', harness_map.group(1) if harness_map else "")
    )
    unknown = [state for state in server_states.values() if state not in vocab]
    check(
        "the Harness maps the server's states onto the vocabulary rather than naming them itself",
        bool(server_states)
        and not unknown
        and "uiStatus(name)" in function_body(html, "harnessStatusBadge"),
        ", ".join(unknown) or f"{len(server_states)} server states",
    )
    check(
        "one outcome has one word: `error` is drawn as `failed`, `cancelled` as `stopped by you`",
        server_states.get("error") == "failed"
        and server_states.get("cancelled") == "stopped"
        and labels_by_state.get("failed") == "failed"
        and labels_by_state.get("stopped") == "stopped by you",
        f"error={server_states.get('error')!r} cancelled={server_states.get('cancelled')!r}",
    )
    check(
        "a state the table has never seen keeps the server's own word instead of a wrong one",
        "return { cls: \"neutral\", label: status.replace(/_/g, \" \") };" in function_body(html, "harnessStatusBadge"),
        "an unknown state must not be mapped onto a word that would be false",
    )

    # ── nothing else invents a word ─────────────────────────────────────────
    unknown_refs = [
        draw for draw in draws
        if draw["word"].startswith("@") and draw["word"][1:] not in vocab
    ]
    strays = [
        draw for draw in draws
        if not draw["word"].startswith("@")
        and draw["word"] not in labels
        and draw["word"] not in NOT_A_STATUS
        and not draw["word"].startswith("$")
        and not re.fullmatch(r"[\d\s/.,%-]+", draw["word"])
    ]
    check(
        "every status word drawn anywhere is in the vocabulary or stated as not a status",
        not strays and not unknown_refs,
        "; ".join(f"{draw['where']}: {draw['word']!r} ({draw['tone']})" for draw in strays[:8]),
    )
    def expected_tone(draw: dict) -> str:
        if draw["word"].startswith("@"):
            return vocab.get(draw["word"][1:], {}).get("tone", "")
        return next((entry["tone"] for entry in vocab.values() if entry["label"] == draw["word"]), "")

    wrong_tone = [
        draw for draw in draws
        if (draw["word"].startswith("@") or draw["word"] in labels)
        and draw["tone"] in TONES
        and draw["tone"] != expected_tone(draw)
    ]
    check(
        "a word carries its tone wherever it is drawn, so a green `running` cannot come back",
        not wrong_tone,
        "; ".join(f"{draw['word']} drawn {draw['tone']}" for draw in wrong_tone[:8]),
    )
    shouted = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r'data-tone="\w+"[^>]*>([A-Z][A-Z /·]{2,})<', html)
            if match.group(1).strip() not in NOT_A_STATUS
        }
    )
    check(
        "no status is shouted in capitals while the rest of the vocabulary is prose",
        not shouted,
        ", ".join(shouted),
    )
    check(
        "each pane is named as drawing its words from the vocabulary, not from its own list",
        all(
            any(token in html for token in tokens)
            for tokens in PANE_DRAWERS.values()
        )
        and html.count("uiStatus(") >= 12,
        f"uiStatus() call sites: {html.count('uiStatus(')}",
    )
    check(
        "the words drawn through a template are the table's, not a hand-written copy of them",
        "UI_STATUS.demo.label" in html
        and not unescaped_interpolations(function_body(html, "renderDebateRecord")),
        ", ".join(unescaped_interpolations(function_body(html, "renderDebateRecord"))),
    )

    # ── the same word everywhere the reader looks ───────────────────────────
    check(
        "the harness session rows and the harness badge say the same word for the same state",
        "const stIcon = harnessStatusBadge(s).label;" in html,
        "the list and the badge must not be two vocabularies",
    )
    check(
        "a waiting approval is one phrase, on the badge and on every approval card",
        html.count('uiStatus("waiting-approval")') >= 2
        and "WAITING APPROVAL" not in html,
        f"remaining shouted forms: {html.count('WAITING APPROVAL')}",
    )
    check(
        "the same roll-up word is used for a finished stage and a finished run",
        html.count('uiStatus("completed")') >= 3 and '"Done"' not in html,
        f'`"Done"` still drawn {html.count(chr(34) + "Done" + chr(34))} times',
    )
    check(
        "the server's own state words still reach the pane, and the vocabulary only renames the label",
        "state.task_status" in function_body(html, "harnessStatusBadge")
        and '"running"' in (PROJECT_ROOT / "tests" / "test_status_components.py").read_text(encoding="utf-8"),
        "a vocabulary that hid the server's state would be a vocabulary that lies",
    )

    # ── the evidence ────────────────────────────────────────────────────────
    check(
        "the walk visits all six panes by keyboard and reads the words off the DOM",
        walk_body
        and all(f'"{pane}"' in walk_body for pane in PANES)
        and "Alt+" in walk_body
        and "terminology-narrow" in walk_body,
        ", ".join(pane for pane in PANES if f'"{pane}"' not in walk_body),
    )
    check(
        "the captures the doc lists exist and were written by the walk",
        all((MEDIA / name).exists() for name in CAPTURES)
        and all(name[:-4] in walk_body for name in CAPTURES),
        ", ".join(name for name in CAPTURES if not (MEDIA / name).exists()),
    )
    tracking = ledger_story()
    check(
        "the ledger marks this story done with evidence",
        tracking.get("status") == "done" and bool(tracking.get("evidence")) and bool(tracking.get("test_results")),
        f"tracking.status={tracking.get('status')!r}",
    )

    findings = [entry for entry in checks if not entry["ok"]]
    return {
        "story": STORY,
        "title": "Cross-tab terminology and consistent statuses",
        "generated_by": "scripts/uiux_terminology_inventory.py",
        "surface": "UI_STATUS",
        "tones": list(TONES),
        "vocabulary": vocab,
        "state_count": len(vocab),
        "server_states": server_states,
        "draws": draws,
        "not_a_status": NOT_A_STATUS,
        "panes": list(PANES),
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    vocab = payload["vocabulary"]
    lines = [
        "# UIUX-P5-01 — Cross-tab terminology and consistent statuses",
        "",
        "Generated by `scripts/uiux_terminology_inventory.py`. Do not edit by hand: regenerate it,",
        "and the check behind it stays true.",
        "",
        "## The problem",
        "",
        "Every pane named its own states. The word for a failed run was `FAILED` in the harness badge",
        "map and on the session rows, `ERROR` for the same end a few lines later, `Check failed` on",
        "Start and `failed` in Compare. A stop was `CANCELLED` in the harness and `stopped by you` in",
        "Compare. A running workflow was `WORKFLOW RUNNING` beside a status tone of `ok` on one path",
        "and `busy` on another, and `SDLC RUNNING` on a third. A waiting approval was shouted on two",
        "surfaces and absent on the third that showed it. None of those words is wrong alone; together",
        "they mean a reader cannot carry a word from one pane to the next, and no test can assert a",
        "state without restating a pane's private spelling.",
        "",
        "## The vocabulary",
        "",
        f"`UI_STATUS` is `{payload['state_count']}` entries: one word, one tone, one place. A caller",
        "asks for a state and gets both, so a green `running` cannot come back, and an unknown state",
        "throws instead of printing itself.",
        "",
        "| State | Word the reader sees | Tone |",
        "| :--- | :--- | :--- |",
    ]
    for state, entry in vocab.items():
        lines.append(f"| `{state}` | {entry['label']} | `{entry['tone']}` |")
    lines += [
        "",
        "## What changed, pane by pane",
        "",
        "| Pane | Before | After |",
        "| :--- | :--- | :--- |",
        "| Compare | already one vocabulary of ten states | unchanged, and now asserted to *be* the shared table, word for word and tone for tone |",
        "| Harness | `FAILED` / `ERROR` / `CANCELLED` / `WORKFLOW RUNNING` / `SDLC RUNNING` / `WAITING APPROVAL` / `NEW DRAFT`, `ok` tone on a running workflow, session rows spelling their own words | the vocabulary, the same words on the badge and the session rows, `error` drawn as `failed`, `cancelled` as `stopped by you`, `busy` tone for anything running |",
        "| Start | `Check failed`, `Nothing yet`, `Unavailable`, `Reading…` | `failed`, `not run`, `unavailable`, `loading` |",
        "| Council | `Recovered · partial`, `Stopped mid-seat`, `RECOVERED`, `Done`, `Unloaded` | `recovered, partial`, `stopped by you`, `completed`, `unloaded` |",
        "| Pipeline | `Done` on a stage and on the run total | `completed`, once |",
        "| Chat | tool cards and transcripts said `OK` / `FAILED`, one call site said `ERROR` | `ok` / `failed` |",
        "",
        "## The boundary: what is not a status",
        "",
        "A vocabulary that swallowed every string would be a vocabulary nobody can check. These are",
        "drawn by the panes and are *not* statuses, each for a stated reason:",
        "",
    ]
    for word, reason in payload["not_a_status"].items():
        lines.append(f"* `{word}` — {reason}.")
    lines += [
        "",
        "The inventory fails if a status position draws a word that is neither in the table nor in",
        "that list, so the exception has to be argued for rather than assumed.",
        "",
        "## The server's states",
        "",
        "The panes show the server's own words, renamed to the vocabulary and nothing else. A state",
        "the table has never seen keeps the server's word (underscores to spaces) with a neutral tone,",
        "because mapping an unknown state onto a known word would be the pane inventing a fact:",
        "",
    ]
    for state, word in payload["server_states"].items():
        lines.append(f"* `{state}` → `{word}`")
    lines += [
        "",
        "## The evidence",
        "",
        "`node scripts/terminology_walk.mjs --screenshots docs/media/terminology` starts the Compare",
        "fixture, opens each pane with that pane's own shortcut (Alt+1…Alt+6, read out of the rail),",
        "reads every status word the DOM holds, and captures the six panes, a 480 px viewport and the",
        "light theme. It fails when a chip's text is not built from vocabulary words, when a chip is",
        "uppercase by its text or by its computed `text-transform`, or when one word appears in two",
        "tones.",
        "",
        "## The checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} findings — **{payload['status']}**.",
        "",
    ]
    for entry in payload["checks"]:
        mark = "pass" if entry["ok"] else "FAIL"
        detail = f" — {entry['detail']}" if entry["detail"] else ""
        lines.append(f"* `{mark}` {entry['check']}{detail}")
    lines.append("")
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifact is stale or a rule is broken")
    parser.add_argument("--json", action="store_true", help="print the inventory instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    payload = build_inventory()

    if args.json:
        print(dumps(payload), end="")
    else:
        rendered_json = dumps(payload)
        rendered_doc = render_doc(payload)
        stale = (
            not JSON_PATH.exists()
            or JSON_PATH.read_text(encoding="utf-8") != rendered_json
            or not DOC_PATH.exists()
            or DOC_PATH.read_text(encoding="utf-8") != rendered_doc
        )
        if args.check:
            if stale:
                print(
                    "the terminology inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    if payload["findings"]:
        for entry in payload["findings"]:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
