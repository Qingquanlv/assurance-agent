"""Deterministic inspect: classify closed execution evidence and publish a gate."""

from __future__ import annotations

import re
from typing import Literal, cast

from pydantic import Field

from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import FrozenModel, TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.common import CoverageThreshold, FunctionalCounts, FunctionalDimension
from assurance_quality.contracts.inspect import (
    CoverageDimensionV2,
    CoverageGapEntry,
    EvidenceCoverageErrorV2,
    FailureAnalysis,
    FailureEntry,
    FailureEvidence,
    QualityGateDimensionsV2,
    QualityGateResultV2,
)
from assurance_quality.operations.common import InputError, failed_input, validate_input

TargetName = Literal["api", "e2e", "fuzz", "performance", "coverage"]
CaseStatus = Literal["passed", "failed", "skipped"]

_SHA256 = r"^[0-9a-f]{64}$"

_DEFAULT_RULES: tuple[tuple[str, str, bool | Literal["review"], str, bool], ...] = (
    (r"locator|selector|no such element|not found: \.", "locator_failure", True, "medium", False),
    (r"timeout|waited|waiting for", "wait_strategy_failure", True, "medium", False),
    (r"syntaxerror|nameerror|importerror|module not found", "test_code_error", True, "medium", False),
    (r"fixture|seed data|testdata", "test_data_failure", "review", "medium", True),
    (r"connection refused|econnrefused|network|unreachable", "environment_failure", False, "high", False),
    (r"assertionerror|assert |expected .+ got", "assertion_failure", False, "medium", False),
    (r"status.?5\d\d|internal server error|\b500\b", "business_logic_failure", False, "high", False),
)

_FUZZ_RULES: tuple[tuple[str, str, bool | Literal["review"], str, bool], ...] = (
    (r"schema|hypothesis|health-check|setup", "fuzz_configuration_error", False, "medium", False),
    (r"status.?5\d\d|internal server error|\b500\b", "fuzz_stateful_failure", "review", "high", True),
)

_DIAGNOSIS = {
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
    "manifest_asset_missing": "Execution asset missing for a selected target.",
    "coverage_gap": "Coverage is below the authenticated threshold.",
}

_ACTION = {
    "locator_failure": "Generate a Fix Proposal to update the locator strategy after review.",
    "wait_strategy_failure": "Generate a Fix Proposal to adjust wait conditions.",
    "assertion_failure": "Investigate whether product behaviour changed or the expected value is wrong.",
    "environment_failure": "Fix the environment and rerun. Do not generate a Fix Proposal.",
    "test_data_failure": "Review test fixtures and seed data.",
    "business_logic_failure": "File a product defect. This is not a test issue.",
    "known_product_issue": "Route to product tracking; do not generate a test fix.",
    "test_code_error": "Fix the syntax or import error in the test file and rerun.",
    "case_semantic_failure": "Review the case against requirements.",
    "coverage_gap": "Add or improve tests for the uncovered file. Coverage gaps are never auto-fixed.",
    "manifest_asset_missing": "Regenerate the missing execution asset before reporting.",
    "fuzz_configuration_error": "Fix the fuzz setup and rerun.",
    "fuzz_stateful_failure": "Review the server fault surfaced by generated input.",
}


class InspectCaseInputV1(FrozenModel):
    case_id: str = Field(min_length=1)
    test_name: str = Field(min_length=1)
    status: CaseStatus
    target: TargetName
    message: str = ""
    file: str = ""
    trace: str = ""
    screenshot: str = ""
    video: str = ""
    raw_log: str = ""


class UncoveredFileV1(FrozenModel):
    file: str = ""
    line_coverage: float = 0.0


class InspectCoverageInputV1(FrozenModel):
    available: bool = False
    line_coverage: float = 0.0
    branch_coverage: float = 0.0
    threshold_line: float = 0.0
    threshold_branch: float = 0.0
    uncovered_critical_files: tuple[UncoveredFileV1, ...] = ()


class InspectIntegrityIssueV1(FrozenModel):
    target: TargetName
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class InspectInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str | None = Field(default=None, pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)
    result_paths: dict[str, str] = Field(default_factory=dict)
    cases: tuple[InspectCaseInputV1, ...] = ()
    coverage: InspectCoverageInputV1 | None = None
    integrity_issues: tuple[InspectIntegrityIssueV1, ...] = ()


