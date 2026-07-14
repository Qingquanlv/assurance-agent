"""`aa init` command: scaffold or repair the .aa/ + qa/ + tests/ project layout."""

from pathlib import Path
from typing import Literal, cast

import click

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.generator import GenerateResult, generate_project, repair_project
from assurance_agent.workflow.core.templates import InitAnswers

ApiFramework = Literal["pytest", "none"]
E2eFramework = Literal["playwright", "none"]


def _print_result(result: GenerateResult) -> None:
    for rel in result.created:
        click.secho(f"created: {rel}", fg="green")
    for rel in result.skipped:
        click.secho(f"skipped (exists): {rel}", fg="yellow")


@click.command("init")
@click.option("--repair", is_flag=True, help="Repair mode: only create missing files, never overwrite.")
@click.option("--yes", is_flag=True, help="Non-interactive: accept all defaults.")
@click.option("--api-framework", type=click.Choice(["pytest", "none"]), default=None)
@click.option("--e2e-framework", type=click.Choice(["playwright", "none"]), default=None)
@click.option("--frontend", default=None, help="Frontend source path (default ./frontend).")
@click.option("--backend", default=None, help="Backend source path (default ./backend).")
@click.option("--enable-mcp", is_flag=True, default=None)
def init_command(
    repair: bool,
    yes: bool,
    api_framework: str | None,
    e2e_framework: str | None,
    frontend: str | None,
    backend: str | None,
    enable_mcp: bool | None,
) -> None:
    """Initialize an assurance-agent QA project in the current directory."""
    root = Path.cwd()
    try:
        if repair:
            click.echo("Running repair...")
            _print_result(repair_project(root))
            click.secho("Repair complete.", fg="green", bold=True)
            return

        # click.Choice already restricts these to the literal values at runtime.
        api_choice = cast("ApiFramework | None", api_framework)
        e2e_choice = cast("E2eFramework | None", e2e_framework)
        answers = _collect_answers(yes, api_choice, e2e_choice, frontend, backend, enable_mcp)
        if answers is None:
            click.echo("Init cancelled.")
            return
        _print_result(generate_project(root, answers))
        click.secho("assurance-agent initialized successfully.", fg="green", bold=True)
        click.echo("Run 'aa doctor' to verify your environment.")
    except AaError as err:
        # stdout (not stderr): click 8.2+'s CliRunner.output no longer merges
        # stderr, so errors go to stdout for consistent integration-test/manual output.
        click.secho(str(err), fg="red")
        raise SystemExit(1)


def _collect_answers(
    yes: bool,
    api_framework: ApiFramework | None,
    e2e_framework: E2eFramework | None,
    frontend: str | None,
    backend: str | None,
    enable_mcp: bool | None,
) -> InitAnswers | None:
    if yes:
        return InitAnswers(
            api_framework=api_framework or "pytest",
            e2e_framework=e2e_framework or "playwright",
            enable_mcp=bool(enable_mcp),
            frontend_path=frontend,
            backend_path=backend,
        )

    api = api_framework or cast(
        ApiFramework,
        click.prompt("API test framework", type=click.Choice(["pytest", "none"]), default="pytest"),
    )
    e2e = e2e_framework or cast(
        E2eFramework,
        click.prompt("E2E test framework", type=click.Choice(["playwright", "none"]), default="playwright"),
    )
    mcp = enable_mcp if enable_mcp is not None else click.confirm("Enable MCP config?", default=False)
    if not click.confirm("Confirm and write files?", default=True):
        return None
    return InitAnswers(
        api_framework=api,
        e2e_framework=e2e,
        enable_mcp=mcp,
        frontend_path=frontend,
        backend_path=backend,
    )
