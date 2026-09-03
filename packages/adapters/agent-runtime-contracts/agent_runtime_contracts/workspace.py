from __future__ import annotations

from pathlib import Path, PurePosixPath

from agent_runtime_contracts.models import AgentRunRequest, AgentWorkspaceV1
from agent_runtime_contracts.schema import canonical_digest


def rebind_agent_run_workspace(
    agent_run: AgentRunRequest,
    *,
    project_root: Path,
    write_root: Path,
) -> AgentRunRequest:
    """Bind provider-visible workspace identity to the current task workspace."""
    project = project_root.resolve()
    current_write_root = write_root.resolve()
    try:
        relative_write_root = current_write_root.relative_to(project).as_posix()
    except ValueError as error:
        raise ValueError("write_root must be inside project_root") from error

    workspace_payload = agent_run.workspace.model_dump(mode="json", exclude={"identity_digest"})
    workspace_payload["write_root"] = relative_write_root
    workspace = AgentWorkspaceV1.model_validate(
        {
            **workspace_payload,
            "identity_digest": canonical_digest(workspace_payload),
        }
    )
    return AgentRunRequest.model_validate(
        {
            **agent_run.model_dump(mode="json"),
            "workspace": workspace.model_dump(mode="json"),
        }
    )


class AuthenticatedStagingWriter:
    """Write only declared staging paths under the authenticated write root."""

    def __init__(self, write_root: Path, claims: tuple[str, ...]) -> None:
        self._root = write_root
        self._claims = frozenset(claims)

    def write_bytes(self, relative: str, data: bytes) -> Path:
        if relative not in self._claims:
            raise ValueError(f"undeclared staging write: {relative}")
        path = self._root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"staging write path is a symlink: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


__all__ = ["AuthenticatedStagingWriter", "rebind_agent_run_workspace"]
