from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.canonical import canonical_digest

from assurance_quality.contracts.agent import QualitySkillInputV1
from assurance_quality.contracts.assessment import AssessmentInputsV1, MaterializeAssessmentInputV1
from assurance_quality.contracts.decisions import CoverageAssessmentPublicV1, IssueAnalysisPublicV1
from assurance_quality.graphs.state import (
    QualityAssessPublicV1,
    QualityIssuePublicV1,
    QualityReportPublicV1,
    QualityState,
)

activation_one_shot = BusinessActivation.one_shot()

_SKILL_DIGESTS = (
    "execution_digest",
    "healing_digest",
    "trace_digest",
    "coverage_digest",
    "metrics_digest",
    "case_digest",
    "plan_digest",
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
        "artifact_paths": state["allowed_artifact_paths"],
        **{name: state[name] for name in _SKILL_DIGESTS},
    }


def select_quality(state: Mapping[str, object]) -> QualitySkillInputV1:
    return QualitySkillInputV1.model_validate(_skill_payload(state))


def select_materialize_assessment(state: Mapping[str, object]) -> MaterializeAssessmentInputV1:
    return MaterializeAssessmentInputV1.model_validate(
        {
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
    if "coverage_state" not in state:
        raise ValueError("report requires a coverage reference")
    if "report_refs" not in state:
        raise ValueError("report requires report references")
    if "execution_digest" not in state and "execution_status" not in state:
        raise ValueError("report requires an execution reference")
    return select_quality(state)


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
            )
        ],
    }


def publish_fact_baseline(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    return {
        "evidence_refs": _as_refs(payload.get("evidence_refs") or state.get("evidence_refs")),
        "rounds_budget": _published_int(payload, "rounds_budget", state.get("rounds_budget", 0)),
        "rounds_used": _published_int(payload, "rounds_used", state.get("rounds_used", 0)),
    }


def publish_inspect(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    assessed = CoverageAssessmentPublicV1.model_validate(
        {
            "coverage_state": payload.get("coverage_state"),
            "rounds_budget": payload.get("rounds_budget", state.get("rounds_budget")),
            "rounds_used": payload.get("rounds_used", state.get("rounds_used")),
        }
    )
    change_id = state["change_id"]
    if not isinstance(change_id, str):
        raise TypeError("change_id must be a string")
    return QualityAssessPublicV1(
        change_id=change_id,
        coverage_state=assessed.coverage_state,
        evidence_refs=_as_refs(payload.get("evidence_refs") or state.get("evidence_refs")),
        rounds_budget=assessed.rounds_budget,
        rounds_used=assessed.rounds_used,
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


def publish_report(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del receipt
    payload = _output_payload(output)
    change_id = state["change_id"]
    if not isinstance(change_id, str):
        raise TypeError("change_id must be a string")
    return QualityReportPublicV1.model_validate(
        {
            "change_id": change_id,
            "coverage_state": payload.get("coverage_state", state.get("coverage_state")),
            "report_refs": _as_refs(payload.get("report_refs") or state.get("report_refs")),
        }
    ).model_dump(mode="json")


def terminal_done(state: QualityState) -> dict[str, object]:
    del state
    return {}


__all__ = [
    "activation_assess",
    "activation_materialize_assessment",
    "activation_one_shot",
    "publish_fact_baseline",
    "publish_inspect",
    "publish_materialize_assessment",
    "publish_issue",
    "publish_report",
    "select_quality",
    "select_materialize_assessment",
    "select_report",
    "terminal_done",
]
