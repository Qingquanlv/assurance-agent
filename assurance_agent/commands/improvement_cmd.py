"""``aa improvement list|show`` — project-ledger-backed Improvement views."""

from __future__ import annotations

import json
from pathlib import Path

import click
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementState,
)
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.improvements.events import (
    ImprovementLedgerIntegrityError,
    read_improvement_events,
)


def _project_root() -> Path:
    return Path.cwd()


def _load_ledger(project_root: Path) -> ImprovementLedgerProjection:
    path = project_root / "qa" / "improvements" / "improvements.json"
    if not path.is_file():
        return ImprovementLedgerProjection(schema_version="1", last_seq=0, improvements={}, by_fingerprint={})
    try:
        return ImprovementLedgerProjection.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as err:
        raise AaError(f"corrupt improvements.json: {err}") from err


@click.group("improvement")
def improvement_group() -> None:
    """Project Improvement Ledger projections (never scans qa/retro/)."""


@improvement_group.command("list")
@click.option("--state", "state_filter", default=None, help="Exact ImprovementState filter")
@click.option("--kind", "kind_filter", default=None, help="Exact ImprovementKind filter")
@click.option("--delivery", "delivery_filter", default=None, help="Exact DeliveryKind filter")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
def improvement_list(
    state_filter: str | None,
    kind_filter: str | None,
    delivery_filter: str | None,
    as_json: bool,
) -> None:
    """List Improvements from ``qa/improvements/improvements.json``, sorted by ID."""
    try:
        state = ImprovementState(state_filter) if state_filter else None
        kind = ImprovementKind(kind_filter) if kind_filter else None
        delivery = DeliveryKind(delivery_filter) if delivery_filter else None
    except ValueError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(2) from err

    try:
        ledger = _load_ledger(_project_root())
    except AaError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(1) from err

    items = [ledger.improvements[key] for key in sorted(ledger.improvements)]
    if state is not None:
        items = [item for item in items if item.state is state]
    if kind is not None:
        items = [item for item in items if item.kind is kind]
    if delivery is not None:
        items = [item for item in items if item.delivery is delivery]

    payload = {
        "improvements": [item.model_dump(mode="json") for item in items],
        "count": len(items),
    }
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    if not items:
        click.echo("(no improvements)")
        return
    for item in items:
        click.echo(f"{item.improvement_id}  [{item.state}]  {item.kind}  {item.delivery}")


@improvement_group.command("show")
@click.option("--id", "improvement_id", required=True, help="Improvement id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
def improvement_show(improvement_id: str, as_json: bool) -> None:
    """Show one Improvement projection plus its event timeline."""
    root = _project_root()
    try:
        ledger = _load_ledger(root)
    except AaError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(1) from err

    item = ledger.improvements.get(improvement_id)
    if item is None:
        click.echo(f"Error: improvement not found: {improvement_id}", err=True)
        raise SystemExit(1)

    events_path = root / "qa" / "improvements" / "events.jsonl"
    try:
        events = [
            event
            for event in read_improvement_events(events_path)
            if event.improvement_id == improvement_id
        ]
    except ImprovementLedgerIntegrityError as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(1) from err

    payload = item.model_dump(mode="json")
    payload["events"] = [event.model_dump(mode="json") for event in events]
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    click.echo(f"improvement_id: {item.improvement_id}")
    click.echo(f"state: {item.state}")
    click.echo(f"kind: {item.kind}")
    click.echo(f"delivery: {item.delivery}")
    click.echo(f"version: {item.version}")
    click.echo(f"events: {len(events)}")
    for event in events:
        click.echo(f"  seq={event.seq}  {event.type}  {event.event_id}")
