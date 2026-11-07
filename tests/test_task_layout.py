"""The owner-requested task-first layout preserves one copy of every real control."""
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TaskMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = {}
        self.parents = {}
        self.counts = {}

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        element_id = attrs.get("id")
        if element_id:
            self.controls[element_id] = attrs
            self.parents[element_id] = tuple(self.stack)
            self.counts[element_id] = self.counts.get(element_id, 0) + 1
        if tag not in {"input", "img", "br", "meta", "link", "hr", "path", "line", "polyline", "circle", "polygon"}:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def markup():
    parsed = TaskMarkup()
    parsed.feed((ROOT / "static/index.html").read_text())
    return parsed


def test_composer_has_exactly_one_real_control_per_decision():
    parsed = markup()
    for element_id in (
        "harnessPromptInput", "harnessLeadModel", "harnessCriticModel", "harnessPolicy",
        "harnessWorkflowMode", "harnessRunBtn", "harnessAbortBtn", "harnessWorkspaceInput",
        "harnessSessionFilter", "harnessSessionsList", "activeSkillsPills", "activeMcpPills",
    ):
        assert parsed.counts[element_id] == 1


def test_primary_and_safety_controls_are_not_inside_disclosures():
    parsed = markup()
    for element_id in ("harnessPromptInput", "harnessLeadModel", "harnessPolicy", "harnessRunBtn", "harnessAbortBtn"):
        assert not any(tag == "details" for tag, _ in parsed.parents[element_id])
    assert ("details", "harnessTaskSettings") in parsed.parents["harnessCriticModel"]
    assert ("details", "harnessTaskSettings") in parsed.parents["harnessWorkspaceInput"]
    assert "open" not in parsed.controls["harnessTaskSettings"]
    assert parsed.controls["harnessPolicy"]["onchange"] == "patchActiveHarnessSettings()"
    assert parsed.controls["harnessRunBtn"]["onclick"] == "executeHarnessSelectedWorkflow()"
    assert parsed.controls["harnessAbortBtn"]["onclick"] == "abortHarnessRun()"


def test_secondary_integrations_and_inspector_start_closed():
    parsed = markup()
    assert "open" not in parsed.controls["studioTools"]
    assert ("details", "studioTools") in parsed.parents["activeSkillsPills"]
    assert ("details", "studioTools") in parsed.parents["activeMcpPills"]
    assert "rail-collapsed" in parsed.controls["mainLayout"]["class"].split()
    assert parsed.controls["monitorToggleBtn"]["aria-expanded"] == "false"
    assert parsed.controls["harnessSessionsToggle"]["aria-controls"] == "harnessSessionsPanel"


def test_recovery_and_approvals_are_independent_of_secondary_panels():
    parsed = markup()
    for element_id in ("harnessRecoveryPanel", "harnessRefusalState", "harnessPermissionBadge", "harnessTimeline"):
        assert not any(tag == "details" for tag, _ in parsed.parents[element_id])
        assert ("section", "pane-harness") in parsed.parents[element_id]
