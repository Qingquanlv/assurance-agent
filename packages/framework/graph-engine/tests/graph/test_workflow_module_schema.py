from __future__ import annotations

from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from graph_engine.composition import CapabilityRegistry
from graph_engine.graph import parse_workflow, parse_workflow_module
from graph_engine.graph.compiler import CompileError, compile_workflow
from graph_engine.graph.schema import NodeDef

FEATURE_MODULE = """
schema_version: "1"
role: feature
owner_id: toy.feature
module_id: toy.feature.workflow
module_version: 1.0.0
entrypoints: {}
imports: {}
exports:
  run:
    graph: run
    input_schema: toy.feature.workflow.run.input.v1
    output_schema: toy.feature.workflow.run.output.v1
    output_projection:
      type: child_output_pointer
      pointer: ""
capability_slots:
  worker.execute:
    contract_id: toy.feature.agent.worker.v1
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
  run:
    max_activations: 2
    start: work
    nodes:
      work:
        kind: task
        capability_slot: worker.execute
        retry: once
        timeout: short
      done: {kind: end}
    edges:
      - {from: work, to: done}
"""

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


def _load(text: str) -> dict[str, object]:
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


def _dump(raw: dict[str, object]) -> str:
    return yaml.safe_dump(raw, sort_keys=False)


def test_feature_module_fixture_parses_verbatim() -> None:
    module = parse_workflow_module(FEATURE_MODULE)
    assert module.role == "feature"
    assert module.name is None
    assert module.owner_id == "toy.feature"
    assert module.module_id == "toy.feature.workflow"
    assert module.module_version == "1.0.0"
    assert module.entrypoints == {}
    assert module.imports == {}
    assert set(module.exports) == {"run"}
    assert module.exports["run"].graph == "run"
    assert module.capability_slots["worker.execute"].contract_id == "toy.feature.agent.worker.v1"
    work = module.graphs["run"].nodes["work"]
    assert work.capability is None
    assert work.capability_slot == "worker.execute"


def test_parse_workflow_module_accepts_bytes() -> None:
    module = parse_workflow_module(FEATURE_MODULE.encode("utf-8"))
    assert module.module_id == "toy.feature.workflow"


def test_product_root_module_parses_with_entrypoints_and_imports() -> None:
    module = parse_workflow_module(PRODUCT_MODULE)
    assert module.role == "product"
    assert module.name == "toy"
    assert module.entrypoints == {"main": "root"}
    imported = module.imports["run"]
    assert imported.owner_id == "toy.feature"
    assert imported.module_id == "toy.feature.workflow"
    assert imported.export == "run"
    child = module.graphs["root"].nodes["child"]
    assert child.graph is None
    assert child.graph_import == "run"


def test_owner_and_module_ids_must_be_qualified() -> None:
    for field, value in (("owner_id", "feature"), ("module_id", "workflow")):
        raw = _load(FEATURE_MODULE)
        raw[field] = value
        with pytest.raises(ValidationError, match="qualified"):
            parse_workflow_module(_dump(raw))


def test_module_version_is_normalized() -> None:
    raw = _load(FEATURE_MODULE)
    raw["module_version"] = "1.00.0"
    module = parse_workflow_module(_dump(raw))
    assert module.module_version == "1.0.0"

    raw["module_version"] = "not-a-version"
    with pytest.raises(ValidationError, match="version"):
        parse_workflow_module(_dump(raw))


def test_duplicate_yaml_keys_are_rejected() -> None:
    duplicated = FEATURE_MODULE.replace("role: feature\n", "role: feature\nrole: feature\n", 1)
    with pytest.raises(ValueError, match="duplicate"):
        parse_workflow_module(duplicated)


def test_graphs_are_private_by_default() -> None:
    raw = _load(FEATURE_MODULE)
    graphs = cast(dict[str, object], raw["graphs"])
    graphs["helper"] = {
        "max_activations": 1,
        "start": "done",
        "nodes": {"done": {"kind": "end"}},
        "edges": [],
    }
    module = parse_workflow_module(_dump(raw))
    assert "helper" in module.graphs
    assert {item.graph for item in module.exports.values()} == {"run"}


def test_export_target_must_exist() -> None:
    raw = _load(FEATURE_MODULE)
    exports = cast(dict[str, object], raw["exports"])
    run = cast(dict[str, object], exports["run"])
    run["graph"] = "missing"
    with pytest.raises(ValidationError, match="export"):
        parse_workflow_module(_dump(raw))


def test_graph_import_must_use_a_declared_alias() -> None:
    raw = _load(PRODUCT_MODULE)
    nodes = cast(dict[str, object], cast(dict[str, object], raw["graphs"])["root"])["nodes"]
    child = cast(dict[str, object], cast(dict[str, object], nodes)["child"])
    child["graph_import"] = "undeclared"
    with pytest.raises(ValidationError, match="import"):
        parse_workflow_module(_dump(raw))


def test_capability_slot_must_be_declared() -> None:
    raw = _load(FEATURE_MODULE)
    nodes = cast(dict[str, object], cast(dict[str, object], raw["graphs"])["run"])["nodes"]
    work = cast(dict[str, object], cast(dict[str, object], nodes)["work"])
    work["capability_slot"] = "missing.execute"
    with pytest.raises(ValidationError, match="slot"):
        parse_workflow_module(_dump(raw))


def test_product_requires_entrypoints_and_non_empty_name() -> None:
    raw = _load(PRODUCT_MODULE)
    raw["entrypoints"] = {}
    with pytest.raises(ValidationError, match="entrypoint"):
        parse_workflow_module(_dump(raw))

    raw = _load(PRODUCT_MODULE)
    raw["name"] = ""
    with pytest.raises(ValidationError, match="name"):
        parse_workflow_module(_dump(raw))

    raw = _load(PRODUCT_MODULE)
    del raw["name"]
    with pytest.raises(ValidationError, match="name"):
        parse_workflow_module(_dump(raw))


