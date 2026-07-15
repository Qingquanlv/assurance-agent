"""Path / change-id safety helpers for the risk (Explore) package.

Change-id validation delegates to the M2-wide identifier contract; resolved
risk paths additionally must stay inside the project root.
"""

from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe as _assert_id


class RiskSafetyError(AaError):
    pass


def assert_change_id_safe(change_id: str) -> None:
    try:
        _assert_id(change_id)
    except UnsafeIdentifierError as err:
        raise RiskSafetyError(str(err)) from err


def assert_inside_project(project_root: Path, target: Path) -> None:
    root = project_root.resolve()
    resolved = target.resolve()
    if root != resolved and root not in resolved.parents:
        raise RiskSafetyError(f"Path escapes project root: {target}")


def resolve_inside_project(project_root: Path, *segments: str) -> Path:
    joined = project_root.joinpath(*segments)
    assert_inside_project(project_root, joined)
    return joined


def resolve_requirement_path(project_root: Path, requirement_path: str) -> Path:
    candidate = Path(requirement_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
    assert_inside_project(project_root, resolved)
    return resolved
