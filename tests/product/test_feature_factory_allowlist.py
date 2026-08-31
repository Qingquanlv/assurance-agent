from __future__ import annotations

import importlib
from importlib import metadata
import sys
from pathlib import Path

from graph_engine.boot.graph_revision import FeatureFactoryRef

from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES


EXPECTED_FEATURE_GRAPH_FACTORIES = (
    FeatureFactoryRef("assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"),
    FeatureFactoryRef(
        "assurance.generation",
        "assurance_generation.graphs.factory:build_generation_graphs",
    ),
    FeatureFactoryRef(
        "assurance.execution",
        "assurance_execution.graphs.factory:build_execution_graphs",
    ),
    FeatureFactoryRef("assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"),
    FeatureFactoryRef("assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"),
    FeatureFactoryRef(
        "assurance.improvement",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
)


def test_feature_graph_factories_are_the_fixed_six_owner_symbol_pairs() -> None:
    assert FEATURE_GRAPH_FACTORIES == EXPECTED_FEATURE_GRAPH_FACTORIES
    assert tuple((item.owner_id, item.symbol) for item in FEATURE_GRAPH_FACTORIES) == tuple(
        (item.owner_id, item.symbol) for item in EXPECTED_FEATURE_GRAPH_FACTORIES
    )
    assert {item.owner_id for item in FEATURE_GRAPH_FACTORIES} == {
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.quality",
        "assurance.healing",
        "assurance.improvement",
    }


def test_feature_graph_factories_are_not_derived_from_an_entrypoint_scan() -> None:
    scanned = tuple(
        FeatureFactoryRef(entry.name, entry.value)
        for group in ("graph_engine.plugins", "graph_engine.products", "graph_engine.graphs")
        for entry in metadata.entry_points().select(group=group)
    )
    assert FEATURE_GRAPH_FACTORIES != scanned
    assert not any(
        item.symbol == scanned_item.symbol for item in FEATURE_GRAPH_FACTORIES for scanned_item in scanned
    )


def test_feature_graph_factories_ignore_configuration_override(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    (project / ".aa").mkdir(parents=True)
    (project / ".aa" / "graph-factories.yaml").write_text(
        "factories:\n  - owner_id: assurance.rogue\n    symbol: rogue.graphs:build\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("AA_FEATURE_GRAPH_FACTORIES", "assurance.rogue:rogue.graphs:build")
    importlib.reload(sys.modules["assurance_product.graph_factories"])
    from assurance_product.graph_factories import FEATURE_GRAPH_FACTORIES as reloaded

    assert reloaded == EXPECTED_FEATURE_GRAPH_FACTORIES
    assert all(item.owner_id != "assurance.rogue" for item in reloaded)


def test_importing_the_allowlist_does_not_import_feature_factory_modules() -> None:
    for name in list(sys.modules):
        if ".graphs.factory" in name:
            del sys.modules[name]
    importlib.reload(sys.modules["assurance_product.graph_factories"])
    loaded = {name for name in sys.modules if name.endswith(".graphs.factory")}
    assert loaded == set()
