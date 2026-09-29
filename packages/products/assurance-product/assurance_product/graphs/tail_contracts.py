"""Internal execute-tail input and terminal result contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan
from assurance_product.models import BusinessBudgetsV1, ResourceRefV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1, ReportOutcomeV1


class ExecuteTailInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    run_mode: Literal["case", "implement", "verify"]
    coverage_epoch: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    reviewed_case: ReviewedCaseV1 | None = None
    source_artifacts: tuple[EvidenceArtifactRefV1, ...] = ()
    selected_test_families: tuple[Literal["api", "e2e", "fuzz", "performance"], ...]
    capability_leafs: tuple[str, ...]
    capability_catalog: ResourceRefV1
    product_policy: ResourceRefV1
    data_knowledge: ResourceRefV1
    allowed_artifact_paths: tuple[str, ...]
    budgets: BusinessBudgetsV1
    decision: str
    ui_exploration_ref: EvidenceArtifactRefV1 | None = None
    api_discovery_ref: EvidenceArtifactRefV1 | None = None

    @model_validator(mode="after")
    def _reviewed_case_matches_cycle(self) -> Self:
        if self.reviewed_case is not None:
            if self.reviewed_case.change_id != self.change_id:
                raise ValueError("tail Reviewed Case change_id must match")
            if self.reviewed_case.coverage_epoch != self.coverage_epoch:
                raise ValueError("tail Reviewed Case epoch must match")
        return self


class ExecuteTailResultV1(FrozenModel):
    status: Literal[
        "reported",
        "diagnostic",
        "coverage_insufficient",
        "repairable_execution_failure",
        "needs_human",
        "blocked",
    ]
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    inspection: InspectionOutcomeV1 | None = None
    issue_analysis_ref: EvidenceArtifactRefV1 | None = None
    report: ReportOutcomeV1 | None = None
    report_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    report_receipt: ReceiptRef | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _terminal_evidence_matches_status(self) -> Self:
        if self.inspection is not None:
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.inspection.plan_digest,
                self.inspection.plan_ref,
            )
        if self.status == "reported":
            if (
                self.inspection is None
                or self.inspection.disposition != "satisfied"
                or self.report is None
                or not self.report_refs
                or self.report_receipt is None
            ):
                raise ValueError("reported tail requires satisfied Inspect and committed Report")
            if (
                self.inspection.change_id,
                self.inspection.coverage_epoch,
                self.inspection.batch_id,
                self.inspection.inspection_receipt,
            ) != (
                self.report.change_id,
                self.report.coverage_epoch,
                self.report.batch_id,
                self.report.inspection_receipt,
            ):
                raise ValueError("report does not describe the current inspection")
            if (
                self.report.report_refs != self.report_refs
                or self.report.report_receipt != self.report_receipt
            ):
                raise ValueError("reported tail evidence does not match the Report outcome")
            require_same_plan(
                self.plan_digest,
                self.plan_ref,
                self.report.plan_digest,
                self.report.plan_ref,
            )
        elif self.status == "diagnostic":
            if self.inspection is None or self.inspection.disposition not in {"blocked", "analysis_required"}:
                raise ValueError("diagnostic tail requires a failed Inspect outcome")
            if not self.report_refs or self.report_receipt is None:
                raise ValueError("diagnostic tail requires committed diagnostic report evidence")
        elif self.status == "coverage_insufficient":
            if self.inspection is None or self.inspection.disposition != "coverage_insufficient":
                raise ValueError("coverage_insufficient tail requires matching Inspect evidence")
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("coverage retry cannot publish an intermediate Report")
        elif self.status in {"repairable_execution_failure", "needs_human"}:
            analyzed_failure = (
                self.status == "needs_human"
                and self.inspection is not None
                and self.inspection.disposition in {"analysis_required", "blocked"}
                and self.issue_analysis_ref is not None
                and bool(self.reason)
            )
            if self.inspection is None or (
                self.inspection.disposition != self.status and not analyzed_failure
            ):
                raise ValueError(f"{self.status} tail requires matching Inspect evidence")
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("unresolved execution result cannot publish a Report")
        else:
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("blocked tail cannot carry successful report evidence")
            if not self.reason:
                raise ValueError("blocked tail requires a reason")
        if self.status != "reported" and self.report is not None:
            raise ValueError("non-reported tail cannot carry a normal Report outcome")
        return self


def reported_tail_result(
    inspection: InspectionOutcomeV1,
    report: ReportOutcomeV1,
) -> ExecuteTailResultV1:
    if (inspection.coverage_epoch, inspection.batch_id, inspection.inspection_receipt) != (
        report.coverage_epoch,
        report.batch_id,
        report.inspection_receipt,
    ) or inspection.change_id != report.change_id:
        raise ValueError("report does not describe the current inspection")
    if inspection.disposition != "satisfied":
        raise ValueError("normal report requires satisfied inspection")
    return ExecuteTailResultV1(
        status="reported",
        plan_digest=report.plan_digest,
        plan_ref=report.plan_ref,
        inspection=inspection,
        report=report,
        report_refs=report.report_refs,
        report_receipt=report.report_receipt,
    )


def diagnostic_tail_result(
    inspection: InspectionOutcomeV1,
    report_refs: tuple[EvidenceArtifactRefV1, ...],
    report_receipt: ReceiptRef,
) -> ExecuteTailResultV1:
    return ExecuteTailResultV1(
        status="diagnostic",
        plan_digest=inspection.plan_digest,
        plan_ref=inspection.plan_ref,
        inspection=inspection,
        report_refs=report_refs,
        report_receipt=report_receipt,
    )


__all__ = [
    "ExecuteTailInputV1",
    "ExecuteTailResultV1",
    "diagnostic_tail_result",
    "reported_tail_result",
]
