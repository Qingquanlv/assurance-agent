"""Deterministic inspect: classify closed execution evidence and publish a gate."""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel, TaskContext, TaskOutcome, TaskRequest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_healing.contracts.status import HealingStatusV1
from assurance_quality.contracts.common import CoverageThreshold, FunctionalCounts, FunctionalDimension
from assurance_quality.contracts.inspect import (
    CoverageDimensionV2,
    CoverageGapEntry,
    EvidenceCoverageErrorV2,
    EvidenceCoverageSuccessV2,
    FailureAnalysis,
    FailureEntry,
    FailureEvidence,
    NonFunctionalDimension,
    PerformanceScenarioVerdict,
    QualityGateDimensionsV2,
    QualityGateResultV2,
)
from assurance_quality.contracts.sufficiency import SufficiencyReportV2
from assurance_quality.operations.common import InputError, failed_input, validate_input

TargetName = Literal["api", "e2e", "fuzz", "performance", "coverage"]
CaseStatus = Literal["passed", "failed", "skipped"]
_SHA256 = r"^[0-9a-f]{64}$"

_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(expr, re.IGNORECASE)
    for name, expr in {
        "environment": (
            "connection refused|cannot connect|econnrefused|service unavailable|host not found|"
            "timeout connecting|failed to start server|502 bad gateway|503 service|503 during|"
            "no such file or directory.*server"
        ),
        "known_product_marker": "expected-product-fail|known product issue|known-product",
        "anomaly_token": r"anomaly-\d+",
        "fact_baseline": "fact-baseline",
        "locator": (
            "locator|selector|element not found|no element|waiting for selector|unable to find element|"
            "strict mode violation|ambiguous|getbytext|getbyrole|getbylabel|getbyplaceholder|getbytestid"
        ),
        "wait_strategy": r"timed? ?out|timeout exceeded|exceeded.*ms|waitfor|networkidle|domcontentloaded|load.*event",
        "fuzz_configuration": (
            "schema.*not found|failed to load schema|cannot fetch schema|invalid schema|no api definition|"
            "hypothesis.*could not|unsatisfied|failed health check|filtered out|base url"
        ),
        "fuzz_stateful": r"stateful|state machine|apistatemachine|sequence|transition|link|rule .* failed",
        "test_data": (
            "fixture|test data|seed|database.*empty|no.*record|not found.*user|not found.*product|"
            "not found.*order|factory|invalid data|missing.*field|required.*field"
        ),
        "assertion": (
            "assertionerror|assert.*expected|expected.*received|to equal|to be|tobecalled|tohavetext|"
            "tohavevalue|statuscode.*expected|response.*expected"
        ),
        "business_logic": (
            "400 bad request|401 unauthorized|403 forbidden|404 not found|422 unprocessable|"
            "500 internal server|business rule|validation error|permission denied|insufficient"
        ),
        "server_error_5xx": r"server error|5\d\d|internal server",
        "test_code": (
            "syntaxerror|typeerror|nameerror|importerror|attributeerror|referenceerror|"
            "cannot read properties|is not a function|is not defined|indentationerror"
        ),
        "fixture_not_found": r"fixture.+not[ ]+found",
        "case_semantic": "step not covered|missing step|precondition not met|out of scope|no acceptance criteria",
    }.items()
}

_DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {
        "category": "known_product_issue",
        "match": {"any_of": ["known_product_marker", {"all_of": ["anomaly_token", "fact_baseline"]}]},
    },
    {"category": "environment_failure", "match": "environment"},
    {"category": "locator_failure", "target": ("e2e",), "match": "locator"},
    {"category": "wait_strategy_failure", "target": ("e2e",), "match": "wait_strategy"},
    {"category": "assertion_failure", "match": "assertion"},
    {"category": "test_data_failure", "match": "test_data"},
    {"category": "business_logic_failure", "match": "business_logic"},
    {"category": "test_code_error", "match": "test_code"},
    {"category": "case_semantic_failure", "match": "case_semantic"},
)
_FUZZ_RULES: tuple[dict[str, Any], ...] = (
    {"category": "test_code_error", "match": "fixture_not_found"},
    {"category": "fuzz_configuration_error", "match": "fuzz_configuration"},
    {"category": "environment_failure", "match": "environment"},
    {"category": "fuzz_stateful_failure", "match": "fuzz_stateful"},
    {"category": "business_logic_failure", "match": {"any_of": ["business_logic", "server_error_5xx"]}},
    {"category": "test_code_error", "match": "test_code"},
)
_FALLBACK = {"default": "unknown", "fuzz": "fuzz_stateful_failure"}
_FIX_PROPOSAL: dict[str, bool | Literal["review"]] = {
    "locator_failure": True,
    "wait_strategy_failure": True,
    "test_data_failure": True,
    "test_code_error": True,
    "environment_failure": False,
    "assertion_failure": False,
    "assertion_expectation_error": "review",
    "business_logic_failure": False,
    "case_semantic_failure": False,
    "known_product_issue": False,
    "coverage_gap": False,
    "fuzz_configuration_error": False,
    "fuzz_stateful_failure": "review",
    "perf_script_error": False,
    "perf_threshold_exceeded": "review",
    "perf_environment": False,
    "manifest_asset_missing": False,
    "unknown": False,
}
_SEVERITY = {
    "environment_failure": "critical",
    "business_logic_failure": "critical",
    "manifest_asset_missing": "critical",
    "fuzz_stateful_failure": "high",
    "perf_threshold_exceeded": "high",
    "assertion_failure": "high",
    "assertion_expectation_error": "high",
    "case_semantic_failure": "high",
    "locator_failure": "medium",
    "wait_strategy_failure": "medium",
    "test_code_error": "medium",
    "test_data_failure": "medium",
    "fuzz_configuration_error": "medium",
    "perf_script_error": "medium",
    "perf_environment": "medium",
    "known_product_issue": "low",
    "coverage_gap": "low",
    "unknown": "low",
}

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


