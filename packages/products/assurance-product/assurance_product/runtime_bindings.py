from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from agent_runtime_contracts import (
    AgentExecutionContract,
    AgentRuntimeBinding,
    AgentRuntimeCapabilities,
    AgentRuntimePolicy,
    CompositeAttemptExecutor,
)
from graph_engine.attempts import ResolvedAttemptContract, TaskAttemptContract, resolve_contract
from graph_engine.composition import FrozenComposition
from graph_engine.frozen_json import thaw_json

from assurance_product.agent_contracts import (
    all_feature_agent_contracts,
    all_feature_task_contracts,
)
from assurance_product.models import AdapterName, alias_ids_for_prepare
from assurance_product.source_catalog import adapter_for_entrypoint

_DEFAULT_ADAPTER: AdapterName = "opencode"
_DEFAULT_MODEL = "fixture-model"
_DEFAULT_REQUEST_POLICY = "assurance.product.agent.request.default"
_DEFAULT_REQUEST_CONFIG = "assurance.product.agent.permission.default"
_DEFAULT_SECRETS: dict[AdapterName, tuple[str, ...]] = {
    "opencode": ("opencode.token",),
    "cursor": ("cursor.api-key",),
}


class _DeferredPhase:
    def __init__(self, handler_id: str, handler: object) -> None:
        self.handler_id = handler_id
        self.handler = handler

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(f"semantic attempt phase is not driven: {self.handler_id}")


class _DeferredTaskExecutor:
    def __init__(self, handler_id: str, handler: object) -> None:
        self.handler_id = handler_id
        self.handler = handler

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(f"semantic task attempt is not driven: {self.handler_id}")


def _prepare_id(contract: AgentExecutionContract[Any, Any, Any]) -> str:
    body = contract.contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, base = body.partition(".agent.")
    if marker != ".agent.":
        raise ValueError(f"invalid agent job contract id: {contract.contract_id!r}")
    return f"assurance.{feature}.{base}.prepare"


def _runtime_handler_owner(runtime_handler_id: str) -> str:
    owner, separator, leaf = runtime_handler_id.rpartition(".")
    if separator != "." or not owner or not leaf:
        raise ValueError(f"invalid runtime handler id: {runtime_handler_id!r}")
    return owner


def _catalog_binding(contract_id: str, adapter: AdapterName = _DEFAULT_ADAPTER) -> AgentRuntimeBinding:
    return AgentRuntimeBinding(
        contract_id=contract_id,
        runtime_handler_id=f"runtime.{adapter}.execute",
        provider=adapter,
        model=_DEFAULT_MODEL,
        policy=AgentRuntimePolicy(
            request_policy_handle=_DEFAULT_REQUEST_POLICY,
            request_config_handle=_DEFAULT_REQUEST_CONFIG,
        ),
        secret_handles=_DEFAULT_SECRETS[adapter],
    )


AGENT_RUNTIME_BINDINGS: Mapping[str, AgentRuntimeBinding] = MappingProxyType(
    {contract_id: _catalog_binding(contract_id) for contract_id in all_feature_agent_contracts()}
)


def _composition_adapter(composition: FrozenComposition) -> AdapterName:
    source = composition.manifest.source
    if source is None:
        raise ValueError("composition is missing a product entry-point")
    return adapter_for_entrypoint(source.entrypoint_name)


def runtime_bindings_from_composition(
    composition: FrozenComposition,
) -> Mapping[str, AgentRuntimeBinding]:
    adapter = _composition_adapter(composition)
    bindings = composition.registries.capabilities.bindings
    resolved: dict[str, AgentRuntimeBinding] = {}
    for contract_id, contract in all_feature_agent_contracts().items():
        prepare_alias, execute_alias, _finalize_alias = alias_ids_for_prepare(_prepare_id(contract))
        prepare = bindings[prepare_alias]
        execute = bindings[execute_alias]
        data = thaw_json(prepare.data)
        if not isinstance(data, Mapping):
            raise ValueError(f"prepare alias is missing assignment data: {prepare_alias}")
        execution = data.get("execution")
        if not isinstance(execution, Mapping):
            raise ValueError(f"prepare alias is missing execution assignment: {prepare_alias}")
        model = execution.get("provider_model")
        if not isinstance(model, str) or not model:
            raise ValueError(f"prepare alias is missing provider_model: {prepare_alias}")
        resource_ids = tuple(prepare.resource_ids)
        request_config = resource_ids[0] if resource_ids else _DEFAULT_REQUEST_CONFIG
        request_policy = resource_ids[1] if len(resource_ids) > 1 else _DEFAULT_REQUEST_POLICY
        resolved[contract_id] = AgentRuntimeBinding(
            contract_id=contract_id,
            runtime_handler_id=execute.target_capability_id,
            provider=adapter,
            model=model,
            policy=AgentRuntimePolicy(
                request_policy_handle=request_policy,
                request_config_handle=request_config,
            ),
            secret_handles=tuple(execute.secret_handles),
        )
    if set(resolved) != set(AGENT_RUNTIME_BINDINGS) or len(resolved) != 33:
        raise ValueError("composition runtime binding set is not the exact 33 Agent contracts")
    return MappingProxyType(resolved)


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


def _resolve_agent_contract(
    contract: AgentExecutionContract[Any, Any, Any],
    binding: AgentRuntimeBinding,
    composition: FrozenComposition,
) -> ResolvedAttemptContract[Any, Any]:
    handlers = composition.registries.capabilities.task_handlers
    executor = CompositeAttemptExecutor(
        contract,
        prepare=_DeferredPhase(contract.prepare_handler_id, handlers[contract.prepare_handler_id]),
        runtime=_DeferredPhase(binding.runtime_handler_id, handlers[binding.runtime_handler_id]),
        finalize=_DeferredPhase(contract.finalize_handler_id, handlers[contract.finalize_handler_id]),
        capabilities=AgentRuntimeCapabilities(provider_schema=False),
    )
    return executor.resolve()


def _resolve_task_contract(
    contract: TaskAttemptContract[Any, Any],
    composition: FrozenComposition,
) -> ResolvedAttemptContract[Any, Any]:
    handler = composition.registries.capabilities.task_handlers[contract.handler_id]
    return resolve_contract(contract, executor=_DeferredTaskExecutor(contract.handler_id, handler))


def boot_semantic_attempt_contracts(
    composition: FrozenComposition,
    bindings: Mapping[str, AgentRuntimeBinding] | None = None,
) -> Mapping[str, ResolvedAttemptContract[Any, Any]]:
    catalog = dict(runtime_bindings_from_composition(composition))
    if bindings is not None:
        catalog.update(bindings)
    for binding in catalog.values():
        _authenticate_runtime_binding(composition, binding)
    resolved: dict[str, ResolvedAttemptContract[Any, Any]] = {}
    agents = all_feature_agent_contracts()
    for contract_id, binding in catalog.items():
        resolved[contract_id] = _resolve_agent_contract(agents[contract_id], binding, composition)
    for contract in all_feature_task_contracts().values():
        resolved[contract.contract_id] = _resolve_task_contract(contract, composition)
    if len(resolved) != 41:
        raise ValueError(f"semantic attempt registry must contain 41 contracts, got {len(resolved)}")
    return MappingProxyType(resolved)


__all__ = [
    "AGENT_RUNTIME_BINDINGS",
    "boot_semantic_attempt_contracts",
    "runtime_bindings_from_composition",
]
