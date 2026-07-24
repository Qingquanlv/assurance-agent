"""aa gate check — return the latest frozen gate report for a node path.

Refuses to re-adjudicate mutable files; only ledger-frozen ``gate_report`` values
are returned.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR, exit_code_for_gate_verdict
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.runtime_factory import build_graph_runtime
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.runtime import GraphRuntimeError


@click.group("gate")
def gate_group() -> None:
    """Gate adjudication commands."""


@gate_group.command("check")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option(
    "--node-path",
    "node_path",
    required=True,
    help="Structural task id / node path whose frozen gate report to return.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def gate_check(change_id: str, node_path: str, as_json: bool) -> None:
    """Return the latest frozen gate report for a node task (no live re-adjudication)."""
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    adapter = HeadlessAdapter(agent_cmd="true", cwd=project_root)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
        )
        latest = bundle.runtime.latest_root_invocation()
        if latest is None:
            click.secho("gate check failed: no graph invocation found", fg="red")
            raise SystemExit(EXIT_ERROR)
        projection = CheckpointStore(loc.path).project(latest)
    except GraphRuntimeError as err:
        click.secho(f"gate check failed: {err}", fg="red")
        raise SystemExit(1) from err
    except Exception as err:
        click.secho(f"gate check failed: {err}", fg="red")
        raise SystemExit(1) from err

    task = projection.tasks.get(node_path)
    if task is None:
        # Allow matching by node_id suffix when a single candidate exists.
        matches = [
            t
            for t in projection.tasks.values()
            if t.node_id == node_path or t.task_id.endswith(f":{node_path}")
        ]
        if len(matches) == 1:
            task = matches[0]
        else:
            click.secho(
                f"gate check failed: no task matched node-path '{node_path}'",
                fg="red",
            )
            raise SystemExit(EXIT_ERROR)

    report = task.gate_report
    if report is None:
        click.secho(
            f"gate check failed: task '{task.task_id}' has no frozen gate_report "
            "(refusing to re-adjudicate mutable files)",
            fg="red",
        )
        raise SystemExit(EXIT_ERROR)

    verdict = str(report.get("verdict") or report.get("value") or "")
    if as_json:
        click.echo(
            json.dumps(
                {
                    "node_path": node_path,
                    "task_id": task.task_id,
                    "gate_report": report,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        click.secho(f"aa gate check — {task.task_id}", bold=True)
        click.echo()
        click.echo(f"  Verdict : {verdict}")
        if report.get("reason"):
            click.echo(f"  Reason  : {report['reason']}")
        details = report.get("details")
        if isinstance(details, dict):
            missing = details.get("missing_capabilities")
            if isinstance(missing, list) and missing:
                click.echo(f"  Missing capabilities: {', '.join(str(item) for item in missing)}")
        click.echo()

    raise SystemExit(exit_code_for_gate_verdict(verdict))
