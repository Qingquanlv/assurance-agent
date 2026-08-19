#!/usr/bin/env python3
"""Freeze and render benchmark evidence for the two architecture initiatives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from assurance_agent.artifacts.models.inspect import (
    EvidenceCoverageSuccessV2,
    QualityGateResultV2,
    load_quality_gate_result_document,
)
from assurance_agent.artifacts.models.sufficiency import SufficiencyBindingError
from assurance_agent.artifacts.models.trace import (
    TraceProjectionV2,
    load_trace_projection_document,
)
from assurance_agent.change_location import resolve_change
from benchmark.specialty.specialty_models import (
    CapabilityPolicyReplayV2,
    CommittedSpecialtyPublicationReceipt,
    CompleteTraceabilityEvidenceV3,
    CoverageSummary,
    IncompleteTraceabilityEvidenceV3,
    LegacySpecialtyReportV1,
    PendingSpecialtyPublicationReceipt,
    SpecialtyReportV2,
    SpecialtyReportV3,
    SpecialtyReportVariant,
    TraceCollectionFailureReason,
    TraceCommandStatus,
    TracePhaseEvidence,
    VerifyDiagnostics,
    load_specialty_publication_receipt,
    load_specialty_report,
)
from benchmark.specialty.specialty_render import render_specialty_sections
from benchmark.specialty.specialty_replay import collect_capability_policy_replay
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
from assurance_agent.evidence.verify import VerifyResult

_CURRENT_IDENTITY_STALE_REASONS = frozenset({"phase_mismatch", "change_id_mismatch", "batch_id_mismatch"})
_CURRENT_STALE_REASONS = frozenset({"legacy_version", "digest_mismatch"})
_PUBLICATION_CRASH_ENV = "AA_SPECIALTY_PUBLICATION_CRASH"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _report_wire_bytes(report: SpecialtyReportV3) -> bytes:
    # Do not sort object keys: sufficiency execution_state_counts is order-sensitive.
    return (report.model_dump_json(indent=2) + "\n").encode("utf-8")


def _crash_maybe(window: str) -> None:
    if os.environ.get(_PUBLICATION_CRASH_ENV) == window:
        raise RuntimeError(f"injected publication crash:{window}")


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
    from assurance_agent.product import select_product
    from assurance_agent.workflow.graph.capability_state import DEFAULT_PRODUCT_ID

    select_product(DEFAULT_PRODUCT_ID)
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


def collect_report(inputs: TraceCollectionInputs) -> SpecialtyReportV3:
    return collect_v3_report(inputs)


def report_collection_exit(report: SpecialtyReportVariant) -> Literal[0, 1]:
    if isinstance(report, SpecialtyReportV3):
        if report.capability_contract_policy.integrity != "complete":
            return 1
        if report.traceability_evidence.status != "complete":
            return 1
        return 0
    if isinstance(report, SpecialtyReportV2):
        return 0 if report.capability_contract_policy.integrity == "complete" else 1
    return 0


def _resolve_distinct_paths(output: Path, receipt_path: Path) -> tuple[Path, Path]:
    resolved_output = output.resolve()
    resolved_receipt = receipt_path.resolve()
    if resolved_output == resolved_receipt:
        raise ValueError("report output and publication receipt paths must differ")
    if resolved_output.exists() and resolved_receipt.exists() and resolved_output.samefile(resolved_receipt):
        raise ValueError("report output and publication receipt paths must differ")
    return resolved_output, resolved_receipt


def publish_specialty_report(
    report: SpecialtyReportV3,
    *,
    output: Path,
    receipt_path: Path,
    attempt_id: str,
) -> Literal[0, 1]:
    if not attempt_id or not str(attempt_id).strip():
        raise ValueError("attempt_id must be non-empty")
    if not report.change_id or not str(report.change_id).strip():
        raise ValueError("change_id must be non-empty")
    output, receipt_path = _resolve_distinct_paths(output, receipt_path)

    report_bytes = _report_wire_bytes(report)
    # Validate wire bytes round-trip before touching disk.
    load_specialty_report(json.loads(report_bytes.decode("utf-8")))
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    pending = PendingSpecialtyPublicationReceipt(
        attempt_id=attempt_id,
        change_id=report.change_id,
    )
    committed = CommittedSpecialtyPublicationReceipt(
        attempt_id=attempt_id,
        change_id=report.change_id,
        report_sha256=report_sha256,
        trace_status=report.traceability_evidence.status,
        capability_integrity=report.capability_contract_policy.integrity,
    )
    pending_bytes = (
        json.dumps(pending.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    committed_bytes = (
        json.dumps(committed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    _crash_maybe("before_pending")
    _atomic_write_bytes(receipt_path, pending_bytes)
    _crash_maybe("after_pending")
    _atomic_write_bytes(output, report_bytes)
    _crash_maybe("after_report")
    _atomic_write_bytes(receipt_path, committed_bytes)
    _crash_maybe("after_committed")
    return report_collection_exit(report)


def _legacy_evidence_fields(
    report: SpecialtyReportV2 | LegacySpecialtyReportV1,
) -> tuple[str, str, str, str, str, str, str]:
    evidence = report.traceability_evidence
    command_status = evidence.get("command_status")
    execution = evidence.get("execution_projection")
    verify = evidence.get("verify")
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
    if verdict not in {"pass", "needs_human", "fail"}:
        raise ValueError("specialty report has invalid verify verdict")
    return (
        str(trace_exit),
        str(integrity),
        str(gaps),
        str(verify_exit),
        str(verdict),
        str(blocking),
        str(insufficient),
    )


def evidence_row(report: SpecialtyReportVariant, *, expected_change_id: str) -> str:
    if report.change_id != expected_change_id:
        raise ValueError(
            f"specialty report change_id mismatch: expected {expected_change_id!r}, got {report.change_id!r}"
        )
    if isinstance(report, SpecialtyReportV3):
        evidence = report.traceability_evidence
        if isinstance(evidence, IncompleteTraceabilityEvidenceV3):
            if evidence.command_status is None:
                trace_exit = "unknown"
                verify_exit = "unknown"
            else:
                trace_exit = str(evidence.command_status.trace_exit)
                verify_exit = str(evidence.command_status.verify_exit)
            return (
                f"{report.change_id}|incomplete|{evidence.reason_code}|{trace_exit}|"
                f"unknown|unknown|{verify_exit}|unknown|unknown|unknown"
            )
        return (
            f"{report.change_id}|complete|none|{evidence.command_status.trace_exit}|"
            f"{evidence.execution.overview.integrity}|{evidence.execution.overview.gap_count}|"
            f"{evidence.command_status.verify_exit}|{evidence.verify.verdict}|"
            f"{evidence.verify.blocking_gap_count}|{evidence.verify.reported_insufficient_count}"
        )
    if isinstance(report, (SpecialtyReportV2, LegacySpecialtyReportV1)):
        trace_exit, integrity, gaps, verify_exit, verdict, blocking, insufficient = _legacy_evidence_fields(
            report
        )
        return (
            f"{report.change_id}|legacy_unlayered|none|{trace_exit}|{integrity}|{gaps}|"
            f"{verify_exit}|{verdict}|{blocking}|{insufficient}"
        )
    raise TypeError(f"unsupported specialty report type: {type(report)!r}")


def validate_publication(
    *,
    report_path: Path,
    receipt_path: Path,
    change_id: str,
    mode: Literal["fresh", "reuse"],
    attempt_id: str | None = None,
) -> None:
    if not change_id or not str(change_id).strip():
        raise ValueError("change_id must be non-empty")
    report = load_specialty_report(_load_json(report_path))
    if report.change_id != change_id:
        raise ValueError(
            f"specialty report change_id mismatch: expected {change_id!r}, got {report.change_id!r}"
        )

    receipt_exists = receipt_path.is_file()
    if isinstance(report, SpecialtyReportV3):
        if not receipt_exists:
            raise ValueError("v3 specialty report requires a durable publication receipt")
        receipt = load_specialty_publication_receipt(_load_json(receipt_path))
        if not isinstance(receipt, CommittedSpecialtyPublicationReceipt):
            raise ValueError("publication receipt must be committed")
        report_bytes = report_path.read_bytes()
        digest = hashlib.sha256(report_bytes).hexdigest()
        if receipt.change_id != change_id:
            raise ValueError("publication receipt change_id mismatch")
        if receipt.report_sha256 != digest:
            raise ValueError("publication receipt report digest mismatch")
        if receipt.trace_status != report.traceability_evidence.status:
            raise ValueError("publication receipt trace_status mismatch")
        if receipt.capability_integrity != report.capability_contract_policy.integrity:
            raise ValueError("publication receipt capability_integrity mismatch")
        if mode == "fresh":
            if not attempt_id or not str(attempt_id).strip():
                raise ValueError("fresh validation requires non-empty attempt_id")
            if receipt.attempt_id != attempt_id:
                raise ValueError("publication receipt attempt_id mismatch")
        elif not receipt.attempt_id or not str(receipt.attempt_id).strip():
            raise ValueError("publication receipt attempt_id missing")
        return

    # Legacy V1/V2: receiptless only.
    if receipt_exists:
        raise ValueError("legacy specialty report cannot validate while a receipt is present")
    if mode == "fresh" and attempt_id is not None and not str(attempt_id).strip():
        raise ValueError("attempt_id must be non-empty when provided")


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
    collect.add_argument("--attempt-id", required=True)
    collect.add_argument("--publication-receipt", type=Path, required=True)
    render = commands.add_parser("render")
    render.add_argument("reports", nargs="+", type=Path)
    evidence = commands.add_parser("evidence-row")
    evidence.add_argument("--change-id", required=True)
    evidence.add_argument("report", type=Path)
    validate = commands.add_parser("validate-publication")
    validate.add_argument("--report", type=Path, required=True)
    validate.add_argument("--publication-receipt", type=Path, required=True)
    validate.add_argument("--change-id", required=True)
    validate.add_argument("--mode", choices=("fresh", "reuse"), required=True)
    validate.add_argument("--attempt-id", default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "collect":
        if not args.attempt_id or not str(args.attempt_id).strip():
            print("attempt_id must be non-empty", file=sys.stderr)
            return 2
        if not args.change_id or not str(args.change_id).strip():
            print("change_id must be non-empty", file=sys.stderr)
            return 2
        try:
            output, receipt = _resolve_distinct_paths(args.output, args.publication_receipt)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        inputs = TraceCollectionInputs(
            project_root=args.project_root.resolve(),
            change_id=args.change_id,
            root_invocation_id=args.root_invocation_id,
            workflow_entrypoint=args.workflow_entrypoint,
            trace_path=args.trace.resolve(),
            verify_path=args.verify.resolve(),
            trace_exit=args.trace_exit,
            verify_exit=args.verify_exit,
        )
        report = collect_report(inputs)
        return publish_specialty_report(
            report,
            output=output,
            receipt_path=receipt,
            attempt_id=args.attempt_id,
        )
    if args.command == "evidence-row":
        try:
            loaded = load_specialty_report(_load_json(args.report))
            print(evidence_row(loaded, expected_change_id=args.change_id))
        except (OSError, ValueError, ValidationError, TypeError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return report_collection_exit(loaded)
    if args.command == "validate-publication":
        try:
            validate_publication(
                report_path=args.report.resolve(),
                receipt_path=args.publication_receipt.resolve(),
                change_id=args.change_id,
                mode=args.mode,
                attempt_id=args.attempt_id,
            )
        except (OSError, ValueError, ValidationError, TypeError, json.JSONDecodeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    reports = [load_specialty_report(_load_json(path)) for path in args.reports]
    print(render_specialty_sections(reports), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
