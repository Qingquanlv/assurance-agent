from __future__ import annotations

import pytest

from assurance_generation.graphs.factory import build_generation_graphs
from graph_engine.testing import GraphHarness
from test_generation_graph_factory import (  # pyright: ignore[reportMissingImports]
    generation_contracts,
)


@pytest.fixture
def recording_context():
    return GraphHarness().recording_context(
        owner_id="assurance.generation",
        contracts=generation_contracts(),
    )


def test_init_runtime_is_a_one_attempt_graph(recording_context) -> None:
    bundle = build_generation_graphs(recording_context)
    assert "generation.init-test-runtime" in bundle.init_runtime.nodes
    assert "generation" not in bundle.init_runtime.nodes
