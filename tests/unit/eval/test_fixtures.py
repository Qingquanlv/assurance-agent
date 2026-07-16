from __future__ import annotations

from pathlib import Path

import yaml

from assurance_agent.eval.fixtures import load_tier, seed_change
from assurance_agent.workflow.core.state import verify_state_integrity, write_state
from assurance_agent.artifacts.models import WorkflowState


def _write_synth_fixtures(root: Path) -> Path:
    fixtures = root / "eval-fixtures"
    sample = fixtures / "samples" / "eval-sample-001"
    sample.mkdir(parents=True)
    (sample / "proposal.md").write_text("# proposal\n", encoding="utf-8")
    (sample / "cases").mkdir()
    (sample / "cases" / "case.yaml").write_text("cases: []\n", encoding="utf-8")
    write_state(
        sample,
        WorkflowState.model_validate(
            {
                "phases": {
                    "case-design": {"status": "done"},
                    "execution": {"status": "pending"},
                }
            }
        ),
    )
    # Also keep a copy named workflow-state.yaml already via write_state
    tiers = fixtures / "tiers"
    tiers.mkdir()
    (tiers / "L0-case-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L0-case-seed",
                "paths": ["proposal.md", "cases/case.yaml", "workflow-state.yaml"],
                "resets": {
                    "workflow_state": {
                        "phases.case-design.status": "done",
                        "phases.execution.status": "pending",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tiers / "L3-run-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L3-run-seed",
                "extends": "L0-case-seed",
                "paths": ["tests/api/test_synth.py"],
                "resets": {"workflow_state": {"phases.api_codegen.status": "done"}},
            }
        ),
        encoding="utf-8",
    )
    (sample / "tests" / "api").mkdir(parents=True)
    (sample / "tests" / "api" / "test_synth.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return fixtures


def test_load_tier_extends_merges_paths_and_resets(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    tier = load_tier(fixtures, "L3-run-seed")
    assert "proposal.md" in tier.paths
    assert "tests/api/test_synth.py" in tier.paths
    assert tier.resets.workflow_state["phases.api_codegen.status"] == "done"
    assert tier.resets.workflow_state["phases.execution.status"] == "pending"


def test_seed_change_preserves_state_integrity(tmp_path: Path) -> None:
    fixtures = _write_synth_fixtures(tmp_path)
    sut = tmp_path / "sut"
    sut.mkdir()
    seed_change(
        sut_sandbox=sut,
        change_id="eval-sample-001",
        tier_name="L3-run-seed",
        fixtures_root=fixtures,
    )
    change = sut / "qa" / "changes" / "eval-sample-001"
    assert (change / "proposal.md").exists()
    assert (sut / "tests" / "api" / "test_synth.py").exists()
    assert verify_state_integrity(change) is None
