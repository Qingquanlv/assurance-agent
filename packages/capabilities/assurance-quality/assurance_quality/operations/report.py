"""Deterministic report builder and dashboard projection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import Field

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import FrozenModel, TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.common import CoverageDimension
from assurance_quality.contracts.inspect import (
    FailureAnalysis,
    QualityGateResultLike,
    load_quality_gate_result_document,
)
from assurance_quality.contracts.issues import ChangeIssueSnapshot, ProblemProjection
from assurance_quality.contracts.report import (
    IssueReport,
    QualityReport,
    QualityScoreBreakdown,
    ReportDefect,
    ReportDefects,
    ReportScope,
)
from assurance_quality.operations.common import InputError, failed_input, validate_input
from assurance_quality.operations.inspect import worst_status

_SHA256 = r"^[0-9a-f]{64}$"
_PRODUCT = {
    "business_logic_failure",
    "fuzz_stateful_failure",
    "known_product_issue",
    "perf_threshold_exceeded",
}
_ENVIRONMENT = {"environment_failure", "perf_environment"}
_ACTIVE = frozenset({"detected", "triaged", "in_progress", "verification_pending", "accepted_risk"})
_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_KEYS = ("functional", "coverage", "fuzz", "performance")


class ScoreDimension(FrozenModel):
    active: bool
    ratio: float
    weight: float


class GenerateReportInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    quality_gate: dict[str, Any]
    analysis: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    problems: dict[str, Any] | None = None
    scope: ReportScope
    started_at: str | None = None
    duration: str | None = None
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str = Field(pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)


class DashboardInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    report: dict[str, Any]
    quality_gate: dict[str, Any] | None = None
    metrics_digest: str = Field(pattern=_SHA256)
    report_digest: str = Field(pattern=_SHA256)


def _clamp01(value: float) -> float:
    if value != value:
        return 0.0
    return max(0.0, min(1.0, value))


def _round1(value: float) -> float:
    return round(value * 10) / 10


def compute_quality_score(dims: dict[str, ScoreDimension]) -> tuple[int, QualityScoreBreakdown]:
    active_weight = sum(dims[key].weight for key in _KEYS if dims[key].active)
    parts: dict[str, float | Literal["N/A"]] = {key: "N/A" for key in _KEYS}
    if active_weight <= 0:
        return 0, QualityScoreBreakdown.model_validate(parts)
    total = 0.0
    for key in _KEYS:
        dim = dims[key]
        if not dim.active:
            continue
        points = (dim.weight / active_weight) * 100 * _clamp01(dim.ratio)
        parts[key] = _round1(points)
        total += points
    return round(total), QualityScoreBreakdown.model_validate(parts)


def _dimensions(gate: QualityGateResultLike) -> dict[str, ScoreDimension]:
    func = gate.dimensions.functional
    func_total = func.api.total + func.e2e.total
    func_passed = func.api.passed + func.e2e.passed
    cov = gate.dimensions.coverage
    threshold_line = cov.threshold.line
    cov_ratio = cov.line_coverage / threshold_line if (cov.available and threshold_line > 0) else 0.0
    fuzz_active = bool(func.fuzz and func.fuzz.total > 0)
    fuzz_ratio = (func.fuzz.passed / func.fuzz.total) if fuzz_active and func.fuzz else 0.0
    non_func = gate.dimensions.non_functional
    ran = [item for item in non_func.performance if item.verdict != "SKIPPED"] if non_func else []
    perf_active = bool(non_func and non_func.status != "SKIPPED" and ran)
    perf_ratio = (sum(1 for item in ran if item.verdict == "PASS") / len(ran)) if perf_active else 0.0
    weights = (
        {"functional": 50, "coverage": 20, "fuzz": 15, "performance": 15}
        if fuzz_active or perf_active
        else {"functional": 70, "coverage": 30, "fuzz": 0, "performance": 0}
    )
    return {
        "functional": ScoreDimension(
            active=func_total > 0,
            ratio=(func_passed / func_total) if func_total > 0 else 0.0,
            weight=weights["functional"],
        ),
        "coverage": ScoreDimension(active=cov.available, ratio=cov_ratio, weight=weights["coverage"]),
        "fuzz": ScoreDimension(active=fuzz_active, ratio=fuzz_ratio, weight=weights["fuzz"]),
        "performance": ScoreDimension(active=perf_active, ratio=perf_ratio, weight=weights["performance"]),
    }


def _report_coverage(coverage: CoverageDimension | object) -> CoverageDimension:
    status = coverage.status  # type: ignore[attr-defined]
    available = bool(coverage.available)  # type: ignore[attr-defined]
    if not available:
        status = "SKIPPED"
    return CoverageDimension(
        status=status,
        available=available,
        line_coverage=float(coverage.line_coverage),  # type: ignore[attr-defined]
        branch_coverage=float(coverage.branch_coverage),  # type: ignore[attr-defined]
        threshold=coverage.threshold,  # type: ignore[attr-defined]
        scope=getattr(coverage, "scope", None),
        evidence=None,
    )


def _bucket_defects(analysis: FailureAnalysis | None) -> ReportDefects:
    product: list[ReportDefect] = []
    test: list[ReportDefect] = []
    environment: list[ReportDefect] = []
    for failure in analysis.failures if analysis else []:
        defect = ReportDefect(case_id=failure.case_id, category=failure.category, diagnosis=failure.diagnosis)
        if failure.category in _PRODUCT:
            product.append(defect)
        elif failure.category in _ENVIRONMENT:
            environment.append(defect)
        else:
            test.append(defect)
    return ReportDefects(product=product, test=test, environment=environment)


def _risk(
    gate: QualityGateResultLike,
    defects: ReportDefects,
    *,
    final_status: str,
    coverage_status: str,
) -> tuple[str, str]:
    if defects.product:
        return (
            "HIGH",
            f"Detected {len(defects.product)} product-level defect(s); product behaviour is incorrect.",
        )
    if final_status == "FAIL":
        return "HIGH", "Functional gate failed — one or more selected test targets did not pass."
    if final_status == "PASS_WITH_WARNINGS":
        unmapped = gate.dimensions.functional.unmapped_tests or 0
        reasons: list[str] = []
        if coverage_status == "PASS_WITH_WARNINGS":
            reasons.append("coverage below threshold")
        if unmapped > 0:
            reasons.append(f"{unmapped} executed test(s) not traceable to case IDs")
        if defects.environment:
            reasons.append(f"{len(defects.environment)} environment issue(s)")
        if defects.test:
            reasons.append(f"{len(defects.test)} test-level issue(s)")
        level = "MEDIUM" if (unmapped > 0 or defects.test or defects.environment) else "LOW"
        return level, f"All functional tests passed; warnings: {', '.join(reasons) or 'none'}."
    if final_status == "SKIPPED":
        return "MEDIUM", "No test targets ran — quality cannot be assessed."
    return "LOW", "All dimensions passed with no defects."


def _recommendation(status: str, defects: ReportDefects) -> str:
    if status == "FAIL":
        return "Do not release: failing tests or hard blockers must be resolved first."
    if defects.product:
        return "Release only with caution — unresolved product defects exist; track them as known issues."
    if status == "PASS_WITH_WARNINGS":
        return "Release is acceptable but address the noted warnings (e.g. coverage)."
    if status == "SKIPPED":
        return "No tests ran — run the suite before deciding on release."
    return "Safe to release."


def _empty_issue_report(
    *, analysis_status: str = "failed", project_sync_status: str = "pending"
) -> IssueReport:
    return IssueReport(
        analysis_status=analysis_status,
        project_sync_status=project_sync_status,
        total_occurrences=0,
        counts_by_status={},
        counts_by_classification={},
        counts_by_severity={},
        new_count=0,
        repeated_count=0,
        regressed_count=0,
        resolved_count=0,
        accepted_risk_count=0,
        not_an_issue_count=0,
        issue_risk="unknown",
        issue_risk_rationale="Issue analysis failed or incomplete",
    )


def _issue_report(
    snapshot_raw: dict[str, Any] | None,
    problems_raw: dict[str, Any] | None,
) -> IssueReport:
    if snapshot_raw is None:
        return _empty_issue_report()
    snapshot = ChangeIssueSnapshot.model_validate(snapshot_raw)
    projection = ProblemProjection.model_validate(problems_raw) if problems_raw else None
    analysis_status = snapshot.analysis_status.status if snapshot.analysis_status else "failed"
    if analysis_status != "completed" or snapshot.project_sync_status == "pending" or projection is None:
        return IssueReport(
            analysis_status=analysis_status,
            project_sync_status=snapshot.project_sync_status,
            total_occurrences=len(snapshot.occurrences),
            counts_by_status={},
            counts_by_classification={},
            counts_by_severity={},
            new_count=0,
            repeated_count=0,
            regressed_count=0,
            resolved_count=0,
            accepted_risk_count=0,
            not_an_issue_count=0,
            issue_risk="unknown",
            issue_risk_rationale="Issue analysis failed or incomplete",
        )
    change_occ = {item.occurrence_id for item in snapshot.occurrences}
    linked = {
        problem.problem_id: problem
        for problem in projection.problems
        if any(occ_id in change_occ for occ_id in problem.occurrences)
    }
    counts_by_status: dict[str, int] = {}
    counts_by_classification: dict[str, int] = {}
    counts_by_severity: dict[str, int] = {}
    active: list[str] = []
    new_count = repeated_count = resolved_count = accepted_risk_count = not_an_issue_count = 0
    for problem in linked.values():
        counts_by_status[problem.status] = counts_by_status.get(problem.status, 0) + 1
        counts_by_classification[problem.assessment.classification] = (
            counts_by_classification.get(problem.assessment.classification, 0) + 1
        )
        if problem.status in _ACTIVE:
            counts_by_severity[problem.assessment.severity] = (
                counts_by_severity.get(problem.assessment.severity, 0) + 1
            )
            active.append(problem.assessment.severity)
        if problem.status == "accepted_risk":
            accepted_risk_count += 1
        elif problem.status == "not_an_issue":
            not_an_issue_count += 1
        elif problem.status == "resolved":
            resolved_count += 1
    for occurrence in snapshot.occurrences:
        problem = linked.get(occurrence.problem_id)
        if problem is None:
            continue
        if problem.first_seen.occurrence_id == occurrence.occurrence_id:
            new_count += 1
        else:
            repeated_count += 1
    if not active:
        issue_risk = "clear"
        rationale = "No active Issues — all resolved or not-an-issue."
    else:
        highest = max(active, key=lambda item: _SEVERITY_RANK.get(item, 0))
        issue_risk = highest
        rationale = f"{len(active)} active issue(s); highest severity: {highest}."
    return IssueReport(
        analysis_status=analysis_status,
        project_sync_status=snapshot.project_sync_status,
        total_occurrences=len(snapshot.occurrences),
        counts_by_status=counts_by_status,
        counts_by_classification=counts_by_classification,
        counts_by_severity=counts_by_severity,
        new_count=new_count,
        repeated_count=repeated_count,
        regressed_count=0,
        resolved_count=resolved_count,
        accepted_risk_count=accepted_risk_count,
        not_an_issue_count=not_an_issue_count,
        issue_risk=issue_risk,  # type: ignore[arg-type]
        issue_risk_rationale=rationale,
    )


def build_quality_report(payload: GenerateReportInputV1) -> QualityReport:
    gate = load_quality_gate_result_document(payload.quality_gate)
    analysis = FailureAnalysis.model_validate(payload.analysis) if payload.analysis else None
    functional = gate.dimensions.functional
    coverage = _report_coverage(gate.dimensions.coverage)
    non_functional = gate.dimensions.non_functional
    report_final = worst_status(
        [
            functional.status,
            coverage.status,
            non_functional.status if non_functional is not None else "SKIPPED",
        ]
    )
    score, breakdown = compute_quality_score(_dimensions(gate))
    defects = _bucket_defects(analysis)
    risk_level, risk_rationale = _risk(
        gate,
        defects,
        final_status=report_final,
        coverage_status=coverage.status,
    )
    return QualityReport(
        schema_version="1.1",
        change_id=payload.change_id,
        batch_id=gate.batch_id or payload.batch_id,
        final_status=report_final,  # type: ignore[arg-type]
        quality_score=score,
        score_breakdown=breakdown,
        scope=payload.scope,
        functional=functional,
        coverage=coverage,
        defects=defects,
        risk_level=risk_level,  # type: ignore[arg-type]
        risk_rationale=risk_rationale,
        recommendation=_recommendation(report_final, defects),
        started_at=payload.started_at,
        duration=payload.duration,
        non_functional=non_functional,
        issues=_issue_report(payload.snapshot, payload.problems),
        metrics=payload.metrics,
    )


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def render_quality_report_markdown(raw: Mapping[str, object]) -> bytes:
    """Render the typed report as stable, human-readable Markdown."""

    report = QualityReport.model_validate(raw)
    source_raw = raw.get("source_digests")
    if not isinstance(source_raw, Mapping):
        raise ValueError("quality report source_digests are missing")
    expected = ("execution", "healing", "trace", "coverage", "metrics")
    source_digests: dict[str, str] = {}
    for name in expected:
        digest = source_raw.get(name)
        if not isinstance(digest, str) or re.fullmatch(_SHA256, digest) is None:
            raise ValueError(f"quality report {name} digest is invalid")
        source_digests[name] = digest
    lines = [
        "# Quality Report",
        "",
        f"- Change: {_one_line(report.change_id)}",
        f"- Batch: {_one_line(report.batch_id)}",
        f"- Final status: {report.final_status}",
        f"- Quality score: {report.quality_score:g}",
        "",
        "## Scope",
        "",
        f"- Cases: {report.scope.cases}",
        "- Requirements: "
        + (", ".join(_one_line(item) for item in report.scope.requirements) or "not collected"),
        "",
        "## Dimensions",
        "",
        f"- Functional: {report.functional.status}",
        f"- Coverage: {report.coverage.status}",
        "- Non-functional: "
        + (report.non_functional.status if report.non_functional is not None else "not collected"),
        "",
        "## Risk",
        "",
        f"- Level: {report.risk_level}",
        f"- Rationale: {_one_line(report.risk_rationale)}",
        f"- Recommendation: {_one_line(report.recommendation)}",
        "",
        "## Authenticated sources",
        "",
    ]
    labels = {
        "execution": "Execution",
        "healing": "Healing",
        "trace": "Trace",
        "coverage": "Coverage",
        "metrics": "Metrics",
    }
    lines.extend(f"- {labels[name]}: {source_digests[name]}" for name in expected)
    return ("\n".join(lines) + "\n").encode("utf-8")


class GenerateReportHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(GenerateReportInputV1, request.input)
            report = build_quality_report(payload)
            output = report.model_dump(mode="json")
            output["source_digests"] = {
                "execution": payload.execution_digest,
                "healing": payload.healing_digest,
                "trace": payload.trace_digest,
                "coverage": payload.coverage_digest,
                "metrics": payload.metrics_digest,
            }
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except InputError as error:
            return failed_input(error)
        except (ValueError, TypeError) as error:
            return failed_input(InputError(str(error)))


class DashboardHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(DashboardInputV1, request.input)
            report = QualityReport.model_validate(payload.report)
            if report.change_id != payload.change_id:
                raise InputError("dashboard report change_id does not match")
            projection = {
                "schema_version": "1.0",
                "change_id": payload.change_id,
                "batch_id": payload.batch_id,
                "final_status": report.final_status,
                "quality_score": report.quality_score,
                "issue_risk": report.issues.issue_risk,
                "metrics_digest": payload.metrics_digest,
                "report_digest": payload.report_digest,
            }
            return TaskOutcome.succeeded(cast(JSONValue, projection))
        except InputError as error:
            return failed_input(error)
        except (ValueError, TypeError) as error:
            return failed_input(InputError(str(error)))
