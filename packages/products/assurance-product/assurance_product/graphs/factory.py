from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast, get_type_hints

from langchain_core.runnables.config import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from assurance_execution.feature import ExecutionGraphs
from assurance_generation.feature import GenerationGraphs
from assurance_healing.feature import HealingGraphs
from assurance_improvement.feature import ImprovementGraphs
from assurance_intake.feature import IntakeGraphs
from assurance_product.graphs.entrypoints import (
    build_archive_root,
    build_improvement_apply_root,
    build_improvement_evaluate_root,
    build_improvement_export_root,
    build_improvement_review_root,
    build_improvement_rollback_root,
    build_init_root,
    build_intake_root,
    build_issue_analyze_root,
    build_issue_reconcile_root,
    build_issue_review_root,
    build_retro_root,
)
from assurance_product.feature_set import CAPABILITY_OWNERS
from assurance_product.features import FEATURES
from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS, ENTRYPOINT_RECURSION_LIMITS
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from assurance_quality.feature import QualityGraphs
from graph_engine.boot.boot import GraphBuildContext
from graph_engine.boot.graph_revision import EntrypointGraphContract
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1

_FORBIDDEN_PRODUCT_TAIL_NODES = frozenset(
    {"coverage-repair", "coverage-repair-brief", "quality-recheck", "coverage-needed"}
)


@dataclass(frozen=True, slots=True)
class ProductFeatureBundles:
    intake: IntakeGraphs
    generation: GenerationGraphs
    execution: ExecutionGraphs
    quality: QualityGraphs
    healing: HealingGraphs
    improvement: ImprovementGraphs


_DECLARED_BUNDLE_TYPES = get_type_hints(ProductFeatureBundles)
_INSTALLED_BUNDLE_TYPES = {
    feature.owner_id.removeprefix("assurance."): feature.bundle_type for feature in FEATURES
}
if _DECLARED_BUNDLE_TYPES != _INSTALLED_BUNDLE_TYPES:
    raise RuntimeError("ProductFeatureBundles drifted from installed feature bundle types")


@dataclass(frozen=True, slots=True)
class ThinEntrypointGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]


def coerce_feature_bundles(features: Mapping[str, object]) -> ProductFeatureBundles:
    owners = tuple(features)
    if len(owners) != len(set(owners)):
        raise ValueError("duplicate feature owners")
    expected = set(CAPABILITY_OWNERS)
    got = set(owners)
    missing = expected - got
    extra = got - expected
    if missing:
        raise ValueError(f"missing feature owners: {sorted(missing)}")
    if extra:
        raise ValueError(f"extra feature owners: {sorted(extra)}")
    checked: dict[str, object] = {}
    for feature in FEATURES:
        bundle = features[feature.owner_id]
        if not isinstance(bundle, feature.bundle_type):
            raise TypeError(f"{feature.owner_id} must be {feature.bundle_type.__name__}")
        checked[feature.owner_id] = bundle
    return ProductFeatureBundles(
        intake=cast(IntakeGraphs, checked["assurance.intake"]),
        generation=cast(GenerationGraphs, checked["assurance.generation"]),
        execution=cast(ExecutionGraphs, checked["assurance.execution"]),
        quality=cast(QualityGraphs, checked["assurance.quality"]),
        healing=cast(HealingGraphs, checked["assurance.healing"]),
        improvement=cast(ImprovementGraphs, checked["assurance.improvement"]),
    )


