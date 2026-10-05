"""Inspect: classify the current execution batch against the locked assessment."""

from __future__ import annotations

from agent_runtime_contracts.ops import Agent, Finalize, Out, OutputError, Prepare

from assurance_quality.contracts.agent import InspectionResultV1
from assurance_intake.contracts.coverage_rework import COVERAGE_REWORK_HANDOFF_PATH
from assurance_quality.contracts.assessment import (
    INSPECTION_OUTCOME_PATH,
    InspectBoundInputV1,
    InspectPublishedV1,
)
from assurance_quality.ops import router
from assurance_quality.ops.inspect import hooks

op = router.agent(
    "inspect",
    input=InspectBoundInputV1,
    prepare=Prepare(hook=hooks.before, errors=(OutputError,)),
    agent=Agent(
        profile="assurance-v1-reviewer",
        skill="aa-inspect",
        result=InspectionResultV1,
        writes=(Out("inspection", hooks.PATH, model=InspectionResultV1, format="json"),),
        strict_files=True,
    ),
    finalize=Finalize(
        hook=hooks.after,
        writes=(
            Out("inspection-outcome", INSPECTION_OUTCOME_PATH),
            Out("coverage-rework-handoff", COVERAGE_REWORK_HANDOFF_PATH),
        ),
        same=("change_id",),
    ),
    output=InspectPublishedV1,
)

__all__ = ["op"]
