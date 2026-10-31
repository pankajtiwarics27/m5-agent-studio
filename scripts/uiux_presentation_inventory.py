#!/usr/bin/env python3
"""Inventory the empty, error and recovery presentations and check their shape (UIUX-P1-05).

    python3 scripts/uiux_presentation_inventory.py            # regenerate JSON + docs
    python3 scripts/uiux_presentation_inventory.py --check    # fail if either drifted, or a rule broke
    python3 scripts/uiux_presentation_inventory.py --json     # print the inventory

Before this story the shell had one way to say "that failed": paste `"Error: " + e.message`
into whichever element happened to be nearby. Four places did that, and a fifth - the
harness event stream - said nothing at all: it retried every four seconds in silence, so
a frozen timeline looked exactly like a task that had stopped. None of them said what was
kept or what a reader could do next, which is the difference between a failure and a
recovery.

The component answers that with three kinds sharing one shape, and it is the *arguments*
that carry the meaning:

* **empty** - nothing has happened yet: what will appear, and the action that starts it;
* **error** - something failed: the cause, what is retained, and the actions that exist;
* **recovery** - the state was interrupted: what survived, and how it resumes.

`presentInto()` refuses a spec with no cause, no "what is kept" line, no action, an action
whose handler is not a function, or a kind nobody declared. This script checks the parts a
runtime assertion cannot see across the file: that each kind is styled, that every
container which can be empty or broken is actually wired, that the wired handlers no longer
write raw errors, and that the stylesheet still prints the "Cause:" and "Kept:" labels
rather than trusting every caller to type them.

The honest limit is in the artifact: the rest of the shell still has raw `"Error: "` writers,
and they are listed with their lines rather than quietly fixed in a story about the
component. Converting them is pane work, and P2 to P4 are where the panes get rebuilt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Presentations.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-presentations.md"

KINDS = ("empty", "error", "recovery")

# Containers that can be empty or broken, the pane each belongs to, and the state it
# holds. Each must be wired: a container nobody draws into is a hole a reader falls into.
CONTAINERS = {
    "onboardingState": ("Start", "a preflight that could not be read"),
    "hallStream": ("Council", "no round yet"),
    "debateSynthesisArea": ("Compare", "no comparison yet"),
    "chatState": ("Chat", "a reply that stopped"),
    "harnessStreamState": ("Harness", "an event stream that dropped"),
}

# Handlers this story converted. A raw writer inside one of them means the conversion
# regressed, not that the pane needs more work.
WIRED_HANDLERS = (
    "refreshOnboarding",
    "sendChatRequest",
    "openHarnessEventStream",
    "renderHall",
)


class PresentationError(Exception):
    """The presentations and their rules disagree."""


def _line(html: str, index: int) -> int:
    return html.count("\n", 0, index) + 1


def read_kinds(html: str) -> dict[str, dict[str, bool]]:
    """Which kinds have a rule of their own, and whether the slot labels are printed."""
    return {
        "declared": {
            kind: f'"{kind}"' in html.split("const PRESENTATION_KINDS")[1][:80]
            for kind in KINDS
        },
        "styled": {
            "empty": ".presentation {" in html,
            "error": '.presentation[data-kind="error"]' in html,
            "recovery": '.presentation[data-kind="recovery"]' in html,
        },
        "labels": {
            "cause": 'content: "Cause: "' in html,
            "context": 'content: "Kept: "' in html,
        },
    }


def _object_at(html: str, index: int) -> str:
    """The balanced ``{ ... }`` starting at or after ``index``."""
    start = html.find("{", index)
    if start < 0:
        return ""
    depth = 0
    for offset in range(start, len(html)):
        char = html[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : offset + 1]
    return html[start:]


def read_surfaces(html: str) -> list[dict[str, object]]:
    """Every presentInto() call: which container, which kind, which actions."""
    surfaces: list[dict[str, object]] = []
    for match in re.finditer(r'presentInto\("([\w-]+)",', html):
        block = _object_at(html, match.end())
        kind = re.search(r'\bkind:\s*"(\w+)"', block)
        titles = re.search(r'\btitle:\s*(?:"([^"]*)"|([^,\n]+))', block)
        actions_block = block[block.find("actions:") :] if "actions:" in block else ""
        labels = re.findall(r'\blabel:\s*"([^"]*)"', actions_block)
        surfaces.append(
            {
                "container": match.group(1),
                "pane": CONTAINERS.get(match.group(1), ("?", ""))[0],
                "kind": kind.group(1) if kind else None,
                "title": (
                    titles.group(1)
                    if titles and titles.group(1)
                    else (titles.group(2).strip() if titles else None)
                ),
                "has_cause": bool(re.search(r"cause:\s*\S", block)),
                "has_context": bool(re.search(r"context:\s*\S", block)),
                "actions": labels,
                "line": _line(html, match.start()),
            }
        )
    return surfaces


def read_raw_writers(html: str) -> list[dict[str, object]]:
    """The "Error: " + message writers this story did not convert, with their lines."""
    writers: list[dict[str, object]] = []
    for match in re.finditer(r'(?:textContent|innerHTML)\s*=\s*"Error: "', html):
        writers.append(
            {
                "line": _line(html, match.start()),
                "handler": _enclosing_function(html, match.start()),
            }
        )
    return writers


def _enclosing_function(html: str, index: int) -> str | None:
    names = re.findall(r"function (\w+)\s*\(", html[:index])
    return names[-1] if names else None


def build_inventory() -> dict[str, object]:
    html = FRONTEND.read_text(encoding="utf-8")
    kinds = read_kinds(html)
    surfaces = read_surfaces(html)
    raw_writers = read_raw_writers(html)
    findings: list[dict[str, str]] = []

    def fail(check: str, detail: str) -> None:
        findings.append({"check": check, "detail": detail})

    # 1. Three kinds, each declared, styled and labelled.
    for kind in KINDS:
        if not kinds["declared"][kind]:
            fail("kind-undeclared", f"{kind!r} is not in PRESENTATION_KINDS")
        if not kinds["styled"].get(kind):
            fail(
                "kind-unstyled",
                f"{kind!r} has no rule, so it renders as the base presentation",
            )
    for slot in ("cause", "context"):
        if not kinds["labels"][slot]:
            fail(
                "label-missing",
                f'the stylesheet no longer prints the "{slot.capitalize()}:" label',
            )

    # 2. The builder requires the slots rather than trusting callers.
    builder = html[
        html.find("function presentationProblem") : html.find("function presentInto")
    ]
    for field in ("kind", "title", "cause", "context", "actions"):
        if field not in builder:
            fail("builder-unchecked", f"presentInto() does not validate {field!r}")
    if "window.M5Presentations" not in html:
        fail(
            "builder-unexported",
            "the builder is not exported, so no walk can exercise it",
        )

    # 3. Every container that can be empty or broken is wired.
    wired = {surface["container"] for surface in surfaces}
    for container, (_pane, description) in CONTAINERS.items():
        if container not in html:
            fail(
                "container-missing",
                f"#{container} is not in the markup ({description})",
            )
        elif container not in wired:
            fail(
                "container-unwired",
                f"#{container} can be empty or broken but nothing presents into it",
            )

    # 4. Each call carries the three slots and at least one real action.
    for surface in surfaces:
        where = f"{surface['container']} at line {surface['line']}"
        if not surface["has_cause"]:
            fail("surface-no-cause", f"{where} passes no cause")
        if not surface["has_context"]:
            fail("surface-no-context", f"{where} passes no kept-context line")
        if not surface["actions"]:
            fail("surface-no-actions", f"{where} offers no action")
        if any(not label.strip() for label in surface["actions"]):
            fail("surface-empty-action", f"{where} has an action with no label")
        if surface["kind"] not in KINDS:
            fail("surface-bad-kind", f"{where} uses kind {surface['kind']!r}")

    # 5. The handlers this story converted no longer write raw errors.
    for writer in raw_writers:
        if writer["handler"] in WIRED_HANDLERS:
            fail(
                "raw-writer-in-wired-path",
                f'{writer["handler"]}() still writes a bare "Error: " at line {writer["line"]}',
            )

    # 6. The empty panes are drawn at startup, not only after a failed action.
    startup = html[
        html.find("applyShortcutHints();") : html.find("applyShortcutHints();") + 400
    ]
    for call in ("presentDebateEmpty()", "presentHallEmpty()"):
        if call not in startup:
            fail(
                "startup-missing",
                f"{call} is not called at startup, so the pane opens blank",
            )

    by_kind: dict[str, int] = {
        kind: sum(1 for surface in surfaces if surface["kind"] == kind)
        for kind in KINDS
    }
    return {
        "schema_version": "1.0.0",
        "story": "UIUX-P1-05",
        "statement": (
            "Three presentations share one shape and one builder: empty says what will appear, error says what failed "
            "and what is kept, recovery says what survived and how it resumes. The cause and the kept line are required "
            "arguments, so a presentation cannot degrade into a bare error string."
        ),
        "source": {"frontend": "static/index.html"},
        "kinds": [
            {
                "kind": "empty",
                "means": "nothing has happened yet",
                "tone": "neutral (a hairline in --border-hi)",
                "required": ["title", "cause", "context", "actions"],
            },
            {
                "kind": "error",
                "means": "something failed; role=alert so a screen reader hears it",
                "tone": "danger (--rose)",
                "required": ["title", "cause", "context", "actions"],
            },
            {
                "kind": "recovery",
                "means": "the state was interrupted and resumes from somewhere known",
                "tone": "warn (--amber)",
                "required": ["title", "cause", "context", "actions"],
            },
        ],
        "surfaces": surfaces,
        "containers": [
            {"id": ident, "pane": pane, "meaning": meaning, "wired": ident in wired}
            for ident, (pane, meaning) in CONTAINERS.items()
        ],
        "remaining_raw_writers": raw_writers,
        "totals": {
            "kinds": len(KINDS),
            "surfaces": len(surfaces),
            "containers": len(CONTAINERS),
            "wired_containers": len(wired & set(CONTAINERS)),
            "by_kind": by_kind,
            "remaining_raw_writers": len(raw_writers),
            "findings": len(findings),
        },
        "findings": findings,
        "limits": [
            "the remaining raw error writers are listed, not converted: they are pane work for P2 to P4",
            "the inventory reads the source, so it proves a call passes a cause, not that the cause is useful",
            "the behaviour - a refused request and a disconnected stream - is scripts/presentation_walk.mjs",
        ],
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    lines = [
        "# Empty, error and recovery presentations",
        "",
        "Generated by `python3 scripts/uiux_presentation_inventory.py` — **never hand-edit this page**.",
        "",
        payload["statement"],
        "",
        "## The three kinds",
        "",
        "| Kind | Means | Tone | Required |",
        "|---|---|---|---|",
    ]
    for kind in payload["kinds"]:
        lines.append(
            f"| `{kind['kind']}` | {kind['means']} | {kind['tone']} | {', '.join(kind['required'])} |"
        )
    lines += [
        "",
        'The labels are printed by the stylesheet: `.presentation-cause::before { content: "Cause: " }` and',
        '`.presentation-context::before { content: "Kept: " }`. A caller writes the sentence, not the',
        "punctuation, so a presentation cannot quietly ship without saying why or what survived.",
        "",
        "`presentInto()` refuses a spec that is missing any of them, or that offers an action whose handler",
        "is not a function. The refusal is a thrown error, not a silent fallback: an unusable presentation",
        "is a bug in the caller, and the `error` kind is already how bugs are shown.",
        "",
        "## Where they are used",
        "",
        "| Container | Pane | Kind | Actions | Line |",
        "|---|---|---|---|---|",
    ]
    for surface in payload["surfaces"]:
        actions = ", ".join(f"`{label}`" for label in surface["actions"]) or "—"
        lines.append(
            f"| `#{surface['container']}` | {surface['pane']} | `{surface['kind']}` | {actions} | {surface['line']} |"
        )
    lines += [
        "",
        f"{totals['surfaces']} call sites across {totals['wired_containers']} of {totals['containers']} containers: "
        + ", ".join(f"{count} {kind}" for kind, count in totals["by_kind"].items())
        + ".",
        "",
        "## The paths that were broken before",
        "",
        "- **Start, a refused preflight.** The old catch replaced the checklist with one blocked row, so a failed",
        "  *re-check* threw away the last result that had loaded. The steps now stay and the presentation sits above",
        "  them, saying that nothing was changed by the attempt.",
        "- **Chat, a reply that stopped.** The old handler appended `**Error:** message` to the assistant bubble and",
        "  left an empty bubble behind, so a retry duplicated the message. The placeholder is rolled back, the",
        "  transcript is kept, and Retry re-sends exactly once.",
        "- **Harness, a dropped event stream.** The old code retried every four seconds in silence; a frozen timeline",
        "  and a stopped task looked identical. The recovery presentation names the cause, the number of events still",
        "  shown, the cursor it will resume from, and offers both a reconnect and an authoritative snapshot reload.",
        "- **Council and Compare, nothing yet.** Both panes now open with the empty presentation and the action that",
        "  starts them, instead of a bespoke block in one case and a blank column in the other.",
        "",
        "## What is still raw",
        "",
        f'{totals["remaining_raw_writers"]} `"Error: " + message` writers remain outside the wired handlers. They are listed',
        "here rather than converted, because they belong to panes P2 to P4 rebuild, and a story about the component",
        "should not quietly become a pane rewrite:",
        "",
        "| Handler | Line |",
        "|---|---|",
    ]
    for writer in payload["remaining_raw_writers"]:
        lines.append(f"| `{writer['handler']}()` | {writer['line']} |")
    lines += [
        "",
        "## Checks and limits",
        "",
        "`--check` fails on: a kind declared but not styled, a missing `Cause:`/`Kept:` label, a builder that stops",
        "validating a required field, a container that can be empty or broken but is never presented into, a call",
        "that passes no cause or no kept line or no action, a raw error writer inside a converted handler, and an",
        "empty pane that is not drawn at startup.",
        "",
    ]
    for limit in payload["limits"]:
        lines.append(f"- {limit}.")
    lines += [
        "",
        "The behaviour is measured by `node scripts/presentation_walk.mjs`, which starves the app on purpose: it",
        "aborts the preflight and asserts the error presentation, retries and asserts the checklist returns; aborts",
        "the chat stream and asserts the transcript survives without an empty bubble; creates a real Harness",
        "session, aborts its event stream and asserts the recovery presentation and that reconnecting clears it.",
        "It also checks that the Compare pane opens empty and that the builder refuses an unusable spec. `--screenshots`",
        "writes those four broken states to `docs/media/presentations/`.",
        "",
        "## Regenerating",
        "",
        "1. Edit the component or a call site in `static/index.html`.",
        "2. `python3 scripts/uiux_presentation_inventory.py`.",
        "3. `node scripts/presentation_walk.mjs --screenshots docs/media/presentations`, then regenerate the sign-off.",
        "",
    ]
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the artifacts drifted or a rule broke",
    )
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
            print("error: the presentation inventory has drifted from the markup:")
            for problem in problems:
                print(f"       {problem}")
            print("       Regenerate it (the change belongs to the story):")
            print("           python3 scripts/uiux_presentation_inventory.py")
            return 1
        if payload["findings"]:
            print("error: the presentations and their rules disagree:")
            for finding in payload["findings"]:
                print(f"       {finding['check']}: {finding['detail']}")
            return 1
        print(
            f"presentations OK: {payload['totals']['kinds']} kinds, {payload['totals']['surfaces']} call sites across "
            f"{payload['totals']['wired_containers']} containers, "
            f"{payload['totals']['remaining_raw_writers']} raw writers left, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{payload['totals']['surfaces']} call sites, {payload['totals']['remaining_raw_writers']} raw writers, "
        f"{len(payload['findings'])} findings"
    )
    for finding in payload["findings"]:
        print(f"  {finding['check']}: {finding['detail']}")
    return 1 if payload["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
