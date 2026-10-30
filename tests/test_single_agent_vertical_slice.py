"""M5-P1-06 tests: Single-agent verified vertical slice, crash boundaries, and false-completion rejection."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient
import httpx
import pytest

import failure_injection
import studio

BUGGY_INVOICE_SOURCE = '''"""Invoice calculation helpers for billing."""
from __future__ import annotations


def compute_invoice_total_cents(
    line_items_cents: list[int],
    *,
    discount_bps: int = 0,
    tax_bps: int = 0,
) -> int:
    """Compute final invoice total in cents using basis-point discount and tax."""
    if any(item < 0 for item in line_items_cents):
        raise ValueError("line item amounts must be non-negative")
    if not (0 <= discount_bps <= 10_000):
        raise ValueError("discount_bps must be in [0, 10000]")
    if tax_bps < 0:
        raise ValueError("tax_bps must be non-negative")

    subtotal = sum(line_items_cents)
    # BUG: divides by 100 (percent) instead of 10_000 (basis points), over-discounting 100x!
    discount = (subtotal * discount_bps) // 100
    discounted = max(0, subtotal - discount)
    tax = (discounted * tax_bps + 5_000) // 10_000
    return discounted + tax
'''

FIXED_INVOICE_SOURCE = '''"""Invoice calculation helpers for billing."""
from __future__ import annotations


def compute_invoice_total_cents(
    line_items_cents: list[int],
    *,
    discount_bps: int = 0,
    tax_bps: int = 0,
) -> int:
    """Compute final invoice total in cents using basis-point discount and tax."""
    if any(item < 0 for item in line_items_cents):
        raise ValueError("line item amounts must be non-negative")
    if not (0 <= discount_bps <= 10_000):
        raise ValueError("discount_bps must be in [0, 10000]")
    if tax_bps < 0:
        raise ValueError("tax_bps must be non-negative")

    subtotal = sum(line_items_cents)
    discount = (subtotal * discount_bps + 5_000) // 10_000
    discounted = max(0, subtotal - discount)
    tax = (discounted * tax_bps + 5_000) // 10_000
    return discounted + tax
'''

STILL_BUGGY_INVOICE_SOURCE = '''"""Invoice calculation helpers for billing."""
from __future__ import annotations


def compute_invoice_total_cents(
    line_items_cents: list[int],
    *,
    discount_bps: int = 0,
    tax_bps: int = 0,
) -> int:
    if any(item < 0 for item in line_items_cents):
        raise ValueError("line item amounts must be non-negative")
    subtotal = sum(line_items_cents)
    discount = (subtotal * discount_bps) // 1_000  # Still wrong by 10x!
    discounted = max(0, subtotal - discount)
    tax = (discounted * tax_bps + 5_000) // 10_000
    return discounted + tax
'''

INITIAL_TEST_SUITE = '''from __future__ import annotations

import pytest

from billing.invoice import compute_invoice_total_cents


def test_subtotal_with_basis_point_discount_and_tax() -> None:
    # $100.00 subtotal, 12.50% (1250 bps) discount -> $87.50, 8.00% (800 bps) tax -> $94.50
    assert compute_invoice_total_cents([6_000, 4_000], discount_bps=1_250, tax_bps=800) == 9_450


def test_negative_line_item_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compute_invoice_total_cents([1_000, -5])
'''

REGRESSION_TEST_CONTENT = '''from __future__ import annotations

from billing.invoice import compute_invoice_total_cents


def test_half_cent_discount_rounding_and_zero_discount() -> None:
    # 333 cents * 500 bps (5%) = 16.65 cents -> rounds half-up to 17 cents -> 316 cents
    assert compute_invoice_total_cents([333], discount_bps=500, tax_bps=0) == 316
    assert compute_invoice_total_cents([333], discount_bps=0, tax_bps=0) == 333
'''

DIRTY_USER_WIP_NOTE = (
    "# Operator Local Investigation Notes (DO NOT TOUCH)\n"
    "- Customer #4821 reported 1250 bps coupon zeroed out their $100 invoice.\n"
    "- Keep my manual SQL reconciliation checklist intact.\n"
)

UNTRACKED_USER_DRAFT = (
    "# Uncommitted user experiment\n"
    "USER_SCRATCH_CONST = 'preserve-operator-work-across-crashes'\n"
)


@pytest.fixture()
def coding_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Create an isolated Studio DB + real Git Python fixture repo with dirty user work."""
    db_path = tmp_path / "m5_p1_06.sqlite3"
    ws_root = tmp_path / "invoice_repo"
    ws_root.mkdir(parents=True, exist_ok=True)
    harness_store = tmp_path / "harness_store"
    harness_store.mkdir(parents=True, exist_ok=True)

    # Seed Git repository with real failing coding fixture.
    (ws_root / "billing").mkdir(parents=True, exist_ok=True)
    (ws_root / "tests").mkdir(parents=True, exist_ok=True)
    (ws_root / "notes").mkdir(parents=True, exist_ok=True)
    (ws_root / "scratch").mkdir(parents=True, exist_ok=True)

    (ws_root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n',
        encoding="utf-8",
    )
    (ws_root / "billing" / "__init__.py").write_text("", encoding="utf-8")
    (ws_root / "billing" / "invoice.py").write_text(BUGGY_INVOICE_SOURCE, encoding="utf-8")
    (ws_root / "tests" / "test_invoice.py").write_text(INITIAL_TEST_SUITE, encoding="utf-8")
    (ws_root / "notes" / "operator_wip.md").write_text("# Initial committed note\n", encoding="utf-8")

    git_env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "Fixture Seed",
        "GIT_AUTHOR_EMAIL": "fixture@example.local",
        "GIT_COMMITTER_NAME": "Fixture Seed",
        "GIT_COMMITTER_EMAIL": "fixture@example.local",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(["git", "init"], cwd=ws_root, check=True, capture_output=True, env=git_env)
    subprocess.run(["git", "add", "."], cwd=ws_root, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-m", "Seed buggy invoice package"],
        cwd=ws_root,
        check=True,
        capture_output=True,
        env=git_env,
    )

    # Layer dirty tracked + untracked operator files on top of HEAD before the agent runs.
    (ws_root / "notes" / "operator_wip.md").write_text(DIRTY_USER_WIP_NOTE, encoding="utf-8")
    (ws_root / "scratch" / "local_draft.py").write_text(UNTRACKED_USER_DRAFT, encoding="utf-8")

    # Sanity-prove the fixture genuinely fails pytest before agent intervention.
    pre_test = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ws_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pre_test.returncode != 0
    assert "9_450" in (pre_test.stdout + pre_test.stderr) or "9450" in (pre_test.stdout + pre_test.stderr)

    monkeypatch.setattr(studio, "DB_PATH", db_path)
    monkeypatch.setattr(studio, "WORKSPACE_ROOT", ws_root)
    monkeypatch.setattr(studio, "HARNESS_STORE_DIR", harness_store)
    monkeypatch.setattr(studio, "STUDIO_API_TOKEN", "v" * 40)
    monkeypatch.setattr(studio, "STUDIO_EXECUTION_MODE", "host")
    monkeypatch.setattr(studio, "STUDIO_ALLOW_HOST_EXECUTION", True)
    studio._HARNESS_ABORT_FLAGS.clear()
    studio._PENDING_APPROVALS.clear()
    studio.failure_injection.DEFAULT_BOUNDARY_INJECTOR.clear()
    failure_injection.DEFAULT_BOUNDARY_INJECTOR.clear()
    studio.init_db()
    studio._set_default_projects_dir(str(ws_root), create_if_missing=True)

    wip_sha = hashlib.sha256(DIRTY_USER_WIP_NOTE.encode("utf-8")).hexdigest()
    draft_sha = hashlib.sha256(UNTRACKED_USER_DRAFT.encode("utf-8")).hexdigest()
    buggy_sha = hashlib.sha256(BUGGY_INVOICE_SOURCE.encode("utf-8")).hexdigest()

    try:
        yield {
            "ws_root": ws_root,
            "db_path": db_path,
            "wip_sha": wip_sha,
            "draft_sha": draft_sha,
            "buggy_sha": buggy_sha,
        }
    finally:
        studio.failure_injection.DEFAULT_BOUNDARY_INJECTOR.clear()
        failure_injection.DEFAULT_BOUNDARY_INJECTOR.clear()
        studio._HARNESS_ABORT_FLAGS.clear()
        studio._PENDING_APPROVALS.clear()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {studio.STUDIO_API_TOKEN}"}


