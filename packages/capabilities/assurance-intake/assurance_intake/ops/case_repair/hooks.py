"""Case repair freezes the needs_fix review before the run and checks the bounded edits after it."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_intake.domain.artifacts import (
    ArtifactDigestV1,
    ArtifactListResultV1,
    authenticate_review_repair_images,
)
from assurance_intake.domain.case_delta import (
    bind_case_delta_evidence,
    case_delta_outputs,
    finalize_inventory,
    finalize_plan,
    require_receipt_paths,
    validate_case_delta,
)
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.domain.prepare_evidence import frozen_plan
from assurance_intake.domain.review_repair import review_repair_contract, validate_review_repair
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, CaseRepairOutputV1


def allowed_outputs(business: CaseRepairInputV1) -> tuple[str, ...]:
    return case_delta_outputs(business.case_delta_paths)


def before(ctx: PrepareContext, business: CaseRepairInputV1) -> CaseRepairInputV1:
    plan = ctx.dep(frozen_plan)
    business = bind_case_delta_evidence(ctx.project_root, business, plan)
    repair = review_repair_contract(
        ctx.project_root,
        change_id=business.change_id,
        case_delta_paths=business.case_delta_paths,
        plan=plan,
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
    return business.model_copy(update={"review_repair": repair})


def after(
    ctx: FinalizeContext, business: CaseRepairInputV1, result: ArtifactListResultV1
) -> CaseRepairOutputV1:
    repair = business.review_repair
    if repair is None:
        raise InputError("case repair finalize requires the prepared review_repair contract")
    plan = finalize_plan(ctx.project_root, business)
    inventory = finalize_inventory(ctx.project_root, plan, business.change_id)
    require_receipt_paths(result, business, inventory)
    if tuple(result.output_files) != tuple(repair.baseline_file_digests):
        raise OutputError("review repair receipt must exactly match the frozen case-design outputs")
    images = validate_review_repair(ctx.project_root, ctx.write_root, repair)
    artifacts = authenticate_review_repair_images(images, result.output_files, business.artifact_paths)
    validate_case_delta(
        project_root=ctx.project_root,
        write_root=ctx.write_root,
        business=business,
        plan=plan,
        inventory=inventory,
        receipt=result,
        artifacts=artifacts,
        images=images,
    )
    return CaseRepairOutputV1(
        artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
        review_repair=repair,
    )
