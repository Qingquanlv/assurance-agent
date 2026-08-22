"""Provider-neutral improvement prepare/finalize handlers."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, InstructionPart, ResultContract
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import (
    AgentBindingDataV1,
    AgentFinalizeInputV1,
    ArchiveResultV1,
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    RetroAnalysisResultV3,
)
from assurance_improvement.contracts.retro import ImprovementCandidateDocumentV3
from assurance_improvement.operations.common import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_input,
)
from assurance_improvement.resource_loader import resource_bytes, resource_text

RETRO_SKILL = "skills/aa-retro/SKILL.md"
RETRO_EVAL_SKILL = "skills/aa-retro-eval-analysis/SKILL.md"
RETRO_ISSUE_SKILL = "skills/aa-retro-issue-analysis/SKILL.md"
RETRO_WORKFLOW_SKILL = "skills/aa-retro-workflow-analysis/SKILL.md"
REVIEW_SKILL = "skills/aa-improvement-reviewer/SKILL.md"
ARCHIVE_SKILL = "skills/aa-archive/SKILL.md"
REVIEWER_PERSONA = "personas/reviewer.md"
ARCHIVER_PERSONA = "personas/archiver.md"

RETRO_RESULT_ID = "assurance.improvement.result.retro-analysis.v3"
REVIEW_RESULT_ID = "assurance.improvement.result.improvement-review.v1"
ARCHIVE_RESULT_ID = "assurance.improvement.result.archive.v1"

_RESULT_FILES: dict[str, str] = {
    RETRO_RESULT_ID: "result-contracts/retro-analysis.v3.schema.json",
    REVIEW_RESULT_ID: "result-contracts/improvement-review.v1.schema.json",
    ARCHIVE_RESULT_ID: "result-contracts/archive.v1.schema.json",
}

DomainName = Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]


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
    business = validate_input(ImprovementSkillInputV1, request.input)
    binding = validate_binding(request.binding_data)
    return prepare_outcome(
        skill_path=skill,
        persona_path=persona,
        business=business,
        binding=binding,
        result_schema_id=result_id,
    )


def _source_ids(result: RetroAnalysisResultV3) -> tuple[str, ...]:
    ids: list[str] = []
    for signal in result.signals:
        ids.extend(signal.source_refs.all_ids())
    for candidate in result.candidates:
        ids.extend(candidate.source_refs.all_ids())
    return tuple(ids)


def _finalize_retro(
    request: TaskRequest,
    *,
    expected_domain: DomainName | None,
) -> TaskOutcome:
    payload = validate_input(AgentFinalizeInputV1, request.input)
    try:
        document = RetroAnalysisResultV3.model_validate(_structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if document.retro_id != payload.retro_id:
        raise OutputError("retro analysis identity does not match the locked retro")
    if expected_domain is not None and document.domain != expected_domain:
        raise OutputError(f"retro analysis domain must be {expected_domain}")
    allowed = payload.source_manifest.resolvable_ids()
    for source_id in _source_ids(document):
        if source_id not in allowed:
            raise OutputError("candidate source is outside the retro manifest")
    if document.candidates:
        ImprovementCandidateDocumentV3.model_validate(
            {
                "schema_version": "3",
                "retro_id": document.retro_id,
                "context_sha256": payload.context_digest
                if payload.context_digest.startswith("sha256:")
                else f"sha256:{payload.context_digest}",
                "candidates": [item.model_dump(mode="json") for item in document.candidates],
            },
            context={"retro_manifest": payload.source_manifest},
        )
        locked = frozenset(payload.locked_signal_ids)
        if locked:
            for candidate in document.candidates:
                if any(signal_id not in locked for signal_id in candidate.signal_ids):
                    raise OutputError("candidate cites a signal outside the locked context")
    return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))


class RetroPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(RETRO_SKILL, REVIEWER_PERSONA, RETRO_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class RetroEvalPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(RETRO_EVAL_SKILL, REVIEWER_PERSONA, RETRO_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class RetroIssuePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(RETRO_ISSUE_SKILL, REVIEWER_PERSONA, RETRO_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class RetroWorkflowPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(RETRO_WORKFLOW_SKILL, REVIEWER_PERSONA, RETRO_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class ImprovementReviewPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(REVIEW_SKILL, REVIEWER_PERSONA, REVIEW_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class ArchivePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _prepare(ARCHIVE_SKILL, ARCHIVER_PERSONA, ARCHIVE_RESULT_ID, request)
        except InputError as error:
            return failed_input(error)


class RetroFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _finalize_retro(request, expected_domain=None)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroEvalFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _finalize_retro(request, expected_domain="eval")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroIssueFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _finalize_retro(request, expected_domain="issue")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroWorkflowFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            return _finalize_retro(request, expected_domain="workflow")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ImprovementReviewFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = ImprovementReviewResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if not payload.subject_digest:
                raise InputError("subject_digest must authenticate the review subject")
            if document.decision == "pass" and document.evidence_traceability != "complete":
                raise OutputError("pass review requires complete evidence traceability")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ArchiveFinalizeHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = ArchiveResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if document.change_id != payload.change_id:
                raise OutputError("archive identity does not match the locked change")
            if document.invocation_id != payload.invocation_id:
                raise OutputError("archive invocation is not locked")
            if document.archive_digest != payload.archive_digest:
                raise OutputError("archive digest is not closed against the locked tree")
            if (
                document.issue_risk not in {None, "clear"}
                and document.archive_status != "archived_with_warnings"
            ):
                raise OutputError("non-clear issue risk requires archived_with_warnings")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))
