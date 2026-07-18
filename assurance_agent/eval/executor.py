from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.eval.types import DatasetSample, ExecutionResult
from assurance_agent.eval.write_scan import (
    capture_write_scan_after,
    capture_write_scan_before,
    resolve_write_policy,
)
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.driver.adapter import Adapter
from assurance_agent.workflow.driver.loop import CliPhaseExecutor, LoopResult, run_workflow_loop

LoopRunner = Callable[..., LoopResult]

_IN_PROCESS_TYPES = frozenset({"in_process", "score-only"})

_SAMPLE_INPUT_VAR = re.compile(r"\{\{\s*sample\.input\.([A-Za-z0-9_]+)\s*\}\}")


def _expand_sample_input_vars(value: str, sample: DatasetSample) -> str:
    """Expand ``{{sample.input.<key>}}`` tokens (suite executor run_mode templating)."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in sample.input:
            raise AaError(f"sample {sample.id} missing input.{key} referenced by executor config")
        return str(sample.input[key])

    return _SAMPLE_INPUT_VAR.sub(repl, value)


def _copy_change_artifacts(change_dir: Path, raw_output: Path) -> None:
    raw_output.mkdir(parents=True, exist_ok=True)
    if not change_dir.is_dir():
        return
    for entry in change_dir.iterdir():
        target = raw_output / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def _copy_sut_tests(sut_dir: Path, raw_output: Path) -> None:
    """Mirror SUT ``tests/`` into raw-output so codegen scorers can see seeded code.

    Fixture seeding places generated tests at the SUT root (not under
    ``qa/changes/<id>/``). Without this copy, golden-sample replay would leave
    ``raw-output/tests`` empty and ``schema_valid_rate`` would hard-fail.
    """
    src = sut_dir / "tests"
    if not src.is_dir():
        return
    dest = raw_output / "tests"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def execute_in_process(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    suite: str,
) -> ExecutionResult:
    """Score-only attempt: no workflow loop, no change_id required.

    Writes the evidence trio (stdout/stderr/execution.json) plus a tiny
    ``raw-output/`` marker so evidence_integrity and _test scorers pass.
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)
    raw = attempt_dir / "raw-output"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / ".in-process").write_text(f"suite={suite}\nsample={sample.id}\n", encoding="utf-8")
    (attempt_dir / "stdout.log").write_text("in_process\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
    execution = {
        "executor": f"in_process:{suite}",
        "sample_id": sample.id,
        "exit_code": 0,
        "reason": "in_process",
    }
    (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")
    return ExecutionResult(
        sample_id=sample.id,
        attempt=0,
        executor="in_process",
        status="ok",
        exit_code=0,
        error=None,
        extra={},
    )


def execute_attempt(
    sample: DatasetSample,
    attempt_dir: Path,
    *,
    suite: str,
    sut_dir: Path,
    adapter: Adapter,
    scope: str = "full",
    loop_runner: LoopRunner = run_workflow_loop,
    status_provider: Callable[[], object] | None = None,
    cli_executor: CliPhaseExecutor | None = None,
    expected_outputs: list[str] | None = None,
    fixtures_root: Path | None = None,
    executor_type: str = "workflow-run",
    run_mode: str | None = None,
    test_types: str | None = None,
) -> ExecutionResult:
    if executor_type in _IN_PROCESS_TYPES:
        return execute_in_process(sample, attempt_dir, suite=suite)

    change_id = sample.input.get("change_id")
    if not change_id:
        raise AaError(f"sample {sample.id} missing input.change_id")
    assert_change_id_safe(change_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    fixture_tier = sample.input.get("fixture_tier")
    if fixture_tier:
        if fixtures_root is None:
            raise AaError(f"sample {sample.id} has fixture_tier but fixtures_root was not provided")
        fixture_id = sample.input.get("fixture_id")
        if not fixture_id:
            raise AaError(f"sample {sample.id} has fixture_tier but no explicit fixture_id")
        from assurance_agent.eval.fixtures import seed_change

        seed_change(
            sut_sandbox=sut_dir,
            change_id=str(change_id),
            tier_name=str(fixture_tier),
            fixtures_root=fixtures_root,
            fixture_id=str(fixture_id),
        )

    # Write-scan (P0 forbidden_write_executed_count): snapshot the SUT worktree
    # after seeding, before the loop — aligned with the TS workflow-run executor.
    # A non-git SUT fails closed: the attempt is recorded as an infrastructure
    # error and the loop never runs (same as the old executor).
    resolved_run_mode = _expand_sample_input_vars(run_mode, sample) if run_mode else "full"
    policy = resolve_write_policy(resolved_run_mode, test_types)
    try:
        before_porcelain = capture_write_scan_before(attempt_dir, sut_dir, policy)
    except AaError as exc:
        (attempt_dir / "stdout.log").write_text("", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(str(exc) + "\n", encoding="utf-8")
        execution = {
            "executor": f"workflow-run:{suite}",
            "change_id": change_id,
            "scope": scope,
            "exit_code": 1,
            "reason": str(exc),
            "fixture_tier": fixture_tier,
            "executor_type": executor_type,
            "run_mode": resolved_run_mode,
            "infrastructure_error": True,
        }
        (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")
        return ExecutionResult(
            sample_id=sample.id,
            attempt=0,
            executor="workflow-run",
            status="error",
            exit_code=1,
            error=str(exc),
            extra={"infrastructure_error": True},
        )

    loop = loop_runner(
        project_root=sut_dir,
        change_id=change_id,
        scope=scope,
        adapter=adapter,
        status_provider=status_provider,
        cli_executor=cli_executor,
        skip_lock=True,
    )

    post_error: str | None = None
    try:
        capture_write_scan_after(attempt_dir, sut_dir, policy, before_porcelain)
    except AaError as exc:
        post_error = str(exc)

    change_dir = sut_dir / "qa" / "changes" / change_id
    raw_output = attempt_dir / "raw-output"
    _copy_change_artifacts(change_dir, raw_output)
    _copy_sut_tests(sut_dir, raw_output)

    status = "ok" if loop.exit_code == 0 else "error"
    (attempt_dir / "stdout.log").write_text(loop.reason + "\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
    execution = {
        "executor": f"workflow-run:{suite}",
        "change_id": change_id,
        "scope": scope,
        "exit_code": loop.exit_code,
        "reason": loop.reason,
        "fixture_tier": fixture_tier,
        "executor_type": executor_type,
        "run_mode": resolved_run_mode,
    }
    if post_error:
        execution["infrastructure_error"] = True
        execution["error"] = post_error
    (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")

    missing = [rel for rel in (expected_outputs or []) if not (raw_output / rel).exists()]
    if missing or post_error:
        status = "error"

    return ExecutionResult(
        sample_id=sample.id,
        attempt=0,
        executor="workflow-run",
        status=status,
        exit_code=loop.exit_code,
        error=None if status == "ok" else (post_error or loop.reason or f"missing outputs: {missing}"),
        extra={"missing_outputs": missing},
    )
