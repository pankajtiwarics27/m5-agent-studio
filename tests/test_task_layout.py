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


def test_integration_picker_is_a_single_bounded_disclosure_with_explicit_counts():
    parsed = markup()
    for element_id in (
        "integrationSearch", "integrationMcpTab", "integrationSkillsTab", "integrationPrev",
        "integrationNext", "integrationPageStatus", "mcpServersCountBadge", "mcpEnabledCountBadge",
        "mcpToolsCountBadge", "providersCountBadge", "skillsCountBadge", "ocDocsCountBadge",
    ):
        assert parsed.counts[element_id] == 1
        assert ("details", "studioTools") in parsed.parents[element_id]
    assert parsed.controls["integrationSearch"]["type"] == "search"
    assert parsed.controls["integrationMcpTab"]["aria-pressed"] == "true"
    assert parsed.controls["integrationSkillsTab"]["aria-pressed"] == "false"
    assert "hidden" in parsed.controls["activeSkillsPills"]
    assert "disabled" in parsed.controls["integrationPrev"]
    assert "disabled" in parsed.controls["integrationNext"]
    assert parsed.controls["integrationPageStatus"]["aria-live"] == "polite"


def test_picker_filters_without_execution_or_policy_changes():
    html = (ROOT / "static/index.html").read_text()
    picker = html.split("  function renderIntegrationPicker(", 1)[1].split("  function renderSkillsUI()", 1)[0]
    assert "matches.slice(start, start + INTEGRATION_PAGE_SIZE)" in picker
    assert "const INTEGRATION_PAGE_SIZE = 6;" in html
    assert "data-integration-id=" in picker
    assert 'onclick="toggleIntegrationRow(this)"' in picker
    assert 'aria-pressed="${on}"' in picker
    for forbidden in ("fetch(", "localStorage", "approval_policy", "tools/call", "color:#fef3c7"):
        assert forbidden not in picker
    assert "MCP servers: <strong" in html
    assert "MCP tools: <strong" in html
    assert "MCP:<span" not in html


def test_navigation_metrics_are_shared_and_sessions_follow_measured_height():
    html = (ROOT / "static/index.html").read_text()
    assert "--studio-nav-width: 200px;" in html
    assert "#mainLayout { padding-left: var(--studio-nav-width); }" in html
    assert "width: var(--studio-nav-width); padding: var(--space-4);" in html
    assert "--studio-nav-height: 348px;" in html
    assert "var(--studio-nav-height)" in html
    assert "new ResizeObserver(syncSessionsPosition).observe(tabRail);" in html
    for line in html.splitlines():
        if 'body[data-active-pane="harness"] .mode-tabs {' in line:
            assert all(token not in line for token in ("width:", "padding:", "gap:"))
    assert 'body[data-active-pane="harness"] .mode-tab {' not in html
