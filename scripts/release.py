#!/usr/bin/env python3
"""Package, install, update, roll back, and verify versioned Studio releases (AS-040).

The artifact is the deterministic standalone `studio.py` produced by
`scripts/build_studio.py` (AS-014). Every command is local, needs no network or
root privileges, and never shells out.

Typical flow on a developer machine or a macOS host:

    python scripts/release.py package --version 9.0.1
    python scripts/release.py install --version 9.0.1 --root ~/.local/share/m5-agent-studio
    python scripts/release.py update  --version 9.0.2
    python scripts/release.py verify
    python scripts/release.py rollback

Signing, notarization, and SBOM/provenance are AS-042 and are deliberately not
claimed here; see `docs/packaging-rollback.md`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_manager as rm  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = PROJECT_ROOT / "dist" / "releases"
STANDALONE = PROJECT_ROOT / rm.ARTIFACT_NAME
BUILDER = PROJECT_ROOT / "scripts" / "build_studio.py"


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _dist_version_dir(dist: Path, version: str) -> Path:
    return dist / rm.validate_version(version)


def _artifact_for(dist: Path, version: str) -> Path:
    return _dist_version_dir(dist, version) / rm.ARTIFACT_NAME


def cmd_package(args: argparse.Namespace) -> int:
    version = rm.validate_version(args.version)
    source = Path(args.artifact).expanduser() if args.artifact else STANDALONE
    if not args.artifact:
        completed = subprocess.run(
            [sys.executable, str(BUILDER)], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            print(completed.stdout + completed.stderr, file=sys.stderr)
            raise rm.ReleaseError("Building the standalone artifact failed; packaging aborted.")
    if not source.is_file():
        raise rm.ReleaseError(f"Artifact not found: {source}")
    dist = Path(args.output).expanduser()
    target_dir = _dist_version_dir(dist, version)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / rm.ARTIFACT_NAME
    target.write_bytes(source.read_bytes())
    manifest = rm.build_manifest(target, version, source_commit=_git_commit())
    (target_dir / rm.MANIFEST_NAME).write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"action": "package", "version": version, "path": str(target), **manifest.to_dict()}, indent=2))
    return 0


def _load_dist_manifest(dist: Path, version: str) -> tuple[Path, rm.ReleaseManifest]:
    artifact = _artifact_for(dist, version)
    manifest_path = _dist_version_dir(dist, version) / rm.MANIFEST_NAME
    manifest = rm.ReleaseManifest.load(manifest_path)
    if manifest.version != version:
        raise rm.ReleaseError(f"Packaged manifest declares {manifest.version}, expected {version}.")
    return artifact, manifest


def cmd_install(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    artifact, manifest = _load_dist_manifest(Path(args.from_dir).expanduser(), rm.validate_version(args.version))
    print(json.dumps(rm.install(layout, artifact, manifest), indent=2))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    artifact, manifest = _load_dist_manifest(Path(args.from_dir).expanduser(), rm.validate_version(args.version))
    print(json.dumps(rm.update(layout, artifact, manifest, allow_downgrade=args.allow_downgrade), indent=2))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    print(json.dumps(rm.rollback(layout), indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    problems = rm.verify(layout)
    if problems:
        print("Verification failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print(json.dumps({"action": "verify", "healthy": True, "root": str(layout.root)}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    print(json.dumps(rm.status(layout), indent=2))
    return 0 if rm.status(layout)["healthy"] else 1


def cmd_prune(args: argparse.Namespace) -> int:
    layout = rm.InstallLayout(Path(args.root).expanduser())
    print(json.dumps(rm.prune(layout, keep=args.keep), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package", help="Build/copy the standalone artifact and write its manifest")
    package.add_argument("--version", required=True, help="MAJOR.MINOR.PATCH")
    package.add_argument("--artifact", help="Use an existing studio.py instead of rebuilding")
    package.add_argument("--output", default=str(DEFAULT_DIST), help="Release staging directory")
    package.set_defaults(func=cmd_package)

    for name, handler, help_text in (
        ("install", cmd_install, "Install and activate one packaged release"),
        ("update", cmd_update, "Install and activate a newer packaged release"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--version", required=True, help="MAJOR.MINOR.PATCH")
        command.add_argument("--root", default=str(rm.DEFAULT_ROOT), help="Installation root")
        command.add_argument("--from-dir", default=str(DEFAULT_DIST), help="Packaged release directory")
        if name == "update":
            command.add_argument(
                "--allow-downgrade",
                action="store_true",
                help="Permit activating an older version (normally use rollback instead)",
            )
        command.set_defaults(func=handler)

    rollback = sub.add_parser("rollback", help="Swap the active release with the previous one")
    rollback.add_argument("--root", default=str(rm.DEFAULT_ROOT))
    rollback.set_defaults(func=cmd_rollback)

    verify = sub.add_parser("verify", help="Verify digests of the active and previous releases")
    verify.add_argument("--root", default=str(rm.DEFAULT_ROOT))
    verify.set_defaults(func=cmd_verify)

    status = sub.add_parser("status", help="Show the active, previous, and installed versions")
    status.add_argument("--root", default=str(rm.DEFAULT_ROOT))
    status.set_defaults(func=cmd_status)

    prune = sub.add_parser("prune", help="Delete superseded releases, keeping the active and rollback target")
    prune.add_argument("--root", default=str(rm.DEFAULT_ROOT))
    prune.add_argument("--keep", type=int, default=2)
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except rm.ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
