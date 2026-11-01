"""Structural regression checks for layout-only Harness controls."""

from html.parser import HTMLParser
from pathlib import Path


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}
        self.attrs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.parents[attrs["id"]] = tuple(self.stack)
            self.attrs[attrs["id"]] = attrs
        if tag not in {"input", "br", "meta", "link", "hr", "img", "path", "circle", "line", "polyline", "polygon"}:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def test_harness_disclosure_keeps_essential_and_stop_controls_visible():
    parser = Elements()
    parser.feed((Path(__file__).resolve().parents[1] / "static/index.html").read_text())
    for element in ("harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps"):
        assert ("details", "harnessAdvanced") in parser.parents[element]
    for element in ("harnessLeadModel", "harnessCriticModel", "harnessPolicy", "harnessAbortBtn", "harnessRunBtn"):
        assert ("details", "harnessAdvanced") not in parser.parents[element]
    assert "open" not in parser.attrs["harnessAdvanced"]
    assert parser.attrs["densityToggleBtn"]["aria-pressed"] == "false"


def test_harness_run_settings_summary_tracks_every_advanced_value():
    parser = Elements()
    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    parser.feed(source)

    assert parser.attrs["harnessAdvancedSummary"]
    assert ("details", "harnessAdvanced") in parser.parents["harnessAdvancedSummary"]
    assert "Discipline: Ponytail + Concise · Think: Low · Max: 10 steps" in source
    assert "function syncHarnessAdvancedSummary()" in source
    assert "selectedOptions?.[0]?.textContent.trim()" in source
    assert 'for (const id of ["harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps"])' in source
    assert 'setting.addEventListener("change", syncHarnessAdvancedSummary)' in source
    for setting in ("harnessEfficiencyMode", "harnessReasoning", "harnessMaxSteps"):
        assert f'document.getElementById("{setting}")' in source

    # The summary is presentation-only: the existing run payloads still read the
    # selected controls directly, with the same names and numeric step conversion.
    for payload_field in (
        'reasoning_mode: document.getElementById("harnessReasoning").value',
        'efficiency_mode: document.getElementById("harnessEfficiencyMode").value',
        'max_steps: parseInt(document.getElementById("harnessMaxSteps").value, 10)',
    ):
        assert payload_field in source


def test_council_disclosure_keeps_round_and_stop_available():
    parser = Elements()
    parser.feed((Path(__file__).resolve().parents[1] / "static/index.html").read_text())
    assert "open" in parser.attrs["hallSeatsSettings"]
    assert "open" not in parser.attrs["hallSingleSettings"]
    for element in ("hallSeatArch", "hallSeatCoder", "hallSeatCritic", "hallSeatJudge"):
        assert ("details", "hallSeatsSettings") in parser.parents[element]
    for element in ("hallRoleSelect", "hallModelSelect", "hallSummonBtn"):
        assert ("details", "hallSingleSettings") in parser.parents[element]
    for element in ("hallReasoningSelect", "hallStopBtn", "hallFullRoundBtn", "hallConcludeBtn", "hallHumanInput"):
        assert not any(tag == "details" for tag, _ in parser.parents[element])


def test_compare_arbiter_dependency_preserves_selector():
    parser = Elements()
    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    parser.feed(source)
    assert parser.attrs["d-synth"]["onchange"] == "syncDebateArbiter()"
    assert ("div", "debateArbiterModel") in parser.parents["d-judge"]
    assert "hidden" in parser.attrs["debateArbiterHint"]
    assert 'document.getElementById("debateArbiterModel").hidden = !enabled' in source
    assert 'judge_model: document.getElementById("d-judge").value' in source


def test_pipeline_filters_are_keyboard_buttons_with_selection_state():
    parser = Elements()
    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    parser.feed(source)
    for index in range(5):
        attributes = parser.attrs[f"step-card-{index}"]
        assert attributes["type"] == "button"
        assert attributes["aria-pressed"] == ("true" if index == 0 else "false")
    assert 'button.setAttribute("aria-pressed", String(i === n))' in source


def test_integrations_strip_can_be_focused_for_scrolling():
    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    assert '<div class="skills-strip" tabindex="0" aria-label="Integrations and telemetry controls">' in source


def test_harness_composer_and_residency_use_consolidated_dropdowns_and_direct_project_dir():
    parser = Elements()
    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text()
    parser.feed(source)
    assert "harnessWorkflowMode" in parser.attrs
    assert "harnessRunBtn" in parser.attrs
    assert "harnessSessionActionSelect" in parser.attrs
    assert "integrationsQuickMenu" in parser.attrs
    assert "omlxLoadModelPicker" in parser.attrs
    assert "sdlcRunModeSelect" in parser.attrs
    assert "hallSendModeSelect" in parser.attrs
    assert "checked" not in parser.attrs["harnessUseWorktree"]
    assert "loadedOnly" in source

