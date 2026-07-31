"""Declared artifact path -> pydantic model registry (spec 4a).

Globs mirror ARTIFACT_SPECS in the TS source src/schema/index.ts, plus
healing/fixer-safety-check.json (read by fixer-safety-gate; the plan-series
contract lists SafetyCheck as a core model). Glob semantics: `*` matches
within one path segment, `**/` matches zero or more segments — same effective
behavior micromatch gave the TS globs. `free`-grade artifacts (markdown
reports, events extensions) are deliberately absent: they are not validated.
"""

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    Advisory,
    ApplySummary,
    CaseYaml,
    ChangeIssueSnapshot,
    DataKnowledgeProposal,
    EvalEvidenceSlice,
    EvalRunProjection,
    ExecutionManifest,
    FactBaseline,
    FailureAnalysis,
    FixProposal,
    IssueAnalysisStatus,
    IssueCandidateDocument,
    IssueEvidenceManifest,
    IssueEvidenceSlice,
    IssueReconcileStatusDocument,
    ImprovementCandidateDocumentDraftV3,
    ImprovementCandidateDocumentV3,
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewBatchSummary,
    ImprovementAutoReviewStatus,
    ImprovementReviewSubject,
    ImprovementOutboxEntry,
    ObservationDocument,
    QaYaml,
    QualityGateResultDocument,
    QualityReport,
    PlanReview,
    PlanReviewAuthoring,
    TraceProjectionDocument,
    RetroContextV3,
    RetroPipelineFailureDocument,
    RetroRunStatus,
    RetroWindow,
    Review,
    SafetyCheck,
    SignalDocumentV3,
    SignalDraftDocument,
    WorkflowEvidenceSlice,
    WorkflowState,
)
from assurance_agent.artifacts.models.plan_checks import PlanCheckDocument

Compat = Literal["must_compat", "versioned", "free"]


class ArtifactSpec(BaseModel):
    artifact_type: str
    pattern: str
    model: type[BaseModel]
    compat: Compat
    # 落盘校验模型与 agent 撰写契约不一致时（如 runtime 回填字段），
    # prompt 渲染必须用撰写契约，否则会要求模型输出 runtime 自己插入的字段。
    authoring_model: type[BaseModel] | None = None


