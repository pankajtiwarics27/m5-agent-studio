"""M5-P5-03 Fault-injection and security release gate evaluator.

Pure stdlib module (inlined into ``studio.py``) that:

1. Parses and validates ``docs/security-review-signoff.md`` (AS-031) for
   release-gate readiness without ever allowing an unsigned, deferred,
   agent-authored, or partially-verified record to pass.
2. Evaluates mandatory M5 2.0 roadmap prerequisite qualifications (including
   ``M5-P3-01`` container-backed isolation and ``M5-P4-01`` Apple M5 hardware
   qualification telemetry) plus optional supply-chain release evidence
   (``AS-042``).
3. Aggregates executed fault-injection, user-edit race, external-write
   uncertainty, and sandbox/Git adversarial suite results alongside measured
   reliability, memory, and UX metrics.
4. Produces an auditable ``release-ready`` vs ``release-blocked`` decision
   satisfying M5-P5-03 Acceptance Criteria 1–3:
   - AC 1: No known critical unauthorized execution paths
   - AC 2: No silent user-data loss in injected tests
   - AC 3: Required security sign-off recorded or release blocked
"""

from __future__ import annotations

import json
import re
import resource
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SRG_SCHEMA_VERSION = 1

SRG_EXPECTED_BOUNDARIES: tuple[str, ...] = tuple(f"B{i}" for i in range(1, 13))
SRG_EXPECTED_KNOWN_FINDINGS: tuple[str, ...] = ("W-1", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6")
SRG_REQUIRED_SUITES: tuple[str, ...] = (
    "crash_matrix",
    "user_edit_races",
    "external_write_uncertainty",
    "sandbox_adversarial",
)

_SRG_COMMENT_BLOCK_RE = re.compile(r"<!--\s*as031-signoff\s*\n(.*?)\n-->", re.DOTALL)
_SRG_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SRG_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SRG_REVIEWER_FINDING_RE = re.compile(r"^R-\d+$")
_SRG_AGENT_REVIEWER_RE = re.compile(
    r"\b(claude|chatgpt|gpt-4|gpt-5|gemini|grok|qwen|kimi|copilot|cursor|ai\s+agent|autonomous\s+agent|self-review)\b",
    re.IGNORECASE,
)
_SRG_PLACEHOLDER_CELLS: frozenset[str] = frozenset(
    {"", "_unset_", "_unassigned_", "_unsigned_", "tbd", "todo", "n/a", "pending"}
)
_SRG_VALID_STATES: frozenset[str] = frozenset({"unsigned", "deferred", "signed"})
_SRG_VALID_DECISIONS: frozenset[str] = frozenset({"pending", "accepted", "release-blocked"})
_SRG_VALID_SEVERITIES: frozenset[str] = frozenset({"unrated", "none", "low", "medium", "high", "critical"})
_SRG_VALID_VERIFIED: frozenset[str] = frozenset({"not-verified", "verified", "not-applicable"})


def _srg_current_peak_rss_bytes() -> int:
    """Return process peak RSS in bytes across Darwin (bytes) and Linux (KiB)."""
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return max(0, raw)
    return max(0, raw * 1024)


def _srg_parse_kv_comment(text: str) -> dict[str, str] | None:
    match = _SRG_COMMENT_BLOCK_RE.search(text)
    if match is None:
        return None
    fields: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_val = line.partition(":")
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] == '"':
            val = val[1:-1]
        fields[key.strip()] = val
    return fields


def _srg_parse_markdown_tables(text: str) -> list[list[dict[str, str]]]:
    tables: list[list[dict[str, str]]] = []
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        if lines[idx].lstrip().startswith("|"):
            block: list[str] = []
            while idx < len(lines) and lines[idx].lstrip().startswith("|"):
                block.append(lines[idx])
                idx += 1
            if len(block) >= 3 and set(block[1].replace("|", "").replace(":", "").strip()) <= {"-", " "}:
                headers = [cell.strip() for cell in block[0].strip().strip("|").split("|")]
                rows: list[dict[str, str]] = []
                for row_line in block[2:]:
                    cells = [cell.strip() for cell in row_line.strip().strip("|").split("|")]
                    if len(cells) == len(headers):
                        rows.append(dict(zip(headers, cells, strict=True)))
                tables.append(rows)
        else:
            idx += 1
    return tables


@dataclass(frozen=True)
class SignoffGateEvaluation:
    """Structured release-gate evaluation of ``docs/security-review-signoff.md``."""

    signoff_path: str
    schema_valid: bool
    state: str
    decision: str
    reviewer: str
    reviewer_independence: str
    commit: str
    reviewed_on: str
    deferral: str
    boundaries_total: int
    boundaries_examined_and_adequate: int
    known_findings_total: int
    unverified_high_or_critical: tuple[str, ...]
    passed: bool
    blocking_reasons: tuple[str, ...]
    schema_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "signoff_path": self.signoff_path,
            "schema_valid": self.schema_valid,
            "state": self.state,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reviewer_independence": self.reviewer_independence,
            "commit": self.commit,
            "reviewed_on": self.reviewed_on,
            "deferral": self.deferral,
            "boundaries_total": self.boundaries_total,
            "boundaries_examined_and_adequate": self.boundaries_examined_and_adequate,
            "known_findings_total": self.known_findings_total,
            "unverified_high_or_critical": list(self.unverified_high_or_critical),
            "passed": self.passed,
            "blocking_reasons": list(self.blocking_reasons),
            "schema_errors": list(self.schema_errors),
        }


