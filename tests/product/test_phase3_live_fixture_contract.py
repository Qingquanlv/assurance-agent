from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from agent_runtime_contracts import AgentRunRequest
from agent_runtime_fixture.contracts import frozen_run_request
from agent_runtime_fixture.product import FixtureProduct

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "benchmark" / "agent-runtime-phase3" / "manifest.json"


@pytest.fixture
def repo_root() -> Path:
    return _REPO_ROOT


def test_phase3_live_manifest_has_one_locked_fixture_per_adapter(repo_root: Path) -> None:
    manifest = json.loads((repo_root / "benchmark/agent-runtime-phase3/manifest.json").read_text())
    assert {item["adapter"] for item in manifest["items"]} == {"opencode"}
    for item in manifest["items"]:
        assert item["adapter_version"] == "0.1.0"
        assert "binding_manifest" in item
        assert "expected_artifacts" in item


def test_fixture_run_node_projects_the_frozen_agent_run_request() -> None:
    manifest = FixtureProduct.manifest()
    assert getattr(manifest, "workflow", None) is None
    assert manifest.graph_factory_symbol == "agent_runtime_fixture.product:build_fixture_graphs"
    AgentRunRequest.model_validate(frozen_run_request().model_dump(mode="json"))


def _load_run_item(repo_root: Path):
    spec = importlib.util.spec_from_file_location(
        "phase3_run_item",
        repo_root / "benchmark" / "agent-runtime-phase3" / "run_item.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_workspace_output_is_the_dual_root_write_set(tmp_path: Path, repo_root: Path) -> None:
    run_item = _load_run_item(repo_root)
    payload = {"artifact": "result.json", "status": "ok"}
    item_id = "phase3-opencode-live"
    write_root = tmp_path / ".engine-attempts" / "task-1" / "attempt-1"
    leftover_workspace = tmp_path / "engine" / "invocations" / item_id / "workspace"
    published = write_root / "result.json"
    leftover = leftover_workspace / "trees" / ("5" * 64) / "result.json"
    published.parent.mkdir(parents=True)
    leftover.parent.mkdir(parents=True)
    published.write_text(json.dumps(payload), encoding="utf-8")
    leftover.write_text(json.dumps({"status": "stale"}), encoding="utf-8")
    (leftover_workspace).mkdir(parents=True, exist_ok=True)
    (leftover_workspace / "HEAD.json").write_text(json.dumps({"tree_id": "5" * 64}), encoding="utf-8")
    manifest = json.loads((repo_root / "benchmark" / "agent-runtime-phase3" / "manifest.json").read_text())
    item = run_item.ManifestItem(
        adapter="opencode",
        adapter_version="0.1.0",
        item_id=item_id,
        binding_manifest={},
        expected_artifacts=(),
        secret_handle="opencode.token",
        secret_env="OPENCODE_PHASE3_TOKEN",
    )
    run_item._validate_workspace_output(manifest, tmp_path, item)
    selected = run_item._published_workspace_output(
        tmp_path / ".engine-attempts",
        "result.json",
    )
    assert selected == published
