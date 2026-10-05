"""Plugin registrations must be reachable from a published attempt contract."""

from __future__ import annotations

from graph_engine import ENGINE_API_VERSION, RegistryPorts

from assurance_execution.plugin import ExecutionPlugin
from assurance_generation.plugin import GenerationPlugin
from assurance_healing.plugin import HealingPlugin
from assurance_improvement.plugin import ImprovementPlugin
from assurance_intake.plugin import IntakePlugin
from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts
from assurance_quality.plugin import QualityPlugin

_PLUGINS = (
    IntakePlugin,
    GenerationPlugin,
    ExecutionPlugin,
    HealingPlugin,
    QualityPlugin,
    ImprovementPlugin,
)


def _referenced_handler_ids() -> set[str]:
    referenced: set[str] = set()
    for contract in all_feature_agent_contracts().values():
        referenced.add(contract.prepare_handler_id)
        referenced.add(contract.finalize_handler_id)
    for contract in all_feature_task_contracts().values():
        referenced.add(contract.handler_id)
    return referenced


def _referenced_validator_ids() -> set[str]:
    referenced: set[str] = set()
    catalogs = (
        *all_feature_agent_contracts().values(),
        *all_feature_task_contracts().values(),
    )
    for contract in catalogs:
        referenced.update(contract.validators)
    return referenced


def test_registered_task_handlers_are_referenced_by_a_contract() -> None:
    referenced = _referenced_handler_ids()
    missing = [
        handler_id
        for plugin in _PLUGINS
        for handler_id in plugin.descriptor().task_handlers
        if handler_id not in referenced
    ]
    assert missing == []


def test_registered_commit_validators_are_referenced_by_a_contract() -> None:
    referenced = _referenced_validator_ids()
    missing = [
        validator_id
        for plugin in _PLUGINS
        for validator_id in plugin()
        .contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
        .commit_validators
        if validator_id not in referenced
    ]
    assert missing == []