def evaluate_security_signoff_gate(
    signoff_path: Path,
    *,
    expected_head_commit: str = "",
) -> SignoffGateEvaluation:
    """Validate ``docs/security-review-signoff.md`` and decide whether sign-off opens the gate.

    Fail-closed contract:
    - Any missing file, missing comment block, malformed table, or invalid token
      fails both ``schema_valid`` and ``passed``.
    - ``state == "deferred"`` or ``state == "unsigned"`` or ``decision != "accepted"``
      keeps ``schema_valid == True`` (when properly formatted) while setting
      ``passed == False`` with explicit ``blocking_reasons`` so the release gate
      mechanically blocks publication without forging a human review.
    - ``state == "signed"`` + ``decision == "accepted"`` opens the sign-off gate
      only when reviewer identity & independence are non-placeholder and
      non-agent, ``commit`` is a full 40-hex SHA (matching ``expected_head_commit``
      when provided), ``reviewed_on`` is ``YYYY-MM-DD``, all B1–B12 rows are
      ``examined`` + ``adequate`` + ``adequate``, every finding is rated &
      verified, and zero ``critical``/``high`` findings remain ``not-verified``.
    """
    path_str = str(signoff_path)
    if not signoff_path.is_file():
        reason = f"security_signoff_missing: sign-off file not found at {path_str}"
        return SignoffGateEvaluation(
            signoff_path=path_str,
            schema_valid=False,
            state="missing",
            decision="missing",
            reviewer="",
            reviewer_independence="",
            commit="",
            reviewed_on="",
            deferral="",
            boundaries_total=0,
            boundaries_examined_and_adequate=0,
            known_findings_total=0,
            unverified_high_or_critical=(),
            passed=False,
            blocking_reasons=(reason,),
            schema_errors=(reason,),
        )

    try:
        raw_text = signoff_path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = f"security_signoff_unreadable: {exc}"
        return SignoffGateEvaluation(
            signoff_path=path_str,
            schema_valid=False,
            state="unreadable",
            decision="unreadable",
            reviewer="",
            reviewer_independence="",
            commit="",
            reviewed_on="",
            deferral="",
            boundaries_total=0,
            boundaries_examined_and_adequate=0,
            known_findings_total=0,
            unverified_high_or_critical=(),
            passed=False,
            blocking_reasons=(reason,),
            schema_errors=(reason,),
        )

    schema_errors: list[str] = []
    blocking_reasons: list[str] = []

    meta = _srg_parse_kv_comment(raw_text)
    if meta is None:
        reason = "security_signoff_schema_invalid: missing <!-- as031-signoff ... --> metadata block"
        return SignoffGateEvaluation(
            signoff_path=path_str,
            schema_valid=False,
            state="invalid",
            decision="invalid",
            reviewer="",
            reviewer_independence="",
            commit="",
            reviewed_on="",
            deferral="",
            boundaries_total=0,
            boundaries_examined_and_adequate=0,
            known_findings_total=0,
            unverified_high_or_critical=(),
            passed=False,
            blocking_reasons=(reason,),
            schema_errors=(reason,),
        )

    required_keys = {"state", "reviewer", "reviewer_independence", "commit", "reviewed_on", "decision"}
    missing_keys = sorted(required_keys - set(meta.keys()))
    if missing_keys:
        schema_errors.append(f"security_signoff_schema_invalid: missing metadata keys {missing_keys}")

    state = meta.get("state", "").strip()
    decision = meta.get("decision", "").strip()
    reviewer = meta.get("reviewer", "").strip()
    reviewer_ind = meta.get("reviewer_independence", "").strip()
    commit = meta.get("commit", "").strip()
    reviewed_on = meta.get("reviewed_on", "").strip()
    deferral = meta.get("deferral", "").strip()

    if state not in _SRG_VALID_STATES:
        schema_errors.append(f"security_signoff_schema_invalid: invalid state {state!r}")
    if decision not in _SRG_VALID_DECISIONS:
        schema_errors.append(f"security_signoff_schema_invalid: invalid decision {decision!r}")

    tables = _srg_parse_markdown_tables(raw_text)
    boundary_rows = next((t for t in tables if t and "Boundary" in t[0]), None)
    finding_rows = next((t for t in tables if t and "ID" in t[0] and "Reviewer severity" in t[0]), None)

    boundaries_ok = 0
    boundaries_total = 0
    if boundary_rows is None:
        schema_errors.append("security_signoff_schema_invalid: missing B1-B12 boundary coverage table")
    else:
        seen_b: list[str] = []
        for row in boundary_rows:
            bid = row.get("Boundary", "").split()[0] if row.get("Boundary") else ""
            seen_b.append(bid)
            examined = row.get("Reviewer examined", "")
            ctrl_adeq = row.get("Control adequate", "")
            tests_adeq = row.get("Tests adequate", "")
            if examined == "examined" and ctrl_adeq == "adequate" and tests_adeq == "adequate":
                boundaries_ok += 1
        boundaries_total = len(seen_b)
        if tuple(seen_b) != SRG_EXPECTED_BOUNDARIES:
            schema_errors.append(
                f"security_signoff_schema_invalid: boundary IDs {seen_b} != {list(SRG_EXPECTED_BOUNDARIES)}"
            )

    known_total = 0
    unverified_high: list[str] = []
    unrated_ids: list[str] = []
    unverified_any_ids: list[str] = []
    if finding_rows is None:
        schema_errors.append("security_signoff_schema_invalid: missing finding register table")
    else:
        seen_f: list[str] = []
        for row in finding_rows:
            fid = row.get("ID", "").strip()
            seen_f.append(fid)
            sev = row.get("Reviewer severity", "").strip()
            ver = row.get("Fix verified", "").strip()
            if sev not in _SRG_VALID_SEVERITIES:
                schema_errors.append(f"security_signoff_schema_invalid: finding {fid} invalid severity {sev!r}")
            if ver not in _SRG_VALID_VERIFIED:
                schema_errors.append(f"security_signoff_schema_invalid: finding {fid} invalid fix status {ver!r}")
            if sev == "unrated":
                unrated_ids.append(fid)
            if ver == "not-verified":
                unverified_any_ids.append(fid)
                if sev in {"critical", "high"}:
                    unverified_high.append(fid)
        known_total = sum(1 for fid in seen_f if fid in SRG_EXPECTED_KNOWN_FINDINGS)
        if tuple(seen_f[: len(SRG_EXPECTED_KNOWN_FINDINGS)]) != SRG_EXPECTED_KNOWN_FINDINGS:
            schema_errors.append(
                "security_signoff_schema_invalid: finding table does not start with W-1, F-1..F-6 in order"
            )
        for extra_id in seen_f[len(SRG_EXPECTED_KNOWN_FINDINGS) :]:
            if not _SRG_REVIEWER_FINDING_RE.match(extra_id):
                schema_errors.append(
                    f"security_signoff_schema_invalid: reviewer finding ID {extra_id!r} must match R-<n>"
                )

    if state in {"unsigned", "deferred"}:
        if decision != "pending":
            schema_errors.append(
                f"security_signoff_schema_invalid: state={state!r} requires decision='pending', got {decision!r}"
            )
        if state == "deferred" and len(deferral) < 20:
            schema_errors.append("security_signoff_schema_invalid: state='deferred' requires explicit deferral note")
        blocking_reasons.append(
            f"security_signoff_not_signed: AS-031 independent human security review is state={state!r}, "
            f"decision={decision!r} (release blocked until signed 'accepted' by an independent human reviewer)."
        )
    elif state == "signed":
        if deferral:
            schema_errors.append("security_signoff_schema_invalid: state='signed' must clear deferral text")
        if reviewer.lower() in _SRG_PLACEHOLDER_CELLS:
            schema_errors.append("security_signoff_reviewer_invalid: signed record has empty/placeholder reviewer")
        elif _SRG_AGENT_REVIEWER_RE.search(reviewer) or _SRG_AGENT_REVIEWER_RE.search(reviewer_ind):
            schema_errors.append(
                "security_signoff_reviewer_invalid: AI/self-review cannot satisfy independent human sign-off"
            )
        if reviewer_ind.lower() in _SRG_PLACEHOLDER_CELLS:
            schema_errors.append("security_signoff_reviewer_invalid: missing reviewer_independence statement")
        if not _SRG_COMMIT_SHA_RE.match(commit):
            schema_errors.append(f"security_signoff_commit_invalid: commit {commit!r} is not a 40-char lowercase SHA")
        if not _SRG_ISO_DATE_RE.match(reviewed_on):
            schema_errors.append(f"security_signoff_date_invalid: reviewed_on {reviewed_on!r} is not YYYY-MM-DD")
        if decision == "pending":
            schema_errors.append("security_signoff_schema_invalid: state='signed' cannot keep decision='pending'")
        if unrated_ids:
            schema_errors.append(f"security_signoff_unrated_findings: signed record left unrated findings {unrated_ids}")
        if unverified_any_ids and decision == "accepted":
            blocking_reasons.append(
                f"security_signoff_unverified_findings: decision='accepted' not permitted with unverified findings {unverified_any_ids}"
            )
        if unverified_high:
            blocking_reasons.append(
                f"security_signoff_unverified_high_finding: critical/high findings not verified: {unverified_high}"
            )
        if boundaries_ok != len(SRG_EXPECTED_BOUNDARIES):
            blocking_reasons.append(
                f"security_signoff_boundary_gap: only {boundaries_ok}/{len(SRG_EXPECTED_BOUNDARIES)} boundaries marked examined+adequate"
            )
        if decision != "accepted":
            blocking_reasons.append(
                f"security_signoff_not_accepted: signed security review recorded decision={decision!r}"
            )
        if expected_head_commit and _SRG_COMMIT_SHA_RE.match(expected_head_commit) and commit != expected_head_commit:
            blocking_reasons.append(
                f"security_signoff_commit_drift: signed commit {commit[:12]} does not match target {expected_head_commit[:12]}"
            )

    for err in schema_errors:
        if err not in blocking_reasons:
            blocking_reasons.insert(0, err)

    schema_valid = len(schema_errors) == 0
    passed = schema_valid and state == "signed" and decision == "accepted" and len(blocking_reasons) == 0
    return SignoffGateEvaluation(
        signoff_path=path_str,
        schema_valid=schema_valid,
        state=state,
        decision=decision,
        reviewer=reviewer,
        reviewer_independence=reviewer_ind,
        commit=commit,
        reviewed_on=reviewed_on,
        deferral=deferral,
        boundaries_total=boundaries_total,
        boundaries_examined_and_adequate=boundaries_ok,
        known_findings_total=known_total,
        unverified_high_or_critical=tuple(unverified_high),
        passed=passed,
        blocking_reasons=tuple(blocking_reasons),
        schema_errors=tuple(schema_errors),
    )


