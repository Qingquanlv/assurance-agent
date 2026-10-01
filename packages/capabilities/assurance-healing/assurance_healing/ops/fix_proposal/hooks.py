"""Check the locked issue analysis, then seal the staged fix proposal."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from pydantic import ValidationError

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext
from graph_engine.canonical import JSONValue, canonical_json_bytes

from assurance_healing.contracts.agent import FixProposalInputV1, FixProposalResultV1
from assurance_healing.operations.agent import require_prepare_lock, under_root, workspace_file

PATH = "qa/results/healing/fix-proposal.json"


def before(ctx: PrepareContext, business: FixProposalInputV1) -> FixProposalInputV1:
    if business.issue_analysis_ref is not None:
        ref = business.issue_analysis_ref
        data = workspace_file(ctx.project_root, ref.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.digest:
            raise OutputError("issue analysis digest changed")
    return business


def after(
    ctx: FinalizeContext, business: FixProposalInputV1, result: FixProposalResultV1
) -> FixProposalResultV1:
    locked = ctx.prepared.get("prepare")
    if not isinstance(locked, dict):
        raise InputError("finalize digests do not match the locked prepare payload")
    require_prepare_lock(business, FixProposalInputV1.model_validate(locked))
    unknown = [item for item in business.claimed_capabilities if item not in business.capability_leafs]
    if unknown:
        raise OutputError(f"unknown capability: {unknown[0]}")
    if result.change_id != business.change_id:
        raise OutputError("proposal change_id does not match the locked change")
    allowed = set(business.allowed_paths)
    for item in result.proposals:
        if not item.eligible:
            continue
        if not item.files_to_modify:
            raise OutputError("eligible proposal requires files_to_modify")
        for path in item.files_to_modify:
            if path not in allowed or not under_root(path, business.allowed_roots):
                raise OutputError(f"undeclared target file: {path}")
            workspace_file(ctx.project_root, path)
    staged = workspace_file(ctx.write_root, PATH)
    try:
        staged_proposal = FixProposalResultV1.model_validate(json.loads(staged.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise OutputError("staged fix proposal is not the typed agent result") from error
    if staged_proposal != result:
        raise OutputError("staged fix proposal differs from the typed agent result")
    expected = canonical_json_bytes(cast(JSONValue, result.model_dump(mode="json"))) + b"\n"
    if staged.read_bytes() != expected:
        raise OutputError("staged fix proposal must use canonical JSON bytes")
    return result
