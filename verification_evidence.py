"""Verification evidence for coding-completion claims (M5-P2-04).

`M5-P1-06` refuses to mark a coding task complete without an authoritative patch,
protected-file and test check. A green `exit_code` on its own still says very
little: *which* revision was tested, whether the command actually ran or was
refused, and whether a failure was already there before the agent touched the
workspace. Without those answers a passing run can be attributed to the wrong
commit and a pre-existing failure can be blamed on the change.

This module produces the missing evidence, and nothing else:

1. **A source revision** — the workspace's Git ``HEAD`` (when it is a Git
   repository), the dirty path list, and the SHA-256 of every file the
   verification contract names, folded into one canonical digest. Capture it
   before and after the authoritative command: if the digest changed while the
   command ran, the result cannot be tied to one revision.
2. **A test result tied to it** — the transcript digest covers the command, the
   exit code, the bounded output *and* the revision digest, so two runs that
   agree on all four produce the same evidence hash and any difference is
   visible.
3. **An environment record** — interpreter, platform and working directory the
   command ran in, plus the execution surface.
4. **A failure classification** — every ``FAILED``/``ERROR`` line is classified as
   ``introduced``, ``pre_existing`` (a failure the caller recorded before the
   task started) or ``environmental`` (missing interpreter/package, no disk,
   network or container runtime unavailable). Unparseable output falls back to
   the global markers rather than silently calling the run clean.
5. **A denial verdict** — if the output shows the runtime *refused* to execute
   (plan policy, disabled execution, blocked bypass, missing sandbox runtime) the
   evidence is marked denied. A denied run never counts as a passing one, even
   when its exit code is 0.

The module is standard-library only, never uses a shell, sanitizes the child
environment, bounds the output it keeps, refuses to read through symlinks, and
performs no writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

EVIDENCE_SCHEMA_VERSION = 1

#: Largest slice of command output kept in the evidence record.
MAX_OUTPUT_BYTES = 64 * 1024
#: Model-facing excerpt length stored alongside the digest.
MAX_EXCERPT_CHARS = 500
#: Bound on the number of failure records carried in one report.
MAX_FAILURES = 50
#: Bound on the number of dirty paths recorded from `git status --porcelain`.
MAX_DIRTY_PATHS = 50
#: Test-tool artifacts that a run creates but that never describe the revision.
#: Filtering them keeps `revision_changed` about source changes, not caches.
ARTIFACT_PATH_PREFIXES: tuple[str, ...] = (
    ".pytest_cache/",
    "__pycache__/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".venv/",
    "node_modules/",
    "htmlcov/",
    ".harnesses/",
)
ARTIFACT_PATH_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".coverage", ".coverage.xml")

#: Bound on the size of a single file hashed into the revision.
MAX_HASH_BYTES = 8 * 1024 * 1024
#: Wall-clock bound on the `git` probes used for the revision.
GIT_TIMEOUT_S = 10.0

#: Environment variables passed to child processes; everything else is dropped so a
#: verification run cannot inherit credentials the task never needed.
SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
    }
)

#: Substrings that mean the runtime refused to execute, so there is no test result.
DENIAL_MARKERS: tuple[str, ...] = (
    "code execution is disabled",
    "denied by `plan`",
    "denied by plan",
    "direct executor bypass blocked",
    "requires an explicit approval in standard policy",
    "external mcp tools are disabled",
    "sandbox runtime unavailable",
    "container runtime unavailable",
    "cannot connect to the docker daemon",
    "cannot connect to the podman socket",
)

#: Substrings that mean the environment could not run the command at all.
ENVIRONMENT_MARKERS: tuple[str, ...] = (
    "modulenotfounderror",
    "importerror: no module named",
    "no module named pytest",
    "command not found",
    "executable file not found",
    "errno 28",
    "no space left on device",
    "connection refused",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "operation not permitted",
)

_PYTEST_FAILURE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$", re.MULTILINE)
_UNITTEST_FAILURE_RE = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)(?:\s+\(([^)]+)\))?", re.MULTILINE)

CLASSIFICATION_INTRODUCED = "introduced"
CLASSIFICATION_PRE_EXISTING = "pre_existing"
CLASSIFICATION_ENVIRONMENTAL = "environmental"

RUN_PASSED = "passed"
RUN_DENIED = "denied"
RUN_INTRODUCED = "introduced"
RUN_PRE_EXISTING_ONLY = "pre_existing_only"
RUN_ENVIRONMENT_BLOCKED = "environmental"
RUN_UNCLASSIFIED = "unclassified"


class VerificationEvidenceError(ValueError):
    """Raised when evidence cannot be captured safely."""


def _bounded(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace")[-limit:]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _join_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Combine captured streams, tolerating the bytes a timeout can hand back."""
    parts: list[str] = []
    for stream in (stdout, stderr):
        if stream is None:
            continue
        parts.append(stream.decode("utf-8", errors="replace") if isinstance(stream, bytes) else str(stream))
    return "\n".join(parts).strip()


