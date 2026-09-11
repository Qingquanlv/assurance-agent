from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Literal, cast

from pydantic import BaseModel

from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_execution.graphs.state import ExecutionPublicOutput, ExecutionState
from graph_engine.attempts.resolutions import ReceiptRef
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.canonical import JSONValue, canonical_digest

_SKILL_FIELDS = (
    "change_id",
    "plan_digest",
    "plan_ref",
    "capability_leafs",
    "selected_test_families",
    "coverage_epoch",
    "repair_round",
    "generation_result",
)


def _skill_payload(state: Mapping[str, object]) -> dict[str, object]:
    return {name: state[name] for name in _SKILL_FIELDS if name in state}


def select_execute(state: Mapping[str, object]) -> ExecutionPrepareInputV1:
    return ExecutionPrepareInputV1.model_validate(_skill_payload(state))


def select_rerun(state: Mapping[str, object]) -> ExecutionPrepareInputV1:
    return ExecutionPrepareInputV1.model_validate(_skill_payload(state))


def _int(state: Mapping[str, object], key: str, default: int = 0) -> int:
    value = state.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{key} must be a non-negative int")
    return value


def activation_execute(state: Mapping[str, object]) -> BusinessActivation:
    return BusinessActivation.for_trigger(f"coverage.{_int(state, 'coverage_epoch')}.execute")


def activation_rerun(state: Mapping[str, object]) -> BusinessActivation:
    epoch = _int(state, "coverage_epoch")
    repair_round = _int(state, "repair_round", _int(state, "rounds_used"))
    return BusinessActivation.for_trigger(f"coverage.{epoch}.repair.{repair_round}.rerun")


def publish_execution(
    state: Mapping[str, object],
    output: object,
    receipt: object,
    *,
    semantic_node_id: Literal["execution.execute", "execution.run"] = "execution.execute",
) -> dict[str, object]:
    payload = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    if not isinstance(payload, Mapping):
        raise TypeError("execution output must be a mapping")
    evidence = ExecutionEvidenceV1.model_validate(payload)
    if evidence.executed_at is None:
        raise ValueError("execution evidence must bind executed_at")
    document = evidence.model_dump(mode="json")
    raw_status = "PASS" if evidence.status == "passed" else "FAIL"
    status = evidence.status
    rounds_budget = state["rounds_budget"]
    rounds_used = state["rounds_used"]
    if not isinstance(rounds_budget, int) or isinstance(rounds_budget, bool):
        raise TypeError("rounds_budget must be an int")
    if not isinstance(rounds_used, int) or isinstance(rounds_used, bool):
        raise TypeError("rounds_used must be an int")
    published: dict[str, object] = ExecutionPublicOutput(
        batch_id=evidence.batch_id,
        execution_evidence=document,
        execution_digest=canonical_digest(cast(JSONValue, document)),
        execution_semantic_node_id=semantic_node_id,
        rounds_budget=rounds_budget,
        rounds_used=rounds_used,
        status=status,
    ).model_dump(mode="json")
    raw_generation = state.get("generation_result")
    if raw_generation is not None:
        generation = GenerationCycleResultV1.model_validate(raw_generation)
        filename = "run-result.json" if semantic_node_id == "execution.run" else "execute-result.json"
        encoded = (json.dumps(evidence.model_dump(mode="json"), indent=2) + "\n").encode()
        evidence_ref = EvidenceArtifactRefV1(
            path=f"qa/results/execution/{filename}",
            digest=hashlib.sha256(encoded).hexdigest(),
        )
        cycle = ExecutionCycleResultV1(
            change_id=evidence.change_id,
            plan_digest=evidence.plan_digest,
            plan_ref=evidence.plan_ref,
            coverage_epoch=generation.coverage_epoch,
            repair_round=_int(state, "repair_round"),
            batch_id=evidence.batch_id,
            executed_at=evidence.executed_at,
            final_status=raw_status,
            evidence_ref=evidence_ref,
            mapping_ref=generation.mapping_ref,
            source_refs=generation.source_refs,
            receipt=ReceiptRef.model_validate(receipt),
        )
        published["execution_result"] = cycle.model_dump(mode="json")
    return published


def route_execution(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return "failed"
    return "committed"


def terminal_committed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


def terminal_failed(state: ExecutionState) -> dict[str, object]:
    del state
    return {}


__all__ = [
    "activation_execute",
    "activation_rerun",
    "publish_execution",
    "route_execution",
    "select_execute",
    "select_rerun",
    "terminal_committed",
    "terminal_failed",
]
