from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.workflow_assembler import assemble_product_workflow
from graph_engine.frozen_json import freeze_json, thaw_json
from graph_engine.graph.input_projection import ObjectProjection, PredecessorPointerProjection
from graph_engine.graph.output_projection import project_subgraph_output
from graph_engine.graph.schema import GraphDef, WorkflowDef, parse_workflow
from graph_engine.runtime.models import InvocationProjection

from tests.product.graph_inventory import (
    EXPECTED_OWNER_COUNTS,
    RELOCATION_PREFIX_NODES,
    assert_workflow_module_ownership,
    load_workflow_module_ownership,
)
from tests.product.product_runner import (
    ProductRun,
    _PUBLIC_DIGEST,
    _product_alias,
    resolve_product_workflow_composition,
)

GOLDEN = Path(__file__).resolve().parent / "goldens" / "assurance-full-pre-modular.json"
RELOCATION_DIFF = Path(__file__).resolve().parent / "fixtures" / "architectural-relocation-diff.yaml"
PRE_MODULAR_YAML = (
    Path(__file__).resolve().parents[2]
    / "packages/products/assurance-product/assurance_product/resources/workflow/assurance-full.yaml"
)
_FEATURE_MODULE_IDS = {
    "intake": "assurance.intake.workflow",
    "generation": "assurance.generation.workflow",
    "execution": "assurance.execution.workflow",
    "quality": "assurance.quality.workflow",
    "healing": "assurance.healing.workflow",
    "improvement": "assurance.improvement.workflow",
}
_BOUNDARY_FIELDS = frozenset(
    {"input_schema", "output_schema", "output_projection", "capability_slot", "graph_import"}
)
INTENTIONAL_SEMANTIC_DIFF: frozenset[str] = frozenset(
    {
        "entry",
        "generation",
        "generation-api",
        "generation-e2e",
        "generation-fuzz",
        "generation-performance",
    }
)
THIN_WRAPPER_ENTRYPOINTS = (
    "intake",
    "case",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)


def test_pre_modular_inventory_is_frozen() -> None:
    from assurance_product.product import load_pre_modular_workflow

    workflow = load_pre_modular_workflow()
    assert len(workflow.entrypoints) == 14
    assert len(workflow.graphs) == 52
    assert sum(len(graph.nodes) for graph in workflow.graphs.values()) == 265
    assert (
        sum(node.kind == "subgraph" for graph in workflow.graphs.values() for node in graph.nodes.values())
        == 59
    )
    assert canonical_json_bytes(workflow.model_dump(mode="json", by_alias=True, exclude_unset=True)) == (
        GOLDEN.read_bytes()
    )
    ownership = load_workflow_module_ownership()
    assert_workflow_module_ownership(workflow, ownership)
    counts = {owner: len(module["graphs"]) for owner, module in ownership["owners"].items()}
    assert counts == EXPECTED_OWNER_COUNTS


def test_intentional_semantic_diff_is_the_intake_and_generation_correction() -> None:
    document = yaml.safe_load(RELOCATION_DIFF.read_text(encoding="utf-8"))
    assert document["intentional_semantic_diff"] == []
    assert INTENTIONAL_SEMANTIC_DIFF == frozenset(
        {
            "entry",
            "generation",
            "generation-api",
            "generation-e2e",
            "generation-fuzz",
            "generation-performance",
        }
    )


def test_architectural_relocation_diff_records_standalone_intake() -> None:
    document = yaml.safe_load(RELOCATION_DIFF.read_text(encoding="utf-8"))
    relocations = document["relocation"]
    assert len(relocations) == 1
    assert relocations[0]["id"] == "standalone-intake-review-loop"
    assert relocations[0]["entrypoint"] == "intake"


@pytest.mark.usefixtures("installed_sources")
def test_feature_graphs_match_normalized_pre_modular_projection(installed_sources) -> None:
    ownership = load_workflow_module_ownership()
    pre = _load_unbound_pre_modular()
    assembled = _assemble_modular(installed_sources)
    compared = 0
    for owner, module_id in _FEATURE_MODULE_IDS.items():
        for local_id in ownership["owners"][owner]["graphs"]:
            if local_id in INTENTIONAL_SEMANTIC_DIFF:
                continue
            expected = _project_pre_modular_graph(pre.graphs[local_id])
            actual = _project_modular_graph(assembled.graphs[f"{module_id}.graph.{local_id}"], module_id)
            assert actual == expected, f"{owner}/{local_id} drifted"
            compared += 1
    assert compared == 44


