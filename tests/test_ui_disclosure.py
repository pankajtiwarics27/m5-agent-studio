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
