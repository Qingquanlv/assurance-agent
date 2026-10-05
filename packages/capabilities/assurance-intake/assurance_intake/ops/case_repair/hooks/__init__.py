"""Case repair freezes the needs_fix review before the run and checks the bounded edits after it."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    ArtifactListResultV1,
    FinalizeContext,
    InputError,
    OutputError,
    PrepareContext,
)

from assurance_intake.contracts import CaseYamlAuthoring
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.domain.case_checks import (
    bound_obligations,
    reject_unbound_covered_repairs,
    review_requests_matrix_coverage,
)
from assurance_intake.domain.case_delta import (
    bind_case_delta_evidence,
    case_delta_outputs,
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
from assurance_intake.domain.auto_fix import auto_fix_actions
from assurance_intake.ops.case_repair.hooks.checks import validate_review_repair
from assurance_intake.ops.case_repair.models import CaseRepairInputV1, ReviewRepairContractV1

_REVIEW_PATH = "qa/results/review/case-review.json"


def before(ctx: PrepareContext, business: CaseRepairInputV1) -> CaseRepairInputV1:
    note_case_rework(ctx)
    if business.case_refs:
        business = business.model_copy(
            update={"case_delta_paths": tuple(sorted(ref.path for ref in business.case_refs))}
        )
    plan = ctx.dep(PLAN)
    business = bind_case_delta_evidence(
        business,
        plan,
        ctx.dep(CASE_INVENTORY),
        ctx.dep(CASE_EXPLORATION),
        ctx.dep(CASE_UI),
        ctx.dep(CASE_API),
    )
    repair = _review_repair_contract(
        ctx,
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
) -> dict[str, object]:
    repair = business.review_repair
    if repair is None:
        raise InputError("case repair finalize requires the prepared review_repair contract")
    plan = ctx.dep(PLAN)
    inventory = ctx.dep(CASE_INVENTORY)
    if tuple(result.output_files) != tuple(repair.baseline_file_digests):
        raise OutputError("review repair receipt must exactly match the frozen case-design outputs")
    inputs = (repair.review_path, *repair.baseline_file_digests)
    validate_review_repair(
        repair,
        project_refs={path: ctx.project_ref(path).digest for path in inputs},
        project_images={path: ctx.project_image(path) for path in repair.baseline_file_digests},
        project_documents={path: ctx.project_file(path) for path in repair.baseline_file_digests},
        candidate_documents={path: ctx.file(path) for path in repair.baseline_file_digests},
        images=ctx.images(),
        staged_paths=ctx.staged_paths(),
    )
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
    return {"review_repair": repair.model_dump(mode="json")}


def _review_repair_contract(
    ctx: PrepareContext,
    *,
    change_id: str,
    case_delta_paths: tuple[str, ...],
    plan: ResolvedAssurancePlan,
) -> ReviewRepairContractV1:
    """Freeze the committed needs_fix review into bounded actions over the current outputs."""
    review = ctx.file(_REVIEW_PATH)
    assert isinstance(review, CaseReviewResultV1)
    if review.change_id != change_id:
        raise InputError("case-review.json change_id does not match case-repair change_id")
    if review.public_outcome != "needs_fix":
        raise InputError("case repair requires a needs_fix case-review.json")
    if review_requests_matrix_coverage(review):
        try:
            reject_unbound_covered_repairs(
                review,
                bound_obligations(
                    ctx.dep(CASE_EXPLORATION),
                    ctx.dep(CASE_CATALOG).root,
                    ctx.dep(CASE_KNOWLEDGE).root,
                ),
            )
        except OutputError as error:
            raise InputError(str(error)) from error

    actions = auto_fix_actions(review, error=InputError)
    if not actions:
        raise InputError("needs_fix case-review must provide at least one bounded repair action")

    outputs = case_delta_outputs(case_delta_paths)
    allowed = set(outputs)
    for action in actions:
        if action.artifact not in allowed:
            raise InputError(
                f"case-review repair artifact is outside the locked case-design write set: {action.artifact}"
            )
    baseline_file_digests: dict[str, str] = {}
    baseline_case_documents: dict[str, object] = {}
    for relative in outputs:
        baseline_file_digests[relative] = ctx.project_ref(relative).digest
        if relative.endswith("/case.yaml"):
            document = ctx.file(relative)
            assert isinstance(document, CaseYamlAuthoring)
            baseline_case_documents[relative] = document.model_dump(mode="json")
    try:
        return ReviewRepairContractV1(
            review_path=_REVIEW_PATH,
            review_sha256=ctx.project_ref(_REVIEW_PATH).digest,
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error
