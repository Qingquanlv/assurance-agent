from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from graph_engine.boot import FeatureFactoryRef, FeatureSpec
from graph_engine.plugin_api import ProviderSource
from graph_engine.plugin_kit import CapabilityPlugin, CapabilitySpec


class DemoPlugin(CapabilityPlugin):
    spec = CapabilitySpec(
        plugin_id="assurance.demo",
        version="0.1.0",
        engine_api="2.0",
        source=ProviderSource(
            distribution="assurance-demo",
            version="0.1.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="demo",
            entrypoint_value="demo.plugin:DemoPlugin",
            declaration_path="demo/plugin-declaration.json",
            import_roots=("",),
        ),
        resource_bytes=lambda _: b"",
        schema_files={},
        resource_files={},
        task_handlers={},
    )


@dataclass(frozen=True)
class DemoContract:
    contract_id: str
    owner_id: str = "assurance.demo"


class DemoTask:
    contract = DemoContract("assurance.demo.agent.demo.v1")


def _feature(
    *,
    agent_contracts: Mapping[str, DemoContract] | None = None,
    output_route_templates: Mapping[str, tuple[str, ...]] | None = None,
    graph_factory: FeatureFactoryRef | None = None,
    agent_task_types: tuple[type[Any], ...] | None = None,
) -> FeatureSpec[DemoContract]:
    return FeatureSpec(
        plugin=DemoPlugin,
        agent_contracts={"demo": DemoTask.contract} if agent_contracts is None else agent_contracts,
        task_contracts={},
        output_route_templates=(
            {"demo": ("out/{change_id}.json",)} if output_route_templates is None else output_route_templates
        ),
        graph_factory=graph_factory or FeatureFactoryRef("assurance.demo", "demo.graphs.factory:build"),
        agent_task_types=(DemoTask,) if agent_task_types is None else agent_task_types,
    )


def test_feature_spec_snapshots_catalogs() -> None:
    agents = {"demo": DemoTask.contract}
    feature = _feature(agent_contracts=agents)
    agents.clear()
    assert tuple(feature.agent_contracts) == ("demo",)
    with pytest.raises(TypeError):
        feature.agent_contracts["other"] = DemoTask.contract  # type: ignore[index]


def test_feature_spec_rejects_plugin_factory_owner_drift() -> None:
    with pytest.raises(ValueError, match="feature owner"):
        _feature(graph_factory=FeatureFactoryRef("assurance.other", "demo.graphs:build"))


def test_feature_spec_rejects_missing_or_duplicate_agent_task() -> None:
    with pytest.raises(ValueError, match="agent task"):
        _feature(agent_task_types=())
    with pytest.raises(ValueError, match="agent task"):
        _feature(agent_task_types=(DemoTask, DemoTask))


def test_feature_spec_rejects_missing_output_route() -> None:
    with pytest.raises(ValueError, match="output route"):
        _feature(output_route_templates={})
