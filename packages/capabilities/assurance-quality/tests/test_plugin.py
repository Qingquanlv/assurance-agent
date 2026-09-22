from __future__ import annotations

import inspect
import json
from importlib.resources import files
from types import FunctionType

import pytest

from graph_engine import ENGINE_API_VERSION, RegistryPorts
from graph_engine.plugin_api import PluginDependency, PluginDescriptor, ProviderSource

from assurance_quality.operations import quality_handlers
from assurance_quality.plugin import QUALITY_SOURCE, QualityPlugin
from assurance_quality.resource_loader import resource_bytes
from tests.capabilities.conformance import PluginExpectation, assert_plugin_conforms


def test_quality_descriptor_declares_exact_dependency_versions() -> None:
    assert QualityPlugin.descriptor().dependencies == (
        PluginDependency("assurance.intake", "==0.3.0"),
        PluginDependency("assurance.generation", "==0.3.0"),
        PluginDependency("assurance.execution", "==0.3.0"),
        PluginDependency("assurance.healing", "==0.3.0"),
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
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    )
    descriptor = QualityPlugin.descriptor()
    assert descriptor.engine_api == ENGINE_API_VERSION
    assert ENGINE_API_VERSION == "2.0"
    assert descriptor.schemas == tuple(sorted(descriptor.schemas))
    assert len(descriptor.schemas) == 33
    assert descriptor.task_handlers == tuple(sorted(descriptor.task_handlers))
    assert descriptor.commit_validators == tuple(sorted(descriptor.commit_validators))
    assert "assurance.quality.inspect" in descriptor.task_handlers
    assert "assurance.quality.generate-report" in descriptor.task_handlers
    assert "assurance.quality.dashboard" in descriptor.task_handlers
    assert "assurance.quality.validator.report.v1" in descriptor.commit_validators
    assert descriptor.resources
    assert descriptor.effects == ()
    assert descriptor.bindings == ()


def test_quality_handlers_own_execute() -> None:
    for handler_id, handler in quality_handlers().items():
        cls = type(handler)
        descriptor = inspect.getattr_static(cls, "execute")
        declaring = next(
            (candidate for candidate in cls.__mro__ if candidate.__dict__.get("execute") is descriptor),
            None,
        )
        assert declaring is cls, handler_id
        assert isinstance(descriptor, FunctionType), handler_id
        assert descriptor.__qualname__.startswith(f"{cls.__name__}."), handler_id


def test_quality_rejects_unsupported_engine_api() -> None:
    with pytest.raises(ValueError, match="unsupported engine API"):
        QualityPlugin.contribute(RegistryPorts(engine_api="1.0"))


def test_resource_bytes_rejects_non_canonical_path() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("../secret")
    with pytest.raises(ValueError, match="canonical and relative"):
        resource_bytes("/schemas/trace.v2.schema.json")


def test_quality_plugin_does_not_discover_graph_factory() -> None:
    descriptor = QualityPlugin.descriptor()
    contribution = QualityPlugin.contribute(RegistryPorts(engine_api=ENGINE_API_VERSION))
    assert descriptor.source is not None
    assert descriptor.source.entrypoint_value == "assurance_quality.plugin:QualityPlugin"
    assert "graphs.factory" not in descriptor.source.entrypoint_value
    assert "assurance.quality.workflow.module.v1" not in descriptor.resources
    assert not hasattr(contribution, "graph_factories")
    assert contribution.attempt_contracts
    assert all("graphs.factory" not in ref.contract_id for ref in contribution.attempt_contracts)