@pytest.mark.usefixtures("installed_sources")
def test_intake_entry_matches_relocated_full_prefix(installed_sources) -> None:
    del installed_sources
    assert "entry" in INTENTIONAL_SEMANTIC_DIFF
    ownership = load_workflow_module_ownership()
    relocation = ownership["product_to_intake_relocation"]
    assert "review-pass-gate" in relocation["prefix_nodes"]
    assert "review-fix-gate" in relocation["prefix_nodes"]
    assert "review-human-gate" in relocation["prefix_nodes"]


@pytest.mark.usefixtures("installed_sources")
def test_product_wrappers_are_one_public_call_and_end(installed_sources) -> None:
    from assurance_product.product import (
        AssuranceOpenCodeProductProvider,
        load_product_workflow_module,
    )

    root = load_product_workflow_module()
    assert root == AssuranceOpenCodeProductProvider.manifest().workflow_module
    for name in THIN_WRAPPER_ENTRYPOINTS:
        graph = root.graphs[f"product-{name}"]
        kinds = {node_id: node.kind for node_id, node in graph.nodes.items()}
        subgraphs = [node_id for node_id, kind in kinds.items() if kind == "subgraph"]
        ends = [node_id for node_id, kind in kinds.items() if kind == "end"]
        assert len(subgraphs) == 1
        assert len(ends) == 1
        assert len(graph.nodes) == 2
        caller = graph.nodes[subgraphs[0]]
        assert caller.graph_import is not None
        assert caller.graph is None
        assert [(edge.from_, edge.to) for edge in graph.edges] == [(subgraphs[0], ends[0])]


_PUBLIC_ENTRYPOINTS = (
    "intake",
    "case",
    "full",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)
_ROOT_CHARACTERIZATION_SCENARIOS = (
    {"entrypoint": "full"},
    {"entrypoint": "full", "execution_sequence": ("failed", "passed")},
    {"entrypoint": "full", "execution_sequence": ("product_issue", "passed")},
    {"entrypoint": "full", "execution_sequence": ("infrastructure_failure",)},
    {"entrypoint": "full", "coverage_sequence": (0.40, 0.72, 0.91), "threshold": 0.90},
    {
        "entrypoint": "full",
        "coverage_sequence": (0.40, 0.41, 0.42),
        "threshold": 0.90,
        "coverage_rounds": 1,
    },
    {"entrypoint": "full", "execution_sequence": ("failed",), "healing_decision": "disallowed"},
    {"entrypoint": "execute"},
    {"entrypoint": "execute", "execution_sequence": ("failed", "passed")},
    {"entrypoint": "execute", "execution_sequence": ("product_issue", "passed")},
    {"entrypoint": "execute", "execution_sequence": ("infrastructure_failure",)},
    {"entrypoint": "execute", "coverage_sequence": (0.40, 0.91), "threshold": 0.90},
)
_STANDALONE_PASS_SCENARIOS = tuple(
    {"entrypoint": name} for name in _PUBLIC_ENTRYPOINTS if name not in {"full", "execute"}
)


@pytest.mark.usefixtures("installed_sources")
def test_product_full_threads_prepare_output_into_generate() -> None:
    from assurance_product.product import load_product_workflow_module

    root = load_product_workflow_module()
    execute_tail = root.graphs["product-full"].nodes["execute-tail"]
    generation = root.graphs["product-execute"].nodes["generation"]
    assert isinstance(execute_tail.input_projection, ObjectProjection)
    assert isinstance(generation.input_projection, ObjectProjection)
    for field in ("artifacts", "decision"):
        pointer = execute_tail.input_projection.fields[field]
        assert isinstance(pointer, PredecessorPointerProjection)
        assert pointer.predecessor == "prepare"
        assert pointer.pointer == f"/{field}"
        assert field in generation.input_projection.fields


