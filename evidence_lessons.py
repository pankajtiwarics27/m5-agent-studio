"""Evidence-backed cross-task lessons and bounded decision-boundary retrieval (M5-P6-01).

Curated cross-task lessons are promoted and retrieved ONLY when verifiable
completion evidence justifies them, under strict project-scope isolation and
hard token ceilings:

1. **Provenance and promotion gates:** Candidate lessons never promote from raw
   model text alone. Promotion requires ``verified=True``, a concrete
   ``verification_spec_sha256`` with ``passed_checks >= 1``, trusted/verified
   provenance (never ``untrusted`` or ``secret``), secret/credential hygiene,
   and at least one confined ``O_NOFOLLOW`` workspace file SHA-256 anchor.
2. **Decision-boundary retrieval and hard token ceilings:** Retrieval executes
   only at explicit decision boundaries (``turn_plan_start``,
   ``verification_replan``, ``handoff_switch``) — never on mid-turn tool loops —
   and enforces a strict cumulative token ceiling (including banner framing)
   without mid-truncating lesson text.
3. **Staleness invalidation:** Before serving any lesson, anchored workspace
   files, security configuration generation, lineage evidence freshness, and
   verification-regression counters are checked; drifted lessons flip to
   ``stale`` and are excluded.
4. **Optional toggle & measured A/B gate:** Disabled cleanly via environment or
   runtime/per-turn configuration with zero prompt or persistence overhead, and
   gated fail-closed if A/B coding evaluation does not show positive verified
   completion benefit with zero cross-project leakage.

Stdlib-only module designed for both modular ``app.py`` import and single-file
inlining into ``studio.py`` (private ``_el_`` / ``_EL_`` namespace).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Literal
import uuid

EL_SCHEMA_VERSION = 1
EL_DIRNAME = "lessons"
EL_STATE_NAME = "state.json"
EL_TEMP_PREFIX = ".el-state-"

DecisionBoundary = Literal["turn_plan_start", "verification_replan", "handoff_switch"]
DECISION_BOUNDARIES: tuple[str, ...] = (
    "turn_plan_start",
    "verification_replan",
    "handoff_switch",
)

LessonStatus = Literal["candidate", "promoted", "stale", "revoked"]
LESSON_STATUSES: tuple[str, ...] = ("candidate", "promoted", "stale", "revoked")

ALLOWED_LESSON_TRUST_LEVELS: frozenset[str] = frozenset({"trusted", "verified_task"})
ALLOWED_LESSON_CONFIDENTIALITY: frozenset[str] = frozenset({"public", "internal"})

DEFAULT_LESSON_TOKEN_CEILING = 160
ABSOLUTE_MAX_LESSON_TOKEN_CEILING = 256
MAX_SINGLE_LESSON_TOKENS = 110
MAX_RETRIEVED_LESSONS = 3
MAX_CONSECUTIVE_VERIFICATION_FAILURES = 2

_EL_WORD_RE = re.compile(r"[a-z0-9_./:-]{2,}", re.IGNORECASE)
_EL_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_EL_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:API_KEY|SECRET_KEY|ACCESS_TOKEN|PRIVATE_KEY|PASSWORD|STUDIO_API_TOKEN)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)
_EL_UNIX_ABS_PATH_RE = re.compile(r"(?:^|\s)(/(?:Users|home|var|tmp|etc|opt|private)/[^\s`'\"<>]+)")
_EL_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "when",
        "only",
        "must",
        "task",
        "file",
        "files",
        "code",
        "make",
        "update",
        "fix",
        "use",
        "keep",
        "during",
        "instead",
        "follow",
        "import",
        "implement",
        "satisfy",
        "pass",
        "passes",
        "pkg",
        "src",
        "lib",
        "app",
        "test",
        "tests",
        "check",
    }
)


class EvidenceLessonError(RuntimeError):
    """Base error for evidence-backed lesson failures."""


class LessonStoreIntegrityError(EvidenceLessonError):
    """Raised when ``$HARNESS_STORE_DIR/lessons/state.json`` is unreadable, symlinked, or corrupt."""


class LessonPromotionRejectedError(EvidenceLessonError):
    """Raised when a candidate lesson fails provenance, privacy, or size promotion gates."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _el_now() -> float:
    return time.time()


