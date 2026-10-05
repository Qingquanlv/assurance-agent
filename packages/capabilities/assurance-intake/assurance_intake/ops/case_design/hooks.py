"""Case design binds its evidence before the run and validates the authored delta after it."""

from __future__ import annotations

from agent_runtime_contracts.ops import ArtifactListResultV1, FinalizeContext, PrepareContext

from assurance_intake.domain.case_delta import (
    bind_case_delta_evidence,
    validate_case_delta,
)
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.handoff import (
    CASE_API,
    CASE_CATALOG,
    CASE_EXPLORATION,
    CASE_INVENTORY,
    CASE_KNOWLEDGE,
    CASE_UI,
    PLAN,
    note_case_rework,
)
from assurance_intake.ops.case_design.models import CaseDesignInputV1


def before(ctx: PrepareContext, business: CaseDesignInputV1) -> CaseDesignInputV1:
    note_case_rework(ctx)
    plan = ctx.dep(PLAN)
    business = bind_case_delta_evidence(
        business,
        plan,
        ctx.dep(CASE_INVENTORY),
        ctx.dep(CASE_EXPLORATION),
        ctx.dep(CASE_UI),
        ctx.dep(CASE_API),
    )
    ctx.extra(
        "planning_facts",
        build_planning_facts(
            ctx.project_root,
            change_id=business.change_id,
            capability_leafs=business.capability_leafs,
            families=plan.selected_test_families,
        ),
    )
    return business


def after(
    ctx: FinalizeContext, business: CaseDesignInputV1, result: ArtifactListResultV1
) -> dict[str, object]:
    plan = ctx.dep(PLAN)
    inventory = ctx.dep(CASE_INVENTORY)
    validate_case_delta(
        business=business,
        plan=plan,
        inventory=inventory,
        exploration=ctx.dep(CASE_EXPLORATION),
        catalog=ctx.dep(CASE_CATALOG).root,
        knowledge=ctx.dep(CASE_KNOWLEDGE).root,
        receipt=result,
        captured={path: ctx.file(path) for path in result.output_files},
    )
    return {}
