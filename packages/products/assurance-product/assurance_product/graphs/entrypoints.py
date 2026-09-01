from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import GraphBuildContext
from graph_engine.stategraph.checkpoint_bridge import omit_checkpoint_bridge_fields

from assurance_product.graphs.state import ProductState
from assurance_product.models import ProductInputV1, ProductPublicOutput, ProductReceiptRefV1

_INPUT_KEYS = (
    "schema_version",
    "change_id",
    "requirement",
    "run_mode",
    "selected_test_families",
    "case_delta_paths",
    "capability_leafs",
    "capability_catalog",
    "product_policy",
    "data_knowledge",
    "allowed_artifact_paths",
    "budgets",
    "artifacts",
    "decision",
)


def _as_mapping(state: object) -> Mapping[str, object]:
    if not isinstance(state, Mapping):
        raise TypeError("product state must be a mapping")
    return omit_checkpoint_bridge_fields(state)


def _public_payload(state: object) -> dict[str, object]:
    cleaned = _as_mapping(state)
    return {key: cleaned[key] for key in _INPUT_KEYS if key in cleaned}


def _input_from_state(state: object) -> ProductInputV1:
    return ProductInputV1.model_validate(_public_payload(state))


def validate_public_input(entrypoint: str):
    def node(state: ProductState) -> dict[str, object]:
        _input_from_state(state).validate_for_entrypoint(entrypoint)
        return {}

    return node


def adapt_intake(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "requirement": payload.requirement,
        "selected_test_families": list(payload.selected_test_families),
        "case_delta_paths": list(payload.case_delta_paths),
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "rounds_budget": payload.budgets.review_rounds,
        "rounds_used": 0,
        "decision": payload.decision,
        "artifacts": [item.model_dump(mode="json") for item in payload.artifacts],
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_quality(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "budgets": payload.budgets.model_dump(mode="json"),
        "rounds_budget": payload.budgets.coverage_rounds,
        "rounds_used": 0,
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_improvement(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "artifact_paths": list(payload.allowed_artifact_paths),
    }
    return {**feature_input, "feature_input": feature_input}


def _receipt_items(state: Mapping[str, object]) -> list[dict[str, object]]:
    for key in ("receipts", "receipt_refs"):
        raw = state.get(key)
        if isinstance(raw, list) and raw:
            return [dict(item) for item in raw if isinstance(item, Mapping)]
    for key in ("artifacts", "evidence_refs"):
        raw = state.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        receipts: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            receipt_id = item.get("receipt_id") or item.get("path")
            digest = item.get("receipt_digest") or item.get("digest")
            if isinstance(receipt_id, str) and isinstance(digest, str):
                receipts.append({"receipt_id": receipt_id, "receipt_digest": digest})
        if receipts:
            return receipts
    return []


def publish_public_output(state: ProductState) -> dict[str, object]:
    cleaned = _as_mapping(state)
    output = ProductPublicOutput(
        change_id=str(cleaned["change_id"]),
        status=cast(Any, cleaned.get("status") or "completed"),
        receipts=tuple(ProductReceiptRefV1.model_validate(item) for item in _receipt_items(cleaned)),
    )
    dumped = output.model_dump(mode="json")
    return {"output": dumped, "status": dumped["status"], "receipts": list(dumped["receipts"])}


def compile_thin_root(
    context: GraphBuildContext,
    child: CompiledStateGraph,
    *,
    entrypoint: str,
    adapt: object,
) -> CompiledStateGraph:
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input(entrypoint))
    builder.add_node("adapt", cast(Any, adapt))
    builder.add_node("feature", child)
    builder.add_node("publish", publish_public_output)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt")
    builder.add_edge("adapt", "feature")
    builder.add_edge("feature", "publish")
    builder.add_edge("publish", END)
    return context.compile_root(builder)


def build_intake_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="intake", adapt=adapt_intake)


def build_case_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="case", adapt=adapt_intake)


def build_archive_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="archive", adapt=adapt_improvement)


def build_retro_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="retro", adapt=adapt_improvement)


def build_issue_review_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="issue-review", adapt=adapt_quality)


def build_issue_analyze_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="issue-analyze", adapt=adapt_quality)


def build_issue_reconcile_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="issue-reconcile", adapt=adapt_quality)


def build_improvement_review_root(
    context: GraphBuildContext, child: CompiledStateGraph
) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="improvement-review", adapt=adapt_improvement)


def build_improvement_evaluate_root(
    context: GraphBuildContext, child: CompiledStateGraph
) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="improvement-evaluate", adapt=adapt_improvement)


def build_improvement_export_root(
    context: GraphBuildContext, child: CompiledStateGraph
) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="improvement-export", adapt=adapt_improvement)


def build_improvement_apply_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="improvement-apply", adapt=adapt_improvement)


def build_improvement_rollback_root(
    context: GraphBuildContext, child: CompiledStateGraph
) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="improvement-rollback", adapt=adapt_improvement)


__all__ = [
    "adapt_improvement",
    "adapt_intake",
    "adapt_quality",
    "build_archive_root",
    "build_case_root",
    "build_improvement_apply_root",
    "build_improvement_evaluate_root",
    "build_improvement_export_root",
    "build_improvement_review_root",
    "build_improvement_rollback_root",
    "build_intake_root",
    "build_issue_analyze_root",
    "build_issue_reconcile_root",
    "build_issue_review_root",
    "build_retro_root",
    "compile_thin_root",
    "publish_public_output",
    "validate_public_input",
]
