from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.eval.dataset_loader import load_for_run
from assurance_agent.eval.executor import LoopRunner, execute_attempt
from assurance_agent.eval.gate import compute_gate_result, write_gate_result
from assurance_agent.eval.judge import run_judge
from assurance_agent.eval.metrics import aggregate_scores, write_metrics
from assurance_agent.eval.paths import attempt_dir as attempt_dir_for
from assurance_agent.eval.paths import datasets_dir, run_dir as run_dir_for
from assurance_agent.eval.plan import load_suite, load_suite_file, read_plan
from assurance_agent.eval.report import write_run_report
from assurance_agent.eval.scorers import get_scorer
from assurance_agent.eval.types import (
    EvalGateResult,
    JudgeConfig,
    RunManifest,
    SampleScore,
)
from assurance_agent.workflow.driver.adapter import Adapter
from assurance_agent.workflow.driver.loop import CliPhaseExecutor, run_workflow_loop


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id(suite: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"eval-{stamp}-{secrets.token_hex(4)}"


def _copy_attempt_workspace(source: Path, attempt: Path) -> Path:
    """Create an isolated SUT snapshot; never recurse into eval/out or VCS/env data."""
    target = attempt / "sut"

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).resolve().relative_to(source.resolve())
        ignored = {name for name in names if name in {".git", ".venv", "__pycache__"}}
        ignored.update(name for name in names if (Path(directory) / name).is_symlink())
        if rel == Path("eval"):
            ignored.add("out")
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    return target


def run_suite(
    *,
    suite_file: Path,
    project_root: Path,
    sut_dir: Path,
    sample_id: str | None = None,
    repeat: int = 1,
    calibrate: bool = False,
    run_id: str | None = None,
    adapter_factory: Callable[..., Adapter] | None = None,
    status_provider_factory: Callable[..., Callable[[], object]] | None = None,
    cli_executor_factory: Callable[..., CliPhaseExecutor] | None = None,
    loop_runner: LoopRunner = run_workflow_loop,
) -> tuple[str, EvalGateResult]:
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    suite = load_suite_file(suite_file)
    dataset = datasets_dir(project_root, suite.name) if suite.dataset_dir is None else Path(suite.dataset_dir)
    samples = load_for_run(dataset, sample_id=sample_id)
    run_id = run_id or _new_run_id(suite.name)
    # Artifacts land under the SUT so retro read_eval_trend(sut) can see them.
    run_dir = run_dir_for(sut_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = RunManifest(
        run_id=run_id,
        suite=suite.name,
        scorer=suite.scorer,
        selected_sample_ids=[f"{s.id}#attempt-{i}" for s in samples for i in range(repeat)],
        total_samples=len(samples) * repeat,
        executed_samples=0,
        target_model=str(suite.executor.get("model", "unknown")),
        started_at=_now(),
    )

    scorer = get_scorer(suite.scorer)
    scores: list[SampleScore] = []
    scope = str(suite.executor.get("scope", "full"))
    for sample in samples:
        for attempt_index in range(repeat):
            attempt = attempt_dir_for(sut_dir, run_id, sample.id, attempt_index)
            attempt.mkdir(parents=True, exist_ok=True)
            attempt_sut = _copy_attempt_workspace(sut_dir, attempt)
            factory_args = {
                "sample": sample,
                "sut_dir": attempt_sut,
                "attempt": attempt_index,
            }
            adapter = adapter_factory(**factory_args) if adapter_factory else None
            status_provider = status_provider_factory(**factory_args) if status_provider_factory else None
            cli_executor = cli_executor_factory(**factory_args) if cli_executor_factory else None
            if adapter is None:
                raise ValueError("adapter_factory required (real adapters wired by CLI)")
            result = execute_attempt(
                sample,
                attempt,
                suite=suite.name,
                sut_dir=attempt_sut,
                adapter=adapter,
                scope=scope,
                loop_runner=loop_runner,
                status_provider=status_provider,
                cli_executor=cli_executor,
                expected_outputs=suite.executor.get("expected_outputs"),
            )
            manifest.executed_samples += 1
            score_key = f"{sample.id}#attempt-{attempt_index}"
            if result.status == "error":
                scores.append(SampleScore(sample_id=score_key, status="error", error=result.error))
                continue
            score = scorer(sample, attempt).model_copy(update={"sample_id": score_key})
            if calibrate:
                judge_model = os.environ.get("AA_JUDGE_MODEL")
                if not judge_model:
                    raise ValueError("--calibrate requires AA_JUDGE_MODEL")
                judged = run_judge(
                    sample,
                    attempt,
                    JudgeConfig(model=judge_model),
                    target_model=manifest.target_model,
                )
                (attempt / "judge.json").write_text(
                    json.dumps(judged.model_dump(mode="json"), indent=2), encoding="utf-8"
                )
                score.notes.update(
                    {
                        "judge_label": judged.label,
                        "judge_confidence": judged.confidence,
                        "judge_needs_human_review": float(judged.needs_human_review),
                    }
                )
            scores.append(score)

    manifest.completed_at = _now()
    metrics = aggregate_scores(run_id, suite.name, scores)
    gate = compute_gate_result(suite, manifest, metrics)

    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    write_metrics(run_dir, metrics)
    write_gate_result(run_dir, gate)
    write_run_report(run_dir, manifest, metrics, gate)
    return run_id, gate


def run_plan(
    *,
    plan_path: Path,
    project_root: Path,
    sut_dir: Path,
    adapter_factory: Callable[..., Adapter] | None = None,
    status_provider_factory: Callable[..., Callable[[], object]] | None = None,
) -> tuple[str, list[EvalGateResult]]:
    plan = read_plan(plan_path)
    batch_id = _new_run_id("batch")
    results: list[EvalGateResult] = []
    for suite_name in plan.get("suites", []):
        _, suite_file = load_suite(project_root, suite_name)
        _, gate = run_suite(
            suite_file=suite_file,
            project_root=project_root,
            sut_dir=sut_dir,
            adapter_factory=adapter_factory,
            status_provider_factory=status_provider_factory,
        )
        results.append(gate)
    return batch_id, results
