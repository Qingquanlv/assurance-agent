"""Case repair freezes the needs_fix review before the run and checks the bounded edits after it."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    ArtifactListResultV1,
    FinalizeContext,
    InputError,
    OutputError,
    PrepareContext,
)

from assurance_intake.contracts.agent import ArtifactDigestV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    ReviewRepairActionV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.domain.artifacts import authenticate_review_repair_images
from assurance_intake.domain.case_checks import (
    bound_obligations,
    reject_unbound_covered_repairs,
    review_requests_matrix_coverage,
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
from assurance_intake.domain.prepare_evidence import frozen_plan, require_regular_project_input
from assurance_intake.ops.case_repair.hooks.checks import validate_review_repair
from assurance_intake.ops.case_repair.models import (
    CaseRepairInputV1,
    CaseRepairOutputV1,
    ReviewRepairContractV1,
)

_REVIEW_PATH = "qa/results/review/case-review.json"


def before(ctx: PrepareContext, business: CaseRepairInputV1) -> CaseRepairInputV1:
    plan = ctx.dep(frozen_plan)
    business = bind_case_delta_evidence(ctx.project_root, business, plan)
    repair = _review_repair_contract(
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


def _review_repair_contract(
    project_root: Path,
    *,
    change_id: str,
    case_delta_paths: tuple[str, ...],
    plan: ResolvedAssurancePlan,
) -> ReviewRepairContractV1:
    """Freeze the committed needs_fix review into bounded actions over the current outputs."""
    review_path = project_root.joinpath(*_REVIEW_PATH.split("/"))
    if not review_path.exists() and not review_path.is_symlink():
        raise InputError("case repair requires a committed case-review.json")
    require_regular_project_input(project_root, _REVIEW_PATH)
    review_bytes = review_path.read_bytes()
    try:
        review = CaseReviewResultV1.model_validate_json(review_bytes)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case-review.json for repair: {error}") from error
    if review.change_id != change_id:
        raise InputError("case-review.json change_id does not match case-repair change_id")
    if review.public_outcome != "needs_fix":
        raise InputError("case repair requires a needs_fix case-review.json")
    if review_requests_matrix_coverage(review):
        try:
            reject_unbound_covered_repairs(review, bound_obligations(project_root, plan))
        except OutputError as error:
            raise InputError(str(error)) from error

    findings = {finding.id: finding for finding in review.findings}
    actions: list[ReviewRepairActionV1] = []
    for raw_plan in review.auto_fix_plan:
        if not isinstance(raw_plan, Mapping):
            raise InputError("case-review auto_fix_plan items must be mappings")
        finding_id = raw_plan.get("finding_id")
        artifact = raw_plan.get("artifact")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise InputError("case-review repair finding_id must reference an existing finding")
        finding = findings[finding_id]
        try:
            case_id = normalized_auto_fix_case_id(raw_plan, finding.locator.case_id)
            edits = normalized_auto_fix_edits(raw_plan)
        except ValueError as error:
            raise InputError(str(error)) from error
        if finding.severity in {"critical", "blocking"}:
            raise InputError("critical or blocking case-review findings cannot be auto-fixed")
        if not isinstance(artifact, str) or artifact != finding.locator.artifact:
            raise InputError("case-review repair artifact must match its finding locator")
        if case_id != finding.locator.case_id:
            raise InputError("case-review repair case_id must match its finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise InputError("automatic case repair requires an exact locator key")
        allowed_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        try:
            actions.append(
                ReviewRepairActionV1(
                    finding_id=finding_id,
                    artifact=artifact,
                    case_id=case_id,
                    allowed_paths=allowed_paths,
                    instructions=edits,
                )
            )
        except ValidationError as error:
            raise InputError(f"invalid case-review repair action: {error}") from error
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
        require_regular_project_input(project_root, relative)
        data = project_root.joinpath(*relative.split("/")).read_bytes()
        baseline_file_digests[relative] = hashlib.sha256(data).hexdigest()
        if relative.endswith("/case.yaml"):
            try:
                baseline_case_documents[relative] = yaml.safe_load(data)
            except yaml.YAMLError as error:
                raise InputError(f"invalid baseline case.yaml {relative}: {error}") from error
    try:
        return ReviewRepairContractV1(
            review_path=_REVIEW_PATH,
            review_sha256=hashlib.sha256(review_bytes).hexdigest(),
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error