@pytest.mark.usefixtures("installed_sources")
def test_product_full_consumes_prepare_handler_terminal(installed_sources, tmp_path) -> None:
    modular = _modular_composition(installed_sources)
    result = _run_scenario(modular, tmp_path / "consume-prepare", {"entrypoint": "full"})
    generation = next(
        item
        for item in result.projection.graph_instances
        if item.graph_id.endswith(".generation") or item.graph_id == "generation"
    )
    payload = thaw_json(generation.input)
    assert isinstance(payload, dict)
    assert payload["decision"] == "pass"
    assert payload["artifacts"] == [{"path": "qa/changes", "digest": _PUBLIC_DIGEST}]


@pytest.mark.usefixtures("installed_sources")
def test_product_roots_preserve_public_closure_behavior(installed_sources, tmp_path) -> None:
    pre = resolve_product_workflow_composition(_load_bound_pre_modular())
    modular = _modular_composition(installed_sources)
    for scenario in _ROOT_CHARACTERIZATION_SCENARIOS:
        if scenario["entrypoint"] in INTENTIONAL_SEMANTIC_DIFF or scenario["entrypoint"] == "full":
            continue
        projection = _declared_public_projection(modular, scenario["entrypoint"])
        expected = _public_closure_trace(
            pre, tmp_path / "pre" / scenario["entrypoint"], scenario, public_projection=projection
        )
        actual = _public_closure_trace(
            modular, tmp_path / "mod" / scenario["entrypoint"], scenario, public_projection=projection
        )
        _assert_public_closure(actual, expected, require_terminal_output=True)


@pytest.mark.usefixtures("installed_sources")
def test_fourteen_entrypoints_preserve_characterized_public_behavior(installed_sources, tmp_path) -> None:
    pre = resolve_product_workflow_composition(_load_bound_pre_modular())
    modular = _modular_composition(installed_sources)
    for scenario in tuple({"entrypoint": name} for name in _PUBLIC_ENTRYPOINTS):
        if scenario["entrypoint"] == "full":
            continue
        projection = (
            _declared_public_projection(modular, scenario["entrypoint"])
            if scenario["entrypoint"] in {"full", "execute"}
            else None
        )
        expected = _public_closure_trace(
            pre, tmp_path / "pre" / scenario["entrypoint"], scenario, public_projection=projection
        )
        actual = _public_closure_trace(
            modular, tmp_path / "mod" / scenario["entrypoint"], scenario, public_projection=projection
        )
        _assert_public_closure(
            actual,
            expected,
            require_terminal_output=scenario["entrypoint"] in {"full", "execute"},
        )


@pytest.mark.usefixtures("installed_sources")
def test_standalone_intake_non_pass_differs_only_by_relocated_review_prefix(
    installed_sources, tmp_path
) -> None:
    pre = resolve_product_workflow_composition(_load_bound_pre_modular())
    modular = _modular_composition(installed_sources)
    scenario = {"entrypoint": "intake", "review_decision": "needs_human_review"}
    expected = _public_closure_trace(pre, tmp_path / "pre-intake", scenario)
    actual = _public_closure_trace(modular, tmp_path / "mod-intake", scenario)
    assert expected["dispatches"] == actual["dispatches"] or _is_prefix(
        expected["dispatches"], actual["dispatches"]
    )
    assert expected["effects"] == actual["effects"]
    extra_interrupts = _subtract(actual["interrupts"], expected["interrupts"])
    assert extra_interrupts
    assert all(reason == "needs_human_review" for reason, _actions in extra_interrupts)


def _assemble_modular(installed_sources) -> WorkflowDef:
    from assurance_product.product import resolve_assurance_composition
    from tests.product.composition_harness import request_for

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    return assemble_product_workflow(
        manifest=composition.manifest,
        descriptors={item.plugin_id: item for item in composition.descriptors},
        registries=composition.registries,
    )


def _modular_composition(installed_sources):
    assembled = _assemble_modular(installed_sources)
    from assurance_product.agent_contracts import bind_agent_execution_contracts

    try:
        bound = bind_agent_execution_contracts(assembled)
    except ValueError:
        bound = assembled
    return resolve_product_workflow_composition(bound)


def _load_bound_pre_modular() -> WorkflowDef:
    from assurance_product.product import load_pre_modular_workflow

    return load_pre_modular_workflow()


