#!/usr/bin/env python3
"""Prove that a split test run preserved the unsplit one, test by test.

A sharded or xdist run is only safe when it ran *the same tests* to *the same
outcomes*. Comparing `passed` totals does not show that: a test can move from
`passed` to `skipped`, a module can be collected twice, and a skip can appear on
one interpreter only - all three survive a count comparison.

    # per interpreter, once per run
    pytest --collect-only -q > baseline-collect.txt
    pytest -q --junitxml=baseline.xml

    # the split phases of the same commit
    pytest --collect-only -q tests/test_a.py > phase-a-collect.txt
    pytest -q --junitxml=phase-a.xml tests/test_a.py

    python scripts/verify_test_identity.py \
        --version 3.11 \
        --baseline-collect baseline-collect.txt --baseline-junit baseline.xml \
        --phase serial:phase-a-collect.txt:phase-a.xml \
        --phase parallel:phase-b-collect.txt:phase-b.xml

Exit codes: 0 identical, 1 mismatch, 2 unreadable or ambiguous input. Mismatches
are printed one per line so a CI step can annotate them; `--json` emits the full
report for retention next to the run id.

This tool only reads files. It never runs pytest, never rewrites a report, and
never treats an unresolved node id as a match: an unmatched test id fails the
comparison, because the alternative is silently losing a test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

# A node id is `<path>.py::<name>`; the name may contain spaces, because pytest
# parametrisation keeps the parameter text verbatim (`test_x[<placeholder here>]`).
COLLECT_LINE = re.compile(r"^(\S+\.py::.+)$")
OUTCOMES = ("passed", "failed", "error", "skipped", "xfail")


class InputError(Exception):
    """An input file cannot be trusted, so no comparison is possible."""


@dataclass
class Report:
    """Everything the comparison found, in a shape that can be stored."""

    version: str
    baseline_count: int = 0
    union_count: int = 0
    duplicate_ids: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    outcome_changes: list[dict[str, str]] = field(default_factory=list)
    skip_changes: list[dict[str, str]] = field(default_factory=list)
    empty_phases: list[str] = field(default_factory=list)
    unmapped_cases: list[str] = field(default_factory=list)
    phase_counts: dict[str, int] = field(default_factory=dict)

    @property
    def identical(self) -> bool:
        return not any(
            (
                self.duplicate_ids,
                self.missing,
                self.unexpected,
                self.outcome_changes,
                self.skip_changes,
                self.empty_phases,
                self.unmapped_cases,
            )
        )

    def problems(self) -> list[str]:
        lines: list[str] = []
        lines.extend(f"empty phase collected no tests: {name}" for name in self.empty_phases)
        lines.extend(f"collected twice: {node} in {', '.join(phases)}" for node, phases in sorted(self.duplicate_ids.items()))
        lines.extend(f"missing from the split run: {node}" for node in self.missing)
        lines.extend(f"unexpected in the split run: {node}" for node in self.unexpected)
        for change in self.outcome_changes:
            lines.append(f"outcome changed: {change['node']} {change['baseline']} -> {change['split']}")
        for change in self.skip_changes:
            lines.append(f"skip reason changed: {change['node']}: {change['baseline']!r} -> {change['split']!r}")
        lines.extend(f"cannot map a junction case to a node id: {case}" for case in self.unmapped_cases)
        return lines


def read_collection(path: Path, *, allow_empty: bool = False) -> list[str]:
    """Node ids from `pytest --collect-only -q`, tolerating its trailing summary.

    A phase is allowed to collect nothing so that the report can say so; the
    baseline is not, because an empty baseline would make every phase "identical".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    nodes: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        match = COLLECT_LINE.match(line)
        if match:
            nodes.append(match.group(1))
    if not nodes and not allow_empty:
        raise InputError(f"{path} contains no `path.py::test` node ids; was it `pytest --collect-only -q`?")
    return nodes


def read_junit(path: Path, *, allow_empty: bool = False) -> list[tuple[str, str, str, str]]:
    """(classname, name, outcome, skip reason) rows from a pytest JUnit report."""
    try:
        tree = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError) as exc:
        raise InputError(f"cannot read {path} as JUnit XML: {exc}") from exc
    cases: list[tuple[str, str, str, str]] = []
    for case in tree.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        if not classname or not name:
            raise InputError(f"{path} has a <testcase> without classname/name")
        cases.append((classname, name, junit_outcome(case), skip_reason(case)))
    if not cases and not allow_empty:
        raise InputError(f"{path} contains no <testcase> elements")
    return cases


def junit_outcome(case: ElementTree.Element) -> str:
    """Collapse a JUnit testcase's children into one of the five outcomes."""
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "error"
    skipped = case.find("skipped")
    if skipped is not None:
        return "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
    return "passed"


def skip_reason(case: ElementTree.Element) -> str:
    """The skip message pytest writes into the report, normalised."""
    skipped = case.find("skipped")
    if skipped is None:
        return ""
    return " ".join((skipped.get("message") or "").split())


def resolve_node(classname: str, name: str, known: set[str]) -> str | None:
    """Map a JUnit classname/name pair onto a collected node id.

    `classname` is dotted: `tests.test_docs` for a module, `tests.test_x.TestY`
    for a class inside it. Walk the split from longest module path to shortest
    and accept the first candidate that the collection actually contains, so an
    unknown case cannot be guessed into a match.
    """
    parts = classname.split(".")
    for size in range(len(parts), 0, -1):
        module = "/".join(parts[:size]) + ".py"
        remainder = parts[size:]
        node = "::".join([module, *remainder, name])
        if node in known:
            return node
    return None


