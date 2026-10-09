from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from assurance_improvement.contracts.delivery import artifact_digest
from assurance_improvement.contracts.improvements import ImprovementProjection
from graph_engine.attempts.context import AttemptExecutionContext
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.keys import BusinessActivation, derive_attempt_key
from graph_engine.attempts.resolutions import CommittedTaskResult
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.persistence.attempt_checkpoint import MemoryAttemptCheckpointStore
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore


pytestmark = pytest.mark.usefixtures("installed_sources")
_TARGET_DIGEST = "a" * 64


def _delivery_input(operation: str) -> dict[str, object]:
    export = operation == "export-change-improvement"
    projection = {
        "improvement_id": "IMP-1",
        "fingerprint": "f" * 64,
        "kind": "workflow_improvement" if export else "prompt_improvement",
        "delivery": "change_draft" if export else "memory_patch",
        "source_refs": {"problem_ids": ["PROB-1"], "occurrence_ids": ["OCC-1"]},
        "target": "schemas/workflow-schema.yaml" if export else ".aa/memory/aa-api-plan.md",
        "rationale": "gap",
        "proposed_change": "register adapters",
        "verification": {"suites": [], "required_cases": [], "success_criteria": "review"},
        "risk": "low",
        "confidence": "high",
        "state": "approved",
        "version": 1,
        "proposed_by_retro_ids": ["RET-1"],
        "last_event_id": "IMPEVT-1",
        "approval_source": "human",
    }
    receipt = {
        "eval_run_id": "eval-1",
        "outcome": "passed",
        "report_sha256": "report",
        "staged_sha256": "staged",
    }
    payload: dict[str, object] = {"projection": projection, "target_digest": _TARGET_DIGEST}
    if operation == "evaluate-memory-improvement":
        payload.update(receipt)
    elif operation == "apply-memory-improvement":
        approved_digest = artifact_digest(ImprovementProjection.model_validate(projection))
        payload.update(
            eval_receipt={
                **receipt,
                "approved_state_digest": approved_digest,
                "approved_version": 1,
            },
            approved_state_digest=approved_digest,
            approved_version=1,
            before_sha256="before",
            after_sha256="after",
            receipt_sha256="receipt",
        )
    elif operation == "rollback-memory-improvement":
        payload.update(reason="regressed", restored_sha256="restored")
    else:
        payload.update(artifact_path="changes/IMP-1.patch", sha256="export", created=True)
    return payload


@pytest.mark.parametrize(
    ("operation", "filename", "target_kind", "receipt_field", "receipt_value"),
    [
        ("evaluate-memory-improvement", "memory-eval", "memory_eval", "eval_run_id", "eval-1"),
        ("apply-memory-improvement", "memory-apply", "memory_apply", "after_sha256", "after"),
        ("rollback-memory-improvement", "memory-rollback", "memory_rollback", "restored_sha256", "restored"),
        (
            "export-change-improvement",
            "change-export",
            "change_export",
            "artifact_path",
            "changes/IMP-1.patch",
        ),
    ],
)
def test_same_delivery_business_operation_commits_in_separate_attempts(
    tmp_path: Path,
    opencode_composition,
    operation: str,
    filename: str,
    target_kind: str,
    receipt_field: str,
    receipt_value: str,
) -> None:
    asyncio.run(
        _repeat_delivery(
            tmp_path,
            opencode_composition,
            operation,
            filename,
            target_kind,
            receipt_field,
            receipt_value,
        )
    )


async def _repeat_delivery(
    tmp_path: Path,
    composition,
    operation: str,
    filename: str,
    target_kind: str,
    receipt_field: str,
    receipt_value: str,
) -> None:
    resolved = composition.semantic_attempt_contracts[f"assurance.improvement.task.{operation}"]
    validated = resolved.contract.input_model.model_validate(_delivery_input(operation))
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    revision = "b" * 64
    journal = MemoryAttemptCheckpointStore()
    kernel = AssuranceAttemptKernel(
        checkpoints=journal,
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=revision,
        validators=composition.registries.capabilities.commit_validators,
    )
    results = []
    try:
        for index in (1, 2):
            key = derive_attempt_key(
                invocation_id="delivery-repeat",
                graph_revision=revision,
                public_entrypoint="retro",
                semantic_node_id=operation,
                business_activation=BusinessActivation.for_round(index),
                contract_id=resolved.contract.contract_id,
                validated_input=validated,
            )
            result = await kernel.execute_or_recover(
                key,
                resolved,
                validated,
                AttemptExecutionContext(
                    invocation_id="delivery-repeat",
                    public_entrypoint="retro",
                    semantic_node_id=operation,
                    attempt_key=key,
                    fencing_token=1,
                ),
            )
            assert isinstance(result, CommittedTaskResult), result
            path = f"qa/results/improvement/{filename}.json"
            assert tuple(artifact.path for artifact in result.committed_artifacts) == (path,)
            record = json.loads((project / path).read_bytes())
            assert record["schema_version"] == "1"
            assert record["improvement_id"] == "IMP-1"
            assert record["version"] == 1
            assert record["target_kind"] == target_kind
            assert record["target_digest"] == _TARGET_DIGEST
            assert record["receipt"][receipt_field] == receipt_value
            results.append(result)
        assert results[0].receipt.receipt_id != results[1].receipt.receipt_id
        assert results[0].committed_artifacts == results[1].committed_artifacts
    finally:
        store.close()
