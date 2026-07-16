"""End-to-end happy path over the *packaged* workflow schema.

Regression guard for the healing-overlay bug: on the no-healing-needed path the
orchestrator must record `not_needed` so `report.ready_when` unblocks and the DAG
reaches `completed`. Uses real `compute_status` + in-process `apply_phase_outcome`.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.helpers_aa import write_aa_config

from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    DefaultCliPhaseExecutor,
    DefaultHealingActionExecutor,
    run_workflow_loop,
)
from assurance_agent.workflow.orchestration.healing_episode import HealingEpisodeAction
from assurance_agent.workflow.orchestration.operations import record_heal_transition
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


_JSON = {
    "explore/advisory.json": {"advisories": []},
    "review/case-review.json": {
        "decision": "pass",
        "human_review_required": False,
        "auto_fix_allowed": False,
    },
    "facts/fact-baseline.json": {"facts": []},
    "review/api-plan-review.json": {
        "decision": "pass",
        "codegen_readiness": "ready",
        "human_review_required": False,
        "risk_level": "low",
    },
    "inspect/failure-analysis.json": {"failures": [], "source_batch_id": "b1"},
    "inspect/quality-gate-result.json": {"decision": "pass"},
    "report/quality-report.json": {"score": 100},
}
_TEXT = {
    ".qa.yaml": "approval: {}\n",
    "proposal.md": "# proposal\n",
    "cases/case-1.yaml": "id: c1\n",
    "plans/api-plan.md": "# api plan\n",
    "plans/api-test-data-plan.md": "# data\n",
    "plans/api-codegen-plan.md": "# codegen\n",
    "plans/m3-review-summary.md": "# summary\n",
    "codegen/api-codegen-summary.md": "# codegen summary\n",
    "report/executive-summary.md": "# exec summary\n",
}

_PHASE_PRODUCES: dict[str, list[str]] = {
    "explore": ["explore/advisory.json"],
    "case-design": [".qa.yaml", "proposal.md", "cases/case-1.yaml"],
    "case-review": ["review/case-review.json"],
    "fact-baseline": ["facts/fact-baseline.json"],
    "api-plan": [
        "plans/api-plan.md",
        "plans/api-test-data-plan.md",
        "plans/api-codegen-plan.md",
        "plans/m3-review-summary.md",
    ],
    "api-plan-review": ["review/api-plan-review.json"],
    "api-codegen": ["codegen/api-codegen-summary.md"],
    "inspect": ["inspect/failure-analysis.json", "inspect/quality-gate-result.json"],
    "report": ["report/quality-report.json", "report/executive-summary.md"],
}


def _write(change_dir: Path, rel: str) -> None:
    path = change_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if rel in _JSON:
        path.write_text(json.dumps(_JSON[rel]), encoding="utf-8")
    else:
        path.write_text(_TEXT[rel], encoding="utf-8")


class WritingAdapter:
    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.dispatched: list[str] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.dispatched.append(request.phase_id)
        for rel in _PHASE_PRODUCES.get(request.phase_id, []):
            _write(self.change_dir, rel)
        return PhaseResult(ok=True, output="ok")


class HappyPathCli(DefaultCliPhaseExecutor):
    def run_cli_phase(self, entry, ctx) -> PhaseResult:  # noqa: ANN001
        if entry.phase_id == "execution":
            manifest = ctx.change_dir / "execution" / "execution-manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("batch_id: b1\n", encoding="utf-8")
            return PhaseResult(ok=True, output="ran")
        return PhaseResult(ok=False, error=f"unexpected cli phase {entry.phase_id}")


class InProcessHealing(DefaultHealingActionExecutor):
    def execute(self, action: HealingEpisodeAction, ctx) -> PhaseResult:  # noqa: ANN001
        if action.kind == "complete" and action.outcome:
            try:
                record_heal_transition(ctx.change_dir, action.outcome)
            except Exception as err:  # noqa: BLE001
                return PhaseResult(ok=False, error=str(err))
            return PhaseResult(ok=True, output=action.outcome)
        return super().execute(action, ctx)


def _bootstrap(tmp_path: Path) -> Path:
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text("tables: []\n", encoding="utf-8")
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "run_context:\n  interaction_mode: autonomous\nphases:\n  skill_registry_check:\n    status: pass\n",
        encoding="utf-8",
    )
    return change_dir


def test_packaged_happy_path_reaches_completed(tmp_path: Path) -> None:
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)
    schema = load_workflow_schema(tmp_path)

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="full",
        adapter=adapter,
        params={"test_types": ["api"]},
        schema=schema,
        cli_executor=HappyPathCli(schema=schema),
        healing_executor=InProcessHealing(),
        max_iterations=40,
    )

    assert result.exit_code == EXIT_COMPLETED, result.reason
    assert read_state(change_dir).phases.healing.status == "not_needed"
    assert (change_dir / "report" / "quality-report.json").is_file()
    assert "api-codegen" in adapter.dispatched
    assert "report" in adapter.dispatched
    assert not any("e2e" in pid or "fuzz" in pid or "performance" in pid for pid in adapter.dispatched)
