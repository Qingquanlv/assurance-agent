"""Reduced ``aa retro`` CLI — trigger the canonical Retro graph / show one run."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import click
from pydantic import ValidationError

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.retro_batch import RetroBatchScope, RetroPipelineFailure
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_path_segment_safe
from assurance_agent.retro.supervisor import RetroInvocation, run_retro_supervised
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.loop import (
    EXIT_ERROR,
    run_workflow_loop,
)
from assurance_agent.workflow.graph.runtime import ensure_retro_params


def _generate_retro_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"retro-{stamp}"


def _ensure_shell_change(project_root: Path, retro_id: str) -> str:
    """Create a unique GraphRuntime shell Change for this Retro invocation."""
    shell_id = f"RETRO-RUN-{retro_id}"
    assert_path_segment_safe(shell_id, label="change id")
    shell_dir = project_root / "qa" / "changes" / shell_id
    shell_dir.mkdir(parents=True, exist_ok=True)
    return shell_id


def _build_params(
    *,
    retro_id: str,
    changes: tuple[str, ...],
    since: str | None,
    until: str | None,
    last: int | None,
    dry_run: bool,
    batch_scope: RetroBatchScope | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {
        "retro_id": retro_id,
        "retro_dry_run": dry_run,
    }
    if changes:
        params["change_ids"] = list(changes)
        if batch_scope is not None:
            params["batch_scope"] = batch_scope.model_dump(mode="json")
    elif since is not None or until is not None:
        params["since"] = since or ""
        params["until"] = until or ""
    else:
        if last is not None:
            params["retro_last"] = last
    return ensure_retro_params(params)


def _display_current_run(project_root: Path, retro_id: str, *, as_json: bool) -> dict[str, object]:
    """Read only the explicit current-run directory (no qa/retro listing)."""
    assert_path_segment_safe(retro_id, label="retro id")
    retro_dir = project_root / "qa" / "retro" / retro_id
    if not retro_dir.is_dir():
        raise AaError(f"retro run not found: {retro_id}")

    payload: dict[str, object] = {"retro_id": retro_id, "path": str(retro_dir)}
    context_path = retro_dir / "context.json"
    if context_path.is_file():
        try:
            payload["context"] = json.loads(context_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise AaError(f"corrupt context.json for {retro_id}: {err}") from err
    for name in (
        "proposal-candidates.json",
        "accept-status.json",
        "retro-summary.md",
        "review-queue.md",
    ):
        path = retro_dir / name
        if not path.is_file():
            continue
        if name.endswith(".json"):
            try:
                payload[name.removesuffix(".json")] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload[name.removesuffix(".json")] = None
        else:
            payload[name] = path.read_text(encoding="utf-8")

    if as_json:
        click.echo(json.dumps(payload, sort_keys=True))
    else:
        click.echo(f"retro_id: {retro_id}")
        ctx = payload.get("context")
        if isinstance(ctx, dict):
            click.echo(f"signal_count: {ctx.get('signal_count', 0)}")
        for key in ("proposal-candidates", "accept-status", "retro-summary.md", "review-queue.md"):
            if key in payload or key.replace(".md", "") in payload:
                click.echo(f"  present: {key}")
    return payload


def _run_retro_graph(
    *,
    project_root: Path,
    changes: tuple[str, ...],
    since: str | None,
    until: str | None,
    last: int | None,
    retro_id: str | None,
    dry_run: bool,
    as_json: bool,
    batch_manifest: Path | None = None,
) -> None:
    if since and changes:
        click.echo("Error: --since and --change are mutually exclusive", err=True)
        raise SystemExit(2)
    if until and changes:
        click.echo("Error: --until and --change are mutually exclusive", err=True)
        raise SystemExit(2)
    if last is not None and (changes or since is not None or until is not None):
        click.echo(
            "Error: --last is mutually exclusive with --change/--since/--until",
            err=True,
        )
        raise SystemExit(2)
    if batch_manifest is not None and (changes or since is not None or until is not None or last is not None):
        click.echo(
            "Error: --batch-manifest is mutually exclusive with --change/--last/--since/--until",
            err=True,
        )
        raise SystemExit(2)
    if batch_manifest is None and not changes and since is None and until is None and last is None:
        click.echo("Error: an explicit Retro window or --batch-manifest is required", err=True)
        raise SystemExit(2)

    batch_scope: RetroBatchScope | None = None
    preflight_failure: RetroPipelineFailure | None = None
    try:
        resolved_id = (
            retro_id.strip() if isinstance(retro_id, str) and retro_id.strip() else _generate_retro_id()
        )
        assert_path_segment_safe(resolved_id, label="retro id")
        if batch_manifest is not None:
            try:
                manifest_raw = batch_manifest.read_text(encoding="utf-8")
                manifest_payload = json.loads(manifest_raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
                raise AaError(f"cannot read Batch manifest: {err}") from err
            try:
                batch_scope = RetroBatchScope.model_validate(manifest_payload)
                changes = tuple(member.change_id for member in batch_scope.members)
            except ValidationError as err:
                identity = {
                    "retro_id": resolved_id,
                    "stage": "batch_contract",
                    "error_kind": "batch_scope_invalid",
                }
                preflight_failure = RetroPipelineFailure(
                    failure_id="FAIL-"
                    + sha256_bytes(canonical_json_bytes(identity)).removeprefix("sha256:")[:24],
                    retro_id=resolved_id,
                    stage="batch_contract",
                    error_kind="batch_scope_invalid",
                    message_fingerprint=sha256_bytes(str(err).encode("utf-8")),
                    occurred_at=datetime.now(timezone.utc),
                )
        params = _build_params(
            retro_id=resolved_id,
            changes=changes,
            since=since,
            until=until,
            last=last,
            dry_run=dry_run,
            batch_scope=batch_scope,
        )
        resolved_id = str(params["retro_id"])
        shell_id = _ensure_shell_change(project_root, resolved_id)
    except (UnsafeIdentifierError, AaError) as err:
        click.echo(f"Error: {err}", err=True)
        raise SystemExit(2) from err

    agent_cmd = os.environ.get("AA_RETRO_AGENT_CMD", "cursor-agent --print")
    adapter = HeadlessAdapter(agent_cmd=agent_cmd, cwd=project_root)
    loop_result = None

    def graph_runner(invocation: RetroInvocation):  # noqa: ANN202
        nonlocal loop_result
        loop_result = run_workflow_loop(
            project_root=invocation.project_root,
            change_id=invocation.shell_change_id,
            entrypoint="retro",
            adapter=adapter,
            params=dict(invocation.params),
        )
        return loop_result

    supervised = run_retro_supervised(
        RetroInvocation(
            project_root=project_root,
            shell_change_id=shell_id,
            retro_id=resolved_id,
            params=params,
        ),
        graph_runner=graph_runner,
        preflight_failure=preflight_failure,
    )
    if supervised.result == "technical_failure" or supervised.status is None:
        reason = getattr(loop_result, "reason", "Retro supervision failed")
        click.echo(reason, err=True)
        raise SystemExit(EXIT_ERROR)

    summary = {
        "retro_id": resolved_id,
        "dry_run": dry_run,
        "status": supervised.status.result,
        "reason": getattr(loop_result, "reason", supervised.status.result),
    }
    summary.update(
        {
            "batch_id": supervised.status.batch_id,
            "improvement_ids": list(supervised.status.improvement_ids),
            "outbox_id": supervised.status.outbox_id,
            "failure_ids": list(supervised.status.failure_ids),
        }
    )
    context_path = project_root / "qa" / "retro" / resolved_id / "context.json"
    if context_path.is_file():
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            summary["signal_count"] = context.get("signal_count", 0)
            summary["change_count"] = len(context.get("window", {}).get("change_ids", []))
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    if as_json:
        click.echo(json.dumps(summary, sort_keys=True))
        return
    click.echo(f"retro_id: {summary['retro_id']}")
    if "signal_count" in summary:
        click.echo(f"signal_count: {summary['signal_count']}")
    if "change_count" in summary:
        click.echo(f"change_count: {summary['change_count']}")
    click.echo(summary["reason"])


def register_retro(main_group: click.Group) -> None:
    @click.group("retro", invoke_without_command=True)
    @click.option("--since", default=None, help="Include Changes with terminal ts at/after this ISO time")
    @click.option("--until", default=None, help="Include Changes with terminal ts at/before this ISO time")
    @click.option("--change", "changes", multiple=True, help="Explicit Change id (repeatable)")
    @click.option("--last", type=int, default=None, help="Explicit last N terminal Changes")
    @click.option(
        "--batch-manifest",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
        help="Explicit Retro Batch manifest JSON",
    )
    @click.option("--retro-id", "retro_id", default=None, help="Retro id for the current run")
    @click.option(
        "--dry-run",
        is_flag=True,
        help="Collect and analyze evidence; skip propose/reconcile",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
    @click.pass_context
    def retro(ctx, since, until, changes, last, batch_manifest, retro_id, dry_run, as_json) -> None:
        """Trigger the canonical Retro graph or show one current run."""
        if ctx.invoked_subcommand is not None:
            return
        _run_retro_graph(
            project_root=Path.cwd(),
            changes=tuple(changes),
            since=since,
            until=until,
            last=last,
            retro_id=retro_id,
            dry_run=dry_run,
            as_json=as_json,
            batch_manifest=batch_manifest,
        )

    @retro.command("show")
    @click.option("--retro-id", "retro_id", required=True, help="Exact current-run retro id")
    @click.option("--json", "as_json", is_flag=True, help="Emit JSON")
    def show_run(retro_id: str, as_json: bool) -> None:
        """Display files under one explicit ``qa/retro/<retro-id>/`` (no listing)."""
        try:
            _display_current_run(Path.cwd(), retro_id, as_json=as_json)
        except (UnsafeIdentifierError, AaError) as err:
            click.echo(f"Error: {err}", err=True)
            raise SystemExit(1) from err

    main_group.add_command(retro)
