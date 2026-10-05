"""Workspace checks shared by healing agent finalize hooks."""

from __future__ import annotations

from pathlib import Path

from agent_runtime_contracts.ops import OutputError
from graph_engine.artifacts import ArtifactReadError, read_workspace_file


def workspace_file(workspace: Path, relative: str) -> bytes:
    try:
        return read_workspace_file(workspace, relative)
    except ArtifactReadError as error:
        raise OutputError(str(error)) from error
