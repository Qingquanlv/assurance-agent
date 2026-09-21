from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, TypedDict

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_improvement.contracts.retro import RetroWindow, RetroRunStatus
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    CaseReworkContextV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
    merge_history_refs,
)
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    InspectionOutcomeV1,
    ReportOutcomeV1,
    ReportPurpose,
)
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")


class GenerationLaneResult(TypedDict):
    coverage_epoch: int
    family: str
    receipt_id: str
    selected: bool
    status: str


def replace_receipts(existing: object, incoming: object) -> list[dict[str, object]]:
    if incoming is None:
        return (
            [dict(item) for item in existing]
            if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes))
            else []
        )
    if not isinstance(incoming, Sequence) or isinstance(incoming, (str, bytes)):
        raise TypeError("receipts must be a list")
    receipts: list[dict[str, object]] = []
    for item in incoming:
        if not isinstance(item, Mapping):
            raise TypeError("receipt must be a mapping")
        receipts.append({str(key): value for key, value in item.items()})
    return receipts


def make_generation_lane_result(
    *,
    coverage_epoch: int = 0,
    family: str,
    receipt_id: str,
    selected: bool,
    status: str,
) -> GenerationLaneResult:
    return {
        "coverage_epoch": coverage_epoch,
        "family": family,
        "receipt_id": receipt_id,
        "selected": selected,
        "status": status,
    }


def _as_results(raw: object) -> list[GenerationLaneResult]:
    if raw is None:
        return []
    if isinstance(raw, Mapping) and "family" in raw:
        return [dict(raw)]  # type: ignore[arg-type]
    if not isinstance(raw, list):
        raise TypeError("generation results must be a list")
    return [dict(item) for item in raw if isinstance(item, Mapping)]  # type: ignore[misc]


def merge_generation_results(left: object, right: object) -> list[GenerationLaneResult]:
    by_key: dict[tuple[int, str, str], GenerationLaneResult] = {}
    for item in [*_as_results(left), *_as_results(right)]:
        key = (int(item.get("coverage_epoch", 0)), str(item["family"]), str(item["receipt_id"]))
        by_key[key] = item
    order = {name: index for index, name in enumerate(GENERATION_FAMILIES)}
    return sorted(
        by_key.values(),
        key=lambda item: (
            int(item.get("coverage_epoch", 0)),
            order.get(str(item["family"]), 99),
            str(item["receipt_id"]),
        ),
    )


def _as_receipts(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if isinstance(raw, Mapping) and "receipt_id" in raw:
        return [dict(raw)]
    if not isinstance(raw, list):
        raise TypeError("generation receipts must be a list")
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def merge_generation_receipts(left: object, right: object) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for item in [*_as_receipts(left), *_as_receipts(right)]:
        by_id[str(item["receipt_id"])] = item
    order = {name: index for index, name in enumerate(GENERATION_FAMILIES)}
    return sorted(
        by_id.values(),
        key=lambda item: (order.get(str(item.get("family", "")), 99), str(item["receipt_id"])),
    )


def merge_receipt_refs(left: object, right: object) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in merge_generation_receipts(left, right):
        receipt_id = item.get("receipt_id")
        digest = item.get("receipt_digest")
        if isinstance(receipt_id, str) and isinstance(digest, str):
            refs.append({"receipt_id": receipt_id, "receipt_digest": digest})
    return refs


class ProductStateDocument(FrozenModel):
    schema_version: str
    change_id: str
    requirement: str
    run_mode: str
    candidate_test_families: list[str]
    resolved_plan_ref: EvidenceArtifactRefV1 | None
    selected_test_families: list[str]
    plan_digest: str
    plan_ref: EvidenceArtifactRefV1
    family_policy: dict[str, list[str]]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, Any]]
    history_refs: list[dict[str, str]]
    retro_window: RetroWindow | None
    retro_id: str
    retro_runtime_ref: EvidenceArtifactRefV1
    retro_status: RetroRunStatus
    window: RetroWindow
    decision: str
    receipts: list[dict[str, str]]
    output: dict[str, Any]
    status: str
    feature_input: dict[str, Any]
    feature_output: dict[str, Any]
    rounds_budget: int
    rounds_used: int
    artifact_paths: list[str]
    evidence_refs: list[dict[str, str]]
    receipt_refs: list[dict[str, str]]
    current_trigger: dict[str, Any] | None
    generation_results: list[dict[str, Any]]
    generation_receipts: list[dict[str, Any]]
    coverage_state: str
    classification: str
    fix_eligible: bool
    human_action: str
    feature_action: str
    terminal: dict[str, str]
    execution_evidence: dict[str, Any]
    execution_digest: str
    healing_digest: str | None
    trace_digest: str
    coverage_digest: str
    metrics_digest: str
    case_digest: str
    mapping_digest: str
    issue_digest: str | None
    execution_semantic_node_id: str
    families: dict[str, dict[str, bool]]
    selected_families: list[str]
    projection: dict[str, Any]
    eval_run_id: str
    outcome: str
    report_sha256: str
    staged_sha256: str
    baseline_sha256: str | None
    target_digest: str
    coverage_epoch: int
    healing_rounds_used: int
    reviewed_case: ReviewedCaseV1
    preparation_refs: list[dict[str, str]]
    case_refs: list[dict[str, str]]
    case_receipt: ReceiptRef
    receipt: ReceiptRef
    source_artifacts: list[dict[str, str]]
    case_result: CaseFlowResultV1
    generation_result: GenerationCycleResultV1
    execution_result: ExecutionCycleResultV1
    assessment_inputs: AssessmentInputsV1
    fact_baseline_ref: EvidenceArtifactRefV1
    inspection_outcome: InspectionOutcomeV1
    tail_result: dict[str, Any]
    case_rework_context: CaseReworkContextV1
    last_coverage_source_receipt: ReceiptRef | None
    report_refs: list[dict[str, str]]
    report_receipt: ReceiptRef | None
    report_outcome: ReportOutcomeV1
    report_purpose: ReportPurpose
    activation: dict[str, str]
    batch_id: str
    policy_resource_id: str
    policy_sha256: str
    execution_at: str
    healing_ref: EvidenceArtifactRefV1 | None
    issue_ref: EvidenceArtifactRefV1 | None
    issue_analysis_ref: EvidenceArtifactRefV1 | None
    issue_analysis: dict[str, object] | None
    observations_ref: EvidenceArtifactRefV1
    issue_evidence_manifest_ref: EvidenceArtifactRefV1
    owned_evidence_ids: list[str]
    evidence_bundle_digest: str
    kind: str
    owner_id: str
    allowed_paths: list[str]
    allowed_roots: list[str]
    baseline_digest: str
    candidate_digest: str
    policy_digest: str
    mapping_paths: list[str]
    execution_evidence_digest: str
    proposal_ref: EvidenceArtifactRefV1
    approval_ref: EvidenceArtifactRefV1 | None
    execution_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: list[dict[str, str]]
    allowed_test_paths: list[str]
    repair_round: int
    proposal_result: dict[str, Any]
    proposal_receipt: dict[str, str]
    repair_result: dict[str, Any]
    effect_refs: list[dict[str, str]]


