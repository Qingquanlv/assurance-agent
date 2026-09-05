from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from assurance_product.graphs.entrypoints import (
    _input_from_state,
    publish_public_output,
    validate_public_input,
)
from assurance_product.graphs.routes import (
    route_coverage_decision,
    route_coverage_repair,
    route_execute,
    route_issue_analysis,
    route_quality,
    route_quality_recheck,
    route_run,
)
from assurance_product.graphs.state import (
    COVERAGE_DECISION_ACTIONS,
    AssessmentTrigger,
    ProductState,
    consume_coverage_needed_trigger,
    consume_failed_join_trigger,
    empty_coverage_needed_inbox,
    empty_failed_join_inbox,
    make_assessment_trigger,
    make_coverage_needed_arrival,
    make_failed_join_arrival,
    offer_coverage_needed_arrival,
    offer_failed_join_arrival,
)
from assurance_product.graphs.tail_contracts import ExecuteTailInputV1
from graph_engine.boot.boot import GraphBuildContext
from graph_engine.plugin_api import FrozenModel

_COVERAGE_INTERRUPT_ID = "product-coverage-decision"


class CoverageDecisionV1(FrozenModel):
    action: Literal["approve", "reject"]


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
            "reviewed_case": reviewed_case,
            "source_artifacts": source_artifacts,
            "selected_test_families": payload.selected_test_families,
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
    update["healing_rounds_used"] = 0
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
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "reviewed_case": state.get("reviewed_case"),
        "source_artifacts": source_artifacts,
        "selected_test_families": list(payload.selected_test_families),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "artifacts": [item.model_dump(mode="json") for item in payload.artifacts],
        "decision": payload.decision,
    }
    return {
        **feature_input,
        "feature_input": feature_input,
        "failed_join_inbox": state.get("failed_join_inbox") or empty_failed_join_inbox(),
        "coverage_needed_inbox": state.get("coverage_needed_inbox") or empty_coverage_needed_inbox(),
    }


def adapt_execution(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "selected_test_families": list(payload.selected_test_families),
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "repair_round": 0,
        "rounds_budget": payload.budgets.healing_rounds,
        "rounds_used": 0,
    }
    generation_result = state.get("generation_result")
    if generation_result is not None:
        feature_input["generation_result"] = generation_result
    return {**feature_input, "feature_input": feature_input}


def adapt_rerun(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "selected_test_families": list(payload.selected_test_families),
        "coverage_epoch": int(state.get("coverage_epoch", 0)),
        "repair_round": int(state.get("healing_rounds_used") or state.get("rounds_used") or 0),
        "rounds_budget": int(state.get("rounds_budget") or payload.budgets.healing_rounds),
        "rounds_used": int(state.get("rounds_used") or 0),
    }
    generation_result = state.get("generation_result")
    if generation_result is not None:
        feature_input["generation_result"] = generation_result
    return {**feature_input, "feature_input": feature_input}