def safe_child_env() -> dict[str, str]:
    """The environment a verification command is allowed to inherit."""
    env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# ---------------------------------------------------------------------------
# Source revision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """The exact workspace state a verification result belongs to."""

    digest: str
    git_head: str = ""
    git_dirty_paths: tuple[str, ...] = ()
    file_digests: tuple[tuple[str, str | None], ...] = ()
    captured_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "digest": self.digest,
            "git_head": self.git_head,
            "git_dirty": list(self.git_dirty_paths),
            "files": {path: digest for path, digest in self.file_digests},
            "captured_at": self.captured_at,
        }


def _normalize_rel_path(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise VerificationEvidenceError("Revision file paths must be non-empty.")
    if "\x00" in text:
        raise VerificationEvidenceError("Revision file paths must not contain NUL.")
    if os.path.isabs(text) or text.startswith("~"):
        raise VerificationEvidenceError(f"Revision file path must be workspace-relative: {text!r}")
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise VerificationEvidenceError(f"Revision file path must not traverse upward: {text!r}")
    return "/".join(parts)


def hash_workspace_file(workspace: Path, raw_rel_path: str) -> str | None:
    """SHA-256 of one confined workspace file, or ``None`` when it does not exist.

    Symlinked directories and leaves are refused instead of followed, so the
    revision never hashes bytes from outside the workspace.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    rel_parts = _normalize_rel_path(raw_rel_path).split("/")
    current = root
    for part in rel_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise VerificationEvidenceError(f"Refusing to hash through symlinked directory {str(current)!r}.")
        if not current.is_dir():
            return None
    leaf = root.joinpath(*rel_parts)
    try:
        entry = os.lstat(leaf)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(entry.st_mode):
        raise VerificationEvidenceError(f"Refusing to hash symlink {str(leaf)!r}.")
    if not stat.S_ISREG(entry.st_mode):
        return None
    if entry.st_size > MAX_HASH_BYTES:
        raise VerificationEvidenceError(f"{str(leaf)!r} is larger than the {MAX_HASH_BYTES}-byte revision limit.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(leaf, flags)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _is_artifact_path(rel_path: str) -> bool:
    """True for cache/coverage artifacts a test run creates rather than author."""
    normalized = rel_path.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if any(normalized.startswith(prefix) or f"/{prefix}" in f"/{normalized}" for prefix in ARTIFACT_PATH_PREFIXES):
        return True
    return any(normalized.endswith(suffix) for suffix in ARTIFACT_PATH_SUFFIXES)


def _git_probe(workspace: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=safe_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return int(proc.returncode), (proc.stdout or "").strip()


def capture_source_revision(workspace: Path, files: Sequence[str] = ()) -> SourceRevision:
    """Capture the workspace revision a verification result is tied to."""
    root = Path(workspace).expanduser().resolve(strict=True)
    git_head = ""
    dirty: tuple[str, ...] = ()
    if (root / ".git").exists():
        code, head = _git_probe(root, "rev-parse", "HEAD")
        if code == 0 and re.fullmatch(r"[0-9a-f]{40}", head or ""):
            git_head = head
            status_code, status_out = _git_probe(root, "status", "--porcelain=v1", "--untracked-files=all")
            if status_code == 0:
                paths = []
                for line in status_out.splitlines():
                    entry = line[3:].strip() if len(line) > 3 else line.strip()
                    if not entry or _is_artifact_path(entry):
                        continue
                    paths.append(entry[:200])
                dirty = tuple(sorted(set(paths))[:MAX_DIRTY_PATHS])
    normalized = [_normalize_rel_path(rel) for rel in files]
    file_digests = tuple((rel, hash_workspace_file(root, rel)) for rel in sorted(set(normalized)))
    payload = json.dumps(
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "git_head": git_head,
            "git_dirty": list(dirty),
            "files": [[rel, digest] for rel, digest in file_digests],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceRevision(
        digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        git_head=git_head,
        git_dirty_paths=dirty,
        file_digests=file_digests,
        captured_at=round(time.time(), 6),
    )


def environment_snapshot(workspace: Path) -> dict[str, Any]:
    """Record the environment the authoritative command ran in."""
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system() or sys.platform,
        "machine": platform.machine(),
        "cwd": str(Path(workspace).expanduser().resolve()),
        "execution_surface": "subprocess",
    }


# ---------------------------------------------------------------------------
# Failure and denial classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """One classified failure reported by a verification command."""

    test_id: str
    kind: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"test_id": self.test_id, "kind": self.kind, "detail": self.detail[:240]}


def detect_denial(output: str) -> str:
    """Return the denial marker an output matched, or ``""`` when it did not."""
    lowered = (output or "").lower()
    for marker in DENIAL_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _parse_failure_lines(output: str) -> list[tuple[str, str]]:
    """``(test_id, detail)`` pairs from pytest and unittest failure lines."""
    found: list[tuple[str, str]] = []
    for match in _PYTEST_FAILURE_RE.finditer(output or ""):
        test_id = match.group(1).strip().rstrip(":")
        if test_id:
            found.append((test_id[:300], (match.group(2) or "").strip()[:300]))
    for match in _UNITTEST_FAILURE_RE.finditer(output or ""):
        test_id = match.group(1).strip()
        where = (match.group(2) or "").strip()
        if test_id:
            found.append((f"{where}::{test_id}" if where else test_id, ""))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for test_id, detail in found:
        if test_id in seen:
            continue
        seen.add(test_id)
        unique.append((test_id, detail))
        if len(unique) >= MAX_FAILURES:
            break
    return unique


def _matches_pre_existing(test_id: str, pre_existing: Sequence[str]) -> bool:
    for base in pre_existing:
        candidate = str(base or "").strip()
        if not candidate:
            continue
        if test_id == candidate or test_id.startswith(candidate) or candidate.startswith(test_id):
            return True
    return False


def classify_test_output(
    output: str,
    *,
    exit_code: int | None,
    pre_existing: Sequence[str] = (),
) -> tuple[FailureRecord, ...]:
    """Classify the failures in a verification command's output.

    A failure is ``pre_existing`` when the caller recorded the same test id
    before the task started, ``environmental`` when the line (or, for output we
    cannot parse, the whole output) carries an environment marker, and
    ``introduced`` otherwise. Output with no failure lines and a non-zero exit is
    returned as one unclassified record so it is never mistaken for a clean run.
    """
    parsed = _parse_failure_lines(output)
    lowered = (output or "").lower()
    records: list[FailureRecord] = []
    for test_id, detail in parsed:
        haystack = f"{test_id} {detail}".lower()
        env_marker = next((marker for marker in ENVIRONMENT_MARKERS if marker in haystack), "")
        if env_marker:
            records.append(FailureRecord(test_id=test_id, kind=CLASSIFICATION_ENVIRONMENTAL, detail=env_marker))
        elif _matches_pre_existing(test_id, pre_existing):
            records.append(FailureRecord(test_id=test_id, kind=CLASSIFICATION_PRE_EXISTING, detail=detail))
        else:
            records.append(FailureRecord(test_id=test_id, kind=CLASSIFICATION_INTRODUCED, detail=detail))
    if records:
        return tuple(records)
    if exit_code == 0:
        return ()
    env_marker = next((marker for marker in ENVIRONMENT_MARKERS if marker in lowered), "")
    if env_marker:
        return (FailureRecord(test_id="", kind=CLASSIFICATION_ENVIRONMENTAL, detail=env_marker),)
    if _matches_pre_existing(output, pre_existing):  # pragma: no cover - defensive
        return (FailureRecord(test_id="", kind=CLASSIFICATION_PRE_EXISTING, detail=output[:240]),)
    return (FailureRecord(test_id="", kind=CLASSIFICATION_INTRODUCED, detail=output[:240]),)


# ---------------------------------------------------------------------------
# Evidence record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestEvidence:
    """Auditable result of one authoritative verification command."""

    command: tuple[str, ...]
    exit_code: int | None
    output_sha256: str
    output_excerpt: str
    revision_before: SourceRevision | None = None
    revision_after: SourceRevision | None = None
    denied: str = ""
    failures: tuple[FailureRecord, ...] = ()
    classification: str = RUN_UNCLASSIFIED
    environment: dict[str, Any] = field(default_factory=dict)
    #: The bounded transcript itself. Kept in memory for coverage matching and
    #: never serialised (``output_sha256`` + ``output_excerpt`` are the record).
    output_text: str = field(default="", repr=False, compare=False)

    @property
    def revision_changed(self) -> bool:
        if self.revision_before is None or self.revision_after is None:
            return False
        return self.revision_before.digest != self.revision_after.digest

    @property
    def introduced_failures(self) -> tuple[FailureRecord, ...]:
        return tuple(record for record in self.failures if record.kind == CLASSIFICATION_INTRODUCED)

    @property
    def pre_existing_failures(self) -> tuple[FailureRecord, ...]:
        return tuple(record for record in self.failures if record.kind == CLASSIFICATION_PRE_EXISTING)

    @property
    def environment_failures(self) -> tuple[FailureRecord, ...]:
        return tuple(record for record in self.failures if record.kind == CLASSIFICATION_ENVIRONMENTAL)

    @property
    def passed(self) -> bool:
        return self.classification == RUN_PASSED and not self.denied

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "output_sha256": self.output_sha256,
            "output_excerpt": self.output_excerpt,
            "revision_before": self.revision_before.to_dict() if self.revision_before else None,
            "revision_after": self.revision_after.to_dict() if self.revision_after else None,
            "revision_changed": self.revision_changed,
            "denied": self.denied,
            "failures": [record.to_dict() for record in self.failures],
            "classification": self.classification,
            "environment": dict(self.environment),
        }


def build_test_evidence(
    command: Sequence[str],
    *,
    exit_code: int | None,
    output: str,
    revision_before: SourceRevision | None = None,
    revision_after: SourceRevision | None = None,
    pre_existing: Sequence[str] = (),
    environment: dict[str, Any] | None = None,
) -> TestEvidence:
    """Assemble the evidence record, tying the transcript hash to the revision."""
    bounded = _bounded(output or "")
    command_tuple = tuple(str(token) for token in command)
    denial = detect_denial(bounded)
    failures = () if denial else classify_test_output(bounded, exit_code=exit_code, pre_existing=pre_existing)
    if denial:
        classification = RUN_DENIED
    elif exit_code is None:
        classification = RUN_UNCLASSIFIED
    elif exit_code == 0 and not failures:
        classification = RUN_PASSED
    elif exit_code == 0:
        classification = RUN_UNCLASSIFIED
    elif failures and all(record.kind == CLASSIFICATION_ENVIRONMENTAL for record in failures):
        classification = RUN_ENVIRONMENT_BLOCKED
    elif failures and all(record.kind == CLASSIFICATION_PRE_EXISTING for record in failures):
        classification = RUN_PRE_EXISTING_ONLY
    elif failures and any(record.kind == CLASSIFICATION_INTRODUCED for record in failures):
        classification = RUN_INTRODUCED
    else:  # pragma: no cover - covered by the classifier's fallback record
        classification = RUN_UNCLASSIFIED
    revision_digest = revision_before.digest if revision_before is not None else ""
    transcript = json.dumps(
        {
            "command": list(command_tuple),
            "exit_code": exit_code,
            "output": bounded,
            "revision_digest": revision_digest,
            "revision_after": revision_after.digest if revision_after is not None else "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return TestEvidence(
        command=command_tuple,
        exit_code=exit_code,
        output_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        output_excerpt=bounded[:MAX_EXCERPT_CHARS],
        revision_before=revision_before,
        revision_after=revision_after,
        denied=denial,
        failures=failures,
        classification=classification,
        environment=dict(environment or {}),
        output_text=bounded,
    )


def evaluate_acceptance_coverage(
    declared: Sequence[tuple[str, Sequence[str]]],
    *,
    output: str,
    failures: Sequence[FailureRecord] = (),
) -> tuple[dict[str, Any], ...]:
    """Map declared acceptance criteria to the tests observed in one transcript.

    A declared test id counts as covered only when it appears verbatim in the
    transcript and is not among the classified failures. This records what the
    run actually printed; it does not assert that the test exercises the
    criterion, so an uncovered criterion fails closed instead of being assumed.
    """
    haystack = output or ""
    failing = {record.test_id for record in failures if record.test_id}
    entries: list[dict[str, Any]] = []
    for criterion, test_ids in declared:
        ids = [str(t).strip() for t in test_ids if str(t).strip()]
        missing = [test_id for test_id in ids if test_id not in haystack or test_id in failing]
        entries.append(
            {
                "criterion": str(criterion)[:300],
                "test_ids": ids,
                "covered": bool(ids) and not missing,
                "missing_test_ids": missing,
            }
        )
    return tuple(entries)


def run_verification_command(
    command: Sequence[str],
    *,
    workspace: Path,
    timeout_s: float,
    files: Sequence[str] = (),
    pre_existing: Sequence[str] = (),
) -> TestEvidence:
    """Run one authoritative verification command and capture its evidence.

    The command is executed without a shell, with a sanitized environment, under a
    timeout, and the workspace revision is captured before and after so a result
    that spanned a change cannot be tied to one revision.
    """
    if not command:
        raise VerificationEvidenceError("A verification command is required.")
    if any((not str(token) or "\x00" in str(token)) for token in command):
        raise VerificationEvidenceError("Verification command tokens must be non-empty and NUL-free.")
    root = Path(workspace).expanduser().resolve(strict=True)
    env = safe_child_env()
    revision_before = capture_source_revision(root, files)
    exit_code: int | None = None
    output = ""
    failure_detail = ""
    try:
        proc = subprocess.run(
            [str(token) for token in command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
            env=env,
        )
        exit_code = int(proc.returncode)
        output = _join_output(proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        output = _join_output(exc.stdout, exc.stderr)
        failure_detail = f"verification command timed out after {timeout_s}s"
    except OSError as exc:
        exit_code = None
        output = f"verification command failed to start: {exc}"
        failure_detail = str(exc)
    revision_after = capture_source_revision(root, files)
    evidence = build_test_evidence(
        command,
        exit_code=exit_code,
        output=output,
        revision_before=revision_before,
        revision_after=revision_after,
        pre_existing=pre_existing,
        environment=environment_snapshot(root),
    )
    if failure_detail and not evidence.denied:
        # A command that never produced an exit code is an execution failure, not a
        # test verdict; keep the reason on the record via an environment failure.
        evidence = TestEvidence(
            command=evidence.command,
            exit_code=evidence.exit_code,
            output_sha256=evidence.output_sha256,
            output_excerpt=evidence.output_excerpt,
            revision_before=evidence.revision_before,
            revision_after=evidence.revision_after,
            denied=evidence.denied,
            failures=(
                FailureRecord(test_id="", kind=CLASSIFICATION_ENVIRONMENTAL, detail=failure_detail[:240]),
            )
            + evidence.failures,
            classification=RUN_ENVIRONMENT_BLOCKED,
            environment=evidence.environment,
            output_text=evidence.output_text,
        )
    return evidence


