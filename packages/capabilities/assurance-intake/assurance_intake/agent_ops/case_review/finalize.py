"""Finalize the case-review Agent result."""

from __future__ import annotations

from collections.abc import Mapping

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import CaseFinalizeInputV1
from assurance_intake.contracts.case_selection import selection_path
from assurance_intake.contracts.review import CaseMinimumCoverageReview, CaseReviewResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_intake.operations.case_review_seal import (
    collect_selected_cases,
    expected_case_selection,
    expected_review_history,
    expected_reviewed_case,
)
from assurance_intake.operations.finalize import (
    authenticate_files,
    authenticated_journey_keys,
    bound_obligations,
    case_change_id,
    file_digest,
    leafs,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    read_case_review_inputs,
    read_regular_bytes,
    reject_unbound_covered_repairs,
    require_frozen_unresolved_rows,
    require_selected_test_families,
    structured,
    validate_case_review_repair_scope,
    workspace_file,
)
from assurance_intake.operations.plan_codec import decode_plan
from assurance_intake.operations.prepare import case_review_outputs

input_model = CaseFinalizeInputV1


def _commit(payload: CaseFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    try:
        plan_path = workspace_file(context.project_root, payload.plan_ref.path)
        plan = decode_plan(plan_path.read_bytes(), payload.plan_ref)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.plan_digest != payload.plan_digest:
        raise InputError("frozen assurance plan does not match case review input")
    try:
        document = CaseReviewResultV1.model_validate(structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    runtime_document = document
    change_id = case_change_id(payload.change_id or document.change_id)
    if document.change_id != change_id:
        raise OutputError("case review change_id does not match locked change_id")
    if plan.change_id != change_id:
        raise InputError("frozen assurance plan does not match case review change_id")
    validate_case_review_repair_scope(document, payload, change_id=change_id)
    matrix_relative = "qa/results/trace/minimum-coverage-matrix.json"
    images = read_case_review_inputs(context.project_root, payload, matrix_relative)
    authored = load_authored_case_delta(
        context.project_root,
        change_id=change_id,
        locked=payload.case_delta_paths,
        declared=payload.case_delta_paths,
        capability_leafs=leafs(payload.capability_leafs),
        images=images,
    )
    require_selected_test_families(authored, plan.selected_test_families)
    matrix = load_minimum_coverage_matrix(
        context.project_root,
        relative=matrix_relative,
        authored=authored,
        selected=plan.selected_test_families,
        journey_keys=authenticated_journey_keys(
            context.project_root,
            plan.quality_goal.source_resource_digests,
        ),
        images=images,
    )
    require_frozen_unresolved_rows(context.project_root, plan, matrix)
    reject_unbound_covered_repairs(document, bound_obligations(context.project_root, plan))
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
    if payload.preparation_refs and payload.case_refs:
        artifacts = authenticate_files(
            context.write_root,
            case_review_outputs(
                change_id,
                coverage_epoch=payload.coverage_epoch,
                review_round=payload.review_round,
            ),
            payload.artifact_paths,
        )
        by_path = {item["path"]: item for item in artifacts}
        review_relative = "qa/results/review/case-review.json"
        review_ref = EvidenceArtifactRefV1.model_validate(by_path[review_relative])
        review_bytes = read_regular_bytes(context.write_root, review_relative, kind="staged case review")
        if file_digest(review_bytes) != review_ref.digest:
            raise OutputError("staged case review changed during finalization")
        try:
            staged_review = CaseReviewResultV1.model_validate_json(review_bytes)
        except ValidationError as error:
            raise OutputError(f"invalid staged case review: {error}") from error
        if staged_review != runtime_document:
            raise OutputError("staged case review differs from the typed agent result")
        selection_relative = selection_path(payload.coverage_epoch)
        history_relative = (
            f"qa/cases/reviews/epochs/{payload.coverage_epoch}/rounds/{payload.review_round}.json"
        )
        manifest_relative = "qa/cases/reviewed-case.json"
        documents: list[tuple[EvidenceArtifactRefV1, Mapping[str, object]]] = []
        for ref in payload.case_refs:
            try:
                source = yaml.safe_load(read_regular_bytes(context.write_root, ref.path, kind="case"))
            except (OutputError, yaml.YAMLError):
                source = yaml.safe_load(read_regular_bytes(context.project_root, ref.path, kind="case"))
            if not isinstance(source, Mapping):
                raise OutputError(f"case source is not a mapping: {ref.path}")
            documents.append((ref, source))
        try:
            selected = collect_selected_cases(
                payload.case_refs,
                payload.case_delta_paths,
                documents,
            )
        except ValueError as error:
            raise OutputError(str(error)) from error
        expected_selection = expected_case_selection(
            change_id=change_id,
            coverage_epoch=payload.coverage_epoch,
            plan=plan,
            cases=selected,
        )
        expected_history = expected_review_history(
            change_id=change_id,
            coverage_epoch=payload.coverage_epoch,
            review_round=payload.review_round,
            document=document,
            preparation_refs=payload.preparation_refs,
            case_refs=payload.case_refs,
            review_ref=review_ref,
        )
        derived_refs: dict[str, EvidenceArtifactRefV1] = {}
        for relative, derived in (
            (selection_relative, expected_selection),
            (history_relative, expected_history),
        ):
            data = canonical_json_bytes(derived.model_dump(mode="json")) + b"\n"
            path = workspace_file(context.write_root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            derived_refs[relative] = EvidenceArtifactRefV1(path=relative, digest=file_digest(data))
        selection_ref = derived_refs[selection_relative]
        history_ref = derived_refs[history_relative]
        expected_reviewed = expected_reviewed_case(
            change_id=change_id,
            coverage_epoch=payload.coverage_epoch,
            plan_digest=payload.plan_digest,
            plan_ref=payload.plan_ref,
            preparation_refs=payload.preparation_refs,
            case_refs=payload.case_refs,
            review_ref=review_ref,
            selection_ref=selection_ref,
        )
        manifest_bytes = canonical_json_bytes(expected_reviewed.model_dump(mode="json")) + b"\n"
        manifest_path = workspace_file(context.write_root, manifest_relative)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(manifest_bytes)
        output["artifacts"] = [
            *artifacts,
            selection_ref.model_dump(mode="json"),
            history_ref.model_dump(mode="json"),
            {"path": manifest_relative, "digest": file_digest(manifest_bytes)},
        ]
        output["reviewed_case"] = expected_reviewed.model_dump(mode="json")
        output["history_ref"] = history_ref.model_dump(mode="json")
    else:
        output["artifacts"] = []
    return TaskOutcome.succeeded(output)


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=CaseFinalizeInputV1, commit=_commit)
