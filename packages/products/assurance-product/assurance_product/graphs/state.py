from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal, TypedDict

from graph_engine.plugin_api import FrozenModel
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.stategraph.checkpoint_bridge import CheckpointBridgeState

from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import (
    CaseFlowResultV1,
    CaseReworkContextV1,
    EvidenceArtifactRefV1,
    ReviewedCaseV1,
)
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    InspectionOutcomeV1,
    ReportOutcomeV1,
    ReportPurpose,
)

FAILED_JOIN_PREDECESSORS = ("execute", "run")
COVERAGE_NEEDED_PREDECESSORS = ("quality", "quality-recheck")
COVERAGE_DECISION_ACTIONS = ("approve", "reject")
GENERATION_FAMILIES = ("api", "e2e", "fuzz", "performance")

FailedJoinPredecessor = Literal["execute", "run"]
CoverageNeededPredecessor = Literal["quality", "quality-recheck"]
AssessmentSource = Literal["quality", "quality-recheck"]
AssessmentCoverage = Literal["satisfied", "exhausted", "inconclusive"]
CoverageDecision = Literal["approve", "reject"]


class JoinArrival(TypedDict):
    business_epoch: int
    predecessor: str
    source_activation: str
    sequence: int
    value: dict[str, int]
    arrival_id: str


class JoinInbox(TypedDict):
    arrivals: list[JoinArrival]
    dispatched_ids: list[str]
    current_trigger: JoinArrival | None


class AssessmentTrigger(TypedDict):
    source: AssessmentSource
    coverage_state: AssessmentCoverage
    rounds: dict[str, int]
    evidence: list[dict[str, str]]


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


def _as_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _as_inbox(raw: object) -> JoinInbox:
    if raw is None:
        return {"arrivals": [], "dispatched_ids": [], "current_trigger": None}
    if not isinstance(raw, Mapping):
        raise TypeError("join inbox must be a mapping")
    arrivals = [item for item in list(raw.get("arrivals") or []) if isinstance(item, Mapping)]
    dispatched = [str(item) for item in list(raw.get("dispatched_ids") or [])]
    current = raw.get("current_trigger")
    return {
        "arrivals": [dict(item) for item in arrivals],  # type: ignore[misc]
        "dispatched_ids": dispatched,
        "current_trigger": dict(current) if isinstance(current, Mapping) else None,  # type: ignore[arg-type]
    }


def empty_failed_join_inbox() -> JoinInbox:
    return {"arrivals": [], "dispatched_ids": [], "current_trigger": None}


def empty_coverage_needed_inbox() -> JoinInbox:
    return {"arrivals": [], "dispatched_ids": [], "current_trigger": None}


def _make_arrival(
    *,
    predecessor: str,
    allowed: tuple[str, ...],
    business_epoch: int,
    sequence: int,
    value: Mapping[str, int],
    source_activation: str | None,
) -> JoinArrival:
    if predecessor not in allowed:
        raise ValueError(f"unknown join predecessor: {predecessor}")
    arrival_id = f"{predecessor}.{business_epoch}.{sequence}"
    return {
        "business_epoch": business_epoch,
        "predecessor": predecessor,
        "source_activation": source_activation or f"{predecessor}-{business_epoch}-{sequence}",
        "sequence": sequence,
        "value": {
            "rounds_used": int(value["rounds_used"]),
            "rounds_budget": int(value["rounds_budget"]),
        },
        "arrival_id": arrival_id,
    }


def make_failed_join_arrival(
    *,
    predecessor: str,
    business_epoch: int,
    sequence: int,
    value: Mapping[str, int],
    source_activation: str | None = None,
) -> JoinArrival:
    return _make_arrival(
        predecessor=predecessor,
        allowed=FAILED_JOIN_PREDECESSORS,
        business_epoch=business_epoch,
        sequence=sequence,
        value=value,
        source_activation=source_activation,
    )


def make_coverage_needed_arrival(
    *,
    predecessor: str,
    business_epoch: int,
    sequence: int,
    value: Mapping[str, int],
    source_activation: str | None = None,
) -> JoinArrival:
    return _make_arrival(
        predecessor=predecessor,
        allowed=COVERAGE_NEEDED_PREDECESSORS,
        business_epoch=business_epoch,
        sequence=sequence,
        value=value,
        source_activation=source_activation,
    )


