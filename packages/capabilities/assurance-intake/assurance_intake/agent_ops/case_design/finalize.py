"""Finalize the case-design Agent result."""

from __future__ import annotations

import hashlib

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_model,
)
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import ArtifactDigestV1, CaseDesignOutputV1, CaseFinalizeInputV1
from assurance_intake.contracts.cases import QaYaml, require_impact_row_coverage
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.operations.case_modules import infer_case_delta_paths
from assurance_intake.operations.finalize import (
    artifact_list,
    authenticate_files,
    authenticate_review_repair_images,
    authenticated_journey_keys,
    case_change_id,
    file_digest,
    leafs,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    read_minimum_coverage_matrix,
    read_regular_bytes,
    reject_endpoint_literals,
    require_frozen_unresolved_rows,
    require_selected_test_families,
    validate_review_repair,
    validation_repair_images,
    workspace_file,
)
from assurance_intake.operations.plan_codec import decode_plan

input_model = CaseFinalizeInputV1


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    parsed: CaseFinalizeInputV1 | None = None
    try:
        payload = validate_model(CaseFinalizeInputV1, request.input)
        parsed = payload
        change_id = case_change_id(payload.change_id)
        try:
            plan_path = workspace_file(context.project_root, payload.plan_ref.path)
            plan = decode_plan(plan_path.read_bytes(), payload.plan_ref)
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid frozen assurance plan: {error}") from error
        if plan.change_id != change_id or plan.plan_digest != payload.plan_digest:
            raise InputError("frozen assurance plan does not match case input")
        if plan.selected_test_families != payload.selected_test_families:
            raise InputError("case selected families do not match frozen assurance plan")
        capability_leafs = leafs(payload.capability_leafs)
        try:
            inventory_path = workspace_file(context.project_root, plan.impact_inventory_ref.path)
            inventory_bytes = inventory_path.read_bytes()
            if hashlib.sha256(inventory_bytes).hexdigest() != plan.impact_inventory_ref.digest:
                raise InputError("impact inventory digest changed after it was committed")
            inventory = ChangeImpactInventoryV1.model_validate_json(inventory_bytes)
        except InputError:
            raise
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid impact-inventory.json: {error}") from error
        if inventory.change_id != change_id:
            raise InputError("impact inventory does not belong to the case-design change")
        receipt = artifact_list(payload)
        change_root = "qa"
        for relative in receipt.output_files:
            if not relative.startswith(f"{change_root}/"):
                raise OutputError("case-design receipt may contain only current change outputs")
        matrix_relative = f"{change_root}/results/trace/minimum-coverage-matrix.json"
        required = {
            f"{change_root}/.qa.yaml",
            f"{change_root}/proposal.md",
            matrix_relative,
        }
        missing = sorted(required.difference(receipt.output_files))
        if missing:
            raise OutputError("case-design receipt is missing required output files: " + ", ".join(missing))
        try:
            inferred = infer_case_delta_paths(inventory)
        except ValueError:
            inferred = ()
        if inferred:
            expected_cases = set(inferred)
        elif payload.case_delta_paths:
            expected_cases = set(payload.case_delta_paths)
        else:
            raise InputError("impact inventory does not imply any case module")
        declared_cases = {
            relative
            for relative in receipt.output_files
            if relative.startswith(f"{change_root}/cases/") and relative.endswith("/case.yaml")
        }
        if declared_cases != expected_cases:
            missing_cases = sorted(expected_cases - declared_cases)
            unexpected_cases = sorted(declared_cases - expected_cases)
            raise OutputError(
                "case-design receipt case paths do not match locked case_delta_paths; "
                f"missing={missing_cases}, unexpected={unexpected_cases}"
            )
        if payload.review_repair is not None:
            if tuple(receipt.output_files) != tuple(payload.review_repair.baseline_file_digests):
                raise OutputError("review repair receipt must exactly match the frozen case-design outputs")
            images = validate_review_repair(
                context.project_root,
                context.write_root,
                payload.review_repair,
            )
            artifacts = authenticate_review_repair_images(
                images,
                receipt.output_files,
                payload.artifact_paths,
            )
        elif payload.validation_attempt == 1:
            images = validation_repair_images(
                context.project_root,
                context.write_root,
                receipt.output_files,
                payload.artifact_paths,
            )
            artifacts = authenticate_review_repair_images(
                images,
                receipt.output_files,
                payload.artifact_paths,
            )
        else:
            images = None
            artifacts = authenticate_files(
                context.write_root,
                receipt.output_files,
                payload.artifact_paths,
            )
        qa_relative = f"{change_root}/.qa.yaml"
        try:
            qa_bytes = (
                images[qa_relative]
                if images is not None
                else read_regular_bytes(context.write_root, qa_relative, kind="change document")
            )
            qa_digest = next(item["digest"] for item in artifacts if item["path"] == qa_relative)
            if file_digest(qa_bytes) != qa_digest:
                raise OutputError("qa/.qa.yaml changed during finalization")
            qa = QaYaml.model_validate(yaml.safe_load(qa_bytes))
        except (yaml.YAMLError, UnicodeError, ValidationError) as error:
            raise OutputError(f"invalid {qa_relative}: {error}") from error
        if qa.change.change_id != change_id:
            raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
        authored = load_authored_case_delta(
            context.write_root,
            change_id=change_id,
            locked=payload.artifact_paths,
            declared=receipt.output_files,
            capability_leafs=capability_leafs,
            inventory=inventory,
            images=images,
        )
        try:
            require_impact_row_coverage(authored, inventory)
        except ValueError as error:
            raise OutputError(str(error)) from error
        validation_errors: list[str] = []
        try:
            reject_endpoint_literals(authored)
        except OutputError as error:
            validation_errors.append(str(error))
        if payload.selected_test_families:
            try:
                require_selected_test_families(authored, payload.selected_test_families)
            except OutputError as error:
                validation_errors.append(str(error))
        if payload.ui_exploration is not None and payload.api_discovery is not None:
            # Lazy: avoid generation↔quality import cycle at module load.
            from assurance_intake.operations.surface_guard import (
                SurfaceMismatch,
                assert_cases_match_surface,
            )
            from assurance_quality.contracts.surface import (
                ApiDiscoveryDocument,
                UiExplorationDocument,
            )

            try:
                api_document = ApiDiscoveryDocument.model_validate(thaw_json(payload.api_discovery))
                ui_document = UiExplorationDocument.model_validate(thaw_json(payload.ui_exploration))
            except ValidationError as error:
                raise OutputError(f"invalid surface document on case finalize: {error}") from error
            try:
                assert_cases_match_surface(
                    authored,
                    api_document,
                    ui_document,
                    set(payload.selected_test_families),
                )
            except SurfaceMismatch as error:
                validation_errors.append(str(error))
        if authored.added or authored.modified:
            journey_keys = authenticated_journey_keys(
                context.project_root,
                plan.quality_goal.source_resource_digests,
            )
            try:
                matrix = load_minimum_coverage_matrix(
                    context.write_root,
                    relative=matrix_relative,
                    authored=authored,
                    selected=plan.selected_test_families,
                    journey_keys=journey_keys,
                    images=images,
                )
                require_frozen_unresolved_rows(context.project_root, plan, matrix)
            except OutputError as error:
                validation_errors.append(str(error))
        else:
            matrix = read_minimum_coverage_matrix(
                context.write_root,
                relative=matrix_relative,
                images=images,
            )
            require_frozen_unresolved_rows(context.project_root, plan, matrix)
        if validation_errors:
            raise OutputError("; ".join(validation_errors))
        output = CaseDesignOutputV1(
            validation_status="pass",
            validation_attempt=payload.validation_attempt,
            artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
            review_repair=payload.review_repair,
        )
        return TaskOutcome.succeeded(output.model_dump(mode="json"))
    except InputError as error:
        return failed_input(error)
    except OutputError as error:
        if parsed is not None and parsed.review_repair is not None:
            # A review repair is bound to the committed baseline. Reject
            # invalid candidates before promotion so that retry keeps it.
            return failed_output(str(error))
        if isinstance(request.input, dict) and request.input.get("validation_attempt") == 0:
            repair_output = CaseDesignOutputV1(
                validation_status="needs_fix",
                validation_attempt=1,
                validation_error=str(error)[:8192],
                review_repair=parsed.review_repair if parsed is not None else None,
            )
            return TaskOutcome.succeeded(repair_output.model_dump(mode="json"))
        return failed_output(str(error))
