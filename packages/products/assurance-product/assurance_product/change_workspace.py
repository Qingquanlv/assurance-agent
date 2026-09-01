from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graph_engine.plugin_api import InvocationWorkspaceBinding


def _mkdir(path: Path) -> None:
    path.mkdir()


@dataclass(frozen=True, slots=True)
class ChangePaths:
    project_root: Path
    change_root: Path
    staging_root: Path
    runtime_root: Path
    generated_root: Path
    apply_manifest: Path
    langgraph_root: Path
    langgraph_checkpoints: Path
    langgraph_leases: Path
    langgraph_selections: Path


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


def _ensure_real_directory(path: Path, created: list[Path]) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError("change workspace directories must be real directories")
        return
    _mkdir(path)
    created.append(path)


_LANGGRAPH_SQLITE_NAMES = frozenset(
    {
        "checkpoints.sqlite3",
        "checkpoints.sqlite3-wal",
        "checkpoints.sqlite3-shm",
        "checkpoints.sqlite3-journal",
    }
)
_LANGGRAPH_DIRECTORY_NAMES = frozenset({"leases", "selections"})


def _validate_langgraph_subtree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("runtime directory contains a non-directory path")
    children = {child.name for child in path.iterdir()}
    if not children.issubset(_LANGGRAPH_SQLITE_NAMES | _LANGGRAPH_DIRECTORY_NAMES):
        raise ValueError("runtime directory has an incomplete layout")
    for name in children:
        child = path / name
        if child.is_symlink():
            raise ValueError("change workspace contains a symlink")
        if name in _LANGGRAPH_DIRECTORY_NAMES:
            if not child.is_dir():
                raise ValueError("runtime directory contains a non-directory path")
            continue
        if not child.is_file():
            raise ValueError("runtime directory contains a non-directory path")


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
        runtime_root = change / ".runtime"
        langgraph_root = runtime_root / "langgraph"
        paths = ChangePaths(
            project_root=project,
            change_root=change,
            staging_root=change / ".staging",
            runtime_root=runtime_root,
            generated_root=change / "generated",
            apply_manifest=change / "apply-manifest.json",
            langgraph_root=langgraph_root,
            langgraph_checkpoints=langgraph_root / "checkpoints.sqlite3",
            langgraph_leases=langgraph_root / "leases",
            langgraph_selections=langgraph_root / "selections",
        )
        return cls(paths)

    @classmethod
    def prepare(cls, project_root: Path, change_id: str) -> ChangeWorkspace:
        project = require_real_directory(project_root)
        resolved_id = safe_change_id(change_id)
        qa = project / "qa"
        changes = qa / "changes"
        change = changes / resolved_id
        created: list[Path] = []
        try:
            _ensure_real_directory(qa, created)
            _ensure_real_directory(changes, created)
            _ensure_real_directory(change, created)
            workspace = cls.open(project, resolved_id)
            workspace.initialize()
        except Exception:
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise
        return workspace

    def initialize(self) -> None:
        runtime = self.paths.runtime_root
        staging = self.paths.staging_root
        required_runtime = {"activities", "receipts"}
        allowed_runtime = required_runtime | {"invocations", "ledger", "langgraph"}
        runtime_exists = runtime.exists()
        staging_exists = staging.exists()
        if runtime_exists != staging_exists:
            raise ValueError("change workspace has a partial initialization")
        if runtime_exists:
            if runtime.is_symlink() or staging.is_symlink():
                raise ValueError("change workspace contains a symlink")
            if not runtime.is_dir() or not staging.is_dir():
                raise ValueError("change workspace contains a non-directory path")
            children = {child.name for child in runtime.iterdir()}
            if not required_runtime.issubset(children) or not children.issubset(allowed_runtime):
                raise ValueError("runtime directory has an incomplete layout")
            if any((runtime / name).is_symlink() or not (runtime / name).is_dir() for name in children):
                raise ValueError("runtime directory contains a non-directory path")
            if "langgraph" in children:
                _validate_langgraph_subtree(runtime / "langgraph")
            return

        created: list[Path] = []

        def create_directory(path: Path) -> None:
            _mkdir(path)
            created.append(path)

        try:
            create_directory(runtime)
            for name in ("activities", "receipts"):
                create_directory(runtime / name)
            create_directory(staging)
        except OSError as exc:
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise ValueError("could not initialize change workspace") from exc

    def runtime_binding(self) -> InvocationWorkspaceBinding:
        return InvocationWorkspaceBinding(
            project_root=self.paths.project_root,
            attempts_root=self.paths.staging_root,
            receipts_root=self.paths.runtime_root / "receipts",
        )

    def output_route(self, capability_alias: str) -> tuple[str, ...]:
        from assurance_product.output_routes import OutputRouteCatalog

        return OutputRouteCatalog().outputs(capability_alias, self.paths.change_root.name)
