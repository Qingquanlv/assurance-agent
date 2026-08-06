"""`aa run` command: execute selected test layers and publish execution evidence."""

from pathlib import Path

import click

from assurance_agent.artifacts.models import QualityGateResult
from assurance_agent.change_location import resolve_change
from assurance_agent.config import AaConfig, load_config
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError, transaction
from assurance_agent.workflow.execution.runner import generate_batch_id, run_change
from assurance_agent.workflow.execution.tree_hash import hash_test_tree
from assurance_agent.workflow.healing.override_evidence import build_test_changes_override_evidence
from assurance_agent.workflow.healing.override_policy import (
    TOKEN_REL_PATH,
    assert_test_changes_override_allowed,
    consume_test_changes_override_token,
    load_test_changes_override_policy,
    read_test_changes_override_token,
    token_json_bytes,
)
from assurance_agent.workflow.healing.safety import (
    HealingGuardError,
    assert_product_tree_unchanged_in_healing,
    assert_test_tree_unchanged_or_healing,
)


@click.command("run")
@click.option("--change", "change_id", required=True, help="Change ID (e.g. REQ-002-user-logout).")
@click.option(
    "--rerun-reason",
    "rerun_reason",
    default=None,
    help="Required when re-running after execution is already done.",
)
@click.option(
    "--allow-test-changes",
    "allow_test_changes",
    is_flag=True,
    default=False,
    help="Consume a prior aa decide allow_test_changes authorization for the current test tree.",
)
def run_command(change_id: str, rerun_reason: str | None, allow_test_changes: bool) -> None:
    """Execute API/E2E/Fuzz/Performance tests for a change and write normalized results."""
    project_root = Path.cwd()
    try:
        config = load_config(project_root)
        change_dir = resolve_change(project_root, change_id).path
    except AaError as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(1) from err

    click.secho(f"\naa run — change: {change_id}\n", bold=True)

    try:
        manifest = _execute(
            project_root,
            change_dir,
            config,
            change_id,
            rerun_reason,
            allow_test_changes,
        )
    except ProgressionError as err:
        click.secho(f"Run failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err
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
    if allow_test_changes and not (rerun_reason or "").strip():
        raise AaError("--rerun-reason is required with --allow-test-changes")
    token = None
    try:
        integrity = assert_test_tree_unchanged_or_healing(
            project_root,
            change_id,
            allow_test_changes=False,
        )
    except HealingGuardError as err:
        if "TESTS-CHANGED-WITHOUT-HEALING" not in str(err):
            raise
        if not allow_test_changes:
            raise
        current_tree = hash_test_tree(project_root)
        token = read_test_changes_override_token(
            change_dir,
            change_id=change_id,
            current_tests_tree_sha256=current_tree.aggregate,
        )
        if token is None:
            raise
        integrity = assert_test_tree_unchanged_or_healing(
            project_root,
            change_id,
            allow_test_changes=True,
        )
    assert_product_tree_unchanged_in_healing(project_root, change_id)

    batch_id = generate_batch_id()

    if integrity.tests_changed and token is not None:
        assert_test_changes_override_allowed(
            change_dir,
            integrity,
            load_test_changes_override_policy(project_root),
        )
        from datetime import datetime, timezone
        import subprocess

        current_tree = hash_test_tree(project_root)
        try:
            diff_result = subprocess.run(
                ["git", "diff", "--", "tests/"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            diff_text = diff_result.stdout or ""
        except OSError:
            diff_text = ""
        evidence = build_test_changes_override_evidence(
            change_id=change_id,
            batch_id=batch_id,
            reason=token.reason,
            integrity=current_tree,
            created_at=datetime.now(timezone.utc).isoformat(),
            diff_text=diff_text,
        )
        with transaction(change_dir) as txn:
            txn.write_file(
                f"execution/runs/{batch_id}/test-changes-override.json",
                evidence.json_bytes,
            )
            txn.write_file(
                f"execution/runs/{batch_id}/test-changes-override.diff",
                evidence.diff_bytes,
            )
            txn.write_file(
                TOKEN_REL_PATH.as_posix(),
                token_json_bytes(consume_test_changes_override_token(token, batch_id=batch_id)),
            )

    append_event_best_effort(
        change_dir,
        {
            "source": "run",
            "type": "execution_start",
            **({"rerun_reason": rerun_reason} if rerun_reason else {}),
        },
    )
    manifest = run_change(project_root, change_dir, config, batch_id=batch_id)
    append_event_best_effort(
        change_dir,
        {
            "source": "run",
            "type": "execution_manifest_written",
            "batch_id": manifest.batch_id,
            "final_status": manifest.final_status,
        },
    )
    append_event_best_effort(
        change_dir,
        {
            "source": "run",
            "type": "execution_end",
            "batch_id": manifest.batch_id,
        },
    )
    return manifest


def _print_manifest(manifest, change_dir: Path) -> None:  # noqa: ANN001
    execution_dir = change_dir / "execution"
    color = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red", "SKIPPED": "yellow"}
    click.echo("Execution Results")
    click.echo(
        "  Final Status : " + click.style(manifest.final_status, fg=color[manifest.final_status], bold=True)
    )
    click.echo(f"  Batch ID     : {manifest.batch_id}")
    for key in ("api", "e2e", "fuzz", "performance", "coverage"):
        sel = getattr(manifest.selected_targets, key, None)
        rel = manifest.result_files.get(key)
        if rel:
            click.echo(f"  {key:<12}: {execution_dir / rel}")
        elif sel is False:
            click.echo(f"  {key:<12}: unselected")
    click.echo(f"  manifest     : {execution_dir / 'execution-manifest.yaml'}")

    warnings = _gate_warnings(execution_dir)
    if warnings:
        # Informational, and printed after the verdict for that reason: these do
        # not imply PASS_WITH_WARNINGS. That status is a dimension that degraded;
        # a warning here is usually the opposite — something that could not be
        # judged at all — and the verdict above already accounts for everything
        # that was.
        click.secho("  Warnings", fg="yellow")
        for warning in warnings:
            click.secho(f"    - {warning}", fg="yellow")

    if manifest.final_status == "FAIL":
        click.secho("→ Quality gate failed. Run: aa report inspect --change <id>", fg="red")
    elif manifest.final_status == "PASS":
        click.secho("→ Quality gate passed. Proceed to archive-for-qa.", fg="green")
    elif manifest.final_status == "PASS_WITH_WARNINGS":
        click.secho("→ Passed with warnings. Run: aa report inspect --change <id>", fg="yellow")
    else:
        click.secho("→ No results produced. See summary.md / run report inspect.", fg="cyan")


def _gate_warnings(execution_dir: Path) -> list[str]:
    """The warnings on the gate this run just published, or none if unreadable.

    A read-only convenience view, not a second source of truth: the verdict and
    the exit code come from the manifest the runner returned, so a gate file that
    is missing or unparseable costs the operator these lines and nothing else.
    Worth reading at all because the manifest carries only `final_status`, and
    the runner records things there that no status can express — a sufficiency
    evaluation that could not run, for one.
    """
    try:
        text = (execution_dir / "quality-gate-result.json").read_text(encoding="utf-8")
        gate = QualityGateResult.model_validate_json(text)
    except (OSError, ValueError):
        return []
    return list(gate.warnings or [])


def _exit_code(manifest) -> int:  # noqa: ANN001
    if manifest.final_status in ("PASS", "PASS_WITH_WARNINGS"):
        return 0
    if manifest.final_status == "FAIL":
        return 1
    any_selected = any(getattr(manifest.selected_targets, k) for k in ("api", "e2e", "fuzz", "performance"))
    return 1 if any_selected else 0
