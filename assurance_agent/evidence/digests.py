"""Shared projection digests, TraceSource recording, and safe evidence-entry digests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.artifacts.models.issues import IssueEvidenceManifestEntry
from assurance_agent.artifacts.models.trace import TraceProjectionLike, TraceSource

EVIDENCE_ENTRY_DIGEST_SEMANTICS = "evidence_entry_digest/v1"
EVIDENCE_ENTRY_PREFIXES = (
    "execution",
    "cases",
    "facts",
    "review",
    "healing",
    "codegen",
)

_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW

# Collector V1 secret-redaction patterns (order and literals are digest-stable).
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)access[_-]?token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)api[_-]?key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)password[\"']?\s*[:=]\s*[\"']?\S{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
)
_REDACTED = "[REDACTED]"


class TraceSourceConflictError(ValueError):
    """The same source path was observed with conflicting facts."""


class EvidenceEntryPathError(ValueError):
    """A manifest evidence path cannot be opened under the allowed root."""


class TraceSourceRecorder:
    def __init__(self) -> None:
        self._by_path: dict[str, TraceSource] = {}

    def add(self, source: TraceSource) -> None:
        existing = self._by_path.get(source.path)
        if existing is not None and existing != source:
            raise TraceSourceConflictError(source.path)
        self._by_path[source.path] = source

    def freeze(self) -> tuple[TraceSource, ...]:
        return tuple(self._by_path[path] for path in sorted(self._by_path))


@dataclass(frozen=True, slots=True)
class ValidatedEvidenceEntry:
    path: str
    data: bytes
    entry_digest: str
    raw_sha256: str


def _after_evidence_entry_normalized_hook(change_dir: Path, path: str) -> None:
    """Test seam between path normalization and descriptor-bound open."""
    del change_dir, path


def redact_evidence_text_v1(text: str) -> str:
    for pat in _REDACT_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


def raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evidence_entry_digest_v1(data: bytes) -> str:
    try:
        digest_input = redact_evidence_text_v1(data.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        digest_input = data
    return f"sha256:{raw_sha256(digest_input)}"


def evidence_bundle_digest_v1(
    entries: Sequence[IssueEvidenceManifestEntry],
) -> str:
    canonical = json.dumps(
        [
            {"digest": entry.digest, "path": entry.path}
            for entry in sorted(entries, key=lambda item: item.path)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{raw_sha256(canonical)}"


def normalize_evidence_entry_path(path: str) -> str:
    if not path or path != path.strip():
        raise EvidenceEntryPathError(f"invalid evidence path: {path!r}")
    if "\\" in path or path.startswith("/"):
        raise EvidenceEntryPathError(f"invalid evidence path: {path!r}")
    parts = path.split("/")
    if len(parts) < 2:
        raise EvidenceEntryPathError(f"invalid evidence path: {path!r}")
    if any(part == "" or part == "." or part == ".." for part in parts):
        raise EvidenceEntryPathError(f"invalid evidence path: {path!r}")
    if parts[0] not in EVIDENCE_ENTRY_PREFIXES:
        raise EvidenceEntryPathError(f"evidence path outside allowlist: {path!r}")
    normalized = "/".join(parts)
    if normalized != path:
        raise EvidenceEntryPathError(f"invalid evidence path: {path!r}")
    return normalized


def _read_fd_to_eof(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def read_evidence_entry_v1(change_dir: Path, path: str) -> ValidatedEvidenceEntry:
    normalized = normalize_evidence_entry_path(path)
    _after_evidence_entry_normalized_hook(change_dir, normalized)
    parts = PurePosixPath(normalized).parts
    root_fd: int | None = None
    opened: list[int] = []
    file_fd: int | None = None
    try:
        try:
            root_fd = os.open(change_dir, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise EvidenceEntryPathError(f"change root unreadable: {change_dir}") from exc
        parent_fd = root_fd
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise EvidenceEntryPathError(f"evidence path unreadable: {normalized}") from exc
            opened.append(next_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(parts[-1], _OPEN_FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise EvidenceEntryPathError(f"evidence path unreadable: {normalized}") from exc
        try:
            st = os.fstat(file_fd)
        except OSError as exc:
            raise EvidenceEntryPathError(f"evidence path unreadable: {normalized}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceEntryPathError(f"evidence path is not a regular file: {normalized}")
        data = _read_fd_to_eof(file_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(opened):
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)

    return ValidatedEvidenceEntry(
        path=normalized,
        data=data,
        entry_digest=evidence_entry_digest_v1(data),
        raw_sha256=raw_sha256(data),
    )


def canonical_json_bytes(obj: object) -> bytes:
    """Deterministic JSON bytes for digests (sorted keys, compact, ensure_ascii default)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_canonical_json_default,
    ).encode("utf-8")


def _canonical_json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SelectedTargets):
        return value.model_dump()
    raise TypeError(f"unsupported type for canonical JSON: {type(value)!r}")


def projection_digest(projection: TraceProjectionLike) -> str:
    payload = projection.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
