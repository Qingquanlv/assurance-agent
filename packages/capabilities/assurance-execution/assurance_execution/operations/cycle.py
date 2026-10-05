"""Seal one execution attempt from its input and evidence. The receipt stays outside."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast

from agent_runtime_contracts.ops import OutputError
from graph_engine.canonical import JSONValue, canonical_digest

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import (
    ExecutionAttemptOutputV1,
    ExecutionCycleDocumentV1,
    execution_evidence_path,
    execution_node_for_kind,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1


def seal_execution(
    evidence: ExecutionEvidenceV1,
    *,
    change_id: str,
    coverage_epoch: int,
    repair_round: int,
    execution_kind: Literal["execute", "run"],
    generation: GenerationCycleResultV1 | None,
) -> ExecutionAttemptOutputV1:
    if evidence.executed_at is None:
        raise OutputError("execution evidence must bind executed_at")
    document = evidence.model_dump(mode="json")
    semantic = execution_node_for_kind(execution_kind)
    families = tuple(item.model_dump(mode="json") for item in evidence.family_outcomes)
    execution_result: ExecutionCycleDocumentV1 | None = None
    if generation is not None:
        encoded = (json.dumps(document, indent=2) + "\n").encode()
        evidence_ref = EvidenceArtifactRefV1(
            path=execution_evidence_path(semantic),
            digest=hashlib.sha256(encoded).hexdigest(),
        )
        final_status: Literal["PASS", "FAIL"] = "PASS" if evidence.status == "passed" else "FAIL"
        execution_result = ExecutionCycleDocumentV1(
            change_id=evidence.change_id,
            plan_digest=evidence.plan_digest,
            plan_ref=evidence.plan_ref,
            coverage_epoch=generation.coverage_epoch,
            repair_round=repair_round,
            batch_id=evidence.batch_id,
            executed_at=evidence.executed_at,
            final_status=final_status,
            evidence_ref=evidence_ref,
            observations_ref=evidence.observations_ref,
            mapping_ref=generation.mapping_ref,
            source_refs=generation.source_refs,
            family_outcomes=evidence.family_outcomes,
        )
    admission: Literal["committed", "failed"] = "failed"
    if execution_result is not None and (
        execution_result.change_id == change_id
        and execution_result.coverage_epoch == coverage_epoch
        and execution_result.final_status in {"PASS", "FAIL"}
    ):
        admission = "committed"
    return ExecutionAttemptOutputV1(
        admission=admission,
        batch_id=evidence.batch_id,
        execution_evidence=document,
        execution_digest=canonical_digest(cast(JSONValue, document)),
        execution_semantic_node_id=semantic,
        family_outcomes=families,
        execution_result=execution_result,
    )
