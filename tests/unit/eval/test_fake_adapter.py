from __future__ import annotations

from pathlib import Path

from assurance_agent.artifacts.models.coverage_repair import (
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
)
from assurance_agent.commands.eval_cmd import _FakeAdapter
from assurance_agent.workflow.graph.agent_api import AgentRequest


def test_fake_adapter_materializes_noop_coverage_repair_receipt(tmp_path: Path) -> None:
    change_id = "eval-sample-001"
    change_dir = tmp_path / "qa" / "changes" / change_id
    baseline_path = change_dir / "coverage-repair" / "entry-baseline.json"
    baseline_path.parent.mkdir(parents=True)
    baseline = CoverageRepairBaseline(
        change_id=change_id,
        attempt=1,
        attempt_token="attempt-token",
        test_tree_sha256="test-tree",
        test_files_sha256={},
        product_tree_sha256="product-tree",
        product_files_sha256={},
        declaration_tree_sha256="declaration-tree",
        declaration_files_sha256={},
    )
    baseline_path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    request = AgentRequest(
        target="skill:aa-coverage-repair",
        node_id="repair",
        change_id=change_id,
        workspace_root=tmp_path,
        allowed_writes=("change:coverage-repair/apply-summary.json",),
        prompt="fixture-backed coverage repair",
        timeout_seconds=60,
    )

    result = _FakeAdapter().invoke(request)

    assert result.ok is True
    summary = CoverageRepairApplySummary.model_validate_json(
        (change_dir / "coverage-repair" / "apply-summary.json").read_text(encoding="utf-8")
    )
    assert summary.change_id == change_id
    assert summary.attempt == 1
    assert summary.attempt_token == "attempt-token"
    assert summary.applied is False
    assert summary.files_modified == ()
