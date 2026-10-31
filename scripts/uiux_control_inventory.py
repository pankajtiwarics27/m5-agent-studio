#!/usr/bin/env python3
"""Inventory every control in the six UI panes, by parsing the markup (UIUX-P0-02).

    python3 scripts/uiux_control_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_control_inventory.py --check     # fail if either drifted
    python3 scripts/uiux_control_inventory.py --json      # print the inventory

The story asks for every control, and a control list written by reading a
4576-line single file is the artifact most likely to look finished while being
wrong. So the inventory is parsed, not typed:

* `html.parser` walks the document and tracks which `<section id="pane-...">` each
  interactive element sits in, so nothing is attributed to a pane by guesswork;
* the same walk records controls outside the panes (the chrome: tab bar, top bar,
  monitor rail), because those are reachable from every tab and would otherwise
  vanish from a "per-tab" inventory;
* controls built by inline-script templates or `document.createElement()` are
  counted separately and attributed to the function that renders them, instead of
  being silently omitted.

That last point is the honesty rule of this artifact: the markup inventory is
exact for what is in the markup, and it says out loud that JS-rendered controls
are a different set. A later story can diff both; neither pretends to be the whole
UI.

`--check` compares both outputs against the tree, so a story that adds a control
without regenerating this inventory fails CI rather than drifting quietly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
LEDGER = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
INVENTORY_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Control_Inventory.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-control-inventory.md"

SCHEMA_VERSION = "1.0.0"
STORY = "UIUX-P0-02"
#: `details` is a disclosure container, not a control; its `<summary>` is the thing you click,
#: so counting both would inflate every pane by the number of disclosures.
INTERACTIVE_TAGS = {"button", "input", "select", "textarea", "a", "summary"}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
#: Attributes worth recording. Everything else is noise for an inventory whose
#: job is "what can the user touch, and what does it call".
RECORDED_ATTRS = (
    "id", "name", "type", "role", "class", "aria-label", "aria-labelledby", "aria-describedby",
    "placeholder", "value", "href", "title", "disabled", "readonly", "required", "checked",
    "min", "max", "step", "rows", "cols", "open",
)
HANDLER_ATTRS = ("onclick", "onchange", "oninput", "onsubmit", "onkeydown", "onkeyup", "onfocus", "onblur")
SECTION_ID_RE = re.compile(r"^pane-(?P<dom_id>[a-z]+)$")
SCRIPT_TAG_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)
JS_FUNCTION_RE = re.compile(r"(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(")
JS_INTERACTIVE_RE = re.compile(r"<(button|input|select|textarea|a)\b")
JS_CREATE_ELEMENT_RE = re.compile(
    r"document\.createElement\(\s*['\"](button|input|select|textarea|a|summary)['\"]\s*\)"
)


class InventoryError(RuntimeError):
    """The markup cannot be inventoried, so the inventory cannot be trusted."""


def _rel(path: Path) -> str:
    """Relative for messages when the path is inside the repo, absolute otherwise.

    `--check` is exercised from tests against copies in a temporary directory, and a
    message that crashes while formatting a path would hide the drift it reports.
    """
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ledger_pin() -> str:
    pin = str(json.loads(LEDGER.read_text(encoding="utf-8")).get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise InventoryError(
            f"{LEDGER.name}: source_commit_sha is {pin!r}, not a full 40-char sha; re-pin the ledger first"
        )
    return pin


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _PaneWalker(HTMLParser):
    """Walk the document once, attributing every interactive element to its pane."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.pane_stack: list[str] = []
        self.panes: dict[str, list[dict[str, object]]] = {}
        self.chrome: list[dict[str, object]] = []
        self.sections: list[str] = []
        self._current: dict[str, object] | None = None
        self._text: list[str] = []
        self._current_depth = 0

    # -- helpers ---------------------------------------------------------
    def _record(self, attrs: dict[str, str], tag: str) -> dict[str, object]:
        entry: dict[str, object] = {"tag": tag}
        for name in RECORDED_ATTRS:
            if name in attrs and attrs[name] != "":
                entry[name] = attrs[name]
        for name in HANDLER_ATTRS:
            if attrs.get(name):
                entry[name] = attrs[name]
        data = {k: v for k, v in attrs.items() if k.startswith("data-")}
        if data:
            entry["data"] = dict(sorted(data.items()))
        return entry

    def _flush(self) -> None:
        if self._current is not None:
            if self._text:
                self._current["text"] = _clean("".join(self._text))
            pane = self.pane_stack[-1] if self.pane_stack else None
            target = self.panes.setdefault(pane, []) if pane else self.chrome
            target.append(self._current)
        self._current = None
        self._text = []
        self._current_depth = 0

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v if v is not None else "") for k, v in attrs_list}
        if tag in VOID_TAGS:
            if tag in INTERACTIVE_TAGS:
                entry = self._record(attrs, tag)
                pane = self.pane_stack[-1] if self.pane_stack else None
                (self.panes.setdefault(pane, []) if pane else self.chrome).append(entry)
            return

        self.stack.append(tag)
        if tag == "section":
            section_id = attrs.get("id", "")
            match = SECTION_ID_RE.match(section_id)
            if match:
                dom_id = match.group("dom_id")
                self.sections.append(dom_id)
                self.pane_stack.append(dom_id)
                self.panes.setdefault(dom_id, [])

        if tag in INTERACTIVE_TAGS:
            self._flush()
            self._current = self._record(attrs, tag)
            self._current_depth = len(self.stack)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if self._current is not None and self._current["tag"] == tag and len(self.stack) == self._current_depth:
            self._flush()
        if self.stack:
            # tolerate stray end tags by unwinding to the nearest match
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        if tag == "section" and self.pane_stack:
            self.pane_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)


