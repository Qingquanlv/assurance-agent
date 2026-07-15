from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE,
    NIGHTLY_NOOP,
    NIGHTLY_OK,
    NIGHTLY_PENDING_REVIEW,
)
from assurance_agent.retro.nightly.phase_a import (
    IsTerminal,
    enumerate_candidates,
    snapshot_unarchived_evidence,
)
from assurance_agent.retro.nightly.phase_d import (
    build_review_queue_markdown,
    partition_proposals_for_review,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import generate_retro_id, write_json
from assurance_agent.retro.proposals import read_proposals, validate_retro_proposals
from assurance_agent.retro.state import complete_retro_stage, mark_consumed_change, read_state
from assurance_agent.retro.types import RetroContext
from assurance_agent.workflow.core.state import read_state as read_workflow_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

AgentRunner = Callable[[str, Path], int]
ContextBuilder = Callable[..., RetroContext]


def _default_is_terminal(change_dir: Path, change_id: str) -> bool:
    project_root = change_dir.parents[2]
    try:
        schema = load_workflow_schema(project_root)
        state = read_workflow_state(change_dir)
        status = compute_status(
            schema,
            change_dir,
            state,
            state.params,
            scope="full",
        )
    except (AaError, OSError, ValueError):
        return False
    return status.terminal is not None


def collect_nightly(
    options: NightlyOptions,
    *,
    agent_runner: AgentRunner,
    context_builder: ContextBuilder = build_retro_context,
    is_terminal: IsTerminal = _default_is_terminal,
    now: datetime | None = None,
) -> int:
    sut = Path(options.sut)
    retro_id = options.retro_id or generate_retro_id(now)
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut / "qa" / "retro" / retro_id

    state = read_state(sut)
    candidates, _incomplete = enumerate_candidates(sut, state, is_terminal=is_terminal)
    if not candidates:
        return NIGHTLY_NOOP
    for candidate in candidates:
        if candidate.evidence_source == "unarchived":
            snapshot_unarchived_evidence(sut, retro_id, candidate.change_id)

    try:
        context = context_builder(sut, changes=[c.change_id for c in candidates], retro_id=retro_id)
    except Exception:  # noqa: BLE001 - aggregation failure is infrastructure failure
        return NIGHTLY_FAILURE
    write_json(retro_dir / "context.json", context.model_dump())

    consumed_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for candidate in candidates:
        mark_consumed_change(
            sut,
            change_id=candidate.change_id,
            source=candidate.evidence_source,
            consumed_at=consumed_at,
            retro_id=retro_id,
        )

    if count_signals(context) == 0:
        complete_retro_stage(sut, retro_id)
        return NIGHTLY_NOOP

    if options.dry_run:
        return NIGHTLY_OK

    retro_dir.mkdir(parents=True, exist_ok=True)
    agent_exit = agent_runner(options.agent, retro_dir)
    if agent_exit != 0:
        return NIGHTLY_FAILURE
    if not (retro_dir / "proposals.json").exists():
        return NIGHTLY_FAILURE

    proposals = read_proposals(retro_dir)
    proposals = [p for p in proposals if not validate_retro_proposals(context, [p])]
    if not proposals:
        complete_retro_stage(sut, retro_id)
        return NIGHTLY_NOOP

    partition = partition_proposals_for_review(
        proposals, promotions=[], min_evidence=options.min_evidence, rework_alert=options.rework_alert
    )
    (retro_dir / "review-queue.md").write_text(
        build_review_queue_markdown(retro_id, partition), encoding="utf-8"
    )
    complete_retro_stage(sut, retro_id)
    return NIGHTLY_OK


def resume_nightly(options: NightlyOptions, *, eval_runner: Callable[..., dict] | None = None) -> int:
    sut = Path(options.sut)
    retro_id = options.retro_id
    if not retro_id:
        return NIGHTLY_FAILURE
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = sut / "qa" / "retro" / retro_id
    if not (retro_dir / "proposals.json").exists():
        return NIGHTLY_FAILURE
    if options.skip_eval or eval_runner is None:
        return NIGHTLY_PENDING_REVIEW
    return NIGHTLY_OK


def report_nightly(options: NightlyOptions) -> int:
    sut = Path(options.sut)
    retro_root = sut / "qa" / "retro"
    runs = [p.name for p in retro_root.glob("retro-*")] if retro_root.is_dir() else []
    write_json(
        retro_root / "cross-run-report.json", {"runs": sorted(runs)[-options.last :], "count": len(runs)}
    )
    return NIGHTLY_OK
