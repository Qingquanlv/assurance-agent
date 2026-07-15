"""aa decide — 记录一次受支持的人工工作流决定。

对齐 TS src/commands/decide.ts + workflow/core/decide.ts 的 flag 面与语义。
审计型命令复用 state_cmd.commit_state_change 的 snapshot 边界：strict human_decision
后写 canonical WorkflowState，失败恢复 event/state 两文件。
"""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.commands.state_cmd import commit_state_change
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.engine import compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema

HUMAN_DECISION_ACTIONS = {"fix_and_proceed", "accept_risk", "stop", "allow_test_changes", "skip_branch"}


@click.command("decide")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--at", "checkpoint", required=True, help="Gate, phase, or supported workflow checkpoint.")
@click.option("--action", "action", required=True, help="Supported action for the checkpoint.")
@click.option("--reason", "reason", required=True, help="Human decision reason.")
@click.option(
    "--evidence", "evidence", default=None, help="Supporting evidence file within the project root."
)
def decide_command(change_id: str, checkpoint: str, action: str, reason: str, evidence: str | None) -> None:
    """Record a supported human workflow decision."""
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(1)
    if action not in HUMAN_DECISION_ACTIONS:
        click.secho(f"decide failed: unsupported action '{action}'", fg="red")
        raise SystemExit(1)
    if not reason.strip():
        click.secho("decide failed: decision reason is required", fg="red")
        raise SystemExit(1)

    who = (os.environ.get("USER") or "unknown").strip() or "unknown"
    event: dict = {
        "source": "decide",
        "type": "human_decision",
        "checkpoint": checkpoint,
        "action": action,
        "reason": reason,
        "who": who,
    }
    if evidence is not None:
        evidence_path = (project_root / evidence).resolve()
        try:
            evidence_path.relative_to(project_root.resolve())
        except ValueError:
            click.secho("decide failed: evidence must stay under project root", fg="red")
            raise SystemExit(1)
        if not evidence_path.is_file():
            click.secho(f"decide failed: evidence not found: {evidence}", fg="red")
            raise SystemExit(1)
        event["review_file"] = evidence_path.relative_to(project_root).as_posix()
        event["review_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    stop_snapshot: dict | None = None
    if action == "stop":
        try:
            schema = load_workflow_schema(project_root)
            state = read_state(change_dir)
            params = state.params
            status = compute_status(schema, change_dir, state, params)
        except AaError as err:
            click.secho(f"decide failed: {err}", fg="red")
            raise SystemExit(1) from err
        if status.terminal is not None:
            click.secho(f"decide failed: workflow already terminal ({status.terminal.kind})", fg="red")
            raise SystemExit(1)
        stop_snapshot = {
            "next": [d.phase_id for d in status.next_dispatch],
            "phases": {p.id: p.status for p in status.phases},
        }
    _commit_decision(change_dir, event, action, checkpoint, reason, who, stop_snapshot)
    click.secho(f"aa decide — {checkpoint}", bold=True)
    click.secho(f"human_decision recorded: action={action}", fg="green")


def _commit_decision(
    change_dir: Path,
    event: dict,
    action: str,
    checkpoint: str,
    reason: str,
    who: str,
    stop_snapshot: dict | None,
) -> None:
    try:
        state = read_state(change_dir)
    except AaError as err:
        click.secho(f"decide failed: {err}", fg="red")
        raise SystemExit(1) from err

    decisions = (state.model_extra or {}).get("decisions")
    if not isinstance(decisions, list):
        decisions = []
    record: dict[str, object] = {
        "checkpoint": checkpoint,
        "action": action,
        "reason": reason,
        "who": who,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if stop_snapshot is not None:
        record["state_at_stop"] = stop_snapshot
    decisions.append(record)
    data = state.model_dump(mode="python", exclude_none=True)
    data["decisions"] = decisions
    if action == "stop":
        data["terminal"] = {"kind": "stopped", "reason": reason}
    commit_state_change(change_dir, event, WorkflowState.model_validate(data))
