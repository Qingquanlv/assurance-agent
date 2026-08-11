"""Materialize Change-local generated API tests into a temporary SUT workspace.

Writes only into a fresh temp workspace — never into the caller's canonical
``project_root/tests/**``. Callers own temp lifecycle; ``destroy_workspace``
is provided as a helper. Change-local discovery assets under the round dir
are left untouched when the temp workspace is destroyed.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.discovery import GeneratedFileEntry, GeneratedManifest
from assurance_agent.verification.manifest import (
    ManifestValidationError,
    ManifestValidationResult,
    digest_file,
    validate_generated_manifest,
)

MaterializationErrorCode = str  # narrow codes below


class MaterializationError(Exception):
    """Typed materialization failure (post-validation or workspace policy)."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True)
class MaterializedFile:
    target: str
    sha256: str


@dataclass(frozen=True)
class MaterializationReceipt:
    """Record of files placed into the temporary workspace."""

    materialized: tuple[MaterializedFile, ...]
    environment_digest: str
    seed: int | None = None
    base_revision: str | None = None
    round_id: str | None = None


@dataclass(frozen=True)
class MaterializeResult:
    workspace: Path
    receipt: MaterializationReceipt


def validate_generated_manifest_op(
    round_dir: Path,
    manifest: GeneratedManifest,
    *,
    allow_overlay: bool = False,
) -> ManifestValidationResult:
    """Thin ``validate-generated-manifest`` operation wrapper."""
    return validate_generated_manifest(round_dir, manifest, allow_overlay=allow_overlay)


def materialize_test_overlay(
    *,
    project_root: Path,
    round_dir: Path,
    manifest: GeneratedManifest,
    temp_factory: Callable[[], Path] | None = None,
    selection_only: bool = True,
) -> MaterializeResult:
    """Thin ``materialize-test-overlay`` operation wrapper."""
    return materialize_round(
        project_root=project_root,
        round_dir=round_dir,
        manifest=manifest,
        temp_factory=temp_factory,
        selection_only=selection_only,
    )


def materialize_round(
    *,
    project_root: Path,
    round_dir: Path,
    manifest: GeneratedManifest,
    temp_factory: Callable[[], Path] | None = None,
    selection_only: bool = True,
) -> MaterializeResult:
    """Validate manifest, provision a temp workspace, and materialize files.

    Parameters
    ----------
    project_root:
        Frozen product revision tree (read-only; never written).
    round_dir:
        Change-local round directory containing ``generated/`` + manifest sources.
    manifest:
        Authorized materialization map.
    temp_factory:
        Optional factory returning a fresh directory path. Defaults to
        ``tempfile.mkdtemp(prefix="aa-discovery-")``.
    selection_only:
        When True (default), only ``execution_selection`` targets are copied.
        Manifest still must validate in full (including unselected files).
    """
    project_root = project_root.resolve()
    round_dir = round_dir.resolve()

    # Fail-closed validation before any workspace writes.
    validate_generated_manifest(round_dir, manifest)

    workspace = _provision_workspace(project_root, temp_factory)
    try:
        entries = _selected_entries(manifest, selection_only=selection_only)
        materialized = _copy_entries(
            project_root=project_root,
            round_dir=round_dir,
            workspace=workspace,
            entries=entries,
        )
        # Recompute digests after copy; mismatch → typed failure.
        verified: list[MaterializedFile] = []
        for entry in materialized:
            dest = workspace / entry.target
            actual = digest_file(dest)
            if actual != entry.sha256:
                raise MaterializationError(
                    "post_copy_digest_mismatch",
                    f"digest mismatch after materialize: {entry.target}",
                    path=entry.target,
                )
            verified.append(MaterializedFile(target=entry.target, sha256=actual))

        env_digest = _environment_digest(
            base_revision=manifest.base_revision,
            materialized=tuple(verified),
            seed=manifest.seed,
        )
        receipt = MaterializationReceipt(
            materialized=tuple(verified),
            environment_digest=env_digest,
            seed=manifest.seed,
            base_revision=manifest.base_revision,
            round_id=manifest.round_id,
        )
        return MaterializeResult(workspace=workspace, receipt=receipt)
    except Exception:
        # Avoid leaking partial workspaces on failure when we created them.
        destroy_workspace(workspace)
        raise


