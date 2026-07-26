import pytest

from assurance_agent.workflow.driver.driver_state import driver_status_for_graph
from assurance_agent.workflow.driver import loop


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
def test_mapping(graph: str, driver: str) -> None:
    assert driver_status_for_graph(graph) == driver


def test_loop_references_shared_mapping() -> None:
    assert loop._driver_status_for is driver_status_for_graph  # noqa: SLF001
