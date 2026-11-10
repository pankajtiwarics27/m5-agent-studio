#!/usr/bin/env python3
"""Run and verify deterministic job-level pytest shards for CI.

The committed manifest partitions every ``tests/test_*.py`` module exactly once.
Each runner records the collected node IDs, per-test outcomes and skip reasons,
and module wall time.  A same-run verifier rejects empty/overlapping shards and
compares their collected-node union with an unsharded collection.  An optional
manual baseline publishes the same canonical outcome hash, allowing a final
candidate's unsharded and sharded outcomes to be compared at one commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "ci_test_shards.json"
SHARD_IDS = ("1", "2", "3")
PYTEST_ARGS = ("-q", "--tb=short", "-rf")


def _json_dump(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _workflow_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _notice(title: str, message: str) -> None:
    if "," in title or "\n" in title or "\r" in title:
        raise ValueError("workflow-command titles must not contain property delimiters or newlines")
    print(f"::notice title={title}::{_workflow_escape(message)}", flush=True)


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or set(raw.get("shards", {})) != set(SHARD_IDS):
        raise ValueError("CI shard manifest must use schema 1 with shards 1, 2 and 3")
    return raw


def all_test_modules() -> tuple[str, ...]:
    return tuple(str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_*.py")))


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    expected = set(all_test_modules())
    seen: Counter[str] = Counter()
    errors: list[str] = []
    for shard in SHARD_IDS:
        modules = manifest["shards"].get(shard)
        if not isinstance(modules, list) or not modules:
            errors.append(f"shard {shard} is empty")
            continue
        if modules != sorted(modules):
            errors.append(f"shard {shard} modules are not sorted")
        for module in modules:
            if not isinstance(module, str):
                errors.append(f"shard {shard} contains a non-string module")
                continue
            seen[module] += 1
    missing = sorted(expected - set(seen))
    extra = sorted(set(seen) - expected)
    duplicates = sorted(module for module, count in seen.items() if count != 1)
    if missing:
        errors.append(f"missing modules: {missing}")
    if extra:
        errors.append(f"unknown modules: {extra}")
    if duplicates:
        errors.append(f"duplicate modules: {duplicates}")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "modules": len(expected),
        "module_counts": {shard: len(manifest["shards"][shard]) for shard in SHARD_IDS},
    }


def modules_for_shard(shard: str, manifest: dict[str, Any] | None = None) -> tuple[str, ...]:
    if shard not in SHARD_IDS:
        raise ValueError(f"unknown shard {shard!r}")
    selected = manifest or load_manifest()
    validate_manifest(selected)
    return tuple(selected["shards"][shard])


def canonical_outcome_hash(outcomes: dict[str, dict[str, str]]) -> str:
    canonical = [[nodeid, item["outcome"], item.get("skip_reason", "")] for nodeid, item in sorted(outcomes.items())]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def canonical_skip_hash(outcomes: dict[str, dict[str, str]]) -> str:
    skips = [[nodeid, item.get("skip_reason", "")] for nodeid, item in sorted(outcomes.items()) if item["outcome"] in {"skipped", "xfail"}]
    return hashlib.sha256(json.dumps(skips, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _skip_reason(report: Any) -> str:
    value = report.longrepr
    if isinstance(value, tuple) and value:
        return str(value[-1])
    return str(value).splitlines()[-1] if value else ""


@dataclass
class ResultRecorder:
    node_ids: list[str] = field(default_factory=list)
    outcomes: dict[str, dict[str, str]] = field(default_factory=dict)
    module_seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    @staticmethod
    def _module(nodeid: str) -> str:
        return nodeid.split("::", 1)[0]

    def pytest_collection_modifyitems(self, items: Sequence[Any]) -> None:
        self.node_ids = [item.nodeid for item in items]

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.module_seconds[self._module(report.nodeid)] += float(report.duration)
        if report.when == "call":
            if report.skipped:
                outcome = "xfail" if hasattr(report, "wasxfail") else "skipped"
                self.outcomes[report.nodeid] = {"outcome": outcome, "skip_reason": _skip_reason(report)}
            elif report.failed:
                self.outcomes[report.nodeid] = {"outcome": "failed", "skip_reason": ""}
            else:
                outcome = "xpass" if hasattr(report, "wasxfail") else "passed"
                self.outcomes[report.nodeid] = {"outcome": outcome, "skip_reason": ""}
        elif report.failed:
            self.outcomes[report.nodeid] = {"outcome": "error", "skip_reason": ""}
        elif report.skipped and report.nodeid not in self.outcomes:
            self.outcomes[report.nodeid] = {"outcome": "skipped", "skip_reason": _skip_reason(report)}


def _result_payload(*, label: str, python_version: str, elapsed: float, exit_code: int, recorder: ResultRecorder) -> dict[str, Any]:
    counts = Counter(item["outcome"] for item in recorder.outcomes.values())
    return {
        "schema_version": 1,
        "label": label,
        "python_version": python_version,
        "elapsed_seconds": round(elapsed, 3),
        "exit_code": int(exit_code),
        "node_ids": sorted(recorder.node_ids),
        "outcomes": dict(sorted(recorder.outcomes.items())),
        "outcome_counts": dict(sorted(counts.items())),
        "outcome_sha256": canonical_outcome_hash(recorder.outcomes),
        "skip_sha256": canonical_skip_hash(recorder.outcomes),
        "module_seconds": {key: round(value, 6) for key, value in sorted(recorder.module_seconds.items())},
    }


def run_pytest(*, modules: Sequence[str], label: str, python_version: str, output: Path) -> int:
    import pytest

    recorder = ResultRecorder()
    started = time.monotonic()
    exit_code = int(pytest.main([*PYTEST_ARGS, *modules], plugins=[recorder]))
    elapsed = time.monotonic() - started
    payload = _result_payload(
        label=label,
        python_version=python_version,
        elapsed=elapsed,
        exit_code=exit_code,
        recorder=recorder,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_json_dump(payload), encoding="utf-8")
    counts = ", ".join(f"{name}={count}" for name, count in payload["outcome_counts"].items())
    _notice(
        f"Pytest {label} - {round(elapsed)}s - Python {python_version}",
        f"nodes={len(payload['node_ids'])}; {counts}; outcomes_sha256={payload['outcome_sha256']}; skip_sha256={payload['skip_sha256']}",
    )
    timings = "; ".join(f"{module}={seconds:.3f}s" for module, seconds in payload["module_seconds"].items())
    _notice(f"Pytest {label} module timings - Python {python_version}", timings)
    return exit_code


def collect_node_ids(modules: Sequence[str]) -> set[str]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", *modules]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"collection failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
    return {line.strip() for line in completed.stdout.splitlines() if line.startswith("tests/") and "::" in line}


def verify_results(*, result_dir: Path, python_version: str, output: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    validate_manifest(manifest)
    paths = sorted(result_dir.glob(f"pytest-result-{python_version}-*.json"))
    expected_names = {f"pytest-result-{python_version}-{shard}.json" for shard in SHARD_IDS}
    if {path.name for path in paths} != expected_names:
        raise ValueError(f"expected result files {sorted(expected_names)}, found {[path.name for path in paths]}")

    results = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    node_counts: Counter[str] = Counter()
    outcomes: dict[str, dict[str, str]] = {}
    for result in results:
        if result.get("exit_code") != 0:
            raise ValueError(f"{result.get('label')} exited {result.get('exit_code')}")
        for nodeid in result.get("node_ids", []):
            node_counts[nodeid] += 1
        for nodeid, item in result.get("outcomes", {}).items():
            if nodeid in outcomes:
                raise ValueError(f"duplicate outcome for {nodeid}")
            outcomes[nodeid] = item

    duplicates = sorted(nodeid for nodeid, count in node_counts.items() if count != 1)
    if duplicates:
        raise ValueError(f"duplicate collected node IDs: {duplicates[:20]}")
    baseline_nodes = collect_node_ids(all_test_modules())
    union_nodes = set(node_counts)
    if union_nodes != baseline_nodes:
        missing = sorted(baseline_nodes - union_nodes)
        extra = sorted(union_nodes - baseline_nodes)
        raise ValueError(f"collection identity failed; missing={missing[:20]} extra={extra[:20]}")
    if set(outcomes) != union_nodes:
        missing = sorted(union_nodes - set(outcomes))
        extra = sorted(set(outcomes) - union_nodes)
        raise ValueError(f"outcome identity incomplete; missing={missing[:20]} extra={extra[:20]}")

    counts = Counter(item["outcome"] for item in outcomes.values())
    payload = {
        "schema_version": 1,
        "label": "sharded-union",
        "python_version": python_version,
        "node_count": len(union_nodes),
        "duplicate_count": 0,
        "outcome_counts": dict(sorted(counts.items())),
        "outcome_sha256": canonical_outcome_hash(outcomes),
        "skip_sha256": canonical_skip_hash(outcomes),
        "source_results": [path.name for path in paths],
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_json_dump(payload), encoding="utf-8")
    count_text = ", ".join(f"{name}={count}" for name, count in payload["outcome_counts"].items())
    _notice(
        f"Pytest sharded outcome - Python {python_version}",
        f"nodes={payload['node_count']}; duplicates=0; {count_text}; outcomes_sha256={payload['outcome_sha256']}; skip_sha256={payload['skip_sha256']}",
    )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--run-shard", choices=SHARD_IDS)
    mode.add_argument("--run-baseline", action="store_true")
    mode.add_argument("--verify-results", type=Path)
    parser.add_argument("--python-version", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = load_manifest()
        stats = validate_manifest(manifest)
        if args.validate:
            print(f"CI pytest shard map is valid: {stats['modules']} modules; {stats['module_counts']}")
            return 0
        if args.run_shard:
            if args.output is None:
                raise ValueError("--output is required with --run-shard")
            return run_pytest(
                modules=modules_for_shard(args.run_shard, manifest),
                label=f"shard {args.run_shard}",
                python_version=args.python_version,
                output=args.output,
            )
        if args.run_baseline:
            if args.output is None:
                raise ValueError("--output is required with --run-baseline")
            return run_pytest(
                modules=all_test_modules(),
                label="unsharded baseline",
                python_version=args.python_version,
                output=args.output,
            )
        verify_results(result_dir=args.verify_results, python_version=args.python_version, output=args.output)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"CI pytest shard verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
