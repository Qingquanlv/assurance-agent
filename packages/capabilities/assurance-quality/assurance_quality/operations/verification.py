"""The single deterministic evaluator for verified business execution."""

from __future__ import annotations

from typing import Any, Literal, cast

from assurance_execution.contracts.verification import VerificationEvidenceV1
from assurance_generation.contracts.execution_plan import CaseExecutionPlanV1
from assurance_intake.contracts.verification import InputExpectedV1, LiteralExpectedV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.verification import (
    VerificationObligationV1,
    VerificationVerdictV1,
    VerificationStatus,
)
from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.frozen_json import thaw_json


def _expected(plan: CaseExecutionPlanV1, obligation_id: str) -> tuple[Literal["business", "completion"], Any]:
    assertions = {item.assertion_id: item for item in plan.assertions}
    assertion = assertions.get(obligation_id)
    if assertion is not None:
        if isinstance(assertion.expected, LiteralExpectedV1):
            return "business", assertion.expected.value
        if isinstance(assertion.expected, InputExpectedV1):
            return "business", plan.inputs[assertion.expected.key]
        raise AssertionError("closed expected union was not exhausted")
    runtime = {
        "initial.user_absent": 0,
        "action.finished": True,
        "oracle.executed": True,
        "trace.http": True,
        "trace.user_write": True,
        "trace.user_completed": True,
        "trace.drained": True,
    }
    return "completion", runtime[obligation_id]


def reduce_verdict(*, violated: bool, complete: bool, runner_ok: bool) -> VerificationStatus:
    if violated:
        return "FAILED"
    if not complete or not runner_ok:
        return "INCOMPLETE"
    return "PASSED"


def evaluate_verification(
    plan: CaseExecutionPlanV1,
    evidence: VerificationEvidenceV1,
    *,
    completion_status: str,
    execution_id: str | None = None,
) -> VerificationVerdictV1:
    """Compare facts with frozen expectations without trusting runner assertions."""

    expected_execution_id = execution_id or evidence.execution_id
    if evidence.execution_id != expected_execution_id or any(
        item.execution_id != expected_execution_id for item in evidence.observations
    ):
        raise ValueError("verification evidence execution identity does not match")
    observed = {item.obligation_id: item for item in evidence.observations}
    unknown = set(observed) - set(plan.required)
    if unknown:
        raise ValueError(f"verification evidence contains unknown obligations: {sorted(unknown)}")
    obligations: list[VerificationObligationV1] = []
    business_violated = False
    complete = True
    unavailable = False
    completion_unsatisfied = False
    for obligation_id in plan.required:
        kind, expected = _expected(plan, obligation_id)
        observation = observed.get(obligation_id)
        if observation is None:
            obligations.append(
                VerificationObligationV1(
                    obligation_id=obligation_id,
                    kind=kind,
                    evidence_status="missing",
                    business_status="not_evaluated",
                    expected=expected,
                    reason="required_observation_missing",
                )
            )
            complete = False
            unavailable = True
            continue
        if observation.state != "observed":
            obligations.append(
                VerificationObligationV1(
                    obligation_id=obligation_id,
                    kind=kind,
                    evidence_status=observation.state,
                    business_status="not_evaluated",
                    expected=expected,
                    reason=observation.reason,
                )
            )
            complete = False
            unavailable = True
            continue
        holds = canonical_json_bytes(cast(JSONValue, thaw_json(observation.actual))) == canonical_json_bytes(
            cast(JSONValue, thaw_json(expected))
        )
        if not holds:
            if kind == "business":
                business_violated = True
            else:
                complete = False
                completion_unsatisfied = True
        obligations.append(
            VerificationObligationV1(
                obligation_id=obligation_id,
                kind=kind,
                evidence_status="observed",
                business_status="satisfied" if holds else "violated",
                expected=expected,
                actual=observation.actual,
                reason=(
                    None
                    if holds
                    else (
                        "actual_differs_from_frozen_expected"
                        if kind == "business"
                        else "completion_obligation_not_satisfied"
                    )
                ),
            )
        )
    collector_ok = evidence.collector_completion.state == "complete" or (
        plan.validation_profile == "api_db.v1" and evidence.collector_completion.state == "not_required"
    )
    runner_ok = (
        completion_status == "collected"
        and evidence.state == "collected"
        and evidence.host_completion.state == "complete"
        and collector_ok
    )
    verdict = reduce_verdict(
        violated=business_violated,
        complete=complete,
        runner_ok=runner_ok,
    )
    reasons: set[str] = set()
    if business_violated:
        reasons.add("verification.business_violation")
    if unavailable:
        reasons.add("verification.required_evidence_missing")
    if completion_unsatisfied:
        reasons.add("verification.completion_unsatisfied")
    if not runner_ok:
        reasons.add("verification.runner_incomplete")
    ordered = tuple(sorted(obligations, key=lambda item: item.obligation_id))
    return VerificationVerdictV1(
        validation_profile=plan.validation_profile,
        execution_id=expected_execution_id,
        case_id=plan.case_id,
        verdict=verdict,
        required=len(ordered),
        executed=sum(item.evidence_status == "observed" for item in ordered),
        evaluated=sum(
            item.business_status in {"satisfied", "violated", "not_applicable"} for item in ordered
        ),
        satisfied=sum(item.business_status in {"satisfied", "not_applicable"} for item in ordered),
        obligations=ordered,
        reason_codes=tuple(sorted(reasons)),
    )


def verification_failure_facts(verdict: VerificationVerdictV1):
    """Project a verified verdict into the existing closed disposition facts."""

    from assurance_quality.contracts.assessment import FailureClassificationFactsV1

    return FailureClassificationFactsV1(
        identity_valid=True,
        analysis_required=False,
        blocking_failure=verdict.verdict == "INCOMPLETE" and not verdict.repairable_bridge_defect,
        needs_human=verdict.verdict == "FAILED",
        repairable_failure=verdict.repairable_bridge_defect,
    )


def _markdown_cell(value: object) -> str:
    encoded = canonical_json_bytes(cast(JSONValue, thaw_json(value))).decode("utf-8")
    return encoded.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def render_verification_report_section(
    verdict: VerificationVerdictV1,
    verification_ref: EvidenceArtifactRefV1,
) -> str:
    """Render the reserved report section from one authenticated verdict artifact."""

    lines = [
        "## Deterministic business verification",
        "",
        f"- Artifact: `{verification_ref.path}`",
        f"- Digest: `{verification_ref.digest}`",
        f"- Profile: `{verdict.validation_profile}`",
        f"- Execution: `{verdict.execution_id}`",
        f"- Case: `{verdict.case_id}`",
        f"- Verdict: **{verdict.verdict}**",
        (
            "- Counts: "
            f"required={verdict.required}, executed={verdict.executed}, "
            f"evaluated={verdict.evaluated}, satisfied={verdict.satisfied}"
        ),
        "",
        "| Obligation | Kind | Evidence | Business | Expected | Actual | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in verdict.obligations:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(item.obligation_id),
                    item.kind,
                    item.evidence_status,
                    item.business_status,
                    _markdown_cell(item.expected),
                    _markdown_cell(item.actual),
                    _markdown_cell(item.reason),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "evaluate_verification",
    "reduce_verdict",
    "render_verification_report_section",
    "verification_failure_facts",
]
