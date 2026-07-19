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
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.driver.runtime_factory import (
    RuntimeBundle,
    build_graph_runtime,
    runtime_context_for,
)
from assurance_agent.workflow.graph.agent_api import AgentInvoker, AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import parse_import_manifest
from assurance_agent.workflow.graph.runtime import GraphRuntimeError

RuntimeFactory = Callable[..., RuntimeBundle]  # tests may return structural stand-ins

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


def _exit_for_status(status: str) -> int:
    if status == "completed":
        return EXIT_COMPLETED
    if status == "stopped":
        return EXIT_STOPPED
    if status == "interrupted":
        return EXIT_HUMAN_REVIEW
    return EXIT_ERROR


def _as_invoker(adapter: AgentInvoker | object) -> AgentInvoker:
    """Accept graph ``AgentInvoker`` or legacy ``run_phase``-only adapters."""
    if callable(getattr(adapter, "invoke", None)):
        return adapter  # type: ignore[return-value]

    run_phase = getattr(adapter, "run_phase", None)
    if not callable(run_phase):
        raise AaError("eval adapter must implement invoke() or run_phase()")

    class _Bridge:
        def invoke(self, request: AgentRequest) -> AgentResult:
            from assurance_agent.workflow.driver.adapter import PhaseRequest

            phase = PhaseRequest(
                change_id=request.change_id,
                phase_id=request.node_id,
                skill=request.target.removeprefix("skill:") if request.target.startswith("skill:") else None,
                agent=None,
                prompt=request.prompt,
            )
            result = run_phase(phase)
            ok = bool(getattr(result, "ok", False))
            error = getattr(result, "error", None)
            return AgentResult(ok=ok, error=str(error) if error else None)

    return _Bridge()


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
    adapter: AgentInvoker | object,
    entrypoint: str = "full",
    runtime_factory: Callable[..., object] | None = None,
    expected_outputs: list[str] | None = None,
    fixtures_root: Path | None = None,
    executor_type: str = "workflow-run",
    run_mode: str | None = None,
    test_types: str | None = None,
    run_tests: bool | None = None,
) -> ExecutionResult:
    if executor_type in _IN_PROCESS_TYPES:
        return execute_in_process(sample, attempt_dir, suite=suite)

    change_id = sample.input.get("change_id")
    if not change_id:
        raise AaError(f"sample {sample.id} missing input.change_id")
    assert_change_id_safe(change_id)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    resolved_run_mode = _expand_sample_input_vars(run_mode, sample) if run_mode else "full"
    resolved_test_types = (
        [t.strip() for t in str(test_types).split(",") if t.strip()]
        if test_types
        else ["api", "e2e"]
    )
    resolved_run_tests = True if run_tests is None else bool(run_tests)

    fixture_tier = sample.input.get("fixture_tier")
    import_manifest_path: Path | None = None
    if fixture_tier:
        if fixtures_root is None:
            raise AaError(f"sample {sample.id} has fixture_tier but fixtures_root was not provided")
        fixture_id = sample.input.get("fixture_id")
        if not fixture_id:
            raise AaError(f"sample {sample.id} has fixture_tier but no explicit fixture_id")
        from assurance_agent.eval.fixtures import seed_change

        seeded = seed_change(
            sut_sandbox=sut_dir,
            change_id=str(change_id),
            tier_name=str(fixture_tier),
            fixtures_root=fixtures_root,
            fixture_id=str(fixture_id),
            entrypoint=entrypoint,
        )
        import_manifest_path = seeded.import_manifest_path

    # Write-scan (P0 forbidden_write_executed_count): snapshot the SUT worktree
    # after seeding, before the runtime — aligned with the TS workflow-run executor.
    policy = resolve_write_policy(resolved_run_mode, test_types)
    try:
        before_porcelain = capture_write_scan_before(attempt_dir, sut_dir, policy)
    except AaError as exc:
        (attempt_dir / "stdout.log").write_text("", encoding="utf-8")
        (attempt_dir / "stderr.log").write_text(str(exc) + "\n", encoding="utf-8")
        execution = {
            "executor": f"workflow-run:{suite}",
            "change_id": change_id,
            "entrypoint": entrypoint,
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

    params: dict[str, object] = {
        "run_mode": resolved_run_mode,
        "test_types": resolved_test_types,
        "run_tests": resolved_run_tests,
    }
    invoker = _as_invoker(adapter)
    factory = runtime_factory or build_graph_runtime
    reason = "ok"
    exit_code = EXIT_COMPLETED
    try:
        bundle = factory(project_root=sut_dir, change_id=str(change_id), adapter=invoker)
        context = runtime_context_for(sut_dir, str(change_id), params)
        runtime = bundle.runtime  # type: ignore[attr-defined]
        compiled = bundle.compiled  # type: ignore[attr-defined]
        if import_manifest_path is not None and import_manifest_path.is_file():
            manifest = parse_import_manifest(import_manifest_path.read_text(encoding="utf-8"))
            imported = runtime.import_checkpoint(compiled, manifest, context)
            status = runtime.status(imported.invocation_id)
            exit_code = _exit_for_status(status.status)
            reason = status.terminal_reason or status.status
        else:
            result = runtime.run(compiled, entrypoint, context)
            exit_code = result.exit_code
            reason = result.reason
    except (GraphRuntimeError, AaError, ValueError, OSError) as exc:
        exit_code = EXIT_ERROR
        reason = str(exc)

    post_error: str | None = None
    try:
        capture_write_scan_after(attempt_dir, sut_dir, policy, before_porcelain)
    except AaError as exc:
        post_error = str(exc)

    change_dir = sut_dir / "qa" / "changes" / change_id
    raw_output = attempt_dir / "raw-output"
    _copy_change_artifacts(change_dir, raw_output)
    _copy_sut_tests(sut_dir, raw_output)

    status = "ok" if exit_code == 0 else "error"
    (attempt_dir / "stdout.log").write_text(reason + "\n", encoding="utf-8")
    (attempt_dir / "stderr.log").write_text("", encoding="utf-8")
    execution = {
        "executor": f"workflow-run:{suite}",
        "change_id": change_id,
        "entrypoint": entrypoint,
        "exit_code": exit_code,
        "reason": reason,
        "fixture_tier": fixture_tier,
        "executor_type": executor_type,
        "run_mode": resolved_run_mode,
        "import_manifest": str(import_manifest_path) if import_manifest_path else None,
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
        exit_code=exit_code,
        error=None if status == "ok" else (post_error or reason or f"missing outputs: {missing}"),
        extra={"missing_outputs": missing},
    )
