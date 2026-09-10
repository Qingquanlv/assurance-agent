from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_telemetry.plugin import TELEMETRY_SOURCE, TelemetryPlugin
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_telemetry_descriptor_has_exact_dependencies() -> None:
    assert TelemetryPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.2.0"),
        PluginDependency("assurance.generation", "==0.2.0"),
    )


def test_telemetry_plugin_conforms() -> None:
    assert_plugin_conforms(
        TelemetryPlugin(),
        PluginExpectation(
            plugin_id="assurance.telemetry",
            dependencies=("assurance.intake", "assurance.generation"),
            id_prefix="assurance.telemetry.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_telemetry").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == TelemetryPlugin.descriptor()


def test_telemetry_source_identity() -> None:
    source = TelemetryPlugin.descriptor().source
    assert source == TELEMETRY_SOURCE
    assert source == ProviderSource(
        distribution="assurance-telemetry",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="telemetry",
        entrypoint_value="assurance_telemetry.plugin:TelemetryPlugin",
        declaration_path="assurance_telemetry/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = TelemetryPlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert descriptor.task_handlers == ()
    assert descriptor.commit_validators == ()
    assert descriptor.effects == ()
    assert descriptor.bindings == ()


def test_telemetry_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        TelemetryPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_telemetry_adds_no_graph_nodes() -> None:
    import inspect

    from assurance_product.graphs import full as full_mod

    text = inspect.getsource(full_mod.build_full_graph)
    assert "telemetry" not in text
    assert 'add_node("seal' not in text
