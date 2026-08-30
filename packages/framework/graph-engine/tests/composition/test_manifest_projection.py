from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.lock import _manifest_projection
from graph_engine.composition.models import (
    PluginRequirement,
    ProductManifest,
    WorkflowModuleRequirement,
    WorkflowSlotBinding,
)
from graph_engine.graph import parse_workflow_module
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource


PRODUCT_MODULE = """
schema_version: "1"
role: product
name: toy
owner_id: toy.product
module_id: toy.product.workflow
module_version: 1.0.0
entrypoints: {main: root}
imports:
  run:
    owner_id: toy.feature
    module_id: toy.feature.workflow
    export: run
exports: {}
capability_slots: {}
schemas:
  - toy.feature.workflow.run.input.v1
  - toy.feature.workflow.run.output.v1
resources: []
effects: []
retry:
  once: {max_attempts: 1}
timeout:
  short: {run_seconds: 30}
graphs:
  root:
    max_activations: 2
    start: child
    nodes:
      child:
        kind: subgraph
        graph_import: run
        input_schema: toy.feature.workflow.run.input.v1
        output_schema: toy.feature.workflow.run.output.v1
        output_projection:
          type: child_output_pointer
          pointer: ""
      done: {kind: end}
    edges:
      - {from: child, to: done}
"""


def _workflow() -> WorkflowDef:
    return WorkflowDef.model_validate(
        {
            "name": "proj",
            "entrypoints": {"run": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 1}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "run",
                    "nodes": {
                        "run": {
                            "kind": "task",
                            "capability": "test.run",
                            "input": {"ok": True},
                            "input_projection": {"type": "config_pointer", "pointer": ""},
                            "retry": "once",
                            "timeout": "short",
                        },
                        "done": {"kind": "end"},
                    },
                    "edges": [{"from": "run", "to": "done"}],
                }
            },
        }
    )


def _legacy_source() -> ProviderSource:
    return ProviderSource(
        distribution="test-product",
        version="1.0.0",
        entrypoint_group="graph_engine.products",
        entrypoint_name="test",
        entrypoint_value="test_product:Provider",
        declaration_path="test_product/product-declaration.json",
        import_roots=("",),
    )


def _legacy_inline_manifest() -> ProductManifest:
    workflow = _workflow()
    return ProductManifest(
        schema_version="1",
        source=_legacy_source(),
        product_id="test.product",
        product_version="1.0.0",
        engine_api="2.0",
        plugins=(PluginRequirement(plugin_id="test.runtime", version_specifier="==1.0.0"),),
        entrypoints={"run": "root"},
        workflow=workflow,
    )


def _golden_legacy_manifest() -> ProductManifest:
    return ProductManifest(
        schema_version="1",
        source=ProviderSource(
            distribution="toy-a",
            version="1.0.0",
            entrypoint_group="graph_engine.products",
            entrypoint_name="toy.a",
            entrypoint_value="toy_a.product:provider",
            declaration_path="toy_a/product-declaration.json",
            import_roots=("",),
        ),
        product_id="toy.a",
        product_version="1.0.0",
        engine_api="2.0",
        plugins=(PluginRequirement(plugin_id="toy.runtime", version_specifier="==2.0.0"),),
        entrypoints={"你好": "根"},
        workflow_resource_id="toy.workflow",
    )


def _requirement() -> WorkflowModuleRequirement:
    return WorkflowModuleRequirement(
        module_id="toy.feature.workflow",
        owner_id="toy.feature",
        resource_id="toy.feature.workflow.module",
    )


def _binding() -> WorkflowSlotBinding:
    return WorkflowSlotBinding(
        module_id="toy.feature.workflow",
        slot="worker.execute",
        capability_id="toy.product.agent.worker.execute",
        contract_id="toy.feature.agent.worker.v1",
    )


def _modular_manifest(**overrides: object) -> ProductManifest:
    values: dict[str, object] = {
        "schema_version": "1",
        "source": None,
        "product_id": "toy.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": (PluginRequirement(plugin_id="toy.runtime", version_specifier="==1.0.0"),),
        "entrypoints": {"main": "root"},
        "workflow_module": parse_workflow_module(PRODUCT_MODULE),
        "workflow_module_resources": (_requirement(),),
        "workflow_slot_bindings": (_binding(),),
    }
    values.update(overrides)
    return ProductManifest.model_validate(values)


def test_manifest_projection_keeps_input_projection_discriminator() -> None:
    manifest = _legacy_inline_manifest()
    projection = _manifest_projection(manifest)
    assert projection["workflow"]["graphs"]["root"]["nodes"]["run"]["input_projection"] == {
        "type": "config_pointer",
        "pointer": "",
    }
    ProductManifest.model_validate(projection)