def _arrival_sort_key(arrival: Mapping[str, object], allowed: tuple[str, ...]) -> tuple[int, int, int, str]:
    predecessor = str(arrival["predecessor"])
    predecessor_order = allowed.index(predecessor) if predecessor in allowed else 99
    return (
        _as_int(arrival["business_epoch"], name="business_epoch"),
        _as_int(arrival["sequence"], name="sequence"),
        predecessor_order,
        str(arrival["arrival_id"]),
    )


def _merge_inbox(left: object, right: object, allowed: tuple[str, ...]) -> JoinInbox:
    left_inbox = _as_inbox(left)
    right_inbox = _as_inbox(right)
    by_id: dict[str, JoinArrival] = {}
    for arrival in [*left_inbox["arrivals"], *right_inbox["arrivals"]]:
        by_id[arrival["arrival_id"]] = arrival
    arrivals = sorted(by_id.values(), key=lambda item: _arrival_sort_key(item, allowed))
    dispatched = sorted(set(left_inbox["dispatched_ids"]) | set(right_inbox["dispatched_ids"]))
    current = next((item for item in arrivals if item["arrival_id"] not in dispatched), None)
    return {"arrivals": arrivals, "dispatched_ids": dispatched, "current_trigger": current}


def merge_failed_join_inbox(left: object, right: object) -> JoinInbox:
    return _merge_inbox(left, right, FAILED_JOIN_PREDECESSORS)


def merge_coverage_needed_inbox(left: object, right: object) -> JoinInbox:
    return _merge_inbox(left, right, COVERAGE_NEEDED_PREDECESSORS)


def offer_failed_join_arrival(inbox: object, arrival: JoinArrival) -> JoinInbox:
    return merge_failed_join_inbox(
        inbox,
        {"arrivals": [arrival], "dispatched_ids": [], "current_trigger": None},
    )


def offer_coverage_needed_arrival(inbox: object, arrival: JoinArrival) -> JoinInbox:
    return merge_coverage_needed_inbox(
        inbox,
        {"arrivals": [arrival], "dispatched_ids": [], "current_trigger": None},
    )


def _consume(inbox: object, merge: Callable[[object, object], JoinInbox]) -> JoinInbox:
    current = _as_inbox(inbox).get("current_trigger")
    if current is None:
        return merge(inbox, empty_failed_join_inbox())
    return merge(
        inbox,
        {"arrivals": [], "dispatched_ids": [current["arrival_id"]], "current_trigger": None},
    )


def consume_failed_join_trigger(inbox: object) -> JoinInbox:
    return _consume(inbox, merge_failed_join_inbox)


def consume_coverage_needed_trigger(inbox: object) -> JoinInbox:
    return _consume(inbox, merge_coverage_needed_inbox)


def _as_trigger(raw: object) -> AssessmentTrigger:
    if not isinstance(raw, Mapping):
        raise TypeError("assessment trigger must be a mapping")
    source = raw.get("source")
    coverage = raw.get("coverage_state")
    if source not in {"quality", "quality-recheck"}:
        raise ValueError("assessment source must be quality or quality-recheck")
    if coverage not in {"satisfied", "exhausted", "inconclusive"}:
        raise ValueError("assessment coverage_state is not an exclusive exit")
    rounds = raw.get("rounds")
    if not isinstance(rounds, Mapping):
        raise TypeError("assessment rounds must be a mapping")
    evidence_raw = raw.get("evidence") or []
    if not isinstance(evidence_raw, list):
        raise TypeError("assessment evidence must be a list")
    evidence = [
        {str(key): str(value) for key, value in item.items()}
        for item in evidence_raw
        if isinstance(item, Mapping)
    ]
    return {
        "source": source,
        "coverage_state": coverage,
        "rounds": {
            "rounds_used": _as_int(rounds["rounds_used"], name="rounds_used"),
            "rounds_budget": _as_int(rounds["rounds_budget"], name="rounds_budget"),
        },
        "evidence": evidence,
    }


