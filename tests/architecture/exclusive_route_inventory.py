from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from graph_engine.graph.compiler import CompiledGraph, _compile_graph
from graph_engine.graph.schema import GraphDef

NodeKind = Literal["task", "subgraph", "interrupt", "gate"]
OwnerKind = Literal["feature", "product"]


@dataclass(frozen=True, slots=True)
class ExclusiveRouteRow:
    graph_id: str
    node_id: str
    node_kind: NodeKind
    otherwise_target: str
    owner: OwnerKind
    target_test: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.graph_id, self.node_id, self.node_kind, self.otherwise_target)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_yaml_paths() -> tuple[Path, ...]:
    root = _repo_root()
    return (
        *sorted(root.glob("packages/capabilities/*/assurance_*/resources/workflow/module.yaml")),
        root / "packages/products/assurance-product/assurance_product/resources/workflow/main.yaml",
    )


def _adapt_graph_for_compiler(graph: dict[str, object]) -> dict[str, object]:
    nodes: dict[str, object] = {}
    raw_nodes = graph["nodes"]
    assert isinstance(raw_nodes, dict)
    for node_id, node in raw_nodes.items():
        assert isinstance(node, dict)
        adapted = dict(node)
        if adapted.get("kind") == "task" and not adapted.get("capability"):
            adapted["capability"] = "dummy.capability"
            adapted.pop("capability_slot", None)
        if adapted.get("kind") == "subgraph" and not adapted.get("graph"):
            adapted["graph"] = "dummy.graph"
            adapted.pop("graph_import", None)
        nodes[str(node_id)] = adapted
    return {**graph, "nodes": nodes}


def iter_legacy_compiled_graphs() -> Iterator[CompiledGraph]:
    for path in _module_yaml_paths():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        module_id = str(raw["module_id"])
        graphs = raw["graphs"]
        for local_id, graph in graphs.items():
            qualified = f"{module_id}.graph.{local_id}"
            yield _compile_graph(qualified, GraphDef.model_validate(_adapt_graph_for_compiler(graph)))


def collect_legacy_exclusive_routes() -> frozenset[tuple[str, str, str, str]]:
    rows: set[tuple[str, str, str, str]] = set()
    for compiled in iter_legacy_compiled_graphs():
        for node_id, node in compiled.nodes.items():
            routing = node.definition.routing
            if routing is None or routing.mode != "exclusive":
                continue
            otherwise = [edge.to for edge in node.outgoing if edge.otherwise]
            if len(otherwise) != 1:
                raise AssertionError(f"{compiled.graph_id}/{node_id} exclusive otherwise={otherwise}")
            rows.add((compiled.graph_id, node_id, node.definition.kind, otherwise[0]))
    return frozenset(rows)


_FEATURE_ROUTE_TESTS = {
    "assurance.generation": "packages/capabilities/assurance-generation/tests/test_graph_routes.py",
    "assurance.intake": "packages/capabilities/assurance-intake/tests/test_graph_routes.py",
    "assurance.healing": "packages/capabilities/assurance-healing/tests/test_graph_routes.py",
    "assurance.improvement": "packages/capabilities/assurance-improvement/tests/test_graph_routes.py",
}


def _row(
    graph_id: str,
    node_id: str,
    node_kind: NodeKind,
    otherwise_target: str,
    owner: OwnerKind,
) -> ExclusiveRouteRow:
    if owner == "product":
        package = "tests/product/test_graph_routes.py"
    else:
        package = _FEATURE_ROUTE_TESTS[graph_id.split(".workflow.", 1)[0]]
    target_test = f"{package}::test_exclusive_route[{graph_id}/{node_id}]"
    return ExclusiveRouteRow(graph_id, node_id, node_kind, otherwise_target, owner, target_test)


EXCLUSIVE_ROUTE_INVENTORY: tuple[ExclusiveRouteRow, ...] = (
    _row("assurance.generation.workflow.graph.generation", "select-api", "gate", "api-skip", "feature"),
    _row("assurance.generation.workflow.graph.generation", "select-e2e", "gate", "e2e-skip", "feature"),
    _row("assurance.generation.workflow.graph.generation", "select-fuzz", "gate", "fuzz-skip", "feature"),
    _row(
        "assurance.generation.workflow.graph.generation",
        "select-performance",
        "gate",
        "performance-skip",
        "feature",
    ),
    _row("assurance.generation.workflow.graph.generation-api", "codegen", "subgraph", "exhausted", "feature"),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "plan-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row("assurance.generation.workflow.graph.generation-e2e", "codegen", "subgraph", "exhausted", "feature"),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.healing.workflow.graph.healing-coverage-repair", "admit", "gate", "not-eligible", "feature"
    ),
    _row("assurance.healing.workflow.graph.healing-coverage-repair", "finalize", "task", "failed", "feature"),
    _row("assurance.healing.workflow.graph.healing-fix-proposal", "admit", "gate", "not-eligible", "feature"),
    _row(
        "assurance.improvement.workflow.graph.improvement-apply",
        "apply-auto-review",
        "task",
        "failed",
        "feature",
    ),
    _row(
        "assurance.improvement.workflow.graph.improvement-apply",
        "apply-human-review",
        "task",
        "failed",
        "feature",
    ),
    _row("assurance.improvement.workflow.graph.improvement-apply", "evaluate", "task", "failed", "feature"),
    _row(
        "assurance.improvement.workflow.graph.improvement-apply",
        "human-review",
        "interrupt",
        "failed",
        "feature",
    ),
    _row("assurance.intake.workflow.graph.case-design", "finalize", "task", "repair-prepare", "feature"),
    _row("assurance.intake.workflow.graph.entry", "case-review", "subgraph", "exhausted", "feature"),
    _row("assurance.intake.workflow.graph.entry", "case-review-retry", "subgraph", "exhausted", "feature"),
    _row("assurance.intake.workflow.graph.entry", "human-review", "interrupt", "exhausted", "feature"),
    _row("assurance.intake.workflow.graph.entry", "human-review-retry", "interrupt", "exhausted", "feature"),
    _row(
        "assurance.product.workflow.graph.product-execute",
        "coverage-repair",
        "subgraph",
        "not-achieved",
        "product",
    ),
    _row(
        "assurance.product.workflow.graph.product-execute", "execute", "subgraph", "not-achieved", "product"
    ),
    _row(
        "assurance.product.workflow.graph.product-execute",
        "issue-analysis",
        "subgraph",
        "not-achieved",
        "product",
    ),
    _row(
        "assurance.product.workflow.graph.product-execute", "quality", "subgraph", "not-achieved", "product"
    ),
    _row(
        "assurance.product.workflow.graph.product-execute",
        "quality-recheck",
        "subgraph",
        "not-achieved",
        "product",
    ),
    _row("assurance.product.workflow.graph.product-execute", "run", "subgraph", "not-achieved", "product"),
    _row(
        "assurance.product.workflow.graph.product-full", "execute-tail", "subgraph", "not-achieved", "product"
    ),
    _row("assurance.product.workflow.graph.product-full", "prepare", "subgraph", "not-achieved", "product"),
)
