from __future__ import annotations

from pathlib import Path

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


__all__ = ["rebind_agent_run_workspace"]
