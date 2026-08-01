"""Current healing topology/contract conformance (dark-ship; not compiler-activated)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from assurance_agent.workflow.graph.contracts import ExecutionContract, ExecutionContractCatalog
from assurance_agent.workflow.graph.durable_effects import (
    FIXER_PROPOSAL_APPROVED_V1,
    HEAL_RECORD_APPLY_V2,
    HEALING_ALLOCATION_V2,
)
from assurance_agent.workflow.graph.precommit import CODEGEN_FIX_CANDIDATE_V1
from assurance_agent.workflow.graph.schema_v2 import NodeDef, WorkflowSchemaV2

HealingConformanceCode = Literal[
    "missing_allocate_outputs",
    "missing_authority_ready_gate",
    "missing_approval_interrupt",
    "missing_dispatch_single_pass",
    "missing_record_join",
    "missing_aggregate_safety",
    "wrong_validator_binding",
    "wrong_durable_effect_binding",
    "missing_hard_outputs",
    "inactive_target_required",
]


@dataclass(frozen=True, slots=True)
class HealingConformanceIssue:
    category: Literal["healing_conformance"]
    code: HealingConformanceCode
    owner: str
    locator: str
    detail: str


def find_current_healing_conformance_issues(
    schema: WorkflowSchemaV2,
    contracts: ExecutionContractCatalog | Mapping[str, ExecutionContract],
) -> tuple[HealingConformanceIssue, ...]:
    """Return sorted structured healing issues for an in-test target topology.

    Does not load packaged resources. ``compile_packaged_workflow`` stays on the
    pre-activation path until Task 15.
    """
    contract_map = (
        contracts.contracts if isinstance(contracts, ExecutionContractCatalog) else dict(contracts)
    )
    issues: list[HealingConformanceIssue] = []

    allocate_nodes = _nodes_using(schema, "operation:allocate-healing-attempt")
    for graph_id, node_id, node in allocate_nodes:
        required = {
            "change:healing/entry-baseline.json",
            "change:healing/fixer-authority.json",
        }
        if not required.issubset(set(node.outputs)):
            issues.append(
                _issue(
                    "missing_allocate_outputs",
                    owner=node_id,
                    locator=f"graphs.{graph_id}.nodes.{node_id}.outputs",
                    detail="allocate must hard-output entry-baseline and fixer-authority",
                )
            )
    allocate_contract = contract_map.get("operation:allocate-healing-attempt")
    if allocate_contract is not None and HEALING_ALLOCATION_V2 not in allocate_contract.durable_effects:
        issues.append(
            _issue(
                "wrong_durable_effect_binding",
                owner="operation:allocate-healing-attempt",
                locator="contracts.operation:allocate-healing-attempt.durable_effects",
                detail="allocate must declare healing_allocation/v2",
            )
        )

    if not _uses_target(schema, "operation:fixer-authority-ready"):
        issues.append(
            _issue(
                "missing_authority_ready_gate",
                owner="healing.topology",
                locator="graphs.*.nodes",
                detail="approved healing topology requires fixer-authority-ready",
            )
        )

    if not _uses_target(schema, "operation:fixer-dispatch"):
        issues.append(
            _issue(
                "missing_dispatch_single_pass",
                owner="healing.topology",
                locator="graphs.*.nodes",
                detail="approved healing topology requires fixer-dispatch",
            )
        )

    for fixer in ("skill:aa-api-codegen-fixer", "skill:aa-e2e-codegen-fixer"):
        contract = contract_map.get(fixer)
        if contract is not None and contract.precommit_validator != CODEGEN_FIX_CANDIDATE_V1:
            issues.append(
                _issue(
                    "wrong_validator_binding",
                    owner=fixer,
                    locator=f"contracts.{fixer}.precommit_validator",
                    detail="codegen-fixer contracts must select codegen_fix_candidate/v1",
                )
            )

    record = "operation:record-codegen-fix-apply"
    record_contract = contract_map.get(record)
    if record_contract is not None and HEAL_RECORD_APPLY_V2 not in record_contract.durable_effects:
        issues.append(
            _issue(
                "wrong_durable_effect_binding",
                owner=record,
                locator=f"contracts.{record}.durable_effects",
                detail="record-codegen-fix-apply must declare heal_record_apply/v2",
            )
        )

    approval = contract_map.get("operation:record-fixer-approval")
    if approval is not None and FIXER_PROPOSAL_APPROVED_V1 not in approval.durable_effects:
        issues.append(
            _issue(
                "wrong_durable_effect_binding",
                owner="operation:record-fixer-approval",
                locator="contracts.operation:record-fixer-approval.durable_effects",
                detail="record-fixer-approval must declare fixer_proposal_approved/v1",
            )
        )

    if not _uses_target(schema, "operation:combine-fixer-safety"):
        issues.append(
            _issue(
                "missing_aggregate_safety",
                owner="healing.topology",
                locator="graphs.*.nodes",
                detail="approved healing topology requires combine-fixer-safety",
            )
        )

    return tuple(
        sorted(
            issues,
            key=lambda issue: (issue.code, issue.owner, issue.locator, issue.detail),
        )
    )


def _uses_target(schema: WorkflowSchemaV2, target: str) -> bool:
    return bool(_nodes_using(schema, target))


def _nodes_using(schema: WorkflowSchemaV2, target: str) -> list[tuple[str, str, NodeDef]]:
    found: list[tuple[str, str, NodeDef]] = []
    for graph_id, graph in schema.graphs.items():
        for node_id, node in graph.nodes.items():
            if node.uses == target:
                found.append((graph_id, node_id, node))
    return found


def _issue(
    code: HealingConformanceCode,
    *,
    owner: str,
    locator: str,
    detail: str,
) -> HealingConformanceIssue:
    return HealingConformanceIssue(
        category="healing_conformance",
        code=code,
        owner=owner,
        locator=locator,
        detail=detail,
    )


__all__ = [
    "HealingConformanceCode",
    "HealingConformanceIssue",
    "find_current_healing_conformance_issues",
]
