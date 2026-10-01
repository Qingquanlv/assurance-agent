from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from graph_engine.boot.boot import GraphBuildContext

from assurance_product.features import FEATURES

FEATURE_GRAPH_FACTORIES = tuple(feature.graph_factory for feature in FEATURES)


def _load_feature_factory(symbol: str) -> Callable[..., Any]:
    module_name, attribute = symbol.split(":", 1)
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError(f"feature graph factory is not callable: {symbol}")
    return factory


def build_feature_graphs(context: GraphBuildContext) -> dict[str, object]:
    return {
        ref.owner_id: _load_feature_factory(ref.symbol)(context.for_capability(ref.owner_id))
        for ref in FEATURE_GRAPH_FACTORIES
    }
