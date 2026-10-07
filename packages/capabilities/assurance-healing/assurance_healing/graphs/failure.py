from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_healing.contracts.repair_input import RepairBoundInputV1
from assurance_healing.ops.apply_test_repair import op as apply_test_repair
from assurance_healing.ops.fix_proposal import op as fix_proposal


def build_repair_failure_graph(context: CapabilityBuildContext) -> BoundFlow:
    """Propose and apply a bounded test repair; the product owns eligibility and budget."""
    flow = Flow(
        "healing",
        input=RepairBoundInputV1,
        outcomes=("applied", "needs_review", "failed"),
    )
    flow.step("fix-proposal", fix_proposal, then="apply-test-repair", on_failure="failed")
    flow.step(
        "apply-test-repair",
        apply_test_repair,
        then="applied",
        on_failure={"invalid_output": "needs_review", "*": "failed"},
    )
    return flow.bind(context)


__all__ = ["build_repair_failure_graph"]
