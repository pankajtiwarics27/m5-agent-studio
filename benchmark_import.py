"""Import community benchmark contributions without trusting them (AS-063).

A contribution is an *envelope* around an unmodified AS-060 publication bundle:

    {
      "schema_version": 1,
      "license": {"id": "CC-BY-4.0", "url": "https://...", "attribution": "Name"},
      "contributor": {"name": "Name", "url": "https://..."},
      "hardware": { ...HardwareProfile-style snapshot of the claimed machine... },
      "bundle": { ...publication bundle as written by scripts/publish_benchmark_results.py... }
    }

Import validates four things and then labels every imported profile
``untrusted``:

- **schema** — envelope and bundle versions, allowed keys only, size limits;
- **license** — an open data license from an explicit allowlist, with attribution
  when the license requires it;
- **provenance** — the bundle digest, and for every profile the raw JSONL digest,
  a full re-load through ``model_calibration.load_profile``, and recomputation of
  the profile, inputs, and key digests;
- **hardware identity** — the claimed hardware snapshot must be valid, must hash
  to the bundle's ``hardware_digest``, must agree on ``os_id``, and every profile
  must be pinned to that same machine.

Nothing here executes contributed code: raw JSONL is parsed as data, staged in a
temporary directory, and re-verified with the same loader the publisher uses.

Promotion to ``locally-verified`` is a separate, explicit act. It requires a
profile with the same ``profile_digest`` already in the local AS-060 profile
vault under the same key — in other words, we measured the same thing on the same
machine ourselves. There is no bulk "trust everything" switch, and a contributor
cannot promote their own data by asserting that it is correct.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark_publication import (
    _MAX_BUNDLE_BYTES,
    _MAX_PROFILES,
    BenchmarkPublicationError,
    _validate_hardware,
    verify_bundle_mapping,
)
from model_calibration import ModelPerformanceProfile, load_profile
from performance_profiles import (
    BenchmarkMethodology,
    ProfileKey,
    ProfileStoreError,
    ProfileVault,
    os_id_from_hardware,
)

IMPORT_SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
REGISTRY_FILENAME = "imports.jsonl"
TRUST_UNTRUSTED = "untrusted"
TRUST_LOCAL = "locally-verified"

_MAX_ENVELOPE_BYTES = _MAX_BUNDLE_BYTES + 1024 * 1024
_MAX_TEXT = 200

# Permissive licenses that allow redistributing measurement data. A license
# outside this set is refused rather than interpreted: "probably fine" is not a
# legal position this project can take on a contributor's behalf.
ALLOWED_LICENSES: dict[str, bool] = {
    # id -> attribution required
    "CC0-1.0": False,
    "CC-BY-4.0": True,
    "CC-BY-SA-4.0": True,
    "MIT": True,
    "Apache-2.0": True,
    "ODbL-1.0": True,
    "PDDL-1.0": False,
}
_ENVELOPE_FIELDS = frozenset({"schema_version", "license", "contributor", "hardware", "bundle"})
_LICENSE_FIELDS = frozenset({"id", "url", "attribution"})
_CONTRIBUTOR_FIELDS = frozenset({"name", "url"})
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_URL_RE = re.compile(r"\Ahttps?://[^\s]{1,290}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class BenchmarkImportError(ValueError):
    """A contribution failed validation; nothing was written."""


class ImportTrustError(BenchmarkImportError):
    """A trust transition was requested without the evidence it requires."""


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(value: object, field_name: str, *, max_length: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise BenchmarkImportError(f"{field_name} must be a string.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise BenchmarkImportError(f"{field_name} must not be empty.")
    if len(cleaned) > max_length:
        raise BenchmarkImportError(f"{field_name} must be at most {max_length} characters.")
    if _CONTROL_RE.search(cleaned):
        raise BenchmarkImportError(f"{field_name} must not contain control characters.")
    return cleaned


def _url(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _URL_RE.match(value):
        raise BenchmarkImportError(f"{field_name} must be an http(s) URL.")
    return value


def _digest_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise BenchmarkImportError(f"{field_name} must be a sha256: digest.")
    return value


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise BenchmarkImportError(f"{what} contains unsupported fields: {', '.join(sorted(unknown))}.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Envelope parts
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class License:
    """The declared data license, restricted to an explicit allowlist."""

    id: str
    attribution: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        if self.id not in ALLOWED_LICENSES:
            raise BenchmarkImportError(
                f"license {self.id!r} is not accepted for imported measurements; accepted licenses are: "
                f"{', '.join(sorted(ALLOWED_LICENSES))}."
            )
        attribution = " ".join(self.attribution.split())
        if ALLOWED_LICENSES[self.id] and not attribution:
            raise BenchmarkImportError(f"license {self.id} requires an attribution line.")
        if len(attribution) > _MAX_TEXT:
            raise BenchmarkImportError(f"license.attribution must be at most {_MAX_TEXT} characters.")
        object.__setattr__(self, "attribution", attribution)
        if self.url:
            object.__setattr__(self, "url", _url(self.url, "license.url"))

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "attribution": self.attribution}
        if self.url:
            payload["url"] = self.url
        return payload

    @classmethod
    def from_mapping(cls, value: object) -> License:
        if not isinstance(value, Mapping):
            raise BenchmarkImportError("license must be an object.")
        _unknown_fields(value, _LICENSE_FIELDS, "license")
        license_id = value.get("id")
        if not isinstance(license_id, str) or not license_id.strip():
            raise BenchmarkImportError("license.id is required.")
        attribution = value.get("attribution", "")
        url = value.get("url", "")
        if not isinstance(attribution, str) or not isinstance(url, str):
            raise BenchmarkImportError("license.attribution and license.url must be strings.")
        return cls(id=license_id.strip(), attribution=attribution, url=url)

    @property
    def digest(self) -> str:
        return _digest(self.to_mapping())


@dataclass(frozen=True, slots=True)
class Contributor:
    """Who submitted the measurements: a name and an optional project URL, no contact data."""

    name: str
    url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "contributor.name"))
        if self.url:
            object.__setattr__(self, "url", _url(self.url, "contributor.url"))

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name}
        if self.url:
            payload["url"] = self.url
        return payload

    @classmethod
    def from_mapping(cls, value: object) -> Contributor:
        if not isinstance(value, Mapping):
            raise BenchmarkImportError("contributor must be an object.")
        _unknown_fields(value, _CONTRIBUTOR_FIELDS, "contributor")
        url = value.get("url", "")
        if not isinstance(url, str):
            raise BenchmarkImportError("contributor.url must be a string.")
        return cls(name=value.get("name", ""), url=url)


@dataclass(frozen=True, slots=True)
class ImportedProfile:
    """One profile taken from a contribution, with the trust label it carries."""

    model_id: str
    model_digest: str
    profile_digest: str
    inputs_digest: str
    profile_key: ProfileKey
    trust: str = TRUST_UNTRUSTED

    @property
    def profile_key_digest(self) -> str:
        return self.profile_key.digest

    @property
    def hardware_digest(self) -> str:
        return self.profile_key.hardware_digest

    @property
    def os_id(self) -> str:
        return self.profile_key.os_id

    def to_mapping(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "profile_digest": self.profile_digest,
            "inputs_digest": self.inputs_digest,
            "profile_key": self.profile_key.to_mapping(),
            "profile_key_digest": self.profile_key_digest,
            "trust": self.trust,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ImportedProfile:
        try:
            key = ProfileKey.from_mapping(value["profile_key"])
        except KeyError as exc:
            raise BenchmarkImportError(f"registry profile is missing {exc.args[0]!r}.") from exc
        except (TypeError, ValueError) as exc:
            raise BenchmarkImportError(f"registry profile key is invalid ({type(exc).__name__}).") from exc
        if value.get("profile_key_digest") not in (None, key.digest):
            raise BenchmarkImportError("registry profile key does not hash to its recorded digest.")
        return cls(
            model_id=str(value.get("model_id", "")),
            model_digest=str(value.get("model_digest", "")),
            profile_digest=str(value.get("profile_digest", "")),
            inputs_digest=str(value.get("inputs_digest", "")),
            profile_key=key,
            trust=str(value.get("trust", TRUST_UNTRUSTED)),
        )


@dataclass(frozen=True, slots=True)
class Contribution:
    """A fully verified envelope. It is only ever constructed after validation."""

    envelope_digest: str
    bundle_digest: str
    license: License
    contributor: Contributor
    hardware_digest: str
    os_id: str
    methodology_digest: str
    profiles: tuple[ImportedProfile, ...] = field(default_factory=tuple)

    @property
    def trust(self) -> str:
        return TRUST_UNTRUSTED

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": IMPORT_SCHEMA_VERSION,
            "envelope_digest": self.envelope_digest,
            "bundle_digest": self.bundle_digest,
            "license": self.license.to_mapping(),
            "contributor": self.contributor.to_mapping(),
            "hardware_digest": self.hardware_digest,
            "os_id": self.os_id,
            "methodology_digest": self.methodology_digest,
            "trust": self.trust,
            "profiles": [profile.to_mapping() for profile in self.profiles],
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _read_envelope(path: Path) -> Mapping[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BenchmarkImportError(f"contribution is unavailable ({type(exc).__name__}).") from exc
    if not raw or len(raw) > _MAX_ENVELOPE_BYTES:
        raise BenchmarkImportError("contribution is empty or exceeds the import size limit.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkImportError(f"contribution is not UTF-8 JSON ({type(exc).__name__}).") from exc
    if not isinstance(value, Mapping):
        raise BenchmarkImportError("contribution must be a JSON object.")
    return value


def _verify_raw_profile(entry: Mapping[str, Any], index: int) -> ModelPerformanceProfile:
    """Re-load one embedded profile from its raw JSONL and check the declared raw digest."""
    raw_text = entry.get("raw_profile_jsonl")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise BenchmarkImportError(f"profiles[{index}].raw_profile_jsonl must be a non-empty string.")
    if not raw_text.endswith("\n"):
        raise BenchmarkImportError(f"profiles[{index}].raw_profile_jsonl must end with a newline.")
    raw = raw_text.encode("utf-8")
    claimed = _digest_value(entry.get("raw_sha256"), f"profiles[{index}].raw_sha256")
    if claimed != f"sha256:{hashlib.sha256(raw).hexdigest()}":
        raise BenchmarkImportError(f"profiles[{index}] raw JSONL does not match its raw_sha256.")
    try:
        with tempfile.TemporaryDirectory(prefix="m5-benchmark-import-") as temp_dir:
            staged = Path(temp_dir) / "profile.jsonl"
            staged.write_bytes(raw)
            profile = load_profile(staged)
    except Exception as exc:
        raise BenchmarkImportError(
            f"profiles[{index}] failed integrity verification ({type(exc).__name__})."
        ) from exc
    if not isinstance(profile, ModelPerformanceProfile):
        raise BenchmarkImportError(f"profiles[{index}] did not produce a calibration profile.")
    return profile


def _methodology(config: Mapping[str, Any]) -> BenchmarkMethodology:
    methodology = config.get("methodology")
    if not isinstance(methodology, Mapping):
        raise BenchmarkImportError("bundle config must include a methodology object.")
    try:
        return BenchmarkMethodology.from_mapping(methodology)
    except (TypeError, ValueError) as exc:
        raise BenchmarkImportError(f"bundle methodology is invalid ({type(exc).__name__}).") from exc


def validate_envelope(value: Mapping[str, Any]) -> Contribution:
    """Verify schema, license, provenance, and hardware identity. Never trusts."""
    if not isinstance(value, Mapping):
        raise BenchmarkImportError("contribution must be an object.")
    if value.get("schema_version") != IMPORT_SCHEMA_VERSION:
        raise BenchmarkImportError("unsupported contribution schema_version.")
    _unknown_fields(value, _ENVELOPE_FIELDS, "contribution")

    license_entry = License.from_mapping(value.get("license"))
    contributor = Contributor.from_mapping(value.get("contributor"))

    hardware = value.get("hardware")
    if not isinstance(hardware, Mapping):
        raise BenchmarkImportError("hardware must be an object describing the claimed machine.")
    try:
        _validate_hardware(hardware)
    except BenchmarkPublicationError as exc:
        raise BenchmarkImportError(f"hardware snapshot is not acceptable for import: {exc}") from exc
    hardware_digest = _digest(dict(hardware))
    try:
        hardware_os_id = os_id_from_hardware(hardware)
    except Exception as exc:
        raise BenchmarkImportError(
            f"hardware snapshot has no usable operating_system/architecture ({type(exc).__name__})."
        ) from exc

    bundle = value.get("bundle")
    if not isinstance(bundle, Mapping):
        raise BenchmarkImportError("bundle must be an object.")
    try:
        bundle_digest = verify_bundle_mapping(bundle)
    except BenchmarkPublicationError as exc:
        raise BenchmarkImportError(f"bundle provenance is invalid: {exc}") from exc

    context = bundle.get("context")
    config = bundle.get("config")
    entries = bundle.get("profiles")
    if not isinstance(context, Mapping) or not isinstance(config, Mapping):
        raise BenchmarkImportError("bundle must include context and config objects.")
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence) or not entries:
        raise BenchmarkImportError("bundle must include a non-empty profiles list.")
    if len(entries) > _MAX_PROFILES:
        raise BenchmarkImportError(f"bundle carries more than {_MAX_PROFILES} profiles.")

    if bundle.get("config_digest") != _digest(dict(config)):
        raise BenchmarkImportError("bundle config does not hash to its config_digest.")

    claimed_hardware = _digest_value(context.get("hardware_digest"), "bundle.context.hardware_digest")
    claimed_os = context.get("os_id")
    if not isinstance(claimed_os, str) or not claimed_os.strip():
        raise BenchmarkImportError("bundle.context.os_id is required.")
    if claimed_hardware != hardware_digest:
        raise BenchmarkImportError(
            "the claimed hardware does not hash to the hardware_digest the bundle was published under."
        )
    if claimed_os != hardware_os_id:
        raise BenchmarkImportError(
            f"bundle.context.os_id {claimed_os!r} disagrees with the claimed hardware ({hardware_os_id!r})."
        )
    methodology_digest = _digest_value(
        context.get("methodology_digest"), "bundle.context.methodology_digest"
    )
    if claimed_os != hardware_os_id.strip():  # pragma: no cover - defensive duplicate check
        raise BenchmarkImportError("bundle.context.os_id disagrees with the claimed hardware.")

    methodology = _methodology(config)
    if methodology.digest != methodology_digest:
        raise BenchmarkImportError("bundle methodology does not hash to bundle.context.methodology_digest.")

    plan = config.get("calibration_plan")
    if not isinstance(plan, Mapping):
        raise BenchmarkImportError("bundle config must include a calibration_plan object.")
    warm_repeats = plan.get("warm_repeats")
    cold_repeats = plan.get("cold_repeats")
    if isinstance(warm_repeats, bool) or not isinstance(warm_repeats, int) or warm_repeats < 1:
        raise BenchmarkImportError("bundle calibration_plan.warm_repeats must be a positive integer.")
    if isinstance(cold_repeats, bool) or not isinstance(cold_repeats, int) or cold_repeats < 0:
        raise BenchmarkImportError("bundle calibration_plan.cold_repeats must be a non-negative integer.")

    imported: list[ImportedProfile] = []
    seen_identities: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise BenchmarkImportError(f"profiles[{index}] must be an object.")
        profile = _verify_raw_profile(entry, index)
        if _digest_value(entry.get("profile_digest"), f"profiles[{index}].profile_digest") != profile.digest:
            raise BenchmarkImportError(f"profiles[{index}] does not hash to its profile_digest.")
        if _digest_value(entry.get("inputs_digest"), f"profiles[{index}].inputs_digest") != profile.inputs.digest:
            raise BenchmarkImportError(f"profiles[{index}] inputs do not hash to their inputs_digest.")
        if entry.get("model_id") != profile.inputs.model_id:
            raise BenchmarkImportError(f"profiles[{index}].model_id disagrees with the raw profile.")
        if entry.get("model_digest") != profile.inputs.model_digest:
            raise BenchmarkImportError(f"profiles[{index}].model_digest disagrees with the raw profile.")
        identity = (profile.inputs.model_id, profile.inputs.model_digest)
        if identity in seen_identities:
            raise BenchmarkImportError(f"profiles[{index}] repeats a model identity already imported.")
        seen_identities.add(identity)
        if profile.inputs.hardware is None:
            raise BenchmarkImportError(f"profiles[{index}] carries no hardware snapshot.")
        try:
            key = ProfileKey.derive(profile, methodology=methodology)
        except Exception as exc:
            raise BenchmarkImportError(f"profiles[{index}] has no usable profile key ({type(exc).__name__}).") from exc
        declared_key = entry.get("profile_key")
        if not isinstance(declared_key, Mapping):
            raise BenchmarkImportError(f"profiles[{index}].profile_key must be an object.")
        try:
            stored_key = ProfileKey.from_mapping(declared_key)
        except (TypeError, ValueError) as exc:
            raise BenchmarkImportError(f"profiles[{index}].profile_key is invalid ({type(exc).__name__}).") from exc
        if stored_key.to_mapping() != key.to_mapping():
            raise BenchmarkImportError(
                f"profiles[{index}].profile_key disagrees with the key derived from the raw profile."
            )
        if key.hardware_digest != hardware_digest or key.os_id != hardware_os_id:
            raise BenchmarkImportError(f"profiles[{index}] was measured on a different machine than claimed.")
        if key.methodology_digest != methodology_digest:
            raise BenchmarkImportError(f"profiles[{index}] used a different methodology than the bundle declares.")
        warm_count = sum(sample.kind == "warm" for sample in profile.samples)
        cold_count = sum(sample.kind == "cold" for sample in profile.samples)
        if warm_count != warm_repeats or cold_count != cold_repeats:
            raise BenchmarkImportError(f"profiles[{index}] sample counts do not match the calibration plan.")
        imported.append(
            ImportedProfile(
                model_id=profile.inputs.model_id,
                model_digest=profile.inputs.model_digest,
                profile_digest=profile.digest,
                inputs_digest=profile.inputs.digest,
                profile_key=key,
            )
        )

    return Contribution(
        envelope_digest=_digest({key: item for key, item in value.items()}),
        bundle_digest=bundle_digest,
        license=license_entry,
        contributor=contributor,
        hardware_digest=hardware_digest,
        os_id=hardware_os_id,
        methodology_digest=methodology_digest,
        profiles=tuple(sorted(imported, key=lambda item: (item.model_id, item.model_digest))),
    )


def build_envelope(
    bundle: Mapping[str, Any],
    *,
    hardware: Mapping[str, Any],
    license: License,
    contributor: Contributor,
) -> dict[str, Any]:
    """Wrap a published bundle as a contribution, returning it only if it validates.

    Contributors should not hand-assemble envelopes: this composes one and then
    runs the same validation an importer will run, so a mistake is caught by the
    person who made it.
    """
    if not isinstance(license, License) or not isinstance(contributor, Contributor):
        raise TypeError("license and contributor must be License and Contributor values.")
    envelope = {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "license": license.to_mapping(),
        "contributor": contributor.to_mapping(),
        "hardware": dict(hardware),
        "bundle": dict(bundle),
    }
    validate_envelope(envelope)
    return envelope


def write_envelope(path: Path | str, envelope: Mapping[str, Any]) -> Path:
    """Write a canonical contribution file, refusing anything that does not validate."""
    validate_envelope(envelope)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(envelope), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return target


def load_contribution(path: Path | str) -> Contribution:
    """Read and fully verify a contribution file. Nothing is written."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise TypeError("contribution path must be a non-empty path.")
    return validate_envelope(_read_envelope(Path(path)))


