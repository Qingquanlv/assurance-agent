"""Loop-kind registry — loops as self-describing nested execution units (subgraph 协议).

The engine drives loops only through :class:`LoopSnapshot`: which phases to
dispatch, which owned members to keep blocked, which control actions to
commit, and whether the loop reached a terminal state. Each loop *kind*
(healing, review_fix, …) registers a projector here; adding a new kind never
touches ``engine.py``.

Import discipline: this module is a **leaf** inside ``workflow.orchestration``
— it must not import the episode modules, or registration-at-import would
deadlock. The control-action vocabulary (:class:`HealingAttemptIntent` /
:class:`HealingEpisodeAction`) therefore lives here and is re-exported by
``healing_episode`` for backward compatibility. Kind-specific snapshots
(e.g. ``HealingEpisodeSnapshot``) stay with their episode module and cross
this boundary as the opaque :attr:`LoopSnapshot.episode` payload.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.change_location import ChangeLocation
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.orchestration.healing_state import HealingStateSnapshot
from assurance_agent.workflow.orchestration.schema import LoopDef, WorkflowSchema


class LoopRegistryError(AaError):
    """A schema loop declares a kind with no registered projector."""


# ---- control-action vocabulary (engine ↔ loop projector boundary) ------------
class HealingAttemptIntent(BaseModel):
    episode_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    source_batch_id: str = Field(min_length=1)
    pin_entry_baseline: bool


class HealingEpisodeAction(BaseModel):
    kind: Literal["dispatch_phase", "allocate_attempt", "await_human", "complete"]
    phase: str | None = None
    allocation: HealingAttemptIntent | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        valid = (
            (
                self.kind == "dispatch_phase"
                and self.phase is not None
                and self.allocation is None
                and self.outcome is None
            )
            or (
                self.kind == "allocate_attempt"
                and self.phase is None
                and self.allocation is not None
                and self.outcome is None
            )
            or (
                self.kind == "await_human"
                and self.phase is None
                and self.allocation is None
                and self.outcome is None
            )
            or (
                self.kind == "complete"
                and self.phase is None
                and self.allocation is None
                and self.outcome is not None
            )
        )
        if not valid:
            raise ValueError(f"invalid healing action shape for kind={self.kind}")
        return self


class LoopSnapshot(BaseModel):
    """Unified projection of one loop for one ``compute_status`` call."""

    loop_id: str
    state: Literal["inactive", "active", "awaiting_human", "terminal"] = "inactive"
    # Phases the loop wants dispatched now (engine re-adds them to next_dispatch).
    dispatch: list[str] = Field(default_factory=list)
    # Owned member phases the loop is NOT dispatching: engine marks a `ready`
    # ordinary-DAG view of these `blocked` so loop membership stays exclusive.
    block_members: list[str] = Field(default_factory=list)
    # Non-dispatch control actions (allocate_attempt / await_human / complete).
    control_actions: list[HealingEpisodeAction] = Field(default_factory=list)
    terminal_kind: str | None = None  # e.g. "stopped"
    terminal_reason: str | None = None
    terminal_phase: str | None = None
    # Opaque kind-specific payload (healing: HealingEpisodeSnapshot) for
    # callers that need more than the unified vocabulary.
    episode: object | None = None


@dataclass(frozen=True)
class LoopContext:
    schema: WorkflowSchema
    loc: ChangeLocation
    state: WorkflowState
    params: dict
    # healing kind only: ledger-derived healing counters overlaid by the engine.
    derived_healing: HealingStateSnapshot | None = None
    # review_fix kind only: whether a phase is inside the active (non-pruned) scope.
    phase_active: Callable[[str], bool] | None = None


LoopProjector = Callable[[LoopContext, LoopDef], LoopSnapshot]

_REGISTRY: dict[str, LoopProjector] = {}


def register(kind: str, projector: LoopProjector) -> None:
    if not kind:
        raise LoopRegistryError("loop kind must be non-empty")
    _REGISTRY[kind] = projector


def registered_kinds() -> frozenset[str]:
    return frozenset(_REGISTRY)


def project(ctx: LoopContext, loop: LoopDef) -> LoopSnapshot:
    projector = _REGISTRY.get(loop.kind)
    if projector is None:
        raise LoopRegistryError(
            f"loop '{loop.id}' kind '{loop.kind}' has no registered projector "
            f"(registered: {sorted(_REGISTRY) or 'none'})"
        )
    return projector(ctx, loop)
