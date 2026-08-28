from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from graph_engine.canonical import canonical_json_bytes
from graph_engine.composition.workflow_assembler import assemble_product_workflow
from graph_engine.graph.schema import GraphDef, WorkflowDef, parse_workflow

from tests.product.graph_inventory import (
    EXPECTED_OWNER_COUNTS,
    RELOCATION_PREFIX_NODES,
    assert_workflow_module_ownership,
    load_workflow_module_ownership,
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
_PRODUCT_MODULE_ID = "assurance.product.workflow"
_BOUNDARY_FIELDS = frozenset(
    {"input_schema", "output_schema", "output_projection", "capability_slot", "graph_import"}
)
_PUBLIC_CALL_MAP = {
    "generation": "generation.generate",
    "execution-execute": "execution.execute",
    "execution-run": "execution.rerun",
    "quality": "quality.assess",
    "quality-issue-triage": "quality.issue-review",
    "issue-review": "quality.issue-review",
    "quality-issue-analysis": "quality.issue-analyze",
    "issue-analyze": "quality.issue-analyze",
    "healing-fix-proposal": "healing.repair-failure",
    "healing-coverage-repair": "healing.repair-coverage",
    "quality-report": "quality.report",
    "retro": "improvement.retro",
    "improvement-apply": "improvement.apply",
}
INTENTIONAL_SEMANTIC_DIFF: frozenset[str] = frozenset()
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


def test_intentional_semantic_diff_is_empty() -> None:
    document = yaml.safe_load(RELOCATION_DIFF.read_text(encoding="utf-8"))
    assert document["intentional_semantic_diff"] == []
    assert INTENTIONAL_SEMANTIC_DIFF == frozenset()


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
            if owner == "intake" and local_id == "entry":
                continue
            expected = _project_pre_modular_graph(pre.graphs[local_id])
            actual = _project_modular_graph(assembled.graphs[f"{module_id}.graph.{local_id}"], module_id)
            assert actual == expected, f"{owner}/{local_id} drifted"
            compared += 1
    assert compared == 49


@pytest.mark.usefixtures("installed_sources")
def test_intake_entry_matches_relocated_full_prefix(installed_sources) -> None:
    ownership = load_workflow_module_ownership()
    relocation = ownership["product_to_intake_relocation"]
    pre = _load_unbound_pre_modular()
    assembled = _assemble_modular(installed_sources)
    prefix = _extract_full_intake_prefix(pre.graphs["full"], relocation)
    modular = assembled.graphs["assurance.intake.workflow.graph.entry"]
    actual = _project_modular_graph(modular, "assurance.intake.workflow")
    assert actual["nodes"] == prefix["nodes"]
    assert actual["edges"] == prefix["edges"]
    assert actual["start"] == prefix["start"]


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


@pytest.mark.usefixtures("installed_sources")
def test_product_roots_preserve_public_closure_behavior(installed_sources) -> None:
    pre = _load_unbound_pre_modular()
    assembled = _assemble_modular(installed_sources)
    scenarios = (
        {"execute_status": "passed", "coverage_state": "satisfied"},
        {"execute_status": "failed", "coverage_state": "satisfied"},
        {"execute_status": "passed", "coverage_state": "repair_required"},
    )
    for scenario in scenarios:
        assert _walk_public_closure(
            pre.graphs["full"], scenario, legacy=True, workflow=pre
        ) == _walk_public_closure(
            assembled.graphs[f"{_PRODUCT_MODULE_ID}.graph.product-full"],
            scenario,
            legacy=False,
            workflow=assembled,
        )
        assert _walk_public_closure(
            pre.graphs["execute"], scenario, legacy=True, workflow=pre
        ) == _walk_public_closure(
            assembled.graphs[f"{_PRODUCT_MODULE_ID}.graph.product-execute"],
            scenario,
            legacy=False,
            workflow=assembled,
        )


def _assemble_modular(installed_sources) -> WorkflowDef:
    from assurance_product.product import resolve_assurance_composition
    from tests.product.composition_harness import request_for

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    return assemble_product_workflow(
        manifest=composition.manifest,
        descriptors={item.plugin_id: item for item in composition.descriptors},
        registries=composition.registries,
    )


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


_QUALIFIED_PUBLIC_ALIASES = {
    "assurance.intake.workflow.graph.entry": "intake.prepare",
    "assurance.intake.workflow.graph.case": "intake.case",
    "assurance.generation.workflow.graph.generation": "generation.generate",
    "assurance.execution.workflow.graph.execution-execute": "execution.execute",
    "assurance.execution.workflow.graph.execution-run": "execution.rerun",
    "assurance.quality.workflow.graph.quality": "quality.assess",
    "assurance.quality.workflow.graph.issue-review": "quality.issue-review",
    "assurance.quality.workflow.graph.quality-issue-triage": "quality.issue-review",
    "assurance.quality.workflow.graph.issue-analyze": "quality.issue-analyze",
    "assurance.quality.workflow.graph.quality-issue-analysis": "quality.issue-analyze",
    "assurance.quality.workflow.graph.quality-report": "quality.report",
    "assurance.healing.workflow.graph.healing-fix-proposal": "healing.repair-failure",
    "assurance.healing.workflow.graph.healing-coverage-repair": "healing.repair-coverage",
    "assurance.improvement.workflow.graph.retro": "improvement.retro",
    "assurance.improvement.workflow.graph.improvement-apply": "improvement.apply",
}
_INTAKE_PREFIX_NODES = frozenset(
    {
        "intake",
        "explore",
        "case-design",
        "case-review",
        "review-pass-gate",
        "review-fix-gate",
        "review-human-gate",
        "human-review",
        "prepare",
    }
)
_PRODUCT_LOCAL_MARKERS = ("product-execute", "execute-tail")


def _public_alias(node, current: str) -> str | None:
    if node.graph_import is not None:
        return node.graph_import
    target = node.graph or current
    if target in _QUALIFIED_PUBLIC_ALIASES:
        return _QUALIFIED_PUBLIC_ALIASES[target]
    if target in _PUBLIC_CALL_MAP:
        return _PUBLIC_CALL_MAP[target]
    if current in _PUBLIC_CALL_MAP:
        return _PUBLIC_CALL_MAP[current]
    if current in _INTAKE_PREFIX_NODES or target in {"intake", "entry"}:
        return "intake.prepare"
    return target


def _is_product_local(target: str | None, current: str) -> bool:
    name = target or current
    return any(marker in name for marker in _PRODUCT_LOCAL_MARKERS)


def _walk_public_closure(
    graph: GraphDef,
    scenario: dict[str, str],
    *,
    legacy: bool,
    workflow: WorkflowDef | None = None,
) -> tuple[str, ...]:
    calls, _finished = _walk_public_closure_state(graph, scenario, legacy=legacy, workflow=workflow)
    return calls


def _walk_public_closure_state(
    graph: GraphDef,
    scenario: dict[str, str],
    *,
    legacy: bool,
    workflow: WorkflowDef | None = None,
) -> tuple[tuple[str, ...], bool]:
    calls: list[str] = []
    current = graph.start
    current_graph = graph
    seen: set[tuple[str, str, str, str]] = set()
    tokens = {
        "execute": {"status": scenario["execute_status"]},
        "quality": {"coverage_state": scenario["coverage_state"], "coverage": {"measured": 0.4}},
    }
    while current not in {"done", "achieved"}:
        key = (current_graph.start, current, tokens["execute"]["status"], tokens["quality"]["coverage_state"])
        if key in seen:
            break
        seen.add(key)
        node = current_graph.nodes[current]
        if legacy and current in _INTAKE_PREFIX_NODES and "generation" in current_graph.nodes:
            if not calls or calls[-1] != "intake.prepare":
                calls.append("intake.prepare")
            current = "generation"
            continue
        if node.kind == "subgraph":
            target = node.graph
            if _is_product_local(target, current) and workflow is not None and target in workflow.graphs:
                nested, finished = _walk_public_closure_state(
                    workflow.graphs[target],
                    scenario,
                    legacy=False,
                    workflow=workflow,
                )
                for alias in nested:
                    if not calls or calls[-1] != alias:
                        calls.append(alias)
                if not finished:
                    break
                outgoing = [edge for edge in current_graph.edges if edge.from_ == current]
                current = outgoing[0].to if outgoing else "done"
                continue
            alias = _public_alias(node, current)
            if alias and not _is_product_local(alias, current):
                if not calls or calls[-1] != alias:
                    calls.append(alias)
            current = _next_from_subgraph(current_graph, current, tokens)
            continue
        if node.kind == "gate":
            current = _next_from_gate(current_graph, current, tokens) or "done"
            continue
        if node.kind == "end":
            break
        outgoing = [edge for edge in current_graph.edges if edge.from_ == current]
        current = outgoing[0].to if outgoing else "done"
    return tuple(calls), current in {"done", "achieved"}


def _next_from_subgraph(graph: GraphDef, node_id: str, tokens: dict[str, dict[str, Any]]) -> str:
    outgoing = [edge for edge in graph.edges if edge.from_ == node_id]
    if len(outgoing) == 1:
        return outgoing[0].to
    chosen: list[str] = []
    for edge in outgoing:
        target = graph.nodes.get(edge.to)
        if target is not None and target.kind == "gate" and _gate_value(target.expression or "", tokens):
            nxt = _next_from_gate(graph, edge.to, tokens)
            if nxt:
                chosen.append(nxt)
    return chosen[0] if chosen else (outgoing[0].to if outgoing else "done")


def _next_from_gate(graph: GraphDef, node_id: str, tokens: dict[str, dict[str, Any]]) -> str | None:
    value = _gate_value(graph.nodes[node_id].expression or "", tokens)
    for edge in graph.edges:
        if edge.from_ != node_id:
            continue
        if edge.condition == "output.value == true" and value:
            return edge.to
        if edge.condition == "output.value == false" and not value:
            return edge.to
    return None


def _gate_value(expression: str, tokens: dict[str, dict[str, Any]]) -> bool:
    compact = " ".join(expression.split())
    status = tokens["execute"]["status"]
    coverage_state = tokens["quality"]["coverage_state"]
    if "status == 'passed'" in compact:
        return status == "passed"
    if "product_issue" in compact:
        return status in {"failed", "product_issue"}
    if "infrastructure_failure" in compact:
        return status == "infrastructure_failure"
    if "coverage_state == 'repair_required'" in compact:
        return coverage_state == "repair_required"
    if "measured < threshold" in compact:
        return coverage_state == "repair_required"
    return False