def parse_markup(html: str) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]], list[str]]:
    walker = _PaneWalker()
    walker.feed(html)
    walker.close()
    walker._flush()
    return walker.panes, walker.chrome, walker.sections


def parse_script_controls(html: str) -> list[dict[str, object]]:
    """Count controls built by inline-script templates or DOM creation."""
    rendered: list[dict[str, object]] = []
    for script in SCRIPT_TAG_RE.finditer(html):
        body = script.group("body")
        matches = [*JS_INTERACTIVE_RE.finditer(body), *JS_CREATE_ELEMENT_RE.finditer(body)]
        for match in sorted(matches, key=lambda item: item.start()):
            preceding = [m for m in JS_FUNCTION_RE.finditer(body, 0, match.start())]
            function = preceding[-1].group("name") if preceding else None
            rendered.append({"tag": match.group(1), "rendered_in_function": function})
    return rendered


def _count_by_tag(controls: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for control in controls:
        tag = str(control["tag"])
        counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items()))


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    panes, chrome, section_ids = parse_markup(html)
    if len(section_ids) != 6:
        raise InventoryError(f"expected six pane sections, parsed {section_ids}")

    pane_entries = []
    for dom_id in section_ids:
        controls = panes.get(dom_id, [])
        pane_entries.append(
            {
                "dom_id": dom_id,
                "section_id": f"pane-{dom_id}",
                "control_count": len(controls),
                "counts": _count_by_tag(controls),
                "controls_without_id": sum(1 for control in controls if not control.get("id")),
                "controls_without_label": sum(
                    1
                    for control in controls
                    if not (control.get("id") or control.get("aria-label") or control.get("text")
                            or control.get("placeholder") or control.get("name"))
                ),
                "controls": controls,
            }
        )

    script_controls = parse_script_controls(html)
    rendered_by: dict[str, int] = {}
    for control in script_controls:
        key = str(control.get("rendered_in_function"))
        rendered_by[key] = rendered_by.get(key, 0) + 1

    markup_total = sum(pane["control_count"] for pane in pane_entries) + len(chrome)
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_control_inventory",
        "program": "M5_2_0_UIUX",
        "story": STORY,
        "generator": "scripts/uiux_control_inventory.py",
        "regenerate_with": "python3 scripts/uiux_control_inventory.py",
        "check_with": "python3 scripts/uiux_control_inventory.py --check",
        "statement": (
            "Every interactive element in static/index.html, attributed to the pane section that "
            "contains it, parsed from the markup. Controls rendered only by inline-script templates "
            "or DOM creation are counted separately and attributed to the function that renders them; neither list "
            "claims to be the whole UI on its own."
        ),
        "pinned_source_commit": _ledger_pin(),
        "frontend_sha256": _sha256(FRONTEND),
        "panes": pane_entries,
        "chrome": {
            "control_count": len(chrome),
            "counts": _count_by_tag(chrome),
            "controls": chrome,
        },
        "totals": {
            "panes": len(pane_entries),
            "markup_controls": markup_total,
            "pane_controls": markup_total - len(chrome),
            "chrome_controls": len(chrome),
            "script_rendered_controls": len(script_controls),
            "script_rendered_by_function": dict(sorted(rendered_by.items())),
        },
        "not_verified": [
            "Nothing here was rendered. A control can exist in the markup and never be shown; the "
            "inventory is a static reading, and capture needs a browser this environment does not have.",
            "Script-rendered controls are counted from inline templates and createElement calls, then "
            "attributed to the nearest preceding function, which is a heuristic, not a call graph.",
            "Reachability, keyboard order and focus behaviour are not measured here; UIUX-P5-04 and "
            "UIUX-P5-05 are where those get tested.",
            "The without-id and without-label columns are a static count, not an accessibility verdict: "
            "a control can be labelled by a wrapping <label> element, which this walk does not resolve.",
        ],
    }


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|") if text else "—"


