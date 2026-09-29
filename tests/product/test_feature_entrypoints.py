from __future__ import annotations

from importlib import import_module

import pytest

from graph_engine.boot import FeatureSpec


@pytest.mark.parametrize(
    ("module", "owner", "agents", "tasks", "symbol"),
    [
        ("assurance_intake", "assurance.intake", 4, 2, "build_intake_graphs"),
        ("assurance_generation", "assurance.generation", 8, 3, "build_generation_graphs"),
        ("assurance_execution", "assurance.execution", 0, 2, "build_execution_graphs"),
        ("assurance_quality", "assurance.quality", 5, 3, "build_quality_graphs"),
        ("assurance_healing", "assurance.healing", 3, 0, "build_healing_graphs"),
        ("assurance_improvement", "assurance.improvement", 6, 9, "build_improvement_graphs"),
    ],
)
def test_capability_feature_exports_existing_contracts_and_graph_factory(
    module: str, owner: str, agents: int, tasks: int, symbol: str
) -> None:
    entry = "task" if module == "assurance_intake" else "feature"
    feature: FeatureSpec = import_module(f"{module}.{entry}").FEATURE

    assert feature.graph_factory.owner_id == owner
    assert feature.graph_factory.symbol == f"{module}.graphs.factory:{symbol}"
    assert feature.plugin.descriptor().plugin_id == owner
    assert len(feature.agent_contracts) == len(feature.agent_task_types) == agents
    assert len(feature.task_contracts) == tasks
    assert {task.contract.contract_id for task in feature.agent_task_types} == {
        contract.contract_id for contract in feature.agent_contracts.values()
    }


def test_product_assembles_only_the_six_explicit_features() -> None:
    from assurance_product.agent_contracts import (
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )
    from assurance_product.features import FEATURES, validate_feature_set
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES
    from assurance_product.output_routes import OutputRouteCatalog
    from assurance_product.runtime_bindings import _AGENT_TASK_TYPES

    assert tuple(feature.graph_factory.owner_id for feature in FEATURES) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.quality",
        "assurance.healing",
        "assurance.improvement",
    )
    validate_feature_set(FEATURES)
    assert FEATURE_GRAPH_FACTORIES == tuple(feature.graph_factory for feature in FEATURES)
    assert len(all_feature_agent_contracts()) == len(_AGENT_TASK_TYPES) == 26
    assert len(all_feature_task_contracts()) == 19
    assert len(OutputRouteCatalog().aliases()) == 26
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

    quality = FEATURES[3]
    first_key = next(iter(quality.task_contracts))
    altered_quality = replace(
        quality,
        task_contracts={
            "resolve-plan": quality.task_contracts[first_key],
            **{key: value for key, value in quality.task_contracts.items() if key != first_key},
        },
    )
    with pytest.raises(ValueError, match="duplicate feature task key"):
        validate_feature_set((*FEATURES[:3], altered_quality, *FEATURES[4:]))


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
    entry = "task" if module == "assurance_intake" else "feature"
    feature = import_module(f"{module}.{entry}")
    factory = import_module(f"{module}.graphs.factory")
    assert getattr(feature, bundle_name) is getattr(factory, bundle_name)


def test_intake_task_module_owns_lifecycle_classes() -> None:
    import subprocess
    import sys

    from assurance_intake.plugin import IntakePlugin
    from assurance_intake.task import (
        FEATURE,
        CaseDesignTask,
        CaseReviewTask,
        ExploreTask,
        IntakeTask,
    )

    assert FEATURE.plugin is IntakePlugin
    assert FEATURE.agent_task_types == (
        IntakeTask,
        ExploreTask,
        CaseDesignTask,
        CaseReviewTask,
    )
    assert all(task.__module__ == "assurance_intake.task" for task in FEATURE.agent_task_types)
    assert IntakePlugin.descriptor().attempt_contracts
    for first, second in (("task", "plugin"), ("plugin", "task")):
        subprocess.run(
            [sys.executable, "-c", f"import assurance_intake.{first}; import assurance_intake.{second}"],
            check=True,
        )


def test_intake_plugin_contract_refs_match_task_catalog() -> None:
    from assurance_intake.contracts.attempts import attempt_contract_refs
    from assurance_intake.plugin import IntakePlugin
    from assurance_intake.task import FEATURE

    assert IntakePlugin.descriptor().attempt_contracts == attempt_contract_refs()
    assert set(FEATURE.agent_contracts) == {"intake", "explore", "case-design", "case-review"}
