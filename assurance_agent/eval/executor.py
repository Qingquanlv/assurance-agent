from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from assurance_agent.eval.types import DatasetSample, ExecutionResult
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.driver.adapter import Adapter
from assurance_agent.workflow.driver.loop import CliPhaseExecutor, LoopResult, run_workflow_loop

LoopRunner = Callable[..., LoopResult]


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
) -> ExecutionResult:
    change_id = sample.input.get("change_id")
    if not change_id:
        raise AaError(f"sample {sample.id} missing input.change_id")
    assert_change_id_safe(change_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    fixture_tier = sample.input.get("fixture_tier")
    if fixture_tier:
        if fixtures_root is None:
            raise AaError(f"sample {sample.id} has fixture_tier but fixtures_root was not provided")
        from assurance_agent.eval.fixtures import seed_change

        seed_change(
            sut_sandbox=sut_dir,
            change_id=str(change_id),
            tier_name=str(fixture_tier),
            fixtures_root=fixtures_root,
            sample_id=sample.input.get("sample_id") or sample.input.get("change_id"),
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

    change_dir = sut_dir / "qa" / "changes" / change_id
    _copy_change_artifacts(change_dir, attempt_dir / "raw-output")

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
    }
    (attempt_dir / "execution.json").write_text(json.dumps(execution, indent=2), encoding="utf-8")

    missing = [rel for rel in (expected_outputs or []) if not (attempt_dir / "raw-output" / rel).exists()]
    if missing:
        status = "error"

    return ExecutionResult(
        sample_id=sample.id,
        attempt=0,
        executor="workflow-run",
        status=status,
        exit_code=loop.exit_code,
        error=None if status == "ok" else loop.reason,
        extra={"missing_outputs": missing},
    )
