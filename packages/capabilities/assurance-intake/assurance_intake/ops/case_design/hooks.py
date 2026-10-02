"""Case design binds its evidence before the run and validates the authored delta after it."""

from __future__ import annotations

from agent_runtime_contracts.ops import ArtifactListResultV1, FinalizeContext, PrepareContext

from assurance_intake.contracts.agent import ArtifactDigestV1
from assurance_intake.domain.artifacts import authenticate_files
from assurance_intake.domain.case_delta import (
    bind_case_delta_evidence,
    finalize_inventory,
    require_receipt_paths,
    validate_case_delta,
)
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.domain.prepare_evidence import authenticate_evidence_refs
from assurance_intake.handoff import PLAN
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1


def before(ctx: PrepareContext, business: CaseDesignInputV1) -> CaseDesignInputV1:
    plan = ctx.dep(PLAN)
    business = bind_case_delta_evidence(ctx.project_root, business, plan)
    if business.case_rework_context is not None:
        rework = business.case_rework_context
        authenticate_evidence_refs(ctx.project_root, rework.assessment_refs)
        authenticate_evidence_refs(
            ctx.project_root,
            (*rework.previous_case.preparation_refs, *rework.previous_case.case_refs),
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
) -> CaseDesignOutputV1:
    plan = ctx.dep(PLAN)
    inventory = finalize_inventory(ctx.project_root, plan, business.change_id)
    require_receipt_paths(result, business, inventory)
    artifacts = authenticate_files(ctx.write_root, result.output_files, business.artifact_paths)
    validate_case_delta(
        project_root=ctx.project_root,
        write_root=ctx.write_root,
        business=business,
        plan=plan,
        inventory=inventory,
        receipt=result,
        artifacts=artifacts,
        images=None,
    )
    return CaseDesignOutputV1(
        artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
    )
