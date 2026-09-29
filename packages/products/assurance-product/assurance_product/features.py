"""The Product's closed, installed set of six capability interfaces."""

from __future__ import annotations

from collections.abc import Sequence

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.boot import FeatureSpec

from assurance_execution.feature import FEATURE as EXECUTION
from assurance_generation.feature import FEATURE as GENERATION
from assurance_healing.feature import FEATURE as HEALING
from assurance_improvement.feature import FEATURE as IMPROVEMENT
from assurance_intake.task import FEATURE as INTAKE
from assurance_quality.feature import FEATURE as QUALITY

from assurance_product.source_catalog import product_source_catalog

FEATURES: tuple[FeatureSpec[AgentExecutionContract], ...] = (
    INTAKE,
    GENERATION,
    EXECUTION,
    QUALITY,
    HEALING,
    IMPROVEMENT,
)

_EXPECTED_OWNERS = (
    "assurance.intake",
    "assurance.generation",
    "assurance.execution",
    "assurance.quality",
    "assurance.healing",
    "assurance.improvement",
)


def validate_feature_set(features: Sequence[FeatureSpec[AgentExecutionContract]]) -> None:
    owners = tuple(feature.graph_factory.owner_id for feature in features)
    if owners != _EXPECTED_OWNERS:
        raise ValueError(f"feature owners drifted: {owners}")
    pinned_sources = {source.distribution: source for source in product_source_catalog()[:-1]}
    actual_sources = {
        feature.plugin.spec.source.distribution: feature.plugin.spec.source for feature in features
    }
    if actual_sources != pinned_sources:
        raise ValueError("feature source catalog drifted")
    task_keys = [name for feature in features for name in feature.task_contracts]
    if len(task_keys) != len(set(task_keys)):
        raise ValueError("duplicate feature task key")
    contract_ids = [
        contract.contract_id
        for feature in features
        for contract in (*feature.agent_contracts.values(), *feature.task_contracts.values())
    ]
    if len(contract_ids) != len(set(contract_ids)):
        raise ValueError("duplicate feature contract ID")


validate_feature_set(FEATURES)

__all__ = ["FEATURES", "validate_feature_set"]
