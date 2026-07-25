from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from tempfile import mkdtemp

from assurance_agent.change_location import archive_root, resolve_change
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_path_segment_safe
from assurance_agent.retro.accept_stage import run_retro_accept
from assurance_agent.retro.aggregator import build_retro_context
from assurance_agent.retro.apply import (
    apply_memory_proposal,
    apply_proposal_to_stage,
    resolve_memory_target,
)
from assurance_agent.retro.collect_stage import ContextBuilder, RetroCollectResult, run_retro_collect
from assurance_agent.retro.nightly.exit_codes import (
    NIGHTLY_FAILURE,
    NIGHTLY_NOOP,
    NIGHTLY_OK,
    NIGHTLY_PENDING_REVIEW,
)
from assurance_agent.retro.nightly.phase_a import IsTerminal
from assurance_agent.retro.nightly.phase_d import (
    build_review_queue_markdown,
    partition_proposals_for_review,
)
from assurance_agent.retro.nightly.phase_f import (
    classify_eval_gate,
    compare_suite_regression,
    should_auto_apply,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import generate_retro_id, write_json
from assurance_agent.retro.promotions import (
    application_event,
    append_promotion_events,
    eval_completed_event,
    proposal_states,
    read_promotion_events,
)
from assurance_agent.retro.proposals import read_proposals
from assurance_agent.retro.types import RetroContext
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation

AgentRunner = Callable[[str, Path], int]


def _default_is_terminal(project_root: Path, change_dir: Path, change_id: str) -> bool:
    """Default terminality probe. ``project_root`` is bound by ``collect_nightly``
    so no directory-depth reverse-derivation is needed (ADR-0002)."""
    try:
        if change_dir == archive_root(project_root) / change_id:
            # `aa-archive` only ever archives a change after every archive-gate
            # condition already held — archived is terminal by construction.
            # Re-projecting against the archived directory is unreliable: the
            # archive skill intentionally does not copy all case artifacts.
            return True
        loc = resolve_change(project_root, change_id, prefer="active")
        invocation_id = CheckpointStore(loc.path).latest_root_invocation()
        if invocation_id is None:
            return False
        projection = project_invocation(loc.path, invocation_id)
    except (AaError, OSError, ValueError):
        return False
    return projection.terminal in {"completed", "stopped", "failed"}


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

    try:
        result = run_retro_collect(
            sut,
            retro_id=retro_id,
            context_builder=context_builder,
            is_terminal=is_terminal,
            now=now,
        )
    except Exception:  # noqa: BLE001 - aggregation failure is infrastructure failure
        return NIGHTLY_FAILURE

    if result.signal_count == 0:
        # Stage completion for zero-signal candidates is owned by run_retro_collect.
        return NIGHTLY_NOOP

    if options.dry_run:
        return NIGHTLY_OK

    retro_dir = result.retro_dir
    retro_dir.mkdir(parents=True, exist_ok=True)
    agent_exit = agent_runner(options.agent, retro_dir)
    if agent_exit != 0:
        return NIGHTLY_FAILURE
    if not (retro_dir / "proposals.json").exists():
        return NIGHTLY_FAILURE

    try:
        proposals = run_retro_accept(
            sut,
            retro_id=retro_id,
            min_evidence=options.min_evidence,
            rework_alert=options.rework_alert,
        )
    except AaError:
        return NIGHTLY_FAILURE

    if not proposals:
        return NIGHTLY_NOOP

    return NIGHTLY_OK


# spec §6.4: resume (re-)runs proposals in these states; applied/rejected are
# terminal, rolled_back/needs_rework wait for a fresh human review decision.
RETRYABLE_EVAL_STATES = frozenset({"promoted_pending_eval", "awaiting_baseline", "eval_error"})


def _candidate_metrics(result: dict) -> dict[str, float]:
    """Candidate aggregate metrics from the injected eval_runner result."""
    metrics = result.get("metrics")
    if not metrics:
        return {}
    return {str(name): float(value) for name, value in dict(metrics).items()}


def _hard_gate_failures(result: dict) -> list[str]:
    """Hard gate failures from the injected eval_runner result."""
    failures = result.get("hard_gate_failures")
    if failures is None:
        return []
    return [str(failure) for failure in failures]


def _eval_suite_group(
    *,
    sut: Path,
    engine_root: Path,
    retro_id: str,
    suite_name: str,
    suite_proposals: list,
    stage_dir: Path,
    eval_runner: Callable[..., dict],
    at: str,
) -> tuple[list[dict], dict]:
    """Stage one suite group, run the eval gate and classify per spec §6.

    Fixed decision order: no approved baseline -> inconclusive (never applied);
    candidate gate fail/inconclusive/needs_human_review or hard gate failures
    -> regression; provenance mismatch/missing evidence -> inconclusive;
    direction-aware metric regression -> rollback; otherwise pass. A pass lands
    real memory only when the suite declares hard gates. Returns the promotion
    events plus the eval-results entry; raises on infrastructure failure so the
    caller can record ``eval_error`` for the whole group.
    """
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

    result = eval_runner(
        suite=suite_name,
        sut_dir=sut,
        engine_root=engine_root,
        extra_memory_dir=overlay,
    )
    run_id = str(result.get("run_id", ""))
    gate_verdict = classify_eval_gate(result)
    hard_failures = _hard_gate_failures(result)
    suite_raw = result.get("suite_contract")
    suite_contract = dict(suite_raw) if isinstance(suite_raw, dict) else {}
    baseline_raw = result.get("baseline_metrics")
    baseline_metrics = (
        {str(name): float(value) for name, value in dict(baseline_raw).items()}
        if isinstance(baseline_raw, dict)
        else None
    )
    run_ids = [run_id] if run_id else []
    outcome = {
        "suite": suite_name,
        "eval_run_id": run_id,
        "proposal_ids": [p.id for p in suite_proposals],
        "auto_apply": False,
    }

    def gate_events(eval_result: str, note: str | None) -> list[dict]:
        return [
            eval_completed_event(
                proposal.id,
                result=eval_result,
                actor="eval-gate",
                at=at,
                run_ids=run_ids,
                gate=gate_verdict,
                note=note,
            )
            for proposal in suite_proposals
        ]

    if baseline_metrics is None:
        note = (
            f"no approved baseline for suite {suite_name!r}; approve one with: "
            f"aa eval baseline update --suite {suite_name} "
            f"--run {run_id or '<run-id>'} --approved-by <actor>"
        )
        outcome.update(verdict="inconclusive", note=note)
        return gate_events("inconclusive", note), outcome

    regression_note: str | None = None
    if gate_verdict in {"fail", "inconclusive", "needs_human_review"} or hard_failures:
        regression_note = f"eval gate={gate_verdict}"
        if hard_failures:
            regression_note += f" hard_gate_failures={','.join(hard_failures)}"
    else:
        candidate_metrics = _candidate_metrics(result)
        comparison = compare_suite_regression(
            baseline_metrics,
            candidate_metrics,
            suite_contract,
            baseline_provenance={
                "suite_version": result.get("baseline_suite_version"),
                "repeat": result.get("baseline_repeat"),
                "regression_policy_sha256": result.get("baseline_regression_policy_sha256"),
            },
            candidate_provenance={
                "suite_version": result.get("suite_version"),
                "repeat": result.get("repeat"),
                "regression_policy_sha256": result.get("regression_policy_sha256"),
            },
        )
        if comparison.inconclusive:
            note = f"regression comparison inconclusive: {'; '.join(comparison.details)}"
            outcome.update(verdict="inconclusive", note=note)
            return gate_events("inconclusive", note), outcome
        if comparison.regressed:
            regression_note = f"baseline regression: {'; '.join(comparison.details)}"
        elif not should_auto_apply(comparison, suite_contract):
            note = f"suite {suite_name!r} has no hard gates; manual review required before apply"
            outcome.update(verdict="pass", note=note)
            return gate_events("pass", note), outcome

    if regression_note is not None:
        outcome.update(verdict="regression", note=regression_note)
        events = gate_events("regression", regression_note)
        events.extend(
            application_event(
                proposal.id,
                result="rolled_back",
                actor="eval-gate",
                at=at,
                note=regression_note,
            )
            for proposal in suite_proposals
        )
        return events, outcome

    outcome.update(verdict="pass", auto_apply=True)
    events = gate_events("pass", None)
    for proposal in suite_proposals:
        apply_memory_proposal(sut, retro_id, proposal)
        target = resolve_memory_target(sut, proposal.target)
        events.append(
            application_event(
                proposal.id,
                result="applied",
                actor="eval-gate",
                at=at,
                target=proposal.target,
                content_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            )
        )
    return events, outcome


def resume_nightly(options: NightlyOptions, *, eval_runner: Callable[..., dict] | None = None) -> int:
    """Resume after human review: stage promoted proposals, eval, promote or roll back.

    ``eval_runner`` signature::

        eval_runner(*, suite: str, sut_dir: Path, engine_root: Path,
                    extra_memory_dir: Path | None = None) -> dict
        # returns {"run_id": str, "verdict": str, "metrics": dict,
        #          "hard_gate_failures": list[str],
        #          "suite_contract": dict, "baseline_metrics": dict | None}
        # suite_contract / baseline_metrics are supplied by the commands-layer
        # factory so retro never imports the eval package.
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
    # promotions.json is an append-only event stream (schema_version "2");
    # legacy list / {"promotions": [...]} formats are coerced on read.
    states = proposal_states(read_promotion_events(retro_dir))
    candidates = [
        p
        for p in proposals
        if p.apply_kind == "memory_append"
        and (states.get(p.id) in RETRYABLE_EVAL_STATES or (p.id not in states and p.status == "promoted"))
    ]
    if not candidates:
        return NIGHTLY_PENDING_REVIEW

    by_suite: dict[str, list] = {}
    for proposal in candidates:
        suite = proposal.eval_suite or "workflow-run"
        by_suite.setdefault(suite, []).append(proposal)

    engine_root = Path(options.engine_root).resolve() if options.engine_root else Path.cwd()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_events: list[dict] = []
    eval_results: list[dict] = []

    for suite_name, suite_proposals in by_suite.items():
        stage_dir = Path(mkdtemp(prefix=f"retro-stage-{retro_id}-{suite_name}-"))
        try:
            group_events, outcome = _eval_suite_group(
                sut=sut,
                engine_root=engine_root,
                retro_id=retro_id,
                suite_name=suite_name,
                suite_proposals=suite_proposals,
                stage_dir=stage_dir,
                eval_runner=eval_runner,
                at=now,
            )
            new_events.extend(group_events)
        except Exception as err:  # noqa: BLE001 - infrastructure failure: eval_error, retryable
            note = f"eval infrastructure error: {err}"
            new_events.extend(
                eval_completed_event(proposal.id, result="error", actor="eval-gate", at=now, note=note)
                for proposal in suite_proposals
            )
            outcome = {
                "suite": suite_name,
                "verdict": "error",
                "eval_run_id": "",
                "proposal_ids": [p.id for p in suite_proposals],
                "auto_apply": False,
                "note": note,
            }
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        eval_results.append(outcome)

    if new_events:
        append_promotion_events(retro_dir, new_events)
    write_json(retro_dir / "eval-results.json", {"results": eval_results})
    if any(result.get("verdict") == "error" for result in eval_results):
        return NIGHTLY_FAILURE
    if any(result.get("verdict") != "pass" or not result.get("auto_apply") for result in eval_results):
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