def render_doc(inventory: dict[str, object]) -> str:
    totals = inventory["totals"]
    lines = [
        "# M5 2.0 UI/UX control inventory",
        "",
        "Generated by `python3 scripts/uiux_control_inventory.py` — **never hand-edit this page**.",
        "It is the `UIUX-P0-02` artifact: every interactive element in",
        "`static/index.html`, attributed to the pane section that contains it, with the counts",
        "re-derived from the markup on every run.",
        "",
        f"* Pinned source commit: `{inventory['pinned_source_commit']}` (the ledger's pin).",
        f"* Frontend digest: `{inventory['frontend_sha256']}`.",
        f"* Panes: {totals['panes']} · controls in panes: {totals['pane_controls']} · controls in the",
        f"  shared chrome: {totals['chrome_controls']} · script-rendered: {totals['script_rendered_controls']}.",
        "",
        "## What this page is, and is not",
        "",
        "The table below is parsed, so it cannot be missing an element that sits in the markup. It is",
        "**not** a rendering: a control can exist and never be shown, and nothing here was captured in",
        "a browser. Controls that only exist in inline-script templates or DOM creation are listed",
        "separately at the end, attributed to the function that renders them — a heuristic, not a call graph.",
        "",
        "## Per-pane counts",
        "",
        "| Pane | Section | Total | Without id | Without any label | Breakdown |",
        "|---|---|---|---|---|---|",
    ]
    for pane in inventory["panes"]:
        breakdown = ", ".join(f"{tag} {count}" for tag, count in pane["counts"].items())
        lines.append(
            f"| {pane['dom_id']} | `{pane['section_id']}` | {pane['control_count']} "
            f"| {pane['controls_without_id']} | {pane['controls_without_label']} | {breakdown} |"
        )
    chrome_counts = ", ".join(f"{tag} {count}" for tag, count in inventory["chrome"]["counts"].items())
    chrome = inventory["chrome"]
    lines.append(
        f"| (chrome) | outside the panes | {chrome['control_count']} "
        f"| {sum(1 for control in chrome['controls'] if not control.get('id'))} "
        f"| {sum(1 for control in chrome['controls'] if not (control.get('id') or control.get('aria-label') or control.get('text') or control.get('placeholder') or control.get('name')))} "
        f"| {chrome_counts} |"
    )

    totals_all = totals["markup_controls"] + totals["script_rendered_controls"]
    without_id = sum(pane["controls_without_id"] for pane in inventory["panes"]) + sum(
        1 for control in chrome["controls"] if not control.get("id")
    )
    script_share = round(100 * totals["script_rendered_controls"] / totals_all) if totals_all else 0
    lines += [
        "",
        "## What the numbers already say",
        "",
        f"* {totals['markup_controls']} controls are in the markup and {totals['script_rendered_controls']} "
        f"more are built by script — a checklist written from the markup alone would miss "
        f"{script_share}% of the interactive surface.",
        f"* The shared chrome carries {totals['chrome_controls']} controls; all six panes together carry "
        f"{totals['pane_controls']}. Most of the clicking happens in the shell, not inside a tab.",
        f"* {without_id} controls have no `id`, so they cannot be reached by selector in a test until one "
        "is added or a stable hook is introduced. That is a constraint on the P5 accessibility and "
        "regression work, not a cosmetic detail.",
    ]
    for pane in inventory["panes"]:
        lines += [
            "",
            f"## Pane `{pane['dom_id']}`",
            "",
            f"Section `{pane['section_id']}`, {pane['control_count']} interactive elements.",
            "",
            "| # | Element | id | Type | Label or text | Handler |",
            "|---|---|---|---|---|---|",
        ]
        for index, control in enumerate(pane["controls"], start=1):
            handler = next((control[attr] for attr in HANDLER_ATTRS if control.get(attr)), "")
            label = control.get("aria-label") or control.get("text") or control.get("placeholder") or ""
            lines.append(
                "| {i} | {tag} | {tid} | {typ} | {label} | {handler} |".format(
                    i=index,
                    tag=_cell(control.get("tag")),
                    tid=_cell(control.get("id")),
                    typ=_cell(control.get("type") or control.get("role")),
                    label=_cell(label),
                    handler=_cell(handler),
                )
            )

    lines += [
        "",
        "## Shared chrome",
        "",
        f"{inventory['chrome']['control_count']} interactive elements outside every pane (tab bar,",
        "top bar and monitor rail). These are reachable from all six tabs, which is why they are not",
        "attributed to one.",
        "",
        "| # | Element | id | Type | Label or text | Handler |",
        "|---|---|---|---|---|---|",
    ]
    for index, control in enumerate(inventory["chrome"]["controls"], start=1):
        handler = next((control[attr] for attr in HANDLER_ATTRS if control.get(attr)), "")
        label = control.get("aria-label") or control.get("text") or control.get("placeholder") or ""
        lines.append(
            "| {i} | {tag} | {tid} | {typ} | {label} | {handler} |".format(
                i=index,
                tag=_cell(control.get("tag")),
                tid=_cell(control.get("id")),
                typ=_cell(control.get("type") or control.get("role")),
                label=_cell(label),
                handler=_cell(handler),
            )
        )

    lines += [
        "",
        "## Controls built by script",
        "",
        f"{totals['script_rendered_controls']} interactive tags appear in inline templates or createElement calls",
        "rather than in the markup. They are attributed to the nearest preceding function",
        "and are **not** in the tables above:",
        "",
        "| Function | Controls rendered |",
        "|---|---|",
    ]
    for function, count in totals["script_rendered_by_function"].items():
        lines.append(f"| `{function}` | {count} |")

    lines += [
        "",
        "## Not verified",
        "",
    ]
    lines += [f"* {item}" for item in inventory["not_verified"]]
    lines.append("")
    return "\n".join(lines)


