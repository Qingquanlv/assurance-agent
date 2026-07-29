"""aa verify — reconciled-phase evidence verdict (read-only fold + sufficiency)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import click

from assurance_agent.artifacts.policy import PolicyError, load_policy
from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigInvalidError, ConfigNotFoundError
from assurance_agent.evidence.sufficiency import evaluate_sufficiency
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.evidence.verify import (
    VERIFY_BLOCKING_GAP_CODES,
    VerifyResult,
    VerifyVerdict,
    evaluate_verify_verdict,
)
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR, EXIT_HUMAN_REVIEW

__all__ = [
    "VERIFY_BLOCKING_GAP_CODES",
    "VerifyResult",
    "evaluate_verify_verdict",
    "exit_code_for_verify_verdict",
    "run_verify",
    "verify_command",
]


def exit_code_for_verify_verdict(verdict: VerifyVerdict) -> int:
    if verdict == "pass":
        return 0
    if verdict == "needs_human":
        return EXIT_HUMAN_REVIEW
    return EXIT_ERROR


def _print_human(result: VerifyResult) -> None:
    click.secho(f"aa verify — change: {result.change_id}", bold=True)
    click.echo()
    click.echo(f"  verdict           : {result.verdict}")
    click.echo(f"  phase             : {result.phase}")
    click.echo(f"  as_of             : {result.as_of.isoformat()}")
    click.echo(f"  policy_digest     : {result.policy_digest}")
    click.echo(f"  projection_digest : {result.projection_digest}")
    if result.scope is not None:
        click.echo(f"  batch             : {result.scope.batch}")
        click.echo(f"  cases             : {len(result.scope.cases)}")
    if result.blocking_gaps:
        click.echo()
        click.echo("  blocking_gaps:")
        for gap in result.blocking_gaps:
            click.echo(f"    {gap.code:<28} {gap.source}")
    if result.open_problem_ids:
        click.echo()
        click.echo("  open_problem_ids:")
        for problem_id in result.open_problem_ids:
            click.echo(f"    {problem_id}")
    if result.insufficient:
        click.echo()
        click.echo("  insufficient:")
        for item in result.insufficient:
            click.echo(
                f"    {item.case_id:<24} missing={','.join(item.missing_kinds)} "
                f"reasons={','.join(item.reason_codes)}"
            )
    if result.warnings:
        click.echo()
        click.echo("  warnings:")
        for warning in result.warnings:
            click.echo(f"    {warning}")
    click.echo()


def run_verify(
    change_id: str,
    *,
    as_json: bool,
    as_of: datetime | None = None,
) -> int:
    project_root = Path.cwd()
    try:
        resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError, ConfigInvalidError) as err:
        click.secho(str(err), fg="red", err=True)
        return EXIT_ERROR

    try:
        policy = load_policy(project_root)
    except PolicyError as err:
        click.secho(f"verify failed: {err}", fg="red", err=True)
        return EXIT_ERROR

    aware_as_of = as_of if as_of is not None else datetime.now(UTC)
    projection = fold_trace(project_root, change_id, phase="reconciled")
    try:
        report = evaluate_sufficiency(projection, policy, as_of=aware_as_of)
    except TypeError as err:
        click.secho(f"verify failed: {err}", fg="red", err=True)
        return EXIT_ERROR

    result = evaluate_verify_verdict(projection, policy, report)
    if as_json:
        click.echo(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        _print_human(result)
    return exit_code_for_verify_verdict(result.verdict)


@click.command("verify")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def verify_command(change_id: str, as_json: bool) -> None:
    """Fold reconciled trace projection and adjudicate evidence sufficiency."""
    raise SystemExit(run_verify(change_id, as_json=as_json))
