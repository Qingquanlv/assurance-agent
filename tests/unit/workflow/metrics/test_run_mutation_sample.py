"""M2 Task 3: run-mutation-sample uses sampling/cache + injectable mutmut runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from assurance_agent.artifacts.models.metrics import PR_METRICS_REL
from assurance_agent.artifacts.models.pr_metric_evidence import MutationEvidence
from assurance_agent.verification.mutation_cache import (
    MUTATION_CACHE_DIR_REL,
    MutationCacheKey,
    cache_path,
    digest_module_contents,
    digest_test_tree,
    read_cache,
)
from assurance_agent.verification.mutation_runner import MutantOutcome, MutantResult, MutationToolError
from assurance_agent.verification.mutation_sampling import (
    SAMPLER_VERSION,
    MutantCandidate,
    SelectedMutant,
)
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.mutation import (
    DEFAULT_SURVIVOR_REPORT_CAP,
    MUTATION_BATCH_ID,
    MUTATION_EVIDENCE_REL,
    resolve_mutation_budget_seconds,
    run_mutation_sample,
    run_mutation_sample_operation,
)
from assurance_agent.workflow.metrics.nightly import MUTATION_EVIDENCE_REL as NIGHTLY_MUTATION_REL
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-MUT-001"


class FakeRunner:
    """Injectable mutmut stand-in — never shells out."""

    def __init__(
        self,
        candidates: Sequence[MutantCandidate],
        outcomes: dict[str, MutantOutcome] | None = None,
        *,
        discover_error: MutationToolError | None = None,
        test_error: MutationToolError | None = None,
        seconds_per_test: float = 0.0,
    ) -> None:
        self.candidates = tuple(candidates)
        self.outcomes = outcomes or {}
        self.discover_error = discover_error
        self.test_error = test_error
        self.seconds_per_test = seconds_per_test
        self.tested: list[SelectedMutant] = []

    def discover(self, modules: Sequence[str]) -> Sequence[MutantCandidate]:
        if self.discover_error is not None:
            raise self.discover_error
        wanted = set(modules)
        return tuple(c for c in self.candidates if c.module in wanted)

    def test(self, mutant: SelectedMutant, *, timeout_seconds: float) -> MutantResult:
        del timeout_seconds
        if self.test_error is not None:
            raise self.test_error
        self.tested.append(mutant)
        outcome = self.outcomes.get(mutant.mutant_id, "killed")
        return MutantResult(mutant=mutant, outcome=outcome, elapsed_seconds=self.seconds_per_test)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write_aa_config(root)
    (root / "app").mkdir(parents=True)
    (root / "app" / "svc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_svc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    change = root / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    return root


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t-mut",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        base_tree_id="tree-0",
    )


def _context(project_root: Path, **params: object) -> RuntimeContext:
    return RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=project_root / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params=params,
    )


def _task() -> ExecutableTask:
    return ExecutableTask.model_construct(
        task_id="t-mut",
        node_id="run-mutation-sample",
        graph_id="metrics-nightly-workflow",
        target="operation:run-mutation-sample",
        input={"with": {}},
    )


def _candidates() -> tuple[MutantCandidate, ...]:
    return (
        MutantCandidate(module="app/svc.py", line=2, operator="AOR", mutant_id="m1"),
        MutantCandidate(module="app/svc.py", line=2, operator="ROR", mutant_id="m2"),
        MutantCandidate(module="app/svc.py", line=1, operator="SDL", mutant_id="m3"),
    )


def test_evidence_path_matches_nightly_contract_surface() -> None:
    assert MUTATION_EVIDENCE_REL == NIGHTLY_MUTATION_REL
    assert MUTATION_EVIDENCE_REL == "execution/runs/nightly/mutation.json"
    assert MUTATION_BATCH_ID == "nightly"
    assert DEFAULT_SURVIVOR_REPORT_CAP > 0


def test_budget_prefers_nonzero_param_else_policy(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert resolve_mutation_budget_seconds(root, params={"mutation_budget_seconds": 12}) == 12
    assert resolve_mutation_budget_seconds(root, params={"mutation_budget_seconds": 0}) == 300
    assert resolve_mutation_budget_seconds(root, params={}) == 300


def test_run_writes_mutation_evidence_with_score_and_survivors(tmp_path: Path) -> None:
    root = _project(tmp_path)
    runner = FakeRunner(
        _candidates(),
        outcomes={"m1": "survived", "m2": "killed", "m3": "killed"},
    )
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=7,
        touched_lines={"app/svc.py": frozenset({1, 2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=runner,
    )
    assert isinstance(evidence, MutationEvidence)
    assert evidence.status == "evaluated"
    assert evidence.batch_id == "nightly"
    assert evidence.tested == 2  # ≤1 per touched line
    assert evidence.survived == 1
    assert evidence.killed == 1
    assert evidence.value == pytest.approx(0.5)
    assert evidence.collection_gaps == ()
    assert len(evidence.survivors) == 1
    assert evidence.survivors[0].equivalent is False
    assert "app/svc.py:2:" in evidence.survivors[0].locator


def test_operation_writes_nightly_mutation_json_and_never_pr_metrics(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workspace = _workspace(root)
    pr = workspace.change_dir / PR_METRICS_REL
    pr.parent.mkdir(parents=True, exist_ok=True)
    planted = b'{"planted":true}\n'
    pr.write_bytes(planted)
    runner = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "survived"})
    result = run_mutation_sample_operation(
        _task(),
        workspace,
        _context(
            root,
            mutation_budget_seconds=60,
            mutation_seed=7,
            mutation_touched_lines={"app/svc.py": [1, 2]},
        ),
        runner=runner,
    )
    assert result.status == "succeeded"
    assert pr.read_bytes() == planted
    path = workspace.change_dir / MUTATION_EVIDENCE_REL
    assert path.is_file()
    payload = MutationEvidence.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert payload.change_id == CHANGE_ID
    assert payload.status == "evaluated"
    assert "stub" not in path.read_text(encoding="utf-8")


def test_budget_exceeded_shortboard_not_task_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)

    class SlowRunner(FakeRunner):
        def test(self, mutant: SelectedMutant, *, timeout_seconds: float) -> MutantResult:
            self.tested.append(mutant)
            return MutantResult(mutant=mutant, outcome="killed", elapsed_seconds=10.0)

    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=5,
        seed=1,
        touched_lines={"app/svc.py": frozenset({1, 2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=SlowRunner(_candidates()),
    )
    assert evidence.status == "evaluated"
    assert evidence.budget_exceeded is True
    assert evidence.tested == 1
    assert evidence.selected == 2
    assert any(board.code == "mutation_budget_exceeded" for board in evidence.shortboards)
    assert evidence.collection_gaps == ()


def test_tool_start_failure_is_typed_gap_and_operation_succeeds(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workspace = _workspace(root)
    runner = FakeRunner(
        (),
        discover_error=MutationToolError(kind="start_failed", detail="mutmut not found"),
    )
    result = run_mutation_sample_operation(
        _task(),
        workspace,
        _context(root, mutation_touched_lines={"app/svc.py": [2]}),
        runner=runner,
    )
    assert result.status == "succeeded"
    evidence = MutationEvidence.model_validate(
        json.loads((workspace.change_dir / MUTATION_EVIDENCE_REL).read_text(encoding="utf-8"))
    )
    assert evidence.status == "collection_failed"
    assert evidence.collection_gaps
    assert evidence.collection_gaps[0].code == "collection_failed"
    assert evidence.collection_gaps[0].metric == "mutation_score"


def test_corrupt_tool_output_is_artifact_corrupt_gap(tmp_path: Path) -> None:
    root = _project(tmp_path)
    runner = FakeRunner(
        _candidates(),
        discover_error=MutationToolError(kind="output_corrupt", detail="bad json"),
    )
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=0,
        touched_lines={"app/svc.py": frozenset({2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=runner,
    )
    assert evidence.status == "collection_failed"
    assert evidence.collection_gaps[0].code == "artifact_corrupt"


def test_equivalent_marking_excludes_from_survive_pressure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    runner = FakeRunner(
        (
            MutantCandidate(module="app/svc.py", line=2, operator="AOR", mutant_id="m1"),
            MutantCandidate(module="app/svc.py", line=1, operator="SDL", mutant_id="m3"),
        ),
        outcomes={"m1": "equivalent", "m3": "survived"},
    )
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=0,
        touched_lines={"app/svc.py": frozenset({1, 2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=runner,
    )
    assert evidence.equivalent == 1
    assert evidence.survived == 1
    assert evidence.tested == 2
    # Score = killed / (killed + survived); equivalents excluded from denominator.
    assert evidence.killed == 0
    assert evidence.value == pytest.approx(0.0)
    assert {s.mutant_id for s in evidence.survivors} == {"m3"}
    assert all(not s.equivalent for s in evidence.survivors)


def test_survivor_report_is_capped(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cands = tuple(
        MutantCandidate(module="app/svc.py", line=i, operator="AOR", mutant_id=f"m{i}") for i in range(1, 8)
    )
    runner = FakeRunner(cands, outcomes={f"m{i}": "survived" for i in range(1, 8)})
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=0,
        touched_lines={"app/svc.py": frozenset(range(1, 8))},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=runner,
        survivor_report_cap=3,
    )
    assert evidence.survived == 7
    assert len(evidence.survivors) == 3
    assert evidence.survivors_truncated is True
    assert evidence.survivor_report_cap == 3


def test_cache_hit_skips_rediscovery(tmp_path: Path) -> None:
    root = _project(tmp_path)
    modules = {"app/svc.py": (root / "app" / "svc.py").read_bytes()}
    touched = {"app/svc.py": frozenset({1, 2})}
    runner1 = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"})
    first = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=7,
        touched_lines=touched,
        module_contents=modules,
        runner=runner1,
    )
    assert first.cache_hit is False
    key = MutationCacheKey(
        module_digest=digest_module_contents(modules),
        test_tree_digest=digest_test_tree(root),
        sampler_version=SAMPLER_VERSION,
    )
    assert cache_path(root, key).is_file()
    assert MUTATION_CACHE_DIR_REL in str(cache_path(root, key))
    assert read_cache(root, key) is not None

    runner2 = FakeRunner((), outcomes={})  # discover must not be needed
    second = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=7,
        touched_lines=touched,
        module_contents=modules,
        runner=runner2,
    )
    assert second.cache_hit is True
    assert second.selected == first.selected
    assert runner2.tested  # still evaluates selected mutants
    # discover should not have been called — FakeRunner.discover returns () if called
    # but selected count matches first, proving cache supplied selection.


def test_no_floor_fields_on_mutation_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path)
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=0,
        touched_lines={"app/svc.py": frozenset({2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=FakeRunner(_candidates(), outcomes={"m1": "killed"}),
    )
    dumped = evidence.model_dump()
    assert "floor" not in dumped
    assert "floors" not in dumped


def test_outcome_error_counts_as_survived_for_score_pressure(tmp_path: Path) -> None:
    """Tool/runtime ``error`` is pressure (survived), not an equivalent nor a kill."""
    root = _project(tmp_path)
    runner = FakeRunner(
        (MutantCandidate(module="app/svc.py", line=2, operator="AOR", mutant_id="m1"),),
        outcomes={"m1": "error"},
    )
    evidence = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=0,
        touched_lines={"app/svc.py": frozenset({2})},
        module_contents={"app/svc.py": (root / "app" / "svc.py").read_bytes()},
        runner=runner,
    )
    assert evidence.status == "evaluated"
    assert evidence.killed == 0
    assert evidence.survived == 1
    assert evidence.equivalent == 0
    assert evidence.value == pytest.approx(0.0)
    assert evidence.survivors[0].mutant_id == "m1"


def test_seed_mismatch_is_cache_miss_and_recomputes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    modules = {"app/svc.py": (root / "app" / "svc.py").read_bytes()}
    touched = {"app/svc.py": frozenset({1, 2})}
    first_runner = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"})
    first = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=7,
        touched_lines=touched,
        module_contents=modules,
        runner=first_runner,
    )
    assert first.cache_hit is False

    recompute_runner = FakeRunner(_candidates(), outcomes={"m1": "survived", "m3": "killed"})
    second = run_mutation_sample(
        project_root=root,
        change_id=CHANGE_ID,
        budget_seconds=300,
        seed=99,  # different seed → must rediscover + resample
        touched_lines=touched,
        module_contents=modules,
        runner=recompute_runner,
    )
    assert second.cache_hit is False
    assert second.seed == 99
    # New cache record written for the new seed under the same digest key.
    key = MutationCacheKey(
        module_digest=digest_module_contents(modules),
        test_tree_digest=digest_test_tree(root),
        sampler_version=SAMPLER_VERSION,
    )
    cached = read_cache(root, key)
    assert cached is not None
    assert cached.seed == 99
