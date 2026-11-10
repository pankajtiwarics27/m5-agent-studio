#!/usr/bin/env python3
"""Select a conservative, feature-scoped pytest set from changed repository paths.

The scope map is `tests/test_scopes.json`. Unknown, shared, or high-risk changes
fall back to the complete suite. Ready implementation PRs and scheduled CI retain
the full gate; draft PRs and explicitly mapped documentation paths use this
selector for their scoped test phase.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "tests" / "test_scopes.json"
TESTS_DIR = ROOT / "tests"


@dataclass(frozen=True)
class TestPlan:
    """Resolved pytest selection and why it was chosen."""

    full_suite: bool
    tests: tuple[str, ...]
    areas: tuple[str, ...]
    changed_paths: tuple[str, ...]
    reason: str


def _load_map(path: Path = MAP_PATH) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read test scope map {path}: {exc}") from exc
    if data.get("schema_version") != 1:
        raise ValueError("test scope map schema_version must be 1")
    if not isinstance(data.get("areas"), dict) or not isinstance(data.get("source_areas"), dict):
        raise ValueError("test scope map must define areas and source_areas objects")
    return data


def _matching_test_files(patterns: list[str]) -> set[str]:
    available = {path.name for path in TESTS_DIR.glob("test_*.py")}
    return {name for name in available if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)}


def validate_scope_map(root: Path = ROOT, map_path: Path = MAP_PATH) -> list[str]:
    """Validate area patterns and ensure every pytest module has a category."""
    try:
        data = _load_map(map_path)
    except ValueError as exc:
        return [str(exc)]

    tests_dir = root / "tests"
    available = {path.name for path in tests_dir.glob("test_*.py")}
    covered: set[str] = set()
    problems: list[str] = []
    areas = data["areas"]
    assert isinstance(areas, dict)
    for area, patterns in areas.items():
        if not isinstance(area, str) or not isinstance(patterns, list) or not all(
            isinstance(pattern, str) for pattern in patterns
        ):
            problems.append(f"{area!r}: patterns must be a list of strings")
            continue
        matches = {
            name for name in available
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
        }
        if not matches:
            problems.append(f"{area}: no test modules match its patterns")
        covered.update(matches)
    missing = sorted(available - covered)
    if missing:
        problems.append(f"unmapped test modules: {', '.join(missing)}")

    source_areas = data["source_areas"]
    assert isinstance(source_areas, dict)
    for source, names in source_areas.items():
        if not isinstance(source, str) or not isinstance(names, list):
            problems.append(f"invalid source_areas entry: {source!r}")
            continue
        unknown = sorted(set(names) - set(areas))
        if unknown:
            problems.append(f"{source}: unknown test areas: {', '.join(unknown)}")
    full_paths = data.get("full_suite_paths", [])
    if not isinstance(full_paths, list) or not all(isinstance(path, str) for path in full_paths):
        problems.append("full_suite_paths must be a list of strings")
    return problems


def _groups_for_test(test_name: str, data: dict[str, object]) -> set[str]:
    groups: set[str] = set()
    areas = data["areas"]
    assert isinstance(areas, dict)
    for area, patterns in areas.items():
        if any(fnmatch.fnmatchcase(test_name, pattern) for pattern in patterns):
            groups.add(area)
    return groups


def _expand_areas(area_names: set[str], data: dict[str, object]) -> tuple[str, ...]:
    areas = data["areas"]
    assert isinstance(areas, dict)
    filenames: set[str] = set()
    for area in area_names:
        patterns = areas.get(area)
        if not isinstance(patterns, list):
            raise ValueError(f"unknown test area: {area}")
        filenames.update(_matching_test_files(patterns))
    return tuple(f"tests/{name}" for name in sorted(filenames))


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def plan_for_paths(
    changed_paths: list[str], data: dict[str, object] | None = None, root: Path = ROOT
) -> TestPlan:
    """Map changed paths to tests, using a full-suite fallback on uncertainty."""
    scope_map = data or _load_map()
    normalized_paths = {path.replace("\\", "/") for path in changed_paths if path}
    normalized = tuple(sorted(path[2:] if path.startswith("./") else path for path in normalized_paths))
    if not normalized:
        return TestPlan(True, (), (), (), "no changed paths were supplied")

    areas = scope_map["areas"]
    source_areas = scope_map["source_areas"]
    full_paths = scope_map.get("full_suite_paths", [])
    assert isinstance(areas, dict) and isinstance(source_areas, dict) and isinstance(full_paths, list)
    selected_areas: set[str] = set()
    selected_tests: set[str] = set()

    for path in normalized:
        if _matches_any(path, full_paths):
            return TestPlan(True, (), (), normalized, f"high-risk or test-infrastructure change: {path}")

        if path.startswith("tests/"):
            name = Path(path).name
            if not name.startswith("test_") or not name.endswith(".py"):
                return TestPlan(True, (), (), normalized, f"unmapped test support file: {path}")
            if not (root / path).is_file():
                return TestPlan(True, (), (), normalized, f"test module was added or removed: {path}")
            groups = _groups_for_test(name, scope_map)
            if not groups:
                return TestPlan(True, (), (), normalized, f"test module is not categorized: {path}")
            selected_tests.add(path)
            selected_areas.update(groups)
            continue

        explicit = source_areas.get(path)
        if isinstance(explicit, list):
            selected_areas.update(explicit)
            continue

        if path in {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md"} or path.startswith(
            ("docs/", "product/")
        ) or path.endswith((".md", ".rst")):
            selected_areas.add("quality.docs-ledger")
            if path.startswith("docs/media/"):
                selected_areas.add("ui.shared")
            continue

        if path.endswith(".py") and "/" not in path:
            paired = f"test_{Path(path).stem}.py"
            if not (TESTS_DIR / paired).is_file():
                return TestPlan(True, (), (), normalized, f"no direct test module for source file: {path}")
            groups = _groups_for_test(paired, scope_map)
            if not groups:
                return TestPlan(True, (), (), normalized, f"paired test module is not categorized: {paired}")
            selected_tests.add(f"tests/{paired}")
            selected_areas.update(groups)
            continue

        if path.startswith("scripts/") and path.endswith(".py"):
            paired = f"test_{Path(path).stem}.py"
            if (TESTS_DIR / paired).is_file():
                groups = _groups_for_test(paired, scope_map)
                if not groups:
                    return TestPlan(True, (), (), normalized, f"paired test module is not categorized: {paired}")
                selected_tests.add(f"tests/{paired}")
                selected_areas.update(groups)
                continue
            return TestPlan(True, (), (), normalized, f"no direct test mapping for script: {path}")

        if path.startswith("static/"):
            return TestPlan(True, (), (), normalized, f"unmapped frontend change: {path}")

        return TestPlan(True, (), (), normalized, f"unmapped changed path: {path}")

    tests = set(_expand_areas(selected_areas, scope_map)) | selected_tests
    if not tests:
        return TestPlan(True, (), (), normalized, "the mapping selected no tests")
    return TestPlan(False, tuple(sorted(tests)), tuple(sorted(selected_areas)), normalized, "mapped change")


def _parse_name_status_z(output: str) -> list[str]:
    """Parse `git diff --name-status -z`, retaining both sides of renames."""
    fields = output.split("\0")
    changed: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                raise RuntimeError("git returned a malformed rename/copy record")
            old, new = fields[index], fields[index + 1]
            index += 2
            changed.extend((old, new))
        else:
            if index >= len(fields):
                raise RuntimeError("git returned a malformed diff record")
            path = fields[index]
            index += 1
            if status.startswith("D"):
                changed.append(path)
            else:
                changed.append(path)
    return changed


def _git_changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", base, head],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return _parse_name_status_z(result.stdout)


def _git_worktree_paths(root: Path = ROOT) -> list[str]:
    """Include staged, unstaged, and untracked (non-ignored) local changes."""
    paths: list[str] = []
    commands = (
        ["diff", "--name-status", "-z", "HEAD"],
        ["diff", "--cached", "--name-status", "-z"],
    )
    for command in commands:
        result = subprocess.run(
            ["git", "-C", str(root), *command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git worktree diff failed")
        paths.extend(_parse_name_status_z(result.stdout))
    untracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if untracked.returncode:
        raise RuntimeError(untracked.stderr.strip() or "git untracked-file scan failed")
    paths.extend(path for path in untracked.stdout.split("\0") if path)
    return paths


def _print_plan(plan: TestPlan) -> None:
    report = {
        "mode": "full" if plan.full_suite else "scoped",
        "reason": plan.reason,
        "changed_paths": list(plan.changed_paths),
        "areas": list(plan.areas),
        "tests": list(plan.tests),
    }
    print(json.dumps(report, indent=2))
    if plan.full_suite:
        print("Selection: full pytest suite (conservative fallback).")
    else:
        print(f"Selection: {len(plan.tests)} test modules across {len(plan.areas)} area(s).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="base commit/ref for changed-file selection")
    parser.add_argument("--head", help="head commit/ref (defaults to HEAD)")
    parser.add_argument("--paths-file", type=Path, help="JSON array of changed paths from an authoritative PR file listing")
    parser.add_argument("--worktree", action="store_true", help="include staged, unstaged, and untracked local changes")
    parser.add_argument("--area", action="append", default=[], help="run a named area; repeat to combine areas")
    parser.add_argument("--list", action="store_true", help="print the selection without running pytest")
    parser.add_argument("--check-map", action="store_true", help="validate that every test module is categorized")
    args = parser.parse_args(argv)

    problems = validate_scope_map()
    if problems:
        for problem in problems:
            print(f"test scope map: {problem}", file=sys.stderr)
        return 2

    data = _load_map()
    try:
        if args.check_map:
            areas = data["areas"]
            assert isinstance(areas, dict)
            count = len(list(TESTS_DIR.glob("test_*.py")))
            print(f"Test scope map is valid: {count} test modules in {len(areas)} areas.")
            return 0
        if args.paths_file is not None:
            if args.area or args.base or args.worktree:
                raise ValueError("--paths-file cannot be combined with --area, --base, or --worktree")
            try:
                changed_paths = json.loads(args.paths_file.read_text(encoding="utf-8"))
                if not isinstance(changed_paths, list) or not all(
                    isinstance(path, str) for path in changed_paths
                ):
                    raise ValueError("changed paths file must contain a JSON array of strings")
                plan = plan_for_paths(changed_paths, data)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                plan = TestPlan(
                    True,
                    (),
                    (),
                    (),
                    f"could not read authoritative changed paths: {exc}",
                )
        elif args.area:
            unknown = sorted(set(args.area) - set(data["areas"]))
            if unknown:
                raise ValueError(f"unknown test area(s): {', '.join(unknown)}")
            plan = TestPlan(False, _expand_areas(set(args.area), data), tuple(sorted(set(args.area))), (), "explicit area selection")
        else:
            if not args.base:
                raise ValueError("provide --base or one or more --area values")
            head = args.head or "HEAD"
            try:
                paths = _git_changed_paths(args.base, head)
                if args.worktree:
                    paths.extend(_git_worktree_paths())
            except RuntimeError as exc:
                plan = TestPlan(True, (), (), (), f"could not determine changed paths: {exc}")
            else:
                plan = plan_for_paths(paths, data)
    except ValueError as exc:
        parser.error(str(exc))

    _print_plan(plan)
    if args.list:
        return 0
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    if not plan.full_suite:
        command.extend(plan.tests)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