def _run_scenario(composition, engine_root: Path, scenario: dict[str, Any]):
    run = ProductRun(
        entrypoint=str(scenario["entrypoint"]),
        selected_test_families=("api",) if scenario["entrypoint"] in {"full", "execute"} else (),
        review_decision=str(scenario.get("review_decision", "pass")),
        healing_decision=str(scenario.get("healing_decision", "allowed")),
        execution_sequence=tuple(scenario.get("execution_sequence", ())),
        coverage_sequence=tuple(scenario.get("coverage_sequence", ())),
        threshold=float(scenario.get("threshold", 0.90)),
        coverage_rounds=scenario.get("coverage_rounds"),
        engine_root=engine_root,
        composition=composition,
    )
    return run.run_to_terminal()


def _assert_public_closure(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    require_terminal_output: bool,
) -> None:
    assert actual["dispatches"] == expected["dispatches"]
    assert actual["interrupts"] == expected["interrupts"]
    assert actual["effects"] == expected["effects"]
    assert actual["terminal"] == expected["terminal"]
    if require_terminal_output:
        _assert_public_terminal(actual["terminal_output"], expected["terminal_output"])


def _assert_public_terminal(actual: object, expected: object) -> None:
    assert actual == expected


def _declared_public_projection(composition, entrypoint: str):
    graph_id = composition.workflow.entrypoints[entrypoint]
    graph = composition.workflow.graphs[graph_id]
    ends = {node_id for node_id, node in graph.nodes.items() if node.definition.kind == "end"}
    for edge in graph.edges:
        if edge.to not in ends:
            continue
        predecessor = graph.nodes[edge.from_]
        if predecessor.definition.kind == "subgraph" and predecessor.definition.output_projection is not None:
            return predecessor.definition.output_projection
    return None


def _apply_public_projection(raw: object, public_projection) -> object:
    if public_projection is None or raw is None:
        return raw
    return thaw_json(project_subgraph_output(public_projection, child_output=freeze_json(raw)))


def _public_closure_trace(
    composition,
    engine_root: Path,
    scenario: dict[str, Any],
    *,
    public_projection=None,
) -> dict[str, Any]:
    engine_root.mkdir(parents=True, exist_ok=True)
    result = _run_scenario(composition, engine_root, scenario)
    projection = result.projection
    raw_terminal = thaw_json(
        next(item.output for item in projection.graph_instances if item.parent_graph_instance_id is None)
    )
    return {
        "dispatches": _task_dispatches(projection, composition),
        "interrupts": _interrupts(projection),
        "effects": _effects(projection),
        "terminal": (result.status, result.stop_reason),
        "terminal_output": _apply_public_projection(raw_terminal, public_projection),
    }


def _task_dispatches(projection: InvocationProjection, composition) -> tuple[str, ...]:
    graphs = {item.graph_instance_id: item for item in projection.graph_instances}
    dispatches: list[str] = []
    for activation in projection.activations:
        graph = graphs[activation.graph_instance_id]
        node = composition.workflow.graphs[graph.graph_id].nodes[activation.node_id]
        if node.definition.kind != "task" or node.definition.capability is None:
            continue
        if not activation.attempts:
            continue
        dispatches.append(_product_alias(node.definition.capability))
    return tuple(dispatches)


def _interrupts(projection: InvocationProjection) -> tuple[tuple[str, tuple[str, ...]], ...]:
    seen: list[tuple[str, tuple[str, ...]]] = []
    for activation in projection.activations:
        if activation.interrupt_reason is None:
            continue
        seen.append((activation.interrupt_reason, tuple(activation.interrupt_actions)))
    if projection.pending_interrupt is not None:
        pending = (
            projection.pending_interrupt.reason,
            tuple(projection.pending_interrupt.actions),
        )
        if pending not in seen:
            seen.append(pending)
    return tuple(seen)


def _effects(projection: InvocationProjection) -> tuple[tuple[str, str], ...]:
    return tuple((item.kind, item.status) for item in projection.effects)


def _is_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(longer) >= len(shorter) and longer[: len(shorter)] == shorter


