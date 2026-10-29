#!/usr/bin/env python3
"""Capture reproducible AS-031 review evidence for an independent reviewer.

The output is a Markdown bundle that records *facts about the tree under review*:
the exact commit and dirty state, the interpreter/platform, resolved dependency
versions, the pinned baseline check, and (with ``--run-checks``) the verbatim
result of every verification command in ``BASELINE.md``.

It is deliberately boring: it never judges severity, never edits the sign-off
record, never calls the network, and it prints failures instead of hiding them.
An evidence bundle produced here is still author-run evidence; only
``docs/security-review-signoff.md`` can carry a reviewer's decision.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_LIMIT = 300

# (label, argv). Kept in the same order as BASELINE.md so a reviewer can diff.
CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Pinned baseline ancestry", ("scripts/verify_baseline.py",)),
    ("Ruff lint", ("-m", "ruff", "check", ".")),
    ("mypy typed contract modules", ("-m", "mypy",)),
    ("Compile all Python sources", ("-m", "compileall", "-q", "app.py", "scripts", "tests")),
    ("Frontend JavaScript syntax", ("scripts/check_frontend_syntax.py",)),
    ("Test suite", ("-m", "pytest", "-q")),
    ("Loopback smoke test", ("scripts/smoke_local.py",)),
    ("Standalone smoke test", ("scripts/smoke_local.py", "--standalone")),
    ("Threat model validation", ("scripts/validate_threat_model.py",)),
    ("Governance validation", ("scripts/validate_governance.py",)),
    ("ADR validation", ("scripts/validate_adrs.py",)),
    ("Security sign-off record", ("scripts/validate_security_signoff.py",)),
    ("Roadmap dashboard regeneration check", ("scripts/generate_roadmap_status.py", "--check")),
)

DEPENDENCIES = ("fastapi", "starlette", "uvicorn", "httpx", "httpcore", "keyring", "pytest")


@dataclass(frozen=True)
class CheckResult:
    """One verification command's outcome, recorded honestly including failures."""

    name: str
    command: str
    exit_code: int | None
    summary: str

    @property
    def status(self) -> str:
        if self.exit_code is None:
            return "not-run"
        return "passed" if self.exit_code == 0 else "FAILED"


def _default_runner(command: list[str], cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    return lines[-1][:SUMMARY_LIMIT]


def _argv(entry: tuple[str, ...]) -> list[str]:
    head = entry[0]
    if head == "-m":
        return [sys.executable, *entry]
    return [sys.executable, *entry]


def run_checks(
    checks: tuple[tuple[str, tuple[str, ...]], ...] = CHECKS,
    root: Path = ROOT,
    runner=_default_runner,
) -> list[CheckResult]:
    """Run each verification command, capturing its exit code and final line."""
    results: list[CheckResult] = []
    for name, entry in checks:
        command = _argv(entry)
        code, output = runner(command, root)
        results.append(
            CheckResult(
                name=name,
                command=" ".join(entry),
                exit_code=code,
                summary=_tail(output),
            )
        )
    return results


def collect_git_state(root: Path = ROOT, runner=_default_runner) -> dict[str, str]:
    """Return the commit identity the reviewer must pin in the sign-off record."""

    def query(*args: str) -> str:
        code, output = runner(["git", *args], root)
        return output.strip() if code == 0 else f"(unavailable: git {' '.join(args)})"

    working_tree = query("status", "--porcelain")
    if working_tree.startswith("("):
        working_tree = "unknown (git unavailable)"
    elif not working_tree:
        working_tree = "clean"
    return {
        "commit": query("rev-parse", "HEAD"),
        "commit_subject": query("log", "-1", "--pretty=%s"),
        "commit_author_date": query("log", "-1", "--pretty=%aI"),
        "branch": query("rev-parse", "--abbrev-ref", "HEAD"),
        "working_tree": working_tree,
        "shallow": query("rev-parse", "--is-shallow-repository"),
    }


def collect_environment(root: Path = ROOT) -> dict[str, str]:
    """Return interpreter, platform, and resolved dependency versions."""

    from importlib import metadata

    environment = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    for name in DEPENDENCIES:
        try:
            environment[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            environment[name] = "not installed"
    return environment


def render_report(
    git_state: dict[str, str],
    environment: dict[str, str],
    results: list[CheckResult],
    generated_at: datetime | None = None,
) -> str:
    """Render the evidence bundle as Markdown."""
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "# AS-031 review evidence bundle",
        "",
        f"Generated {stamp} by `scripts/security_review_evidence.py`.",
        "",
        "> This bundle records machine facts about the tree under review. It is "
        "**author-run evidence**, not an independent security review, a penetration "
        "test, or a certification. Only `docs/security-review-signoff.md` can carry "
        "a reviewer's severity ratings and accept/block decision.",
        "",
        "## Tree under review",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for key in ("commit", "commit_subject", "commit_author_date", "branch", "shallow"):
        lines.append(f"| {key.replace('_', ' ')} | `{git_state.get(key, 'unknown')}` |")
    working_tree = git_state.get("working_tree", "unknown")
    clean = "clean" if working_tree == "clean" else "DIRTY — review a clean checkout"
    lines.append(f"| working tree | {clean} |")
    lines += [
        "",
        "## Environment",
        "",
        "| Component | Version |",
        "|---|---|",
    ]
    for key, value in environment.items():
        lines.append(f"| {key} | {value} |")
    lines += [
        "",
        "## Verification commands",
        "",
        "| Check | Exit | Result |",
        "|---|---|---|",
    ]
    for result in results:
        lines.append(f"| {result.name} | {result.exit_code if result.exit_code is not None else '—'} | {result.status} |")
    lines += ["", "### Command output (final line)", ""]
    for result in results:
        lines.append(f"- **{result.name}** (`{result.command}`): {result.status} — {result.summary}")
    lines += [
        "",
        "## What this bundle does not establish",
        "",
        "- Threat-model coverage of B1–B12, finding severity, and fix verification.",
        "- Runtime coverage beyond the platform above (no macOS/Windows leg unless run there).",
        "- The opt-in live container sandbox test, when no container runtime is present.",
        "- Provider/MCP behaviour, reverse-proxy deployment, or dependency supply-chain risk.",
        "",
        "Record the reviewer decision in `docs/security-review-signoff.md` and run "
        "`python scripts/validate_security_signoff.py` before closing AS-031.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the bundle here instead of stdout",
    )
    parser.add_argument(
        "--run-checks",
        action="store_true",
        help="execute the full verification sequence (slow; may need extra tooling)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root to inspect (default: this checkout)",
    )
    args = parser.parse_args(argv)

    git_state = collect_git_state(args.root)
    environment = collect_environment(args.root)
    results = run_checks(root=args.root) if args.run_checks else [
        CheckResult(name=name, command=" ".join(entry), exit_code=None, summary="not run (pass --run-checks)")
        for name, entry in CHECKS
    ]
    report = render_report(git_state, environment, results)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Wrote evidence bundle: {args.output}")
    else:
        print(report)
    return 0 if all(result.exit_code in (None, 0) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