class ProductState(CheckpointBridgeState, total=False):
    schema_version: str
    change_id: str
    requirement: str
    run_mode: str
    candidate_test_families: list[str]
    resolved_plan_ref: dict[str, str] | None
    selected_test_families: list[str]
    plan_digest: str
    plan_ref: dict[str, str]
    family_policy: dict[str, object]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, object]]
    history_refs: Annotated[list[dict[str, str]], merge_history_refs]
    retro_window: RetroWindow | None
    retro_id: str
    retro_runtime_ref: dict[str, str]
    retro_status: dict[str, object]
    window: dict[str, object]
    decision: str
    receipts: Annotated[list[dict[str, object]], replace_receipts]
    output: dict[str, object]
    status: str
    feature_input: dict[str, object]
    feature_output: dict[str, object]
    rounds_budget: int
    rounds_used: int
    artifact_paths: list[str]
    evidence_refs: list[dict[str, str]]
    receipt_refs: Annotated[list[dict[str, str]], merge_receipt_refs]
    current_trigger: dict[str, object]
    generation_results: Annotated[list[GenerationLaneResult], merge_generation_results]
    generation_receipts: Annotated[list[dict[str, object]], merge_generation_receipts]
    coverage_state: str
    classification: str
    fix_eligible: bool
    human_action: str
    feature_action: str
    terminal: dict[str, str]
    execution_evidence: dict[str, object]
    execution_digest: str
    healing_digest: str | None
    trace_digest: str
    coverage_digest: str
    metrics_digest: str
    case_digest: str
    mapping_digest: str
    issue_digest: str | None
    execution_semantic_node_id: str
    families: dict[str, dict[str, bool]]
    selected_families: list[str]
    projection: dict[str, object]
    eval_run_id: str
    outcome: str
    report_sha256: str
    staged_sha256: str
    baseline_sha256: str | None
    target_digest: str
    coverage_epoch: int
    healing_rounds_used: int
    reviewed_case: ReviewedCaseV1
    preparation_refs: list[dict[str, str]]
    case_refs: list[dict[str, str]]
    case_receipt: ReceiptRef
    receipt: ReceiptRef
    source_artifacts: list[dict[str, str]]
    case_result: CaseFlowResultV1
    generation_result: GenerationCycleResultV1
    execution_result: ExecutionCycleResultV1
    assessment_inputs: AssessmentInputsV1
    fact_baseline_ref: EvidenceArtifactRefV1
    inspection_outcome: InspectionOutcomeV1
    tail_result: dict[str, object]
    case_rework_context: CaseReworkContextV1
    last_coverage_source_receipt: ReceiptRef | None
    report_refs: list[dict[str, str]]
    report_receipt: ReceiptRef | None
    report_outcome: ReportOutcomeV1
    report_purpose: str
    activation: dict[str, str]
    batch_id: str
    policy_resource_id: str
    policy_sha256: str
    execution_at: str
    healing_ref: EvidenceArtifactRefV1 | None
    issue_ref: EvidenceArtifactRefV1 | None
    issue_analysis_ref: EvidenceArtifactRefV1 | None
    issue_analysis: dict[str, object] | None
    observations_ref: EvidenceArtifactRefV1
    issue_evidence_manifest_ref: EvidenceArtifactRefV1
    owned_evidence_ids: list[str]
    evidence_bundle_digest: str
    kind: str
    owner_id: str
    allowed_paths: list[str]
    allowed_roots: list[str]
    baseline_digest: str
    candidate_digest: str
    policy_digest: str
    mapping_paths: list[str]
    execution_evidence_digest: str
    proposal_ref: EvidenceArtifactRefV1
    approval_ref: EvidenceArtifactRefV1 | None
    execution_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: list[dict[str, str]]
    allowed_test_paths: list[str]
    repair_round: int
    proposal_result: dict[str, object]
    proposal_receipt: dict[str, str]
    repair_result: dict[str, object]
    effect_refs: list[dict[str, str]]
    attempt_failure: dict[str, object]


__all__ = [
    "GenerationLaneResult",
    "ProductState",
    "ProductStateDocument",
    "make_generation_lane_result",
    "merge_generation_receipts",
    "merge_generation_results",
    "merge_receipt_refs",
    "replace_receipts",
]
