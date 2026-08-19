"""``aa improvement list|show`` — project-ledger-backed Improvement views."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.improvements import (
    DeliveryKind,
    ImprovementKind,
    ImprovementLedgerProjection,
    ImprovementState,
)
from assurance_agent.commands._product_guard import require_assurance_product
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.improvements.events import (
    ImprovementLedgerIntegrityError,
    read_improvement_events,
)
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.loop import EXIT_COMPLETED, run_workflow_loop
from assurance_agent.workflow.improvements.auto_review import AutoReviewItem


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
    require_assurance_product()


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
        click.echo(
            f"{item.improvement_id}  [{item.state}]  {item.kind}  {item.delivery}  "
            f"approval={item.approval_source}"
        )


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
            event for event in read_improvement_events(events_path) if event.improvement_id == improvement_id
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
    click.echo(f"approval_source: {item.approval_source}")
    click.echo(f"review_subject_sha256: {item.review_subject_sha256 or '-'}")
    if item.last_auto_review is not None:
        click.echo(f"last_auto_review: {item.last_auto_review.verdict} ({item.last_auto_review.review_id})")
    click.echo(f"events: {len(events)}")
    for event in events:
        click.echo(f"  seq={event.seq}  {event.type}  {event.event_id}")


@improvement_group.command("auto-review")
@click.argument("improvement_id")
@click.option("--attempt", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
def improvement_auto_review(improvement_id: str, attempt: int, as_json: bool) -> None:
    """Run the standalone bounded Auto Review Graph for one current subject."""
    root = _project_root()
    ledger = _load_ledger(root)
    current = ledger.improvements.get(improvement_id)
    if current is None or current.review_subject_sha256 is None:
        raise click.ClickException("Improvement has no current review subject")
    if attempt > 1 and (
        current.last_auto_review is None
        or current.last_auto_review.subject_sha256 != current.review_subject_sha256
        or current.last_auto_review.verdict != "review_error"
    ):
        raise click.ClickException("retry is allowed only after review_error for the current subject")
    identity = f"{improvement_id}:{current.review_subject_sha256}:1:{attempt}"
    review_id = "AUTO-" + sha256_bytes(identity.encode()).removeprefix("sha256:")[:24]
    item = AutoReviewItem(
        improvement_id=improvement_id,
        subject_sha256=current.review_subject_sha256,
        expected_improvement_version=current.version,
        review_id=review_id,
        attempt=attempt,
    )
    shell_id = f"IMPROVEMENT-AUTO-REVIEW-{review_id}"
    (root / "qa" / "changes" / shell_id).mkdir(parents=True, exist_ok=True)
    adapter = HeadlessAdapter(
        agent_cmd=os.environ.get("AA_IMPROVEMENT_REVIEW_AGENT_CMD", "cursor-agent --print"),
        cwd=root,
    )
    result = run_workflow_loop(
        project_root=root,
        change_id=shell_id,
        entrypoint="improvement-auto-review",
        adapter=adapter,
        params=item.model_dump(mode="json"),
    )
    status_path = root / "qa" / "improvements" / "reviews" / review_id / "status.json"
    payload = (
        json.loads(status_path.read_text())
        if status_path.is_file()
        else {
            "review_id": review_id,
            "result": "technical_failure",
            "reason": result.reason,
        }
    )
    click.echo(json.dumps(payload, sort_keys=True) if as_json else f"{review_id}: {payload['result']}")
    if result.exit_code != EXIT_COMPLETED:
        raise SystemExit(result.exit_code)