@dataclass(frozen=True)
class FaultCaseVerdict:
    """Outcome of one deterministic fault-injection, race, or adversarial test case."""

    suite: str
    case_id: str
    description: str
    passed: bool
    unauthorized_execution: bool = False
    silent_user_data_loss: bool = False
    duration_ms: float = 0.0
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "case_id": self.case_id,
            "description": self.description,
            "passed": self.passed,
            "unauthorized_execution": self.unauthorized_execution,
            "silent_user_data_loss": self.silent_user_data_loss,
            "duration_ms": round(float(self.duration_ms), 3),
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FaultSuiteSummary:
    """Aggregated summary for one required M5-P5-03 verification suite."""

    suite: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    unauthorized_executions: int
    silent_user_data_losses: int
    duration_ms: float
    cases: tuple[FaultCaseVerdict, ...]

    @property
    def passed(self) -> bool:
        return (
            self.total_cases > 0
            and self.failed_cases == 0
            and self.unauthorized_executions == 0
            and self.silent_user_data_losses == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "passed": self.passed,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "unauthorized_executions": self.unauthorized_executions,
            "silent_user_data_losses": self.silent_user_data_losses,
            "duration_ms": round(float(self.duration_ms), 3),
            "cases": [c.to_dict() for c in self.cases],
        }


