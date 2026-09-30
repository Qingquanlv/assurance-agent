"""Provider-neutral improvement Agent request and retro commit helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    result_contract_from,
    skill_request,
)
from graph_engine.canonical import JSONValue
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext, TaskOutcome

from assurance_improvement.contracts.agent import (
    AgentFinalizeInputV1,
    RetroAnalysisFinalizeInputV1,
    RetroAnalysisResultV3,
    RetroSynthesisFinalizeInputV1,
)
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


def prepare_request(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_schema_id: str,
    context: TaskContext,
) -> AgentRunRequest:
    return skill_request(
        skill_text=resource_text(skill_path),
        business=business,
        binding=binding,
        result=result_contract(result_schema_id),
        roots=context,
        allowed_outputs=_IMPROVEMENT_OUTPUTS[skill_path](business.change_id),
        scope_id=business.change_id,
    )


def structured(
    payload: AgentFinalizeInputV1 | RetroAnalysisFinalizeInputV1 | RetroSynthesisFinalizeInputV1,
) -> object:
    return thaw_json(payload.agent_result.result_payload)


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


def commit_retro(
    payload: RetroSynthesisFinalizeInputV1 | RetroAnalysisFinalizeInputV1,
    context: TaskContext,
    *,
    expected_domain: DomainName | None,
) -> TaskOutcome:
    try:
        document = RetroAnalysisResultV3.model_validate(structured(payload))
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if expected_domain is None:
        context_lock = cast(RetroSynthesisFinalizeInputV1, payload).context
        if document.retro_id != context_lock.retro_id:
            raise OutputError("retro analysis identity does not match the locked retro")
        if document.domain != expected_domain:
            raise OutputError(f"retro analysis domain must be {expected_domain}")
        allowed = context_lock.source_manifest.resolvable_ids()
        if document.signals:
            raise OutputError("synthesis cannot change the locked context signals")
        from assurance_improvement.operations.retro import validate_candidates

        try:
            validate_candidates(context_lock, document.candidates)
        except (InputError, ValidationError) as error:
            raise OutputError(str(error)) from error
    else:
        slice_lock = cast(RetroAnalysisFinalizeInputV1, payload).evidence_slice
        if document.retro_id != slice_lock.retro_id:
            raise OutputError("retro analysis identity does not match the locked retro")
        if document.domain != expected_domain:
            raise OutputError(f"retro analysis domain must be {expected_domain}")
        if slice_lock.domain != expected_domain:
            raise InputError("analysis input slice domain does not match its handler")
        if document.candidates:
            raise OutputError("domain analysis produces signals, not improvement candidates")
        allowed = slice_lock.resolvable_ids()
        ids = [signal.signal_id for signal in document.signals]
        deterministic_ids = {signal.signal_id for signal in slice_lock.deterministic_signals}
        if len(ids) != len(set(ids)) or deterministic_ids.intersection(ids):
            raise OutputError("analysis signals must be unique and must not repeat deterministic signals")
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
