from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from agent_runtime_contracts import (
    AgentExecutionContract,
    AgentRunRequest,
    AgentRunResult,
    AgentRuntimeBinding,
    AgentRuntimePolicy,
    RawAgentRuntimeBindingProjectionV1,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
)
from agent_runtime_contracts.attempt_executor import phase_task_id
from graph_engine.attempts import (
    AuthorizedAttemptScope,
    ExecutedAttemptResult,
    PermanentTaskFailure,
    ResolvedAttemptContract,
    TaskAttemptContract,
    resolve_contract,
)
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import CapabilityBindingEntry, FrozenComposition
from graph_engine.frozen_json import thaw_json as thaw_frozen
from graph_engine.plugin_api import (
    InvocationMetadata,
    TaskContext,
    TaskOutcome,
    TaskRequest,
)

from assurance_product.agent_contracts import (
    all_feature_agent_contracts,
    all_feature_task_contracts,
)

_RUNTIME_HANDLER_ID = "runtime.opencode.execute"
_PROVIDER = "opencode"
_ACTIVITY_RECOVERY = "adopt-observe-reconcile-v1"


def _contract_digest(contract: AgentExecutionContract[Any, Any, Any]) -> str:
    return canonical_digest(cast(JSONValue, contract.canonical_projection()))


def _contract_owner(contract_id: str) -> str:
    body = contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, _base = body.partition(".agent.")
    if marker != ".agent." or not feature:
        raise ValueError(f"wrong-owner: invalid agent contract id {contract_id!r}")
    return f"assurance.{feature}"


def _runtime_handler_owner(runtime_handler_id: str) -> str:
    owner, separator, leaf = runtime_handler_id.rpartition(".")
    if separator != "." or not owner or not leaf:
        raise ValueError(f"invalid runtime handler id: {runtime_handler_id!r}")
    return owner


def project_raw_agent_runtime_binding(
    binding: AgentRuntimeBinding,
    contract: AgentExecutionContract[Any, Any, Any],
) -> RawAgentRuntimeBindingProjectionV1:
    return RawAgentRuntimeBindingProjectionV1(
        schema_version="raw-agent-runtime-binding-v1",
        contract_id=binding.contract_id,
        contract_digest=_contract_digest(contract),
        runtime_handler_id=binding.runtime_handler_id,
        adapter=_PROVIDER,
        provider=binding.provider,
        model=binding.model,
        policy=binding.policy,
        secret_handles=binding.secret_handles,
        activity_recovery=_ACTIVITY_RECOVERY,
    )


def authenticate_raw_agent_runtime_bindings(
    rows: Sequence[RawAgentRuntimeBindingProjectionV1],
    contracts: Mapping[str, AgentExecutionContract[Any, Any, Any]],
    *,
    adapter: str = _PROVIDER,
) -> tuple[RawAgentRuntimeBindingProjectionV1, ...]:
    seen: dict[str, RawAgentRuntimeBindingProjectionV1] = {}
    for row in rows:
        if row.contract_id in seen:
            raise ValueError(f"duplicate raw agent runtime binding: {row.contract_id}")
        seen[row.contract_id] = row
    extra = sorted(set(seen) - set(contracts))
    if extra:
        raise ValueError(f"extra raw agent runtime binding: {extra}")
    missing = sorted(set(contracts) - set(seen))
    if missing:
        raise ValueError(f"missing raw agent runtime binding: {missing}")
    for contract_id, row in seen.items():
        contract = contracts[contract_id]
        if row.adapter != adapter or row.provider != adapter:
            raise ValueError(f"wrong-adapter: {contract_id}")
        if (
            _runtime_handler_owner(row.runtime_handler_id) != f"runtime.{adapter}"
            or contract.owner_id != _contract_owner(contract_id)
            or row.runtime_handler_id != f"runtime.{adapter}.execute"
        ):
            raise ValueError(f"wrong-owner: {contract_id}")
        if row.contract_digest != _contract_digest(contract):
            raise ValueError(f"contract-digest-drift: {contract_id}")
        if row.schema_version != "raw-agent-runtime-binding-v1":
            raise ValueError(f"raw-agent-runtime-binding schema_version drifted: {contract_id}")
        if row.activity_recovery != _ACTIVITY_RECOVERY:
            raise ValueError(f"activity recovery drifted: {contract_id}")
        if not row.model or row.model != row.model.strip():
            raise ValueError(f"missing authenticated model: {contract_id}")
    return tuple(seen[contract_id] for contract_id in sorted(contracts))


