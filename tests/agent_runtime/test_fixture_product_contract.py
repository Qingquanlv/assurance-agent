from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_fixture.contracts import frozen_run_request
from agent_runtime_fixture.product import FixtureProduct


def test_fixture_run_node_projects_the_frozen_agent_run_request() -> None:
    manifest = FixtureProduct.manifest()

    assert getattr(manifest, "workflow", None) is None
    assert manifest.graph_factory_symbol == "agent_runtime_fixture.product:build_fixture_graphs"
    AgentRunRequest.model_validate(frozen_run_request().model_dump(mode="json"))
