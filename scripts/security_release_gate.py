#!/usr/bin/env python3
"""Execute and evaluate the M5-P5-03 fault-injection and security release gate.

Modes:
- Default (`verify-invariants` mode): runs the full isolated crash matrix,
  user-edit races, external-write uncertainty, and sandbox/Git adversarial
  suite, evaluates ``docs/security-review-signoff.md`` + roadmap prerequisites,
  and exits ``0`` when all verifiable code/security invariants hold and release
  is either signed-ready or properly blocked (`gate_invariants_satisfied=True`).
- ``--enforce`` (`release-publish` gate mode): exits ``0`` ONLY when
  ``release_ready=True`` (``release_decision == "release-ready"``); exits ``1``
  whenever ``release-blocked`` (for example while AS-031 sign-off is deferred
  or M5-P3-01 container qualification is blocked).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import security_release_gate as srg  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--signoff",
        type=Path,
        default=ROOT / "docs" / "security-review-signoff.md",
        help="Path to docs/security-review-signoff.md",
    )
    parser.add_argument(
        "--roadmap",
        type=Path,
        default=ROOT / "product" / "M5_2_0_Autonomous_Roadmap.json",
        help="Path to product/M5_2_0_Autonomous_Roadmap.json",
    )
    parser.add_argument(
        "--hardware-evidence",
        type=Path,
        default=ROOT / "docs" / "m5-2-0-hardware-qualification-evidence.md",
        help="Path to docs/m5-2-0-hardware-qualification-evidence.md",
    )
    parser.add_argument(
        "--supply-chain-dir",
        type=Path,
        default=None,
        help="Optional supply-chain evidence directory (dist/evidence) to enforce signature readiness",
    )
    parser.add_argument(
        "--expected-commit",
        default="",
        help="Optional 40-char commit SHA that a signed AS-031 record must match",
    )
    parser.add_argument(
        "--skip-fault-suite",
        action="store_true",
        help="Evaluate sign-off and prerequisites only without running the fault/adversarial suites",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Fail closed with exit code 1 unless release_ready is True ('release-ready')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON report to stdout (default emits Markdown summary unless --json is passed)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write JSON report",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=None,
        help="Optional path to write Markdown report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roadmap_arg = args.roadmap if args.roadmap and args.roadmap.is_file() else None
    hw_arg = args.hardware_evidence if args.hardware_evidence and args.hardware_evidence.is_file() else None

    if args.skip_fault_suite:
        signoff_eval = srg.evaluate_security_signoff_gate(
            args.signoff,
            expected_head_commit=args.expected_commit,
        )
        prereqs = srg.inspect_prerequisite_qualifications(
            roadmap_path=roadmap_arg,
            hardware_evidence_path=hw_arg,
            supply_chain_dir=args.supply_chain_dir,
        )
        report = srg.evaluate_release_gate(
            signoff=signoff_eval,
            prerequisites=prereqs,
            cases=(),
            suites_executed=False,
        )
    else:
        import app  # noqa: PLC0415

        report = asyncio.run(
            app.run_security_release_gate_async(
                signoff_path=args.signoff,
                roadmap_path=roadmap_arg,
                hardware_evidence_path=hw_arg,
                supply_chain_dir=args.supply_chain_dir,
                expected_head_commit=args.expected_commit,
            )
        )

    payload_json = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    payload_md = srg.render_release_gate_markdown(report)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(payload_json, encoding="utf-8")
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(payload_md, encoding="utf-8")

    if args.json:
        sys.stdout.write(payload_json)
    else:
        sys.stdout.write(payload_md)

    if args.enforce:
        return 0 if report.release_ready else 1
    return 0 if report.gate_invariants_satisfied else 1


if __name__ == "__main__":
    raise SystemExit(main())
