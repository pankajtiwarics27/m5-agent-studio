#!/usr/bin/env python3
"""Inventory how a comparison is kept, restored, read back and exported (UIUX-P4-06).

    python3 scripts/uiux_compare_continuity_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_compare_continuity_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_compare_continuity_inventory.py --json     # print the inventory

A comparison was twenty minutes of work on screen and nothing else: reload and it was gone. The
server did save a `runs` row (`debate_done` carries its id; `save_run` returns 0 when the write
fails), but this pane never mentioned it and had no way to reach it - and there was no way to take
the comparison out of the page at all except by reading the screen.

What is measured here:

* **the record**: the bar states where the comparison on screen came from - just run here, kept by
  this browser, read back from the server's export, or not kept at all - and every restored view
  says it is not a live run, because a recovered run passing for a live one is the defect this
  program keeps re-learning;
* **the save**: the run id comes from `debate_done`, and a zero id is stated as *not saved* rather
  than drawn as a success;
* **the copy**: what this browser keeps is capped, the truncation is recorded and stated, and
  forgetting it says what it did not touch (the server's record);
* **the read-back**: the server's own copy comes from `GET /api/history/export` (nested under
  `tables.runs`), matched by id and mode, and labelled as the redacted export it is;
* **the export**: markdown and JSON both keep every reply and the arbiter under its own heading,
  with the measured table, and neither picks a winner or merges the answers.
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
WALK = PROJECT_ROOT / "scripts" / "compare_continuity_walk.mjs"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
MEDIA = PROJECT_ROOT / "docs" / "media" / "compare-continuity"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Compare_Continuity.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-compare-continuity.md"

STORY = "UIUX-P4-06"

# The four sources the bar may claim, and the only ones.
SOURCES = ("live", "browser", "server", "detached")

# What the export may never say. It reports; it does not choose.
FORBIDDEN_CLAIMS = ("best answer", "the winner", "winner is", "recommended answer", "final answer")

# The routes this story is allowed to touch.
ROUTES = ("/api/debate/stream", "/api/history/export")

# The captures the doc lists and the walk writes.
CAPTURES = (
    "compare-record-live.png",
    "compare-record-restored.png",
    "compare-record-server.png",
    "compare-record-forgotten.png",
    "compare-record-light.png",
    "compare-record-narrow.png",
)

LIVE_CLAIM = 'debateSource = "live";'

SAFE_EXPRESSIONS = {"", "null", "undefined", "true", "false"}


# ── reading the sources ─────────────────────────────────────────────────────


def function_body(source: str, name: str) -> str:
    """The balanced body of ``function name(...)``."""
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


def element(source: str, element_id: str) -> str:
    match = re.search(rf"<[a-z]+[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", source)
    return match.group(0) if match else ""


def called_routes(source: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer("""["'`](/api/[^"'`]*)["'`]""", source):
        path = re.sub(r"\$\{[^}]*\}", "{}", match.group(1)).split("?")[0]
        if path not in found:
            found.append(path)
    return found


def unescaped_interpolations(markup: str) -> list[str]:
    offenders: list[str] = []
    for expression in _expressions(markup):
        if "${" in expression:
            offenders.extend(unescaped_interpolations(expression))
            continue
        stripped = expression.strip()
        if stripped in SAFE_EXPRESSIONS:
            continue
        if stripped.startswith(("escapeHtml(", "Number(", "String(", "Boolean(", "renderMarkdown(")):
            continue
        if re.fullmatch(r"[A-Za-z_$][\w$]*Html", stripped):
            continue
        offenders.append(stripped)
    return offenders


def _expressions(markup: str) -> list[str]:
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


# ── the inventory ───────────────────────────────────────────────────────────


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    app_source = APP.read_text(encoding="utf-8")
    seed = SEED.read_text(encoding="utf-8") if SEED.exists() else ""
    walk_body = WALK.read_text(encoding="utf-8") if WALK.exists() else ""

    snapshot = function_body(html, "debateSnapshot")
    capped = function_body(html, "debateSnapshotCapped")
    write = function_body(html, "debateRecordWrite")
    read = function_body(html, "debateRecordRead")
    clear = function_body(html, "debateRecordClear")
    apply_snapshot = function_body(html, "debateApplySnapshot")
    markdown = function_body(html, "debateComparisonMarkdown")
    json_export = function_body(html, "debateComparisonJson")
    read_server = function_body(html, "debateReadServerRecord")
    render = function_body(html, "renderDebateRecord")
    run = function_body(html, "runDebate")
    init = function_body(html, "initDebatePane")

    record_block = "".join([snapshot, capped, write, read, clear, apply_snapshot, markdown, json_export, read_server, render])

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": "" if ok else detail})

    # ── the bar ─────────────────────────────────────────────────────────────
    check(
        "the pane states where this comparison came from, in a labelled block of its own",
        'id="debateRecord"' in html
        and 'role="region"' in element(html, "debateRecord")
        and ("This comparison's record and export" in element(html, "debateRecord"))
        and html.index('id="debateProvenance"') < html.index('id="debateRecord"')
        and "renderDebateRecord();" in function_body(html, "renderDebate"),
        "a comparison with no stated provenance is a screenshot",
    )
    check(
        "the four sources are named, and a restored comparison never passes for a live one",
        all(f'"{source}"' in record_block for source in SOURCES)
        and "not the server's record, and not a live run" in render
        and "read back from the server's redacted export" in render
        and "a reload will lose it" in render
        and "This comparison is the one you just ran on this page" in render,
        ", ".join(source for source in SOURCES if f'"{source}"' not in record_block),
    )
    check(
        "the run's id comes from the response, and a zero id is stated as not saved",
        "debateRunId = Number(d.run_id || 0);" in run
        and "The server did not save this run" in render
        and "The server saved this run as <strong>" in render,
        "a save the server did not make must not be drawn as one",
    )
    check(
        "the bar says which models a restored comparison used, and when the list no longer offers them",
        "missing" in render
        and "are not all listed right now" in render
        and "so Start would compare something else" in render,
        "a restored comparison whose models are gone must not look re-runnable",
    )

    # ── the copy this browser keeps ─────────────────────────────────────────
    # The stream's completion handler runs for a finished, a stopped and a lost run alike, so that
    # is where the copy is written - not in the success path only.
    completion = ""
    if "debateRecordWrite();" in html:
        index = html.index("debateRecordWrite();")
        completion = html[max(0, index - 900) : index + 200]
    check(
        "the comparison is written when a run ends, whatever way it ended",
        html.count("debateRecordWrite();") == 1
        and "localStorage.setItem(DEBATE_RECORD_KEY" in write
        and 'if (debatePanels().some(slot => slot.status === "running"' in completion
        and "debateRecordWrite();" in completion,
        "the run that failed is the one a reader most wants back",
    )
    check(
        "the copy is capped, and the truncation is recorded rather than hidden",
        "DEBATE_RECORD_MAX_CHARS = 60000" in html
        and "snapshot.truncated = dropped > 0;" in capped
        and "dropped_chars" in capped
        and "characters of the longest replies were dropped" in render,
        "a silently shortened comparision is a different comparison",
    )
    check(
        "the copy is read back on load, and the reader is told it is this browser's",
        "debateRecordRead()" in init
        and 'debateApplySnapshot(keptComparison, "browser"' in init
        and 'debateLocalKept = source === "browser" ? true : debateLocalKept;' in apply_snapshot,
        "a restore that cannot state its source is a false memory",
    )
    check(
        "forgetting says what it did not touch, and leaves the pane stating it keeps nothing",
        "debateRecordClear" in html
        and "the server's record, if any, is untouched" in html
        and 'debateSource = "detached";' in html
        and "not keeping a copy of it, and a reload will lose it" in render,
        "forget that also implies the server's copy is gone would be a lie",
    )

    # ── the server's copy ───────────────────────────────────────────────────
    check(
        "the server's copy is read from the export that already exists, by id and mode",
        'fetch("/api/history/export")' in read_server
        and "bundle && bundle.tables ? bundle.tables" in read_server
        and "tables.runs" in read_server
        and 'String(entry.mode || "debate") === "debate"' in read_server
        and "Number(entry.id) === Number(debateRunId)" in read_server,
        "a record read back from anything but the server's own export is a rumour",
    )
    check(
        "the read-back is labelled as the redacted export it is",
        "tables.runs" in read_server
        and "read back from the server's redacted export" in render
        and "credential-shaped text in it was replaced by the exporter" in markdown,
        "an export scrubs credentials, and a copy of it must say so",
    )
    check(
        "a recorded reply's status is derived from the usage the server stored",
        "debateRecordedStatus" in html
        and 'usage.demo === true || usage.demo === "true"' in function_body(html, "debateRecordedStatus")
        and 'String(usage.finish_reason || "") === "length"' in function_body(html, "debateRecordedStatus"),
        "a restored reply is the same reply only if its status is read from the same payload",
    )

    # ── the export ──────────────────────────────────────────────────────────
    check(
        "the markdown export keeps every reply and the arbiter under their own headings",
        "## Opinion " in markdown
        and "## Arbiter verdict" in markdown
        and "## Measured performance" in markdown
        and "panels.forEach" in markdown,
        "an export that merges the answers is the collapse this story exists to prevent",
    )
    check(
        "the export picks no winner, and says so",
        "nothing here picks a winner" in markdown
        and all(claim not in markdown.lower() for claim in FORBIDDEN_CLAIMS),
        ", ".join(claim for claim in FORBIDDEN_CLAIMS if claim in markdown.lower()),
    )
    check(
        "the export states which copy it is, and whether the server saved it",
        "This copy was kept by this browser" in markdown
        and "read back from the server's redacted export" in markdown
        and "The server did not save this run." in markdown
        and "Cost is not stated: no price is configured for any model." in markdown,
        "an exported comparison with no provenance is an unattributable document",
    )
    check(
        "the JSON export carries one entry per panel with its usage, and its source",
        "panels.map(" in snapshot
        and "usage: panel.u || null" in snapshot
        and "synthesis:" in snapshot
        and "snapshot.source = debateSource;" in json_export,
        "the JSON is the machine-readable half of the same promise",
    )
    check(
        "the export is offered as a copy and as a download, and a refusal is stated",
        "debateCopyComparison(this)" in render
        and "debateDownloadComparison()" in render
        and "The clipboard refused; the comparison is on screen" in html
        and "This browser refused the download; copy the comparison instead" in html,
        "an export that fails silently is worse than none",
    )

    # ── the contracts ───────────────────────────────────────────────────────
    check(
        "the pane touches only the two routes it is allowed to",
        set(called_routes(record_block)) <= set(ROUTES)
        and called_routes(record_block) == ["/api/history/export"],
        ", ".join(called_routes(record_block)),
    )
    check(
        "the bar escapes what it draws",
        not unescaped_interpolations(render),
        ", ".join(unescaped_interpolations(render)),
    )
    # One declaration (`let debateSource = "live";`) and one assignment, inside runDebate: nothing
    # else may claim a restored comparison is live.
    check(
        "only a run sets the source back to live",
        html.count(LIVE_CLAIM) == 2
        and f"let {LIVE_CLAIM}" in html
        and LIVE_CLAIM in run,
        f"{LIVE_CLAIM} appears {html.count(LIVE_CLAIM)} times",
    )
    check(
        "the server still saves the comparison it saved before, and the export still carries it",
        'save_run("debate"' in app_source
        and '"runs"' in (PROJECT_ROOT / "history_privacy.py").read_text(encoding="utf-8"),
        "the story reads a record the server already wrote; it does not invent one",
    )

    # ── the evidence ────────────────────────────────────────────────────────
    check(
        "the fixture can produce a saved run and an unsaved one",
        "--resident-model" in seed and "--slow-model" in seed,
        "a comparison that answers needs two resident models; a stopped one needs a slow reply",
    )
    check(
        "the walk reloads, reads the server's copy, forgets it and exports",
        all(
            token in walk_body
            for token in (
                "page.reload(", 
                "debateComparisonMarkdown",
                "Read the server",
                "Forget this copy",
                "GET /api/history/export",
                "this browser's copy",
            )
        ),
        "the story's proof is a reload, so the walk has to perform one",
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
        "title": "Saved comparison and export continuity",
        "generated_by": "scripts/uiux_compare_continuity_inventory.py",
        "surface": "#debateRecord",
        "sources": list(SOURCES),
        "storage": {
            "key": "m5_debate_comparison",
            "cap_chars": 60000,
            "what_is_kept": "every panel with its model, status, reply and usage; the arbiter; the question; the criteria; the run id",
        },
        "server_record": {
            "route": "GET /api/history/export",
            "table": "tables.runs",
            "match": "id and mode=debate",
            "redacted": True,
        },
        "exports": ["markdown (clipboard)", "json (download)"],
        "routes": list(ROUTES),
        "captures": list(CAPTURES),
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
        "status": "clean" if not findings else "drifted",
    }


def render_doc(payload: dict) -> str:
    lines = [
        "# UIUX-P4-06 — Saved comparison and export continuity (Compare)",
        "",
        "Generated by `scripts/uiux_compare_continuity_inventory.py`. Do not edit by hand:",
        "regenerate it, and the check behind it stays true.",
        "",
        "## The problem",
        "",
        "A comparison was twenty minutes of work on screen and nothing else. `reload` deleted it. The",
        "server did save a `runs` row — `debate_done` carries its id, and `save_run` returns `0` when",
        "the write fails — but the pane never mentioned it and could not reach it, and the only way to",
        "take a comparison out of the page was to read the screen.",
        "",
        "## What the record bar says",
        "",
        "`#debateRecord` sits under the measured-performance block and names exactly one of four",
        "sources every time it is drawn:",
        "",
        "| Source | What it means |",
        "| :--- | :--- |",
        "| `live` | the comparison you just ran on this page |",
        "| `browser` | this browser's copy, kept when the run ended — *not* the server's record and not a live run |",
        "| `server` | read back from `GET /api/history/export`, i.e. the redacted copy on record |",
        "| `detached` | this browser is keeping nothing: a reload will lose it |",
        "",
        "It also states the server's verdict on the save itself: `The server saved this run as run N`",
        "or `The server did not save this run (no run id came back), so the only copy is the one this",
        "page holds`. A restored comparison whose models are no longer in the list says so too, rather",
        "than looking re-runnable.",
        "",
        "## What is kept, and where",
        "",
        f"* This browser: `{payload['storage']['key']}` in `localStorage`, capped at "
        f"{payload['storage']['cap_chars']:,} characters. Only the longest replies are shortened, and "
        "the drop is recorded and stated on the bar — a silently shortened comparison is a different "
        "comparison.",
        "* The server: `runs` (mode `debate`), written by `save_run` at the end of every run, read back",
        "  here through the export where it sits under `tables.runs`, matched by id and mode. The",
        "  export redacts credential-shaped text, so the bar and the export both call it the redacted",
        "  copy.",
        "",
        "## How it leaves the page",
        "",
        "* **Copy as markdown** — one `## Opinion N · model · status` section per reply, the arbiter",
        "  under `## Arbiter verdict`, the measured table, and the sentences that matter: *nothing here",
        "  picks a winner*, *cost is not stated*, and which copy this is.",
        "* **Download .json** — the same comparison as data: one entry per panel with model, status,",
        "  content and the `usage` the response carried, plus the arbiter, the question, the criteria",
        "  and the run id.",
        "",
        "Neither export merges the answers, ranks them, or drops the failed and the stopped ones: the",
        "markdown says so in as many words — *nothing here picks a winner or merges the answers*.",
        "",
        "## The captures",
        "",
        "All six come from `node scripts/compare_continuity_walk.mjs --screenshots docs/media/compare-continuity`,",
        "against two `scripts/council_readiness_seed.py` processes (`--resident-model` so two panels",
        "answer and there is an arbiter verdict to export; `--slow-model` so a run can be stopped and a",
        "run the server never saved can be checked).",
        "",
        "| File | Size | What it shows |",
        "| :--- | :--- | :--- |",
        "| `compare-record-live.png` | 868 × 635 | the results table, the measured table and the record"
        " bar right after a run: `ran on this page`, *a copy is kept in this browser, so a reload restores"
        " it*, `The server saved this run as run 1`, and the three buttons |",
        "| `compare-record-restored.png` | 868 × 427 | the same comparison after a reload: the measured"
        " figures identical, the bar now `kept in this browser … not the server's record, and not a live"
        " run`, and `Forget this copy` offered |",
        "| `compare-record-server.png` | 868 × 202 | the server's own copy read back from the redacted"
        " export |",
        "| `compare-record-forgotten.png` | 868 × 202 | after forgetting: `not kept here`, *a reload will"
        " lose it* |",
        "| `compare-record-light.png` | 868 × 414 | the same state and figures in the light theme |",
        "| `compare-record-narrow.png` | 480 × 900 | the bar and its buttons at a 480 px viewport |",
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
                    "the compare-continuity inventory has drifted from the source: "
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