def _el_new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _el_canonical_sha256(payload: Mapping[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "report_sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _el_reject_if_symlink(path: Path, label: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LessonStoreIntegrityError(f"Cannot stat {label} `{path}`: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise LessonStoreIntegrityError(f"Refusing symlinked {label}: `{path}`.")


def estimate_lesson_tokens(text: str) -> int:
    """Conservative deterministic token ceiling estimate (max of word count and ceil(chars/3.5))."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return 0
    by_words = len(cleaned.split())
    by_chars = math.ceil(len(cleaned) / 3.5)
    return max(1, by_words, by_chars)


def resolve_canonical_project_root(
    workspace_path: Path | str,
    *,
    owning_workspace_root: Path | str | None = None,
) -> Path:
    """Resolve a workspace or ``.harnesses/worktrees/<sid>`` path to its canonical project root."""
    if owning_workspace_root is not None and str(owning_workspace_root).strip():
        return Path(owning_workspace_root).resolve()
    resolved = Path(workspace_path).resolve()
    parts = resolved.parts
    for idx in range(len(parts) - 2):
        if parts[idx] == ".harnesses" and parts[idx + 1] == "worktrees":
            prefix = parts[:idx]
            if prefix:
                return Path(*prefix).resolve()
    return resolved


def derive_project_scope_id(
    workspace_path: Path | str,
    *,
    owning_workspace_root: Path | str | None = None,
) -> str:
    """Derive an opaque, collision-resistant project scope identifier for strict isolation."""
    canon = resolve_canonical_project_root(
        workspace_path, owning_workspace_root=owning_workspace_root
    )
    digest = hashlib.sha256(f"m5-project-scope:v1:{canon}".encode("utf-8")).hexdigest()
    return f"proj-{digest[:32]}"


def _el_normalize_rel_path(workspace_root: Path, raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise LessonPromotionRejectedError(
            "invalid_anchor_path",
            f"Lesson anchor path must be a non-empty relative path: {raw_path!r}",
        )
    cleaned = raw_path.strip().replace("\\", "/")
    if cleaned.startswith("/"):
        raise LessonPromotionRejectedError(
            "anchor_path_escapes_workspace",
            f"Absolute lesson anchor path is forbidden: {raw_path!r}",
        )
    parts = [pt for pt in cleaned.split("/") if pt not in ("", ".")]
    if not parts or any(pt == ".." for pt in parts):
        raise LessonPromotionRejectedError(
            "anchor_path_escapes_workspace",
            f"Lesson anchor path {raw_path!r} must stay strictly within workspace_root.",
        )
    lower_leaf = parts[-1].lower()
    if (
        ".git" in parts
        or lower_leaf == ".env"
        or lower_leaf.startswith(".env.")
        or lower_leaf.endswith((".pem", ".key", ".p12", ".pfx"))
    ):
        raise LessonPromotionRejectedError(
            "anchor_path_forbidden_secret_or_vcs",
            f"Lesson anchor path {raw_path!r} cannot reference VCS metadata or secret files.",
        )
    ws_res = workspace_root.resolve()
    cursor = ws_res
    for part in parts:
        cursor = cursor / part
        try:
            st = os.lstat(cursor)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(st.st_mode):
            raise LessonPromotionRejectedError(
                "anchor_symlink_forbidden",
                f"Symlink component `{cursor.name}` in lesson anchor `{cleaned}` is forbidden.",
            )
    resolved = (ws_res / "/".join(parts)).resolve()
    if resolved != ws_res and ws_res not in resolved.parents:
        raise LessonPromotionRejectedError(
            "anchor_path_escapes_workspace",
            f"Lesson anchor path `{cleaned}` resolves outside workspace_root.",
        )
    return "/".join(parts)


def compute_confined_file_sha256(workspace_root: Path, rel_path: str) -> str | None:
    """Compute SHA-256 of ``workspace_root / rel_path`` refusing symlinks along every segment."""
    try:
        norm = _el_normalize_rel_path(workspace_root, rel_path)
    except LessonPromotionRejectedError:
        return None
    target = workspace_root.resolve() / norm
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        return None
    hasher = hashlib.sha256()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(fd)
    return hasher.hexdigest()


def _el_check_privacy_hygiene(
    text: str,
    *,
    canonical_project_root: Path,
    known_secret_values: Iterable[str] = (),
) -> None:
    for pat in _EL_SECRET_PATTERNS:
        if pat.search(text):
            raise LessonPromotionRejectedError(
                "lesson_contains_secret_pattern",
                "Candidate lesson contains a credential or secret pattern; promotion refused.",
            )
    for sec in known_secret_values:
        s_val = str(sec or "").strip()
        if len(s_val) >= 8 and s_val in text:
            raise LessonPromotionRejectedError(
                "lesson_contains_registered_secret",
                "Candidate lesson contains a registered secret value; promotion refused.",
            )
    canon_str = str(canonical_project_root.resolve())
    for match in _EL_UNIX_ABS_PATH_RE.finditer(text):
        found_path = match.group(1)
        if not found_path.startswith(canon_str):
            raise LessonPromotionRejectedError(
                "lesson_contains_external_path",
                f"Candidate lesson embeds host/cross-project path `{found_path}`; use repo-relative paths only.",
            )


def _el_tokenize_keywords(text: str) -> set[str]:
    out: set[str] = set()
    for tok in _EL_WORD_RE.findall(str(text or "").lower()):
        cleaned = tok.strip("./:-_")
        if len(cleaned) >= 3 and cleaned not in _EL_STOPWORDS:
            out.add(cleaned)
        if "/" in cleaned:
            leaf = cleaned.rsplit("/", 1)[-1]
            if len(leaf) >= 3 and leaf not in _EL_STOPWORDS:
                out.add(leaf)
            stem = leaf.split(".", 1)[0]
            if len(stem) >= 3 and stem not in _EL_STOPWORDS:
                out.add(stem)
    return out


@dataclass(frozen=True, slots=True)
class LessonProvenance:
    """Cryptographic and verification provenance justifying a promoted lesson."""

    source_session_id: str
    source_task_id: str
    source_epoch: int
    verified: bool
    verification_spec_sha256: str
    passed_checks: int
    source_trust: str = "verified_task"
    confidentiality: str = "internal"
    security_generation: int = 0
    source_revision: str = ""
    lineage_evidence_id: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_session_id": self.source_session_id,
            "source_task_id": self.source_task_id,
            "source_epoch": self.source_epoch,
            "verified": self.verified,
            "verification_spec_sha256": self.verification_spec_sha256,
            "passed_checks": self.passed_checks,
            "source_trust": self.source_trust,
            "confidentiality": self.confidentiality,
            "security_generation": self.security_generation,
            "source_revision": self.source_revision,
            "lineage_evidence_id": self.lineage_evidence_id,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LessonProvenance:
        return cls(
            source_session_id=str(data.get("source_session_id", "")),
            source_task_id=str(data.get("source_task_id", "")),
            source_epoch=int(data.get("source_epoch", 0)),
            verified=bool(data.get("verified", False)),
            verification_spec_sha256=str(data.get("verification_spec_sha256", "")),
            passed_checks=int(data.get("passed_checks", 0)),
            source_trust=str(data.get("source_trust", "verified_task")),
            confidentiality=str(data.get("confidentiality", "internal")),
            security_generation=int(data.get("security_generation", 0)),
            source_revision=str(data.get("source_revision", "")),
            lineage_evidence_id=str(data.get("lineage_evidence_id", "")),
        )


@dataclass(frozen=True, slots=True)
class LessonRecord:
    """Durable, project-scoped, evidence-backed lesson record."""

    lesson_id: str
    project_scope_id: str
    topic_key: str
    title: str
    guidance: str
    tags: tuple[str, ...]
    failure_codes: tuple[str, ...]
    anchored_files: dict[str, str]
    provenance: LessonProvenance
    status: LessonStatus
    estimated_tokens: int
    created_at: float
    updated_at: float
    stale_reason: str = ""
    retrieval_count: int = 0
    verified_hit_count: int = 0
    consecutive_failures: int = 0

    def render_bullet(self) -> str:
        anchors = ", ".join(sorted(self.anchored_files.keys())[:3])
        return f"- [{self.topic_key}] {self.title}: {self.guidance} (anchors: {anchors})"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "project_scope_id": self.project_scope_id,
            "topic_key": self.topic_key,
            "title": self.title,
            "guidance": self.guidance,
            "tags": list(self.tags),
            "failure_codes": list(self.failure_codes),
            "anchored_files": dict(self.anchored_files),
            "provenance": self.provenance.to_mapping(),
            "status": self.status,
            "estimated_tokens": self.estimated_tokens,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stale_reason": self.stale_reason,
            "retrieval_count": self.retrieval_count,
            "verified_hit_count": self.verified_hit_count,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LessonRecord:
        status_raw = str(data.get("status", "candidate"))
        if status_raw not in LESSON_STATUSES:
            raise LessonStoreIntegrityError(f"Invalid lesson status: {status_raw!r}")
        anchors_raw = data.get("anchored_files")
        if not isinstance(anchors_raw, Mapping):
            raise LessonStoreIntegrityError("Lesson anchored_files must be a mapping.")
        return cls(
            lesson_id=str(data["lesson_id"]),
            project_scope_id=str(data["project_scope_id"]),
            topic_key=str(data["topic_key"]),
            title=str(data["title"]),
            guidance=str(data["guidance"]),
            tags=tuple(str(x) for x in data.get("tags", ())),
            failure_codes=tuple(str(x) for x in data.get("failure_codes", ())),
            anchored_files={str(k): str(v) for k, v in anchors_raw.items()},
            provenance=LessonProvenance.from_mapping(data["provenance"]),
            status=status_raw,  # type: ignore[arg-type]
            estimated_tokens=int(data.get("estimated_tokens", 0)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            stale_reason=str(data.get("stale_reason", "")),
            retrieval_count=int(data.get("retrieval_count", 0)),
            verified_hit_count=int(data.get("verified_hit_count", 0)),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
        )


@dataclass(frozen=True, slots=True)
class LessonRetrievalResult:
    """Bounded decision-boundary retrieval output with full budget/staleness audit."""

    enabled: bool
    boundary: str
    project_scope_id: str
    lessons: tuple[LessonRecord, ...]
    rendered_prompt: str
    used_tokens: int
    ceiling_tokens: int
    dropped_for_budget_ids: tuple[str, ...] = ()
    invalidated_stale_ids: tuple[str, ...] = ()
    skipped_reason: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "boundary": self.boundary,
            "project_scope_id": self.project_scope_id,
            "lesson_ids": [item.lesson_id for item in self.lessons],
            "lessons": [item.to_mapping() for item in self.lessons],
            "rendered_prompt": self.rendered_prompt,
            "used_tokens": self.used_tokens,
            "ceiling_tokens": self.ceiling_tokens,
            "dropped_for_budget_ids": list(self.dropped_for_budget_ids),
            "invalidated_stale_ids": list(self.invalidated_stale_ids),
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True, slots=True)
class LessonEvalRun:
    """Single coding run observation for baseline vs evidence-backed lessons A/B testing."""

    run_id: str
    arm: Literal["baseline", "lessons"]
    scenario_id: str
    task_digest: str
    config_digest: str
    hardware_digest: str
    verified: bool
    steps: int
    total_tokens: float
    retrieved_lesson_tokens: int
    latency_ms: float
    cross_project_leaks: int = 0
    stale_lessons_injected: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "arm": self.arm,
            "scenario_id": self.scenario_id,
            "task_digest": self.task_digest,
            "config_digest": self.config_digest,
            "hardware_digest": self.hardware_digest,
            "verified": self.verified,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "retrieved_lesson_tokens": self.retrieved_lesson_tokens,
            "latency_ms": self.latency_ms,
            "cross_project_leaks": self.cross_project_leaks,
            "stale_lessons_injected": self.stale_lessons_injected,
        }


def _el_median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return round(ordered[mid], 6)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 6)


def compare_lesson_ab_runs(
    scenario_id: str,
    runs: Sequence[LessonEvalRun],
    *,
    ceiling_tokens: int = DEFAULT_LESSON_TOKEN_CEILING,
) -> dict[str, Any]:
    """Compare baseline vs lessons arms on matched task/config/hardware digests."""
    if not runs:
        raise ValueError("compare_lesson_ab_runs requires at least one run per arm.")
    baseline_runs = [r for r in runs if r.arm == "baseline"]
    lesson_runs = [r for r in runs if r.arm == "lessons"]
    if not baseline_runs or not lesson_runs:
        raise ValueError("Both 'baseline' and 'lessons' arms are required.")
    task_digests = {r.task_digest for r in runs}
    cfg_digests = {r.config_digest for r in runs}
    hw_digests = {r.hardware_digest for r in runs}
    if len(task_digests) != 1 or len(cfg_digests) != 1 or len(hw_digests) != 1:
        raise ValueError("A/B runs must share identical task, config, and hardware digests.")

    def _summarize(arm_runs: Sequence[LessonEvalRun]) -> dict[str, Any]:
        sr = round(sum(1 for r in arm_runs if r.verified) / len(arm_runs), 6)
        return {
            "count": len(arm_runs),
            "success_rate": sr,
            "steps_median": _el_median([float(r.steps) for r in arm_runs]),
            "total_tokens_median": _el_median([r.total_tokens for r in arm_runs]),
            "latency_ms_median": _el_median([r.latency_ms for r in arm_runs]),
            "max_retrieved_lesson_tokens": max(r.retrieved_lesson_tokens for r in arm_runs),
            "cross_project_leaks": sum(r.cross_project_leaks for r in arm_runs),
            "stale_lessons_injected": sum(r.stale_lessons_injected for r in arm_runs),
        }

    b_sum = _summarize(baseline_runs)
    l_sum = _summarize(lesson_runs)
    sr_delta = round(float(l_sum["success_rate"]) - float(b_sum["success_rate"]), 6)
    tok_delta = round(float(l_sum["total_tokens_median"]) - float(b_sum["total_tokens_median"]), 6)
    step_delta = round(float(l_sum["steps_median"]) - float(b_sum["steps_median"]), 6)
    lat_delta = round(float(l_sum["latency_ms_median"]) - float(b_sum["latency_ms_median"]), 6)
    leaks = int(b_sum["cross_project_leaks"]) + int(l_sum["cross_project_leaks"])
    stale_inj = int(l_sum["stale_lessons_injected"])
    within_ceiling = int(l_sum["max_retrieved_lesson_tokens"]) <= int(ceiling_tokens)

    benefit_justified = bool(
        leaks == 0
        and stale_inj == 0
        and within_ceiling
        and (
            sr_delta > 0.0
            or (sr_delta == 0.0 and tok_delta < 0.0 and step_delta <= 0.0)
        )
    )
    payload: dict[str, Any] = {
        "schema_version": EL_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "task_digest": next(iter(task_digests)),
        "config_digest": next(iter(cfg_digests)),
        "hardware_digest": next(iter(hw_digests)),
        "ceiling_tokens": int(ceiling_tokens),
        "baseline": b_sum,
        "lessons": l_sum,
        "lessons_minus_baseline": {
            "success_rate": sr_delta,
            "total_tokens_median": tok_delta,
            "steps_median": step_delta,
            "latency_ms_median": lat_delta,
        },
        "no_cross_project_leakage": (leaks == 0),
        "no_stale_lessons_injected": (stale_inj == 0),
        "within_hard_token_ceiling": within_ceiling,
        "measured_improvement_over_baseline": benefit_justified,
        "runs": [r.to_mapping() for r in runs],
    }
    payload["report_sha256"] = _el_canonical_sha256(payload)
    return payload


class EvidenceLessonStore:
    """Crash-safe durable store at ``$HARNESS_STORE_DIR/lessons/state.json``."""

    def __init__(self, store_dir: Path | str) -> None:
        self._root = Path(store_dir)
        self._dir = self._root / EL_DIRNAME
        self._state_path = self._dir / EL_STATE_NAME
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _ensure_dir_unlocked(self) -> None:
        _el_reject_if_symlink(self._root, "store root")
        if not self._root.exists():
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _el_reject_if_symlink(self._dir, "lessons state directory")
        if not self._dir.exists():
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._dir.is_dir():
            raise LessonStoreIntegrityError(f"Lessons directory `{self._dir}` is not a directory.")
        os.chmod(self._dir, 0o700)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": EL_SCHEMA_VERSION,
            "config": {
                "enabled": True,
                "token_ceiling": DEFAULT_LESSON_TOKEN_CEILING,
                "ab_gate_open": False,
            },
            "lessons": {},
            "ab_reports": {},
        }

    def _read_state_unlocked(self) -> dict[str, Any]:
        _el_reject_if_symlink(self._root, "store root")
        _el_reject_if_symlink(self._dir, "lessons state directory")
        _el_reject_if_symlink(self._state_path, "lessons state file")
        if not self._state_path.exists():
            return self._empty_state()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._state_path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LessonStoreIntegrityError(f"Refusing symlinked `{self._state_path}`.") from exc
            raise LessonStoreIntegrityError(f"Cannot open `{self._state_path}`: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise LessonStoreIntegrityError(f"`{self._state_path}` is not a regular file.")
            raw = os.read(fd, st.st_size + 1)
        finally:
            os.close(fd)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise LessonStoreIntegrityError(f"Corrupt JSON in `{self._state_path}`: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LessonStoreIntegrityError(f"`{self._state_path}` root must be a JSON object.")
        ver = parsed.get("schema_version")
        if not isinstance(ver, int) or isinstance(ver, bool) or ver != EL_SCHEMA_VERSION:
            raise LessonStoreIntegrityError(
                f"Unsupported lessons schema_version {ver!r} (expected {EL_SCHEMA_VERSION})."
            )
        cfg = parsed.get("config")
        lessons = parsed.get("lessons")
        ab_reports = parsed.get("ab_reports")
        if not isinstance(cfg, dict) or not isinstance(lessons, dict) or not isinstance(ab_reports, dict):
            raise LessonStoreIntegrityError(f"Malformed collections in `{self._state_path}`.")
        return {
            "schema_version": EL_SCHEMA_VERSION,
            "config": {
                "enabled": bool(cfg.get("enabled", True)),
                "token_ceiling": min(
                    ABSOLUTE_MAX_LESSON_TOKEN_CEILING,
                    max(24, int(cfg.get("token_ceiling", DEFAULT_LESSON_TOKEN_CEILING))),
                ),
                "ab_gate_open": bool(cfg.get("ab_gate_open", True)),
            },
            "lessons": dict(lessons),
            "ab_reports": dict(ab_reports),
        }

    def _write_state_unlocked(self, state: Mapping[str, Any]) -> None:
        self._ensure_dir_unlocked()
        _el_reject_if_symlink(self._state_path, "lessons state file")
        encoded = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        nonce = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
        tmp_path = self._dir / f"{EL_TEMP_PREFIX}{nonce}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        os.close(fd)
        try:
            os.replace(tmp_path, self._state_path)
            os.chmod(self._state_path, 0o600)
            dir_fd = os.open(self._dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def get_config(self, *, env_enabled: bool = True) -> dict[str, Any]:
        with self._lock:
            state = self._read_state_unlocked()
            cfg = dict(state["config"])
            cfg["env_enabled"] = bool(env_enabled)
            cfg["effective_enabled"] = bool(env_enabled and cfg["enabled"] and cfg["ab_gate_open"])
            return cfg

    def update_config(
        self,
        *,
        enabled: bool | None = None,
        token_ceiling: int | None = None,
        ab_gate_open: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_state_unlocked()
            cfg = dict(state["config"])
            if enabled is not None:
                cfg["enabled"] = bool(enabled)
            if token_ceiling is not None:
                val = int(token_ceiling)
                if val < 24 or val > ABSOLUTE_MAX_LESSON_TOKEN_CEILING:
                    raise ValueError(
                        f"token_ceiling must be in [24, {ABSOLUTE_MAX_LESSON_TOKEN_CEILING}]; got {val}"
                    )
                cfg["token_ceiling"] = val
            if ab_gate_open is not None:
                cfg["ab_gate_open"] = bool(ab_gate_open)
            state["config"] = cfg
            self._write_state_unlocked(state)
            return dict(cfg)

    def promote_lesson(
        self,
        *,
        workspace_root: Path,
        owning_workspace_root: Path | None = None,
        topic_key: str,
        title: str,
        guidance: str,
        anchor_paths: Sequence[str],
        provenance: LessonProvenance,
        tags: Sequence[str] = (),
        failure_codes: Sequence[str] = (),
        known_secret_values: Iterable[str] = (),
        env_enabled: bool = True,
    ) -> LessonRecord:
        """Validate all provenance, privacy, anchor, and token-budget gates, then promote."""
        if not env_enabled:
            raise LessonPromotionRejectedError(
                "lessons_feature_disabled",
                "Evidence-backed lessons are disabled by STUDIO_ENABLE_EVIDENCE_LESSONS=0.",
            )
        with self._lock:
            state = self._read_state_unlocked()
            if not state["config"]["enabled"]:
                raise LessonPromotionRejectedError(
                    "lessons_feature_disabled",
                    "Evidence-backed lessons are disabled in runtime configuration.",
                )

            if not provenance.verified:
                raise LessonPromotionRejectedError(
                    "unverified_task_cannot_promote",
                    "Candidate lesson rejected: originating task completion was not verified.",
                )
            if (
                not provenance.verification_spec_sha256
                or not _EL_HEX64_RE.match(provenance.verification_spec_sha256)
                or provenance.passed_checks < 1
            ):
                raise LessonPromotionRejectedError(
                    "missing_verification_evidence",
                    "Candidate lesson rejected: valid 64-hex verification_spec_sha256 and passed_checks >= 1 required.",
                )
            if provenance.source_trust not in ALLOWED_LESSON_TRUST_LEVELS:
                raise LessonPromotionRejectedError(
                    "untrusted_provenance",
                    f"Candidate lesson trust {provenance.source_trust!r} is not eligible for promotion.",
                )
            if provenance.confidentiality not in ALLOWED_LESSON_CONFIDENTIALITY:
                raise LessonPromotionRejectedError(
                    "secret_confidentiality_forbidden",
                    f"Candidate lesson confidentiality {provenance.confidentiality!r} cannot be promoted.",
                )

            clean_topic = re.sub(r"[^a-z0-9_.:-]+", "-", str(topic_key or "").strip().lower()).strip("-")
            clean_title = " ".join(str(title or "").split()).strip()
            clean_guidance = " ".join(str(guidance or "").split()).strip()
            if not clean_topic or not clean_title or not clean_guidance:
                raise LessonPromotionRejectedError(
                    "empty_lesson_fields",
                    "topic_key, title, and guidance must all be non-empty.",
                )

            canon_root = resolve_canonical_project_root(
                workspace_root, owning_workspace_root=owning_workspace_root
            )
            _el_check_privacy_hygiene(
                f"{clean_title}\n{clean_guidance}",
                canonical_project_root=canon_root,
                known_secret_values=known_secret_values,
            )

            if not anchor_paths:
                raise LessonPromotionRejectedError(
                    "missing_file_anchors",
                    "At least one confined workspace file anchor is required so stale lessons can be invalidated.",
                )
            anchored_digests: dict[str, str] = {}
            ws_resolved = workspace_root.resolve()
            for raw_rel in anchor_paths:
                norm_rel = _el_normalize_rel_path(ws_resolved, raw_rel)
                digest = compute_confined_file_sha256(ws_resolved, norm_rel)
                if digest is None:
                    raise LessonPromotionRejectedError(
                        "anchor_file_unreadable",
                        f"Anchor file `{norm_rel}` does not exist as a regular non-symlink file in workspace.",
                    )
                anchored_digests[norm_rel] = digest

            project_scope_id = derive_project_scope_id(
                ws_resolved, owning_workspace_root=canon_root
            )
            now_ts = _el_now()
            probe_rec = LessonRecord(
                lesson_id="les-probe",
                project_scope_id=project_scope_id,
                topic_key=clean_topic[:64],
                title=clean_title[:120],
                guidance=clean_guidance,
                tags=tuple(dict.fromkeys(str(t).strip().lower() for t in tags if str(t).strip()))[:12],
                failure_codes=tuple(
                    dict.fromkeys(str(c).strip().lower() for c in failure_codes if str(c).strip())
                )[:8],
                anchored_files=anchored_digests,
                provenance=provenance,
                status="promoted",
                estimated_tokens=0,
                created_at=now_ts,
                updated_at=now_ts,
            )
            est_tokens = estimate_lesson_tokens(probe_rec.render_bullet())
            if est_tokens > MAX_SINGLE_LESSON_TOKENS:
                raise LessonPromotionRejectedError(
                    "lesson_exceeds_token_limit",
                    f"Rendered lesson is {est_tokens} tokens (max {MAX_SINGLE_LESSON_TOKENS}).",
                )

            # Supersede any prior lesson for the same (project_scope_id, topic_key).
            existing_id: str | None = None
            created_ts = now_ts
            for lid, raw_item in state["lessons"].items():
                prev = LessonRecord.from_mapping(raw_item)
                if prev.project_scope_id == project_scope_id and prev.topic_key == probe_rec.topic_key:
                    existing_id = lid
                    created_ts = prev.created_at
                    break

            final_id = existing_id or _el_new_id("les")
            record = LessonRecord(
                lesson_id=final_id,
                project_scope_id=project_scope_id,
                topic_key=probe_rec.topic_key,
                title=probe_rec.title,
                guidance=probe_rec.guidance,
                tags=probe_rec.tags,
                failure_codes=probe_rec.failure_codes,
                anchored_files=probe_rec.anchored_files,
                provenance=provenance,
                status="promoted",
                estimated_tokens=est_tokens,
                created_at=created_ts,
                updated_at=now_ts,
            )
            state["lessons"][final_id] = record.to_mapping()
            self._write_state_unlocked(state)
            return record

    def list_lessons(
        self,
        *,
        workspace_root: Path,
        owning_workspace_root: Path | None = None,
        include_stale: bool = False,
    ) -> list[LessonRecord]:
        """List lessons strictly within ``workspace_root``'s canonical project scope."""
        scope_id = derive_project_scope_id(
            workspace_root, owning_workspace_root=owning_workspace_root
        )
        with self._lock:
            state = self._read_state_unlocked()
            out: list[LessonRecord] = []
            for raw_item in state["lessons"].values():
                rec = LessonRecord.from_mapping(raw_item)
                if rec.project_scope_id != scope_id:
                    continue
                if not include_stale and rec.status != "promoted":
                    continue
                out.append(rec)
            out.sort(key=lambda r: (r.updated_at, r.lesson_id), reverse=True)
            return out

    def invalidate_lesson(
        self,
        lesson_id: str,
        *,
        reason: str,
        revoked: bool = False,
    ) -> LessonRecord | None:
        with self._lock:
            state = self._read_state_unlocked()
            raw = state["lessons"].get(lesson_id)
            if raw is None:
                return None
            prev = LessonRecord.from_mapping(raw)
            updated = LessonRecord(
                lesson_id=prev.lesson_id,
                project_scope_id=prev.project_scope_id,
                topic_key=prev.topic_key,
                title=prev.title,
                guidance=prev.guidance,
                tags=prev.tags,
                failure_codes=prev.failure_codes,
                anchored_files=prev.anchored_files,
                provenance=prev.provenance,
                status="revoked" if revoked else "stale",
                estimated_tokens=prev.estimated_tokens,
                created_at=prev.created_at,
                updated_at=_el_now(),
                stale_reason=str(reason or "manual_invalidation")[:200],
                retrieval_count=prev.retrieval_count,
                verified_hit_count=prev.verified_hit_count,
                consecutive_failures=prev.consecutive_failures,
            )
            state["lessons"][lesson_id] = updated.to_mapping()
            self._write_state_unlocked(state)
            return updated

    def revalidate_project_lessons(
        self,
        *,
        workspace_root: Path,
        owning_workspace_root: Path | None = None,
        current_security_generation: int | None = None,
        is_lineage_evidence_valid_fn: Callable[[str], bool] | None = None,
    ) -> tuple[str, ...]:
        """Invalidate any promoted project lesson whose anchored files, generation, or lineage drifted."""
        ws_resolved = workspace_root.resolve()
        scope_id = derive_project_scope_id(
            ws_resolved, owning_workspace_root=owning_workspace_root
        )
        invalidated: list[str] = []
        with self._lock:
            state = self._read_state_unlocked()
            mutated = False
            now_ts = _el_now()
            for lid, raw_item in list(state["lessons"].items()):
                rec = LessonRecord.from_mapping(raw_item)
                if rec.project_scope_id != scope_id or rec.status != "promoted":
                    continue
                stale_reason = ""
                if (
                    current_security_generation is not None
                    and rec.provenance.security_generation != int(current_security_generation)
                ):
                    stale_reason = (
                        f"security_generation_changed:{rec.provenance.security_generation}"
                        f"->{int(current_security_generation)}"
                    )
                elif (
                    rec.provenance.lineage_evidence_id
                    and is_lineage_evidence_valid_fn is not None
                    and not is_lineage_evidence_valid_fn(rec.provenance.lineage_evidence_id)
                ):
                    stale_reason = f"lineage_evidence_stale:{rec.provenance.lineage_evidence_id}"
                else:
                    for rel_path, expected_sha in rec.anchored_files.items():
                        actual_sha = compute_confined_file_sha256(ws_resolved, rel_path)
                        if actual_sha != expected_sha:
                            stale_reason = f"anchored_file_changed:{rel_path}"
                            break
                if stale_reason:
                    updated = LessonRecord(
                        lesson_id=rec.lesson_id,
                        project_scope_id=rec.project_scope_id,
                        topic_key=rec.topic_key,
                        title=rec.title,
                        guidance=rec.guidance,
                        tags=rec.tags,
                        failure_codes=rec.failure_codes,
                        anchored_files=rec.anchored_files,
                        provenance=rec.provenance,
                        status="stale",
                        estimated_tokens=rec.estimated_tokens,
                        created_at=rec.created_at,
                        updated_at=now_ts,
                        stale_reason=stale_reason,
                        retrieval_count=rec.retrieval_count,
                        verified_hit_count=rec.verified_hit_count,
                        consecutive_failures=rec.consecutive_failures,
                    )
                    state["lessons"][lid] = updated.to_mapping()
                    invalidated.append(lid)
                    mutated = True
            if mutated:
                self._write_state_unlocked(state)
        return tuple(invalidated)

    def record_retrieval_outcome(
        self,
        lesson_ids: Sequence[str],
        *,
        verified: bool,
    ) -> tuple[str, ...]:
        """Record downstream task verification outcomes and demote lessons that regress repeatedly."""
        if not lesson_ids:
            return ()
        demoted: list[str] = []
        with self._lock:
            state = self._read_state_unlocked()
            mutated = False
            now_ts = _el_now()
            for lid in dict.fromkeys(lesson_ids):
                raw = state["lessons"].get(lid)
                if raw is None:
                    continue
                rec = LessonRecord.from_mapping(raw)
                if rec.status != "promoted":
                    continue
                if verified:
                    new_hits = rec.verified_hit_count + 1
                    new_fails = 0
                    new_status: LessonStatus = "promoted"
                    new_reason = ""
                else:
                    new_hits = rec.verified_hit_count
                    new_fails = rec.consecutive_failures + 1
                    if new_fails >= MAX_CONSECUTIVE_VERIFICATION_FAILURES:
                        new_status = "stale"
                        new_reason = f"verification_regression:{new_fails}_consecutive_failures"
                        demoted.append(lid)
                    else:
                        new_status = "promoted"
                        new_reason = rec.stale_reason
                updated = LessonRecord(
                    lesson_id=rec.lesson_id,
                    project_scope_id=rec.project_scope_id,
                    topic_key=rec.topic_key,
                    title=rec.title,
                    guidance=rec.guidance,
                    tags=rec.tags,
                    failure_codes=rec.failure_codes,
                    anchored_files=rec.anchored_files,
                    provenance=rec.provenance,
                    status=new_status,
                    estimated_tokens=rec.estimated_tokens,
                    created_at=rec.created_at,
                    updated_at=now_ts,
                    stale_reason=new_reason,
                    retrieval_count=rec.retrieval_count,
                    verified_hit_count=new_hits,
                    consecutive_failures=new_fails,
                )
                state["lessons"][lid] = updated.to_mapping()
                mutated = True
            if mutated:
                self._write_state_unlocked(state)
        return tuple(demoted)

    def retrieve_for_decision_boundary(
        self,
        *,
        boundary: str,
        workspace_root: Path,
        owning_workspace_root: Path | None = None,
        query: str,
        failure_codes: Collection[str] = (),
        target_paths: Collection[str] = (),
        token_ceiling: int | None = None,
        max_lessons: int = MAX_RETRIEVED_LESSONS,
        current_security_generation: int | None = None,
        is_lineage_evidence_valid_fn: Callable[[str], bool] | None = None,
        env_enabled: bool = True,
        session_enabled: bool = True,
    ) -> LessonRetrievalResult:
        """Retrieve project-scoped, non-stale lessons strictly at decision boundaries within hard token ceiling."""
        ws_resolved = workspace_root.resolve()
        scope_id = derive_project_scope_id(
            ws_resolved, owning_workspace_root=owning_workspace_root
        )
        if not env_enabled or not session_enabled:
            return LessonRetrievalResult(
                enabled=False,
                boundary=boundary,
                project_scope_id=scope_id,
                lessons=(),
                rendered_prompt="",
                used_tokens=0,
                ceiling_tokens=0,
                skipped_reason="feature_disabled",
            )

        with self._lock:
            state = self._read_state_unlocked()
            cfg = state["config"]
            eff_ceiling = min(
                ABSOLUTE_MAX_LESSON_TOKEN_CEILING,
                max(
                    24,
                    int(token_ceiling)
                    if token_ceiling is not None
                    else int(cfg.get("token_ceiling", DEFAULT_LESSON_TOKEN_CEILING)),
                ),
            )
            if not cfg.get("enabled", True):
                return LessonRetrievalResult(
                    enabled=False,
                    boundary=boundary,
                    project_scope_id=scope_id,
                    lessons=(),
                    rendered_prompt="",
                    used_tokens=0,
                    ceiling_tokens=eff_ceiling,
                    skipped_reason="store_config_disabled",
                )
            if not cfg.get("ab_gate_open", True):
                return LessonRetrievalResult(
                    enabled=False,
                    boundary=boundary,
                    project_scope_id=scope_id,
                    lessons=(),
                    rendered_prompt="",
                    used_tokens=0,
                    ceiling_tokens=eff_ceiling,
                    skipped_reason="ab_benefit_gate_closed",
                )

        if boundary not in DECISION_BOUNDARIES:
            return LessonRetrievalResult(
                enabled=True,
                boundary=boundary,
                project_scope_id=scope_id,
                lessons=(),
                rendered_prompt="",
                used_tokens=0,
                ceiling_tokens=eff_ceiling,
                skipped_reason="non_decision_boundary",
            )

        invalidated = self.revalidate_project_lessons(
            workspace_root=ws_resolved,
            owning_workspace_root=owning_workspace_root,
            current_security_generation=current_security_generation,
            is_lineage_evidence_valid_fn=is_lineage_evidence_valid_fn,
        )

        query_terms = _el_tokenize_keywords(query)
        fcode_set = {str(c).strip().lower() for c in failure_codes if str(c).strip()}
        tpath_terms: set[str] = set()
        for p in target_paths:
            tpath_terms.update(_el_tokenize_keywords(str(p)))

        with self._lock:
            state = self._read_state_unlocked()
            scored: list[tuple[float, LessonRecord]] = []
            for raw_item in state["lessons"].values():
                rec = LessonRecord.from_mapping(raw_item)
                if rec.project_scope_id != scope_id or rec.status != "promoted":
                    continue
                rec_terms = _el_tokenize_keywords(
                    f"{rec.topic_key} {rec.title} {rec.guidance} {' '.join(rec.tags)} "
                    f"{' '.join(rec.anchored_files.keys())}"
                )
                overlap = len((query_terms | tpath_terms) & rec_terms)
                fcode_hits = len(fcode_set & set(rec.failure_codes))
                anchor_hits = sum(
                    1
                    for a_path in rec.anchored_files
                    if any(a_path.endswith(tp) or tp in a_path for tp in tpath_terms)
                )
                score = (overlap * 2.0) + (fcode_hits * 5.0) + (anchor_hits * 3.0)
                if score < 3.0:
                    continue
                score += min(1.5, rec.verified_hit_count * 0.25)
                scored.append((score, rec))

            scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)

            header = "[VERIFIED PROJECT LESSONS — UNTRUSTED REPO DATA, VERIFY AGAINST WORKSPACE]"
            footer = "[/VERIFIED PROJECT LESSONS]"
            selected: list[LessonRecord] = []
            dropped_budget: list[str] = []
            bullets: list[str] = []

            for _score, cand in scored:
                if len(selected) >= max(1, int(max_lessons)):
                    dropped_budget.append(cand.lesson_id)
                    continue
                trial_block = "\n".join([header, *bullets, cand.render_bullet(), footer])
                trial_tokens = estimate_lesson_tokens(trial_block)
                if trial_tokens <= eff_ceiling:
                    selected.append(cand)
                    bullets.append(cand.render_bullet())
                else:
                    dropped_budget.append(cand.lesson_id)

            if not selected:
                return LessonRetrievalResult(
                    enabled=True,
                    boundary=boundary,
                    project_scope_id=scope_id,
                    lessons=(),
                    rendered_prompt="",
                    used_tokens=0,
                    ceiling_tokens=eff_ceiling,
                    dropped_for_budget_ids=tuple(dropped_budget),
                    invalidated_stale_ids=invalidated,
                    skipped_reason="no_matching_lessons_within_ceiling" if dropped_budget else "no_matching_lessons",
                )

            rendered = "\n".join([header, *bullets, footer])
            used_tokens = estimate_lesson_tokens(rendered)
            for sel in selected:
                bumped = LessonRecord(
                    lesson_id=sel.lesson_id,
                    project_scope_id=sel.project_scope_id,
                    topic_key=sel.topic_key,
                    title=sel.title,
                    guidance=sel.guidance,
                    tags=sel.tags,
                    failure_codes=sel.failure_codes,
                    anchored_files=sel.anchored_files,
                    provenance=sel.provenance,
                    status=sel.status,
                    estimated_tokens=sel.estimated_tokens,
                    created_at=sel.created_at,
                    updated_at=sel.updated_at,
                    stale_reason=sel.stale_reason,
                    retrieval_count=sel.retrieval_count + 1,
                    verified_hit_count=sel.verified_hit_count,
                    consecutive_failures=sel.consecutive_failures,
                )
                state["lessons"][sel.lesson_id] = bumped.to_mapping()
            self._write_state_unlocked(state)

            return LessonRetrievalResult(
                enabled=True,
                boundary=boundary,
                project_scope_id=scope_id,
                lessons=tuple(selected),
                rendered_prompt=rendered,
                used_tokens=used_tokens,
                ceiling_tokens=eff_ceiling,
                dropped_for_budget_ids=tuple(dropped_budget),
                invalidated_stale_ids=invalidated,
            )

    def record_ab_report(
        self,
        report: Mapping[str, Any],
        *,
        enforce_gate: bool = True,
    ) -> dict[str, Any]:
        """Persist an A/B coding evaluation report and gate automatic injection on measured benefit."""
        scen = str(report.get("scenario_id") or "").strip()
        if not scen:
            raise ValueError("A/B report requires scenario_id.")
        with self._lock:
            state = self._read_state_unlocked()
            state["ab_reports"][scen] = dict(report)
            if enforce_gate:
                all_justified = all(
                    bool(r.get("measured_improvement_over_baseline"))
                    for r in state["ab_reports"].values()
                )
                state["config"]["ab_gate_open"] = all_justified
            self._write_state_unlocked(state)
            return dict(state["config"])

    def purge_project_lessons(
        self,
        *,
        workspace_root: Path,
        owning_workspace_root: Path | None = None,
    ) -> int:
        """Delete all lessons belonging to a single canonical project scope."""
        scope_id = derive_project_scope_id(
            workspace_root, owning_workspace_root=owning_workspace_root
        )
        with self._lock:
            state = self._read_state_unlocked()
            to_delete = [
                lid
                for lid, raw_item in state["lessons"].items()
                if LessonRecord.from_mapping(raw_item).project_scope_id == scope_id
            ]
            for lid in to_delete:
                state["lessons"].pop(lid, None)
            if to_delete:
                self._write_state_unlocked(state)
            return len(to_delete)


__all__ = [
    "ABSOLUTE_MAX_LESSON_TOKEN_CEILING",
    "ALLOWED_LESSON_CONFIDENTIALITY",
    "ALLOWED_LESSON_TRUST_LEVELS",
    "DECISION_BOUNDARIES",
    "DEFAULT_LESSON_TOKEN_CEILING",
    "EL_DIRNAME",
    "EL_SCHEMA_VERSION",
    "EL_STATE_NAME",
    "EvidenceLessonError",
    "EvidenceLessonStore",
    "LESSON_STATUSES",
    "LessonEvalRun",
    "LessonPromotionRejectedError",
    "LessonProvenance",
    "LessonRecord",
    "LessonRetrievalResult",
    "LessonStatus",
    "LessonStoreIntegrityError",
    "MAX_CONSECUTIVE_VERIFICATION_FAILURES",
    "MAX_RETRIEVED_LESSONS",
    "MAX_SINGLE_LESSON_TOKENS",
    "compare_lesson_ab_runs",
    "compute_confined_file_sha256",
    "derive_project_scope_id",
    "estimate_lesson_tokens",
    "resolve_canonical_project_root",
]
