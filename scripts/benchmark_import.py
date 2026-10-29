#!/usr/bin/env python3
"""Validate and import community benchmark contributions (AS-063).

    python3 scripts/benchmark_import.py wrap     --bundle bundle.json --hardware machine.json \
        --license CC-BY-4.0 --attribution "Example Lab" --contributor "Example Lab" --out contrib.json
    python3 scripts/benchmark_import.py validate --contribution contrib.json
    python3 scripts/benchmark_import.py import   --contribution contrib.json --registry ~/.local/share/m5-agent-studio/benchmarks
    python3 scripts/benchmark_import.py list     --registry ~/.local/share/m5-agent-studio/benchmarks
    python3 scripts/benchmark_import.py promote  --registry DIR --profile-digest sha256:... --vault DIR

`validate` and `import` verify schema, license, provenance, and hardware
identity, and `import` records every profile as `untrusted`. `promote` is the
only way to change that, and it requires a matching profile already measured
locally in the AS-060 profile vault. Nothing here executes contributed code or
contacts the network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import benchmark_import as bi  # noqa: E402
from performance_profiles import ProfileStoreError, ProfileVault  # noqa: E402


def cmd_wrap(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).expanduser()
    hardware_path = Path(args.hardware).expanduser()
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"benchmark import: bundle is unreadable ({type(exc).__name__}).", file=sys.stderr)
        return 1
    try:
        hardware = json.loads(hardware_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"benchmark import: hardware snapshot is unreadable ({type(exc).__name__}).", file=sys.stderr)
        return 1
    envelope = bi.build_envelope(
        bundle,
        hardware=hardware,
        license=bi.License(id=args.license, attribution=args.attribution, url=args.license_url),
        contributor=bi.Contributor(name=args.contributor, url=args.contributor_url),
    )
    target = bi.write_envelope(Path(args.out).expanduser(), envelope)
    _print({"written": str(target), "envelope": envelope})
    return 0


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_validate(args: argparse.Namespace) -> int:
    contribution = bi.load_contribution(Path(args.contribution).expanduser())
    _print(
        {
            "valid": True,
            "envelope_digest": contribution.envelope_digest,
            "bundle_digest": contribution.bundle_digest,
            "license": contribution.license.to_mapping(),
            "contributor": contribution.contributor.to_mapping(),
            "hardware_digest": contribution.hardware_digest,
            "os_id": contribution.os_id,
            "methodology_digest": contribution.methodology_digest,
            "trust": contribution.trust,
            "profiles": [
                {
                    "model_id": profile.model_id,
                    "profile_digest": profile.profile_digest,
                    "trust": profile.trust,
                }
                for profile in contribution.profiles
            ],
        }
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    contribution = bi.load_contribution(Path(args.contribution).expanduser())
    registry = bi.TrustRegistry(Path(args.registry).expanduser())
    states = registry.import_contribution(contribution)
    _print(
        {
            "imported": True,
            "envelope_digest": contribution.envelope_digest,
            "registry": str(registry.path),
            "license_notice": registry.licence_notice(),
            "profiles": [
                {"model_id": state.model_id, "profile_digest": state.profile_digest, "trust": state.trust}
                for state in states
            ],
        }
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    registry = bi.TrustRegistry(Path(args.registry).expanduser())
    states = registry.states()
    if args.json:
        _print(
            {
                "registry": str(registry.path),
                "profiles": [
                    {
                        "model_id": state.model_id,
                        "profile_digest": state.profile_digest,
                        "trust": state.trust,
                        "envelope_digest": state.envelope_digest,
                    }
                    for state in states
                ],
                "license_notice": registry.licence_notice(),
            }
        )
        return 0
    if not states:
        print(f"No imported benchmarks in {registry.path}")
        return 0
    print(f"{len(states)} imported profile(s) in {registry.path}")
    for state in states:
        print(f"  {state.trust:<17} {state.model_id:<24} {state.profile_digest}")
    notice = registry.licence_notice()
    if notice:
        print("\nAttribution required by imported licenses:")
        print(notice)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    registry = bi.TrustRegistry(Path(args.registry).expanduser())
    vault = ProfileVault(Path(args.vault).expanduser())
    state = registry.promote(args.profile_digest, vault=vault)
    _print(
        {
            "promoted": True,
            "model_id": state.model_id,
            "profile_digest": state.profile_digest,
            "trust": state.trust,
            "evidence_digest": state.evidence_digest,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    wrap = sub.add_parser("wrap", help="Wrap a published bundle as a contribution")
    wrap.add_argument("--bundle", required=True, help="Published bundle JSON")
    wrap.add_argument("--hardware", required=True, help="JSON file with the measured machine's snapshot")
    wrap.add_argument("--license", required=True, help="Data license id, e.g. CC-BY-4.0")
    wrap.add_argument("--attribution", default="", help="Attribution line the license requires")
    wrap.add_argument("--license-url", default="", help="License URL")
    wrap.add_argument("--contributor", required=True, help="Contributor name")
    wrap.add_argument("--contributor-url", default="", help="Contributor project URL")
    wrap.add_argument("--out", required=True, help="Where to write the contribution envelope")
    wrap.set_defaults(func=cmd_wrap)

    validate = sub.add_parser("validate", help="Verify a contribution without importing it")
    validate.add_argument("--contribution", required=True, help="Path to the contribution envelope")
    validate.set_defaults(func=cmd_validate)

    import_command = sub.add_parser("import", help="Verify and record a contribution as untrusted")
    import_command.add_argument("--contribution", required=True, help="Path to the contribution envelope")
    import_command.add_argument("--registry", required=True, help="Registry directory")
    import_command.set_defaults(func=cmd_import)

    list_command = sub.add_parser("list", help="Show imported profiles and their trust state")
    list_command.add_argument("--registry", required=True, help="Registry directory")
    list_command.add_argument("--json", action="store_true", help="Machine-readable output")
    list_command.set_defaults(func=cmd_list)

    promote = sub.add_parser("promote", help="Mark an imported profile locally verified")
    promote.add_argument("--registry", required=True, help="Registry directory")
    promote.add_argument("--profile-digest", required=True, help="Imported profile digest")
    promote.add_argument("--vault", required=True, help="Local AS-060 profile vault directory")
    promote.set_defaults(func=cmd_promote)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (bi.BenchmarkImportError, ProfileStoreError) as exc:
        print(f"benchmark import: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
