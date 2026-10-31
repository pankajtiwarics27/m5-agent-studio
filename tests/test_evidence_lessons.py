"""End-to-end verification for M5-P6-01: Evidence-backed lessons and bounded retrieval.

Covers all 4 implementation tasks and all 3 acceptance criteria:
- Task 1: Define provenance and promotion gates (verified task, spec digest, trust,
  confidentiality, secret/host-path screening, confined file SHA-256 anchors)
- Task 2: Decision-boundary retrieval (`turn_plan_start`, `verification_replan`,
  `handoff_switch`) and hard token ceilings (`160` default, `256` max)
- Task 3: Invalidate stale lessons (anchored file mutation/deletion/symlink swap,
  security generation advance, artifact lineage invalidation, consecutive regressions)
- Task 4: A/B test coding completion across real isolated workspaces and verification specs
- AC 1: Measured improvement over baseline (`ab_gate_passed == True`)
- AC 2: No cross-project privacy leakage (`cross_project_leak_count == 0`, strict scope isolation)
- AC 3: Optional feature can be disabled (env var, runtime config, per-turn flag, fail-closed gate)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import pytest

import app as studio
import artifact_lineage
import evidence_lessons as el

AUTH_TOKEN = "l" * 40


def _valid_prov(
    *,
    verified: bool = True,
    spec_sha: str = "a" * 64,
    passed_checks: int = 2,
    source_trust: str = "verified_task",
    confidentiality: str = "internal",
    security_generation: int = 1,
    lineage_evidence_id: str = "",
) -> el.LessonProvenance:
    return el.LessonProvenance(
        source_session_id="sess-origin-1",
        source_task_id="task-origin-1",
        source_epoch=1,
        verified=verified,
        verification_spec_sha256=spec_sha,
        passed_checks=passed_checks,
        source_trust=source_trust,
        confidentiality=confidentiality,
        security_generation=security_generation,
        source_revision="rev-1",
        lineage_evidence_id=lineage_evidence_id,
    )


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "repo_alpha"
    (ws / "pkg").mkdir(parents=True, exist_ok=True)
    (ws / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "pkg" / "conventions.py").write_text(
        "ZERO_DIVISOR_SENTINEL = -1.0\nROUND_DIGITS = 4\n",
        encoding="utf-8",
    )
    (ws / "pkg" / "metrics.py").write_text(
        "def compute_ratio(num: float, den: float) -> float:\n"
        "    return num / den\n",
        encoding="utf-8",
    )
    (ws / "pkg" / "check_metrics.py").write_text(
        "import pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "from pkg.metrics import compute_ratio\n"
        "assert compute_ratio(1, 3) == 0.3333\n"
        "assert compute_ratio(5, 0) == -1.0\n"
        "print('METRICS_VERIFIED_OK')\n",
        encoding="utf-8",
    )

    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "studio_lessons_test.db"

    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", store_dir)
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(studio, "STUDIO_AUTH_DISABLED", False)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    monkeypatch.delenv("STUDIO_ENABLE_EVIDENCE_LESSONS", raising=False)

    studio._reset_evidence_lesson_store(None)
    studio._reset_child_delegation_store(None)
    studio._reset_portable_handoff_store(None)
    studio._reset_scoped_grant_store(None)
    studio._reset_mcp_effect_store(None)
    studio._reset_process_supervisor(None)
    studio._reset_heavy_model_admission(
        studio.model_admission.HeavyModelAdmission(
            studio.model_admission.HeavyModelLimits(
                memory_bytes=24 * 1024**3,
                request_bytes=18 * 1024**3,
                slots=1,
            )
        )
    )
    studio.init_db()
    studio._set_default_projects_dir(str(ws), create_if_missing=True)
    return ws


def _client() -> TestClient:
    return TestClient(
        studio.app,
        base_url="http://127.0.0.1:8080",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    cur = ""
    for line in raw.splitlines():
        if line.startswith("event: "):
            cur = line[len("event: ") :].strip()
        elif line.startswith("data: ") and cur:
            events.append((cur, json.loads(line[len("data: ") :])))
            cur = ""
    return events


def test_promotion_gates_reject_unverified_untrusted_secret_and_unanchored_lessons(
    tmp_path: Path,
) -> None:
    """Task 1: Candidate lessons fail closed unless verified, trusted, secret-free, and file-anchored."""
    store = el.EvidenceLessonStore(tmp_path / "store")
    ws = tmp_path / "project"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "config.py").write_text("MAX_RETRIES = 3\n", encoding="utf-8")

    # 1. Unverified task cannot promote.
    with pytest.raises(el.LessonPromotionRejectedError) as exc_unver:
        store.promote_lesson(
            workspace_root=ws,
            topic_key="retry-cap",
            title="Retry bound",
            guidance="Cap retries at MAX_RETRIES in src/config.py.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(verified=False),
        )
    assert exc_unver.value.code == "unverified_task_cannot_promote"

    # 2. Missing / malformed 64-hex verification spec digest or passed_checks < 1.
    with pytest.raises(el.LessonPromotionRejectedError) as exc_spec:
        store.promote_lesson(
            workspace_root=ws,
            topic_key="retry-cap",
            title="Retry bound",
            guidance="Cap retries at MAX_RETRIES in src/config.py.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(spec_sha="not-a-sha256"),
        )
    assert exc_spec.value.code == "missing_verification_evidence"

    # 3. Untrusted source or secret confidentiality rejected.
    with pytest.raises(el.LessonPromotionRejectedError) as exc_trust:
        store.promote_lesson(
            workspace_root=ws,
            topic_key="retry-cap",
            title="Retry bound",
            guidance="Cap retries at MAX_RETRIES in src/config.py.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(source_trust="untrusted_external"),
        )
    assert exc_trust.value.code == "untrusted_provenance"

    with pytest.raises(el.LessonPromotionRejectedError) as exc_conf:
        store.promote_lesson(
            workspace_root=ws,
            topic_key="retry-cap",
            title="Retry bound",
            guidance="Cap retries at MAX_RETRIES in src/config.py.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(confidentiality="secret"),
        )
    assert exc_conf.value.code == "secret_confidentiality_forbidden"

    # 4. Embedded secrets, caller-known secrets, and absolute host paths rejected.
    with pytest.raises(el.LessonPromotionRejectedError) as exc_sec:
        store.promote_lesson(
            workspace_root=ws,
            topic_key="api-auth",
            title="Auth header",
            guidance="Authenticate with sk-liveSecretToken1234567890 against endpoint.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(),
        )
    assert "secret" in exc_sec.value.code

    with pytest.raises(el.LessonPromotionRejectedError):
        store.promote_lesson(
            workspace_root=ws,
            topic_key="custom-secret",
            title="Custom token",
            guidance="Pass SuperPrivateCanary987654321 in header.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(),
            known_secret_values=("SuperPrivateCanary987654321",),
        )

    with pytest.raises(el.LessonPromotionRejectedError):
        store.promote_lesson(
            workspace_root=ws,
            topic_key="abs-path",
            title="Host path leak",
            guidance="Read configuration from /Users/alice/.ssh/config first.",
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(),
        )

    # 5. Anchor path escape or symlink rejected.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (ws / "src" / "link.py").symlink_to(outside)

    with pytest.raises(el.LessonPromotionRejectedError):
        store.promote_lesson(
            workspace_root=ws,
            topic_key="escape-anchor",
            title="Escape",
            guidance="Check config.",
            anchor_paths=("../outside.txt",),
            provenance=_valid_prov(),
        )

    with pytest.raises(el.LessonPromotionRejectedError):
        store.promote_lesson(
            workspace_root=ws,
            topic_key="symlink-anchor",
            title="Symlink",
            guidance="Check config.",
            anchor_paths=("src/link.py",),
            provenance=_valid_prov(),
        )

    # 6. Oversized single lesson rejected (> MAX_SINGLE_LESSON_TOKENS).
    huge_guidance = " ".join(["conventions"] * 140)
    with pytest.raises(el.LessonPromotionRejectedError):
        store.promote_lesson(
            workspace_root=ws,
            topic_key="huge-lesson",
            title="Oversized guidance",
            guidance=huge_guidance,
            anchor_paths=("src/config.py",),
            provenance=_valid_prov(),
        )

    # 7. Valid lesson promotes cleanly and records exact anchored SHA-256.
    promoted = store.promote_lesson(
        workspace_root=ws,
        topic_key="retry-cap",
        title="Bound retries via MAX_RETRIES",
        guidance="Import MAX_RETRIES from src/config.py instead of hardcoding retry counts.",
        anchor_paths=("src/config.py",),
        provenance=_valid_prov(),
        tags=("retries", "config"),
    )
    assert promoted.status == "promoted"
    expected_sha = hashlib.sha256((ws / "src" / "config.py").read_bytes()).hexdigest()
    assert promoted.anchored_files == {"src/config.py": expected_sha}


def test_cross_project_privacy_isolation_and_worktree_canonicalization(tmp_path: Path) -> None:
    """AC 2: Lessons promoted in Project A never leak into Project B, while same-repo worktrees share scope."""
    store = el.EvidenceLessonStore(tmp_path / "store")
    store.update_config(enabled=True, ab_gate_open=True, token_ceiling=160)

    repo_a = tmp_path / "tenant_alpha"
    repo_b = tmp_path / "tenant_bravo"
    for repo in (repo_a, repo_b):
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "pricing.py").write_text("DISCOUNT_CAP = 0.15\n", encoding="utf-8")

    rec_a = store.promote_lesson(
        workspace_root=repo_a,
        topic_key="pricing-discount-cap",
        title="Alpha private discount invariant",
        guidance="In pkg/pricing.py enforce ALPHA_PRIVATE_CAP = 0.15 on enterprise quotes.",
        anchor_paths=("pkg/pricing.py",),
        provenance=_valid_prov(),
        tags=("pricing", "discount", "quotes"),
        failure_codes=("pricing_assertion_failed",),
    )

    # Identical query, target_paths, and failure_codes in `repo_b` must return ZERO lessons from `repo_a`!
    res_b = store.retrieve_for_decision_boundary(
        boundary="turn_plan_start",
        workspace_root=repo_b,
        query="Update pkg/pricing.py enterprise quotes discount cap",
        target_paths=("pkg/pricing.py",),
        failure_codes=("pricing_assertion_failed",),
    )
    assert res_b.lessons == ()
    assert res_b.rendered_prompt == ""
    assert "ALPHA_PRIVATE_CAP" not in res_b.rendered_prompt
    assert store.list_lessons(workspace_root=repo_b, include_stale=True) == []

    # Same-repository harness worktree `<repo_a>/.harnesses/worktrees/s-1` resolves to `repo_a` scope!
    wt_a = repo_a / ".harnesses" / "worktrees" / "s-1"
    (wt_a / "pkg").mkdir(parents=True)
    (wt_a / "pkg" / "pricing.py").write_text("DISCOUNT_CAP = 0.15\n", encoding="utf-8")
    assert el.derive_project_scope_id(wt_a) == el.derive_project_scope_id(repo_a)

    res_wt_a = store.retrieve_for_decision_boundary(
        boundary="turn_plan_start",
        workspace_root=wt_a,
        query="Update pkg/pricing.py enterprise quotes discount cap",
        target_paths=("pkg/pricing.py",),
    )
    assert [r.lesson_id for r in res_wt_a.lessons] == [rec_a.lesson_id]

    # Purging `repo_b` touches 0 lessons; `repo_a` lesson remains intact until `repo_a` is purged.
    assert store.purge_project_lessons(workspace_root=repo_b) == 0
    assert len(store.list_lessons(workspace_root=repo_a)) == 1
    assert store.purge_project_lessons(workspace_root=repo_a) == 1
    assert store.list_lessons(workspace_root=repo_a) == []


def test_staleness_invalidation_on_file_change_security_gen_lineage_and_regressions(
    tmp_path: Path,
) -> None:
    """Task 3: Stale lessons are automatically invalidated before retrieval so obsolete guidance never injects."""
    store = el.EvidenceLessonStore(tmp_path / "store")
    store.update_config(enabled=True, ab_gate_open=True, token_ceiling=160)
    ws = tmp_path / "repo"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "a.py").write_text("V = 1\n", encoding="utf-8")
    (ws / "pkg" / "b.py").write_text("V = 2\n", encoding="utf-8")
    (ws / "pkg" / "c.py").write_text("V = 3\n", encoding="utf-8")
    (ws / "pkg" / "d.py").write_text("V = 4\n", encoding="utf-8")

    l_file = store.promote_lesson(
        workspace_root=ws,
        topic_key="mod-a",
        title="Module A rule",
        guidance="Keep V = 1 in pkg/a.py.",
        anchor_paths=("pkg/a.py",),
        provenance=_valid_prov(security_generation=2),
    )
    l_sec = store.promote_lesson(
        workspace_root=ws,
        topic_key="mod-b",
        title="Module B rule",
        guidance="Keep V = 2 in pkg/b.py.",
        anchor_paths=("pkg/b.py",),
        provenance=_valid_prov(security_generation=1),
    )
    lineage_sha = "b" * 64
    l_lin = store.promote_lesson(
        workspace_root=ws,
        topic_key="mod-c",
        title="Module C rule",
        guidance="Keep V = 3 in pkg/c.py.",
        anchor_paths=("pkg/c.py",),
        provenance=_valid_prov(security_generation=2, lineage_evidence_id=lineage_sha),
    )
    l_reg = store.promote_lesson(
        workspace_root=ws,
        topic_key="mod-d",
        title="Module D rule",
        guidance="Keep V = 4 in pkg/d.py.",
        anchor_paths=("pkg/d.py",),
        provenance=_valid_prov(security_generation=2),
    )

    # Mutate `pkg/a.py` -> `l_file` becomes stale; advance security_generation=2 -> `l_sec` becomes stale;
    # mark lineage invalid -> `l_lin` becomes stale!
    (ws / "pkg" / "a.py").write_text("V = 999\n", encoding="utf-8")
    res = store.retrieve_for_decision_boundary(
        boundary="verification_replan",
        workspace_root=ws,
        query="Check pkg/a.py pkg/b.py pkg/c.py pkg/d.py rules",
        target_paths=("pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"),
        current_security_generation=2,
        is_lineage_evidence_valid_fn=lambda eid: eid != lineage_sha,
    )
    assert [r.lesson_id for r in res.lessons] == [l_reg.lesson_id]
    assert set(res.invalidated_stale_ids) == {
        l_file.lesson_id,
        l_sec.lesson_id,
        l_lin.lesson_id,
    }

    # Consecutive verification failures after retrieving `l_reg` automatically invalidate it!
    assert store.record_retrieval_outcome((l_reg.lesson_id,), verified=False) == ()
    inv_after_2 = store.record_retrieval_outcome((l_reg.lesson_id,), verified=False)
    assert inv_after_2 == (l_reg.lesson_id,)
    assert store.list_lessons(workspace_root=ws, include_stale=False) == []


def test_decision_boundary_enforcement_and_hard_token_ceilings(tmp_path: Path) -> None:
    """Task 2: Only allowlisted decision boundaries can retrieve, and hard token ceilings drop excess items."""
    store = el.EvidenceLessonStore(tmp_path / "store")
    store.update_config(enabled=True, ab_gate_open=True, token_ceiling=120)
    ws = tmp_path / "repo"
    (ws / "pkg").mkdir(parents=True)
    for idx in range(4):
        (ws / "pkg" / f"m{idx}.py").write_text(f"X = {idx}\n", encoding="utf-8")
        store.promote_lesson(
            workspace_root=ws,
            topic_key=f"m{idx}-invariant",
            title=f"Invariant number {idx} for router dispatch",
            guidance=(
                f"When editing pkg/m{idx}.py preserve router dispatch table ordering "
                f"and deterministic fallback handler registration for invariant {idx}."
            ),
            anchor_paths=(f"pkg/m{idx}.py",),
            provenance=_valid_prov(),
            tags=("router", "dispatch", "handler"),
        )

    # Mid-loop / non-boundary retrieval is suppressed with `non_decision_boundary`!
    res_mid = store.retrieve_for_decision_boundary(
        boundary="mid_step_tool_call",
        workspace_root=ws,
        query="router dispatch handler",
    )
    assert res_mid.lessons == ()
    assert res_mid.rendered_prompt == ""
    assert res_mid.skipped_reason == "non_decision_boundary"

    # Hard ceiling above ABSOLUTE_MAX_LESSON_TOKEN_CEILING (256) is rejected on config update.
    with pytest.raises(ValueError):
        store.update_config(token_ceiling=512)

    for valid_boundary in el.DECISION_BOUNDARIES:
        res = store.retrieve_for_decision_boundary(
            boundary=valid_boundary,
            workspace_root=ws,
            query="router dispatch handler ordering",
            token_ceiling=115,
        )
        assert res.used_tokens <= 115
        assert len(res.lessons) >= 1
        assert len(res.dropped_for_budget_ids) >= 1


def test_optional_feature_disable_controls_and_http_endpoints(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1, AC 2, AC 3: HTTP evaluation opens A/B gate, cross-project ops are isolated, and all 3 disable switches work."""
    ws_alpha = isolated_env
    ws_bravo = ws_alpha.parent / "repo_bravo"
    (ws_bravo / "pkg").mkdir(parents=True)
    (ws_bravo / "pkg" / "conventions.py").write_text("BRAVO = 1\n", encoding="utf-8")

    # Start with ab_gate_open=False so retrieval must stay closed until measured A/B evaluation passes.
    studio.get_evidence_lesson_store().update_config(ab_gate_open=False)

    with _client() as tc:
        # Missing workspace/session scope fails closed with 400 (no global cross-project dump).
        r_missing = tc.get("/api/harness/lessons")
        assert r_missing.status_code == 400

        r_init = tc.get("/api/harness/lessons", params={"workspace_dir": str(ws_alpha)})
        assert r_init.status_code == 200, r_init.text
        assert r_init.json()["config"]["ab_gate_open"] is False

        # Promote a verified lesson for `ws_alpha`, anchored to `ArtifactRegistry` as well!
        reg = artifact_lineage.ArtifactRegistry(studio.HARNESS_STORE_DIR)
        art_sha = hashlib.sha256((ws_alpha / "pkg" / "conventions.py").read_bytes()).hexdigest()
        reg.record_artifact(
            art_sha,
            source_kind="workspace_edit",
            source_ref="pkg/conventions.py",
            trust="trusted",
            size=48,
        )

        r_prom = tc.post(
            "/api/harness/lessons/promote",
            json={
                "workspace_dir": str(ws_alpha),
                "topic_key": "metrics-sentinel",
                "title": "Use pkg/conventions.py sentinel in metrics",
                "guidance": "In pkg/metrics.py import ZERO_DIVISOR_SENTINEL (-1.0) and ROUND_DIGITS (4) from pkg.conventions.",
                "anchored_files": ["pkg/conventions.py"],
                "tags": ["metrics", "sentinel", "conventions"],
                "provenance": {
                    "source_session_id": "sess-1",
                    "source_task_id": "task-1",
                    "source_epoch": 1,
                    "verified": True,
                    "verification_spec_sha256": "c" * 64,
                    "passed_checks": 2,
                    "lineage_evidence_id": art_sha,
                },
            },
        )
        assert r_prom.status_code == 200, r_prom.text
        lesson_id = r_prom.json()["lesson"]["lesson_id"]

        # While `ab_gate_open=False`, retrieval skips with `ab_benefit_gate_closed`.
        r_pre_ab = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
                "decision_boundary": "turn_plan_start",
            },
        )
        assert r_pre_ab.status_code == 200
        assert r_pre_ab.json()["lessons"] == []
        assert r_pre_ab.json()["skipped_reason"] == "ab_benefit_gate_closed"

        # Run real A/B coding completion evaluation via HTTP endpoint -> opens `ab_gate_open`!
        r_eval = tc.post("/api/harness/lessons/evaluate", json={"runs_per_variant": 2, "persist_summary": True})
        assert r_eval.status_code == 200, r_eval.text
        eval_body = r_eval.json()
        assert eval_body["ab_gate_passed"] is True
        assert eval_body["cross_project_leak_count"] == 0
        assert eval_body["overall"]["measured_improvement_over_baseline"] is True
        assert eval_body["overall"]["lessons"]["success_rate"] > eval_body["overall"]["baseline"]["success_rate"]
        assert eval_body["overall"]["lessons_minus_baseline"]["total_tokens_median"] < 0
        assert eval_body["overall"]["within_hard_token_ceiling"] is True

        # Now bounded retrieval returns the verified lesson within hard 160-token ceiling!
        r_ok = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
                "decision_boundary": "turn_plan_start",
            },
        )
        assert r_ok.status_code == 200
        assert [item["lesson_id"] for item in r_ok.json()["lessons"]] == [lesson_id]
        assert 0 < r_ok.json()["used_tokens"] <= el.DEFAULT_LESSON_TOKEN_CEILING

        # Cross-project isolation on HTTP invalidate & retrieve (`ws_bravo` cannot see or invalidate `lesson_id`).
        r_cross_inv = tc.post(
            f"/api/harness/lessons/{lesson_id}/invalidate",
            json={"workspace_dir": str(ws_bravo), "reason": "cross_project_probe"},
        )
        assert r_cross_inv.status_code == 404

        # Disable switch 1: per-call `enable_lessons=False`.
        r_sess_off = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
                "enable_lessons": False,
            },
        )
        assert r_sess_off.json()["enabled"] is False
        assert r_sess_off.json()["lessons"] == []
        assert r_sess_off.json()["skipped_reason"] == "feature_disabled"

        # Disable switch 2: process env `STUDIO_ENABLE_EVIDENCE_LESSONS=0`.
        monkeypatch.setenv("STUDIO_ENABLE_EVIDENCE_LESSONS", "0")
        r_env_off = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
            },
        )
        assert r_env_off.json()["enabled"] is False
        assert r_env_off.json()["skipped_reason"] == "feature_disabled"
        monkeypatch.delenv("STUDIO_ENABLE_EVIDENCE_LESSONS", raising=False)

        # Disable switch 3: runtime config `enabled=False`.
        r_cfg_off = tc.post("/api/harness/lessons/config", json={"enabled": False})
        assert r_cfg_off.status_code == 200
        r_store_off = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
            },
        )
        assert r_store_off.json()["enabled"] is False
        assert r_store_off.json()["skipped_reason"] == "store_config_disabled"
        tc.post("/api/harness/lessons/config", json={"enabled": True})

        # ArtifactRegistry invalidation automatically marks the lineage-bound lesson stale!
        reg.invalidate(art_sha, reason="upstream_artifact_quarantined")
        r_after_lin = tc.post(
            "/api/harness/lessons/retrieve",
            json={
                "workspace_dir": str(ws_alpha),
                "query": "Fix compute_ratio in pkg/metrics.py",
            },
        )
        assert r_after_lin.json()["lessons"] == []
        assert lesson_id in r_after_lin.json()["invalidated_stale_ids"]