def destroy_workspace(workspace: Path) -> None:
    """Destroy a temporary materialization workspace.

    Does not touch Change-local discovery assets outside ``workspace``.
    """
    if not workspace.exists():
        return
    resolved = workspace.resolve()
    # Refuse to delete obvious non-temp roots (safety rail for misuse).
    if resolved.is_dir() and (resolved / ".git").is_dir() and (resolved / "pyproject.toml").is_file():
        raise MaterializationError(
            "refuse_destroy_project_root",
            f"refusing to destroy path that looks like a project root: {resolved}",
            path=str(resolved),
        )
    shutil.rmtree(resolved)


def _provision_workspace(
    project_root: Path,
    temp_factory: Callable[[], Path] | None,
) -> Path:
    if temp_factory is None:
        workspace = Path(tempfile.mkdtemp(prefix="aa-discovery-"))
    else:
        workspace = Path(temp_factory())
        workspace.mkdir(parents=True, exist_ok=True)

    workspace_resolved = workspace.resolve()
    if workspace_resolved == project_root or project_root in workspace_resolved.parents:
        raise MaterializationError(
            "workspace_inside_project",
            "temp workspace must not be inside project_root",
            path=str(workspace_resolved),
        )
    if workspace_resolved == project_root or workspace_resolved in project_root.parents:
        raise MaterializationError(
            "workspace_is_project_ancestor",
            "temp workspace must not be project_root or an ancestor",
            path=str(workspace_resolved),
        )

    # Copy product tree into workspace (read from project; write only to temp).
    for item in project_root.iterdir():
        dest = workspace / item.name
        if item.is_dir():
            shutil.copytree(item, dest, symlinks=False, ignore_dangling_symlinks=True)
        elif item.is_file():
            shutil.copy2(item, dest)
    return workspace


def _selected_entries(
    manifest: GeneratedManifest,
    *,
    selection_only: bool,
) -> tuple[GeneratedFileEntry, ...]:
    if not selection_only:
        return manifest.files
    selected = set(manifest.execution_selection)
    return tuple(entry for entry in manifest.files if entry.target in selected)


def _copy_entries(
    *,
    project_root: Path,
    round_dir: Path,
    workspace: Path,
    entries: tuple[GeneratedFileEntry, ...],
) -> tuple[MaterializedFile, ...]:
    out: list[MaterializedFile] = []
    workspace_resolved = workspace.resolve()
    project_resolved = project_root.resolve()

    for entry in entries:
        source = (round_dir / entry.source).resolve()
        lexical_dest = workspace_resolved.joinpath(*Path(entry.target).parts)
        if (
            not _is_under(lexical_dest.parent, workspace_resolved)
            and lexical_dest.parent != workspace_resolved
        ):
            raise MaterializationError(
                "target_escapes_workspace",
                f"materialize target escapes workspace: {entry.target}",
                path=entry.target,
            )

        # Never write into the original project tree (canonical tests or otherwise).
        if _is_under(lexical_dest, project_resolved) or lexical_dest == project_resolved:
            raise MaterializationError(
                "refuse_write_project_tests",
                f"refusing to write into project_root: {entry.target}",
                path=entry.target,
            )

        lexical_dest.parent.mkdir(parents=True, exist_ok=True)

        # Forbid overwriting an existing canonical product test (search_test).
        if lexical_dest.exists():
            raise MaterializationError(
                "canonical_target_exists",
                f"refusing to overwrite existing workspace file without overlay: {entry.target}",
                path=entry.target,
            )

        shutil.copy2(source, lexical_dest)
        out.append(MaterializedFile(target=entry.target, sha256=entry.sha256))
    return tuple(out)


def _environment_digest(
    *,
    base_revision: str,
    materialized: tuple[MaterializedFile, ...],
    seed: int,
) -> str:
    lines = [f"base_revision:{base_revision}", f"seed:{seed}"]
    for item in sorted(materialized, key=lambda m: m.target):
        lines.append(f"{item.target}:{item.sha256}")
    return sha256_bytes("\n".join(lines).encode())


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# Re-export validation error for callers/tests that only import materialize.
__all__ = [
    "MaterializationError",
    "MaterializationReceipt",
    "MaterializeResult",
    "MaterializedFile",
    "ManifestValidationError",
    "destroy_workspace",
    "materialize_round",
    "materialize_test_overlay",
    "validate_generated_manifest_op",
]
