"""Classify execution failures → inspect/ artifacts (failure-analysis + quality-gate).

Reads the primary execution evidence, classifies every failed case, and derives
FailureAnalysis. Manifest integrity issues (a selected target with no result
file) are treated as critical manifest_asset_missing failures, not silent skips.
"""

from pathlib import Path

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    CoverageGapEntry,
    FailureAnalysis,
    FailureEntry,
    FailureEvidence,
    QualityGateResultLike,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.workflow.execution.evidence import (
    EvidenceError,
    ExecutionEvidence,
    load_execution_evidence,
)
from assurance_agent.evidence.sufficiency import EvidenceCoverageEvaluation
from assurance_agent.workflow.execution.results import TargetResult
from assurance_agent.workflow.report.failure_classifier import classify_failure
from assurance_agent.workflow.report.quality_gate import coverage_metrics_status


class InspectResult(BaseModel):
    analysis: FailureAnalysis
    quality_gate: QualityGateResultLike
    analysis_path: str
    summary_path: str
    quality_gate_path: str


def _load_for_inspection(
    execution_dir: Path,
    batch_id: str | None,
) -> tuple[ExecutionEvidence, str, str | None]:
    if batch_id is not None:
        return load_execution_evidence(execution_dir, batch_id=batch_id), "primary", None
    primary_error: str | None = None
    try:
        primary = load_execution_evidence(execution_dir)
        if not primary.integrity_issues and primary.quality_gate is not None:
            return primary, "primary", None
        primary_error = "latest execution evidence is incomplete"
    except EvidenceError as err:
        primary_error = str(err)
    runs = execution_dir / "runs"
    candidates = (p.name for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else ()
    for candidate in sorted(candidates, reverse=True):
        try:
            evidence = load_execution_evidence(execution_dir, batch_id=candidate)
        except EvidenceError:
            continue
        if not evidence.integrity_issues and evidence.quality_gate is not None:
            return evidence, "compat_fallback", primary_error
    raise EvidenceError(primary_error or "no readable execution evidence")


def inspect_change(
    project_root: Path,
    change_id: str,
    *,
    batch_id: str | None = None,
) -> InspectResult:
    change_base = resolve_change(project_root, change_id).path
    execution_dir = change_base / "execution"
    inspect_dir = change_base / "inspect"

    evidence, inspect_mode, compat_reason = _load_for_inspection(execution_dir, batch_id)
    gate = evidence.quality_gate
    if gate is None:
        from assurance_agent.workflow.report.quality_gate import build_quality_gate

        gate = build_quality_gate(
            change_id=change_id,
            batch_id=evidence.batch_id,
            api=evidence.api,
            e2e=evidence.e2e,
            coverage=evidence.coverage,
            evidence_coverage=EvidenceCoverageEvaluation(
                report=None,
                action=None,
                error_code="evidence_projection_missing",
            ),
            fuzz=evidence.fuzz,
            performance=evidence.performance,
        )

    manifest_path = str(execution_dir / "execution-manifest.yaml")

    if evidence.integrity_issues:
        integrity = _integrity_failures(evidence)
        _complete(integrity)
        analysis = _analysis(
            change_id,
            manifest_path,
            evidence.batch_id,
            "FAIL",
            "failed",
            "failed",
            integrity,
            integrity,
            [],
            [],
            [],
            inspect_mode,
            compat_reason,
        )
        gate = gate.model_copy(update={"final_status": "FAIL"})
        _write(inspect_dir, change_id, analysis, gate)
        return _result(analysis, gate, inspect_dir)

    failures: list[FailureEntry] = []
    for result, target in ((evidence.api, "api"), (evidence.e2e, "e2e"), (evidence.fuzz, "fuzz")):
        failures.extend(_classify_target(result, target, evidence, change_id))  # type: ignore[arg-type]

    coverage_gaps = _coverage_gaps(evidence)
    if coverage_metrics_status(gate.dimensions.coverage) == "FAIL":
        failures.extend(_coverage_failures(evidence, coverage_gaps))

    _complete(failures)
    hard = [f for f in failures if not f.fix_proposal_eligible and not f.needs_review]
    review = [f for f in failures if f.needs_review]
    known = [f for f in failures if f.category == "known_product_issue"]
    status = "no_failures" if not failures else "analyzed"
    analysis = _analysis(
        change_id,
        manifest_path,
        evidence.batch_id,
        gate.final_status,
        "completed",
        status,
        failures,
        hard,
        review,
        known,
        coverage_gaps,
        inspect_mode,
        compat_reason,
    )
    _write(inspect_dir, change_id, analysis, gate)
    return _result(analysis, gate, inspect_dir)


def _classify_target(
    result: TargetResult | None,
    target: str,
    evidence: ExecutionEvidence,
    change_id: str,
) -> list[FailureEntry]:
    if result is None:
        return []
    entries: list[FailureEntry] = []
    for case in [*result.cases, *result.unmapped_tests]:
        if case.status != "failed":
            continue
        classification = classify_failure(
            message=case.message,
            log_excerpt="",
            target=target,  # type: ignore[arg-type]
        )
        entries.append(
            FailureEntry(
                case_id=case.case_id or case.test_name,
                target=target,  # type: ignore[arg-type]
                category=classification.category,
                fix_proposal_eligible=classification.fix_proposal_eligible,
                severity=classification.severity,
                needs_review=classification.needs_review,
                evidence=FailureEvidence(
                    result_file=evidence.result_paths.get(target, ""),
                    test_file=case.file,
                    trace=case.trace,
                    screenshot=case.screenshot,
                    video=case.video,
                    raw_log=case.raw_log_ref,
                    log_excerpt=case.message[:400],
                ),
                diagnosis=_diagnosis(case.message, classification.category),
                recommended_action=_recommended_action(classification.category, change_id),
            )
        )
    return entries


def _coverage_gaps(evidence: ExecutionEvidence) -> list[CoverageGapEntry]:
    cov = evidence.coverage
    if not cov or not cov.available:
        return []
    return [
        CoverageGapEntry(
            file=str(f.get("file", "")),
            line_coverage=float(f.get("line_coverage", 0.0)),
            threshold=cov.threshold.line,
        )
        for f in cov.uncovered_critical_files
    ]


def _coverage_failures(evidence: ExecutionEvidence, gaps: list[CoverageGapEntry]) -> list[FailureEntry]:
    cov = evidence.coverage
    line = cov.line_coverage if cov else 0.0
    threshold = cov.threshold.line if cov else 0.0
    resolved = gaps or [CoverageGapEntry(file="coverage", line_coverage=line, threshold=threshold)]
    return [
        FailureEntry(
            case_id=gap.file,
            target="coverage",
            category="coverage_gap",
            fix_proposal_eligible=False,
            severity="high",
            needs_review=False,
            evidence=FailureEvidence(
                result_file=evidence.result_paths.get("coverage", ""),
                test_file=gap.file,
                trace="",
                screenshot="",
                video="",
                raw_log="",
                log_excerpt=f"line coverage {gap.line_coverage}% below threshold {gap.threshold}%",
            ),
            diagnosis=f"Coverage gate failed for {gap.file}: {gap.line_coverage}% below threshold {gap.threshold}%.",
            recommended_action="Add or improve tests for the uncovered critical file. Coverage gaps are never auto-fixed.",
        )
        for gap in resolved
    ]


def _integrity_failures(evidence: ExecutionEvidence) -> list[FailureEntry]:
    return [
        FailureEntry(
            case_id=f"manifest:{issue.target}",
            target=issue.target,  # type: ignore[arg-type]
            category="manifest_asset_missing",
            fix_proposal_eligible=False,
            severity="critical",
            needs_review=False,
            evidence=FailureEvidence(
                result_file=issue.path,
                test_file="",
                trace="",
                screenshot="",
                video="",
                raw_log="",
                log_excerpt=issue.reason,
            ),
            diagnosis=f"Execution asset missing for target '{issue.target}': {issue.path}",
            recommended_action="Re-run `aa run` to regenerate the missing execution asset before archiving.",
        )
        for issue in evidence.integrity_issues
    ]


def _complete(failures: list[FailureEntry]) -> None:
    for index, failure in enumerate(failures, start=1):
        failure.id = failure.id or f"FAIL-{index:03d}"
        failure.test = failure.test or failure.evidence.test_file or failure.case_id
        failure.recommended_next_action = failure.recommended_next_action or failure.recommended_action


def _analysis(
    change_id,
    manifest_path,
    batch_id,
    final_status,
    inspection_status,
    status,  # noqa: ANN001
    failures,
    hard,
    review,
    known,
    coverage_gaps,
    inspect_mode: str = "primary",
    compat_fallback_reason: str | None = None,
) -> FailureAnalysis:
    return FailureAnalysis(
        schema_version="1.0",
        change_id=change_id,
        source_manifest=manifest_path,
        inspection_status=inspection_status,
        batch_id=batch_id,
        source_batch_id=batch_id,
        final_status=final_status,
        inspect_mode=inspect_mode,  # type: ignore[arg-type]
        compat_fallback_reason=compat_fallback_reason,
        classification_performed=status != "failed",
        status=status,
        failures=failures,
        hard_fails=hard,
        needs_review=review,
        known_product_issues=known,
        coverage_gaps=coverage_gaps or None,
    )


def _write(inspect_dir: Path, change_id: str, analysis: FailureAnalysis, gate: QualityGateResultLike) -> None:
    inspect_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "failure-analysis.json").write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    (inspect_dir / "quality-gate-result.json").write_text(gate.model_dump_json(indent=2), encoding="utf-8")
    (inspect_dir / "failure-summary.md").write_text(_summary_md(change_id, analysis), encoding="utf-8")