def test_harness_turn_injects_at_decision_boundary_and_promotes_verified_candidate(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end `/api/harness/run` test: injects bounded lessons at turn start and promotes candidate on verified completion."""
    ws = isolated_env
    studio.get_evidence_lesson_store().update_config(enabled=True, ab_gate_open=True, token_ceiling=160)

    # Seed an active verified lesson for `pkg/conventions.py`.
    seeded = studio.get_evidence_lesson_store().promote_lesson(
        workspace_root=ws,
        topic_key="metrics-conventions",
        title="Follow pkg/conventions.py sentinel and rounding",
        guidance="In pkg/metrics.py import ZERO_DIVISOR_SENTINEL (-1.0) and ROUND_DIGITS (4) from pkg.conventions.",
        anchor_paths=("pkg/conventions.py",),
        provenance=_valid_prov(security_generation=int(studio._current_security_generation() or 0)),
        tags=("metrics", "conventions", "ratio"),
    )

    observed_sys_prompts: list[str] = []
    step_counter = 0

    async def _scripted_model(
        _client_obj: Any,
        _model_spec: str,
        msgs: list[dict[str, str]],
        **_kw: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal step_counter
        step_counter += 1
        observed_sys_prompts.append(msgs[0]["content"])
        if step_counter == 1:
            call_read = json.dumps({"name": "read_file", "arguments": {"path": "pkg/metrics.py"}})
            call_write = json.dumps(
                {
                    "name": "write_file",
                    "arguments": {
                        "path": "pkg/metrics.py",
                        "content": (
                            "from pkg.conventions import ROUND_DIGITS, ZERO_DIVISOR_SENTINEL\n\n"
                            "def compute_ratio(num: float, den: float) -> float:\n"
                            "    if den == 0:\n"
                            "        return ZERO_DIVISOR_SENTINEL\n"
                            "    return round(float(num) / float(den), ROUND_DIGITS)\n"
                        ),
                    },
                }
            )
            yield {
                "type": "content",
                "text": f"Fixing compute_ratio.\n<tool_call>{call_read}</tool_call>\n<tool_call>{call_write}</tool_call>",
            }
            yield {
                "type": "usage",
                "decode_tps": 50.0,
                "prefill_tps": 160.0,
                "ttft": 0.03,
                "completion_tokens": 45,
                "cached_tokens": 0,
            }
        else:
            yield {"type": "content", "text": "Implemented verified ratio fix."}
            yield {
                "type": "usage",
                "decode_tps": 50.0,
                "prefill_tps": 160.0,
                "ttft": 0.03,
                "completion_tokens": 12,
                "cached_tokens": 0,
            }

    async def _noop_unload(*_a: Any, **_k: Any) -> list[str]:
        return []

    monkeypatch.setattr(studio, "_unload_other_omlx_models", _noop_unload)
    monkeypatch.setattr(studio, "stream_any_model", _scripted_model)

    with _client() as tc:
        r_turn = tc.post(
            "/api/harness/run",
            json={
                "workspace_dir": str(ws),
                "policy": "autonomous",
                "lead_model": "Qwen3.8-27B-MLX-oQ4e-mtp",
                "critic_model": "Gemma-4-21B-REAP-4bit",
                "prompt": "Fix compute_ratio in pkg/metrics.py according to project conventions.",
                "verification": {
                    "require_managed_edit": True,
                    "required_files": ["pkg/metrics.py"],
                    "test_command": [sys.executable, "-I", "pkg/check_metrics.py"],
                    "expected_test_output_contains": ["METRICS_VERIFIED_OK"],
                },
                "candidate_lesson": {
                    "topic_key": "metrics-zero-den-verified",
                    "title": "Verified compute_ratio zero-denominator guard",
                    "guidance": "Ensure compute_ratio in pkg/metrics.py delegates zero denominators to ZERO_DIVISOR_SENTINEL.",
                    "anchored_files": ["pkg/metrics.py"],
                    "tags": ["metrics", "zero"],
                },
            },
        )
        assert r_turn.status_code == 200, r_turn.text
    sse_events = _parse_sse(r_turn.text)
    event_names = [name for name, _ in sse_events]
    assert "verification_passed" in event_names, sse_events

    # System prompt at turn boundary included the retrieved lesson within token ceiling!
    assert observed_sys_prompts
    assert "[VERIFIED PROJECT LESSONS" in observed_sys_prompts[0]
    assert "ZERO_DIVISOR_SENTINEL" in observed_sys_prompts[0]

    # Candidate lesson was promoted because `verification_report.verified` + `spec_sha256` succeeded!
    active_lessons = studio.get_evidence_lesson_store().list_lessons(workspace_root=ws)
    topics = {item.topic_key for item in active_lessons}
    assert "metrics-zero-den-verified" in topics
    updated_seed = next(item for item in active_lessons if item.lesson_id == seeded.lesson_id)
    assert updated_seed.verified_hit_count >= 1


def test_standalone_studio_bundle_evidence_lessons_parity(tmp_path: Path) -> None:
    """Single-file `studio.py` bundle enforces `EvidenceLessonStore` contracts outside the repo tree."""
    repo_root = Path(__file__).resolve().parents[1]
    bundle_dir = tmp_path / "standalone_bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo_root / "studio.py", bundle_dir / "studio.py")

    script = textwrap.dedent(
        """
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path.cwd()))
        import studio

        ws_a = Path.cwd() / "ws_a"
        ws_b = Path.cwd() / "ws_b"
        for w in (ws_a, ws_b):
            w.mkdir(parents=True, exist_ok=True)
            (w / "mod.py").write_text("K = 42\\n", encoding="utf-8")

        store = studio.EvidenceLessonStore(Path.cwd() / "store")
        store.update_config(enabled=True, ab_gate_open=True, token_ceiling=160)
        prov = studio.LessonProvenance(
            source_session_id="s1",
            source_task_id="t1",
            source_epoch=1,
            verified=True,
            verification_spec_sha256="d" * 64,
            passed_checks=1,
        )
        rec = store.promote_lesson(
            workspace_root=ws_a,
            topic_key="mod-k",
            title="Keep K constant",
            guidance="Preserve K = 42 in mod.py during refactor.",
            anchor_paths=("mod.py",),
            provenance=prov,
            tags=("mod", "refactor"),
        )
        res_a = store.retrieve_for_decision_boundary(
            boundary="turn_plan_start",
            workspace_root=ws_a,
            query="Refactor mod.py K constant",
            target_paths=("mod.py",),
        )
        res_b = store.retrieve_for_decision_boundary(
            boundary="turn_plan_start",
            workspace_root=ws_b,
            query="Refactor mod.py K constant",
            target_paths=("mod.py",),
        )
        assert len(res_a.lessons) == 1 and res_a.lessons[0].lesson_id == rec.lesson_id
        assert len(res_b.lessons) == 0
        print("STANDALONE_EVIDENCE_LESSONS_OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "STANDALONE_EVIDENCE_LESSONS_OK" in proc.stdout