# ---------------------------------------------------------------------------
# Trust registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TrustState:
    """The effective trust state of one imported profile."""

    profile_digest: str
    model_id: str
    trust: str
    envelope_digest: str
    evidence_digest: str = ""

    @property
    def is_trusted(self) -> bool:
        return self.trust == TRUST_LOCAL


class TrustRegistry:
    """An append-only record of imports and promotions.

    Every line carries a digest over its own payload and every read re-verifies
    all of them, so an edited registry fails loudly instead of serving a promoted
    profile that was never promoted. Promotions are separate lines: the import
    record always keeps ``untrusted``, and the later evidence is what a reader
    sees on top of it.
    """

    def __init__(self, directory: Path | str) -> None:
        if not isinstance(directory, (str, Path)) or not str(directory).strip():
            raise BenchmarkImportError("registry directory must be a non-empty path.")
        self.directory = Path(directory)
        self.path = self.directory / REGISTRY_FILENAME

    # -- reading ------------------------------------------------------------

    def _records(self) -> tuple[dict[str, Any], ...]:
        if not self.path.is_file():
            return ()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchmarkImportError(f"registry is unreadable ({type(exc).__name__}).") from exc
        records: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkImportError(f"registry line {number} is not valid JSON.") from exc
            if not isinstance(payload, dict):
                raise BenchmarkImportError(f"registry line {number} is not an object.")
            claimed = payload.pop("record_digest", None)
            if claimed != _digest(payload):
                raise BenchmarkImportError(f"registry line {number} digest mismatch: the registry was edited.")
            if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
                raise BenchmarkImportError(f"registry line {number} has an unsupported schema_version.")
            records.append(payload)
        return tuple(records)

    def states(self) -> tuple[TrustState, ...]:
        """Return one effective state per imported profile digest."""
        imported: dict[str, dict[str, Any]] = {}
        promotions: dict[str, str] = {}
        for record in self._records():
            kind = record.get("kind")
            if kind == "import":
                for profile in record.get("profiles", []):
                    if isinstance(profile, Mapping) and isinstance(profile.get("profile_digest"), str):
                        imported[profile["profile_digest"]] = {
                            "model_id": str(profile.get("model_id", "")),
                            "envelope_digest": str(record.get("envelope_digest", "")),
                        }
            elif kind == "promotion":
                digest = record.get("profile_digest")
                if isinstance(digest, str):
                    promotions[digest] = str(record.get("evidence_digest", ""))
            else:
                raise BenchmarkImportError(f"registry contains an unknown record kind: {kind!r}")
        states = [
            TrustState(
                profile_digest=profile_digest,
                model_id=entry["model_id"],
                trust=TRUST_LOCAL if profile_digest in promotions else TRUST_UNTRUSTED,
                envelope_digest=entry["envelope_digest"],
                evidence_digest=promotions.get(profile_digest, ""),
            )
            for profile_digest, entry in imported.items()
        ]
        return tuple(sorted(states, key=lambda state: state.profile_digest))

    def get(self, profile_digest: str) -> TrustState | None:
        for state in self.states():
            if state.profile_digest == profile_digest:
                return state
        return None

    def imported_key(self, profile_digest: str) -> ProfileKey | None:
        """Return the profile key recorded at import time, if any."""
        for record in self._records():
            if record.get("kind") != "import":
                continue
            for profile in record.get("profiles", []):
                if isinstance(profile, Mapping) and profile.get("profile_digest") == profile_digest:
                    return ImportedProfile.from_mapping(profile).profile_key
        return None

    def licence_notice(self) -> str:
        """Render the attribution lines the imported licenses require."""
        lines: list[str] = []
        for record in self._records():
            if record.get("kind") != "import":
                continue
            license_entry = record.get("license")
            contributor = record.get("contributor")
            if not isinstance(license_entry, Mapping):
                continue
            license_id = str(license_entry.get("id", ""))
            attribution = str(license_entry.get("attribution", "")).strip()
            name = str(contributor.get("name", "")) if isinstance(contributor, Mapping) else ""
            if ALLOWED_LICENSES.get(license_id, True):
                lines.append(f"- {attribution or name} ({license_id})")
            else:
                lines.append(f"- {name} ({license_id})")
        return "\n".join(sorted(set(lines)))

    # -- writing ------------------------------------------------------------

    def _append(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {**payload, "schema_version": REGISTRY_SCHEMA_VERSION}
        line = json.dumps({**record, "record_digest": _digest(record)}, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def import_contribution(self, contribution: Contribution, *, imported_at: str = "") -> tuple[TrustState, ...]:
        """Record a validated contribution. Every profile it carries stays untrusted."""
        if not isinstance(contribution, Contribution):
            raise TypeError("contribution must be a Contribution value.")
        existing = {state.profile_digest for state in self.states()}
        fresh = [profile for profile in contribution.profiles if profile.profile_digest not in existing]
        if not fresh:
            return tuple(state for state in self.states() if state.envelope_digest == contribution.envelope_digest)
        self._append(
            {
                "kind": "import",
                "envelope_digest": contribution.envelope_digest,
                "bundle_digest": contribution.bundle_digest,
                "license": contribution.license.to_mapping(),
                "contributor": contribution.contributor.to_mapping(),
                "hardware_digest": contribution.hardware_digest,
                "os_id": contribution.os_id,
                "methodology_digest": contribution.methodology_digest,
                "imported_at": imported_at or _now(),
                "profiles": [{**profile.to_mapping(), "trust": TRUST_UNTRUSTED} for profile in fresh],
            }
        )
        return tuple(state for state in self.states() if state.envelope_digest == contribution.envelope_digest)

    def promote(self, profile_digest: str, *, vault: ProfileVault, promoted_at: str = "") -> TrustState:
        """Mark one imported profile locally verified, against a local measurement only.

        The evidence must be a profile already in the local vault under the same
        key with the same ``profile_digest``. A contributor cannot promote their
        own data by asserting it; only a local re-measurement of the same thing
        on the same key can.
        """
        state = self.get(profile_digest)
        if state is None:
            raise ImportTrustError("that profile digest has not been imported.")
        if state.is_trusted:
            return state
        if not isinstance(vault, ProfileVault):
            raise TypeError("vault must be a ProfileVault value.")
        key = self.imported_key(profile_digest)
        if key is None:  # pragma: no cover - get() already proved it exists
            raise ImportTrustError("that profile digest has not been imported.")
        try:
            record = vault.get(key)
        except ProfileStoreError as exc:
            raise ImportTrustError(f"local vault could not be read: {exc}") from exc
        if record is None:
            raise ImportTrustError(
                "no local measurement exists for this hardware/OS/model/quantization/runtime/methodology key, "
                "so there is nothing to verify the imported profile against."
            )
        if record.profile_digest != profile_digest:
            raise ImportTrustError(
                "the local measurement for this key differs from the imported profile; re-measure the same "
                "configuration or import the matching profile before promoting."
            )
        evidence = {
            "kind": "local-vault-match",
            "vault_key_digest": key.digest,
            "local_profile_digest": record.profile_digest,
            "local_inputs_digest": record.profile.inputs.digest,
            "promoted_at": promoted_at or _now(),
        }
        self._append(
            {
                "kind": "promotion",
                "profile_digest": profile_digest,
                "evidence": evidence,
                "evidence_digest": _digest(evidence),
            }
        )
        promoted = self.get(profile_digest)
        if promoted is None:  # pragma: no cover - defensive
            raise ImportTrustError("promotion did not persist.")
        return promoted
