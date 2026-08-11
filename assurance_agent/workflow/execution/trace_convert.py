"""Convert runner TargetResult / PerformanceResult into evidence fold DTOs."""

from __future__ import annotations

from assurance_agent.evidence.trace import (
    EvidenceUnmappedTest,
    PerformanceResultDocument,
    PerformanceScenarioRow,
    ResultDocument,
    ResultTestRow,
)
from assurance_agent.workflow.execution.results import PerformanceResult, TargetResult


def result_document_from_target(result: TargetResult) -> ResultDocument:
    return ResultDocument(
        change_id=result.change_id,
        batch_id=result.batch_id,
        target=result.target,
        cases=[
            ResultTestRow(
                case_id=row.case_id,
                status=row.status,
                file=row.file,
                test_name=row.test_name,
            )
            for row in result.cases
        ],
        unmapped_tests=[
            EvidenceUnmappedTest(file=row.file, test_name=row.test_name) for row in result.unmapped_tests
        ],
    )


def performance_document_from_result(result: PerformanceResult) -> PerformanceResultDocument:
    return PerformanceResultDocument(
        change_id=result.change_id,
        batch_id=result.batch_id,
        kind="performance",
        scenarios=[
            PerformanceScenarioRow(capability=row.capability, verdict=row.verdict) for row in result.scenarios
        ],
    )
