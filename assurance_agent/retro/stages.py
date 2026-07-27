"""Narrow facade exposing Retro stages to workflow graph handlers."""

from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.assemble import assemble_context, write_noop_receipt
from assurance_agent.retro.candidates import CandidateBatchInvalid
from assurance_agent.retro.eval_history import FileEvalHistoryReader
from assurance_agent.retro.fallback import (
    materialize_evidence_gap_fallback,
    materialize_pipeline_failure_fallback,
)
from assurance_agent.retro.slices import materialize_slices
from assurance_agent.retro.supervisor import RetroInvocation, finalize_retro_status
from assurance_agent.retro.window import BatchScopeContractError, RetroWindowSelection
from assurance_agent.retro.workflow_history import (
    LedgerWorkflowHistoryReader,
    WorkflowHistoryIntegrityError,
)

__all__ = [
    "FileEvalHistoryReader",
    "LedgerWorkflowHistoryReader",
    "BatchScopeContractError",
    "CandidateBatchInvalid",
    "RetroWindowSelection",
    "RetroInvocation",
    "WorkflowHistoryIntegrityError",
    "assemble_context",
    "finalize_retro_status",
    "materialize_slices",
    "materialize_evidence_gap_fallback",
    "materialize_pipeline_failure_fallback",
    "run_retro_accept",
    "write_noop_receipt",
]
