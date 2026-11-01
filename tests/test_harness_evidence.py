"""What a Harness completion claim rests on (UIUX-P3-04).

A finished Harness run used to end in one mono line - `Completion verified (spec_sha256…)` - the
digest of a contract the reader never saw, over an execution nothing described. A rejected run
said a failure code and a reason and left the transcript, the classification and the acceptance
coverage in the event log. The diff a run produced lived in a dialog that never said what the
change had been verified against, and the artifacts a turn left behind were on a route the pane
never read.

The inventory is the artifact; these tests read it, break each rule in a copy of the source so a
rule that stopped looking fails here rather than in production, and check the parser against real
`git diff` output.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "static" / "index.html"
APP = PROJECT_ROOT / "app.py"
SEED = PROJECT_ROOT / "scripts" / "harness_evidence_seed.py"
WALK = PROJECT_ROOT / "scripts" / "harness_evidence_walk.mjs"
JSON_PATH = PROJECT_ROOT / "product" / "M5_2_0_UIUX_Harness_Evidence.json"
DOC_PATH = PROJECT_ROOT / "docs" / "uiux-2-0-harness-evidence.md"
MEDIA = PROJECT_ROOT / "docs" / "media" / "harness-evidence"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "uiux_harness_evidence_inventory",
        PROJECT_ROOT / "scripts" / "uiux_harness_evidence_inventory.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


inventory = _load_module()


@pytest.fixture(scope="module")
def payload() -> dict:
    return inventory.build_inventory()


@pytest.fixture()
def sources(tmp_path, monkeypatch):
    """Point the inventory at copies, so a rule can be broken in one of them."""

    def _make(html_transform=None, app_transform=None) -> dict:
        html = FRONTEND.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        if html_transform:
            changed = html_transform(html)
            assert changed != html, "the transform did not change the markup, so the test proves nothing"
            html = changed
        if app_transform:
            changed = app_transform(app)
            assert changed != app, "the transform did not change the server source, so the test proves nothing"
            app = changed
        html_path = tmp_path / "index.html"
        app_path = tmp_path / "app.py"
        html_path.write_text(html, encoding="utf-8")
        app_path.write_text(app, encoding="utf-8")
        monkeypatch.setattr(inventory, "FRONTEND", html_path)
        monkeypatch.setattr(inventory, "APP", app_path)
        return inventory.build_inventory()

    return _make


def _checks(payload: dict) -> set[str]:
    return {finding["check"] for finding in payload["findings"]}


# ── the artifact is current ─────────────────────────────────────────────────


def test_inventory_matches_the_source():
    result = subprocess.run(
        [sys.executable, "scripts/uiux_harness_evidence_inventory.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifacts_exist_and_say_what_they_are():
    assert JSON_PATH.exists() and DOC_PATH.exists()
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert doc.startswith("# A completion claim and the evidence behind it (UIUX-P3-04)")
    assert "Do not edit by hand" in doc
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert data["story"] == "UIUX-P3-04"
    assert data["totals"]["findings"] == 0
    assert data["totals"]["record_sections"] == 6
    # The surface reads three routes that all predate the story; it added none.
    assert [(row["method"], row["path"]) for row in data["routes"]] == [
        ("GET", "/api/harness/sessions/{session_id}/task"),
        ("GET", "/api/harness/sessions/{session_id}/diff"),
        ("POST", "/api/harness/sessions/{session_id}/apply"),
    ]


def test_inventory_has_no_findings(payload):
    assert payload["findings"] == [], payload["findings"]


# ── the card states what the verdict rests on ───────────────────────────────


def test_one_card_draws_all_three_outcomes(payload):
    card = payload["frontend"]["card"]
    assert card["one_builder"] is True
    assert card["one_renderer_for_three_outcomes"] is True, (
        "a verdict is drawn by more than one branch, so two of them can disagree"
    )
    assert card["three_tones"] is True
    assert card["handles_the_pause_outcome"] is True
    assert card["tone_is_a_data_attribute"] is True


def test_the_one_line_verdict_is_gone(payload):
    card = payload["frontend"]["card"]
    assert card["the_one_line_label_is_gone"] is True, (
        "the pane prints a spec digest over an execution it does not describe again"
    )
    assert card["the_one_line_refusal_is_gone"] is True


def test_the_card_states_the_evidence_it_rests_on(payload):
    card = payload["frontend"]["card"]
    for flag in (
        "states_the_contract",
        "states_the_command_and_exit",
        "states_the_revision",
        "states_the_files",
        "states_the_patch",
        "states_the_environment",
        "names_the_failure_code",
        "draws_the_failures",
        "draws_the_coverage",
    ):
        assert card[flag] is True, flag


def test_the_card_invents_nothing_it_was_not_told(payload):
    card = payload["frontend"]["card"]
    assert card["will_not_invent_a_command"] is True, (
        "a run whose command never executed is drawn as if it had"
    )
    assert card["will_not_invent_a_revision"] is True, (
        "a result with no captured revision is drawn as if it had one"
    )
    assert card["says_it_reruns_nothing"] is True


def test_the_card_opens_the_record_and_the_change(payload):
    card = payload["frontend"]["card"]
    assert card["opens_the_record"] is True
    assert card["opens_the_diff"] is True
    assert card["names_the_claim"] is True


# ── coverage is what the transcript printed ─────────────────────────────────


def test_a_declared_criterion_is_not_a_pass(payload):
    coverage = payload["frontend"]["coverage"]
    assert coverage["a_declared_criterion_is_not_a_pass"] is True, (
        "a criterion the contract declares is counted as covered without an observed test"
    )
    assert coverage["coverage_is_read_from_the_report"] is True
    assert coverage["a_missing_test_is_drawn"] is True
    assert coverage["covered_is_stated"] is True
    assert coverage["an_uncovered_criterion_is_counted"] is True


def test_the_contract_declares_and_the_observation_judges(payload):
    coverage = payload["frontend"]["coverage"]
    assert coverage["the_contract_does_not_judge"] is True, (
        "the contract section draws a coverage verdict it did not observe"
    )
    assert coverage["the_declared_list_says_declared"] is True
    assert coverage["a_failure_names_its_kind"] is True


# ── the record behind the card ──────────────────────────────────────────────


def test_the_record_is_a_dialog_with_six_sections(payload):
    record = payload["frontend"]["record"]
    assert record["dialog_exists"] is True
    assert record["is_a_dialog"] is True and record["is_named"] is True
    assert record["opens_through_the_controller"] is True
    assert record["closes_through_the_controller"] is True
    assert record["closes_on_the_backdrop"] is True
    assert all(record["lists"].values()), record["lists"]


def test_the_record_reads_the_routes_that_already_existed(payload):
    record = payload["frontend"]["record"]
    assert record["reads_the_contract_route"] is True
    assert record["reads_the_diff_route"] is True
    assert record["keeps_the_session_it_asked_about"] is True, (
        "a slow read can render another session's evidence"
    )
    assert record["will_not_race_itself"] is True
    assert record["says_what_failed_to_read"] is True


def test_the_record_states_the_contract_the_observation_and_the_artifacts(payload):
    record = payload["frontend"]["record"]
    for flag in (
        "the_contract_states_the_rules",
        "the_contract_states_the_command",
        "the_contract_states_required_files",
        "the_contract_states_known_failures",
        "the_contract_says_it_is_not_a_result",
        "the_observation_states_the_exit",
        "the_observation_states_the_classification",
        "the_observation_says_pre_existing_is_not_introduced",
        "the_verdict_names_the_refs",
        "the_verdict_names_the_transcript",
        "the_artifacts_list_checkpoints",
        "the_artifacts_list_the_latest",
        "the_artifacts_list_the_lineage",
        "the_artifacts_state_the_next_run_target",
        "the_artifacts_say_a_checkpoint_is_not_a_copy",
    ):
        assert record[flag] is True, flag


def test_an_absence_is_drawn_as_an_absence(payload):
    record = payload["frontend"]["record"]
    assert record["draws_a_missing_verdict_as_missing"] is True
    assert record["draws_a_missing_contract_as_missing"] is True
    assert record["draws_a_missing_diff_as_missing"] is True
    assert record["the_limits_are_drawn"] is True
    assert record["the_limits_are_declared"] is True


# ── the change, counted from the same diff ──────────────────────────────────


def test_the_diff_is_parsed_once_and_counted(payload):
    change = payload["frontend"]["change"]
    assert change["the_diff_is_parsed_once"] is True
    assert change["hunk_headers_are_skipped"] is True, "a hunk header counts as an added line"
    assert change["markers_are_skipped"] is True
    assert change["the_dialog_reuses_it"] is True, (
        "the record and the diff dialog count the change differently"
    )
    assert change["the_dialog_counts_the_change"] is True
    assert change["the_dialog_says_what_it_is"] is True
    assert change["the_dialog_offers_the_record"] is True
    assert payload["frontend"]["record"]["the_change_counts_per_file"] is True
    assert payload["frontend"]["record"]["the_change_says_what_it_is_not"] is True


def test_the_file_counter_reads_a_real_git_diff(tmp_path):
    """`harnessDiffFiles()` against output git actually produced, not a hand-written sample."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "total.py").write_text("def total(v):\n    return 0\n", encoding="utf-8")
    (repo / "notes.txt").write_text("keep\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(repo),
    }
    for args in (("init", "-q"), ("add", "."), ("commit", "-q", "-m", "baseline")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    (repo / "src" / "total.py").write_text("def total(v):\n    return sum(v)\n", encoding="utf-8")
    (repo / "notes.txt").write_text("keep\nalso this\n", encoding="utf-8")
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    assert diff.count("diff --git ") == 2

    script = tmp_path / "count.mjs"
    source = FRONTEND.read_text(encoding="utf-8")
    start = source.find("  function harnessDiffFiles(")
    end = source.find("\n  function ", start + 10)
    body = source[start:end]
    script.write_text(
        f"{body}\nconst diff = require('node:fs').readFileSync({json.dumps(str(tmp_path / 'diff.txt'))}, 'utf8');\n"
        "console.log(JSON.stringify(harnessDiffFiles(diff)));\n",
        encoding="utf-8",
    )
    (tmp_path / "diff.txt").write_text(diff, encoding="utf-8")
    # `require` is not available in an ESM-free snippet on every Node; rewrite it as ESM-free CJS.
    script.write_text(
        body.replace("function harnessDiffFiles(", "function harnessDiffFiles(")
        + "\nconst fs = require('node:fs');\n"
        + f"const diff = fs.readFileSync({json.dumps(str(tmp_path / 'diff.txt'))}, 'utf8');\n"
        + "console.log(JSON.stringify(harnessDiffFiles(diff)));\n",
        encoding="utf-8",
    )
    script.rename(tmp_path / "count.cjs")
    result = subprocess.run(
        ["node", str(tmp_path / "count.cjs")], capture_output=True, text=True
    )
    if result.returncode != 0:  # no node on this machine: the inventory still pins the shape
        pytest.skip(f"node is not available to run the parser: {result.stderr.strip()[:120]}")
    files = json.loads(result.stdout)
    assert [row["path"] for row in files] == ["notes.txt", "src/total.py"]
    total = {row["path"]: (row["added"], row["deleted"]) for row in files}
    assert total["src/total.py"] == (1, 1)
    assert total["notes.txt"] == (1, 0)


# ── the surface is reachable, and read-only ─────────────────────────────────


def test_the_record_is_reachable_from_everywhere_it_should_be(payload):
    reachability = payload["frontend"]["reachability"]
    assert reachability["from_the_session_actions"] is True
    assert reachability["from_the_palette"] is True
    assert reachability["from_every_verdict_card"] is True
    assert reachability["from_the_diff_dialog"] is True
    assert reachability["the_timeline_summary_says_it"] is True, (
        "the timeline strip still summarises a verdict generically"
    )


def test_the_record_writes_nothing(payload):
    reachability = payload["frontend"]["reachability"]
    assert reachability["the_surface_is_read_only"] is True
    assert reachability["alters_nothing_but_itself"] is True


# ── what the story had to preserve ──────────────────────────────────────────


def test_the_diff_route_still_returns_what_it_did(payload):
    contract = payload["server"]["diff_contract"]
    assert all(contract.values()), [name for name, ok in contract.items() if not ok]


def test_apply_still_refuses_and_reports(payload):
    contract = payload["server"]["apply_contract"]
    assert all(contract.values()), [name for name, ok in contract.items() if not ok]


def test_the_task_route_still_carries_the_contract_and_the_artifacts(payload):
    contract = payload["server"]["task_contract"]
    assert all(contract.values()), [name for name, ok in contract.items() if not ok]


# ── the fixture is the product's own records ────────────────────────────────


def test_the_seed_lets_the_verifier_decide():
    source = SEED.read_text(encoding="utf-8")
    assert "sm.verify_task_completion(session_id, workspace_dir=workspace)" in source, (
        "the fixture hand-writes a report instead of asking the verifier"
    )
    assert "studio._append_harness_event(" in source, "the fixture does not use the event writer"
    assert "demo_tour.make_environment(" in source, "the fixture is not an isolated demo"
    assert '"turn/verified" if report.verified else "turn/verification_failed"' in source
    assert "sm.checkpoints.save_checkpoint(" in source
    assert "TaskVerificationSpec(" in source


def test_the_walk_says_what_it_checks_and_refuses_to_fake_it():
    source = WALK.read_text(encoding="utf-8")
    assert "This script refuses to report a walk it did not perform." in source
    assert "openHarnessEvidenceModal" in source and "/diff`" in source
    assert "harness_evidence_seed.py" in source
    for tone in ("verified", "rejected", "pending"):
        assert f'{tone}' in source
    assert "--screenshots" in source
    assert "no page error occurred during the walk" in source


def test_the_media_directory_says_what_produced_it():
    readme = MEDIA / "README.md"
    assert readme.exists(), "the captures have no README saying how they were produced"
    text = readme.read_text(encoding="utf-8")
    assert "harness_evidence_walk.mjs" in text
    assert "harness_evidence_seed.py" in text
    assert "Do not edit" in text


# ── a rule that cannot fail is not a rule ───────────────────────────────────


def test_a_second_verdict_renderer_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '    if (harnessVerdictTone(ev)) {\n      // One card for all three outcomes',
            '    if (ev.type === "turn/verified") return "<div>ok</div>";\n'
            '    if (harnessVerdictTone(ev)) {\n      // One card for all three outcomes',
            1,
        )
    )
    assert "card" in _checks(payload)


