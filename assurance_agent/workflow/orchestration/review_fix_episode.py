"""Bounded review→fix→re-review loop projection (case / api-plan / e2e-plan).

Symmetric with the healing episode: a review phase produces a review artifact
whose gate may return ``needs_fix``; a fixer phase then applies safe fixes and
the reviewer is re-run. The cycle is bounded by ``max_<...>_fix_attempts`` and
terminates ``stopped`` once the fixer budget is exhausted without the gate
reaching ``pass``.

This closes two migration gaps that let the DAG's plain ``repair_of`` routing
spin forever:

1. **No re-review edge** — after a fix, the stale review artifact still reads
   ``needs_fix`` and nothing re-ran the reviewer, so the gate never re-evaluated.
2. **No attempt bound** — ``max_attempts_param`` was declared on the fix phase
   but never enforced at runtime.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.change_location import ChangeLocation
from assurance_agent.workflow.core.events import Ledger, event_seq
from assurance_agent.workflow.orchestration.gates import check_gate, resolve_change_path
from assurance_agent.workflow.orchestration.loop_registry import LoopContext, LoopSnapshot, register
from assurance_agent.workflow.orchestration.schema import LoopDef, WorkflowSchema

_COMMIT = "phase_outcome_committed"


class ReviewFixSnapshot(BaseModel):
    """Projection of a single review_fix loop for one ``compute_status`` call."""

    loop_id: str
    state: Literal["inactive", "active", "terminal"]
    dispatch: list[str] = Field(default_factory=list)
    attempts_used: int = 0
    terminal_kind: Literal["stopped"] | None = None
    reason: str | None = None


def project_review_fix_loop(
    schema: WorkflowSchema,
    loc: ChangeLocation,
    state: WorkflowState,
    params: dict,
    loop: LoopDef,
    *,
    review_active: bool,
) -> ReviewFixSnapshot:
    """Decide the next action for one bounded review→fix loop.

    Returns ``inactive`` when the loop must not drive anything (loop phase out of
    scope / pruned, first review not yet produced, or gate not ``needs_fix``);
    ``active`` with a single dispatch (the fixer, or a reviewer re-run); or
    ``terminal`` ``stopped`` when the fixer budget is exhausted.
    """
    inactive = ReviewFixSnapshot(loop_id=loop.id, state="inactive")
    if not review_active:
        return inactive

    # The very first review is an ordinary DAG phase; the loop only engages once
    # its review artifact exists on disk (so the gate can be adjudicated).
    review_produces = schema.phase_produces(loop.review_phase) or []
    review_artifact = review_produces[0] if review_produces else None
    if review_artifact is None or not resolve_change_path(loc, review_artifact).exists():
        return inactive

    verdict = check_gate(schema, loop.gate, loc, state, params).verdict.value
    if verdict != "needs_fix":
        # pass / needs_human_review / reject / stop are handled by ordinary gate
        # routing and terminal aggregation; the loop stays out of the way.
        return inactive

    ledger = Ledger(loc.path)
    latest_review = ledger.latest(type=_COMMIT, phase=loop.review_phase)
    latest_fix = ledger.latest(type=_COMMIT, phase=loop.fix_phase)
    review_seq = event_seq(latest_review) if latest_review else 0
    fix_seq = event_seq(latest_fix) if latest_fix else 0
    attempts = len(ledger.filter(type=_COMMIT, phase=loop.fix_phase))

    if fix_seq > review_seq:
        # A fix landed against the current review → re-run the reviewer so the
        # gate re-adjudicates the freshly patched plan.
        return ReviewFixSnapshot(
            loop_id=loop.id,
            state="active",
            dispatch=[loop.review_phase],
            attempts_used=attempts,
        )

    maximum = int(params.get(loop.max_param, 0) or 0)
    if attempts >= maximum:
        return ReviewFixSnapshot(
            loop_id=loop.id,
            state="terminal",
            attempts_used=attempts,
            terminal_kind="stopped",
            reason=f"{loop.fix_phase} attempts exhausted: {attempts}/{maximum}",
        )

    return ReviewFixSnapshot(
        loop_id=loop.id,
        state="active",
        dispatch=[loop.fix_phase],
        attempts_used=attempts,
    )


def _project(ctx: LoopContext, loop: LoopDef) -> LoopSnapshot:
    """Registry adapter: bounded review→fix loop → unified LoopSnapshot."""
    if ctx.phase_active is None:
        from assurance_agent.workflow.orchestration.loop_registry import LoopRegistryError

        raise LoopRegistryError("review_fix loop requires LoopContext.phase_active")
    snap = project_review_fix_loop(
        ctx.schema,
        ctx.loc,
        ctx.state,
        ctx.params,
        loop,
        review_active=ctx.phase_active(loop.review_phase),
    )
    return LoopSnapshot(
        loop_id=loop.id,
        state=snap.state,
        dispatch=list(snap.dispatch),
        # The loop owns its fix phase even when not dispatching it (reviewer
        # re-run in flight or budget exhausted): keep a `ready` view honest.
        block_members=[] if loop.fix_phase in snap.dispatch else [loop.fix_phase],
        terminal_kind=snap.terminal_kind,
        terminal_reason=snap.reason,
        terminal_phase=loop.fix_phase if snap.terminal_kind else None,
    )


register("review_fix", _project)
