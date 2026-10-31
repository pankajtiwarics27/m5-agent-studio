#!/usr/bin/env python3
"""Report UI/UX work readiness without claiming or modifying a story.

    python3 scripts/uiux_coordination.py            # local, no network
    python3 scripts/uiux_coordination.py --github   # explicitly inspect open PRs, blockers and main
    python3 scripts/uiux_coordination.py --github --json

The ledger is a checkout-local snapshot; a story claimed on another branch is
not necessarily visible there. This advisory check never reserves a story, and
even a live GitHub result can race another session. Follow the program's claim
and review protocol after inspecting the output.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "product" / "M5_2_0_UIUX_Autonomous_Roadmap.json"
STATUSES = {"not_started", "in_progress", "blocked", "done"}
STORY_ID = re.compile(r"^UIUX-P([0-6])-\d{2}$")
MENTION = re.compile(r"\bUIUX-P\d+-\d+\b", re.IGNORECASE)
UI_TITLE = re.compile(r"\bUI(?:/|[- ]?)UX\b", re.IGNORECASE)
OTHER_PROGRAM = re.compile(r"\b(?:M5-P\d+-\d+|AS-\d+)\b", re.IGNORECASE)
BLOCKER_MARKER = re.compile(r"^[ \t]*UIUX-COORDINATION-BLOCKER[ \t]*:[ \t]*([^\r\n]*)", re.IGNORECASE | re.MULTILINE)
PR_LIMIT = 100
ISSUE_LIMIT = 100


class CoordinationError(RuntimeError):
    """The data needed for a trustworthy report could not be inspected."""


def _run(args: list[str], *, cwd: Path = ROOT) -> str:
    """Run a fixed local/GitHub CLI command; never use a shell or print credentials."""
    try:
        result = subprocess.run(  # noqa: S603 - argv is constructed here, never a shell
            args, cwd=cwd, text=True, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoordinationError(f"could not run {args[0]} ({type(exc).__name__})") from exc
    if result.returncode:
        detail = result.stderr.lower()
        if args[0] == "gh" and any(word in detail for word in ("auth", "login", "credential")):
            raise CoordinationError("GitHub authentication failed; reconnect GitHub in Arena")
        raise CoordinationError(f"{args[0]} inspection failed (exit {result.returncode})")
    return result.stdout


def read_ledger(path: Path = LEDGER) -> dict:
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoordinationError(f"could not read UI/UX ledger: {exc}") from exc
    if not isinstance(ledger, dict):
        raise CoordinationError("the UI/UX ledger must be a JSON object")
    return ledger


def validate_stories(ledger: dict) -> list[dict]:
    """Reject malformed graphs and unsubstantiated completion claims."""
    pin = ledger.get("source_commit_sha")
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise CoordinationError("the source commit pin must be a full SHA")
    stories = ledger.get("stories")
    if not isinstance(stories, list) or not stories:
        raise CoordinationError("the ledger has no stories")
    by_id: dict[str, dict] = {}
    for story in stories:
        if not isinstance(story, dict):
            raise CoordinationError("a story is not an object")
        sid = story.get("id")
        match = STORY_ID.fullmatch(sid) if isinstance(sid, str) else None
        if not match or sid in by_id:
            raise CoordinationError(f"invalid or duplicate story id: {sid!r}")
        tracking = story.get("tracking")
        if not isinstance(tracking, dict):
            raise CoordinationError(f"{sid}: invalid tracking status")
        status = tracking.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            raise CoordinationError(f"{sid}: invalid tracking status")
        progress = tracking.get("progress_percent")
        if not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100:
            raise CoordinationError(f"{sid}: invalid progress percentage")
        if status == "not_started" and (progress != 0 or tracking.get("owner_session") is not None):
            raise CoordinationError(f"{sid}: not-started tracking contains progress or an owner")
        if status == "in_progress" and (not 0 < progress < 100 or not tracking.get("owner_session")):
            raise CoordinationError(f"{sid}: in-progress tracking needs an owner and partial progress")
        if status == "blocked" and progress == 100:
            raise CoordinationError(f"{sid}: a blocked story cannot be complete")
        if status == "done":
            sha = tracking.get("commit_sha")
            if (progress != 100 or not tracking.get("completed_at")
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                    or not isinstance(tracking.get("evidence"), list) or not tracking["evidence"]
                    or not isinstance(tracking.get("test_results"), list) or not tracking["test_results"]):
                raise CoordinationError(f"{sid}: done tracking lacks completion, commit, evidence or test results")
        if story.get("phase") != f"P{match.group(1)}":
            raise CoordinationError(f"{sid}: phase does not match its id")
        if not isinstance(story.get("priority"), str) or not re.fullmatch(r"P\d+", story["priority"]):
            raise CoordinationError(f"{sid}: invalid priority")
        if not isinstance(story.get("title"), str) or not story["title"].strip():
            raise CoordinationError(f"{sid}: missing title")
        dependencies = story.get("dependencies")
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise CoordinationError(f"{sid}: dependencies must be a list of story ids")
        if not isinstance(story.get("blocked_by_unknowns", []), list) or not isinstance(tracking.get("blockers", []), list):
            raise CoordinationError(f"{sid}: blockers must be lists")
        by_id[sid] = story

    unresolved: dict[str, set[str]] = {}
    for sid, story in by_id.items():
        deps = set(story["dependencies"])
        if sid in deps or deps - by_id.keys():
            raise CoordinationError(f"{sid}: self or unknown dependency")
        unresolved[sid] = deps
    resolved: set[str] = set()
    while unresolved:
        ready = {sid for sid, deps in unresolved.items() if deps <= resolved}
        if not ready:
            raise CoordinationError(f"dependency cycle among {', '.join(sorted(unresolved))}")
        resolved.update(ready)
        for sid in ready:
            del unresolved[sid]

    # The displayed counts must not contradict the story-level source of truth.
    totals = ledger.get("overall_progress")
    counts = Counter(story["tracking"]["status"] for story in stories)
    if (not isinstance(totals, dict) or totals.get("total_stories") != len(stories)
            or any(totals.get(k) != counts[k] for k in STATUSES)
            or totals.get("percent_complete") != round(100 * counts["done"] / len(stories))):
        raise CoordinationError("overall ledger counters disagree with story tracking")
    phases = ledger.get("phases")
    if not isinstance(phases, list) or not phases:
        raise CoordinationError("the ledger phase list is missing")
    phase_ids: set[str] = set()
    for phase in phases:
        if not isinstance(phase, dict) or not re.fullmatch(r"P[0-6]", str(phase.get("id") or "")):
            raise CoordinationError("the ledger contains an invalid phase")
        pid = phase["id"]
        if pid in phase_ids:
            raise CoordinationError(f"duplicate phase {pid}")
        phase_ids.add(pid)
        members = [story for story in stories if story["phase"] == pid]
        phase_counts = Counter(story["tracking"]["status"] for story in members)
        if (not members or phase.get("story_ids") != [story["id"] for story in members]
                or phase.get("total") != len(members) or any(phase.get(k) != phase_counts[k] for k in STATUSES)
                or phase.get("percent_complete") != round(100 * phase_counts["done"] / len(members))):
            raise CoordinationError(f"{pid}: phase ledger disagrees with story tracking")
    if ({story["phase"] for story in stories} - phase_ids or "P0" not in phase_ids
            or not any(story["phase"] == "P0" for story in stories)):
        raise CoordinationError("a story phase or the P0 gate is missing from the phase list")
    for story in stories:
        if story["tracking"]["status"] not in {"in_progress", "done"}:
            continue
        if any(by_id[dep]["tracking"]["status"] != "done" for dep in story["dependencies"]):
            raise CoordinationError(f"{story['id']}: started before a dependency was completed")
        phase = int(story["phase"][1:])
        if any(int(other["phase"][1:]) < phase and other["tracking"]["status"] != "done" for other in stories):
            raise CoordinationError(f"{story['id']}: started before a previous phase was completed")
    return stories


def local_candidates(stories: list[dict]) -> list[dict[str, str]]:
    """Apply dependency *and* complete-previous-phase gates, independent of PRs."""
    by_id = {story["id"]: story for story in stories}
    candidates = []
    for story in stories:
        if story["tracking"]["status"] != "not_started":
            continue
        if story.get("blocked_by_unknowns") or story["tracking"].get("blockers"):
            continue
        if any(by_id[dep]["tracking"]["status"] != "done" for dep in story["dependencies"]):
            continue
        phase = int(story["phase"][1:])
        if any(int(other["phase"][1:]) < phase and other["tracking"]["status"] != "done" for other in stories):
            continue
        candidates.append({key: story[key] for key in ("id", "title", "phase", "priority")})
    return sorted(candidates, key=lambda s: (int(s["phase"][1:]), int(s["priority"][1:]), s["id"]))


def inspect_pull_requests(prs: list[dict], known_ids: set[str]) -> list[dict]:
    """Classify every open PR conservatively; an ID mention is not proof of ownership."""
    if not isinstance(prs, list):
        raise CoordinationError("GitHub pull request response is not a list")
    rows = []
    for pr in prs:
        if not isinstance(pr, dict) or not isinstance(pr.get("number"), int) or pr["number"] <= 0:
            raise CoordinationError("GitHub returned a pull request without a valid number")
        if not all(isinstance(pr.get(k), str) for k in ("title", "headRefName", "url")):
            raise CoordinationError(f"pull request #{pr['number']} is missing title, branch or URL")
        if pr.get("body") is not None and not isinstance(pr["body"], str):
            raise CoordinationError(f"pull request #{pr['number']} has an invalid body")
        subject = f"{pr['title']} {pr['headRefName']}"
        text = f"{subject}\n{pr.get('body') or ''}"
        mentions = sorted({match.upper() for match in MENTION.findall(text)})
        unknown_ids = sorted(set(mentions) - known_ids)
        if mentions or UI_TITLE.search(subject):
            classification = "uiux_or_possible_uiux"
        elif OTHER_PROGRAM.search(subject):
            classification = "other_program"
        else:
            # An unidentified PR might own a story despite a generic title.
            classification = "unclassified"
        rows.append({
            "number": pr["number"],
            "title": pr["title"],
            "head": pr["headRefName"],
            "url": pr["url"],
            "mergeable": pr.get("mergeable", "UNKNOWN"),
            "draft": bool(pr.get("isDraft", False)),
            "classification": classification,
            "story_mentions": mentions,
            "unknown_story_ids": unknown_ids,
        })
    return sorted(rows, key=lambda pr: pr["number"])


def inspect_blocking_issues(issues: list[dict]) -> list[dict]:
    """Find explicitly announced program blockers; unmarked issues may still matter."""
    if not isinstance(issues, list):
        raise CoordinationError("GitHub issue response is not a list")
    rows = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int) or issue["number"] <= 0:
            raise CoordinationError("GitHub returned an issue without a valid number")
        if not all(isinstance(issue.get(key), str) for key in ("title", "url")) or "body" not in issue:
            raise CoordinationError(f"issue #{issue['number']} is missing title, URL or body")
        body = issue["body"]
        if body is not None and not isinstance(body, str):
            raise CoordinationError(f"issue #{issue['number']} has an invalid body")
        marker = BLOCKER_MARKER.search(body or "")
        if marker:
            rows.append({"number": issue["number"], "title": issue["title"], "url": issue["url"],
                         "scope": marker.group(1).strip() or "unspecified"})
    return sorted(rows, key=lambda issue: issue["number"])


def git_state(pin: str) -> dict:
    head = _run(["git", "rev-parse", "HEAD"]).strip()
    branch = _run(["git", "branch", "--show-current"]).strip()
    dirty_lines = _run(["git", "status", "--porcelain", "--untracked-files=normal"]).splitlines()
    try:
        ancestry = subprocess.run(  # noqa: S603 - validated SHA and fixed git argv
            ["git", "merge-base", "--is-ancestor", pin, "HEAD"], cwd=ROOT, capture_output=True,
            check=False, timeout=30,
        )
        pin_relation = {0: "ancestor", 1: "not_ancestor"}.get(ancestry.returncode, "unknown")
    except (OSError, subprocess.TimeoutExpired):
        pin_relation = "unknown"
    return {"head": head, "branch": branch or None, "clean": not dirty_lines,
            "changed_entries": len(dirty_lines), "pin_relation": pin_relation}


def _repo_slug(ledger: dict) -> tuple[str, str]:
    parsed = urlparse(str(ledger.get("repository") or ""))
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if parsed.scheme != "https" or parsed.netloc != "github.com" or len(parts) != 2:
        raise CoordinationError("the ledger repository must be an https://github.com/owner/repo URL")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise CoordinationError("invalid owner or repository in the UI/UX ledger")
    branch = ledger.get("branch")
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise CoordinationError("the UI/UX ledger is missing its main branch")
    return "/".join(parts), branch


def github_state(ledger: dict) -> dict:
    """Explicit online inspection. Never fetch, push, claim, merge or edit state."""
    slug, branch = _repo_slug(ledger)
    raw = _run([
        "gh", "pr", "list", "--repo", slug, "--state", "open", "--limit", str(PR_LIMIT),
        "--json", "number,title,body,headRefName,url,mergeable,isDraft",
    ])
    try:
        prs = json.loads(raw)
    except ValueError as exc:
        raise CoordinationError("GitHub returned invalid pull request JSON") from exc
    if not isinstance(prs, list):
        raise CoordinationError("GitHub returned an invalid pull request list")
    issue_raw = _run([
        "gh", "issue", "list", "--repo", slug, "--state", "open", "--limit", str(ISSUE_LIMIT),
        "--json", "number,title,body,url",
    ])
    try:
        issues = json.loads(issue_raw)
    except ValueError as exc:
        raise CoordinationError("GitHub returned invalid issue JSON") from exc
    if not isinstance(issues, list):
        raise CoordinationError("GitHub returned an invalid issue list")
    sha = _run(["gh", "api", f"repos/{slug}/commits/{quote(branch, safe='')}", "--jq", ".sha"]).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CoordinationError("GitHub returned an invalid main-branch SHA")
    # Only use locally known objects. Fetching during a status check would change
    # the checkout and still would not create an atomic cross-session claim.
    try:
        present = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=ROOT, capture_output=True,
            check=False, timeout=30,
        )
        if present.returncode == 0:
            ancestor = subprocess.run(  # noqa: S603 - fixed git argv
                ["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=ROOT, capture_output=True,
                check=False, timeout=30,
            )
            relation = {0: "current", 1: "behind_or_diverged"}.get(ancestor.returncode, "unknown")
        else:
            relation = "unknown"
        ahead = None
        if relation == "current":
            count = _run(["git", "rev-list", "--count", f"{sha}..HEAD"]).strip()
            if not count.isdecimal():
                raise CoordinationError("could not determine commits ahead of GitHub main")
            ahead = int(count)
    except (OSError, subprocess.TimeoutExpired):
        relation, ahead = "unknown", None
    return {"main_branch": branch, "main_sha": sha, "main_relation": relation, "commits_ahead": ahead,
            "prs": prs, "pr_scan_complete": len(prs) < PR_LIMIT,
            "issues": issues, "issue_scan_complete": len(issues) < ISSUE_LIMIT}


def make_report(ledger: dict, local: dict, github: dict | None = None) -> dict:
    stories = validate_stories(ledger)
    ids = {story["id"] for story in stories}
    counts = Counter(story["tracking"]["status"] for story in stories)
    active = sorted(story["id"] for story in stories if story["tracking"]["status"] == "in_progress")
    declared_blockers = sorted(story["id"] for story in stories if story["tracking"]["status"] == "not_started"
                               and (story.get("blocked_by_unknowns") or story["tracking"].get("blockers")))
    candidates = local_candidates(stories)
    holds: list[str] = []
    warnings: list[str] = []
    if not local.get("branch"):
        holds.append("The checkout is detached; use the session's assigned branch.")
    if not local.get("clean"):
        holds.append("The working tree is not clean; inspect existing changes before a claim.")
    if local.get("pin_relation") != "ancestor":
        holds.append("The ledger's source pin is not confirmed as an ancestor of HEAD; inspect the checkout history.")
    if active:
        holds.append("A UI/UX story is already in progress in this ledger: " + ", ".join(active) + ".")

    state = ledger.get("session_state")
    if (not isinstance(state, dict) or not {"active_story", "active_stories", "next_recommended_story"} <= state.keys()
            or not isinstance(state["active_stories"], list)):
        raise CoordinationError("the ledger session_state is malformed")
    declared_items = state["active_stories"] + ([state["active_story"]] if state["active_story"] is not None else [])
    if any(not isinstance(sid, str) for sid in declared_items) or not set(declared_items) <= ids:
        raise CoordinationError("session_state names an unknown active story")
    declared = set(declared_items)
    if declared != set(active):
        holds.append("Ledger session_state and story tracking disagree about active work; reconcile them first.")
    next_id = state["next_recommended_story"]
    if next_id is not None and (not isinstance(next_id, str) or next_id not in ids):
        raise CoordinationError("session_state names an unknown next story")

    rows: list[dict] = []
    blockers: list[dict] = []
    remote = {"checked": False, "main_branch": None, "main_sha": None, "main_relation": "unknown",
              "commits_ahead": None, "pr_scan_complete": False, "issue_scan_complete": False}
    if github is None:
        holds.append("GitHub PRs, issues and main were not checked; rerun with --github before claiming.")
    else:
        rows = inspect_pull_requests(github["prs"], ids)
        blockers = inspect_blocking_issues(github["issues"])
        remote = {"checked": True, "main_branch": github["main_branch"], "main_sha": github["main_sha"],
                  "main_relation": github["main_relation"], "commits_ahead": github["commits_ahead"],
                  "pr_scan_complete": github["pr_scan_complete"],
                  "issue_scan_complete": github["issue_scan_complete"]}
        if not github["pr_scan_complete"]:
            holds.append(f"The open-PR scan reached its {PR_LIMIT}-PR limit and may be incomplete.")
        if not github["issue_scan_complete"]:
            holds.append(f"The open-issue scan reached its {ISSUE_LIMIT}-issue limit and may be incomplete.")
        if blockers:
            holds.append("Open UI/UX coordination-blocker issues require resolution or coordination: "
                         + ", ".join(f"#{issue['number']}" for issue in blockers) + ".")
        if github["main_relation"] != "current":
            holds.append("The checkout is not confirmed to contain the current GitHub main commit; fetch and inspect it.")
        elif github["commits_ahead"] != 0:
            holds.append("The session branch has unmerged commits or an unknown ahead count; inspect them before claiming.")
        uncertain = [pr for pr in rows if pr["classification"] != "other_program"]
        if uncertain:
            holds.append("Open UI/UX or unclassified PRs require review before a new story: "
                         + ", ".join(f"#{pr['number']}" for pr in uncertain) + ".")
        if any(pr["classification"] == "other_program" for pr in rows):
            warnings.append("Review other-program PRs for shared-file conflicts before starting work.")
    if not candidates:
        warnings.append("No not-started story passes the local dependency and phase gates.")
    if candidates and next_id != candidates[0]["id"]:
        warnings.append("The ledger's next recommendation differs from the computed local candidate; investigate.")
        holds.append("The next-story recommendation is inconsistent; reconcile it before claiming.")
    return {
        "schema_version": "1.1.0",
        "read_only": True,
        "coordination_notice": "Advisory snapshot only; not an atomic claim or a substitute for the agent protocol.",
        "checkout": local,
        "source_commit_sha": ledger.get("source_commit_sha"),
        "counts": {status: counts[status] for status in ("done", "in_progress", "blocked", "not_started")},
        "total_stories": len(stories),
        "ledger_active_stories": active,
        "not_started_with_declared_blockers": declared_blockers,
        "locally_eligible": candidates,
        "github": remote,
        "open_pull_requests": rows,
        "open_blocking_issues": blockers,
        "holds": holds,
        "warnings": warnings,
        "next_candidate": candidates[0] if candidates and not holds else None,
    }


def render(report: dict) -> str:
    local, remote, counts = report["checkout"], report["github"], report["counts"]
    lines = [
        "M5 UI/UX coordination (read-only)",
        f"Checkout: {local['branch'] or 'detached'} @ {local['head'][:12]} "
        f"({'clean' if local['clean'] else str(local['changed_entries']) + ' changed entries'}; "
        f"source pin: {local['pin_relation']})",
        f"Ledger: {counts['done']}/{report['total_stories']} done; {counts['in_progress']} in progress; "
        f"{counts['blocked']} blocked; {counts['not_started']} not started.",
        "Locally eligible: " + (", ".join(s['id'] for s in report["locally_eligible"]) or "none"),
    ]
    if report["not_started_with_declared_blockers"]:
        lines.append("Not started with recorded blockers: " + ", ".join(report["not_started_with_declared_blockers"]))
    if remote["checked"]:
        ahead = remote["commits_ahead"] if remote["commits_ahead"] is not None else "unknown"
        lines.append(f"GitHub {remote['main_branch']}: {remote['main_sha'][:12]} ({remote['main_relation']}, "
                     f"{ahead} commits ahead); {len(report['open_pull_requests'])} open PR(s) inspected.")
        for pr in report["open_pull_requests"]:
            mentions = ", ".join(pr["story_mentions"]) or "no UI/UX story ID"
            lines.append(f"  #{pr['number']} [{pr['classification']}; {pr['mergeable']}; {mentions}] {pr['title']}")
            lines.append(f"    {pr['url']}")
        for issue in report["open_blocking_issues"]:
            lines.append(f"  Issue #{issue['number']} [blocker: {issue['scope']}] {issue['title']}")
            lines.append(f"    {issue['url']}")
    else:
        lines.append("GitHub: not checked (offline mode).")
    lines.extend(f"Hold: {item}" for item in report["holds"])
    lines.extend(f"Note: {item}" for item in report["warnings"])
    candidate = report["next_candidate"]
    lines.append("Next candidate: " + (f"{candidate['id']} — {candidate['title']}" if candidate else "none"))
    lines.append(report["coordination_notice"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github", action="store_true", help="explicitly inspect GitHub PRs, blocker issues and main")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable advisory report")
    args = parser.parse_args(argv)
    try:
        ledger = read_ledger()
        # Validate locally before making any optional outbound request.
        validate_stories(ledger)
        local = git_state(ledger["source_commit_sha"])
        github = github_state(ledger) if args.github else None
        report = make_report(ledger, local, github)
    except (CoordinationError, KeyError, TypeError) as exc:
        print(f"uiux_coordination: {exc}. No claim recommendation was made.", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
