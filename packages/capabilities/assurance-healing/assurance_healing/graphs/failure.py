from __future__ import annotations

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_healing.contracts.repair_input import RepairBoundInputV1
from assurance_healing.graphs.nodes import ProposalApprovalDecision
from assurance_healing.ops.apply_test_repair import op as apply_test_repair
from assurance_healing.ops.fix_proposal import op as fix_proposal


def build_repair_failure_graph(context: CapabilityBuildContext) -> BoundFlow:
    """Proposal, approval, and apply. Eligibility and the healing budget stay on the product."""
    flow = Flow(
        "healing",
        input=RepairBoundInputV1,
        outcomes=("applied", "needs_review", "failed"),
    )
    flow.step("fix-proposal", fix_proposal, then="approval", on_failure="failed")
    approval = flow.gate(
        "approval",
        decision=ProposalApprovalDecision,
        show=(fix_proposal.artifact("proposal"),),
        routes={"approve": "apply-test-repair", "reject": "needs_review"},
    )
    flow.step(
        "apply-test-repair",
        apply_test_repair,
        inputs={"approval_ref": approval.field("approval_ref")},
        then="applied",
        on_failure={"permanent:invalid_output": "needs_review", "*": "failed"},
    )
    return flow.bind(context)


__all__ = ["build_repair_failure_graph"]
