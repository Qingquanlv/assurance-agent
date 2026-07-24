from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.knowledge.promote import KnowledgePromoteError, promote_knowledge
from assurance_agent.knowledge.validate import validate_knowledge


@click.group("knowledge")
def knowledge_group() -> None:
    """Domain knowledge base commands (L1/L2 data-knowledge)."""


@knowledge_group.command("validate")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
@click.option("--change", "change_id", default=None, help="Also validate change L2 proposals.")
@click.option("--proposal", "proposal_path", default=None, help="Validate a specific L2 proposal file.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output {ok, results}.")
def validate_command(
    project_dir: str | None,
    change_id: str | None,
    proposal_path: str | None,
    as_json: bool,
) -> None:
    """Validate .aa/data-knowledge.yaml and optional L2 proposals against canonical schema."""
    root = Path(project_dir).resolve() if project_dir else Path.cwd()
    proposal = Path(proposal_path).resolve() if proposal_path else None
    try:
        report = validate_knowledge(root, change_id=change_id, proposal_path=proposal)
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    if as_json:
        click.echo(report.model_dump_json(indent=2))
    else:
        _print_report(report)
    raise SystemExit(0 if report.ok else 1)


@knowledge_group.command("promote")
@click.option("--project-dir", "project_dir", default=None, help="Project root (default: cwd).")
@click.option("--change", "change_id", default=None, help="Promote change L2 proposals into L1.")
@click.option("--from", "proposal_path", default=None, help="Promote a single L2 proposal file.")
@click.option("--yes", is_flag=True, help="Apply without interactive confirmation.")
@click.option("--force", is_flag=True, help="Override conflicting leaf keys.")
def promote_command(
    project_dir: str | None,
    change_id: str | None,
    proposal_path: str | None,
    yes: bool,
    force: bool,
) -> None:
    """Merge validated L2 proposals into .aa/data-knowledge.yaml."""
    root = Path(project_dir).resolve() if project_dir else Path.cwd()
    proposal = Path(proposal_path).resolve() if proposal_path else None
    try:
        outcome = promote_knowledge(
            root,
            change_id=change_id,
            proposal_path=proposal,
            yes=yes,
            force=force,
        )
    except KnowledgePromoteError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    if outcome.changed:
        click.secho(
            f"promoted {len(outcome.merged_keys)} leaf key(s) into {root / '.aa/data-knowledge.yaml'}",
            fg="green",
        )
        for key in outcome.merged_keys:
            click.echo(f"  + {key}")
    else:
        click.secho("no changes (idempotent no-op)", fg="yellow")
    raise SystemExit(0)


def _print_report(report) -> None:
    click.secho("aa knowledge validate", bold=True)
    click.echo()
    for result in report.results:
        if result.ok:
            click.secho(f"✓ {result.path} [{result.artifact_type}]", fg="green")
        else:
            click.secho(f"✗ {result.path} [{result.artifact_type}]", fg="red")
            for error in result.errors:
                click.echo(f"    · {error}")
    click.echo()
