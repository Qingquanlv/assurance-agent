from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from tempfile import mkdtemp

from assurance_agent.change_location import archive_root, resolve_change
from assurance_agent.eval.baseline import compare_with_baseline, read_baseline
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.apply import apply_proposal_to_stage
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
from assurance_agent.retro.types import RetroContext, RetroPromoteRecord
from assurance_agent.workflow.core.state import read_state as read_workflow_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

AgentRunner = Callable[[str, Path], int]
ContextBuilder = Callable[..., RetroContext]


def _default_is_terminal(project_root: Path, change_dir: Path, change_id: str) -> bool:
    """Default terminality probe. ``project_root`` is bound by ``collect_nightly``
    so no directory-depth reverse-derivation is needed (ADR-0002)."""
    try:
        if change_dir == archive_root(project_root) / change_id:
            # `aa-archive` only ever archives a change after every archive-gate
            # condition (execution PASS/PASS_WITH_WARNINGS, healing resolved,
            # review gates pass) already held — archived is terminal by
            # construction. Recomputing terminality via `compute_status` against
            # the archived directory is unreliable: the archive skill
            # intentionally does not copy `cases/<module>/case.yaml` (merged
            # into the stable `qa/cases/` file instead, not archived as a
            # process artifact), so produces-presence checks for case-design /
            # case-review phases spuriously fail against the archived copy.
            return True
        loc = resolve_change(project_root, change_id, prefer="active")
        schema = load_workflow_schema(project_root)
        state = read_workflow_state(loc.path)
        status = compute_status(
            schema,
            loc,
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
    is_terminal: IsTerminal | None = None,
    now: datetime | None = None,
) -> int:
    sut = Path(options.sut)
    # Bind project_root into the default probe so it needs no path reverse-derivation.
    if is_terminal is None:
        is_terminal = partial(_default_is_terminal, sut)
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
    """Resume after human review: stage promoted proposals, eval, promote or roll back.

    ``eval_runner`` signature::

        eval_runner(*, suite: str, sut_dir: Path, engine_root: Path,
                    extra_memory_dir: Path | None = None) -> dict
        # returns {"run_id": str, "verdict": str, "metrics": dict}
    """
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

    proposals = read_proposals(retro_dir)
    promotions_path = retro_dir / "promotions.json"
    existing: list[dict] = []
    if promotions_path.exists():
        raw = json.loads(promotions_path.read_text(encoding="utf-8"))
        existing = raw if isinstance(raw, list) else raw.get("promotions", [])

    promoted_ids = {
        str(item.get("proposal_id"))
        for item in existing
        if item.get("decision") == "promoted"
    }
    candidates = [
        p
        for p in proposals
        if p.apply_kind == "memory_append"
        and (p.status == "promoted" or p.id in promoted_ids)
    ]
    if not candidates:
        return NIGHTLY_PENDING_REVIEW

    by_suite: dict[str, list] = {}
    for proposal in candidates:
        suite = proposal.eval_suite or "workflow-run"
        by_suite.setdefault(suite, []).append(proposal)

    engine_root = Path.cwd()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_records: list[dict] = []
    eval_results: list[dict] = []

    for suite_name, suite_proposals in by_suite.items():
        stage_dir = Path(mkdtemp(prefix=f"retro-stage-{retro_id}-{suite_name}-"))
        try:
            for proposal in suite_proposals:
                apply_proposal_to_stage(
                    sut_root=sut,
                    retro_id=retro_id,
                    proposal_id=proposal.id,
                    stage_dir=stage_dir / proposal.id,
                )
            overlay = stage_dir / "overlay"
            memory = overlay / ".aa" / "memory"
            memory.mkdir(parents=True)
            for proposal in suite_proposals:
                src = stage_dir / proposal.id / ".aa" / "memory" / f"{proposal.id}.md"
                if src.exists():
                    shutil.copy2(src, memory / src.name)

            baseline = read_baseline(engine_root)
            baseline_entry = baseline.get(suite_name)
            result = eval_runner(
                suite=suite_name,
                sut_dir=sut,
                engine_root=engine_root,
                extra_memory_dir=overlay,
            )
            verdict = str(result.get("verdict", "inconclusive"))
            run_id = str(result.get("run_id", ""))

            if baseline_entry is None:
                gate_verdict = "inconclusive"
            else:
                run_path = sut / "eval" / "out" / "runs" / run_id
                gate_verdict = "pass"
                if run_path.is_dir():
                    delta = compare_with_baseline(run_path, baseline_entry.metrics)
                    for value in delta.values():
                        if value < -0.05:
                            gate_verdict = "fail"
                            break
                if verdict in {"fail", "inconclusive", "needs_human_review"}:
                    gate_verdict = "fail" if verdict == "fail" else verdict

            eval_results.append(
                {
                    "suite": suite_name,
                    "verdict": gate_verdict,
                    "eval_run_id": run_id,
                    "proposal_ids": [p.id for p in suite_proposals],
                }
            )
            decision = "promoted" if gate_verdict == "pass" else "needs_rework"
            for proposal in suite_proposals:
                record = RetroPromoteRecord(
                    proposal_id=proposal.id,
                    decision=decision,  # type: ignore[arg-type]
                    decided_by="eval-gate",
                    decided_at=now,
                    rework_note=None if decision == "promoted" else f"eval gate={gate_verdict}",
                    eval_run_id=run_id or None,
                )
                new_records.append(record.model_dump(mode="json"))
                if decision == "promoted":
                    dest_dir = sut / ".aa" / "memory"
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    src = memory / f"{proposal.id}.md"
                    if src.exists():
                        shutil.copy2(src, dest_dir / src.name)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    write_json(promotions_path, existing + new_records)
    write_json(retro_dir / "eval-results.json", {"results": eval_results})
    if any(r.get("verdict") == "inconclusive" for r in eval_results):
        return NIGHTLY_PENDING_REVIEW
    if any(r.get("decision") == "needs_rework" for r in new_records):
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
