"""Deterministic agent test double used by fixture-backed Eval suites."""

from __future__ import annotations

from pydantic import ValidationError

from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult


class FixtureBackedFakeAdapter:
    """Skip model calls while completing receipts required by live graph nodes.

    Most outputs are supplied by the seeded fixture. Coverage Repair is an
    in-change loop allocated after the imported execution checkpoint, so its
    attempt token is minted dynamically and cannot be stored in the fixture.
    Emit a truthful no-op receipt bound to that token; deterministic operations
    still compute safety, rerun evidence, and enforce the attempt budget.
    """

    def invoke(self, request: AgentRequest) -> AgentResult:
        if request.target != "skill:aa-coverage-repair":
            return AgentResult(ok=True)

        change_dir = request.workspace_root / "qa" / "changes" / request.change_id
        baseline_path = change_dir / COVERAGE_REPAIR_BASELINE_REL
        try:
            baseline = CoverageRepairBaseline.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError) as exc:
            return AgentResult(
                ok=False,
                error_kind="invalid_input",
                error=f"fixture-backed coverage repair requires a valid baseline: {exc}",
            )

        summary = CoverageRepairApplySummary(
            change_id=request.change_id,
            attempt=baseline.attempt,
            attempt_token=baseline.attempt_token,
            applied=False,
            notes="AA_EVAL_FAKE_ADAPTER deterministic no-op",
        )
        output = change_dir / COVERAGE_REPAIR_APPLY_SUMMARY_REL
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return AgentResult(ok=True)


__all__ = ["FixtureBackedFakeAdapter"]
