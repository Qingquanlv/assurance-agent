"""workflow-state.yaml — canonical cross-milestone state type (versioned).

This is the ONE state type the whole series binds to.  Known cross-milestone
fields are explicit and typed; `extra="allow"` is compatibility-only for phase
ids and extension data not yet promoted to the canonical contract.  In
particular `phases.healing.attempts_used` is never represented by an untyped
mapping, so a misspelling cannot silently cross the M2/M3/M6 boundary.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PhaseState(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    skill_loaded: bool | None = None
    skill_md_path: str | None = None
    skill_loaded_at: str | None = None
    batch_id: str | None = None
    inspect_mode: str | None = None


class HealingPhaseState(PhaseState):
    attempts_used: int = Field(default=0, ge=0)
    all_fixers_no_op: bool = False


class WorkflowPhases(BaseModel):
    model_config = ConfigDict(extra="allow")

    skill_registry_check: PhaseState | None = None
    execution: PhaseState | None = None
    inspect: PhaseState | None = None
    healing: HealingPhaseState = Field(default_factory=HealingPhaseState)


class WorkflowGates(BaseModel):
    model_config = ConfigDict(extra="allow")

    healing_available: bool | None = None


class RunContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    orchestrator_skill: str | None = None
    interaction_mode: str | None = None
    active_scope: str | None = None
    stamped_at: str | None = None


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    phases: WorkflowPhases = Field(default_factory=WorkflowPhases)
    gates: WorkflowGates = Field(default_factory=WorkflowGates)
    run_context: RunContext = Field(default_factory=RunContext)
