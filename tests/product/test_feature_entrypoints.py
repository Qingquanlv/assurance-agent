from __future__ import annotations

from importlib import import_module

import pytest

from graph_engine.boot import FeatureSpec

from assurance_product.feature_set import CAPABILITIES, CapabilityPin
from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES

_FACTORY_SYMBOLS = {ref.owner_id: ref.symbol for ref in FEATURE_GRAPH_FACTORIES}


@pytest.mark.parametrize(
    ("pin", "agents", "tasks"),
    tuple(
        (pin, agents, tasks)
        for pin, (agents, tasks) in zip(
            CAPABILITIES,
            ((5, 1), (8, 3), (0, 2), (3, 0), (5, 3), (6, 9)),
            strict=True,
        )
    ),
    ids=tuple(pin.owner_id for pin in CAPABILITIES),
)
def test_capability_feature_exports_existing_contracts_and_graph_factory(
    pin: CapabilityPin, agents: int, tasks: int
) -> None:
    feature: FeatureSpec = import_module(f"{pin.package}.feature").FEATURE

    assert feature.graph_factory.owner_id == pin.owner_id
    assert feature.graph_factory.symbol == _FACTORY_SYMBOLS[pin.owner_id]
    assert feature.plugin.descriptor().plugin_id == pin.owner_id
    assert len(feature.agent_contracts) == agents
    assert len(feature.task_contracts) == tasks


def test_product_assembles_only_the_six_explicit_features() -> None:
    from assurance_product.agent_contracts import (
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )
    from assurance_product.feature_set import CAPABILITY_OWNERS
    from assurance_product.features import FEATURES, validate_feature_set
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
    from assurance_product.output_routes import OutputRouteCatalog

    assert tuple(feature.owner_id for feature in FEATURES) == CAPABILITY_OWNERS
    validate_feature_set(FEATURES)
    assert FEATURE_GRAPH_FACTORIES == tuple(feature.graph_factory for feature in FEATURES)
    assert len(all_feature_agent_contracts()) == 27
    assert len(all_feature_task_contracts()) == 18
    assert len(OutputRouteCatalog().aliases()) == 27
    with pytest.raises(ValueError, match="feature owners"):
        validate_feature_set((*FEATURES[:-1], FEATURES[0]))


def test_product_rejects_feature_source_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from assurance_intake.plugin import IntakePlugin
    from assurance_product.features import FEATURES, validate_feature_set

    original = IntakePlugin.spec
    monkeypatch.setattr(
        IntakePlugin,
        "spec",
        replace(original, source=original.source.model_copy(update={"version": "9.9.9"})),
    )
    with pytest.raises(ValueError, match="feature source"):
        validate_feature_set(FEATURES)


def test_product_rejects_duplicate_task_catalog_key() -> None:
    from dataclasses import replace

    from assurance_product.features import FEATURES, validate_feature_set

    index = next(i for i, feature in enumerate(FEATURES) if feature.owner_id == "assurance.quality")
    quality = FEATURES[index]
    first_key = next(iter(quality.task_contracts))
    altered_quality = replace(
        quality,
        task_contracts={
            "resolve-plan": quality.task_contracts[first_key],
            **{key: value for key, value in quality.task_contracts.items() if key != first_key},
        },
    )
    with pytest.raises(ValueError, match="duplicate feature task key"):
        validate_feature_set((*FEATURES[:index], altered_quality, *FEATURES[index + 1 :]))


@pytest.mark.parametrize(
    ("module", "bundle_name"),
    [
        ("assurance_intake", "IntakeGraphs"),
        ("assurance_generation", "GenerationGraphs"),
        ("assurance_execution", "ExecutionGraphs"),
        ("assurance_quality", "QualityGraphs"),
        ("assurance_healing", "HealingGraphs"),
        ("assurance_improvement", "ImprovementGraphs"),
    ],
)
def test_feature_exposes_its_graph_bundle_type(module: str, bundle_name: str) -> None:
    feature = import_module(f"{module}.feature")
    factory = import_module(f"{module}.graphs.factory")
    assert getattr(feature, bundle_name) is getattr(factory, bundle_name)


def test_intake_feature_module_imports_with_plugin() -> None:
    import subprocess
    import sys

    from assurance_intake.feature import FEATURE
    from assurance_intake.plugin import IntakePlugin

    assert FEATURE.plugin is IntakePlugin
    assert IntakePlugin.descriptor().attempt_contracts
    for first, second in (("feature", "plugin"), ("plugin", "feature")):
        subprocess.run(
            [sys.executable, "-c", f"import assurance_intake.{first}; import assurance_intake.{second}"],
            check=True,
        )


def test_intake_plugin_contract_refs_match_task_catalog() -> None:
    from assurance_intake.feature import attempt_contract_refs
    from assurance_intake.feature import FEATURE
    from assurance_intake.plugin import IntakePlugin

    assert IntakePlugin.descriptor().attempt_contracts == attempt_contract_refs()
    assert set(FEATURE.agent_contracts) == {"intake", "explore", "case-design", "case-repair", "case-review"}
