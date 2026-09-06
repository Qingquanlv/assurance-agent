from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.composition.sources import WheelPluginDeclaration
from graph_engine.plugin_api import PluginDependency, ProviderSource

from assurance_generation.plugin import GENERATION_SOURCE, GenerationPlugin
from assurance_generation.operations import generation_handlers
from assurance_generation.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_generation_descriptor_declares_only_intake_dependency() -> None:
    descriptor = GenerationPlugin.descriptor()
    assert descriptor.dependencies == (PluginDependency("assurance.intake", "==0.2.0"),)


def test_generation_plugin_conforms() -> None:
    assert_plugin_conforms(
        GenerationPlugin(),
        PluginExpectation(
            plugin_id="assurance.generation",
            dependencies=("assurance.intake",),
            id_prefix="assurance.generation.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_generation").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    declaration = WheelPluginDeclaration.model_validate(static)
    assert declaration.descriptor == GenerationPlugin.descriptor()
    assert declaration.source == GENERATION_SOURCE


def test_generation_source_identity() -> None:
    source = GenerationPlugin.descriptor().source
    assert source == GENERATION_SOURCE
    assert source == ProviderSource(
        distribution="assurance-generation",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = GenerationPlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert descriptor.schemas == tuple(sorted(descriptor.schemas))
    assert descriptor.task_handlers == tuple(sorted(descriptor.task_handlers))
    assert descriptor.commit_validators == tuple(sorted(descriptor.commit_validators))
    assert descriptor.resources == tuple(sorted(descriptor.resources))
    assert descriptor.effects == ()
    assert descriptor.bindings == ()


def test_generation_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        GenerationPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_generation_handlers_are_reconstructible_by_the_production_worker() -> None:
    for handler in generation_handlers().values():
        assert type(handler)() is not None


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/plan-check.v1.schema.json")


def test_generation_plugin_does_not_discover_graph_factory() -> None:
    descriptor = GenerationPlugin.descriptor()
    contribution = GenerationPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert descriptor.source is not None
    assert descriptor.source.entrypoint_value == "assurance_generation.plugin:GenerationPlugin"
    assert "graphs.factory" not in descriptor.source.entrypoint_value
    assert "assurance.generation.workflow.module.v1" not in descriptor.resources
    assert not hasattr(contribution, "graph_factories")
    assert contribution.attempt_contracts
    assert all("graphs.factory" not in ref.contract_id for ref in contribution.attempt_contracts)
