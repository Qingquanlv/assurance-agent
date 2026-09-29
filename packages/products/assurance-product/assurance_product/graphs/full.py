from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.contracts.workflow import CaseFlowResultV1, EvidenceArtifactRefV1
from assurance_product.graphs.entrypoints import (
    adapt_case,
    adapt_init,
    adapt_prepare,
    publish_public_output,
    validate_public_input,
)
from assurance_product.graphs.execute import adapt_execute_tail_input
from assurance_product.graphs.loop_state import advance_coverage, can_reenter_case
from assurance_product.graphs.routes import route_init, route_prepare
from assurance_product.graphs.state import ProductState
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_product.models import BusinessBudgetsV1
from assurance_quality.contracts.surface import (
    ApiDiscoveryDocument,
    SurfaceSource,
    UiExplorationDocument,
)
from graph_engine.boot.boot import GraphBuildContext

_API_FAMILIES = frozenset({"api", "fuzz", "performance"})
_SURFACE_SOURCES = frozenset({"live", "unavailable", "unused"})


def adapt_execute_tail(state: ProductState) -> dict[str, object]:
    return adapt_execute_tail_input(state, standalone=False)


def _load_surface_document(
    root: Path,
    raw_ref: object,
    model: type[UiExplorationDocument] | type[ApiDiscoveryDocument],
) -> UiExplorationDocument | ApiDiscoveryDocument | None:
    try:
        ref = EvidenceArtifactRefV1.model_validate(raw_ref)
    except (TypeError, ValueError):
        return None
    path = root.joinpath(*ref.path.split("/"))
    if not path.is_file():
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ref.digest:
        return None
    try:
        return model.model_validate(json.loads(data))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _surface_sources_from_state(
    state: Mapping[str, object],
) -> tuple[SurfaceSource, SurfaceSource] | None:
    ui_source = state.get("ui_exploration_source")
    api_source = state.get("api_discovery_source")
    if ui_source is None or api_source is None:
        return None
    if ui_source not in _SURFACE_SOURCES or api_source not in _SURFACE_SOURCES:
        return None
    return cast(SurfaceSource, ui_source), cast(SurfaceSource, api_source)


def route_surface(
    state: Mapping[str, object],
    *,
    project_root: Path | None = None,
) -> Literal["prepare", "not-achieved"]:
    if state.get("attempt_failure"):
        return "not-achieved"
    sources = _surface_sources_from_state(state)
    if sources is not None:
        ui_source, api_source = sources
    elif project_root is not None:
        root = Path(project_root)
        ui = _load_surface_document(root, state.get("ui_exploration_ref"), UiExplorationDocument)
        api = _load_surface_document(root, state.get("api_discovery_ref"), ApiDiscoveryDocument)
        if ui is None or api is None:
            return "not-achieved"
        ui_source = ui.source
        api_source = api.source
    else:
        return "not-achieved"
    families = state.get("candidate_test_families") or ()
    if not isinstance(families, (list, tuple)):
        return "not-achieved"
    if any(name in _API_FAMILIES for name in families) and api_source != "live":
        return "not-achieved"
    if "e2e" in families and ui_source != "live":
        return "not-achieved"
    return "prepare"


def route_case_result(state: Mapping[str, object]) -> Literal["reviewed", "failed"]:
    try:
        result = CaseFlowResultV1.model_validate(
            {
                "status": state.get("status"),
                "reviewed_case": state.get("reviewed_case"),
                "receipt": state.get("case_receipt", state.get("receipt")),
            }
        )
    except (TypeError, ValueError):
        return "failed"
    if (
        result.status == "reviewed"
        and result.reviewed_case is not None
        and result.reviewed_case.change_id == state.get("change_id")
        and result.reviewed_case.coverage_epoch == state.get("coverage_epoch", 0)
    ):
        return "reviewed"
    return "failed"


