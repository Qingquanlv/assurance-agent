"""`aa heal` healing-support commands: validate fix proposals and safety checks.

All subcommands are deterministic read/validate over M2 healing artifacts; they
never mutate product/test code. Fixer execution itself lives outside the CLI.
"""

import json
from pathlib import Path

import click
from pydantic import ValidationError

from assurance_agent.artifacts.models import FailureAnalysis, FixProposal, SafetyCheck
from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_COMPLETED, EXIT_ERROR, EXIT_HUMAN_REVIEW
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.healing.safety import HealingGuardError, record_apply_summary


@click.group("heal")
def heal_group() -> None:
    """Healing-support commands (fix-proposal validation, eligibility, safety checks)."""


def _change_base(change_id: str) -> Path:
    try:
        return resolve_change(Path.cwd(), change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@heal_group.command("validate-proposal")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--file", "file_path", default=None, help="Override path to fix-proposal.json.")
def validate_proposal(change_id: str, file_path: str | None) -> None:
    """Validate healing/fix-proposal.json against the FixProposal contract."""
    path = Path(file_path) if file_path else _change_base(change_id) / "healing" / "fix-proposal.json"
    raw = _load_json(path)
    if raw is None:
        click.secho(f"fix-proposal.json not found or unreadable: {path}", fg="red")
        raise SystemExit(EXIT_ERROR)
    try:
        proposal = FixProposal.model_validate(raw)
    except ValidationError as err:
        click.secho(f"fix-proposal.json is invalid:\n{err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(f"\naa heal validate-proposal — change: {change_id}\n", bold=True)
    click.echo(f"  eligible_count : {proposal.summary.eligible_count}")
    for index, item in enumerate(proposal.proposals, start=1):
        flag = (
            click.style("eligible", fg="green") if item.eligible else click.style("not-eligible", fg="yellow")
        )
        click.echo(f"  [{index}] target={item.target}  {flag}")
    raise SystemExit(EXIT_COMPLETED)


@heal_group.command("eligibility-summary")
@click.option("--change", "change_id", required=True, help="Change ID.")
def eligibility_summary(change_id: str) -> None:
    """Summarise fix eligibility from inspect/failure-analysis.json."""
    path = _change_base(change_id) / "inspect" / "failure-analysis.json"
    raw = _load_json(path)
    if raw is None:
        click.secho(f"failure-analysis.json not found: {path}. Run `aa report inspect` first.", fg="red")
        raise SystemExit(EXIT_ERROR)
    analysis = FailureAnalysis.model_validate(raw)
    fixable = [f for f in analysis.failures if f.fix_proposal_eligible]
    review = [f for f in analysis.failures if f.needs_review]

    click.secho(f"\naa heal eligibility-summary — change: {change_id}\n", bold=True)
    click.echo(f"  Total failures : {len(analysis.failures)}")
    click.echo(f"  Fixable        : {len(fixable)}")
    click.echo(f"  Needs review   : {len(review)}")
    click.echo(f"  Hard fails     : {len(analysis.hard_fails)}")
    for failure in fixable:
        click.echo(f"    fixable: {failure.case_id} ({failure.category})")
    raise SystemExit(EXIT_COMPLETED)


@heal_group.command("safety-check")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--file", "file_path", default=None, help="Override path to fixer-safety-check.json.")
def safety_check(change_id: str, file_path: str | None) -> None:
    """Validate healing/fixer-safety-check.json and map its verdict to an exit code.

    Path is `healing/fixer-safety-check.json` — the SAME artifact M2 registers and
    the `fixer-safety-gate` expression consumes. Reading a different filename would
    let the gate pass on an unvalidated (or absent) safety report.
    """
    path = Path(file_path) if file_path else _change_base(change_id) / "healing" / "fixer-safety-check.json"
    raw = _load_json(path)
    if raw is None:
        click.secho(f"fixer-safety-check.json not found or unreadable: {path}", fg="red")
        raise SystemExit(EXIT_ERROR)
    try:
        check = SafetyCheck.model_validate(raw)
    except ValidationError as err:
        click.secho(f"fixer-safety-check.json is invalid:\n{err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(f"\naa heal safety-check — change: {change_id}\n", bold=True)
    click.echo(f"  passed              : {check.passed}")
    click.echo(f"  needs_review        : {check.needs_review}")
    click.echo(f"  product_code_modified: {check.product_code_modified}")
    if not check.passed:
        click.secho("→ Safety check failed. Do not apply the fix.", fg="red")
        raise SystemExit(EXIT_ERROR)
    if check.needs_review:
        click.secho("→ Safety check needs human review before applying.", fg="yellow")
        raise SystemExit(EXIT_HUMAN_REVIEW)
    click.secho("→ Safety check passed.", fg="green")
    raise SystemExit(EXIT_COMPLETED)


@heal_group.command("record-apply")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--target", required=True, type=click.Choice(["api", "e2e"]), help="Heal target.")
@click.option(
    "--proposal",
    "proposal_ids",
    multiple=True,
    required=False,
    help="Proposal ID(s). Required when --outcome=applied.",
)
@click.option(
    "--outcome",
    type=click.Choice(["applied", "no_op", "skipped"]),
    default="applied",
    show_default=True,
    help="applied=diff-based apply; no_op/skipped=contract-closing summary with applied=false.",
)
@click.option(
    "--reason",
    default=None,
    help="Required for --outcome no_op|skipped (e.g. Condition 3 failure).",
)
def record_apply(
    change_id: str,
    target: str,
    proposal_ids: tuple[str, ...],
    outcome: str,
    reason: str | None,
) -> None:
    """Record apply summary and frozen heal_record_apply audit event.

    Always writes ``healing/{target}-apply-summary.json`` so graph fixer nodes
    that declare that file as a hard output can close on STOP/no-op paths.
    """
    if any(pid.lower() in {"all", "*"} for pid in proposal_ids):
        click.secho("Placeholder proposal ids (e.g. 'all') are not allowed.", fg="red")
        raise SystemExit(1)
    try:
        result = record_apply_summary(
            Path.cwd(),
            change_id,
            target,
            list(proposal_ids),
            outcome=outcome,  # type: ignore[arg-type]
            reason=reason,
        )
    except ProgressionError as err:
        click.secho(f"record-apply failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err
    except (HealingGuardError, UnsafeIdentifierError, AaError) as err:
        click.secho(f"record-apply failed: {err}", fg="red")
        raise SystemExit(1) from err

    click.secho(f"\naa heal record-apply — change: {change_id}\n", bold=True)
    click.echo(f"  target          : {target}")
    click.echo(f"  outcome         : {outcome}")
    click.echo(f"  files_modified  : {len(result.files_modified)}")
    click.echo(f"  apply-summary   : {result.json_path}")
    raise SystemExit(EXIT_COMPLETED)
