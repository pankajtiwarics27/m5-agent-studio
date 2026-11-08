#!/usr/bin/env python3
"""Replay synthetic LC-00 evidence; never qualify or contact installed runtimes.

This disposable research oracle evaluates proposed necessary conditions, NOT a
wire adapter, coordinator, memory profiler, or release authority. Normalized
facts are fixture assumptions: production adapters still have to prove them.
A successful replay means only that expectations match this offline oracle.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tests/fixtures/model_lifecycle/feasibility.json"


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _instances(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def assess_release(facts: Mapping[str, object]) -> str:
    """Check necessary release evidence, not physical release itself."""
    if facts.get("operation_terminal") is not True:
        return "blocked:operation_unsettled"
    if not all(facts.get(key) is True for key in (
        "inventory_reachable", "inventory_fresh", "inventory_complete",
    )):
        return "blocked:observation_unknown"
    if not all(_instances(facts.get(key)) for key in ("resident_instances", "loading_instances")):
        return "blocked:residency_unknown"
    if facts["resident_instances"] or facts["loading_instances"]:
        return "blocked:resident_or_loading"
    active = facts.get("active_requests")
    if not _integer(active) or active != 0:
        return "blocked:requests_not_drained"
    if facts.get("headroom_verified") is not True:
        return "blocked:headroom_unverified"
    return "candidate_only"


def assess_request(facts: Mapping[str, object]) -> str:
    """Check necessary request-fit evidence without rendering or inference."""
    keys = ("approved_context_tokens", "active_context_tokens", "input_tokens", "output_reserve_tokens")
    if not all(_integer(facts.get(key), minimum=1 if "context" in key else 0) for key in keys):
        return "blocked:counts_unknown"
    if facts.get("instance_binding_verified") is not True:
        return "blocked:instance_unverified"
    load_profile = facts.get("load_profile")
    if (not isinstance(load_profile, str) or not load_profile.strip()
            or load_profile != facts.get("inference_profile")
            or facts["approved_context_tokens"] != facts["active_context_tokens"]):
        return "blocked:profile_mismatch"
    if facts.get("counter_load_admitted") is not True:
        return "blocked:counter_unadmitted"
    if not all(facts.get(key) is True for key in ("counter_verified", "count_matches_request")):
        return "blocked:counter_unverified"
    if not all(facts.get(key) is True for key in ("truncation_disabled", "shifting_disabled")):
        return "blocked:context_controls_unverified"
    # Values are narrowed explicitly rather than coercing strings or booleans.
    context, input_tokens, output = (facts[key] for key in (
        "active_context_tokens", "input_tokens", "output_reserve_tokens",
    ))
    assert isinstance(context, int) and isinstance(input_tokens, int) and isinstance(output, int)
    if input_tokens + output > context:
        return "blocked:context_budget"
    return "candidate_only"


def replay(corpus: object) -> dict[str, object]:
    """Return only case IDs/outcomes, never raw wire data or supplied evidence."""
    if (not isinstance(corpus, dict) or type(corpus.get("schema_version")) is not int
            or corpus.get("schema_version") != 1 or corpus.get("synthetic") is not True):
        raise ValueError("invalid synthetic corpus")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("invalid synthetic cases")
    seen: set[str] = set()
    outcomes: list[dict[str, object]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("invalid synthetic case")
        case_id, facts, kind = case.get("id"), case.get("facts"), case.get("kind")
        if (not isinstance(case_id, str) or not case_id or len(case_id) > 80
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in case_id)
                or case_id in seen or not isinstance(facts, dict)
                or kind not in ("release", "request")):
            raise ValueError("invalid synthetic case")
        seen.add(case_id)
        result = assess_release(facts) if kind == "release" else assess_request(facts)
        outcomes.append({"id": case_id, "result": result, "matches_expectation": result == case.get("expected")})
    return {
        "mode": "offline_synthetic",
        "installed_runtime_qualification": "not_run",
        "activation_allowed": False,
        "cases": outcomes,
        "expectations_match": all(row["matches_expectation"] for row in outcomes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    try:
        # Bounded, explicit local read; no environment discovery, SDK or app import.
        with args.corpus.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("oversized corpus")
        report = replay(json.loads(raw))
    except (OSError, ValueError, UnicodeError):
        print("Invalid offline fixture corpus; no runtime was contacted.")
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["expectations_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