def route_full_tail(
    state: Mapping[str, object],
) -> Literal["achieved", "advance-coverage", "not-achieved"]:
    try:
        result = ExecuteTailResultV1.model_validate(state.get("tail_result"))
    except (TypeError, ValueError):
        return "not-achieved"
    if result.status == "reported":
        return "achieved"
    if result.status != "coverage_insufficient" or result.inspection is None:
        return "not-achieved"
    try:
        budgets = BusinessBudgetsV1.model_validate(state.get("budgets"))
        epoch = state.get("coverage_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return "not-achieved"
        return (
            "advance-coverage"
            if result.inspection.coverage_epoch == epoch
            and can_reenter_case(coverage_epoch=epoch, budgets=budgets)
            else "not-achieved"
        )
    except (TypeError, ValueError):
        return "not-achieved"


def _terminal_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "completed"}))
    return {**published, "terminal": {"status": "completed", "reason": "achieved"}, "status": "completed"}


def _terminal_not_achieved(state: ProductState) -> dict[str, object]:
    published = publish_public_output(cast(ProductState, {**dict(state), "status": "failed"}))
    return {**published, "terminal": {"status": "failed", "reason": "not_achieved"}, "status": "failed"}


def build_full_graph(bundles: object, execute: CompiledStateGraph) -> StateGraph[ProductState]:
    typed = cast(Any, bundles)
    builder: StateGraph[ProductState] = StateGraph(ProductState)
    builder.add_node("validate", validate_public_input("full"))
    builder.add_node("surface-baseline", typed.quality.surface_baseline)
    builder.add_node("adapt-prepare", cast(Any, adapt_prepare))
    builder.add_node("prepare", typed.intake.prepare)
    builder.add_node("adapt-init", cast(Any, adapt_init))
    builder.add_node("init", typed.generation.init_runtime)
    builder.add_node("adapt-case", cast(Any, adapt_case))
    builder.add_node("case", typed.intake.case)
    builder.add_node("advance-coverage", cast(Any, advance_coverage))
    builder.add_node("adapt-execute-tail", cast(Any, adapt_execute_tail))
    builder.add_node("execute-tail", execute)
    builder.add_node("achieved", cast(Any, _terminal_achieved))
    builder.add_node("not-achieved", cast(Any, _terminal_not_achieved))
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "surface-baseline")
    builder.add_conditional_edges(
        "surface-baseline",
        cast(Callable[..., Any], route_surface),
        {"prepare": "adapt-prepare", "not-achieved": "not-achieved"},
    )
    builder.add_edge("adapt-prepare", "prepare")
    builder.add_conditional_edges(
        "prepare",
        cast(Callable[..., Any], route_prepare),
        {"prepared": "adapt-init", "failed": "not-achieved"},
    )
    builder.add_edge("adapt-init", "init")
    builder.add_conditional_edges(
        "init",
        cast(Callable[..., Any], route_init),
        {"initialized": "adapt-case", "failed": "not-achieved"},
    )
    builder.add_edge("adapt-case", "case")
    builder.add_edge("advance-coverage", "adapt-case")
    builder.add_conditional_edges(
        "case",
        cast(Callable[..., Any], route_case_result),
        {"reviewed": "adapt-execute-tail", "failed": "not-achieved"},
    )
    builder.add_edge("adapt-execute-tail", "execute-tail")
    builder.add_conditional_edges(
        "execute-tail",
        cast(Callable[..., Any], route_full_tail),
        {
            "achieved": "achieved",
            "advance-coverage": "advance-coverage",
            "not-achieved": "not-achieved",
        },
    )
    builder.add_edge("achieved", END)
    builder.add_edge("not-achieved", END)
    return builder


def build_full_root(
    context: GraphBuildContext,
    bundles: object,
    execute: CompiledStateGraph,
) -> CompiledStateGraph:
    return context.compile_root(build_full_graph(bundles, execute))


__all__ = [
    "adapt_execute_tail",
    "build_full_graph",
    "build_full_root",
    "route_case_result",
    "route_full_tail",
    "route_surface",
]
