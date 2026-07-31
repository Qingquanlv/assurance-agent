"""Deterministic Quality Report trio (quality-report.json/.md + executive-summary.md).

Consumes the inspect artifacts + execution evidence, scores the run, buckets
defects, and derives risk/recommendation. CLI is the only trusted scorer.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from assurance_agent.artifacts.models import (
    ChangeIssueSnapshot,
    FailureAnalysis,
    IssueReconcileStatus,
    IssueReport,
    ProblemProjection,
    QualityGateResultLike,
    QualityReport,
    ReportDefect,
    ReportDefects,
    ReportRiskLevel,
    ReportScope,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.workflow.core.events import Ledger
from assurance_agent.workflow.execution.evidence import load_execution_evidence
from assurance_agent.workflow.issues.events import (
    LedgerIntegrityError,
    ProblemRegressedEvent,
    read_problem_events,
)
from assurance_agent.workflow.report.quality_gate import (
    load_quality_gate_result_file,
    quality_gate_legacy_view,
)
from assurance_agent.workflow.report.quality_score import ScoreDimension, compute_quality_score

_NO_DATA = "No data"

_PRODUCT = {
    "business_logic_failure",
    "fuzz_stateful_failure",
    "known_product_issue",
    "perf_threshold_exceeded",
}
_ENVIRONMENT = {"environment_failure", "perf_environment"}

# Issue statuses that are considered active (not resolved/dismissed).
# accepted_risk is active — the risk exists even though it has been acknowledged.
_ACTIVE_PROBLEM_STATUSES = frozenset(
    {"detected", "triaged", "in_progress", "verification_pending", "accepted_risk"}
)
_SEVERITY_RANK: dict[str, int] = {"critical": 4, "high": 3, "medium": 2, "low": 1}

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _regressed_occurrence_ids(project_root: Path, change_id: str) -> set[str]:
    events_path = project_root / "qa" / "issues" / "events.jsonl"
    if not events_path.is_file():
        return set()
    try:
        events = read_problem_events(events_path)
    except (OSError, LedgerIntegrityError):
        return set()
    return {
        event.occurrence_id
        for event in events
        if isinstance(event, ProblemRegressedEvent) and event.change_id == change_id
    }


class GenerateReportResult(BaseModel):
    report: QualityReport
    json_path: str
    md_path: str
    exec_summary_path: str


def generate_report(project_root: Path, change_id: str) -> GenerateReportResult:
    change_base = resolve_change(project_root, change_id).path
    inspect_dir = change_base / "inspect"
    report_dir = change_base / "report"

    evidence = load_execution_evidence(change_base / "execution")
    gate = load_quality_gate_result_file(inspect_dir / "quality-gate-result.json")
    if gate is None:
        raise FileNotFoundError(
            f"quality-gate-result.json not found for '{change_id}'. Run `aa report inspect` first."
        )
    analysis = _load(inspect_dir / "failure-analysis.json", FailureAnalysis)

    score, breakdown = compute_quality_score(_dimensions(gate))
    defects = _bucket_defects(analysis)
    risk_level, risk_rationale = _risk(gate, defects)
    recommendation = _recommendation(gate.final_status, defects)
    started_at, duration = _execution_timing(change_base)
    issue_report = _derive_issue_report(change_base, project_root)
    functional, coverage, non_functional = quality_gate_legacy_view(gate)

    report = QualityReport(
        schema_version="1.1",
        change_id=change_id,
        batch_id=gate.batch_id or evidence.batch_id,
        final_status=gate.final_status,
        quality_score=score,
        score_breakdown=breakdown,
        scope=_scope(change_base),
        functional=functional,
        coverage=coverage,
        defects=defects,
        risk_level=risk_level,
        risk_rationale=risk_rationale,
        recommendation=recommendation,
        started_at=started_at,
        duration=duration,
        non_functional=non_functional,
        issues=issue_report,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "quality-report.json"
    md_path = report_dir / "quality-report.md"
    exec_path = report_dir / "executive-summary.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(_report_md(report), encoding="utf-8")
    exec_path.write_text(_exec_summary(report), encoding="utf-8")
    return GenerateReportResult(
        report=report,
        json_path=str(json_path),
        md_path=str(md_path),
        exec_summary_path=str(exec_path),
    )


def _derive_issue_report(change_base: Path, project_root: Path) -> IssueReport | None:
    """Load Change Issue snapshot and Project Problem projection; derive IssueReport.

    Returns None when no Issue data exists (no snapshot and no reconcile status).
    Never raises; a corrupt/missing file yields ``unknown`` risk rather than an
    exception. Reading a 1.0 report does not look up any legacy Issue Markdown files.

    Failed ``issue-reconcile-status.json`` is fail-visible even when the canonical
    snapshot was never written: archive/report must not treat that as clear.
    """
    reconcile_status = _load(
        change_base / "inspect" / "issue-reconcile-status.json",
        IssueReconcileStatus,
    )
    snapshot = _load(change_base / "issues" / "snapshot.json", ChangeIssueSnapshot)
    if snapshot is None and reconcile_status is None:
        return None

    problems_path = project_root / "qa" / "issues" / "problems.json"
    projection = _load(problems_path, ProblemProjection)

    if snapshot is None:
        # Semantic rejection / incomplete reconcile with no canonical analysis.
        return IssueReport(
            analysis_status="failed",
            project_sync_status="pending",
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
            issue_risk_rationale=(
                "Issue reconciliation failed or incomplete"
                if reconcile_status is not None and reconcile_status.status == "failed"
                else "Issue analysis failed or incomplete"
            ),
        )

    if reconcile_status is not None and reconcile_status.status == "failed":
        analysis_status_val = "failed"
        project_sync_status_val = snapshot.project_sync_status
        return _build_issue_report(
            snapshot,
            projection,
            analysis_status_val,
            project_sync_status_val,
            "unknown",
            "Issue reconciliation failed or incomplete",
        )

    analysis_status_val = (
        snapshot.analysis_status.status if snapshot.analysis_status is not None else "failed"
    )
    project_sync_status_val = snapshot.project_sync_status

    # Derive risk first so we can return early on unknown.
    if analysis_status_val != "completed" or project_sync_status_val == "pending":
        issue_risk: str = "unknown"
        issue_risk_rationale = (
            "Issue analysis failed or incomplete"
            if analysis_status_val != "completed"
            else "Project synchronization pending"
        )
        return _build_issue_report(
            snapshot,
            projection,
            analysis_status_val,
            project_sync_status_val,
            issue_risk,
            issue_risk_rationale,
        )

    if projection is None:
        return _build_issue_report(
            snapshot,
            projection,
            analysis_status_val,
            project_sync_status_val,
            "unknown",
            "Project Problem projection is missing or corrupt",
        )

    # Aggregate counts from problems linked to this change's occurrences.
    change_occ_ids = {occ.occurrence_id for occ in snapshot.occurrences}
    counts_by_status: dict[str, int] = {}
    counts_by_classification: dict[str, int] = {}
    counts_by_severity: dict[str, int] = {}
    active_severities: list[str] = []
    new_count = repeated_count = regressed_count = 0
    resolved_count = accepted_risk_count = not_an_issue_count = 0
    linked_problems = {
        prob.problem_id: prob
        for prob in projection.problems
        if any(occ_id in change_occ_ids for occ_id in prob.occurrences)
    }
    regressed_occurrence_ids = _regressed_occurrence_ids(project_root, snapshot.change_id)

    for prob in linked_problems.values():
        st = prob.status
        counts_by_status[st] = counts_by_status.get(st, 0) + 1
        cls = prob.assessment.classification
        counts_by_classification[cls] = counts_by_classification.get(cls, 0) + 1
        sev = prob.assessment.severity
        if st in _ACTIVE_PROBLEM_STATUSES:
            counts_by_severity[sev] = counts_by_severity.get(sev, 0) + 1
            active_severities.append(sev)
        if st == "accepted_risk":
            accepted_risk_count += 1
        elif st == "not_an_issue":
            not_an_issue_count += 1
        elif st == "resolved":
            resolved_count += 1

    # A Problem is new only for the occurrence that created it. Exact links to
    # any existing Problem (including an earlier batch of the same Change) are
    # repeated occurrences.
    for occurrence in snapshot.occurrences:
        problem = linked_problems.get(occurrence.problem_id)
        if problem is None:
            continue
        if occurrence.occurrence_id in regressed_occurrence_ids:
            regressed_count += 1
        elif problem.first_seen.occurrence_id == occurrence.occurrence_id:
            new_count += 1
        else:
            repeated_count += 1

    if not active_severities:
        issue_risk = "clear"
        issue_risk_rationale = "No active Issues — all resolved or not-an-issue."
    else:
        highest = max(active_severities, key=lambda s: _SEVERITY_RANK.get(s, 0))
        issue_risk = highest
        active_count = len(active_severities)
        issue_risk_rationale = f"{active_count} active issue(s); highest severity: {highest}."
        if accepted_risk_count:
            issue_risk_rationale += f" ({accepted_risk_count} accepted_risk — still active)."

    return _build_issue_report(
        snapshot,
        projection,
        analysis_status_val,
        project_sync_status_val,
        issue_risk,
        issue_risk_rationale,
        counts_by_status=counts_by_status,
        counts_by_classification=counts_by_classification,
        counts_by_severity=counts_by_severity,
        new_count=new_count,
        repeated_count=repeated_count,
        regressed_count=regressed_count,
        resolved_count=resolved_count,
        accepted_risk_count=accepted_risk_count,
        not_an_issue_count=not_an_issue_count,
    )


def _build_issue_report(
    snapshot: ChangeIssueSnapshot,
    projection: ProblemProjection | None,
    analysis_status: str,
    project_sync_status: str,
    issue_risk: str,
    issue_risk_rationale: str,
    *,
    counts_by_status: dict[str, int] | None = None,
    counts_by_classification: dict[str, int] | None = None,
    counts_by_severity: dict[str, int] | None = None,
    new_count: int = 0,
    repeated_count: int = 0,
    regressed_count: int = 0,
    resolved_count: int = 0,
    accepted_risk_count: int = 0,
    not_an_issue_count: int = 0,
) -> IssueReport:
    return IssueReport(
        analysis_status=analysis_status,
        project_sync_status=project_sync_status,
        total_occurrences=len(snapshot.occurrences),
        counts_by_status=counts_by_status or {},
        counts_by_classification=counts_by_classification or {},
        counts_by_severity=counts_by_severity or {},
        new_count=new_count,
        repeated_count=repeated_count,
        regressed_count=regressed_count,
        resolved_count=resolved_count,
        accepted_risk_count=accepted_risk_count,
        not_an_issue_count=not_an_issue_count,
        issue_risk=issue_risk,  # type: ignore[arg-type]
        issue_risk_rationale=issue_risk_rationale,
    )


def _dimensions(gate: QualityGateResultLike) -> dict[str, ScoreDimension]:
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
    weights = (
        {"functional": 50, "coverage": 20, "fuzz": 15, "performance": 15}
        if m3
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


def _risk(gate: QualityGateResultLike, defects: ReportDefects) -> tuple[ReportRiskLevel, str]:
    if defects.product:
        return (
            "HIGH",
            f"Detected {len(defects.product)} product-level defect(s); product behaviour is incorrect.",
        )
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


def _execution_timing(change_base: Path) -> tuple[str | None, str | None]:
    """Derive Start / Duration for the latest primary execution from the ledger.

    Prefer ``dispatch_signed`` → ``phase_outcome_committed`` for phase
    ``execution`` (not healing-rerun). Missing either side → None ("No data").
    """
    ledger = Ledger(change_base)
    dispatch = ledger.latest(type="dispatch_signed", phase="execution")
    if dispatch is None:
        dispatch = ledger.latest(type="dispatch_phase", phase="execution")
    outcome = ledger.latest(type="phase_outcome_committed", phase="execution")
    start_raw = None
    if dispatch is not None:
        start_raw = dispatch.get("ts") or (
            _ms_to_iso(dispatch.get("dispatched_at")) if dispatch.get("dispatched_at") is not None else None
        )
    end_raw = outcome.get("ts") if outcome is not None else None
    if not isinstance(start_raw, str) or not start_raw.strip():
        return None, None
    started_at = start_raw.strip()
    if not isinstance(end_raw, str) or not end_raw.strip():
        return started_at, None
    duration = _format_duration(started_at, end_raw.strip())
    return started_at, duration


def _ms_to_iso(value: object) -> str | None:
    try:
        ms = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0).astimezone().isoformat()


def _format_duration(started: str, ended: str) -> str | None:
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = max(0, int((b - a).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _report_md(r: QualityReport) -> str:
    cov = r.coverage
    lines = [
        f"# Quality Report — {r.change_id}",
        "",
        f"- **Batch**: {r.batch_id or '(unknown)'}",
        f"- **Start**: {r.started_at or _NO_DATA}",
        f"- **Duration**: {r.duration or _NO_DATA}",
        f"- **Final Status**: {r.final_status}",
        f"- **Quality Score**: {r.quality_score} / 100",
        f"- **Risk Level**: {r.risk_level}",
        "",
        "## Score Breakdown",
        "",
        "| Dimension | Points |",
        "|-----------|--------|",
        f"| Functional | {_fmt(r.score_breakdown.functional)} |",
        f"| Coverage | {_fmt(r.score_breakdown.coverage)} |",
        f"| Fuzz | {_fmt(r.score_breakdown.fuzz)} |",
        f"| Performance | {_fmt(r.score_breakdown.performance)} |",
        "",
        "## Scope",
        "",
        f"- Cases: {r.scope.cases}",
        f"- Requirements: {', '.join(r.scope.requirements) or '(none detected)'}",
        "",
        "## Functional",
        "",
        f"- API: total={r.functional.api.total} passed={r.functional.api.passed} failed={r.functional.api.failed}",
        f"- E2E: total={r.functional.e2e.total} passed={r.functional.e2e.passed} failed={r.functional.e2e.failed}",
        "",
        "## Coverage",
        "",
        (
            f"- Line: {cov.line_coverage}% (threshold {cov.threshold.line}%)\n"
            f"- Branch: {cov.branch_coverage}% (threshold {cov.threshold.branch}%)\n- Status: {cov.status}"
            if cov.available
            else "- Not collected (treated as a warning, not a failure)."
        ),
        "",
        "## Defects",
        "",
        f"- Product: {len(r.defects.product)}",
        f"- Test: {len(r.defects.test)}",
        f"- Environment: {len(r.defects.environment)}",
        "",
        *_defect_section("Product Defects", r.defects.product),
        *_defect_section("Test Defects", r.defects.test),
        *_defect_section("Environment Defects", r.defects.environment),
        "## Risk & Recommendation",
        "",
        f"- **Risk Level**: {r.risk_level}",
        f"- **Rationale**: {r.risk_rationale}",
        f"- **Recommendation**: {r.recommendation}",
        "",
        *_issue_risk_section(r),
    ]
    return "\n".join(lines)


def _issue_risk_section(r: QualityReport) -> list[str]:
    """Render the Issue Risk section (omitted when no Issue data)."""
    if r.issues is None:
        return []
    ir = r.issues
    lines = [
        "## Issue Risk",
        "",
        f"- **Issue Risk**: {ir.issue_risk}",
        f"- **Rationale**: {ir.issue_risk_rationale}",
        f"- **Analysis Status**: {ir.analysis_status}",
        f"- **Project Sync**: {ir.project_sync_status}",
        f"- **Occurrences**: {ir.total_occurrences}",
    ]
    if ir.counts_by_status:
        lines.append(
            "- **By Status**: " + ", ".join(f"{k}={v}" for k, v in sorted(ir.counts_by_status.items()))
        )
    lines.append("")
    return lines


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
    coverage = (
        f" Coverage: {r.coverage.line_coverage}% line."
        if r.coverage.available
        else " Coverage: not collected."
    )
    issue_line = (
        f"**Issue Risk**: {r.issues.issue_risk} — {r.issues.issue_risk_rationale}"
        if r.issues is not None
        else ""
    )
    lines = [
        f"# Executive Summary — {r.change_id}",
        "",
        f"**Final Status**: {r.final_status}  |  **Quality Score**: {r.quality_score}/100  |  **Risk**: {r.risk_level}",
        "",
        f"**Start**: {r.started_at or _NO_DATA}  |  **Duration**: {r.duration or _NO_DATA}",
        "",
        r.risk_rationale,
        "",
        f"**Recommendation**: {r.recommendation}",
        "",
        f"Functional: {passed}/{total} passed.{coverage}",
        "",
    ]
    if issue_line:
        lines.extend([issue_line, ""])
    return "\n".join(lines)
