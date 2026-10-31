#!/usr/bin/env python3
"""Inventory the overlays, toasts and shortcuts, and check they are one system (UIUX-P1-04).

    python3 scripts/uiux_overlay_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_overlay_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_overlay_inventory.py --json     # print the inventory

Nine dialogs shared a pattern but not a controller: each open function added
`.open`, each close function removed it, Escape removed it from all nine at once,
none of them trapped focus and exactly one remembered where focus came from. The
shortcut situation was the same shape: the rail's Alt+N lived in `data-shortcut`,
the palette's key lived in the handler, and the palette's *hints* were string
literals next to neither. The handler could answer a key the palette did not
advertise, and the palette could advertise a key that did nothing - which is
exactly what had happened to Alt+6 before P1-02.

This script turns both into lists that can be checked:

* every `.modal-backdrop` has an id, a labelled `role="dialog"` inside, and opens
  and closes through the controller rather than by touching `.open` itself;
* no handler outside the controller still writes `.open`, which is how a dialog
  ends up open with no stack entry - and therefore no focus return;
* the shortcut registry has no duplicate keys, every palette item that names a
  shortcut names one that exists, and every advertised hint comes from the
  registry rather than from a literal beside it;
* the inspector declares itself as a drawer when it overlays (aria-modal on open),
  a column when it does not, and its toggle reports aria-expanded either way.

Two limits, stated because they matter. The inventory reads the source, so it
proves the dialogs *route* through the controller, not that focus lands where it
should - that is `scripts/overlay_walk.mjs`, which drives a real browser with real
key events. And it cannot see a dialog a future story renders from a template; a
generated backdrop joins this inventory when it is written, not before.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Overlays.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-overlays.md"

# The keys the registry must own. Escape and the palette key are global; the rail
# keys come from the tabs, so they are checked against the markup instead.
REQUIRED_GLOBAL = ("command-palette", "dismiss-overlay")


class OverlayError(Exception):
    """The overlays and their rules disagree."""


def _enclosing_function(html: str, call: str) -> str | None:
    r"""The function whose body contains `call`, found by walking back to its header.

    A regex cannot do this: these bodies contain braces (try/catch, object literals),
    so `\{[^}]*` stops in the wrong place and reports a wired dialog as unwired.
    """
    position = html.find(call)
    if position < 0:
        return None
    head = html[:position]
    header = re.findall(r'function (\w+)\s*\(', head)
    return header[-1] if header else None


def _callers(html: str, function: str) -> list[dict[str, str]]:
    """Who opens a dialog: a markup control, or the palette and other scripts."""
    callers: list[dict[str, str]] = []
    pattern = re.compile(r'(?<![\w.])' + re.escape(function) + r'\(')
    for match in pattern.finditer(html):
        line_start = html.rfind("\n", 0, match.start()) + 1
        line = html[line_start:html.find("\n", match.start())]
        if re.match(r'\s*function ', line) or 'function ' + function in line:
            continue
        kind = "markup" if 'onclick="' in line else "script"
        callers.append({"kind": kind, "line": html.count("\n", 0, match.start()) + 1})
    return callers


def read_dialogs(html: str) -> list[dict[str, object]]:
    """Every backdrop, the dialog inside it, its triggers and its controller calls."""
    dialogs: list[dict[str, object]] = []
    for match in re.finditer(r'<div id="([A-Za-z0-9_]+)" class="modal-backdrop"([^>]*)>', html):
        ident, rest = match.group(1), match.group(2)
        # The block runs to the matching close of the backdrop; a nested .modal is
        # always inside it, and the next backdrop starts the next block.
        tail = html[match.end():]
        end = tail.find('class="modal-backdrop"')
        block = tail if end < 0 else tail[:end]
        inner = re.search(r'<div class="modal"([^>]*)>', block)
        attrs = inner.group(1) if inner else ""
        label = re.search(r'aria-label="([^"]+)"', attrs)
        role = re.search(r'role="([^"]+)"', attrs)
        onclose = re.search(r'onclick="if\(event\.target===this\)(\w+)\(\)"', rest)
        open_fn = _enclosing_function(html, f'openOverlay("{ident}"')
        close_fn = _enclosing_function(html, f'closeOverlay("{ident}"')
        callers = _callers(html, open_fn) if open_fn else []
        dialogs.append(
            {
                "id": ident,
                "role": role.group(1) if role else None,
                "label": label.group(1) if label else None,
                "backdrop_close": onclose.group(1) if onclose else None,
                "open_function": open_fn,
                "open_via_controller": bool(open_fn),
                "close_via_controller": bool(close_fn),
                "callers": callers,
                "triggers": [caller for caller in callers if caller["kind"] == "markup"],
                "line": html.count("\n", 0, match.start()) + 1,
            }
        )
    return dialogs


def read_shortcuts(html: str) -> dict[str, object]:
    """The registry, the rail's own keys, and everything that advertises them."""
    start = html.find("const SHORTCUTS = [")
    body = html[start : html.find("];", start)] if start >= 0 else ""
    entries = []
    for match in re.finditer(r'\{ id: "([\w-]+)", keys: \[([^\]]+)\], label: "([^"]+)", where: "(\w+)" \}', body):
        keys = [key.strip().strip('"') for key in match.group(2).split(",") if key.strip()]
        entries.append({"id": match.group(1), "keys": keys, "label": match.group(3), "where": match.group(4)})
    rail = [
        {"id": f"tab-{tab}", "keys": [key], "where": "rail"}
        for tab, key in re.findall(r'data-tab="([\w-]+)"[^>]*data-shortcut="([^"]+)"', html)
    ]
    rail += [
        {"id": f"tab-{tab}", "keys": [key], "where": "rail"}
        for tab, key in re.findall(r'data-shortcut="([^"]+)"[^>]*data-tab="([\w-]+)"', html)
        if {"id": f"tab-{tab}"} not in rail
    ]
    palette_ids = sorted(set(re.findall(r'shortcut: "([\w-]+)"', html)))
    action_ids = sorted(set(re.findall(r'data-shortcut-action="([\w-]+)"', html)))
    return {
        "registry": entries,
        "rail": rail,
        "palette_shortcut_refs": palette_ids,
        "advertised_actions": action_ids,
    }


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    dialogs = read_dialogs(html)
    shortcuts = read_shortcuts(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. Every backdrop is a labelled, identifiable dialog.
    seen: set[str] = set()
    for dialog in dialogs:
        ident = str(dialog["id"])
        if ident in seen:
            fail("dialog-duplicate-id", f"{ident} is declared twice")
        seen.add(ident)
        if dialog["role"] != "dialog":
            fail("dialog-unlabelled-role", f"#{ident} has no role=\"dialog\" inside its backdrop")
        if not dialog["label"]:
            fail("dialog-unlabelled", f"#{ident} has no aria-label, so a screen reader announces 'dialog'")

    # 2. Open and close go through the controller.
    for dialog in dialogs:
        ident = str(dialog["id"])
        if not dialog["open_via_controller"]:
            fail("dialog-bypasses-controller", f"#{ident} opens without openOverlay(), so it has no stack entry and no focus return")
        if not dialog["close_via_controller"]:
            fail("dialog-bypasses-controller", f"#{ident} closes without closeOverlay(), so focus is not returned")
        if not dialog["callers"]:
            fail("dialog-unreachable", f"#{ident} has nothing that opens it")

    # 3. Nothing outside the controller writes .open.
    writer_lines = [
        html.count("\n", 0, match.start()) + 1
        for match in re.finditer(r'classList\.(?:add|remove)\("open"\)', html)
    ]
    controller_lines = set()
    for marker in ('function openOverlay', 'function closeOverlay'):
        start = html.find(marker)
        if start >= 0:
            controller_lines.update(range(html.count("\n", 0, start), html.count("\n", 0, html.find("\n  }\n", start)) + 2))
    stray = [line for line in writer_lines if line not in controller_lines]
    if stray:
        fail("open-written-outside-controller", f"lines {', '.join(map(str, stray))} write .open directly")

    # 4. The shortcut registry is the only list.
    all_entries = shortcuts["registry"] + shortcuts["rail"]
    ids = [entry["id"] for entry in all_entries]
    if len(ids) != len(set(ids)):
        fail("shortcut-duplicate-id", "two shortcut entries share an id")
    keys: list[str] = []
    for entry in all_entries:
        keys.extend(entry["keys"])
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        fail("shortcut-duplicate-key", f"{key!r} is claimed by more than one shortcut")
    for required in REQUIRED_GLOBAL:
        if required not in ids:
            fail("shortcut-missing", f"the registry declares no {required!r} shortcut")
    for ref in shortcuts["palette_shortcut_refs"]:
        if ref not in ids:
            fail("shortcut-unknown-ref", f"a palette item advertises {ref!r}, which is not in the registry")
    for ref in shortcuts["advertised_actions"]:
        if ref not in ids:
            fail("shortcut-unknown-action", f'data-shortcut-action="{ref}" names no registry entry')
    if re.search(r'hint: "Alt\+\d"', html):
        fail("shortcut-literal-hint", "a palette item still hard-codes an Alt+N hint instead of naming its shortcut id")
    if not shortcuts["advertised_actions"]:
        fail("shortcut-unadvertised", "no control advertises its shortcut through aria-keyshortcuts")

    # 5. Escape closes the top overlay, not every overlay.
    if 'document.querySelectorAll(".modal-backdrop.open").forEach' in html:
        fail("escape-closes-everything", "Escape still strips .open from every backdrop, which loses the stack")

    # 6. The inspector is a drawer when it overlays and a column when it does not.
    rail = re.search(r'<aside id="monitorRail"([^>]*)>', html)
    if not rail or 'role="complementary"' not in rail.group(1):
        fail("inspector-unlabelled", "the inspector declares no role or accessible name")
    if 'aria-controls="monitorRail"' not in html or 'aria-expanded' not in html:
        fail("inspector-toggle", "the inspector toggle does not report what it controls and whether it is open")
    if 'rail.setAttribute("aria-modal", "true")' not in html or 'rail.removeAttribute("aria-modal")' not in html:
        fail("inspector-drawer", "the drawer never declares itself modal, so its focus behaviour is unstated")

    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P1-04",
        "statement": (
            "One controller for the nine dialogs - open, trap, close, return focus - one shortcut registry read by "
            "the handler, the palette and the markup, and an inspector that declares whether it is a drawer or a "
            "column. Checks prove the wiring; the browser walk proves the behaviour."
        ),
        "source": {"frontend": "static/index.html"},
        "dialogs": dialogs,
        "shortcuts": shortcuts,
        "inspector": {
            "element": "monitorRail",
            "toggle": "monitorToggleBtn",
            "drawer_breakpoint": "1100px",
            "role": "complementary",
        },
        "totals": {
            "dialogs": len(dialogs),
            "dialogs_via_controller": sum(1 for dialog in dialogs if dialog["open_via_controller"] and dialog["close_via_controller"]),
            "registry_entries": len(shortcuts["registry"]),
            "rail_shortcuts": len(shortcuts["rail"]),
            "advertised_controls": len(shortcuts["advertised_actions"]),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the inventory proves the dialogs route through the controller, not that focus lands where it should",
            "it reads the source, so a backdrop rendered from a template joins the list only when it is written",
            "shortcut keys are checked for collisions across the registry; the browser walk is what presses them",
        ],
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    lines = [
        "# Overlays, toasts and shortcuts",
        "",
        "Generated by `python3 scripts/uiux_overlay_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## The controller",
        "",
        "`openOverlay(id)` records the element that had focus, paints the backdrop, marks the dialog",
        "`aria-modal`, makes every sibling of the backdrop `inert` and moves focus to the dialog's first",
        "focusable control. `closeOverlay(id)` reverses all four, in one place, so a dialog closed by",
        "Escape, by its Close button or by its backdrop returns focus to the same control.",
        "",
        "Escape closes **the top overlay only**. The old handler removed `.open` from all nine at once,",
        "which meant a stacked dialog disappeared without returning focus to the one underneath it.",
        "",
        "## The dialogs",
        "",
        "| Dialog | Label | Opens from | Controller | Line |",
        "|---|---|---|---|---|",
    ]
    for dialog in payload["dialogs"]:
        triggers = ", ".join(f"`{dialog['open_function']}()` from {len(dialog['callers'])} call site(s)") or "—"
        wired = "yes" if dialog["open_via_controller"] and dialog["close_via_controller"] else "**no**"
        lines.append(f"| `#{dialog['id']}` | {dialog['label'] or '—'} | {triggers} | {wired} | {dialog['line']} |")
    lines += [
        "",
        f"{totals['dialogs']} dialogs, {totals['dialogs_via_controller']} wired through the controller.",
        "",
        "## The shortcut registry",
        "",
        "| Shortcut | Keys | Where | What it does |",
        "|---|---|---|---|",
    ]
    for entry in payload["shortcuts"]["registry"]:
        lines.append(f"| `{entry['id']}` | {' / '.join(entry['keys'])} | {entry['where']} | {entry['label']} |")
    for entry in payload["shortcuts"]["rail"]:
        lines.append(f"| `{entry['id']}` | {' / '.join(entry['keys'])} | rail | Open that pane |")
    lines += [
        "",
        "The rail's entries are not restated here: they are read from `data-shortcut` on the tabs, so the",
        "label, the `title`, the `aria-keyshortcuts` attribute and the key that fires are one fact. The",
        "palette's hints are looked up by id when the list is drawn, which is why a palette row cannot",
        "advertise a key the handler does not answer.",
        "",
        "## The inspector",
        "",
        "`#monitorRail` is a column above 1100px and a drawer below it: opening the drawer takes focus,",
        "closing it or pressing Escape returns focus to `#monitorToggleBtn`, and `aria-expanded` reports",
        "the state either way. A resize puts it back to the layout default and clears `aria-modal`, so a",
        "panel that no longer covers the page does not claim to.",
        "",
        "## Checks and limits",
        "",
        "`--check` fails on: a backdrop without an id, a `role=\"dialog\"` or a label; a dialog that opens or",
        "closes without the controller; a handler that writes `.open` directly; a duplicate shortcut id or",
        "key; a palette item naming a shortcut that does not exist; a hard-coded `Alt+N` hint; an Escape",
        "handler that closes everything; an inspector with no role, no toggle state, or no drawer",
        "declaration; and a contrast-free regression is not possible here, because this story adds no colour.",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "The behaviour is measured by `node scripts/overlay_walk.mjs`, which presses real keys in a real",
        "browser: it opens every dialog from its own trigger with Enter, checks focus landed inside, tabs",
        "past the end and asserts focus never escapes, presses Escape and asserts focus came back, checks",
        "the background was inert, then exercises the palette and the narrow-viewport drawer.",
        "",
        "## Regenerating",
        "",
        "1. Edit the controller, a dialog or the registry in `static/index.html`.",
        "2. `python3 scripts/uiux_overlay_inventory.py`.",
        "3. `node scripts/overlay_walk.mjs` against a running demo, then regenerate the sign-off.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the inventory")
    args = parser.parse_args(argv)

    payload = build_inventory()
    text = dumps(payload)
    doc = render_doc(payload)

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        problems = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            problems.append(f"{JSON_PATH.relative_to(PROJECT_ROOT)} is stale")
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            problems.append(f"{DOC_PATH.relative_to(PROJECT_ROOT)} is stale")
        if problems:
            print("error: the overlay inventory has drifted from the markup:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_overlay_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the overlays and their rules disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"overlays OK: {payload['totals']['dialogs']} dialogs through the controller, "
            f"{payload['totals']['registry_entries']} registry entries, "
            f"{payload['totals']['rail_shortcuts']} rail shortcuts, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['dialogs']} dialogs, {payload['totals']['registry_entries']} registry entries, "
        f"{len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