def _create_session(ws_root: Path, *, policy: str = "autonomous", title: str = "P1-06 Vertical Slice") -> dict[str, Any]:
    return studio.create_harness_session_db(
        title=title,
        workspace_dir=str(ws_root),
        use_worktree=False,
        policy=policy,
        lead_model="Qwen3.8-27B-MLX-oQ4e-mtp",
        critic_model="Gemma-4-21B-REAP-4bit",
    )


def _standard_verification_contract() -> dict[str, Any]:
    return {
        "require_managed_edit": True,
        "require_git_diff": True,
        "required_modified_files": ["billing/invoice.py"],
        "required_files": ["tests/test_invoice_regression.py"],
        "protected_files": ["notes/operator_wip.md", "scratch/local_draft.py"],
        "require_agent_test_action": True,
        "test_command": [sys.executable, "-m", "pytest", "-q"],
        "expected_test_output_contains": ["passed"],
        "timeout_s": 30.0,
    }


def _parse_sse_events(raw_sse: str) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    cur_event = ""
    for line in raw_sse.splitlines():
        if line.startswith("event: "):
            cur_event = line[len("event: ") :].strip()
        elif line.startswith("data: ") and cur_event:
            payload = json.loads(line[len("data: ") :])
            parsed.append((cur_event, payload))
            cur_event = ""
    return parsed


def _assert_user_work_intact(fixture: dict[str, Any]) -> None:
    ws_root: Path = fixture["ws_root"]
    wip_bytes = (ws_root / "notes" / "operator_wip.md").read_bytes()
    draft_bytes = (ws_root / "scratch" / "local_draft.py").read_bytes()
    assert hashlib.sha256(wip_bytes).hexdigest() == fixture["wip_sha"]
    assert hashlib.sha256(draft_bytes).hexdigest() == fixture["draft_sha"]


