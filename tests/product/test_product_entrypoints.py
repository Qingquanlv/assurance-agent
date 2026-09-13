from __future__ import annotations

from assurance_product.graphs.factory import (
    ProductFeatureBundles,
    ThinEntrypointGraphs,
    build_thin_entrypoint_graphs,
)
from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS

PUBLIC_ENTRYPOINTS = {
    "full",
    "intake",
    "case",
    "execute",
    "init",
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
}


def test_product_entrypoints_are_fifteen_python_roots() -> None:
    assert set(PRODUCT_ENTRYPOINTS) == PUBLIC_ENTRYPOINTS
    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_CONTRACTS) == 15
    assert set(THIN_ENTRYPOINTS) <= set(PRODUCT_ENTRYPOINTS)
    assert len(THIN_ENTRYPOINTS) == 13


def test_feature_bundles_cover_six_owners() -> None:
    assert set(PUBLIC_BUNDLE_FIELDS) == {
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.quality",
        "assurance.healing",
        "assurance.improvement",
    }
    assert set(ProductFeatureBundles.__dataclass_fields__) == {
        "intake",
        "generation",
        "execution",
        "quality",
        "healing",
        "improvement",
    }


def test_thin_entrypoint_graphs_match_checked_route_inventory() -> None:
    assert set(ThinEntrypointGraphs.__dataclass_fields__) == {"entrypoints"}
    assert build_thin_entrypoint_graphs is not None
    for name in THIN_ENTRYPOINTS:
        assert name in ENTRYPOINT_CONTRACTS
        assert ENTRYPOINT_CONTRACTS[name].name == name
