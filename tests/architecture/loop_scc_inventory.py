from __future__ import annotations

from dataclasses import dataclass

from tests.architecture.exclusive_route_inventory import iter_legacy_compiled_graphs

EXPECTED_LOOP_SCC_ANCHORS = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ("assurance.intake.workflow.graph.entry", "advance-join"),
    ("assurance.product.workflow.graph.product-execute", "coverage-needed"),
    ("assurance.product.workflow.graph.product-execute", "failed-join"),
)


@dataclass(frozen=True, slots=True)
class LoopSccRow:
    graph_id: str
    anchor_node_id: str
    membership: tuple[str, ...]
    target_test: str

    @property
    def anchor(self) -> tuple[str, str]:
        return (self.graph_id, self.anchor_node_id)


def _target_test(graph_id: str, anchor_node_id: str) -> str:
    if graph_id.startswith("assurance.generation."):
        path = "packages/capabilities/assurance-generation/tests/test_graph_join_any.py"
    elif graph_id.startswith("assurance.intake."):
        path = "packages/capabilities/assurance-intake/tests/test_graph_join_any.py"
    else:
        path = "tests/product/test_product_join_any.py"
    return f"{path}::test_current_trigger[{graph_id}/{anchor_node_id}]"


def collect_loop_scc_rows() -> tuple[LoopSccRow, ...]:
    rows: list[LoopSccRow] = []
    for compiled in iter_legacy_compiled_graphs():
        outgoing = {node_id: [] for node_id in compiled.nodes}
        for edge in compiled.edges:
            outgoing[edge.from_].append(edge.to)
        for component in compiled.sccs:
            members = tuple(component)
            self_edge = len(members) == 1 and members[0] in outgoing[members[0]]
            if len(members) <= 1 and not self_edge:
                continue
            anchors = [
                node_id
                for node_id in members
                if compiled.nodes[node_id].definition.kind == "join"
                and compiled.nodes[node_id].definition.join == "any"
            ]
            if len(anchors) != 1:
                raise AssertionError(f"{compiled.graph_id} loop SCC anchors={anchors} members={members}")
            rows.append(
                LoopSccRow(
                    graph_id=compiled.graph_id,
                    anchor_node_id=anchors[0],
                    membership=members,
                    target_test=_target_test(compiled.graph_id, anchors[0]),
                )
            )
    return tuple(sorted(rows, key=lambda row: (row.graph_id, row.anchor_node_id)))


LOOP_SCC_INVENTORY: tuple[LoopSccRow, ...] = (
    LoopSccRow(
        "assurance.generation.workflow.graph.generation-api",
        "plan-round-join",
        (
            "plan-round-join",
            "plan-retry",
            "plan-review-retry",
            "plan-review-round-advance-retry",
            "plan-human-review-retry",
        ),
        _target_test("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ),
    LoopSccRow(
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-round-join",
        (
            "plan-round-join",
            "plan-retry",
            "plan-review-retry",
            "plan-review-round-advance-retry",
            "plan-human-review-retry",
        ),
        _target_test("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ),
    LoopSccRow(
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-round-join",
        (
            "plan-round-join",
            "plan-retry",
            "plan-review-retry",
            "plan-review-round-advance-retry",
            "plan-human-review-retry",
        ),
        _target_test("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ),
    LoopSccRow(
        "assurance.generation.workflow.graph.generation-performance",
        "plan-round-join",
        (
            "plan-round-join",
            "plan-retry",
            "plan-review-retry",
            "plan-review-round-advance-retry",
            "plan-human-review-retry",
        ),
        _target_test("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ),
    LoopSccRow(
        "assurance.intake.workflow.graph.entry",
        "advance-join",
        (
            "advance-join",
            "case-design-retry",
            "case-review-retry",
            "review-round-advance-retry",
            "human-review-retry",
            "review-round-advance-rework-retry",
        ),
        _target_test("assurance.intake.workflow.graph.entry", "advance-join"),
    ),
    LoopSccRow(
        "assurance.product.workflow.graph.product-execute",
        "coverage-needed",
        ("quality-recheck", "coverage-needed", "coverage-repair"),
        _target_test("assurance.product.workflow.graph.product-execute", "coverage-needed"),
    ),
    LoopSccRow(
        "assurance.product.workflow.graph.product-execute",
        "failed-join",
        ("failed-join", "issue-analysis", "fix-proposal", "run"),
        _target_test("assurance.product.workflow.graph.product-execute", "failed-join"),
    ),
)
