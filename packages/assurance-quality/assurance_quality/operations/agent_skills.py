"""Provider-neutral quality prepare/finalize handlers."""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    FactBaselineResultV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    QualitySkillInputV1,
    ReportResultV1,
)
from assurance_quality.contracts.issues import IssueCandidateDocument
from assurance_quality.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_input,
)
from assurance_quality.operations.identity import (
    candidate_document_digest,
    problem_fingerprint,
    problem_id,
)
from assurance_quality.resource_loader import resource_bytes, resource_text

FACT_BASELINE_SKILL = "skills/aa-fact-baseline/SKILL.md"
INSPECT_SKILL = "skills/aa-inspect/SKILL.md"
ISSUE_ANALYSIS_SKILL = "skills/aa-issue-analyzer/SKILL.md"
ISSUE_TRIAGE_SKILL = "skills/aa-issue-triage-advisor/SKILL.md"
REPORT_SKILL = "skills/aa-report-generator/SKILL.md"
EXPLORER_PERSONA = "personas/explorer.md"
REVIEWER_PERSONA = "personas/reviewer.md"
REPORTER_PERSONA = "personas/reporter.md"

FACT_BASELINE_RESULT_ID = "assurance.quality.result.fact-baseline.v1"
INSPECTION_RESULT_ID = "assurance.quality.result.inspection.v1"
ISSUE_ANALYSIS_RESULT_ID = "assurance.quality.result.issue-analysis.v1"
ISSUE_TRIAGE_RESULT_ID = "assurance.quality.result.issue-triage.v1"
REPORT_RESULT_ID = "assurance.quality.result.report.v1"

_RESULT_FILES: dict[str, str] = {
    FACT_BASELINE_RESULT_ID: "result-contracts/fact-baseline.v1.schema.json",
    INSPECTION_RESULT_ID: "result-contracts/inspection.v1.schema.json",
    ISSUE_ANALYSIS_RESULT_ID: "result-contracts/issue-analysis.v1.schema.json",
    ISSUE_TRIAGE_RESULT_ID: "result-contracts/issue-triage.v1.schema.json",
    REPORT_RESULT_ID: "result-contracts/report.v1.schema.json",
}

_TRIAGE_ACTIONS = frozenset(
    {
        "confirm_assessment",
        "mark_not_an_issue",
        "accept_risk",
        "start_work",
        "reopen",
        "merge",
        "stop",
    }
)


def result_contract(schema_id: str) -> ResultContract:
    payload = json.loads(resource_bytes(_RESULT_FILES[schema_id]))
    return ResultContract(
        schema_id=schema_id,
        schema_digest=canonical_digest(payload),
        extraction_mode="structured",
    )


def validate_binding(data: object) -> AgentBindingDataV1:
    try:
        return AgentBindingDataV1.model_validate(data)
    except ValidationError as error:
        raise InputError(str(error)) from error


def prepare_outcome(
    *,
    skill_path: str,
    persona_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
) -> TaskOutcome:
    agent_request = AgentRunRequest(
        instructions=(
            InstructionPart.text("text/plain", resource_text(skill_path)),
            InstructionPart.text("text/plain", resource_text(persona_path)),
            InstructionPart.from_json(business.model_dump(mode="json")),
        ),
        result_contract=result_contract(result_schema_id),
        execution=binding.execution,
        request_policy_digest=binding.request_policy_digest,
        request_config_digest=binding.request_config_digest,
    )
    return TaskOutcome.succeeded(agent_request.model_dump(mode="json"))


def _structured(payload: AgentFinalizeInputV1) -> object:
    return thaw_json(payload.agent_result.structured_result)


def _prepare(skill: str, persona: str, result_id: str, request: TaskRequest) -> TaskOutcome:
    business = validate_input(QualitySkillInputV1, request.input)
    binding = validate_binding(request.binding_data)
    return prepare_outcome(
        skill_path=skill,
        persona_path=persona,
        business=business,
        binding=binding,
        result_schema_id=result_id,
    )


class FactBaselinePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(FACT_BASELINE_SKILL, EXPLORER_PERSONA, FACT_BASELINE_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class InspectPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(INSPECT_SKILL, EXPLORER_PERSONA, INSPECTION_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class IssueAnalysisPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(ISSUE_ANALYSIS_SKILL, EXPLORER_PERSONA, ISSUE_ANALYSIS_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class IssueTriagePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(ISSUE_TRIAGE_SKILL, REVIEWER_PERSONA, ISSUE_TRIAGE_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class ReportPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(REPORT_SKILL, REPORTER_PERSONA, REPORT_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class FactBaselineFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = FactBaselineResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            owned = frozenset(payload.owned_evidence_ids)
            for evidence_id in document.source_evidence_ids:
                if evidence_id not in owned:
                    raise OutputError(f"fact baseline cites unowned source evidence: {evidence_id}")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class InspectFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = InspectionResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = {
                "execution_digest": payload.execution_digest,
                "healing_digest": payload.healing_digest,
                "trace_digest": payload.trace_digest,
                "coverage_digest": payload.coverage_digest,
                "metrics_digest": payload.metrics_digest,
            }
            actual = {
                "execution_digest": document.execution_digest,
                "healing_digest": document.healing_digest,
                "trace_digest": document.trace_digest,
                "coverage_digest": document.coverage_digest,
                "metrics_digest": document.metrics_digest,
            }
            for key, locked in expected.items():
                if actual[key] != locked:
                    raise OutputError(f"inspection {key} is not closed against the locked projection")
            if document.change_id != payload.change_id or document.batch_id != payload.batch_id:
                raise OutputError("inspection identity does not match the locked change")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class IssueAnalysisFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = IssueAnalysisResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.change_id != payload.change_id or document.batch_id != payload.batch_id:
                raise OutputError("issue analysis identity does not match the locked change")
            if not payload.evidence_bundle_digest:
                raise InputError("evidence_bundle_digest must authenticate issue analysis")
            if document.evidence_bundle_digest != payload.evidence_bundle_digest:
                raise OutputError("issue analysis evidence bundle is not authenticated")
            owned = frozenset(payload.owned_evidence_ids)
            for candidate in document.candidates:
                for observation_id in candidate.observation_ids:
                    if observation_id not in owned:
                        raise OutputError(f"issue candidate cites unowned evidence: {observation_id}")
                fingerprint = problem_fingerprint(
                    affected_surface=candidate.affected_surface,
                    fingerprint_inputs=candidate.fingerprint_inputs,
                )
                expected_id = problem_id(fingerprint)
                if candidate.possible_problem_ids and expected_id not in candidate.possible_problem_ids:
                    raise OutputError(f"issue candidate possible_problem_ids does not contain {expected_id}")
            candidates_doc = IssueCandidateDocument(
                schema_version="1.0",
                change_id=document.change_id,
                batch_id=document.batch_id,
                evidence_bundle_digest=document.evidence_bundle_digest,
                candidates=list(document.candidates),
            )
            output = document.model_dump(mode="json")
            if document.status == "completed":
                output["candidate_digest"] = candidate_document_digest(candidates_doc)
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class IssueTriageFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = IssueTriageResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.recommended_action not in _TRIAGE_ACTIONS:
                raise OutputError(
                    f"issue triage recommended_action is not declared: {document.recommended_action}"
                )
            locked = payload.locked_evidence_digests
            if not locked:
                raise InputError("locked_evidence_digests must authenticate triage evidence")
            if dict(document.evidence_digests) != dict(locked):
                raise OutputError("issue triage evidence_digests must equal locked_evidence_digests")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ReportFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = ReportResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            expected = {
                "case_digest": payload.case_digest,
                "plan_digest": payload.plan_digest,
                "mapping_digest": payload.mapping_digest,
                "execution_digest": payload.execution_digest,
                "healing_digest": payload.healing_digest,
                "trace_digest": payload.trace_digest,
                "coverage_digest": payload.coverage_digest,
                "issue_digest": payload.issue_digest,
                "metrics_digest": payload.metrics_digest,
            }
            actual = {
                "case_digest": document.case_digest,
                "plan_digest": document.plan_digest,
                "mapping_digest": document.mapping_digest,
                "execution_digest": document.execution_digest,
                "healing_digest": document.healing_digest,
                "trace_digest": document.trace_digest,
                "coverage_digest": document.coverage_digest,
                "issue_digest": document.issue_digest,
                "metrics_digest": document.metrics_digest,
            }
            for key, locked in expected.items():
                if actual[key] != locked:
                    raise OutputError(f"report {key} is not closed against the locked projection")
            if document.change_id != payload.change_id or document.batch_id != payload.batch_id:
                raise OutputError("report identity does not match the locked change")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
