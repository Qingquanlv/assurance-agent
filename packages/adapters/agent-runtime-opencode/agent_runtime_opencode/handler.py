from __future__ import annotations

import asyncio
import time
from typing import Any

from agent_runtime_contracts import AgentRunRequest
from graph_engine.plugin_api import (
    SecretHandleUnauthorized,
    TaskActivityCancelResult,
    TaskActivityPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from agent_runtime_opencode.config import AdapterConfigurationError, OpenCodeAdapterConfig
from agent_runtime_opencode.observe.poll import _observe_bound, _observe_fingerprint
from agent_runtime_opencode.prompt import _admit_prompt
from agent_runtime_opencode.security import _secret_canaries
from agent_runtime_opencode.session.binding import (
    _create_and_bind,
    _load_bound_session,
    _verify_workspace_binding,
    activity_label_for_session,
)
from agent_runtime_opencode.session.discovery import (
    OpenCodeDispatchIncomplete,
    _bind_match,
    _list_sessions,
    adapter_source_digest,
    discovery_metadata,
    exact_metadata_matches,
)
from agent_runtime_opencode.session.identity import (
    _dispatch_fingerprint,
    _effective_agent_run,
    _expected_reference_fields,
    _identity_mismatch,
)
from agent_runtime_opencode.transport.connection import PROVIDER_ERRORS, open_client
from agent_runtime_opencode.transport.http import OpenCodeHttpClient


def _activity_is_bound(context: TaskContext) -> bool:
    port = context.activity
    if port is None:
        return False
    snapshot = port.snapshot
    return snapshot.reference is not None or snapshot.state == "bound"


class OpenCodeHandler:
    async def preflight(self, request: TaskRequest, context: TaskContext) -> dict[str, Any]:
        config = OpenCodeAdapterConfig.from_request(request)
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        async with open_client(config, context) as connection:
            return await _observe_fingerprint(connection.client, config, connection.secret)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            config = OpenCodeAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return self._configuration_outcome(str(error))
        deadline = time.monotonic() + config.observation_horizon_seconds
        while True:
            result = await self._reconcile_session(request, context, config, allow_create=True)
            if isinstance(result, TaskOutcome):
                return result
            if result.status == "terminal":
                if result.outcome is None:
                    raise OpenCodeDispatchIncomplete("terminal observation is missing an outcome")
                return result.outcome
            if result.status != "running":
                if _activity_is_bound(context):
                    reason = result.reason or result.status
                    return TaskOutcome.failed(
                        "external_effect",
                        reason,
                        retryable=reason != "prompt identity conflict",
                    )
                raise OpenCodeDispatchIncomplete(result.reason or result.status)
            if time.monotonic() >= deadline:
                raise OpenCodeDispatchIncomplete("observation horizon exceeded")
            await asyncio.sleep(config.poll_interval_seconds)

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        try:
            config = OpenCodeAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return TaskActivityReconcileResult(status="indeterminate", reason=str(error))
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="activity snapshot does not match the live port",
            )
        result = await self._reconcile_session(request, context, config, allow_create=False)
        if isinstance(result, TaskOutcome):
            return TaskActivityReconcileResult(status="terminal", outcome=result)
        return result

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        try:
            config = OpenCodeAdapterConfig.from_request(request)
        except AdapterConfigurationError as error:
            return TaskActivityCancelResult(status="indeterminate", reason=str(error))
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        if activity.activity_id != port.snapshot.activity_id:
            return TaskActivityCancelResult(
                status="indeterminate",
                reason="activity snapshot does not match the live port",
            )
        mismatch = _identity_mismatch(request, context, port.snapshot)
        if mismatch is not None:
            return TaskActivityCancelResult(status="indeterminate", reason=mismatch)
        agent_run = _effective_agent_run(request, context)
        async with open_client(config, context) as connection:
            client = connection.client
            secret = connection.secret
            try:
                context.heartbeat()
                fingerprint = await _observe_fingerprint(client, config, secret)
                expected = _expected_reference_fields(request, port.snapshot, fingerprint, agent_run)
                bound = await _load_bound_session(client, port.snapshot, expected)
                if isinstance(bound, TaskActivityReconcileResult):
                    return TaskActivityCancelResult(
                        status="indeterminate",
                        reason=bound.reason or "bound reference is not authentic",
                    )
                reference, record = bound
                canaries = _secret_canaries(secret)
                observed = await _observe_bound(
                    client,
                    request,
                    context,
                    reference,
                    record,
                    agent_run=agent_run,
                    canaries=canaries,
                )
                if observed.status == "terminal":
                    if observed.outcome is None:
                        return TaskActivityCancelResult(
                            status="indeterminate",
                            reason="terminal observation is missing an outcome",
                        )
                    return TaskActivityCancelResult(status="terminal", outcome=observed.outcome)
                if observed.status == "indeterminate":
                    return TaskActivityCancelResult(
                        status="indeterminate",
                        reason=observed.reason or "provider observation is indeterminate",
                    )
                await client.abort(reference.session_id or "")
                deadline = time.monotonic() + config.cancel_timeout_seconds
                while True:
                    raced = await _observe_bound(
                        client,
                        request,
                        context,
                        reference,
                        record,
                        agent_run=agent_run,
                        canaries=canaries,
                    )
                    if raced.status == "terminal":
                        if raced.outcome is None:
                            return TaskActivityCancelResult(
                                status="indeterminate",
                                reason="terminal observation is missing an outcome",
                            )
                        return TaskActivityCancelResult(status="terminal", outcome=raced.outcome)
                    if raced.status == "indeterminate":
                        return TaskActivityCancelResult(
                            status="indeterminate",
                            reason=raced.reason or "provider observation is indeterminate",
                        )
                    if time.monotonic() >= deadline:
                        return TaskActivityCancelResult(status="acknowledged")
                    await asyncio.sleep(config.poll_interval_seconds)
            except PROVIDER_ERRORS as error:
                return TaskActivityCancelResult(
                    status="indeterminate",
                    reason=str(error) or "provider cancel is indeterminate",
                )

    async def _reconcile_session(
        self,
        request: TaskRequest,
        context: TaskContext,
        config: OpenCodeAdapterConfig,
        *,
        allow_create: bool,
    ) -> TaskActivityReconcileResult | TaskOutcome:
        if context.secrets is None:
            raise SecretHandleUnauthorized("secret port is required")
        port = context.activity
        if port is None:
            raise ValueError("activity port is required")
        snapshot = port.snapshot
        mismatch = _identity_mismatch(request, context, snapshot)
        if mismatch is not None:
            if allow_create:
                raise ValueError(mismatch)
            return TaskActivityReconcileResult(status="indeterminate", reason=mismatch)
        agent_run = _effective_agent_run(request, context)
        async with open_client(config, context) as connection:
            client = connection.client
            secret = connection.secret
            canaries = _secret_canaries(secret)
            try:
                context.heartbeat()
                fingerprint = await _observe_fingerprint(client, config, secret)
                dispatch_fingerprint = _dispatch_fingerprint(fingerprint, context)
                expected = _expected_reference_fields(request, snapshot, fingerprint, agent_run)
                if snapshot.reference is not None:
                    return await self._reconcile_bound(
                        client,
                        request,
                        context,
                        snapshot,
                        expected,
                        agent_run=agent_run,
                        canaries=canaries,
                    )
                if snapshot.state == "prepared" and not allow_create:
                    return await self._reconcile_prepared(
                        client, port, request, snapshot, dispatch_fingerprint, expected
                    )
                was_prepared = snapshot.state == "prepared"
                snapshot = port.mark_dispatch_started(dispatch_fingerprint)
                expected = _expected_reference_fields(request, snapshot, fingerprint, agent_run)
                return await self._discover_or_create(
                    client,
                    port,
                    request,
                    context,
                    snapshot,
                    expected,
                    agent_run=agent_run,
                    allow_create=allow_create and was_prepared,
                )
            except PROVIDER_ERRORS as error:
                if allow_create:
                    raise
                return TaskActivityReconcileResult(
                    status="indeterminate",
                    reason=str(error) or "provider observation is indeterminate",
                )

    async def _reconcile_prepared(
        self,
        client: OpenCodeHttpClient,
        port: TaskActivityPort,
        request: TaskRequest,
        snapshot: TaskActivitySnapshot,
        fingerprint: dict[str, Any],
        expected: dict[str, str],
    ) -> TaskActivityReconcileResult:
        sessions = await _list_sessions(client)
        metadata = discovery_metadata(
            request=request,
            snapshot=snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        matches = exact_metadata_matches(
            sessions,
            metadata,
            parent_session_id=expected.get("parent_session_id"),
            worktree=expected.get("worktree"),
        )
        if len(matches) > 1:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="multiple exact metadata matches",
            )
        if len(matches) == 1:
            port.mark_dispatch_started(fingerprint)
            return _bind_match(port, matches[0], expected)
        return TaskActivityReconcileResult(status="not_dispatched")

    async def _discover_or_create(
        self,
        client: OpenCodeHttpClient,
        port: TaskActivityPort,
        request: TaskRequest,
        context: TaskContext,
        snapshot: TaskActivitySnapshot,
        expected: dict[str, str],
        *,
        agent_run: AgentRunRequest,
        allow_create: bool,
    ) -> TaskActivityReconcileResult:
        sessions = await _list_sessions(client)
        metadata = discovery_metadata(
            request=request,
            snapshot=snapshot,
            adapter_source_digest=adapter_source_digest(),
        )
        matches = exact_metadata_matches(
            sessions,
            metadata,
            parent_session_id=expected.get("parent_session_id"),
            worktree=expected.get("worktree"),
        )
        if len(matches) > 1:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="multiple exact metadata matches",
            )
        if len(matches) == 1:
            return _bind_match(port, matches[0], expected)
        if allow_create:
            return await _create_and_bind(
                client,
                port,
                request,
                context,
                metadata,
                expected,
                agent_run=agent_run,
            )
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="pending observation after ambiguous create",
        )

    async def _reconcile_bound(
        self,
        client: OpenCodeHttpClient,
        request: TaskRequest,
        context: TaskContext,
        snapshot: TaskActivitySnapshot,
        expected: dict[str, str],
        *,
        agent_run: AgentRunRequest,
        canaries: tuple[str, ...],
    ) -> TaskActivityReconcileResult:
        bound = await _load_bound_session(client, snapshot, expected)
        if isinstance(bound, TaskActivityReconcileResult):
            return bound
        reference, record = bound
        session_id = reference.session_id
        if not session_id:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="bound session identity is unknown",
            )
        verified = await _verify_workspace_binding(
            client,
            agent_run,
            context,
            session_id,
            activity_label=activity_label_for_session(agent_run, request.node_id),
            check_title=False,
        )
        if verified is not None:
            return verified
        admitted = await _admit_prompt(client, agent_run, reference)
        if admitted is not None:
            return admitted
        return await _observe_bound(
            client,
            request,
            context,
            reference,
            record,
            agent_run=agent_run,
            canaries=canaries,
        )

    @staticmethod
    def _configuration_outcome(message: str) -> TaskOutcome:
        return TaskOutcome.failed("configuration", message, retryable=False)
