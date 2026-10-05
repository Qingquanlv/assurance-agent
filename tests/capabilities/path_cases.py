"""Path and symlink confinement cases against each wheel."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
)
from graph_engine.artifacts import ArtifactReadError, is_canonical_relative, read_workspace_file, under_root

from assurance_generation.contracts.mapping import _safe_project_relative_path
from assurance_execution.operations.paths import resolve_selected_file
from assurance_generation.contracts.plans import canonical_relative_path
from assurance_healing.operations.agent import workspace_file as healing_workspace_file
from assurance_improvement.operations.agent import _workspace_file as improvement_workspace_file
from agent_runtime_contracts.ops import ArtifactListResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.operations.assessment import _read_ref as quality_workspace_file

_LOCKED = ("qa/cases", "qa/results", "qa/tests")

PATH_CASES = (
    "absolute",
    "parent-dotdot",
    "windows-drive",
    "symlink-file",
    "symlink-parent",
    "hard-link",
    "path-swap",
    "undeclared-write-root",
)
WHEELS = (
    "intake",
    "generation",
    "execution",
    "healing",
    "quality",
    "improvement",
)
STRING_CASES = frozenset({"absolute", "parent-dotdot", "windows-drive", "undeclared-write-root"})
FILESYSTEM_CASES = frozenset({"symlink-file", "symlink-parent", "hard-link", "path-swap"})

_SHA = "a" * 64
_STRING_PATHS = {
    "absolute": "/tmp/capabilities-secret",
    "parent-dotdot": "qa/cases/../secret.yaml",
    "windows-drive": "C:/capabilities-secret",
    "undeclared-write-root": "src/app.py",
}
_INTAKE_LOCK = ("qa/.qa.yaml", "qa/cases", "qa/proposal.md", "qa/requirement.md", "qa/results")


@dataclass
class PathProceedHook:
    """Spawn/effect fire only after a production validator or open helper accepts."""

    spawned: bool = False
    effect_emitted: bool = False

    def on_production_accept(self) -> None:
        self.spawned = True
        self.effect_emitted = True


@dataclass
class PathObservation:
    rejected: bool
    spawned: bool
    effect_emitted: bool
    workspace: Path
    outside: Path
    outside_before: frozenset[str]
    seam: str
    _lifetime: TemporaryDirectory | None = field(default=None, repr=False, compare=False)


def assert_no_write_outside_workspace(observed: PathObservation) -> None:
    after = {
        path.relative_to(observed.outside).as_posix()
        for path in observed.outside.rglob("*")
        if path.is_file()
    }
    leftovers = after - observed.outside_before
    assert leftovers == set()


def _candidate(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def _context() -> ValidationContext:
    return ValidationContext(
        invocation_id="capabilities-test",
        task_id="capabilities-task",
        graph_instance_id="capabilities-graph",
        node_id="capabilities-node",
        resources=ResourceClaims(),
    )


def _file_snapshot(root: Path) -> frozenset[str]:
    if not root.exists():
        return frozenset()
    return frozenset(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _string_rejected(wheel: str, path: str, hook: PathProceedHook) -> bool:
    if wheel == "intake":
        try:
            ArtifactListResultV1(output_files=(path,))
        except ValueError:
            return True
        accepted = under_root(path, _INTAKE_LOCK)
    elif wheel == "generation":
        try:
            canonical_relative_path(path)
        except ValueError:
            return True
        accepted = under_root(path, _LOCKED)
    elif wheel == "execution":
        try:
            _safe_project_relative_path(path)
        except ValueError:
            return True
        accepted = under_root(path, _LOCKED)
    elif wheel == "healing":
        if not is_canonical_relative(path):
            return True
        accepted = under_root(path, _LOCKED)
    elif wheel == "quality":
        if not is_canonical_relative(path):
            return True
        accepted = under_root(path, _LOCKED)
    elif wheel == "improvement":
        if not is_canonical_relative(path):
            return True
        accepted = under_root(path, _LOCKED)
    else:
        raise ValueError(wheel)
    if accepted:
        hook.on_production_accept()
    return accepted is False


def _workspace_rejected(wheel: str, workspace: Path, relative: str, hook: PathProceedHook) -> bool:
    try:
        if wheel == "intake":
            read_workspace_file(workspace, relative)
        elif wheel == "generation":
            canonical_relative_path(relative)
            from assurance_generation.operations.planning import _workspace_file

            _workspace_file(workspace, relative)
        elif wheel == "execution":
            resolve_selected_file(workspace, relative)
        elif wheel == "healing":
            healing_workspace_file(workspace, relative)
        elif wheel == "improvement":
            improvement_workspace_file(workspace, relative)
        elif wheel == "quality":
            quality_workspace_file(
                workspace,
                EvidenceArtifactRefV1(path=relative, digest=_SHA),
            )
        else:
            raise ValueError(wheel)
    except (ValueError, OSError, FileNotFoundError, ArtifactReadError):
        return True
    hook.on_production_accept()
    return False


async def exercise_path_case(wheel: str, case: str) -> PathObservation:
    lifetime = TemporaryDirectory(prefix="capabilities-path-")
    root = Path(lifetime.name)
    workspace = root / "workspace"
    outside = root / "outside"
    workspace.mkdir()
    outside.mkdir()
    hook = PathProceedHook()
    if case in _STRING_PATHS:
        outside_before = _file_snapshot(outside)
        rejected = _string_rejected(wheel, _STRING_PATHS[case], hook)
        return PathObservation(
            rejected=rejected,
            spawned=hook.spawned,
            effect_emitted=hook.effect_emitted,
            workspace=workspace,
            outside=outside,
            outside_before=outside_before,
            seam="validator",
            _lifetime=lifetime,
        )
    relative = _prepare_filesystem(case, workspace, outside)
    outside_before = _file_snapshot(outside)
    rejected = _workspace_rejected(wheel, workspace, relative, hook)
    return PathObservation(
        rejected=rejected,
        spawned=hook.spawned,
        effect_emitted=hook.effect_emitted,
        workspace=workspace,
        outside=outside,
        outside_before=outside_before,
        seam="workspace-open",
        _lifetime=lifetime,
    )


def _prepare_filesystem(case: str, workspace: Path, outside: Path) -> str:
    if case == "symlink-file":
        target = outside / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        link = workspace / "qa" / "cases" / "out.yaml"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        return "qa/cases/out.yaml"
    if case == "symlink-parent":
        target = outside / "escaped"
        target.mkdir()
        (target / "out.yaml").write_text("secret", encoding="utf-8")
        parent = workspace / "qa"
        parent.mkdir()
        (parent / "cases").symlink_to(target)
        return "qa/cases/out.yaml"
    if case == "hard-link":
        inside = workspace / "qa" / "cases" / "out.yaml"
        inside.parent.mkdir(parents=True)
        inside.write_text("inside", encoding="utf-8")
        outside_link = outside / "hard-out.yaml"
        outside_link.hardlink_to(inside)
        return "qa/cases/out.yaml"
    if case == "path-swap":
        inside = workspace / "qa" / "cases" / "out.yaml"
        inside.parent.mkdir(parents=True)
        inside.write_text("inside", encoding="utf-8")
        target = outside / "swapped.txt"
        target.write_text("secret", encoding="utf-8")
        inside.unlink()
        inside.symlink_to(target)
        return "qa/cases/out.yaml"
    raise ValueError(case)