def summarize_fault_cases(cases: Sequence[FaultCaseVerdict]) -> dict[str, FaultSuiteSummary]:
    """Group ``FaultCaseVerdict`` items into per-suite summaries."""
    grouped: dict[str, list[FaultCaseVerdict]] = {name: [] for name in SRG_REQUIRED_SUITES}
    for case in cases:
        grouped.setdefault(case.suite, []).append(case)
    out: dict[str, FaultSuiteSummary] = {}
    for suite_name, items in grouped.items():
        passed_n = sum(1 for c in items if c.passed)
        failed_n = len(items) - passed_n
        unauth_n = sum(1 for c in items if c.unauthorized_execution)
        loss_n = sum(1 for c in items if c.silent_user_data_loss)
        dur = sum(float(c.duration_ms) for c in items)
        out[suite_name] = FaultSuiteSummary(
            suite=suite_name,
            total_cases=len(items),
            passed_cases=passed_n,
            failed_cases=failed_n,
            unauthorized_executions=unauth_n,
            silent_user_data_losses=loss_n,
            duration_ms=dur,
            cases=tuple(items),
        )
    return out


@dataclass(frozen=True)
class MeasuredGateMetrics:
    """Measured reliability, memory, and UX responsiveness metrics for M5-P5-03 Task 2."""

    reliability: Mapping[str, Any]
    memory: Mapping[str, Any]
    ux: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reliability": dict(self.reliability),
            "memory": dict(self.memory),
            "ux": dict(self.ux),
        }


@dataclass(frozen=True)
class PrerequisiteQualificationStatus:
    """Status of external/host prerequisites and supply-chain evidence."""

    blocked_mandatory_stories: tuple[str, ...]
    incomplete_mandatory_stories: tuple[str, ...]
    hardware_qualification_recorded: bool
    hardware_metrics: Mapping[str, Any]
    supply_chain_checked: bool
    supply_chain_ready: bool
    supply_chain_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_mandatory_stories": list(self.blocked_mandatory_stories),
            "incomplete_mandatory_stories": list(self.incomplete_mandatory_stories),
            "hardware_qualification_recorded": self.hardware_qualification_recorded,
            "hardware_metrics": dict(self.hardware_metrics),
            "supply_chain_checked": self.supply_chain_checked,
            "supply_chain_ready": self.supply_chain_ready,
            "supply_chain_reasons": list(self.supply_chain_reasons),
        }


