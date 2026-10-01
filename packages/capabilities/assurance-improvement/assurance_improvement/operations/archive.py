"""Archive projection from canonical quality summaries."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime_contracts.ops import InputError, failed_input
from graph_engine.plugin_api import EffectIntent, TaskContext, TaskOutcome, TaskRequest
from assurance_quality.contracts.report import QualityReport

from assurance_improvement.contracts.effects import ImprovementEffectIntentV1
from assurance_improvement.operations.common import as_json, succeeded, validate_input
from assurance_improvement.operations.keys import archive_effect_key

_FROZEN = ConfigDict(frozen=True, extra="forbid")
ARCHIVE_EFFECT = "assurance.improvement.effect.archive.v1"
_WARNING_RISKS = frozenset({"unknown", "critical", "high", "medium", "low"})


class ArchivePublishReceipt(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["1"]
    change_id: str = Field(min_length=1)
    manifest_digest: str = Field(min_length=1)
    source_digest: str = Field(min_length=1)
    target_baseline: str = Field(min_length=1)
    final_digest: str = Field(min_length=1)


class ProjectArchiveInput(BaseModel):
    model_config = _FROZEN

    change_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    archive_digest: str = Field(min_length=1)
    report: QualityReport
    publish_receipt: ArchivePublishReceipt
    artifact_paths: tuple[str, ...] = ()


def project_archive(payload: ProjectArchiveInput) -> dict[str, object]:
    if payload.publish_receipt.change_id != payload.change_id:
        raise InputError("publish receipt change_id does not match archive subject")
    if payload.report.change_id != payload.change_id:
        raise InputError("quality report change_id does not match archive subject")
    issues = payload.report.issues
    risk = issues.issue_risk if issues is not None else None
    rationale = issues.issue_risk_rationale if issues is not None else None
    status: Literal["archived", "archived_with_warnings"] = (
        "archived_with_warnings" if risk in _WARNING_RISKS else "archived"
    )
    summary = f"# Archive {payload.change_id}\n\narchive_status: {status}\nissue_risk: {risk or 'clear'}\n"
    if rationale:
        summary += f"issue_risk_rationale: {rationale}\n"
    return {
        "schema_version": "1",
        "change_id": payload.change_id,
        "archive_status": status,
        "issue_risk": risk,
        "issue_risk_rationale": rationale,
        "summary": summary,
        "artifact_paths": list(payload.artifact_paths),
        "invocation_id": payload.invocation_id,
        "archive_digest": payload.archive_digest,
    }


class ProjectArchiveHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(ProjectArchiveInput, request.input)
            projection = project_archive(payload)
            intent_payload = ImprovementEffectIntentV1.model_validate(
                {
                    "schema_version": "1",
                    "kind": "archive",
                    "improvement_id": payload.change_id,
                    "invocation_id": payload.invocation_id,
                    "archive_digest": payload.archive_digest,
                }
            )
            key = archive_effect_key(intent_payload)
            del key
            return succeeded(
                projection,
                effects=(
                    EffectIntent(
                        kind=ARCHIVE_EFFECT,
                        payload=as_json(intent_payload.model_dump(mode="json")),
                    ),
                ),
            )
        except InputError as error:
            return failed_input(error)


__all__ = [
    "ArchivePublishReceipt",
    "ProjectArchiveHandler",
    "project_archive",
]