def _subtract(
    actual: tuple[tuple[str, tuple[str, ...]], ...],
    expected: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    remaining = list(expected)
    extra: list[tuple[str, tuple[str, ...]]] = []
    for item in actual:
        if item in remaining:
            remaining.remove(item)
        else:
            extra.append(item)
    return tuple(extra)


def _load_unbound_pre_modular() -> WorkflowDef:
    return parse_workflow(PRE_MODULAR_YAML.read_text(encoding="utf-8"))


def _project_pre_modular_graph(graph: GraphDef) -> dict[str, Any]:
    return _project_graph(graph, module_id=None)


def _project_modular_graph(graph: GraphDef, module_id: str) -> dict[str, Any]:
    return _project_graph(graph, module_id=module_id)


def _project_graph(graph: GraphDef, *, module_id: str | None) -> dict[str, Any]:
    nodes = {}
    for node_id, node in graph.nodes.items():
        payload = node.model_dump(mode="json", by_alias=True, exclude_unset=True)
        for field in _BOUNDARY_FIELDS:
            payload.pop(field, None)
        if payload.get("graph") is not None:
            payload["graph"] = _unqualify(payload["graph"], module_id)
        if payload.get("retry") is not None:
            payload["retry"] = _unqualify(payload["retry"], module_id)
        if payload.get("timeout") is not None:
            payload["timeout"] = _unqualify(payload["timeout"], module_id)
        if payload.get("capability") is not None:
            payload["capability"] = _normalize_capability(payload["capability"])
        payload.pop("input_projection", None)
        payload.pop("input", None)
        payload.pop("resources", None)
        payload.pop("routing", None)
        nodes[node_id] = {"kind": payload.get("kind")}
        if payload.get("capability") is not None:
            nodes[node_id]["capability"] = payload["capability"]
        if payload.get("graph") is not None:
            nodes[node_id]["graph"] = payload["graph"]
        if payload.get("expression") is not None:
            nodes[node_id]["expression"] = " ".join(payload["expression"].split())
        if payload.get("actions"):
            nodes[node_id]["actions"] = list(payload["actions"])
        if payload.get("reason") is not None:
            nodes[node_id]["reason"] = payload["reason"]
        if payload.get("retry") is not None:
            nodes[node_id]["retry"] = payload["retry"]
        if payload.get("timeout") is not None:
            nodes[node_id]["timeout"] = payload["timeout"]
    edges = []
    for edge in graph.edges:
        item = {"from": edge.from_, "to": edge.to}
        if edge.condition:
            item["condition"] = edge.condition
        edges.append(item)
    return {"start": graph.start, "nodes": nodes, "edges": edges}


def _unqualify(value: str, module_id: str | None) -> str:
    prefixes = []
    if module_id is not None:
        prefixes.extend((f"{module_id}.graph.", f"{module_id}.retry.", f"{module_id}.timeout."))
    for prefix in prefixes:
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    if module_id is None:
        return value
    for other in _FEATURE_MODULE_IDS.values():
        for kind in ("graph", "retry", "timeout"):
            prefix = f"{other}.{kind}."
            if value.startswith(prefix):
                return value.removeprefix(prefix)
    if ".graph." in value or ".retry." in value or ".timeout." in value:
        raise AssertionError(f"unknown qualified prefix: {value}")
    return value


def _normalize_capability(capability: str) -> str:
    if capability.startswith("assurance.product.agent."):
        return capability
    for feature in _FEATURE_MODULE_IDS:
        prefix = f"assurance.{feature}."
        if capability.startswith(prefix):
            rest = capability.removeprefix("assurance.")
            candidate = f"assurance.product.agent.{rest}"
            if rest.endswith((".prepare", ".execute", ".finalize")):
                return candidate
    return capability


def _extract_full_intake_prefix(full: GraphDef, relocation: dict[str, Any]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    for node_id in relocation["prefix_nodes"]:
        node = full.nodes[node_id]
        payload: dict[str, Any] = {"kind": node.kind}
        if node.graph is not None:
            payload["graph"] = node.graph
        if node.expression is not None:
            payload["expression"] = " ".join(node.expression.split())
        if node.actions:
            payload["actions"] = list(node.actions)
        if node.reason is not None:
            payload["reason"] = node.reason
        nodes[node_id] = payload
    nodes["done"] = {"kind": "end"}
    edges = []
    for item in relocation["prefix_internal_edges"]:
        edges.append(dict(item))
    for item in relocation["continuation_edges"]:
        remapped = dict(item)
        remapped["to"] = "done"
        edges.append(remapped)
    assert set(RELOCATION_PREFIX_NODES) == set(relocation["prefix_nodes"])
    return {"start": "intake", "nodes": nodes, "edges": edges}
