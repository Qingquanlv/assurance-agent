"""Improvement review: judge one improvement against its locked subject."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Prepare

from assurance_improvement.contracts.agent import ImprovementReviewResultV1, ImprovementSkillInputV1
from assurance_improvement.ops import router
from assurance_improvement.ops.improvement_review import hooks

op = router.agent(
    "improvement-review",
    input=ImprovementSkillInputV1,
    prepare=Prepare(),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-improvement-reviewer",
        result=ImprovementReviewResultV1,
        writes=("qa/results/review/improvement-review.json",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=ImprovementReviewResultV1,
)

__all__ = ["op"]
