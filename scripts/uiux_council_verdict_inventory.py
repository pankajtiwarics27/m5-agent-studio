#!/usr/bin/env python3
"""Inventory what the Council says about agreement, dissent and its decision (UIUX-P4-03).

    python3 scripts/uiux_council_verdict_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_council_verdict_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_council_verdict_inventory.py --json     # print the inventory

The Hall could draw a transcript and, if a seat had been asked for its verdict, a banner whose only
claim was `Pinned Council Conclusion (author)`. What it could not say:

* what the decision had to do with the four replies above it - it is one seat's reply, and nothing
  said whether the other seats' words reached it or were left behind;
* whether four seats produced four readings, or one reading four times. Two seats on one model say
  the same thing, and the offline demo path makes every seat say the same thing; a results pane
  that counted replies would present that as consensus;
* that a session ending with nothing pinned has settled nothing.

So the pane states the decision's provenance and measures, per reply, how much of that reply's
wording the decision also contains. The measure is blunt on purpose and named for what it is: the
fraction of a reply's 4-word sequences that reappear in the text it is compared with. It is a text
measure, not a reading of intent.

What is measured here:

* **the provenance**, which must be a reply the server recorded - matched on the author it stored
  *and* the words it kept, so an edited conclusion is not credited to a seat and a seat that
  happens to say the same thing is not credited with a conclusion it did not write;
* **the measure**, its shingle size and its thresholds, and the sentence that says what it is not;
* **the readings** (one per reply), and that the pane never calls shared wording agreement;
* **dissent**, which is named, counted, and left in the transcript: the verdict block may not
  write into the stream above it;
* **the surfaces that existed**, the conclusion banner and the hall stream's events;
* **the evidence**, the fixture's per-model replies, the walk's states, and the captures.

It fails when the decision's provenance stops being checked, when the pane hard-codes a verdict,
when shared wording is called consensus, when a reply is merged into a summary, when a value
reaches the page unescaped, and when this story invents an endpoint the server does not mount.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "council_readiness_seed.py"
WALK = PROJECT_ROOT / "scripts" / "council_verdict_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "council-verdict"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Council_Verdict.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-council-verdict.md"

STORY = "UIUX-P4-03"

CALLED_PATH = re.compile("""["'`](/api/[^"'`]*)["'`]""")

# The columns a conclusion is stored in, and the writers that put one there.
CONCLUSION_COLUMNS = ("conclusion_author", "conclusion_text", "conclusion_at")

# The hall stream's events; none of them may disappear.
STREAM_EVENTS = ("summon_start", "token", "summon_done", "error")

# The six readings a reply can be drawn in, and nothing else.
READINGS = (
    "is the decision",
    "mostly in the decision",
    "partly in the decision",
    "not in the decision",
    "the same words as",
    "echoes",
    "its own words",
)

# The table the copy control writes. It is the same table the block draws.
TABLE_COLUMNS = ("Seat", "Model", "Size", "Shared wording", "Reading")

# What the measure may be called. A claim of agreement is not one of them.
FORBIDDEN_CLAIMS = ("consensus", "agreed", "majority", "averaged")

# The captures the doc lists, and the walk must be able to produce.
CAPTURES = (
    "council-verdict-unpinned.png",
    "council-verdict-pinned.png",
    "council-verdict-decision.png",
    "council-verdict-narrow.png",
    "council-verdict-light.png",
    "council-verdict-offline.png",
)

SAFE_EXPRESSIONS = {"", "null", "undefined", "true", "false"}


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``, skipping the parameter list."""
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


def python_function(source: str, name: str) -> str:
    """The indented body of ``def name(...)`` in Python source, by dedent."""
    match = re.search(rf"^(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.M)
    if not match:
        return ""
    depth = 0
    index = match.start()
    while index < len(source):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    colon = source.find(":", index)
    if colon < 0:
        return ""
    body_start = source.find("\n", colon)
    if body_start < 0:
        return ""
    collected: list[str] = []
    for line in source[body_start + 1 :].splitlines():
        if line.strip() and not line[0].isspace():
            break
        collected.append(line)
    return "\n".join(collected)


def element(source: str, element_id: str) -> str:
    """The start tag carrying ``id="<element_id>"``, or an empty string."""
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def called_routes(source: str) -> list[str]:
    """Every `/api/...` path literal in ``source``, with each interpolation collapsed."""
    found: list[str] = []
    for match in re.finditer(CALLED_PATH, source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1))
        path = path.split("?")[0]
        if path not in found:
            found.append(path)
    return found


def unescaped_interpolations(markup: str) -> list[str]:
    """``${…}`` expressions inside markup that do not pass through `escapeHtml(` or `String(`."""
    offenders: list[str] = []
    for expression in _expressions(markup):
        if "${" in expression:
            offenders.extend(unescaped_interpolations(expression))
            continue
        stripped = expression.strip()
        if stripped in SAFE_EXPRESSIONS:
            continue
        if stripped.startswith(("escapeHtml(", "Number(", "String(", "Boolean(")):
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*Html", stripped):
            continue
        offenders.append(stripped)
    return offenders


def _expressions(markup: str) -> list[str]:
    """Every ``${…}`` body in ``markup``, brace-balanced."""
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
    """The story's own tracking entry, so this artifact can fail if the ledger forgets it."""
    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    for story in ledger.get("stories", []):
        if story.get("id") == STORY:
            return story.get("tracking", {}) or {}
    return {}


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    server = APP.read_text(encoding="utf-8")
    seed = SEED.read_text(encoding="utf-8") if SEED.exists() else ""
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    summon = python_function(server, "api_hall_summon")
    close_route = python_function(server, "api_hall_close")
    session_reader = python_function(server, "get_hall_session")
    schema = server[server.find("CREATE TABLE IF NOT EXISTS hall_sessions") :][:1400]

    render_hall = function_body(html, "renderActiveHall")
    words = function_body(html, "hallVerdictWords")
    shingles = function_body(html, "hallVerdictShingles")
    contained = function_body(html, "hallVerdictContainedIn")
    replies_of = function_body(html, "hallVerdictReplies")
    groups_of = function_body(html, "hallVerdictGroups")
    decision_of = function_body(html, "hallVerdictDecision")
    summary_of = function_body(html, "hallVerdictSummary")
    markdown_of = function_body(html, "hallVerdictMarkdown")
    copy_body = function_body(html, "copyHallVerdict")
    render_of = function_body(html, "renderHallVerdict")

    verdict_functions = "".join(
        [words, shingles, contained, replies_of, groups_of, decision_of, summary_of, markdown_of, copy_body, render_of]
    )

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the server's side of a conclusion ───────────────────────────────────
    check(
        "the session table records a conclusion with its author and the time it was pinned",
        all(f"{column} " in schema or f"{column}," in schema for column in CONCLUSION_COLUMNS)
        and "status = 'closed'" in python_function(server, "close_hall_session_db"),
        "a conclusion without an author and a time cannot be attributed or dated",
    )
    check(
        "a pinned verdict is the seat's own reply, closed by the call that saves it",
        "append_hall_message(session_id, author_label, role_key, final_text" in summon
        and "if pin_as_conclusion:" in summon
        and "close_hall_session_db(session_id, author_label, final_text)" in summon,
        "the decision must be the reply the seat wrote, not something the pane composes",
    )
    check(
        "a human can close a session with a conclusion that is not any seat's reply",
        'body.get("author") or "Human Operative"' in close_route
        and "close_hall_session_db(" in close_route,
        "the close route is the one writer that is not a seat",
    )
    check(
        "the session payload carries the conclusion to the pane",
        "SELECT * FROM hall_sessions" in session_reader and "messages" in session_reader,
        "the pane reads the conclusion from the session it already loads",
    )
    check(
        "the hall stream still speaks the same four events",
        all(f'_sse("{event}"' in summon for event in STREAM_EVENTS),
        ", ".join(event for event in STREAM_EVENTS if f'_sse("{event}"' not in summon),
    )

    # ── the pane's side ─────────────────────────────────────────────────────
    verdict_element = element(html, "hallVerdict")
    check(
        "the verdict area is a labelled region, hidden until there is a reply",
        'role="region"' in verdict_element
        and 'aria-label="Council verdict"' in verdict_element
        and "hidden" in verdict_element
        and "box.hidden = true" in render_of,
        "a region without a name, or one drawn empty, is noise in a screen reader",
    )
    check(
        "the verdict is drawn with the transcript it describes",
        "renderHallVerdict();" in render_hall and "activeHallData" in summary_of,
        "two surfaces describing one session must be drawn from one payload",
    )
    check(
        "the decision's provenance is a reply the server recorded, matched on author and words",
        'reply.author === author' in decision_of
        and 'reply.content.trim() === text.trim()' in decision_of
        and "const author = session.conclusion_author" in decision_of,
        "an author match alone credits an edited conclusion; a text match alone credits the wrong seat",
    )
    check(
        "every other reply is measured against the decision, and the pinned seat is not",
        "hallVerdictContainedIn(reply.content, decision.text)" in summary_of
        and 'reading: "decision", text: "is the decision"' in summary_of,
        "the decision is not measured against itself",
    )
    check(
        "the measure is containment of 4-word sequences, and the pane says so",
        "const VERDICT_SHINGLE_SIZE = 4;" in html
        and "words.length >= VERDICT_SHINGLE_SIZE ? VERDICT_SHINGLE_SIZE : 1" in shingles
        and "shared / left.size" in contained
        and "4-word sequences" in render_of
        and "It measures text, not intent" in render_of,
        "a measure whose size is hidden is a number nobody can check",
    )
    check(
        "the pane never calls shared wording agreement or consensus",
        all(claim not in render_of.lower() for claim in FORBIDDEN_CLAIMS[:3] if claim != "consensus")
        and render_of.count("consensus") == 1
        and "would call this consensus" in render_of
        and [match for match in re.findall(r".{0,12}agree.{0,12}", render_of) if "without agreeing" not in match] == [],
        "measured wording is not assent, and the pane must not imply that it is",
    )
    check(
        "a session with nothing pinned says that nothing is settled",
        "no decision pinned" in render_of and "nothing has been settled" in render_of,
        "an unpinned session is not a verdict, and the pane must not draw it as one",
    )
    check(
        "the pane counts voices, not replies",
        "hallVerdictGroups(replies)" in summary_of
        and "groups.length" in summary_of
        and "VERDICT_SAME" in groups_of
        and "voiceWord" in render_of,
        "four replies from two models are two voices; counting replies would read as consensus",
    )
    check(
        "a seat that answered more than once is told apart",
        "reply.occurrence" in replies_of
        and '" (reply " + reply.occurrence + ")"' in replies_of
        and "row.reply.display" in render_of,
        "the Arbiter answers in the round and again for its verdict",
    )
    check(
        "dissent is named, counted, and left where it is",
        "not in the decision" in summary_of
        and "Dissent:" in render_of
        and "stays in the transcript" in render_of
        and "hallStream" not in verdict_functions
        and "innerHTML" not in verdict_functions.replace('box.innerHTML = ', ""),
        "a verdict that rewrites the replies above it cannot preserve dissent",
    )
    check(
        "the block is not rewritten while nothing in it has moved",
        "box.dataset.signature === signature" in render_of,
        "a redraw per token would move the focus off the copy control",
    )
    check(
        "Copy verdict writes the same table as markdown",
        "| Seat | Model | Size | " in markdown_of
        and "Dissent:" in markdown_of
        and "navigator.clipboard" in copy_body
        and "4-word sequences" in markdown_of,
        "a verdict that cannot be quoted is half an artifact",
    )
    check(
        "the pinned conclusion banner still draws the pinned text",
        "hallConclusionBanner" in html
        and "s.conclusion_text" in render_hall
        and "Pinned Council Conclusion" in render_hall,
        "the surface this story builds on may not be replaced by it",
    )
    check(
        "every value the verdict draws is escaped",
        not unescaped_interpolations(verdict_functions),
        ", ".join(unescaped_interpolations(verdict_functions)),
    )
    check(
        "the pane calls no route for the verdict, and invents none",
        called_routes(verdict_functions) == [],
        ", ".join(called_routes(verdict_functions)),
    )

    # ── the evidence this story ships ───────────────────────────────────────
    check(
        "the fixture answers each catalogue model with its own paragraph, and a stranger with a line",
        "REPLY_TEXTS = {" in seed
        and all(model in seed for model in ("LOADED_MODEL", "OVER_BUDGET_MODEL", "FITS_MODEL"))
        and "def _reply_words(model: str) -> str:" in seed
        and 'words = _reply_words(model).split(" ")' in seed
        and "return f\"The fixture runtime answered as {model}" in seed,
        "distinguishable replies are what makes four voices measurable",
    )
    check(
        "the walk exercises every state this story adds",
        all(
            token in walk_body
            for token in (
                "no decision pinned",
                "Dissent:",
                "One voice, not 4:",
                "M5CouncilVerdict.markdown()",
                "shared wording is the fraction",
            )
        ),
        "the states have to be seen, not only asserted",
    )
    check(
        "the captures the doc lists exist and are the pane's own",
        all((MEDIA / name).exists() for name in CAPTURES)
        and all(f"{name[:-4]}" in walk_body for name in CAPTURES),
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
        "title": "Council consensus, disagreement and results",
        "generated_by": "scripts/uiux_council_verdict_inventory.py",
        "surfaces": {
            "transcript": "#hallStream",
            "decision": "#hallConclusionBanner",
            "verdict": "#hallVerdict",
        },
        "decision": {
            "stored_in": list(CONCLUSION_COLUMNS),
            "writers": ["api_hall_summon (pin_as_conclusion)", "api_hall_close"],
            "is_a_seat_reply": True,
            "matched_on": "the author the server recorded and the words it kept",
        },
        "measure": {
            "name": "shared wording",
            "algorithm": "containment of a reply's n-word sequences in the text it is compared with",
            "n": 4,
            "short_text": "single words for a reply shorter than four words",
            "thresholds": {"same": 0.85, "mostly": 0.70, "partly": 0.25},
            "claims": "text, not intent",
        },
        "readings": list(READINGS),
        "columns": list(TABLE_COLUMNS),
        "stream_events": list(STREAM_EVENTS),
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-03 — Council consensus, disagreement and results",
        "",
        "Generated by `scripts/uiux_council_verdict_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## What the Hall now says",
        "",
        "| Where | What it says |",
        "| :--- | :--- |",
        "| the transcript | every reply as the seat wrote it — unchanged, and never merged |",
        "| the pinned conclusion | the decision's own text, with the author the server recorded |",
        "| the verdict area | where the decision came from, how much of each reply's wording it carries,"
        " and whether the room said four things or one thing four times |",
        "",
        "## The measure, exactly",
        "",
        "`shared wording` is the fraction of a reply's 4-word sequences that also appear in the text",
        "it is compared with — single words for a reply shorter than four words. Containment, not",
        "Jaccard: a short reply carried by a long decision should read as carried.",
        "",
        "It is a text measure. A seat that makes the same point in its own words scores low and is",
        "drawn as `not in the decision`; a seat that repeats a phrase scores high and is drawn as",
        "`the same words as …`. Neither means that a seat agreed with anything, and the pane says so",
        "in the block itself.",
        "",
        "## The readings",
        "",
    ]
    for reading in payload["readings"]:
        explanation = {
            "is the decision": "this reply is the decision, word for word",
            "mostly in the decision": "at least 70 % of its wording reappears in the decision",
            "partly in the decision": "between 25 % and 70 % of its wording reappears",
            "not in the decision": "less than 25 % — named under **Dissent** and left in the transcript",
            "the same words as": "at least 85 % shared with another seat (no decision pinned)",
            "echoes": "between 25 % and 85 % shared with another seat",
            "its own words": "under 25 % shared with any other reply",
        }[reading]
        lines.append(f"* `{reading}` — {explanation}.")
    lines += [
        "",
        "## What it refuses to invent",
        "",
        "* **consensus** — four replies are not a vote, and the pane has no ballot to count. It states",
        "  shared wording and the number of distinct voices instead;",
        "* **agreement** — the measure cannot read intent. A seat that agrees keeps its own words and",
        "  scores low; a seat that repeats a phrase without agreeing scores high;",
        "* **a synthesis** — the decision is one seat's reply as stored, and the block says so. The pane",
        "  never composes a conclusion and never rewrites a reply;",
        "* **a summary** — the transcript is the record. The verdict block adds a table, and writes",
        "  nothing into the stream it describes;",
        "* **a rate** — the block prints characters, not tokens per second, and a demo reply keeps no",
        "  fabricated rate.",
        "",
        "## The captures",
        "",
        "All six come from `node scripts/council_verdict_walk.mjs --screenshots docs/media/council-verdict`,",
        "against `scripts/council_readiness_seed.py`.",
        "",
        "| File | What it shows |",
        "| :--- | :--- |",
        "| `council-verdict-unpinned.png` | four replies from two models, nothing pinned:"
        " `4 replies · 2 voices · no decision pinned`, and the repeated pairs named |",
        "| `council-verdict-pinned.png` | the pinned conclusion as the server stored it |",
        "| `council-verdict-decision.png` | the verdict after the Arbiter pinned its ruling:"
        " `is the decision`, `mostly in the decision`, and two replies named under **Dissent** |",
        "| `council-verdict-narrow.png` | the same block at a 480 px viewport |",
        "| `council-verdict-light.png` | the same readings in the light theme |",
        "| `council-verdict-offline.png` | no runtime: every seat is demo text, and the block says"
        " `4 replies · 1 voice` rather than presenting four identical replies as a council that agrees |",
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
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


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
                    "the council-verdict inventory has drifted from the source: "
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
