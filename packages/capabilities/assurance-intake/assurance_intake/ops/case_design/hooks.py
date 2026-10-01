"""Case design binds its evidence before the run and validates the authored delta after it."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, OutputError, PrepareContext

from assurance_intake.domain.artifacts import (
    ArtifactDigestV1,
    ArtifactListResultV1,
    authenticate_files,
    authenticate_review_repair_images,
    validation_repair_images,
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
from assurance_intake.domain.prepare_evidence import authenticate_evidence_refs, frozen_plan
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1


def allowed_outputs(business: CaseDesignInputV1) -> tuple[str, ...]:
    return case_delta_outputs(business.case_delta_paths)


def before(ctx: PrepareContext, business: CaseDesignInputV1) -> CaseDesignInputV1:
    plan = ctx.dep(frozen_plan)
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
    plan = finalize_plan(ctx.project_root, business)
    inventory = finalize_inventory(ctx.project_root, plan, business.change_id)
    require_receipt_paths(result, business, inventory)
    if business.validation_attempt == 1:
        images = validation_repair_images(
            ctx.project_root,
            ctx.write_root,
            result.output_files,
            business.artifact_paths,
        )
        artifacts = authenticate_review_repair_images(images, result.output_files, business.artifact_paths)
    else:
        images = None
        artifacts = authenticate_files(ctx.write_root, result.output_files, business.artifact_paths)
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
    return CaseDesignOutputV1(
        validation_status="pass",
        validation_attempt=business.validation_attempt,
        artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
    )


def on_output_error(
    ctx: FinalizeContext, business: CaseDesignInputV1, error: OutputError
) -> CaseDesignOutputV1:
    # A first attempt hands its validation error to the validation retry;
    # the retry itself fails the attempt.
    del ctx
    if business.validation_attempt != 0:
        raise error
    return CaseDesignOutputV1(
        validation_status="needs_fix",
        validation_attempt=1,
        validation_error=str(error)[:8192],
    )
