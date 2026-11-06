#!/usr/bin/env python3
"""Inventory the Chat pane's persona registry and the picker it draws (UIV2-19).

    python3 scripts/uiux_chat_persona_inventory.py            # regenerate JSON + doc
    python3 scripts/uiux_chat_persona_inventory.py --check    # fail on drift or a broken rule
    python3 scripts/uiux_chat_persona_inventory.py --json     # print the inventory

The pane shipped three personas as `<option value="You are a concise Principal Software Engineer …">
`. The picker *was* the contract, and every consequence of that followed:

* **nothing could describe a persona.** Choosing between "Bug & UX Auditor" and "Code Only" meant
  sending a question to both and reading the answers.
* **nothing could be remembered.** Every reload went back to the first option.
* **the value was the prompt.** The request carried a 130-character paragraph as a `<select>`
  value, so the picker could not be filtered, sorted or identified without shipping the prose.
* **a deleted persona was invisible.** An option removed from the markup silently became whichever
  option happened to be first, with the request carrying that persona's prompt instead.

`CHAT_PERSONAS` is the authority now: an id, a label, a one-line description and the prompt, in one
place. The markup's select ships no options at all and is filled by `renderChatPersonaPicker()`.

What this inventory checks: that the registry is well formed (ids, labels, descriptions, prompts,
uniqueness, a default that exists); that the picker is drawn from it and ships nothing of its own;
that the filter, its labels and the description line are wired for a keyboard and a screen reader;
that the *prompt* and not the id is what the request sends; that a stored selection survives a
reload and an unknown one falls back to the default with the reason shown; that the prose-value
defect cannot come back; and that the picker still fits the control bar UIV2-18 bought back.

It fails when a persona is added without a description, when the picker starts shipping options
again, when the request sends something the registry does not name, or when a stored id can point
at a persona that is not in the table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
WALK_PATH = PROJECT_ROOT / "scripts" / "chat_persona_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Chat_Persona.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-chat-persona.md"
MEDIA_DIR = PROJECT_ROOT / "docs" / "media" / "chat-persona"

STORY = "UIV2-19"
TITLE = "Chat persona registry audit and complete supported picker"

#: A persona is only useful if its description fits one clamped line and its id can be stored.
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_MIN = 30
DESCRIPTION_MAX = 140
LABEL_MAX = 40

#: The design system's floor for a control a reader types an answer into; the filter is documented
#: as the one deliberate exception, and it may not go below the target-size floor either.
CONTROL_MIN_H = 44
TARGET_MIN = 24


def function_body(source: str, name: str) -> str:
    """The body of a top-level `function name(...) { … }`, braces balanced.

    The opening brace is looked for after the parameter list closes: a default parameter such as
    `options = {}` would otherwise be mistaken for the body, and every check below would read `{}`.
    """
    start = source.find(f"function {name}(")
    if start < 0:
        return ""
    paren = source.index("(", start)
    depth_params = 0
    for position in range(paren, len(source)):
        if source[position] == "(":
            depth_params += 1
        elif source[position] == ")":
            depth_params -= 1
            if depth_params == 0:
                paren = position
                break
    index = source.index("{", paren)
    depth = 0
    for position in range(index, len(source)):
        char = source[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index : position + 1]
    return ""


def read_registry(script: str) -> list[dict[str, str]]:
    start = script.find("const CHAT_PERSONAS = [")
    if start < 0:
        return []
    end = script.index("];", start)
    table = script[start:end]
    entries = []
    for block in re.findall(r"\{[^{}]*\}", table):
        entry = {}
        for field in ("id", "label", "description", "prompt"):
            match = re.search(rf'{field}:\s*"((?:[^"\\]|\\.)*)"', block)
            if match:
                entry[field] = match.group(1)
        if entry:
            entries.append(entry)
    return entries


def read_css(html: str) -> str:
    return "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.S))


def rule(css: str, selector: str) -> str:
    """Every declaration block whose selector list contains `selector`, joined.

    The comment that introduces a rule belongs to the same `[^{}]+` chunk as its selector, so it is
    stripped first: otherwise a rule written after a paragraph of explanation is unfindable.
    """
    blocks = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        cleaned = re.sub(r"/\*.*?\*/", "", selectors, flags=re.S)
        names = [part.strip() for part in cleaned.split(",")]
        if selector in names:
            blocks.append(body)
    return "\n".join(blocks)


def build_inventory() -> dict:
    html = FRONTEND.read_text(encoding="utf-8")
    script = html[html.index("<script>") :]
    css = read_css(html)

    personas = read_registry(script)
    ids = [entry.get("id", "") for entry in personas]
    labels = [entry.get("label", "") for entry in personas]
    descriptions = [entry.get("description", "") for entry in personas]
    prompts = [entry.get("prompt", "") for entry in personas]

    select = re.search(r'<select\b[^>]*\bid="chatPersona"[^>]*>(.*?)</select>', html, re.S)
    shipped_options = re.findall(r"<option\b", select.group(1)) if select else []
    select_tag = select.group(0) if select else ""

    render = function_body(script, "renderChatPersonaPicker")
    by_id = function_body(script, "chatPersonaById")
    matches = function_body(script, "chatPersonaMatches")
    describe = function_body(script, "updateChatPersonaDescription")
    label_fn = function_body(script, "chatPersonaLabel")
    request = function_body(script, "sendChatRequest")
    boot = script[script.find("renderChatPersonaPicker();") :]

    default_match = re.search(r'const CHAT_PERSONA_DEFAULT = "([^"]+)"', script)
    key_match = re.search(r'const CHAT_PERSONA_KEY = "([^"]+)"', script)
    default_id = default_match.group(1) if default_match else ""
    storage_key = key_match.group(1) if key_match else ""

    checks: list[dict[str, object]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    # ── the registry ──────────────────────────────────────────────────────────
    check(
        "the registry exists and holds at least the three personas the pane shipped",
        len(personas) >= 3,
        f"{len(personas)} persona(s): {', '.join(ids) or 'none'}",
    )
    check(
        "every persona has an id, a label, a description and a prompt",
        bool(personas)
        and all(entry.get(field) for entry in personas for field in ("id", "label", "description", "prompt")),
        "; ".join(
            f"{entry.get('id', '?')}: "
            + ",".join(field for field in ("label", "description", "prompt") if not entry.get(field))
            for entry in personas
        )
        or "all four fields present",
    )
    check(
        "ids are stable slugs, not prose",
        bool(ids) and all(ID_RE.match(pid) for pid in ids),
        ", ".join(ids),
    )
    check(
        "ids, labels and prompts are all distinct",
        len(set(ids)) == len(ids) and len(set(labels)) == len(labels) and len(set(prompts)) == len(prompts),
        f"{len(set(ids))} unique id(s), {len(set(labels))} unique label(s), {len(set(prompts))} unique prompt(s)",
    )
    check(
        "every label fits a picker row",
        bool(labels) and all(len(label) <= LABEL_MAX for label in labels),
        f"longest {max((len(label) for label in labels), default=0)} chars (limit {LABEL_MAX})",
    )
    check(
        "every description explains the persona in one line",
        bool(descriptions)
        and all(DESCRIPTION_MIN <= len(text) <= DESCRIPTION_MAX for text in descriptions),
        f"{min((len(t) for t in descriptions), default=0)}-{max((len(t) for t in descriptions), default=0)} chars "
        f"(band {DESCRIPTION_MIN}-{DESCRIPTION_MAX})",
    )
    check(
        "every prompt is a system prompt the pane can send",
        bool(prompts) and all(10 <= len(prompt) <= 400 for prompt in prompts),
        f"{min((len(p) for p in prompts), default=0)}-{max((len(p) for p in prompts), default=0)} chars",
    )
    check(
        "the default persona is one the registry names",
        bool(default_id) and default_id in ids,
        f"CHAT_PERSONA_DEFAULT = {default_id or '(missing)'}",
    )
    check(
        "the pane remembers the selection under its own key",
        storage_key == "m5_chat_persona",
        f"CHAT_PERSONA_KEY = {storage_key or '(missing)'}",
    )

    # ── the picker is drawn from the registry ────────────────────────────────
    check(
        "the markup ships no persona options of its own",
        select is not None and not shipped_options,
        f"{len(shipped_options)} <option> in select#chatPersona"
        + (f" — {shipped_options}" if shipped_options else ""),
    )
    check(
        "the picker is filled from the registry, with nothing hard-coded beside it",
        "CHAT_PERSONAS" in render and "CHAT_PERSONAS.filter" in render and "persona.label" in render,
        "renderChatPersonaPicker() filters CHAT_PERSONAS and writes persona.label",
    )
    check(
        "the picker escapes what it writes into the markup",
        "escapeHtml(persona.id)" in render and "escapeHtml(persona.label)" in render,
        "both the id and the label go through escapeHtml",
    )
    check(
        "every registry entry reaches the picker, and the count says how many",
        "chatPersonaCount" in render and "CHAT_PERSONAS.length" in render,
        "the label shows (N) / N of M while filtering",
    )

    # ── the filter and the description are usable ────────────────────────────
    check(
        "the filter is labelled, and controls the picker it filters",
        bool(re.search(r'<input\b[^>]*id="chatPersonaSearch"[^>]*aria-label="Filter personas"', html, re.S))
        and 'aria-controls="chatPersona"' in html,
        'input#chatPersonaSearch with aria-label="Filter personas" and aria-controls',
    )
    check(
        "the filter reads labels and descriptions, never prompts",
        "persona.label.toLowerCase().includes" in matches
        and "persona.description.toLowerCase().includes" in matches
        and "prompt" not in matches,
        "chatPersonaMatches() looks at label and description only",
    )
    description_tag = re.search(r'<p\b[^>]*\bid="chatPersonaDescription"[^>]*>', html)
    check(
        "the description line is announced, and the picker points at it",
        description_tag is not None
        and 'aria-live="polite"' in description_tag.group(0)
        and 'aria-describedby="chatPersonaDescription"' in select_tag
        and "chatPersonaDescription" in describe,
        "select#chatPersona aria-describedby=chatPersonaDescription, whose own tag is aria-live=polite",
    )
    check(
        "the description says what the selected persona does, not what it is called",
        describe.count("persona.description") >= 2
        and "host.textContent = persona.label" not in describe,
        "updateChatPersonaDescription() writes persona.description, and names the label only when "
        "the filter matched nothing else",
    )
    check(
        "the description is one clamped line, with the whole sentence in its title",
        "text-overflow: ellipsis" in rule(css, ".chat-persona-description")
        and "white-space: nowrap" in rule(css, ".chat-persona-description")
        and "host.title = text" in describe,
        "nowrap + ellipsis in CSS, and the full text on the element's title",
    )
    check(
        "Enter chooses the first persona the filter matched",
        "CHAT_PERSONAS.find(persona => chatPersonaMatches(persona, needle))" in boot,
        "the Enter handler resolves through chatPersonaMatches, not through options[0]",
    )
    check(
        "Escape clears the filter rather than leaving an empty list",
        'event.key === "Escape"' in boot and 'renderChatPersonaPicker("")' in boot,
        "Escape empties the filter and redraws every persona",
    )
    check(
        "the filter never filters out the persona that is about to be sent",
        "|| persona.id === selected" in render,
        "the selected persona is kept in the list whatever the filter says",
    )

    # ── the request carries the prompt, never the id ─────────────────────────
    check(
        "the request sends the selected persona's prompt",
        "chatPersonaSelected()" in request and ".prompt" in request,
        "sendChatRequest() builds the system message from chatPersonaSelected().prompt",
    )
    check(
        "nothing sends the select's value any more",
        'document.getElementById("chatPersona").value' not in script,
        "no read of #chatPersona.value outside the picker helpers",
    )
    check(
        "the context line still names the persona by its label",
        "sel.options[sel.selectedIndex].textContent" in label_fn,
        "chatPersonaLabel() reads the selected option's text",
    )

    # ── fallback: a stored id can only name a persona that exists ────────────
    check(
        "a stored id resolves through the registry",
        "CHAT_PERSONAS.find" in by_id and "CHAT_PERSONA_DEFAULT" in by_id,
        "chatPersonaById() searches the table and falls back to the default id",
    )
    check(
        "a persona cannot be synthesised from an unknown id",
        "prompt:" not in by_id and "description:" not in by_id,
        "chatPersonaById() returns a registry entry or nothing; it never builds one",
    )
    check(
        "a deleted persona is detected, not silently replaced",
        "!CHAT_PERSONAS.some(entry => entry.id === stored)" in describe
        and "no longer available" in describe,
        "the fallback note fires only when the stored id is absent from the table",
    )
    check(
        "the choice is written to storage when it is made",
        "localStorage.setItem(CHAT_PERSONA_KEY" in script,
        "selectChatPersona() stores the id, guarded against blocked storage",
    )
    check(
        "the picker is drawn before the pane is used",
        "renderChatPersonaPicker();" in script,
        "boot draws the picker, then wires the filter and the change handler",
    )

    # ── the design system, and the row UIV2-18 bought back ───────────────────
    filter_rule = rule(css, ".chat-header-bar #chatPersonaSearch")
    search_height = re.search(r"min-height:\s*(\d+)px", filter_rule)
    check(
        "the filter is the one documented exception to the control floor, and stays tappable",
        search_height is not None
        and TARGET_MIN <= int(search_height.group(1)) < CONTROL_MIN_H,
        f"min-height {search_height.group(1) if search_height else '?'}px "
        f"(control floor {CONTROL_MIN_H}px, target floor {TARGET_MIN}px), declared on the id selector",
    )
    check(
        "the picker and its filter share one row at every width, with no breakpoint of their own",
        "grid-template-columns: minmax(0, 1fr) auto" in rule(css, ".chat-header-bar > div:has(#chatPersona)")
        and not re.search(r"#chatPersonaSearch[^{]*\{[^}]*grid-column: 1;", css),
        "a two-column grid whose select shrinks by min-width, not a stacking breakpoint",
    )

    # ── the walk that proves it ──────────────────────────────────────────────
    walk = WALK_PATH.read_text(encoding="utf-8") if WALK_PATH.exists() else ""
    recorded = len(re.findall(r"record\(", walk))
    check(
        "the browser walk exists and asserts the prompt on the wire",
        "the request carries the selected persona's prompt, not its id" in walk
        and "request.postData()" in walk,
        f"{WALK_PATH.name}: {recorded} recorded checks",
    )
    check(
        "the walk covers the six states the ledger asks for",
        all(state in walk for state in ("empty", "populated", "long", "loading", "error", "recovery")),
        "STATES = empty, populated, long, loading, error, recovery",
    )
    captures = sorted(MEDIA_DIR.glob("*.png")) if MEDIA_DIR.exists() else []
    check(
        "the state captures are present for every viewport and theme",
        len(captures) >= 48,
        f"{len(captures)} PNG in docs/media/chat-persona",
    )

    findings = [
        {"check": entry["check"], "detail": entry["detail"]}
        for entry in checks
        if not entry["passed"]
    ]
    return {
        "story": STORY,
        "title": TITLE,
        "schema_version": 1,
        "source": str(FRONTEND.relative_to(PROJECT_ROOT)),
        "registry": {
            "constant": "CHAT_PERSONAS",
            "personas": personas,
            "default": default_id,
            "storage_key": storage_key,
        },
        "picker": {
            "select": "select#chatPersona",
            "shipped_options": len(shipped_options),
            "renderer": "renderChatPersonaPicker",
            "filter": "input#chatPersonaSearch",
            "description": "#chatPersonaDescription",
        },
        "checks": checks,
        "check_count": len(checks),
        "findings": findings,
    }


def render_doc(payload: dict) -> str:
    registry = payload["registry"]
    lines = [
        "# Chat persona registry",
        "",
        f"Story **{payload['story']}** — {payload['title']}.",
        "",
        "The picker used to be three `<option>` tags whose value was the system prompt, so nothing",
        "could describe a persona, remember a choice, or recover from one being deleted. `CHAT_PERSONAS`",
        "is the authority now, and `select#chatPersona` ships no options of its own.",
        "",
        "## The registry",
        "",
        f"`{registry['constant']}` in `{payload['source']}` — default `{registry['default']}`, remembered",
        f"under `localStorage[{registry['storage_key']!r}]`.",
        "",
        "| id | label | description | prompt |",
        "| --- | --- | --- | --- |",
    ]
    for persona in registry["personas"]:
        lines.append(
            f"| `{persona.get('id', '')}` | {persona.get('label', '')} | "
            f"{persona.get('description', '')} | {persona.get('prompt', '')} |"
        )
    lines += [
        "",
        "## The picker",
        "",
        f"* `{payload['picker']['select']}` ships **{payload['picker']['shipped_options']}** options of",
        "  its own; `renderChatPersonaPicker()` fills it from the registry on load and on every filter.",
        f"* `{payload['picker']['filter']}` narrows by label or description, keeps the selected persona",
        "  in the list, says so when it matches nothing else, clears on Escape and chooses on Enter.",
        f"* `{payload['picker']['description']}` is one clamped line (the whole sentence is its title",
        "  and its text) that says what the selected persona does.",
        "* A stored id that is not in the table falls back to the default persona and says why.",
        "* The request sends `chatPersonaSelected().prompt`; the id never reaches the wire.",
        "",
        "## Checks",
        "",
        f"{payload['check_count']} checks, {len(payload['findings'])} finding(s).",
        "",
        "| check | result | detail |",
        "| --- | --- | --- |",
    ]
    for entry in payload["checks"]:
        lines.append(f"| {entry['check']} | {'pass' if entry['passed'] else '**FAIL**'} | {entry['detail']} |")
    lines += [
        "",
        "## Regenerate",
        "",
        "```",
        "python3 scripts/uiux_chat_persona_inventory.py",
        "python3 scripts/uiux_chat_persona_inventory.py --check",
        "```",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


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
                    "the chat-persona inventory has drifted from the source: "
                    f"re-run `python3 {Path(__file__).name}`",
                    file=sys.stderr,
                )
                return 1
        else:
            JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
            JSON_PATH.write_text(rendered_json, encoding="utf-8")
            DOC_PATH.write_text(rendered_doc, encoding="utf-8")

    findings = payload["findings"]
    if findings:
        for entry in findings:
            print(f"FAIL  {entry['check']} — {entry['detail']}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{payload['check_count']} checks, 0 findings — clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
