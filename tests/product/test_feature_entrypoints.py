from __future__ import annotations

from importlib import import_module

import pytest

from graph_engine.boot import FeatureSpec


@pytest.mark.parametrize(
    ("module", "owner", "agents", "tasks", "symbol"),
    [
        ("assurance_intake", "assurance.intake", 4, 2, "build_intake_graphs"),
        ("assurance_generation", "assurance.generation", 8, 3, "build_generation_graphs"),
        ("assurance_execution", "assurance.execution", 0, 2, "build_execution_graphs"),
        ("assurance_quality", "assurance.quality", 5, 3, "build_quality_graphs"),
        ("assurance_healing", "assurance.healing", 3, 0, "build_healing_graphs"),
        ("assurance_improvement", "assurance.improvement", 6, 9, "build_improvement_graphs"),
    ],
)
def test_capability_feature_exports_existing_contracts_and_graph_factory(
    module: str, owner: str, agents: int, tasks: int, symbol: str
) -> None:
    feature: FeatureSpec = import_module(f"{module}.feature").FEATURE

    assert feature.graph_factory.owner_id == owner
    assert feature.graph_factory.symbol == f"{module}.graphs.factory:{symbol}"
    assert feature.plugin.descriptor().plugin_id == owner
    assert len(feature.agent_contracts) == len(feature.agent_task_types) == agents
    assert len(feature.task_contracts) == tasks
    assert {task.contract.contract_id for task in feature.agent_task_types} == {
        contract.contract_id for contract in feature.agent_contracts.values()
    }
