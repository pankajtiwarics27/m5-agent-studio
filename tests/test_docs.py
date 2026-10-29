from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.validate_docs import (
    DOCS_INDEX,
    Document,
    README,
    REQUIRED_DOCS,
    markdown_files,
    validate_docs,
)

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = tuple(REQUIRED_DOCS)


def _copy_docs(destination: Path) -> None:
    """Copy the whole documentation tree into a scratch directory.

    Every Markdown file is copied, not just the required pages: the pages
    cross-link each other, so a partial copy would report the missing neighbours
    as broken links and drown the failure being tested.
    """
    copied: set[Path] = set()
    for relative in markdown_files(ROOT):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
        copied.add(relative)
    # Pages link to source files and generated docs too (app.py, ../SECURITY.md).
    # Mirror exactly the targets that exist in the repository so the scratch tree
    # reproduces link validity instead of tripping over absent code files.
    for relative in list(copied):
        document_text = (ROOT / relative).read_text(encoding="utf-8")
        for target in Document(path=relative, text=document_text).links():
            resolved = (relative.parent / target)
            source = ROOT / resolved
            if source.is_file() and resolved not in copied:
                destination_target = destination / resolved
                destination_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination_target)
                copied.add(resolved)


def _docs_tree() -> Path:
    """Return the repository root, which the validator scans in place."""
    return ROOT


def test_repository_documentation_set_is_valid() -> None:
    assert validate_docs(ROOT) == []
    for relative in REQUIRED:
        assert (ROOT / relative).is_file()


def test_required_doc_headings_are_present_in_the_repository() -> None:
    for relative, headings in REQUIRED_DOCS.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for heading in headings:
            assert f"## {heading}" in text, f"{relative} lost its '{heading}' section"


def test_validator_reports_missing_required_document(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    (tmp_path / "docs" / "privacy.md").unlink()

    errors = validate_docs(tmp_path)

    assert "missing required document: docs/privacy.md" in errors


def test_validator_reports_a_renamed_section(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "migration-guide.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("## Rolling back", "## Reverting", 1),
        encoding="utf-8",
    )

    errors = validate_docs(tmp_path)

    assert any("missing required section: Rolling back" in error for error in errors)


def test_validator_reports_a_broken_relative_link(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "architecture.md"
    text = target.read_text(encoding="utf-8")
    assert "(privacy.md)" in text
    target.write_text(text.replace("(privacy.md)", "(privaci.md)", 1), encoding="utf-8")

    errors = validate_docs(tmp_path)

    assert any("broken relative link: privaci.md" in error for error in errors)


def test_validator_reports_a_link_escaping_the_repository(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "privacy.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n[escape](../../etc/passwd)\n", encoding="utf-8")

    errors = validate_docs(tmp_path)

    assert any("link escapes the repository" in error for error in errors)


@pytest.mark.parametrize("marker", ["TODO", "TBD", "FIXME", "<placeholder here>"])
def test_validator_reports_placeholder_text(tmp_path: Path, marker: str) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "quickstart.md"
    target.write_text(target.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8")

    errors = validate_docs(tmp_path)

    assert any("placeholder text" in error for error in errors)


def test_validator_reports_an_unpublished_page(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    # Drop every mention of supported-hardware.md from the two entry points.
    for relative in (README, DOCS_INDEX):
        path = tmp_path / relative
        text = path.read_text(encoding="utf-8").replace("supported-hardware.md", "hardware.md")
        path.write_text(text, encoding="utf-8")

    errors = validate_docs(tmp_path)

    assert any("docs/supported-hardware.md: is not linked" in error for error in errors)
    # The redirected links are themselves broken, which is also reported.
    assert any("broken relative link: hardware.md" in error for error in errors)


def test_validator_reports_a_missing_docs_index(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    (tmp_path / DOCS_INDEX).unlink()

    errors = validate_docs(tmp_path)

    assert "missing docs index: docs/README.md" in errors


def test_validator_reports_a_missing_readme(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    (tmp_path / README).unlink()

    errors = validate_docs(tmp_path)

    assert "missing or unreadable README.md" in errors


def test_external_links_and_anchors_are_not_fetched_or_flagged(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "architecture.md"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n[site](https://example.invalid/nope) [jump](#components) [mail](mailto:a@b.test)\n",
        encoding="utf-8",
    )

    assert validate_docs(tmp_path) == []


def test_markdown_files_cover_docs_and_root_pages() -> None:
    scanned = {path.as_posix() for path in markdown_files(_docs_tree())}

    assert "docs/quickstart.md" in scanned
    assert "README.md" in scanned
    assert "SECURITY.md" in scanned
    assert any(path.startswith("docs/adr/") for path in scanned)


def test_generated_dashboard_is_exempt_from_the_placeholder_scan(tmp_path: Path) -> None:
    """The dashboard quotes roadmap evidence prose, which may mention markers."""
    _copy_docs(tmp_path)
    dashboard = tmp_path / "docs" / "product-roadmap-status.md"
    dashboard.write_text(dashboard.read_text(encoding="utf-8") + "\nTODO TBD\n", encoding="utf-8")

    assert validate_docs(tmp_path) == []


def test_authored_pages_are_still_scanned_for_markers(tmp_path: Path) -> None:
    _copy_docs(tmp_path)
    target = tmp_path / "docs" / "architecture.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nFIXME\n", encoding="utf-8")

    errors = validate_docs(tmp_path)

    assert any("docs/architecture.md: contains placeholder text" in error for error in errors)
