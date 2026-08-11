"""``operation:run-mutation-sample`` — nightly B1 mutation sampling (§5-B1).

Uses Task 2 ``sample_mutants`` + ``.aa/cache/mutation/`` and an injectable
``MutationTool`` (default: subprocess mutmut). Writes batch evidence at
``execution/runs/nightly/mutation.json`` for ``aggregate-nightly``; never writes
``inspect/metrics.json`` and never attaches a floor.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import MetricCollectionGap, MetricShortboard, PR_METRICS_REL
from assurance_agent.artifacts.models.pr_metric_evidence import MutationEvidence, MutationSurvivor
from assurance_agent.artifacts.policy import load_policy
from assurance_agent.verification.mutation_cache import (
    MutationCacheKey,
    MutationCacheRecord,
    digest_module_contents,
    digest_test_tree,
    read_cache,
    write_cache,
)
from assurance_agent.verification.mutation_runner import (
    MutantResult,
    MutationTool,
    MutationToolError,
    SubprocessMutmutRunner,
)
from assurance_agent.verification.mutation_sampling import (
    SAMPLER_VERSION,
    SelectedMutant,
    sample_mutants,
)
from assurance_agent.workflow.execution.evidence import atomic_write_bytes
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace

MUTATION_BATCH_ID = "nightly"
MUTATION_EVIDENCE_REL = f"execution/runs/{MUTATION_BATCH_ID}/mutation.json"
DEFAULT_SURVIVOR_REPORT_CAP = 50
DEFAULT_MUTATION_SEED = 0


def resolve_mutation_budget_seconds(project_root: Path, *, params: Mapping[str, object]) -> int:
    """``params.mutation_budget_seconds`` when non-zero; else policy default (300)."""
    raw = params.get("mutation_budget_seconds", 0)
    try:
        param_budget = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        param_budget = 0
    if param_budget > 0:
        return param_budget
    policy = load_policy(project_root)
    return int(policy.evidence_sufficiency.mutation_budget_seconds)


def run_mutation_sample(
    *,
    project_root: Path,
    change_id: str,
    budget_seconds: int,
    seed: int,
    touched_lines: Mapping[str, frozenset[int] | set[int]],
    module_contents: Mapping[str, bytes],
    runner: MutationTool,
    survivor_report_cap: int = DEFAULT_SURVIVOR_REPORT_CAP,
    batch_id: str = MUTATION_BATCH_ID,
) -> MutationEvidence:
    """Sample + evaluate mutants under ``budget_seconds``; return pure evidence."""
    modules = tuple(sorted(touched_lines))
    if not modules:
        return MutationEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            killed=0,
            survived=0,
            equivalent=0,
            tested=0,
            selected=0,
            budget_seconds=budget_seconds,
            elapsed_seconds=0.0,
            budget_exceeded=False,
            cache_hit=False,
            seed=seed,
            survivor_report_cap=survivor_report_cap,
            source={"sampler_version": SAMPLER_VERSION, "detail": "no touched lines"},
        )

    key = MutationCacheKey(
        module_digest=digest_module_contents(module_contents),
        test_tree_digest=digest_test_tree(project_root),
        sampler_version=SAMPLER_VERSION,
    )
    cached = read_cache(project_root, key)
    cache_hit = cached is not None and cached.seed == seed
    if cache_hit:
        assert cached is not None
        selected = cached.selected
    else:
        try:
            candidates = runner.discover(modules)
        except MutationToolError as err:
            return _tool_gap_evidence(
                change_id=change_id,
                batch_id=batch_id,
                budget_seconds=budget_seconds,
                seed=seed,
                err=err,
            )
        selected = sample_mutants(candidates, seed=seed, touched_lines=touched_lines)
        write_cache(
            project_root,
            MutationCacheRecord(key=key, seed=seed, selected=selected),
        )

    if not selected:
        return MutationEvidence(
            schema_version="1",
            change_id=change_id,
            batch_id=batch_id,
            status="not_evaluated",
            value=None,
            killed=0,
            survived=0,
            equivalent=0,
            tested=0,
            selected=0,
            budget_seconds=budget_seconds,
            elapsed_seconds=0.0,
            budget_exceeded=False,
            cache_hit=cache_hit,
            seed=seed,
            survivor_report_cap=survivor_report_cap,
            source={"sampler_version": SAMPLER_VERSION},
        )

    killed = 0
    survived = 0
    equivalent = 0
    survivor_rows: list[MutationSurvivor] = []
    elapsed = 0.0
    budget_exceeded = False
    tested_mutants: list[SelectedMutant] = []

    for mutant in selected:
        remaining = budget_seconds - elapsed
        if remaining <= 0:
            budget_exceeded = True
            break
        try:
            started = time.perf_counter()
            result = runner.test(mutant, timeout_seconds=remaining)
            step = result.elapsed_seconds if result.elapsed_seconds > 0 else (time.perf_counter() - started)
        except MutationToolError as err:
            return _tool_gap_evidence(
                change_id=change_id,
                batch_id=batch_id,
                budget_seconds=budget_seconds,
                seed=seed,
                err=err,
                cache_hit=cache_hit,
                selected=len(selected),
            )
        elapsed += max(step, 0.0)
        tested_mutants.append(mutant)
        killed, survived, equivalent = _accumulate(result, killed, survived, equivalent, survivor_rows)
        if elapsed >= budget_seconds and len(tested_mutants) < len(selected):
            budget_exceeded = True
            break

    tested = killed + survived + equivalent
    denom = killed + survived
    value = None if denom == 0 else killed / denom
    capped, truncated = _cap_survivors(survivor_rows, survivor_report_cap)
    shortboards: tuple[MetricShortboard, ...] = ()
    if budget_exceeded:
        shortboards = (
            MetricShortboard(
                code="mutation_budget_exceeded",
                metric="mutation_score",
                detail=f"tested {tested}/{len(selected)} mutants within {budget_seconds}s",
            ),
        )

    return MutationEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="evaluated",
        value=value,
        killed=killed,
        survived=survived,
        equivalent=equivalent,
        tested=tested,
        selected=len(selected),
        budget_seconds=budget_seconds,
        elapsed_seconds=elapsed,
        budget_exceeded=budget_exceeded,
        cache_hit=cache_hit,
        seed=seed,
        survivors=capped,
        survivors_truncated=truncated,
        survivor_report_cap=survivor_report_cap,
        shortboards=shortboards,
        source={"sampler_version": SAMPLER_VERSION, "pr_metrics_rel": PR_METRICS_REL},
    )


def run_mutation_sample_operation(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
    *,
    runner: MutationTool | None = None,
) -> TaskResult:
    """Side-effecting op: resolve inputs, run sample, write nightly mutation.json."""
    del task
    change_id = context.change_id or workspace.change_dir.name
    params = context.params
    budget = resolve_mutation_budget_seconds(workspace.project_root, params=params)
    seed = _param_int(params, "mutation_seed", DEFAULT_MUTATION_SEED)
    cap = _param_int(params, "mutation_survivor_report_cap", DEFAULT_SURVIVOR_REPORT_CAP)
    touched = _resolve_touched_lines(workspace.change_dir, params)
    module_contents = _read_modules(workspace.project_root, touched)
    tool: MutationTool = runner if runner is not None else SubprocessMutmutRunner(workspace.project_root)

    evidence = run_mutation_sample(
        project_root=workspace.project_root,
        change_id=change_id,
        budget_seconds=budget,
        seed=seed,
        touched_lines=touched,
        module_contents=module_contents,
        runner=tool,
        survivor_report_cap=cap if cap > 0 else DEFAULT_SURVIVOR_REPORT_CAP,
    )
    out = workspace.change_dir / MUTATION_EVIDENCE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out, canonical_json_bytes(evidence))
    return TaskResult(
        status="succeeded",
        value={
            "path": MUTATION_EVIDENCE_REL,
            "status": evidence.status,
            "budget_exceeded": evidence.budget_exceeded,
            "cache_hit": evidence.cache_hit,
            "value": evidence.value,
        },
    )


def _accumulate(
    result: MutantResult,
    killed: int,
    survived: int,
    equivalent: int,
    survivor_rows: list[MutationSurvivor],
) -> tuple[int, int, int]:
    if result.outcome == "killed":
        return killed + 1, survived, equivalent
    if result.outcome == "equivalent":
        return killed, survived, equivalent + 1
    # survived and tool/runtime error both count as survived for score pressure
    # (errors are not kills and are not equivalence relief).
    survivor_rows.append(
        MutationSurvivor(
            locator=result.locator,
            module=result.mutant.module,
            line=result.mutant.line,
            operator=result.mutant.operator,
            mutant_id=result.mutant.mutant_id,
            equivalent=False,
        )
    )
    return killed, survived + 1, equivalent


def _cap_survivors(
    rows: Sequence[MutationSurvivor],
    cap: int,
) -> tuple[tuple[MutationSurvivor, ...], bool]:
    ordered = tuple(sorted(rows, key=lambda s: (s.module, s.line, s.operator, s.mutant_id)))
    if cap <= 0 or len(ordered) <= cap:
        return ordered, False
    return ordered[:cap], True


def _tool_gap_evidence(
    *,
    change_id: str,
    batch_id: str,
    budget_seconds: int,
    seed: int,
    err: MutationToolError,
    cache_hit: bool = False,
    selected: int = 0,
) -> MutationEvidence:
    code = "artifact_corrupt" if err.kind == "output_corrupt" else "collection_failed"
    return MutationEvidence(
        schema_version="1",
        change_id=change_id,
        batch_id=batch_id,
        status="collection_failed",
        value=None,
        killed=0,
        survived=0,
        equivalent=0,
        tested=0,
        selected=selected,
        budget_seconds=budget_seconds,
        elapsed_seconds=0.0,
        budget_exceeded=False,
        cache_hit=cache_hit,
        seed=seed,
        collection_gaps=(MetricCollectionGap(code=code, metric="mutation_score", detail=err.detail),),
        source={"sampler_version": SAMPLER_VERSION},
    )


def _param_int(params: Mapping[str, object], key: str, default: int) -> int:
    raw = params.get(key, default)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _resolve_touched_lines(
    change_dir: Path,
    params: Mapping[str, object],
) -> dict[str, frozenset[int]]:
    raw = params.get("mutation_touched_lines")
    if isinstance(raw, Mapping):
        out: dict[str, frozenset[int]] = {}
        for path, lines in raw.items():
            if isinstance(lines, (list, tuple, set, frozenset)):
                out[str(path)] = frozenset(int(line) for line in lines)
        if out:
            return out
    # Prefer the latest batch raw/changed-lines.json when present.
    runs = change_dir / "execution" / "runs"
    if runs.is_dir():
        for batch_dir in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True):
            path = batch_dir / "raw" / "changed-lines.json"
            if not path.is_file():
                continue
            try:
                import json

                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            out = {}
            for module, lines in payload.items():
                if isinstance(lines, list):
                    out[str(module)] = frozenset(int(line) for line in lines if int(line) > 0)
            if out:
                return out
    return {}


def _read_modules(project_root: Path, touched: Mapping[str, frozenset[int]]) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for rel in sorted(touched):
        path = project_root / rel
        if path.is_file():
            try:
                contents[rel] = path.read_bytes()
            except OSError:
                contents[rel] = b""
        else:
            contents[rel] = b""
    return contents


__all__ = [
    "DEFAULT_SURVIVOR_REPORT_CAP",
    "MUTATION_BATCH_ID",
    "MUTATION_EVIDENCE_REL",
    "resolve_mutation_budget_seconds",
    "run_mutation_sample",
    "run_mutation_sample_operation",
]
