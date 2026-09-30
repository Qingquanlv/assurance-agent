"""Prepare the case-design Agent request."""

from __future__ import annotations

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.agent import CaseDesignInputV1
from assurance_intake.contracts.impact import ChangeImpactInventoryV1
from assurance_intake.operations.case_design_prepare import review_repair_contract
from assurance_intake.operations.case_modules import infer_case_delta_paths
from assurance_intake.domain.explore_context import load_exploration_document
from assurance_intake.domain.planning_facts import build_planning_facts
from assurance_intake.operations.prepare import (
    CASE_DESIGN_PERSONA,
    CASE_DESIGN_REPAIR_SKILL,
    CASE_DESIGN_RESULT_ID,
    CASE_DESIGN_SKILL,
    case_design_outputs,
    prepare_request,
)
from assurance_intake.operations.prepare_evidence import authenticate_evidence_refs, authenticate_plan


def _build(business: CaseDesignInputV1, binding: AgentBindingDataV1, context: TaskContext) -> AgentRunRequest:
    plan = authenticate_plan(
        context.project_root,
        change_id=business.change_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
    )
    if business.selected_test_families != plan.selected_test_families:
        raise InputError("case selected families do not match frozen assurance plan")
    authenticate_evidence_refs(context.project_root, business.preparation_refs)
    authenticate_evidence_refs(context.project_root, (plan.impact_inventory_ref,))
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
        authenticate_evidence_refs(context.project_root, rework.assessment_refs)
        authenticate_evidence_refs(
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
        authenticate_evidence_refs(context.project_root, (business.ui_exploration_ref,))
        try:
            ui_exploration = UiExplorationDocument.model_validate_json(
                context.project_root.joinpath(*business.ui_exploration_ref.path.split("/")).read_bytes()
            )
        except (OSError, ValidationError, ValueError) as error:
            raise InputError(f"invalid ui-exploration.json: {error}") from error
        if ui_exploration.change_id != business.change_id:
            raise InputError("ui-exploration.json change_id does not match case-design change_id")
        surface_updates["ui_exploration"] = ui_exploration.model_dump(mode="json")
    if business.api_discovery_ref is not None:
        authenticate_evidence_refs(context.project_root, (business.api_discovery_ref,))
        try:
            api_discovery = ApiDiscoveryDocument.model_validate_json(
                context.project_root.joinpath(*business.api_discovery_ref.path.split("/")).read_bytes()
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
    review_repair = business.review_repair or review_repair_contract(
        context.project_root,
        business=business,
        plan=plan,
    )
    business = business.model_copy(update={"review_repair": review_repair})
    return prepare_request(
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


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=CaseDesignInputV1,
        build=_build,
        input_errors=(InputError, ValidationError),
    )
