"""Plan-check registry; its declaration order defines document check ordering."""

from __future__ import annotations

from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument
from assurance_agent.verification.checks.base import CheckContext, CheckFn
from assurance_agent.verification.checks.l1_path import check_l1_path
from assurance_agent.verification.checks.shared_factory import check_shared_factory

PLAN_CHECKS: tuple[CheckFn, ...] = (check_l1_path, check_shared_factory)


def run_plan_checks(ctx: CheckContext) -> PlanCheckDocument:
    return PlanCheckDocument.from_checks([check(ctx) for check in PLAN_CHECKS])
