"""Case review binds the reviewed delta before the run and seals its decision after it."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext
from graph_engine.canonical import canonical_json_bytes

from assurance_intake.contracts.case_selection import selection_path
from assurance_intake.contracts.review import (
    CaseMinimumCoverageReview,
    CaseReviewResultV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.domain.artifacts import (
    authenticate_files,
    file_digest,
    leafs,
    read_regular_bytes,
    workspace_file,
)
from assurance_intake.domain.case_checks import (
    authenticated_journey_keys,
    bound_obligations,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    reject_unbound_covered_repairs,
    require_frozen_unresolved_rows,
    require_selected_test_families,
)
from assurance_intake.domain.case_review_seal import (
    collect_selected_cases,
    expected_case_selection,
    expected_review_history,
    expected_reviewed_case,
)
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.domain.prepare_evidence import (
    authenticate_evidence_refs,
    frozen_plan,
    require_regular_project_input,
)
from assurance_intake.domain.review_repair import ReviewRepairActionV1
from assurance_intake.ops.case_review.models import CaseReviewInputV1

REVIEW_PATH = "qa/results/review/case-review.json"
SUMMARY_PATH = "qa/results/review/case-review-summary.md"
MATRIX_PATH = "qa/results/trace/minimum-coverage-matrix.json"
REVIEWED_CASE_PATH = "qa/cases/reviewed-case.json"
REVIEW_HISTORY_ROOT = "qa/cases/reviews"
SELECTION_ROOT = "qa/results/cases/epochs"


def case_review_outputs(
    change_id: str,
    *,
    coverage_epoch: int = 0,
    review_round: int = 0,
) -> tuple[str, ...]:
    del change_id, coverage_epoch, review_round
    return (SUMMARY_PATH, REVIEW_PATH)


def case_review_inputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    del change_id
    return tuple(
        sorted(
            (
                "qa/.qa.yaml",
                *case_delta_paths,
                "qa/proposal.md",
                "qa/requirement.md",
                MATRIX_PATH,
            )
        )
    )


def validate_case_review_repair_scope(
    document: CaseReviewResultV1,
    payload: CaseReviewInputV1,
    *,
    change_id: str,
) -> None:
    if document.public_outcome != "needs_fix":
        return
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required to lock case-review automatic repairs")
    change_root = "qa"
    allowed = {
        f"{change_root}/.qa.yaml",
        f"{change_root}/proposal.md",
        f"{change_root}/results/trace/minimum-coverage-matrix.json",
        *payload.case_delta_paths,
    }
    if document.next_action != "run_case_design":
        raise OutputError("needs_fix case review next_action must be run_case_design")
    findings = {finding.id: finding for finding in document.findings}
    if not document.auto_fix_plan:
        raise OutputError("needs_fix case review must provide at least one auto_fix_plan item")
    planned_findings: set[str] = set()
    for item in document.auto_fix_plan:
        if not isinstance(item, Mapping):
            raise OutputError("case review auto_fix_plan items must be mappings")
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise OutputError("case review auto_fix_plan finding_id must reference an existing finding")
        if finding_id in planned_findings:
            raise OutputError("case review auto_fix_plan must not duplicate a finding_id")
        planned_findings.add(finding_id)
        finding = findings[finding_id]
        flags = finding.model_extra or {}
        if flags.get("auto_fix_allowed") is not True or flags.get("human_review_required") is not False:
            raise OutputError(
                "automatic repair finding must explicitly allow auto-fix and forbid human review"
            )
        if finding.severity in {"critical", "blocking"}:
            raise OutputError("critical or blocking case review finding cannot be auto-fixed")
        artifact = item.get("artifact")
        if not isinstance(artifact, str) or artifact not in allowed:
            raise OutputError(
                f"automatic repair artifact is outside the locked case-design write set: {artifact!r}"
            )
        if artifact != finding.locator.artifact:
            raise OutputError("automatic repair artifact must match the finding locator")
        try:
            case_id = normalized_auto_fix_case_id(item, finding.locator.case_id)
            edits = normalized_auto_fix_edits(item)
        except ValueError as error:
            raise OutputError(str(error)) from error
        if case_id != finding.locator.case_id:
            raise OutputError("automatic repair case_id must match the finding locator")
        key = finding.locator.key
        if not isinstance(key, str) or not key.strip():
            raise OutputError("automatic case repair requires an exact locator key")
        locator_paths = tuple(part.strip() for part in key.split(",") if part.strip())
        if artifact in payload.case_delta_paths and "case_id" in locator_paths:
            raise OutputError(
                "case_id identifies the case and cannot be an automatic repair field; "
                "use added or modified to authorize whole-case removal"
            )
        try:
            ReviewRepairActionV1(
                finding_id=finding_id,
                artifact=artifact,
                case_id=finding.locator.case_id,
                allowed_paths=locator_paths,
                instructions=edits,
            )
        except ValidationError as error:
            raise OutputError(f"invalid automatic repair locator: {error}") from error


def read_case_review_inputs(
    workspace: Path,
    payload: CaseReviewInputV1,
    matrix_relative: str,
) -> dict[str, bytes]:
    if not payload.case_delta_paths:
        raise InputError("case_delta_paths are required for case-review scope validation")
    if payload.case_delta_paths and not set(payload.case_delta_paths) <= {
        ref.path for ref in payload.case_refs
    }:
        raise InputError("case_refs must authenticate every locked case.yaml for case review")
    matrix_refs = tuple(ref for ref in payload.preparation_refs if ref.path == matrix_relative)
    if len(matrix_refs) != 1:
        raise InputError("preparation_refs must authenticate the minimum coverage matrix for case review")
    images: dict[str, bytes] = {}
    for ref in (*payload.case_refs, *matrix_refs):
        data = read_regular_bytes(workspace, ref.path, kind="case-review input")
        if file_digest(data) != ref.digest:
            raise OutputError(f"case-review input digest mismatch: {ref.path}")
        images[ref.path] = data
    return images


def before(ctx: PrepareContext, business: CaseReviewInputV1) -> CaseReviewInputV1:
    plan = ctx.dep(frozen_plan)
    authenticate_evidence_refs(ctx.project_root, business.preparation_refs)
    authenticate_evidence_refs(ctx.project_root, business.case_refs)
    if business.case_delta_paths and not set(business.case_delta_paths) <= {
        item.path for item in business.case_refs
    }:
        raise InputError("case_refs must bind every locked case_delta_path")
    review_inputs = case_review_inputs(business.change_id, business.case_delta_paths)
    for relative in review_inputs:
        require_regular_project_input(ctx.project_root, relative)
    business = CaseReviewInputV1.model_validate(
        {**business.model_dump(mode="json"), "review_input_paths": review_inputs}
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


def after(ctx: FinalizeContext, business: CaseReviewInputV1, result: CaseReviewResultV1) -> dict[str, Any]:
    try:
        plan_path = workspace_file(ctx.project_root, business.plan_ref.path)
        plan = decode_plan(plan_path.read_bytes(), business.plan_ref)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.plan_digest != business.plan_digest:
        raise InputError("frozen assurance plan does not match case review input")
    document = result
    change_id = business.change_id
    if document.change_id != change_id:
        raise OutputError("case review change_id does not match locked change_id")
    if plan.change_id != change_id:
        raise InputError("frozen assurance plan does not match case review change_id")
    validate_case_review_repair_scope(document, business, change_id=change_id)
    images = read_case_review_inputs(ctx.project_root, business, MATRIX_PATH)
    authored = load_authored_case_delta(
        ctx.project_root,
        change_id=change_id,
        locked=business.case_delta_paths,
        declared=business.case_delta_paths,
        capability_leafs=leafs(business.capability_leafs),
        images=images,
    )
    require_selected_test_families(authored, plan.selected_test_families)
    matrix = load_minimum_coverage_matrix(
        ctx.project_root,
        relative=MATRIX_PATH,
        authored=authored,
        selected=plan.selected_test_families,
        journey_keys=authenticated_journey_keys(
            ctx.project_root,
            plan.quality_goal.source_resource_digests,
        ),
        images=images,
    )
    require_frozen_unresolved_rows(ctx.project_root, plan, matrix)
    reject_unbound_covered_repairs(document, bound_obligations(ctx.project_root, plan))
    required = [row for row in matrix.root if row.required]
    expected_projection = {
        "total_required": len(required),
        "covered": sum(row.status == "covered" for row in required),
        "skipped_by_scope": sum(row.status == "skipped_by_scope" for row in required),
        "missing": [row.key or row.mrc_id for row in required if row.status == "skipped_by_scope"],
    }
    document = document.model_copy(
        update={"minimum_coverage": CaseMinimumCoverageReview.model_validate(expected_projection)}
    )
    output = document.model_dump(mode="json")
    if not (business.preparation_refs and business.case_refs):
        output["artifacts"] = []
        return output
    artifacts = authenticate_files(
        ctx.write_root,
        case_review_outputs(
            change_id,
            coverage_epoch=business.coverage_epoch,
            review_round=business.review_round,
        ),
        business.artifact_paths,
    )
    by_path = {item["path"]: item for item in artifacts}
    review_ref = EvidenceArtifactRefV1.model_validate(by_path[REVIEW_PATH])
    review_bytes = read_regular_bytes(ctx.write_root, REVIEW_PATH, kind="staged case review")
    if file_digest(review_bytes) != review_ref.digest:
        raise OutputError("staged case review changed during finalization")
    try:
        staged_review = CaseReviewResultV1.model_validate_json(review_bytes)
    except ValidationError as error:
        raise OutputError(f"invalid staged case review: {error}") from error
    if staged_review != result:
        raise OutputError("staged case review differs from the typed agent result")
    selection_relative = selection_path(business.coverage_epoch)
    history_relative = (
        f"{REVIEW_HISTORY_ROOT}/epochs/{business.coverage_epoch}/rounds/{business.review_round}.json"
    )
    documents: list[tuple[EvidenceArtifactRefV1, Mapping[str, object]]] = []
    for ref in business.case_refs:
        try:
            source = yaml.safe_load(read_regular_bytes(ctx.write_root, ref.path, kind="case"))
        except (OutputError, yaml.YAMLError):
            source = yaml.safe_load(read_regular_bytes(ctx.project_root, ref.path, kind="case"))
        if not isinstance(source, Mapping):
            raise OutputError(f"case source is not a mapping: {ref.path}")
        documents.append((ref, source))
    try:
        selected = collect_selected_cases(business.case_refs, business.case_delta_paths, documents)
    except ValueError as error:
        raise OutputError(str(error)) from error
    expected_selection = expected_case_selection(
        change_id=change_id,
        coverage_epoch=business.coverage_epoch,
        plan=plan,
        cases=selected,
    )
    expected_history = expected_review_history(
        change_id=change_id,
        coverage_epoch=business.coverage_epoch,
        review_round=business.review_round,
        document=document,
        preparation_refs=business.preparation_refs,
        case_refs=business.case_refs,
        review_ref=review_ref,
    )
    derived_refs: dict[str, EvidenceArtifactRefV1] = {}
    for relative, derived in ((selection_relative, expected_selection), (history_relative, expected_history)):
        data = canonical_json_bytes(derived.model_dump(mode="json")) + b"\n"
        ctx.write(relative, data)
        derived_refs[relative] = EvidenceArtifactRefV1(path=relative, digest=file_digest(data))
    selection_ref = derived_refs[selection_relative]
    history_ref = derived_refs[history_relative]
    expected_reviewed = expected_reviewed_case(
        change_id=change_id,
        coverage_epoch=business.coverage_epoch,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        preparation_refs=business.preparation_refs,
        case_refs=business.case_refs,
        review_ref=review_ref,
        selection_ref=selection_ref,
    )
    manifest_bytes = canonical_json_bytes(expected_reviewed.model_dump(mode="json")) + b"\n"
    ctx.write(REVIEWED_CASE_PATH, manifest_bytes)
    output["artifacts"] = [
        *artifacts,
        selection_ref.model_dump(mode="json"),
        history_ref.model_dump(mode="json"),
        {"path": REVIEWED_CASE_PATH, "digest": file_digest(manifest_bytes)},
    ]
    output["reviewed_case"] = expected_reviewed.model_dump(mode="json")
    output["history_ref"] = history_ref.model_dump(mode="json")
    return output
