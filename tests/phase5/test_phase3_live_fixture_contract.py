from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunRequest
from agent_runtime_fixture.product import FixtureProduct
from graph_engine.graph.input_projection import project_task_input

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "benchmark" / "agent-runtime-phase3" / "manifest.json"


@pytest.fixture
def repo_root() -> Path:
    return _REPO_ROOT


def test_phase3_live_manifest_has_one_locked_fixture_per_adapter(repo_root: Path) -> None:
    manifest = json.loads((repo_root / "benchmark/agent-runtime-phase3/manifest.json").read_text())
    assert {item["adapter"] for item in manifest["items"]} == {"opencode", "cursor"}
    for item in manifest["items"]:
        assert item["adapter_version"] == "0.1.0"
        assert "binding_manifest" in item
        assert "expected_artifacts" in item


def test_fixture_run_node_projects_the_frozen_agent_run_request() -> None:
    node = FixtureProduct.manifest().workflow.graphs["root"].nodes["run"]
    assert node.input_projection is not None
    projected = project_task_input(
        node.input_projection,
        root_input={},
        node_config=node.input,
        predecessor_tokens={},
    )
    AgentRunRequest.model_validate(projected)
