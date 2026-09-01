from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from langgraph.graph.state import CompiledStateGraph

from assurance_execution.graphs.factory import ExecutionGraphs
from assurance_generation.graphs.factory import GenerationGraphs
from assurance_healing.graphs.factory import HealingGraphs
from assurance_improvement.graphs.factory import ImprovementGraphs
from assurance_intake.graphs.factory import IntakeGraphs
from assurance_product.graphs.entrypoints import (
    build_archive_root,
    build_case_root,
    build_improvement_apply_root,
    build_improvement_evaluate_root,
    build_improvement_export_root,
    build_improvement_review_root,
    build_improvement_rollback_root,
    build_intake_root,
    build_issue_analyze_root,
    build_issue_reconcile_root,
    build_issue_review_root,
    build_retro_root,
)
from assurance_product.models import FEATURE_WORKFLOW_OWNERS, THIN_ENTRYPOINTS
from assurance_quality.graphs.factory import QualityGraphs
from graph_engine.boot.boot import GraphBuildContext

_BUNDLE_TYPES: Mapping[str, type] = {
    "assurance.intake": IntakeGraphs,
    "assurance.generation": GenerationGraphs,
    "assurance.execution": ExecutionGraphs,
    "assurance.quality": QualityGraphs,
    "assurance.healing": HealingGraphs,
    "assurance.improvement": ImprovementGraphs,
}


@dataclass(frozen=True, slots=True)
class ProductFeatureBundles:
    intake: IntakeGraphs
    generation: GenerationGraphs
    execution: ExecutionGraphs
    quality: QualityGraphs
    healing: HealingGraphs
    improvement: ImprovementGraphs


@dataclass(frozen=True, slots=True)
class ThinEntrypointGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]


def coerce_feature_bundles(features: Mapping[str, object]) -> ProductFeatureBundles:
    owners = tuple(features)
    if len(owners) != len(set(owners)):
        raise ValueError("duplicate feature owners")
    expected = set(FEATURE_WORKFLOW_OWNERS)
    got = set(owners)
    missing = expected - got
    extra = got - expected
    if missing:
        raise ValueError(f"missing feature owners: {sorted(missing)}")
    if extra:
        raise ValueError(f"extra feature owners: {sorted(extra)}")
    typed: dict[str, object] = {}
    for owner in FEATURE_WORKFLOW_OWNERS:
        bundle = features[owner]
        required = _BUNDLE_TYPES[owner]
        if not isinstance(bundle, required):
            raise TypeError(f"{owner} must be {required.__name__}")
        typed[owner] = bundle
    return ProductFeatureBundles(
        intake=cast(IntakeGraphs, typed["assurance.intake"]),
        generation=cast(GenerationGraphs, typed["assurance.generation"]),
        execution=cast(ExecutionGraphs, typed["assurance.execution"]),
        quality=cast(QualityGraphs, typed["assurance.quality"]),
        healing=cast(HealingGraphs, typed["assurance.healing"]),
        improvement=cast(ImprovementGraphs, typed["assurance.improvement"]),
    )


def build_thin_entrypoint_graphs(
    *,
    context: GraphBuildContext,
    features: Mapping[str, object],
) -> ThinEntrypointGraphs:
    bundles = coerce_feature_bundles(features)
    entrypoints = {
        "intake": build_intake_root(context, bundles.intake.prepare),
        "case": build_case_root(context, bundles.intake.case),
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
    if set(entrypoints) != set(THIN_ENTRYPOINTS) or len(entrypoints) != 12:
        raise ValueError("thin roots must be the 12 declared entrypoints")
    return ThinEntrypointGraphs(entrypoints=MappingProxyType(entrypoints))


__all__ = [
    "ProductFeatureBundles",
    "ThinEntrypointGraphs",
    "build_thin_entrypoint_graphs",
    "coerce_feature_bundles",
]
