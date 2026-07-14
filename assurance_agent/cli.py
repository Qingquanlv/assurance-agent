import click

from assurance_agent import __version__


@click.group()
@click.version_option(__version__, prog_name="aa")
def main() -> None:
    """aa - Assurance Agent deterministic QA workflow CLI."""
