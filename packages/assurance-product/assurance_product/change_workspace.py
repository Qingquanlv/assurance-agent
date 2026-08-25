from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ChangePaths:
    project_root: Path
    change_root: Path
    staging_root: Path
    runtime_root: Path
    generated_root: Path
    apply_manifest: Path


def safe_change_id(change_id: str) -> str:
    if not isinstance(change_id, str) or not change_id:
        raise ValueError("change_id must be a non-empty path component")
    if "\x00" in change_id or change_id in {".", ".."}:
        raise ValueError("change_id must be a safe path component")
    if "/" in change_id or "\\" in change_id:
        raise ValueError("change_id must not contain path separators")
    return change_id


def safe_relative_path(output: str | Path) -> Path:
    path = Path(output)
    if "\x00" in str(output) or path.is_absolute():
        raise ValueError("output path must be relative and NUL-free")
    if any(part == ".." for part in path.parts):
        raise ValueError("output path must not escape the project")
    return path


def require_real_directory(path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("project_root must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project_root must be an existing directory") from exc
    if resolved != candidate or not candidate.is_dir():
        raise ValueError("project_root must be a canonical real directory")
    return candidate


def require_descendant(project: Path, path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("change workspace must be an existing directory") from exc
    if resolved != path or not path.is_dir():
        raise ValueError("change workspace directories must be real directories")
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ValueError("path must remain under project_root") from exc
    return path


class ChangeWorkspace:
    def __init__(self, paths: ChangePaths) -> None:
        self.paths = paths

    @classmethod
    def open(cls, project_root: Path, change_id: str) -> ChangeWorkspace:
        project = require_real_directory(project_root)
        qa = project / "qa"
        changes = qa / "changes"
        if not qa.is_dir() or qa.is_symlink() or not changes.is_dir() or changes.is_symlink():
            raise ValueError("project must contain real qa/changes directories")
        change = require_descendant(project, changes / safe_change_id(change_id))
        paths = ChangePaths(
            project_root=project,
            change_root=change,
            staging_root=change / ".staging",
            runtime_root=change / ".runtime",
            generated_root=change / "generated",
            apply_manifest=change / "apply-manifest.json",
        )
        return cls(paths)

    def initialize(self) -> None:
        runtime = self.paths.runtime_root
        staging = self.paths.staging_root
        expected_runtime = {"ledger", "activities", "receipts"}
        runtime_exists = runtime.exists()
        staging_exists = staging.exists()
        if runtime_exists != staging_exists:
            raise ValueError("change workspace has a partial initialization")
        if runtime_exists:
            if not runtime.is_dir() or not staging.is_dir():
                raise ValueError("change workspace contains a non-directory path")
            if {child.name for child in runtime.iterdir()} != expected_runtime:
                raise ValueError("runtime directory has an incomplete layout")
            if any(not (runtime / name).is_dir() for name in expected_runtime):
                raise ValueError("runtime directory contains a non-directory path")
            return

        created: list[Path] = []

        def create_directory(path: Path) -> None:
            path.mkdir()
            created.append(path)

        try:
            create_directory(runtime)
            for name in ("ledger", "activities", "receipts"):
                create_directory(runtime / name)
            create_directory(staging)
        except OSError as exc:
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise ValueError("could not initialize change workspace") from exc
