"""performance codegen review: check the authored suite against the frozen plan."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare
from agent_runtime_contracts.qa_paths import qa_route

from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.ops import router
from assurance_generation.ops.performance_codegen_review import hooks

op = router.agent(
    "performance.codegen-review",
    input=CodegenInputV1,
    prepare=Prepare(request=hooks.request),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-performance-codegen-reviewer",
        result=PlanReviewAuthoring,
        writes=(
            "qa/results/review/performance-codegen-review.json",
            "qa/results/review/performance-codegen-review-summary.md",
        ),
    ),
    finalize=Finalize(hook=hooks.after, writes=qa_route("codegen/performance/reviews")),
    output=PlanReview,
)

__all__ = ["op"]
