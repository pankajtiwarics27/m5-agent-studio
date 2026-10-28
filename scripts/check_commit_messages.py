#!/usr/bin/env python3
"""Check that commits in a range are documented per the repository policy.

Every commit must let a future reader (human or LLM) understand what changed
and why without diffing the tree:

- a subject of 8-100 characters, no trailing period, no surrounding whitespace,
  and not a WIP/TMP/FIXME placeholder;
- a blank line separating subject and body;
- a body of at least 40 non-whitespace characters explaining the what/why
  (evidence such as commands run, tests added, or links is encouraged).

Recommended subject forms: ``AS-XXX: <imperative summary>`` for roadmap
stories, or ``type: summary`` (feat/fix/docs/refactor/test/chore/ci) for
everything else. Merge commits are exempt.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

MIN_SUBJECT = 8
MAX_SUBJECT = 100
MIN_BODY_CHARS = 40
BAD_PREFIXES = ("wip", "tmp", "temp", "fixme", "draft")


class Commit:
    def __init__(self, sha: str, message: str, parents: int) -> None:
        self.sha = sha
        self.parents = parents
        lines = message.splitlines()
        self.subject = lines[0].rstrip() if lines else ""
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        self.body = "\n".join(rest)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def commits_in_range(base: str, head: str) -> list[Commit]:
    log = git("log", "--format=%H%x00%P%x00%B", f"{base}..{head}").stdout
    commits: list[Commit] = []
    for entry in log.split("\x00"):
        if not entry.strip():
            continue
        parts = entry.split("\x00", 2)
        if len(parts) < 3:
            continue
        sha, parents, message = parts[0], parts[1], parts[2]
        commits.append(Commit(sha, message, len(parents.split())))
    return commits


def problems(commit: Commit) -> list[str]:
    issues: list[str] = []
    subject = commit.subject
    stripped = subject.strip()
    if len(stripped) < MIN_SUBJECT:
        issues.append(f"subject too short (min {MIN_SUBJECT} chars): {subject!r}")
    if len(stripped) > MAX_SUBJECT:
        issues.append(f"subject too long (max {MAX_SUBJECT} chars): {subject[:80]!r}...")
    if subject != stripped:
        issues.append("subject has leading/trailing whitespace")
    if stripped.endswith("."):
        issues.append("subject should not end with a period")
    if not stripped:
        issues.append("subject is empty")
        return issues
    if stripped.lower().replace(":", " ").split()[0] in BAD_PREFIXES:
        issues.append("subject looks like a placeholder (WIP/TMP/FIXME/DRAFT); finish or squash the commit")
    if not commit.body.strip():
        issues.append("missing body; every commit must be documented (what changed and why)")
    elif sum(len(ch) for ch in commit.body if not ch.isspace()) < MIN_BODY_CHARS:
        issues.append(f"body too thin (< {MIN_BODY_CHARS} non-whitespace chars); explain what and why")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="base ref; commits after it are checked (default: origin/main)")
    parser.add_argument("--head", default="HEAD", help="head ref to check (default: HEAD)")
    args = parser.parse_args()

    try:
        commits = commits_in_range(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"Cannot resolve range {args.base}..{args.head}: {exc.stderr.strip()}", file=sys.stderr)
        return 1

    failures = 0
    for commit in commits:
        if commit.parents > 1:
            continue
        issues = problems(commit)
        if issues:
            failures += 1
            print(f"{commit.sha[:12]} {commit.subject!r}")
            for issue in issues:
                print(f"  - {issue}")
    if failures:
        print(
            f"\n{failures} commit(s) do not meet the documented-commit policy; "
            "see AGENTS.md and CONTRIBUTING.md. Rewrite with 'git rebase -i' before pushing.",
            file=sys.stderr,
        )
        return 1
    print(f"Commit messages OK ({len(commits)} commit(s) checked, merge commits exempt).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
