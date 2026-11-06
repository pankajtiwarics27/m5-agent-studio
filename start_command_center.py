"""The Start pane's verdict, the evidence under it, and the contradictions it refuses to hide.

Start used to lead with a six-row checklist. The checklist is still the truth, but it is the wrong
first thing to read: what a person needs on landing is *what can I do next*, *is anything in the way*,
and only then *why do you say so*. This module produces that short answer from the preflight the
server already computes, plus what is actually known about the local model runtime.

The rule it exists to enforce: **a port answering is not a runtime that can run a model.** A loopback
port can answer `/v1/models` with an empty list, a model can be listed but not loaded, and the
health card can be the server's own demo payload. So the runtime is observed separately from the
preflight, the observation records how it was obtained (``not-run`` / ``ran`` / ``unavailable``), and
a disagreement between the two is reported as a contradiction with its evidence rather than averaged
into a friendly green word.

Nothing here performs I/O: the caller passes in what it observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["ready", "needs-you", "blocked", "contradiction"]
ProbeState = Literal["not-run", "ran", "unavailable"]

#: The preflight steps that make up the short readiness summary, in reading order. `auth` and
#: `first-task` stay in the detailed checklist: the first is about the port, the second is about
#: what the pane is inviting you to do next.
SUMMARY_STEPS = ("model-source", "model-available", "workspace", "execution")

STEP_LABELS = {
    "auth": "Workspace protected",
    "model-source": "Model source",
    "model-available": "Model ready",
    "workspace": "Project workspace",
    "execution": "Command execution",
    "first-task": "First task",
    "runtime": "Local runtime",
}


@dataclass(frozen=True)
class RuntimeObservation:
    """What was seen of the local model runtime, and how it was seen.

    ``port_answered`` is a statement about the socket, not about the runtime: a process can hold
    the port and serve an empty model list. ``model_count`` is the number the runtime itself
    reported, and ``None`` means nobody asked.
    """

    configured: bool = False
    url: str = ""
    port_answered: bool | None = None
    model_count: int | None = None
    probe: ProbeState = "not-run"

    @property
    def measured(self) -> bool:
        return self.probe == "ran" and self.port_answered is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "url": self.url,
            "port_answered": self.port_answered,
            "model_count": self.model_count,
            "probe": self.probe,
            "measured": self.measured,
            "basis": "measured" if self.measured else "not measured",
        }


@dataclass(frozen=True)
class TelemetryObservation:
    """Which health payload the pane is holding, and what it claimed about the runtime."""

    source: str = ""
    omlx_online: bool | None = None

    @property
    def simulated(self) -> bool:
        return self.source == "simulated_demo"

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source or "none", "omlx_online": self.omlx_online, "simulated": self.simulated}


def _check(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step.get("id", ""),
        "label": STEP_LABELS.get(str(step.get("id")), str(step.get("title", ""))),
        "status": step.get("status", "action-required"),
        "detail": step.get("detail", ""),
        "action": step.get("action", ""),
        "action_label": step.get("action_label", ""),
    }


def _runtime_check(runtime: RuntimeObservation) -> dict[str, Any]:
    """The runtime row, which is the one row that must never be inferred from the port alone."""
    if not runtime.configured:
        return {
            "id": "runtime",
            "label": STEP_LABELS["runtime"],
            "status": "optional",
            "detail": "No local runtime is configured, so every model comes from a cloud provider.",
            "action": "",
            "action_label": "",
        }
    if not runtime.measured:
        return {
            "id": "runtime",
            "label": STEP_LABELS["runtime"],
            "status": "unchecked",
            "detail": "Not asked yet: the runtime is only contacted when you press the button that checks it.",
            "action": "probe-local-models",
            "action_label": "Check the runtime",
        }
    if runtime.port_answered and (runtime.model_count or 0) > 0:
        return {
            "id": "runtime",
            "label": STEP_LABELS["runtime"],
            "status": "ready",
            "detail": f"The runtime answered and reported {runtime.model_count} model(s).",
            "action": "",
            "action_label": "",
        }
    if runtime.port_answered:
        return {
            "id": "runtime",
            "label": STEP_LABELS["runtime"],
            "status": "action-required",
            "detail": "The port answered, but the runtime reported no models: a turn would fail even though the port is up.",
            "action": "probe-local-models",
            "action_label": "Check again",
        }
    return {
        "id": "runtime",
        "label": STEP_LABELS["runtime"],
        "status": "action-required",
        "detail": f"Nothing answered at {runtime.url or 'the configured runtime'} when it was asked.",
        "action": "probe-local-models",
        "action_label": "Check again",
    }


def _contradictions(
    preflight: dict[str, Any],
    runtime: RuntimeObservation,
    telemetry: TelemetryObservation,
) -> list[dict[str, Any]]:
    """Where two honest observations disagree, say so instead of picking the friendlier one."""
    found: list[dict[str, Any]] = []
    steps = {str(step.get("id")): step for step in preflight.get("steps", [])}

    if runtime.port_answered and (runtime.model_count or 0) == 0:
        found.append(
            {
                "id": "port-without-models",
                "title": "The runtime answers, but has no model to run",
                "detail": (
                    "A process is holding the port and answering requests, so a port check passes. "
                    "It reports zero models, so a task sent to it would fail. Load a model, or point "
                    "Studio at a provider that has one."
                ),
                "evidence": f"port_answered={runtime.port_answered} · model_count={runtime.model_count}",
                "action": "probe-local-models",
                "action_label": "Check the runtime",
            }
        )

    model_step = steps.get("model-available", {})
    if runtime.port_answered and (runtime.model_count or 0) > 0 and model_step.get("status") not in ("ready", "optional"):
        found.append(
            {
                "id": "runtime-models-without-usable-selection",
                "title": "The runtime has models, but the check found none usable",
                "detail": (
                    f"The runtime reported {runtime.model_count} model(s). The readiness check did "
                    "not find a usable one, so the two are describing different things: check the "
                    "selected model, not the port."
                ),
                "evidence": f"model_count={runtime.model_count} · step_status={model_step.get('status')}",
                "action": model_step.get("action") or "open-providers",
                "action_label": model_step.get("action_label") or "Open providers",
            }
        )

    if preflight.get("ready") and runtime.measured and not runtime.port_answered:
        found.append(
            {
                "id": "checks-passed-without-runtime",
                "title": "The checks passed, but the runtime did not answer",
                "detail": (
                    "Every local check passed, and the runtime did not answer when it was asked. "
                    "That combination is possible — the checks read configuration — so it is printed "
                    "instead of being smoothed into a green word."
                ),
                "evidence": f"preflight.ready=true · probe={runtime.probe}",
                "action": "probe-local-models",
                "action_label": "Check the runtime",
            }
        )

    if telemetry.simulated and preflight.get("ready") and (runtime.model_count or 0) > 0:
        found.append(
            {
                "id": "demo-health-with-live-runtime",
                "title": "Health is the demo payload while a runtime is answering",
                "detail": (
                    "The health card is the server's own simulated payload, and a separate probe "
                    "found the runtime answering. The health card's rows describe this machine only "
                    "where it says so; do not read its model runtime row as the verdict."
                ),
                "evidence": f"telemetry_source={telemetry.source} · model_count={runtime.model_count}",
                "action": "probe-local-models",
                "action_label": "Refresh health",
            }
        )
    return found


def build_readiness(
    preflight: dict[str, Any],
    runtime: RuntimeObservation | None = None,
    telemetry: TelemetryObservation | None = None,
    workspace_dir: str = "",
    workspace_writable: bool = True,
) -> dict[str, Any]:
    """The one-line verdict, the next action, and the evidence under both."""
    runtime = runtime or RuntimeObservation()
    telemetry = telemetry or TelemetryObservation()
    steps = {str(step.get("id")): step for step in preflight.get("steps", [])}

    checks = [_check(steps[step_id]) for step_id in SUMMARY_STEPS if step_id in steps]
    checks.append(_runtime_check(runtime))

    contradictions = _contradictions(preflight, runtime, telemetry)
    next_step = preflight.get("next_step") or None
    blocked = bool(preflight.get("blocked"))
    ready = bool(preflight.get("ready"))

    following: dict[str, Any] | None
    if contradictions:
        verdict: Verdict = "contradiction"
        headline = "The checks and the runtime disagree"
        detail = contradictions[0]["detail"]
        following = {
            "id": contradictions[0]["id"],
            "title": contradictions[0]["title"],
            "detail": contradictions[0]["detail"],
            "action": contradictions[0]["action"],
            "action_label": contradictions[0]["action_label"],
        }
    elif blocked:
        verdict = "blocked"
        headline = f"Blocked: {next_step['title'] if next_step else 'a check needs attention'}"
        detail = (next_step or {}).get("detail", "")
        following = next_step
    elif ready:
        verdict = "ready"
        headline = "Ready to run"
        detail = (
            f"All {preflight.get('total_count', len(checks))} checks read locally"
            + (" and the runtime answered when asked." if runtime.measured and runtime.port_answered else ".")
        )
        following = None
    else:
        verdict = "needs-you"
        headline = f"Needs you: {next_step['title'] if next_step else 'one check'}"
        detail = (next_step or {}).get("detail", "")
        following = next_step

    return {
        "verdict": verdict,
        "headline": headline,
        "detail": detail,
        "ready": verdict == "ready",
        "blocked": verdict == "blocked",
        "counts": {
            "ready": int(preflight.get("completed_count", 0)),
            "total": int(preflight.get("total_count", len(checks))),
            "optional": sum(1 for check in checks if check["status"] == "optional"),
            "unchecked": sum(1 for check in checks if check["status"] == "unchecked"),
        },
        "basis": "measured" if runtime.measured else "derived",
        "probe": runtime.probe,
        "runtime": runtime.to_dict(),
        "telemetry": telemetry.to_dict(),
        "workspace": {"dir": workspace_dir, "writable": bool(workspace_writable)},
        "checks": checks,
        "contradictions": contradictions,
        "next": following,
        "summary": f"{preflight.get('completed_count', 0)} of {preflight.get('total_count', len(checks))} checks ready",
    }
