"""Report: write the current batch's quality report from the locked inspection."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, OutputError, Prepare

from assurance_quality.contracts.agent import ReportResultV1
from assurance_quality.contracts.assessment import (
    REPORT_OUTCOME_PATH,
    ReportPublishedV1,
    ReportBoundInputV1,
)
from assurance_quality.ops import router
from assurance_quality.ops.report import hooks

op = router.agent(
    "report",
    input=ReportBoundInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reporter",
        skill="aa-report-generator",
        result=ReportResultV1,
        writes=(Out("report", hooks.PATH),),
        strict_files=True,
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(Out("report-outcome", REPORT_OUTCOME_PATH),),
        same=("change_id", "purpose"),
    ),
    output=ReportPublishedV1,
)

__all__ = ["op"]
