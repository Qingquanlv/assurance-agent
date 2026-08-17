"""M2 Task 7: end-to-end metrics-nightly acceptance (mocked mutmut + perf batches).

Runs the ``metrics-nightly`` graph ops in schema order with an injectable FakeRunner.
Does not rewrite historical benchmark archives.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assurance_agent.artifacts.canonical import canonical_json_bytes
from assurance_agent.artifacts.models.metrics import (
    MetricEntry,
    MetricScope,
    MetricsDocument,
    NIGHTLY_METRICS_REL,
    PR_METRICS_REL,
)
from assurance_agent.artifacts.models.pr_metric_evidence import MutationEvidence
from assurance_agent.evidence.metrics import DEFAULT_NIGHTLY_KEYS
from assurance_agent.verification.baseline_history import (
    DEFAULT_MIN_BASELINE_SAMPLES,
    PerformanceBaselineObservation,
    append_baseline_observation,
)
from assurance_agent.verification.mutation_runner import MutantOutcome, MutantResult
from assurance_agent.verification.mutation_sampling import MutantCandidate, SelectedMutant
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.metrics.mutation import MUTATION_EVIDENCE_REL
from assurance_agent.workflow.metrics.nightly import (
    ASSERTION_STRENGTH_EVIDENCE_REL,
    BASELINE_DRIFT_EVIDENCE_REL,
    NIGHTLY_GRAPH_TARGETS,
    NIGHTLY_SHORTBOARDS_REL,
    NIGHTLY_SOURCE_REL,
    run_metrics_nightly_graph,
    run_nightly_metrics_pipeline_operation,
)
from tests.helpers_aa import write_aa_config

CHANGE_ID = "CH-NIGHTLY-E2E-001"
COMPUTED_AT = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
DIGEST = "a" * 64

_API_STRONG = (
    "def test_strong(client):\n"
    "    response = client.post('/depts', json={'name': 'dup'})\n"
    "    assert response.status_code == 400\n"
    "    assert response.json()['detail'] == 'name already exists'\n"
)
_E2E_STRONG = (
    "async def test_strong(page):\n"
    "    await page.goto('/depts')\n"
    "    await expect(page.get_by_text('Engineering')).to_have_text('Engineering')\n"
)


class FakeRunner:
    """Injectable mutmut stand-in — never shells out (Task 3 seam)."""

    def __init__(
        self,
        candidates: Sequence[MutantCandidate],
        outcomes: dict[str, MutantOutcome] | None = None,
        *,
        seconds_per_test: float = 0.0,
    ) -> None:
        self.candidates = tuple(candidates)
        self.outcomes = outcomes or {}
        self.seconds_per_test = seconds_per_test
        self.discover_calls = 0
        self.tested: list[SelectedMutant] = []

    def discover(self, modules: Sequence[str]) -> Sequence[MutantCandidate]:
        self.discover_calls += 1
        wanted = set(modules)
        return tuple(c for c in self.candidates if c.module in wanted)

    def test(self, mutant: SelectedMutant, *, timeout_seconds: float) -> MutantResult:
        del timeout_seconds
        self.tested.append(mutant)
        outcome = self.outcomes.get(mutant.mutant_id, "killed")
        return MutantResult(mutant=mutant, outcome=outcome, elapsed_seconds=self.seconds_per_test)


def _candidates() -> tuple[MutantCandidate, ...]:
    return (
        MutantCandidate(module="app/svc.py", line=2, operator="AOR", mutant_id="m1"),
        MutantCandidate(module="app/svc.py", line=2, operator="ROR", mutant_id="m2"),
        MutantCandidate(module="app/svc.py", line=1, operator="SDL", mutant_id="m3"),
    )


def _pr_document() -> MetricsDocument:
    return MetricsDocument(
        schema_version="2",
        change_id=CHANGE_ID,
        cadence="pr",
        computed_at=COMPUTED_AT,
        risk_tier="medium",
        risk_tier_lower_bound="medium",
        risk_tier_declared=None,
        risk_declaration_lowered=False,
        risk_lowered_declarations=(),
        metrics={
            "constraint_coverage": MetricEntry(
                layer="api",
                status="evaluated",
                value=1.0,
                declared=MetricScope.of(total=4, covered=4),
                evidence="constraint-coverage.json",
            )
        },
        collection_gaps=(),
        shortboards=(),
        floor_ratio=0.95,
        policy_digest=DIGEST,
    )


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write_aa_config(root)
    (root / "app").mkdir(parents=True)
    (root / "app" / "svc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_svc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    api = root / "tests" / "api"
    e2e = root / "tests" / "e2e"
    api.mkdir(parents=True)
    e2e.mkdir(parents=True)
    (api / "test_api.py").write_text(_API_STRONG, encoding="utf-8")
    (e2e / "test_e2e.py").write_text(_E2E_STRONG, encoding="utf-8")
    change = root / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    return root


def _workspace(project_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        task_id="t-nightly-e2e",
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


def _write_perf_batch(
    project_root: Path,
    *,
    batch_id: str,
    p95_ms: float,
    error_rate: float = 0.0,
    capability: str = "login",
    endpoint: str = "POST /api/login",
) -> None:
    change = project_root / "qa" / "changes" / CHANGE_ID
    batch = change / "execution" / "runs" / batch_id
    batch.mkdir(parents=True, exist_ok=True)
    (batch / "raw").mkdir(exist_ok=True)
    (batch / "raw" / "changed-lines.json").write_text(
        json.dumps({"app/svc.py": [1, 2]}),
        encoding="utf-8",
    )
    manifest = change / "execution" / "execution-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": batch_id,
                "executed_at": "2026-08-05T10:00:00+00:00",
                "selected_targets": {
                    "api": False,
                    "e2e": False,
                    "fuzz": False,
                    "performance": True,
                },
                "result_files": {"performance": "performance-result.json"},
                "final_status": "PASS",
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "schema_version": "1.0",
        "change_id": CHANGE_ID,
        "batch_id": batch_id,
        "kind": "performance",
        "available": True,
        "status": "PASS",
        "scenarios": [
            {
                "capability": capability,
                "endpoint": endpoint,
                "measured_p95_ms": p95_ms,
                "threshold_p95_ms": 2000.0,
                "measured_error_rate": error_rate,
                "threshold_error_rate_max": 0.01,
                "verdict": "PASS",
            }
        ],
        "command": "locust",
        "source": {"raw_log": "raw/perf.log"},
    }
    (batch / "performance-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_baseline_history(project_root: Path, *, p95_ms: float = 10.0) -> None:
    for i in range(DEFAULT_MIN_BASELINE_SAMPLES):
        append_baseline_observation(
            project_root,
            PerformanceBaselineObservation(
                capability="login",
                endpoint="POST /api/login",
                change_id=f"CH-HIST-{i}",
                batch_id=f"hist-{i}",
                p95_ms=p95_ms,
                error_rate=0.0,
                source_digest=f"{i:064x}",
            ),
        )


def _plant_pr_metrics(change_dir: Path) -> bytes:
    pr_path = change_dir / PR_METRICS_REL
    pr_path.parent.mkdir(parents=True, exist_ok=True)
    planted = canonical_json_bytes(_pr_document())
    pr_path.write_bytes(planted)
    return planted


def test_nightly_graph_helper_targets_match_schema_order() -> None:
    assert NIGHTLY_GRAPH_TARGETS == (
        "operation:load-latest-pr-metrics",
        "operation:run-mutation-sample",
        "operation:compute-assertion-strength",
        "operation:compute-baseline-drift",
        "operation:collect-adversarial-yield",
        "operation:materialize-quarantine-projection",
        "operation:materialize-c-layer-metrics",
        "operation:aggregate-nightly-metrics",
        "operation:evaluate-retrospective-shortboards",
    )


def test_metrics_nightly_graph_e2e_artifacts_cache_shortboards_and_pr_isolation(
    tmp_path: Path,
) -> None:
    """Happy path: two perf batches, cache miss→hit, artifacts, PR bytes frozen."""
    root = _project(tmp_path)
    workspace = _workspace(root)
    planted = _plant_pr_metrics(workspace.change_dir)
    _seed_baseline_history(root, p95_ms=10.0)

    # Batch 1 — mild drift within band; mutation cache miss.
    _write_perf_batch(root, batch_id="perf-batch-001", p95_ms=11.0)
    runner1 = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "survived"})
    results1 = run_metrics_nightly_graph(
        workspace,
        _context(
            root,
            mutation_budget_seconds=60,
            mutation_seed=7,
        ),
        mutation_runner=runner1,
    )
    assert all(r.status == "succeeded" for r in results1)
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted

    mut1 = MutationEvidence.model_validate(
        json.loads((workspace.change_dir / MUTATION_EVIDENCE_REL).read_text(encoding="utf-8"))
    )
    assert mut1.status == "evaluated"
    assert mut1.cache_hit is False
    assert runner1.discover_calls == 1

    nightly1 = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert nightly1.cadence == "nightly"
    assert nightly1.change_id == CHANGE_ID
    assert set(DEFAULT_NIGHTLY_KEYS) <= set(nightly1.metrics)
    assert nightly1.metrics["mutation_score"].status == "evaluated"
    assert nightly1.metrics["assertion_strength"].status == "evaluated"
    assert nightly1.metrics["baseline_drift"].status == "evaluated"
    assert nightly1.metrics["baseline_drift"].value == pytest.approx(0.1)
    # No discovery yield receipt → B3 stays pending (never invented collection gap).
    assert nightly1.metrics["adversarial_yield"].status == "not_evaluated"
    assert any(b.code == "pending_nightly" and b.metric == "adversarial_yield" for b in nightly1.shortboards)
    assert not any(
        g.code in {"collection_failed", "artifact_corrupt"} and g.metric == "adversarial_yield"
        for g in nightly1.collection_gaps
    )
    assert (workspace.change_dir / ASSERTION_STRENGTH_EVIDENCE_REL).is_file()
    assert (workspace.change_dir / BASELINE_DRIFT_EVIDENCE_REL).is_file()
    assert (workspace.change_dir / NIGHTLY_SOURCE_REL).is_file()
    shortboards1 = json.loads((workspace.change_dir / NIGHTLY_SHORTBOARDS_REL).read_text(encoding="utf-8"))
    assert shortboards1["reopens_pr_verdict"] is False
    assert shortboards1["consumers"] == ["pr_metrics_batch", "retro"]
    assert "confidence" not in shortboards1
    assert '"confidence"' not in (workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8")
    replay1 = nightly1.replay_subtree()

    # Batch 2 — out-of-band drift shortboard; mutation cache hit (no rediscovery).
    _write_perf_batch(root, batch_id="perf-batch-002", p95_ms=15.0)
    runner2 = FakeRunner((), outcomes={"m1": "killed", "m3": "survived"})
    results2 = run_metrics_nightly_graph(
        workspace,
        _context(
            root,
            mutation_budget_seconds=60,
            mutation_seed=7,
        ),
        mutation_runner=runner2,
    )
    assert all(r.status == "succeeded" for r in results2)
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted

    mut2 = MutationEvidence.model_validate(
        json.loads((workspace.change_dir / MUTATION_EVIDENCE_REL).read_text(encoding="utf-8"))
    )
    assert mut2.cache_hit is True
    assert runner2.discover_calls == 0

    nightly2 = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert nightly2.metrics["baseline_drift"].status == "evaluated"
    # Batch-001 was appended into history, so mean is (3×10 + 11)/4 = 10.25;
    # 15 vs 10.25 → ~0.463 relative regression (still above the 0.2 band).
    assert nightly2.metrics["baseline_drift"].value == pytest.approx((15.0 - 10.25) / 10.25)
    assert nightly2.metrics["baseline_drift"].value is not None
    assert nightly2.metrics["baseline_drift"].value > 0.2
    assert any(b.code == "baseline_drift_out_of_band" for b in nightly2.shortboards)
    # Replay anchor from run 1 is independent of run 2's computed_at / new drift.
    assert replay1 == nightly1.replay_subtree()
    assert nightly2.metrics["adversarial_yield"].status == "not_evaluated"
    # PR metrics / verdict never rewritten across both graph runs.
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted
    pr_doc = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / PR_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert pr_doc.cadence == "pr"
    assert pr_doc.floor_ratio == 0.95


def test_metrics_nightly_budget_shortboard_does_not_fail_graph(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workspace = _workspace(root)
    planted = _plant_pr_metrics(workspace.change_dir)
    _seed_baseline_history(root)
    _write_perf_batch(root, batch_id="perf-batch-001", p95_ms=11.0)

    runner = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"}, seconds_per_test=10.0)
    results = run_metrics_nightly_graph(
        workspace,
        _context(root, mutation_budget_seconds=5, mutation_seed=1),
        mutation_runner=runner,
    )
    assert all(r.status == "succeeded" for r in results)
    evidence = MutationEvidence.model_validate(
        json.loads((workspace.change_dir / MUTATION_EVIDENCE_REL).read_text(encoding="utf-8"))
    )
    assert evidence.budget_exceeded is True
    assert any(b.code == "mutation_budget_exceeded" for b in evidence.shortboards)
    nightly = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert any(b.code == "mutation_budget_exceeded" for b in nightly.shortboards)
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted


def test_metrics_nightly_replay_determinism_ignores_computed_at(tmp_path: Path) -> None:
    root = _project(tmp_path)
    workspace = _workspace(root)
    planted = _plant_pr_metrics(workspace.change_dir)
    _seed_baseline_history(root)
    _write_perf_batch(root, batch_id="perf-batch-001", p95_ms=11.0)

    runner_a = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"})
    run_metrics_nightly_graph(
        workspace,
        _context(root, mutation_budget_seconds=60, mutation_seed=7),
        mutation_runner=runner_a,
    )
    doc_a = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )

    runner_b = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"})
    run_metrics_nightly_graph(
        workspace,
        _context(root, mutation_budget_seconds=60, mutation_seed=7),
        mutation_runner=runner_b,
    )
    doc_b = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )

    assert doc_a.replay_subtree() == doc_b.replay_subtree()
    assert canonical_json_bytes(doc_a.replay_subtree()) == canonical_json_bytes(doc_b.replay_subtree())
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted


def test_insufficient_baseline_samples_stay_not_evaluated_with_pending_path(tmp_path: Path) -> None:
    """Cold baseline → not_evaluated + sample_insufficient; adversarial still pending_nightly."""
    root = _project(tmp_path)
    workspace = _workspace(root)
    planted = _plant_pr_metrics(workspace.change_dir)
    # No history seeded — only the current batch observation after the collector appends.
    _write_perf_batch(root, batch_id="perf-batch-001", p95_ms=11.0)

    runner = FakeRunner(_candidates(), outcomes={"m1": "killed", "m3": "killed"})
    results = run_metrics_nightly_graph(
        workspace,
        _context(root, mutation_budget_seconds=60, mutation_seed=7),
        mutation_runner=runner,
    )
    assert all(r.status == "succeeded" for r in results)
    nightly = MetricsDocument.model_validate(
        json.loads((workspace.change_dir / NIGHTLY_METRICS_REL).read_text(encoding="utf-8"))
    )
    assert nightly.metrics["baseline_drift"].status == "not_evaluated"
    assert any(b.code == "sample_insufficient" and b.metric == "baseline_drift" for b in nightly.shortboards)
    assert nightly.metrics["adversarial_yield"].status == "not_evaluated"
    assert any(b.code == "pending_nightly" and b.metric == "adversarial_yield" for b in nightly.shortboards)
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted


def test_nightly_host_fail_fast_does_not_write_nightly_json_after_earlier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed collector must stop the host; later artifacts stay unwritten."""
    root = _project(tmp_path)
    workspace = _workspace(root)
    planted = _plant_pr_metrics(workspace.change_dir)
    failure = TaskResult(status="failed", error_kind="internal", error="collector exploded")

    def _fail_mutation(*_args: object, **_kwargs: object) -> TaskResult:
        return failure

    monkeypatch.setattr(
        "assurance_agent.workflow.metrics.nightly.run_mutation_sample_operation",
        _fail_mutation,
    )
    results = run_metrics_nightly_graph(
        workspace,
        _context(root, mutation_budget_seconds=60, mutation_seed=7),
    )
    assert [result.status for result in results] == ["succeeded", "failed"]
    assert results[-1] is failure
    assert not (workspace.change_dir / NIGHTLY_METRICS_REL).exists()
    assert not (workspace.change_dir / NIGHTLY_SHORTBOARDS_REL).exists()

    host = run_nightly_metrics_pipeline_operation(
        ExecutableTask.model_construct(
            task_id="t-run-nightly-metrics-pipeline",
            node_id="run-nightly-metrics-pipeline",
            graph_id="metrics-nightly-workflow",
            target="operation:run-nightly-metrics-pipeline",
            input={"with": {}},
        ),
        workspace,
        _context(root, mutation_budget_seconds=60, mutation_seed=7),
    )
    assert host is failure
    assert host.status == "failed"
    assert host.error == "collector exploded"
    assert not (workspace.change_dir / NIGHTLY_METRICS_REL).exists()
    assert workspace.change_dir.joinpath(PR_METRICS_REL).read_bytes() == planted
