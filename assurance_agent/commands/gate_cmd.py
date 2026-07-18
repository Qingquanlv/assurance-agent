"""aa gate check — 把单个相位 gate 裁决为一个 verdict（确定性, 无 LLM）。

对齐 TS src/commands/gate.ts 的 flag 面与退出码：查询类命令写 best-effort 事件。
"""

from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.audit_evidence import build_gate_verdict_event
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import exit_code_for_gate_verdict
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.gates import check_gate
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


@click.group("gate")
def gate_group() -> None:
    """Gate adjudication commands."""


@gate_group.command("check")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", "phase_id", required=True, help="Phase whose gate to check.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def gate_check(change_id: str, phase_id: str, as_json: bool) -> None:
    """Adjudicate a single phase gate to one verdict (deterministic, no LLM)."""
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    change_dir = loc.path

    try:
        schema = load_workflow_schema(project_root)
        if not schema.has_phase(phase_id):
            click.secho(f"gate check failed: unknown phase '{phase_id}'", fg="red")
            raise SystemExit(1)
        gate_name = schema.gate_for_phase(phase_id)
        if gate_name is None:
            click.secho(f"gate check failed: phase '{phase_id}' has no gate", fg="red")
            raise SystemExit(1)
        state = read_state(change_dir)
        params = getattr(state, "params", None) or {}
        verdict = check_gate(schema, gate_name, loc, state, params)
    except AaError as err:
        click.secho(f"gate check failed: {err}", fg="red")
        raise SystemExit(1) from err

    append_event_best_effort(
        change_dir,
        build_gate_verdict_event(
            loc,
            schema,
            phase=phase_id,
            gate=verdict.gate,
            verdict=verdict.verdict.value if hasattr(verdict.verdict, "value") else str(verdict.verdict),
            matched_rule=verdict.matched_rule,
            reason=verdict.reason,
        ),
    )

    if as_json:
        click.echo(verdict.model_dump_json(indent=2))
    else:
        click.secho(f"aa gate check — {phase_id} → {verdict.gate}", bold=True)
        click.echo()
        click.echo(f"  Verdict : {verdict.verdict}")
        if verdict.reason:
            click.echo(f"  Reason  : {verdict.reason}")
        click.echo()

    raise SystemExit(exit_code_for_gate_verdict(verdict.verdict))
