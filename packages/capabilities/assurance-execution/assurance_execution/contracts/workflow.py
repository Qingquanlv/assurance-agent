"""Versioned output for one completed execution or rerun batch."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic.types import AwareDatetime

from graph_engine.attempts.models.resolutions import ReceiptRef
from graph_engine.plugin_api import FrozenModel

from assurance_execution.contracts.evidence import FamilyExecutionOutcomeV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


EXECUTION_CYCLE_PATH = "qa/results/execution/execution-cycle.json"
APPLIED_REPAIR_PATH = "qa/results/healing/applied-repair.json"

ExecutionSemanticNodeId = Literal["execution.execute", "execution.run"]

EXECUTE_EVIDENCE_PATH = "qa/results/execution/execute-result.json"
RUN_EVIDENCE_PATH = "qa/results/execution/run-result.json"

EXECUTION_EVIDENCE_PATHS: Mapping[ExecutionSemanticNodeId, str] = MappingProxyType(
    {
        "execution.execute": EXECUTE_EVIDENCE_PATH,
        "execution.run": RUN_EVIDENCE_PATH,
    }
)


def execution_evidence_path(node: ExecutionSemanticNodeId) -> str:
    return EXECUTION_EVIDENCE_PATHS[node]


def execution_node_for_kind(kind: Literal["execute", "run"]) -> ExecutionSemanticNodeId:
    return "execution.run" if kind == "run" else "execution.execute"


def execution_semantic_node(path: str) -> ExecutionSemanticNodeId:
    for node, evidence_path in EXECUTION_EVIDENCE_PATHS.items():
        if evidence_path == path:
            return node
    raise ValueError(f"execution evidence path is not bound: {path}")


class ExecutionCycleDocumentV1(FrozenModel):
    """Sealed execution cycle. This attempt's commit receipt stays on the ledger."""

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
    observations_ref: EvidenceArtifactRefV1 | None = None
    source_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    family_outcomes: tuple[FamilyExecutionOutcomeV1, ...]

    @model_validator(mode="after")
    def _paths_match_change(self) -> Self:
        prefix = "qa/"
        if not self.evidence_ref.path.startswith(prefix):
            raise ValueError("execution evidence must belong to the current change")
        if not self.mapping_ref.path.startswith(prefix):
            raise ValueError("execution mapping must belong to the current change")
        if self.observations_ref is not None and not self.observations_ref.path.startswith(prefix):
            raise ValueError("runtime observations must belong to the current change")
        if any(not item.path.startswith(prefix) for item in self.source_refs):
            raise ValueError("execution sources must belong to the current change")
        return self


class ExecutionCycleResultV1(ExecutionCycleDocumentV1):
    receipt: ReceiptRef


class ExecutionAttemptOutputV1(FrozenModel):
    """One run's public fields. The commit receipt is exported beside this document."""

    admission: Literal["committed", "failed"]
    batch_id: str
    execution_evidence: dict[str, object]
    execution_digest: str
    execution_semantic_node_id: Literal["execution.execute", "execution.run"]
    family_outcomes: tuple[dict[str, object], ...]
    execution_result: ExecutionCycleDocumentV1 | None = None


class ExecutionCycleInputV1(FrozenModel):
    generation: GenerationCycleResultV1
    repair_round: int = Field(default=0, ge=0)


class AppliedRepairHandoffV1(FrozenModel):
    """Healing's applied repair, written for rerun. The commit receipt stays on the ledger."""

    schema_version: Literal["1"] = "1"
    change_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ref: EvidenceArtifactRefV1
    coverage_epoch: int = Field(ge=0)
    repair_round: int = Field(ge=1)
    changed_test_refs: tuple[EvidenceArtifactRefV1, ...] = Field(min_length=1)
    mapping_ref: EvidenceArtifactRefV1


__all__ = [
    "APPLIED_REPAIR_PATH",
    "EXECUTION_CYCLE_PATH",
    "EXECUTION_EVIDENCE_PATHS",
    "EXECUTE_EVIDENCE_PATH",
    "RUN_EVIDENCE_PATH",
    "AppliedRepairHandoffV1",
    "ExecutionAttemptOutputV1",
    "ExecutionCycleDocumentV1",
    "ExecutionCycleInputV1",
    "ExecutionCycleResultV1",
    "ExecutionSemanticNodeId",
    "execution_evidence_path",
    "execution_node_for_kind",
    "execution_semantic_node",
]
