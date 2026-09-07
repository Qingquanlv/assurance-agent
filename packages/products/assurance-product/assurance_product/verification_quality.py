"""Host-authorized quality materialization for verified execution profiles."""

from __future__ import annotations

from copy import copy
from typing import Any, cast

from graph_engine.attempts import AuthorizedAttemptScope, ExecutedAttemptResult
from graph_engine.canonical import JSONValue

from assurance_execution.contracts.workflow import VerifiedExecutionCycleResultV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, MaterializeAssessmentInputV1
from assurance_product.verification_execution import VerificationConfiguration


CONTRACT_ID = "assurance.quality.materialize-assessment-inputs"


class ProfiledAssessmentExecutor:
    """Route verified assessment through the host that owns the SUT authority."""

    def __init__(
        self,
        *,
        config: VerificationConfiguration,
        config_digest: str,
        legacy: Any,
        handler_id: str,
        handler: object,
        callable_path: str,
    ) -> None:
        from assurance_product.runtime_bindings import _HostBackedInstalledPhase

        self.config = config
        self.config_digest = config_digest
        self.backend = "legacy_local" if config.validation_profile is None else "verified_host"
        self._legacy = legacy if config.validation_profile is None else None
        authority_handle = config.host.managed_sut_authority_handle
        self._phase = (
            None
            if config.validation_profile is None
            else _HostBackedInstalledPhase(
                handler_id,
                handler,
                phase="runtime",
                callable_path=callable_path,
                secret_handles=(() if authority_handle is None else (authority_handle,)),
                timeout_seconds=30,
            )
        )

    def with_host(
        self, host: object, *, graph_revision: str, product_lock_digest: str
    ) -> ProfiledAssessmentExecutor:
        bound = copy(self)
        if bound._legacy is not None:
            bound._legacy = bound._legacy.with_host(
                host, graph_revision=graph_revision, product_lock_digest=product_lock_digest
            )
        if bound._phase is not None:
            bound._phase = bound._phase.with_host(
                host, graph_revision=graph_revision, product_lock_digest=product_lock_digest
            )
        return bound

    async def execute(
        self,
        validated_input: MaterializeAssessmentInputV1,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[AssessmentInputsV1] | Any:
        if self.config.validation_profile is None:
            if isinstance(validated_input.execution, VerifiedExecutionCycleResultV1):
                raise ValueError("legacy assessment cannot accept a verified execution")
            if self._legacy is None:
                raise ValueError("legacy assessment executor is unavailable")
            return await self._legacy.execute(validated_input, scope)
        if (
            not isinstance(validated_input.execution, VerifiedExecutionCycleResultV1)
            or validated_input.execution.validation_profile != self.config.validation_profile
        ):
            raise ValueError("assessment validation profile disagrees with frozen product configuration")
        if self.config.host.managed_sut_authority_handle is None:
            raise ValueError("verified assessment requires a managed SUT authority handle")
        if self._phase is None or self._phase._host is None:
            raise ValueError("verified assessment requires an authorized production host")

        from assurance_product.runtime_bindings import _outcome_failure, _task_request

        authority_handle = cast(str, self.config.host.managed_sut_authority_handle)
        request = _task_request(
            capability_id=self._phase.handler_id,
            payload=validated_input.model_dump(mode="json", exclude_computed_fields=True),
            scope=scope,
            task_id=scope.execution.attempt_key.digest,
            lock_digest=self._phase.lock_digest,
            binding_data={
                "managed_sut_authority_handle": authority_handle,
                "verification_config_digest": self.config_digest,
                "validation_profile": self.config.validation_profile,
            },
        )
        outcome = await self._phase._invoke(request, scope)
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return ExecutedAttemptResult(
            output=AssessmentInputsV1.model_validate(cast(JSONValue, outcome.output)),
            effects=tuple(outcome.effects),
        )


__all__ = ["CONTRACT_ID", "ProfiledAssessmentExecutor"]