def test_single_agent_vertical_slice_completes_coding_task_with_approval_ledger_and_evidence(
    coding_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: Complete one representative real coding task end-to-end through admission,
    operator approval (`standard` policy), durable action ledger, managed edits, real
    pytest execution, and authoritative patch + test verification evidence."""
    ws_root: Path = coding_workspace["ws_root"]
    sess = _create_session(ws_root, policy="standard", title="Fix basis-point invoice discount")
    sid = sess["id"]

    model_step = 0

    async def scripted_coding_agent(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal model_step
        model_step += 1
        if model_step == 1:
            tc_read_src = json.dumps({"name": "read_file", "arguments": {"path": "billing/invoice.py"}})
            tc_read_tst = json.dumps({"name": "read_file", "arguments": {"path": "tests/test_invoice.py"}})
            text = (
                "Inspecting buggy invoice implementation and failing test suite.\n"
                f"<tool_call>{tc_read_src}</tool_call>\n"
                f"<tool_call>{tc_read_tst}</tool_call>"
            )
            yield {"type": "content", "text": text}
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 40, "cached_tokens": 0}
        elif model_step == 2:
            tc_edit = json.dumps(
                {
                    "name": "edit_file",
                    "arguments": {
                        "path": "billing/invoice.py",
                        "old_text": "    # BUG: divides by 100 (percent) instead of 10_000 (basis points), over-discounting 100x!\n    discount = (subtotal * discount_bps) // 100",
                        "new_text": "    discount = (subtotal * discount_bps + 5_000) // 10_000",
                    },
                }
            )
            tc_write_reg = json.dumps(
                {
                    "name": "write_file",
                    "arguments": {
                        "path": "tests/test_invoice_regression.py",
                        "content": REGRESSION_TEST_CONTENT,
                    },
                }
            )
            text = (
                "Applying basis-point rounding fix and adding half-cent regression test.\n"
                f"<tool_call>{tc_edit}</tool_call>\n"
                f"<tool_call>{tc_write_reg}</tool_call>"
            )
            yield {"type": "content", "text": text}
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 55, "cached_tokens": 0}
        elif model_step == 3:
            pytest_cmd = f"{sys.executable} -m pytest -q"
            tc_test = json.dumps({"name": "run_bash", "arguments": {"command": pytest_cmd}})
            yield {"type": "content", "text": f"Running test suite.\n<tool_call>{tc_test}</tool_call>"}
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 25, "cached_tokens": 0}
        else:
            yield {
                "type": "content",
                "text": "Fixed basis-point discount calculation in billing/invoice.py and verified all pytest tests pass.",
            }
            yield {"type": "usage", "decode_tps": 55.0, "prefill_tps": 180.0, "ttft": 0.04, "completion_tokens": 20, "cached_tokens": 0}

    monkeypatch.setattr(studio, "stream_any_model", scripted_coding_agent)

    async def _run_turn_with_live_operator_approval() -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
        studio.app.state.http = httpx.AsyncClient()
        try:
            transport = httpx.ASGITransport(app=studio.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as ac:
                approved_ids: list[str] = []

                async def _operator_approver() -> None:
                    for _ in range(400):
                        for aid, slot in list(studio._PENDING_APPROVALS.items()):
                            if slot.get("session_id") == sid and not slot["event"].is_set():
                                ap_resp = await ac.post(
                                    "/api/harness/approve",
                                    headers=_headers(),
                                    json={
                                        "session_id": sid,
                                        "approval_id": aid,
                                        "decision": "approve",
                                        "note": "Approved pytest verification run",
                                    },
                                )
                                assert ap_resp.status_code == 200
                                approved_ids.append(aid)
                                return
                        await asyncio.sleep(0.01)

                approver_task = asyncio.create_task(_operator_approver())
                resp = await ac.post(
                    "/api/harness/run",
                    headers=_headers(),
                    json={
                        "session_id": sid,
                        "prompt": "Fix the basis-point discount bug in billing/invoice.py and add regression coverage.",
                        "policy": "standard",
                        "max_steps": 6,
                        "verification": _standard_verification_contract(),
                    },
                )
                await approver_task
                assert resp.status_code == 200
                assert len(approved_ids) == 1
                task_snap = (await ac.get(f"/api/harness/sessions/{sid}/task", headers=_headers())).json()
                return _parse_sse_events(resp.text), task_snap
        finally:
            await studio.app.state.http.aclose()

    events, snap = asyncio.run(_run_turn_with_live_operator_approval())
    event_names = [name for name, _ in events]
    assert "approval_required" in event_names
    assert "approval_resolved" in event_names
    assert "verification_passed" in event_names
    assert "turn_done" in event_names

    ver_ev = dict(events)[  # last verification_passed payload
        "verification_passed"
    ]
    assert ver_ev["verified"] is True
    assert ver_ev["test_exit_code"] == 0
    assert "billing/invoice.py" in ver_ev["modified_files"]
    assert "tests/test_invoice_regression.py" in ver_ev["modified_files"]
    assert len(ver_ev["patch_sha256"]) == 64
    assert len(ver_ev["test_sha256"]) == 64

    # Durable task state, checkpoint integrity, and multi-kind evidence references.
    assert snap["task"]["state"] == "completed"
    assert studio.get_harness_session_db(sid)["status"] == "completed"
    assert snap["checkpoint_integrity_error"] == ""
    final_ckpt = snap["latest_checkpoint"]
    assert final_ckpt is not None
    assert final_ckpt["status"] == "committed"
    assert final_ckpt["payload"]["terminal"] is True
    ref_kinds = {ref["kind"] for ref in final_ckpt["evidence_refs"]}
    assert {"action", "git_diff", "patch_verification", "test_verification"}.issubset(ref_kinds)

    # Durable action ledger proves every tool passed admission and finalized cleanly.
    sm = studio.get_task_state_machine()
    actions = sm.store.list_task_actions(sid)
    assert [a.tool_name for a in actions] == [
        "read_file",
        "read_file",
        "edit_file",
        "write_file",
        "run_bash",
    ]
    assert all(a.status == "committed" and a.ok == 1 for a in actions)
    bash_act = [a for a in actions if a.tool_name == "run_bash"][0]
    assert bash_act.explicit_approval is True

    # Final transition durably embeds the verification report.
    completed_tr = [t for t in snap["transitions"] if t["to_state"] == "completed"]
    assert len(completed_tr) == 1
    assert completed_tr[0]["evidence"]["verification"]["verified"] is True

    # Pre-existing dirty & untracked operator files were completely preserved!
    _assert_user_work_intact(coding_workspace)


@pytest.mark.parametrize(
    "crash_boundary",
    [
        "before_intent",
        "after_intent_before_effect",
        "after_effect_before_result_commit",
        "before_task_completion",
    ],
)
def test_crash_injection_at_action_boundaries_preserves_user_work_and_prevents_duplicate_effects(
    coding_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    crash_boundary: failure_injection.ActionBoundary,
) -> None:
    """AC 2 (Part A): Inject hard crashes at `before_intent`, `after_intent_before_effect`,
    `after_effect_before_result_commit`, and `before_task_completion`; verify user work is
    never lost, committed steps/writes are never duplicated, and recovery completes cleanly."""
    ws_root: Path = coding_workspace["ws_root"]
    sess = _create_session(ws_root, policy="autonomous", title=f"Crash boundary: {crash_boundary}")
    sid = sess["id"]

    # Track real executions of inner tools to prove zero duplicate side effects!
    orig_inner = studio._execute_harness_tool_inner
    execution_counts: dict[str, int] = {"read_file": 0, "write_file": 0, "edit_file": 0, "run_bash": 0}

    async def counting_inner(
        client: Any,
        s: dict[str, Any],
        tool_name: str,
        args: dict[str, Any],
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        execution_counts[tool_name] = execution_counts.get(tool_name, 0) + 1
        return await orig_inner(client, s, tool_name, args, approved=approved)

    monkeypatch.setattr(studio, "_execute_harness_tool_inner", counting_inner)

    def _usage(tok: int) -> dict[str, Any]:
        return {
            "type": "usage",
            "decode_tps": 50.0,
            "prefill_tps": 160.0,
            "ttft": 0.04,
            "completion_tokens": tok,
            "cached_tokens": 0,
        }

    # Step-aware deterministic coding agent keyed on committed conversation state.
    async def step_aware_agent(
        _client: Any,
        _model: str,
        messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
        if assistant_turns == 0:
            # Step 1: read buggy file
            tc = json.dumps({"name": "read_file", "arguments": {"path": "billing/invoice.py"}})
            yield {"type": "content", "text": f"Step 1 read.\n<tool_call>{tc}</tool_call>"}
            yield _usage(15)
        elif assistant_turns == 1:
            # Step 2: write new regression test (`reconcilable`)
            tc = json.dumps(
                {
                    "name": "write_file",
                    "arguments": {
                        "path": "tests/test_invoice_regression.py",
                        "content": REGRESSION_TEST_CONTENT,
                    },
                }
            )
            yield {"type": "content", "text": f"Step 2 write regression test.\n<tool_call>{tc}</tool_call>"}
            yield _usage(20)
        elif assistant_turns == 2:
            # Step 3: patch invoice.py and run pytest
            tc_edit = json.dumps(
                {
                    "name": "edit_file",
                    "arguments": {
                        "path": "billing/invoice.py",
                        "old_text": "    # BUG: divides by 100 (percent) instead of 10_000 (basis points), over-discounting 100x!\n    discount = (subtotal * discount_bps) // 100",
                        "new_text": "    discount = (subtotal * discount_bps + 5_000) // 10_000",
                    },
                }
            )
            tc_test = json.dumps({"name": "run_bash", "arguments": {"command": f"{sys.executable} -m pytest -q"}})
            yield {"type": "content", "text": f"Step 3 fix & test.\n<tool_call>{tc_edit}</tool_call>\n<tool_call>{tc_test}</tool_call>"}
            yield _usage(30)
        else:
            # Step 4: conclude
            yield {"type": "content", "text": "Fix and tests complete."}
            yield _usage(10)

    monkeypatch.setattr(studio, "stream_any_model", step_aware_agent)

    if crash_boundary == "before_task_completion":
        fault_spec = studio.failure_injection.ActionBoundaryFaultSpec(
            boundary="before_task_completion",
            task_id=sid,
            max_fires=1,
            detail="simulated crash right before task completion commit",
        )
    else:
        fault_spec = studio.failure_injection.ActionBoundaryFaultSpec(
            boundary=crash_boundary,
            tool_name="write_file",
            task_id=sid,
            step_id=2,
            max_fires=1,
            detail=f"simulated hard crash at {crash_boundary}",
        )
    studio.failure_injection.DEFAULT_BOUNDARY_INJECTOR.arm(fault_spec)

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        with pytest.raises(studio.failure_injection.InjectedBoundaryCrashError) as crash_exc:
            client.post(
                "/api/harness/run",
                headers=_headers(),
                json={
                    "session_id": sid,
                    "prompt": "Fix invoice discount bug and add regression test.",
                    "policy": "autonomous",
                    "max_steps": 6,
                    "verification": _standard_verification_contract(),
                },
            )
        assert crash_exc.value.boundary == crash_boundary
        _assert_user_work_intact(coding_workspace)

        sm = studio.get_task_state_machine()
        mid_actions = {a.tool_name: a for a in sm.store.list_task_actions(sid)}
        reg_file = ws_root / "tests" / "test_invoice_regression.py"

        # Verify authentic post-SIGKILL state BEFORE restart recovery runs:
        ep1 = sm.store.get_epoch(sid, 1)
        assert ep1 is not None and ep1.status == "active"
        if crash_boundary == "before_intent":
            assert "write_file" not in mid_actions
            assert not reg_file.exists()
            assert execution_counts["write_file"] == 0
        elif crash_boundary == "after_intent_before_effect":
            assert mid_actions["write_file"].status == "in_flight"
            assert not reg_file.exists()
            assert execution_counts["write_file"] == 0
        elif crash_boundary == "after_effect_before_result_commit":
            assert mid_actions["write_file"].status == "in_flight"
            assert reg_file.read_text(encoding="utf-8") == REGRESSION_TEST_CONTENT
            assert execution_counts["write_file"] == 1
        elif crash_boundary == "before_task_completion":
            assert sm.store.get_task(sid).state == "running"  # type: ignore[union-attr]
            assert execution_counts["write_file"] == 1
            assert execution_counts["edit_file"] == 1
            assert execution_counts["run_bash"] == 1

        # Simulate process restart -> fences foreign-boot epoch 1 and reconciles `write_file`!
        studio.simulate_process_restart()
        ep1_after = sm.store.get_epoch(sid, 1)
        assert ep1_after is not None and ep1_after.status == "fenced"

        prep = client.post(f"/api/harness/sessions/{sid}/resume", headers=_headers())
        assert prep.status_code == 200, prep.text
        prep_json = prep.json()
        expected_next_step = 4 if crash_boundary == "before_task_completion" else 2
        assert prep_json["next_step"] == expected_next_step

        # Resume and complete the vertical slice (without re-sending `verification` — persisted in SQLite!).
        res2 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={
                "session_id": sid,
                "prompt": "Fix invoice discount bug and add regression test.",
                "policy": "autonomous",
                "max_steps": 6,
                "resume": True,
            },
        )
        assert res2.status_code == 200
        events2 = _parse_sse_events(res2.text)
        ev2_names = [n for n, _ in events2]
        assert "verification_passed" in ev2_names
        assert "turn_done" in ev2_names

        snap = client.get(f"/api/harness/sessions/{sid}/task", headers=_headers()).json()
        assert snap["task"]["state"] == "completed"
        assert snap["budget"]["recoveries_used"] == 1

        # CRITICAL INVARIANT: ZERO duplicate side effects across crash + recovery!
        # Step 1 `read_file` was checkpointed before Step 2 and NEVER ran again;
        # `write_file`, `edit_file`, and `run_bash` each executed on the host EXACTLY ONCE!
        assert execution_counts["read_file"] == 1
        assert execution_counts["write_file"] == 1
        assert execution_counts["edit_file"] == 1
        assert execution_counts["run_bash"] == 1
        assert reg_file.read_text(encoding="utf-8") == REGRESSION_TEST_CONTENT
        _assert_user_work_intact(coding_workspace)


def test_crash_injection_protects_diverged_user_edits_and_blocks_blind_non_idempotent_replay(
    coding_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2 (Part B):
    1) If `write_file` crashes `after_intent_before_effect` targeting a file containing
       pre-existing user edits (`notes/operator_wip.md`), startup reconciliation refuses to
       clobber user work, holds the task in `'blocked'`, and blocks resume with HTTP 409.
    2) If `non_idempotent` `run_bash` crashes `after_effect_before_result_commit` after
       mutating state once, startup marks it `'uncertain'` and blocks blind re-execution."""
    ws_root: Path = coding_workspace["ws_root"]

    # --- Sub-case 1: Crash before overwriting user-modified `notes/operator_wip.md` ---
    sess_wip = _create_session(ws_root, policy="autonomous", title="Diverged User File Protection")
    sid_wip = sess_wip["id"]

    async def stream_attempt_overwrite_wip(
        _client: Any,
        _model: str,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        tc = json.dumps(
            {
                "name": "write_file",
                "arguments": {"path": "notes/operator_wip.md", "content": "AGENT OVERWRITE ATTEMPT\n"},
            }
        )
        yield {"type": "content", "text": f"Writing note.\n<tool_call>{tc}</tool_call>"}

    monkeypatch.setattr(studio, "stream_any_model", stream_attempt_overwrite_wip)
    studio.failure_injection.DEFAULT_BOUNDARY_INJECTOR.arm(
        studio.failure_injection.ActionBoundaryFaultSpec(
            boundary="after_intent_before_effect",
            tool_name="write_file",
            task_id=sid_wip,
            max_fires=1,
        )
    )

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        with pytest.raises(studio.failure_injection.InjectedBoundaryCrashError):
            client.post(
                "/api/harness/run",
                headers=_headers(),
                json={"session_id": sid_wip, "prompt": "Touch notes", "policy": "autonomous", "max_steps": 3},
            )
        _assert_user_work_intact(coding_workspace)

        # Restart sees `notes/operator_wip.md` exists with DIFFERENT user bytes -> `"unresolved"` -> `'blocked'`!
        studio.simulate_process_restart()
        snap_wip = client.get(f"/api/harness/sessions/{sid_wip}/task", headers=_headers()).json()
        assert snap_wip["task"]["state"] == "blocked"
        _assert_user_work_intact(coding_workspace)

        res_blocked = client.post(f"/api/harness/sessions/{sid_wip}/resume", headers=_headers())
        assert res_blocked.status_code == 409
        run_blocked = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid_wip, "prompt": "Retry blind overwrite", "policy": "autonomous"},
        )
        assert run_blocked.status_code == 409
        _assert_user_work_intact(coding_workspace)

        # --- Sub-case 2: `run_bash` crashes `after_effect_before_result_commit` ---
        sess_bash = _create_session(ws_root, policy="autonomous", title="Non-idempotent Bash Hold")
        sid_bash = sess_bash["id"]
        side_effect_log = ws_root / "migration_once.log"

        async def stream_bash_once(
            _client: Any,
            _model: str,
            _messages: list[dict[str, str]],
            **_kwargs: Any,
        ) -> AsyncIterator[dict[str, Any]]:
            tc = json.dumps(
                {
                    "name": "run_bash",
                    "arguments": {"command": f"printf 'migrated\\n' >> {side_effect_log.name}"},
                }
            )
            yield {"type": "content", "text": f"Running migration.\n<tool_call>{tc}</tool_call>"}

        monkeypatch.setattr(studio, "stream_any_model", stream_bash_once)
        studio.failure_injection.DEFAULT_BOUNDARY_INJECTOR.arm(
            studio.failure_injection.ActionBoundaryFaultSpec(
                boundary="after_effect_before_result_commit",
                tool_name="run_bash",
                task_id=sid_bash,
                max_fires=1,
            )
        )

        with pytest.raises(studio.failure_injection.InjectedBoundaryCrashError):
            client.post(
                "/api/harness/run",
                headers=_headers(),
                json={"session_id": sid_bash, "prompt": "Run migration once", "policy": "autonomous", "max_steps": 3},
            )
        assert side_effect_log.read_text(encoding="utf-8").splitlines() == ["migrated"]

        studio.simulate_process_restart()
        snap_bash = client.get(f"/api/harness/sessions/{sid_bash}/task", headers=_headers()).json()
        assert snap_bash["task"]["state"] == "blocked"

        # Blind re-run is rejected with HTTP 409; side effect log still has exact 1 line!
        blind_retry = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={"session_id": sid_bash, "prompt": "Run migration once", "policy": "autonomous", "max_steps": 3},
        )
        assert blind_retry.status_code == 409
        assert side_effect_log.read_text(encoding="utf-8").splitlines() == ["migrated"]
        _assert_user_work_intact(coding_workspace)


