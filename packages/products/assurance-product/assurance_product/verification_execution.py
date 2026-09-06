"""Closed installed composition for execution semantic Task facades."""

from __future__ import annotations

import hashlib
from copy import copy
from pathlib import Path
from typing import Any, Literal, cast

from graph_engine.attempts import (
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    IndeterminateTaskResult,
    SystemReference,
)
from graph_engine.attempts.events import AttemptSnapshot
from graph_engine.attempts.host_protocol import (
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostCallIdentity,
    TaskActivityRpcIdentity,
    current_bound_identity,
)
from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import FrozenModel, TaskActivitySnapshot, TaskOutcome
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    authorize_binding_secret_handles,
)
from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.verification import ExecutionDispatchResultV1
from assurance_execution.contracts.readiness import VerificationReadinessBindingV1
from assurance_execution.operations.readiness import authenticate_host_readiness, HostReadinessError
from assurance_execution.operations.host_secrets import HostSecretDocumentError
from assurance_execution.operations.agent_skills import authenticate_generation_result
from assurance_execution.operations.verified_process import DockerVerificationHost
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_product.models import VerificationHostConfigV1

RESOURCE_ID = "assurance.product.agent.verification-execution"
FACADE_DELEGATES = {
    "assurance.execution.task.execute.v1": "assurance.execution.agent.execute.v1",
    "assurance.execution.task.run.v1": "assurance.execution.agent.run.v1",
}
HANDLER_ID = "assurance.execution.verified-attempt"


class VerificationConfiguration(FrozenModel):
    validation_profile: Literal["api_db.v1", "api_db_trace.v1"] | None
    host: VerificationHostConfigV1


def execution_backend(profile: str | None) -> str:
    if profile is None:
        return "legacy_raw_agent"
    if profile in {"api_db.v1", "api_db_trace.v1"}:
        return "verified_host"
    raise ValueError("unknown validation profile")


def verification_configuration(composition: object) -> tuple[VerificationConfiguration, str]:
    entries = getattr(getattr(getattr(composition, "registries", None), "resources", None), "entries", {})
    entry = entries.get(RESOURCE_ID)
    if entry is None or entry.owner_id != "assurance.product.agent" or entry.media_type != "application/json":
        raise ValueError("authenticated verification configuration is missing")
    if hashlib.sha256(entry.content).hexdigest() != entry.sha256:
        raise ValueError("verification configuration digest drifted")
    return VerificationConfiguration.model_validate_json(entry.content), entry.sha256


def _readiness_binding(
    config: VerificationConfiguration, config_digest: str | None
) -> VerificationReadinessBindingV1:
    host = config.host
    if (
        config_digest is None
        or config.validation_profile is None
        or host.managed_sut_readiness_handle is None
        or host.managed_sut_authority_handle is None
    ):
        raise ValueError("NOT_READY: authenticated managed SUT readiness selection is required")
    return VerificationReadinessBindingV1(
        selection_handle=host.managed_sut_readiness_handle,
        authority_handle=host.managed_sut_authority_handle,
        collector_handle=host.collector_readiness_handle,
        configuration_digest=config_digest,
        validation_profile=config.validation_profile,
    )


def _preflight_verification(
    config: VerificationConfiguration,
    authorization: InvocationRuntimeAuthorization,
    config_digest: str | None,
) -> None:
    if config.validation_profile is None:
        return
    from assurance_product.runtime_ports import AuthorizedSecretResolver

    host = config.host
    if host.runner is None or host.managed_sut_authority_handle is None or host.credential_handle is None:
        raise ValueError("NOT_READY: managed SUT and qualified verification runner are required")
    binding = _readiness_binding(config, config_digest)
    handles = (binding.selection_handle, binding.authority_handle, host.credential_handle)
    if config.validation_profile == "api_db_trace.v1" and binding.collector_handle is not None:
        handles += (binding.collector_handle,)
    authorize_binding_secret_handles(handles, authorization)
    authenticate_host_readiness(
        binding,
        source_root=Path(host.runner.source_root),
        secret_port=AuthorizedSecretResolver(authorization),
    )
    if (
        hashlib.sha256(Path(host.runner.qualification_path).read_bytes()).hexdigest()
        != host.runner.qualification_digest
    ):
        raise ValueError("runner qualification digest drifted")
    DockerVerificationHost(
        source_root=Path(host.runner.source_root), qualification_path=Path(host.runner.qualification_path)
    ).preflight()


