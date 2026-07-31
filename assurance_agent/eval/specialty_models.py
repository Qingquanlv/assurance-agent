"""Specialty report v2/v3 models, four-layer matrix invariants, and loaders."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from assurance_agent.artifacts.models.assurance import CASE_TYPES, LAYER_NAMES, PLAN_CHECK_IDS
from assurance_agent.artifacts.models.common import GateStatus
from assurance_agent.artifacts.models.sufficiency import TraceLayerSufficiencySummary
from assurance_agent.artifacts.models.trace import (
    StrictNonNegativeInt,
    TraceIntegrity,
    TraceLayerFactSummary,
    TraceProjectionLike,
)
from assurance_agent.evidence.digests import projection_digest
from assurance_agent.evidence.layer_summary import summarize_projection_by_layer
from assurance_agent.evidence.verify import VerifyVerdict
from assurance_agent.workflow.orchestration.plan_check_replay import PolicyEffect

ReplayIntegrity = Literal["complete", "incomplete"]
LayerReplayStatus = Literal["complete", "not_selected", "not_wired", "incomplete"]
LayerApplicability = Literal["applicable", "not_applicable"]
ScenarioAction = Literal["warn", "block", "require_human"]
ReplaySemantics = Literal[
    "counterfactual_plan_check_actions/v1",
    "counterfactual_plan_check_actions/v2",
]
_SCENARIO_ACTIONS: tuple[ScenarioAction, ...] = ("warn", "block", "require_human")


class DefinitionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_invocation_id: str
    assurance_invocation_id: str
    graph_digest: str
    gate_definition_source: Literal["pinned_schema"]
    baseline_policy_digest: str
    policy_source: Literal["pinned_runtime_snapshot"]
    policy_origin: str
    gate_semantics_digest: str
    assurance_profile_digest: str


class CapabilitiesSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required: list[str]
    missing: list[str]


class CheckSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    status: str
    finding_count: int = Field(ge=0)


class MechanicalAggregate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    finding_count: int = Field(ge=0)
    checks: tuple[CheckSummary, ...]

    @model_validator(mode="after")
    def _reconcile(self) -> Self:
        if len(self.checks) != len(PLAN_CHECK_IDS):
            raise ValueError("mechanical_checks must contain exactly four checks")
        if [item.check_id for item in self.checks] != list(PLAN_CHECK_IDS):
            raise ValueError("mechanical_checks.checks must follow PLAN_CHECK_IDS order")
        total = sum(item.finding_count for item in self.checks)
        if total != self.finding_count:
            raise ValueError("mechanical_checks finding_count must reconcile with checks")
        return self


class EvidenceDigests(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review: str | None
    checks: str
    data_knowledge: str | None


class ReplayScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: ScenarioAction
    policy_digest: str
    verdict: str
    route: str
    matched_rule: str | None
    reason: str
    missing_capabilities: list[str]
    policy_effect: PolicyEffect


class CompleteLayerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str
    case_type: str
    status: Literal["complete"]
    reason_code: None = None
    applicability: LayerApplicability
    gate_id: str
    review_artifact: str
    checks_artifact: str
    capabilities: CapabilitiesSummary | None
    mechanical_checks: MechanicalAggregate
    mechanical_execution_contract_digest: str
    evidence_digests: EvidenceDigests
    scenarios: tuple[ReplayScenario, ...]

    @model_validator(mode="after")
    def _validate_complete_shape(self) -> Self:
        if [item.action for item in self.scenarios] != list(_SCENARIO_ACTIONS):
            raise ValueError("complete row scenarios must be warn, block, require_human in order")
        if len(set(item.action for item in self.scenarios)) != len(self.scenarios):
            raise ValueError("duplicate scenario actions")
        if self.applicability == "not_applicable":
            if self.capabilities is not None:
                raise ValueError("inapplicable row must have null capabilities")
            if self.evidence_digests.review is not None or self.evidence_digests.data_knowledge is not None:
                raise ValueError("inapplicable row must have null review/L1 digests")
            if any(item.status != "not_applicable" for item in self.mechanical_checks.checks):
                raise ValueError("inapplicable row checks must be not_applicable")
            if any(item.verdict != "skip" for item in self.scenarios):
                raise ValueError("inapplicable row scenarios must all be skip")
        else:
            if self.capabilities is None:
                raise ValueError("applicable row must include capabilities summary")
            if self.evidence_digests.review is None or self.evidence_digests.data_knowledge is None:
                raise ValueError("applicable row must include review and L1 digests")
        return self


class NotSelectedLayerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str
    case_type: str
    status: Literal["not_selected"]
    reason_code: None = None


class NotWiredLayerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str
    case_type: str
    status: Literal["not_wired"]
    reason_code: None = None


class IncompleteLayerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str
    case_type: str
    status: Literal["incomplete"]
    reason_code: str


LayerRow = Annotated[
    CompleteLayerRow | NotSelectedLayerRow | NotWiredLayerRow | IncompleteLayerRow,
    Field(discriminator="status"),
]


class CapabilityPolicyReplayV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    semantics: ReplaySemantics = "counterfactual_plan_check_actions/v2"
    integrity: ReplayIntegrity
    definition_binding: DefinitionBinding | None = None
    definition_failure: str | None = None
    rows: tuple[LayerRow, ...]

    @field_validator("rows")
    @classmethod
    def _exact_four_rows(cls, value: tuple[LayerRow, ...]) -> tuple[LayerRow, ...]:
        if len(value) != len(LAYER_NAMES):
            raise ValueError("capability replay must contain exactly four rows")
        layers = [row.layer for row in value]
        if layers != list(LAYER_NAMES):
            raise ValueError("capability replay rows must follow stable layer order")
        case_types = [row.case_type for row in value]
        if case_types != list(CASE_TYPES):
            raise ValueError("capability replay rows must follow stable case type order")
        return value

    @model_validator(mode="after")
    def _integrity_matches_matrix(self) -> Self:
        _validate_row_topology(self.rows, semantics=self.semantics)
        derived = _derive_integrity(self.definition_binding, self.rows)
        if self.integrity != derived:
            raise ValueError("integrity must match definition binding and row statuses")
        return self


class SpecialtyReportV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2"] = "2"
    change_id: str
    capability_contract_policy: CapabilityPolicyReplayV2
    traceability_evidence: dict[str, Any]


class LegacySpecialtyReportV1(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal["1"] = "1"
    change_id: str
    capability_contract_policy: dict[str, Any]
    traceability_evidence: dict[str, Any] = Field(default_factory=dict)


TraceCollectionFailureReason = Literal[
    "execution_projection_missing",
    "execution_projection_invalid",
    "reconciled_projection_missing",
    "reconciled_projection_invalid",
    "reconciled_projection_stale",
    "projection_identity_mismatch",
    "projection_phase_pair_mismatch",
    "quality_gate_missing",
    "quality_gate_invalid",
    "quality_gate_binding_mismatch",
    "sufficiency_binding_mismatch",
    "verify_result_missing",
    "verify_result_invalid",
    "verify_binding_mismatch",
    "layer_summary_invalid",
]


class TraceCommandStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_exit: StrictNonNegativeInt
    verify_exit: StrictNonNegativeInt


class ProjectionOverview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["execution", "reconciled"]
    batch_id: str
    projection_digest: str
    integrity: TraceIntegrity
    row_count: StrictNonNegativeInt
    source_count: StrictNonNegativeInt
    gap_count: StrictNonNegativeInt
    unmapped_test_count: StrictNonNegativeInt

    @classmethod
    def from_projection(cls, projection: TraceProjectionLike) -> Self:
        return cls(
            phase=projection.phase,
            batch_id=projection.authoritative_batch_id,
            projection_digest=projection_digest(projection),
            integrity=projection.integrity,
            row_count=len(projection.rows),
            source_count=len(projection.sources),
            gap_count=len(projection.gaps),
            unmapped_test_count=len(projection.unmapped_tests),
        )


class TracePhaseEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overview: ProjectionOverview
    facts: TraceLayerFactSummary

    @classmethod
    def from_projection(cls, projection: TraceProjectionLike) -> Self:
        return cls(
            overview=ProjectionOverview.from_projection(projection),
            facts=summarize_projection_by_layer(projection),
        )

    @model_validator(mode="after")
    def _overview_matches_facts(self) -> Self:
        if self.overview.phase != self.facts.phase:
            raise ValueError("overview.phase must match facts.phase")
        if self.overview.batch_id != self.facts.authoritative_batch_id:
            raise ValueError("overview.batch_id must match facts.authoritative_batch_id")
        if self.overview.projection_digest != self.facts.source_projection_digest:
            raise ValueError("overview.projection_digest must match facts.source_projection_digest")
        if self.overview.integrity != self.facts.projection_integrity:
            raise ValueError("overview.integrity must match facts.projection_integrity")
        row_total = sum(layer.total for layer in self.facts.layers)
        if self.overview.row_count != row_total:
            raise ValueError("overview.row_count must equal sum of facts.layers totals")
        gap_total = sum(layer.gaps.total for layer in self.facts.layers) + self.facts.global_gaps.total
        if self.overview.gap_count != gap_total:
            raise ValueError("overview.gap_count must equal layer plus global gap totals")
        return self


class CoverageSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: GateStatus
    line: float
    branch: float
    final_status: GateStatus


class VerifyDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["reconciled"]
    verdict: VerifyVerdict
    policy_digest: str
    projection_digest: str
    blocking_gap_count: StrictNonNegativeInt
    open_problem_count: StrictNonNegativeInt
    reported_insufficient_count: StrictNonNegativeInt


class CompleteTraceabilityEvidenceV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["complete"]
    command_status: TraceCommandStatus
    execution: TracePhaseEvidence
    reconciled: TracePhaseEvidence
    sufficiency: TraceLayerSufficiencySummary
    coverage: CoverageSummary
    verify: VerifyDiagnostics

    @model_validator(mode="after")
    def _validate_complete_bindings(self) -> Self:
        if self.execution.overview.phase != "execution" or self.execution.facts.phase != "execution":
            raise ValueError("execution phase evidence must be phase=execution")
        if self.reconciled.overview.phase != "reconciled" or self.reconciled.facts.phase != "reconciled":
            raise ValueError("reconciled phase evidence must be phase=reconciled")
        if self.execution.facts.change_id != self.reconciled.facts.change_id:
            raise ValueError("execution and reconciled change_id must match")
        if self.execution.overview.batch_id != self.reconciled.overview.batch_id:
            raise ValueError("execution and reconciled authoritative batch must match")
        if self.execution.facts.authoritative_batch_id != self.reconciled.facts.authoritative_batch_id:
            raise ValueError("execution and reconciled authoritative batch must match")
        if self.sufficiency.source_projection_digest != self.execution.overview.projection_digest:
            raise ValueError("sufficiency.source_projection_digest must bind to execution")
        if self.sufficiency.semantics != "evidence_sufficiency/v2":
            raise ValueError("sufficiency.semantics must be evidence_sufficiency/v2")
        if self.sufficiency.require_current_batch is not True:
            raise ValueError("sufficiency.require_current_batch must be true")
        if self.verify.phase != "reconciled":
            raise ValueError("verify.phase must be reconciled")
        if self.verify.projection_digest != self.reconciled.overview.projection_digest:
            raise ValueError("verify.projection_digest must bind to reconciled")
        for fact_layer, sufficiency_layer in zip(
            self.execution.facts.layers,
            self.sufficiency.layers,
            strict=True,
        ):
            if (
                fact_layer.layer != sufficiency_layer.layer
                or fact_layer.case_type != sufficiency_layer.case_type
            ):
                raise ValueError("sufficiency layers must match execution fact layer identity")
            if sufficiency_layer.sufficient + sufficiency_layer.insufficient != fact_layer.total:
                raise ValueError("sufficiency sufficient+insufficient must equal execution facts.total")
        return self


class IncompleteTraceabilityEvidenceV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["incomplete"]
    reason_code: TraceCollectionFailureReason
    detail: str = ""
    command_status: TraceCommandStatus | None = None


TraceabilityEvidenceV3 = Annotated[
    CompleteTraceabilityEvidenceV3 | IncompleteTraceabilityEvidenceV3,
    Field(discriminator="status"),
]


class SpecialtyReportV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["3"] = "3"
    change_id: str
    capability_contract_policy: CapabilityPolicyReplayV2
    traceability_evidence: TraceabilityEvidenceV3

    @model_validator(mode="after")
    def _validate_complete_report(self) -> Self:
        evidence = self.traceability_evidence
        if not isinstance(evidence, CompleteTraceabilityEvidenceV3):
            return self
        binding = self.capability_contract_policy.definition_binding
        if binding is None:
            raise ValueError("complete traceability requires capability definition_binding")
        if evidence.execution.facts.change_id != self.change_id:
            raise ValueError("change_id must match execution facts.change_id")
        if evidence.reconciled.facts.change_id != self.change_id:
            raise ValueError("change_id must match reconciled facts.change_id")
        baseline = binding.baseline_policy_digest
        if evidence.sufficiency.source_policy_digest != baseline:
            raise ValueError("sufficiency.source_policy_digest must bind to replay baseline")
        if evidence.verify.policy_digest != baseline:
            raise ValueError("verify.policy_digest must bind to replay baseline")
        return self


SpecialtyReport = SpecialtyReportV2 | LegacySpecialtyReportV1

SpecialtyReportVariant = Annotated[
    LegacySpecialtyReportV1 | SpecialtyReportV2 | SpecialtyReportV3,
    Field(discriminator="schema_version"),
]


class SpecialtyReportDocument(RootModel[SpecialtyReportVariant]):
    """Discriminated specialty-report wire document."""


def load_specialty_report_document(raw: object) -> SpecialtyReportVariant:
    return SpecialtyReportDocument.model_validate(raw).root


def _validate_row_topology(rows: tuple[LayerRow, ...], *, semantics: ReplaySemantics) -> None:
    """Apply historical v1 static wiring rules; v2 defers authority to pinned topology."""
    if semantics != "counterfactual_plan_check_actions/v1":
        return
    v1_wired = frozenset({"api", "e2e"})
    v1_unwired = frozenset({"fuzz", "performance"})
    for row in rows:
        if row.layer in v1_wired and row.status == "not_wired":
            raise ValueError(f"wired layer {row.layer!r} cannot have status not_wired")
        if row.layer in v1_unwired and row.status == "complete":
            raise ValueError(f"unwired layer {row.layer!r} cannot have status complete")


def _derive_integrity(
    definition_binding: DefinitionBinding | None,
    rows: tuple[LayerRow, ...],
) -> ReplayIntegrity:
    if definition_binding is None:
        return "incomplete"
    if any(row.status == "incomplete" for row in rows):
        return "incomplete"
    return "complete"


def build_capability_replay_v2(
    *,
    definition_binding: DefinitionBinding | dict[str, object] | None,
    rows: Sequence[dict[str, object] | LayerRow],
    integrity: ReplayIntegrity | None = None,
    definition_failure: str | None = None,
    semantics: ReplaySemantics = "counterfactual_plan_check_actions/v2",
) -> CapabilityPolicyReplayV2:
    binding = (
        None
        if definition_binding is None
        else (
            definition_binding
            if isinstance(definition_binding, DefinitionBinding)
            else DefinitionBinding.model_validate(definition_binding)
        )
    )
    parsed_rows: list[LayerRow] = []
    for row in rows:
        if isinstance(row, (CompleteLayerRow, NotSelectedLayerRow, NotWiredLayerRow, IncompleteLayerRow)):
            parsed_rows.append(row)
            continue
        status = row.get("status")
        if status == "complete":
            parsed_rows.append(CompleteLayerRow.model_validate(row))
        elif status == "not_selected":
            parsed_rows.append(NotSelectedLayerRow.model_validate(row))
        elif status == "not_wired":
            parsed_rows.append(NotWiredLayerRow.model_validate(row))
        else:
            parsed_rows.append(IncompleteLayerRow.model_validate(row))
    derived = integrity if integrity is not None else _derive_integrity(binding, tuple(parsed_rows))
    return CapabilityPolicyReplayV2(
        semantics=semantics,
        integrity=derived,
        definition_binding=binding,
        definition_failure=definition_failure,
        rows=tuple(parsed_rows),
    )


def load_specialty_report(payload: dict[str, Any]) -> SpecialtyReport:
    version = payload.get("schema_version")
    if version == "2":
        return SpecialtyReportV2.model_validate(payload)
    if version == "1":
        return LegacySpecialtyReportV1.model_validate(payload)
    raise ValueError(f"unsupported specialty report schema_version: {version!r}")


__all__ = [
    "CapabilityPolicyReplayV2",
    "CapabilitiesSummary",
    "CheckSummary",
    "CompleteLayerRow",
    "CompleteTraceabilityEvidenceV3",
    "CoverageSummary",
    "DefinitionBinding",
    "EvidenceDigests",
    "IncompleteLayerRow",
    "IncompleteTraceabilityEvidenceV3",
    "LayerRow",
    "LegacySpecialtyReportV1",
    "MechanicalAggregate",
    "NotSelectedLayerRow",
    "NotWiredLayerRow",
    "ProjectionOverview",
    "ReplayScenario",
    "ReplaySemantics",
    "SpecialtyReport",
    "SpecialtyReportDocument",
    "SpecialtyReportV2",
    "SpecialtyReportV3",
    "SpecialtyReportVariant",
    "TraceCollectionFailureReason",
    "TraceCommandStatus",
    "TracePhaseEvidence",
    "TraceabilityEvidenceV3",
    "VerifyDiagnostics",
    "build_capability_replay_v2",
    "load_specialty_report",
    "load_specialty_report_document",
]
