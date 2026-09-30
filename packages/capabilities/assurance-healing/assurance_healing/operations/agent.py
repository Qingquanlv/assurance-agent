"""Provider-neutral fix-proposal and coverage-repair request helpers."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from agent_runtime_contracts import AgentRunRequest, ResultContract
from agent_runtime_contracts.ops import (
    AgentBindingDataV1,
    InputError,
    OutputError,
    result_contract_from,
    skill_request,
)
from graph_engine.canonical import JSONValue, canonical_digest as engine_digest
from graph_engine.frozen_json import thaw_json
from graph_engine.plugin_api import TaskContext

from assurance_healing.contracts.agent import FixProposalFinalizeInputV1, FixProposalInputV1
from assurance_healing.contracts.coverage_repair import CoverageRepairBrief
from assurance_healing.resource_loader import resource_bytes, resource_text

FIX_PROPOSAL_SKILL = "skills/aa-fix-proposal/SKILL.md"
COVERAGE_REPAIR_SKILL = "skills/aa-coverage-repair/SKILL.md"
FIX_PROPOSAL_RESULT_ID = "assurance.healing.result.fix-proposal.v1"
COVERAGE_REPAIR_RESULT_ID = "assurance.healing.result.coverage-repair.v1"
FIX_RESULT_FILE = "result-contracts/fix-proposal.v1.schema.json"
REPAIR_RESULT_FILE = "result-contracts/coverage-repair.v1.schema.json"


def result_contract(schema_id: str, relative: str) -> ResultContract:
    return result_contract_from(schema_id, json.loads(resource_bytes(relative)))


def prepare_request(
    *,
    skill_path: str,
    business: Any,
    binding: AgentBindingDataV1,
    result_id: str,
    result_file: str,
    context: TaskContext,
    allowed_outputs: tuple[str, ...],
) -> AgentRunRequest:
    return skill_request(
        skill_text=resource_text(skill_path),
        business=business,
        binding=binding,
        result=result_contract(result_id, result_file),
        roots=context,
        allowed_outputs=allowed_outputs,
        scope_id=business.change_id,
    )


def _canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def under_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root.rstrip('/')}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False


def workspace_file(workspace: Path, relative: str) -> Path:
    if not _canonical_relative(relative):
        raise OutputError(f"output file path must be canonical and relative: {relative}")
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if not path.is_file() or path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    if path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def structured(result: object) -> object:
    return thaw_json(result)


def _prepare_lock_fields(payload: FixProposalFinalizeInputV1 | FixProposalInputV1) -> JSONValue:
    return {
        "baseline_digest": payload.baseline_digest,
        "candidate_digest": payload.candidate_digest,
        "change_id": payload.change_id,
        "plan_digest": payload.plan_digest,
        "plan_ref": payload.plan_ref.model_dump(mode="json"),
        "execution_evidence_digest": payload.execution_evidence_digest,
        "issue_analysis_ref": payload.issue_analysis_ref.model_dump(mode="json")
        if payload.issue_analysis_ref
        else None,
        "policy_digest": payload.policy_digest,
        "require_approval": payload.require_approval,
    }


def require_prepare_lock(payload: FixProposalFinalizeInputV1) -> None:
    if engine_digest(_prepare_lock_fields(payload)) != engine_digest(_prepare_lock_fields(payload.prepare)):
        raise InputError("finalize digests do not match the locked prepare payload")


def brief_locator_ids(brief: CoverageRepairBrief) -> set[str]:
    ids: set[str] = set()
    for item in brief.repair_items:
        locator = item.locator
        for value in (locator.case_id, locator.constraint_key, locator.cell, locator.cluster_key):
            if value:
                ids.add(value)
    return ids
