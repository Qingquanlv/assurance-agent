from pathlib import Path

import click

from assurance_agent.config import CONFIG_RELPATH


@click.group("config")
def config_group() -> None:
    """Manage assurance-agent configuration."""


@config_group.command("print")
def config_print() -> None:
    """Print the raw contents of .aa/config.yaml."""
    path = Path.cwd() / CONFIG_RELPATH
    if not path.is_file():
        click.secho(f"{CONFIG_RELPATH} not found. Run `aa init` first.", fg="red")
        raise SystemExit(1)
    click.echo(path.read_text(encoding="utf-8"), nl=False)
