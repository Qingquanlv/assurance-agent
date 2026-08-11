"""Deterministic, worst-wins Quality Gate (aligned with TS quality_gate.ts).

Functional folds api + e2e + fuzz; coverage and performance are separate
dimensions. final_status is the worst status across active dimensions.

``final_status`` states what the *execution* found, and nothing else. Case
evidence sufficiency is a different question with a different consequence — a
change can have every test passing and still be under-evidenced — so it is
adjudicated by a dedicated trace-sufficiency gate rather than folded in here.
``evidence_coverage`` is therefore reported and not read: its dump is attached to
``dimensions.coverage.evidence`` so the verdict travels with the batch, and no
status, warning or route may be derived from it.

``coverage.status`` is the legacy line/branch judgement via ``_coverage_status``
and project ``coverage.gate_mode`` only. That pair must never be reused to route
evidence sufficiency (superseded Traceability Task 9 assumption — retired).
Inspect rebuilds must load ``gate_mode`` from config (§12.10), not hardcode warn.

Verification metrics (``dimensions.metrics``) are likewise informational only:
``worst_status`` / ``final_status`` never include that dimension; routing is
``metrics-sufficiency-gate``'s job.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from assurance_agent.artifacts.models import (
    CoverageDimension,
    CoverageThreshold,
    FunctionalCounts,
    FunctionalDimension,
    GateStatus,
    NonFunctionalDimension,
    QualityGateDimensions,
    QualityGateResult,
    QualityGateResultLike,
    load_quality_gate_result_document,
)
from assurance_agent.evidence.sufficiency import EvidenceCoverageEvaluation
from assurance_agent.workflow.execution.results import (
    CoverageResult,
    PerformanceResult,
    TargetResult,
)


def worst_status(statuses: list[GateStatus]) -> GateStatus:
    present = [s for s in statuses if s]
    if not present:
        return "SKIPPED"
    if "FAIL" in present:
        return "FAIL"
    if "PASS_WITH_WARNINGS" in present:
        return "PASS_WITH_WARNINGS"
    if "PASS" in present:
        return "PASS"
    return "SKIPPED"


def _counts(result: TargetResult | None) -> FunctionalCounts:
    if not result:
        return FunctionalCounts(total=0, passed=0, failed=0)
    return FunctionalCounts(total=result.total, passed=result.passed, failed=result.failed)


def _functional_status(*results: TargetResult | None) -> GateStatus:
    ran = [r for r in results if r and r.total > 0]
    if not ran:
        return "SKIPPED"
    if any(r.failed > 0 for r in ran):
        return "FAIL"
    return "PASS"


def _unmapped_count(*results: TargetResult | None) -> int:
    return sum(len(r.unmapped_tests) for r in results if r and r.total > 0)


def _coverage_status(coverage: CoverageResult | None, gate_mode: Literal["warn", "block"]) -> GateStatus:
    if not coverage or not coverage.available:
        return "SKIPPED"
    if coverage.status == "PASS":
        return "PASS"
    return "FAIL" if gate_mode == "block" else "PASS_WITH_WARNINGS"


def _non_functional(perf: PerformanceResult | None) -> NonFunctionalDimension | None:
    if not perf:
        return None
    if not perf.available:
        status: GateStatus = "SKIPPED"
    elif perf.status == "FAIL":
        status = "FAIL"
    elif perf.status == "PASS":
        status = "PASS"
    else:
        status = "SKIPPED"
    return NonFunctionalDimension(status=status, performance=perf.scenarios)


def build_quality_gate(
    *,
    change_id: str,
    batch_id: str,
    api: TargetResult | None,
    e2e: TargetResult | None,
    coverage: CoverageResult | None,
    coverage_gate_mode: Literal["warn", "block"],
    fuzz: TargetResult | None = None,
    performance: PerformanceResult | None = None,
    evidence_coverage: EvidenceCoverageEvaluation | None = None,
) -> QualityGateResult:
    """Build the batch verdict, optionally reporting the evidence evaluation.

    ``evidence_coverage`` reaches exactly one field,
    ``dimensions.coverage.evidence``, and is read by nothing here. Callers with
    no evaluation to hand — ``inspect`` rebuilds, compat fallbacks — omit it and
    get the same verdict, which is what makes the parameter safe to be optional.
    """
    func_status = _functional_status(api, e2e, fuzz)
    cov_status = _coverage_status(coverage, coverage_gate_mode)
    non_functional = _non_functional(performance)
    warnings: list[str] = []

    unmapped = _unmapped_count(api, e2e, fuzz)
    if unmapped > 0:
        warnings.append(
            f"TRACEABILITY-BROKEN: {unmapped} executed test(s) have no case_id mapping. "
            "Test function names must use the test_<case_id lowercase>__<description> prefix "
            "(e.g. test_tc_user_api_001__list_users_happy_path)."
        )
        if func_status == "PASS":
            func_status = "PASS_WITH_WARNINGS"

    functional = FunctionalDimension(
        status=func_status,
        api=_counts(api),
        e2e=_counts(e2e),
        unmapped_tests=unmapped if unmapped > 0 else None,
    )
    if fuzz and fuzz.status != "skipped":
        functional.fuzz = _counts(fuzz)

    coverage_dim = CoverageDimension(
        status=cov_status,
        available=bool(coverage and coverage.available),
        line_coverage=coverage.line_coverage if coverage else 0.0,
        branch_coverage=coverage.branch_coverage if coverage else 0.0,
        threshold=coverage.threshold if coverage else CoverageThreshold(line=0, branch=0),
        evidence=None if evidence_coverage is None else evidence_coverage.to_json_dict(),
    )

    dimensions = QualityGateDimensions(functional=functional, coverage=coverage_dim)
    # Intentionally omit dimensions.metrics from gate_statuses: informational only.
    gate_statuses: list[GateStatus] = [func_status, cov_status]
    if non_functional is not None:
        dimensions.non_functional = non_functional
        gate_statuses.append(non_functional.status)

    return QualityGateResult(
        schema_version="1.0",
        change_id=change_id,
        batch_id=batch_id,
        dimensions=dimensions,
        final_status=worst_status(gate_statuses),
        warnings=warnings or None,
    )


def quality_gate_legacy_view(
    gate: QualityGateResultLike,
) -> tuple[FunctionalDimension, CoverageDimension, NonFunctionalDimension | None]:
    return (
        FunctionalDimension.model_validate(gate.dimensions.functional.model_dump(mode="json")),
        CoverageDimension.model_validate(gate.dimensions.coverage.model_dump(mode="json")),
        (
            None
            if gate.dimensions.non_functional is None
            else NonFunctionalDimension.model_validate(gate.dimensions.non_functional.model_dump(mode="json"))
        ),
    )


def load_quality_gate_result_file(path: Path) -> QualityGateResultLike | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return load_quality_gate_result_document(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError):
        return None
