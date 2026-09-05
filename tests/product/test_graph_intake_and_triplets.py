from __future__ import annotations

from assurance_intake.graphs.factory import IntakeGraphs, build_intake_graphs
from assurance_product.models import PRODUCT_ENTRYPOINTS
from assurance_product.output_routes import OutputRouteCatalog
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS

_PUBLIC_ENTRYPOINTS = (
    "intake",
    "case",
    "full",
    "execute",
    "archive",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
    "improvement-review",
    "improvement-evaluate",
    "improvement-export",
    "improvement-apply",
    "improvement-rollback",
)


def test_public_entrypoints_are_the_python_product_roots() -> None:
    assert set(PRODUCT_ENTRYPOINTS) == set(_PUBLIC_ENTRYPOINTS)
    assert len(PRODUCT_ENTRYPOINTS) == 14


def test_intake_bundle_exposes_prepare_load_and_case_graphs() -> None:
    assert PUBLIC_BUNDLE_FIELDS["assurance.intake"] == ("prepare", "load_plan", "case")
    assert "prepare" in IntakeGraphs.__dataclass_fields__
    assert "load_plan" in IntakeGraphs.__dataclass_fields__
    assert "case" in IntakeGraphs.__dataclass_fields__
    assert build_intake_graphs is not None


def test_output_route_catalog_covers_semantic_intake_contracts() -> None:
    catalog = OutputRouteCatalog()
    aliases = catalog.aliases()
    assert any(item.startswith("assurance.intake.agent.") for item in aliases)
    assert "assurance.intake.agent.intake.v1" in aliases
    assert "assurance.intake.agent.case-design.v1" in aliases
    outputs = catalog.outputs("assurance.intake.agent.intake.v1", "CH-DEMO-001")
    assert outputs
    assert all("CH-DEMO-001" in path or path.startswith("qa/") for path in outputs)


def test_intake_factory_is_the_bundle_authority() -> None:
    assert build_intake_graphs is not None
    assert "prepare" in PUBLIC_BUNDLE_FIELDS["assurance.intake"]
    assert "case" in PUBLIC_BUNDLE_FIELDS["assurance.intake"]
