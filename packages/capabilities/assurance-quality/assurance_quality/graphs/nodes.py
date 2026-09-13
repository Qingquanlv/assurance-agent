from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import canonical_digest

from assurance_quality.contracts.agent import FinalizedIssueAnalysisV1, QualitySkillInputV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    AssessmentSkillInputV1,
    FactBaselineSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    FinalizedReportV1,
    InspectionOutcomeV1,
    MaterializeAssessmentInputV1,
    ReportOutcomeV1,
    ReportSkillInputV1,
)
from assurance_quality.contracts.coverage import classify_coverage_state
from assurance_quality.contracts.decisions import (
    IssueAnalysisPublicV1,
    classify_inspection_disposition,
    classify_issue_candidates,
)
from assurance_quality.graphs.state import (
    QualityAssessPublicV1,
    QualityIssuePublicV1,
    QualityReportPublicV1,
    QualityState,
)

activation_one_shot = BusinessActivation.one_shot()


def activation_issue_analysis(state: Mapping[str, object]) -> BusinessActivation:
    business = select_quality(state)
    if not business.evidence_bundle_digest:
        raise ValueError("issue analysis requires an evidence bundle identity")
    return BusinessActivation.for_trigger(
        canonical_digest(
            {
                "change_id": business.change_id,
                "batch_id": business.batch_id,
                "evidence_bundle_digest": business.evidence_bundle_digest,
            }
        )
    )


_SKILL_DIGESTS = (
    "healing_digest",
    "trace_digest",
    "coverage_digest",
    "metrics_digest",
    "case_digest",
    "plan_digest",
    "plan_ref",
    "mapping_digest",
    "issue_digest",
)


def _output_payload(output: object) -> dict[str, object]:
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json")
    if isinstance(output, Mapping):
        return {str(name): value for name, value in output.items()}
    raise TypeError("attempt output must be a mapping")


