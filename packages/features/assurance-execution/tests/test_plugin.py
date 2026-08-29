from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_execution.plugin import (
    EXECUTION_HANDLER_IDS,
    EXECUTION_RESOURCE_IDS,
    EXECUTION_SCHEMA_IDS,
    EXECUTION_SOURCE,
    EXECUTION_VALIDATOR_IDS,
    ExecutionPlugin,
)
from assurance_execution.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_execution_descriptor_has_exact_dependencies() -> None:
    assert ExecutionPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.2.0"),
        PluginDependency("assurance.generation", "==0.2.0"),
    )


def test_execution_plugin_conforms() -> None:
    assert_plugin_conforms(
        ExecutionPlugin(),
        PluginExpectation(
            plugin_id="assurance.execution",
            dependencies=("assurance.intake", "assurance.generation"),
            id_prefix="assurance.execution.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_execution").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == ExecutionPlugin.descriptor()


def test_execution_source_identity() -> None:
    source = ExecutionPlugin.descriptor().source
    assert source == EXECUTION_SOURCE
    assert source == ProviderSource(
        distribution="assurance-execution",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="execution",
        entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
        declaration_path="assurance_execution/plugin-declaration.json",
        import_roots=("",),
    )
    assert ExecutionPlugin.descriptor().engine_api == ENGINE_API_VERSION
    assert ExecutionPlugin.descriptor().schemas == EXECUTION_SCHEMA_IDS
    assert tuple(EXECUTION_SCHEMA_IDS) == tuple(sorted(EXECUTION_SCHEMA_IDS))
    assert ExecutionPlugin.descriptor().task_handlers == EXECUTION_HANDLER_IDS
    assert ExecutionPlugin.descriptor().commit_validators == EXECUTION_VALIDATOR_IDS
    assert ExecutionPlugin.descriptor().resources == EXECUTION_RESOURCE_IDS
    assert tuple(EXECUTION_HANDLER_IDS) == tuple(sorted(EXECUTION_HANDLER_IDS))
    assert tuple(EXECUTION_VALIDATOR_IDS) == tuple(sorted(EXECUTION_VALIDATOR_IDS))
    assert tuple(EXECUTION_RESOURCE_IDS) == tuple(sorted(EXECUTION_RESOURCE_IDS))
    assert ExecutionPlugin.descriptor().effects == ()
    assert ExecutionPlugin.descriptor().bindings == ()


def test_execution_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        ExecutionPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/closed-mapping.v1.schema.json")