def test_feature_forbids_name_and_entrypoints() -> None:
    raw = _load(FEATURE_MODULE)
    raw["name"] = "toy"
    with pytest.raises(ValidationError, match="name"):
        parse_workflow_module(_dump(raw))

    raw = _load(FEATURE_MODULE)
    raw["entrypoints"] = {"main": "run"}
    with pytest.raises(ValidationError, match="entrypoint"):
        parse_workflow_module(_dump(raw))


def test_feature_requires_at_least_one_export() -> None:
    raw = _load(FEATURE_MODULE)
    raw["exports"] = {}
    with pytest.raises(ValidationError, match="export"):
        parse_workflow_module(_dump(raw))


def test_feature_may_declare_imports_for_assembler() -> None:
    raw = _load(FEATURE_MODULE)
    raw["imports"] = {
        "peer": {
            "owner_id": "toy.other",
            "module_id": "toy.other.workflow",
            "export": "run",
        }
    }
    module = parse_workflow_module(_dump(raw))
    assert module.imports["peer"].module_id == "toy.other.workflow"


def test_extra_fields_are_forbidden() -> None:
    raw = _load(FEATURE_MODULE)
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        parse_workflow_module(_dump(raw))

    export_payload = _load(FEATURE_MODULE)
    exports = cast(dict[str, object], export_payload["exports"])
    cast(dict[str, object], exports["run"])["export_extra"] = True
    with pytest.raises(ValidationError, match="export_extra"):
        parse_workflow_module(_dump(export_payload))

    slot_payload = _load(FEATURE_MODULE)
    slots = cast(dict[str, object], slot_payload["capability_slots"])
    cast(dict[str, object], slots["worker.execute"])["slot_extra"] = True
    with pytest.raises(ValidationError, match="slot_extra"):
        parse_workflow_module(_dump(slot_payload))

    import_payload = _load(PRODUCT_MODULE)
    imports = cast(dict[str, object], import_payload["imports"])
    cast(dict[str, object], imports["run"])["import_extra"] = True
    with pytest.raises(ValidationError, match="import_extra"):
        parse_workflow_module(_dump(import_payload))


def test_task_requires_exactly_one_of_capability_or_slot() -> None:
    NodeDef.model_validate(
        {"kind": "task", "capability_slot": "worker.execute", "retry": "once", "timeout": "short"}
    )
    NodeDef.model_validate(
        {"kind": "task", "capability": "toy.one.ping", "retry": "once", "timeout": "short"}
    )
    with pytest.raises(ValidationError, match="capability"):
        NodeDef.model_validate({"kind": "task", "retry": "once", "timeout": "short"})
    with pytest.raises(ValidationError, match="capability"):
        NodeDef.model_validate(
            {
                "kind": "task",
                "capability": "toy.one.ping",
                "capability_slot": "worker.execute",
                "retry": "once",
                "timeout": "short",
            }
        )


def test_subgraph_requires_exactly_one_of_graph_or_graph_import() -> None:
    NodeDef.model_validate({"kind": "subgraph", "graph_import": "run"})
    NodeDef.model_validate({"kind": "subgraph", "graph": "child"})
    with pytest.raises(ValidationError, match="graph"):
        NodeDef.model_validate({"kind": "subgraph"})
    with pytest.raises(ValidationError, match="graph"):
        NodeDef.model_validate({"kind": "subgraph", "graph": "child", "graph_import": "run"})


def _ordinary_workflow_raw() -> dict[str, object]:
    return {
        "name": "toy",
        "entrypoints": {"main": "root"},
        "retry": {"once": {"max_attempts": 1, "retry_on": []}},
        "timeout": {"short": {"run_seconds": 5}},
        "graphs": {
            "root": {
                "max_activations": 20,
                "start": "ping",
                "nodes": {
                    "ping": {
                        "kind": "task",
                        "capability": "toy.one.ping",
                        "retry": "once",
                        "timeout": "short",
                    },
                    "done": {"kind": "end"},
                },
                "edges": [{"from": "ping", "to": "done"}],
            }
        },
    }


def _workflow_nodes(raw: dict[str, object], graph_id: str) -> dict[str, object]:
    graphs = cast(dict[str, object], raw["graphs"])
    graph = cast(dict[str, object], graphs[graph_id])
    return cast(dict[str, object], graph["nodes"])


def test_compile_rejects_unresolved_capability_slot() -> None:
    raw = _ordinary_workflow_raw()
    ping = cast(dict[str, object], _workflow_nodes(raw, "root")["ping"])
    del ping["capability"]
    ping["capability_slot"] = "worker.execute"
    workflow = parse_workflow(_dump(raw))
    with pytest.raises(CompileError, match=r"unresolved capability_slot .* at root/ping"):
        compile_workflow(workflow, CapabilityRegistry.empty())


def test_compile_rejects_unresolved_graph_import() -> None:
    raw = _ordinary_workflow_raw()
    graphs = cast(dict[str, object], raw["graphs"])
    graphs["child"] = {
        "max_activations": 1,
        "start": "done",
        "nodes": {"done": {"kind": "end"}},
        "edges": [],
    }
    root = cast(dict[str, object], graphs["root"])
    nodes = cast(dict[str, object], root["nodes"])
    nodes["ping"] = {"kind": "subgraph", "graph_import": "run"}
    workflow = parse_workflow(_dump(raw))
    with pytest.raises(CompileError, match=r"unresolved graph_import .* at root/ping"):
        compile_workflow(workflow, CapabilityRegistry.empty())
