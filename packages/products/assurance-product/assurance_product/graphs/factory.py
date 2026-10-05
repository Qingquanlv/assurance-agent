from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, cast, get_type_hints

from langchain_core.runnables.config import RunnableConfig

from graph_engine.boot.boot import EngineGraphBuildContext, GraphBuildContext
from graph_engine.flow import CompiledFlow, Flow

from assurance_execution.feature import ExecutionGraphs
from assurance_generation.feature import GenerationGraphs
from assurance_healing.feature import HealingGraphs
from assurance_improvement.feature import ImprovementGraphs
from assurance_intake.feature import IntakeGraphs
from assurance_product.graphs.entrypoints import thin_root_flows
from assurance_product.feature_set import CAPABILITY_OWNERS
from assurance_product.features import FEATURES
from assurance_product.graphs.revisions import ENTRYPOINT_RECURSION_LIMITS, contracts_from_roots
from assurance_product.graph_factories import build_feature_graphs
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from assurance_quality.feature import QualityGraphs
from graph_engine.boot.graph_revision import EntrypointGraphContract


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
    entrypoints: Mapping[str, CompiledFlow]


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


def product_root_flows(bundles: ProductFeatureBundles) -> dict[str, Flow]:
    from assurance_product.graphs.full import build_full_flow

    flows = dict(thin_root_flows(bundles))
    flows["full"] = build_full_flow(bundles)
    if set(flows) != set(PRODUCT_ENTRYPOINTS):
        raise ValueError("product root flows must match the declared entrypoints")
    return flows


def declared_root_flows() -> dict[str, Flow]:
    context = EngineGraphBuildContext(contracts={}, checkpointer=None, approved_source_roots=())
    return product_root_flows(coerce_feature_bundles(build_feature_graphs(context)))


@cache
def entrypoint_contracts() -> Mapping[str, EntrypointGraphContract]:
    return contracts_from_roots(declared_root_flows())


def build_thin_entrypoint_graphs(
    *,
    context: GraphBuildContext,
    features: Mapping[str, object],
) -> ThinEntrypointGraphs:
    flows = thin_root_flows(coerce_feature_bundles(features))
    if set(flows) != set(THIN_ENTRYPOINTS):
        raise ValueError("thin roots must match the declared entrypoints")
    entrypoints = {name: flow.compile(context) for name, flow in flows.items()}
    return ThinEntrypointGraphs(entrypoints=MappingProxyType(entrypoints))


@dataclass(frozen=True, slots=True)
class ProductGraphs:
    entrypoints: Mapping[str, CompiledFlow]
    contracts: Mapping[str, EntrypointGraphContract]


def _closed_entrypoints(entrypoints: Mapping[str, CompiledFlow]) -> Mapping[str, CompiledFlow]:
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
) -> ProductGraphs:
    flows = product_root_flows(coerce_feature_bundles(features))
    names = tuple(flows)
    if len(names) != len(set(names)):
        raise ValueError("duplicate product roots")
    entrypoints = {name: flow.compile(context) for name, flow in flows.items()}
    return ProductGraphs(
        entrypoints=_closed_entrypoints(entrypoints),
        contracts=contracts_from_roots(flows),
    )


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
    "declared_root_flows",
    "entrypoint_contracts",
    "product_root_flows",
    "invoke_product_root",
    "product_invoke_config",
]
