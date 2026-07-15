"""End-to-end happy path over the *packaged* workflow schema.

Regression guard for the healing-overlay bug: on the no-healing-needed path the
orchestrator must record `not_needed` so `report.ready_when` unblocks and the DAG
reaches `completed`. Earlier driver tests used synthetic gateless schemas plus a
scripted status provider, which masked this. This test drives the real
`compute_status` projection of the shipped schema through `run_workflow_loop`,
with an adapter that writes gate-passing artifacts and the real `aa state apply` /
`aa state heal` commands run in-process.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from click.testing import CliRunner

from assurance_agent.cli import main
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.driver.adapter import PhaseRequest, PhaseResult
from assurance_agent.workflow.driver.loop import EXIT_COMPLETED, run_workflow_loop
from assurance_agent.workflow.orchestration.healing_episode import HealingEpisodeAction


@contextlib.contextmanager
def _chdir(path: Path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


# Gate-passing produces for each dispatched skill phase (api-only slice of the
# packaged schema; e2e/fuzz/performance branches prune via test_types).
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

# phase_id -> relative produces this phase writes.
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
    """Simulates an agent: writes each skill phase's gate-passing produces."""

    def __init__(self, change_dir: Path) -> None:
        self.change_dir = change_dir
        self.dispatched: list[str] = []

    def run_phase(self, request: PhaseRequest) -> PhaseResult:
        self.dispatched.append(request.phase_id)
        for rel in _PHASE_PRODUCES.get(request.phase_id, []):
            _write(self.change_dir, rel)
        return PhaseResult(ok=True, output="ok")


class InProcessCli:
    """cli_executor: writes the `execution` manifest and runs the REAL aa commands."""

    def run_cli_phase(self, entry, ctx) -> PhaseResult:  # noqa: ANN001
        if entry.phase_id == "execution":
            manifest = ctx.change_dir / "execution" / "execution-manifest.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("batch_id: b1\n", encoding="utf-8")
            return PhaseResult(ok=True, output="ran")
        return PhaseResult(ok=False, error=f"unexpected cli phase {entry.phase_id}")

    def apply_phase_state(self, entry, ctx, attempt_id) -> PhaseResult:  # noqa: ANN001
        args = [
            "state",
            "apply",
            "--change",
            ctx.change_id,
            "--phase",
            entry.phase_id,
            "--attempt-id",
            attempt_id,
        ]
        if entry.skill:
            args += ["--skill", entry.skill]
        return _invoke(ctx.project_root, args)


class InProcessHealing:
    """healing_executor: records the healing judgment via the REAL aa state heal."""

    def execute(self, action: HealingEpisodeAction, ctx) -> PhaseResult:  # noqa: ANN001
        if action.kind == "complete" and action.outcome:
            return _invoke(
                ctx.project_root,
                ["state", "heal", "--change", ctx.change_id, "--status", action.outcome],
            )
        return PhaseResult(ok=False, error=f"unexpected healing action {action.kind}")


def _invoke(project_root: Path, args: list[str]) -> PhaseResult:
    with _chdir(project_root):
        res = CliRunner().invoke(main, args, catch_exceptions=False)
    code = 0 if res.exit_code is None else res.exit_code
    if code != 0:
        return PhaseResult(ok=False, output=res.output, error=res.output[:500])
    return PhaseResult(ok=True, output=res.output)


def _bootstrap(tmp_path: Path) -> Path:
    # `aa init`-equivalent seed: run_context (case-design-gate) + registry pass.
    (tmp_path / ".aa").mkdir()
    (tmp_path / ".aa" / "data-knowledge.yaml").write_text("tables: []\n", encoding="utf-8")
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    (change_dir / "workflow-state.yaml").write_text(
        "run_context:\n"
        "  interaction_mode: autonomous\n"
        "phases:\n"
        "  skill_registry_check:\n"
        "    status: pass\n",
        encoding="utf-8",
    )
    return change_dir


def test_packaged_happy_path_reaches_completed(tmp_path: Path) -> None:
    change_dir = _bootstrap(tmp_path)
    adapter = WritingAdapter(change_dir)

    result = run_workflow_loop(
        project_root=tmp_path,
        change_id="CH-1",
        scope="full",
        adapter=adapter,
        params={"test_types": ["api"]},
        cli_executor=InProcessCli(),
        healing_executor=InProcessHealing(),
        max_iterations=40,
    )

    assert result.exit_code == EXIT_COMPLETED, result.reason
    # Healing was not needed and must be recorded so report could unblock.
    assert read_state(change_dir).phases.healing.status == "not_needed"
    # Report actually produced its artifacts (DAG truly completed, not short-circuited).
    assert (change_dir / "report" / "quality-report.json").is_file()
    # The api slice ran; e2e/fuzz/performance never dispatched.
    assert "api-codegen" in adapter.dispatched
    assert "report" in adapter.dispatched
    assert not any("e2e" in pid or "fuzz" in pid or "performance" in pid for pid in adapter.dispatched)
