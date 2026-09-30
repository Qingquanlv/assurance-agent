from __future__ import annotations

import agent_runtime_contracts

EXPECTED = frozenset(
    {
        "AgentExecutionContract",
        "AgentPhaseWriteClaims",
        "AgentRunRequest",
        "AgentRunResult",
        "AgentRuntimeBinding",
        "AgentRuntimeCapabilities",
        "AgentRuntimePolicy",
        "AgentWorkspaceV1",
        "FinalizePhase",
        "FinallyContext",
        "FrozenExecutionSelection",
        "InstructionPart",
        "PreparePhase",
        "RAW_AGENT_RUNTIME_BINDING_SCHEMA_VERSION",
        "RawAgentRuntimeBindingProjectionV1",
        "RawAgentRuntimeOutcome",
        "RawFinalizeBundle",
        "ReadOnlyRawWorkspace",
        "ResolvedRawAgentExecutor",
        "ResultContract",
        "RuntimePhase",
        "after",
        "before",
        "bound_redacted_diagnostics",
        "canonical_digest",
        "canonical_json_bytes",
        "finally_",
        "prompt_model_json",
        "qa_join",
        "qa_route",
        "rebind_agent_run_workspace",
        "result_schema_from_model",
        "validate_local_agent_result",
        "validate_structured_result",
        "with_validation_retry",
    }
)


def test_public_surface_is_stable() -> None:
    assert frozenset(agent_runtime_contracts.__all__) == EXPECTED
    for name in EXPECTED:
        assert getattr(agent_runtime_contracts, name) is not None
