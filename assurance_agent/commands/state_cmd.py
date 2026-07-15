"""aa state apply / aa state heal — typed audit event + state presentation update."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import click

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.events import EventWriteError, append_event_strict
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.core.state import read_state, state_file, write_state
from assurance_agent.workflow.orchestration.gates import resolve_change_path
from assurance_agent.workflow.orchestration.schema import ORCHESTRATOR_INTERNAL, load_workflow_schema

HEAL_STATUSES = {"resolved", "exhausted", "not_needed", "failed", "skipped"}


@click.group("state")
def state_group() -> None:
    """Workflow-state maintenance commands for the orchestrator."""


def _validated_change_dir(change_id: str) -> tuple[Path, Path]:
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    return project_root, project_root / "qa" / "changes" / change_id


@state_group.command("apply")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", "phase_id", required=True, help="Schema phase id to apply.")
@click.option("--attempt-id", "attempt_id", default=None, help="Dispatch attempt id from the driver.")
@click.option("--skill", "skill", default=None, help="Skill loaded for this phase (Skill Load Gate).")
@click.option("--skill-md-path", "skill_md_path", default=None, help="Path to the loaded SKILL.md.")
def state_apply(
    change_id: str,
    phase_id: str,
    attempt_id: str | None,
    skill: str | None,
    skill_md_path: str | None,
) -> None:
    """Commit one completed phase outcome; gate routing happens in compute_status."""
    project_root, change_dir = _validated_change_dir(change_id)
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(1)

    try:
        schema = load_workflow_schema(project_root)
        if not schema.has_phase(phase_id):
            click.secho(f"state apply failed: unknown phase '{phase_id}'", fg="red")
            raise SystemExit(1)
        missing = [
            rel for rel in (schema.phase_produces(phase_id) or [])
            if not resolve_change_path(change_dir, rel).exists()
        ]
        if missing:
            click.secho(
                f"state apply failed: missing declared produces: {', '.join(missing)}", fg="red"
            )
            raise SystemExit(1)
    except AaError as err:
        click.secho(f"state apply failed: {err}", fg="red")
        raise SystemExit(1) from err

    applied_status = "pass" if phase_id in ORCHESTRATOR_INTERNAL else "done"
    phase_entry: dict[str, object] = {
        "status": applied_status,
        "skill_loaded": skill is not None,
        "skill_md_path": skill_md_path,
        "skill_loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if skill is not None:
        phase_entry["skill"] = skill

    outcome_id = attempt_id or f"manual:{phase_id}:{uuid4()}"
    commit_state_change(
        change_dir,
        event={
            "source": "progression",
            "type": "phase_outcome_committed",
            "phase": phase_id,
            "attempt_id": outcome_id,
            "gate_report": None,
        },
        next_state=_with_phase(read_state(change_dir), phase_id, phase_entry),
    )
    click.secho(f'workflow-state.yaml updated for phase "{phase_id}"', fg="green")


@state_group.command("heal")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--status", "status", required=True, help="Healing judgment.")
def state_heal(change_id: str, status: str) -> None:
    """Record an orchestrator healing judgment."""
    _project_root, change_dir = _validated_change_dir(change_id)
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(1)
    if status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        click.secho(f'unsupported healing status "{status}". Expected one of: {allowed}', fg="red")
        raise SystemExit(1)

    prior = _current_healing_status(change_dir)
    current = read_state(change_dir)
    commit_state_change(
        change_dir,
        event={"source": "status", "type": "heal_transition", "from": prior or "pending", "to": status},
        next_state=_with_healing_status(current, status),
    )
    click.secho(f"healing judgment recorded: {prior} → {status}", fg="green")


def commit_state_change(change_dir: Path, event: dict, next_state: WorkflowState) -> None:
    """Snapshot → strict event → atomic typed state; restore both files on failure."""
    snapshots = capture_files((change_dir / "events.jsonl", state_file(change_dir)))
    try:
        append_event_strict(change_dir, event)
        write_state(change_dir, next_state)
    except (EventWriteError, AaError, OSError) as err:
        restore_files(snapshots)
        click.secho(f"state transition rolled back: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _with_phase(state: WorkflowState, phase_id: str, entry: dict) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    phases[phase_id.replace("-", "_")] = entry
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _with_healing_status(state: WorkflowState, status: str) -> WorkflowState:
    data = state.model_dump(mode="python", exclude_none=True)
    phases = dict(data.get("phases") or {})
    healing = dict(phases.get("healing") or {})
    healing["status"] = status
    phases["healing"] = healing
    data["phases"] = phases
    return WorkflowState.model_validate(data)


def _current_healing_status(change_dir: Path) -> str | None:
    try:
        state = read_state(change_dir)
    except AaError:
        return None
    return state.phases.healing.status
