#!/usr/bin/env python3
"""Package integrity-checked AS-059 profile files for explicit publication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark_publication import (  # noqa: E402
    BenchmarkPublicationError,
    build_publication_bundle,
    load_publication_config,
    write_bundle_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON config with methodology, sample plan, and caveats")
    parser.add_argument("--profile", required=True, action="append", type=Path, help="verified AS-059 raw profile JSONL; repeat for each model")
    parser.add_argument("--output", required=True, type=Path, help="destination JSON publication bundle")
    args = parser.parse_args()
    try:
        output_path = args.output.resolve()
        source_paths = {args.config.resolve(), Path(__file__).resolve(), *(item.resolve() for item in args.profile)}
        if output_path in source_paths:
            raise BenchmarkPublicationError("output must not overwrite a config, raw profile, or publisher script.")
        config = load_publication_config(args.config)
        bundle = build_publication_bundle(args.profile, config, publisher_script=Path(__file__))
        write_bundle_atomic(args.output, bundle)
    except (BenchmarkPublicationError, OSError, TypeError, ValueError) as exc:
        # Do not echo source paths, raw profile records, or parser details.
        print(f"Benchmark publication refused ({type(exc).__name__}).", file=sys.stderr)
        return 2
    print(f"Published verified bundle {bundle.digest} ({len(bundle.profiles)} profile(s)) to {args.output.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
