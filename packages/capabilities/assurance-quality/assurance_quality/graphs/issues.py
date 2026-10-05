"""Issue review, analysis, and reconcile as single-step flows."""

from __future__ import annotations

from typing import Literal

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.flow import BoundFlow, Flow

from assurance_quality.contracts.agent import IssueAnalysisBoundInputV1, ReconcileBoundInputV1
from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_quality.ops.issue_analysis import op as issue_analysis
from assurance_quality.ops.issue_triage import op as issue_triage

_ISSUE_RECONCILE = TASK_ATTEMPT_CONTRACTS["reconcile-issues"]


class IssueFlowInput(IssueAnalysisBoundInputV1):
    """Parent fields the issue ops read. Prepare opens the refs."""


class ReconcileFlowInput(ReconcileBoundInputV1):
    """Reconcile classifies the analysis the previous step already published."""


def _review(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "issue-review",
        input=IssueFlowInput,
        outcomes=("fix_eligible", "report_issue", "unclassified", "failed"),
    )
    flow.step(
        "issue-review",
        issue_triage,
        on_failure="failed",
        route_on="route",
        routes={
            "fix_eligible": "fix_eligible",
            "report_issue": "report_issue",
            "unclassified": "unclassified",
        },
        inputs={"execution_digest": "execution_evidence_digest"},
    )
    return flow.bind(context)


def _analyze(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow(
        "issue-analyze",
        input=IssueFlowInput,
        outcomes=("fix_eligible", "report_issue", "unclassified", "failed"),
    )
    flow.step(
        "issue-analyze",
        issue_analysis,
        on_failure="failed",
        route_on="route",
        routes={
            "fix_eligible": "fix_eligible",
            "report_issue": "report_issue",
            "unclassified": "unclassified",
            "failed": "failed",
        },
    )
    return flow.bind(context)


def _reconcile(context: CapabilityBuildContext) -> BoundFlow:
    flow = Flow("issue-reconcile", input=ReconcileFlowInput, outcomes=("ready", "failed"))
    flow.step(
        "issue-reconcile",
        _ISSUE_RECONCILE,
        on_failure="failed",
        then="ready",
    )
    return flow.bind(context)


def build_issue_graph(
    context: CapabilityBuildContext,
    *,
    export: Literal["issue-review", "issue-analyze", "issue-reconcile"],
) -> BoundFlow:
    if export == "issue-review":
        return _review(context)
    if export == "issue-analyze":
        return _analyze(context)
    return _reconcile(context)


__all__ = ["build_issue_graph"]
