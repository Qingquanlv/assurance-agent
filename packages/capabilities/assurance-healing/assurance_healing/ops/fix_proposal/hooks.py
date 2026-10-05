"""Check the locked issue analysis, then seal the staged fix proposal."""

from __future__ import annotations

from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext
from graph_engine.artifacts import ArtifactReadError, open_artifact, under_root
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_healing.contracts.agent import FixProposalInputV1, FixProposalResultV1
from assurance_healing.contracts.issue_handoff import IssueAnalysisHandoffV1
from assurance_healing.contracts.repair_input import RepairBoundInputV1
from assurance_healing.operations.agent import workspace_file
from assurance_healing.operations.repair_input import opened_repair_input

PATH = "qa/results/healing/fix-proposal.json"


def _proposal(
    ctx: PrepareContext | FinalizeContext, business: RepairBoundInputV1, error: type[Exception]
) -> FixProposalInputV1:
    filled = opened_repair_input(ctx.project_root, business, error)
    fields = set(FixProposalInputV1.model_fields)
    return FixProposalInputV1.model_validate({key: value for key, value in filled.items() if key in fields})


def before(ctx: PrepareContext, business: RepairBoundInputV1) -> FixProposalInputV1:
    """Open the analysis when it is bound. Routing, not this hook, authorizes repair.

    A handoff from an earlier coverage epoch is absent: the ledger keeps the file,
    and this hook is what drops it.
    """
    skill = _proposal(ctx, business, InputError)
    if skill.issue_analysis_handoff_ref is not None:
        try:
            document = open_artifact(
                ctx.project_root,
                skill.issue_analysis_handoff_ref,
                model=IssueAnalysisHandoffV1,
            )
        except ArtifactReadError as error:
            if error.reason == "digest":
                raise InputError("issue analysis handoff digest changed") from error
            raise InputError(str(error)) from error
        analysis = document.issue_analysis_ref if document.coverage_epoch == skill.coverage_epoch else None
        skill = skill.model_copy(update={"issue_analysis_ref": analysis})
    if skill.issue_analysis_ref is None:
        return skill
    try:
        open_artifact(ctx.project_root, skill.issue_analysis_ref)
    except ArtifactReadError as error:
        if error.reason == "digest":
            raise InputError("issue analysis digest changed") from error
        raise InputError(str(error)) from error
    return skill


def after(
    ctx: FinalizeContext, business: RepairBoundInputV1, result: FixProposalResultV1
) -> FixProposalResultV1:
    skill = _proposal(ctx, business, OutputError)
    allowed = set(skill.allowed_paths)
    for item in result.proposals:
        if not item.eligible:
            continue
        if not item.files_to_modify:
            raise OutputError("eligible proposal requires files_to_modify")
        for path in item.files_to_modify:
            if path not in allowed or not under_root(path, skill.allowed_roots):
                raise OutputError(f"undeclared target file: {path}")
            workspace_file(ctx.project_root, path)
    data = workspace_file(ctx.write_root, PATH)
    try:
        staged = FixProposalResultV1.model_validate_json(data)
    except ValidationError as error:
        raise OutputError(str(error)) from error
    if staged != result:
        raise OutputError("staged fix proposal differs from the typed agent result")
    expected = canonical_json_bytes(cast(JSONValue, result.model_dump(mode="json"))) + b"\n"
    if data != expected:
        raise OutputError("staged fix proposal must use canonical JSON bytes")
    return result
