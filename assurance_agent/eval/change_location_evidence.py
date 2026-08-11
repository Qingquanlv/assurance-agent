"""D17 strict change-location evidence records and pure replay verification."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.change_location import (
    ChangeLocationProbe,
    ChangeNotFoundError,
    ParsedChangeRoots,
    decide_change_location,
    parse_change_roots,
    probe_change_location_candidates,
)
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe

ChangeLocationLstatKind = Literal["missing", "directory", "symlink", "other"]


class ChangeLocationEvidenceError(AaError):
    """Change-location evidence build/replay failure."""


class ChangeLocationCandidateV1(StrictWireModel):
    source: Literal["changes", "archive"]
    configured_root: str
    candidate_repo_path: str
    lstat_kind: ChangeLocationLstatKind
    mode: int | None
    has_before_manifest_leaf: bool
    selected: bool

    @model_validator(mode="after")
    def validate_kind_mode(self) -> ChangeLocationCandidateV1:
        if self.lstat_kind == "missing":
            if self.mode is not None:
                raise ValueError("missing candidate must omit mode")
        elif self.mode is None:
            raise ValueError(f"{self.lstat_kind} candidate requires mode")
        return self


class ChangeLocationEvidenceV1(StrictWireModel):
    schema_version: Literal["1"]
    change_id: str
    preference: Literal["active"]
    selected_source: Literal["changes"]
    resolved_change_repo_path: str
    configured_changes_root: str
    configured_archive_root: str
    config_sha256: str
    candidates: list[ChangeLocationCandidateV1] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_canonical(self) -> ChangeLocationEvidenceV1:
        assert_change_id_safe(self.change_id)
        digest = self.config_sha256
        if (
            len(digest) != len("sha256:") + 64
            or not digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in digest.removeprefix("sha256:"))
        ):
            raise ValueError("config_sha256 must be lowercase sha256:<64-hex>")
        sources = [c.source for c in self.candidates]
        if sources != ["changes", "archive"]:
            raise ValueError("candidates must be exactly [changes, archive] in that order")
        selected = [c for c in self.candidates if c.selected]
        if len(selected) != 1 or selected[0].source != "changes":
            raise ValueError("exactly one changes candidate must be selected")
        if selected[0].candidate_repo_path != self.resolved_change_repo_path:
            raise ValueError("resolved_change_repo_path must match selected candidate")
        if selected[0].configured_root != self.configured_changes_root:
            raise ValueError("configured_changes_root must match changes candidate")
        archive = self.candidates[1]
        if archive.configured_root != self.configured_archive_root:
            raise ValueError("configured_archive_root must match archive candidate")
        if archive.selected:
            raise ValueError("archive candidate must not be selected for preference=active")
        return self


def _leaf_under(candidate_repo_path: str, leaves: Collection[str]) -> bool:
    prefix = candidate_repo_path.rstrip("/") + "/"
    return any(leaf == candidate_repo_path or leaf.startswith(prefix) for leaf in leaves)


def _probe_to_candidate(
    probe: ChangeLocationProbe,
    *,
    leaves: Collection[str],
    selected: bool,
) -> ChangeLocationCandidateV1:
    return ChangeLocationCandidateV1(
        source=probe.source,
        configured_root=probe.configured_root,
        candidate_repo_path=probe.candidate_repo_path,
        lstat_kind=probe.lstat_kind,
        mode=probe.mode,
        has_before_manifest_leaf=_leaf_under(probe.candidate_repo_path, leaves),
        selected=selected,
    )


def build_change_location_evidence(
    *,
    change_id: str,
    config_bytes: bytes,
    probes: Sequence[ChangeLocationProbe],
    before_manifest_leaves: Collection[str],
) -> ChangeLocationEvidenceV1:
    """Build strict D17 evidence from config bytes, probes, and leaf facts.

    Preference is always ``active``. Archive-only / unsafe resolution raises
    and produces no writable evidence.
    """
    assert_change_id_safe(change_id)
    roots = parse_change_roots(config_bytes)
    if len(probes) != 2 or [p.source for p in probes] != ["changes", "archive"]:
        raise ChangeLocationEvidenceError("probes must be exactly [changes, archive]")

    try:
        decided = decide_change_location(
            change_id=change_id,
            preference="active",
            roots=roots,
            probes=tuple(probes),
        )
    except ChangeNotFoundError as exc:
        raise ChangeLocationEvidenceError(str(exc)) from exc

    if decided.source != "changes":
        raise ChangeLocationEvidenceError("preference=active must select the changes root")

    candidates = [
        _probe_to_candidate(probes[0], leaves=before_manifest_leaves, selected=True),
        _probe_to_candidate(probes[1], leaves=before_manifest_leaves, selected=False),
    ]
    if candidates[0].lstat_kind != "directory":
        raise ChangeLocationEvidenceError("selected changes candidate must be a real directory")
    if candidates[0].candidate_repo_path != decided.repo_path:
        raise ChangeLocationEvidenceError("probe/decision path mismatch")

    return ChangeLocationEvidenceV1(
        schema_version="1",
        change_id=change_id,
        preference="active",
        selected_source="changes",
        resolved_change_repo_path=decided.repo_path,
        configured_changes_root=roots.changes_root,
        configured_archive_root=roots.archive_root,
        config_sha256=sha256_bytes(config_bytes),
        candidates=candidates,
    )


def replay_change_location_evidence(
    *,
    evidence: ChangeLocationEvidenceV1,
    config_bytes: bytes,
    before_manifest_leaves: Collection[str],
) -> ChangeLocationEvidenceV1:
    """Reparse config bytes, recompute decision from recorded probes, require identical bytes."""
    digest = sha256_bytes(config_bytes)
    if digest != evidence.config_sha256:
        raise ChangeLocationEvidenceError("config digest mismatch")

    roots = parse_change_roots(config_bytes)
    if roots.changes_root != evidence.configured_changes_root:
        raise ChangeLocationEvidenceError("configured changes root mismatch")
    if roots.archive_root != evidence.configured_archive_root:
        raise ChangeLocationEvidenceError("configured archive root mismatch")

    if len(evidence.candidates) != 2:
        raise ChangeLocationEvidenceError("evidence must contain exactly two candidates")

    probes = tuple(
        ChangeLocationProbe(
            source=c.source,
            configured_root=c.configured_root,
            candidate_repo_path=c.candidate_repo_path,
            lstat_kind=c.lstat_kind,
            mode=c.mode,
        )
        for c in evidence.candidates
    )
    rebuilt = build_change_location_evidence(
        change_id=evidence.change_id,
        config_bytes=config_bytes,
        probes=probes,
        before_manifest_leaves=before_manifest_leaves,
    )
    if canonical_json_bytes(rebuilt) != canonical_json_bytes(evidence):
        raise ChangeLocationEvidenceError("change-location evidence replay mismatch")
    return rebuilt


def probe_live_candidates(
    project_root: Path,
    change_id: str,
    config_bytes: bytes,
) -> tuple[ParsedChangeRoots, tuple[ChangeLocationProbe, ...]]:
    """Parse roots and lstat-probe candidates under a live project root."""
    roots = parse_change_roots(config_bytes)
    probes = probe_change_location_candidates(project_root, change_id, roots)
    return roots, probes


def config_sha256_hex(config_bytes: bytes) -> str:
    """Unprefixed SHA-256 hex of config bytes (diagnostics)."""
    return hashlib.sha256(config_bytes).hexdigest()


def lstat_kind_from_mode(mode: int | None, *, missing: bool) -> ChangeLocationLstatKind:
    if missing or mode is None:
        return "missing"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


__all__ = [
    "ChangeLocationCandidateV1",
    "ChangeLocationEvidenceError",
    "ChangeLocationEvidenceV1",
    "build_change_location_evidence",
    "config_sha256_hex",
    "lstat_kind_from_mode",
    "probe_live_candidates",
    "replay_change_location_evidence",
]
