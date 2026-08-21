from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_generation.plugin import (
    GENERATION_HANDLER_IDS,
    GENERATION_RESOURCE_IDS,
    GENERATION_SCHEMA_IDS,
    GENERATION_SOURCE,
    GENERATION_VALIDATOR_IDS,
    GenerationPlugin,
)
from assurance_generation.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_generation_descriptor_declares_only_intake_dependency() -> None:
    descriptor = GenerationPlugin.descriptor()
    assert descriptor.dependencies == (PluginDependency("assurance.intake", "==0.1.0"),)


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
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == GenerationPlugin.descriptor()


def test_generation_source_identity() -> None:
    source = GenerationPlugin.descriptor().source
    assert source == GENERATION_SOURCE
    assert source == ProviderSource(
        distribution="assurance-generation",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    )
    assert GenerationPlugin.descriptor().engine_api == ENGINE_API_VERSION
    assert GenerationPlugin.descriptor().schemas == GENERATION_SCHEMA_IDS
    assert tuple(GENERATION_SCHEMA_IDS) == tuple(sorted(GENERATION_SCHEMA_IDS))
    assert GenerationPlugin.descriptor().task_handlers == GENERATION_HANDLER_IDS
    assert GenerationPlugin.descriptor().commit_validators == GENERATION_VALIDATOR_IDS
    assert GenerationPlugin.descriptor().resources == GENERATION_RESOURCE_IDS
    assert tuple(GENERATION_HANDLER_IDS) == tuple(sorted(GENERATION_HANDLER_IDS))
    assert tuple(GENERATION_VALIDATOR_IDS) == tuple(sorted(GENERATION_VALIDATOR_IDS))
    assert tuple(GENERATION_RESOURCE_IDS) == tuple(sorted(GENERATION_RESOURCE_IDS))
    assert GenerationPlugin.descriptor().effects == ()
    assert GenerationPlugin.descriptor().bindings == ()


def test_generation_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        GenerationPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/plan-check.v1.schema.json")
