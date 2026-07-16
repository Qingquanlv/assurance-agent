from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.retro.aggregator import build_retro_context, count_signals
from assurance_agent.retro.nightly.agent import run_agent
from assurance_agent.retro.nightly.driver import (
    collect_nightly,
    report_nightly,
    resume_nightly,
)
from assurance_agent.retro.nightly.types import NightlyOptions
from assurance_agent.retro.nightly.utils import write_json
from assurance_agent.retro.state import mark_consumed_change


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
    main_group.add_command(retro)


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
        options = NightlyOptions(sut=sut, retro_id=retro_id, skip_eval=skip_eval)

        def eval_runner(
            *,
            suite: str,
            sut_dir: Path,
            engine_root: Path,
            extra_memory_dir: Path | None = None,
        ) -> dict:
            import os

            from assurance_agent.eval.plan import load_suite
            from assurance_agent.eval.runner import run_suite
            from assurance_agent.eval.metrics import read_metrics
            from assurance_agent.eval.paths import run_dir as run_dir_for

            os.environ.setdefault("AA_EVAL_FAKE_ADAPTER", "1")
            _, suite_file = load_suite(engine_root, suite)

            def fake_factory(**_: object):
                from assurance_agent.commands.eval_cmd import _FakeAdapter

                return _FakeAdapter()

            from assurance_agent.commands.eval_cmd import _terminal_status_provider_factory

            run_id, gate = run_suite(
                suite_file=suite_file,
                project_root=engine_root,
                sut_dir=sut_dir,
                adapter_factory=fake_factory,
                status_provider_factory=_terminal_status_provider_factory,
                fixtures_root=sut_dir / "eval-fixtures",
            )
            metrics = {}
            try:
                metrics = read_metrics(run_dir_for(sut_dir, run_id)).metrics
            except Exception:
                pass
            _ = extra_memory_dir  # reserved for memory overlay into sandbox
            return {"run_id": run_id, "verdict": gate.verdict, "metrics": metrics}

        root = Path(engine_root).resolve() if engine_root else Path.cwd()

        def bound_runner(**kwargs):
            kwargs.setdefault("engine_root", root)
            return eval_runner(**kwargs)

        raise SystemExit(
            resume_nightly(options, eval_runner=None if skip_eval else bound_runner)
        )

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