def build_thin_entrypoint_graphs(
    *,
    context: GraphBuildContext,
    features: Mapping[str, object],
) -> ThinEntrypointGraphs:
    bundles = coerce_feature_bundles(features)
    entrypoints = {
        "intake": build_intake_root(
            context, bundles.intake.prepare, bundles.generation.init_runtime, bundles.intake.case
        ),
        "init": build_init_root(context, bundles.generation.init_runtime),
        "archive": build_archive_root(context, bundles.improvement.archive),
        "retro": build_retro_root(context, bundles.improvement.retro),
        "issue-review": build_issue_review_root(context, bundles.quality.issue_review),
        "issue-analyze": build_issue_analyze_root(context, bundles.quality.issue_analyze),
        "issue-reconcile": build_issue_reconcile_root(context, bundles.quality.issue_reconcile),
        "improvement-review": build_improvement_review_root(context, bundles.improvement.review),
        "improvement-evaluate": build_improvement_evaluate_root(context, bundles.improvement.evaluate),
        "improvement-export": build_improvement_export_root(context, bundles.improvement.export),
        "improvement-apply": build_improvement_apply_root(context, bundles.improvement.apply),
        "improvement-rollback": build_improvement_rollback_root(context, bundles.improvement.rollback),
    }
    if set(entrypoints) != set(THIN_ENTRYPOINTS):
        raise ValueError("thin roots must match the declared entrypoints")
    return ThinEntrypointGraphs(entrypoints=MappingProxyType(entrypoints))


@dataclass(frozen=True, slots=True)
class ProductGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]
    contracts: Mapping[str, EntrypointGraphContract]


def _closed_entrypoints(entrypoints: Mapping[str, CompiledStateGraph]) -> Mapping[str, CompiledStateGraph]:
    names = tuple(entrypoints)
    if len(names) != len(set(names)):
        raise ValueError("duplicate product roots")
    expected = set(PRODUCT_ENTRYPOINTS)
    got = set(names)
    missing = expected - got
    extra = got - expected
    if missing:
        raise ValueError(f"missing product entrypoints: {sorted(missing)}")
    if extra:
        raise ValueError(f"extra product entrypoints: {sorted(extra)}")
    return MappingProxyType(dict(entrypoints))


def build_product_graphs(
    *,
    context: GraphBuildContext,
    features: Mapping[str, object],
    runtime_snapshot: Callable[[], Awaitable[EvidenceArtifactRefV1]] | None = None,
) -> ProductGraphs:
    from assurance_product.graphs.execute import build_execute_tail
    from assurance_product.graphs.full import build_full_root

    bundles = coerce_feature_bundles(features)
    thin = build_thin_entrypoint_graphs(context=context, features=features)
    thin_names = tuple(thin.entrypoints)
    if len(thin_names) != len(set(thin_names)):
        raise ValueError("duplicate product roots")
    execute_tail = build_execute_tail(bundles, runtime_snapshot=runtime_snapshot)
    stale_tail_nodes = _FORBIDDEN_PRODUCT_TAIL_NODES.intersection(execute_tail.nodes)
    if stale_tail_nodes:
        raise ValueError(f"obsolete Product coverage nodes are reachable: {sorted(stale_tail_nodes)}")
    full = build_full_root(context, bundles, execute_tail)
    entrypoints = {
        **dict(thin.entrypoints),
        "full": full,
    }
    return ProductGraphs(entrypoints=_closed_entrypoints(entrypoints), contracts=ENTRYPOINT_CONTRACTS)


def product_invoke_config(name: str) -> RunnableConfig:
    return {"recursion_limit": ENTRYPOINT_RECURSION_LIMITS[name]}


def invoke_product_root(
    graphs: ProductGraphs,
    name: str,
    payload: Mapping[str, object],
    **kwargs: Any,
) -> dict[str, Any]:
    config = dict(product_invoke_config(name))
    extra = kwargs.pop("config", None)
    if isinstance(extra, Mapping):
        config.update(dict(extra))
    result = graphs.entrypoints[name].invoke(payload, config=cast(RunnableConfig, config), **kwargs)
    if not isinstance(result, dict):
        raise TypeError("product root invoke must return a mapping")
    return result


invoke_product_root.config = product_invoke_config  # type: ignore[attr-defined]


__all__ = [
    "ProductFeatureBundles",
    "ProductGraphs",
    "ThinEntrypointGraphs",
    "build_product_graphs",
    "build_thin_entrypoint_graphs",
    "coerce_feature_bundles",
    "invoke_product_root",
    "product_invoke_config",
]
