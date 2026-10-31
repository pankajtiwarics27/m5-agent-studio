#!/usr/bin/env python3
"""Pin the UI/UX program's source surface to a hashed manifest (UIUX-P0-01).

    python3 scripts/uiux_source_manifest.py            # regenerate the manifest
    python3 scripts/uiux_source_manifest.py --check    # fail if it has drifted
    python3 scripts/uiux_source_manifest.py --json     # print it, write nothing

The UI/UX roadmap (`product/M5_2_0_UIUX_Autonomous_Roadmap.json`) was authored
from GitHub web extracts, so every statement it makes about the current UI was a
hypothesis. This script replaces that with something checkable: the exact files
the program is allowed to touch, their SHA-256 digests, and the two inventories
every later story depends on — the six mode tabs as the browser declares them,
and the route table the server actually mounts.

What the manifest is for:

* **A pin that cannot silently rot.** A story that changes `static/index.html`
  must regenerate the manifest; `--check` fails in CI otherwise, and the review
  diff then shows exactly which surface a story moved.
* **The tab-label trap, removed.** The roadmap's labels are not the DOM ids
  ("Council" is `data-tab="hall"`, "Pipeline" is `data-tab="council"`). The
  manifest records both, parsed from the markup, so a session never has to guess.
* **A route inventory, before anything is mapped.** UIUX-P0-03 maps handlers to
  routes; this records the routes as they exist today without claiming a mapping
  it has not verified.

The generator is deterministic and pure: it reads files under the repository
root and writes JSON. It deliberately does not stamp a wall-clock time, so
`--check` means "the surface changed", never "the file is stale by the clock".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Source_Manifest.json"
LEDGER_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"

SCHEMA_VERSION = "1.0.0"

#: Files the UI/UX program treats as its source surface, with the role each one
#: plays. Paths are relative to the repository root and must exist; a missing
#: file is an error, not an omission, because a manifest with a hole pins nothing.
TRACKED_FILES: tuple[tuple[str, str], ...] = (
    ("static/index.html", "the entire browser UI: markup, styles and script in one file"),
    ("app.py", "the FastAPI server the UI talks to; source of the route table"),
    ("studio.py", "generated standalone bundle; must be rebuilt, never hand-edited"),
    ("design-system/m5-agentic-studio/MASTER.md", "the design tokens and component contract"),
    ("docs/media/screenshots/manifest.json", "the screenshot set every visual claim is checked against"),
    ("docs/ui-audit.md", "the current-state UI audit the P1 stories rewrite"),
    ("scripts/capture_screenshots.mjs", "the only accepted way to capture visual evidence"),
    ("scripts/validate_screenshots.py", "validates capture output against the manifest"),
    ("scripts/build_studio.py", "regenerates studio.py from app.py and the inlined modules"),
    ("scripts/check_frontend_syntax.py", "parses the inline script block for syntax errors"),
)

BUTTON_RE = re.compile(
    r'<button\b(?P<attrs>[^>]*\bdata-tab="(?P<dom_id>[^"]+)"[^>]*)>(?P<body>.*?)</button>',
    re.DOTALL,
)
TITLE_RE = re.compile(r'\btitle="([^"]*)"')
ONCLICK_RE = re.compile(r"switchTab\('([^']+)'\)")
LABEL_RE = re.compile(r"<span>([^<]+)</span>")
SHORTCUT_RE = re.compile(r"\(Alt\+(\d)\)")
ROUTE_RE = re.compile(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
API_LITERAL_RE = re.compile(r"""["'`](/api/[^"'`\s]*)["'`]""")
TEMPLATE_RE = re.compile(r"\$\{[^}]*\}")
FETCH_RE = re.compile(r"\bfetch\(")


class ManifestError(RuntimeError):
    """The source surface cannot be described, so the manifest cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_count(text: str) -> int:
    return len(text.splitlines())


def read_tabs(html: str) -> list[dict[str, object]]:
    """Return the mode tabs in document order, as markup declares them."""
    tabs: list[dict[str, object]] = []
    for match in BUTTON_RE.finditer(html):
        attrs = match.group("attrs")
        body = match.group("body")
        title_match = TITLE_RE.search(attrs)
        label_match = LABEL_RE.search(body)
        onclick_match = ONCLICK_RE.search(attrs)
        title = title_match.group(1) if title_match else ""
        shortcut_match = SHORTCUT_RE.search(title)
        tabs.append(
            {
                "dom_id": match.group("dom_id"),
                "label": label_match.group(1).strip() if label_match else "",
                "button_title": title,
                "shortcut": f"Alt+{shortcut_match.group(1)}" if shortcut_match else None,
                "onclick_target": onclick_match.group(1) if onclick_match else None,
            }
        )
    return tabs


def read_routes(source: str) -> list[dict[str, str]]:
    """Return the routes `app.py` mounts, in source order."""
    return [{"method": m.group(1).upper(), "path": m.group(2)} for m in ROUTE_RE.finditer(source)]


def read_api_literals(html: str) -> list[str]:
    """Return the distinct `/api/...` paths the browser script mentions, normalised.

    Template placeholders collapse to `{param}` so an interpolated id does not
    produce a new literal on every edit.
    """
    literals = {TEMPLATE_RE.sub("{param}", found) for found in API_LITERAL_RE.findall(html)}
    return sorted(literals)


def _read_ledger_pin() -> str:
    ledger = json.loads(_text(LEDGER_PATH))
    pin = str(ledger.get("source_commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise ManifestError(
            f"{LEDGER_PATH.name}: source_commit_sha is {pin!r}, not a full 40-char sha; "
            "re-pin the ledger before pinning the manifest"
        )
    return pin


def build_manifest() -> dict[str, object]:
    """Describe the current source surface. Deterministic for a given tree."""
    files = []
    for relative, role in TRACKED_FILES:
        path = PROJECT_ROOT / relative
        if not path.is_file():
            raise ManifestError(f"tracked file is missing: {relative}")
        text = _text(path) if path.suffix in {".html", ".py", ".md", ".json", ".mjs"} else ""
        files.append(
            {
                "path": relative,
                "role": role,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "lines": _line_count(text) if text else None,
            }
        )

    html = _text(PROJECT_ROOT / "static" / "index.html")
    app_source = _text(PROJECT_ROOT / "app.py")
    shot_manifest = json.loads(_text(PROJECT_ROOT / "docs" / "media" / "screenshots" / "manifest.json"))

    tabs = read_tabs(html)
    if not tabs:
        raise ManifestError("no data-tab buttons found in static/index.html")

    routes = read_routes(app_source)
    if not routes:
        raise ManifestError("no @app.<method> routes found in app.py")

    by_method: dict[str, int] = {}
    for route in routes:
        by_method[route["method"]] = by_method.get(route["method"], 0) + 1

    shots = shot_manifest.get("shots", [])
    shot_dir = PROJECT_ROOT / "docs" / "media" / "screenshots"

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "m5_uiux_source_manifest",
        "program": "M5_2_0_UIUX",
        "pinned_by_story": "UIUX-P0-01",
        "generator": "scripts/uiux_source_manifest.py",
        "regenerate_with": "python3 scripts/uiux_source_manifest.py",
        "check_with": "python3 scripts/uiux_source_manifest.py --check",
        "statement": (
            "Hashed inventory of the UI surface this program may change, plus the tab and route "
            "tables every later story depends on. Values are parsed from the files, not from the "
            "roadmap's web extracts. A story that changes the surface regenerates this manifest; "
            "the check fails when the two disagree."
        ),
        "pinned_source_commit": _read_ledger_pin(),
        "files": files,
        "frontend": {
            "path": "static/index.html",
            "sha256": _sha256(PROJECT_ROOT / "static" / "index.html"),
            "lines": _line_count(html),
            "tab_count": len(tabs),
            "tabs": tabs,
            "fetch_call_count": len(FETCH_RE.findall(html)),
            "api_literal_count": len(read_api_literals(html)),
            "api_literals": read_api_literals(html),
        },
        "api": {
            "path": "app.py",
            "sha256": _sha256(PROJECT_ROOT / "app.py"),
            "route_count": len(routes),
            "by_method": dict(sorted(by_method.items())),
            "routes": routes,
        },
        "screenshot_manifest": {
            "path": "docs/media/screenshots/manifest.json",
            "sha256": _sha256(PROJECT_ROOT / "docs" / "media" / "screenshots" / "manifest.json"),
            "viewport": shot_manifest.get("viewport", {}),
            "captured_with": shot_manifest.get("captured_with"),
            "shots": [
                {
                    "id": entry.get("id"),
                    "tab": entry.get("tab"),
                    "title": entry.get("title"),
                    "file": entry.get("file"),
                    "captured": bool(entry.get("file")) and (shot_dir / str(entry.get("file"))).is_file(),
                }
                for entry in shots
            ],
            "tab_ids": [entry.get("tab") for entry in shots],
            "captured": sum(
                1 for entry in shots if entry.get("file") and (shot_dir / str(entry.get("file"))).is_file()
            ),
            "total": len(shots),
            "tab_ids_match_frontend": [entry.get("tab") for entry in shots] == [tab["dom_id"] for tab in tabs],
        },
        "not_verified": [
            "No screenshot has been captured in this environment (capture needs a browser), so no "
            "visual claim is made anywhere in this program yet.",
            "The manifest records that handlers and routes exist and where they are; it does not "
            "claim which handler calls which route. That mapping is UIUX-P0-03.",
            "Line counts and digests describe this tree only; nothing here was checked against an "
            "upstream version or the roadmap's web extracts.",
        ],
    }


def dumps(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed manifest has drifted")
    parser.add_argument("--json", action="store_true", help="print the manifest instead of writing it")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest()
    except (ManifestError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    rendered = dumps(manifest)

    if args.json:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        if not MANIFEST_PATH.is_file():
            print(f"error: {MANIFEST_PATH.relative_to(PROJECT_ROOT)} is missing", file=sys.stderr)
            return 1
        committed = MANIFEST_PATH.read_text(encoding="utf-8")
        if committed != rendered:
            print(
                "error: the UI source manifest has drifted from the tree.\n"
                "       Regenerate it (the change is part of the story, not a fix-up):\n"
                "           python3 scripts/uiux_source_manifest.py",
                file=sys.stderr,
            )
            try:
                before = json.loads(committed)
                after = json.loads(rendered)
            except json.JSONDecodeError:
                return 1
            before_hashes = {f["path"]: f["sha256"] for f in before.get("files", [])}
            after_hashes = {f["path"]: f["sha256"] for f in after.get("files", [])}
            for path in sorted(set(before_hashes) | set(after_hashes)):
                old, new = before_hashes.get(path), after_hashes.get(path)
                if old != new:
                    print(f"       changed: {path}", file=sys.stderr)
            if before.get("pinned_source_commit") != after.get("pinned_source_commit"):
                print(
                    "       the ledger pin moved: "
                    f"{before.get('pinned_source_commit')} -> {after.get('pinned_source_commit')}",
                    file=sys.stderr,
                )
            return 1
        if not args.quiet:
            print(
                "UI source manifest is up to date: "
                f"{len(manifest['files'])} files, {manifest['frontend']['tab_count']} tabs, "
                f"{manifest['api']['route_count']} routes, pinned to "
                f"{manifest['pinned_source_commit'][:12]}"
            )
        return 0

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    if not args.quiet:
        print(
            f"Wrote {MANIFEST_PATH.relative_to(PROJECT_ROOT)}: {len(manifest['files'])} files, "
            f"{manifest['frontend']['tab_count']} tabs, {manifest['api']['route_count']} routes, "
            f"pinned to {manifest['pinned_source_commit'][:12]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