def _outcome(trigger: AssessmentTrigger) -> str:
    return "satisfied" if trigger["coverage_state"] == "satisfied" else "unsatisfied"


def make_assessment_trigger(
    *,
    source: str,
    coverage_state: str,
    rounds: Mapping[str, int],
    evidence: Sequence[Mapping[str, str]],
) -> AssessmentTrigger:
    return _as_trigger(
        {
            "source": source,
            "coverage_state": coverage_state,
            "rounds": dict(rounds),
            "evidence": [dict(item) for item in evidence],
        }
    )


def merge_assessment_trigger(left: object, right: object) -> AssessmentTrigger:
    left_empty = left in (None, {})
    right_empty = right in (None, {})
    if left_empty and right_empty:
        return {}  # type: ignore[return-value]
    if right is None:
        return {}  # type: ignore[return-value]
    if left_empty:
        return _as_trigger(right)
    if right_empty:
        return _as_trigger(left)
    left_trigger = _as_trigger(left)
    right_trigger = _as_trigger(right)
    if left_trigger == right_trigger:
        return left_trigger
    if _outcome(left_trigger) != _outcome(right_trigger):
        raise ValueError("assessment outcomes are mutually exclusive")
    if left_trigger["source"] != right_trigger["source"]:
        raise ValueError("quality and quality-recheck cannot both reach the same assessment exit")
    raise ValueError("assessment trigger conflict")


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
        by_key[(int(item.get("coverage_epoch", 0)), str(item["family"]), str(item["receipt_id"]))] = item
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
    merged = merge_generation_receipts(left, right)
    refs: list[dict[str, str]] = []
    for item in merged:
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
    selected_test_families: list[str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, Any]]
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
    failed_join_inbox: dict[str, Any]
    coverage_needed_inbox: dict[str, Any]
    current_trigger: dict[str, Any] | None
    assessment_trigger: dict[str, Any] | None
    generation_results: list[dict[str, Any]]
    generation_receipts: list[dict[str, Any]]
    coverage_state: str
    classification: str
    fix_eligible: bool
    coverage_decision: str
    human_action: str
    feature_action: str
    terminal: str
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


class ProductState(CheckpointBridgeState, total=False):
    schema_version: str
    change_id: str
    requirement: str
    run_mode: str
    selected_test_families: list[str]
    case_delta_paths: list[str]
    capability_leafs: list[str]
    capability_catalog: dict[str, str]
    product_policy: dict[str, str]
    data_knowledge: dict[str, str]
    allowed_artifact_paths: list[str]
    budgets: dict[str, int]
    artifacts: list[dict[str, object]]
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
    failed_join_inbox: Annotated[JoinInbox, merge_failed_join_inbox]
    coverage_needed_inbox: Annotated[JoinInbox, merge_coverage_needed_inbox]
    current_trigger: JoinArrival
    assessment_trigger: Annotated[AssessmentTrigger, merge_assessment_trigger]
    generation_results: Annotated[list[GenerationLaneResult], merge_generation_results]
    generation_receipts: Annotated[list[dict[str, object]], merge_generation_receipts]
    coverage_state: str
    classification: str
    fix_eligible: bool
    coverage_decision: str
    human_action: str
    feature_action: str
    terminal: str
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


__all__ = [
    "COVERAGE_DECISION_ACTIONS",
    "COVERAGE_NEEDED_PREDECESSORS",
    "FAILED_JOIN_PREDECESSORS",
    "AssessmentTrigger",
    "GenerationLaneResult",
    "JoinArrival",
    "JoinInbox",
    "ProductState",
    "ProductStateDocument",
    "consume_coverage_needed_trigger",
    "consume_failed_join_trigger",
    "empty_coverage_needed_inbox",
    "empty_failed_join_inbox",
    "make_assessment_trigger",
    "make_coverage_needed_arrival",
    "make_failed_join_arrival",
    "make_generation_lane_result",
    "merge_assessment_trigger",
    "merge_coverage_needed_inbox",
    "merge_failed_join_inbox",
    "merge_generation_receipts",
    "merge_generation_results",
    "merge_receipt_refs",
    "offer_coverage_needed_arrival",
    "offer_failed_join_arrival",
    "replace_receipts",
]
