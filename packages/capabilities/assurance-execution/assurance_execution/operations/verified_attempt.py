"""Installed verified semantic delegate, using one production runtime activity."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from graph_engine.attempts import AttemptKey
from graph_engine.canonical import canonical_digest
from assurance_execution.operations.user_attempt import start_user_attempt, recover_user_attempt, UserAttempt
from assurance_execution.contracts.readiness import ManagedSutReadinessSelectionV1
from assurance_execution.operations.managed_sut import authenticate_managed_sut_readiness
from pathlib import Path
from collections.abc import Mapping

from graph_engine.plugin_api import (
    TaskContext,
    TaskRequest,
    TaskOutcome,
    TaskActivitySnapshot,
    TaskActivityReconcileResult,
    TaskActivityCancelResult,
)
from assurance_execution.contracts.agent import ExecutionPrepareInputV1, ExecuteInputV1
from assurance_execution.contracts.readiness import VerificationReadinessBindingV1
from assurance_execution.operations.readiness import authenticate_host_readiness, authenticate_host_selection
from assurance_execution.execution_view import ExecutionView, execution_view_relative
from assurance_execution.operations.agent_skills import assemble_execution_input
from assurance_execution.operations.verified_execution import (
    VerifiedExecutionHandler,
    VerifiedExecutionInputV1,
)


class UserAttemptNotStarted(ValueError):
    """No lifecycle has been allocated for this authenticated attempt."""


class VerifiedAttemptHandler:
    def __init__(self) -> None:
        self._delegate = VerifiedExecutionHandler()

    def _request(
        self, request: TaskRequest, context: TaskContext, *, recovering: bool = False
    ) -> TaskRequest:
        binding = request.binding_data
        host = binding.get("user_host") if isinstance(binding, Mapping) else None
        source = host.get("sut_source_root") if isinstance(host, Mapping) else None
        if not isinstance(source, str) or not source:
            raise ValueError("NOT_READY: SUT source root is required")
        root = ExecutionPrepareInputV1.model_validate(request.input)
        if root.verification is None or root.validation_profile != root.verification.validation_profile:
            raise ValueError("NOT_READY: verified attempt requires the frozen verification profile")
        if context.secrets is None or not isinstance(binding, Mapping):
            raise ValueError("NOT_READY: host readiness secret port is required")
        readiness = VerificationReadinessBindingV1.model_validate(binding.get("readiness"))
        selected = (
            authenticate_host_selection(readiness, secret_port=context.secrets)
            if recovering
            else authenticate_host_readiness(readiness, source_root=Path(source), secret_port=context.secrets)
        )
        if (
            selected.workspace_root != str(context.project_root.resolve())
            or selected.verification != root.verification
        ):
            raise ValueError("NOT_READY: host readiness selection disagrees with this attempt")
        prepared = assemble_execution_input(
            request.input,
            workspace=context.project_root,
            write_root=context.write_root,
            model=ExecuteInputV1,
            request=request,
            context=context,
        )
        if prepared.verification_manifest_ref is None or prepared.execution_id is None:
            raise ValueError("verified prepare did not freeze a manifest")
        if selected.execution_id != prepared.execution_id:
            raise ValueError("NOT_READY: host readiness execution identity drifted")
        payload = VerifiedExecutionInputV1(
            manifest_ref=prepared.verification_manifest_ref,
            verification=root.verification,
            view=ExecutionView(
                batch_id=prepared.batch_id,
                root=execution_view_relative(prepared.change_id, prepared.batch_id, prepared.execution_id),
                selected_targets=prepared.mapping.selected,
                digest=prepared.execution_view_digest,
                executed_at=prepared.executed_at,
                mode="verified",
                execution_id=prepared.execution_id,
            ),
        )
        return request.model_copy(
            update={
                "input": payload.model_dump(mode="json"),
                "binding_data": {},
            }
        )

    def _owned_request(
        self,
        request: TaskRequest,
        context: TaskContext,
        *,
        recovering: bool = False,
        allow_start: bool = True,
        cancelling: bool = False,
    ) -> tuple[TaskRequest, TaskContext, UserAttempt | None]:
        binding = request.binding_data
        host = binding.get("user_host") if isinstance(binding, Mapping) else None
        root = ExecutionPrepareInputV1.model_validate(request.input)
        if not isinstance(host, Mapping) or set(host) == {"sut_source_root"}:
            return self._request(request, context, recovering=recovering), context, None
        if root.verification is not None or root.validation_profile not in {"api_db.v1", "api_db_trace.v1"}:
            raise ValueError("NOT_READY: dynamic verification must be owned by the execution host")
        if (
            context.activity is None
            or context.activity.snapshot.workspace_identity != context.workspace_identity
            or context.secrets is None
            or host.get("configuration_digest") != root.verification_config_digest
        ):
            raise ValueError("NOT_READY: authorized execution host context is required")
        assert isinstance(binding, Mapping)
        source_value = host.get("sut_source_root")
        if not isinstance(source_value, str) or not source_value:
            raise ValueError("NOT_READY: SUT source root is required")
        source = Path(source_value)
        authority_handle, credential_handle = host.get("authority_handle"), host.get("credential_handle")
        if not isinstance(authority_handle, str) or not isinstance(credential_handle, str):
            raise ValueError("NOT_READY: required host handles are missing")
        key = AttemptKey(digest=context.workspace_identity.task_id)
        owned = recover_user_attempt(
            root,
            workspace_root=context.project_root,
            source_root=source,
            attempt_key=key,
            secrets=context.secrets,
            authority_handle=authority_handle,
        )
        if owned is None:
            if not allow_start and not recovering:
                raise UserAttemptNotStarted()
            if recovering:
                raise ValueError("NOT_READY: dispatched attempt has no retained authority")
            owned = start_user_attempt(
                root,
                source_root=source,
                workspace_root=context.project_root,
                attempt_key=key,
                invocation_id=request.invocation_id,
                task_id=request.task_id,
                graph_instance_id=request.graph_instance_id,
                node_id=request.node_id,
                authorization_scope_digest=context.workspace_identity.identity_digest,
                secrets=context.secrets,
                authority_handle=authority_handle,
                credential_handle=credential_handle,
            )
        expected_activity = canonical_digest(
            {
                "attempt_key": key.digest,
                "invocation_id": request.invocation_id,
                "task_id": request.task_id,
                "graph_instance_id": request.graph_instance_id,
                "node_id": request.node_id,
                "workspace_identity_digest": context.workspace_identity.identity_digest,
            }
        )
        if (
            owned.authority.authorization_scope_digest != context.workspace_identity.identity_digest
            or owned.authority.activity_receipt_digest != expected_activity
        ):
            raise ValueError("NOT_READY: retained authority belongs to a different activity")
        private_context = replace(context, secrets=owned.secrets)
        try:
            if cancelling and context.activity.snapshot.state == "prepared":
                return request, private_context, owned
            if not recovering:
                if root.validation_profile == "api_db_trace.v1":
                    from assurance_execution.contracts.readiness import CollectorReadinessReceiptV1
                    from assurance_execution.operations.readiness import authenticate_collector_readiness

                    if owned.record.collector_receipt is None:
                        raise ValueError("NOT_READY: attempt-bound Collector/OTel lifecycle is required")
                    authenticate_collector_readiness(
                        CollectorReadinessReceiptV1.model_validate(owned.record.collector_receipt),
                        sut_instance_id=owned.verification.sut_instance_id,
                        execution_id=owned.execution_id,
                        configuration_digest=str(host["configuration_digest"]),
                        authorization_scope_digest=context.workspace_identity.identity_digest,
                        activity_receipt_digest=owned.authority.activity_receipt_digest,
                    )
                authenticate_managed_sut_readiness(
                    ManagedSutReadinessSelectionV1(
                        workspace_root=str(context.project_root.resolve()),
                        verification=owned.verification,
                        configuration_digest=str(host["configuration_digest"]),
                        execution_id=owned.execution_id,
                        authorization_scope_digest=context.workspace_identity.identity_digest,
                        activity_receipt_digest=owned.authority.activity_receipt_digest,
                    ),
                    source_root=source,
                    secret_port=owned.secrets,
                )
            if owned.record.prepared_input is None:
                if recovering:
                    raise ValueError("NOT_READY: dispatched attempt has no prepared evidence")
                prepared = assemble_execution_input(
                    root.model_copy(update={"verification": owned.verification}).model_dump(mode="json"),
                    workspace=context.project_root,
                    write_root=context.write_root,
                    model=ExecuteInputV1,
                    request=request,
                    context=private_context,
                )
                if prepared.verification_manifest_ref is None or prepared.execution_id != owned.execution_id:
                    raise ValueError("NOT_READY: prepared execution identity drifted")
                payload = VerifiedExecutionInputV1(
                    manifest_ref=prepared.verification_manifest_ref,
                    verification=owned.verification,
                    view=ExecutionView(
                        batch_id=prepared.batch_id,
                        root=execution_view_relative(
                            prepared.change_id, prepared.batch_id, prepared.execution_id
                        ),
                        selected_targets=prepared.mapping.selected,
                        digest=prepared.execution_view_digest,
                        executed_at=prepared.executed_at,
                        mode="verified",
                        execution_id=prepared.execution_id,
                    ),
                )
                owned.retain_prepared(payload.model_dump(mode="json"))
            return (
                request.model_copy(
                    update={
                        "input": owned.record.prepared_input,
                        "binding_data": {},
                    }
                ),
                private_context,
                owned,
            )
        except BaseException:
            owned.stop()
            raise

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        prepared, private_context, owned = await asyncio.to_thread(self._owned_request, request, context)
        try:
            return await self._delegate.execute(prepared, private_context)
        finally:
            if owned is not None:
                await asyncio.to_thread(owned.stop)

    async def reconcile(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityReconcileResult:
        if context.activity is None or activity != context.activity.snapshot:
            return TaskActivityReconcileResult(
                status="indeterminate", reason="unauthenticated activity snapshot"
            )
        try:
            prepared, private_context, owned = await asyncio.to_thread(
                self._owned_request,
                request,
                context,
                recovering=activity.state != "prepared",
                allow_start=False,
            )
        except UserAttemptNotStarted:
            return TaskActivityReconcileResult(status="not_dispatched")
        except (ValueError, OSError):
            return TaskActivityReconcileResult(
                status="indeterminate", reason="frozen execution identity unavailable"
            )
        try:
            return await self._delegate.reconcile(prepared, private_context, activity)
        finally:
            if owned is not None and activity.state != "prepared":
                await asyncio.to_thread(owned.stop)

    async def cancel(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityCancelResult:
        if context.activity is None or activity != context.activity.snapshot:
            return TaskActivityCancelResult(
                status="indeterminate", reason="unauthenticated activity snapshot"
            )
        try:
            prepared, private_context, owned = await asyncio.to_thread(
                self._owned_request,
                request,
                context,
                recovering=activity.state != "prepared",
                allow_start=False,
                cancelling=True,
            )
        except UserAttemptNotStarted:
            return TaskActivityCancelResult(status="acknowledged")
        except (ValueError, OSError):
            return TaskActivityCancelResult(
                status="indeterminate", reason="frozen execution identity unavailable"
            )
        try:
            if owned is not None and activity.state == "prepared":
                return TaskActivityCancelResult(status="acknowledged")
            return await self._delegate.cancel(prepared, private_context, activity)
        finally:
            if owned is not None:
                await asyncio.to_thread(owned.stop)
