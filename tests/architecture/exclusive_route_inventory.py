from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

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
    candidates = (
        *sorted(root.glob("packages/capabilities/*/assurance_*/resources/workflow/module.yaml")),
        root / "packages/products/assurance-product/assurance_product/resources/workflow/main.yaml",
    )
    return tuple(path for path in candidates if path.is_file())


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


def iter_legacy_graphs() -> Iterator[tuple[str, dict[str, object]]]:
    for path in _module_yaml_paths():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        module_id = str(raw["module_id"])
        graphs = raw["graphs"]
        for local_id, graph in graphs.items():
            if isinstance(graph, dict):
                yield f"{module_id}.graph.{local_id}", _adapt_graph_for_compiler(graph)


def collect_legacy_exclusive_routes() -> frozenset[tuple[str, str, str, str]]:
    rows: set[tuple[str, str, str, str]] = set()
    if not _module_yaml_paths():
        return frozenset(row.key() for row in EXCLUSIVE_ROUTE_INVENTORY)
    for graph_id, graph in iter_legacy_graphs():
        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, dict) or not isinstance(edges, list):
            continue
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            routing = node.get("routing")
            if not isinstance(routing, Mapping) or routing.get("mode") != "exclusive":
                continue
            otherwise = [
                str(edge["to"])
                for edge in edges
                if isinstance(edge, dict) and edge.get("from") == node_id and edge.get("otherwise")
            ]
            if len(otherwise) != 1:
                raise AssertionError(f"{graph_id}/{node_id} exclusive otherwise={otherwise}")
            rows.add((graph_id, str(node_id), str(node.get("kind")), otherwise[0]))
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
        package = "tests/product/test_product_stategraph_flow.py"
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
        "codegen-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "codegen-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "codegen-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "codegen-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "codegen-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-api",
        "codegen-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row("assurance.generation.workflow.graph.generation-e2e", "codegen", "subgraph", "exhausted", "feature"),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-e2e",
        "codegen-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-fuzz",
        "codegen-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-human-review",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-human-review-retry",
        "interrupt",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-review",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-review-retry",
        "subgraph",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-review-round-advance",
        "task",
        "exhausted",
        "feature",
    ),
    _row(
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-review-round-advance-retry",
        "task",
        "exhausted",
        "feature",
    ),
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
    _row("assurance.product.workflow.graph.product-execute", "execute", "subgraph", "blocked", "product"),
    _row(
        "assurance.product.workflow.graph.product-execute",
        "fix-proposal",
        "subgraph",
        "blocked",
        "product",
    ),
    _row("assurance.product.workflow.graph.product-execute", "quality", "subgraph", "blocked", "product"),
    _row("assurance.product.workflow.graph.product-execute", "run", "subgraph", "blocked", "product"),
    _row("assurance.product.workflow.graph.product-full", "prepare", "subgraph", "failed", "product"),
)
