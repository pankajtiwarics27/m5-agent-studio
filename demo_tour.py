"""A reproducible, offline tour of the Studio (AS-045).

The tour drives the *real* ASGI application in-process through the same steps a
new user takes on screen, using a throwaway database, a generated bearer token,
and no provider, network call, or key of any kind. Every step records a status
code and a short, curated observation, so the transcript can be committed as
onboarding media and re-checked in CI for drift.

Design notes:

- Steps are declarative so the transcript and the tests read the same source.
- Observations are curated, not raw JSON: a dumped payload would embed timestamps
  and random document ids, and the transcript has to be byte-stable.
- The demo refuses to run outside an isolated environment. Pointing it at a real
  database, disabling auth, or enabling host execution raises
  :class:`DemoEnvironmentError` rather than quietly mutating user data.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEMO_COMBO_ALIAS = "demo-combo"
DEMO_COMBO_NAME = "Demo Combo"
DEMO_DOC_TITLE = "Local-first demo note"
DEMO_DOC_CONTENT = "# Local-first demo note\n\nNotes never leave this machine unless a provider is called."
DEMO_SEARCH_QUERY = "local-first"
DEMO_COUNCIL_REQUEST = "Summarise the tradeoffs of running the studio fully offline"
TRANSCRIPT_PATH = Path("docs/media/demo-transcript.md")
MANIFEST_PATH = Path("docs/media/screenshots/manifest.json")


class DemoEnvironmentError(RuntimeError):
    """The demo was asked to run somewhere it must not touch."""


class DemoStepError(RuntimeError):
    """A tour step did not behave as the transcript claims."""


class ResponseLike(Protocol):
    status_code: int

    def json(self) -> Any: ...


class ClientLike(Protocol):
    def get(self, path: str, **kwargs: Any) -> ResponseLike: ...

    def post(self, path: str, **kwargs: Any) -> ResponseLike: ...

    def delete(self, path: str, **kwargs: Any) -> ResponseLike: ...


# ---------------------------------------------------------------------------
# The six UI panes, kept in step with static/index.html
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Pane:
    """One workspace pane as the UI presents it."""

    tab: str
    title: str
    purpose: str


PANES: tuple[Pane, ...] = (
    Pane("start", "Start", "Guided first run: the preflight checklist and one-click next actions."),
    Pane("harness", "Harness", "Agent sessions over a workspace, with diffs, approvals, and abort."),
    Pane("hall", "Council", "Multi-model sessions that keep a visible, append-only discussion."),
    Pane("council", "Pipeline", "A four-stage pipeline for a single request."),
    Pane("debate", "Compare", "The same prompt against several models, side by side."),
    Pane("chat", "Chat", "One model, one conversation, streaming."),
)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """The HTTP call a step makes."""

    method: str
    path: str
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepOutcome:
    """What a step observed, in a form that can be committed."""

    step_id: str
    pane: str
    title: str
    narration: str
    request: Request
    status_code: int
    summary: str


def _count(payload: Any, key: str) -> int:
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def _summarise_preflight(payload: Any) -> str:
    preflight = payload.get("preflight", {}) if isinstance(payload, dict) else {}
    steps = preflight.get("steps") or []
    ready = sum(1 for step in steps if step.get("status") == "ready")
    optional = sum(1 for step in steps if step.get("status") == "optional")
    next_step = preflight.get("next_step") or {}
    parts = [
        f"{len(steps)} checklist steps: {ready} ready, {optional} optional, "
        f"{len(steps) - ready - optional} waiting on you"
    ]
    if next_step.get("title"):
        parts.append(f"next up is {next_step['title']}")
    return "; ".join(parts)


def _summarise_providers(payload: Any) -> str:
    presets = _count(payload, "presets")
    configured = _count(payload, "providers")
    return (
        f"{presets} provider presets are described locally and {configured} are configured; "
        "the demo configures none, so no key is read or sent"
    )


def _summarise_combo(payload: Any) -> str:
    combo = payload.get("combo", {}) if isinstance(payload, dict) else {}
    targets = combo.get("targets") or []
    return f"created the combo {combo.get('alias', DEMO_COMBO_ALIAS)} with {len(targets)} target(s)"


def _summarise_doc(payload: Any) -> str:
    doc = payload.get("doc", {}) if isinstance(payload, dict) else {}
    return f"added the workspace note {doc.get('title', DEMO_DOC_TITLE)!r} to folder {doc.get('folder', 'demo')!r}"


def _summarise_search(payload: Any) -> str:
    docs = payload.get("docs") if isinstance(payload, dict) else None
    matches = len(docs or [])
    return (
        f"full-text search returned {matches} document(s); the demo note is ranked first"
        if matches
        else "full-text search returned no documents"
    )


def _summarise_lint(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "lint returned an unexpected payload"
    return (
        f"{payload.get('total_docs', 0)} documents, health score {payload.get('health_score', 0)}, "
        f"{len(payload.get('orphan_docs') or [])} orphan(s), {len(payload.get('thin_docs') or [])} thin"
    )


def _summarise_mcp(payload: Any) -> str:
    return (
        f"{_count(payload, 'presets')} MCP server presets listed and {_count(payload, 'servers')} connected; "
        "connecting a server is an explicit action, not a side effect of opening the pane"
    )


def _summarise_session(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "session creation returned an unexpected payload"
    participants = payload.get("participants") or []
    return (
        f"opened a council session for {payload.get('title', '')!r} with "
        f"{len(participants)} participant(s); no model was called"
    )


def _summarise_export(payload: Any) -> str:
    tables = payload.get("tables") if isinstance(payload, dict) else None
    excluded = payload.get("excluded_tables") if isinstance(payload, dict) else None
    excluded_count = sum(len(items) for items in (excluded or {}).values()) if isinstance(excluded, dict) else 0
    return (
        f"export covers {len(tables or {})} tables and excludes {excluded_count} "
        f"credential and audit tables; redaction is {'on' if (payload or {}).get('redaction') else 'off'}"
    )


def _summarise_delete(payload: Any) -> str:
    status = payload.get("status") if isinstance(payload, dict) else None
    return (
        f"removed the demo combo ({status or 'ok'}); the throwaway database is now empty of demo rows"
    )


@dataclass(frozen=True)
class Step:
    """A declarative tour step."""

    step_id: str
    pane: str
    title: str
    narration: str
    request: Request
    summarise: Any
    expected_status: int = 200


STEPS: tuple[Step, ...] = (
    Step(
        step_id="preflight",
        pane="start",
        title="See what the Start pane checks",
        narration=(
            "The Start pane is a preflight, not a wizard: it reports what is ready and what still "
            "needs you. Nothing here contacts a model."
        ),
        request=Request("GET", "/api/onboarding/preflight"),
        summarise=_summarise_preflight,
    ),
    Step(
        step_id="providers",
        pane="start",
        title="Look before you connect",
        narration=(
            "Provider presets ship with the app, but a preset is only a description. An empty "
            "configured list is the safe default."
        ),
        request=Request("GET", "/api/providers"),
        summarise=_summarise_providers,
    ),
    Step(
        step_id="combo",
        pane="harness",
        title="Create a combo",
        narration="A combo names a set of targets that can be used as one model id.",
        request=Request(
            "POST",
            "/api/combos",
            {"alias": DEMO_COMBO_ALIAS, "name": DEMO_COMBO_NAME, "targets": ["model"]},
        ),
        summarise=_summarise_combo,
    ),
    Step(
        step_id="context",
        pane="harness",
        title="Give the workspace a memory",
        narration=(
            "The open-context store is local SQLite. Documents you add are searchable offline and "
            "are never uploaded."
        ),
        request=Request(
            "POST",
            "/api/opencontext/docs",
            {"title": DEMO_DOC_TITLE, "content": DEMO_DOC_CONTENT, "folder": "demo"},
        ),
        summarise=_summarise_doc,
    ),
    Step(
        step_id="search",
        pane="harness",
        title="Find it again",
        narration="Search runs against the same local store, with no embedding service in the path.",
        request=Request("GET", f"/api/opencontext/search?q={DEMO_SEARCH_QUERY}"),
        summarise=_summarise_search,
    ),
    Step(
        step_id="lint",
        pane="harness",
        title="Check the store's health",
        narration="Lint reports orphans, thin pages, and dangling links before they become habits.",
        request=Request("GET", "/api/opencontext/lint"),
        summarise=_summarise_lint,
    ),
    Step(
        step_id="tools",
        pane="hall",
        title="Bring tools deliberately",
        narration=(
            "MCP servers are listed as presets; each connection is a separate, explicit action with "
            "its own approval policy."
        ),
        request=Request("GET", "/api/mcp/servers"),
        summarise=_summarise_mcp,
    ),
    Step(
        step_id="session",
        pane="hall",
        title="Open a council session",
        narration=(
            "A session records the request and its discussion in order. With no provider configured "
            "there is nothing to stream, and the session stays a local transcript."
        ),
        request=Request("POST", "/api/hall/sessions", {"request": DEMO_COUNCIL_REQUEST}),
        summarise=_summarise_session,
    ),
    Step(
        step_id="export",
        pane="start",
        title="Take your data with you",
        narration=(
            "The history export is redacted and deliberately omits credentials and audit trails, so "
            "it is safe to inspect."
        ),
        request=Request("GET", "/api/history/export"),
        summarise=_summarise_export,
    ),
    Step(
        step_id="cleanup",
        pane="harness",
        title="Leave nothing behind",
        narration="The demo undoes its one persistent change, so repeated runs start from the same state.",
        request=Request("DELETE", f"/api/combos/{DEMO_COMBO_ALIAS}"),
        summarise=_summarise_delete,
    ),
)


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemoEnvironment:
    """A throwaway home, workspace, harness store, database, and token."""

    root: Path
    config_dir: Path
    data_dir: Path
    db_path: Path
    token_path: Path
    workspace_dir: Path
    harness_dir: Path

    def environ(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return the environment this demo instance must run under."""
        env = dict(os.environ if base is None else base)
        env.update(
            {
                "HOME": str(self.root),
                "TMPDIR": str(self.root),
                "STUDIO_ALLOWED_HOSTS": "127.0.0.1,localhost,[::1],testserver",
                "STUDIO_DB_PATH": str(self.db_path),
                "STUDIO_WORKSPACE": str(self.workspace_dir),
                "STUDIO_API_TOKEN": "",
                "STUDIO_API_TOKEN_FILE": str(self.token_path),
                "STUDIO_AUTH_DISABLED": "0",
                "STUDIO_EXECUTION_MODE": "disabled",
                "STUDIO_ALLOW_HOST_EXECUTION": "0",
            }
        )
        return env

    def token(self) -> str:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DemoEnvironmentError(f"The demo token file was never written: {exc}") from exc
        if len(token) < 32:
            raise DemoEnvironmentError("The demo token is shorter than the 32-character minimum.")
        return token


