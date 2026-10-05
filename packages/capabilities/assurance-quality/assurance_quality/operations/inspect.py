"""Deterministic inspect: classify closed execution evidence and publish a gate."""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from pydantic import BaseModel

from graph_engine.canonical import JSONValue, canonical_digest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_quality.contracts.assessment import FailureClassificationFactsV1
from assurance_quality.contracts.inspect import (
    FailureEntry,
    FailureEvidence,
)
from assurance_quality.contracts.metrics import MetricsDocument

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
            r"(?:^|\n)\s*(?:E\s+)?assert\s+|\bexpect\s*\(|"
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
    # An assertion reports a mismatch, not ownership of its cause. Family-specific
    # keywords (for example a Playwright locator) must not authorize a repair.
    pipeline = (
        {"category": "assertion_failure", "match": "assertion"},
        *(_FUZZ_RULES if target == "fuzz" else _DEFAULT_RULES),
    )
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


_BLOCKING_FAILURE_CATEGORIES = frozenset(
    {
        "business_logic_failure",
        "environment_failure",
        "known_product_issue",
        "manifest_asset_missing",
        "perf_environment",
    }
)
_REPAIRABLE_FAILURE_CATEGORIES = frozenset(
    category for category, eligible in _FIX_PROPOSAL.items() if eligible is True
)


def build_failure_classification_facts(
    execution: ExecutionEvidenceV1,
    metrics: MetricsDocument,
) -> tuple[FailureClassificationFactsV1, tuple[str, ...]]:
    """Reduce authenticated evidence to facts without choosing a workflow route."""

    blocking = False
    analysis_required = False
    needs_human = False
    repairable = False
    reason_codes: set[str] = set()
    for result in execution.results:
        if result.status != "failed":
            continue
        target = _layer_for(result.test, execution)
        category = classify_failure(message=result.message, target=target).category
        reason_codes.add(f"execution.{category}")
        if category in _BLOCKING_FAILURE_CATEGORIES:
            blocking = True
        elif category == "assertion_failure":
            analysis_required = True
        elif category in _REPAIRABLE_FAILURE_CATEGORIES:
            repairable = True
        else:
            needs_human = True

    adversarial = metrics.metrics["adversarial_clean"]
    if adversarial.status == "evaluated" and adversarial.holds is False:
        blocking = True
        reason_codes.add("adversarial.open_counterexample")

    return (
        FailureClassificationFactsV1(
            identity_valid=True,
            blocking_failure=blocking,
            analysis_required=analysis_required,
            needs_human=needs_human,
            repairable_failure=repairable,
        ),
        tuple(sorted(reason_codes)),
    )


def _diagnosis(message: str, category: str) -> str:
    first = (message or "").split("\n")[0][:200]
    prefix = _DIAGNOSIS.get(category)
    return f"{prefix} {first}".strip() if prefix else (first or "Unknown failure.")


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
