"""Execution orchestrator: run selected layers, build the gate, publish evidence.

run_change is the public entry consumed by `aa run`. It never fabricates: an
unselected or missing layer becomes a SKIPPED result and the quality gate
degrades accordingly.
"""

from datetime import UTC, datetime
from pathlib import Path

from assurance_agent.artifacts.models import ExecutionManifest
from assurance_agent.artifacts.policy import PolicyError, load_policy
from assurance_agent.config import AaConfig
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageEvaluation,
    build_evidence_coverage_evaluation,
)
from assurance_agent.evidence.trace import ExecutionFoldInput, canonical_json_bytes, fold_trace
from assurance_agent.workflow.execution.evidence import (
    publish_execution_evidence,
    write_batch_result_files,
)
from assurance_agent.workflow.execution.exec_config import load_coverage_config, load_perf_config
from assurance_agent.workflow.execution.results import CoverageResult
from assurance_agent.workflow.execution.runners import (
    parse_coverage_result,
    run_performance_target,
    run_pytest_target,
)
from assurance_agent.workflow.execution.scope import resolve_test_paths
from assurance_agent.workflow.execution.selection import resolve_selected_targets
from assurance_agent.workflow.execution.tree_hash import hash_product_tree, hash_test_tree
from assurance_agent.workflow.healing.safety import load_product_code_roots
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def generate_batch_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_aware() -> datetime:
    return datetime.now(UTC)


def _strip(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


def _test_dir(config: AaConfig, attr: str, default: str) -> str:
    tests = getattr(config, "tests", None)
    value = getattr(tests, attr, None) if tests is not None else None
    return _strip(value) if isinstance(value, str) else default


def _evidence_diagnostics(
    evaluation: EvidenceCoverageEvaluation,
) -> dict:
    payload: dict = {}
    if evaluation.error_code == "policy_error":
        payload["policy_error"] = "invalid or unreadable .aa/policy.yaml"
    elif evaluation.report is not None:
        payload.update(evaluation.report.model_dump(mode="json"))
    return {"evidence_sufficiency": payload}


def _evaluate_evidence_coverage(
    project_root: Path,
    projection,
    *,
    as_of: datetime,
) -> EvidenceCoverageEvaluation:
    try:
        policy = load_policy(project_root)
    except PolicyError:
        return EvidenceCoverageEvaluation(
            report=None,
            action=None,
            error_code="policy_error",
        )
    return build_evidence_coverage_evaluation(projection, policy, as_of=as_of)


def _assert_gate_projection_matches_disk_fold(gate_projection, disk_projection) -> None:  # noqa: ANN001
    gate_bytes = canonical_json_bytes(gate_projection.model_dump(mode="json"))
    disk_bytes = canonical_json_bytes(disk_projection.model_dump(mode="json"))
    if gate_bytes != disk_bytes:
        raise RuntimeError("gate trace projection diverged from post-publish disk fold")


def run_change(
    project_root: Path,
    change_dir: Path,
    config: AaConfig,
    *,
    batch_id: str | None = None,
) -> ExecutionManifest:
    change_id = change_dir.name
    batch_id = batch_id or generate_batch_id()
    execution_dir = change_dir / "execution"
    batch_dir = execution_dir / "runs" / batch_id

    selected = resolve_selected_targets(change_dir)
    cov_config = load_coverage_config(config)
    perf_config = load_perf_config(config)

    cov_package = cov_config.target_package if cov_config.enabled else None
    api = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="api",
            test_dir=_test_dir(config, "api", "tests/api"),
            test_paths=resolve_test_paths(change_dir, "api"),
            cov_package=cov_package,
        )
        if selected.api
        else None
    )
    e2e = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="e2e",
            test_dir=_test_dir(config, "e2e", "tests/e2e"),
            test_paths=resolve_test_paths(change_dir, "e2e"),
        )
        if selected.e2e
        else None
    )
    fuzz = (
        run_pytest_target(
            project_root=project_root,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            target="fuzz",
            test_dir=_test_dir(config, "fuzz", "tests/fuzz"),
            test_paths=resolve_test_paths(change_dir, "fuzz"),
        )
        if selected.fuzz
        else None
    )

    if selected.api:
        coverage = parse_coverage_result(
            change_id=change_id,
            batch_id=batch_id,
            batch_dir=batch_dir,
            threshold=cov_config.threshold,
        )
    else:
        coverage = CoverageResult(
            change_id=change_id,
            batch_id=batch_id,
            available=False,
            line_coverage=0.0,
            branch_coverage=0.0,
            threshold=cov_config.threshold,
            status="SKIPPED",
            skip_reason="api_unselected",
        )

    performance = (
        run_performance_target(
            project_root=project_root,
            change_dir=change_dir,
            batch_dir=batch_dir,
            change_id=change_id,
            batch_id=batch_id,
            perf_config=perf_config,
            test_paths=resolve_test_paths(change_dir, "performance"),
        )
        if selected.performance
        else None
    )

    test_tree = hash_test_tree(project_root)
    now_aware = _now_aware()
    batch_dir.mkdir(parents=True, exist_ok=True)
    write_batch_result_files(
        batch_dir,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        performance=performance,
    )
    current = ExecutionFoldInput(
        batch_id=batch_id,
        executed_at=now_aware,
        selected_targets=selected,
        test_files_sha256=test_tree.files,
    )
    gate_projection = fold_trace(project_root, change_id, phase="execution", current=current)
    evidence_coverage = _evaluate_evidence_coverage(project_root, gate_projection, as_of=now_aware)
    diagnostics = _evidence_diagnostics(evidence_coverage)

    quality_gate = build_quality_gate(
        change_id=change_id,
        batch_id=batch_id,
        api=api,
        e2e=e2e,
        coverage=coverage,
        evidence_coverage=evidence_coverage,
        fuzz=fuzz,
        performance=performance,
    )
    quality_gate = quality_gate.model_copy(update={"diagnostics": diagnostics})
    summary = _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, quality_gate)

    product_tree = hash_product_tree(project_root, load_product_code_roots(project_root))

    manifest = publish_execution_evidence(
        execution_dir=execution_dir,
        change_id=change_id,
        batch_id=batch_id,
        selected_targets=selected,
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        coverage=coverage,
        performance=performance,
        quality_gate=quality_gate,
        summary=summary,
        tests_tree_sha256=test_tree.aggregate,
        test_files_sha256=test_tree.files,
        product_tree_sha256=product_tree.aggregate,
        executed_at=now_aware,
    )
    disk_projection = fold_trace(project_root, change_id, phase="execution", current=None)
    (batch_dir / "trace-projection.json").write_text(
        gate_projection.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _assert_gate_projection_matches_disk_fold(gate_projection, disk_projection)
    return manifest


def _build_summary(change_id, batch_id, api, e2e, fuzz, coverage, performance, gate) -> str:  # noqa: ANN001
    def line(label: str, r) -> str:  # noqa: ANN001
        if r is None:
            return f"- {label}: unselected"
        return f"- {label}: {r.status} total={r.total} passed={r.passed} failed={r.failed}"

    parts = [
        f"# Execution Summary — {change_id}",
        "",
        f"- Batch: {batch_id}",
        f"- Final Status: {gate.final_status}",
        "",
        line("API", api),
        line("E2E", e2e),
        line("Fuzz", fuzz),
        f"- Coverage: {coverage.status} line={coverage.line_coverage}%"
        if coverage
        else "- Coverage: unselected",
        f"- Performance: {performance.status}" if performance else "- Performance: unselected",
        "",
    ]
    return "\n".join(parts)
