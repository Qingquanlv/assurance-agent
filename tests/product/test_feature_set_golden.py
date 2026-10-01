from __future__ import annotations

from assurance_product.feature_set import CAPABILITIES, CAPABILITY_OWNERS
from assurance_product.features import FEATURES
from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

_GOLDEN: tuple[tuple[str, str, str], ...] = (
    (
        "assurance.intake",
        "assurance_intake.plugin:IntakePlugin",
        "assurance_intake.graphs.factory:build_intake_graphs",
    ),
    (
        "assurance.generation",
        "assurance_generation.plugin:GenerationPlugin",
        "assurance_generation.graphs.factory:build_generation_graphs",
    ),
    (
        "assurance.execution",
        "assurance_execution.plugin:ExecutionPlugin",
        "assurance_execution.graphs.factory:build_execution_graphs",
    ),
    (
        "assurance.healing",
        "assurance_healing.plugin:HealingPlugin",
        "assurance_healing.graphs.factory:build_healing_graphs",
    ),
    (
        "assurance.quality",
        "assurance_quality.plugin:QualityPlugin",
        "assurance_quality.graphs.factory:build_quality_graphs",
    ),
    (
        "assurance.improvement",
        "assurance_improvement.plugin:ImprovementPlugin",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
)


def test_capability_pins_and_graph_factories_match_the_golden_table() -> None:
    assert tuple((pin.owner_id, pin.plugin) for pin in CAPABILITIES) == tuple(
        (owner_id, plugin) for owner_id, plugin, _symbol in _GOLDEN
    )
    assert tuple((ref.owner_id, ref.symbol) for ref in FEATURE_GRAPH_FACTORIES) == tuple(
        (owner_id, symbol) for owner_id, _plugin, symbol in _GOLDEN
    )
    assert {feature.owner_id for feature in FEATURES} == set(CAPABILITY_OWNERS)
