from __future__ import annotations

import logging
from dataclasses import dataclass

from agent_runtime_contracts import (
    AgentRunRequest,
    FinalizePhase,
    FinallyContext,
    PreparePhase,
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    RuntimePhase,
    after,
    before,
    finally_,
)
from graph_engine.attempts import AuthorizedAttemptScope, PermanentTaskFailure

from assurance_intake.contracts.agent import (
    ArtifactListResultV1,
    CaseDesignInputV1,
    CaseDesignOutputV1,
)
from assurance_intake.contracts.attempts import AGENT_JOB_CONTRACTS

logger = logging.getLogger(__name__)

CaseDesignBundle = RawFinalizeBundle[CaseDesignInputV1, AgentRunRequest, ArtifactListResultV1]


@dataclass
class CaseDesignTask:
    contract = AGENT_JOB_CONTRACTS["case-design"]

    prepare_phase: PreparePhase[CaseDesignInputV1, AgentRunRequest]
    opencode: RuntimePhase[AgentRunRequest]
    finalize_phase: FinalizePhase[
        CaseDesignInputV1, AgentRunRequest, ArtifactListResultV1, CaseDesignOutputV1
    ]

    @before
    async def prepare(
        self, validated_input: CaseDesignInputV1, scope: AuthorizedAttemptScope
    ) -> AgentRunRequest | PermanentTaskFailure:
        return await self.prepare_phase.execute(validated_input, scope)

    async def run(
        self, prepared: AgentRunRequest, scope: AuthorizedAttemptScope
    ) -> RawAgentRuntimeOutcome | PermanentTaskFailure:
        return await self.opencode.execute(prepared, scope)

    @after
    async def finalize(
        self, bundle: CaseDesignBundle, scope: AuthorizedAttemptScope
    ) -> CaseDesignOutputV1 | PermanentTaskFailure:
        return await self.finalize_phase.execute(bundle, scope)

    @finally_
    async def on_exit(self, context: FinallyContext) -> None:
        logger.debug(
            "case-design segment exited: attempt=%s mode=%s outcome=%s",
            context.attempt_key,
            context.mode,
            context.outcome,
        )
