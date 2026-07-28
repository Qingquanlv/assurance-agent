from __future__ import annotations

import json
import os
from pathlib import Path

import click

from assurance_agent.eval.baseline import (
    compare_with_baseline,
    read_baseline,
    read_run_manifest,
    update_baseline,
)
from assurance_agent.eval.gate import read_gate_result
from assurance_agent.eval.paths import run_dir as run_dir_for
from assurance_agent.eval.plan import generate_plan, load_suite, write_plan
from assurance_agent.eval.report import generate_trend_report
from assurance_agent.eval.runner import run_plan, run_suite
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.agent_api import AgentInvoker, AgentRequest, AgentResult

_FAILING = {"fail", "inconclusive", "needs_human_review"}


class _FakeAdapter:
    """AA_EVAL_FAKE_ADAPTER: skip real agent; seed/fixture supplies artifacts."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        return AgentResult(ok=True)


def _resolve_adapter_factory(*, use_fake: bool, sut: Path):
    if use_fake:

        def fake_factory(**_: object) -> AgentInvoker:
            return _FakeAdapter()

        return fake_factory

    from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter

    agent_cmd = os.environ.get("AA_EVAL_AGENT_CMD", "cursor-agent")

    def real_factory(*, sut_dir: Path | None = None, **_: object) -> AgentInvoker:
        return HeadlessAdapter(agent_cmd=agent_cmd, cwd=sut_dir or sut)

    return real_factory


def _resolve_sut(project_root: Path, sut_dir: str | None) -> Path:
    if sut_dir:
        return Path(sut_dir).resolve()
    env = os.environ.get("AA_EVAL_SUT_DIR")
    if env:
        return Path(env).resolve()
    from assurance_agent.eval.suts import load_sut_registry, resolve_sut_dir

    if not load_sut_registry(project_root).suts:
        return project_root
    return resolve_sut_dir(project_root)


@click.group("eval")
def eval_group() -> None:
    """AI Eval Harness — evaluate AI tool quality."""


@eval_group.command("run")
@click.option("--suite", "suite_name", help="Suite name to run")
@click.option("--plan", "plan_path", help="Path to eval-plan.json")
@click.option("--sample", "sample_id", help="Run a single sample only")
@click.option("--repeat", type=int, default=1, help="Repeat runs (stability)")
@click.option("--output", "output_mode", help="Output mode: id")
@click.option("--json", "as_json", is_flag=True, help="Output { run_id, verdict } JSON")
@click.option("--fail-on-verdict", is_flag=True, help="Exit 1 when verdict is not pass")
@click.option("--calibrate", is_flag=True, help="Run judge calibration (records only)")
@click.option("--extra-memory-dir", help="Overlay .aa/memory files into SUT workspaces")
@click.option("--change", "change_ids", multiple=True, help="Source Change ID (repeatable)")
@click.option("--sut-dir", help="Override SUT checkout directory")
def eval_run(
    suite_name,
    plan_path,
    sample_id,
    repeat,
    output_mode,
    as_json,
    fail_on_verdict,
    calibrate,
    extra_memory_dir,
    change_ids,
    sut_dir,
) -> None:
    project_root = Path.cwd()
    if not suite_name and not plan_path:
        click.echo("Error: --suite <name> or --plan <path> required", err=True)
        raise SystemExit(1)
    if suite_name and plan_path:
        click.echo("Error: --suite and --plan are mutually exclusive", err=True)
        raise SystemExit(1)

    use_fake = bool(os.environ.get("AA_EVAL_FAKE_ADAPTER"))
    sut = _resolve_sut(project_root, sut_dir)
    adapter_factory = _resolve_adapter_factory(use_fake=use_fake, sut=sut)
    overlay = Path(extra_memory_dir).resolve() if extra_memory_dir else None

    try:
        if suite_name:
            _, suite_file = load_suite(project_root, suite_name)
            run_id, gate = run_suite(
                suite_file=suite_file,
                project_root=project_root,
                sut_dir=sut,
                sample_id=sample_id,
                repeat=repeat,
                calibrate=calibrate,
                adapter_factory=adapter_factory,
                extra_memory_dir=overlay,
                change_ids=tuple(change_ids),
            )
            _print_run(output_mode, as_json, run_id, gate.verdict)
            if fail_on_verdict and gate.verdict in _FAILING:
                raise SystemExit(1)
        else:
            batch_id, gates = run_plan(
                plan_path=Path(plan_path),
                project_root=project_root,
                sut_dir=sut,
                adapter_factory=adapter_factory,
                extra_memory_dir=overlay,
                change_ids=tuple(change_ids),
            )
            worst = _worst_verdict([g.verdict for g in gates])
            _print_run(output_mode, as_json, batch_id, worst, key="batch_id")
            if fail_on_verdict and worst in _FAILING:
                raise SystemExit(1)
    except AaError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(1) from err


def _worst_verdict(verdicts: list[str]) -> str:
    order = ["fail", "inconclusive", "needs_human_review", "pass_with_warnings", "pass"]
    for candidate in order:
        if candidate in verdicts:
            return candidate
    return "pass"


def _print_run(output_mode, as_json, run_id, verdict, key="run_id") -> None:
    if output_mode == "id":
        click.echo(run_id)
        return
    if as_json:
        click.echo(json.dumps({key: run_id, "verdict": verdict}))
        return
    click.echo(f"{key}: {run_id}")
    click.echo(f"verdict: {verdict}")


@eval_group.command("plan")
@click.option("--event", required=True, help="pull_request | manual")
@click.option("--changed-files", help="Path to changed files list")
@click.option("--suite", "suite_name", help="Suite to include (manual)")
@click.option("--out", default="eval-plan.json", help="Output path")
def eval_plan(event, changed_files, suite_name, out) -> None:
    changed = None
    if changed_files:
        changed = Path(changed_files).read_text(encoding="utf-8").split()
    plan = generate_plan(event, changed, suite_name)
    write_plan(plan, Path(out))
    click.echo(f"plan: {out}")


@eval_group.command("report")
@click.option("--run", "run_id", help="Run id")
@click.option("--trend", is_flag=True, help="Trend report")
@click.option("--suite", "suite_name", help="Suite for --trend")
@click.option("--from", "date_from", help="trend filter: started_at >= from")
@click.option("--to", "date_to", help="trend filter: started_at <= to")
@click.option("--html", "as_html", is_flag=True, help="Generate HTML")
@click.option("--output", "output_path", help="Override HTML output path")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.option("--sut-dir", help="SUT root that holds eval/out")
def eval_report(
    run_id, trend, suite_name, date_from, date_to, as_html, output_path, as_json, sut_dir
) -> None:
    project_root = Path.cwd()
    sut = _resolve_sut(project_root, sut_dir)
    if trend:
        if not suite_name:
            click.echo("Error: --trend requires --suite", err=True)
            raise SystemExit(1)
        out = generate_trend_report(
            sut,
            suite_name,
            date_from=date_from,
            date_to=date_to,
            html_out=Path(output_path) if output_path else None,
        )
        click.echo(f"trend: {out}")
        return
    if not run_id:
        click.echo("Error: --run <id> or --trend required", err=True)
        raise SystemExit(1)
    run_dir = run_dir_for(sut, run_id)
    report_path = run_dir / "report.json"
    if not report_path.exists():
        click.echo(f"Error: run not found: {run_id}", err=True)
        raise SystemExit(1)
    if as_json:
        click.echo(report_path.read_text(encoding="utf-8"))
        return
    gate = read_gate_result(run_dir)
    click.echo(f"run_id: {run_id}")
    click.echo(f"verdict: {gate.verdict}")
    if as_html:
        click.echo(f"html: {run_dir / 'report.html'}")


_VERDICT_EXIT = {"pass": 0, "pass_with_warnings": 0, "fail": 1, "inconclusive": 1, "needs_human_review": 30}


@eval_group.command("gate")
@click.option("--run", "run_id", required=True, help="Run id")
@click.option("--sut-dir", help="SUT root that holds eval/out")
def eval_gate(run_id: str, sut_dir: str | None) -> None:
    """Read gate result (does NOT recompute)."""
    project_root = Path.cwd()
    sut = _resolve_sut(project_root, sut_dir)
    run_dir = run_dir_for(sut, run_id)
    try:
        gate = read_gate_result(run_dir)
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval gate failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"suite:   {gate.suite}")
    click.echo(f"verdict: {gate.verdict}")
    if gate.hard_gate_failures:
        click.echo(f"hard_gate_failures: {', '.join(gate.hard_gate_failures)}")
    raise SystemExit(_VERDICT_EXIT.get(gate.verdict, 1))


@eval_group.command("compare")
@click.option(
    "--baseline", "baseline_name", required=True, help='Baseline name (currently only "main" supported)'
)
@click.option("--run", "run_id", required=True, help="Run id")
@click.option("--sut-dir", help="SUT root that holds eval/out")
def eval_compare(baseline_name: str, run_id: str, sut_dir: str | None) -> None:
    """Compare a run against the named baseline (read-only)."""
    project_root = Path.cwd()
    sut = _resolve_sut(project_root, sut_dir)
    try:
        baseline = read_baseline(project_root, baseline_name)
        run_dir = run_dir_for(sut, run_id)
        manifest = read_run_manifest(run_dir)
        entry = baseline.get(manifest.suite)
        if entry is None:
            click.echo(f"No baseline found for suite: {manifest.suite}")
            raise SystemExit(0)
        delta = compare_with_baseline(run_dir, entry.metrics)
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval compare failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"Comparing {run_id} vs baseline ({entry.run_id})")
    for metric, value in delta.items():
        sign = f"+{value:.4f}" if value >= 0 else f"{value:.4f}"
        click.echo(f"  {metric}: {sign}")


@eval_group.group("baseline")
def eval_baseline() -> None:
    """Manage eval baselines."""


@eval_baseline.command("update")
@click.option("--suite", "suite_name", required=True, help="Suite name")
@click.option("--run", "run_id", required=True, help="Run id to use as new baseline")
@click.option("--approved-by", default="unknown", help="Approver initials")
@click.option("--yes", is_flag=True, help="Skip interactive confirmation")
@click.option("--sut-dir", help="SUT root that holds eval/out")
def eval_baseline_update(
    suite_name: str, run_id: str, approved_by: str, yes: bool, sut_dir: str | None
) -> None:
    """Update baseline for a suite (requires human confirmation)."""
    project_root = Path.cwd()
    sut = _resolve_sut(project_root, sut_dir)
    if not yes and not click.confirm(f"Promote run {run_id} to baseline 'main' for suite {suite_name}?"):
        click.echo("aborted", err=True)
        raise SystemExit(1)
    try:
        update_baseline(
            project_root,
            suite_name=suite_name,
            run_id=run_id,
            approved_by=approved_by,
            sut_root=sut,
        )
    except (FileNotFoundError, AaError) as err:
        click.echo(f"eval baseline update failed: {err}", err=True)
        raise SystemExit(1) from err
    click.echo(f"baseline updated: main.json [{suite_name}] <- {run_id}")
