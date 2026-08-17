from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.helpers_aa import write_aa_config

from assurance_agent.eval.fixtures import write_fixture_lock
from assurance_agent.eval.runner import run_suite
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult


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
                "executor": {"type": "workflow-run", "entrypoint": "execute"},
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
                    "fixture_id": "eval-sample-001",
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
    (sample / "workflow-state.json").write_text(
        json.dumps(
            {
                "phases": {"skill_registry_check": {"status": "pass"}},
                "run_context": {
                    "interaction_mode": "autonomous",
                    "orchestrator_skill": "aa-workflow",
                },
            }
        ),
        encoding="utf-8",
    )
    (sample / "tests").mkdir()
    for required in ("config.py", "conftest.py", "schema_validation.py"):
        (sample / "tests" / required).write_text("# fixture\n", encoding="utf-8")
    (fixtures / "tiers").mkdir()
    (fixtures / "tiers" / "L3-run-seed.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "L3-run-seed",
                "paths": [
                    "proposal.md",
                    "workflow-state.json",
                    "tests/config.py",
                    "tests/conftest.py",
                    "tests/schema_validation.py",
                ],
                "resets": {"workflow_state": {"phases.execution.status": "pending"}},
                "imports": {
                    "execute": {
                        "entrypoint": "execute",
                        "inputs": ["change:proposal.md"],
                        "completed": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_fixture_lock(fixtures, {"eval-sample-001": "samples/eval-sample-001"})

    class FakeAdapter:
        def invoke(self, request: AgentRequest) -> AgentResult:
            return AgentResult(ok=True)

    run_id, gate = run_suite(
        suite_file=suite_file,
        project_root=engine,
        sut_dir=sut,
        adapter_factory=lambda **_: FakeAdapter(),
        fixtures_root=fixtures,
    )
    report = sut / "eval" / "out" / "runs" / run_id / "report.json"
    assert report.exists()
    assert gate.verdict in {"pass", "pass_with_warnings", "fail", "inconclusive"}
    data = json.loads(report.read_text())
    assert data["suite"] == "workflow-run"
