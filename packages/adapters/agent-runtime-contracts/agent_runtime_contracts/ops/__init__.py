"""Shared Agent request kit for capability prepare and finalize handlers."""

from agent_runtime_contracts.ops.binding import AgentBindingDataV1, validate_binding
from agent_runtime_contracts.ops.errors import (
    InputError,
    OutputError,
    failed_input,
    failed_output,
    validate_model,
    validate_output,
)
from agent_runtime_contracts.ops.op import run_finalize, run_prepare
from agent_runtime_contracts.ops.router import (
    AgentOp,
    AgentOpFinalizeInputV1,
    FinalizeContext,
    OpRequest,
    OpRouter,
    PrepareContext,
    TaskOp,
    WriteScopeError,
)
from agent_runtime_contracts.ops.request import (
    BOUNDED_PROFILES,
    WorkspaceRoots,
    agent_run_request,
    agent_workspace,
    logical_write_root,
    prepared_outcome,
    result_contract_from,
    skill_request,
)

__all__ = [
    "AgentBindingDataV1",
    "AgentOp",
    "AgentOpFinalizeInputV1",
    "BOUNDED_PROFILES",
    "FinalizeContext",
    "InputError",
    "OpRequest",
    "OpRouter",
    "OutputError",
    "PrepareContext",
    "TaskOp",
    "WorkspaceRoots",
    "WriteScopeError",
    "agent_run_request",
    "agent_workspace",
    "failed_input",
    "failed_output",
    "logical_write_root",
    "prepared_outcome",
    "result_contract_from",
    "run_finalize",
    "run_prepare",
    "skill_request",
    "validate_binding",
    "validate_model",
    "validate_output",
]