class InspectCoverageMetricsV1(FrozenModel):
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


class InspectPerformanceInputV1(FrozenModel):
    available: bool
    status: Literal["PASS", "FAIL", "SKIPPED"]
    scenarios: tuple[PerformanceScenarioVerdict, ...] = ()


class InspectInputV1(FrozenModel):
    change_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    execution: ExecutionEvidenceV1
    healing: HealingStatusV1
    trace: dict[str, Any]
    coverage: dict[str, Any]
    metrics: dict[str, Any]
    execution_digest: str = Field(pattern=_SHA256)
    healing_digest: str = Field(pattern=_SHA256)
    trace_digest: str = Field(pattern=_SHA256)
    coverage_digest: str = Field(pattern=_SHA256)
    metrics_digest: str = Field(pattern=_SHA256)
    result_paths: dict[str, str] = Field(default_factory=dict)
    sufficiency: SufficiencyReportV2 | None = None
    performance: InspectPerformanceInputV1 | None = None
    coverage_metrics: InspectCoverageMetricsV1 | None = None
    unmapped_tests: tuple[InspectCaseInputV1, ...] = ()
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


def _matches(match: Any, text: str) -> bool:
    if isinstance(match, str):
        return _PATTERNS[match].search(text) is not None
    if isinstance(match, dict):
        if "any_of" in match:
            return any(_matches(item, text) for item in match["any_of"])
        if "all_of" in match:
            return all(_matches(item, text) for item in match["all_of"])
    return False


