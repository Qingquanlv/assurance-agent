"""Specialty report v2 models, four-layer matrix invariants, and discriminated loader."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from assurance_agent.artifacts.models.assurance import CASE_TYPES, LAYER_NAMES, PLAN_CHECK_IDS
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


SpecialtyReport = SpecialtyReportV2 | LegacySpecialtyReportV1


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
    "DefinitionBinding",
    "EvidenceDigests",
    "IncompleteLayerRow",
    "LayerRow",
    "LegacySpecialtyReportV1",
    "MechanicalAggregate",
    "NotSelectedLayerRow",
    "NotWiredLayerRow",
    "ReplayScenario",
    "ReplaySemantics",
    "SpecialtyReport",
    "SpecialtyReportV2",
    "build_capability_replay_v2",
    "load_specialty_report",
]
