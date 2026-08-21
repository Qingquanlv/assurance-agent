from __future__ import annotations

from typing import Any

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import reject_credentials_in_digest_input, thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_cursor.process import (
    ConfinedProcessHost,
    authenticate_confinement,
    authenticate_executable,
    build_launch_request,
    cursor_dispatch_fingerprint,
)


class CursorHandler:
    def __init__(
        self,
        config: CursorAdapterConfig | None = None,
        host: ConfinedProcessHost | None = None,
    ) -> None:
        self._config = config
        self._host = host
        self.dispatch_fingerprint: dict[str, Any] = {}

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        config = self._require_config()
        host = self._require_host()
        executable = authenticate_executable(config)
        agent_run = AgentRunRequest.model_validate(thaw_json(request.input))
        launch = build_launch_request(config, agent_run, context, executable)
        identity = host.preflight(launch)
        authenticate_confinement(identity, expected_version=config.expected_version)
        fingerprint = cursor_dispatch_fingerprint(config, launch.argv)
        reject_credentials_in_digest_input(fingerprint)
        self.dispatch_fingerprint = fingerprint
        await host.spawn(launch)
        return TaskOutcome.succeeded({"schema_version": "1", "dispatch": "spawned"})

    def _require_config(self) -> CursorAdapterConfig:
        if self._config is None:
            raise ValueError("Cursor adapter config is required")
        return self._config

    def _require_host(self) -> ConfinedProcessHost:
        if self._host is None:
            raise ValueError("confined process host is required")
        return self._host