def _capability_bindings(composition: FrozenComposition) -> dict[str, CapabilityBindingEntry]:
    return {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }


def _binding_from_entry(
    entry: CapabilityBindingEntry,
    contract: AgentExecutionContract[Any, Any, Any],
) -> AgentRuntimeBinding:
    data = thaw_frozen(entry.data)
    if not isinstance(data, Mapping):
        raise ValueError(f"binding data is not a mapping: {entry.capability_id}")
    execution = data.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError(f"binding execution is not a mapping: {entry.capability_id}")
    model = execution.get("provider_model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"missing authenticated model: {entry.capability_id}")
    resource_ids = list(entry.resource_ids)
    if len(resource_ids) != 2:
        raise ValueError(f"binding resource ids drifted: {entry.capability_id}")
    permission_id, request_policy_id = resource_ids[0], resource_ids[1]
    if entry.contract_id != contract.contract_id:
        raise ValueError(f"wrong-owner: {entry.capability_id}")
    if entry.target_capability_id != _RUNTIME_HANDLER_ID:
        raise ValueError(f"wrong-adapter: {entry.capability_id}")
    return AgentRuntimeBinding(
        contract_id=contract.contract_id,
        runtime_handler_id=entry.target_capability_id,
        provider=_PROVIDER,
        model=model,
        policy=AgentRuntimePolicy(
            request_policy_handle=request_policy_id,
            request_config_handle=permission_id,
        ),
        secret_handles=entry.secret_handles,
    )


def _require_opencode_composition(composition: FrozenComposition) -> None:
    source = composition.manifest.source
    if source is None or source.entrypoint_name != "assurance-opencode":
        raise ValueError("composition is not the OpenCode product")


def runtime_bindings_from_composition(
    composition: FrozenComposition,
) -> Mapping[str, AgentRuntimeBinding]:
    _require_opencode_composition(composition)
    contracts = all_feature_agent_contracts()
    entries = _capability_bindings(composition)
    extra = sorted(set(entries) - set(contracts))
    if extra:
        raise ValueError(f"extra raw agent runtime binding: {extra}")
    missing = sorted(set(contracts) - set(entries))
    if missing:
        raise ValueError(f"missing raw agent runtime binding: {missing}")
    resolved = {
        contract_id: _binding_from_entry(entries[contract_id], contract)
        for contract_id, contract in contracts.items()
    }
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, contracts[contract_id])
            for contract_id, binding in sorted(resolved.items())
        ),
        contracts,
        adapter=_PROVIDER,
    )
    if len(resolved) != 33:
        raise ValueError("composition runtime binding set is not the exact 33 Agent contracts")
    return MappingProxyType(resolved)


def raw_agent_runtime_binding_rows(
    composition: FrozenComposition,
) -> tuple[RawAgentRuntimeBindingProjectionV1, ...]:
    contracts = all_feature_agent_contracts()
    bindings = runtime_bindings_from_composition(composition)
    return authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(bindings[contract_id], contracts[contract_id])
            for contract_id in sorted(contracts)
        ),
        contracts,
        adapter=_PROVIDER,
    )


def _authenticate_runtime_binding(
    composition: FrozenComposition,
    binding: AgentRuntimeBinding,
) -> None:
    from assurance_product.product import AssuranceCompositionError

    owner = _runtime_handler_owner(binding.runtime_handler_id)
    closure = set(composition.manifest.required_plugin_ids)
    if owner not in closure:
        raise AssuranceCompositionError(
            f"runtime handler owner is outside authenticated Product dependency closure: {owner}"
        )
    handlers = composition.registries.capabilities.task_handlers
    if binding.runtime_handler_id not in handlers:
        raise AssuranceCompositionError(
            f"runtime handler is outside authenticated Product dependency closure: "
            f"{binding.runtime_handler_id}"
        )