REGISTRY: list[ArtifactSpec] = [
    ArtifactSpec(
        artifact_type="improvement_review_subject_v1",
        pattern="qa/improvements/review-subjects/*.json",
        model=ImprovementReviewSubject,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="improvement_auto_review_assessment_v1",
        pattern="qa/improvements/reviews/*/assessment.json",
        model=ImprovementAutoReviewAssessment,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="improvement_auto_review_status_v1",
        pattern="qa/improvements/reviews/*/status.json",
        model=ImprovementAutoReviewStatus,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="improvement_auto_review_batch_summary_v1",
        pattern="qa/retro/*/auto-review-summary.json",
        model=ImprovementAutoReviewBatchSummary,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="improvement_reconcile_outbox_v1",
        pattern="qa/improvements/outbox/pending/*.json",
        model=ImprovementOutboxEntry,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="eval_run_projection_v1",
        pattern="qa/eval/runs/*/report.json",
        model=EvalRunProjection,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="improvement_candidate_document",
        pattern="qa/retro/*/proposal-candidates.json",
        model=ImprovementCandidateDocumentV3,
        compat="must_compat",
        authoring_model=ImprovementCandidateDocumentDraftV3,
    ),
    ArtifactSpec(
        artifact_type="retro_context_v3",
        pattern="qa/retro/*/context.json",
        model=RetroContextV3,
        compat="must_compat",
    ),
    # Retro v3 current-run window and typed evidence slices.
    ArtifactSpec(
        artifact_type="retro_window_v3",
        pattern="qa/retro/*/window.json",
        model=RetroWindow,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="retro_pipeline_failure_v1",
        pattern="qa/retro/*/pipeline-failure.json",
        model=RetroPipelineFailureDocument,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="retro_run_status_v1",
        pattern="qa/retro/*/retro-status.json",
        model=RetroRunStatus,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="retro_issue_evidence_slice_v3",
        pattern="qa/retro/*/evidence/issue-slice.json",
        model=IssueEvidenceSlice,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="retro_workflow_evidence_slice_v3",
        pattern="qa/retro/*/evidence/workflow-slice.json",
        model=WorkflowEvidenceSlice,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="retro_eval_evidence_slice_v3",
        pattern="qa/retro/*/evidence/eval-slice.json",
        model=EvalEvidenceSlice,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="retro_issue_signal_v3",
        pattern="qa/retro/*/signals/issue.json",
        model=SignalDocumentV3,
        compat="must_compat",
        authoring_model=SignalDraftDocument,
    ),
    ArtifactSpec(
        artifact_type="retro_workflow_signal_v3",
        pattern="qa/retro/*/signals/workflow.json",
        model=SignalDocumentV3,
        compat="must_compat",
        authoring_model=SignalDraftDocument,
    ),
    ArtifactSpec(
        artifact_type="retro_eval_signal_v3",
        pattern="qa/retro/*/signals/eval.json",
        model=SignalDocumentV3,
        compat="must_compat",
        authoring_model=SignalDraftDocument,
    ),
    ArtifactSpec(
        artifact_type="case_yaml", pattern="cases/**/case.yaml", model=CaseYaml, compat="must_compat"
    ),
    ArtifactSpec(artifact_type="qa_yaml", pattern=".qa.yaml", model=QaYaml, compat="must_compat"),
    ArtifactSpec(
        artifact_type="execution_manifest",
        pattern="execution/execution-manifest.yaml",
        model=ExecutionManifest,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="failure_analysis",
        pattern="inspect/failure-analysis.json",
        model=FailureAnalysis,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="quality_gate_result",
        pattern="inspect/quality-gate-result.json",
        model=QualityGateResultDocument,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="trace_projection",
        pattern="inspect/trace-projection.json",
        model=TraceProjectionDocument,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="observation_document",
        pattern="inspect/observations.json",
        model=ObservationDocument,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="issue_evidence_manifest",
        pattern="inspect/issue-evidence-manifest.json",
        model=IssueEvidenceManifest,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="issue_candidate_document",
        pattern="inspect/issue-candidates.json",
        model=IssueCandidateDocument,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="issue_analysis_status",
        pattern="inspect/issue-analysis-status.json",
        model=IssueAnalysisStatus,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="issue_reconcile_status",
        pattern="inspect/issue-reconcile-status.json",
        model=IssueReconcileStatusDocument,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="change_issue_snapshot",
        pattern="issues/snapshot.json",
        model=ChangeIssueSnapshot,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="quality_report",
        pattern="report/quality-report.json",
        model=QualityReport,
        compat="versioned",
    ),
    ArtifactSpec(
        artifact_type="fix_proposal",
        pattern="healing/fix-proposal.json",
        model=FixProposal,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="apply_summary",
        pattern="healing/*-apply-summary.json",
        model=ApplySummary,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="safety_check",
        pattern="healing/fixer-safety-check.json",
        model=SafetyCheck,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="plan_check",
        pattern="review/*-plan-checks.json",
        model=PlanCheckDocument,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="review",
        pattern="review/api-plan-review.json",
        model=PlanReview,
        compat="must_compat",
        authoring_model=PlanReviewAuthoring,
    ),
    ArtifactSpec(
        artifact_type="review",
        pattern="review/plan-review.json",
        model=PlanReview,
        compat="must_compat",
        authoring_model=PlanReviewAuthoring,
    ),
    ArtifactSpec(
        artifact_type="review",
        pattern="review/fuzz-plan-review.json",
        model=PlanReview,
        compat="must_compat",
        authoring_model=PlanReviewAuthoring,
    ),
    ArtifactSpec(
        artifact_type="review",
        pattern="review/performance-plan-review.json",
        model=PlanReview,
        compat="must_compat",
        authoring_model=PlanReviewAuthoring,
    ),
    ArtifactSpec(artifact_type="review", pattern="review/*.json", model=Review, compat="must_compat"),
    ArtifactSpec(
        artifact_type="fact_baseline",
        pattern="facts/fact-baseline.json",
        model=FactBaseline,
        compat="must_compat",
    ),
    ArtifactSpec(
        artifact_type="advisory", pattern="explore/advisory.json", model=Advisory, compat="must_compat"
    ),
    ArtifactSpec(
        artifact_type="workflow_state", pattern="workflow-state.yaml", model=WorkflowState, compat="versioned"
    ),
    ArtifactSpec(
        artifact_type="data_knowledge_proposal",
        pattern="plans/data-knowledge.proposal.*.yaml",
        model=DataKnowledgeProposal,
        compat="versioned",
    ),
]


@lru_cache(maxsize=None)
def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def match_artifact(relpath: str) -> ArtifactSpec | None:
    """Return the first registry spec whose glob matches its root-relative path."""
    norm = relpath.replace("\\", "/")
    for spec in REGISTRY:
        if _pattern_regex(spec.pattern).match(norm):
            return spec
    return None
