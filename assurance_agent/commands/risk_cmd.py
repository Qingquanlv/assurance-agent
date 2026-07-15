"""aa risk — Explore 命令（Phase 0.5）。对齐 TS src/commands/risk.ts 的 flag 面。"""
import json
from pathlib import Path

import click
import yaml

from assurance_agent.risk.advisory import validate_advisory
from assurance_agent.risk.context import (
    RiskContext,
    build_risk_context,
    serialize_context,
    validate_context_shape,
    write_risk_context,
)
from assurance_agent.risk.paths import advisory_json_path, context_json_path
from assurance_agent.risk.safety import RiskSafetyError, assert_change_id_safe, assert_inside_project


@click.group("risk")
def risk_group() -> None:
    """Explore commands (Phase 0.5)."""


@risk_group.command("context")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
@click.option("--diff-base", "diff_base", default="main", help="Git diff base ref.")
@click.option("--archive-depth", "archive_depth", default=10, type=int, help="Recent archives to sample.")
@click.option("--staleness-days", "staleness_days", default=30, type=int, help="Archive staleness threshold (days).")
@click.option("--requirement", "requirement", default=None, help="Requirement text file inside project root.")
@click.option("--output-dir", "output_dir", default=None, help="Write context.json into this dir instead.")
@click.option("--stdout", "to_stdout", is_flag=True, help="Print JSON to stdout and do NOT write to disk.")
def risk_context(
    change_id: str,
    project_dir: str | None,
    diff_base: str,
    archive_depth: int,
    staleness_days: int,
    requirement: str | None,
    output_dir: str | None,
    to_stdout: bool,
) -> None:
    """Aggregate git diff, cases, and archive history into explore/context.json."""
    try:
        assert_change_id_safe(change_id)
        project_root = Path(project_dir).resolve() if project_dir else Path.cwd()
        if archive_depth <= 0 or staleness_days <= 0:
            raise RiskSafetyError("--archive-depth and --staleness-days must be positive integers")
        if not project_root.is_dir():
            raise RiskSafetyError(f"--project-dir is not a directory: {project_root}")

        ctx = build_risk_context(
            change_id=change_id,
            project_root=project_root,
            diff_base=diff_base,
            archive_depth=archive_depth,
            staleness_days=staleness_days,
            requirement_path=requirement,
        )
        ok, errors = validate_context_shape(ctx)
        if not ok:
            click.secho("Context validation failed: " + "; ".join(errors), fg="red")
            raise SystemExit(1)

        if to_stdout:
            click.echo(serialize_context(ctx), nl=False)
            return

        if output_dir is not None:
            out_dir = (project_root / output_dir).resolve()
            assert_inside_project(project_root, out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "context.json"
            out_path.write_text(serialize_context(ctx), encoding="utf-8")
        else:
            out_path = write_risk_context(project_root, change_id, ctx)
        click.secho(f"Wrote {out_path}", fg="green")
        click.echo(f"Evidence entries: {len(ctx.evidence)}")
        click.echo(f"Degraded: {ctx.degraded}")
    except RiskSafetyError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err


@risk_group.command("validate-advisory")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
def risk_validate_advisory(change_id: str, project_dir: str | None) -> None:
    """Validate explore/advisory.json against explore/context.json (§5.5)."""
    try:
        assert_change_id_safe(change_id)
        project_root = Path(project_dir).resolve() if project_dir else Path.cwd()
        ctx_path = context_json_path(project_root, change_id)
        adv_path = advisory_json_path(project_root, change_id)
        if not ctx_path.is_file():
            click.secho(f"Missing {ctx_path}", fg="red")
            raise SystemExit(1)
        if not adv_path.is_file():
            click.secho(f"Missing {adv_path}", fg="red")
            raise SystemExit(1)

        context = RiskContext.model_validate(json.loads(ctx_path.read_text(encoding="utf-8")))
        advisory = json.loads(adv_path.read_text(encoding="utf-8"))
        run_ctx = _read_run_context(project_root, change_id)
        ok, errors = validate_advisory(
            context,
            advisory,
            known_case_ids=[],
            interaction_mode=run_ctx.get("interaction_mode"),
            orchestrator_skill=run_ctx.get("orchestrator_skill"),
        )
        if ok:
            click.secho("advisory.json validation passed", fg="green")
            raise SystemExit(0)
        click.secho("advisory.json validation failed:", fg="red")
        for e in errors:
            click.secho(f"  - {e}", fg="red")
        raise SystemExit(1)
    except RiskSafetyError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err


def _read_run_context(project_root: Path, change_id: str) -> dict:
    state_file = project_root / "qa" / "changes" / change_id / "workflow-state.yaml"
    if not state_file.is_file():
        return {}
    parsed = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return {}
    run_context = parsed.get("run_context")
    if not isinstance(run_context, dict):
        return {}
    mode = run_context.get("interaction_mode")
    return {
        "interaction_mode": mode if mode in ("interactive", "autonomous") else None,
        "orchestrator_skill": run_context.get("orchestrator_skill")
        if isinstance(run_context.get("orchestrator_skill"), str)
        else None,
    }