def read_phase(spec: str, known: set[str]) -> tuple[str, list[str], dict[str, tuple[str, str]]]:
    """Parse one `name:collect:junit` phase into ids and outcomes."""
    pieces = spec.split(":")
    if len(pieces) != 3 or not all(pieces):
        raise InputError(f"phase spec must be name:collect-file:junit-file, got {spec!r}")
    name, collect_name, junit_name = pieces
    nodes = read_collection(Path(collect_name), allow_empty=True)
    outcomes: dict[str, tuple[str, str]] = {}
    for classname, case_name, outcome, reason in read_junit(Path(junit_name), allow_empty=True):
        node = resolve_node(classname, case_name, known | set(nodes))
        if node is None:
            raise InputError(f"phase {name}: cannot map {classname}::{case_name} to a collected node id")
        outcomes[node] = (outcome, reason)
    return name, nodes, outcomes


def compare(
    version: str,
    baseline_nodes: list[str],
    baseline_outcomes: dict[str, tuple[str, str]],
    phases: list[tuple[str, list[str], dict[str, tuple[str, str]]]],
) -> Report:
    """Check set identity, duplicates, outcomes and skip reasons."""
    report = Report(version=version, baseline_count=len(baseline_nodes))
    baseline = set(baseline_nodes)
    if len(baseline) != len(baseline_nodes):
        duplicates = sorted({node for node in baseline_nodes if baseline_nodes.count(node) > 1})
        raise InputError(f"baseline collection repeats node ids: {duplicates[:5]}")

    seen: dict[str, list[str]] = {}
    union: set[str] = set()
    for name, nodes, _outcomes in phases:
        report.phase_counts[name] = len(nodes)
        if not nodes:
            report.empty_phases.append(name)
        for node in nodes:
            seen.setdefault(node, []).append(name)
            union.add(node)

    report.union_count = len(union)
    report.duplicate_ids = {node: names for node, names in seen.items() if len(names) > 1}
    report.missing = sorted(baseline - union)
    report.unexpected = sorted(union - baseline)

    split_outcomes: dict[str, tuple[str, str]] = {}
    for name, nodes, outcomes in phases:
        for node in nodes:
            if node in outcomes:
                split_outcomes[node] = outcomes[node]

    for node in sorted(baseline & union):
        base = baseline_outcomes.get(node)
        split = split_outcomes.get(node)
        if base is None or split is None:
            report.unmapped_cases.append(f"{node} (baseline outcome known={base is not None}, split outcome known={split is not None})")
            continue
        if base[0] != split[0]:
            report.outcome_changes.append({"node": node, "baseline": base[0], "split": split[0]})
        elif base[0] == "skipped" and base[1] != split[1]:
            report.skip_changes.append({"node": node, "baseline": base[1], "split": split[1]})
    return report


def load_outcomes(path: Path, known: set[str]) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Outcomes and unresolved case labels for a baseline JUnit report."""
    outcomes: dict[str, tuple[str, str]] = {}
    unmapped: list[str] = []
    tree = ElementTree.parse(path)
    for case in tree.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        node = resolve_node(classname, name, known)
        if node is None:
            unmapped.append(f"{classname}::{name}")
            continue
        outcomes[node] = (junit_outcome(case), skip_reason(case))
    return outcomes, unmapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="interpreter label this comparison belongs to, e.g. 3.11")
    parser.add_argument("--baseline-collect", required=True, type=Path, help="--collect-only -q output for the unsplit run")
    parser.add_argument("--baseline-junit", required=True, type=Path, help="--junitxml output for the unsplit run")
    parser.add_argument("--phase", action="append", default=[], metavar="NAME:COLLECT:JUNIT", help="one split phase; repeat it")
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of lines")
    args = parser.parse_args(argv)

    if not args.phase:
        parser.error("give at least one --phase so there is something to compare")
    try:
        baseline_nodes = read_collection(args.baseline_collect)
        known = set(baseline_nodes)
        baseline_outcomes, baseline_unmapped = load_outcomes(args.baseline_junit, known)
        if baseline_unmapped:
            raise InputError(f"baseline report has cases absent from its own collection: {baseline_unmapped[:5]}")
        phases = [read_phase(spec, known) for spec in args.phase]
        report = compare(args.version, baseline_nodes, baseline_outcomes, phases)
    except InputError as exc:
        print(f"identity check could not run: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.__dict__ | {"identical": report.identical}, indent=2, sort_keys=True))
    else:
        lines = report.problems()
        if lines:
            print(f"Python {args.version}: split run is not identical to the unsplit run", file=sys.stderr)
            for line in lines:
                print(f"  {line}", file=sys.stderr)
            print(
                f"  phases: {report.phase_counts} | union {report.union_count} of baseline {report.baseline_count}",
                file=sys.stderr,
            )
        else:
            print(
                f"Python {args.version}: identical - {report.union_count} tests, "
                f"{len(report.phase_counts)} phases, no duplicates, outcomes and skips unchanged"
            )
    return 0 if report.identical else 1


if __name__ == "__main__":
    raise SystemExit(main())
