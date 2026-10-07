from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_improvement.plugin import IMPROVEMENT_SOURCE, ImprovementPlugin
from assurance_improvement.resource_loader import resource_bytes
from tests.capabilities.conformance import PluginExpectation, assert_plugin_conforms


def test_improvement_descriptor_declares_exact_dependency_versions() -> None:
    assert ImprovementPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.3.0"),
        PluginDependency("assurance.generation", "==0.3.0"),
        PluginDependency("assurance.execution", "==0.3.0"),
        PluginDependency("assurance.healing", "==0.3.0"),
        PluginDependency("assurance.quality", "==0.3.0"),
    )


def test_improvement_plugin_conforms() -> None:
    assert_plugin_conforms(
        ImprovementPlugin(),
        PluginExpectation(
            plugin_id="assurance.improvement",
            dependencies=(
                "assurance.intake",
                "assurance.generation",
                "assurance.execution",
                "assurance.healing",
                "assurance.quality",
            ),
            id_prefix="assurance.improvement.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_improvement").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == ImprovementPlugin.descriptor()


def test_improvement_source_identity() -> None:
    source = ImprovementPlugin.descriptor().source
    assert source == IMPROVEMENT_SOURCE
    assert source == ProviderSource(
        distribution="assurance-improvement",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="improvement",
        entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
        declaration_path="assurance_improvement/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = ImprovementPlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert ENGINE_API_VERSION == "2.0"
    assert descriptor.schemas == tuple(sorted(descriptor.schemas))
    assert len(descriptor.schemas) == 7
    assert descriptor.task_handlers == tuple(sorted(descriptor.task_handlers))
    assert descriptor.commit_validators == tuple(sorted(descriptor.commit_validators))
    assert descriptor.resources == tuple(sorted(descriptor.resources))
    assert "assurance.improvement.retro.prepare" in descriptor.task_handlers
    assert "assurance.improvement.improvement-review.finalize" in descriptor.task_handlers
    assert "assurance.improvement.archive.prepare" in descriptor.task_handlers
    assert "assurance.improvement.retro-collect-v3" in descriptor.task_handlers
    assert "assurance.improvement.rollback-memory-improvement" in descriptor.task_handlers
    assert descriptor.commit_validators == ()
    assert "assurance.improvement.skill.aa-improvement-reviewer.v1" in descriptor.resources
    assert descriptor.bindings == ()


def test_improvement_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        ImprovementPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/retro-context.v3.schema.json")


def test_improvement_plugin_does_not_discover_graph_factory() -> None:
    descriptor = ImprovementPlugin.descriptor()
    contribution = ImprovementPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert descriptor.source is not None
    assert descriptor.source.entrypoint_value == "assurance_improvement.plugin:ImprovementPlugin"
    assert "graphs.factory" not in descriptor.source.entrypoint_value
    assert "assurance.improvement.workflow.module.v1" not in descriptor.resources
    assert not hasattr(contribution, "graph_factories")
    assert contribution.attempt_contracts
    assert all("graphs.factory" not in ref.contract_id for ref in contribution.attempt_contracts)
