"""`aa run` command: execute selected test layers and publish execution evidence."""
from pathlib import Path

import click

from assurance_agent.config import AaConfig, load_config
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.core.events import (
    EventWriteError,
    HumanDecisionEvent,
    append_event_best_effort,
    append_event_strict,
)
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.execution.runner import generate_batch_id, run_change
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.override_evidence import write_test_changes_override_evidence
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    assert_product_tree_unchanged_in_healing,
    assert_test_tree_unchanged_or_healing,
)


@click.command("run")
@click.option("--change", "change_id", required=True, help="Change ID (e.g. REQ-002-user-logout).")
@click.option("--rerun-reason", "rerun_reason", default=None,
              help="Required when re-running after execution is already done.")
@click.option("--allow-test-changes", "allow_test_changes", is_flag=True, default=False,
              help="Allow test-tree changes with override evidence and audit reason.")
def run_command(change_id: str, rerun_reason: str | None, allow_test_changes: bool) -> None:
    """Execute API/E2E/Fuzz/Performance tests for a change and write normalized results."""
    project_root = Path.cwd()
    try:
        config = load_config(project_root)
        change_dir = _change_dir(project_root, config, change_id)
    except AaError as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(1) from err

    if not change_dir.is_dir():
        click.secho(f"Run failed: change directory not found: {change_dir}", fg="red")
        raise SystemExit(1)

    click.secho(f"\naa run — change: {change_id}\n", bold=True)

    try:
        manifest = _execute(
            project_root, change_dir, config, change_id, rerun_reason, allow_test_changes,
        )
    except (AaError, HealingGuardError) as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(1) from err

    _print_manifest(manifest, change_dir)
    raise SystemExit(_exit_code(manifest))


def _execute(
    project_root: Path,
    change_dir: Path,
    config: AaConfig,
    change_id: str,
    rerun_reason: str | None,
    allow_test_changes: bool,
):
    integrity = assert_test_tree_unchanged_or_healing(
        project_root, change_id, allow_test_changes=allow_test_changes,
    )
    assert_product_tree_unchanged_in_healing(project_root, change_id)

    batch_id = generate_batch_id()
    batch_dir = change_dir / "execution" / "runs" / batch_id

    if integrity.tests_changed and allow_test_changes:
        if not rerun_reason:
            raise HealingGuardError(
                "--allow-test-changes requires --rerun-reason for audit trail"
            )
        current_tree = hash_test_tree(project_root)
        override_json = batch_dir / "test-changes-override.json"
        override_diff = batch_dir / "test-changes-override.diff"
        events_path = change_dir / "events.jsonl"
        snapshots = capture_files((override_json, override_diff, events_path))
        try:
            evidence = write_test_changes_override_evidence(
                project_root=project_root,
                change_id=change_id,
                batch_id=batch_id,
                batch_dir=batch_dir,
                reason=rerun_reason,
                integrity=current_tree,
            )
            append_event_strict(change_dir, HumanDecisionEvent(
                checkpoint="test-tree-guard",
                action="allow_test_changes",
                reason=rerun_reason,
                who="cli",
                review_file=evidence.rel_path,
                review_sha256=evidence.sha256,
            ))
        except EventWriteError:
            restore_files(snapshots)
            raise

    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_start",
        **({"rerun_reason": rerun_reason} if rerun_reason else {}),
    })
    manifest = run_change(project_root, change_dir, config, batch_id=batch_id)
    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_manifest_written",
        "batch_id": manifest.batch_id, "final_status": manifest.final_status,
    })
    append_event_best_effort(change_dir, {
        "source": "run", "type": "execution_end", "batch_id": manifest.batch_id,
    })
    return manifest


def _change_dir(project_root: Path, config: AaConfig, change_id: str) -> Path:
    assert_change_id_safe(change_id)
    rel = config.qa.changes
    rel = rel[2:] if rel.startswith("./") else rel
    return project_root / rel / change_id


def _print_manifest(manifest, change_dir: Path) -> None:  # noqa: ANN001
    execution_dir = change_dir / "execution"
    color = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red", "SKIPPED": "yellow"}
    click.echo("Execution Results")
    click.echo("  Final Status : " + click.style(manifest.final_status, fg=color[manifest.final_status], bold=True))
    click.echo(f"  Batch ID     : {manifest.batch_id}")
    for key in ("api", "e2e", "fuzz", "performance", "coverage"):
        sel = getattr(manifest.selected_targets, key, None)
        rel = manifest.result_files.get(key)
        if rel:
            click.echo(f"  {key:<12}: {execution_dir / rel}")
        elif sel is False:
            click.echo(f"  {key:<12}: unselected")
    click.echo(f"  manifest     : {execution_dir / 'execution-manifest.yaml'}")

    if manifest.final_status == "FAIL":
        click.secho("→ Quality gate failed. Run: aa report inspect --change <id>", fg="red")
    elif manifest.final_status == "PASS":
        click.secho("→ Quality gate passed. Proceed to archive-for-qa.", fg="green")
    elif manifest.final_status == "PASS_WITH_WARNINGS":
        click.secho("→ Passed with warnings. Run: aa report inspect --change <id>", fg="yellow")
    else:
        click.secho("→ No results produced. See summary.md / run report inspect.", fg="cyan")


def _exit_code(manifest) -> int:  # noqa: ANN001
    if manifest.final_status in ("PASS", "PASS_WITH_WARNINGS"):
        return 0
    if manifest.final_status == "FAIL":
        return 1
    any_selected = any(
        getattr(manifest.selected_targets, k) for k in ("api", "e2e", "fuzz", "performance")
    )
    return 1 if any_selected else 0
