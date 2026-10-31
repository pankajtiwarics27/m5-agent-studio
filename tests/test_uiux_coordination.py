"""Read-only, fail-closed UI/UX session coordination (no live GitHub calls)."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "uiux_coordination.py"
spec = importlib.util.spec_from_file_location("uiux_coordination", SCRIPT)
assert spec and spec.loader
coord = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coord)


def tracking(status: str) -> dict:
    result = {"status": status, "progress_percent": 0, "owner_session": None,
              "completed_at": None, "commit_sha": None, "evidence": [], "test_results": []}
    if status == "done":
        result.update(progress_percent=100, owner_session="arena/example", completed_at="2026-10-31T00:00:00Z",
                      commit_sha="d" * 40, evidence=["fixture evidence"], test_results=["fixture test"])
    elif status == "in_progress":
        result.update(progress_percent=10, owner_session="arena/example")
    return result


def story(sid: str, status: str, deps: list[str] | None = None, priority: str = "P0") -> dict:
    return {
        "id": sid,
        "title": f"Story {sid}",
        "phase": sid.split("-")[1],
        "priority": priority,
        "dependencies": deps or [],
        "tracking": tracking(status),
    }


def refresh_counts(data: dict) -> dict:
    stories = data["stories"]
    data["overall_progress"] = {"total_stories": len(stories), **{
        status: sum(s["tracking"]["status"] == status for s in stories) for status in coord.STATUSES
    }, "percent_complete": round(100 * sum(s["tracking"]["status"] == "done" for s in stories) / len(stories))}
    data["phases"] = []
    for phase_id in sorted({s["phase"] for s in stories}):
        members = [s for s in stories if s["phase"] == phase_id]
        data["phases"].append({"id": phase_id, "story_ids": [s["id"] for s in members], "total": len(members), **{
            status: sum(s["tracking"]["status"] == status for s in members) for status in coord.STATUSES
        }, "percent_complete": round(100 * sum(s["tracking"]["status"] == "done" for s in members) / len(members))})
    return data


def ledger() -> dict:
    # The later-phase story deliberately has no dependencies: phase order alone
    # must keep it from being suggested before the complete P0 gate closes.
    return refresh_counts({
        "repository": "https://github.com/example/project",
        "branch": "main",
        "source_commit_sha": "a" * 40,
        "stories": [
            story("UIUX-P0-01", "done"),
            story("UIUX-P0-02", "not_started", ["UIUX-P0-01"]),
            story("UIUX-P0-03", "not_started", ["UIUX-P0-02"]),
            story("UIUX-P1-01", "not_started"),
        ],
        "session_state": {"active_story": None, "active_stories": [], "next_recommended_story": "UIUX-P0-02"},
    })


def local(*, clean: bool = True, branch: str | None = "arena/session", pin: str = "ancestor") -> dict:
    return {"head": "b" * 40, "branch": branch, "clean": clean, "changed_entries": 0 if clean else 1,
            "pin_relation": pin}


def online(prs: list[dict] | None = None, *, relation: str = "current", complete: bool = True,
           ahead: int | None = 0, issues: list[dict] | None = None, issues_complete: bool = True) -> dict:
    return {"main_branch": "main", "main_sha": "c" * 40, "main_relation": relation, "commits_ahead": ahead,
            "pr_scan_complete": complete, "prs": prs or [], "issues": issues or [],
            "issue_scan_complete": issues_complete}


def pr(number: int, title: str, body: str = "", branch: str = "arena/other") -> dict:
    return {"number": number, "title": title, "body": body, "headRefName": branch,
            "url": f"https://github.com/example/project/pull/{number}", "mergeable": "MERGEABLE", "isDraft": False}


def test_local_readiness_obeys_dependencies_priority_and_previous_phase_gate() -> None:
    data = ledger()
    candidates = coord.local_candidates(coord.validate_stories(data))
    assert [s["id"] for s in candidates] == ["UIUX-P0-02"]
    data["stories"][1]["tracking"] = tracking("blocked")
    refresh_counts(data)
    assert coord.local_candidates(coord.validate_stories(data)) == []
    # Once all P0 stories are done, the dependency-independent P1 story can run.
    for item in data["stories"][:3]:
        item["tracking"] = tracking("done")
    refresh_counts(data)
    assert [s["id"] for s in coord.local_candidates(coord.validate_stories(data))] == ["UIUX-P1-01"]


@pytest.mark.parametrize("field", ["unknowns", "blockers"])
def test_declared_blockers_keep_a_not_started_story_ineligible(field: str) -> None:
    data = ledger()
    if field == "unknowns":
        data["stories"][1]["blocked_by_unknowns"] = ["unresolved contract"]
    else:
        data["stories"][1]["tracking"]["blockers"] = ["unresolved contract"]
    report = coord.make_report(data, local(), online())
    assert report["locally_eligible"] == []
    assert report["not_started_with_declared_blockers"] == ["UIUX-P0-02"]
    assert report["next_candidate"] is None


def test_priority_and_stable_story_order_within_the_ready_phase() -> None:
    data = ledger()
    data["stories"] = [story("UIUX-P0-01", "not_started", priority="P2"),
                       story("UIUX-P0-02", "not_started", priority="P0"),
                       story("UIUX-P0-03", "not_started", priority="P0")]
    refresh_counts(data)
    assert [s["id"] for s in coord.local_candidates(coord.validate_stories(data))] == [
        "UIUX-P0-02", "UIUX-P0-03", "UIUX-P0-01"
    ]


@pytest.mark.parametrize("violation", ["dependency", "phase"])
def test_started_story_with_unfinished_prerequisites_is_rejected(violation: str) -> None:
    data = ledger()
    if violation == "dependency":
        data["stories"][2]["tracking"] = tracking("in_progress")
    else:
        # The P1 fixture has no explicit dependencies, so only the phase gate
        # detects this inconsistent start while P0 is still open.
        data["stories"][3]["tracking"] = tracking("in_progress")
    refresh_counts(data)
    with pytest.raises(coord.CoordinationError, match="started before"):
        coord.make_report(data, local(), online())


def test_offline_report_never_offers_a_claim_candidate() -> None:
    report = coord.make_report(ledger(), local())
    assert report["locally_eligible"][0]["id"] == "UIUX-P0-02"
    assert report["next_candidate"] is None
    assert not report["github"]["checked"]
    assert any("not checked" in hold for hold in report["holds"])


def test_live_complete_snapshot_can_suggest_but_never_claim() -> None:
    report = coord.make_report(ledger(), local(), online())
    assert report["next_candidate"]["id"] == "UIUX-P0-02"
    assert report["read_only"] is True
    assert "not an atomic claim" in report["coordination_notice"]
    assert report["holds"] == []


def test_story_mentioned_only_in_pr_body_holds_the_whole_program() -> None:
    # The open PR's title and Arena branch can be generic, like the real PR;
    # its claim may exist only in the body and not in the checked-out ledger.
    pending = pr(104, "Map frontend handlers to routes", "Closes `UIUX-P0-02`.")
    pending["mergeable"] = "CONFLICTING"
    report = coord.make_report(ledger(), local(), online([pending]))
    assert report["open_pull_requests"][0]["story_mentions"] == ["UIUX-P0-02"]
    assert report["open_pull_requests"][0]["mergeable"] == "CONFLICTING"
    assert report["next_candidate"] is None
    assert any("#104" in hold for hold in report["holds"])


def test_ambiguous_prs_and_unknown_story_ids_are_not_silently_ignored() -> None:
    data = ledger()
    pending = [pr(1, "Improve interface", "No story ID"),
               pr(2, "UI/UX work", ""),
               pr(3, "Some work", "Mentions UIUX-P9-99")]
    report = coord.make_report(data, local(), online(pending))
    assert [row["classification"] for row in report["open_pull_requests"]] == [
        "unclassified", "uiux_or_possible_uiux", "uiux_or_possible_uiux"
    ]
    assert report["open_pull_requests"][2]["unknown_story_ids"] == ["UIUX-P9-99"]
    assert report["next_candidate"] is None
    assert all(f"#{num}" in " ".join(report["holds"]) for num in (1, 2, 3))


def test_announced_open_issue_holds_even_when_ledger_prerequisites_are_done() -> None:
    issues = [
        {"number": 108, "title": "Other issue", "url": "https://github.com/example/project/issues/108",
         "body": "No coordination marker."},
        {"number": 110, "title": "Fix the route map", "url": "https://github.com/example/project/issues/110",
         "body": "UIUX-COORDINATION-BLOCKER: UIUX-P0-01\n\nAn issue found after merge."},
    ]
    report = coord.make_report(ledger(), local(), online(issues=issues))
    assert [issue["number"] for issue in report["open_blocking_issues"]] == [110]
    assert report["open_blocking_issues"][0]["scope"] == "UIUX-P0-01"
    assert report["next_candidate"] is None
    assert any("#110" in hold for hold in report["holds"])
    assert "Issue #110" in coord.render(report)


def test_incomplete_issue_scan_holds_and_unscoped_marker_still_blocks() -> None:
    issue = {"number": 111, "title": "Unknown scope", "url": "https://github.com/example/project/issues/111",
             "body": "uiux-coordination-blocker:\n"}
    report = coord.make_report(ledger(), local(), online(issues=[issue], issues_complete=False))
    assert report["open_blocking_issues"][0]["scope"] == "unspecified"
    assert report["next_candidate"] is None
    assert any("open-issue scan" in hold for hold in report["holds"])


def test_invalid_issue_data_is_rejected_instead_of_hiding_a_blocker() -> None:
    with pytest.raises(coord.CoordinationError):
        coord.inspect_blocking_issues([{"number": 110}])
    with pytest.raises(coord.CoordinationError):
        coord.inspect_blocking_issues({"not": "a list"})


def test_explicit_other_program_pr_is_shown_but_not_assumed_to_be_a_uiux_claim() -> None:
    pending = pr(106, "M5-P6-03: Advanced calibration", "Includes UIUX source-manifest checks.")
    report = coord.make_report(ledger(), local(), online([pending]))
    assert report["open_pull_requests"][0]["classification"] == "other_program"
    assert report["next_candidate"]["id"] == "UIUX-P0-02"
    assert any("shared-file conflicts" in warning for warning in report["warnings"])


@pytest.mark.parametrize("relation", ["unknown", "behind_or_diverged"])
def test_unknown_or_outdated_main_holds_claim(relation: str) -> None:
    report = coord.make_report(ledger(), local(), online(relation=relation))
    assert report["next_candidate"] is None
    assert any("current GitHub main" in hold for hold in report["holds"])


@pytest.mark.parametrize("ahead", [1, None])
def test_unmerged_or_unknown_local_commit_count_holds_claim(ahead: int | None) -> None:
    report = coord.make_report(ledger(), local(), online(ahead=ahead))
    assert report["next_candidate"] is None
    assert any("unmerged commits" in hold for hold in report["holds"])


def test_truncated_pr_scan_dirty_tree_and_detached_head_hold_claim() -> None:
    report = coord.make_report(ledger(), local(clean=False, branch=None), online(complete=False))
    assert report["next_candidate"] is None
    assert any("not clean" in hold for hold in report["holds"])
    assert any("detached" in hold for hold in report["holds"])
    assert any("incomplete" in hold for hold in report["holds"])


@pytest.mark.parametrize("pin", ["unknown", "not_ancestor"])
def test_source_pin_must_be_a_confirmed_ancestor(pin: str) -> None:
    report = coord.make_report(ledger(), local(pin=pin), online())
    assert report["next_candidate"] is None
    assert any("source pin" in hold for hold in report["holds"])


def test_local_in_progress_and_session_state_inconsistency_hold_claim() -> None:
    data = ledger()
    data["stories"][1]["tracking"] = tracking("in_progress")
    refresh_counts(data)
    report = coord.make_report(data, local(), online())
    assert report["next_candidate"] is None
    assert any("already in progress" in hold for hold in report["holds"])
    assert any("disagree" in hold for hold in report["holds"])
    data["session_state"]["active_story"] = "UIUX-P0-02"
    data["session_state"]["active_stories"] = ["UIUX-P0-02"]
    report = coord.make_report(data, local(), online())
    assert not any("disagree" in hold for hold in report["holds"])
    assert report["next_candidate"] is None


def test_missing_session_state_is_not_treated_as_no_active_work() -> None:
    data = ledger()
    data.pop("session_state")
    with pytest.raises(coord.CoordinationError, match="session_state"):
        coord.make_report(data, local(), online())


def test_ledger_recommendation_disagreement_is_a_hold() -> None:
    data = ledger()
    data["session_state"]["next_recommended_story"] = "UIUX-P1-01"
    report = coord.make_report(data, local(), online())
    assert report["next_candidate"] is None
    assert any("recommendation is inconsistent" in hold for hold in report["holds"])


@pytest.mark.parametrize("mutation", ["duplicate", "unknown_dep", "cycle", "status", "counters",
                                      "overall_percent", "phase_percent", "no_evidence", "no_tests",
                                      "bad_commit", "bad_pin", "phase_drift"])
def test_malformed_ledger_is_rejected_instead_of_suggesting_a_story(mutation: str) -> None:
    data = ledger()
    if mutation == "duplicate":
        data["stories"].append(deepcopy(data["stories"][0]))
    elif mutation == "unknown_dep":
        data["stories"][1]["dependencies"] = ["UIUX-P5-99"]
    elif mutation == "cycle":
        data["stories"][0]["dependencies"] = ["UIUX-P0-02"]
    elif mutation == "status":
        data["stories"][1]["tracking"]["status"] = "maybe"
    elif mutation == "counters":
        data["overall_progress"] = {"total_stories": 999}
    elif mutation == "overall_percent":
        data["overall_progress"]["percent_complete"] = 99
    elif mutation == "phase_percent":
        data["phases"][0]["percent_complete"] = 99
    elif mutation == "no_evidence":
        data["stories"][0]["tracking"]["evidence"] = []
    elif mutation == "no_tests":
        data["stories"][0]["tracking"]["test_results"] = []
    elif mutation == "bad_commit":
        data["stories"][0]["tracking"]["commit_sha"] = "not-a-sha"
    elif mutation == "bad_pin":
        data["source_commit_sha"] = "main"
    else:
        data["phases"][0]["story_ids"].pop()
    with pytest.raises(coord.CoordinationError):
        coord.make_report(data, local(), online())


def test_default_cli_does_not_call_github(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(coord, "read_ledger", ledger)
    monkeypatch.setattr(coord, "git_state", lambda _pin: local())

    def forbidden(_ledger: dict) -> dict:
        raise AssertionError("offline mode must not call GitHub")

    monkeypatch.setattr(coord, "github_state", forbidden)
    assert coord.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["github"]["checked"] is False
    assert report["next_candidate"] is None


def test_github_failure_exits_nonzero_without_fallback_recommendation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(coord, "read_ledger", ledger)
    monkeypatch.setattr(coord, "git_state", lambda _pin: local())

    def unavailable(_ledger: dict) -> dict:
        raise coord.CoordinationError("GitHub unavailable")

    monkeypatch.setattr(coord, "github_state", unavailable)
    assert coord.main(["--github", "--json"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "No claim recommendation" in output.err


def test_invalid_or_incomplete_pr_response_is_rejected() -> None:
    with pytest.raises(coord.CoordinationError):
        coord.inspect_pull_requests([{"number": 104}], set())
    with pytest.raises(coord.CoordinationError):
        coord.inspect_pull_requests({"not": "a list"}, set())


def test_remote_repo_is_constrained_to_the_ledger_github_url() -> None:
    data = ledger()
    assert coord._repo_slug(data) == ("example/project", "main")
    data["repository"] = "https://elsewhere.example/example/project"
    with pytest.raises(coord.CoordinationError):
        coord._repo_slug(data)


def test_github_inspection_detects_truncation_and_ahead_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_command(args: list[str], **_kwargs) -> str:
        calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return json.dumps([pr(i + 1, "M5-P6-03: runtime") for i in range(coord.PR_LIMIT)])
        if args[:3] == ["gh", "issue", "list"]:
            return "[]"
        if args[:2] == ["gh", "api"]:
            return "c" * 40 + "\n"
        if args[:3] == ["git", "rev-list", "--count"]:
            return "2\n"
        raise AssertionError(args)

    def fake_subprocess(args: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(coord, "_run", fake_command)
    monkeypatch.setattr(coord.subprocess, "run", fake_subprocess)
    result = coord.github_state(ledger())
    assert result["main_relation"] == "current"
    assert result["commits_ahead"] == 2
    assert result["pr_scan_complete"] is False
    assert any(args[:3] == ["gh", "pr", "list"] for args in calls)
    assert any(args[:2] == ["git", "merge-base"] for args in calls)
    report = coord.make_report(ledger(), local(), result)
    assert report["next_candidate"] is None
    assert any("incomplete" in hold for hold in report["holds"])
    assert any("unmerged commits" in hold for hold in report["holds"])


def test_missing_main_commit_object_is_unknown_not_current(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_command(args: list[str], **_kwargs) -> str:
        return "[]" if args[:3] in (["gh", "pr", "list"], ["gh", "issue", "list"]) else "c" * 40

    def fake_subprocess(args: list[str], **_kwargs) -> SimpleNamespace:
        assert args[:2] == ["git", "cat-file"]  # never guess ancestry or fetch
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(coord, "_run", fake_command)
    monkeypatch.setattr(coord.subprocess, "run", fake_subprocess)
    result = coord.github_state(ledger())
    assert result["main_relation"] == "unknown"
    assert result["commits_ahead"] is None
    assert coord.make_report(ledger(), local(), result)["next_candidate"] is None


def test_authentication_failure_does_not_echo_gh_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stderr="authentication failed: sensitive details", stdout=""
    ))
    with pytest.raises(coord.CoordinationError, match="reconnect GitHub in Arena") as error:
        coord._run(["gh", "pr", "list"])
    assert "sensitive details" not in str(error.value)
