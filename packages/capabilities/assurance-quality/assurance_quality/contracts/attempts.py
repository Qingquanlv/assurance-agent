from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts.qa_paths import qa_join, qa_route
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, ResourceClaimTemplate

from graph_engine.stategraph.ledger import NamedWrite

from assurance_quality.contracts.assessment import (
    ASSESSMENT_INPUTS_PATH,
    AssessmentInputsV1,
    MaterializeAssessmentBoundV1,
)
from assurance_quality.contracts.agent import ReconcileBoundInputV1
from assurance_quality.contracts.issues import ReconcileIssuesResultV1
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


_MATERIALIZE_ASSESSMENT = TaskAttemptContract(
    contract_id="assurance.quality.materialize-assessment-inputs",
    owner_id="assurance.quality",
    handler_id="assurance.quality.materialize-assessment-inputs.execute",
    input_model=MaterializeAssessmentBoundV1,
    output_model=AssessmentInputsV1,
    writes=(NamedWrite("assessment", ASSESSMENT_INPUTS_PATH),),
    resources=ResourceClaimTemplate(
        parameters={
            "repair_round": "/repair_round_token",
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
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/coverage-gaps.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/metrics.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/observations.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/obligation-assessment.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/issue-evidence-manifest.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/trace-sufficiency.json",
            "inspect/epochs/{coverage_epoch}/rounds/{repair_round}/trace.json",
            "inspect/assessment-inputs.json",
        ),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_SNAPSHOT_PATH = qa_join("issues/snapshot.json")
_UI_EXPLORATION_PATH = qa_join("facts/ui-exploration.json")
_API_DISCOVERY_PATH = qa_join("facts/api-discovery.json")
_RECONCILE_ISSUES = TaskAttemptContract(
    contract_id="assurance.quality.reconcile-issues",
    owner_id="assurance.quality",
    handler_id="assurance.quality.reconcile-issues.execute",
    input_model=ReconcileBoundInputV1,
    output_model=ReconcileIssuesResultV1,
    writes=(NamedWrite("snapshot", _SNAPSHOT_PATH),),
    resources=ResourceClaims(
        reads=("qa",),
        writes=(_SNAPSHOT_PATH,),
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
    writes=(
        NamedWrite("ui-exploration", _UI_EXPLORATION_PATH),
        NamedWrite("api-discovery", _API_DISCOVERY_PATH),
    ),
    resources=ResourceClaims(
        reads=("qa",),
        writes=qa_route("facts/ui-exploration.json", "facts/api-discovery.json"),
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
AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()


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
