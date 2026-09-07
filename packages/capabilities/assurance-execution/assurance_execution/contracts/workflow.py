"""Versioned output for one completed execution or rerun batch."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic.types import AwareDatetime

from graph_engine.attempts.contracts import AttemptResultProvenanceV1, TerminalReceiptRef
from graph_engine.attempts.host_protocol import TaskHostCallIdentity
from graph_engine.attempts.resolutions import ReceiptRef
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import FrozenModel

from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_generation.contracts.workflow import VerifiedGenerationDefectV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1, require_same_plan
from assurance_generation.contracts.execution_plan import ValidationProfile
from graph_engine.attempts import AttemptKey, BusinessActivation


class ExecutionCycleResultV1(FrozenModel):
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    executed_at: AwareDatetime
    final_status: Literal["PASS", "FAIL"]
    evidence_ref: EvidenceArtifactRefV1
    mapping_ref: EvidenceArtifactRefV1
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _paths_match_change(self) -> Self:
        prefix = f"qa/changes/{self.change_id}/"
        if not self.evidence_ref.path.startswith(prefix):
            raise ValueError("execution evidence must belong to the current change")
        if not self.mapping_ref.path.startswith(prefix):
            raise ValueError("execution mapping must belong to the current change")
        if any(not item.path.startswith(prefix) for item in self.source_refs):
            raise ValueError("execution sources must belong to the current change")
        return self


class ExecutionCycleInputV1(FrozenModel):
    generation: GenerationCycleResultV1
    repair_round: int = Field(default=0, ge=0)


class VerifiedExecutionCycleResultV1(FrozenModel):
    """Committed verified execution cycle kept separate from legacy PASS/FAIL."""

    schema_version: Literal["1"] = "1"
    validation_profile: ValidationProfile
    change_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    reviewed_case: ReviewedCaseV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    case_execution_plan_ref: EvidenceArtifactRefV1
    case_execution_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    attempt_key: AttemptKey
    batch_id: str = Field(min_length=1)
    executed_at: AwareDatetime
    completion_status: Literal["collected", "incomplete"]
    mapping_ref: EvidenceArtifactRefV1
    mapping_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_ref: EvidenceArtifactRefV1
    evidence_ref: EvidenceArtifactRefV1
    execution_index_ref: EvidenceArtifactRefV1
    execution_authority_ref: EvidenceArtifactRefV1
    raw_evidence_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    receipt: ReceiptRef

    @model_validator(mode="after")
    def _closed_verified_cycle(self) -> Self:
        if (
            self.reviewed_case.change_id != self.change_id
            or self.reviewed_case.coverage_epoch != self.coverage_epoch
        ):
            raise ValueError("verified cycle ReviewedCase identity does not match")
        require_same_plan(
            self.plan_digest,
            self.plan_ref,
            self.reviewed_case.plan_digest,
            self.reviewed_case.plan_ref,
        )
        if self.case_execution_plan_ref.digest != self.case_execution_plan_digest:
            raise ValueError("verified cycle machine plan ref and digest do not match")
        if self.mapping_ref.digest != self.mapping_digest:
            raise ValueError("verified cycle mapping ref and digest do not match")
        prefix = f"qa/changes/{self.change_id}/"
        refs = (
            self.case_execution_plan_ref,
            self.mapping_ref,
            self.manifest_ref,
            self.evidence_ref,
            self.execution_index_ref,
            self.execution_authority_ref,
            *self.raw_evidence_refs,
            *self.source_refs,
        )
        if any(not item.path.startswith(prefix) for item in refs):
            raise ValueError("verified cycle refs must belong to the current change")
        return self


class VerifiedGenerationDefectAttemptV1(FrozenModel):
    """Generation defect sealed by the production host for one execution attempt."""

    defect: VerifiedGenerationDefectV1
    authority_identity: TaskHostCallIdentity
    authority_receipt: TerminalReceiptRef

    @model_validator(mode="after")
    def _bind_host_authority(self) -> Self:
        identity = self.authority_identity
        if (
            identity.attempt_key_digest != self.defect.attempt_key.digest
            or identity.task_id != self.defect.attempt_key.digest
            or identity.activity_id != self.defect.attempt_key.digest
            or identity.activation_id not in {"execution.execute", "execution.run"}
            or identity.operation != "execute"
            or identity.phase != "runtime"
            or identity.handler_id != "assurance.execution.generation-defect"
        ):
            raise ValueError("generation defect host authority identity does not match")
        identity_payload: JSONValue = identity.model_dump(mode="json")
        if self.authority_receipt.identity_digest != canonical_digest(identity_payload):
            raise ValueError("generation defect authority ref does not identify its host call")
        return self


class VerifiedGenerationDefectCycleV1(FrozenModel):
    """Published repairable defect plus its independently authenticated execution provenance."""

    attempt: VerifiedGenerationDefectAttemptV1
    execution_provenance: AttemptResultProvenanceV1

    @model_validator(mode="after")
    def _bind_attempt_provenance(self) -> Self:
        provenance = self.execution_provenance
        attempt_payload: JSONValue = self.attempt.model_dump(mode="json")
        if (
            provenance.attempt_key != self.attempt.defect.attempt_key
            or provenance.semantic_node_id != self.attempt.authority_identity.activation_id
            or provenance.invocation_id != self.attempt.authority_identity.invocation_id
            or provenance.graph_revision != self.attempt.authority_identity.graph_revision
            or provenance.authorization_id != self.attempt.authority_identity.authorization_id
            or provenance.activity_id != self.attempt.authority_identity.activity_id
            or provenance.output_digest != canonical_digest(attempt_payload)
            or provenance.source_terminal_receipt != self.attempt.authority_receipt
            or provenance.promotion_receipt.receipt_id
            != self.attempt.authority_identity.workspace_identity_digest
        ):
            raise ValueError("generation defect differs from authenticated execution provenance")
        return self


class ExecutionAttemptBindingV1(FrozenModel):
    """Checkpointed identity of the execution attempt selected by the current run."""

    schema_version: Literal["1"] = "1"
    invocation_id: str = Field(min_length=1)
    public_entrypoint: str = Field(min_length=1)
    semantic_node_id: Literal["execution.execute", "execution.run"]
    attempt_key: AttemptKey
    business_activation: BusinessActivation
    graph_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_id: Literal[
        "assurance.execution.task.execute.v1",
        "assurance.execution.task.run.v1",
    ]
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_id: str = Field(min_length=1)
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=0)
    validation_profile: ValidationProfile
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _bind_semantic_contract(self) -> Self:
        expected_contract = (
            "assurance.execution.task.execute.v1"
            if self.semantic_node_id == "execution.execute"
            else "assurance.execution.task.run.v1"
        )
        if self.contract_id != expected_contract:
            raise ValueError("execution attempt binding uses the wrong contract")
        return self


__all__ = [
    "ExecutionAttemptBindingV1",
    "ExecutionCycleInputV1",
    "ExecutionCycleResultV1",
    "VerifiedExecutionCycleResultV1",
    "VerifiedGenerationDefectAttemptV1",
    "VerifiedGenerationDefectCycleV1",
]
