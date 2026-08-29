from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_healing.plugin import (
    HEALING_RESOURCE_IDS,
    HEALING_SCHEMA_IDS,
    HEALING_SOURCE,
    HealingPlugin,
)
from assurance_healing.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_healing_descriptor_declares_exact_upstream_dependencies() -> None:
    assert tuple(item.plugin_id for item in HealingPlugin.descriptor().dependencies) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
    )
    assert HealingPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.2.0"),
        PluginDependency("assurance.generation", "==0.2.0"),
        PluginDependency("assurance.execution", "==0.2.0"),
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
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    )
    assert HealingPlugin.descriptor().engine_api == ENGINE_API_VERSION
    assert ENGINE_API_VERSION == "2.0"
    assert HealingPlugin.descriptor().schemas == HEALING_SCHEMA_IDS
    assert tuple(HEALING_SCHEMA_IDS) == tuple(sorted(HEALING_SCHEMA_IDS))
    assert HealingPlugin.descriptor().resources == HEALING_RESOURCE_IDS
    assert tuple(HEALING_RESOURCE_IDS) == tuple(sorted(HEALING_RESOURCE_IDS))
    from assurance_healing.plugin import (
        HEALING_EFFECT_IDS,
        HEALING_HANDLER_IDS,
        HEALING_VALIDATOR_IDS,
    )

    assert HealingPlugin.descriptor().task_handlers == HEALING_HANDLER_IDS
    assert HealingPlugin.descriptor().commit_validators == HEALING_VALIDATOR_IDS
    assert HealingPlugin.descriptor().effects == HEALING_EFFECT_IDS
    assert tuple(HEALING_HANDLER_IDS) == tuple(sorted(HEALING_HANDLER_IDS))
    assert tuple(HEALING_VALIDATOR_IDS) == tuple(sorted(HEALING_VALIDATOR_IDS))
    assert tuple(HEALING_EFFECT_IDS) == tuple(sorted(HEALING_EFFECT_IDS))
    assert HealingPlugin.descriptor().bindings == ()


def test_healing_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        HealingPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/fix-proposal.v1.schema.json")
