"""Case-design preparation and bounded review-repair input."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import (
    AgentBindingDataV1,
    CaseDesignInputV1,
    ReviewRepairActionV1,
    ReviewRepairContractV1,
)
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.contracts.plan import ResolvedAssurancePlan
from assurance_intake.contracts.review import (
    CaseReviewResultV1,
    normalized_auto_fix_case_id,
    normalized_auto_fix_edits,
)
from assurance_intake.operations.case_modules import infer_case_delta_paths
from assurance_intake.operations.explore_context import load_exploration_document
from assurance_intake.operations.planning_facts import build_planning_facts
from assurance_intake.operations.prepare import (
    CASE_DESIGN_PERSONA,
    CASE_DESIGN_REPAIR_SKILL,
    CASE_DESIGN_RESULT_ID,
    CASE_DESIGN_SKILL,
    case_design_outputs,
    failed_input,
    prepare_outcome,
)
from assurance_intake.operations.prepare_evidence import (
    InputError,
    _authenticate_evidence_refs,
    _authenticate_plan,
    _require_regular_project_input,
)


def _review_repair_contract(
    project_root: Path,
    *,
    business: CaseDesignInputV1,
    plan: ResolvedAssurancePlan,
) -> ReviewRepairContractV1 | None:
    review_relative = "qa/results/review/case-review.json"
    review_path = project_root.joinpath(*review_relative.split("/"))
    if not review_path.exists() and not review_path.is_symlink():
        return None
    _require_regular_project_input(project_root, review_relative)
    review_bytes = review_path.read_bytes()
    try:
        review = CaseReviewResultV1.model_validate_json(review_bytes)
    except (OSError, ValidationError, ValueError) as error:
        raise InputError(f"invalid case-review.json for repair: {error}") from error
    if review.change_id != business.change_id:
        raise InputError("case-review.json change_id does not match case-design change_id")
    if review.public_outcome != "needs_fix":
        return None
    # Imported lazily: finalize imports this module at load time.
    from assurance_intake.operations.finalize import (
        OutputError,
        _bound_obligations,
        _reject_unbound_covered_repairs,
        _review_requests_matrix_coverage,
    )

    if _review_requests_matrix_coverage(review):
        try:
            _reject_unbound_covered_repairs(review, _bound_obligations(project_root, plan))
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
        _require_regular_project_input(project_root, relative)
        data = project_root.joinpath(*relative.split("/")).read_bytes()
        baseline_file_digests[relative] = hashlib.sha256(data).hexdigest()
        if relative.endswith("/case.yaml"):
            try:
                baseline_case_documents[relative] = yaml.safe_load(data)
            except yaml.YAMLError as error:
                raise InputError(f"invalid baseline case.yaml {relative}: {error}") from error
    try:
        return ReviewRepairContractV1(
            review_path=review_relative,
            review_sha256=hashlib.sha256(review_bytes).hexdigest(),
            baseline_file_digests=baseline_file_digests,
            baseline_case_documents=baseline_case_documents,
            actions=tuple(actions),
        )
    except ValidationError as error:
        raise InputError(f"invalid deterministic case-review repair contract: {error}") from error


class CaseDesignPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            business = CaseDesignInputV1.model_validate(request.input)
            binding = AgentBindingDataV1.model_validate(request.binding_data)
            plan = _authenticate_plan(
                context.project_root,
                change_id=business.change_id,
                plan_digest=business.plan_digest,
                plan_ref=business.plan_ref,
            )
            if business.selected_test_families != plan.selected_test_families:
                raise InputError("case selected families do not match frozen assurance plan")
            _authenticate_evidence_refs(context.project_root, business.preparation_refs)
            _authenticate_evidence_refs(context.project_root, (plan.impact_inventory_ref,))
            try:
                inventory = ChangeImpactInventoryV1.model_validate_json(
                    context.project_root.joinpath(*plan.impact_inventory_ref.path.split("/")).read_bytes()
                )
            except (OSError, ValidationError, ValueError) as error:
                raise InputError(f"invalid impact-inventory.json: {error}") from error
            if inventory.change_id != business.change_id:
                raise InputError("impact-inventory.json change_id does not match case-design change_id")
            if business.case_rework_context is not None:
                rework = business.case_rework_context
                _authenticate_evidence_refs(context.project_root, rework.assessment_refs)
                _authenticate_evidence_refs(
                    context.project_root,
                    (*rework.previous_case.preparation_refs, *rework.previous_case.case_refs),
                )
            exploration_relative = "qa/results/explore/exploration.json"
            exploration_path = context.project_root.joinpath(*exploration_relative.split("/"))
            exploration = None
            if exploration_path.exists() or exploration_path.is_symlink():
                if not exploration_path.is_file() or exploration_path.is_symlink():
                    raise InputError("exploration.json must be a regular file")
                try:
                    exploration = load_exploration_document(exploration_path.read_bytes())
                except (OSError, ValidationError, ValueError) as error:
                    raise InputError(f"invalid exploration.json: {error}") from error
                if exploration.change_id != business.change_id:
                    raise InputError("exploration.json change_id does not match case-design change_id")
                if exploration.context_ref != "explore/context.json":
                    raise InputError("exploration.json context_ref must be explore/context.json")
            business = business.model_copy(update={"exploration": exploration, "impact_inventory": inventory})
            # Lazy: quality.contracts.surface must not load at intake import time
            # (generation → intake → quality → execution → generation cycle).
            from assurance_quality.contracts.surface import ApiDiscoveryDocument, UiExplorationDocument

            surface_updates: dict[str, object] = {}
            if business.ui_exploration_ref is not None:
                _authenticate_evidence_refs(context.project_root, (business.ui_exploration_ref,))
                try:
                    ui_exploration = UiExplorationDocument.model_validate_json(
                        context.project_root.joinpath(
                            *business.ui_exploration_ref.path.split("/")
                        ).read_bytes()
                    )
                except (OSError, ValidationError, ValueError) as error:
                    raise InputError(f"invalid ui-exploration.json: {error}") from error
                if ui_exploration.change_id != business.change_id:
                    raise InputError("ui-exploration.json change_id does not match case-design change_id")
                surface_updates["ui_exploration"] = ui_exploration.model_dump(mode="json")
            if business.api_discovery_ref is not None:
                _authenticate_evidence_refs(context.project_root, (business.api_discovery_ref,))
                try:
                    api_discovery = ApiDiscoveryDocument.model_validate_json(
                        context.project_root.joinpath(
                            *business.api_discovery_ref.path.split("/")
                        ).read_bytes()
                    )
                except (OSError, ValidationError, ValueError) as error:
                    raise InputError(f"invalid api-discovery.json: {error}") from error
                if api_discovery.change_id != business.change_id:
                    raise InputError("api-discovery.json change_id does not match case-design change_id")
                surface_updates["api_discovery"] = api_discovery.model_dump(mode="json")
            if surface_updates:
                business = business.model_copy(update=surface_updates)
            try:
                inferred = infer_case_delta_paths(inventory)
            except ValueError:
                inferred = ()
            if inferred:
                business = business.model_copy(update={"case_delta_paths": inferred})
            elif not business.case_delta_paths:
                raise InputError("impact inventory does not imply any case module")
            review_repair = business.review_repair or _review_repair_contract(
                context.project_root,
                business=business,
                plan=plan,
            )
            business = business.model_copy(update={"review_repair": review_repair})
            return prepare_outcome(
                skill_path=(CASE_DESIGN_REPAIR_SKILL if review_repair is not None else CASE_DESIGN_SKILL),
                persona_path=CASE_DESIGN_PERSONA,
                business=business,
                binding=binding,
                result_schema_id=CASE_DESIGN_RESULT_ID,
                context=context,
                allowed_outputs=case_design_outputs(
                    business.change_id,
                    business.case_delta_paths,
                ),
                planning_facts=build_planning_facts(
                    context.project_root,
                    change_id=business.change_id,
                    capability_leafs=business.capability_leafs,
                    families=plan.selected_test_families,
                ),
            )
        except (InputError, ValidationError) as error:
            return failed_input(error)
