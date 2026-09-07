from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from assurance_execution.contracts.workflow import (
    ExecutionCycleResultV1,
    VerifiedExecutionCycleResultV1,
    VerifiedGenerationDefectCycleV1,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_healing.contracts.application import AppliedTestRepairV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_product.graphs.entrypoints import (
    _input_from_state,
    adapt_load_plan,
    publish_public_output,
    validate_public_input,
)
from assurance_product.graphs.routes import (
    route_applied_repair,
    route_execute,
    route_quality,
    route_run,
    route_prepare,
)
from assurance_product.graphs.state import ProductState
from assurance_product.graphs.tail_contracts import (
    ExecuteTailInputV1,
    ExecuteTailResultV1,
    reported_tail_result,
)
from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1
from graph_engine.boot.boot import GraphBuildContext
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest


def adapt_execute_tail_input(
    state: ProductState,
    *,
    standalone: bool,
) -> dict[str, object]:
    payload = _input_from_state(state)
    reviewed_case: object = None if standalone else state.get("reviewed_case")
    if reviewed_case is None and not standalone:
        case_result = state.get("case_result")
        if isinstance(case_result, Mapping):
            reviewed_case = case_result.get("reviewed_case")
    source_artifacts = state.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        source_artifacts = [item.model_dump(mode="json") for item in payload.artifacts]
    tail_input = ExecuteTailInputV1.model_validate(
        {
            "change_id": payload.change_id,
            "requirement": payload.requirement,
            "run_mode": payload.run_mode,
            "coverage_epoch": 0 if standalone else state.get("coverage_epoch", 0),
            "plan_digest": state.get("plan_digest"),
            "plan_ref": state.get("plan_ref"),
            "reviewed_case": reviewed_case,
            "source_artifacts": source_artifacts,
            "selected_test_families": state.get("selected_test_families"),
            "capability_leafs": payload.capability_leafs,
            "capability_catalog": payload.capability_catalog,
            "product_policy": payload.product_policy,
            "data_knowledge": payload.data_knowledge,
            "allowed_artifact_paths": payload.allowed_artifact_paths,
            "budgets": payload.budgets,
            "decision": payload.decision,
        }
    )
    update = tail_input.model_dump(mode="json")
    update["artifacts"] = list(update["source_artifacts"])
    update["healing_rounds_used"] = int(state.get("healing_rounds_used", 0))
    return update


def adapt_public_execute_tail(state: ProductState) -> dict[str, object]:
    return adapt_execute_tail_input(state, standalone=True)


def adapt_generation(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    source_artifacts = state.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        source_artifacts = [item.model_dump(mode="json") for item in payload.artifacts]
    feature_input = {
        "change_id": payload.change_id,
        "plan_digest": state.get("plan_digest"),
        "plan_ref": state.get("plan_ref"),
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "reviewed_case": state.get("reviewed_case"),
        "source_artifacts": source_artifacts,
        "selected_test_families": list(state.get("selected_test_families") or []),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "artifacts": [item.model_dump(mode="json") for item in payload.artifacts],
        "decision": payload.decision,
    }
    return {**feature_input, "feature_input": feature_input}


def _route_generation(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return "blocked"
    try:
        result = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    except (TypeError, ValueError):
        return "blocked"
    if result.change_id == state.get("change_id") and result.coverage_epoch == state.get("coverage_epoch", 0):
        return "execution"
    return "blocked"


def adapt_execution(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "validation_profile": payload.validation_profile,
        "verification_config_digest": payload.verification_config_digest,
        "verification": state.get("verification"),
        "plan_digest": state.get("plan_digest"),
        "plan_ref": state.get("plan_ref"),
        "capability_leafs": list(payload.capability_leafs),
        "selected_test_families": list(state.get("selected_test_families") or []),
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "repair_round": 0,
        "rounds_budget": payload.budgets.healing_rounds,
        "rounds_used": int(state.get("healing_rounds_used", 0)),
        "generation_result": state.get("generation_result"),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_rerun(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    repair = AppliedTestRepairV1.model_validate(state.get("repair_result"))
    if repair.status != "applied" or repair.mapping_ref is None:
        raise ValueError("rerun requires an applied test repair")
    source_refs = {ref.path: ref for ref in generation.source_refs}
    source_refs.update({ref.path: ref for ref in repair.changed_test_refs})
    repaired_generation = generation.model_copy(
        update={
            "mapping_ref": repair.mapping_ref,
            "source_refs": tuple(source_refs[path] for path in sorted(source_refs)),
        }
    )
    repair_round = int(state.get("healing_rounds_used") or state.get("rounds_used") or 0)
    feature_input = {
        "change_id": payload.change_id,
        "validation_profile": payload.validation_profile,
        "verification_config_digest": payload.verification_config_digest,
        "verification": state.get("verification"),
        "plan_digest": state.get("plan_digest"),
        "plan_ref": state.get("plan_ref"),
        "capability_leafs": list(payload.capability_leafs),
        "selected_test_families": list(state.get("selected_test_families") or []),
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "repair_round": repair_round,
        "rounds_budget": int(state.get("rounds_budget") or payload.budgets.healing_rounds),
        "rounds_used": repair_round,
        "generation_result": repaired_generation.model_dump(mode="json"),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_quality_assess(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    if payload.validation_profile is None:
        execution: ExecutionCycleResultV1 | VerifiedExecutionCycleResultV1 = (
            ExecutionCycleResultV1.model_validate(state.get("execution_result"))
        )
        execution_status = "passed" if execution.final_status == "PASS" else "failed"
    else:
        execution = VerifiedExecutionCycleResultV1.model_validate(state.get("execution_result"))
        if execution.validation_profile != payload.validation_profile:
            raise ValueError("verified execution profile differs from the product input")
        execution_status = execution.completion_status
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    reviewed = generation.reviewed_case
    current_reviewed = state.get("reviewed_case")
    if current_reviewed is not None and ReviewedCaseV1.model_validate(current_reviewed) != reviewed:
        raise ValueError("execution does not use the current Reviewed Case")
    feature_input = {
        "change_id": payload.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "rounds_budget": payload.budgets.coverage_rounds,
        "rounds_used": int(state.get("coverage_epoch", 0)),
        "evidence_refs": [],
        "execution_status": execution_status,
        "batch_id": execution.batch_id,
        "coverage_epoch": execution.coverage_epoch,
        "reviewed_case": reviewed.model_dump(mode="json"),
        "generation_result": generation.model_dump(mode="json"),
        "execution_result": execution.model_dump(mode="json"),
        "policy_resource_id": payload.product_policy.resource_id,
        "policy_sha256": payload.product_policy.sha256,
        "execution_at": execution.executed_at.isoformat(),
        "healing_ref": state.get("healing_ref"),
        "issue_ref": state.get("issue_ref"),
        "activation": {
            "kind": "trigger",
            "value": f"inspect.{execution.coverage_epoch}.{execution.batch_id}.{execution.repair_round}",
        },
    }
    public = payload.model_dump(mode="json", exclude={"retro_window"})
    return {**public, **feature_input, "feature_input": feature_input}


def adapt_repair_failure(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    reviewed = generation.reviewed_case
    next_round = int(state.get("healing_rounds_used", 0)) + 1
    raw_execution = state.get("execution_result")
    defect_cycle: VerifiedGenerationDefectCycleV1 | None = None
    if payload.validation_profile is None:
        execution = ExecutionCycleResultV1.model_validate(raw_execution)
        defect = None
        allowed_paths = tuple(sorted(ref.path for ref in generation.source_refs))
        baseline_digest = execution.evidence_ref.digest
        candidate_digest = execution.receipt.receipt_digest
        evidence_digest = execution.evidence_ref.digest
        coverage_epoch = execution.coverage_epoch
        execution_ref: dict[str, str] | None = execution.evidence_ref.model_dump(mode="json")
    else:
        defect_cycle = VerifiedGenerationDefectCycleV1.model_validate(raw_execution)
        defect = defect_cycle.attempt.defect
        declared_defect = state.get("generation_defect")
        declared_authority = state.get("generation_defect_authority_ref")
        if (
            declared_defect is not None
            and VerifiedGenerationDefectCycleV1.model_validate(declared_defect) != defect_cycle
        ) or (
            declared_authority is not None
            and declared_authority != defect_cycle.attempt.authority_receipt.model_dump(mode="json")
        ):
            raise ValueError("verified generation defect provenance differs from execution state")
        if defect.generation != generation or defect.validation_profile != payload.validation_profile:
            raise ValueError("verified generation defect differs from the current generation")
        allowed_paths = (defect.bridge_ref.path,)
        baseline_digest = defect.bridge_ref.digest
        candidate_digest = defect.expected_digest
        evidence_digest = engine_digest(cast(JSONValue, defect.model_dump(mode="json")))
        coverage_epoch = generation.coverage_epoch
        execution_ref = None
    allowed_roots = tuple(sorted({path.split("/", 1)[0] for path in allowed_paths}))
    feature_input = {
        "change_id": payload.change_id,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "classification": "test",
        "fix_eligible": True,
        "kind": "failure",
        "rounds_budget": payload.budgets.healing_rounds,
        "rounds_used": int(state.get("healing_rounds_used", 0)),
        "repair_round": next_round,
        "healing_rounds_used": next_round,
        "activation": {"kind": "round", "value": str(next_round)},
        "owner_id": "assurance.healing",
        "allowed_paths": list(allowed_paths),
        "allowed_roots": list(allowed_roots),
        "baseline_digest": baseline_digest,
        "candidate_digest": candidate_digest,
        "policy_digest": payload.product_policy.sha256,
        "mapping_paths": [generation.mapping_ref.path],
        "execution_evidence_digest": evidence_digest,
        "coverage_epoch": coverage_epoch,
        "reviewed_case": reviewed.model_dump(mode="json"),
        "execution_ref": execution_ref,
        "mapping_ref": generation.mapping_ref.model_dump(mode="json"),
        "source_refs": [ref.model_dump(mode="json") for ref in generation.source_refs],
        "allowed_test_paths": list(allowed_paths),
        "validation_profile": payload.validation_profile,
        "selected_test_families": list(state.get("selected_test_families") or []),
        "generation_defect": (None if defect_cycle is None else defect_cycle.model_dump(mode="json")),
        "generation_defect_authority_ref": (
            None if defect_cycle is None else defect_cycle.attempt.authority_receipt.model_dump(mode="json")
        ),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_report(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
    feature_input = {
        "change_id": payload.change_id,
        "plan_digest": inspection.plan_digest,
        "plan_ref": inspection.plan_ref.model_dump(mode="json"),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "evidence_refs": [ref.model_dump(mode="json") for ref in inspection.assessment_refs],
        "execution_status": "passed",
        "rounds_budget": payload.budgets.healing_rounds,
        "rounds_used": int(state.get("healing_rounds_used", 0)),
        "coverage_state": inspection.coverage_state,
        "report_purpose": "normal",
    }
    return {**feature_input, "feature_input": feature_input}


def _finish_inspection(status: Literal["coverage_insufficient", "needs_human"]):
    def node(state: ProductState) -> dict[str, object]:
        inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
        tail = ExecuteTailResultV1(
            status=status,
            plan_digest=inspection.plan_digest,
            plan_ref=inspection.plan_ref,
            inspection=inspection,
        )
        return {
            "tail_result": tail.model_dump(mode="json"),
            "terminal": {"status": "stopped", "reason": status},
            "status": "failed",
        }

    return node


def _finish_reported(state: ProductState) -> dict[str, object]:
    inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
    report = ReportOutcomeV1.model_validate(state.get("report_outcome"))
    tail = reported_tail_result(inspection, report)
    return {
        "tail_result": tail.model_dump(mode="json"),
        "terminal": {"status": "completed", "reason": "done"},
        "status": "completed",
    }


def _route_report(state: ProductState) -> str:
    if state.get("attempt_failure"):
        return "blocked"
    try:
        reported_tail_result(
            InspectionOutcomeV1.model_validate(state.get("inspection_outcome")),
            ReportOutcomeV1.model_validate(state.get("report_outcome")),
        )
    except (TypeError, ValueError):
        return "blocked"
    return "reported"


def _finish_blocked(state: ProductState, *, reason: str = "execute tail is blocked") -> dict[str, object]:
    inspection: InspectionOutcomeV1 | None = None
    try:
        inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
    except (TypeError, ValueError):
        pass
    plan_digest = inspection.plan_digest if inspection is not None else str(state.get("plan_digest"))
    plan_ref = (
        inspection.plan_ref
        if inspection is not None
        else EvidenceArtifactRefV1.model_validate(state.get("plan_ref"))
    )
    tail = ExecuteTailResultV1(
        status="blocked",
        plan_digest=plan_digest,
        plan_ref=plan_ref,
        inspection=inspection,
        reason=reason,
    )
    return {
        "tail_result": tail.model_dump(mode="json"),
        "terminal": {"status": "failed", "reason": "blocked"},
        "status": "failed",
    }


def blocked(state: ProductState) -> dict[str, object]:
    return _finish_blocked(state)


def complete_parallel_generation(state: Mapping[str, object]) -> dict[str, object]:
    epoch = state.get("coverage_epoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise TypeError("coverage_epoch must be an int")
    raw_results = state.get("generation_results") or []
    if not isinstance(raw_results, list):
        raise TypeError("generation_results must be a list")
    results = [
        item for item in raw_results if isinstance(item, Mapping) and item.get("coverage_epoch", 0) == epoch
    ]
    if len(results) != 4:
        raise ValueError("generation completion requires one result per family")
    selected = state.get("selected_test_families") or []
    if not isinstance(selected, list):
        raise TypeError("selected_test_families must be a list")
    return {
        "families": {
            str(item["family"]): {"completed": True} for item in results if item.get("selected") is True
        },
        "selected_families": list(selected),
    }


def resume_product_interrupts(
    graph: CompiledStateGraph,
    config: Mapping[str, object],
    resume: object,
) -> dict[str, object]:
    snapshot = graph.get_state(cast(RunnableConfig, config))
    pending: list[tuple[str, str]] = []
    items = list(getattr(snapshot, "interrupts", ()) or ())
    for task in snapshot.tasks:
        items.extend(getattr(task, "interrupts", ()) or ())
    for item in items:
        value = getattr(item, "value", item)
        logical = value.get("interrupt_id") if isinstance(value, Mapping) else None
        interrupt_id = getattr(item, "id", None)
        if isinstance(logical, str) and isinstance(interrupt_id, str):
            pending.append((logical, interrupt_id))
    if len(pending) > 1 and not isinstance(resume, Mapping):
        raise ValueError("scalar resume rejected when more than one interrupt is pending")
    payload: object = resume
    if isinstance(resume, Mapping) and pending:
        mapped = {interrupt_id: resume[logical] for logical, interrupt_id in pending if logical in resume}
        if mapped:
            payload = mapped
    result = graph.invoke(Command(resume=payload), config=cast(RunnableConfig, config))
    if not isinstance(result, dict):
        raise TypeError("product resume must return a mapping")
    return result


def build_execute_graph(bundles: object, *, validate: bool = True) -> StateGraph[ProductState]:
    typed = cast(Any, bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    if validate:
        builder.add_node("validate", validate_public_input("execute"))
    builder.add_node("adapt-generation", cast(Any, adapt_generation))
    builder.add_node("generation", typed.generation.generation)
    builder.add_node("adapt-execution", cast(Any, adapt_execution))
    builder.add_node("execute", typed.execution.execute)
    builder.add_node("adapt-quality", cast(Any, adapt_quality_assess))
    builder.add_node("quality", typed.quality.assess)
    builder.add_node("adapt-repair-failure", cast(Any, adapt_repair_failure))
    builder.add_node("fix-proposal", typed.healing.repair_failure)
    builder.add_node("adapt-rerun", cast(Any, adapt_rerun))
    builder.add_node("run", typed.execution.rerun)
    builder.add_node("adapt-report", cast(Any, adapt_report))
    builder.add_node("report", typed.quality.report)
    builder.add_node("finish-reported", cast(Any, _finish_reported))
    builder.add_node("finish-coverage-insufficient", cast(Any, _finish_inspection("coverage_insufficient")))
    builder.add_node("finish-needs-human", cast(Any, _finish_inspection("needs_human")))
    builder.add_node("blocked", cast(Any, blocked))

    first = "validate" if validate else "adapt-generation"
    builder.add_edge(START, first)
    if validate:
        builder.add_edge("validate", "adapt-generation")
    builder.add_edge("adapt-generation", "generation")
    builder.add_conditional_edges(
        "generation",
        cast(Callable[..., Any], _route_generation),
        {"execution": "adapt-execution", "blocked": "blocked"},
    )
    builder.add_edge("adapt-execution", "execute")
    builder.add_conditional_edges(
        "execute",
        cast(Callable[..., Any], route_execute),
        {"quality": "adapt-quality", "repair": "adapt-repair-failure", "blocked": "blocked"},
    )
    builder.add_edge("adapt-quality", "quality")
    builder.add_conditional_edges(
        "quality",
        cast(Callable[..., Any], route_quality),
        {
            "quality-report": "adapt-report",
            "coverage-insufficient": "finish-coverage-insufficient",
            "fix-proposal": "adapt-repair-failure",
            "needs-human": "finish-needs-human",
            "blocked": "blocked",
        },
    )
    builder.add_edge("adapt-repair-failure", "fix-proposal")
    builder.add_conditional_edges(
        "fix-proposal",
        cast(Callable[..., Any], route_applied_repair),
        {"rerun": "adapt-rerun", "needs-human": "blocked", "blocked": "blocked"},
    )
    builder.add_edge("adapt-rerun", "run")
    builder.add_conditional_edges(
        "run",
        cast(Callable[..., Any], route_run),
        {"quality": "adapt-quality", "blocked": "blocked"},
    )
    builder.add_edge("adapt-report", "report")
    builder.add_conditional_edges(
        "report",
        cast(Callable[..., Any], _route_report),
        {"reported": "finish-reported", "blocked": "blocked"},
    )
    for node in (
        "finish-reported",
        "finish-coverage-insufficient",
        "finish-needs-human",
        "blocked",
    ):
        builder.add_edge(node, END)
    return builder


def build_execute_tail(bundles: object) -> CompiledStateGraph:
    return build_execute_graph(bundles, validate=False).compile(checkpointer=None)


def build_execute_root(
    context: GraphBuildContext,
    bundles: object,
    load_plan: CompiledStateGraph,
    execute_tail: CompiledStateGraph | None = None,
) -> CompiledStateGraph:
    tail = execute_tail or build_execute_tail(bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("execute"))
    builder.add_node("adapt-load-plan", cast(Any, adapt_load_plan))
    builder.add_node("load-plan", load_plan)
    builder.add_node("adapt-tail", cast(Any, adapt_public_execute_tail))
    builder.add_node("execute-tail", tail)
    builder.add_node("publish", publish_public_output)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt-load-plan")
    builder.add_edge("adapt-load-plan", "load-plan")
    builder.add_conditional_edges(
        "load-plan",
        cast(Any, route_prepare),
        {"prepared": "adapt-tail", "failed": "publish"},
    )
    builder.add_edge("adapt-tail", "execute-tail")
    builder.add_edge("execute-tail", "publish")
    builder.add_edge("publish", END)
    return context.compile_root(builder)


__all__ = [
    "adapt_execution",
    "adapt_execute_tail_input",
    "adapt_generation",
    "adapt_public_execute_tail",
    "adapt_quality_assess",
    "adapt_repair_failure",
    "adapt_report",
    "adapt_rerun",
    "blocked",
    "build_execute_graph",
    "build_execute_root",
    "build_execute_tail",
    "complete_parallel_generation",
    "resume_product_interrupts",
]
