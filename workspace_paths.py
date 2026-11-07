"""Bounded Studio-host path selection. No file reads, process launches or inference.

POSIX directory capabilities are opened component-by-component without following
symlinks. Unsupported hosts fail closed; existing manual path entry remains usable.
"""
from __future__ import annotations

from contextlib import contextmanager
import itertools
import os
from pathlib import Path
import re
import stat
import unicodedata
from typing import Any, Iterator, Sequence


class PickerError(ValueError):
    """An invalid, disallowed, missing or unavailable picker location."""


def folder_name(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PickerError("Enter a folder name without leading or trailing spaces.")
    if (not _visible(value) or len(value.encode("utf-8")) > 240 or value.startswith(".") or value.endswith(".")
            or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>|?*' for c in value)
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
        raise PickerError("Use a single ordinary folder name, not a path, hidden name or reserved name.")
    return value


def _visible(name: str) -> bool:
    low = name.lower()
    return (not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in name)
            and (not name.startswith(".") or name == ".venv")
            and low != "application_default_credentials.json"
            and not low.startswith(("credentials", "secrets", "id_rsa", "id_ed25519"))
            and not low.endswith((".pem", ".key", ".p12", ".pfx")))


def roots(candidates: Sequence[Path]) -> list[Path]:
    result = []
    for candidate in candidates:
        try:
            path = candidate.expanduser().resolve(strict=True)
            if path.is_dir() and path not in result and all(_visible(p) for p in path.parts):
                result.append(path)
        except (OSError, ValueError, RuntimeError):
            continue
    if not result:
        raise PickerError("No accessible picker roots. Keep using manual path entry.")
    return result


def _target(raw: str, allowed: Sequence[Path]) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise PickerError("Choose an absolute Studio-host path.")
    path = Path(raw).expanduser()
    if not path.is_absolute() or ".." in path.parts or not all(_visible(p) for p in path.parts):
        raise PickerError("Hidden, secret and parent-traversal paths are not browsable.")
    if not any(path.is_relative_to(root) for root in allowed):
        raise PickerError("Path is outside the approved picker roots; manual entry remains available.")
    return path


@contextmanager
def directory(raw: str, allowed: Sequence[Path]) -> Iterator[tuple[int, Path]]:
    path = _target(raw, allowed)
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
            and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd
            and os.scandir in os.supports_fd):
        raise PickerError("Secure host browsing is unavailable on this platform. Use manual path entry.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, path
    except OSError as exc:
        raise PickerError("Folder is unavailable, inaccessible or a symlink. Nothing was changed.") from exc
    finally:
        os.close(fd)


def browse(raw: str, allowed: Sequence[Path], *, kind: str = "directory", query: str = "") -> dict[str, Any]:
    if kind not in {"directory", "file"} or len(query) > 100:
        raise PickerError("Invalid picker type or search text.")
    entries: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    with directory(raw, allowed) as (fd, path):
        with os.scandir(fd) as iterator:
            for entry in itertools.islice(iterator, 2001):
                scanned += 1
                if scanned > 2000:
                    truncated = True
                    break
                if not _visible(entry.name) or query.casefold() not in entry.name.casefold():
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not (kind == "file" and stat.S_ISREG(info.st_mode)):
                    continue
                if len(entries) == 200:
                    truncated = True
                    break
                entries.append({"name": entry.name, "path": str(path / entry.name), "kind": "directory" if is_dir else "file"})
    parent = str(path.parent) if any(path.parent.is_relative_to(root) for root in allowed) else None
    return {"path": str(path), "parent": parent, "entries": sorted(entries, key=lambda e: (e["kind"] != "directory", e["name"].casefold())),
            "roots": [str(root) for root in allowed], "truncated": truncated, "filesystem": "studio_host"}


def create_folder(parent: str, name: Any, allowed: Sequence[Path], *, confirmed: bool) -> str:
    if confirmed is not True:
        raise PickerError("Explicit folder-creation confirmation is required.")
    clean = folder_name(name)
    with directory(parent, allowed) as (fd, path):
        try:
            os.mkdir(clean, mode=0o700, dir_fd=fd)
        except FileExistsError as exc:
            raise PickerError("That name already exists. Choose another name or browse into it.") from exc
        return str(path / clean)


def suggestion(text: str) -> str:
    """Only a JSON basename, never a filesystem action or executable instruction."""
    import json
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        value = json.loads(clean)
    except (ValueError, TypeError) as exc:
        raise PickerError("The model did not return a valid folder-name suggestion. Enter a name yourself.") from exc
    if not isinstance(value, dict) or set(value) != {"name"}:
        raise PickerError("The model must suggest just one folder name, not paths or commands.")
    return folder_name(value["name"])
