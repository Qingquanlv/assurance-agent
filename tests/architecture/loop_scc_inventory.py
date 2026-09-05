from __future__ import annotations

from dataclasses import dataclass

EXPECTED_LOOP_SCC_ANCHORS = (
    ("assurance.generation.workflow.graph.generation-api", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-round-join"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-round-join"),
    ("assurance.intake.workflow.graph.entry", "advance-join"),
    ("assurance.product.workflow.graph.product-execute", "fix-proposal"),
    ("assurance.product.workflow.graph.product-full", "advance-coverage"),
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
        path = "tests/product/test_product_stategraph_flow.py"
    return f"{path}::test_full_uses_internal_execute_tail_while_public_execute_wraps_it"


def collect_loop_scc_rows() -> tuple[LoopSccRow, ...]:
    return LOOP_SCC_INVENTORY


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
        "fix-proposal",
        ("fix-proposal", "adapt-rerun", "run", "adapt-quality", "quality", "adapt-repair-failure"),
        _target_test("assurance.product.workflow.graph.product-execute", "fix-proposal"),
    ),
    LoopSccRow(
        "assurance.product.workflow.graph.product-full",
        "advance-coverage",
        ("advance-coverage", "adapt-case", "case", "adapt-execute-tail", "execute-tail"),
        _target_test("assurance.product.workflow.graph.product-full", "advance-coverage"),
    ),
)