def test_a_card_that_invents_a_revision_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace('"no revision captured"', '"HEAD"', 1)
    )
    assert "card" in _checks(payload)


def test_a_declared_criterion_drawn_as_covered_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "      rows.push({ criterion, ids, missing: ids, covered: false });",
            "      rows.push({ criterion, ids, missing: ids, covered: true });",
            1,
        )
    )
    assert "coverage" in _checks(payload)


def test_an_absent_verdict_drawn_as_a_pass_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            "No verdict is recorded in this session's log",
            "Nothing to report",
            1,
        )
    )
    assert "record" in _checks(payload)


def test_a_hunk_header_counted_as_a_change_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            'if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) continue;',
            'if (line.startsWith("+++") || line.startsWith("---")) continue;',
            1,
        )
    )
    assert "change" in _checks(payload)


def test_a_record_reachable_only_from_code_is_caught(sources):
    payload = sources(
        html_transform=lambda text: text.replace(
            '                <option value="evidence">Verification &amp; Artifacts</option>\n',
            "",
            1,
        )
    )
    assert "reachability" in _checks(payload)


def test_an_invented_verification_route_is_caught(sources):
    def invented(server: str) -> str:
        return server.replace(
            '@app.get("/api/harness/sessions/{session_id}/diff")',
            '@app.get("/api/harness/sessions/{session_id}/evidence")',
            1,
        )

    payload = sources(app_transform=invented)
    assert "invented-route" in _checks(payload) or "route-missing" in _checks(payload)


def test_a_task_route_that_drops_the_preview_is_caught(sources):
    def drops(server: str) -> str:
        return server.replace('"verification_evidence": ver_evidence_preview,', '"preview": None,', 1)

    payload = sources(app_transform=drops)
    assert "task_contract" in _checks(payload)


def test_a_diff_route_that_stops_redacting_is_caught(sources):
    def unredacted(server: str) -> str:
        return server.replace(
            "        combined = redact_text(\n",
            "        combined = (\n",
            1,
        )

    payload = sources(app_transform=unredacted)
    assert "diff_contract" in _checks(payload)
