"""Report: write the current batch's quality report from the locked inspection."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, OutputError, Prepare

from assurance_quality.contracts.agent import ReportResultV1
from assurance_quality.contracts.assessment import FinalizedReportV1, ReportSkillInputV1
from assurance_quality.ops import router
from assurance_quality.ops.report import hooks

op = router.agent(
    "report",
    input=ReportSkillInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reporter",
        skill="aa-report-generator",
        result=ReportResultV1,
        writes=("qa/results/report/report.md",),
    ),
    finalize=Finalize(hook=hooks.after),
    output=FinalizedReportV1,
)

__all__ = ["op"]
