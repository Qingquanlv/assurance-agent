from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_healing.plugin import HEALING_SOURCE, HealingPlugin
from assurance_healing.resource_loader import resource_bytes
from tests.capabilities.conformance import PluginExpectation, assert_plugin_conforms


def test_healing_descriptor_declares_exact_upstream_dependencies() -> None:
    assert tuple(item.plugin_id for item in HealingPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
    )
    assert HealingPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.3.0"),
        PluginDependency("assurance.generation", "==0.3.0"),
        PluginDependency("assurance.execution", "==0.3.0"),
    )


def test_healing_plugin_conforms() -> None:
    assert_plugin_conforms(
        HealingPlugin(),
        PluginExpectation(
            plugin_id="assurance.healing",
            dependencies=("assurance.intake", "assurance.generation", "assurance.execution"),
            id_prefix="assurance.healing.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_healing").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == HealingPlugin.descriptor()


def test_healing_source_identity() -> None:
    source = HealingPlugin.descriptor().source
    assert source == HEALING_SOURCE
    assert source == ProviderSource(
        distribution="assurance-healing",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = HealingPlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert ENGINE_API_VERSION == "2.0"
    assert descriptor.schemas == tuple(sorted(descriptor.schemas))
    assert descriptor.resources == tuple(sorted(descriptor.resources))
    assert descriptor.task_handlers == tuple(sorted(descriptor.task_handlers))
    assert descriptor.commit_validators == tuple(sorted(descriptor.commit_validators))
    assert descriptor.bindings == ()


def test_healing_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        HealingPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/fix-proposal.v1.schema.json")


def test_healing_plugin_does_not_discover_graph_factory() -> None:
    descriptor = HealingPlugin.descriptor()
    contribution = HealingPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert descriptor.source is not None
    assert descriptor.source.entrypoint_value == "assurance_healing.plugin:HealingPlugin"
    assert "graphs.factory" not in descriptor.source.entrypoint_value
    assert "assurance.healing.workflow.module.v1" not in descriptor.resources
    assert not hasattr(contribution, "graph_factories")
    assert contribution.attempt_contracts
    assert all("graphs.factory" not in ref.contract_id for ref in contribution.attempt_contracts)