def test_product_manifest_xor_selects_exactly_one_workflow_form() -> None:
    manifest = _modular_manifest()
    assert (
        sum(
            value is not None
            for value in (manifest.workflow, manifest.workflow_resource_id, manifest.workflow_module)
        )
        == 1
    )
    inline = _legacy_inline_manifest()
    assert (
        sum(
            value is not None
            for value in (inline.workflow, inline.workflow_resource_id, inline.workflow_module)
        )
        == 1
    )
    resource = _golden_legacy_manifest()
    assert (
        sum(
            value is not None
            for value in (resource.workflow, resource.workflow_resource_id, resource.workflow_module)
        )
        == 1
    )


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"workflow": _workflow(), "workflow_resource_id": "toy.product.flow"},
        {"workflow": _workflow(), "workflow_module": parse_workflow_module(PRODUCT_MODULE)},
        {
            "workflow_resource_id": "toy.product.flow",
            "workflow_module": parse_workflow_module(PRODUCT_MODULE),
        },
        {
            "workflow": _workflow(),
            "workflow_resource_id": "toy.product.flow",
            "workflow_module": parse_workflow_module(PRODUCT_MODULE),
        },
    ),
)
def test_product_manifest_rejects_missing_or_combined_workflow_forms(payload: dict[str, object]) -> None:
    base = {
        "schema_version": "1",
        "source": None,
        "product_id": "toy.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": [{"plugin_id": "toy.runtime", "version_specifier": "==1.0.0"}],
        "entrypoints": {"main": "root"},
    }
    with pytest.raises(ValidationError, match="exactly one workflow form"):
        ProductManifest.model_validate({**base, **payload})


@pytest.mark.parametrize(
    "overrides",
    (
        {"workflow_module_resources": (_requirement(),)},
        {"workflow_slot_bindings": (_binding(),)},
    ),
)
def test_legacy_manifest_rejects_modular_auxiliary_fields(overrides: dict[str, object]) -> None:
    workflow = _workflow()
    values = {
        "schema_version": "1",
        "source": None,
        "product_id": "test.product",
        "product_version": "1.0.0",
        "engine_api": "2.0",
        "plugins": (PluginRequirement(plugin_id="test.runtime", version_specifier="==1.0.0"),),
        "entrypoints": {"run": "root"},
        "workflow": workflow,
        **overrides,
    }
    with pytest.raises(ValidationError, match="modular"):
        ProductManifest.model_validate(values)


def test_modular_requirements_and_slot_bindings_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="module id"):
        _modular_manifest(
            workflow_module_resources=(
                _requirement(),
                WorkflowModuleRequirement(
                    module_id="toy.feature.workflow",
                    owner_id="toy.feature",
                    resource_id="toy.feature.workflow.other",
                ),
            )
        )
    with pytest.raises(ValidationError, match="resource id"):
        _modular_manifest(
            workflow_module_resources=(
                _requirement(),
                WorkflowModuleRequirement(
                    module_id="toy.other.workflow",
                    owner_id="toy.other",
                    resource_id="toy.feature.workflow.module",
                ),
            )
        )
    with pytest.raises(ValidationError, match="slot"):
        _modular_manifest(
            workflow_slot_bindings=(
                _binding(),
                WorkflowSlotBinding(
                    module_id="toy.feature.workflow",
                    slot="worker.execute",
                    capability_id="toy.product.agent.worker.other",
                    contract_id="toy.feature.agent.worker.v1",
                ),
            )
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    (
        ({"product_id": "toy.other"}, "owner"),
        ({"product_version": "2.0.0"}, "version"),
        ({"entrypoints": {"other": "root"}}, "entrypoint"),
    ),
)
def test_modular_root_must_match_product_identity(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _modular_manifest(**overrides)


def test_modular_root_must_use_product_role() -> None:
    feature_module = """
schema_version: "1"
role: feature
owner_id: toy.product
module_id: toy.product.workflow
module_version: 1.0.0
exports:
  run:
    graph: run
    input_schema: toy.feature.workflow.run.input.v1
    output_schema: toy.feature.workflow.run.output.v1
    output_projection:
      type: child_output_pointer
      pointer: ""
capability_slots: {}
retry:
  once: {max_attempts: 1}
timeout:
  short: {run_seconds: 30}
graphs:
  run:
    max_activations: 2
    start: done
    nodes:
      done: {kind: end}
    edges: []
"""
    with pytest.raises(ValidationError, match="role"):
        _modular_manifest(workflow_module=parse_workflow_module(feature_module))


def test_legacy_manifest_projection_matches_lock_golden_bytes() -> None:
    projection = _manifest_projection(_golden_legacy_manifest())
    golden = json.loads(
        Path(__file__).with_name("invocation-lock-v2.golden.json").read_text(encoding="utf-8")
    )["product"]["manifest"]
    assert "workflow_module" not in projection
    assert "workflow_module_resources" not in projection
    assert "workflow_slot_bindings" not in projection
    assert canonical_json_bytes(projection) == canonical_json_bytes(golden)


def test_modular_manifest_projection_authenticates_modular_values() -> None:
    manifest = _modular_manifest()
    projection = _manifest_projection(manifest)
    assert projection["workflow_module"]["owner_id"] == "toy.product"
    assert projection["workflow_module"]["imports"]["run"] == {
        "owner_id": "toy.feature",
        "module_id": "toy.feature.workflow",
        "export": "run",
    }
    assert projection["workflow_module_resources"] == [
        {
            "module_id": "toy.feature.workflow",
            "owner_id": "toy.feature",
            "resource_id": "toy.feature.workflow.module",
        }
    ]
    assert projection["workflow_slot_bindings"] == [
        {
            "module_id": "toy.feature.workflow",
            "slot": "worker.execute",
            "capability_id": "toy.product.agent.worker.execute",
            "contract_id": "toy.feature.agent.worker.v1",
        }
    ]
    ProductManifest.model_validate(projection)
