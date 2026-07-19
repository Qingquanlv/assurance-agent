from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.apply import (
    apply_memory_proposal,
    deprecate_memory_block,
    resolve_memory_target,
)
from assurance_agent.retro.nightly.agent import run_agent
from assurance_agent.retro.nightly.driver import (
    collect_nightly,
    report_nightly,
    resume_nightly,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.promotions import (
    TERMINAL_STATES,
    append_promotion_events,
    application_event,
    effective_review_decisions,
    proposal_states,
    read_promotion_events,
    review_decision_event,
)
from assurance_agent.retro.proposals import read_proposals, validate_retro_proposals
from assurance_agent.retro.state import complete_retro_stage, mark_consumed_change
from assurance_agent.retro.types import RetroContext


def _build_eval_runner(data_root: Path, sut_root: Path):
    """Shared real ``eval_runner`` factory (spec §5).

    ``aa retro promote`` and ``aa retro nightly resume`` both build their
    runner here so the two entry points never diverge. Explicit
    ``sut_dir``/``engine_root`` call kwargs (passed by ``resume_nightly``)
    win over the bound roots.
    """

    def eval_runner(
        *,
        suite: str,
        sut_dir: Path | None = None,
        engine_root: Path | None = None,
        extra_memory_dir: Path | None = None,
    ) -> dict:
        import os

        from assurance_agent.commands.eval_cmd import _resolve_adapter_factory
        from assurance_agent.eval.baseline import read_baseline, read_run_manifest
        from assurance_agent.eval.metrics import read_metrics
        from assurance_agent.eval.paths import run_dir as run_dir_for
        from assurance_agent.eval.plan import load_suite
        from assurance_agent.eval.runner import run_suite

        sut_dir = Path(sut_dir) if sut_dir is not None else sut_root
        engine_root = Path(engine_root) if engine_root is not None else data_root
        suite_obj, suite_file = load_suite(engine_root, suite)
        baseline_entry = read_baseline(engine_root).get(suite)

        # Real validation: use the real agent adapter (cursor-agent) so the
        # candidate memory overlay actually influences generation. Fake stays
        # available as an explicit opt-in (AA_EVAL_FAKE_ADAPTER) for CI/tests
        # and for deterministic suites where memory has no effect.
        use_fake = bool(os.environ.get("AA_EVAL_FAKE_ADAPTER"))
        adapter_factory = _resolve_adapter_factory(use_fake=use_fake, sut=sut_dir)

        run_id, gate = run_suite(
            suite_file=suite_file,
            project_root=engine_root,
            sut_dir=sut_dir,
            adapter_factory=adapter_factory,
            fixtures_root=sut_dir / "eval-fixtures",
            extra_memory_dir=extra_memory_dir,
            repeat=suite_obj.regression.repeat if suite_obj.regression is not None else 1,
        )
        manifest = read_run_manifest(run_dir_for(sut_dir, run_id))
        metrics = {}
        try:
            metrics = read_metrics(run_dir_for(sut_dir, run_id)).metrics
        except Exception:
            pass
        return {
            "run_id": run_id,
            "verdict": gate.verdict,
            "metrics": metrics,
            "hard_gate_failures": list(gate.hard_gate_failures),
            "suite_contract": suite_obj.model_dump(mode="json"),
            "baseline_metrics": baseline_entry.metrics if baseline_entry is not None else None,
            "suite_version": manifest.suite_version,
            "repeat": manifest.repeat,
            "regression_policy_sha256": manifest.regression_policy_sha256,
            "baseline_suite_version": (baseline_entry.suite_version if baseline_entry is not None else None),
            "baseline_repeat": baseline_entry.repeat if baseline_entry is not None else None,
            "baseline_regression_policy_sha256": (
                baseline_entry.regression_policy_sha256 if baseline_entry is not None else None
            ),
        }

    return eval_runner


def _run_retro(since, changes, retro_id, out, as_json) -> None:
    project_root = Path.cwd()
    if since and changes:
        click.echo("Error: --since and --change are mutually exclusive", err=True)
        raise SystemExit(2)
    context = build_retro_context(
        project_root, since=since, changes=list(changes) if changes else None, retro_id=retro_id
    )
    retro_dir = project_root / "qa" / "retro" / context.retro_id
    out_path = Path(out).resolve() if out else (retro_dir / "context.json")
    inside = retro_dir in out_path.parents or out_path.parent == retro_dir
    if inside and (retro_dir / "promotions.json").exists():
        click.echo(
            f"Error: retro dir already contains promotions.json and is immutable: {context.retro_id}",
            err=True,
        )
        raise SystemExit(1)
    write_json(out_path, context.model_dump())

    consumed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for source in context.window.change_sources:
        mark_consumed_change(
            project_root,
            change_id=source.change_id,
            source=source.evidence_source,
            consumed_at=consumed_at,
            retro_id=context.retro_id,
        )

    summary = {
        "retro_id": context.retro_id,
        "change_count": context.window.change_count,
        "signal_count": count_signals(context),
    }
    if as_json:
        click.echo(json.dumps(summary))
        return
    click.echo(f"retro_id: {summary['retro_id']}")
    click.echo(f"change_count: {summary['change_count']}")
    click.echo(f"signal_count: {summary['signal_count']}")


def register_retro(main_group: click.Group) -> None:
    @click.group("retro", invoke_without_command=True)
    @click.option("--since", help="Scan archived changes archived at/after this ISO date")
    @click.option("--change", "changes", multiple=True, help="Archived change id (repeatable)")
    @click.option("--retro-id", "retro_id", help="Retro id for output dir and context")
    @click.option("--out", help="Output path for context.json")
    @click.option("--json", "as_json", is_flag=True, help="Output { retro_id, change_count, signal_count }")
    @click.pass_context
    def retro(ctx, since, changes, retro_id, out, as_json) -> None:
        """Aggregate archived QA evidence into retro context."""
        if ctx.invoked_subcommand is not None:
            return
        _run_retro(since, changes, retro_id, out, as_json)

    _register_nightly(retro)
    _register_promotion_commands(retro)
    main_group.add_command(retro)


def _register_promotion_commands(retro: click.Group) -> None:
    @retro.command("promote")
    @click.option("--retro", "retro_id", required=True, help="Retro id")
    @click.option("--proposal", "proposal_id", required=True, help="Proposal id")
    @click.option(
        "--decision",
        type=click.Choice(["promoted", "rejected", "needs_rework"]),
        required=True,
        help="Review decision",
    )
    @click.option("--decided-by", required=True, help="Decision maker")
    @click.option("--rework-note", default=None, help="Rework note when decision=needs_rework")
    @click.option("--engine-root", default=None, help="Engine repo root for suite lookup (default: cwd)")
    def promote(retro_id, proposal_id, decision, decided_by, rework_note, engine_root) -> None:
        """Record a human review decision for a retro proposal.

        A ``promoted`` decision is the authorization point: after recording
        the event the command immediately resumes the nightly eval/apply gate
        with the real runner (spec §6).
        """
        project_root = Path.cwd()
        engine = Path(engine_root).resolve() if engine_root else project_root
        retro_dir = project_root / "qa" / "retro" / retro_id
        proposal = next((p for p in read_proposals(retro_dir) if p.id == proposal_id), None)
        if proposal is None:
            click.echo(f"Error: proposal not found: {proposal_id}", err=True)
            raise SystemExit(1)

        events = read_promotion_events(retro_dir)
        state = proposal_states(events).get(proposal_id, "proposed")
        previous = effective_review_decisions(events).get(proposal_id)
        if state in TERMINAL_STATES or (previous and previous.get("decision") == decision):
            # Terminal proposals (applied/rejected) and repeated identical
            # decisions return the original result: no duplicate event, no
            # re-eval, no re-append (spec §6.4). Retry after a non-terminal
            # decision goes through `aa retro nightly resume`.
            original = (previous or {}).get("decision", decision)
            click.echo(
                json.dumps(
                    {
                        "retro_id": retro_id,
                        "proposal_id": proposal_id,
                        "decision": original,
                        "state": state,
                        "idempotent": True,
                    }
                )
            )
            return

        if decision == "promoted":
            if proposal.apply_kind != "memory_append":
                click.echo(
                    f"Error: proposal {proposal_id} apply_kind={proposal.apply_kind!r} "
                    "cannot be promoted (only memory_append is supported)",
                    err=True,
                )
                raise SystemExit(1)
            suite = (proposal.eval_suite or "").strip()
            if not suite:
                click.echo(f"Error: proposal {proposal_id} has no eval_suite", err=True)
                raise SystemExit(1)
            from assurance_agent.eval.plan import load_suite

            try:
                load_suite(engine, suite)
            except AaError as err:
                click.echo(f"Error: {err}", err=True)
                raise SystemExit(1) from err
            try:
                resolve_memory_target(project_root, proposal.target)
            except AaError as err:
                click.echo(f"Error: {err}", err=True)
                raise SystemExit(1) from err

        append_promotion_events(
            retro_dir,
            [
                review_decision_event(
                    proposal_id, decision=decision, actor=decided_by, rework_note=rework_note
                )
            ],
        )
        new_state = {
            "promoted": "promoted_pending_eval",
            "rejected": "rejected",
            "needs_rework": "needs_rework",
        }[decision]
        click.echo(
            json.dumps(
                {
                    "retro_id": retro_id,
                    "proposal_id": proposal_id,
                    "decision": decision,
                    "state": new_state,
                    "idempotent": False,
                }
            )
        )

        if decision == "promoted":
            runner = _build_eval_runner(engine, project_root)
            code = resume_nightly(
                NightlyOptions(
                    sut=str(project_root),
                    retro_id=retro_id,
                    engine_root=str(engine),
                ),
                eval_runner=runner,
            )
            raise SystemExit(code)

    @retro.command("complete")
    @click.option("--retro", "retro_id", required=True, help="Retro id")
    def complete(retro_id) -> None:
        """Mark a collect run finished; consumed changes become terminal."""
        complete_retro_stage(Path.cwd(), retro_id)
        click.echo(f"retro run completed: {retro_id}")

    @retro.command("apply")
    @click.option("--retro", "retro_id", required=True, help="Retro id")
    @click.option("--proposal", "proposal_id", default=None, help="Proposal id to apply")
    @click.option(
        "--stage-dir",
        "stage_dir",
        default=None,
        type=click.Path(),
        help="Render into a stage dir instead of writing live memory",
    )
    def apply_cmd(retro_id, proposal_id, stage_dir) -> None:
        """Apply promoted memory_append proposals to their .aa/memory targets.

        Without --stage-dir writes the live ``.aa/memory/<target>`` with a
        ``<!-- retro:<id>#<proposal> evidence:... -->`` marker (idempotent:
        each proposal appears at most once). With --stage-dir renders
        "live content + new block" into the stage dir instead.
        """
        project_root = Path.cwd()
        retro_dir = project_root / "qa" / "retro" / retro_id
        context_path = retro_dir / "context.json"
        if not context_path.exists():
            click.echo(f"Error: required file not found: {context_path}", err=True)
            raise SystemExit(1)
        context = RetroContext.model_validate(json.loads(context_path.read_text(encoding="utf-8")))
        proposals = read_proposals(retro_dir)
        if proposal_id and not any(p.id == proposal_id for p in proposals):
            click.echo(f"Error: proposal not found: {proposal_id}", err=True)
            raise SystemExit(1)
        requested = next((p for p in proposals if p.id == proposal_id), None)
        if requested is not None and requested.apply_kind == "memory_append":
            try:
                resolve_memory_target(project_root, requested.target)
            except AaError as err:
                click.echo(f"Error: {err}", err=True)
                raise SystemExit(1) from err

        promotion_events = read_promotion_events(retro_dir)
        decisions = effective_review_decisions(promotion_events)
        states = proposal_states(promotion_events)
        selected = [
            p
            for p in proposals
            if (proposal_id is None or p.id == proposal_id)
            and p.apply_kind == "memory_append"
            and decisions.get(p.id, {}).get("decision") == "promoted"
            and (stage_dir is not None or states.get(p.id) == "applied")
        ]
        if stage_dir is None and proposal_id is not None and not selected:
            if requested is not None and decisions.get(proposal_id, {}).get("decision") == "promoted":
                click.echo(
                    "Error: live memory apply requires a passing eval application state",
                    err=True,
                )
                raise SystemExit(1)
        # Validate only the proposals being applied; a malformed unrelated
        # proposal must not block the valid ones.
        errors = validate_retro_proposals(context, selected)
        if errors:
            for error in errors:
                click.echo(f"Error: {error}", err=True)
            raise SystemExit(1)

        stage = Path(stage_dir).resolve() if stage_dir else None
        for proposal in selected:
            try:
                apply_memory_proposal(project_root, retro_id, proposal, stage_dir=stage)
            except AaError as err:
                click.echo(f"Error: {err}", err=True)
                raise SystemExit(1) from err
        click.echo(
            json.dumps(
                {
                    "retro_id": retro_id,
                    "applied": [p.id for p in selected],
                    "eval_suites": sorted({p.eval_suite for p in selected if p.eval_suite}),
                    "stage_dir": str(stage) if stage else None,
                }
            )
        )

    @retro.command("rollback")
    @click.option("--retro", "retro_id", required=True, help="Retro id")
    @click.option("--proposal", "proposal_id", required=True, help="Proposal id to roll back")
    @click.option("--by", "actor", required=True, help="Who or what triggered the rollback")
    @click.option("--note", default=None, help="Rework note recorded in promotions.json")
    def rollback(retro_id, proposal_id, actor, note) -> None:
        """Mark an applied memory block deprecated (no physical delete).

        Idempotent: an already-deprecated block records no new event. A
        successful rollback appends an ``application: rolled_back`` event,
        which the state machine treats as ``needs_rework`` (spec §6.4).
        """
        project_root = Path.cwd()
        retro_dir = project_root / "qa" / "retro" / retro_id
        proposal = next((p for p in read_proposals(retro_dir) if p.id == proposal_id), None)
        if proposal is None:
            click.echo(f"Error: proposal not found: {proposal_id}", err=True)
            raise SystemExit(1)
        try:
            changed = deprecate_memory_block(project_root, retro_id, proposal)
        except AaError as err:
            click.echo(f"Error: {err}", err=True)
            raise SystemExit(1) from err
        if changed:
            append_promotion_events(
                retro_dir,
                [
                    application_event(
                        proposal_id,
                        result="rolled_back",
                        actor=actor,
                        target=proposal.target,
                        note=note,
                    )
                ],
            )
        click.echo(
            json.dumps(
                {
                    "retro_id": retro_id,
                    "proposal_id": proposal_id,
                    "rolled_back": changed,
                }
            )
        )


def _register_nightly(retro: click.Group) -> None:
    @retro.group("nightly")
    def nightly() -> None:
        """Run the nightly retro pipeline."""

    @nightly.command("collect")
    @click.option("--sut", required=True, help="SUT project root")
    @click.option("--retro-id", "retro_id", help="Stable retro run id")
    @click.option("--dry-run", is_flag=True, help="Stop before invoking the proposal agent")
    @click.option("--agent", default="cursor-agent", help="Proposal agent command")
    @click.option("--history", type=int, default=5)
    @click.option("--min-evidence", type=int, default=2)
    @click.option("--rework-alert", type=int, default=3)
    def collect(sut, retro_id, dry_run, agent, history, min_evidence, rework_alert) -> None:
        options = NightlyOptions(
            sut=sut,
            retro_id=retro_id,
            dry_run=dry_run,
            agent=agent,
            history=history,
            min_evidence=min_evidence,
            rework_alert=rework_alert,
        )
        code = collect_nightly(options, agent_runner=run_agent)
        raise SystemExit(code)

    @nightly.command("resume")
    @click.option("--sut", required=True)
    @click.option("--retro-id", "retro_id")
    @click.option("--skip-eval", is_flag=True)
    @click.option("--engine-root", default=None, help="Engine repo root (default: cwd)")
    def resume(sut, retro_id, skip_eval, engine_root) -> None:
        if not retro_id:
            click.echo("error: required option --retro-id <id> not specified", err=True)
            raise SystemExit(2)
        root = Path(engine_root).resolve() if engine_root else Path.cwd()
        options = NightlyOptions(
            sut=sut,
            retro_id=retro_id,
            skip_eval=skip_eval,
            engine_root=str(root),
        )
        runner = None if skip_eval else _build_eval_runner(root, Path(sut))
        raise SystemExit(resume_nightly(options, eval_runner=runner))

    @nightly.command("apply")
    @click.option("--sut", required=True)
    @click.option("--retro", "retro_id", required=True)
    @click.option("--proposal", "proposal_id", required=True)
    @click.option("--stage-dir", required=True, type=click.Path())
    def apply_cmd(sut, retro_id, proposal_id, stage_dir) -> None:
        from assurance_agent.retro.apply import apply_proposal_to_stage

        try:
            proposal = apply_proposal_to_stage(
                sut_root=Path(sut),
                retro_id=retro_id,
                proposal_id=proposal_id,
                stage_dir=Path(stage_dir),
            )
        except Exception as err:
            click.echo(f"Error: {err}", err=True)
            raise SystemExit(1) from err
        click.echo(f"staged: {proposal.id} -> {stage_dir}")

    @nightly.command("report")
    @click.option("--sut", required=True)
    @click.option("--last", type=int, default=10)
    @click.option("--rework-alert", type=int, default=3)
    def report(sut, last, rework_alert) -> None:
        options = NightlyOptions(sut=sut, last=last, rework_alert=rework_alert)
        raise SystemExit(report_nightly(options))
