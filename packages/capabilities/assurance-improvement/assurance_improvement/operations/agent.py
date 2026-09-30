"""Provider-neutral improvement prepare/finalize handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from agent_runtime_contracts import ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    failed_input,
    failed_output,
    prepared_outcome,
    result_contract_from,
    skill_request,
    validate_binding,
)
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_improvement.contracts.agent import (
    AgentFinalizeInputV1,
    ArchiveResultV1,
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    RetroAnalysisResultV3,
    RetroAnalysisInputV1,
    RetroSynthesisInputV1,
    RetroAnalysisFinalizeInputV1,
    RetroSynthesisFinalizeInputV1,
)
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.operations.common import validate_input
from assurance_improvement.resource_loader import resource_bytes, resource_text
from assurance_improvement.validators.paths import canonical_relative

RETRO_SKILL = "skills/aa-retro/SKILL.md"
RETRO_EVAL_SKILL = "skills/aa-retro-eval-analysis/SKILL.md"
RETRO_ISSUE_SKILL = "skills/aa-retro-issue-analysis/SKILL.md"
RETRO_WORKFLOW_SKILL = "skills/aa-retro-workflow-analysis/SKILL.md"
REVIEW_SKILL = "skills/aa-improvement-reviewer/SKILL.md"
ARCHIVE_SKILL = "skills/aa-archive/SKILL.md"

RETRO_RESULT_ID = "assurance.improvement.result.retro-analysis.v3"
REVIEW_RESULT_ID = "assurance.improvement.result.improvement-review.v1"
ARCHIVE_RESULT_ID = "assurance.improvement.result.archive.v1"

_RESULT_FILES: dict[str, str] = {
    RETRO_RESULT_ID: "result-contracts/retro-analysis.v3.schema.json",
    REVIEW_RESULT_ID: "result-contracts/improvement-review.v1.schema.json",
    ARCHIVE_RESULT_ID: "result-contracts/archive.v1.schema.json",
}

DomainName = Literal["issue", "workflow", "eval", "discovery", "coverage_gap"]
_IMPROVEMENT_OUTPUTS = {
    RETRO_SKILL: lambda change_id: ("qa/results/retro/retro.json",),
    RETRO_EVAL_SKILL: lambda change_id: ("qa/results/retro/retro-eval-analysis.json",),
    RETRO_ISSUE_SKILL: lambda change_id: ("qa/results/retro/retro-issue-analysis.json",),
    RETRO_WORKFLOW_SKILL: lambda change_id: ("qa/results/retro/retro-workflow-analysis.json",),
    REVIEW_SKILL: lambda change_id: ("qa/results/review/improvement-review.json",),
    ARCHIVE_SKILL: lambda change_id: ("qa/results/archive/archive-receipt.json",),
}


def result_contract(schema_id: str) -> ResultContract:
    return result_contract_from(schema_id, json.loads(resource_bytes(_RESULT_FILES[schema_id])))


def prepare_outcome(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
) -> TaskOutcome:
    return prepared_outcome(
        skill_request(
            skill_text=resource_text(skill_path),
            business=business,
            binding=binding,
            result=result_contract(result_schema_id),
            roots=context,
            allowed_outputs=_IMPROVEMENT_OUTPUTS[skill_path](business.change_id),
            scope_id=business.change_id,
        )
    )


def _structured(
    payload: AgentFinalizeInputV1 | RetroAnalysisFinalizeInputV1 | RetroSynthesisFinalizeInputV1,
) -> object:
    return thaw_json(payload.agent_result.result_payload)


def _prepare(skill: str, result_id: str, request: TaskRequest, context: TaskContext) -> TaskOutcome:
    input_model = (
        RetroSynthesisInputV1
        if skill == RETRO_SKILL
        else RetroAnalysisInputV1
        if skill in {RETRO_EVAL_SKILL, RETRO_ISSUE_SKILL, RETRO_WORKFLOW_SKILL}
        else ImprovementSkillInputV1
    )
    business = validate_input(input_model, request.input)
    binding = validate_binding(request.binding_data)
    return prepare_outcome(
        skill_path=skill,
        business=business,
        binding=binding,
        result_schema_id=result_id,
        context=context,
    )


def _source_ids(result: RetroAnalysisResultV3) -> tuple[str, ...]:
    ids: list[str] = []
    for signal in result.signals:
        ids.extend(signal.source_refs.all_ids())
    for candidate in result.candidates:
        ids.extend(candidate.source_refs.all_ids())
    return tuple(ids)


def _workspace_file(workspace: Path, relative: str) -> Path:
    if not relative or not canonical_relative(relative):
        raise OutputError("Retro result path must be canonical and relative")
    path = workspace / relative
    path.resolve(strict=True).relative_to(workspace.resolve(strict=True))
    for part in (path, *path.parents):
        if part == workspace:
            break
        if part.is_symlink():
            raise OutputError("Retro result path must not contain symlinks")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise OutputError("Retro result must be a regular single-link file")
    return path


def _finalize_retro(
    request: TaskRequest,
    context: TaskContext,
    *,
    expected_domain: DomainName | None,
) -> TaskOutcome:
    payload = validate_input(
        RetroSynthesisFinalizeInputV1 if expected_domain is None else RetroAnalysisFinalizeInputV1,
        request.input,
    )
    try:
        document = RetroAnalysisResultV3.model_validate(_structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    locked = payload.context if expected_domain is None else payload.evidence_slice
    if document.retro_id != locked.retro_id:
        raise OutputError("retro analysis identity does not match the locked retro")
    if document.domain != expected_domain:
        raise OutputError(f"retro analysis domain must be {expected_domain}")
    if expected_domain is not None:
        if locked.domain != expected_domain:
            raise InputError("analysis input slice domain does not match its handler")
        if document.candidates:
            raise OutputError("domain analysis produces signals, not improvement candidates")
        allowed = locked.resolvable_ids()
        ids = [signal.signal_id for signal in document.signals]
        deterministic_ids = {signal.signal_id for signal in locked.deterministic_signals}
        if len(ids) != len(set(ids)) or deterministic_ids.intersection(ids):
            raise OutputError("analysis signals must be unique and must not repeat deterministic signals")
    else:
        allowed = locked.source_manifest.resolvable_ids()
        if document.signals:
            raise OutputError("synthesis cannot change the locked context signals")
        from assurance_improvement.operations.retro import validate_candidates

        try:
            validate_candidates(locked, document.candidates)
        except (InputError, ValidationError) as error:
            raise OutputError(str(error)) from error
    for source_id in _source_ids(document):
        if source_id not in allowed:
            raise OutputError("candidate source is outside the retro manifest")
    suffix = f"retro-{expected_domain}-analysis" if expected_domain else "retro"
    try:
        path = _workspace_file(context.write_root, f"qa/results/retro/{suffix}.json")
        written = RetroAnalysisResultV3.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise OutputError(f"required Retro result artifact is invalid: {suffix}.json") from error
    if written != document:
        raise OutputError("written Retro result differs from the assistant result")
    return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))


class RetroPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(RETRO_SKILL, RETRO_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class RetroEvalPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(RETRO_EVAL_SKILL, RETRO_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class RetroIssuePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(RETRO_ISSUE_SKILL, RETRO_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class RetroWorkflowPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(RETRO_WORKFLOW_SKILL, RETRO_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class ImprovementReviewPrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(REVIEW_SKILL, REVIEW_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class ArchivePrepareHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _prepare(ARCHIVE_SKILL, ARCHIVE_RESULT_ID, request, context)
        except InputError as error:
            return failed_input(error)


class RetroFinalizeHandler:
    input_model = RetroSynthesisFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _finalize_retro(request, context, expected_domain=None)
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroEvalFinalizeHandler:
    input_model = RetroAnalysisFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _finalize_retro(request, context, expected_domain="eval")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroIssueFinalizeHandler:
    input_model = RetroAnalysisFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _finalize_retro(request, context, expected_domain="issue")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class RetroWorkflowFinalizeHandler:
    input_model = RetroAnalysisFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            return _finalize_retro(request, context, expected_domain="workflow")
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ImprovementReviewFinalizeHandler:
    input_model = AgentFinalizeInputV1

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(AgentFinalizeInputV1, request.input)
            try:
                document = ImprovementReviewResultV1.model_validate(_structured(payload))
            except ValidationError as error:
                raise OutputError(str(error)) from error
            if payload.subject is None or payload.projection is None:
                raise InputError("review finalize requires the authenticated subject and projection")
            if payload.subject.improvement_id != payload.improvement_id:
                raise OutputError("review subject improvement_id does not match")
            if payload.projection.improvement_id != payload.improvement_id:
                raise OutputError("review projection improvement_id does not match")
            if payload.projection.version != payload.expected_improvement_version:
                raise OutputError("review version does not match the current improvement")
            if digest_hex(artifact_digest(payload.subject)) != payload.subject_digest:
                raise OutputError("review subject digest does not match")
            if document.decision == "pass" and document.evidence_traceability != "complete":
                raise OutputError("pass review requires complete evidence traceability")
            return TaskOutcome.succeeded(cast(JSONValue, document.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except OutputError as error:
            return failed_output(str(error))


class ArchiveFinalizeHandler:
    input_model = AgentFinalizeInputV1

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
            if payload.quality_report is None:
                raise InputError("archive finalize requires the authenticated quality report")
            if digest_hex(artifact_digest(payload.quality_report)) != payload.quality_report_digest:
                raise OutputError("quality report digest does not match")
            if payload.quality_report.change_id != payload.change_id:
                raise OutputError("quality report change_id does not match")
            issues = payload.quality_report.issues
            locked_risk = issues.issue_risk if issues is not None else None
            locked_rationale = issues.issue_risk_rationale if issues is not None else None
            if document.issue_risk != locked_risk:
                raise OutputError("archive issue_risk does not match the locked quality report")
            if document.issue_risk_rationale != locked_rationale:
                raise OutputError("archive issue_risk_rationale does not match the locked quality report")
            locked_paths = frozenset(payload.artifact_paths)
            if any(path not in locked_paths for path in document.artifact_paths):
                raise OutputError("archive artifact path is outside the locked manifest")
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