def inspect_prerequisite_qualifications(
    *,
    roadmap_path: Path | None = None,
    hardware_evidence_path: Path | None = None,
    supply_chain_dir: Path | None = None,
    ignore_story_ids: Sequence[str] = ("M5-P5-03",),
) -> PrerequisiteQualificationStatus:
    """Inspect roadmap mandatory prerequisites, hardware evidence, and supply-chain bundle."""
    ignored = set(ignore_story_ids)
    blocked_mandatory: list[str] = []
    incomplete_mandatory: list[str] = []
    if roadmap_path is not None and roadmap_path.is_file():
        try:
            data = json.loads(roadmap_path.read_text(encoding="utf-8"))
            for story in data.get("stories", []):
                if not isinstance(story, dict):
                    continue
                sid = str(story.get("id") or "")
                phase = str(story.get("phase") or "")
                raw_tracking = story.get("tracking")
                tracking = raw_tracking if isinstance(raw_tracking, Mapping) else {}
                status = str(tracking.get("status") or story.get("status") or "")
                if phase == "P6" or sid in ignored:
                    continue
                if status == "blocked":
                    blocked_mandatory.append(sid)
                elif status != "done":
                    incomplete_mandatory.append(sid)
        except (OSError, ValueError, TypeError):
            incomplete_mandatory.append("roadmap_unreadable")

    hw_recorded = False
    hw_metrics: dict[str, Any] = {}
    if hardware_evidence_path is not None and hardware_evidence_path.is_file():
        try:
            hw_text = hardware_evidence_path.read_text(encoding="utf-8")
            hw_recorded = "M5-P4-01: qualified" in hw_text or "Apple M5" in hw_text
            mem_match = re.search(r"([\d,]+)\s+bytes\s*=\s*([\d.]+)\s*GiB", hw_text)
            cold_match = re.search(r"\|\s*1 \(cold load\)\s*\|\s*([\d.]+)\s*s\s*\|\s*([\d,.]+)\s*ms", hw_text)
            warm_match = re.search(r"\|\s*3\s*\|\s*([\d.]+)\s*s\s*\|\s*([\d,.]+)\s*ms", hw_text)
            swap_match = re.search(r"growth\s+[\d,]+\s+bytes\s*\(([\d.]+)\s*GiB\)", hw_text)
            hw_metrics = {
                "target_chip": "Apple M5" if "Apple M5" in hw_text else "unknown",
                "unified_memory_bytes": int(mem_match.group(1).replace(",", "")) if mem_match else None,
                "unified_memory_gib": float(mem_match.group(2)) if mem_match else None,
                "cold_load_wall_s": float(cold_match.group(1)) if cold_match else None,
                "cold_first_token_ms": float(cold_match.group(2).replace(",", "")) if cold_match else None,
                "warm_run_wall_s": float(warm_match.group(1)) if warm_match else None,
                "warm_first_token_ms": float(warm_match.group(2).replace(",", "")) if warm_match else None,
                "model_load_swap_growth_gib": float(swap_match.group(1)) if swap_match else None,
                "thermal_telemetry_status": (
                    "blocked_non_root_macos"
                    if "| `p4.thermal` | blocked |" in hw_text
                    else "recorded"
                ),
            }
        except OSError:
            hw_recorded = False

    sc_checked = False
    sc_ready = False
    sc_reasons: list[str] = []
    if supply_chain_dir is not None:
        sc_checked = True
        ev_file = supply_chain_dir / "evidence.json"
        sig_file = supply_chain_dir / "signature.json"
        if not ev_file.is_file():
            sc_reasons.append(f"supply_chain_evidence_missing: {ev_file} not found")
        else:
            try:
                ev_data = json.loads(ev_file.read_text(encoding="utf-8"))
                gate_obj = ev_data.get("gate") if isinstance(ev_data, dict) else None
                if isinstance(gate_obj, dict) and gate_obj.get("ready") is True and sig_file.is_file():
                    sc_ready = True
                else:
                    raw_reasons = gate_obj.get("reasons", []) if isinstance(gate_obj, dict) else []
                    for r in raw_reasons:
                        sc_reasons.append(f"supply_chain_gate: {r}")
                    if not sig_file.is_file() and not sc_reasons:
                        sc_reasons.append("supply_chain_gate: signature.json is missing")
            except (OSError, ValueError, TypeError) as exc:
                sc_reasons.append(f"supply_chain_evidence_invalid: {exc}")

    return PrerequisiteQualificationStatus(
        blocked_mandatory_stories=tuple(blocked_mandatory),
        incomplete_mandatory_stories=tuple(incomplete_mandatory),
        hardware_qualification_recorded=hw_recorded,
        hardware_metrics=hw_metrics,
        supply_chain_checked=sc_checked,
        supply_chain_ready=sc_ready,
        supply_chain_reasons=tuple(sc_reasons),
    )


