#!/usr/bin/env python3
"""Verify the pinned source baseline is present in this checkout's history."""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class BaselineError(RuntimeError):
    """Raised when the repository does not contain its declared baseline."""


def verify_baseline(root: pathlib.Path = ROOT) -> str:
    pin_file = root / "BASELINE_COMMIT"
    try:
        pin = pin_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BaselineError(f"Cannot read {pin_file.name}: {exc}") from exc
    if not _SHA_RE.fullmatch(pin):
        raise BaselineError("BASELINE_COMMIT must contain one full, lowercase 40-character Git SHA")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )

    commit = git("cat-file", "-e", f"{pin}^{{commit}}")
    if commit.returncode:
        raise BaselineError(f"Pinned commit {pin} is not present in this checkout")

    ancestry = git("merge-base", "--is-ancestor", pin, "HEAD")
    if ancestry.returncode == 1:
        raise BaselineError(f"Pinned commit {pin} is not an ancestor of HEAD")
    if ancestry.returncode:
        detail = ancestry.stderr.strip() or "Git could not verify commit ancestry"
        raise BaselineError(f"Unable to verify baseline ancestry: {detail}")
    return pin


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=pathlib.Path, default=ROOT, help="repository root (defaults to this checkout)")
    args = parser.parse_args()
    try:
        pin = verify_baseline(args.repo.resolve())
    except BaselineError as exc:
        parser.exit(1, f"Baseline verification failed: {exc}\n")
    print(f"Baseline verified: {pin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
