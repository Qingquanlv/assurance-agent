"""Prepare the fix-proposal Agent request."""

from __future__ import annotations

import hashlib

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.ops import AgentBindingDataV1, InputError, OutputError, run_prepare
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_healing.contracts.agent import FixProposalInputV1
from assurance_healing.operations.agent import (
    FIX_PROPOSAL_RESULT_ID,
    FIX_PROPOSAL_SKILL,
    FIX_RESULT_FILE,
    prepare_request,
    workspace_file,
)


def _build(
    business: FixProposalInputV1,
    binding: AgentBindingDataV1,
    context: TaskContext,
) -> AgentRunRequest:
    if business.issue_analysis_ref is not None:
        ref = business.issue_analysis_ref
        data = workspace_file(context.project_root, ref.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.digest:
            raise OutputError("issue analysis digest changed")
    return prepare_request(
        skill_path=FIX_PROPOSAL_SKILL,
        business=business,
        binding=binding,
        result_id=FIX_PROPOSAL_RESULT_ID,
        result_file=FIX_RESULT_FILE,
        context=context,
        allowed_outputs=("qa/results/healing/fix-proposal.json",),
    )


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_prepare(
        request,
        context,
        input_model=FixProposalInputV1,
        build=_build,
        input_errors=(InputError, OutputError),
    )
