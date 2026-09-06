"""Installed verified semantic delegate, using one production runtime activity."""

from __future__ import annotations

import hashlib
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


class VerifiedAttemptHandler:
    def __init__(self) -> None:
        self._delegate = VerifiedExecutionHandler()

    def _request(
        self, request: TaskRequest, context: TaskContext, *, recovering: bool = False
    ) -> TaskRequest:
        binding = request.binding_data
        runner = binding.get("verification_runner") if isinstance(binding, Mapping) else None
        if not isinstance(runner, Mapping) or set(runner) != {
            "source_root",
            "qualification_path",
            "qualification_digest",
        }:
            raise ValueError("NOT_READY: frozen runner qualification is required")
        if not recovering and (
            hashlib.sha256(Path(str(runner["qualification_path"])).read_bytes()).hexdigest()
            != runner["qualification_digest"]
        ):
            raise ValueError("NOT_READY: runner qualification digest drifted")
        root = ExecutionPrepareInputV1.model_validate(request.input)
        if root.verification is None or root.validation_profile != root.verification.validation_profile:
            raise ValueError("NOT_READY: verified attempt requires the frozen verification profile")
        if context.secrets is None or not isinstance(binding, Mapping):
            raise ValueError("NOT_READY: host readiness secret port is required")
        readiness = VerificationReadinessBindingV1.model_validate(binding.get("readiness"))
        selected = (
            authenticate_host_selection(readiness, secret_port=context.secrets)
            if recovering
            else authenticate_host_readiness(
                readiness, source_root=Path(str(runner["source_root"])), secret_port=context.secrets
            )
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
                "binding_data": {
                    "verification_runner": {key: runner[key] for key in ("source_root", "qualification_path")}
                },
            }
        )

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await self._delegate.execute(self._request(request, context), context)

    async def reconcile(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityReconcileResult:
        if context.activity is None or activity != context.activity.snapshot:
            return TaskActivityReconcileResult(
                status="indeterminate", reason="unauthenticated activity snapshot"
            )
        try:
            prepared = self._request(request, context, recovering=activity.state != "prepared")
        except (ValueError, OSError):
            return TaskActivityReconcileResult(
                status="indeterminate", reason="frozen execution identity unavailable"
            )
        return await self._delegate.reconcile(prepared, context, activity)

    async def cancel(
        self, request: TaskRequest, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityCancelResult:
        if context.activity is None or activity != context.activity.snapshot:
            return TaskActivityCancelResult(
                status="indeterminate", reason="unauthenticated activity snapshot"
            )
        try:
            prepared = self._request(request, context, recovering=activity.state != "prepared")
        except (ValueError, OSError):
            return TaskActivityCancelResult(
                status="indeterminate", reason="frozen execution identity unavailable"
            )
        return await self._delegate.cancel(prepared, context, activity)
