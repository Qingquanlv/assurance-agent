from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import (
    AgentExecutionContract,
    AgentRuntimeBinding,
    AgentRuntimePolicy,
    RawAgentRuntimeBindingProjectionV1,
    RawAgentRuntimeOutcome,
    ResolvedRawAgentExecutor,
)
from graph_engine.attempts import ResolvedAttemptContract, TaskAttemptContract, resolve_contract
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.composition import FrozenComposition

from assurance_product.agent_contracts import (
    all_feature_agent_contracts,
    all_feature_task_contracts,
)
from assurance_product.models import AdapterName
from assurance_product.source_catalog import adapter_for_entrypoint

_DEFAULT_ADAPTER: AdapterName = "opencode"
_DEFAULT_MODEL = "fixture-model"
_DEFAULT_REQUEST_POLICY = "assurance.product.agent.request.default"
_DEFAULT_REQUEST_CONFIG = "assurance.product.agent.permission.default"
_DEFAULT_SECRETS: dict[AdapterName, tuple[str, ...]] = {
    "opencode": ("opencode.token",),
    "cursor": ("cursor.api-key",),
}
_ACTIVITY_RECOVERY = "adopt-observe-reconcile-v1"


class _DeferredPhase:
    def __init__(self, handler_id: str, handler: object) -> None:
        self.handler_id = handler_id
        self.handler = handler

    async def execute(
        self,
        prepared: object,
        context: AttemptExecutionContext,
    ) -> RawAgentRuntimeOutcome:
        raise RuntimeError(f"semantic attempt phase is not driven: {self.handler_id}")


class _DeferredTaskExecutor:
    def __init__(self, handler_id: str, handler: object) -> None:
        self.handler_id = handler_id
        self.handler = handler

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(f"semantic task attempt is not driven: {self.handler_id}")


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


def project_raw_agent_runtime_binding(
    binding: AgentRuntimeBinding,
    contract: AgentExecutionContract[Any, Any, Any],
    *,
    adapter: str,
) -> RawAgentRuntimeBindingProjectionV1:
    return RawAgentRuntimeBindingProjectionV1(
        schema_version="raw-agent-runtime-binding-v1",
        contract_id=binding.contract_id,
        contract_digest=_contract_digest(contract),
        runtime_handler_id=binding.runtime_handler_id,
        adapter=adapter,
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
    adapter: str,
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
    return tuple(seen[contract_id] for contract_id in sorted(contracts))


AGENT_RUNTIME_BINDINGS: Mapping[str, AgentRuntimeBinding] = MappingProxyType(
    {contract_id: _catalog_binding(contract_id) for contract_id in all_feature_agent_contracts()}
)
RAW_AGENT_RUNTIME_BINDING_ROWS: tuple[RawAgentRuntimeBindingProjectionV1, ...] = (
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(
                AGENT_RUNTIME_BINDINGS[contract_id],
                contract,
                adapter=_DEFAULT_ADAPTER,
            )
            for contract_id, contract in sorted(all_feature_agent_contracts().items())
        ),
        all_feature_agent_contracts(),
        adapter=_DEFAULT_ADAPTER,
    )
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
    contracts = all_feature_agent_contracts()
    resolved = {contract_id: _catalog_binding(contract_id, adapter) for contract_id in contracts}
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, contracts[contract_id], adapter=adapter)
            for contract_id, binding in sorted(resolved.items())
        ),
        contracts,
        adapter=adapter,
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
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=cast(Any, _DeferredPhase(contract.prepare_handler_id, handlers[contract.prepare_handler_id])),
        runtime=_DeferredPhase(binding.runtime_handler_id, handlers[binding.runtime_handler_id]),
        finalize=cast(
            Any, _DeferredPhase(contract.finalize_handler_id, handlers[contract.finalize_handler_id])
        ),
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
    adapter = _composition_adapter(composition)
    agents = all_feature_agent_contracts()
    authenticate_raw_agent_runtime_bindings(
        tuple(
            project_raw_agent_runtime_binding(binding, agents[binding.contract_id], adapter=adapter)
            for binding in catalog.values()
        ),
        agents,
        adapter=adapter,
    )
    resolved: dict[str, ResolvedAttemptContract[Any, Any]] = {}
    for contract_id, binding in catalog.items():
        resolved[contract_id] = _resolve_agent_contract(agents[contract_id], binding, composition)
    for contract in all_feature_task_contracts().values():
        resolved[contract.contract_id] = _resolve_task_contract(contract, composition)
    if len(resolved) != 41:
        raise ValueError(f"semantic attempt registry must contain 41 contracts, got {len(resolved)}")
    return MappingProxyType(resolved)


__all__ = [
    "AGENT_RUNTIME_BINDINGS",
    "RAW_AGENT_RUNTIME_BINDING_ROWS",
    "authenticate_raw_agent_runtime_bindings",
    "boot_semantic_attempt_contracts",
    "project_raw_agent_runtime_binding",
    "runtime_bindings_from_composition",
]
