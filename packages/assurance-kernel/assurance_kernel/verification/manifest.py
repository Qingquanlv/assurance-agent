"""Validate GeneratedManifest against on-disk Change-local round trees.

Pure-ish verification helpers: digest computation and fail-closed path/digest/
extra-file checks. No workflow imports. Phase 1 forbids ``overlay`` role.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from assurance_kernel.artifacts.canonical import sha256_bytes
from assurance_kernel.artifacts.models.discovery import GeneratedManifest

ManifestErrorCode = Literal[
    "source_missing",
    "digest_mismatch",
    "path_escape",
    "symlink_escape",
    "extra_unmanifested_file",
    "overlay_forbidden",
    "target_not_phase1_api",
    "execution_selection_unknown",
    "source_not_under_generated",
    "duplicate_target",
    "round_root_missing",
]

_GENERATED_PREFIX = "generated/"
_PHASE1_TARGET_PREFIX = "tests/api/"


class ManifestValidationError(Exception):
    """Fail-closed manifest/tree mismatch with a stable error code."""

    def __init__(self, code: ManifestErrorCode, message: str, *, path: str | None = None) -> None:
        self.code: ManifestErrorCode = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True)
class ManifestValidationResult:
    """Successful validation receipt (digests recomputed from disk)."""

    round_id: str
    file_digests: dict[str, str]


def digest_file(path: Path) -> str:
    """Return ``sha256:<hex>`` for file bytes."""
    return sha256_bytes(path.read_bytes())


def digest_bytes(data: bytes) -> str:
    """Return ``sha256:<hex>`` for raw bytes."""
    return sha256_bytes(data)


def validate_generated_manifest(
    round_root: Path,
    manifest: GeneratedManifest,
    *,
    allow_overlay: bool = False,
) -> ManifestValidationResult:
    """Validate ``manifest`` against the on-disk round tree under ``round_root``.

    Fail-closed rules (Phase 1):
    - every ``files[].source`` exists under ``generated/`` relative to round root
    - sha256 matches file bytes
    - resolved paths must stay inside the round root (symlink escape rejected)
    - every regular file under ``generated/`` must appear in the manifest
    - targets must remain under ``tests/api/**``
    - ``role=overlay`` forbidden unless ``allow_overlay``
    - ``execution_selection`` entries must map to manifest targets
    """
    if not round_root.is_dir():
        raise ManifestValidationError(
            "round_root_missing",
            f"round root does not exist or is not a directory: {round_root}",
            path=str(round_root),
        )

    try:
        root_resolved = round_root.resolve(strict=True)
    except OSError as exc:
        raise ManifestValidationError(
            "round_root_missing",
            f"cannot resolve round root: {round_root}",
            path=str(round_root),
        ) from exc

    seen_targets: set[str] = set()
    file_digests: dict[str, str] = {}
    declared_sources: set[str] = set()

    for entry in manifest.files:
        if entry.role == "overlay" and not allow_overlay:
            raise ManifestValidationError(
                "overlay_forbidden",
                "Phase 1 forbids role=overlay without explicit allow_overlay",
                path=entry.target,
            )
        if not entry.target.startswith(_PHASE1_TARGET_PREFIX) or entry.target == "tests/api":
            raise ManifestValidationError(
                "target_not_phase1_api",
                f"target must be under tests/api/**: {entry.target}",
                path=entry.target,
            )
        if entry.target in seen_targets:
            raise ManifestValidationError(
                "duplicate_target",
                f"duplicate manifest target: {entry.target}",
                path=entry.target,
            )
        seen_targets.add(entry.target)

        if not entry.source.startswith(_GENERATED_PREFIX) or entry.source == "generated":
            raise ManifestValidationError(
                "source_not_under_generated",
                f"source must be under generated/: {entry.source}",
                path=entry.source,
            )
        if ".." in entry.source.split("/") or ".." in entry.target.split("/"):
            raise ManifestValidationError(
                "path_escape",
                f"path contains '..' segment: {entry.source}",
                path=entry.source,
            )

        source_path = _resolve_under_root(root_resolved, entry.source)
        declared_sources.add(entry.source)

        if not source_path.is_file():
            raise ManifestValidationError(
                "source_missing",
                f"manifest source missing on disk: {entry.source}",
                path=entry.source,
            )

        actual = digest_file(source_path)
        if actual != entry.sha256:
            raise ManifestValidationError(
                "digest_mismatch",
                f"sha256 mismatch for {entry.source}: expected {entry.sha256}, got {actual}",
                path=entry.source,
            )
        file_digests[entry.target] = actual

    for selected in manifest.execution_selection:
        if selected not in seen_targets:
            raise ManifestValidationError(
                "execution_selection_unknown",
                f"execution_selection target not in manifest files: {selected}",
                path=selected,
            )

    _reject_extra_generated_files(root_resolved, declared_sources)

    return ManifestValidationResult(round_id=manifest.round_id, file_digests=file_digests)


def _resolve_under_root(root_resolved: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root_resolved``; reject symlink/path escape."""
    # Lexical join first; then resolve and require containment.
    candidate = root_resolved.joinpath(*Path(rel).parts)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ManifestValidationError(
            "path_escape",
            f"cannot resolve path under round root: {rel}",
            path=rel,
        ) from exc

    if not _is_under(resolved, root_resolved):
        raise ManifestValidationError(
            "symlink_escape",
            f"resolved path escapes round root: {rel} -> {resolved}",
            path=rel,
        )

    # If the leaf (or any parent under root) is a symlink that left the tree,
    # resolve(strict=False) already caught it. Also reject when the lexical
    # candidate exists as a symlink pointing outside.
    if candidate.is_symlink():
        link_target = candidate.resolve(strict=False)
        if not _is_under(link_target, root_resolved):
            raise ManifestValidationError(
                "symlink_escape",
                f"symlink escapes round root: {rel}",
                path=rel,
            )
    return resolved


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_extra_generated_files(root_resolved: Path, declared_sources: set[str]) -> None:
    generated = root_resolved / "generated"
    if not generated.exists():
        return
    if generated.is_symlink():
        resolved = generated.resolve(strict=False)
        if not _is_under(resolved, root_resolved):
            raise ManifestValidationError(
                "symlink_escape",
                "generated/ symlink escapes round root",
                path="generated",
            )

    for path in sorted(generated.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        # Skip directories; include symlink files even if dangling.
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            rel = path.resolve(strict=False).relative_to(root_resolved).as_posix()
        except ValueError:
            raise ManifestValidationError(
                "symlink_escape",
                f"generated file resolves outside round root: {path}",
                path=str(path),
            ) from None
        # Prefer the lexical relative path under generated/ for manifest match.
        try:
            lexical_rel = path.relative_to(root_resolved).as_posix()
        except ValueError:
            lexical_rel = rel
        if lexical_rel not in declared_sources and rel not in declared_sources:
            raise ManifestValidationError(
                "extra_unmanifested_file",
                f"unmanifested file under generated/: {lexical_rel}",
                path=lexical_rel,
            )
        # Symlink leaf that escapes (even if declared) — re-check containment.
        if path.is_symlink():
            target = path.resolve(strict=False)
            if not _is_under(target, root_resolved):
                raise ManifestValidationError(
                    "symlink_escape",
                    f"symlink escapes round root: {lexical_rel}",
                    path=lexical_rel,
                )


__all__ = [
    "ManifestErrorCode",
    "ManifestValidationError",
    "ManifestValidationResult",
    "digest_bytes",
    "digest_file",
    "validate_generated_manifest",
]
