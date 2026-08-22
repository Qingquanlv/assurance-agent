from __future__ import annotations

import json
from importlib.resources import files

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_quality.plugin import QUALITY_SCHEMA_IDS, QUALITY_SOURCE, QualityPlugin
from assurance_quality.resource_loader import resource_bytes
from tests.phase4.conformance import PluginExpectation, assert_plugin_conforms


def test_quality_descriptor_declares_exact_dependency_versions() -> None:
    assert QualityPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.1.0"),
        PluginDependency("assurance.generation", "==0.1.0"),
        PluginDependency("assurance.execution", "==0.1.0"),
        PluginDependency("assurance.healing", "==0.1.0"),
    )


def test_quality_plugin_conforms() -> None:
    assert_plugin_conforms(
        QualityPlugin(),
        PluginExpectation(
            plugin_id="assurance.quality",
            dependencies=(
                "assurance.intake",
                "assurance.generation",
                "assurance.execution",
                "assurance.healing",
            ),
            id_prefix="assurance.quality.",
        ),
    )


def test_static_declaration_equals_live_descriptor() -> None:
    static = json.loads(
        files("assurance_quality").joinpath("plugin-declaration.json").read_text(encoding="utf-8")
    )
    payload = static["descriptor"] if isinstance(static, dict) and "descriptor" in static else static
    assert PluginDescriptor.model_validate(payload) == QualityPlugin.descriptor()


def test_quality_source_identity() -> None:
    source = QualityPlugin.descriptor().source
    assert source == QUALITY_SOURCE
    assert source == ProviderSource(
        distribution="assurance-quality",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    )
    assert QualityPlugin.descriptor().engine_api == ENGINE_API_VERSION
    assert ENGINE_API_VERSION == "2.0"
    assert QualityPlugin.descriptor().schemas == QUALITY_SCHEMA_IDS
    assert tuple(QUALITY_SCHEMA_IDS) == tuple(sorted(QUALITY_SCHEMA_IDS))
    assert len(QUALITY_SCHEMA_IDS) == 22
    from assurance_quality.plugin import QUALITY_HANDLER_IDS, QUALITY_VALIDATOR_IDS

    assert QualityPlugin.descriptor().task_handlers == QUALITY_HANDLER_IDS
    assert QualityPlugin.descriptor().commit_validators == QUALITY_VALIDATOR_IDS
    assert tuple(QUALITY_HANDLER_IDS) == tuple(sorted(QUALITY_HANDLER_IDS))
    assert tuple(QUALITY_VALIDATOR_IDS) == tuple(sorted(QUALITY_VALIDATOR_IDS))
    assert "assurance.quality.inspect" in QUALITY_HANDLER_IDS
    assert "assurance.quality.generate-report" in QUALITY_HANDLER_IDS
    assert "assurance.quality.dashboard" in QUALITY_HANDLER_IDS
    assert "assurance.quality.validator.report.v1" in QUALITY_VALIDATOR_IDS
    assert QualityPlugin.descriptor().resources
    assert QualityPlugin.descriptor().effects == ()
    assert QualityPlugin.descriptor().bindings == ()


def test_quality_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        QualityPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/trace.v2.schema.json")