def adapt_quality_assess(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    execution = state.get("execution_result")
    generation = state.get("generation_result")
    reviewed = state.get("reviewed_case")
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "rounds_budget": payload.budgets.coverage_rounds,
        "rounds_used": 0,
        "evidence_refs": [],
        "execution_status": "passed",
    }
    cycle_parts = (reviewed, generation, execution)
    if any(item is not None for item in cycle_parts):
        if any(item is None for item in cycle_parts):
            raise ValueError("quality assessment cycle evidence must be complete")
        execution_payload = (
            execution.model_dump(mode="json") if isinstance(execution, FrozenModel) else execution
        )
        if not isinstance(execution_payload, Mapping):
            raise TypeError("execution_result must be a mapping")
        batch_id = execution_payload.get("batch_id")
        if not isinstance(batch_id, str):
            raise TypeError("execution_result batch_id must be a string")
        try:
            execution_at = datetime.strptime(batch_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError as error:
            raise ValueError("execution batch_id must carry the locked UTC execution time") from error
        feature_input.update(
            {
                "batch_id": batch_id,
                "coverage_epoch": int(state.get("coverage_epoch", 0)),
                "reviewed_case": reviewed,
                "generation_result": generation,
                "execution_result": execution,
                "policy_resource_id": payload.product_policy.resource_id,
                "policy_sha256": payload.product_policy.sha256,
                "execution_at": execution_at.isoformat(),
                "healing_ref": state.get("healing_ref"),
                "issue_ref": state.get("issue_ref"),
            }
        )
    public = {
        "schema_version": payload.schema_version,
        "change_id": payload.change_id,
        "requirement": payload.requirement,
        "run_mode": payload.run_mode,
        "selected_test_families": list(payload.selected_test_families),
        "case_delta_paths": list(payload.case_delta_paths),
        "capability_leafs": list(payload.capability_leafs),
        "capability_catalog": payload.capability_catalog.model_dump(mode="json"),
        "product_policy": payload.product_policy.model_dump(mode="json"),
        "data_knowledge": payload.data_knowledge.model_dump(mode="json"),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "artifacts": [item.model_dump(mode="json") for item in payload.artifacts],
        "decision": payload.decision,
    }
    return {**public, **feature_input, "feature_input": feature_input}


def adapt_quality_recheck(state: ProductState) -> dict[str, object]:
    return adapt_quality_assess(state)


def adapt_issue_analyze(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "evidence_refs": [],
        "execution_status": "failed",
        "rounds_budget": int(state.get("rounds_budget") or payload.budgets.healing_rounds),
        "rounds_used": int(state.get("rounds_used") or 0),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_repair_failure(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "classification": state.get("classification"),
        "fix_eligible": state.get("fix_eligible"),
        "kind": "failure",
        "rounds_budget": int(state.get("rounds_budget") or payload.budgets.healing_rounds),
        "rounds_used": int(state.get("rounds_used") or 0),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_repair_coverage(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "classification": "repair_required",
        "fix_eligible": True,
        "kind": "coverage",
        "rounds_budget": int(state.get("rounds_budget") or payload.budgets.coverage_rounds),
        "rounds_used": int(state.get("rounds_used") or 0),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_report(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    trigger = state.get("assessment_trigger")
    coverage = state.get("coverage_state")
    if isinstance(trigger, Mapping):
        coverage = trigger.get("coverage_state", coverage)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "evidence_refs": [],
        "execution_status": "passed",
        "rounds_budget": payload.budgets.healing_rounds,
        "rounds_used": 0,
        "coverage_state": coverage,
    }
    return {**feature_input, "feature_input": feature_input}


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _evidence_refs(state: Mapping[str, object]) -> list[dict[str, str]]:
    raw = state.get("evidence_refs")
    if not isinstance(raw, list):
        return []
    return [
        {str(key): str(value) for key, value in item.items()} for item in raw if isinstance(item, Mapping)
    ]


def _next_sequence(inbox: Mapping[str, object]) -> int:
    arrivals = inbox.get("arrivals") or []
    if not isinstance(arrivals, list) or not arrivals:
        return 1
    return (
        max(_as_int(item["sequence"], name="sequence") for item in arrivals if isinstance(item, Mapping)) + 1
    )


def _offer_failed(state: Mapping[str, object], predecessor: str) -> dict[str, object]:
    inbox = state.get("failed_join_inbox") or empty_failed_join_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_failed_join_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_failed_join_trigger(inbox)
    used = _as_int(state.get("rounds_used", 0), name="rounds_used")
    arrival = make_failed_join_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1) if used else 0,
        sequence=_next_sequence(inbox),
        value={
            "rounds_used": used,
            "rounds_budget": _as_int(state.get("rounds_budget", 0), name="rounds_budget"),
        },
    )
    return {"failed_join_inbox": offer_failed_join_arrival(inbox, arrival)}


def offer_failed_execute(state: ProductState) -> dict[str, object]:
    return _offer_failed(state, "execute")


def offer_failed_run(state: ProductState) -> dict[str, object]:
    return _offer_failed(state, "run")


def _offer_coverage(state: Mapping[str, object], predecessor: str) -> dict[str, object]:
    inbox = state.get("coverage_needed_inbox") or empty_coverage_needed_inbox()
    if not isinstance(inbox, Mapping):
        inbox = empty_coverage_needed_inbox()
    if inbox.get("current_trigger"):
        inbox = consume_coverage_needed_trigger(inbox)
    used = _as_int(state.get("rounds_used", 0), name="rounds_used")
    arrival = make_coverage_needed_arrival(
        predecessor=predecessor,
        business_epoch=max(0, used - 1) if used else 0,
        sequence=_next_sequence(inbox),
        value={
            "rounds_used": used,
            "rounds_budget": _as_int(state.get("rounds_budget", 0), name="rounds_budget"),
        },
    )
    return {"coverage_needed_inbox": offer_coverage_needed_arrival(inbox, arrival)}


def offer_coverage_quality(state: ProductState) -> dict[str, object]:
    return _offer_coverage(state, "quality")


def offer_coverage_recheck(state: ProductState) -> dict[str, object]:
    return _offer_coverage(state, "quality-recheck")


def apply_failed_join_trigger(state: Mapping[str, object]) -> dict[str, object]:
    inbox = state.get("failed_join_inbox") or empty_failed_join_inbox()
    trigger = inbox.get("current_trigger") if isinstance(inbox, Mapping) else None
    if not isinstance(trigger, Mapping):
        raise ValueError("current_trigger.value")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current_trigger.value")
    return {
        "current_trigger": dict(trigger),
        "failed_join_inbox": inbox,
        "rounds_used": _as_int(value["rounds_used"], name="rounds_used"),
        "rounds_budget": _as_int(value["rounds_budget"], name="rounds_budget"),
    }


def apply_coverage_needed_trigger(state: Mapping[str, object]) -> dict[str, object]:
    inbox = state.get("coverage_needed_inbox") or empty_coverage_needed_inbox()
    trigger = inbox.get("current_trigger") if isinstance(inbox, Mapping) else None
    if not isinstance(trigger, Mapping):
        raise ValueError("current_trigger.value")
    value = trigger.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("current_trigger.value")
    return {
        "current_trigger": dict(trigger),
        "coverage_needed_inbox": inbox,
        "rounds_used": _as_int(value["rounds_used"], name="rounds_used"),
        "rounds_budget": _as_int(value["rounds_budget"], name="rounds_budget"),
    }


def failed_join(state: ProductState) -> dict[str, object]:
    return apply_failed_join_trigger(state)


def coverage_needed_join(state: ProductState) -> dict[str, object]:
    return apply_coverage_needed_trigger(state)


def apply_assessment_trigger(
    state: Mapping[str, object],
    trigger: AssessmentTrigger | None = None,
) -> dict[str, object]:
    if state.get("assess_satisfied") and state.get("assess_unsatisfied"):
        raise ValueError("assessment cannot claim both outcomes")
    incoming = trigger
    if incoming is None:
        incoming = make_assessment_trigger(
            source=str(state.get("assessment_source") or "quality"),
            coverage_state=str(state.get("coverage_state") or "satisfied"),
            rounds={
                "rounds_used": _as_int(state.get("rounds_used") or 0, name="rounds_used"),
                "rounds_budget": _as_int(state.get("rounds_budget") or 0, name="rounds_budget"),
            },
            evidence=_evidence_refs(state),
        )
    existing = state.get("assessment_trigger")
    if not isinstance(existing, Mapping) or not existing:
        return {"assessment_trigger": incoming}
    if incoming == existing:
        return {"assessment_trigger": incoming}
    raise ValueError("late assessment reactivation")


def _set_assessment(source: str):
    def node(state: ProductState) -> dict[str, object]:
        trigger = make_assessment_trigger(
            source=source,
            coverage_state=str(state.get("coverage_state") or "satisfied"),
            rounds={
                "rounds_used": _as_int(state.get("rounds_used") or 0, name="rounds_used"),
                "rounds_budget": _as_int(state.get("rounds_budget") or 0, name="rounds_budget"),
            },
            evidence=_evidence_refs(state),
        )
        return apply_assessment_trigger(state, trigger)

    return node


def _coerce_coverage_decision(raw: object) -> CoverageDecisionV1:
    if isinstance(raw, str):
        return CoverageDecisionV1(action=raw)  # type: ignore[arg-type]
    if isinstance(raw, Mapping):
        action = raw.get("action", raw.get("decision"))
        return CoverageDecisionV1.model_validate({"action": action})
    return CoverageDecisionV1.model_validate(raw)


def coverage_human_interrupt(state: ProductState) -> dict[str, object]:
    del state
    raw = interrupt(
        {
            "reason": "coverage_needs_human",
            "actions": list(COVERAGE_DECISION_ACTIONS),
            "interrupt_id": _COVERAGE_INTERRUPT_ID,
            "ordinal": 0,
        }
    )
    decision = _coerce_coverage_decision(raw)
    update: dict[str, object] = {
        "coverage_decision": decision.action,
        "human_action": decision.action,
    }
    if decision.action == "approve":
        update["coverage_state"] = "satisfied"
    return update


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
        mapped: dict[str, object] = {}
        for logical, interrupt_id in pending:
            if logical in resume:
                mapped[interrupt_id] = resume[logical]
        if mapped:
            payload = mapped
    result = graph.invoke(Command(resume=payload), config=cast(RunnableConfig, config))
    if not isinstance(result, dict):
        raise TypeError("product resume must return a mapping")
    return result


def complete_parallel_generation(state: Mapping[str, object]) -> dict[str, object]:
    epoch = _as_int(state.get("coverage_epoch", 0), name="coverage_epoch")
    raw_results = state.get("generation_results") or []
    results = (
        [
            item
            for item in raw_results
            if isinstance(item, Mapping) and int(item.get("coverage_epoch", 0)) == epoch
        ]
        if isinstance(raw_results, list)
        else []
    )
    if not isinstance(results, list) or len(results) != 4:
        raise ValueError("generation completion requires one result per family")
    selected = state.get("selected_test_families") or []
    if not isinstance(selected, list):
        raise TypeError("selected_test_families must be a list")
    families = {
        str(item["family"]): {"completed": True}
        for item in results
        if isinstance(item, Mapping) and item.get("selected") is True
    }
    return {
        "families": families,
        "selected_families": list(selected),
    }


def _terminal_done(state: ProductState) -> dict[str, object]:
    del state
    return {"terminal": "done", "status": "completed"}


def _terminal_not_achieved(state: ProductState) -> dict[str, object]:
    del state
    return {"terminal": "not-achieved", "status": "failed"}


def _adapt_report_unsatisfied_repair(state: ProductState) -> dict[str, object]:
    adapted = adapt_report(state)
    adapted["coverage_state"] = "repair_required"
    raw_feature = adapted.get("feature_input")
    feature: dict[str, object] = dict(raw_feature) if isinstance(raw_feature, Mapping) else {}
    feature["coverage_state"] = "repair_required"
    adapted["feature_input"] = feature
    return adapted


def _adapt_report_issue(state: ProductState) -> dict[str, object]:
    adapted = adapt_report(state)
    adapted["coverage_state"] = "inconclusive"
    raw_feature = adapted.get("feature_input")
    feature: dict[str, object] = dict(raw_feature) if isinstance(raw_feature, Mapping) else {}
    feature["coverage_state"] = "inconclusive"
    adapted["feature_input"] = feature
    return adapted


def build_execute_graph(bundles: object, *, validate: bool = True) -> StateGraph[ProductState]:
    typed = cast(Any, bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    if validate:
        builder.add_node("validate", validate_public_input("execute"))
    builder.add_node("adapt-generation", cast(Any, adapt_generation))
    builder.add_node("generation", typed.generation.generation)
    builder.add_node("adapt-execution", cast(Any, adapt_execution))
    builder.add_node("execute", typed.execution.execute)
    builder.add_node("offer-failed-execute", cast(Any, offer_failed_execute))
    builder.add_node("offer-failed-run", cast(Any, offer_failed_run))
    builder.add_node("failed-join", cast(Any, failed_join))
    builder.add_node("adapt-issue-analysis", cast(Any, adapt_issue_analyze))
    builder.add_node("issue-analysis", typed.quality.issue_analyze)
    builder.add_node("adapt-repair-failure", cast(Any, adapt_repair_failure))
    builder.add_node("fix-proposal", typed.healing.repair_failure)
    builder.add_node("adapt-rerun", cast(Any, adapt_rerun))
    builder.add_node("run", typed.execution.rerun)
    builder.add_node("adapt-quality", cast(Any, adapt_quality_assess))
    builder.add_node("quality", typed.quality.assess)
    builder.add_node("offer-coverage-quality", cast(Any, offer_coverage_quality))
    builder.add_node("offer-coverage-recheck", cast(Any, offer_coverage_recheck))
    builder.add_node("coverage-needed", cast(Any, coverage_needed_join))
    builder.add_node("adapt-coverage-repair", cast(Any, adapt_repair_coverage))
    builder.add_node("coverage-repair", typed.healing.repair_coverage)
    builder.add_node("adapt-quality-recheck", cast(Any, adapt_quality_recheck))
    builder.add_node("quality-recheck", typed.quality.assess)
    builder.add_node("assess-satisfied-quality", cast(Any, _set_assessment("quality")))
    builder.add_node("assess-unsatisfied-quality", cast(Any, _set_assessment("quality")))
    builder.add_node("assess-satisfied-recheck", cast(Any, _set_assessment("quality-recheck")))
    builder.add_node("assess-unsatisfied-recheck", cast(Any, _set_assessment("quality-recheck")))
    builder.add_node("coverage-human-quality", cast(Any, coverage_human_interrupt))
    builder.add_node("coverage-human-recheck", cast(Any, coverage_human_interrupt))
    builder.add_node("adapt-report", cast(Any, adapt_report))
    builder.add_node("report", typed.quality.report)
    builder.add_node("adapt-report-unsatisfied", cast(Any, adapt_report))
    builder.add_node("report-unsatisfied", typed.quality.report)
    builder.add_node("adapt-report-unsatisfied-repair", cast(Any, _adapt_report_unsatisfied_repair))
    builder.add_node("report-unsatisfied-repair", typed.quality.report)
    builder.add_node("adapt-report-issue", cast(Any, _adapt_report_issue))
    builder.add_node("report-issue", typed.quality.report)
    builder.add_node("done", cast(Any, _terminal_done))
    builder.add_node("not-achieved", cast(Any, _terminal_not_achieved))

    if validate:
        builder.add_edge(START, "validate")
        builder.add_edge("validate", "adapt-generation")
    else:
        builder.add_edge(START, "adapt-generation")
    builder.add_edge("adapt-generation", "generation")
    builder.add_edge("generation", "adapt-execution")
    builder.add_edge("adapt-execution", "execute")
    builder.add_conditional_edges(
        "execute",
        cast(Callable[..., Any], route_execute),
        {"quality": "adapt-quality", "failed-join": "offer-failed-execute", "not-achieved": "not-achieved"},
    )
    builder.add_edge("offer-failed-execute", "failed-join")
    builder.add_edge("offer-failed-run", "failed-join")
    builder.add_edge("failed-join", "adapt-issue-analysis")
    builder.add_edge("adapt-issue-analysis", "issue-analysis")
    builder.add_conditional_edges(
        "issue-analysis",
        cast(Callable[..., Any], route_issue_analysis),
        {
            "fix-proposal": "adapt-repair-failure",
            "report-issue": "adapt-report-issue",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("adapt-repair-failure", "fix-proposal")
    builder.add_edge("fix-proposal", "adapt-rerun")
    builder.add_edge("adapt-rerun", "run")
    builder.add_conditional_edges(
        "run",
        cast(Callable[..., Any], route_run),
        {"quality": "adapt-quality", "failed-join": "offer-failed-run", "not-achieved": "not-achieved"},
    )
    builder.add_edge("adapt-quality", "quality")
    builder.add_conditional_edges(
        "quality",
        cast(Callable[..., Any], route_quality),
        {
            "coverage-needed": "offer-coverage-quality",
            "assess-satisfied": "assess-satisfied-quality",
            "assess-unsatisfied": "assess-unsatisfied-quality",
            "coverage-human": "coverage-human-quality",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("offer-coverage-quality", "coverage-needed")
    builder.add_edge("offer-coverage-recheck", "coverage-needed")
    builder.add_edge("coverage-needed", "adapt-coverage-repair")
    builder.add_edge("adapt-coverage-repair", "coverage-repair")
    builder.add_conditional_edges(
        "coverage-repair",
        cast(Callable[..., Any], route_coverage_repair),
        {
            "quality-recheck": "adapt-quality-recheck",
            "report-unsatisfied-repair": "adapt-report-unsatisfied-repair",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("adapt-quality-recheck", "quality-recheck")
    builder.add_conditional_edges(
        "quality-recheck",
        cast(Callable[..., Any], route_quality_recheck),
        {
            "coverage-needed": "offer-coverage-recheck",
            "assess-satisfied": "assess-satisfied-recheck",
            "assess-unsatisfied": "assess-unsatisfied-recheck",
            "coverage-human": "coverage-human-recheck",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("assess-satisfied-quality", "adapt-report")
    builder.add_edge("assess-satisfied-recheck", "adapt-report")
    builder.add_edge("adapt-report", "report")
    builder.add_edge("report", "done")
    builder.add_edge("assess-unsatisfied-quality", "adapt-report-unsatisfied")
    builder.add_edge("assess-unsatisfied-recheck", "adapt-report-unsatisfied")
    builder.add_edge("adapt-report-unsatisfied", "report-unsatisfied")
    builder.add_edge("report-unsatisfied", "not-achieved")
    builder.add_edge("adapt-report-unsatisfied-repair", "report-unsatisfied-repair")
    builder.add_edge("report-unsatisfied-repair", "not-achieved")
    builder.add_edge("adapt-report-issue", "report-issue")
    builder.add_edge("report-issue", "done")
    builder.add_conditional_edges(
        "coverage-human-quality",
        cast(Callable[..., Any], route_coverage_decision),
        {"assess-satisfied": "assess-satisfied-quality", "not-achieved": "not-achieved"},
    )
    builder.add_conditional_edges(
        "coverage-human-recheck",
        cast(Callable[..., Any], route_coverage_decision),
        {"assess-satisfied": "assess-satisfied-recheck", "not-achieved": "not-achieved"},
    )
    builder.add_edge("done", END)
    builder.add_edge("not-achieved", END)
    return builder


def build_execute_tail(bundles: object) -> CompiledStateGraph:
    return build_execute_graph(bundles, validate=False).compile(checkpointer=None)


def build_execute_root(
    context: GraphBuildContext,
    bundles: object,
    execute_tail: CompiledStateGraph | None = None,
) -> CompiledStateGraph:
    tail = execute_tail or build_execute_tail(bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("execute"))
    builder.add_node("adapt-tail", cast(Any, adapt_public_execute_tail))
    builder.add_node("execute-tail", tail)
    builder.add_node("publish", publish_public_output)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt-tail")
    builder.add_edge("adapt-tail", "execute-tail")
    builder.add_edge("execute-tail", "publish")
    builder.add_edge("publish", END)
    return context.compile_root(builder)


__all__ = [
    "adapt_execution",
    "adapt_execute_tail_input",
    "adapt_generation",
    "adapt_issue_analyze",
    "adapt_quality_assess",
    "adapt_quality_recheck",
    "adapt_repair_coverage",
    "adapt_repair_failure",
    "adapt_report",
    "adapt_rerun",
    "apply_assessment_trigger",
    "apply_coverage_needed_trigger",
    "apply_failed_join_trigger",
    "build_execute_graph",
    "build_execute_root",
    "build_execute_tail",
    "complete_parallel_generation",
    "coverage_human_interrupt",
    "coverage_needed_join",
    "failed_join",
    "offer_coverage_quality",
    "offer_coverage_recheck",
    "offer_failed_execute",
    "offer_failed_run",
    "resume_product_interrupts",
    "route_coverage_decision",
]
