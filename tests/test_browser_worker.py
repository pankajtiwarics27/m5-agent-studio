"""AP-010 offline contract tests for the governed browser worker.

These tests never open a browser. The real-browser evidence lives in
``tests/test_browser_worker_real.py``, which runs against the pinned Chromium
from ``tests/browser/`` and skips (or fails, under ``M5_REQUIRE_BROWSER_TESTS=1``)
when it is not configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import browser_worker as bw
from browser_worker import (
    ActivationRefusedError,
    BakeOffHarness,
    BridgeDriver,
    BrowserAction,
    BrowserSession,
    BrowserWorkerError,
    PolicyViolation,
    SessionClass,
    SessionFencedError,
    SessionPolicy,
    TakeoverConflictError,
    TaskScript,
    allocate_profile,
    assert_not_personal_profile,
    classify_popup,
    compare_outcomes,
    is_private_network_target,
    journal_digest,
    recover_session,
)

FIXED = "2026-11-20T17:30:00Z"
ORIGIN = "http://127.0.0.1:8000"


def clock() -> str:
    return FIXED


def policy(**overrides) -> SessionPolicy:
    base = dict(session_class=SessionClass.OFFLINE, allowed_origins=(ORIGIN,))
    base.update(overrides)
    return SessionPolicy(**base)


def session(tmp_path: Path, *, driver=None, **overrides) -> BrowserSession:
    prof = allocate_profile("sess-1", workspace_root=tmp_path, clock=clock)
    return BrowserSession(
        session_id="sess-1",
        policy=policy(**overrides),
        profile=prof,
        driver=driver,
        clock=clock,
    )


class FakeDriver:
    """Records what the policy actually let through."""

    def __init__(self, *, fail_on: str | None = None, write_bytes: int | None = None):
        self.opened: list[str] = []
        self.downloads: list[tuple[str, int]] = []
        self.closed = False
        self.fail_on = fail_on
        self.write_bytes = write_bytes

    def open_page(self, url: str, *, profile_path: str) -> str:
        if self.fail_on == "open":
            raise RuntimeError("browser crashed")
        self.opened.append(url)
        return f"page-{len(self.opened)}"

    def close_page(self, page_id: str) -> None:
        return None

    def download(self, url: str, *, max_bytes: int) -> int:
        self.downloads.append((url, max_bytes))
        return self.write_bytes if self.write_bytes is not None else max_bytes

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------- activation gate

def test_activation_is_refused_outright():
    with pytest.raises(ActivationRefusedError):
        SessionPolicy(activation_authorized=True)


def test_unknown_session_class_is_refused():
    with pytest.raises(BrowserWorkerError):
        SessionPolicy(session_class="production")


def test_private_network_grant_is_refused_even_if_asked():
    with pytest.raises(PolicyViolation):
        SessionPolicy(allow_private_network=True)


# --------------------------------------------------------------- profiles (AC2)

def test_profile_is_allocated_inside_the_workspace(tmp_path: Path):
    record = allocate_profile("proj-a", workspace_root=tmp_path, clock=clock)
    assert record.path.startswith(str(tmp_path))
    assert Path(record.path).is_dir()
    assert record.isolated is True
    assert record.created_at == FIXED


@pytest.mark.parametrize(
    "path",
    [
        "/home/user/.config/google-chrome/Default",
        "/home/user/.config/chromium/Default",
        "C:/Users/me/AppData/Local/Google/Chrome/User Data",
        "/Users/me/Library/Application Support/Google/Chrome",
        "/home/user/.mozilla/firefox/abc.default",
    ],
)
def test_personal_profiles_are_refused(path):
    with pytest.raises(PolicyViolation):
        assert_not_personal_profile(path)


def test_profile_outside_workspace_but_harmless_is_allowed(tmp_path: Path):
    """A caller-owned ephemeral root is not a personal profile.

    The rule is name-based against personal application/config roots, so an
    isolated profile in an ordinary directory is permitted; the profile is still
    per-session and destroyed on stop. See the workspace docs for why the check
    is not "anything under $HOME".
    """
    assert_not_personal_profile("/tmp/some-run/profiles/sess-1")


def test_ephemeral_temp_root_under_home_is_allowed(monkeypatch):
    """CI runs with HOME=/home/runner and its tmp under /home/runner/work/_temp.

    A profile under the system temp directory is legitimate scratch space and
    must not be refused merely because HOME contains it. This is the exact
    condition that made every real-browser test fail in CI while passing
    locally, where tmp_path lives outside HOME.
    """
    fake_home = Path("/home/runner")
    monkeypatch.setattr(bw.Path, "home", classmethod(lambda cls: fake_home))
    assert_not_personal_profile("/home/runner/work/_temp/m5-browser/tmp/profiles/sess-1")


def test_temp_exception_does_not_admit_personal_dirs(monkeypatch):
    """The temp exception must not weaken the personal-profile refusal."""
    monkeypatch.setattr(bw.Path, "home", classmethod(lambda cls: Path("/home/runner")))
    with pytest.raises(PolicyViolation, match="personal browser profile"):
        assert_not_personal_profile("/home/runner/.config/google-chrome/Default")


@pytest.mark.parametrize(
    "path",
    [
        "/home/operator/.config/google-chrome/Default",
        "/home/operator/.local/share/chromium/Default",
        "/home/operator/.mozilla/firefox/abc.default",
        "/home/operator/Library/Application Support/Google/Chrome",
        "/home/operator/AppData/Local/Google/Chrome/User Data",
    ],
)
def test_personal_application_roots_are_refused(path, monkeypatch):
    monkeypatch.setattr(bw.Path, "home", classmethod(lambda cls: Path("/home/operator")))
    with pytest.raises(PolicyViolation):
        assert_not_personal_profile(path)


def test_workspace_inside_home_is_allowed(monkeypatch):
    monkeypatch.setattr(bw.Path, "home", classmethod(lambda cls: Path("/home/operator")))
    assert_not_personal_profile("/home/operator/M5_AP_Workspace/browser/profiles/sess-1")


def test_two_sessions_never_share_a_profile(tmp_path: Path):
    a = allocate_profile("proj-a", workspace_root=tmp_path, clock=clock)
    b = allocate_profile("proj-b", workspace_root=tmp_path, clock=clock)
    assert a.path != b.path


def test_bad_session_id_is_refused(tmp_path: Path):
    with pytest.raises(BrowserWorkerError):
        allocate_profile("../escape", workspace_root=tmp_path, clock=clock)


# --------------------------------------------------------------- grants (AC3)

def test_navigation_outside_granted_origin_is_refused(tmp_path: Path):
    s = session(tmp_path)
    decision = s.admit(BrowserAction("navigate", url="https://evil.example.com/"))
    assert decision.allowed is False
    assert "origin" in (decision.reason or "")


def test_navigation_inside_granted_origin_is_allowed(tmp_path: Path):
    s = session(tmp_path, driver=FakeDriver())
    result = s.perform(BrowserAction("navigate", url=f"{ORIGIN}/start"))
    assert result["url"] == f"{ORIGIN}/start"


def test_private_network_navigation_is_refused(tmp_path: Path):
    s = session(tmp_path, allowed_origins=("http://169.254.169.254",))
    decision = s.admit(BrowserAction("navigate", url="http://169.254.169.254/latest/meta-data"))
    assert decision.allowed is False


def test_file_and_javascript_schemes_are_refused(tmp_path: Path):
    s = session(tmp_path)
    for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings"):
        assert s.admit(BrowserAction("navigate", url=url)).allowed is False


def test_url_with_userinfo_is_refused(tmp_path: Path):
    s = session(tmp_path)
    assert s.admit(BrowserAction("navigate", url="http://u:p@127.0.0.1:8000/")).allowed is False


def test_navigation_budget_is_enforced_before_the_driver_sees_it(tmp_path: Path):
    driver = FakeDriver()
    s = session(tmp_path, driver=driver, max_navigations=2)
    for _ in range(2):
        s.perform(BrowserAction("navigate", url=f"{ORIGIN}/x"))
    decision = s.admit(BrowserAction("navigate", url=f"{ORIGIN}/x"))
    assert decision.allowed is False
    assert "budget" in (decision.reason or "")
    assert len(driver.opened) == 2


def test_downloads_are_off_by_default(tmp_path: Path):
    s = session(tmp_path)
    assert s.admit(BrowserAction("download", url=f"{ORIGIN}/f", download_bytes=10)).allowed is False


def test_download_requires_a_declared_size(tmp_path: Path):
    s = session(tmp_path, allow_downloads=True)
    decision = s.admit(BrowserAction("download", url=f"{ORIGIN}/f"))
    assert decision.allowed is False
    assert "declared" in (decision.reason or "")


def test_download_respects_nested_byte_budget(tmp_path: Path):
    s = session(
        tmp_path,
        allow_downloads=True,
        max_downloads=5,
        max_download_bytes=1000,
        max_total_download_bytes=1500,
        driver=FakeDriver(write_bytes=900),
    )
    s.perform(BrowserAction("download", url=f"{ORIGIN}/a", download_bytes=900))
    decision = s.admit(BrowserAction("download", url=f"{ORIGIN}/b", download_bytes=900))
    assert decision.allowed is False
    assert "nested byte budget" in (decision.reason or "")


def test_per_download_cap_is_enforced(tmp_path: Path):
    s = session(tmp_path, allow_downloads=True, max_download_bytes=100, max_total_download_bytes=100)
    assert s.admit(BrowserAction("download", url=f"{ORIGIN}/f", download_bytes=101)).allowed is False


def test_driver_writing_more_than_the_cap_is_a_violation(tmp_path: Path):
    s = session(
        tmp_path,
        allow_downloads=True,
        max_download_bytes=100,
        max_total_download_bytes=100,
        driver=FakeDriver(write_bytes=500),
    )
    with pytest.raises(PolicyViolation):
        s.perform(BrowserAction("download", url=f"{ORIGIN}/f", download_bytes=50))


def test_refused_action_never_reaches_the_driver(tmp_path: Path):
    driver = FakeDriver()
    s = session(tmp_path, driver=driver)
    with pytest.raises(PolicyViolation):
        s.perform(BrowserAction("navigate", url="https://evil.example.com/"))
    assert driver.opened == []


def test_refusal_is_journaled(tmp_path: Path):
    s = session(tmp_path)
    with pytest.raises(PolicyViolation):
        s.perform(BrowserAction("navigate", url="https://evil.example.com/"))
    events = [entry["event"] for entry in s.state.journal]
    assert "refused" in events


# --------------------------------------------------------------- takeover (AC4)

def test_takeover_suspends_agent_actions(tmp_path: Path):
    s = session(tmp_path, driver=FakeDriver())
    s.take_over("alice")
    decision = s.admit(BrowserAction("navigate", url=f"{ORIGIN}/x"))
    assert decision.allowed is False
    assert "takeover" in (decision.reason or "")


def test_resume_by_the_same_human_restores_control(tmp_path: Path):
    s = session(tmp_path, driver=FakeDriver())
    s.take_over("alice")
    s.resume("alice")
    assert s.admit(BrowserAction("navigate", url=f"{ORIGIN}/x")).allowed is True


def test_a_different_human_cannot_resume(tmp_path: Path):
    s = session(tmp_path)
    s.take_over("alice")
    with pytest.raises(TakeoverConflictError):
        s.resume("mallory")
    assert s.state.takeover_active is True


def test_double_takeover_is_refused(tmp_path: Path):
    s = session(tmp_path)
    s.take_over("alice")
    with pytest.raises(TakeoverConflictError):
        s.take_over("bob")


def test_resume_without_takeover_is_refused(tmp_path: Path):
    s = session(tmp_path)
    with pytest.raises(TakeoverConflictError):
        s.resume("alice")


def test_takeover_requires_an_identified_human(tmp_path: Path):
    s = session(tmp_path)
    with pytest.raises(TakeoverConflictError):
        s.take_over("   ")


# --------------------------------------------------------------- popups (AC5)

def test_popups_are_refused_by_default(tmp_path: Path):
    decision = classify_popup(f"{ORIGIN}/popup", policy=policy(), opener_origin=ORIGIN)
    assert decision.allowed is False


def test_popup_inside_granted_origin_can_be_allowed(tmp_path: Path):
    decision = classify_popup(
        f"{ORIGIN}/popup", policy=policy(allow_popups=True), opener_origin=ORIGIN
    )
    assert decision.allowed is True


def test_popup_to_other_origin_is_refused_even_when_granted(tmp_path: Path):
    p = policy(allow_popups=True, allowed_origins=(ORIGIN, "https://ok.example.com"))
    decision = classify_popup("https://ok.example.com/x", policy=p, opener_origin=ORIGIN)
    assert decision.allowed is False
    assert "leaves the opener" in decision.reason


def test_popup_to_private_network_is_refused(tmp_path: Path):
    p = policy(allow_popups=True, allowed_origins=(ORIGIN, "http://192.168.1.5"))
    decision = classify_popup("http://192.168.1.5/x", policy=p, opener_origin=ORIGIN)
    assert decision.allowed is False
    assert "private" in decision.reason


def test_popup_with_javascript_scheme_is_refused(tmp_path: Path):
    p = policy(allow_popups=True)
    assert classify_popup("javascript:alert(1)", policy=p, opener_origin=ORIGIN).allowed is False


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.4.4/",
        "http://[::1]/",
        "http://127.0.0.1/",
    ],
)
def test_private_targets_are_detected(url):
    assert is_private_network_target(url) is True


@pytest.mark.parametrize("url", ["https://example.com/", "http://93.184.216.34/"])
def test_public_targets_are_not_flagged(url):
    assert is_private_network_target(url) is False


# --------------------------------------------------------------- stop/recover (AC6)

def test_stop_fences_the_session_and_closes_the_driver(tmp_path: Path):
    driver = FakeDriver()
    s = session(tmp_path, driver=driver)
    state = s.stop()
    assert state.fenced is True
    assert driver.closed is True
    with pytest.raises(SessionFencedError):
        s.perform(BrowserAction("navigate", url=f"{ORIGIN}/x"))


def test_stop_destroys_the_owned_profile(tmp_path: Path):
    s = session(tmp_path)
    profile_path = Path(s.profile.path)
    (profile_path / "Cookies").write_text("synthetic")
    s.stop()
    assert not profile_path.exists()


def test_stop_is_idempotent(tmp_path: Path):
    s = session(tmp_path)
    first = s.stop()
    second = s.stop()
    assert first.stopped_at == second.stopped_at


def test_driver_failure_on_stop_still_fences(tmp_path: Path):
    class Exploding(FakeDriver):
        def close(self) -> None:
            raise RuntimeError("browser already gone")

    s = session(tmp_path, driver=Exploding())
    state = s.stop()
    assert state.fenced is True


def test_stopped_session_cannot_be_revived_in_place(tmp_path: Path):
    s = session(tmp_path)
    s.stop()
    with pytest.raises(SessionFencedError):
        s.take_over("alice")


def test_recovery_needs_a_fresh_profile_and_never_restores_authority(tmp_path: Path):
    s = session(tmp_path, driver=FakeDriver())
    s.perform(BrowserAction("navigate", url=f"{ORIGIN}/x"))
    stopped = s.stop()
    fresh = allocate_profile("sess-1", workspace_root=tmp_path, clock=clock)
    recovered = recover_session(
        stopped, policy=policy(max_navigations=1), profile=fresh, clock=clock
    )
    assert recovered.fenced is False
    # The old navigation budget is NOT restored; the new policy starts clean.
    assert recovered.state.navigations == 0
    assert recovered.admit(BrowserAction("navigate", url=f"{ORIGIN}/y")).allowed is True
    events = [e["event"] for e in recovered.state.journal]
    assert "recovered" in events


def test_recovery_of_a_live_session_is_refused(tmp_path: Path):
    s = session(tmp_path)
    with pytest.raises(BrowserWorkerError):
        recover_session(s.state, policy=policy(), profile=s.profile)


def test_recovery_with_someone_elses_profile_is_refused(tmp_path: Path):
    s = session(tmp_path)
    stopped = s.stop()
    other = allocate_profile("sess-2", workspace_root=tmp_path, clock=clock)
    with pytest.raises(BrowserWorkerError):
        recover_session(stopped, policy=policy(), profile=other)


# --------------------------------------------------------------- secrets

def test_journal_redacts_secret_shaped_fields(tmp_path: Path):
    s = session(tmp_path)
    s.state.journal.append(
        {"at": FIXED, "event": "test", "api_token": "supersecret", "Authorization": "Bearer x"}
    )
    blob = json.dumps(s.state.to_dict())
    assert "supersecret" not in blob
    assert "redacted" in blob


def test_journal_digest_is_stable_and_changes_with_content(tmp_path: Path):
    s = session(tmp_path)
    first = journal_digest(s)
    assert first == journal_digest(s)
    s._journal("something")
    assert journal_digest(s) != first


# --------------------------------------------------------------- bake-off (AC1)

def test_bakeoff_reports_equivalence_when_both_drivers_agree(tmp_path: Path):
    task = TaskScript(
        "same-task",
        (
            BrowserAction("navigate", url=f"{ORIGIN}/a"),
            BrowserAction("screenshot"),
        ),
    )
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(),
        workspace_root=tmp_path,
        clock=clock,
    )
    result = harness.run([task])
    assert result.equivalent is True
    assert result.divergences() == ()
    assert len(result.baseline) == 1 and len(result.candidate) == 1


def test_bakeoff_exposes_divergence_when_the_candidate_fails(tmp_path: Path):
    task = TaskScript("task", (BrowserAction("navigate", url=f"{ORIGIN}/a"),))
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(fail_on="open"),
        workspace_root=tmp_path,
        clock=clock,
    )
    result = harness.run([task])
    assert result.equivalent is False
    assert result.divergences()


def test_bakeoff_counts_refusals_identically_and_never_widens_policy(tmp_path: Path):
    task = TaskScript(
        "escape-attempt",
        (BrowserAction("navigate", url="https://evil.example.com/"),),
    )
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(),
        workspace_root=tmp_path,
        clock=clock,
    )
    result = harness.run([task])
    assert result.baseline[0].refused == 1
    assert result.candidate[0].refused == 1
    assert result.baseline[0].completed is False
    assert result.baseline[0].executed == 0


def test_bakeoff_requires_at_least_one_task(tmp_path: Path):
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(),
        workspace_root=tmp_path,
        clock=clock,
    )
    with pytest.raises(BrowserWorkerError):
        harness.run([])


def test_compare_outcomes_rejects_mismatched_task_sets(tmp_path: Path):
    one = TaskScript("a", ())
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(),
        workspace_root=tmp_path,
        clock=clock,
    )
    base = harness.run([one]).baseline
    with pytest.raises(BrowserWorkerError):
        compare_outcomes(base, base + base[:1])


def test_bakeoff_leaves_no_profile_behind(tmp_path: Path):
    task = TaskScript("t", (BrowserAction("navigate", url=f"{ORIGIN}/a"),))
    harness = BakeOffHarness(
        policy=policy(),
        baseline_driver=FakeDriver(),
        candidate_driver=FakeDriver(),
        workspace_root=tmp_path,
        clock=clock,
    )
    harness.run([task])
    leftovers = list((tmp_path / "browser").rglob("*")) if (tmp_path / "browser").exists() else []
    assert [p for p in leftovers if p.is_dir()] == []


# --------------------------------------------------------------- bridge guard rails

def test_bridge_reports_missing_binary_rather_than_launching(tmp_path: Path):
    driver = BridgeDriver(chrome=tmp_path / "nope", puppeteer_core=tmp_path / "nope.js")
    with pytest.raises(BrowserWorkerError, match="chromium not found"):
        driver.run_plan([{"name": "t", "steps": []}])


def test_bridge_refuses_to_transfer_downloads(tmp_path: Path):
    driver = BridgeDriver(chrome=tmp_path / "c", puppeteer_core=tmp_path / "p.js")
    with pytest.raises(BrowserWorkerError, match="does not transfer downloads"):
        driver.download(f"{ORIGIN}/f", max_bytes=10)