def classify_failure(*, message: str, target: str, log_excerpt: str = "") -> Classification:
    text = f"{message} {log_excerpt}".lower()
    pipeline = _FUZZ_RULES if target == "fuzz" else _DEFAULT_RULES
    category = _FALLBACK["fuzz" if target == "fuzz" else "default"]
    for rule in pipeline:
        allowed_targets = rule.get("target")
        if allowed_targets and target not in allowed_targets:
            continue
        if _matches(rule["match"], text):
            category = rule["category"]
            break
    allowed = _FIX_PROPOSAL.get(category, False)
    return Classification(
        category=category,
        fix_proposal_eligible=allowed is True,
        severity=_SEVERITY.get(category, "low"),
        needs_review=category == "unknown" or allowed == "review",
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


def document_digest(value: object) -> str:
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    return canonical_digest(cast(JSONValue, payload))


def _layer_for(test: str, evidence: ExecutionEvidenceV1) -> str:
    for entry in evidence.mapping.mappings:
        if entry.test == test:
            return entry.layer
    return "api"


def _counts(evidence: ExecutionEvidenceV1, target: str) -> FunctionalCounts:
    selected = [item for item in evidence.results if _layer_for(item.test, evidence) == target]
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


def _entry(
    *,
    case_id: str,
    target: str,
    message: str,
    file: str,
    result_file: str,
    classification: Classification,
    trace: str = "",
    screenshot: str = "",
    video: str = "",
    raw_log: str = "",
) -> FailureEntry:
    return FailureEntry(
        case_id=case_id,
        target=target,  # type: ignore[arg-type]
        category=classification.category,  # type: ignore[arg-type]
        fix_proposal_eligible=classification.fix_proposal_eligible,
        severity=classification.severity,  # type: ignore[arg-type]
        needs_review=classification.needs_review,
        evidence=FailureEvidence(
            result_file=result_file,
            test_file=file,
            trace=trace,
            screenshot=screenshot,
            video=video,
            raw_log=raw_log,
            log_excerpt=message[:400],
        ),
        diagnosis=_diagnosis(message, classification.category),
        recommended_action=_ACTION.get(classification.category, "Investigate manually."),
    )


def _classify_evidence(payload: InspectInputV1) -> list[FailureEntry]:
    entries: list[FailureEntry] = []
    for result in payload.execution.results:
        if result.status != "failed":
            continue
        target = _layer_for(result.test, payload.execution)
        entries.append(
            _entry(
                case_id=result.case_id or result.test,
                target=target,
                message=result.message,
                file=result.test,
                result_file=payload.result_paths.get(target, ""),
                classification=classify_failure(message=result.message, target=target),
            )
        )
    for case in payload.unmapped_tests:
        if case.status != "failed":
            continue
        entries.append(
            _entry(
                case_id=case.case_id or case.test_name,
                target=case.target,
                message=case.message,
                file=case.file,
                result_file=payload.result_paths.get(case.target, ""),
                classification=classify_failure(message=case.message, target=case.target),
                trace=case.trace,
                screenshot=case.screenshot,
                video=case.video,
                raw_log=case.raw_log,
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


def _coverage_gaps(coverage: InspectCoverageMetricsV1 | None) -> list[CoverageGapEntry]:
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


def _coverage_dimension(payload: InspectInputV1) -> CoverageDimensionV2:
    metrics = payload.coverage_metrics
    if payload.sufficiency is None:
        status = "FAIL"
        evidence: EvidenceCoverageErrorV2 | EvidenceCoverageSuccessV2 = EvidenceCoverageErrorV2(
            kind="error",
            error_code="evidence_projection_missing",
        )
    else:
        status = "PASS" if payload.sufficiency.all_sufficient else "FAIL"
        evidence = EvidenceCoverageSuccessV2(kind="sufficiency", report=payload.sufficiency)
    return CoverageDimensionV2(
        status=status,  # type: ignore[arg-type]
        available=bool(metrics and metrics.available),
        line_coverage=metrics.line_coverage if metrics else 0.0,
        branch_coverage=metrics.branch_coverage if metrics else 0.0,
        threshold=CoverageThreshold(
            line=metrics.threshold_line if metrics else 0.0,
            branch=metrics.threshold_branch if metrics else 0.0,
        ),
        evidence=evidence,
    )


def _non_functional(performance: InspectPerformanceInputV1 | None) -> NonFunctionalDimension | None:
    if performance is None:
        return None
    if not performance.available:
        status: str = "SKIPPED"
    elif performance.status == "FAIL":
        status = "FAIL"
    elif performance.status == "PASS":
        status = "PASS"
    else:
        status = "SKIPPED"
    return NonFunctionalDimension(status=status, performance=list(performance.scenarios))  # type: ignore[arg-type]


def _build_gate(payload: InspectInputV1, functional_status: str) -> QualityGateResultV2:
    api = _counts(payload.execution, "api")
    e2e = _counts(payload.execution, "e2e")
    fuzz = _counts(payload.execution, "fuzz")
    unmapped = len(payload.unmapped_tests)
    warnings: list[str] = []
    if unmapped > 0:
        warnings.append(
            f"TRACEABILITY-BROKEN: {unmapped} executed test(s) have no case_id mapping. "
            "Test function names must use the test_<case_id lowercase>__<description> prefix "
            "(e.g. test_tc_user_api_001__list_users_happy_path)."
        )
        if functional_status == "PASS":
            functional_status = "PASS_WITH_WARNINGS"
    coverage_dim = _coverage_dimension(payload)
    non_functional = _non_functional(payload.performance)
    functional = FunctionalDimension(
        status=functional_status,  # type: ignore[arg-type]
        api=api,
        e2e=e2e,
        fuzz=fuzz if fuzz.total > 0 else None,
        unmapped_tests=unmapped if unmapped > 0 else None,
    )
    statuses = [functional_status, coverage_dim.status]
    if non_functional is not None:
        statuses.append(non_functional.status)
    return QualityGateResultV2(
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        dimensions=QualityGateDimensionsV2(
            functional=functional,
            coverage=coverage_dim,
            non_functional=non_functional,
        ),
        final_status=worst_status(statuses),  # type: ignore[arg-type]
        warnings=warnings or None,
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


def _require_document_digests(payload: InspectInputV1) -> None:
    expected = {
        "execution": payload.execution,
        "healing": payload.healing,
        "trace": payload.trace,
        "coverage": payload.coverage,
        "metrics": payload.metrics,
    }
    actual = {
        "execution": payload.execution_digest,
        "healing": payload.healing_digest,
        "trace": payload.trace_digest,
        "coverage": payload.coverage_digest,
        "metrics": payload.metrics_digest,
    }
    for name, document in expected.items():
        if document_digest(document) != actual[name]:
            raise InputError(f"{name} digest does not match the authenticated {name} document")


class InspectHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(InspectInputV1, request.input)
            _require_document_digests(payload)
            if payload.integrity_issues:
                failures = _integrity_failures(payload.integrity_issues)
                _complete(failures)
                gate = _build_gate(payload, "FAIL")
                gate = gate.model_copy(update={"final_status": "FAIL"})
                analysis = _analysis(payload, gate, failures, [], "failed", "failed")
            else:
                failures = _classify_evidence(payload)
                _complete(failures)
                api = _counts(payload.execution, "api")
                e2e = _counts(payload.execution, "e2e")
                fuzz = _counts(payload.execution, "fuzz")
                func_status = _functional_status(api, e2e, fuzz)
                gate = _build_gate(payload, func_status)
                status = "no_failures" if not failures else "analyzed"
                analysis = _analysis(
                    payload,
                    gate,
                    failures,
                    _coverage_gaps(payload.coverage_metrics),
                    "completed",
                    status,
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
