"""Current healing topology/contract conformance (dark-ship; not compiler-activated)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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

_RECORD_TARGET = "operation:record-codegen-fix-apply"
_APPROVAL_INTERRUPT_REASON = "healing.fixer_approval"


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
    *,
    active_targets: Sequence[Literal["api", "e2e"]] | None = None,
) -> tuple[HealingConformanceIssue, ...]:
    """Return sorted structured healing issues for an in-test target topology.

    Does not load packaged resources. ``compile_packaged_workflow`` stays on the
    pre-activation path until Task 15.

    ``active_targets`` selects which record/combine hard-output expectations apply
    for API-only / E2E-only / both positive controls.
    """
    contract_map = contracts.contracts if isinstance(contracts, ExecutionContractCatalog) else dict(contracts)
    targets: tuple[Literal["api", "e2e"], ...] = (
        tuple(active_targets) if active_targets is not None else ("api", "e2e")
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

    if not _has_approval_interrupt(schema):
        issues.append(
            _issue(
                "missing_approval_interrupt",
                owner="healing.topology",
                locator="graphs.*.nodes.*.interrupt",
                detail="approved healing topology requires healing.fixer_approval interrupt",
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

    if not _has_record_join(schema):
        issues.append(
            _issue(
                "missing_record_join",
                owner="healing.topology",
                locator="graphs.*.nodes.*.join",
                detail="approved healing topology requires all_active join over record nodes",
            )
        )

    for graph_id, node_id, node in _nodes_using(schema, _RECORD_TARGET):
        missing = _missing_record_outputs(node_id, node, active_targets=targets)
        if missing:
            issues.append(
                _issue(
                    "missing_hard_outputs",
                    owner=node_id,
                    locator=f"graphs.{graph_id}.nodes.{node_id}.outputs",
                    detail=f"record node missing hard outputs: {', '.join(sorted(missing))}",
                )
            )

    for graph_id, node_id, node in _nodes_using(schema, "operation:record-fixer-approval"):
        required = {"change:healing/fixer-proposal-approval.json"}
        if not required.issubset(set(node.outputs)):
            issues.append(
                _issue(
                    "missing_hard_outputs",
                    owner=node_id,
                    locator=f"graphs.{graph_id}.nodes.{node_id}.outputs",
                    detail="record-fixer-approval must hard-output fixer-proposal-approval",
                )
            )

    for graph_id, node_id, node in _nodes_using(schema, "operation:combine-fixer-safety"):
        required = {"change:healing/fixer-safety-check.json"}
        if not required.issubset(set(node.outputs)):
            issues.append(
                _issue(
                    "missing_hard_outputs",
                    owner=node_id,
                    locator=f"graphs.{graph_id}.nodes.{node_id}.outputs",
                    detail="combine-fixer-safety must hard-output fixer-safety-check",
                )
            )

    for graph_id, node_id, node in _nodes_with_join(schema):
        if node.join is None:
            continue
        if not _join_sources_include_records(schema, graph_id, node.join.sources):
            continue
        if node.join.mode == "all":
            issues.append(
                _issue(
                    "inactive_target_required",
                    owner=node_id,
                    locator=f"graphs.{graph_id}.nodes.{node_id}.join.mode",
                    detail="record join must use all_active so inactive targets are not required",
                )
            )

    inactive_required = _inactive_target_hard_outputs(schema, active_targets=targets)
    for graph_id, node_id, output in inactive_required:
        issues.append(
            _issue(
                "inactive_target_required",
                owner=node_id,
                locator=f"graphs.{graph_id}.nodes.{node_id}.outputs",
                detail=f"inactive target hard output required: {output}",
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

    record = _RECORD_TARGET
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


def _has_approval_interrupt(schema: WorkflowSchemaV2) -> bool:
    for _graph_id, _node_id, node in _all_nodes(schema):
        interrupt = node.interrupt
        if interrupt is None:
            continue
        if interrupt.reason == _APPROVAL_INTERRUPT_REASON:
            return True
        if "approve_and_apply" in interrupt.actions:
            return True
    return False


def _has_record_join(schema: WorkflowSchemaV2) -> bool:
    for graph_id, _node_id, node in _nodes_with_join(schema):
        if node.join is None:
            continue
        if node.join.mode != "all_active":
            continue
        if _join_sources_include_records(schema, graph_id, node.join.sources):
            return True
    return False


def _join_sources_include_records(
    schema: WorkflowSchemaV2,
    graph_id: str,
    sources: Sequence[str],
) -> bool:
    graph = schema.graphs.get(graph_id)
    if graph is None:
        return False
    for source in sources:
        node = graph.nodes.get(source)
        if node is not None and node.uses == _RECORD_TARGET:
            return True
    return False


def _missing_record_outputs(
    node_id: str,
    node: NodeDef,
    *,
    active_targets: Sequence[Literal["api", "e2e"]],
) -> set[str]:
    """Return missing outputs inferred from node id / with.target / declared outputs."""
    declared = set(node.outputs)
    inferred_targets = _infer_record_targets(node_id, node, active_targets=active_targets)
    required: set[str] = set()
    for target in inferred_targets:
        required.add(f"change:healing/{target}-apply-summary.json")
        required.add(f"change:healing/{target}-fixer-safety-check.json")
    if not required:
        for target in active_targets:
            pair = {
                f"change:healing/{target}-apply-summary.json",
                f"change:healing/{target}-fixer-safety-check.json",
            }
            if pair.issubset(declared):
                return set()
        return {
            f"change:healing/{active_targets[0]}-apply-summary.json",
            f"change:healing/{active_targets[0]}-fixer-safety-check.json",
        }
    return required - declared


def _infer_record_targets(
    node_id: str,
    node: NodeDef,
    *,
    active_targets: Sequence[Literal["api", "e2e"]],
) -> list[Literal["api", "e2e"]]:
    with_target = node.with_.get("target") if node.with_ else None
    if with_target in {"api", "e2e"}:
        return [with_target]  # type: ignore[list-item]
    found: list[Literal["api", "e2e"]] = []
    for target in ("api", "e2e"):
        marker = f"/{target}-"
        if any(marker in output for output in node.outputs) or target in node_id:
            found.append(target)  # type: ignore[arg-type]
    if not found:
        for target in active_targets:
            if target in " ".join(node.outputs):
                found.append(target)
    return found


def _inactive_target_hard_outputs(
    schema: WorkflowSchemaV2,
    *,
    active_targets: Sequence[Literal["api", "e2e"]],
) -> list[tuple[str, str, str]]:
    """Detect hard outputs that require a target outside the active set."""
    active = set(active_targets)
    inactive = [target for target in ("api", "e2e") if target not in active]
    if not inactive:
        return []
    found: list[tuple[str, str, str]] = []
    for graph_id, node_id, node in _all_nodes(schema):
        for output in node.outputs:
            for target in inactive:
                if f"/{target}-" in output or output.endswith(f"/{target}-apply-summary.json"):
                    found.append((graph_id, node_id, output))
    return found


def _uses_target(schema: WorkflowSchemaV2, target: str) -> bool:
    return bool(_nodes_using(schema, target))


def _nodes_using(schema: WorkflowSchemaV2, target: str) -> list[tuple[str, str, NodeDef]]:
    found: list[tuple[str, str, NodeDef]] = []
    for graph_id, node_id, node in _all_nodes(schema):
        if node.uses == target:
            found.append((graph_id, node_id, node))
    return found


def _nodes_with_join(schema: WorkflowSchemaV2) -> list[tuple[str, str, NodeDef]]:
    return [(graph_id, node_id, node) for graph_id, node_id, node in _all_nodes(schema) if node.join]


def _all_nodes(schema: WorkflowSchemaV2) -> list[tuple[str, str, NodeDef]]:
    found: list[tuple[str, str, NodeDef]] = []
    for graph_id, graph in schema.graphs.items():
        for node_id, node in graph.nodes.items():
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
