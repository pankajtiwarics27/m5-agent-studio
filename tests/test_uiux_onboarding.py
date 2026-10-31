"""UIUX-P2-01: Start readiness states remain honest and actionable."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _function(name: str, next_marker: str) -> str:
    start = FRONTEND.index(f"function {name}(")
    end = FRONTEND.index(next_marker, start)
    return FRONTEND[start:end]


def test_start_has_a_named_busy_checklist_and_explicit_recheck_control() -> None:
    assert '<div id="onboardingSteps" class="onboarding-steps" role="list" aria-label="Readiness steps" aria-busy="true">' in FRONTEND
    assert '<span id="onboardingSummary" class="status" data-tone="neutral" role="status" tabindex="-1">Checking...</span>' in FRONTEND
    assert 'id="onboardingCheckBtn" type="button" aria-controls="onboardingSteps"' in FRONTEND
    assert "Prepare first task" in FRONTEND


def test_each_step_exposes_its_state_and_renders_report_text_as_text() -> None:
    labels = _function("onboardingStepStatusLabel", "function renderOnboardingSteps(")
    for label in ('ready: "Ready"', 'optional: "Optional"', 'blocked: "Blocked"', '"action-required": "Action needed"'):
        assert label in labels
    render = _function("renderOnboardingSteps", "async function refreshOnboarding(")
    assert 'state.className = "onboarding-step-state"' in render
    assert 'state.textContent = onboardingStepStatusLabel(step.status)' in render
    assert 'title.textContent = String(step.title ?? "Untitled step")' in render
    assert 'detail.textContent = String(step.detail ?? "")' in render
    assert "list.innerHTML" not in render
    assert 'step.action === "run-first-task" ? "Prepare in Chat"' in render


def test_recheck_announces_loading_and_failure_time_without_dropping_last_result() -> None:
    refresh = _function("refreshOnboarding", "async function runOnboardingAction(")
    assert 'setStatus(summary, "busy", "Checking…")' in refresh
    assert 'list.setAttribute("aria-busy", "true")' in refresh
    assert 'list.removeAttribute("aria-busy")' in refresh
    assert 'setStatus(summary, "danger", `Check failed at ${failedAt}`)' in refresh
    assert 'cause: error && error.message ? String(error.message) : "the request failed"' in refresh
    assert "list.children.length" in refresh
    assert "The last successful check" in refresh
    assert 'button.disabled = true' in refresh and 'button.disabled = false' in refresh
    assert 'document.activeElement === checkBtn || list.contains(document.activeElement)' in refresh
    assert 'summary.focus({ preventScroll: true })' in refresh
    assert "requestVersion !== onboardingRefreshVersion" in refresh


def test_guided_first_task_prepares_chat_without_claiming_to_send() -> None:
    start = _function("startGuidedFirstTask", "// The rail is a tablist")
    assert 'switchTab("chat")' in start
    assert 'input.value = prompt' in start
    assert "sendChat" not in start
    assert "submitChat" not in start
    assert "The guided prompt is ready in Chat. Review it and press Send" in FRONTEND


def test_preflight_check_stays_read_only_except_for_the_explicit_local_probe() -> None:
    refresh = _function("refreshOnboarding", "async function runOnboardingAction(")
    assert 'const url = probeLocal ? "/api/onboarding/preflight?probe=local" : "/api/onboarding/preflight";' in refresh
    lede = re.search(r'<p class="onboarding-lede">(.*?)</p>', FRONTEND, re.S)
    assert lede
    assert "only the explicit local-model check contacts a runtime" in " ".join(lede.group(1).split())