def _result(analysis: FailureAnalysis, gate: QualityGateResultLike, inspect_dir: Path) -> InspectResult:
    return InspectResult(
        analysis=analysis,
        quality_gate=gate,
        analysis_path=str(inspect_dir / "failure-analysis.json"),
        summary_path=str(inspect_dir / "failure-summary.md"),
        quality_gate_path=str(inspect_dir / "quality-gate-result.json"),
    )


def _summary_md(change_id: str, analysis: FailureAnalysis) -> str:
    lines = [
        f"# Failure Analysis — {change_id}",
        "",
        f"- Final Status: {analysis.final_status}",
        f"- Batch: {analysis.batch_id}",
        f"- Failures: {len(analysis.failures)} (hard={len(analysis.hard_fails)}, review={len(analysis.needs_review)})",
        "",
    ]
    for failure in analysis.failures:
        flag = (
            "fix-allowed"
            if failure.fix_proposal_eligible
            else ("review" if failure.needs_review else "no-fix")
        )
        lines.append(
            f"- `{failure.case_id}` [{failure.target}] {failure.category} ({flag}): {failure.diagnosis}"
        )
    lines.append("")
    return "\n".join(lines)


_DIAGNOSIS_PREFIX = {
    "locator_failure": "Element locator failed.",
    "wait_strategy_failure": "Wait/timeout strategy failed.",
    "assertion_failure": "Assertion mismatch — expected value differs from actual.",
    "environment_failure": "Environment or connectivity issue prevented test execution.",
    "test_data_failure": "Test data or fixture not available.",
    "business_logic_failure": "Server returned an error suggesting a product-level issue.",
    "known_product_issue": "Known product issue matched from failure evidence.",
    "fuzz_configuration_error": "Fuzz setup/config error. Not a product bug.",
    "fuzz_stateful_failure": "Fuzzing surfaced a server fault under generated input.",
    "test_code_error": "Test code has a syntax or runtime error.",
    "case_semantic_failure": "Test case does not align with acceptance criteria.",
}


