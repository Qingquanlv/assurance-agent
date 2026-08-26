from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, rebind_agent_run_workspace
from agent_runtime_contracts.schema import canonical_digest, reject_credentials_in_digest_input, thaw_json
from graph_engine import TaskActivityProtocolViolation
from graph_engine.plugin_api import (
    JSONValue,
    SecretHandleUnauthorized,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from agent_runtime_cursor.config import AdapterConfigurationError, CursorAdapterConfig
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
    production_process_host,
    workspace_identity_digest_for,
)
from agent_runtime_cursor.redaction import failure_message, stderr_projection


class CursorDispatchIncomplete(ValueError):
    """Raised when execute cannot prove a typed terminal outcome."""


class CursorHandler:
    def __init__(self, host: ConfinedProcessHost | None = None) -> None:
        self._host = host
        self.dispatch_fingerprint: dict[str, Any] = {}

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            config = CursorAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return self._configuration_outcome(str(error))
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        try:
            result = await self._reconcile_process(request, context, config, port.snapshot, allow_spawn=True)
        except CursorProtocolError as error:
            raise CursorDispatchIncomplete(self._redacted_reason(error, context, config)) from error
        if isinstance(result, TaskOutcome):
            return result
        if result.status == "terminal" and result.outcome is not None:
            return result.outcome
        raise CursorDispatchIncomplete(result.reason or result.status)

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        try:
            config = CursorAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return self._indeterminate(str(error))
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return self._indeterminate("activity snapshot does not match the live port")
        try:
            result = await self._reconcile_process(
                request,
                context,
                config,
                activity,
                allow_spawn=False,
            )
            if isinstance(result, TaskOutcome):
                return TaskActivityReconcileResult(status="terminal", outcome=result)
            return result
        except (
            CursorProtocolError,
            TaskActivityProtocolViolation,
            ValidationError,
            ValueError,
        ) as error:
            return self._indeterminate(self._redacted_reason(error, context, config))

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        try:
            config = CursorAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return TaskActivityCancelResult(status="indeterminate", reason=str(error))
        host = self._require_host(context)
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason="activity snapshot does not match the live port",
            )
        try:
            agent_run = self._effective_agent_run(request, context)
            receipt = CursorProcessReceipt.model_validate(thaw_json(activity.reference))
            host.authenticate(receipt)
            if receipt.workspace_identity_digest != workspace_identity_digest_for(context):
                return TaskActivityCancelResult(
                    status="indeterminate",
                    reason="workspace identity drifted",
                )
        except (ValidationError, TaskActivityProtocolViolation, ValueError) as error:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason=self._redacted_reason(error, context, config) or "process receipt is not authentic",
            )
        policy = CancelPolicy(
            graceful_seconds=config.graceful_cancel_seconds,
            forced_seconds=config.forced_cancel_seconds,
        )
        await host.terminate(receipt, policy)
        try:
            observation = await host.observe(receipt)
        except TaskActivityProtocolViolation as error:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason=self._redacted_reason(error, context, config),
            )
        if observation.status == "unknown":
            return TaskActivityCancelResult(
                status="indeterminate",
                reason="unknown process state",
            )
        if observation.status != "exited" or observation.exit_code is None:
            return TaskActivityCancelResult(status="acknowledged")
        terminal = await host.wait(receipt)
        try:
            outcome = self._outcome_from_terminal(
                request,
                context,
                config,
                receipt,
                terminal,
                agent_run=agent_run,
            )
        except (CursorProtocolError, ValidationError, ValueError):
            return TaskActivityCancelResult(
                status="indeterminate",
                reason="incomplete terminal after work started",
            )
        return TaskActivityCancelResult(status="terminal", outcome=outcome)

    async def _reconcile_process(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: CursorAdapterConfig,
        activity: TaskActivitySnapshot,
        *,
        allow_spawn: bool,
    ) -> TaskActivityReconcileResult | TaskOutcome:
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        snapshot = port.snapshot
        agent_run = self._effective_agent_run(request, context)
        mismatch = self._identity_mismatch(request, context, snapshot, agent_run)
        if mismatch is not None:
            if allow_spawn:
                raise ValueError(mismatch)
            return self._indeterminate(mismatch)
        if snapshot.reference is not None:
            return await self._reconcile_bound(
                request, context, config, agent_run, snapshot, allow_spawn=allow_spawn
            )
        if snapshot.dispatch_fingerprint is not None:
            fingerprint = thaw_json(snapshot.dispatch_fingerprint)
            if not isinstance(fingerprint, dict):
                return self._blocked("dispatch fingerprint is not authentic", allow_spawn)
            state = self._require_host(context).unbound_spawn_state(fingerprint)
            if state != "not_spawned":
                return self._blocked("spawn may have occurred", allow_spawn)
            if not allow_spawn:
                return TaskActivityReconcileResult(status="not_dispatched")
            return await self._dispatch(request, context, config, agent_run)
        if not allow_spawn:
            return TaskActivityReconcileResult(status="not_dispatched")
        return await self._dispatch(request, context, config, agent_run)

    async def _dispatch(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: CursorAdapterConfig,
        agent_run: AgentRunRequest,
    ) -> TaskActivityReconcileResult:
        host = self._require_host(context)
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        executable = authenticate_executable(config)
        launch = build_launch_request(config, agent_run, context, executable)
        identity = host.preflight(launch)
        authenticate_confinement(identity, expected_version=config.expected_version)
        fingerprint = cursor_dispatch_fingerprint(
            config,
            launch.argv,
            request_digest=launch.request_digest,
            workspace_identity_digest=launch.workspace_identity_digest,
            host_boot_identity_digest=identity.host_boot_identity_digest,
            host_instance_id=identity.host_instance_id,
            attempt=request.attempt,
            task_id=request.task_id,
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
        outcome = self._outcome_from_terminal(
            request,
            context,
            config,
            process.receipt,
            terminal,
            agent_run=agent_run,
        )
        return TaskActivityReconcileResult(
            status="terminal",
            reference=cast(JSONValue, receipt_payload),
            outcome=outcome,
        )

    async def _reconcile_bound(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: CursorAdapterConfig,
        agent_run: AgentRunRequest,
        snapshot: TaskActivitySnapshot,
        *,
        allow_spawn: bool,
    ) -> TaskActivityReconcileResult:
        host = self._require_host(context)
        try:
            receipt = CursorProcessReceipt.model_validate(thaw_json(snapshot.reference))
            host.authenticate(receipt)
        except (ValidationError, TaskActivityProtocolViolation, ValueError) as error:
            return self._blocked(
                self._redacted_reason(error, context, config) or "process receipt does not match this host",
                allow_spawn,
            )
        if receipt.request_digest != canonical_digest(agent_run.model_dump(mode="json")):
            return self._blocked("request identity drifted", allow_spawn)
        if receipt.workspace_identity_digest != workspace_identity_digest_for(context):
            return self._blocked("workspace identity drifted", allow_spawn)
        durable = host.read_durable_terminal(receipt)
        if durable is not None:
            return self._promote_or_block(
                request,
                context,
                config,
                receipt,
                durable,
                snapshot,
                allow_spawn,
                agent_run=agent_run,
            )
        try:
            observation = await host.observe(receipt)
        except TaskActivityProtocolViolation as error:
            return self._blocked(self._redacted_reason(error, context, config), allow_spawn)
        if observation.status == "running":
            return TaskActivityReconcileResult(
                status="running",
                reference=snapshot.reference,
            )
        if observation.status != "exited" or observation.exit_code is None:
            return self._blocked("unknown process state", allow_spawn)
        terminal = await host.wait(receipt)
        return self._promote_or_block(
            request,
            context,
            config,
            receipt,
            terminal,
            snapshot,
            allow_spawn,
            agent_run=agent_run,
        )

    def _promote_or_block(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: CursorAdapterConfig,
        receipt: CursorProcessReceipt,
        terminal: HostTerminalResult,
        snapshot: TaskActivitySnapshot,
        allow_spawn: bool,
        *,
        agent_run: AgentRunRequest,
    ) -> TaskActivityReconcileResult:
        try:
            outcome = self._outcome_from_terminal(
                request,
                context,
                config,
                receipt,
                terminal,
                agent_run=agent_run,
            )
        except CursorProtocolError as error:
            return self._blocked(self._redacted_reason(error, context, config), allow_spawn)
        return TaskActivityReconcileResult(
            status="terminal",
            reference=snapshot.reference,
            outcome=outcome,
        )

    def _outcome_from_terminal(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: CursorAdapterConfig,
        receipt: CursorProcessReceipt,
        terminal: HostTerminalResult,
        *,
        agent_run: AgentRunRequest,
    ) -> TaskOutcome:
        canaries = self._canaries(context, config)
        _ = stderr_projection(terminal.stderr, canaries=canaries)
        parsed = parse_stream(
            self._stream_input(terminal, cwd=str(context.project_root.resolve()), config=config),
            self._limits(config, agent_run),
        )
        return reduce_terminal(
            parsed,
            receipt=receipt,
            agent_run=agent_run,
            request=request,
            canaries=canaries,
        )

    def _identity_mismatch(
        self,
        request: TaskRequest,
        context: TaskContext,
        snapshot: TaskActivitySnapshot,
        agent_run: AgentRunRequest,
    ) -> str | None:
        if context.workspace_identity.identity_digest != snapshot.workspace_identity.identity_digest:
            return "workspace identity drifted"
        fingerprint = thaw_json(snapshot.dispatch_fingerprint) if snapshot.dispatch_fingerprint else None
        if isinstance(fingerprint, dict):
            expected_identity = canonical_digest({"attempt": request.attempt, "task_id": request.task_id})
            if fingerprint.get("request_identity_digest") not in {None, expected_identity}:
                return "request identity drifted"
            if fingerprint.get("request_digest") not in {
                None,
                canonical_digest(agent_run.model_dump(mode="json")),
            }:
                return "request identity drifted"
            expected_workspace = workspace_identity_digest_for(context)
            if fingerprint.get("workspace_identity_digest") not in {None, expected_workspace}:
                return "workspace identity drifted"
        return None

    def _blocked(self, reason: str, allow_spawn: bool) -> TaskActivityReconcileResult:
        if allow_spawn:
            raise CursorDispatchIncomplete(reason)
        return self._indeterminate(reason)

    def _indeterminate(self, reason: str) -> TaskActivityReconcileResult:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=reason or "process observation is indeterminate",
        )

    def _canaries(self, context: TaskContext, config: CursorAdapterConfig) -> tuple[str, ...]:
        if config.secret_handle is None or context.secrets is None:
            return ()
        try:
            return (context.secrets.resolve(config.secret_handle).decode("utf-8"),)
        except SecretHandleUnauthorized:
            return ()

    def _redacted_reason(
        self,
        error: BaseException,
        context: TaskContext,
        config: CursorAdapterConfig,
    ) -> str:
        return failure_message(
            str(error) or "process observation is indeterminate", canaries=self._canaries(context, config)
        )

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

    def _require_host(self, context: TaskContext) -> ConfinedProcessHost:
        if self._host is not None:
            return self._host
        return production_process_host(context.write_root.parent)

    @staticmethod
    def _effective_agent_run(request: TaskRequest, context: TaskContext) -> AgentRunRequest:
        return rebind_agent_run_workspace(
            AgentRunRequest.model_validate(thaw_json(request.input)),
            project_root=context.project_root,
            write_root=context.write_root,
        )

    @staticmethod
    def _configuration_outcome(message: str) -> TaskOutcome:
        return TaskOutcome.failed("configuration", message, retryable=False)