def _task_context(scope: AuthorizedAttemptScope) -> TaskContext:
    digest = scope.workspace.identity.identity_digest
    invocation = InvocationMetadata(
        invocation_id=scope.execution.invocation_id,
        lock_digest=digest,
        composition_digest=digest,
        entrypoint=scope.execution.public_entrypoint,
    )
    return TaskContext(
        project_root=scope.workspace.project_root,
        write_root=scope.workspace.write_root,
        workspace_identity=scope.workspace.identity,
        heartbeat=lambda: None,
        cancel_requested=lambda: False,
        invocation=invocation,
    )


def _task_request(
    *,
    capability_id: str,
    payload: Mapping[str, object] | None,
    scope: AuthorizedAttemptScope,
    binding_data: object = None,
    task_id: str,
) -> TaskRequest:
    digest = scope.workspace.identity.identity_digest
    invocation = InvocationMetadata(
        invocation_id=scope.execution.invocation_id,
        lock_digest=digest,
        composition_digest=digest,
        entrypoint=scope.execution.public_entrypoint,
    )
    return TaskRequest(
        invocation_id=scope.execution.invocation_id,
        task_id=task_id,
        graph_instance_id=scope.execution.invocation_id,
        node_id=scope.execution.semantic_node_id,
        capability_id=capability_id,
        binding_data=cast(Any, binding_data),
        invocation=invocation,
        attempt=1,
        input=cast(JSONValue, payload),
    )


def _outcome_failure(outcome: TaskOutcome) -> PermanentTaskFailure | None:
    if outcome.failure is None:
        return None
    kind = outcome.failure.kind
    if kind not in {
        "transient",
        "timeout",
        "invalid_input",
        "invalid_output",
        "external_effect",
        "internal",
        "configuration",
    }:
        kind = "internal"
    return PermanentTaskFailure(kind=kind, message=outcome.failure.message)


class InstalledPreparePhase:
    def __init__(self, handler_id: str, handler: object, binding_data: object) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._binding_data = binding_data

    async def execute(
        self,
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
    ) -> AgentRunRequest | PermanentTaskFailure:
        request = _task_request(
            capability_id=self.handler_id,
            payload=validated_input.model_dump(mode="json"),
            scope=scope,
            binding_data=self._binding_data,
            task_id=phase_task_id(scope.execution.attempt_key, "prepare", self.handler_id),
        )
        outcome = await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return AgentRunRequest.model_validate(outcome.output)


