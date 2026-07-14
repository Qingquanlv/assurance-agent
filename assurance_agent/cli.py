import click

from assurance_agent import __version__
from assurance_agent.commands.doctor import doctor_command
from assurance_agent.commands.init_cmd import init_command


@click.group()
@click.version_option(__version__, prog_name="aa")
def main() -> None:
    """aa - Assurance Agent deterministic QA workflow CLI."""


main.add_command(init_command)
main.add_command(doctor_command)
