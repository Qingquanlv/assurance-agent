from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from agent_runtime_contracts import AgentExecutionContract, expand_agent_job_slots
from graph_engine.composition.models import WorkflowModuleRequirement, WorkflowSlotBinding
from graph_engine.graph.schema import RetryPolicyDef, WorkflowDef
from graph_engine.plugin_api import ResourceClaims

from assurance_execution.contracts.workflow import AGENT_JOB_CONTRACTS as EXECUTION_AGENT_JOB_CONTRACTS
from assurance_generation.contracts.workflow import AGENT_JOB_CONTRACTS as GENERATION_AGENT_JOB_CONTRACTS
from assurance_healing.contracts.workflow import AGENT_JOB_CONTRACTS as HEALING_AGENT_JOB_CONTRACTS
from assurance_improvement.contracts.workflow import AGENT_JOB_CONTRACTS as IMPROVEMENT_AGENT_JOB_CONTRACTS
from assurance_intake.contracts.workflow import AGENT_JOB_CONTRACTS as INTAKE_AGENT_JOB_CONTRACTS
from assurance_quality.contracts.workflow import AGENT_JOB_CONTRACTS as QUALITY_AGENT_JOB_CONTRACTS

FEATURE_AGENT_JOB_CATALOGS: tuple[Mapping[str, AgentExecutionContract], ...] = (
    INTAKE_AGENT_JOB_CONTRACTS,
    GENERATION_AGENT_JOB_CONTRACTS,
    EXECUTION_AGENT_JOB_CONTRACTS,
    QUALITY_AGENT_JOB_CONTRACTS,
    HEALING_AGENT_JOB_CONTRACTS,
    IMPROVEMENT_AGENT_JOB_CONTRACTS,
)


def _prepare_id(contract: AgentExecutionContract) -> str:
    body = contract.contract_id.removeprefix("assurance.").removesuffix(".v1")
    feature, marker, base = body.partition(".agent.")
    if marker != ".agent.":
        raise ValueError(f"invalid agent job contract id: {contract.contract_id!r}")
    return f"assurance.{feature}.{base}.prepare"


AGENT_EXECUTION_CONTRACTS: Mapping[str, AgentExecutionContract] = MappingProxyType(
    {
        _prepare_id(contract): contract
        for catalog in FEATURE_AGENT_JOB_CATALOGS
        for contract in catalog.values()
    }
)
PREPARE_IDS: tuple[str, ...] = tuple(AGENT_EXECUTION_CONTRACTS)


def _slot_parts(alias: str) -> tuple[str, str, str]:
    rest = alias.removeprefix("assurance.product.agent.")
    feature, _, remainder = rest.partition(".")
    base, _, phase = remainder.rpartition(".")
    if not feature or not base or phase not in {"prepare", "execute", "finalize"}:
        raise ValueError(f"invalid product agent alias: {alias!r}")
    return feature, base, phase


def product_workflow_module_requirements() -> tuple[WorkflowModuleRequirement, ...]:
    from assurance_product.models import FEATURE_WORKFLOW_OWNERS

    return tuple(
        WorkflowModuleRequirement(
            module_id=f"{owner}.workflow",
            owner_id=owner,
            resource_id=f"{owner}.workflow.module.v1",
        )
        for owner in FEATURE_WORKFLOW_OWNERS
    )


def product_workflow_slot_bindings() -> tuple[WorkflowSlotBinding, ...]:
    expanded = expand_agent_job_slots(FEATURE_AGENT_JOB_CATALOGS)
    return tuple(
        sorted(
            (
                WorkflowSlotBinding(
                    module_id=f"assurance.{feature}.workflow",
                    slot=f"{base}.{phase}",
                    capability_id=alias,
                    contract_id=contract.contract_id,
                )
                for alias, contract in expanded.items()
                for feature, base, phase in (_slot_parts(alias),)
            ),
            key=lambda item: (item.module_id, item.slot),
        )
    )


_AGENT_RETRY_NAME = "agent-transient"
_AGENT_RETRY_POLICY = RetryPolicyDef(max_attempts=12, retry_on=("transient",))


def bind_agent_execution_contracts(workflow: WorkflowDef) -> WorkflowDef:
    from assurance_product.models import alias_ids_for_prepare
    from assurance_product.output_routes import OutputRouteCatalog, execute_alias_for_prepare

    expected = set(PREPARE_IDS)
    actual = set(AGENT_EXECUTION_CONTRACTS)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"agent execution contract set drifted; missing={missing}, extra={extra}")
    catalog_aliases = set(OutputRouteCatalog().aliases())
    execute_aliases = {execute_alias_for_prepare(prepare_id) for prepare_id in PREPARE_IDS}
    if catalog_aliases != execute_aliases:
        missing = sorted(execute_aliases - catalog_aliases)
        extra = sorted(catalog_aliases - execute_aliases)
        raise ValueError(f"output route catalog drifted; missing={missing}, extra={extra}")

    retry = dict(workflow.retry)
    existing_retry = retry.get(_AGENT_RETRY_NAME)
    if existing_retry is not None and existing_retry != _AGENT_RETRY_POLICY:
        raise ValueError(f"workflow retry policy {_AGENT_RETRY_NAME!r} conflicts with agent contract")
    retry[_AGENT_RETRY_NAME] = _AGENT_RETRY_POLICY

    by_execute = {
        alias_ids_for_prepare(prepare_id)[1]: contract
        for prepare_id, contract in AGENT_EXECUTION_CONTRACTS.items()
    }
    seen: set[str] = set()
    graphs = {}
    for graph_id, graph in workflow.graphs.items():
        nodes = {}
        for node_id, node in graph.nodes.items():
            contract = by_execute.get(node.capability or "")
            if contract is None:
                nodes[node_id] = node
                continue
            if node.resources != ResourceClaims():
                raise ValueError(
                    f"agent execute node duplicates its execution contract: {graph_id}/{node_id}"
                )
            assert node.capability is not None
            seen.add(node.capability)
            nodes[node_id] = node.model_copy(
                update={"resources": contract.resources, "retry": _AGENT_RETRY_NAME}
            )
        graphs[graph_id] = graph.model_copy(update={"nodes": nodes})
    missing_execute = sorted(set(by_execute) - seen)
    if missing_execute:
        raise ValueError(f"workflow is missing contracted agent execute aliases: {missing_execute}")
    return workflow.model_copy(update={"graphs": graphs, "retry": retry})


__all__ = [
    "AGENT_EXECUTION_CONTRACTS",
    "FEATURE_AGENT_JOB_CATALOGS",
    "PREPARE_IDS",
    "AgentExecutionContract",
    "bind_agent_execution_contracts",
    "expand_agent_job_slots",
    "product_workflow_module_requirements",
    "product_workflow_slot_bindings",
]