def _rendered() -> tuple[str, str]:
    inventory = build_inventory()
    return (
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
        render_doc(inventory),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if either artifact has drifted")
    parser.add_argument("--json", action="store_true", help="print the inventory and write nothing")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    try:
        inventory_json, inventory_doc = _rendered()
    except (InventoryError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        sys.stdout.write(inventory_json)
        return 0

    if args.check:
        drifted = []
        for path, expected in ((INVENTORY_PATH, inventory_json), (DOC_PATH, inventory_doc)):
            if not path.is_file():
                print(f"error: {_rel(path)} is missing", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                drifted.append(_rel(path))
        if drifted:
            print(
                "error: the UI control inventory has drifted from the markup: "
                + ", ".join(drifted)
                + "\n       Regenerate it (the change belongs to the story):"
                + "\n           python3 scripts/uiux_control_inventory.py",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            payload = json.loads(inventory_json)
            print(
                "UI control inventory is up to date: "
                f"{payload['totals']['markup_controls']} markup controls across "
                f"{payload['totals']['panes']} panes "
                f"({payload['totals']['chrome_controls']} in the chrome, "
                f"{payload['totals']['script_rendered_controls']} rendered by script)"
            )
        return 0

    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(inventory_json, encoding="utf-8")
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(inventory_doc, encoding="utf-8")
    if not args.quiet:
        payload = json.loads(inventory_json)
        print(
            f"Wrote {_rel(INVENTORY_PATH)} and {_rel(DOC_PATH)}: "
            f"{payload['totals']['markup_controls']} markup controls, "
            f"{payload['totals']['script_rendered_controls']} script-rendered"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
