"""Deterministic Quality Report trio (quality-report.json/.md + executive-summary.md).

Consumes the inspect artifacts + execution evidence, scores the run, buckets
defects, and derives risk/recommendation. CLI is the only trusted scorer.
"""
import re
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    FailureAnalysis,
    QualityGateResult,
    QualityReport,
    ReportDefect,
    ReportDefects,
    ReportRiskLevel,
    ReportScope,
)
from assurance_agent.identifiers import assert_change_id_safe
from assurance_agent.workflow.execution.evidence import load_execution_evidence
from assurance_agent.workflow.report.quality_score import ScoreDimension, compute_quality_score

_PRODUCT = {"business_logic_failure", "fuzz_stateful_failure", "perf_threshold_exceeded"}
_ENVIRONMENT = {"environment_failure", "perf_environment"}

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class GenerateReportResult(BaseModel):
    report: QualityReport
    json_path: str
    md_path: str
    exec_summary_path: str


def generate_report(project_root: Path, change_id: str) -> GenerateReportResult:
    assert_change_id_safe(change_id)
    change_base = project_root / "qa" / "changes" / change_id
    inspect_dir = change_base / "inspect"
    report_dir = change_base / "report"

    evidence = load_execution_evidence(change_base / "execution")
    gate = _load(inspect_dir / "quality-gate-result.json", QualityGateResult)
    if gate is None:
        raise FileNotFoundError(
            f"quality-gate-result.json not found for '{change_id}'. Run `aa report inspect` first."
        )
    analysis = _load(inspect_dir / "failure-analysis.json", FailureAnalysis)

    score, breakdown = compute_quality_score(_dimensions(gate))
    defects = _bucket_defects(analysis)
    risk_level, risk_rationale = _risk(gate, defects)
    recommendation = _recommendation(gate.final_status, defects)

    report = QualityReport(
        schema_version="1.0", change_id=change_id, batch_id=gate.batch_id or evidence.batch_id,
        final_status=gate.final_status, quality_score=score, score_breakdown=breakdown,
        scope=_scope(change_base), functional=gate.dimensions.functional,
        coverage=gate.dimensions.coverage, defects=defects, risk_level=risk_level,
        risk_rationale=risk_rationale, recommendation=recommendation,
        non_functional=gate.dimensions.non_functional,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "quality-report.json"
    md_path = report_dir / "quality-report.md"
    exec_path = report_dir / "executive-summary.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(_report_md(report), encoding="utf-8")
    exec_path.write_text(_exec_summary(report), encoding="utf-8")
    return GenerateReportResult(
        report=report, json_path=str(json_path), md_path=str(md_path), exec_summary_path=str(exec_path),
    )


def _dimensions(gate: QualityGateResult) -> dict[str, ScoreDimension]:
    func = gate.dimensions.functional
    func_total = func.api.total + func.e2e.total
    func_passed = func.api.passed + func.e2e.passed
    cov = gate.dimensions.coverage
    cov_ratio = cov.line_coverage / cov.threshold.line if (cov.available and cov.threshold.line > 0) else 0.0

    fuzz_active = bool(func.fuzz and func.fuzz.total > 0)
    fuzz_ratio = (func.fuzz.passed / func.fuzz.total) if fuzz_active and func.fuzz else 0.0

    non_func = gate.dimensions.non_functional
    ran = [s for s in non_func.performance if s.verdict != "SKIPPED"] if non_func else []
    perf_active = bool(non_func and non_func.status != "SKIPPED" and ran)
    perf_ratio = (sum(1 for s in ran if s.verdict == "PASS") / len(ran)) if perf_active else 0.0

    m3 = fuzz_active or perf_active
    weights = ({"functional": 50, "coverage": 20, "fuzz": 15, "performance": 15}
               if m3 else {"functional": 70, "coverage": 30, "fuzz": 0, "performance": 0})
    return {
        "functional": ScoreDimension(active=func_total > 0,
                                     ratio=(func_passed / func_total) if func_total > 0 else 0.0,
                                     weight=weights["functional"]),
        "coverage": ScoreDimension(active=cov.available, ratio=cov_ratio, weight=weights["coverage"]),
        "fuzz": ScoreDimension(active=fuzz_active, ratio=fuzz_ratio, weight=weights["fuzz"]),
        "performance": ScoreDimension(active=perf_active, ratio=perf_ratio, weight=weights["performance"]),
    }


def _bucket_defects(analysis: FailureAnalysis | None) -> ReportDefects:
    product: list[ReportDefect] = []
    test: list[ReportDefect] = []
    environment: list[ReportDefect] = []
    for failure in (analysis.failures if analysis else []):
        defect = ReportDefect(case_id=failure.case_id, category=failure.category, diagnosis=failure.diagnosis)
        if failure.category in _PRODUCT:
            product.append(defect)
        elif failure.category in _ENVIRONMENT:
            environment.append(defect)
        else:
            test.append(defect)
    return ReportDefects(product=product, test=test, environment=environment)


def _risk(gate: QualityGateResult, defects: ReportDefects) -> tuple[ReportRiskLevel, str]:
    if defects.product:
        return "HIGH", f"Detected {len(defects.product)} product-level defect(s); product behaviour is incorrect."
    status = gate.final_status
    if status == "FAIL":
        return "HIGH", "Functional gate failed — one or more selected test targets did not pass."
    if status == "PASS_WITH_WARNINGS":
        unmapped = gate.dimensions.functional.unmapped_tests or 0
        reasons = []
        if gate.dimensions.coverage.status == "PASS_WITH_WARNINGS":
            reasons.append("coverage below threshold")
        if unmapped > 0:
            reasons.append(f"{unmapped} executed test(s) not traceable to case IDs")
        if defects.environment:
            reasons.append(f"{len(defects.environment)} environment issue(s)")
        if defects.test:
            reasons.append(f"{len(defects.test)} test-level issue(s)")
        level: ReportRiskLevel = "MEDIUM" if (unmapped > 0 or defects.test or defects.environment) else "LOW"
        return level, f"All functional tests passed; warnings: {', '.join(reasons) or 'none'}."
    if status == "SKIPPED":
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


_CASE_ID_RE = re.compile(r"^\s*case_id\s*:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)
_REQ_RE = re.compile(r"requirement_id\s*:\s*[\"']?([A-Za-z0-9_-]+)")


def _scope(change_base: Path) -> ReportScope:
    cases_dir = change_base / "cases"
    case_ids: set[str] = set()
    requirements: set[str] = set()
    if cases_dir.is_dir():
        for path in cases_dir.rglob("*.y*ml"):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            case_ids.update(_CASE_ID_RE.findall(content))
            requirements.update(_REQ_RE.findall(content))
    return ReportScope(cases=len(case_ids), requirements=sorted(requirements))


def _load(path: Path, model: type[_ModelT]) -> _ModelT | None:
    if not path.is_file():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fmt(value: float | str) -> str:
    return "N/A" if value == "N/A" else str(value)


def _report_md(r: QualityReport) -> str:
    cov = r.coverage
    lines = [
        f"# Quality Report — {r.change_id}", "",
        f"- **Batch**: {r.batch_id or '(unknown)'}",
        f"- **Final Status**: {r.final_status}",
        f"- **Quality Score**: {r.quality_score} / 100",
        f"- **Risk Level**: {r.risk_level}", "",
        "## Score Breakdown", "",
        "| Dimension | Points |", "|-----------|--------|",
        f"| Functional | {_fmt(r.score_breakdown.functional)} |",
        f"| Coverage | {_fmt(r.score_breakdown.coverage)} |",
        f"| Fuzz | {_fmt(r.score_breakdown.fuzz)} |",
        f"| Performance | {_fmt(r.score_breakdown.performance)} |", "",
        "## Scope", "",
        f"- Cases: {r.scope.cases}",
        f"- Requirements: {', '.join(r.scope.requirements) or '(none detected)'}", "",
        "## Functional", "",
        f"- API: total={r.functional.api.total} passed={r.functional.api.passed} failed={r.functional.api.failed}",
        f"- E2E: total={r.functional.e2e.total} passed={r.functional.e2e.passed} failed={r.functional.e2e.failed}",
        "", "## Coverage", "",
        (f"- Line: {cov.line_coverage}% (threshold {cov.threshold.line}%)\n"
         f"- Branch: {cov.branch_coverage}% (threshold {cov.threshold.branch}%)\n- Status: {cov.status}"
         if cov.available else "- Not collected (treated as a warning, not a failure)."),
        "", "## Defects", "",
        f"- Product: {len(r.defects.product)}",
        f"- Test: {len(r.defects.test)}",
        f"- Environment: {len(r.defects.environment)}", "",
        *_defect_section("Product Defects", r.defects.product),
        *_defect_section("Test Defects", r.defects.test),
        *_defect_section("Environment Defects", r.defects.environment),
        "## Risk & Recommendation", "",
        f"- **Risk Level**: {r.risk_level}",
        f"- **Rationale**: {r.risk_rationale}",
        f"- **Recommendation**: {r.recommendation}", "",
    ]
    return "\n".join(lines)


def _defect_section(title: str, defects: list[ReportDefect]) -> list[str]:
    if not defects:
        return []
    out = [f"### {title}", ""]
    out += [f"- `{d.case_id}` ({d.category}): {d.diagnosis}" for d in defects]
    out.append("")
    return out


def _exec_summary(r: QualityReport) -> str:
    func = r.functional
    passed = func.api.passed + func.e2e.passed
    total = func.api.total + func.e2e.total
    coverage = f" Coverage: {r.coverage.line_coverage}% line." if r.coverage.available else " Coverage: not collected."
    return "\n".join([
        f"# Executive Summary — {r.change_id}", "",
        f"**Final Status**: {r.final_status}  |  **Quality Score**: {r.quality_score}/100  |  **Risk**: {r.risk_level}",
        "", r.risk_rationale, "",
        f"**Recommendation**: {r.recommendation}", "",
        f"Functional: {passed}/{total} passed.{coverage}", "",
    ])
