from __future__ import annotations

from assurance_product.feature_set import CAPABILITY_OWNERS
from assurance_product.models import PRODUCT_ENTRYPOINTS, THIN_ENTRYPOINTS
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS

PUBLIC_ENTRYPOINTS = {
    "full",
    "intake",
    "init",
    "retro",
    "issue-review",
    "issue-analyze",
    "issue-reconcile",
}


def test_product_entrypoints_are_seven_python_roots() -> None:
    from assurance_product.graphs.factory import entrypoint_contracts

    contracts = entrypoint_contracts()
    assert set(PRODUCT_ENTRYPOINTS) == PUBLIC_ENTRYPOINTS
    assert set(contracts) == set(PRODUCT_ENTRYPOINTS)
    assert len(contracts) == 7
    assert set(THIN_ENTRYPOINTS) <= set(PRODUCT_ENTRYPOINTS)
    assert len(THIN_ENTRYPOINTS) == 6


def test_feature_bundles_cover_six_owners() -> None:
    from assurance_product.graphs.factory import ProductFeatureBundles

    assert set(PUBLIC_BUNDLE_FIELDS) == set(CAPABILITY_OWNERS)
    assert set(ProductFeatureBundles.__dataclass_fields__) == {
        "intake",
        "generation",
        "execution",
        "quality",
        "healing",
        "improvement",
    }


def test_thin_entrypoint_graphs_match_checked_route_inventory() -> None:
    from assurance_product.graphs.factory import (
        ThinEntrypointGraphs,
        build_thin_entrypoint_graphs,
        entrypoint_contracts,
    )

    assert set(ThinEntrypointGraphs.__dataclass_fields__) == {"entrypoints"}
    assert build_thin_entrypoint_graphs is not None
    for name in THIN_ENTRYPOINTS:
        assert name in entrypoint_contracts()
        assert entrypoint_contracts()[name].name == name
