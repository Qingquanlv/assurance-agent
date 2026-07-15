from pathlib import Path

import click

from assurance_agent.workflow.core.assets import (
    SyncResult,
    find_project_root,
    sync_opencode,
    sync_skills,
)


def _print(kind: str, result: SyncResult) -> None:
    click.echo(
        f"{kind}: {len(result.created)} created, "
        f"{len(result.updated)} updated, {len(result.unchanged)} unchanged"
    )
    for rel in result.created:
        click.secho(f"  created: {rel}", fg="green")
    for rel in result.updated:
        click.secho(f"  updated: {rel}", fg="yellow")


@click.group("skill")
def skill_group() -> None:
    """Skill maintenance commands."""


@skill_group.command("refresh")
@click.option("--sync-agents", is_flag=True, help="Also sync .opencode/ agents, tools and plugin.")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def refresh_command(sync_agents: bool, dry_run: bool) -> None:
    """Sync packaged skills (always) and OpenCode assets (--sync-agents) into this project."""
    root = find_project_root(Path.cwd())
    if root is None:
        click.secho("No assurance-agent project found near cwd (.aa/config.yaml or qa/).", fg="red")
        raise SystemExit(1)

    click.secho(f"aa skill refresh{' (DRY RUN)' if dry_run else ''} — {root}", bold=True)
    _print("skills", sync_skills(root, dry_run=dry_run))
    if sync_agents:
        _print("opencode", sync_opencode(root, dry_run=dry_run))
    if not dry_run:
        click.secho("Refresh complete. Restart OpenCode to pick up changes.", fg="green", bold=True)
