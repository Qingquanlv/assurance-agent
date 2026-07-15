import click

from assurance_agent import __version__
from assurance_agent.commands.config_cmd import config_group
from assurance_agent.commands.doctor import doctor_command
from assurance_agent.commands.init_cmd import init_command
from assurance_agent.commands.decide_cmd import decide_command
from assurance_agent.commands.gate_cmd import gate_group
from assurance_agent.commands.risk_cmd import risk_group
from assurance_agent.commands.heal_cmd import heal_group
from assurance_agent.commands.report_cmd import report_group
from assurance_agent.commands.run_cmd import run_command
from assurance_agent.commands.state_cmd import state_group
from assurance_agent.commands.status_cmd import status_command
from assurance_agent.commands.validate_cmd import validate_command
from assurance_agent.commands.skill_cmd import skill_group
from assurance_agent.commands.workflow_cmd import workflow_group
from assurance_agent.commands.eval_cmd import eval_group


@click.group()
@click.version_option(__version__, prog_name="aa")
def main() -> None:
    """aa - Assurance Agent deterministic QA workflow CLI."""


main.add_command(init_command)
main.add_command(doctor_command)
main.add_command(config_group)
main.add_command(validate_command)
main.add_command(status_command)
main.add_command(gate_group)
main.add_command(state_group)
main.add_command(decide_command)
main.add_command(risk_group)
main.add_command(run_command)
main.add_command(report_group)
main.add_command(heal_group)
main.add_command(workflow_group)
main.add_command(skill_group)
main.add_command(eval_group)
