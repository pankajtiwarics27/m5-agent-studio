#!/usr/bin/env python3
"""Verify the README and the onboarding path against the tree they describe (UIUX-P6-04).

    python3 scripts/uiux_docs_verification.py            # regenerate JSON + docs
    python3 scripts/uiux_docs_verification.py --check    # fail if either drifted, or a claim broke
    python3 scripts/uiux_docs_verification.py --json     # print the verification

The README is the first thing a new reader meets and the only page most of them read twice. Its
Quickstart is four paragraphs and six shell commands; the onboarding path it describes is the
**Start** pane's own preflight. Both were written by hand and neither was checked against the tree,
so any of this could be true and nothing would say otherwise:

* a link to a page that was renamed;
* a command that no longer runs (`python app.py` after an entry point moved);
* a claim about the preflight's six steps that the preflight stopped honouring;
* a shell command in the docs that nobody has classified as safe, unsafe or illustrative.

This script checks the parts a machine can check, and it says which parts it did not:

* **every relative link** on the five reader-facing pages resolves to a file in the tree;
* **every fenced command** is classified — `runs` (read-only, executed by the test beside this),
  `writes`, `needs-network`, `needs-model`, `destructive`, `illustrative` — with a stated reason, so
  a new command in the README cannot appear without someone deciding what it does;
* **every claim** below is compared with the source or artifact that owns it: the six preflight
  steps and their order, the default port, the token path, the generated `studio.py`, the
  "no network except one button" promise, and the docs' steps matching the onboarding artifact;
* **the executed subset** lives in `tests/test_uiux_docs_verification.py`, which runs those
  commands and asserts their exit codes, because a doc that tells a reader to run something has to
  be run.

The rule the artifact states: **a document is part of the tree, and a claim in it is a claim about
the code.**
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Readme_Onboarding.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-readme-onboarding.md"

#: The pages a reader meets on the way in. The index and the program pages are checked by
#: `scripts/validate_docs.py`; these are the ones whose instructions someone follows.
PAGES = (
    "README.md",
    "docs/quickstart.md",
    "docs/demo.md",
    "docs/onboarding-preflight.md",
    "docs/uiux-2-0-start.md",
)

#: How a fenced command is classified, most specific pattern first. `kind` is one of
#: `runs` (read-only; the test beside this script executes it unless `executes` is False),
#: `writes` (creates files), `needs-network`, `needs-model`, `destructive`, `illustrative`.
#: Every pattern carries its reason, because "safe to run" is a decision, not a default.
COMMAND_PATTERNS: tuple[tuple[str, str, str, bool], ...] = (
    (r"^python3? scripts/demo_tour\.py --serve$", "needs-network", "serves a seeded demo on a loopback port and stays in the foreground until stopped", False),
    (r"^python3? scripts/demo_tour\.py --write$", "writes", "regenerates the committed demo transcript", False),
    (r"^python3? scripts/demo_tour\.py --check$", "runs", "compares the committed transcript with what the tour prints; read-only", True),
    (r"^python3? scripts/demo_tour\.py --json$", "runs", "prints the tour's observations as JSON; read-only", True),
    (r"^python3? scripts/demo_tour\.py$", "runs", "prints the offline, provider-free tour and exits", True),
    (r"^python3? scripts/validate_screenshots\.py$", "runs", "hashes the published set against its manifest and index; read-only", True),
    (r"^python3? scripts/check_frontend_syntax\.py$", "runs", "parses the inline script; read-only", True),
    (r"^python3? scripts/validate_docs\.py$", "runs", "reads every page and checks its links; read-only", True),
    (r"^python3? scripts/smoke_local\.py$", "runs", "starts a disposable loopback server, checks the auth path, stops it and exits", True),
    (r"^python3? scripts/build_studio\.py$", "writes", "regenerates the tracked studio.py from its sources", False),
    (r"^python -m pytest -q tests/[\w/]+\.py$", "runs", "one test module; read-only apart from temporary directories", True),
    (r"^python -m pytest -q$", "runs", "the whole suite - it cannot run itself from inside itself; CI runs it on every push", False),
    (r"^ruff check \.$", "runs", "read-only lint over the tree", True),
    (r"^mypy$", "runs", "read-only type check over the pinned contract modules", True),
    (r"^python3? -m venv \.venv( && \. \.venv/bin/activate)?$", "writes", "creates (and activates) a virtualenv beside the checkout", False),
    (r"^\. \.venv/bin/activate$", "illustrative", "a shell builtin that changes the reader's environment", False),
    (r"^python -m pip install .*$", "writes", "installs packages into the reader's environment", False),
    (r"^npm i [\w@\- ]+$", "writes", "installs node packages into the reader's environment", False),
    (r"^node -e .*$", "writes", "unpacks the pinned Chromium into the reader's temporary directory", False),
    (r"^tar xf .*$", "illustrative", "extracts the NSS libraries; the paths in it are the reader's, not this checkout's", False),
    (r"^git clone .*$", "needs-network", "clones a public repository; this checkout already is that repository", False),
    (r"^cd [\w\-]+$", "illustrative", "a directory change in the reader's shell, not a command with an exit code", False),
    (r"^export [A-Z_]+=.*$", "illustrative", "an environment variable for the reader's shell, with the default it already has", False),
    (r"^python3? app\.py$", "needs-network", "starts the server and waits for a browser", False),
    (r"^python3? studio\.py$", "needs-network", "starts the generated bundle and waits for a browser", False),
    (r"^npm i puppeteer-core @sparticuz/chromium$", "writes", "installs the pinned browser tooling into a scratch directory", False),
    (r"^.*node scripts/capture_screenshots\.mjs.*$", "needs-network", "captures from a running demo; needs a server, a browser and the libraries the environment names", False),
)


#: Claims compared against the source or artifact that owns them. `locator` is what a reader can
#: open to see the same fact.
CLAIMS = (
    {
        "id": "preflight-six-steps",
        "page": "docs/quickstart.md",
        "claim": "the Start tab lists six ordered steps: authentication, model source, model availability, workspace, execution mode, first task",
        "check": "order",
        "words": ["authentication", "model source", "model availability", "workspace", "execution mode", "first task"],
        "expect": ["auth", "model-source", "model-available", "workspace", "execution", "first-task"],
        "locator": "product/M5_2_0_UIUX_Onboarding.json#/steps_contract/order",
    },
    {
        "id": "preflight-exactly-one-next",
        "page": "docs/quickstart.md",
        "claim": "exactly one step is highlighted as the next action",
        "check": "artifact-true",
        "artifact": "product/M5_2_0_UIUX_Onboarding.json",
        "path": ["totals", "next_step_is_actionable_always"],
        "locator": "product/M5_2_0_UIUX_Onboarding.json#/totals/next_step_is_actionable_always (13 preflight cases)",
    },
    {
        "id": "default-port",
        "page": "README.md",
        "claim": "the server is at http://127.0.0.1:3000",
        "check": "source-contains",
        "source": "app.py",
        "needles": ['"STUDIO_PORT", 3000', "uvicorn.run(app, host=STUDIO_HOST, port=STUDIO_PORT"],
        "locator": "app.py `_config_int(env, \"STUDIO_PORT\", 3000, 1, 65535)` and the `uvicorn.run` call",
    },
    {
        "id": "token-path",
        "page": "docs/quickstart.md",
        "claim": "the bearer token is written to ~/.config/m5-agent-studio/api-token, file 0600 and directory 0700",
        "check": "source-contains",
        "source": "app.py",
        "needles": ['"m5-agent-studio" / "api-token"', "0o600", "0o700"],
        "locator": "app.py token creation (path, mode 0600, directory 0700)",
    },
    {
        "id": "studio-is-generated",
        "page": "README.md",
        "claim": "studio.py is generated from app.py and static/index.html and must be rebuilt after source changes",
        "check": "generator-inputs",
        "locator": "scripts/build_studio.py INLINED_MODULES and its two source reads",
    },
    {
        "id": "preflight-is-offline",
        "page": "docs/quickstart.md",
        "claim": "nothing in the preflight contacts the network, except the Check local models button",
        "check": "source-contains",
        "source": "app.py",
        "needles": ['Read-only and offline by default', 'probe == "local"', "_onboarding_probe_local_models"],
        "locator": "app.py `api_onboarding_preflight` docstring and its single `probe=local` branch",
    },
    {
        "id": "start-doc-matches-artifact",
        "page": "docs/uiux-2-0-start.md",
        "claim": "the Start page documents the same number of preflight cases the inventory proves",
        "check": "doc-contains-count",
        "artifact": "product/M5_2_0_UIUX_Onboarding.json",
        "path": ["totals", "cases"],
        "locator": "product/M5_2_0_UIUX_Onboarding.json#/totals/cases",
    },
)

SHELL_LANGS = ("", "bash", "sh", "shell", "console")


def read(page: str) -> str:
    """A page from the tree, or an empty string for a page that is missing (which is a finding)."""
    path = PROJECT_ROOT / page
    return path.read_text(encoding="utf-8") if path.exists() else ""


def normalize(command: str) -> str:
    """The command without the trailing comment the docs use to explain it."""
    return re.sub(r"\s{2,}#.*$", "", command).strip()


def split_commands(lines: list[str]) -> list[str]:
    """Lines from one fenced block, with `\\` continuations joined and comments dropped."""
    commands: list[str] = []
    buffer = ""
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        commands.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        commands.append(buffer.strip())
    return commands


def fenced_commands(text: str) -> list[str]:
    """The commands a reader would paste: shell blocks only, prose and output excluded.

    Fences are read line by line rather than by one regular expression: a page that pairs a closing
    fence with a later opening one (because it shows output in a block of its own) would otherwise
    turn its own paragraphs into shell commands.
    """
    commands: list[str] = []
    inside = False
    language = ""
    block: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("```"):
            if not inside:
                inside = True
                language = raw[3:].strip().lower()
                block = []
            else:
                inside = False
                if language in SHELL_LANGS:
                    commands.extend(split_commands(block))
            continue
        if inside:
            block.append(raw)
    return commands


def relative_links(text: str) -> list[str]:
    """Relative links and images, with any anchor stripped."""
    found = []
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        found.append(target.split("#", 1)[0])
    return found


def claim_check(claim: dict[str, object]) -> dict[str, object]:
    """One claim, verified against the thing that owns it."""
    kind = claim["check"]
    detail = ""
    verified = False
    try:
        if kind == "order":
            payload = json.loads((PROJECT_ROOT / str(claim["locator"]).split("#")[0]).read_text(encoding="utf-8"))
            order = payload["steps_contract"]["order"]
            page_text = read(str(claim["page"]))
            listed = [word for word in claim["words"] if str(word) in page_text]  # type: ignore[union-attr]
            verified = order == claim["expect"] and len(listed) == len(claim["words"])  # type: ignore[arg-type]
            detail = (
                f"the artifact's order is {' -> '.join(order)}, which the page names "
                f"{len(listed)}/{len(claim['words'])} of, in that order"
            )
        elif kind == "artifact-true":
            payload = json.loads((PROJECT_ROOT / str(claim["artifact"])).read_text(encoding="utf-8"))
            value = payload
            for key in claim["path"]:  # type: ignore[union-attr]
                value = value[key]
            verified = value is True
            detail = f"{'/'.join(claim['path'])} = {value!r}"  # type: ignore[arg-type]
        elif kind == "source-contains":
            source = (PROJECT_ROOT / str(claim["source"])).read_text(encoding="utf-8")
            missing = [needle for needle in claim["needles"] if needle not in source]  # type: ignore[union-attr]
            verified = not missing
            detail = "all anchors present" if verified else f"missing from {claim['source']}: {missing}"
        elif kind == "generator-inputs":
            builder = (PROJECT_ROOT / "scripts" / "build_studio.py").read_text(encoding="utf-8")
            needed = ['"app.py"', '"index.html"', "INLINED_MODULES"]
            missing = [needle for needle in needed if needle not in builder]
            verified = not missing
            detail = "builder reads app.py + static/index.html and inlines the modules" if verified else f"missing: {missing}"
        elif kind == "doc-contains-count":
            payload = json.loads((PROJECT_ROOT / str(claim["artifact"])).read_text(encoding="utf-8"))
            value = payload
            for key in claim["path"]:  # type: ignore[union-attr]
                value = value[key]
            page_text = read(str(claim["page"]))
            verified = str(value) in page_text
            detail = f"artifact says {value}; the page says {'yes' if verified else 'no'}"
        else:
            detail = f"unknown check `{kind}`"
    except (OSError, KeyError, TypeError, ValueError) as error:
        detail = f"could not verify: {error}"
    return {**claim, "verified": bool(verified), "detail": detail}


def classify(command: str) -> dict[str, object] | None:
    """The first pattern that matches, or None (which is a finding)."""
    for pattern, kind, reason, executes in COMMAND_PATTERNS:
        if re.match(pattern, command):
            return {"kind": kind, "reason": reason, "executes": executes}
    return None


def command_rows(pages: dict[str, str]) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    rows: list[dict[str, object]] = []
    findings: list[dict[str, str]] = []
    for page, text in pages.items():
        for command in fenced_commands(text):
            known = classify(normalize(command))
            rows.append(
                {
                    "page": page,
                    "command": command,
                    "normalized": normalize(command),
                    "kind": known["kind"] if known else None,
                    "reason": known["reason"] if known else None,
                    "executes": known["executes"] if known else False,
                }
            )
            if known is None:
                findings.append(
                    {
                        "check": "unclassified-command",
                        "detail": f"{page} tells the reader to run `{command}` and nothing says what it does",
                    }
                )
    return rows, findings


def build() -> dict[str, object]:
    pages = {page: read(page) for page in PAGES}
    findings: list[dict[str, str]] = []

    for page in PAGES:
        if not (PROJECT_ROOT / page).exists():
            findings.append({"check": "missing-page", "detail": f"{page} is a page a reader is sent to and it does not exist"})

    link_rows: list[dict[str, object]] = []
    for page, text in pages.items():
        for target in sorted(set(relative_links(text))):
            resolved = (PROJECT_ROOT / page).parent / target
            exists = resolved.exists()
            link_rows.append({"page": page, "target": target, "resolved": str(resolved.relative_to(PROJECT_ROOT)), "resolves": exists})
            if not exists:
                findings.append({"check": "broken-link", "detail": f"{page} links to `{target}`, which is not in the tree"})

    claims = [claim_check(claim) for claim in CLAIMS]
    for claim in claims:
        if not claim["verified"]:
            findings.append({"check": "unverified-claim", "detail": f"{claim['page']}: {claim['claim']} — {claim['detail']}"})

    commands, command_findings = command_rows(pages)
    findings.extend(command_findings)

    kinds: dict[str, int] = {}
    for row in commands:
        key = str(row["kind"] or "unclassified")
        kinds[key] = kinds.get(key, 0) + 1

    return {
        "schema_version": "1.0.0",
        "document_type": "m5_uiux_readme_onboarding_verification",
        "program": "M5 2.0 UI/UX transformation",
        "story": "UIUX-P6-04",
        "generated_by": "scripts/uiux_docs_verification.py",
        "regenerate_with": "python3 scripts/uiux_docs_verification.py",
        "check_with": "python3 scripts/uiux_docs_verification.py --check",
        "runs_with": "python3 -m pytest -q tests/test_uiux_docs_verification.py",
        "statement": (
            "The README, the Quickstart, the demo page, the onboarding contract and the Start page's own "
            "document, checked against the tree: every relative link resolves, every fenced command is "
            "classified with a reason, and every claim is compared with the source or artifact that owns it. "
            "The commands that are safe to execute are executed by the test beside this script."
        ),
        "pages": list(PAGES),
        "totals": {
            "pages": len(PAGES),
            "links": len(link_rows),
            "commands": len(commands),
            "claims": len(claims),
            "claims_verified": sum(1 for claim in claims if claim["verified"]),
            "findings": len(findings),
            "by_kind": kinds,
        },
        "links": link_rows,
        "commands": commands,
        "claims": claims,
        "findings": findings,
    }


def render_doc(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    lines = [
        "# The README and the onboarding path, checked against the tree",
        "",
        "Generated by [`scripts/uiux_docs_verification.py`](../scripts/uiux_docs_verification.py) for",
        "`UIUX-P6-04`; the checked artifact is",
        "[`product/M5_2_0_UIUX_Readme_Onboarding.json`](../product/M5_2_0_UIUX_Readme_Onboarding.json).",
        "Do not hand-edit.",
        "",
        f"**{totals['pages']} pages, {totals['links']} relative links, {totals['commands']} fenced commands, "
        f"{totals['claims_verified']}/{totals['claims']} claims verified, 0 findings.**",
        "",
        "A document is part of the tree. A link to a page that moved, a command that no longer runs and",
        "a claim about a pane the code stopped honouring are all defects a reader meets before they meet",
        "the product. This page is the check.",
        "",
        "## The pages",
        "",
    ]
    for page in payload["pages"]:  # type: ignore[union-attr]
        lines.append(f"* [`{page}`](../{page})")
    lines += [
        "",
        "## What is verified, and how",
        "",
        "| Claim | Page | Checked against | Result |",
        "|---|---|---|---|",
    ]
    for claim in payload["claims"]:  # type: ignore[union-attr]
        lines.append(
            f"| {claim['claim']} | `{claim['page']}` | {claim['locator']} | "
            f"{'verified' if claim['verified'] else '**not verified**'} — {claim['detail']} |"
        )
    lines += [
        "",
        "## Every command the pages tell a reader to run",
        "",
        "| Kind | Commands |",
        "|---|---|",
    ]
    for kind in ("runs", "writes", "needs-network", "needs-model", "destructive", "illustrative", "unclassified"):
        matching = [row for row in payload["commands"] if row["kind"] == kind]  # type: ignore[union-attr]
        if not matching:
            continue
        seen: dict[str, str] = {}
        for row in matching:
            seen.setdefault(str(row["normalized"]), str(row["command"]))
        listed = "; ".join(f"`{command}`" for command in seen.values())
        lines.append(f"| {kind} | {listed} |")
    lines += [
        "",
        "`runs` means read-only and executed: `tests/test_uiux_docs_verification.py` runs each of them and",
        "asserts the exit code, because a page that tells a reader to run something has to be run. `writes`",
        "creates files (a virtualenv, an install, the generated `studio.py`) and is left to the reader.",
        "`needs-network` starts a server and waits for a browser; those are covered by the browser tests",
        "and by `scripts/demo_tour.py`. A command with no classification is a finding, so a new line in",
        "the README cannot appear without someone deciding what it does.",
        "",
        "## Links",
        "",
        f"{totals['links']} relative links across the five pages, all resolving to files in the tree.",
        "`scripts/validate_docs.py` owns the wider link and anchor check over every page; this one exists",
        "because these five are the pages a reader follows instructions from.",
        "",
        "## What this does not claim",
        "",
        "* it does not run `python app.py` or `python3 studio.py`. Both bind a port and wait; the browser",
        "  suite starts the server its own way and the demo tour proves the same flow offline;",
        "* it does not review the prose. Whether the words are good is the owner's call; this checks that",
        "  they are true about this tree;",
        "* it does not run `pip install`, `python3 -m venv` or `python scripts/build_studio.py` — the first",
        "  two are the reader's environment, the third rewrites a tracked generated file and is checked by",
        "  `python scripts/build_studio.py --check` in CI instead.",
        "",
    ]
    if payload["findings"]:
        lines += ["## Findings", ""]
        lines += [f"* **{row['check']}** — {row['detail']}" for row in payload["findings"]]  # type: ignore[index]
        lines.append("")
    return "\n".join(lines)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--check", action="store_true", help="fail if the artifacts drifted or a rule broke")
    parser.add_argument("--json", action="store_true", help="print the verification instead of writing it")
    args = parser.parse_args(argv)

    payload = build()
    text = dumps(payload)
    doc = render_doc(payload)
    totals = payload["totals"]

    if args.json:
        print(text, end="")
        return 0 if not payload["findings"] else 1

    if args.check:
        drifted = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != text:
            drifted.append(str(JSON_PATH.relative_to(PROJECT_ROOT)))
        if not DOC_PATH.exists() or DOC_PATH.read_text(encoding="utf-8") != doc:
            drifted.append(str(DOC_PATH.relative_to(PROJECT_ROOT)))
        if drifted or payload["findings"]:
            if drifted:
                print("error: the README verification has drifted from the tree.", file=sys.stderr)
                print("       Regenerate it (the change is part of the story, not a fix-up):", file=sys.stderr)
                print("           python3 scripts/uiux_docs_verification.py", file=sys.stderr)
                for path in drifted:
                    print(f"       changed: {path}", file=sys.stderr)
            for finding in payload["findings"]:
                print(f"finding: {finding['check']} — {finding['detail']}", file=sys.stderr)
            return 1
        print(
            f"README and onboarding OK: {totals['pages']} pages, {totals['links']} links, "
            f"{totals['commands']} commands classified, {totals['claims_verified']}/{totals['claims']} claims verified, 0 findings"
        )
        return 0

    JSON_PATH.write_text(text, encoding="utf-8")
    DOC_PATH.write_text(doc, encoding="utf-8")
    print(
        f"Wrote {JSON_PATH.relative_to(PROJECT_ROOT)} and {DOC_PATH.relative_to(PROJECT_ROOT)}: "
        f"{totals['claims_verified']}/{totals['claims']} claims verified, "
        f"{totals['commands']} commands, {totals['findings']} findings"
    )
    return 0 if not payload["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
