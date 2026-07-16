from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.helpers_aa import write_aa_config

from assurance_agent.eval.runner import run_suite
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.orchestration.engine import Terminal, WorkflowStatus


def test_synth_workflow_run_writes_under_sut_out(tmp_path: Path) -> None:
    """M1 acceptance: seed + fake adapter + report under SUT eval/out."""
    engine = tmp_path / "engine"
    sut = tmp_path / "sut"
    write_aa_config(engine)
    write_aa_config(sut)

    suites = engine / "eval" / "suites"
    suites.mkdir(parents=True)
    suite_file = suites / "workflow-run.yaml"
    suite_file.write_text(
        yaml.safe_dump(
            {
                "name": "workflow-run",
                "scorer": "workflow-run",
                "executor": {"type": "aws-run", "scope": "full"},
                "thresholds": [],
            }
        ),
        encoding="utf-8",
    )
    ds = engine / "eval" / "datasets" / "workflow-run"
    ds.mkdir(parents=True)
    (ds / "WR-SYNTH-001.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "WR-SYNTH-001",
                "suite": "workflow-run",
                "input": {
                    "change_id": "eval-sample-001",
                    "fixture_tier": "L3-run-seed",
                },
                "expected": {},
            }
        ),
        encoding="utf-8",
    )

    fixtures = sut / "eval-fixtures"
    sample = fixtures / "samples" / "eval-sample-001"
    sample.mkdir(parents=True)
    (sample / "proposal.md").write_text("# p\n", encoding="utf-8")
    (sample / "workflow-state.yaml").write_text("phases: {}\n", encoding="utf-8")
    (fixtures / "tiers").mkdir()
    (fixtures / "tiers" / "L3-run-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L3-run-seed",
                "paths": ["proposal.md", "workflow-state.yaml"],
                "resets": {"workflow_state": {"phases.execution.status": "pending"}},
            }
        ),
        encoding="utf-8",
    )

    class FakeAdapter:
        def run_phase(self, request: PhaseRequest) -> PhaseResult:
            return PhaseResult(ok=True, output="fake")

    def status_provider() -> WorkflowStatus:
        return WorkflowStatus(phases=[], next_dispatch=[], terminal=Terminal(kind="completed", reason="fake"))

    run_id, gate = run_suite(
        suite_file=suite_file,
        project_root=engine,
        sut_dir=sut,
        adapter_factory=lambda **_: FakeAdapter(),
        status_provider_factory=lambda **_: status_provider,
        fixtures_root=fixtures,
    )
    report = sut / "eval" / "out" / "runs" / run_id / "report.json"
    assert report.exists()
    assert gate.verdict in {"pass", "pass_with_warnings", "fail", "inconclusive"}
    data = json.loads(report.read_text())
    assert data["suite"] == "workflow-run"
