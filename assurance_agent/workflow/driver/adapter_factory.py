"""Click-free adapter construction shared by workflow and Retro commands."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from assurance_agent.workflow.driver.adapter import AgentInvoker, DriverError
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.opencode_adapter import (
    OpenCodeAdapter,
    auth_headers_from_env,
)

DEFAULT_CURSOR_MODEL = "cursor-grok-4.5-high-fast"


def resolve_headless_model(model: str | None, *, env: Mapping[str, str]) -> str:
    """Prefer explicit model, then Cursor env aliases, then the stable default."""
    if model is not None and model.strip():
        return model.strip()
    for key in ("AA_CURSOR_MODEL", "CURSOR_MODEL"):
        value = env.get(key, "").strip()
        if value:
            return value
    return DEFAULT_CURSOR_MODEL


def build_adapter(
    adapter_name: str,
    project_root: Path,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
) -> AgentInvoker:
    """Build an adapter without Click/SystemExit side effects."""
    if adapter_name == "opencode":
        if not server:
            raise DriverError("--server is required for --adapter opencode")
        return OpenCodeAdapter(
            server=server,
            directory=directory or str(project_root),
            model=model,
            parent_session=parent_session,
            auth_headers=auth_headers_from_env(),
        )
    if adapter_name == "headless":
        return HeadlessAdapter(
            agent_cmd=agent_cmd,
            cwd=project_root,
            model=resolve_headless_model(model, env=os.environ),
        )
    raise DriverError(f"unsupported adapter: {adapter_name}")


__all__ = ["DEFAULT_CURSOR_MODEL", "build_adapter", "resolve_headless_model"]
