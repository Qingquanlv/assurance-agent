"""Shared exclusive --schema / --contracts Click options."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import click

from assurance_agent.workflow.driver.loop import EXIT_ERROR
from assurance_agent.workflow.graph.overlay import OverlayPathError, resolve_explicit_overlay

_F = TypeVar("_F", bound=Callable[..., object])


def overlay_options(command: _F) -> _F:
    command = click.option(
        "--schema",
        "explicit_schema",
        type=click.Path(path_type=Path),
        default=None,
        help="Exclusive workflow schema under .aa/ or schemas/. Missing path fails closed.",
    )(command)
    command = click.option(
        "--contracts",
        "explicit_contracts",
        type=click.Path(path_type=Path),
        default=None,
        help="Exclusive execution contracts under .aa/ or schemas/. Missing path fails closed.",
    )(command)
    return command


def resolve_cli_overlays(
    project_root: Path,
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
) -> tuple[Path | None, Path | None]:
    try:
        schema = (
            resolve_explicit_overlay(project_root, explicit_schema) if explicit_schema is not None else None
        )
        contracts = (
            resolve_explicit_overlay(project_root, explicit_contracts)
            if explicit_contracts is not None
            else None
        )
    except OverlayPathError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    return schema, contracts