class ProfiledExecutionExecutor:
    def __init__(
        self, *, config: VerificationConfiguration, config_digest: str, legacy: Any, callable_path: str
    ) -> None:
        self.config = config
        self.config_digest = config_digest
        self.backend = execution_backend(config.validation_profile)
        self._legacy = legacy if config.validation_profile is None else None
        self._callable_path = callable_path
        self._host: Any = None
        self._graph_revision = ""
        self._lock_digest = ""

    def with_host(
        self, host: object, *, graph_revision: str, product_lock_digest: str
    ) -> ProfiledExecutionExecutor:
        bound = copy(self)
        bound._host, bound._graph_revision, bound._lock_digest = host, graph_revision, product_lock_digest
        if bound._legacy is not None:
            bound._legacy = bound._legacy.with_host(
                host, graph_revision=graph_revision, product_lock_digest=product_lock_digest
            )
        return bound

    def _validate(self, value: ExecutionPrepareInputV1, scope: AuthorizedAttemptScope) -> None:
        if value.validation_profile != self.config.validation_profile:
            raise ValueError("validation profile disagrees with frozen delegate")
        if value.validation_profile is None:
            if value.verification is not None or value.verification_config_digest is not None:
                raise ValueError("legacy execution cannot accept verified inputs")
        else:
            if value.verification_config_digest != self.config_digest:
                raise ValueError("verification configuration digest drifted")
            if (
                value.verification is None
                or value.verification.validation_profile != value.validation_profile
            ):
                raise ValueError("NOT_READY: verification inputs do not match the frozen profile")
            if (
                value.verification.managed_sut_authority_handle
                != self.config.host.managed_sut_authority_handle
            ):
                raise ValueError("managed SUT authority handle drifted")
        raw = (scope.workspace.project_root / value.plan_ref.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != value.plan_ref.digest:
            raise ValueError("root plan digest drifted")
        plan = ResolvedAssurancePlan.model_validate_json(raw)
        if (
            plan.plan_digest != value.plan_digest
            or (None if plan.verification_policy is None else plan.verification_policy.validation_profile)
            != value.validation_profile
        ):
            raise ValueError("root plan validation profile disagrees with frozen delegate")
        authenticate_generation_result(value, scope.workspace.project_root)

    def _call(self, value: ExecutionPrepareInputV1, scope: AuthorizedAttemptScope) -> TaskHostExecuteCall:
        from assurance_product.runtime_bindings import _attempt_root, _task_request

        if self._host is None or scope.execution.authorization_id is None:
            raise ValueError("verified execution requires an authorized production host")
        runner = self.config.host.runner
        request = _task_request(
            capability_id=HANDLER_ID,
            payload=value.model_dump(mode="json"),
            scope=scope,
            task_id=scope.execution.attempt_key.digest,
            lock_digest=self._lock_digest,
            binding_data={
                "verification_runner": None if runner is None else runner.model_dump(mode="json"),
                "readiness": None
                if self.config.host.managed_sut_readiness_handle is None
                else _readiness_binding(self.config, self.config_digest).model_dump(mode="json"),
            },
        )
        fields = current_bound_identity(
            attempt_key_digest=scope.execution.attempt_key.digest,
            authorization_id=scope.execution.authorization_id,
            workspace_identity_digest=scope.workspace.identity.identity_digest,
            request_digest=canonical_digest(request.model_dump(mode="json")),
            graph_revision=self._graph_revision,
            product_lock_digest=self._lock_digest,
            handler_id=HANDLER_ID,
            fencing_token=scope.execution.fencing_token,
        )
        identity = TaskHostCallIdentity.model_validate(
            {
                "invocation_id": scope.execution.invocation_id,
                "task_id": request.task_id,
                "activation_id": scope.execution.semantic_node_id,
                "attempt": scope.workspace.identity.attempt,
                "activity_id": scope.execution.attempt_key.digest,
                "operation": "execute",
                **fields,
            }
        )
        return TaskHostExecuteCall(
            identity=identity,
            capability_id=HANDLER_ID,
            capability_entrypoint=self._callable_path,
            request=request,
            attempt_root=_attempt_root(scope),
            activity_rpc=TaskActivityRpcIdentity(**identity.model_dump(exclude={"operation"})),
            authorized_secret_handles=tuple(
                sorted(
                    handle
                    for handle in (
                        self.config.host.managed_sut_authority_handle,
                        self.config.host.managed_sut_readiness_handle,
                        self.config.host.credential_handle,
                        self.config.host.collector_readiness_handle
                        if self.config.validation_profile == "api_db_trace.v1"
                        else None,
                    )
                    if handle is not None
                )
            ),
            timeout_seconds=90,
        )

    def _result(self, outcome: TaskOutcome) -> Any:
        from assurance_product.runtime_bindings import _outcome_failure

        failure = _outcome_failure(outcome)
        return failure or ExecutedAttemptResult(
            output=ExecutionDispatchResultV1.model_validate(outcome.output), effects=outcome.effects
        )

    async def execute(self, validated_input: ExecutionPrepareInputV1, scope: AuthorizedAttemptScope) -> Any:
        self._validate(validated_input, scope)
        if self._legacy is not None:
            result = await self._legacy.execute(validated_input, scope)
            if isinstance(result, ExecutedAttemptResult):
                return result.model_copy(
                    update={"output": ExecutionDispatchResultV1.model_validate(result.output)}
                )
            return result
        result = (
            await self._host.execute(self._call(validated_input, scope)) if self._host is not None else None
        )
        if result is None or result.outcome is None:
            raise ValueError("verified execution requires an authorized production host")
        return self._result(result.outcome)

    async def reconcile(
        self, validated_input: ExecutionPrepareInputV1, scope: AuthorizedAttemptScope, snapshot: object
    ) -> Any:
        self._validate(validated_input, scope)
        if self._legacy is not None:
            result = await self._legacy.reconcile(validated_input, scope, snapshot)
            if isinstance(result, ExecutedAttemptResult):
                return result.model_copy(
                    update={"output": ExecutionDispatchResultV1.model_validate(result.output)}
                )
            return result
        call = self._call(validated_input, scope)
        snap = cast(AttemptSnapshot, snapshot)
        activity = TaskActivitySnapshot.model_validate(
            {
                "activity_id": snap.activity_id,
                "request_digest": call.identity.request_digest,
                "workspace_identity": scope.workspace.identity,
                "state": snap.activity_state,
                "dispatch_fingerprint": snap.activity_dispatch_fingerprint,
                "dispatch_fingerprint_digest": snap.activity_dispatch_fingerprint_digest,
                "reference": snap.activity_reference,
                "reference_digest": snap.activity_reference_digest,
            }
        )
        reconcile_call = TaskHostReconcileCall(
            **{
                **call.model_dump(),
                "capability_entrypoint": self._callable_path.removesuffix(".execute") + ".reconcile",
                "identity": {**call.identity.model_dump(), "operation": "reconcile"},
                "activity": activity,
            }
        )
        result = (await self._host.reconcile(reconcile_call)).reconcile_result
        if result is not None and result.status == "terminal" and result.outcome is not None:
            return self._result(result.outcome)
        if result is not None and result.status == "not_dispatched":
            return await self.execute(validated_input, scope)
        return IndeterminateTaskResult(
            reconciliation=SystemReference(reference_id="verified-execution-indeterminate")
        )


def preflight_verification(
    config: VerificationConfiguration,
    authorization: InvocationRuntimeAuthorization,
    config_digest: str | None = None,
) -> None:
    from graph_engine.errors import GraphEngineError

    reason = None
    try:
        _preflight_verification(config, authorization, config_digest)
    except (HostSecretDocumentError, HostReadinessError) as error:
        reason = str(error)
    except (ValueError, OSError, GraphEngineError):
        reason = "verification prerequisites are unavailable"
    if reason is not None:
        raise ValueError(f"NOT_READY: {reason}")