def _as_refs(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("evidence refs must be a list")
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("evidence ref must be a mapping")
        path = item.get("path")
        digest = item.get("digest")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise TypeError("evidence ref requires path and digest")
        refs.append({"path": path, "digest": digest})
    return refs


def _published_int(payload: Mapping[str, object], key: str, fallback: object) -> int:
    value = payload.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an int")
    return value


def _skill_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {
        "change_id": state["change_id"],
        "batch_id": state["batch_id"],
        "capability_leafs": state["capability_leafs"],
        "artifact_paths": state.get("artifact_paths") or state["allowed_artifact_paths"],
        "owned_evidence_ids": state.get("owned_evidence_ids", ()),
        "evidence_bundle_digest": state.get("evidence_bundle_digest"),
        # Artifact bytes and the Product terminal execution object have different
        # digests. Only the artifact digest belongs in the Agent evidence input.
        "execution_digest": state.get("execution_evidence_digest"),
        **{name: state.get(name) for name in _SKILL_DIGESTS},
    }


def select_quality(state: Mapping[str, object]) -> QualitySkillInputV1:
    return QualitySkillInputV1.model_validate(_skill_payload(state))


def _assessment_skill_input(
    state: Mapping[str, object], *, require_fact_baseline: bool
) -> AssessmentSkillInputV1:
    assessment = AssessmentInputsV1.model_validate(state.get("assessment_inputs"))
    reviewed_case = ReviewedCaseV1.model_validate(state.get("reviewed_case"))
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    baseline_raw = state.get("fact_baseline_ref")
    baseline = EvidenceArtifactRefV1.model_validate(baseline_raw) if baseline_raw is not None else None
    if require_fact_baseline and baseline is None:
        raise ValueError("Inspect requires the committed fact baseline")
    return AssessmentSkillInputV1.model_validate(
        {
            "change_id": state.get("change_id"),
            "plan_digest": state.get("plan_digest"),
            "plan_ref": state.get("plan_ref"),
            "coverage_epoch": state.get("coverage_epoch"),
            "batch_id": state.get("batch_id"),
            "capability_leafs": state.get("capability_leafs", ()),
            "artifact_paths": state.get("artifact_paths") or state.get("allowed_artifact_paths", ()),
            "assessment": assessment,
            "reviewed_case": reviewed_case,
            "mapping_ref": generation.mapping_ref,
            "fact_baseline_ref": baseline,
        }
    )


def select_fact_baseline(state: Mapping[str, object]) -> FactBaselineSkillInputV1:
    reviewed_case = ReviewedCaseV1.model_validate(state.get("reviewed_case"))
    return FactBaselineSkillInputV1.model_validate(
        {
            "change_id": state.get("change_id"),
            "coverage_epoch": state.get("coverage_epoch"),
            "plan_digest": state.get("plan_digest") or reviewed_case.plan_digest,
            "plan_ref": state.get("plan_ref") or reviewed_case.plan_ref,
            "capability_leafs": state.get("capability_leafs", ()),
            "artifact_paths": state.get("artifact_paths") or state.get("allowed_artifact_paths", ()),
            "reviewed_case": reviewed_case,
        }
    )


def select_inspect(state: Mapping[str, object]) -> AssessmentSkillInputV1:
    return _assessment_skill_input(state, require_fact_baseline=True)


def select_materialize_assessment(state: Mapping[str, object]) -> MaterializeAssessmentInputV1:
    return MaterializeAssessmentInputV1.model_validate(
        {
            "plan_digest": state.get("plan_digest"),
            "plan_ref": state.get("plan_ref"),
            "reviewed_case": state.get("reviewed_case"),
            "generation": state.get("generation_result"),
            "execution": state.get("execution_result"),
            "policy_resource_id": state.get("policy_resource_id"),
            "policy_sha256": state.get("policy_sha256"),
            "execution_at": state.get("execution_at"),
            "healing_ref": state.get("healing_ref"),
            "issue_ref": state.get("issue_ref"),
        }
    )


def select_report(state: Mapping[str, object]) -> QualitySkillInputV1:
    purpose = state.get("report_purpose", "normal")
    inspection = InspectionOutcomeV1.model_validate(state.get("inspection_outcome"))
    assessment = AssessmentInputsV1.model_validate(state.get("assessment_inputs"))
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    issue_analysis_raw = state.get("issue_analysis_ref")
    issue_analysis_ref = (
        EvidenceArtifactRefV1.model_validate(issue_analysis_raw) if issue_analysis_raw is not None else None
    )
    digests = {
        "execution_digest": assessment.execution_ref.digest,
        "healing_digest": assessment.healing_ref.digest if assessment.healing_ref is not None else None,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
        "case_digest": inspection.reviewed_case.review_ref.digest,
        "plan_digest": generation.plan_digest,
        "plan_ref": generation.plan_ref.model_dump(mode="json"),
        "mapping_digest": inspection.mapping_ref.digest,
        "issue_digest": (
            issue_analysis_ref.digest
            if issue_analysis_ref is not None
            else assessment.issue_ref.digest
            if assessment.issue_ref is not None
            else None
        ),
    }
    return ReportSkillInputV1.model_validate(
        {
            "change_id": state.get("change_id"),
            "batch_id": state.get("batch_id"),
            "capability_leafs": state.get("capability_leafs", ()),
            "artifact_paths": state.get("artifact_paths") or state.get("allowed_artifact_paths", ()),
            **digests,
            "coverage_epoch": state.get("coverage_epoch"),
            "purpose": purpose,
            "inspection": inspection,
            "assessment": assessment,
            "generation": generation,
            "fact_baseline_ref": state.get("fact_baseline_ref"),
            "issue_analysis_ref": issue_analysis_ref,
        }
    )


def activation_assess(state: Mapping[str, object]) -> BusinessActivation:
    raw = state.get("activation")
    if not isinstance(raw, Mapping):
        raise ValueError("assess business activation must be parent-supplied")
    kind = raw.get("kind")
    value = raw.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("assess business activation value is missing")
    if kind == "root":
        return BusinessActivation.one_shot()
    if kind == "round":
        return BusinessActivation.for_round(int(value))
    if kind == "trigger":
        return BusinessActivation.for_trigger(value)
    raise ValueError("assess business activation is not canonical")


def activation_fact_baseline(state: Mapping[str, object]) -> BusinessActivation:
    epoch = state.get("coverage_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("fact-baseline coverage_epoch must be a non-negative int")
    return BusinessActivation.for_trigger(f"coverage.{epoch}.fact-baseline")


def activation_materialize_assessment(state: Mapping[str, object]) -> BusinessActivation:
    epoch = state.get("coverage_epoch")
    batch_id = state.get("batch_id")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("assessment coverage_epoch must be a non-negative int")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("assessment batch_id must be non-empty")
    batch_token = canonical_digest(batch_id)[:16]
    return BusinessActivation.for_trigger(f"coverage.{epoch}.assessment.{batch_token}")


def publish_materialize_assessment(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del state, receipt
    assessment = AssessmentInputsV1.model_validate(output)
    return {
        "assessment_inputs": assessment.model_dump(mode="json"),
        "evidence_refs": [
            ref.model_dump(mode="json")
            for ref in (
                assessment.trace_ref,
                assessment.gaps_ref,
                assessment.metrics_ref,
                assessment.sufficiency_ref,
                assessment.execution_ref,
                assessment.observations_ref,
                assessment.issue_evidence_manifest_ref,
            )
        ],
    }


def publish_fact_baseline(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    finalized = FinalizedFactBaselineV1.model_validate(output)
    reviewed_case = ReviewedCaseV1.model_validate(state.get("reviewed_case"))
    if finalized.reviewed_case != reviewed_case:
        raise ValueError("fact baseline was finalized against a stale Reviewed Case")
    return {
        "fact_baseline_ref": finalized.fact_baseline_ref.model_dump(mode="json"),
    }


def publish_inspect(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    if state.get("attempt_failure"):
        raise ValueError("failed Inspect attempt cannot publish a business outcome")
    finalized = FinalizedInspectionV1.model_validate(output)
    assessment = AssessmentInputsV1.model_validate(state.get("assessment_inputs"))
    reviewed_case = ReviewedCaseV1.model_validate(state.get("reviewed_case"))
    generation = GenerationCycleResultV1.model_validate(state.get("generation_result"))
    if finalized.assessment != assessment:
        raise ValueError("inspection was finalized against stale assessment inputs")
    if finalized.reviewed_case != reviewed_case:
        raise ValueError("inspection was finalized against a stale Reviewed Case")
    if finalized.mapping_ref != generation.mapping_ref:
        raise ValueError("inspection was finalized against a stale test mapping")
    if finalized.fact_baseline_ref != EvidenceArtifactRefV1.model_validate(state.get("fact_baseline_ref")):
        raise ValueError("inspection was finalized against a stale fact baseline")
    if (
        state.get("change_id") != assessment.change_id
        or state.get("coverage_epoch") != assessment.coverage_epoch
        or state.get("batch_id") != assessment.batch_id
        or state.get("policy_sha256") != assessment.scope.policy_digest
    ):
        raise ValueError("inspection identity no longer matches the active assessment cycle")
    facts = finalized.failure_facts
    has_execution_problem = (
        not facts.identity_valid
        or facts.blocking_failure
        or facts.analysis_required
        or facts.needs_human
        or facts.repairable_failure
    )
    coverage_state = None
    if not has_execution_problem:
        coverage_state = classify_coverage_state(
            metrics=finalized.metrics,
            sufficiency=finalized.sufficiency,
            scope=finalized.assessment.scope,
            policy=finalized.assessment.policy,
        )
    disposition = classify_inspection_disposition(facts=facts, coverage_state=coverage_state)
    reason_codes = set(finalized.reason_codes)
    if coverage_state is not None:
        reason_codes.add(f"coverage.{coverage_state}")
    assessment_refs = tuple(
        sorted(
            (
                assessment.trace_ref,
                assessment.gaps_ref,
                assessment.metrics_ref,
                assessment.sufficiency_ref,
                assessment.execution_ref,
                assessment.observations_ref,
                assessment.issue_evidence_manifest_ref,
                *(() if assessment.healing_ref is None else (assessment.healing_ref,)),
                *(() if assessment.issue_ref is None else (assessment.issue_ref,)),
                finalized.fact_baseline_ref,
            ),
            key=lambda item: (item.path, item.digest),
        )
    )
    outcome = InspectionOutcomeV1(
        change_id=assessment.change_id,
        coverage_epoch=assessment.coverage_epoch,
        batch_id=assessment.batch_id,
        plan_digest=assessment.plan_digest,
        plan_ref=assessment.plan_ref,
        disposition=disposition,
        inspection_receipt=ReceiptRef.model_validate(receipt),
        reviewed_case=finalized.reviewed_case,
        mapping_ref=finalized.mapping_ref,
        assessment_refs=assessment_refs,
        reason_codes=tuple(sorted(reason_codes)),
        coverage_state=coverage_state,
    )
    return QualityAssessPublicV1(
        change_id=assessment.change_id,
        coverage_state=coverage_state,
        inspection_outcome=outcome,
        evidence_refs=[ref.model_dump(mode="json") for ref in assessment_refs],
        observations_ref=assessment.observations_ref,
        issue_evidence_manifest_ref=assessment.issue_evidence_manifest_ref,
        owned_evidence_ids=assessment.owned_evidence_ids,
        evidence_bundle_digest=assessment.evidence_bundle_digest,
        rounds_budget=_published_int({}, "rounds_budget", state.get("rounds_budget", 0)),
        rounds_used=_published_int({}, "rounds_used", state.get("rounds_used", 0)),
    ).model_dump(mode="json")


def publish_issue(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    issue = IssueAnalysisPublicV1.model_validate(
        {
            "classification": payload.get("classification"),
            "fix_eligible": payload.get("fix_eligible", False),
        }
    )
    change_id = state["change_id"]
    if not isinstance(change_id, str):
        raise TypeError("change_id must be a string")
    return QualityIssuePublicV1(
        change_id=change_id,
        classification=issue.classification,
        evidence_refs=_as_refs(payload.get("evidence_refs") or state.get("evidence_refs")),
        fix_eligible=issue.fix_eligible,
        rounds_budget=_published_int(payload, "rounds_budget", state.get("rounds_budget", 0)),
        rounds_used=_published_int(payload, "rounds_used", state.get("rounds_used", 0)),
    ).model_dump(mode="json")


def publish_issue_analysis(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    finalized = FinalizedIssueAnalysisV1.model_validate(output)
    summary = classify_issue_candidates(finalized.agent_result)
    if state.get("change_id") != finalized.agent_result.change_id:
        raise ValueError("issue analysis changed the active change identity")
    published = QualityIssuePublicV1(
        change_id=finalized.agent_result.change_id,
        classification=summary.classification,
        evidence_refs=[finalized.issue_analysis_ref.model_dump(mode="json")],
        fix_eligible=summary.fix_eligible,
        rounds_budget=_published_int({}, "rounds_budget", state.get("rounds_budget", 0)),
        rounds_used=_published_int({}, "rounds_used", state.get("rounds_used", 0)),
    ).model_dump(mode="json")
    return {**published, "issue_analysis": finalized.model_dump(mode="json")}


def publish_report(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    if state.get("attempt_failure"):
        raise ValueError("failed Report attempt cannot publish a business outcome")
    selected = ReportSkillInputV1.model_validate(select_report(state))
    finalized = FinalizedReportV1.model_validate(output)
    identity = (selected.change_id, selected.coverage_epoch, selected.batch_id)
    if identity != (finalized.change_id, finalized.coverage_epoch, finalized.batch_id):
        raise ValueError("report was finalized against a stale inspection cycle")
    if finalized.inspection_receipt != selected.inspection.inspection_receipt:
        raise ValueError("report was finalized against a stale inspection receipt")
    if finalized.purpose != selected.purpose:
        raise ValueError("report purpose changed after selection")
    committed = ReceiptRef.model_validate(receipt)
    normal_outcome = None
    status = "diagnostic"
    if finalized.purpose == "normal":
        normal_outcome = ReportOutcomeV1(
            change_id=finalized.change_id,
            coverage_epoch=finalized.coverage_epoch,
            batch_id=finalized.batch_id,
            inspection_receipt=finalized.inspection_receipt,
            plan_digest=finalized.plan_digest,
            plan_ref=finalized.plan_ref,
            report_refs=finalized.report_refs,
            report_receipt=committed,
        )
        status = "reported"
    return QualityReportPublicV1(
        change_id=finalized.change_id,
        coverage_state=selected.inspection.coverage_state,
        purpose=finalized.purpose,
        status=status,  # type: ignore[arg-type]
        report_outcome=normal_outcome,
        report_refs=finalized.report_refs,
        report_receipt=committed,
    ).model_dump(mode="json")


def clear_report_state(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {
        "attempt_failure": {},
        "report_outcome": {},
        "report_refs": [],
        "report_receipt": None,
    }


def route_report_attempt(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return "failed"
    if state.get("report_outcome") or state.get("report_purpose") == "diagnostic":
        return "done"
    return "failed"


def terminal_done(state: QualityState) -> dict[str, object]:
    del state
    return {}


__all__ = [
    "activation_assess",
    "activation_fact_baseline",
    "activation_materialize_assessment",
    "activation_one_shot",
    "clear_report_state",
    "publish_fact_baseline",
    "publish_inspect",
    "publish_materialize_assessment",
    "publish_issue",
    "publish_issue_analysis",
    "publish_report",
    "route_report_attempt",
    "select_fact_baseline",
    "select_inspect",
    "select_quality",
    "select_materialize_assessment",
    "select_report",
    "terminal_done",
]
