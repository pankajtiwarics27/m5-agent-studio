#!/usr/bin/env python3
"""Validate the published documentation set and its internal links (AS-041).

Documentation rots silently: a link to a deleted file still renders, a section
heading gets renamed, and nothing notices until a reader hits a dead end. This
validator is the documentation equivalent of the generated-artifact checks — it
fails the build when the docs stop being true about themselves.

It enforces five things and nothing else:

1. every required document exists;
2. each required document keeps its required sections (renaming a heading is a
   deliberate act, not an accident);
3. no document contains placeholder text (``TODO``, ``TBD``, ``FIXME``, or an
   angle-bracket placeholder) — this repository publishes evidence, not stubs;
4. every relative Markdown link resolves to a file that exists, and every
   required document is reachable from ``README.md`` (otherwise it is written but
   not published);
5. the module counts ``README.md`` states about the code are the counts the code
   actually has, and every module ``app.py`` imports is either inlined by
   ``scripts/build_studio.py`` or one of the two documented special cases. Those
   sentences were correct when written and went stale twice in one afternoon
   (``M5-P3-03`` and ``M5-P3-04`` each added a module), which is exactly the kind
   of rot a reader cannot detect. A sentence that is absent is not an error; a
   sentence that is present must be true.

Deliberately no network access: external URLs are not fetched, so a dead external
link is not caught here.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_INDEX = Path("docs/README.md")
README = Path("README.md")

# Pages this story publishes, with the headings each must keep.
REQUIRED_DOCS: dict[str, tuple[str, ...]] = {
    "docs/quickstart.md": (
        "Install",
        "First run",
        "Connect a model",
        "First task",
        "Troubleshooting",
        "Evidence",
    ),
    "docs/architecture.md": (
        "Components",
        "Request path",
        "Storage",
        "Generated artifacts",
        "Extension seams",
        "Limits",
    ),
    "docs/supported-hardware.md": (
        "Requirements",
        "Platform status",
        "Capability model",
        "Model sizing",
        "Evidence",
    ),
    "docs/privacy.md": (
        "What stays local",
        "What leaves the machine",
        "Secrets",
        "Retention and export",
        "Deletion",
        "Limits",
    ),
    "docs/migration-guide.md": (
        "Before you upgrade",
        "Upgrading the release",
        "Database schema",
        "Credential migration",
        "Rolling back",
        "Configuration changes",
        "Evidence",
    ),
}

# Generated pages carry roadmap/evidence prose rather than authored stubs; their
# source (the roadmap JSON) is validated by its own dashboard-regeneration check.
PLACEHOLDER_EXEMPT = frozenset({"docs/product-roadmap-status.md"})

PLACEHOLDERS = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|<placeholder[^>]*>|<insert[^>]*>", re.IGNORECASE)
INLINE_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#")


@dataclass(frozen=True)
class Document:
    """One Markdown file considered by the validator."""

    path: Path
    text: str

    def sections(self) -> set[str]:
        return {
            match.group(1).strip()
            for match in re.finditer(r"^##\s+(.+?)\s*$", self.text, flags=re.MULTILINE)
        }

    def links(self) -> list[str]:
        """Return in-repository link targets with any fragment stripped."""
        targets: list[str] = []
        for raw in INLINE_LINK.findall(self.text):
            if raw.startswith(EXTERNAL_PREFIXES):
                continue
            target = raw.split("#", 1)[0]
            if target:
                targets.append(target)
        return targets


def pathlib_posixnorm(relative: str) -> str:
    """Collapse ``a/../b`` style segments without touching the filesystem."""
    parts: list[str] = []
    for part in relative.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _read(root: Path, relative: Path) -> Document | None:
    path = root / relative
    try:
        return Document(path=relative, text=path.read_text(encoding="utf-8"))
    except OSError:
        return None


def markdown_files(root: Path) -> list[Path]:
    """Return every published Markdown file: root-level docs plus docs/."""
    files = sorted((root / "docs").rglob("*.md")) if (root / "docs").is_dir() else []
    for name in ("README.md", "AGENTS.md", "BASELINE.md", "SECURITY.md", "CONTRIBUTING.md",
                 "CODE_OF_CONDUCT.md", "IMPLEMENTATION_STATUS.md"):
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)
    return [path.relative_to(root) for path in files]


def _link_errors(root: Path, document: Document) -> list[str]:
    errors: list[str] = []
    for target in document.links():
        resolved = (root / document.path.parent / target).resolve()
        try:
            resolved.relative_to((root).resolve())
        except ValueError:
            errors.append(f"{document.path}: link escapes the repository: {target}")
            continue
        if not resolved.exists():
            errors.append(f"{document.path}: broken relative link: {target}")
    return errors


# The two modules `scripts/build_studio.py` emits through dedicated paths rather
# than INLINED_MODULES (the orchestrator and the retention/privacy helpers are
# injected as whole sections, not as sibling modules).
_BUILD_SPECIAL_MODULES = frozenset({"workflow_orchestration", "history_privacy"})
_SHIPPED_COUNT_RE = re.compile(r"(\d+) of (\d+) shipped Python modules are wired into the served app")
_CONTRACT_COUNT_RE = re.compile(r"`app\.py` plus (\d+) small contract modules")
_INLINES_COUNT_RE = re.compile(r"inlines (\d+) sibling modules")
_TYPECHECKED_COUNT_RE = re.compile(r"\((\d+) of them type-checked")


def _local_modules(root: Path) -> set[str]:
    return {path.stem for path in root.glob("*.py")} - {"app", "studio"}


def _imported_local_modules(root: Path) -> set[str]:
    """Modules `app.py` imports, which is what "wired into the served app" means."""
    tree = ast.parse((root / "app.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names & _local_modules(root)


def _inlined_modules(root: Path) -> set[str]:
    """`INLINED_MODULES` from the builder, read by AST so nothing is executed."""
    tree = ast.parse((root / "scripts" / "build_studio.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "INLINED_MODULES" for target in node.targets
        ):
            return set(ast.literal_eval(node.value))
    return set()


def _mypy_modules(root: Path) -> set[str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^files = \[(.*?)^\]", text, flags=re.DOTALL | re.MULTILINE)
    if not match:
        return set()
    return {
        entry.rsplit(".py", 1)[0].strip().strip('",')
        for entry in re.findall(r'"([^"]+)"', match.group(1))
    }


def _module_count_errors(root: Path) -> list[str]:
    readme = _read(root, README)
    if readme is None:
        return []
    # Scratch trees assembled by the tests hold documentation only; with no code
    # to compare against there is nothing to keep true.
    required = (root / "app.py", root / "scripts" / "build_studio.py", root / "pyproject.toml")
    if not all(path.is_file() for path in required):
        return []
    errors: list[str] = []
    modules = _local_modules(root)
    imported = _imported_local_modules(root)
    inlined = _inlined_modules(root)
    mypy_modules = _mypy_modules(root)

    # A module app.py imports but the builder does not inline is missing from the
    # standalone bundle: the artifact and the served app would disagree.
    unbuilt = imported - inlined - _BUILD_SPECIAL_MODULES
    if unbuilt:
        errors.append(
            f"scripts/build_studio.py does not inline {sorted(unbuilt)}, which app.py imports"
        )

    def _check(pattern: re.Pattern[str], actual: int, what: str) -> None:
        match = pattern.search(readme.text)
        if match is None:
            return  # sentence rewritten; nothing to keep true
        if int(match.group(1)) != actual:
            errors.append(f"{README}: states {match.group(1)} {what}, code has {actual}")

    _check(_INLINES_COUNT_RE, len(inlined | _BUILD_SPECIAL_MODULES), "inlined sibling modules")
    match = _SHIPPED_COUNT_RE.search(readme.text)
    if match is not None:
        if int(match.group(1)) != len(imported):
            errors.append(f"{README}: states {match.group(1)} wired modules, code has {len(imported)}")
        if int(match.group(2)) != len(modules):
            errors.append(f"{README}: states {match.group(2)} shipped modules, code has {len(modules)}")
    _check(_CONTRACT_COUNT_RE, len(modules), "small contract modules")
    _check(_TYPECHECKED_COUNT_RE, len(mypy_modules), "type-checked modules")
    missing_from_mypy = sorted(modules - mypy_modules)
    if missing_from_mypy != ["edit_rollback"]:
        errors.append(
            f"pyproject.toml type-checks {len(mypy_modules)} modules; uncovered: {missing_from_mypy} "
            "(README documents `edit_rollback` as the only omission)"
        )
    return errors


def validate_docs(root: Path = ROOT) -> list[str]:
    """Return documentation defects for `root`; never accesses the network."""
    errors: list[str] = []

    readme = _read(root, README)
    if readme is None:
        errors.append(f"missing or unreadable {README}")
    index = _read(root, DOCS_INDEX)
    if index is None:
        errors.append(f"missing docs index: {DOCS_INDEX}")

    # Normalise every published link to a repository-relative posix path so a
    # link written as "quickstart.md" from docs/ matches "docs/quickstart.md".
    published_targets: set[str] = set()
    for source in (readme, index):
        if source is None:
            continue
        for target in source.links():
            resolved = (source.path.parent / target).as_posix()
            published_targets.add(pathlib_posixnorm(resolved))

    for relative, required_sections in REQUIRED_DOCS.items():
        document = _read(root, Path(relative))
        if document is None:
            errors.append(f"missing required document: {relative}")
            continue
        sections = document.sections()
        for heading in required_sections:
            if heading not in sections:
                errors.append(f"{relative}: missing required section: {heading}")
        if relative not in PLACEHOLDER_EXEMPT and PLACEHOLDERS.search(document.text):
            match = PLACEHOLDERS.search(document.text)
            errors.append(f"{relative}: contains placeholder text: {match.group(0) if match else ''}")
        if relative not in published_targets:
            errors.append(f"{relative}: is not linked from {README} or {DOCS_INDEX} (written but not published)")
        errors.extend(_link_errors(root, document))

    for relative in markdown_files(root):
        document = _read(root, relative)
        if document is None:
            continue
        if relative.as_posix() not in PLACEHOLDER_EXEMPT and PLACEHOLDERS.search(document.text):
            errors.append(f"{relative}: contains placeholder text")
        errors.extend(_link_errors(root, document))

    errors.extend(_module_count_errors(root))
    return errors


def main() -> int:
    errors = validate_docs()
    if errors:
        print("Documentation validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Documentation validation passed ({len(REQUIRED_DOCS)} required pages, "
        f"{len(markdown_files(ROOT))} files scanned)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