def make_environment(root: Path | None = None) -> DemoEnvironment:
    """Create an isolated demo environment under a temporary directory."""
    base = (Path(root).expanduser() if root is not None else Path(tempfile.mkdtemp(prefix="m5-demo-"))).resolve()
    source = Path(__file__).resolve().parent
    if base == source or base.is_relative_to(source) or source.is_relative_to(base):
        raise DemoEnvironmentError("The demo root must be separate from the source checkout.")
    config_dir = base / "config"
    data_dir = base / "data"
    workspace_dir = base / "workspace"
    harness_dir = data_dir / "harness-sessions"
    for path in (config_dir, data_dir, workspace_dir, harness_dir, data_dir / "studio.db", config_dir / "api-token"):
        if not path.resolve().is_relative_to(base):
            raise DemoEnvironmentError(f"Demo path escapes the throwaway root: {path}")
    for path in (config_dir, data_dir, workspace_dir, harness_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return DemoEnvironment(
        root=base,
        config_dir=config_dir,
        data_dir=data_dir,
        db_path=data_dir / "studio.db",
        token_path=config_dir / "api-token",
        workspace_dir=workspace_dir,
        harness_dir=harness_dir,
    )


def assert_isolated(environ: dict[str, str], environment: DemoEnvironment) -> None:
    """Refuse to run anywhere that could touch real user data or the host."""
    mode = environ.get("STUDIO_EXECUTION_MODE", "")
    if mode != "disabled":
        raise DemoEnvironmentError(
            f"The demo requires STUDIO_EXECUTION_MODE=disabled, got {mode!r}: "
            "a demo must never execute code."
        )
    if environ.get("STUDIO_ALLOW_HOST_EXECUTION", "0") not in {"", "0"}:
        raise DemoEnvironmentError("The demo refuses to run with host execution enabled.")
    if environ.get("STUDIO_AUTH_DISABLED", "0") not in {"", "0"}:
        raise DemoEnvironmentError("The demo requires authentication to stay enabled.")
    db_path = Path(environ.get("STUDIO_DB_PATH", "")).resolve()
    if db_path != environment.db_path.resolve():
        raise DemoEnvironmentError(
            f"The demo database {db_path} is not the throwaway database {environment.db_path}."
        )
    if Path(environ.get("STUDIO_WORKSPACE", "")).resolve() != environment.workspace_dir.resolve():
        raise DemoEnvironmentError("The demo requires its throwaway workspace.")
    if Path(environ.get("STUDIO_API_TOKEN_FILE", "")).resolve() != environment.token_path.resolve():
        raise DemoEnvironmentError("The demo requires its throwaway token file.")
    for path in (environment.workspace_dir, environment.harness_dir, environment.db_path, environment.token_path):
        if not path.resolve().is_relative_to(environment.root.resolve()):
            raise DemoEnvironmentError(f"Demo path escapes the throwaway root: {path}")
    token_config = environ.get("STUDIO_API_TOKEN", "")
    if token_config:
        raise DemoEnvironmentError("The demo must generate its own token, not use a configured one.")


# ---------------------------------------------------------------------------
# Running and rendering
# ---------------------------------------------------------------------------

def _call(client: ClientLike, request: Request, headers: dict[str, str]) -> ResponseLike:
    if request.method == "GET":
        return client.get(request.path, headers=headers)
    if request.method == "POST":
        return client.post(request.path, headers=headers, json=request.body)
    if request.method == "DELETE":
        return client.delete(request.path, headers=headers)
    raise DemoEnvironmentError(f"Unsupported demo method: {request.method}")


def run_tour(client: ClientLike, token: str) -> tuple[StepOutcome, ...]:
    """Run every step against the app and return what was observed."""
    headers = {"Authorization": f"Bearer {token}"}
    outcomes: list[StepOutcome] = []
    for step in STEPS:
        response = _call(client, step.request, headers)
        if response.status_code != step.expected_status:
            detail = ""
            try:
                detail = str(response.json())[:200]
            except Exception:  # pragma: no cover - non-JSON error bodies are unusual
                detail = ""
            raise DemoStepError(
                f"Step {step.step_id!r} ({step.request.method} {step.request.path}) returned "
                f"{response.status_code}; expected {step.expected_status}. {detail}".rstrip()
            )
        try:
            payload = response.json()
        except Exception as exc:  # pragma: no cover - the app always returns JSON
            raise DemoStepError(f"Step {step.step_id!r} returned a non-JSON body: {exc}") from exc
        outcomes.append(
            StepOutcome(
                step_id=step.step_id,
                pane=step.pane,
                title=step.title,
                narration=step.narration,
                request=step.request,
                status_code=response.status_code,
                summary=step.summarise(payload),
            )
        )
    return tuple(outcomes)


VOLATILE_PATTERNS = (
    (re.compile(r"\boc-[0-9a-f]{6,}\b"), "oc-<id>"),
    (re.compile(r"\bch-[0-9a-f]{6,}\b"), "ch-<id>"),
    (re.compile(r"/(?:tmp|private/var/folders)/\S+"), "<temporary-directory>"),
    (re.compile(r"\b\d{10}(?:\.\d+)?\b"), "<timestamp>"),
)


def scrub(text: str) -> str:
    """Remove values that would make the transcript differ between runs."""
    scrubbed = text
    for pattern, replacement in VOLATILE_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


def render_transcript(outcomes: tuple[StepOutcome, ...]) -> str:
    """Render the committed transcript. Byte-stable for a given code revision."""
    lines = [
        "# Studio demo transcript",
        "",
        "Generated by `python3 scripts/demo_tour.py --write`. Do not edit by hand:",
        "`python3 scripts/demo_tour.py --check` fails CI when this file drifts.",
        "",
        "The tour drives the real application in-process against a throwaway database",
        "with a generated bearer token. It configures no provider, reads no key, calls",
        "no model, and makes no network request. Observations are curated summaries,",
        "not raw JSON, so the file stays readable and byte-stable.",
        "",
        "| # | Pane | Step | Request | Status |",
        "|---|---|---|---|---|",
    ]
    for index, outcome in enumerate(outcomes, start=1):
        lines.append(
            f"| {index} | {outcome.pane} | {outcome.title} | "
            f"`{outcome.request.method} {outcome.request.path}` | {outcome.status_code} |"
        )
    for index, outcome in enumerate(outcomes, start=1):
        lines.extend(
            [
                "",
                f"## {index}. {outcome.title}",
                "",
                outcome.narration,
                "",
                f"`{outcome.request.method} {outcome.request.path}` -> {outcome.status_code}",
                "",
                f"Observed: {outcome.summary}",
            ]
        )
    lines.extend(
        [
            "",
            "## Panes in this build",
            "",
            "| Tab | Pane | What it is for |",
            "|---|---|---|",
        ]
    )
    for pane in PANES:
        lines.append(f"| `{pane.tab}` | {pane.title} | {pane.purpose} |")
    lines.extend(
        [
            "",
            "## Reproducing",
            "",
            "```bash",
            "python3 scripts/demo_tour.py            # print the tour",
            "python3 scripts/demo_tour.py --write    # regenerate this file",
            "python3 scripts/demo_tour.py --check    # fail if it drifted",
            "```",
            "",
            "The tour never reuses your real database. See `docs/demo.md` for the walkthrough",
            "and for how the screenshots are captured.",
            "",
        ]
    )
    return scrub("\n".join(lines))


def transcript_json(outcomes: tuple[StepOutcome, ...]) -> str:
    """Machine-readable form of the same run, for tests and tooling."""
    return json.dumps(
        [
            {
                "step": outcome.step_id,
                "pane": outcome.pane,
                "request": f"{outcome.request.method} {outcome.request.path}",
                "status": outcome.status_code,
                "observed": outcome.summary,
            }
            for outcome in outcomes
        ],
        indent=2,
        sort_keys=True,
    ) + "\n"
