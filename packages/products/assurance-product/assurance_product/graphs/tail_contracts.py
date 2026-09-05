"""Internal execute-tail input and terminal result contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_product.models import BusinessBudgetsV1, ResourceRefV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1


class ExecuteTailInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    requirement: str = Field(min_length=1)
    run_mode: Literal["case", "implement", "verify"]
    coverage_epoch: int = Field(ge=0)
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
        "coverage_insufficient",
        "repairable_execution_failure",
        "needs_human",
        "blocked",
    ]
    inspection: InspectionOutcomeV1 | None = None
    report_refs: tuple[EvidenceArtifactRefV1, ...] = ()
    report_receipt: ReceiptRef | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _terminal_evidence_matches_status(self) -> Self:
        if self.status == "reported":
            if (
                self.inspection is None
                or self.inspection.disposition != "satisfied"
                or not self.report_refs
                or self.report_receipt is None
            ):
                raise ValueError("reported tail requires satisfied Inspect and committed Report")
        elif self.status == "coverage_insufficient":
            if self.inspection is None or self.inspection.disposition != "coverage_insufficient":
                raise ValueError("coverage_insufficient tail requires matching Inspect evidence")
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("coverage retry cannot publish an intermediate Report")
        elif self.status in {"repairable_execution_failure", "needs_human"}:
            if self.inspection is None or self.inspection.disposition != self.status:
                raise ValueError(f"{self.status} tail requires matching Inspect evidence")
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("unresolved execution result cannot publish a Report")
        else:
            if self.report_refs or self.report_receipt is not None:
                raise ValueError("blocked tail cannot carry successful report evidence")
            if not self.reason:
                raise ValueError("blocked tail requires a reason")
        return self


__all__ = ["ExecuteTailInputV1", "ExecuteTailResultV1"]
