from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import GraphBuildContext
from graph_engine.stategraph.checkpoint_bridge import omit_checkpoint_bridge_fields

from assurance_product.graphs.routes import route_prepare
from assurance_product.graphs.state import ProductState
from assurance_product.models import ProductInputV1, ProductPublicOutput, ProductReceiptRefV1
from assurance_improvement.contracts.retro import RetroSelectionSnapshot, RetroWindow
from assurance_intake.contracts import EvidenceArtifactRefV1
from graph_engine.canonical import JSONValue, canonical_digest

ProductStatus = Literal["completed", "failed"]
_FEATURE_STATUS_TO_PRODUCT: Mapping[str, ProductStatus] = {
    "completed": "completed",
    "passed": "completed",
    "reviewed": "completed",
    "done": "completed",
    "failed": "failed",
    "rejected": "failed",
    "exhausted": "failed",
    "rework": "failed",
    "superseded": "failed",
}

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
    "retro_window",
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
    artifact_refs = [item.model_dump(mode="json") for item in payload.artifacts]
    rework_context = state.get("case_rework_context")
    coverage_epoch = int(state.get("coverage_epoch", 0)) if rework_context is not None else 0
    preparation_refs = [
        item
        for item in artifact_refs
        if "/cases/" not in str(item["path"]) and "/review/" not in str(item["path"])
    ]
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
        "artifacts": artifact_refs,
        "coverage_epoch": coverage_epoch,
        "healing_rounds_used": 0,
        "preparation_refs": preparation_refs,
        "source_artifacts": artifact_refs,
    }
    if rework_context is not None:
        feature_input["case_rework_context"] = rework_context
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


_IMPROVEMENT_TASK_KEYS = (
    "projection",
    "eval_run_id",
    "outcome",
    "report_sha256",
    "staged_sha256",
    "baseline_sha256",
    "target_digest",
)


def adapt_improvement(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    extras = {key: state.get(key) for key in _IMPROVEMENT_TASK_KEYS if key in state}
    feature_input = {
        "change_id": payload.change_id,
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "artifact_paths": list(payload.allowed_artifact_paths),
        **extras,
    }
    return {**feature_input, **extras, "feature_input": feature_input}


def _retro_source_refs(state: ProductState, payload: ProductInputV1) -> tuple[EvidenceArtifactRefV1, ...]:
    candidates: list[object] = [*payload.artifacts]
    for key in ("artifacts", "source_artifacts", "report_refs", "evidence_refs"):
        raw = state.get(key)
        if isinstance(raw, (list, tuple)):
            candidates.extend(raw)
    inspection = state.get("inspection_outcome")
    if isinstance(inspection, Mapping):
        candidates.extend(inspection.get("assessment_refs") or ())
        mapping = inspection.get("mapping_ref")
        if mapping is not None:
            candidates.append(mapping)
    elif inspection is not None:
        candidates.extend(getattr(inspection, "assessment_refs", ()))
        candidates.append(getattr(inspection, "mapping_ref", None))
    report_outcome = state.get("report_outcome")
    if isinstance(report_outcome, Mapping):
        candidates.extend(report_outcome.get("report_refs") or ())
    elif report_outcome is not None:
        candidates.extend(getattr(report_outcome, "report_refs", ()))
    by_path: dict[str, EvidenceArtifactRefV1] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            ref = (
                candidate
                if isinstance(candidate, EvidenceArtifactRefV1)
                else EvidenceArtifactRefV1.model_validate(candidate)
            )
        except (TypeError, ValueError):
            continue
        existing = by_path.get(ref.path)
        if existing is not None and existing.digest != ref.digest:
            raise ValueError(f"conflicting Retro source digest for {ref.path}")
        by_path[ref.path] = ref
    return tuple(by_path[path] for path in sorted(by_path))


def adapt_retro(state: ProductState) -> dict[str, object]:
    payload = _input_from_state(state)
    window = payload.retro_window or RetroWindow(
        selection=RetroSelectionSnapshot(
            mode="change_ids",
            requested_change_ids=(payload.change_id,),
        ),
        change_ids=(payload.change_id,),
    )
    refs = _retro_source_refs(state, payload)
    report_outcome = state.get("report_outcome")
    report_receipt = (
        report_outcome.get("report_receipt")
        if isinstance(report_outcome, Mapping)
        else getattr(report_outcome, "report_receipt", None)
    )
    receipt_digest = (
        report_receipt.get("receipt_digest")
        if isinstance(report_receipt, Mapping)
        else getattr(report_receipt, "receipt_digest", None)
    )
    identity = {
        "window": window.model_dump(mode="json"),
        "source_refs": [item.model_dump(mode="json") for item in refs],
        "report_receipt_digest": receipt_digest,
    }
    retro_id = f"retro-{canonical_digest(cast(JSONValue, identity))}"
    feature_input = {
        "change_id": payload.change_id,
        "retro_id": retro_id,
        "window": window.model_dump(mode="json"),
        "source_refs": [item.model_dump(mode="json") for item in refs],
        "capability_leafs": list(payload.capability_leafs),
        "allowed_artifact_paths": list(payload.allowed_artifact_paths),
        "artifact_paths": list(payload.allowed_artifact_paths),
    }
    return {**feature_input, "feature_input": feature_input}


def adapt_feature_status(status: object) -> ProductStatus:
    if status is None or status == "":
        return "completed"
    if not isinstance(status, str):
        raise TypeError("feature status must be a string")
    try:
        return _FEATURE_STATUS_TO_PRODUCT[status]
    except KeyError:
        raise ValueError(f"unsupported feature terminal status: {status!r}") from None


def _receipt_items(state: Mapping[str, object]) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    for key in ("receipts", "receipt_refs"):
        raw = state.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            receipt_id = item.get("receipt_id")
            digest = item.get("receipt_digest")
            if isinstance(receipt_id, str) and isinstance(digest, str):
                receipts.append({"receipt_id": receipt_id, "receipt_digest": digest})
    return receipts


def publish_public_output(state: ProductState) -> dict[str, object]:
    cleaned = _as_mapping(state)
    output = ProductPublicOutput(
        change_id=str(cleaned["change_id"]),
        status=adapt_feature_status(cleaned.get("status")),
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


def build_intake_root(
    context: GraphBuildContext,
    prepare: CompiledStateGraph,
    case: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("intake"))
    builder.add_node("adapt", cast(Any, adapt_intake))
    builder.add_node("prepare", prepare)
    builder.add_node("case", case)
    builder.add_node("publish", publish_public_output)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "adapt")
    builder.add_edge("adapt", "prepare")
    builder.add_conditional_edges(
        "prepare",
        cast(Any, route_prepare),
        {"prepared": "case", "failed": "publish"},
    )
    builder.add_edge("case", "publish")
    builder.add_edge("publish", END)
    return context.compile_root(builder)


def build_case_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="case", adapt=adapt_intake)


def build_archive_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="archive", adapt=adapt_improvement)


def build_retro_root(context: GraphBuildContext, child: CompiledStateGraph) -> CompiledStateGraph:
    return compile_thin_root(context, child, entrypoint="retro", adapt=adapt_retro)


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
    "adapt_feature_status",
    "adapt_improvement",
    "adapt_retro",
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
