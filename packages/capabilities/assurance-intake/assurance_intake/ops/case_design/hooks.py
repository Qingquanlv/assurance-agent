"""Case design binds its evidence before the run and validates the authored delta after it."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext
from graph_engine.frozen_json import thaw_json

from assurance_intake.contracts.cases import QaYaml, require_impact_row_coverage
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.domain.artifacts import (
    ArtifactDigestV1,
    ArtifactListResultV1,
    authenticate_files,
    authenticate_review_repair_images,
    file_digest,
    leafs,
    read_regular_bytes,
    validation_repair_images,
    workspace_file,
)
from assurance_intake.domain.case_checks import (
    authenticated_journey_keys,
    bound_obligations,
    load_authored_case_delta,
    load_minimum_coverage_matrix,
    read_minimum_coverage_matrix,
    reject_endpoint_literals,
    reject_unbound_covered_repairs,
    require_frozen_unresolved_rows,
    require_selected_test_families,
    review_requests_matrix_coverage,
)
from assurance_intake.domain.case_modules import infer_case_delta_paths
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.plan_codec import decode_plan
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.domain.prepare_evidence import (
    authenticate_evidence_refs,
    frozen_plan,
    require_regular_project_input,
)
from assurance_intake.domain.review_repair import (
    ReviewRepairActionV1,
    ReviewRepairContractV1,
    validate_review_repair,
)
from assurance_intake.ops.case_design.models import CaseDesignInputV1, CaseDesignOutputV1

MARKER_PATH = "qa/.qa.yaml"
PROPOSAL_PATH = "qa/proposal.md"
MATRIX_PATH = "qa/results/trace/minimum-coverage-matrix.json"
EXPLORATION_PATH = "qa/results/explore/exploration.json"
REVIEW_PATH = "qa/results/review/case-review.json"


def case_design_outputs(change_id: str, case_delta_paths: tuple[str, ...]) -> tuple[str, ...]:
    del change_id
    return tuple(sorted((*case_delta_paths, MARKER_PATH, PROPOSAL_PATH, MATRIX_PATH)))


def allowed_outputs(business: CaseDesignInputV1) -> tuple[str, ...]:
    return case_design_outputs(business.change_id, business.case_delta_paths)


def review_repair_contract(
    project_root: Path,
    *,
    business: CaseDesignInputV1,
    plan: ResolvedAssurancePlan,
) -> ReviewRepairContractV1 | None:
    review_path = project_root.joinpath(*REVIEW_PATH.split("/"))
    if not review_path.exists() and not review_path.is_symlink():
        return None
    require_regular_project_input(project_root, REVIEW_PATH)
    review_bytes = review_path.read_bytes()
    try:
        review = CaseReviewResultV1.model_validate_json(review_bytes)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case-review.json for repair: {error}") from error
    if review.change_id != business.change_id:
        raise InputError("case-review.json change_id does not match case-design change_id")
    if review.public_outcome != "needs_fix":
        return None
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

    outputs = case_design_outputs(business.change_id, business.case_delta_paths)
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
            review_path=REVIEW_PATH,
            review_sha256=hashlib.sha256(review_bytes).hexdigest(),
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error


def _committed_inventory(
    project_root: Path, plan: ResolvedAssurancePlan, change_id: str
) -> ChangeImpactInventoryV1:
    authenticate_evidence_refs(project_root, (plan.impact_inventory_ref,))
    try:
        inventory = ChangeImpactInventoryV1.model_validate_json(
            project_root.joinpath(*plan.impact_inventory_ref.path.split("/")).read_bytes()
        )
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid impact-inventory.json: {error}") from error
    if inventory.change_id != change_id:
        raise InputError("impact-inventory.json change_id does not match case-design change_id")
    return inventory


def _committed_exploration(project_root: Path, change_id: str) -> object | None:
    path = project_root.joinpath(*EXPLORATION_PATH.split("/"))
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise InputError("exploration.json must be a regular file")
    try:
        exploration = load_exploration_document(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid exploration.json: {error}") from error
    if exploration.change_id != change_id:
        raise InputError("exploration.json change_id does not match case-design change_id")
    if exploration.context_ref != "explore/context.json":
        raise InputError("exploration.json context_ref must be explore/context.json")
    return exploration


def _surface_documents(project_root: Path, business: CaseDesignInputV1) -> dict[str, object]:
    # Lazy: quality.contracts.surface must not load at intake import time
    # (generation → intake → quality → execution → generation cycle).
    from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

    updates: dict[str, object] = {}
    if business.ui_exploration_ref is not None:
        authenticate_evidence_refs(project_root, (business.ui_exploration_ref,))
        try:
            ui_exploration = UiExplorationDocument.model_validate_json(
                project_root.joinpath(*business.ui_exploration_ref.path.split("/")).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid ui-exploration.json: {error}") from error
        if ui_exploration.change_id != business.change_id:
            raise InputError("ui-exploration.json change_id does not match case-design change_id")
        updates["ui_exploration"] = ui_exploration.model_dump(mode="json")
    if business.api_discovery_ref is not None:
        authenticate_evidence_refs(project_root, (business.api_discovery_ref,))
        try:
            api_discovery = ApiDiscoveryDocument.model_validate_json(
                project_root.joinpath(*business.api_discovery_ref.path.split("/")).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid api-discovery.json: {error}") from error
        if api_discovery.change_id != business.change_id:
            raise InputError("api-discovery.json change_id does not match case-design change_id")
        updates["api_discovery"] = api_discovery.model_dump(mode="json")
    return updates


def before(ctx: PrepareContext, business: CaseDesignInputV1) -> CaseDesignInputV1:
    plan = ctx.dep(frozen_plan)
    if business.selected_test_families != plan.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    authenticate_evidence_refs(ctx.project_root, business.preparation_refs)
    inventory = _committed_inventory(ctx.project_root, plan, business.change_id)
    if business.case_rework_context is not None:
        rework = business.case_rework_context
        authenticate_evidence_refs(ctx.project_root, rework.assessment_refs)
        authenticate_evidence_refs(
            ctx.project_root,
            (*rework.previous_case.preparation_refs, *rework.previous_case.case_refs),
        )
    exploration = _committed_exploration(ctx.project_root, business.change_id)
    business = business.model_copy(update={"exploration": exploration, "impact_inventory": inventory})
    surface = _surface_documents(ctx.project_root, business)
    if surface:
        business = business.model_copy(update=surface)
    try:
        inferred = infer_case_delta_paths(inventory)
    except ValueError:
        inferred = ()
    if inferred:
        business = business.model_copy(update={"case_delta_paths": inferred})
    elif not business.case_delta_paths:
        raise InputError("impact inventory does not imply any case module")
    review_repair = business.review_repair or review_repair_contract(
        ctx.project_root,
        business=business,
        plan=plan,
    )
    business = business.model_copy(update={"review_repair": review_repair})
    if review_repair is not None:
        ctx.use_skill("repair")
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


def _finalize_plan(project_root: Path, business: CaseDesignInputV1) -> ResolvedAssurancePlan:
    try:
        plan_path = workspace_file(project_root, business.plan_ref.path)
        plan = decode_plan(plan_path.read_bytes(), business.plan_ref)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid frozen assurance plan: {error}") from error
    if plan.change_id != business.change_id or plan.plan_digest != business.plan_digest:
        raise InputError("frozen assurance plan does not match case input")
    if plan.selected_test_families != business.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    return plan


def _finalize_inventory(
    project_root: Path, plan: ResolvedAssurancePlan, change_id: str
) -> ChangeImpactInventoryV1:
    try:
        inventory_path = workspace_file(project_root, plan.impact_inventory_ref.path)
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
    return inventory


def _require_receipt_paths(
    receipt: ArtifactListResultV1,
    business: CaseDesignInputV1,
    inventory: ChangeImpactInventoryV1,
) -> None:
    for relative in receipt.output_files:
        if not relative.startswith("qa/"):
            raise OutputError("case-design receipt may contain only current change outputs")
    missing = sorted({MARKER_PATH, PROPOSAL_PATH, MATRIX_PATH}.difference(receipt.output_files))
    if missing:
        raise OutputError("case-design receipt is missing required output files: " + ", ".join(missing))
    try:
        inferred = infer_case_delta_paths(inventory)
    except ValueError:
        inferred = ()
    if inferred:
        expected_cases = set(inferred)
    elif business.case_delta_paths:
        expected_cases = set(business.case_delta_paths)
    else:
        raise InputError("impact inventory does not imply any case module")
    declared_cases = {
        relative
        for relative in receipt.output_files
        if relative.startswith("qa/cases/") and relative.endswith("/case.yaml")
    }
    if declared_cases != expected_cases:
        missing_cases = sorted(expected_cases - declared_cases)
        unexpected_cases = sorted(declared_cases - expected_cases)
        raise OutputError(
            "case-design receipt case paths do not match locked case_delta_paths; "
            f"missing={missing_cases}, unexpected={unexpected_cases}"
        )


def after(
    ctx: FinalizeContext, business: CaseDesignInputV1, result: ArtifactListResultV1
) -> CaseDesignOutputV1:
    plan = _finalize_plan(ctx.project_root, business)
    inventory = _finalize_inventory(ctx.project_root, plan, business.change_id)
    receipt = result
    _require_receipt_paths(receipt, business, inventory)
    if business.review_repair is not None:
        if tuple(receipt.output_files) != tuple(business.review_repair.baseline_file_digests):
            raise OutputError("review repair receipt must exactly match the frozen case-design outputs")
        images = validate_review_repair(ctx.project_root, ctx.write_root, business.review_repair)
        artifacts = authenticate_review_repair_images(images, receipt.output_files, business.artifact_paths)
    elif business.validation_attempt == 1:
        images = validation_repair_images(
            ctx.project_root,
            ctx.write_root,
            receipt.output_files,
            business.artifact_paths,
        )
        artifacts = authenticate_review_repair_images(images, receipt.output_files, business.artifact_paths)
    else:
        images = None
        artifacts = authenticate_files(ctx.write_root, receipt.output_files, business.artifact_paths)
    try:
        qa_bytes = (
            images[MARKER_PATH]
            if images is not None
            else read_regular_bytes(ctx.write_root, MARKER_PATH, kind="change document")
        )
        qa_digest = next(item["digest"] for item in artifacts if item["path"] == MARKER_PATH)
        if file_digest(qa_bytes) != qa_digest:
            raise OutputError("qa/.qa.yaml changed during finalization")
        qa = QaYaml.model_validate(yaml.safe_load(qa_bytes))
    except (yaml.YAMLError, UnicodeError, ValidationError) as error:
        raise OutputError(f"invalid {MARKER_PATH}: {error}") from error
    if qa.change.change_id != business.change_id:
        raise OutputError("qa/.qa.yaml change_id does not match locked change_id")
    authored = load_authored_case_delta(
        ctx.write_root,
        change_id=business.change_id,
        locked=business.artifact_paths,
        declared=receipt.output_files,
        capability_leafs=leafs(business.capability_leafs),
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
    if business.selected_test_families:
        try:
            require_selected_test_families(authored, business.selected_test_families)
        except OutputError as error:
            validation_errors.append(str(error))
    if business.ui_exploration is not None and business.api_discovery is not None:
        # Lazy: avoid generation↔quality import cycle at module load.
        from assurance_intake.domain.surface_guard import SurfaceMismatch, assert_cases_match_surface
        from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

        try:
            api_document = ApiDiscoveryDocument.model_validate(thaw_json(business.api_discovery))
            ui_document = UiExplorationDocument.model_validate(thaw_json(business.ui_exploration))
        except ValidationError as error:
            raise OutputError(f"invalid surface document on case finalize: {error}") from error
        try:
            assert_cases_match_surface(
                authored,
                api_document,
                ui_document,
                set(business.selected_test_families),
            )
        except SurfaceMismatch as error:
            validation_errors.append(str(error))
    if authored.added or authored.modified:
        journey_keys = authenticated_journey_keys(
            ctx.project_root,
            plan.quality_goal.source_resource_digests,
        )
        try:
            matrix = load_minimum_coverage_matrix(
                ctx.write_root,
                relative=MATRIX_PATH,
                authored=authored,
                selected=plan.selected_test_families,
                journey_keys=journey_keys,
                images=images,
            )
            require_frozen_unresolved_rows(ctx.project_root, plan, matrix)
        except OutputError as error:
            validation_errors.append(str(error))
    else:
        matrix = read_minimum_coverage_matrix(ctx.write_root, relative=MATRIX_PATH, images=images)
        require_frozen_unresolved_rows(ctx.project_root, plan, matrix)
    if validation_errors:
        raise OutputError("; ".join(validation_errors))
    return CaseDesignOutputV1(
        validation_status="pass",
        validation_attempt=business.validation_attempt,
        artifacts=tuple(ArtifactDigestV1.model_validate(item) for item in artifacts),
        review_repair=business.review_repair,
    )


def on_output_error(
    ctx: FinalizeContext, business: CaseDesignInputV1, error: OutputError
) -> CaseDesignOutputV1:
    # A review repair is bound to the committed baseline: reject invalid
    # candidates before promotion so that retry keeps it. A first attempt
    # instead hands its validation error to the repair attempt.
    del ctx
    if business.review_repair is not None or business.validation_attempt != 0:
        raise error
    return CaseDesignOutputV1(
        validation_status="needs_fix",
        validation_attempt=1,
        validation_error=str(error)[:8192],
    )