class InstalledRuntimePhase:
    def __init__(self, handler_id: str, handler: object, binding: AgentRuntimeBinding) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._binding = binding

    async def execute(
        self,
        prepared: AgentRunRequest,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        request = _task_request(
            capability_id=self.handler_id,
            payload=prepared.model_dump(mode="json"),
            scope=scope,
            binding_data={
                "provider": self._binding.provider,
                "model": self._binding.model,
                "secret_handles": list(self._binding.secret_handles),
            },
            task_id=phase_task_id(scope.execution.attempt_key, "runtime", self.handler_id),
        )
        outcome = await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        run_result = AgentRunResult.model_validate(outcome.output)
        return RawAgentRuntimeOutcome(
            run_result=run_result,
            raw_workspace=ReadOnlyRawWorkspace(
                scope.workspace.write_root,
                identity_digest=scope.workspace.identity.identity_digest,
            ),
        )


class InstalledFinalizePhase:
    def __init__(self, handler_id: str, handler: object, output_model: type[BaseModel]) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._output_model = output_model

    async def execute(
        self,
        bundle: RawFinalizeBundle[Any, Any, Any],
        scope: AuthorizedAttemptScope,
    ) -> BaseModel | PermanentTaskFailure:
        payload = {
            "agent_result": bundle.run_evidence.model_dump(mode="json"),
            "prepared": (
                bundle.prepared.model_dump(mode="json")
                if isinstance(bundle.prepared, BaseModel)
                else bundle.prepared
            ),
            "validated_input": bundle.validated_input.model_dump(mode="json"),
        }
        request = _task_request(
            capability_id=self.handler_id,
            payload=payload,
            scope=scope,
            task_id=phase_task_id(scope.execution.attempt_key, "finalize", self.handler_id),
        )
        outcome = await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        if isinstance(outcome.output, self._output_model):
            return outcome.output
        return self._output_model.model_validate(outcome.output)


class DeterministicTaskExecutor:
    def __init__(self, handler_id: str, handler: object, output_model: type[BaseModel]) -> None:
        self.handler_id = handler_id
        self._handler = handler
        self._output_model = output_model
        self.dispatch_count = 0

    async def execute(
        self,
        validated_input: BaseModel,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[Any] | PermanentTaskFailure:
        self.dispatch_count += 1
        request = _task_request(
            capability_id=self.handler_id,
            payload=validated_input.model_dump(mode="json"),
            scope=scope,
            task_id=scope.execution.attempt_key.digest,
        )
        outcome = await self._handler.execute(request, _task_context(scope))  # type: ignore[attr-defined]
        failure = _outcome_failure(outcome)
        if failure is not None:
            return failure
        return ExecutedAttemptResult(
            output=_coerce_output(self._output_model, outcome.output),
            effects=tuple(outcome.effects),
        )


def _coerce_output(model: type[BaseModel], payload: object) -> BaseModel:
    if payload is None:
        raise ValueError("handler returned empty output")
    if isinstance(payload, model):
        return payload
    try:
        return model.model_validate(payload)
    except ValidationError:
        if isinstance(payload, Mapping):
            for key in ("status", "projection"):
                nested = payload.get(key)
                if nested is not None:
                    try:
                        return model.model_validate(nested)
                    except ValidationError:
                        continue
        raise


def _resolve_agent_contract(
    contract: AgentExecutionContract[Any, Any, Any],
    binding: AgentRuntimeBinding,
    composition: FrozenComposition,
) -> ResolvedAttemptContract[Any, Any]:
    handlers = composition.registries.capabilities.task_handlers
    entries = _capability_bindings(composition)
    binding_data = thaw_frozen(entries[contract.contract_id].data)
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=InstalledPreparePhase(
            contract.prepare_handler_id,
            handlers[contract.prepare_handler_id],
            binding_data,
        ),
        runtime=InstalledRuntimePhase(
            binding.runtime_handler_id,
            handlers[binding.runtime_handler_id],
            binding,
        ),
        finalize=InstalledFinalizePhase(
            contract.finalize_handler_id,
            handlers[contract.finalize_handler_id],
            contract.output_model,
        ),
    )
    return executor.resolve()


def _resolve_task_contract(
    contract: TaskAttemptContract[Any, Any],
    composition: FrozenComposition,
) -> ResolvedAttemptContract[Any, Any]:
    handler = composition.registries.capabilities.task_handlers[contract.handler_id]
    return resolve_contract(
        contract,
        executor=DeterministicTaskExecutor(contract.handler_id, handler, contract.output_model),
    )


def boot_semantic_attempt_contracts(
    composition: FrozenComposition,
    bindings: Mapping[str, AgentRuntimeBinding] | None = None,
) -> Mapping[str, ResolvedAttemptContract[Any, Any]]:
    catalog = dict(runtime_bindings_from_composition(composition))
    if bindings is not None:
        catalog.update(bindings)
    for binding in catalog.values():
        _authenticate_runtime_binding(composition, binding)
    _require_opencode_composition(composition)
    agents = all_feature_agent_contracts()
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, agents[binding.contract_id])
            for binding in catalog.values()
        ),
        agents,
        adapter=_PROVIDER,
    )
    resolved: dict[str, ResolvedAttemptContract[Any, Any]] = {}
    for contract_id, binding in catalog.items():
        resolved[contract_id] = _resolve_agent_contract(agents[contract_id], binding, composition)
    for contract in all_feature_task_contracts().values():
        resolved[contract.contract_id] = _resolve_task_contract(contract, composition)
    if len(resolved) != 43:
        raise ValueError(f"semantic attempt registry must contain 43 contracts, got {len(resolved)}")
    return MappingProxyType(resolved)


__all__ = [
    "DeterministicTaskExecutor",
    "authenticate_raw_agent_runtime_bindings",
    "boot_semantic_attempt_contracts",
    "project_raw_agent_runtime_binding",
    "raw_agent_runtime_binding_rows",
    "runtime_bindings_from_composition",
]
