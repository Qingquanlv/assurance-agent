"""Project overlay files for workflow schema and execution contracts.

Implicit discovery already prefers ``.aa/`` then ``schemas/``. Explicit CLI
``--schema`` / ``--contracts`` are exclusive: missing files fail closed, and
paths must resolve under those two directories.
"""

from __future__ import annotations

from pathlib import Path

from assurance_agent.exceptions import AaError

_OVERLAY_DIRS = (".aa", "schemas")


class OverlayPathError(AaError):
    """Explicit overlay path is missing or outside the allowed project dirs."""


def resolve_explicit_overlay(project_root: Path, explicit: Path) -> Path:
    """Resolve an exclusive overlay path under ``.aa/`` or ``schemas/``."""
    path = explicit if explicit.is_absolute() else project_root / explicit
    if not path.is_file():
        raise OverlayPathError(f"explicit overlay not found: {path}")
    resolved = path.resolve()
    root = project_root.resolve()
    allowed = tuple((root / name).resolve() for name in _OVERLAY_DIRS)
    if not any(_is_under(resolved, directory) for directory in allowed):
        raise OverlayPathError(f"explicit overlay must be under .aa/ or schemas/: {path}")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True