@dataclass(frozen=True)
class ResidualRiskEntry:
    """Explicit residual risk item documented for the release decision (Task 4)."""

    risk_id: str
    severity: str
    area: str
    summary: str
    mitigation: str
    revisit_trigger: str
    blocks_release: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "severity": self.severity,
            "area": self.area,
            "summary": self.summary,
            "mitigation": self.mitigation,
            "revisit_trigger": self.revisit_trigger,
            "blocks_release": self.blocks_release,
        }


DEFAULT_RESIDUAL_RISKS: tuple[ResidualRiskEntry, ...] = (
    ResidualRiskEntry(
        risk_id="RR-01",
        severity="high",
        area="independent-security-signoff",
        summary=(
            "docs/security-review-signoff.md (AS-031) remains state='deferred', decision='pending'; "
            "no independent human security reviewer has signed B1-B12 and W-1/F-1..F-6 yet."
        ),
        mitigation=(
            "security_release_gate fails closed with decision='release-blocked' until an independent "
            "human reviewer signs docs/security-review-signoff.md with decision='accepted'."
        ),
        revisit_trigger="Completion of independent human security review before v1.0 tag.",
        blocks_release=True,
    ),
    ResidualRiskEntry(
        risk_id="RR-02",
        severity="high",
        area="container-execution-isolation",
        summary=(
            "M5-P3-01 container isolation is UNQUALIFIED, measured: the 2026-10-31 operator run on the target "
            "host (Apple M5, Docker Desktop 29.5.3) passed 11 of 12 sandbox checks and failed "
            "p3.adversarial.memory_limit - a 3 GiB allocation completed (exit 0) under '--memory 512m' while the "
            "container's own /sys/fs/cgroup/memory.max read 536870912, so the memory cap was set but did not "
            "refuse the allocation. Whether swap let it escape the cap or the cgroup was not enforced is "
            "undecided (docs/m5-2-0-sandbox-memory-limit-finding.md); host execution mode still relies on "
            "static inspection and process containment, not OS/VM isolation."
        ),
        mitigation=(
            "STUDIO_EXECUTION_MODE defaults to 'disabled'; 'sandbox' mode fails closed when no qualified "
            "container runtime is available; dependent_execution stays blocked and the release gate blocks v1.0 "
            "until M5-P3-01 is qualified, and '--memory-swap' parity is hardened once the finding is triaged."
        ),
        revisit_trigger="Operator runs ./scripts/qualify_environment.sh with Docker/Podman and records M5-P3-01.",
        blocks_release=True,
    ),
    ResidualRiskEntry(
        risk_id="RR-03",
        severity="medium",
        area="release-artifact-signing",
        summary=(
            "CycloneDX SBOM, SLSA provenance, and secret/dependency scans are generated and verified, "
            "but no maintainer release signing key is provisioned in the repository or CI (AS-042)."
        ),
        mitigation=(
            "scripts/release_supply_chain.py gate and security_release_gate refuse 'release-ready' "
            "when signature.json is absent or mismatched."
        ),
        revisit_trigger="Maintainer signing key provisioning prior to v1.0 artifact publication.",
        blocks_release=True,
    ),
    ResidualRiskEntry(
        risk_id="RR-04",
        severity="low",
        area="hardware-thermal-telemetry",
        summary=(
            "Apple M5 24 GiB hardware qualification (M5-P4-01) measured load, TTFT, swap, and single-heavy-model "
            "admission, but macOS 26.6.1 hid non-root thermal pressure counters (p4.thermal='blocked') and "
            "per-category --coding-cases prompts were skipped."
        ),
        mitigation=(
            "memory_governor enforces conservative unified-memory admission, single heavy resident model, "
            "and swap/RSS pressure degradation without assuming thermal counters are readable."
        ),
        revisit_trigger="Elevated-privilege thermal run or full --coding-cases benchmark pass on target hardware.",
        blocks_release=False,
    ),
    ResidualRiskEntry(
        risk_id="RR-05",
        severity="low",
        area="host-mode-shell-inspection",
        summary=(
            "When an operator explicitly opts into STUDIO_EXECUTION_MODE=host, static shell classification "
            "and scrubbed environments reduce accidental leakage/destruction but cannot defeat arbitrary "
            "Turing-complete code run under the same OS user."
        ),
        mitigation=(
            "UI and docs label host mode as non-sandboxed; internal Git commands disable core.fsmonitor, "
            "core.hooksPath, protocol.file, and diff.external; managed writes enforce O_NOFOLLOW confinement."
        ),
        revisit_trigger="Container-backed sandbox qualification (M5-P3-01) becoming default execution backend.",
        blocks_release=False,
    ),
)


