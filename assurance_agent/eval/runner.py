from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from assurance_agent.eval.dataset_loader import load_for_run
from assurance_agent.eval.executor import execute_attempt
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
    regression_policy_sha256,
)
from assurance_agent.exceptions import AaError


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id(suite: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"eval-{stamp}-{secrets.token_hex(4)}"


def _copy_attempt_workspace(source: Path, attempt: Path) -> Path:
    """Create an isolated SUT snapshot; never recurse into eval/out or env data.

    ``.git`` is intentionally kept: the eval write-scan diffs before/after
    ``git status --porcelain`` snapshots of this workspace.
    """
    target = attempt / "sut"

    def ignore(directory: str, names: list[str]) -> set[str]:
        rel = Path(directory).resolve().relative_to(source.resolve())
        ignored = {
            name
            for name in names
            if name in {".venv", "__pycache__", "node_modules", "dist", ".pytest_cache", ".ruff_cache"}
            or name.startswith("db.sqlite3")
        }
        ignored.update(name for name in names if (Path(directory) / name).is_symlink())
        if rel == Path("eval"):
            ignored.add("out")
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    return target


def _inspect_memory_overlay(extra_memory_dir: Path) -> tuple[list[Path], str]:
    src = extra_memory_dir / ".aa" / "memory"
    if extra_memory_dir.is_symlink() or (extra_memory_dir / ".aa").is_symlink() or src.is_symlink():
        raise AaError(f"memory overlay contains symlinked directory: {src}")
    if not src.is_dir():
        raise AaError(f"memory overlay directory not found: {src}")
    entries: list[Path] = []
    hashes: list[str] = []
    for entry in sorted(src.iterdir(), key=lambda path: path.name):
        if entry.is_symlink():
            raise AaError(f"memory overlay contains symlink: {entry}")
        if not entry.is_file() or entry.suffix != ".md":
            raise AaError(f"memory overlay only accepts regular .md files: {entry}")
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        hashes.append(f".aa/memory/{entry.name}:{digest}")
        entries.append(entry)
    return entries, hashlib.sha256("\n".join(hashes).encode()).hexdigest()


def _overlay_memory(entries: list[Path], attempt_sut: Path) -> None:
    """Merge a promotion-candidate memory overlay into the attempt sandbox.

    ``extra_memory_dir`` mirrors the SUT layout (contains ``.aa/memory/*.md``).
    Files are copied into the isolated sandbox so the agent under eval actually
    loads the candidate memory — this is what makes the promotion gate a real
    validation of the memory's effect rather than a no-op replay.
    """
    dest = attempt_sut / ".aa" / "memory"
    dest.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        shutil.copy2(entry, dest / entry.name)


def run_suite(
    *,
    suite_file: Path,
    project_root: Path,
    sut_dir: Path,
    sample_id: str | None = None,
    repeat: int = 1,
    calibrate: bool = False,
    run_id: str | None = None,
    adapter_factory: Callable[..., object] | None = None,
    runtime_factory: Callable[..., object] | None = None,
    fixtures_root: Path | None = None,
    extra_memory_dir: Path | None = None,
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

    overlay_entries: list[Path] = []
    overlay_sha256: str | None = None
    if extra_memory_dir is not None:
        overlay_entries, overlay_sha256 = _inspect_memory_overlay(extra_memory_dir)

    manifest = RunManifest(
        run_id=run_id,
        suite=suite.name,
        scorer=suite.scorer,
        selected_sample_ids=[f"{s.id}#attempt-{i}" for s in samples for i in range(repeat)],
        total_samples=len(samples) * repeat,
        executed_samples=0,
        target_model=str(suite.executor.get("model", "unknown")),
        suite_version=suite.version,
        repeat=repeat,
        regression_policy_sha256=regression_policy_sha256(suite.regression),
        memory_overlay_sha256=overlay_sha256,
        started_at=_now(),
    )

    resolved_fixtures = fixtures_root
    if resolved_fixtures is None:
        candidate = sut_dir / "eval-fixtures"
        if candidate.is_dir():
            resolved_fixtures = candidate

    scorer = get_scorer(suite.scorer)
    scores: list[SampleScore] = []
    if "entrypoint" not in suite.executor and "scope" in suite.executor:
        raise AaError("suite executor.scope is removed; use executor.entrypoint")
    entrypoint = str(suite.executor.get("entrypoint", "full"))
    executor_type = str(suite.executor.get("type", "workflow-run"))
    run_mode = suite.executor.get("run_mode")
    test_types = suite.executor.get("test_type") or suite.executor.get("test_types")
    run_tests_raw = suite.executor.get("run_tests")
    run_tests = None if run_tests_raw is None else bool(run_tests_raw)
    in_process = executor_type in {"in_process", "score-only"}
    for sample in samples:
        for attempt_index in range(repeat):
            attempt = attempt_dir_for(sut_dir, run_id, sample.id, attempt_index)
            attempt.mkdir(parents=True, exist_ok=True)
            attempt_sut = _copy_attempt_workspace(sut_dir, attempt)
            if extra_memory_dir is not None:
                _overlay_memory(overlay_entries, attempt_sut)
            factory_args = {
                "sample": sample,
                "sut_dir": attempt_sut,
                "attempt": attempt_index,
            }
            adapter = adapter_factory(**factory_args) if adapter_factory else None
            if not in_process and adapter is None:
                raise ValueError("adapter_factory required (real adapters wired by CLI)")
            # in_process suites never call the adapter; pass a noop when unset.
            if adapter is None:
                from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult

                class _NoopAdapter:
                    def invoke(self, request: AgentRequest) -> AgentResult:
                        return AgentResult(ok=True)

                adapter = _NoopAdapter()  # type: ignore[assignment]
            result = execute_attempt(
                sample,
                attempt,
                suite=suite.name,
                sut_dir=attempt_sut,
                adapter=adapter,
                entrypoint=entrypoint,
                runtime_factory=runtime_factory,
                expected_outputs=suite.executor.get("expected_outputs"),
                fixtures_root=resolved_fixtures,
                executor_type=executor_type,
                run_mode=str(run_mode) if run_mode is not None else None,
                test_types=str(test_types) if test_types is not None else None,
                run_tests=run_tests,
            )
            manifest.executed_samples += 1
            score_key = f"{sample.id}#attempt-{attempt_index}"
            if result.status == "error":
                scores.append(SampleScore(sample_id=score_key, status="error", error=result.error))
                continue
            # Judge runs before scoring (aligned with TS runner.ts:139): the scorer
            # reads judge-result.json from the attempt dir to compute P/R/F1.
            if suite.judge is not None:
                try:
                    judged = run_judge(
                        sample,
                        attempt,
                        suite.judge,
                        target_model=manifest.target_model,
                        project_root=project_root,
                    )
                    (attempt / "judge-result.json").write_text(
                        json.dumps(judged.model_dump(mode="json"), indent=2), encoding="utf-8"
                    )
                except Exception as err:
                    # Fail-closed (TS runner catch-all): judge errors become error scores.
                    scores.append(SampleScore(sample_id=score_key, status="error", error=str(err)))
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
                    project_root=project_root,
                )
                (attempt / "judge-result.json").write_text(
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
    adapter_factory: Callable[..., object] | None = None,
    runtime_factory: Callable[..., object] | None = None,
    extra_memory_dir: Path | None = None,
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
            runtime_factory=runtime_factory,
            extra_memory_dir=extra_memory_dir,
        )
        results.append(gate)
    return batch_id, results
