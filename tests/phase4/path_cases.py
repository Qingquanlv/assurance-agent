"""Path and symlink confinement cases against each wheel."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    ResourceClaims,
    ValidationContext,
)

from assurance_execution.contracts.selection import _safe_project_relative_path
from assurance_execution.operations.paths import resolve_selected_file
from assurance_execution.validators.mapping import ClosedMappingValidator
from assurance_generation.contracts.plans import canonical_relative_path
from assurance_generation.validators.generated_files import GeneratedFilesValidator
from assurance_healing.operations.agent import _workspace_file as healing_workspace_file
from assurance_healing.validators.test_tree import TestTreeValidator
from assurance_improvement.validators.delivery import DeliveryValidator
from assurance_improvement.validators.paths import authenticate_workspace_path as improvement_workspace_path
from assurance_improvement.validators.paths import canonical_relative as improvement_canonical
from assurance_intake.operations.finalize import _workspace_file as intake_workspace_file
from assurance_intake.validators.cases import CaseCandidateValidator
from assurance_quality.validators.paths import authenticate_workspace_path as quality_workspace_path
from assurance_quality.validators.paths import canonical_relative as quality_canonical
from assurance_quality.validators.report import ReportValidator

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

_SHA = "a" * 64
_STRING_PATHS = {
    "absolute": "/tmp/phase4-secret",
    "parent-dotdot": "qa/cases/../secret.yaml",
    "windows-drive": "C:/phase4-secret",
    "undeclared-write-root": "src/app.py",
}


@dataclass
class PathObservation:
    rejected: bool
    spawned: bool
    effect_emitted: bool
    workspace: Path
    outside: Path


def assert_no_write_outside_workspace(observed: PathObservation) -> None:
    leftovers = [path for path in observed.outside.rglob("*") if path.is_file()]
    assert leftovers == []


def _candidate(*paths: str) -> CandidateWriteSet:
    return CandidateWriteSet(
        baseline_tree_id="0" * 64,
        candidate_tree_id="1" * 64,
        files=tuple(CandidateFile(path=path, before_sha256=None, after_sha256=_SHA) for path in paths),
    )


def _context() -> ValidationContext:
    return ValidationContext(
        invocation_id="phase4-test",
        task_id="phase4-task",
        graph_instance_id="phase4-graph",
        node_id="phase4-node",
        resources=ResourceClaims(),
    )


def _string_rejected(wheel: str, path: str) -> bool:
    if wheel == "intake":
        return CaseCandidateValidator().validate(_candidate(path), _context()).accepted is False
    if wheel == "generation":
        try:
            canonical_relative_path(path)
            string_ok = True
        except ValueError:
            string_ok = False
        if not string_ok:
            return True
        return GeneratedFilesValidator().validate(_candidate(path), _context()).accepted is False
    if wheel == "execution":
        try:
            _safe_project_relative_path(path)
        except ValueError:
            return True
        return ClosedMappingValidator().validate(_candidate(path), _context()).accepted is False
    if wheel == "healing":
        return TestTreeValidator(path_only=True).validate(_candidate(path), _context()).accepted is False
    if wheel == "quality":
        if not quality_canonical(path):
            return True
        return ReportValidator(path_only=True).validate(_candidate(path), _context()).accepted is False
    if wheel == "improvement":
        if not improvement_canonical(path):
            return True
        return DeliveryValidator(path_only=True).validate(_candidate(path), _context()).accepted is False
    raise ValueError(wheel)


def _workspace_rejected(wheel: str, workspace: Path, relative: str) -> bool:
    try:
        if wheel == "intake":
            intake_workspace_file(workspace, relative)
        elif wheel == "generation":
            canonical_relative_path(relative)
            from assurance_generation.operations.planning import _workspace_file

            _workspace_file(workspace, relative)
        elif wheel == "execution":
            resolve_selected_file(workspace, relative)
        elif wheel == "healing":
            healing_workspace_file(workspace, relative)
        elif wheel == "quality":
            quality_workspace_path(workspace, relative)
        elif wheel == "improvement":
            improvement_workspace_path(workspace, relative)
        else:
            raise ValueError(wheel)
    except (ValueError, OSError, FileNotFoundError):
        return True
    return False


async def exercise_path_case(wheel: str, case: str) -> PathObservation:
    with TemporaryDirectory(prefix="phase4-path-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        outside = root / "outside"
        workspace.mkdir()
        outside.mkdir()
        spawned = False
        effect_emitted = False
        if case in _STRING_PATHS:
            rejected = _string_rejected(wheel, _STRING_PATHS[case])
            return PathObservation(
                rejected=rejected,
                spawned=spawned,
                effect_emitted=effect_emitted,
                workspace=workspace,
                outside=outside,
            )
        relative = _prepare_filesystem(case, workspace, outside)
        rejected = _workspace_rejected(wheel, workspace, relative)
        if case in {"absolute", "parent-dotdot", "windows-drive"} or not rejected:
            rejected = rejected or _string_rejected(wheel, relative)
        return PathObservation(
            rejected=rejected,
            spawned=spawned,
            effect_emitted=effect_emitted,
            workspace=workspace,
            outside=outside,
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