@dataclass(frozen=True)
class SecurityReleaseGateReport:
    """Complete M5-P5-03 fault-injection and security release gate decision record."""

    schema_version: int
    evaluated_at: float
    release_decision: str  # "release-ready" | "release-blocked"
    release_ready: bool
    acceptance_criteria: Mapping[str, bool]
    blocking_reasons: tuple[str, ...]
    suites_executed: bool
    suites: Mapping[str, FaultSuiteSummary]
    total_cases: int
    passed_cases: int
    failed_cases: int
    unauthorized_executions: int
    silent_user_data_losses: int
    metrics: MeasuredGateMetrics
    signoff: SignoffGateEvaluation
    prerequisites: PrerequisiteQualificationStatus
    residual_risks: tuple[ResidualRiskEntry, ...]

    @property
    def gate_invariants_satisfied(self) -> bool:
        """True when all code/test invariants hold and release is either signed-ready or properly blocked."""
        return bool(
            self.acceptance_criteria.get("no_critical_unauthorized_execution_paths")
            and self.acceptance_criteria.get("no_silent_user_data_loss")
            and self.acceptance_criteria.get("security_signoff_recorded_or_release_blocked")
            and self.signoff.schema_valid
            and (not self.suites_executed or self.failed_cases == 0)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": self.evaluated_at,
            "release_decision": self.release_decision,
            "release_ready": self.release_ready,
            "gate_invariants_satisfied": self.gate_invariants_satisfied,
            "acceptance_criteria": dict(self.acceptance_criteria),
            "blocking_reasons": list(self.blocking_reasons),
            "suites_executed": self.suites_executed,
            "summary": {
                "total_cases": self.total_cases,
                "passed_cases": self.passed_cases,
                "failed_cases": self.failed_cases,
                "unauthorized_executions": self.unauthorized_executions,
                "silent_user_data_losses": self.silent_user_data_losses,
            },
            "suites": {k: v.to_dict() for k, v in self.suites.items()},
            "metrics": self.metrics.to_dict(),
            "signoff": self.signoff.to_dict(),
            "prerequisites": self.prerequisites.to_dict(),
            "residual_risks": [r.to_dict() for r in self.residual_risks],
        }


def evaluate_release_gate(
    *,
    signoff: SignoffGateEvaluation,
    prerequisites: PrerequisiteQualificationStatus,
    cases: Sequence[FaultCaseVerdict] = (),
    suites_executed: bool = True,
    metrics: MeasuredGateMetrics | None = None,
    residual_risks: Sequence[ResidualRiskEntry] = DEFAULT_RESIDUAL_RISKS,
    now: float | None = None,
) -> SecurityReleaseGateReport:
    """Combine executed suite verdicts, metrics, prerequisites, and sign-off into a release decision."""
    ts = float(time.time() if now is None else now)
    suite_map = summarize_fault_cases(cases)
    total_cases = sum(s.total_cases for s in suite_map.values())
    passed_cases = sum(s.passed_cases for s in suite_map.values())
    failed_cases = sum(s.failed_cases for s in suite_map.values())
    unauth_execs = sum(s.unauthorized_executions for s in suite_map.values())
    data_losses = sum(s.silent_user_data_losses for s in suite_map.values())

    blocking_reasons: list[str] = []

    if not suites_executed:
        blocking_reasons.append(
            "fault_suites_not_executed: fault-injection and adversarial suites were skipped in status-only check"
        )
    else:
        for req_suite in SRG_REQUIRED_SUITES:
            summary = suite_map.get(req_suite)
            if summary is None or summary.total_cases == 0:
                blocking_reasons.append(f"missing_required_suite: suite {req_suite!r} produced 0 cases")
            elif not summary.passed:
                blocking_reasons.append(
                    f"suite_failed:{req_suite}: {summary.failed_cases} failed, "
                    f"{summary.unauthorized_executions} unauthorized_executions, "
                    f"{summary.silent_user_data_losses} silent_user_data_losses"
                )

    if unauth_execs > 0:
        blocking_reasons.append(
            f"critical_unauthorized_execution_detected: {unauth_execs} unauthorized execution path(s) observed"
        )
    if data_losses > 0:
        blocking_reasons.append(
            f"silent_user_data_loss_detected: {data_losses} silent user-data loss event(s) observed"
        )

    for reason in signoff.blocking_reasons:
        blocking_reasons.append(reason)

    if prerequisites.blocked_mandatory_stories:
        blocking_reasons.append(
            "mandatory_qualification_blocked: roadmap stories still blocked: "
            + ", ".join(prerequisites.blocked_mandatory_stories)
        )
    if prerequisites.incomplete_mandatory_stories:
        blocking_reasons.append(
            "mandatory_stories_incomplete: roadmap stories not yet done: "
            + ", ".join(prerequisites.incomplete_mandatory_stories)
        )
    if prerequisites.supply_chain_checked and not prerequisites.supply_chain_ready:
        for sc_reason in prerequisites.supply_chain_reasons:
            blocking_reasons.append(sc_reason)

    release_ready = len(blocking_reasons) == 0
    release_decision = "release-ready" if release_ready else "release-blocked"

    if suites_executed:
        ac1_ok = (
            unauth_execs == 0
            and suite_map["sandbox_adversarial"].total_cases > 0
            and suite_map["sandbox_adversarial"].passed
        )
        ac2_ok = (
            data_losses == 0
            and suite_map["crash_matrix"].total_cases > 0
            and suite_map["crash_matrix"].passed
            and suite_map["user_edit_races"].total_cases > 0
            and suite_map["user_edit_races"].passed
            and suite_map["external_write_uncertainty"].total_cases > 0
            and suite_map["external_write_uncertainty"].passed
        )
    else:
        ac1_ok = unauth_execs == 0
        ac2_ok = data_losses == 0

    ac3_ok = signoff.schema_valid and (
        (signoff.passed and release_decision == "release-ready")
        or (not signoff.passed and release_decision == "release-blocked" and len(blocking_reasons) > 0)
        or (signoff.passed and release_decision == "release-blocked" and len(blocking_reasons) > 0)
    )

    effective_metrics = metrics or MeasuredGateMetrics(
        reliability={
            "suites_executed": suites_executed,
            "total_cases": total_cases,
            "passed_cases": passed_cases,
            "pass_rate": round(passed_cases / total_cases, 4) if total_cases else None,
            "unauthorized_executions": unauth_execs,
            "silent_user_data_losses": data_losses,
        },
        memory={
            "process_peak_rss_bytes": _srg_current_peak_rss_bytes(),
            "process_peak_rss_mib": round(_srg_current_peak_rss_bytes() / (1024 * 1024), 2),
            "hardware_qualification": dict(prerequisites.hardware_metrics),
        },
        ux={},
    )

    return SecurityReleaseGateReport(
        schema_version=SRG_SCHEMA_VERSION,
        evaluated_at=ts,
        release_decision=release_decision,
        release_ready=release_ready,
        acceptance_criteria={
            "no_critical_unauthorized_execution_paths": ac1_ok,
            "no_silent_user_data_loss": ac2_ok,
            "security_signoff_recorded_or_release_blocked": ac3_ok,
        },
        blocking_reasons=tuple(blocking_reasons),
        suites_executed=suites_executed,
        suites=suite_map,
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        unauthorized_executions=unauth_execs,
        silent_user_data_losses=data_losses,
        metrics=effective_metrics,
        signoff=signoff,
        prerequisites=prerequisites,
        residual_risks=tuple(residual_risks),
    )


