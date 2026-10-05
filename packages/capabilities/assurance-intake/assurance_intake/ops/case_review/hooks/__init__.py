"""Case review binds the reviewed delta before the run and seals its decision after it."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_intake.contracts import CaseYamlAuthoring, MinimumCoverageMatrixAuthoring
from assurance_intake.contracts.case_selection import selection_path
from assurance_intake.contracts.review import (
    CaseMinimumCoverageReview,
    CaseReviewResultV1,
)
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_intake.domain.artifacts import leafs
from assurance_intake.domain.case_checks import (
    authenticated_journey_keys,
    bound_obligations,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    reject_unbound_covered_repairs,
    require_frozen_unresolved_rows,
    require_selected_test_families,
)
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.handoff import (
    CASE,
    CASE_CATALOG,
    CASE_EXPLORATION,
    CASE_KNOWLEDGE,
    PLAN,
    REVIEW_PLAN,
    note_case_rework,
)
from assurance_intake.ops.case_review.hooks.checks import (
    validate_case_review_repair_scope,
)
from assurance_intake.ops.case_review.hooks.seal import (
    collect_selected_cases,
    expected_case_selection,
    expected_review_history,
    expected_reviewed_case,
)
from assurance_intake.ops.case_review.models import CaseReviewInputV1

REVIEW_PATH = "qa/results/review/case-review.json"
SUMMARY_PATH = "qa/results/review/case-review-summary.md"
MATRIX_PATH = "qa/results/trace/minimum-coverage-matrix.json"
REVIEWED_CASE_PATH = "qa/cases/reviewed-case.json"
REVIEW_HISTORY_ROOT = "qa/cases/reviews"
SELECTION_ROOT = "qa/results/cases/epochs"


def _require_reviewed_case_matches_ledger(ctx: FinalizeContext, reviewed: ReviewedCaseV1) -> None:
    opened = ctx.dep(CASE)
    if not isinstance(opened, tuple):
        raise OutputError("reviewed case refs do not match the artifact ledger")
    digests = tuple(
        sorted(hashlib.sha256(item).hexdigest() for item in opened if isinstance(item, (bytes, bytearray)))
    )
    expected = tuple(sorted(ref.digest for ref in reviewed.case_refs))
    if len(opened) != len(reviewed.case_refs) or digests != expected:
        raise OutputError("reviewed case refs do not match the artifact ledger")


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


def before(ctx: PrepareContext, business: CaseReviewInputV1) -> CaseReviewInputV1:
    note_case_rework(ctx)
    if business.case_refs:
        business = business.model_copy(
            update={"case_delta_paths": tuple(sorted(ref.path for ref in business.case_refs))}
        )
    plan = ctx.dep(PLAN)
    if business.case_delta_paths and not set(business.case_delta_paths) <= {
        item.path for item in business.case_refs
    }:
        raise InputError("case_refs must bind every locked case_delta_path")
    if sum(ref.path == MATRIX_PATH for ref in business.preparation_refs) != 1:
        raise InputError("preparation_refs must authenticate the minimum coverage matrix for case review")
    review_inputs = case_review_inputs(business.change_id, business.case_delta_paths)
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
    plan = ctx.dep(REVIEW_PLAN)
    if plan.plan_digest != business.plan_digest:
        raise InputError("frozen assurance plan does not match case review input")
    document = result
    change_id = business.change_id
    if plan.change_id != change_id:
        raise InputError("frozen assurance plan does not match case review change_id")
    validate_case_review_repair_scope(document, business, change_id=change_id)
    case_documents = {path: ctx.project_file(path) for path in business.case_delta_paths}
    assert all(isinstance(item, CaseYamlAuthoring) for item in case_documents.values())
    authored = load_authored_case_delta(
        locked=business.case_delta_paths,
        declared=business.case_delta_paths,
        capability_leafs=leafs(business.capability_leafs),
        documents=cast(dict[str, CaseYamlAuthoring], case_documents),
    )
    require_selected_test_families(authored, plan.selected_test_families)
    matrix_document = ctx.project_file(MATRIX_PATH)
    assert isinstance(matrix_document, MinimumCoverageMatrixAuthoring)
    knowledge = ctx.dep(CASE_KNOWLEDGE).root
    obligations = bound_obligations(ctx.dep(CASE_EXPLORATION), ctx.dep(CASE_CATALOG).root, knowledge)
    matrix = load_minimum_coverage_matrix(
        document=matrix_document,
        authored=authored,
        selected=plan.selected_test_families,
        journey_keys=authenticated_journey_keys(knowledge),
    )
    require_frozen_unresolved_rows(obligations, matrix)
    reject_unbound_covered_repairs(document, obligations)
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
    staged_review = ctx.file(REVIEW_PATH)
    if staged_review != result:
        raise OutputError("staged case review differs from the typed agent result")
    if not (business.preparation_refs and business.case_refs):
        return output
    review_ref = EvidenceArtifactRefV1.model_validate(ctx.ref(REVIEW_PATH).model_dump(mode="json"))
    selection_relative = selection_path(business.coverage_epoch)
    history_relative = (
        f"{REVIEW_HISTORY_ROOT}/epochs/{business.coverage_epoch}/rounds/{business.review_round}.json"
    )
    documents: list[tuple[EvidenceArtifactRefV1, Mapping[str, object]]] = []
    for ref in business.case_refs:
        source = case_documents.get(ref.path)
        if not isinstance(source, CaseYamlAuthoring):
            raise InputError(f"case source is not a declared case: {ref.path}")
        documents.append((ref, source.model_dump(mode="json")))
    selected = collect_selected_cases(business.case_refs, business.case_delta_paths, documents)
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
    selection_ref = EvidenceArtifactRefV1.model_validate(
        ctx.stage(selection_relative, expected_selection).model_dump(mode="json")
    )
    history_ref = EvidenceArtifactRefV1.model_validate(
        ctx.stage(history_relative, expected_history).model_dump(mode="json")
    )
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
    if (
        expected_reviewed.change_id != business.change_id
        or expected_reviewed.coverage_epoch != business.coverage_epoch
    ):
        raise OutputError("reviewed case does not match the case input")
    _require_reviewed_case_matches_ledger(ctx, expected_reviewed)
    ctx.stage(REVIEWED_CASE_PATH, expected_reviewed)
    output["reviewed_case"] = expected_reviewed.model_dump(mode="json")
    output["history_ref"] = history_ref.model_dump(mode="json")
    return output
