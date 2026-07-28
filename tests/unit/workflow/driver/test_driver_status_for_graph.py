from typing import cast

import pytest

from assurance_agent.workflow.driver.driver_state import driver_status_for_graph
from assurance_agent.workflow.graph.models import GraphLifecycleStatus


@pytest.mark.parametrize(
    ("graph", "driver"),
    [
        ("completed", "completed"),
        ("interrupted", "paused"),
        ("stopped", "failed"),
        ("failed", "failed"),
        ("running", "running"),
    ],
)
def test_mapping(graph: GraphLifecycleStatus, driver: str) -> None:
    assert driver_status_for_graph(graph) == driver


def test_unknown_status_raises() -> None:
    with pytest.raises(ValueError, match="unknown graph status"):
        driver_status_for_graph(cast(GraphLifecycleStatus, "typo"))
