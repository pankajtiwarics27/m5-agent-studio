"""M5-P5-03 Fault-injection and security release gate regression tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app as studio
import child_delegation
import execution_budget
import mcp_client
import security_release_gate as srg
import workspace_quarantine
import workspace_snapshots

ROOT = Path(__file__).resolve().parents[1]
SIGNOFF_DOC = ROOT / "docs" / "security-review-signoff.md"
ROADMAP_JSON = ROOT / "product" / "M5_2_0_Autonomous_Roadmap.json"
HW_DOC = ROOT / "docs" / "m5-2-0-hardware-qualification-evidence.md"
GATE_CLI = ROOT / "scripts" / "security_release_gate.py"
RELEASE_CLI = ROOT / "scripts" / "release.py"


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    env = studio._git_subprocess_env()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), env=env, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), env=env, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), env=env, check=True)
    (repo / "src.py").write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=str(repo), env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(repo), env=env, check=True)


def _build_signed_signoff_markdown(
    *,
    decision: str = "accepted",
    reviewer: str = "Dr. Priya Nair (@pnair-sec)",
    reviewer_independence: str = "Independent external security consultant; zero authorship on repository.",
    commit: str = "a" * 40,
    reviewed_on: str = "2026-10-31",
    unverified_f1: bool = False,
    unexamined_b4: bool = False,
) -> str:
    boundary_lines: list[str] = []
    for idx in range(1, 13):
        bid = f"B{idx}"
        if unexamined_b4 and bid == "B4":
            boundary_lines.append(f"| {bid} boundary | not-examined | not-assessed | not-assessed | no |")
        else:
            boundary_lines.append(f"| {bid} boundary | examined | adequate | adequate | no |")

    finding_lines: list[str] = []
    for fid in srg.SRG_EXPECTED_KNOWN_FINDINGS:
        if unverified_f1 and fid == "F-1":
            finding_lines.append(f"| {fid} | desc | rem | impact | high | not-verified |")
        else:
            finding_lines.append(f"| {fid} | desc | rem | impact | medium | verified |")

    return (
        "# AS-031 independent security review — sign-off record\n\n"
        "<!-- as031-signoff\n"
        "state: signed\n"
        f'reviewer: "{reviewer}"\n'
        f'reviewer_independence: "{reviewer_independence}"\n'
        f'commit: "{commit}"\n'
        f'reviewed_on: "{reviewed_on}"\n'
        f"decision: {decision}\n"
        'deferral: ""\n'
        "-->\n\n"
        "| Boundary | Reviewer examined | Control adequate | Tests adequate | Residual risk noted |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(boundary_lines)
        + "\n\n"
        "| ID | Finding (author description) | Author remediation | Author impact sketch (non-binding) | Reviewer severity | Fix verified |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(finding_lines)
        + "\n"
    )


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}


# ---------------------------------------------------------------------------
# 1. Full executed crash matrix, user-edit races, MCP uncertainty & adversarial suite
# ---------------------------------------------------------------------------


def test_full_fault_injection_and_adversarial_suite_passes_and_blocks_deferred_release() -> None:
    report = asyncio.run(
        studio.run_security_release_gate_async(
            signoff_path=SIGNOFF_DOC,
            roadmap_path=ROADMAP_JSON,
            hardware_evidence_path=HW_DOC,
        )
    )

    assert report.suites_executed is True
    assert report.total_cases == 13
    assert report.passed_cases == 13
    assert report.failed_cases == 0
    assert report.unauthorized_executions == 0
    assert report.silent_user_data_losses == 0
    assert report.gate_invariants_satisfied is True

    # All 3 M5-P5-03 acceptance criteria hold (AC3 holds via fail-closed release-blocked)
    assert report.acceptance_criteria == {
        "no_critical_unauthorized_execution_paths": True,
        "no_silent_user_data_loss": True,
        "security_signoff_recorded_or_release_blocked": True,
    }

    # Because docs/security-review-signoff.md is deferred/pending, release MUST be blocked!
    assert report.release_ready is False
    assert report.release_decision == "release-blocked"
    assert any("security_signoff_not_signed" in r for r in report.blocking_reasons)

    # Measured reliability, memory, and UX metrics populated
    metrics = report.metrics.to_dict()
    assert metrics["reliability"]["pass_rate"] == 1.0
    assert metrics["reliability"]["crash_recovery_sweep_max_ms"] >= 0.0
    assert metrics["memory"]["peak_rss_after_bytes"] > 0
    assert metrics["memory"]["hardware_qualification"]["target_chip"] == "Apple M5"
    assert metrics["memory"]["hardware_qualification"]["unified_memory_gib"] == 24.0
    assert metrics["ux"]["sse_cursor_replay_exact"] is True


# ---------------------------------------------------------------------------
# 2. Sign-off & release-gate state matrix (AC 3)
# ---------------------------------------------------------------------------


def test_signoff_gate_blocks_missing_or_malformed_signoff_files(tmp_path: Path) -> None:
    missing = srg.evaluate_security_signoff_gate(tmp_path / "absent.md")
    assert missing.schema_valid is False
    assert missing.passed is False
    assert any("security_signoff_missing" in r for r in missing.blocking_reasons)

    malformed = tmp_path / "malformed.md"
    malformed.write_text("# Broken signoff without metadata comment\n", encoding="utf-8")
    bad_eval = srg.evaluate_security_signoff_gate(malformed)
    assert bad_eval.schema_valid is False
    assert bad_eval.passed is False
    assert any("security_signoff_schema_invalid" in r for r in bad_eval.blocking_reasons)


def test_signoff_gate_rejects_self_signed_ai_agent_reviewer(tmp_path: Path) -> None:
    doc = tmp_path / "agent_signed.md"
    doc.write_text(
        _build_signed_signoff_markdown(
            decision="accepted",
            reviewer="Claude Autonomous Agent",
            reviewer_independence="AI self-review during CI",
        ),
        encoding="utf-8",
    )
    ev = srg.evaluate_security_signoff_gate(doc)
    assert ev.schema_valid is False
    assert ev.passed is False
    assert any("security_signoff_reviewer_invalid" in r for r in ev.blocking_reasons)


def test_signoff_gate_blocks_unverified_high_finding_boundary_gap_and_commit_drift(tmp_path: Path) -> None:
    f1_doc = tmp_path / "unverified_high.md"
    f1_doc.write_text(_build_signed_signoff_markdown(decision="accepted", unverified_f1=True), encoding="utf-8")
    ev_f1 = srg.evaluate_security_signoff_gate(f1_doc)
    assert ev_f1.passed is False
    assert "F-1" in ev_f1.unverified_high_or_critical
    assert any("security_signoff_unverified" in r for r in ev_f1.blocking_reasons)

    b4_doc = tmp_path / "boundary_gap.md"
    b4_doc.write_text(_build_signed_signoff_markdown(decision="accepted", unexamined_b4=True), encoding="utf-8")
    ev_b4 = srg.evaluate_security_signoff_gate(b4_doc)
    assert ev_b4.passed is False
    assert any("security_signoff_boundary_gap" in r for r in ev_b4.blocking_reasons)

    drift_doc = tmp_path / "drift.md"
    drift_doc.write_text(_build_signed_signoff_markdown(decision="accepted", commit="a" * 40), encoding="utf-8")
    ev_drift = srg.evaluate_security_signoff_gate(drift_doc, expected_head_commit="b" * 40)
    assert ev_drift.passed is False
    assert any("security_signoff_commit_drift" in r for r in ev_drift.blocking_reasons)


def test_release_gate_opens_only_when_signoff_accepted_prerequisites_done_and_supply_chain_signed(
    tmp_path: Path,
) -> None:
    signed_doc = tmp_path / "signed_accepted.md"
    commit_sha = "c" * 40
    signed_doc.write_text(
        _build_signed_signoff_markdown(decision="accepted", commit=commit_sha),
        encoding="utf-8",
    )
    roadmap_done = tmp_path / "roadmap_all_done.json"
    roadmap_done.write_text(
        json.dumps(
            {
                "stories": [
                    {"id": "M5-P3-01", "phase": "P3", "tracking": {"status": "done"}},
                    {"id": "M5-P4-01", "phase": "P4", "tracking": {"status": "done"}},
                    {"id": "M5-P5-03", "phase": "P5", "tracking": {"status": "done"}},
                    {"id": "M5-P6-01", "phase": "P6", "tracking": {"status": "not_started"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    sc_dir = tmp_path / "evidence"
    sc_dir.mkdir()
    (sc_dir / "evidence.json").write_text(
        json.dumps({"gate": {"ready": True, "reasons": []}}),
        encoding="utf-8",
    )
    (sc_dir / "signature.json").write_text(
        json.dumps({"digest": "d" * 64, "signature": "c2ln", "signer": "rel-key"}),
        encoding="utf-8",
    )

    report = asyncio.run(
        studio.run_security_release_gate_async(
            signoff_path=signed_doc,
            roadmap_path=roadmap_done,
            hardware_evidence_path=HW_DOC,
            supply_chain_dir=sc_dir,
            expected_head_commit=commit_sha,
        )
    )
    assert report.release_ready is True
    assert report.release_decision == "release-ready"
    assert report.blocking_reasons == ()
    assert all(report.acceptance_criteria.values())


# ---------------------------------------------------------------------------
# 3. Hardened call-path regressions (Git fsmonitor/hooks, worktree apply, .sdlc)
# ---------------------------------------------------------------------------


def test_internal_git_helpers_and_snapshots_neutralize_fsmonitor_hooks_and_external_diff(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "untrusted_repo"
    _init_git_repo(repo)
    marker = tmp_path / "executed.marker"
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\necho executed > {marker}\n", encoding="utf-8")
    evil.chmod(0o755)

    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho executed > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)

    subprocess.run(["git", "config", "core.fsmonitor", str(evil)], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "diff.external", str(evil)], cwd=str(repo), check=True)
    (repo / "src.py").write_text("X = 2\n", encoding="utf-8")

    outside = tmp_path / "secret_host_file.txt"
    outside.write_text("HOST_SECRET_TOKEN_XYZ\n", encoding="utf-8")
    (repo / "leak_symlink.txt").symlink_to(outside)
    (repo / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-secretvalue1234567890\n", encoding="utf-8")

    info = studio._inspect_git_repo(repo)
    assert info["is_git"] is True

    diff_res = studio._compute_git_diff(repo)
    assert "HOST_SECRET_TOKEN_XYZ" not in diff_res["diff"]
    assert "sk-ant-api03-secretvalue1234567890" not in diff_res["diff"]

    wt_dir, branch, _ = studio._create_harness_worktree("hns-secgate01", repo)
    snap_store = workspace_snapshots.WorkspaceSnapshotStore(tmp_path / "snap_store")
    snap_store.capture_baseline("sess-sec-1", repo, label="baseline")
    studio._remove_harness_worktree(repo, wt_dir, branch)

    assert not marker.exists(), "Untrusted Git fsmonitor/diff.external/post-checkout hook executed on host!"


def test_apply_worktree_refuses_plan_policy_quarantine_unjoined_child_and_unresolved_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_store = tmp_path / "harness_store"
    iso_store.mkdir(parents=True)
    iso_db = tmp_path / "studio_test.db"
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", iso_store)
    monkeypatch.setattr(studio, "DB_PATH", iso_db)
    monkeypatch.setattr(studio, "_CHILD_DELEGATION_STORE", None)
    monkeypatch.setattr(studio, "_MCP_EFFECT_STORE", None)
    studio.init_db()

    repo = tmp_path / "ws"
    _init_git_repo(repo)
    wt_dir, branch, _ = studio._create_harness_worktree("hns-applyguard", repo)
    (wt_dir / "src.py").write_text("X = 42\n", encoding="utf-8")

    # 1. Read-only 'plan' mode -> PermissionError
    with pytest.raises(PermissionError, match="plan"):
        studio._apply_worktree_to_main(
            {"id": "sess-plan", "policy": "plan", "workspace_dir": str(repo), "worktree_dir": str(wt_dir)}
        )

    # 2. Quarantined workspace -> conflict with workspace_quarantined
    q_manifest = workspace_quarantine.freeze_workspace(
        repo,
        store_root=iso_store,
        reason="snapshot_hash_mismatch",
        actor="test",
    )
    q_res = studio._apply_worktree_to_main(
        {"id": "sess-q", "policy": "standard", "workspace_dir": str(repo), "worktree_dir": str(wt_dir)}
    )
    assert q_res["status"] == "conflict"
    assert q_res["error_code"] == "workspace_quarantined"
    workspace_quarantine.release_quarantine(iso_store, q_manifest.quarantine_id, note="cleared for test")

    # 3. Unjoined child delegation -> conflict with unjoined_child_delegations
    del_store = studio.get_child_delegation_store()
    del_store.register_spawn(
        child_delegation.ChildDelegationRecord(
            delegation_id="del-unjoined-1",
            child_task_id="child-unjoined-1",
            parent_task_id="sess-parent",
            session_id="sess-parent",
            parent_epoch_at_spawn=1,
            child_epoch=1,
            child_worker_id="wrk-child-1",
            role="verifier",
            objective="verify patch",
            model_spec="Gemma-4-21B-REAP-4bit",
            status="running",
            authority=child_delegation.ChildAuthoritySubset(
                parent_policy="standard",
                policy="plan",
                allowed_tools=("read_file",),
                capabilities=("workspace_read",),
                readable_paths=(".",),
                writable_paths=(),
            ),
            budget_allocation=child_delegation.ChildBudgetAllocation(
                spec=execution_budget.TaskBudgetSpec(
                    max_steps=2,
                    max_tool_calls=4,
                    max_tokens=500,
                    max_elapsed_s=10.0,
                ),
                parent_remaining_steps_at_spawn=10,
                parent_remaining_tool_calls_at_spawn=20,
                parent_remaining_tokens_at_spawn=4000,
                parent_remaining_elapsed_s_at_spawn=60.0,
            ),
            justification={"reason": "independent_verify"},
            base_file_versions={},
            post_file_versions={},
        )
    )
    child_res = studio._apply_worktree_to_main(
        {"id": "sess-parent", "policy": "standard", "workspace_dir": str(repo), "worktree_dir": str(wt_dir)}
    )
    assert child_res["status"] == "conflict"
    assert child_res["error_code"] == "unjoined_child_delegations"

    # 4. Uncertain MCP external effect -> conflict with unresolved_consequential_effects
    eff_store = studio.get_mcp_effect_store()
    sem = mcp_client.McpToolSemantics(
        alias="ext",
        tool_name="mcp__ext__mutate",
        transport="http",
        read_only=False,
        destructive=True,
        idempotency_supported=False,
        idempotency_status="unsupported",
        idempotency_mechanism="none",
    )
    eff_store.stage_dispatch(
        action_id="act-unc-apply",
        session_id="sess-mcp-hold",
        task_id="sess-mcp-hold",
        epoch=1,
        semantics=sem,
        arguments={"k": "v"},
        bound_idempotency_key=None,
    )
    eff_store.sweep_orphaned_in_flight(session_id="sess-mcp-hold")
    mcp_res = studio._apply_worktree_to_main(
        {"id": "sess-mcp-hold", "policy": "standard", "workspace_dir": str(repo), "worktree_dir": str(wt_dir)}
    )
    assert mcp_res["status"] == "conflict"
    assert mcp_res["error_code"] == "unresolved_consequential_effects"
    assert (repo / "src.py").read_text(encoding="utf-8") == "X = 1\n"

    studio._remove_harness_worktree(repo, wt_dir, branch)


def test_sdlc_confined_helpers_reject_symlinks_and_quarantined_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_store = tmp_path / "harness_store"
    iso_store.mkdir(parents=True)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", iso_store)

    ws = tmp_path / "ws_sdlc"
    ws.mkdir()
    outside = tmp_path / "outside_target"
    outside.mkdir()

    # Symlinked .sdlc directory must be rejected
    (ws / ".sdlc").symlink_to(outside)
    with pytest.raises(PermissionError, match="symlink"):
        studio._ensure_sdlc_dir(ws)
    (ws / ".sdlc").unlink()

    sdlc_dir = studio._ensure_sdlc_dir(ws)
    studio._write_sdlc_backlog(sdlc_dir, "Ship safely", [{"id": "S-1", "passes": True}])
    stories = studio._read_sdlc_backlog(sdlc_dir)
    assert len(stories) == 1 and stories[0]["id"] == "S-1"

    # Symlinked backlog.json inside .sdlc must be rejected on write and ignored on read
    (sdlc_dir / "backlog.json").unlink()
    victim = outside / "victim.json"
    victim.write_text("KEEP_ME", encoding="utf-8")
    (sdlc_dir / "backlog.json").symlink_to(victim)
    assert studio._read_sdlc_backlog(sdlc_dir) == []
    with pytest.raises(PermissionError):
        studio._write_sdlc_backlog(sdlc_dir, "Escape", [])
    assert victim.read_text(encoding="utf-8") == "KEEP_ME"


# ---------------------------------------------------------------------------
# 4. CLI & HTTP Endpoint Contracts
# ---------------------------------------------------------------------------


def test_security_release_gate_cli_and_release_py_gate_enforce_block_vs_ready(tmp_path: Path) -> None:
    # Default verify-invariants mode exits 0 on current repo (gate_invariants_satisfied=True)
    proc_verify = subprocess.run(
        [sys.executable, str(GATE_CLI), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_verify.returncode == 0, proc_verify.stderr
    data = json.loads(proc_verify.stdout)
    assert data["gate_invariants_satisfied"] is True
    assert data["release_decision"] == "release-blocked"

    # --enforce mode exits 1 because sign-off is deferred & M5-P3-01 is blocked
    proc_enforce = subprocess.run(
        [sys.executable, str(GATE_CLI), "--enforce", "--skip-fault-suite", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_enforce.returncode == 1

    # release.py package --require-release-gate fails closed with exit 1
    proc_pkg = subprocess.run(
        [
            sys.executable,
            str(RELEASE_CLI),
            "package",
            "--version",
            "9.0.1",
            "--artifact",
            str(ROOT / "studio.py"),
            "--output",
            str(tmp_path / "dist_rel"),
            "--require-release-gate",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_pkg.returncode == 1
    assert "Security release gate blocked packaging" in proc_pkg.stderr


def test_residual_risk_02_reports_the_measured_memory_cap_finding_it_blocks_on() -> None:
    """The isolation residual risk must state what was measured, not that nothing was.

    M5-P3-01 was qualified once on the target host and failed one check, so a
    summary saying qualification "has not been recorded" is wrong and hides the
    specific failure an operator needs to see when reading a blocked release
    decision. The gate must keep blocking, and its claims must be backed by the
    committed finding rather than drifting away from it.
    """
    risks = {r.risk_id: r for r in srg.DEFAULT_RESIDUAL_RISKS}
    rr02 = risks["RR-02"]
    assert rr02.blocks_release is True
    assert rr02.severity == "high"

    summary = rr02.summary
    assert "has not been recorded on the target host" not in summary
    assert "UNQUALIFIED" in summary
    assert "p3.adversarial.memory_limit" in summary
    assert "3 GiB" in summary
    assert "536870912" in summary
    assert "memory.max" in summary
    assert "m5-2-0-sandbox-memory-limit-finding.md" in summary
    assert "undecided" in summary, "an untriaged finding must stay untriaged"

    finding = (ROOT / "docs" / "m5-2-0-sandbox-memory-limit-finding.md").read_text(encoding="utf-8")
    for literal in ("p3.adversarial.memory_limit", "536870912", "3 GiB", "exit 0"):
        assert literal in finding, f"gate cites {literal!r} but the finding does not record it"

    gate_doc = (ROOT / "docs" / "m5-2-0-security-release-gate.md").read_text(encoding="utf-8")
    rr02_row = [line for line in gate_doc.splitlines() if line.startswith("| `RR-02`")]
    assert len(rr02_row) == 1
    assert "m5-2-0-sandbox-memory-limit-finding.md" in rr02_row[0]
    assert "p3.adversarial.memory_limit" in rr02_row[0]


def test_http_release_gate_endpoints_and_standalone_studio_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_store = tmp_path / "http_store"
    iso_store.mkdir(parents=True)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", iso_store)
    monkeypatch.setattr(studio, "DB_PATH", tmp_path / "http_studio.db")

    with TestClient(studio.app, base_url="http://127.0.0.1:8080") as client:
        r_status = client.get("/api/harness/release-gate", headers=_auth_headers())
        assert r_status.status_code == 200, r_status.text
        status_json = r_status.json()
        assert status_json["release_decision"] == "release-blocked"
        assert status_json["signoff"]["state"] == "deferred"

        r_eval = client.post(
            "/api/harness/release-gate/evaluate",
            json={"require_head_commit": False},
            headers=_auth_headers(),
        )
        assert r_eval.status_code == 200, r_eval.text
        eval_json = r_eval.json()
        assert eval_json["suites_executed"] is True
        assert eval_json["summary"]["passed_cases"] == 13
        assert eval_json["summary"]["unauthorized_executions"] == 0
        assert eval_json["summary"]["silent_user_data_losses"] == 0
        assert eval_json["release_decision"] == "release-blocked"

    # Verify standalone studio.py in isolated directory exports security_release_gate
    iso_dir = tmp_path / "standalone_check"
    iso_dir.mkdir()
    iso_studio = iso_dir / "studio.py"
    iso_studio.write_bytes((ROOT / "studio.py").read_bytes())
    spec = importlib.util.spec_from_file_location("studio_gate_isolated", iso_studio)
    assert spec is not None and spec.loader is not None
    mod: Any = importlib.util.module_from_spec(spec)
    sys.modules["studio_gate_isolated"] = mod
    try:
        spec.loader.exec_module(mod)
        assert hasattr(mod, "run_security_release_gate_async")
        assert hasattr(mod, "evaluate_release_gate")
    finally:
        sys.modules.pop("studio_gate_isolated", None)
