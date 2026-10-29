"""Deterministic first-run preflight for guided onboarding (AS-039).

The acceptance criterion is *"a new user completes a guided first task without
editing code"*. That needs an honest answer to one question before the first
task: what is missing, and what is the single next click?

Design decisions:

* **Pure and offline.** :func:`build_report` is a pure function over
  :class:`PreflightInputs`. It never resolves DNS, probes a provider, reads the
  keyring, or opens a database. The app adapter collects the snapshot from local
  state only, so rendering onboarding can never leak a credential or phone home
  (repository principle 1: local-first privacy).
* **Fail explicit, never optimistic.** Anything unknown degrades to
  ``action-required``; a step reports ``ready`` only from positive local
  evidence. A missing model list is *not* treated as "probably fine".
* **No secrets, no shell.** The report carries booleans, counts, aliases and
  UI action tokens only. Guidance is phrased as something to click, never as a
  command the user must paste, and the guided task itself reuses the existing
  ``/api/chat/stream`` path instead of introducing a second executor.
* **Capability, not platform.** The sandbox step checks whether a configured
  container CLI is present; it never assumes macOS, a chip, or a runtime name.

Execution is optional by design: the first task is a plain chat turn, so the
sandbox step can be ``optional`` without blocking onboarding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

StepStatus = Literal["ready", "optional", "action-required", "blocked"]
ExecutionMode = Literal["disabled", "sandbox", "host"]

STEP_ORDER = ("auth", "model-source", "model-available", "workspace", "execution", "first-task")
READY_STATUSES = frozenset({"ready", "optional"})
FIRST_TASK_PROMPT = (
    "Reply with the single word READY. This confirms Studio can reach the selected "
    "model, so we can start the real task next."
)
FIRST_TASK_TITLE = "Ask a model to say READY"


@dataclass(frozen=True)
class ProviderState:
    """Credential-free view of one configured provider; never carries the key itself."""

    alias: str
    enabled: bool
    has_credential: bool
    model_count: int

    def usable_models(self) -> int:
        return self.model_count if self.enabled and self.has_credential else 0


@dataclass(frozen=True)
class PreflightInputs:
    """Local-only snapshot of everything the preflight is allowed to consider."""

    auth_ready: bool
    local_runtime_configured: bool
    providers: tuple[ProviderState, ...] = ()
    combo_count: int = 0
    execution_mode: ExecutionMode = "disabled"
    sandbox_runtime_available: bool = False
    workspace_writable: bool = False
    workspace_detail: str = ""

    @property
    def usable_provider_models(self) -> int:
        return sum(provider.usable_models() for provider in self.providers)

    @property
    def known_model_count(self) -> int:
        return self.usable_provider_models + max(self.combo_count, 0)


@dataclass(frozen=True)
class Step:
    """One onboarding step plus the single action that advances it."""

    id: str
    title: str
    status: StepStatus
    detail: str
    action_label: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "action_label": self.action_label,
            "action": self.action,
        }


@dataclass(frozen=True)
class PreflightReport:
    """Ordered onboarding plan with one explicit next action."""

    steps: tuple[Step, ...]
    first_task_prompt: str = FIRST_TASK_PROMPT
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return any(step.status == "blocked" for step in self.steps)

    @property
    def ready(self) -> bool:
        return all(step.status in READY_STATUSES for step in self.steps)

    @property
    def next_step(self) -> Step | None:
        """The first step that needs the user; None once everything is ready."""
        for step in self.steps:
            if step.status not in READY_STATUSES:
                return step
        return None

    @property
    def completed_count(self) -> int:
        return sum(1 for step in self.steps if step.status in READY_STATUSES)

    def to_dict(self) -> dict[str, Any]:
        next_step = self.next_step
        return {
            "ready": self.ready,
            "blocked": self.blocked,
            "completed_count": self.completed_count,
            "total_count": len(self.steps),
            "summary": f"{self.completed_count} of {len(self.steps)} steps ready",
            "next_step": next_step.to_dict() if next_step else None,
            "steps": [step.to_dict() for step in self.steps],
            "first_task": {"title": FIRST_TASK_TITLE, "prompt": self.first_task_prompt},
            "notes": list(self.notes),
        }


def _auth_step(inputs: PreflightInputs) -> Step:
    if inputs.auth_ready:
        return Step(
            id="auth",
            title="Studio is running and protected",
            status="ready",
            detail="The server is answering on loopback with its bearer token in place.",
        )
    return Step(
        id="auth",
        title="Studio has no API token",
        status="blocked",
        detail=(
            "Protected routes will fail closed until a token exists. Restart Studio on "
            "loopback so it can generate one, or set a token in the environment."
        ),
        action_label="Restart Studio",
        action="restart-server",
    )


def _model_source_step(inputs: PreflightInputs) -> Step:
    usable = [provider for provider in inputs.providers if provider.enabled and provider.has_credential]
    if usable:
        aliases = ", ".join(sorted(provider.alias for provider in usable))
        return Step(
            id="model-source",
            title="A model source is configured",
            status="ready",
            detail=f"Enabled provider(s) with stored credentials: {aliases}.",
        )
    disabled = [provider.alias for provider in inputs.providers if not provider.enabled]
    no_key = [provider.alias for provider in inputs.providers if provider.enabled and not provider.has_credential]
    hints: list[str] = []
    if no_key:
        hints.append("enabled but missing a credential: " + ", ".join(sorted(no_key)))
    if disabled:
        hints.append("disabled: " + ", ".join(sorted(disabled)))
    if inputs.local_runtime_configured:
        hints.append("a local runtime URL is configured but has not answered yet")
    suffix = f" ({'; '.join(hints)})" if hints else ""
    return Step(
        id="model-source",
        title="Connect a model source",
        status="action-required",
        detail=(
            "Add a cloud provider with an API key, or start a local runtime such as oMLX, "
            "Ollama, or llama.cpp on its loopback port" + suffix + "."
        ),
        action_label="Open providers",
        action="open-providers",
    )


def _model_available_step(inputs: PreflightInputs) -> Step:
    known = inputs.known_model_count
    if known > 0:
        return Step(
            id="model-available",
            title="Models are available",
            status="ready",
            detail=f"{known} model(s) or combo(s) are configured and selectable.",
        )
    if any(provider.enabled and provider.has_credential for provider in inputs.providers):
        return Step(
            id="model-available",
            title="Fetch the model list",
            status="action-required",
            detail=(
                "A provider is configured but no models are listed yet. Use Fetch models on "
                "the provider so Studio knows what it can call."
            ),
            action_label="Fetch models",
            action="open-providers",
        )
    return Step(
        id="model-available",
        title="Choose a model",
        status="action-required",
        detail=(
            "Once a source is connected, pick a model (or an auto-fallback combo) in the "
            "Chat header. No model has been discovered yet."
        ),
        action_label="Open Chat",
        action="open-chat",
    )


def _workspace_step(inputs: PreflightInputs) -> Step:
    if inputs.workspace_writable:
        return Step(
            id="workspace",
            title="Workspace is writable",
            status="ready",
            detail=inputs.workspace_detail or "The configured workspace directory is present and writable.",
        )
    return Step(
        id="workspace",
        title="Fix the workspace directory",
        status="action-required",
        detail=inputs.workspace_detail
        or "The configured workspace directory is missing or read-only; file tools will refuse to write.",
        action_label="Check workspace",
        action="fix-workspace",
    )


def _execution_step(inputs: PreflightInputs) -> Step:
    if inputs.execution_mode == "disabled":
        return Step(
            id="execution",
            title="Code execution is off (fine for a first chat)",
            status="optional",
            detail=(
                "Shell/Python tools stay disabled, which is the safe default. Enable the "
                "container sandbox later if you want the agent to run commands."
            ),
        )
    if inputs.execution_mode == "host":
        return Step(
            id="execution",
            title="Host execution is enabled (unsandboxed)",
            status="optional",
            detail=(
                "Commands run as your OS user with no sandbox. Keep this only on a machine "
                "and workspace you trust; the container sandbox is the safer opt-in."
            ),
        )
    if inputs.sandbox_runtime_available:
        return Step(
            id="execution",
            title="Container sandbox is ready",
            status="ready",
            detail="A container runtime is on PATH; sandboxed execution is available.",
        )
    return Step(
        id="execution",
        title="Container runtime not found",
        status="action-required",
        detail=(
            "Sandbox mode is selected but no container CLI is on PATH. Install/start the "
            "runtime, or switch execution mode to disabled to continue onboarding."
        ),
        action_label="Review execution mode",
        action="review-execution",
    )


def _first_task_step(inputs: PreflightInputs) -> Step:
    if inputs.known_model_count > 0:
        return Step(
            id="first-task",
            title=FIRST_TASK_TITLE,
            status="ready",
            detail="Send the guided first prompt from Chat; it makes exactly one model call you trigger.",
            action_label="Run first task",
            action="run-first-task",
        )
    return Step(
        id="first-task",
        title=FIRST_TASK_TITLE,
        status="action-required",
        detail="Available once a model is selectable, so the guided prompt has somewhere to go.",
    )


def build_report(inputs: PreflightInputs) -> PreflightReport:
    """Build the ordered onboarding plan from a local-only snapshot."""
    builders = {
        "auth": _auth_step,
        "model-source": _model_source_step,
        "model-available": _model_available_step,
        "workspace": _workspace_step,
        "execution": _execution_step,
        "first-task": _first_task_step,
    }
    steps = tuple(builders[step_id](inputs) for step_id in STEP_ORDER)
    notes = [
        "This preflight reads local configuration only; it makes no network calls.",
        "Nothing here requires editing a config file or pasting a shell command.",
    ]
    if inputs.local_runtime_configured and inputs.known_model_count == 0:
        notes.append(
            "A local runtime URL is configured. Start the runtime, then reopen this page to "
            "pick up its models."
        )
    return PreflightReport(steps=steps, notes=tuple(notes))