def _diagnosis(message: str, category: str) -> str:
    first = (message or "").split("\n")[0][:200]
    prefix = _DIAGNOSIS_PREFIX.get(category)
    return f"{prefix} {first}".strip() if prefix else (first or "Unknown failure.")


_ACTION = {
    "locator_failure": "Generate a Fix Proposal to update the locator strategy after review. Run `aa heal validate-proposal --change {cid}`.",
    "wait_strategy_failure": "Generate a Fix Proposal to adjust wait conditions. Run `aa heal validate-proposal --change {cid}`.",
    "assertion_failure": "Investigate whether product behaviour changed or the expected value is wrong. Do not auto-fix.",
    "environment_failure": "Fix the environment (server, database, network) and rerun. Do not generate a Fix Proposal.",
    "test_data_failure": "Review test fixtures and seed data. A Fix Proposal may be generated with manual review.",
    "business_logic_failure": "File a bug against the product team. This is not a test issue.",
    "known_product_issue": "Documented product issue — route to product/developer tracking, do not generate a test fix.",
    "test_code_error": "Fix the syntax/import error in the test file and rerun.",
    "case_semantic_failure": "Review the test case against requirements. Update the case YAML if necessary.",
    "coverage_gap": "Add or improve tests for the uncovered critical file. Coverage gaps are never auto-fixed.",
}


def _recommended_action(category: str, change_id: str) -> str:
    template = _ACTION.get(category, "Investigate manually. Classification is uncertain.")
    return template.format(cid=change_id)
