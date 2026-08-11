from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.assets import (
    SyncResult,
    ensure_opencode_plugin_registration,
    find_project_root,
    opencode_user_agents_root,
    opencode_user_skills_root,
    sync_opencode,
    sync_opencode_user_agents,
    sync_opencode_user_skills,
    sync_skills,
    verify_packaged_agents,
    verify_packaged_skills,
)


def _print(kind: str, result: SyncResult) -> None:
    click.echo(
        f"{kind}: {len(result.created)} created, "
        f"{len(result.updated)} updated, {len(result.unchanged)} unchanged, "
        f"{len(result.removed)} removed"
    )
    for rel in result.created:
        click.secho(f"  created: {rel}", fg="green")
    for rel in result.updated:
        click.secho(f"  updated: {rel}", fg="yellow")
    for rel in result.removed:
        click.secho(f"  removed: {rel}", fg="yellow")


@click.group("skill")
def skill_group() -> None:
    """Skill maintenance commands."""


@skill_group.command("refresh")
@click.option(
    "--sync-agents",
    is_flag=True,
    help="Also sync .opencode/ runtime skills, agents, tools and plugin.",
)
@click.option(
    "--sync-opencode-user-skills",
    "sync_user_skills",
    is_flag=True,
    help="Also sync namespaced AA skills to the OpenCode/OMO user runtime.",
)
@click.option(
    "--sync-opencode-user-agents",
    "sync_user_agents",
    is_flag=True,
    help="Also sync namespaced AA agents to the OpenCode user runtime.",
)
@click.option("--dry-run", is_flag=True, help="Show what would change without writing.")
def refresh_command(
    sync_agents: bool,
    sync_user_skills: bool,
    sync_user_agents: bool,
    dry_run: bool,
) -> None:
    """Sync packaged skills (always) and OpenCode assets (--sync-agents) into this project."""
    root = find_project_root(Path.cwd())
    if root is None:
        click.secho("No assurance-agent project found near cwd (.aa/config.yaml or qa/).", fg="red")
        raise SystemExit(1)

    try:
        click.secho(f"aa skill refresh{' (DRY RUN)' if dry_run else ''} — {root}", bold=True)
        _print("skills", sync_skills(root, dry_run=dry_run))
        if sync_agents:
            ensure_opencode_plugin_registration(root, dry_run=dry_run)
            _print("opencode", sync_opencode(root, dry_run=dry_run))
        user_skills_root = opencode_user_skills_root() if sync_user_skills else None
        if user_skills_root is not None:
            _print(
                "opencode-user-skills",
                sync_opencode_user_skills(user_skills_root, dry_run=dry_run),
            )
        user_agents_root = opencode_user_agents_root() if sync_user_agents else None
        if user_agents_root is not None:
            _print(
                "opencode-user-agents",
                sync_opencode_user_agents(user_agents_root, dry_run=dry_run),
            )
        if not dry_run:
            verify_packaged_skills(root / "skills")
            if sync_agents:
                verify_packaged_skills(root / ".opencode" / "skills")
            if user_skills_root is not None:
                verify_packaged_skills(user_skills_root, namespaced_only=True)
            if user_agents_root is not None:
                verify_packaged_agents(user_agents_root)
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    if not dry_run:
        click.secho("Refresh complete. Restart OpenCode to pick up changes.", fg="green", bold=True)
