from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDescriptor, ProviderSource

from assurance_intake.plugin import INTAKE_SOURCE, IntakePlugin
from assurance_intake.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_intake_plugin_conforms() -> None:
    assert_plugin_conforms(
        IntakePlugin(),
        PluginExpectation(
            plugin_id="assurance.intake",
            dependencies=(),
            id_prefix="assurance.intake.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_intake").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == IntakePlugin.descriptor()


def test_intake_source_identity() -> None:
    source = IntakePlugin.descriptor().source
    assert source == INTAKE_SOURCE
    assert source == ProviderSource(
        distribution="assurance-intake",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="intake",
        entrypoint_value="assurance_intake.plugin:IntakePlugin",
        declaration_path="assurance_intake/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = IntakePlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert descriptor.schemas == tuple(sorted(descriptor.schemas))


def test_intake_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        IntakePlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/case.v1.schema.json")