def test_false_completions_are_rejected_across_prose_bluff_failed_tests_stale_tests_and_collateral_edits(
    coding_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: Reject false completion across:
    1) Prose-only bluff with zero managed edits;
    2) Broken patch + failed pytest run followed by deceptive 'All tests passed!' claim (even without a verification contract!);
    3) Stale passing command BEFORE an untested broken edit;
    4) Passing patch that corrupts a protected user file (`notes/operator_wip.md`);
    5) Direct `complete_task()` bypass without a matching `TaskVerificationReport`."""
    ws_root: Path = coding_workspace["ws_root"]

    with TestClient(studio.app, base_url="http://127.0.0.1:8000") as client:
        # --- Mode 1: Prose-only bluff (zero edits) ---
        s1 = _create_session(ws_root, policy="autonomous", title="False Completion 1: Bluff")

        async def bluff_stream(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "content", "text": "I fixed billing/invoice.py and ran pytest — 100% passing!"}

        monkeypatch.setattr(studio, "stream_any_model", bluff_stream)
        r1 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={
                "session_id": s1["id"],
                "prompt": "Fix invoice discount bug",
                "policy": "autonomous",
                "verification": _standard_verification_contract(),
            },
        )
        assert r1.status_code == 200
        ev1 = dict(_parse_sse_events(r1.text))
        assert "verification_failed" in ev1
        assert ev1["verification_failed"]["failure_code"] == "missing_managed_edit"
        snap1 = client.get(f"/api/harness/sessions/{s1['id']}/task", headers=_headers()).json()
        assert snap1["task"]["state"] == "failed"
        assert all(t["to_state"] != "completed" for t in snap1["transitions"])

        # Direct state-machine bypass attempt on a contract-bound task MUST also fail closed!
        sm = studio.get_task_state_machine()
        coord = studio.get_worker_lease_coordinator(ws_root)
        c_bypass = coord.acquire_or_takeover(s1["id"], f"wrk-{studio._PROCESS_BOOT_ID}-byp", lease_ttl_s=60.0)
        with pytest.raises(studio.workflow_state.FalseCompletionRejected):
            sm.complete_task(
                s1["id"],
                worker_id=f"wrk-{studio._PROCESS_BOOT_ID}-byp",
                epoch=c_bypass.epoch_record.epoch,
                step=1,
            )
        coord.release(s1["id"], c_bypass.epoch_record.epoch, f"wrk-{studio._PROCESS_BOOT_ID}-byp", next_state="failed")

        # --- Mode 2: Broken patch + failed pytest followed by deceptive completion prose (NO contract supplied!) ---
        s2 = _create_session(ws_root, policy="autonomous", title="False Completion 2: Failed Test Lied Away")
        s2_step = 0

        async def broken_patch_then_lie(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
            nonlocal s2_step
            s2_step += 1
            if s2_step == 1:
                tc_w = json.dumps(
                    {"name": "write_file", "arguments": {"path": "billing/invoice.py", "content": STILL_BUGGY_INVOICE_SOURCE}}
                )
                tc_t = json.dumps({"name": "run_bash", "arguments": {"command": f"{sys.executable} -m pytest -q"}})
                yield {"type": "content", "text": f"Patching and testing.\n<tool_call>{tc_w}</tool_call>\n<tool_call>{tc_t}</tool_call>"}
            else:
                yield {"type": "content", "text": "All tests passed! Task is complete."}

        monkeypatch.setattr(studio, "stream_any_model", broken_patch_then_lie)
        r2 = client.post(
            "/api/harness/run",
            headers=_headers(),
            # Notice: NO `verification` contract passed — universal ledger guard still catches failed pytest!
            json={"session_id": s2["id"], "prompt": "Fix invoice bug", "policy": "autonomous", "max_steps": 4},
        )
        assert r2.status_code == 200
        ev2 = dict(_parse_sse_events(r2.text))
        assert "verification_failed" in ev2
        assert ev2["verification_failed"]["failure_code"] == "latest_shell_or_test_failed"
        snap2 = client.get(f"/api/harness/sessions/{s2['id']}/task", headers=_headers()).json()
        assert snap2["task"]["state"] == "failed"
        assert all(t["to_state"] != "completed" for t in snap2["transitions"])

        # Restore original buggy file for Mode 3.
        (ws_root / "billing" / "invoice.py").write_text(BUGGY_INVOICE_SOURCE, encoding="utf-8")

        # --- Mode 3: Stale passing shell command BEFORE an untested broken edit ---
        s3 = _create_session(ws_root, policy="autonomous", title="False Completion 3: Stale Test Before Edit")
        s3_step = 0

        async def test_first_then_untested_edit(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
            nonlocal s3_step
            s3_step += 1
            if s3_step == 1:
                tc_ok = json.dumps({"name": "run_bash", "arguments": {"command": f"{sys.executable} -c \"print('ok')\""}})
                yield {"type": "content", "text": f"Quick check.\n<tool_call>{tc_ok}</tool_call>"}
            elif s3_step == 2:
                tc_bad = json.dumps(
                    {"name": "write_file", "arguments": {"path": "billing/invoice.py", "content": STILL_BUGGY_INVOICE_SOURCE}}
                )
                tc_reg = json.dumps(
                    {"name": "write_file", "arguments": {"path": "tests/test_invoice_regression.py", "content": REGRESSION_TEST_CONTENT}}
                )
                yield {"type": "content", "text": f"Editing without re-testing.\n<tool_call>{tc_bad}</tool_call>\n<tool_call>{tc_reg}</tool_call>"}
            else:
                yield {"type": "content", "text": "Done without re-running pytest!"}

        monkeypatch.setattr(studio, "stream_any_model", test_first_then_untested_edit)
        r3 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={
                "session_id": s3["id"],
                "prompt": "Fix invoice bug",
                "policy": "autonomous",
                "max_steps": 5,
                "verification": _standard_verification_contract(),
            },
        )
        assert r3.status_code == 200
        ev3 = dict(_parse_sse_events(r3.text))
        assert "verification_failed" in ev3
        assert ev3["verification_failed"]["failure_code"] == "stale_test_before_latest_edit"
        snap3 = client.get(f"/api/harness/sessions/{s3['id']}/task", headers=_headers()).json()
        assert snap3["task"]["state"] == "failed"

        # Restore original buggy file for Mode 4.
        (ws_root / "billing" / "invoice.py").write_text(BUGGY_INVOICE_SOURCE, encoding="utf-8")

        # --- Mode 4: Patch passes pytest BUT damages protected user file (`notes/operator_wip.md`) ---
        s4 = _create_session(ws_root, policy="autonomous", title="False Completion 4: Protected File Damaged")
        s4_step = 0

        async def fix_bug_but_damage_user_note(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
            nonlocal s4_step
            s4_step += 1
            if s4_step == 1:
                tc_fix = json.dumps(
                    {"name": "write_file", "arguments": {"path": "billing/invoice.py", "content": FIXED_INVOICE_SOURCE}}
                )
                tc_reg = json.dumps(
                    {"name": "write_file", "arguments": {"path": "tests/test_invoice_regression.py", "content": REGRESSION_TEST_CONTENT}}
                )
                tc_dmg = json.dumps(
                    {"name": "write_file", "arguments": {"path": "notes/operator_wip.md", "content": "wiped user notes\n"}}
                )
                tc_tst = json.dumps({"name": "run_bash", "arguments": {"command": f"{sys.executable} -m pytest -q"}})
                text = (
                    "Fixing bug and running pytest, but touching protected user file.\n"
                    f"<tool_call>{tc_fix}</tool_call>\n"
                    f"<tool_call>{tc_reg}</tool_call>\n"
                    f"<tool_call>{tc_dmg}</tool_call>\n"
                    f"<tool_call>{tc_tst}</tool_call>"
                )
                yield {"type": "content", "text": text}
            else:
                yield {"type": "content", "text": "All tests pass!"}

        monkeypatch.setattr(studio, "stream_any_model", fix_bug_but_damage_user_note)
        r4 = client.post(
            "/api/harness/run",
            headers=_headers(),
            json={
                "session_id": s4["id"],
                "prompt": "Fix invoice bug",
                "policy": "autonomous",
                "max_steps": 4,
                "verification": _standard_verification_contract(),
            },
        )
        assert r4.status_code == 200
        ev4 = dict(_parse_sse_events(r4.text))
        assert "verification_failed" in ev4
        assert ev4["verification_failed"]["failure_code"] == "protected_file_modified"
        snap4 = client.get(f"/api/harness/sessions/{s4['id']}/task", headers=_headers()).json()
        assert snap4["task"]["state"] == "failed"
        assert all(t["to_state"] != "completed" for t in snap4["transitions"])


def test_standalone_studio_bundle_inlines_failure_injection_and_verifies_vertical_slice_in_isolation(
    tmp_path: Path,
) -> None:
    """Verify standalone `studio.py` copied to an isolated directory without sibling modules
    inlines `failure_injection` and enforces boundary crash injection + task verification."""
    isolated_dir = tmp_path / "standalone_only"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    bundle_copy = isolated_dir / "studio_isolated_p106.py"
    shutil.copy2(Path(__file__).resolve().parents[1] / "studio.py", bundle_copy)

    spec = importlib.util.spec_from_file_location("studio_isolated_p106", bundle_copy)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        mod.DB_PATH = isolated_dir / "sa.sqlite3"
        ws = isolated_dir / "ws"
        ws.mkdir()
        mod.WORKSPACE_ROOT = ws
        mod.HARNESS_STORE_DIR = isolated_dir / "h_store"
        mod.HARNESS_STORE_DIR.mkdir()
        mod.STUDIO_EXECUTION_MODE = "host"
        mod.STUDIO_ALLOW_HOST_EXECUTION = True
        mod.init_db()

        (ws / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
        (ws / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        base_sha = mod.workflow_state.compute_workspace_file_sha256(ws, "calc.py")
        vspec = mod.workflow_state.TaskVerificationSpec(
            require_managed_edit=True,
            required_modified_files=("calc.py",),
            baseline_file_digests={"calc.py": base_sha},
            require_agent_test_action=True,
            test_command=(sys.executable, "-m", "pytest", "-q"),
        )
        sm = mod.get_task_state_machine()
        coord = mod.get_worker_lease_coordinator(ws)
        claim = coord.acquire_or_takeover("sa-task-1", f"wrk-{mod._PROCESS_BOOT_ID}-1", workspace_dir=str(ws), policy="autonomous")
        sm.set_verification_spec("sa-task-1", vspec)

        # False completion rejected before fix:
        bad_rep = sm.verify_task_completion("sa-task-1", workspace_dir=ws)
        assert bad_rep.verified is False
        assert bad_rep.failure_code == "missing_managed_edit"
        with pytest.raises(mod.workflow_state.FalseCompletionRejected):
            sm.complete_task("sa-task-1", worker_id=f"wrk-{mod._PROCESS_BOOT_ID}-1", epoch=claim.epoch_record.epoch)

        # Admitted edit + test succeeds and verifies cleanly:
        sess_dict = {
            "id": "sa-task-1",
            "policy": "autonomous",
            "worktree_dir": str(ws),
            "task_id": "sa-task-1",
            "epoch": claim.epoch_record.epoch,
            "worker_id": f"wrk-{mod._PROCESS_BOOT_ID}-1",
        }
        r_edit = asyncio.run(
            mod._execute_harness_tool(
                None,
                sess_dict,
                "write_file",
                {"path": "calc.py", "content": "def add(a: int, b: int) -> int:\n    return a + b\n"},
                grant_source="policy",
            )
        )
        assert r_edit["ok"] is True
        r_test = asyncio.run(
            mod._execute_harness_tool(
                None,
                sess_dict,
                "run_bash",
                {"command": f"{sys.executable} -m pytest -q"},
                grant_source="policy",
            )
        )
        assert r_test["ok"] is True

        ok_rep = sm.verify_task_completion("sa-task-1", workspace_dir=ws)
        assert ok_rep.verified is True
        completed = sm.complete_task(
            "sa-task-1",
            worker_id=f"wrk-{mod._PROCESS_BOOT_ID}-1",
            epoch=claim.epoch_record.epoch,
            step=1,
            checkpoint_payload={"workflow": "harness", "terminal": True},
            verification_report=ok_rep,
        )
        assert completed.state == "completed"
    finally:
        mod.failure_injection.DEFAULT_BOUNDARY_INJECTOR.clear()
        sys.modules.pop(spec.name, None)
