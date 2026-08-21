from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.schema import reject_credentials_in_digest_input, thaw_json
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import (
    JSONValue,
    TaskActivityCancelResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from agent_runtime_cursor.config import CursorAdapterConfig
from agent_runtime_cursor.parser import (
    CursorProtocolError,
    StreamInput,
    StreamLimits,
    parse_stream,
    reduce_terminal,
)
from agent_runtime_cursor.process import (
    CancelPolicy,
    ConfinedProcessHost,
    CursorProcessReceipt,
    HostTerminalResult,
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
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        executable = authenticate_executable(config)
        agent_run = AgentRunRequest.model_validate(thaw_json(request.input))
        launch = build_launch_request(config, agent_run, context, executable)
        identity = host.preflight(launch)
        authenticate_confinement(identity, expected_version=config.expected_version)
        fingerprint = cursor_dispatch_fingerprint(
            config,
            launch.argv,
            request_digest=launch.request_digest,
            workspace_identity_digest=launch.workspace_identity_digest,
        )
        reject_credentials_in_digest_input(fingerprint)
        self.dispatch_fingerprint = fingerprint
        port.mark_dispatch_started(cast(JSONValue, fingerprint))
        process = await host.spawn(launch)
        receipt_payload = process.receipt.model_dump(mode="json")
        reject_credentials_in_digest_input(receipt_payload)
        port.bind(cast(JSONValue, receipt_payload))
        context.heartbeat()
        terminal = await host.wait(process.receipt)
        parsed = parse_stream(
            self._stream_input(terminal, cwd=str(launch.cwd), config=config),
            self._limits(config, agent_run),
        )
        return reduce_terminal(
            parsed,
            receipt=process.receipt,
            agent_run=agent_run,
            request=request,
        )

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        config = self._require_config()
        host = self._require_host()
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason="activity snapshot does not match the live port",
            )
        try:
            receipt = CursorProcessReceipt.model_validate(thaw_json(activity.reference))
            host.authenticate(receipt)
        except (ValidationError, TaskActivityProtocolViolation, ValueError) as error:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason=str(error) or "process receipt is not authentic",
            )
        policy = CancelPolicy(
            graceful_seconds=config.graceful_cancel_seconds,
            forced_seconds=config.forced_cancel_seconds,
        )
        await host.terminate(receipt, policy)
        observation = await host.observe(receipt)
        if observation.status != "exited" or observation.exit_code is None:
            return TaskActivityCancelResult(status="acknowledged")
        terminal = await host.wait(receipt)
        try:
            agent_run = AgentRunRequest.model_validate(thaw_json(request.input))
            parsed = parse_stream(
                self._stream_input(terminal, cwd=str(context.workspace_root.resolve()), config=config),
                self._limits(config, agent_run),
            )
            outcome = reduce_terminal(
                parsed,
                receipt=receipt,
                agent_run=agent_run,
                request=request,
            )
        except (CursorProtocolError, ValidationError, ValueError):
            return TaskActivityCancelResult(status="acknowledged")
        return TaskActivityCancelResult(status="terminal", outcome=outcome)

    def _stream_input(
        self,
        terminal: HostTerminalResult,
        *,
        cwd: str,
        config: CursorAdapterConfig,
    ) -> StreamInput:
        return StreamInput(
            stdout=terminal.stdout,
            stderr=terminal.stderr,
            exit_code=terminal.exit_code,
            elapsed_seconds=terminal.elapsed_seconds,
            expected_cwd=cwd,
            expected_version=config.expected_version,
        )

    def _limits(self, config: CursorAdapterConfig, agent_run: AgentRunRequest) -> StreamLimits:
        return StreamLimits(
            max_output_bytes=config.max_output_bytes,
            max_line_bytes=config.max_line_bytes,
            max_record_count=4096,
            max_json_nesting=32,
            max_stderr_bytes=config.max_output_bytes,
            max_elapsed_seconds=float(agent_run.execution.limits.max_seconds),
        )

    def _require_config(self) -> CursorAdapterConfig:
        if self._config is None:
            raise ValueError("Cursor adapter config is required")
        return self._config

    def _require_host(self) -> ConfinedProcessHost:
        if self._host is None:
            raise ValueError("confined process host is required")
        return self._host
