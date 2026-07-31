#!/usr/bin/env python3
"""Freeze and render benchmark evidence for the two architecture initiatives."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from assurance_agent.artifacts.models import QualityGateResult
from assurance_agent.artifacts.models.inspect import (
    EvidenceCoverageSuccessV2,
    QualityGateResultV2,
    load_quality_gate_result_document,
)
from assurance_agent.artifacts.models.sufficiency import SufficiencyBindingError
from assurance_agent.artifacts.models.trace import (
    TraceProjection,
    TraceProjectionV2,
    load_trace_projection_document,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.eval.specialty_models import (
    CapabilityPolicyReplayV2,
    CompleteTraceabilityEvidenceV3,
    CoverageSummary,
    IncompleteTraceabilityEvidenceV3,
    LegacySpecialtyReportV1,
    SpecialtyReportV2,
    SpecialtyReportV3,
    TraceCollectionFailureReason,
    TraceCommandStatus,
    TracePhaseEvidence,
    VerifyDiagnostics,
    load_specialty_report,
)
from assurance_agent.eval.specialty_render import render_specialty_sections
from assurance_agent.eval.specialty_replay import collect_capability_policy_replay
from assurance_agent.evidence.current_projection import (
    CurrentProjectionInvalidError,
    CurrentProjectionMissingError,
    CurrentProjectionStaleError,
    load_current_reconciled_projection,
)
from assurance_agent.evidence.digests import projection_digest as shared_projection_digest
from assurance_agent.evidence.layer_summary import (
    TraceLayerSummaryError,
    TracePhasePairError,
    join_layer_sufficiency,
    validate_trace_phase_pair,
)
from assurance_agent.evidence.sufficiency import SufficiencyReport
from assurance_agent.evidence.verify import VerifyResult, projection_digest

_CURRENT_IDENTITY_STALE_REASONS = frozenset(
    {"phase_mismatch", "change_id_mismatch", "batch_id_mismatch"}
)
_CURRENT_STALE_REASONS = frozenset({"legacy_version", "digest_mismatch"})


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _atomic_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(raw, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _projection_summary(projection: TraceProjection, *, reconciled: bool) -> dict[str, Any]:
    rows = projection.rows
    summary: dict[str, Any] = {
        "phase": projection.phase,
        "batch_id": projection.authoritative_batch_id,
        "integrity": projection.integrity,
        "row_count": len(rows),
        "source_count": len(projection.sources),
        "gap_count": len(projection.gaps),
        "unmapped_test_count": len(projection.unmapped_tests),
    }
    if not reconciled:
        return summary

    failure_rows = 0
    failure_links = 0
    problem_rows = 0
    problem_links = 0
    unique_problems: set[str] = set()
    for row in rows:
        failures = row.failures
        problems = row.open_problem_ids
        if failures:
            failure_rows += 1
        if problems:
            problem_rows += 1
        failure_links += len(failures)
        problem_links += len(problems)
        unique_problems.update(problems)
    summary.update(
        {
            "failure_row_count": failure_rows,
            "failure_link_count": failure_links,
            "open_problem_row_count": problem_rows,
            "open_problem_link_count": problem_links,
            "unique_open_problem_count": len(unique_problems),
        }
    )
    return summary


def _quality_summary(
    quality: QualityGateResult,
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage = quality.dimensions.coverage
    if coverage.evidence is None:
        raise ValueError("quality-gate coverage.evidence is required for specialty reporting")
    evidence = SufficiencyReport.model_validate(coverage.evidence)
    reasons: Counter[str] = Counter()
    for verdict in evidence.verdicts:
        if not verdict.sufficient:
            reasons.update(verdict.reason_codes)
    sufficient = sum(verdict.sufficient for verdict in evidence.verdicts)
    insufficient = len(evidence.verdicts) - sufficient
    return (
        {
            "status": coverage.status,
            "line": coverage.line_coverage,
            "branch": coverage.branch_coverage,
            "final_status": quality.final_status,
        },
        {
            "sufficient_count": sufficient,
            "insufficient_count": insufficient,
            "reason_counts": dict(sorted(reasons.items())),
        },
    )


def _validate_cross_artifact_identity(
    *,
    change_id: str,
    review: dict[str, Any],
    execution: dict[str, Any],
    reconciled: dict[str, Any],
    quality: dict[str, Any],
    verify: dict[str, Any],
) -> None:
    if review.get("change_id") != change_id:
        raise ValueError(
            f"review change_id mismatch: expected {change_id!r}, got {review.get('change_id')!r}"
        )
    if execution.get("change_id") != change_id:
        raise ValueError(
            f"execution trace change_id mismatch: expected {change_id!r}, got {execution.get('change_id')!r}"
        )
    if execution.get("phase") != "execution":
        raise ValueError(
            f"execution trace phase mismatch: expected 'execution', got {execution.get('phase')!r}"
        )
    if reconciled.get("change_id") != change_id:
        raise ValueError(
            "reconciled trace change_id mismatch: "
            f"expected {change_id!r}, got {reconciled.get('change_id')!r}"
        )
    if reconciled.get("phase") != "reconciled":
        raise ValueError(
            f"reconciled trace phase mismatch: expected 'reconciled', got {reconciled.get('phase')!r}"
        )
    execution_batch = execution.get("authoritative_batch_id")
    reconciled_batch = reconciled.get("authoritative_batch_id")
    if execution_batch != reconciled_batch:
        raise ValueError(
            f"trace batch mismatch: execution={execution_batch!r}, reconciled={reconciled_batch!r}"
        )
    if quality.get("change_id") != change_id:
        raise ValueError(
            f"quality gate change_id mismatch: expected {change_id!r}, got {quality.get('change_id')!r}"
        )
    if quality.get("batch_id") != execution_batch:
        raise ValueError(
            f"quality gate batch mismatch: expected {execution_batch!r}, got {quality.get('batch_id')!r}"
        )
    if verify.get("change_id") != change_id:
        raise ValueError(
            f"verify change_id mismatch: expected {change_id!r}, got {verify.get('change_id')!r}"
        )
    if verify.get("phase") != "reconciled":
        raise ValueError(f"verify phase mismatch: expected 'reconciled', got {verify.get('phase')!r}")


def _validate_verify_binding(reconciled: TraceProjection, verify: VerifyResult) -> None:
    expected_digest = projection_digest(reconciled)
    if verify.projection_digest != expected_digest:
        raise ValueError(
            "verify projection digest mismatch: "
            f"expected {expected_digest!r}, got {verify.projection_digest!r}"
        )
    if verify.scope is not None and verify.scope.batch != reconciled.authoritative_batch_id:
        raise ValueError(
            "verify scope batch mismatch: "
            f"expected {reconciled.authoritative_batch_id!r}, got {verify.scope.batch!r}"
        )


class TraceCollectionFailure(Exception):
    reason_code: TraceCollectionFailureReason
    detail: str

    def __init__(
        self,
        reason_code: TraceCollectionFailureReason,
        detail: str,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TraceCollectionInputs:
    project_root: Path
    change_id: str
    root_invocation_id: str
    workflow_entrypoint: str
    trace_path: Path
    verify_path: Path
    trace_exit: int
    verify_exit: int
    change_dir: Path = field(init=False)
    command_status: TraceCommandStatus = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("trace_exit", self.trace_exit),
            ("verify_exit", self.verify_exit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not self.workflow_entrypoint:
            raise ValueError("workflow entrypoint is required")
        change_dir = resolve_change(self.project_root, self.change_id).path
        object.__setattr__(self, "change_dir", change_dir)
        object.__setattr__(
            self,
            "command_status",
            TraceCommandStatus(trace_exit=self.trace_exit, verify_exit=self.verify_exit),
        )


def _load_execution_projection_v2(inputs: TraceCollectionInputs) -> TraceProjectionV2:
    path = inputs.trace_path
    if not path.is_file():
        raise TraceCollectionFailure(
            "execution_projection_missing",
            "execution_trace_absent",
        )
    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceCollectionFailure(
            "execution_projection_invalid",
            "execution_trace_unreadable",
        ) from exc
    if not isinstance(raw, dict):
        raise TraceCollectionFailure(
            "execution_projection_invalid",
            "execution_trace_not_object",
        )
    batch = raw.get("authoritative_batch_id")
    if (
        raw.get("change_id") != inputs.change_id
        or raw.get("phase") != "execution"
        or not isinstance(batch, str)
        or not batch
    ):
        raise TraceCollectionFailure(
            "projection_identity_mismatch",
            "execution_identity_mismatch",
        )
    try:
        document = load_trace_projection_document(raw)
    except ValidationError as exc:
        raise TraceCollectionFailure(
            "execution_projection_invalid",
            "execution_trace_model_invalid",
        ) from exc
    if not isinstance(document, TraceProjectionV2):
        raise TraceCollectionFailure(
            "execution_projection_invalid",
            "execution_trace_not_v2",
        )
    return document


def _validate_projection_identity(
    change_id: str,
    execution: TraceProjectionV2,
    reconciled: TraceProjectionV2,
) -> None:
    if execution.change_id != change_id or reconciled.change_id != change_id:
        raise TraceCollectionFailure(
            "projection_identity_mismatch",
            "change_id_mismatch",
        )
    if execution.phase != "execution" or reconciled.phase != "reconciled":
        raise TraceCollectionFailure(
            "projection_identity_mismatch",
            "phase_mismatch",
        )
    if execution.authoritative_batch_id != reconciled.authoritative_batch_id:
        raise TraceCollectionFailure(
            "projection_identity_mismatch",
            "batch_mismatch",
        )


def _load_current_reconciled_projection_v2(inputs: TraceCollectionInputs) -> TraceProjectionV2:
    try:
        return load_current_reconciled_projection(inputs.project_root, inputs.change_id)
    except CurrentProjectionMissingError as exc:
        raise TraceCollectionFailure(
            "reconciled_projection_missing",
            "reconciled_trace_absent",
        ) from exc
    except CurrentProjectionInvalidError as exc:
        raise TraceCollectionFailure(
            "reconciled_projection_invalid",
            "reconciled_trace_unreadable",
        ) from exc
    except CurrentProjectionStaleError as exc:
        if exc.reason in _CURRENT_IDENTITY_STALE_REASONS:
            raise TraceCollectionFailure(
                "projection_identity_mismatch",
                f"reconciled_{exc.reason}",
            ) from exc
        if exc.reason in _CURRENT_STALE_REASONS:
            raise TraceCollectionFailure(
                "reconciled_projection_stale",
                f"reconciled_{exc.reason}",
            ) from exc
        raise


def _phase_evidence_from_projection(projection: TraceProjectionV2) -> TracePhaseEvidence:
    try:
        return TracePhaseEvidence.from_projection(projection)
    except TraceLayerSummaryError as exc:
        raise TraceCollectionFailure(
            "layer_summary_invalid",
            "layer_summary_rejected",
        ) from exc


def _preflight_quality_sufficiency_binding(
    raw: dict[str, Any],
    *,
    expected_policy_digest: str,
    expected_projection_digest: str,
) -> None:
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, dict):
        return
    coverage = dimensions.get("coverage")
    if not isinstance(coverage, dict):
        return
    evidence = coverage.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("kind") != "sufficiency":
        return
    report = evidence.get("report")
    if not isinstance(report, dict):
        return

    projection_digest_value = report.get("source_projection_digest")
    if (
        isinstance(projection_digest_value, str)
        and projection_digest_value
        and projection_digest_value != expected_projection_digest
    ):
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "quality_projection_digest_mismatch",
        )
    policy_digest_value = report.get("source_policy_digest")
    if (
        isinstance(policy_digest_value, str)
        and policy_digest_value
        and policy_digest_value != expected_policy_digest
    ):
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "quality_policy_digest_mismatch",
        )
    semantics = report.get("semantics")
    if isinstance(semantics, str) and semantics != "evidence_sufficiency/v2":
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "quality_semantics_mismatch",
        )
    require_current_batch = report.get("require_current_batch")
    if isinstance(require_current_batch, bool) and require_current_batch is not True:
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "quality_require_current_batch_mismatch",
        )

    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list):
        return
    case_ids: list[str] = []
    for item in verdicts:
        if not isinstance(item, dict):
            return
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            return
        case_ids.append(case_id)
    if len(case_ids) != len(set(case_ids)):
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "duplicate_sufficiency_case_id",
        )


def _load_bound_quality_v2(
    inputs: TraceCollectionInputs,
    execution: TraceProjectionV2,
    expected_policy_digest: str,
) -> QualityGateResultV2:
    path = inputs.change_dir / "execution" / "quality-gate-result.json"
    if not path.is_file():
        raise TraceCollectionFailure("quality_gate_missing", "quality_gate_absent")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceCollectionFailure(
            "quality_gate_invalid",
            "quality_gate_unreadable",
        ) from exc
    if not isinstance(raw, dict):
        raise TraceCollectionFailure("quality_gate_invalid", "quality_gate_not_object")
    if raw.get("change_id") != inputs.change_id or raw.get("batch_id") != execution.authoritative_batch_id:
        raise TraceCollectionFailure(
            "quality_gate_binding_mismatch",
            "quality_identity_mismatch",
        )
    _preflight_quality_sufficiency_binding(
        raw,
        expected_policy_digest=expected_policy_digest,
        expected_projection_digest=shared_projection_digest(execution),
    )
    try:
        quality = load_quality_gate_result_document(raw)
    except ValidationError as exc:
        raise TraceCollectionFailure(
            "quality_gate_invalid",
            "quality_gate_model_invalid",
        ) from exc
    if not isinstance(quality, QualityGateResultV2):
        raise TraceCollectionFailure(
            "quality_gate_binding_mismatch",
            "quality_gate_not_v2",
        )
    return quality


def _load_bound_verify(
    inputs: TraceCollectionInputs,
    reconciled: TraceProjectionV2,
    expected_policy_digest: str,
) -> VerifyResult:
    path = inputs.verify_path
    if not path.is_file():
        raise TraceCollectionFailure("verify_result_missing", "verify_result_absent")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TraceCollectionFailure(
            "verify_result_invalid",
            "verify_result_unreadable",
        ) from exc
    if not isinstance(raw, dict):
        raise TraceCollectionFailure("verify_result_invalid", "verify_result_not_object")
    if raw.get("change_id") != inputs.change_id or raw.get("phase") != "reconciled":
        raise TraceCollectionFailure(
            "verify_binding_mismatch",
            "verify_identity_mismatch",
        )
    try:
        verify = VerifyResult.model_validate(raw)
    except ValidationError as exc:
        raise TraceCollectionFailure(
            "verify_result_invalid",
            "verify_result_model_invalid",
        ) from exc
    expected_projection_digest = shared_projection_digest(reconciled)
    if (
        verify.projection_digest != expected_projection_digest
        or verify.policy_digest != expected_policy_digest
    ):
        raise TraceCollectionFailure(
            "verify_binding_mismatch",
            "verify_digest_mismatch",
        )
    if verify.verdict == "pass" and verify.scope is None:
        raise TraceCollectionFailure(
            "verify_binding_mismatch",
            "verify_pass_requires_scope",
        )
    if verify.scope is not None:
        expected_cases = tuple(row.case_id for row in reconciled.rows)
        if (
            verify.scope.cases != expected_cases
            or verify.scope.batch != reconciled.authoritative_batch_id
            or verify.scope.policy_digest != expected_policy_digest
            or verify.scope.projection_digest != expected_projection_digest
        ):
            raise TraceCollectionFailure(
                "verify_binding_mismatch",
                "verify_scope_mismatch",
            )
    return verify


def _collect_complete_traceability(
    inputs: TraceCollectionInputs,
    capability: CapabilityPolicyReplayV2,
) -> CompleteTraceabilityEvidenceV3:
    binding = capability.definition_binding
    if binding is None:
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "capability_definition_binding_missing",
        )
    execution = _load_execution_projection_v2(inputs)
    reconciled = _load_current_reconciled_projection_v2(inputs)
    _validate_projection_identity(inputs.change_id, execution, reconciled)
    # Per-projection summary rejection is diagnosed before cross-phase pairing so
    # integrity misstatements map to layer_summary_invalid rather than phase-pair.
    execution_evidence = _phase_evidence_from_projection(execution)
    reconciled_evidence = _phase_evidence_from_projection(reconciled)
    try:
        validate_trace_phase_pair(execution, reconciled)
    except TracePhasePairError as exc:
        raise TraceCollectionFailure(
            "projection_phase_pair_mismatch",
            "phase_pair_mismatch",
        ) from exc
    quality = _load_bound_quality_v2(inputs, execution, binding.baseline_policy_digest)
    coverage_evidence = quality.dimensions.coverage.evidence
    if not isinstance(coverage_evidence, EvidenceCoverageSuccessV2):
        raise TraceCollectionFailure(
            "quality_gate_binding_mismatch",
            "typed_sufficiency_unavailable",
        )
    try:
        sufficiency = join_layer_sufficiency(
            execution,
            execution_evidence.facts,
            coverage_evidence.report,
            expected_policy_digest=binding.baseline_policy_digest,
        )
    except SufficiencyBindingError as exc:
        raise TraceCollectionFailure(
            "sufficiency_binding_mismatch",
            "sufficiency_join_mismatch",
        ) from exc
    except TraceLayerSummaryError as exc:
        raise TraceCollectionFailure(
            "layer_summary_invalid",
            "layer_summary_rejected",
        ) from exc
    verify = _load_bound_verify(inputs, reconciled, binding.baseline_policy_digest)
    coverage_summary = CoverageSummary(
        status=quality.dimensions.coverage.status,
        line=quality.dimensions.coverage.line_coverage,
        branch=quality.dimensions.coverage.branch_coverage,
        final_status=quality.final_status,
    )
    verify_diagnostics = VerifyDiagnostics(
        phase=verify.phase,
        verdict=verify.verdict,
        policy_digest=verify.policy_digest,
        projection_digest=verify.projection_digest,
        blocking_gap_count=len(verify.blocking_gaps),
        open_problem_count=len(verify.open_problem_ids),
        reported_insufficient_count=len(verify.insufficient),
    )
    return CompleteTraceabilityEvidenceV3(
        status="complete",
        command_status=inputs.command_status,
        execution=execution_evidence,
        reconciled=reconciled_evidence,
        sufficiency=sufficiency,
        coverage=coverage_summary,
        verify=verify_diagnostics,
    )


def collect_v3_report(inputs: TraceCollectionInputs) -> SpecialtyReportV3:
    capability = collect_capability_policy_replay(
        change_dir=inputs.change_dir,
        change_id=inputs.change_id,
        root_invocation_id=inputs.root_invocation_id,
        expected_entrypoint=inputs.workflow_entrypoint,
    )
    try:
        traceability: CompleteTraceabilityEvidenceV3 | IncompleteTraceabilityEvidenceV3 = (
            _collect_complete_traceability(inputs, capability)
        )
    except TraceCollectionFailure as exc:
        traceability = IncompleteTraceabilityEvidenceV3(
            status="incomplete",
            reason_code=exc.reason_code,
            detail=exc.detail,
            command_status=inputs.command_status,
        )
    return SpecialtyReportV3(
        change_id=inputs.change_id,
        capability_contract_policy=capability,
        traceability_evidence=traceability,
    )


def _collect_traceability_evidence(
    *,
    change_id: str,
    trace_path: Path,
    verify_path: Path,
    change_dir: Path,
    trace_exit: int,
    verify_exit: int,
) -> dict[str, Any]:
    raw_review = _load_json(change_dir / "review" / "api-plan-review.json")
    raw_execution = _load_json(trace_path)
    raw_reconciled = _load_json(change_dir / "inspect" / "trace-projection.json")
    raw_quality = _load_json(change_dir / "execution" / "quality-gate-result.json")
    raw_verify = _load_json(verify_path)
    _validate_cross_artifact_identity(
        change_id=change_id,
        review=raw_review,
        execution=raw_execution,
        reconciled=raw_reconciled,
        quality=raw_quality,
        verify=raw_verify,
    )

    execution_projection = TraceProjection.model_validate(raw_execution)
    reconciled_projection = TraceProjection.model_validate(raw_reconciled)
    quality = QualityGateResult.model_validate(raw_quality)
    verify = VerifyResult.model_validate(raw_verify)
    _validate_verify_binding(reconciled_projection, verify)
    coverage, sufficiency = _quality_summary(quality)

    return {
        "command_status": {"trace_exit": trace_exit, "verify_exit": verify_exit},
        "execution_projection": _projection_summary(execution_projection, reconciled=False),
        "reconciled_projection": _projection_summary(reconciled_projection, reconciled=True),
        "coverage": coverage,
        "sufficiency": sufficiency,
        "verify": {
            "phase": verify.phase,
            "verdict": verify.verdict,
            "policy_digest": verify.policy_digest,
            "projection_digest": verify.projection_digest,
            "blocking_gap_count": len(verify.blocking_gaps),
            "open_problem_count": len(verify.open_problem_ids),
            "reported_insufficient_count": len(verify.insufficient),
            "observed_insufficient_count": sufficiency["insufficient_count"],
        },
    }


def collect_report(
    *,
    project_root: Path,
    change_id: str,
    root_invocation_id: str,
    workflow_entrypoint: str,
    trace_path: Path,
    verify_path: Path,
    trace_exit: int,
    verify_exit: int,
) -> SpecialtyReportV2:
    if trace_exit < 0 or verify_exit < 0:
        raise ValueError("trace and verify exit codes must be non-negative")
    change_dir = project_root / "qa" / "changes" / change_id
    traceability_evidence = _collect_traceability_evidence(
        change_id=change_id,
        trace_path=trace_path,
        verify_path=verify_path,
        change_dir=change_dir,
        trace_exit=trace_exit,
        verify_exit=verify_exit,
    )
    capability = collect_capability_policy_replay(
        change_dir=change_dir,
        change_id=change_id,
        root_invocation_id=root_invocation_id,
        expected_entrypoint=workflow_entrypoint,
    )
    return SpecialtyReportV2(
        change_id=change_id,
        capability_contract_policy=capability,
        traceability_evidence=traceability_evidence,
    )


def evidence_row(report: dict[str, Any], *, expected_change_id: str) -> str:
    change_id = report.get("change_id")
    traceability = report.get("traceability_evidence")
    if not isinstance(change_id, str) or not isinstance(traceability, dict):
        raise ValueError("specialty report is missing change_id or traceability_evidence")
    if change_id != expected_change_id:
        raise ValueError(
            f"specialty report change_id mismatch: expected {expected_change_id!r}, got {change_id!r}"
        )
    execution = traceability.get("execution_projection")
    command_status = traceability.get("command_status")
    verify = traceability.get("verify")
    if (
        not isinstance(command_status, dict)
        or not isinstance(execution, dict)
        or not isinstance(verify, dict)
    ):
        raise ValueError("specialty report is missing command_status, execution_projection, or verify")
    trace_exit = command_status.get("trace_exit")
    verify_exit = command_status.get("verify_exit")
    integrity = execution.get("integrity")
    gaps = execution.get("gap_count")
    verdict = verify.get("verdict")
    blocking = verify.get("blocking_gap_count")
    insufficient = verify.get("reported_insufficient_count")
    if integrity not in {"complete", "degraded", "incomplete"}:
        raise ValueError("specialty report has invalid trace integrity")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (trace_exit, verify_exit, gaps, blocking, insufficient)
    ):
        raise ValueError("specialty report has invalid evidence counts")
    if not isinstance(verdict, str):
        raise ValueError("specialty report has invalid verify verdict")
    if verdict not in {"pass", "needs_human", "fail"}:
        raise ValueError("specialty report has invalid verify verdict")
    return f"{change_id}|{trace_exit}|{integrity}|{gaps}|{verify_exit}|{verdict}|{blocking}|{insufficient}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--project-root", type=Path, required=True)
    collect.add_argument("--change-id", required=True)
    collect.add_argument("--root-invocation-id", required=True)
    collect.add_argument("--workflow-entrypoint", required=True)
    collect.add_argument("--trace", type=Path, required=True)
    collect.add_argument("--verify", type=Path, required=True)
    collect.add_argument("--trace-exit", type=int, required=True)
    collect.add_argument("--verify-exit", type=int, required=True)
    collect.add_argument("--output", type=Path, required=True)
    render = commands.add_parser("render")
    render.add_argument("reports", nargs="+", type=Path)
    evidence = commands.add_parser("evidence-row")
    evidence.add_argument("--change-id", required=True)
    evidence.add_argument("report", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "collect":
        report = collect_report(
            project_root=args.project_root.resolve(),
            change_id=args.change_id,
            root_invocation_id=args.root_invocation_id,
            workflow_entrypoint=args.workflow_entrypoint,
            trace_path=args.trace.resolve(),
            verify_path=args.verify.resolve(),
            trace_exit=args.trace_exit,
            verify_exit=args.verify_exit,
        )
        _atomic_dump(args.output.resolve(), report.model_dump(mode="json"))
        return 1 if report.capability_contract_policy.integrity == "incomplete" else 0
    if args.command == "evidence-row":
        loaded = load_specialty_report(_load_json(args.report))
        if isinstance(loaded, LegacySpecialtyReportV1):
            payload = loaded.model_dump(mode="json")
        else:
            payload = loaded.model_dump(mode="json")
        print(evidence_row(payload, expected_change_id=args.change_id))
        return 0
    reports = [load_specialty_report(_load_json(path)) for path in args.reports]
    print(render_specialty_sections(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
