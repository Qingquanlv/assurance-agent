from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts.qa_paths import qa_route
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, ResourceClaimTemplate

from assurance_quality.contracts.assessment import AssessmentInputsV1, MaterializeAssessmentInputV1
from assurance_quality.contracts.issues import ReconcileIssuesInputV1, ReconcileIssuesResultV1
from assurance_quality.contracts.surface import SurfaceProbeInputV1, SurfaceProbeResultV1

_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return qa_route(*suffixes)


def _agent_catalog() -> tuple[
    Mapping[str, Any],
    Mapping[str, tuple[str, ...]],
]:
    from assurance_quality.ops import router

    return router.agent_contracts(), router.output_routes()


AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()
_MATERIALIZE_ASSESSMENT = TaskAttemptContract(
    contract_id="assurance.quality.materialize-assessment-inputs",
    owner_id="assurance.quality",
    handler_id="assurance.quality.materialize-assessment-inputs.execute",
    input_model=MaterializeAssessmentInputV1,
    output_model=AssessmentInputsV1,
    resources=ResourceClaimTemplate(
        parameters={
            "batch_id": "/execution/batch_id",
            "coverage_epoch": "/coverage_epoch_token",
        },
        reads=(
            ".aa/capability-catalog.json",
            ".aa/data-knowledge.yaml",
            ".aa/policy.yaml",
            "issues",
            "qa",
        ),
        writes=_paths(
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/coverage-gaps.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/metrics.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/observations.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/obligation-assessment.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/issue-evidence-manifest.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/trace-sufficiency.json",
            "inspect/epochs/{coverage_epoch}/batches/{batch_id}/trace.json",
        ),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_RECONCILE_ISSUES = TaskAttemptContract(
    contract_id="assurance.quality.reconcile-issues",
    owner_id="assurance.quality",
    handler_id="assurance.quality.reconcile-issues.execute",
    input_model=ReconcileIssuesInputV1,
    output_model=ReconcileIssuesResultV1,
    resources=ResourceClaims(
        reads=("qa",),
        writes=_paths("issues/snapshot.json"),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_SURFACE_BASELINE = TaskAttemptContract(
    contract_id="assurance.quality.surface-baseline",
    owner_id="assurance.quality",
    handler_id="assurance.quality.surface-baseline.execute",
    input_model=SurfaceProbeInputV1,
    output_model=SurfaceProbeResultV1,
    resources=ResourceClaims(
        reads=("qa",),
        writes=_paths("facts/ui-exploration.json", "facts/api-discovery.json"),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        "materialize-assessment-inputs": _MATERIALIZE_ASSESSMENT,
        "reconcile-issues": _RECONCILE_ISSUES,
        "surface-baseline": _SURFACE_BASELINE,
    }
)


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in (*AGENT_JOB_CONTRACTS.values(), *TASK_ATTEMPT_CONTRACTS.values())
            ),
            key=lambda item: item.contract_id,
        )
    )


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