class Classification:
    def __init__(
        self,
        category: str,
        *,
        fix_proposal_eligible: bool,
        severity: str,
        needs_review: bool,
    ) -> None:
        self.category = category
        self.fix_proposal_eligible = fix_proposal_eligible
        self.severity = severity
        self.needs_review = needs_review


def classify_failure(*, message: str, target: str) -> Classification:
    text = message.lower()
    rules = _FUZZ_RULES if target == "fuzz" else _DEFAULT_RULES
    for pattern, category, eligible, severity, review in rules:
        if re.search(pattern, text, flags=re.IGNORECASE):
            allowed = eligible is True
            return Classification(
                category=category,
                fix_proposal_eligible=allowed,
                severity=severity,
                needs_review=review or eligible == "review" or category == "unknown",
            )
    return Classification(
        category="unknown",
        fix_proposal_eligible=False,
        severity="low",
        needs_review=True,
    )


def worst_status(statuses: list[str]) -> str:
    present = [item for item in statuses if item]
    if not present:
        return "SKIPPED"
    if "FAIL" in present:
        return "FAIL"
    if "PASS_WITH_WARNINGS" in present:
        return "PASS_WITH_WARNINGS"
    if "PASS" in present:
        return "PASS"
    return "SKIPPED"


def _counts(cases: tuple[InspectCaseInputV1, ...], target: str) -> FunctionalCounts:
    selected = [item for item in cases if item.target == target]
    return FunctionalCounts(
        total=len(selected),
        passed=sum(1 for item in selected if item.status == "passed"),
        failed=sum(1 for item in selected if item.status == "failed"),
    )


def _functional_status(*counts: FunctionalCounts) -> str:
    ran = [item for item in counts if item.total > 0]
    if not ran:
        return "SKIPPED"
    if any(item.failed > 0 for item in ran):
        return "FAIL"
    return "PASS"


def _diagnosis(message: str, category: str) -> str:
    first = (message or "").split("\n")[0][:200]
    prefix = _DIAGNOSIS.get(category)
    return f"{prefix} {first}".strip() if prefix else (first or "Unknown failure.")


def _complete(failures: list[FailureEntry]) -> None:
    for index, failure in enumerate(failures, start=1):
        failure.id = failure.id or f"FAIL-{index:03d}"
        failure.test = failure.test or failure.evidence.test_file or failure.case_id
        failure.recommended_next_action = failure.recommended_next_action or failure.recommended_action


def _classify_cases(
    cases: tuple[InspectCaseInputV1, ...],
    result_paths: dict[str, str],
    change_id: str,
) -> list[FailureEntry]:
    del change_id
    entries: list[FailureEntry] = []
    for case in cases:
        if case.status != "failed":
            continue
        classification = classify_failure(message=case.message, target=case.target)
        entries.append(
            FailureEntry(
                case_id=case.case_id or case.test_name,
                target=case.target,
                category=classification.category,  # type: ignore[arg-type]
                fix_proposal_eligible=classification.fix_proposal_eligible,
                severity=classification.severity,  # type: ignore[arg-type]
                needs_review=classification.needs_review,
                evidence=FailureEvidence(
                    result_file=result_paths.get(case.target, ""),
                    test_file=case.file,
                    trace=case.trace,
                    screenshot=case.screenshot,
                    video=case.video,
                    raw_log=case.raw_log,
                    log_excerpt=case.message[:400],
                ),
                diagnosis=_diagnosis(case.message, classification.category),
                recommended_action=_ACTION.get(classification.category, "Investigate manually."),
            )
        )
    return entries


def _integrity_failures(issues: tuple[InspectIntegrityIssueV1, ...]) -> list[FailureEntry]:
    return [
        FailureEntry(
            case_id=f"manifest:{issue.target}",
            target=issue.target,
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
            recommended_action=_ACTION["manifest_asset_missing"],
        )
        for issue in issues
    ]