def render_release_gate_markdown(report: SecurityReleaseGateReport) -> str:
    """Render a concise, human-auditable Markdown summary of a gate evaluation."""
    lines: list[str] = [
        "# M5-P5-03 Fault-Injection and Security Release Gate Report",
        "",
        f"- **Release decision:** `{report.release_decision}` (`release_ready={report.release_ready}`)",
        f"- **Gate invariants satisfied:** `{report.gate_invariants_satisfied}`",
        f"- **Sign-off state / decision:** `{report.signoff.state}` / `{report.signoff.decision}`",
        f"- **Executed cases:** `{report.passed_cases}/{report.total_cases}` passed "
        f"(`unauthorized_executions={report.unauthorized_executions}`, "
        f"`silent_user_data_losses={report.silent_user_data_losses}`)",
        "",
        "## Acceptance Criteria",
        "",
        "| Criterion | Satisfied |",
        "|---|---|",
    ]
    for key, val in report.acceptance_criteria.items():
        lines.append(f"| `{key}` | `{'yes' if val else 'no'}` |")

    lines.extend(["", "## Blocking Reasons", ""])
    if report.blocking_reasons:
        for reason in report.blocking_reasons:
            lines.append(f"- `{reason}`")
    else:
        lines.append("- None (`release-ready`).")

    lines.extend(
        [
            "",
            "## Suite Summary",
            "",
            "| Suite | Cases | Passed | Failed | Unauthorized Exec | Silent Data Loss | Duration (ms) |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for sname in SRG_REQUIRED_SUITES:
        s = report.suites.get(sname)
        if s is not None:
            lines.append(
                f"| `{s.suite}` | {s.total_cases} | {s.passed_cases} | {s.failed_cases} | "
                f"{s.unauthorized_executions} | {s.silent_user_data_losses} | {s.duration_ms:.1f} |"
            )

    lines.extend(
        [
            "",
            "## Residual Risks",
            "",
            "| ID | Severity | Area | Blocks Release | Summary |",
            "|---|---|---|---|---|",
        ]
    )
    for risk in report.residual_risks:
        lines.append(
            f"| `{risk.risk_id}` | `{risk.severity}` | `{risk.area}` | "
            f"`{'yes' if risk.blocks_release else 'no'}` | {risk.summary} |"
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_RESIDUAL_RISKS",
    "FaultCaseVerdict",
    "FaultSuiteSummary",
    "MeasuredGateMetrics",
    "PrerequisiteQualificationStatus",
    "ResidualRiskEntry",
    "SRG_EXPECTED_BOUNDARIES",
    "SRG_EXPECTED_KNOWN_FINDINGS",
    "SRG_REQUIRED_SUITES",
    "SRG_SCHEMA_VERSION",
    "SecurityReleaseGateReport",
    "SignoffGateEvaluation",
    "evaluate_release_gate",
    "evaluate_security_signoff_gate",
    "inspect_prerequisite_qualifications",
    "render_release_gate_markdown",
    "summarize_fault_cases",
]