def _coverage_gaps(coverage: InspectCoverageInputV1 | None) -> list[CoverageGapEntry]:
    if coverage is None or not coverage.available:
        return []
    return [
        CoverageGapEntry(
            file=item.file,
            line_coverage=item.line_coverage,
            threshold=coverage.threshold_line,
        )
        for item in coverage.uncovered_critical_files
    ]


def _build_gate(payload: InspectInputV1, functional_status: str) -> QualityGateResultV2:
    api = _counts(payload.cases, "api")
    e2e = _counts(payload.cases, "e2e")
    fuzz = _counts(payload.cases, "fuzz")
    coverage = payload.coverage
    available = bool(coverage and coverage.available)
    coverage_status = "SKIPPED" if not available else "PASS"
    if available and coverage is not None and coverage.line_coverage < coverage.threshold_line:
        coverage_status = "FAIL"
    final = worst_status([functional_status, coverage_status])
    functional = FunctionalDimension(
        status=functional_status,  # type: ignore[arg-type]
        api=api,
        e2e=e2e,
        fuzz=fuzz if fuzz.total > 0 else None,
    )
    coverage_dim = CoverageDimensionV2(
        status=coverage_status,  # type: ignore[arg-type]
        available=available,
        line_coverage=coverage.line_coverage if coverage else 0.0,
        branch_coverage=coverage.branch_coverage if coverage else 0.0,
        threshold=CoverageThreshold(
            line=coverage.threshold_line if coverage else 0.0,
            branch=coverage.threshold_branch if coverage else 0.0,
        ),
        evidence=EvidenceCoverageErrorV2(kind="error", error_code="evidence_projection_missing"),
    )
    return QualityGateResultV2(
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        dimensions=QualityGateDimensionsV2(functional=functional, coverage=coverage_dim),
        final_status=final,  # type: ignore[arg-type]
    )


def _analysis(
    payload: InspectInputV1,
    gate: QualityGateResultV2,
    failures: list[FailureEntry],
    coverage_gaps: list[CoverageGapEntry],
    inspection_status: str,
    status: str,
) -> FailureAnalysis:
    hard = [item for item in failures if not item.fix_proposal_eligible and not item.needs_review]
    review = [item for item in failures if item.needs_review]
    known = [item for item in failures if item.category == "known_product_issue"]
    return FailureAnalysis(
        schema_version="1.0",
        change_id=payload.change_id,
        source_manifest="execution/execution-manifest.json",
        inspection_status=inspection_status,  # type: ignore[arg-type]
        batch_id=payload.batch_id,
        source_batch_id=payload.batch_id,
        final_status=gate.final_status,
        inspect_mode="primary",
        classification_performed=status != "failed",
        status=status,  # type: ignore[arg-type]
        failures=failures,
        hard_fails=hard,
        needs_review=review,
        known_product_issues=known,
        coverage_gaps=coverage_gaps or None,
    )


class InspectHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(InspectInputV1, request.input)
            if payload.integrity_issues:
                failures = _integrity_failures(payload.integrity_issues)
                _complete(failures)
                gate = _build_gate(payload, "FAIL")
                gate = gate.model_copy(update={"final_status": "FAIL"})
                analysis = _analysis(payload, gate, failures, [], "failed", "failed")
            else:
                failures = _classify_cases(payload.cases, payload.result_paths, payload.change_id)
                _complete(failures)
                api = _counts(payload.cases, "api")
                e2e = _counts(payload.cases, "e2e")
                fuzz = _counts(payload.cases, "fuzz")
                func_status = _functional_status(api, e2e, fuzz)
                gate = _build_gate(payload, func_status)
                status = "no_failures" if not failures else "analyzed"
                analysis = _analysis(
                    payload, gate, failures, _coverage_gaps(payload.coverage), "completed", status
                )
            output = {
                "analysis": analysis.model_dump(mode="json"),
                "quality_gate": gate.model_dump(mode="json"),
                "execution_digest": payload.execution_digest,
                "healing_digest": payload.healing_digest,
                "trace_digest": payload.trace_digest,
                "coverage_digest": payload.coverage_digest,
                "metrics_digest": payload.metrics_digest,
            }
            return TaskOutcome.succeeded(cast(JSONValue, output))
        except InputError as error:
            return failed_input(error)
